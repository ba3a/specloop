#!/usr/bin/env python
"""specloop: a spec-driven development state machine for Claude Code, driven by hooks.

Hook entry points (read hook JSON from stdin):
    engine.py context <SessionStart|UserPromptSubmit>
    engine.py pretool
    engine.py stop

Manual commands (used by slash commands):
    engine.py init <spec-path>   load a specification, status -> ready
    engine.py init-feature <spec-path>
                                 load a specification in feature mode (multi-commit workflow)
    engine.py go                 start (or resume) the workflow
    engine.py pause              stop driving the session, keep the phase
    engine.py advance            force-pass the current phase
    engine.py status             print the current state
    engine.py reset              delete state and run artifacts
    engine.py update [check|skip|never]
                                 check for kit updates now and report, skip or silence them
    engine.py update-check       background update check (run detached; prints nothing)

Configuration is layered, later layers winning in a deep merge:
    defaults.json      shipped with the kit, replaced on update
    config.json        owned by the project and shared through git, never touched by updates
    config.local.json  personal, per person and git-ignored, optional; an invalid file is
                       ignored with a warning on stderr
Phase instructions are read from overrides/<phase>.md first, then phases/<phase>.md.
Set SPECLOOP_DISABLED=1 to turn every hook into a no-op.
"""

import collections
import contextlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from datetime import datetime, timezone

BASE_DIR = os.path.join(".claude", "specloop")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DONE = "done"
CHECKLIST_ITEM = re.compile(r"^\s*- \[[ x]\]", re.IGNORECASE | re.MULTILINE)
STEP_STATUSES = ("pending", "active", "done", "empty")
LOCKED_STATUSES = ("done", "empty")
COMMIT_TYPES = ("feat", "fix", "refactor", "test", "docs", "chore", "perf", "build", "ci", "style")
MAX_TITLE_LENGTH = 72
FAILURE_FILE = "last-failure.md"
COMMIT = "commit"
REPLAN_FILE = "replan.md"
REVIEW_FILE = "review.md"
LATE_REVIEW_FILE = "late-review.md"
SEAL_FILE = "sealed.json"
ATTEMPTS_DIR = "attempts"
MAX_SLUG_LENGTH = 50
STATE_LOCK = "state.lock"
STATE_LOCK_STALE_SEC = 30
LOCK_WAIT_SEC = 5
LOCK_POLL_SEC = 0.05
FILE_RETRY_SEC = 1
FILE_RETRY_POLL_SEC = 0.005
LOAD_RETRY_SEC = 0.25
CLAIMABLE_STATUSES = ("ready", "active", "paused", "stalled")
RUNNING_STATUSES = ("active", "paused", "stalled")
OWNER_ONLY_COMMANDS = ("advance", "reset")
IN_FLIGHT_TASKS = ("subagent", "workflow", "teammate")
COMMIT_PENDING = object()
STALE_PATHS = ("{run_dir}/" + REVIEW_FILE, "{run_dir}/" + REPLAN_FILE)


class StateCorrupt(Exception):
    """state.json exists but stayed unreadable for LOAD_RETRY_SEC."""


class LockBusy(Exception):
    """A lock was still taken after the wait."""


class Unchanged(Exception):
    """Raised inside update_state: leave the state as it is and return `value`."""

    def __init__(self, value=None):
        super().__init__(value)
        self.value = value


def project_dir(hook_input=None):
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        return os.environ["CLAUDE_PROJECT_DIR"]
    if hook_input and hook_input.get("cwd"):
        return hook_input["cwd"]
    return os.getcwd()


def _path(root, *parts):
    return os.path.join(root, BASE_DIR, *parts)


def _rel(*parts):
    return "/".join((BASE_DIR.replace("\\", "/"),) + parts)


def deep_merge(base, override):
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _read_local_config(root):
    """Read config.local.json; a missing, unreadable or invalid file counts as {} (with a warning)."""
    try:
        with open(_path(root, "config.local.json"), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        reason = str(e).splitlines()[0] if str(e) else e.__class__.__name__
        sys.stderr.write("[specloop] ignoring %s: %s\n" % (_rel("config.local.json"), reason))
        return {}
    if not isinstance(data, dict):
        sys.stderr.write("[specloop] ignoring %s: top level is not a JSON object\n" % _rel("config.local.json"))
        return {}
    return data


def load_config(root):
    config = deep_merge(_read_json(_path(root, "defaults.json")), _read_json(_path(root, "config.json")))
    config = deep_merge(config, _read_local_config(root))
    # Phases explicitly set to null in config.json or config.local.json are removed.
    config["phases"] = {k: v for k, v in config.get("phases", {}).items() if v}
    if not isinstance(config.get("feature"), dict):
        config["feature"] = {}
    feature = config["feature"]
    feature["planPhases"] = {k: v for k, v in (feature.get("planPhases") or {}).items() if v}
    steps = {k: v for k, v in (feature.get("stepPhases") or {}).items() if v}
    for name, phase in steps.items():
        # A step phase without a check reuses the top-level phase's check (the project's test command).
        if not phase.get("check") and config["phases"].get(name, {}).get("check"):
            steps[name] = dict(phase, check=config["phases"][name]["check"])
    feature["stepPhases"] = steps
    return config


def is_feature(state):
    return (state or {}).get("mode", "single") == "feature"


def in_step(state):
    return is_feature(state) and state.get("stepId") is not None


def phase_graph(config, state):
    """The phase graph the current run uses: `phases` in single mode; in feature mode
    `feature.planPhases` while planning and `feature.stepPhases` while a step is active."""
    if not is_feature(state):
        return config["phases"]
    if in_step(state) and not in_replan(config, state):
        return config["feature"]["stepPhases"]
    return config["feature"]["planPhases"]


def in_replan(config, state):
    """True while a feature run is in the replan phase (a step is still recorded as current)."""
    return in_step(state) and state.get("phase") == config["feature"].get("replanPhase", "feature-replan")


def current_step(state):
    """Return (index, step) of the active step in approvedSteps, or (None, None)."""
    for index, step in enumerate((state.get("approvedSteps") or {}).get("steps", [])):
        if step.get("id") == state.get("stepId"):
            return index, step
    return None, None


def step_values(state):
    """Placeholder values for the active step; empty when no step is active."""
    index, step = current_step(state) if in_step(state) else (None, None)
    if step is None:
        return {}
    return {
        "step_index": str(index + 1),
        "step_count": str(len(state["approvedSteps"]["steps"])),
        "step_id": str(step["id"]),
        "step_title": step["title"],
        "step_description": step["description"],
        "step_acceptance": "\n".join("- " + a for a in step["acceptance"]),
        "steps_file": _rel("run", "steps.json"),
        "type": step.get("type", "feat"),
    }


def attempt_key(state):
    """Name of the current attempt's directory: <step id or "single">-<attempt>."""
    return "%s-%d" % (state["stepId"] if in_step(state) else "single", state.get("attempt", 0))


def attempt_path(root, state):
    return os.path.join(run_dir(root), ATTEMPTS_DIR, attempt_key(state))


def replan_path(root, state):
    return os.path.join(attempt_path(root, state), REPLAN_FILE)


def _retry_locked(action, *args):
    """Run a file operation, retrying PermissionError: on Windows another process may have the file open."""
    deadline = time.monotonic() + FILE_RETRY_SEC
    while True:
        try:
            return action(*args)
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(FILE_RETRY_POLL_SEC)


def atomic_write(path, text):
    """Replace `path` in one step, so that a reader sees the old or the new content and never a partial file."""
    temp = "%s.%s.tmp" % (path, uuid.uuid4().hex)
    try:
        with open(temp, "x", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _retry_locked(os.replace, temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)
        raise


def load_state(root):
    """Return the state, None when there is none. A writer's replace can fail a read for a moment, so retry."""
    deadline = time.monotonic() + LOAD_RETRY_SEC
    while True:
        try:
            with open(_path(root, "state.json"), encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                raise ValueError("the top level must be an object")
            return state
        except FileNotFoundError:
            return None
        except (ValueError, PermissionError) as e:
            if time.monotonic() >= deadline:
                raise StateCorrupt(str(e))
            time.sleep(LOCK_POLL_SEC / 2)


def save_state(root, state):
    atomic_write(_path(root, "state.json"), json.dumps(state, indent=2))


class FileLock:
    """A lock file holding a random token and the time it was taken."""

    def __init__(self, path, stale_after, wait=None):
        self.path = path
        self.stale_after = stale_after
        self.wait = LOCK_WAIT_SEC if wait is None else wait
        self.token = uuid.uuid4().hex

    def __enter__(self):
        """Take the lock, waiting up to `wait` seconds (default LOCK_WAIT_SEC); a stale lock is replaced. 0 tries once, then gives up."""
        deadline = time.monotonic() + self.wait
        while True:
            if self._try_create():
                return self
            self._remove_if_stale()
            if self._try_create():
                return self
            if time.monotonic() >= deadline:
                raise LockBusy(self.path)
            time.sleep(LOCK_POLL_SEC)

    def __exit__(self, *exc):
        self.release()

    def _try_create(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):  # Windows: PermissionError while a delete is pending
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"token": self.token, "at": time.time()}, f)
        return True

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _is_stale(self, path):
        try:
            taken_at = float(self._read(path)["at"])
        except (OSError, ValueError, KeyError, TypeError):
            # Being created, or not one of ours: go by the file's age.
            try:
                taken_at = os.path.getmtime(path)
            except OSError:
                return False
        return time.time() - taken_at > self.stale_after

    def _remove_if_stale(self):
        """Rename a stale lock to a unique name first: of several processes, only one rename can succeed."""
        if not self._is_stale(self.path):
            return
        aside = "%s.%s.stale" % (self.path, uuid.uuid4().hex)
        try:
            os.rename(self.path, aside)
        except OSError:
            return
        try:
            if not self._is_stale(aside):
                # Another process replaced the stale lock just before this rename: give the new one back.
                # A link never overwrites a lock taken in the meantime.
                os.link(aside, self.path)
        except OSError:
            pass
        try:
            _retry_locked(os.remove, aside)
        except OSError:
            pass

    def release(self):
        """Remove the lock file, unless it has become another process's lock."""
        try:
            if self._read(self.path).get("token") == self.token:
                _retry_locked(os.remove, self.path)
        except (OSError, ValueError, AttributeError):
            pass


def update_state(root, fn):
    """Every state write goes through here: lock, reload, apply `fn`, raise `rev`, save, unlock.

    Return fn's result. `fn` gets an empty dict when there is no state and writes nothing by raising Unchanged.
    Never call it around a phase check or a git call: the lock is meant to be held for milliseconds."""
    with FileLock(_path(root, STATE_LOCK), STATE_LOCK_STALE_SEC):
        state = load_state(root) or {}
        try:
            result = fn(state)
        except Unchanged as e:
            return e.value
        if state:
            state["rev"] = int(state.get("rev", 0)) + 1
            save_state(root, state)
        return result


def is_owner(state, hook_input):
    """True for the chat that drives the workflow. With no owner or no session id, every chat does."""
    owner = (state or {}).get("owner")
    session = (hook_input or {}).get("session_id")
    return not owner or not session or owner.get("session") == session


def run_dir(root):
    return _path(root, "run")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def seconds_since(stamp):
    """Age of a now() timestamp; a missing or malformed one counts as very old."""
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def add_history(state, session, **entry):
    """Append a history entry; entries written by a hook name the session."""
    entry["at"] = now()
    if session:
        entry["session"] = session[:8]
    state.setdefault("history", []).append(entry)


def cmd_name(config, name):
    return "/" + config.get("commandPrefix", "") + name


# --- bounded subprocesses ------------------------------------------------------------------

RunResult = collections.namedtuple("RunResult", "returncode stdout stderr timed_out")
KILL_WAIT_SEC = 5  # How long to wait for a killed process to be reaped.


def _kill_process_tree(proc):
    """Kill proc and every process it started. Never raises."""
    try:
        if os.name == "nt":
            # /T needs the parent alive to find its children, so kill the tree before the parent.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def run_limited(args, timeout, cwd=None, env=None, merge_stderr=False):
    """Run a command for at most timeout seconds. Return RunResult(returncode, stdout, stderr, timed_out)
    with the output as bytes; with merge_stderr the output holds both streams and stderr is empty.

    The output goes to temporary files, not pipes, so a descendant that outlives the command and keeps
    the streams open cannot block the call. On timeout the whole process tree is killed (a new session
    and killpg on POSIX, taskkill /T on Windows) and returncode is None. stdin is the null device.
    Raises OSError when the executable cannot be started."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        kwargs = {} if os.name == "nt" else {"start_new_session": True}
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT if merge_stderr else err, **kwargs)
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(proc)
            try:
                proc.wait(timeout=KILL_WAIT_SEC)
            except Exception:
                pass
        streams = []
        for stream in (out, err):
            stream.seek(0)
            streams.append(stream.read())
        return RunResult(None if timed_out else proc.returncode, streams[0], streams[1], timed_out)


# --- Update checker building blocks -------------------------------------------------------

UPDATE_FILE = "update.json"
UPDATE_MODES = ("off", "notify", "auto")
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_version(text):
    """Parse 'MAJOR.MINOR.PATCH' (surrounding whitespace allowed) into a tuple; None otherwise."""
    if not isinstance(text, str):
        return None
    match = VERSION_PATTERN.match(text.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_major_update(installed, available):
    """True when `available` has a higher major number, or (while the major number is 0) a higher minor."""
    old, new = parse_version(installed), parse_version(available)
    if old is None or new is None:
        return False
    if new[0] != old[0]:
        return new[0] > old[0]
    return new[0] == 0 and new[1] > old[1]


def _read_json_tolerant(path):
    """Read a JSON object; a missing, unreadable or invalid file, or a non-object, gives {}."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_manifest_source(root):
    """The `source` recorded by the installer in manifest.json, or None (old install or no manifest)."""
    source = _read_json_tolerant(_path(root, "manifest.json")).get("source")
    return source if isinstance(source, dict) else None


def load_update_state(root):
    """The update checker's semaphore file, update.json; {} when missing or unreadable."""
    return _read_json_tolerant(_path(root, UPDATE_FILE))


def save_update_state(root, data):
    os.makedirs(_path(root), exist_ok=True)
    atomic_write(_path(root, UPDATE_FILE), json.dumps(data, indent=2) + "\n")


def merge_update_state(root, **values):
    """Write `values` into update.json, reloaded just now: keys written by others meanwhile stay."""
    data = load_update_state(root)
    data.update(values)
    save_update_state(root, data)
    return data


def update_mode(config):
    """The effective update mode: 'off' in CI or with SPECLOOP_DISABLED=1, else update.mode
    ('off', 'notify' or 'auto'; anything else counts as 'notify')."""
    if os.environ.get("CI") or os.environ.get("SPECLOOP_DISABLED") == "1":
        return "off"
    update = config.get("update")
    mode = update.get("mode") if isinstance(update, dict) else None
    return mode if mode in UPDATE_MODES else "notify"


def _read_version_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, ValueError):
        return None


def available_version(root, source, update_state):
    """The highest of the clone's VERSION and the last recorded remote version, if it is newer
    than the installed VERSION and newer than a skipped version; None otherwise."""
    installed = parse_version(_read_version_file(_path(root, "VERSION")))
    if installed is None:
        return None
    floor = installed
    skipped = parse_version((update_state or {}).get("skipped"))
    if skipped is not None and skipped > floor:
        floor = skipped
    candidates = [(update_state or {}).get("remote")]
    if source and source.get("path"):
        candidates.append(_read_version_file(os.path.join(source["path"], "VERSION")))
    best = None
    for text in candidates:
        version = parse_version(text)
        if version is not None and version > floor and (best is None or version > best):
            best = version
    return "%d.%d.%d" % best if best else None


def display_url(url):
    """Shorten a remote URL for display: drop the scheme, credentials, the 'user@host:' form
    and a trailing '.git', e.g. 'git@github.com:a/b.git' -> 'github.com/a/b'."""
    text = (url or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
        host, sep, rest = text.partition("/")
        text = host.rsplit("@", 1)[-1] + sep + rest
    else:
        scp = re.match(r"^[^/@]+@([^/:]+):(.*)$", text)
        if scp:
            text = scp.group(1) + "/" + scp.group(2)
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return text


# --- Background update check ----------------------------------------------------------------

GIT = "git"  # The git executable used by the update checker; tests point it elsewhere.
UPDATE_LOCK = "update.lock"
DEFAULT_UPDATE_TIMEOUT = 20
DEFAULT_UPDATE_INTERVAL = 24
REINSTALL_HINT = "reinstall"  # announced.version for the old-install hint, which names no version.


def update_timeout(config):
    """update.timeoutSec as a positive number; the default when it is missing or invalid."""
    update = config.get("update") if isinstance(config, dict) else None
    value = update.get("timeoutSec") if isinstance(update, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_UPDATE_TIMEOUT


def update_git(path, timeout, *args):
    """Run `git -C <path> <args>` for the update checker with a timeout.
    Return (ok, output, error): output is stripped stdout, error a short message on failure."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        proc = run_limited([GIT, "-C", path] + list(args), timeout, env=env)
    except OSError as e:
        return False, "", "git could not be run: %s" % e
    if proc.timed_out:
        return False, "", "git %s timed out after %ss" % (args[0], timeout)
    output = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        message = "git %s failed" % args[0]
        return False, output, message + (": " + detail[-1] if detail else "")
    return True, output, None


def _run_update_check(root, config):
    """Fetch the recorded source's upstream and record the result in update.json. Only the keys
    the check owns are written, into the file as it is after the network calls."""
    timeout = update_timeout(config)
    result = dict(checkedAt=now(), installed=_read_version_file(_path(root, "VERSION")), clone=None)
    source = read_manifest_source(root)
    error = None
    if source is None:
        error = "no source recorded in manifest.json; reinstall with install.py to enable update checks"
    elif source.get("checkable") is False:
        error = "update checks unavailable: %s" % (source.get("reason") or "source is not checkable")
    elif not all(source.get(key) and isinstance(source.get(key), str) for key in ("path", "remote", "branch")):
        error = "the recorded source has no path, remote or branch"
    elif not os.path.isdir(source["path"]):
        error = "clone not found: %s" % source["path"]
    if error is None:
        path, ref = source["path"], "%s/%s" % (source["remote"], source["branch"])
        result["clone"] = _read_version_file(os.path.join(path, "VERSION"))
        ok, _, error = update_git(path, timeout, "fetch", "--quiet", source["remote"], source["branch"])
        if ok:
            ok, remote_version, error = update_git(path, timeout, "show", ref + ":VERSION")
        if ok:
            ok, remote_commit, error = update_git(path, timeout, "rev-parse", ref)
        if ok and parse_version(remote_version) is None:
            error = "remote VERSION is not a MAJOR.MINOR.PATCH version: %r" % remote_version[:40]
        elif ok:
            result.update(remote=remote_version, remoteCommit=remote_commit)
    result["error"] = error
    merge_update_state(root, **result)


def run_update_check_locked(root, config):
    """Run the network check under update.lock. Return False, doing nothing, when a fresh lock
    shows that another check is running; True after the check ran."""
    os.makedirs(_path(root), exist_ok=True)
    try:
        with FileLock(_path(root, UPDATE_LOCK), 2 * update_timeout(config), wait=0):
            _run_update_check(root, config)
    except LockBusy:
        return False
    return True


def cmd_update_check(root):
    """engine.py update-check: the network check, meant to run detached. Never raises, prints nothing.
    A lock younger than 2 x timeoutSec belongs to a running check: exit at once."""
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            try:
                config = load_config(root)
            except Exception:
                config = {}
            run_update_check_locked(root, config)
        except Exception:
            pass


def spawn_update_check(root):
    """Start `engine.py update-check` for root as a detached process. Return True when started."""
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "update-check"], cwd=root,
                         env=dict(os.environ, CLAUDE_PROJECT_DIR=root), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kwargs)
        return True
    except Exception as e:
        print("[specloop] could not start the update check: %s" % e, file=sys.stderr)
        return False


def is_busy(state):
    """True while a workflow must not be disturbed: a state exists and its status is not done."""
    return bool(state) and state.get("status") != DONE


def update_interval(config):
    """update.intervalHours as a non-negative number; the default when it is missing or invalid."""
    update = config.get("update") if isinstance(config, dict) else None
    value = update.get("intervalHours") if isinstance(update, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return value
    return DEFAULT_UPDATE_INTERVAL


def interval_elapsed(stamp, hours):
    """False only when the ISO time `stamp` lies less than `hours` in the past. A missing or
    invalid time counts as elapsed, and so does one in the future (a clock that moved back)."""
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return True
    return not 0 <= age < hours * 3600


def _announce_once(root, config, version):
    """Record in update.json that `version` is being announced. Return False, recording nothing,
    when the same version was already announced within intervalHours. Reads update.json afresh,
    so a key written by another process since the caller's read stays."""
    announced = load_update_state(root).get("announced")
    if (isinstance(announced, dict) and announced.get("version") == version
            and not interval_elapsed(announced.get("at"), update_interval(config))):
        return False
    merge_update_state(root, announced={"version": version, "at": now()})
    return True


def update_notice_text(root, config, source, version):
    """The message that tells the user `version` is available from `source`."""
    origin = " ".join(part for part in (display_url(source.get("url")) or source.get("path"),
                                        source.get("branch")) if part)
    update = cmd_name(config, "update")
    return ("[specloop] %s is available (installed %s, from %s). Run %s to update, %s skip to skip this "
            "version, or %s never to turn checks off." % (
                version, _read_version_file(_path(root, "VERSION")), origin, update, update, update))


def update_notice(root, config, state, spawn=True):
    """The notice side of the update checker: start the background check when it is due (only
    with `spawn`; SessionStart is the one place that does) and return the notice to show, or
    None. Errors of past checks are never part of a notice."""
    if update_mode(config) == "off" or not os.path.isfile(_path(root, "manifest.json")):
        return None
    source = read_manifest_source(root)
    data = load_update_state(root)
    if source is None:
        if is_busy(state) or not _announce_once(root, config, REINSTALL_HINT):
            return None
        return ("[specloop] Update checks are not enabled for this install. Reinstall once with "
                "install.py <project> from your specloop clone to enable them.")
    if source.get("checkable") is False:
        return None
    notice = None
    version = available_version(root, source, data)
    if version and not is_busy(state) and _announce_once(root, config, version):
        notice = update_notice_text(root, config, source, version)
    # Spawned last: the check keeps what it reads from update.json, so it must see the announcement.
    if spawn and interval_elapsed(data.get("checkedAt"), update_interval(config)):
        spawn_update_check(root)
    return notice


def safe_update_notice(root, state, spawn=True):
    """update_notice for a hook: an exception is logged to stderr and gives no notice."""
    try:
        config = load_config(root)
        return update_notice(root, config, state) if spawn else update_notice(root, config, state, spawn=False)
    except Exception as e:
        print("[specloop] update check error: %s" % e, file=sys.stderr)
        return None


def add_completion_notice(root, output):
    """Append the update notice to the completion message of a Stop hook that finished the workflow.

    The state is read after it was saved as done, so the workflow counts as idle. This never starts
    a check. Any failure leaves `output` unchanged."""
    try:
        if not isinstance(output, dict) or "decision" in output or not output.get("systemMessage"):
            return output
        state = load_state(root)
        if not state or state.get("status") != DONE:
            return output
        notice = safe_update_notice(root, state, spawn=False)
        if not notice:
            return output
        return dict(output, systemMessage=output["systemMessage"] + "\n\n" + notice)
    except Exception as e:
        print("[specloop] update notice error: %s" % e, file=sys.stderr)
        return output


def _kit_file(root, name, custom=None):
    """Path of <custom>, overrides/<name>.md or phases/<name>.md, whichever exists first; None if none does."""
    names = [custom] if custom else []
    names += [os.path.join("overrides", name + ".md"), os.path.join("phases", name + ".md")]
    for candidate in names:
        path = candidate if os.path.isabs(candidate) else _path(root, candidate)
        if os.path.exists(path):
            return path
    return None


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def instruction_file(root, phase, phase_config, step):
    """Path of a phase's instructions; a step phase prefers step-<phase>."""
    custom = phase_config.get("instructions")
    return (_kit_file(root, "step-" + phase, custom) if step else None) or _kit_file(root, phase, custom)


def _render(text, state, **extra):
    """Fill {spec}, {run_dir}, {attempt_dir}, the `extra` values and, while a step is active, the step placeholders,
    in a single pass."""
    values = dict(step_values(state), spec=state.get("spec", ""), run_dir=_rel("run"),
                  attempt_dir=_rel("run", ATTEMPTS_DIR, attempt_key(state)), **extra)
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), text)


def verdict_check(config, state):
    """The current phase's check when it is a verdict, else None."""
    check = phase_graph(config, state).get(state.get("phase"), {}).get("check") or {}
    return check if check.get("type") == "verdict" else None


def review_file(config, state):
    """Where a review goes: the current phase's check.path, else the default in the attempt's directory."""
    check = verdict_check(config, state)
    return check_path(check, state) if check else _rel("run", ATTEMPTS_DIR, attempt_key(state), REVIEW_FILE)


def render_phase(text, config, state):
    """_render for an instruction file, which may also use {review_file}."""
    return _render(text, state, review_file=review_file(config, state))


def phase_instructions(root, config, state):
    phase = state["phase"]
    if phase == DONE:
        return "The workflow is complete."
    path = instruction_file(root, phase, phase_graph(config, state).get(phase, {}), in_step(state))
    text = _read_text(path) if path else "(no instructions file for phase '%s')" % phase
    return render_phase(text, config, state)


def is_delegated(config, state):
    return state["phase"] != DONE and bool(phase_graph(config, state).get(state["phase"], {}).get("delegate"))


def phase_brief(root, config, state):
    """Self-contained brief for a subagent: the phase instructions plus the files it must read."""
    files = [state.get("spec", "")]
    names = sorted(os.listdir(run_dir(root))) if os.path.isdir(run_dir(root)) else []
    if "plan.md" in names:
        files.append(_rel("run", "plan.md"))
    if is_feature(state) and "steps.json" in names:
        files.append(_rel("run", "steps.json"))
    if is_feature(state) and os.path.exists(replan_path(root, state)):
        files.append(_rel("run", ATTEMPTS_DIR, attempt_key(state), REPLAN_FILE))
    files += [_rel("run", n) for n in names if n.startswith("review") and n.endswith(".md")]
    files += attempt_reviews(root, config, state)
    if FAILURE_FILE in names:
        files.append(_rel("run", FAILURE_FILE))
    return "%s\n\n## Files to read\n\nRead these files from disk first (paths are relative to the project root):\n\n%s" % (
        phase_instructions(root, config, state).rstrip("\n"), "\n".join("- `%s`" % f for f in files))


def attempt_dirs(root):
    """(step part, attempt number, directory name) of every attempt directory, in attempt order."""
    base = os.path.join(run_dir(root), ATTEMPTS_DIR)
    found = [(m.group(1), int(m.group(2)), m.group(0)) for m in (
        re.match(r"(.+)-(\d+)$", n) for n in (sorted(os.listdir(base)) if os.path.isdir(base) else [])) if m]
    return sorted(found, key=lambda entry: entry[1])


def open_attempt(config, state):
    """Key of the attempt that can still receive a review, or None outside a running verdict phase."""
    return attempt_key(state) if verdict_check(config, state) and state.get("status") != "done" else None


def attempt_reviews(root, config, state):
    """Project-relative paths of the current step's (or single-mode run's) reviews that count, oldest first: the
    ones a gate used, plus the open attempt's file. A late review is for the user, not for the implementer."""
    current, open_key = attempt_key(state).rsplit("-", 1)[0], open_attempt(config, state)
    found = []
    for step, _, name in attempt_dirs(root):
        directory = os.path.join(run_dir(root), ATTEMPTS_DIR, name)
        path = os.path.join(directory, REVIEW_FILE)
        if step == current and os.path.exists(path) and (
                name == open_key or is_sealed_copy(path, os.path.join(directory, SEAL_FILE))):
            found.append(_rel("run", ATTEMPTS_DIR, name, REVIEW_FILE))
    return found


def phase_prompt(root, config, state):
    """Phase instructions, wrapped in the orchestration preamble when the phase is delegated."""
    if not is_delegated(config, state):
        return phase_instructions(root, config, state)
    brief = phase_brief(root, config, state)
    path = _kit_file(root, "delegate")
    return render_phase(_read_text(path), config, state).replace("{brief}", brief) if path else brief


def failure_path(root):
    return os.path.join(run_dir(root), FAILURE_FILE)


def write_failure(root, text):
    os.makedirs(run_dir(root), exist_ok=True)
    with open(failure_path(root), "w", encoding="utf-8") as f:
        f.write(text)


def clear_failure(root):
    if os.path.exists(failure_path(root)):
        os.remove(failure_path(root))


def header(state):
    if not is_feature(state):
        return "[specloop] phase=%s, iteration %d, spec=%s" % (
            state["phase"], state.get("iter", 0), state.get("spec", "?"))
    index, step = current_step(state) if in_step(state) else (None, None)
    where = ('feature step %d/%d "%s"' % (index + 1, len(state["approvedSteps"]["steps"]), step["title"])
             if step is not None else "feature planning")
    return "[specloop] %s, phase=%s, iteration %d, branch=%s" % (
        where, state["phase"], state.get("iter", 0), state.get("branch") or "-")


def relative_to_project(root, path):
    if not path:
        return None
    full = path if os.path.isabs(path) else os.path.join(root, path)
    try:
        rel = os.path.relpath(os.path.normcase(os.path.abspath(full)), os.path.normcase(os.path.abspath(root)))
    except ValueError:
        return None
    rel = rel.replace("\\", "/")
    return None if rel.startswith("../") or rel == ".." else rel


def resolve_shell(config):
    shell = config.get("shell", "bash")
    if os.name == "nt" and shell == "bash":
        # Prefer Git Bash over the WSL launcher in System32.
        git = shutil.which("git")
        if git:
            candidate = os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe")
            if os.path.exists(candidate):
                return candidate
    return shutil.which(shell) or shutil.which("sh") or shell


def tail(text, limit):
    return text if len(text) <= limit else "...(truncated)...\n" + text[-limit:]


def head(text, limit):
    return text if len(text) <= limit else text[:limit] + "\n...(truncated)"


# --- steps file ------------------------------------------------------------------

def _step_id(step):
    value = step.get("id") if isinstance(step, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _step_label(index, step):
    step_id = _step_id(step)
    return "steps[%d] (id %d)" % (index, step_id) if step_id is not None else "steps[%d]" % index


def _non_empty_string(value):
    return isinstance(value, str) and value.strip() != ""


def validate_steps(data):
    """Validate a parsed steps.json. Return a list of error strings; empty means valid."""
    if not isinstance(data, dict):
        return ["the top level must be an object with 'feature' and 'steps'"]
    errors = []
    if not _non_empty_string(data.get("feature")):
        errors.append("feature must be a non-empty string")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps must be a non-empty list")
        return errors
    seen = set()
    for index, step in enumerate(steps):
        label = _step_label(index, step)
        if not isinstance(step, dict):
            errors.append("%s: must be an object" % label)
            continue
        step_id = _step_id(step)
        if step_id is None:
            errors.append("%s: id must be a positive integer" % label)
        elif step_id in seen:
            errors.append("%s: id %d is not unique" % (label, step_id))
        else:
            seen.add(step_id)
        title = step.get("title")
        if not _non_empty_string(title):
            errors.append("%s: title must be a non-empty string" % label)
        elif len(title) > MAX_TITLE_LENGTH:
            errors.append("%s: title must be at most %d characters (has %d)" % (label, MAX_TITLE_LENGTH, len(title)))
        if not _non_empty_string(step.get("description")):
            errors.append("%s: description must be a non-empty string" % label)
        acceptance = step.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance or not all(_non_empty_string(a) for a in acceptance):
            errors.append("%s: acceptance must be a non-empty list of non-empty strings" % label)
        if step.get("status") not in STEP_STATUSES:
            errors.append("%s: status must be one of %s" % (label, ", ".join(STEP_STATUSES)))
        if "type" in step and step["type"] not in COMMIT_TYPES:
            errors.append("%s: type must be one of %s" % (label, ", ".join(COMMIT_TYPES)))
        if "files" in step and not (isinstance(step["files"], list) and all(isinstance(f, str) for f in step["files"])):
            errors.append("%s: files must be a list of strings" % label)
        if step.get("commit") is not None and not isinstance(step["commit"], str):
            errors.append("%s: commit must be a string or null" % label)
    return errors


def check_steps_immutable(previous, current):
    """Compare steps.json with a previously approved copy. Return errors for changes Claude may not make:
    done/empty steps changed, removed or reordered; status or commit changed; new steps not pending."""
    def by_id(data):
        steps = data.get("steps") if isinstance(data, dict) else None
        return [(i, s) for i, s in enumerate(steps or []) if _step_id(s) is not None]

    old = {_step_id(s): s for _, s in by_id(previous)}
    new_steps = by_id(current)
    new_ids = {_step_id(s) for _, s in new_steps}
    errors = []
    for step_id, step in old.items():
        if step.get("status") in LOCKED_STATUSES and step_id not in new_ids:
            errors.append("step id %d is %s and was removed" % (step_id, step["status"]))
    for index, step in new_steps:
        label = _step_label(index, step)
        before = old.get(_step_id(step))
        if before is None:
            if step.get("status") != "pending":
                errors.append("%s: status of a new step must be pending" % label)
            if step.get("commit") is not None:
                errors.append("%s: commit of a new step must be null (the engine sets it)" % label)
            continue
        if before.get("status") in LOCKED_STATUSES and step != before:
            errors.append("%s: step is %s and must not be changed" % (label, before["status"]))
            continue
        if step.get("status") != before.get("status"):
            errors.append("%s: status is set by the engine and must stay %s" % (label, before.get("status")))
        if step.get("commit") != before.get("commit"):
            errors.append("%s: commit is set by the engine and must not be changed" % label)
    locked_before = [i for i, s in old.items() if s.get("status") in LOCKED_STATUSES and i in new_ids]
    locked_now = [_step_id(s) for _, s in new_steps if _step_id(s) in locked_before]
    if locked_now != locked_before:
        errors.append("done/empty steps were reordered: expected ids %s in that order" % locked_before)
    return errors


# --- checks --------------------------------------------------------------------

def check_path(check, state):
    """The check's project-relative path, with placeholders such as {attempt_dir} filled in."""
    return _render(check["path"], state)


def check_phase(root, config, phase_config, state):
    """Run the check of one phase (its config dict from the phase graph).

    Return (result, details, full) where result is 'pass', 'fail' or 'reject', details is truncated
    to maxOutputChars for messages, and full is the untruncated text for run/last-failure.md."""
    check = phase_config.get("check")
    if not check:
        return "pass", "no check configured", ""
    kind = check["type"]
    limit = config.get("maxOutputChars", 4000)
    if kind == "command":
        rel = None
    else:
        rel = check_path(check, state)
        path = os.path.join(root, rel)
    if kind == "file":
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return "pass", "%s exists" % rel, ""
        return _same("fail", "Required file %s is missing or empty." % rel)
    if kind == "command":
        try:
            proc = run_limited([resolve_shell(config), "-c", check["command"]],
                               config.get("checkTimeoutSec", 900), cwd=root, merge_stderr=True)
        except OSError as e:
            return _same("fail", "Check command could not be run: %s" % e)
        if proc.timed_out:
            return _same("fail", "Check command timed out: %s" % check["command"])
        output = proc.stdout.decode("utf-8", errors="replace")
        if proc.returncode == 0:
            return "pass", "`%s` succeeded" % check["command"], ""
        summary = "`%s` exited with %d:\n" % (check["command"], proc.returncode)
        return "fail", summary + tail(output, limit), summary + output
    if kind == "verdict":
        if not os.path.exists(path):
            return _same("fail", "Review file %s is missing." % rel)
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if check["approve"] in text:
            return "pass", "review approved", ""
        if check["reject"] in text:
            return "reject", tail(text, limit), text
        return _same("fail", "%s must contain '%s' or '%s'." % (rel, check["approve"], check["reject"]))
    if kind == "steps":
        if not os.path.exists(path):
            return _same("fail", "Steps file %s is missing." % rel)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except ValueError as e:
            return _same("fail", "Steps file %s is not valid JSON: %s" % (rel, e))
        errors = validate_steps(data)
        if not errors and check.get("requireAllPending"):
            errors = ["%s: status must be pending, not %s" % (_step_label(i, s), s["status"])
                      for i, s in enumerate(data["steps"]) if s["status"] != "pending"]
        if errors:
            full = "Steps file %s is invalid:\n%s" % (rel, "\n".join(errors))
            return "fail", head(full, limit), full
        return "pass", "%s is valid" % rel, ""
    return _same("fail", "Unknown check type '%s'." % kind)


def _same(result, details):
    return result, details, details


def size_limit(config, key):
    """Return a largeSpecWarning limit, or None when the block or the key is disabled or not an integer."""
    limits = config.get("largeSpecWarning")
    value = limits.get(key) if isinstance(limits, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def spec_size_warning(root, config, spec):
    """Return a warning when the spec is too big for single mode, else None."""
    max_lines, max_words = size_limit(config, "maxLines"), size_limit(config, "maxWords")
    if max_lines is None and max_words is None:
        return None
    try:
        with open(os.path.join(root, spec), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    lines, words = len(text.splitlines()), len(text.split())
    if not ((max_lines is not None and lines > max_lines) or (max_words is not None and words > max_words)):
        return None
    limits = " / ".join(s for s in (
        "%d lines" % max_lines if max_lines is not None else None,
        "%d words" % max_words if max_words is not None else None) if s)
    return ("Warning: the spec has %d lines and %d words, above the single-mode limit of %s. "
            "It may be too big for one plan -> implement -> review cycle; "
            "consider %s for multi-commit features." % (lines, words, limits, cmd_name(config, "feature")))


def stale_override_warning(root, config, state):
    """Overrides are never updated, so one can still send a review or a replan to run/, where the gate does not
    look. Return a warning naming each such project file that this run uses, else None."""
    if is_feature(state):
        graphs = ((config["feature"]["planPhases"], False), (config["feature"]["stepPhases"], True))
    else:
        graphs = ((config["phases"], False),)
    files = [(instruction_file(root, phase, phase_config, step), phase_config)
             for graph, step in graphs for phase, phase_config in graph.items()]
    files.append((_kit_file(root, "delegate"), {}))
    found = {}
    for path, phase_config in files:
        if not path or os.path.dirname(path) == _path(root, "phases"):
            continue
        text, check = _read_text(path), phase_config.get("check") or {}
        allowed = "{run_dir}/" + REVIEW_FILE if "path" in check and check_path(check, state) == _rel(
            "run", REVIEW_FILE) else None  # a fixed check.path at the old place is what the gate reads
        found.setdefault(path, set()).update(s for s in STALE_PATHS if s in text and s != allowed)
    lines = ["[specloop] Warning: %s still uses %s, where the gate no longer looks. Use {review_file} for a review "
             "and {attempt_dir}/replan.md for a replan request." % (
                 relative_to_project(root, path) or path, " and ".join(sorted(strings)))
             for path, strings in sorted(found.items()) if strings]
    return "\n".join(lines) or None


def count_plan_items(root):
    try:
        with open(os.path.join(run_dir(root), "plan.md"), encoding="utf-8", errors="replace") as f:
            return len(CHECKLIST_ITEM.findall(f.read()))
    except OSError:
        return 0


def fixed_review_path(root, config, state):
    """Path of the current phase's verdict file when the project set one outside the attempt directories."""
    check = verdict_check(config, state)
    if check and "{attempt_dir}" not in check["path"]:
        return os.path.join(root, check_path(check, state))
    return None


def archive_review(root, config, state):
    """A review at a fixed path has to move, or it would count again. One in an attempt directory stays there."""
    src = fixed_review_path(root, config, state)
    if src and os.path.exists(src):
        base, ext = os.path.splitext(src)
        os.replace(src, "%s-%d%s" % (base, state.get("iter", 0), ext))


def new_attempt(root, config, state):
    """Open the next attempt. Results for earlier ones no longer count, so a late reviewer changes nothing."""
    state["attempt"] = state.get("attempt", 0) + 1
    os.makedirs(attempt_path(root, state), exist_ok=True)
    src = fixed_review_path(root, config, state)
    if src and os.path.exists(src):
        # The review a gate already used goes back to its own attempt, so that it is not reported as late.
        sealed = sealed_attempt(root, src)
        os.replace(src, os.path.join(sealed, REVIEW_FILE) if sealed
                   else os.path.join(attempt_path(root, state), LATE_REVIEW_FILE))


def enter_phase(root, config, state, phase):
    """Make `phase` current. Entering a phase with a verdict check starts a new attempt."""
    state["phase"] = phase
    if verdict_check(config, state):
        new_attempt(root, config, state)
    else:
        os.makedirs(attempt_path(root, state), exist_ok=True)


def seal_attempt(root, config, state):
    """Remember the review that was just used, so that /status can tell a later rewrite from it."""
    path = fixed_review_path(root, config, state) or os.path.join(attempt_path(root, state), REVIEW_FILE)
    if os.path.exists(path):
        os.makedirs(attempt_path(root, state), exist_ok=True)
        info = os.stat(path)
        atomic_write(os.path.join(attempt_path(root, state), SEAL_FILE),
                     json.dumps({"size": info.st_size, "mtime": info.st_mtime_ns}))


def is_sealed_copy(path, seal_path):
    try:
        with open(seal_path, encoding="utf-8") as f:
            seal = json.load(f)
        info = os.stat(path)
    except (OSError, ValueError):
        return False
    return seal == {"size": info.st_size, "mtime": info.st_mtime_ns}


def sealed_attempt(root, path):
    """Directory of the attempt whose seal matches the file at `path`, or None."""
    for _, _, name in reversed(attempt_dirs(root)):
        directory = os.path.join(run_dir(root), ATTEMPTS_DIR, name)
        if is_sealed_copy(path, os.path.join(directory, SEAL_FILE)):
            return directory
    return None


def late_reviews(root, config, state):
    """One line per review that arrived in a closed attempt. It never affects the workflow, but the user decides."""
    open_key = open_attempt(config, state)
    lines = []
    for step, attempt, name in attempt_dirs(root):
        directory = os.path.join(run_dir(root), ATTEMPTS_DIR, name)
        seal = os.path.join(directory, SEAL_FILE)
        for file_name in (REVIEW_FILE, LATE_REVIEW_FILE):
            path = os.path.join(directory, file_name)
            if (not os.path.exists(path) or (name == open_key and file_name == REVIEW_FILE and not os.path.exists(seal))
                    or is_sealed_copy(path, seal)):
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                verdict = re.search(r"VERDICT:\s*(\S+)", f.read())
            lines.append("late review %s(attempt %d): %s, %s" % (
                "" if step == "single" else "for step %s " % step, attempt,
                verdict.group(1) if verdict else "no verdict", _rel("run", ATTEMPTS_DIR, name, file_name)))
    return lines


# --- feature mode ----------------------------------------------------------------

def git(root, config, *args, raw=False):
    """Run git with feature.gitTimeoutSec. Return (ok, output); never raises."""
    try:
        proc = run_limited(["git"] + list(args), config.get("feature", {}).get("gitTimeoutSec", 60),
                           cwd=root, merge_stderr=True)
    except OSError as e:
        return False, "git could not be run: %s" % e
    if proc.timed_out:
        return False, "git %s timed out" % " ".join(args)
    output = proc.stdout.decode("utf-8", errors="replace")
    return proc.returncode == 0, output if raw else output.strip()


def _slugify(text):
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")[:MAX_SLUG_LENGTH].strip("-")


def branch_slug(spec_text, spec_path):
    """Slug for the feature branch: the spec's first '# ' heading, else its file name, else 'feature'."""
    heading = next((line[2:] for line in spec_text.splitlines() if line.startswith("# ")), "")
    name = os.path.splitext(os.path.basename(spec_path.replace("\\", "/")))[0]
    return _slugify(heading) or _slugify(name) or "feature"


def _git_status(root, config):
    """Return (ok, error, entries) with entries as (code, path) from `git status --porcelain -z`."""
    ok, out = git(root, config, "status", "--porcelain", "-z", "--untracked-files=all", raw=True)
    if not ok:
        return False, out.strip(), []
    parts, entries, i = out.split("\0"), [], 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC":
            i += 1  # renames and copies are followed by the original path
        entries.append((entry[:2], entry[3:]))
    return True, None, entries


def start_feature_branch(root, config, state):
    """Create and check out the feature branch. Return an error message (state untouched) or None."""
    ok, out = git(root, config, "rev-parse", "--is-inside-work-tree")
    if not ok or out != "true":
        return "%s is not a git repository (%s)." % (root, out or "git rev-parse failed")
    ok, base = git(root, config, "rev-parse", "--verify", "HEAD")
    if not ok:
        return "the repository has no commits yet (%s)." % base
    ok, out = git(root, config, "symbolic-ref", "-q", "--short", "HEAD")
    if not ok:
        return "HEAD is detached. Check out a branch first."
    ok, prefix = git(root, config, "rev-parse", "--show-prefix")
    if not ok:
        return "git rev-parse --show-prefix failed: %s" % prefix
    own_dir = prefix + _rel() + "/"
    ok, error, entries = _git_status(root, config)
    if not ok:
        return "git status failed: %s" % error
    dirty = [p for code, p in entries if code != "??" and not p.startswith(own_dir)]
    if dirty:
        shown = ", ".join(dirty[:10]) + (" (and %d more)" % (len(dirty) - 10) if len(dirty) > 10 else "")
        return "there are uncommitted changes to tracked files: %s. Commit or stash them first." % shown
    untracked = sorted(p for code, p in entries if code == "??" and not p.startswith(own_dir))
    try:
        with open(os.path.join(root, state.get("spec", "")), encoding="utf-8", errors="replace") as f:
            spec_text = f.read()
    except OSError:
        spec_text = ""
    feature = config["feature"]
    name = stem = feature.get("branchPrefix", "specloop/") + branch_slug(spec_text, state.get("spec", ""))
    suffix = 2
    while git(root, config, "rev-parse", "--verify", "--quiet", "refs/heads/" + name)[0]:
        name, suffix = "%s-%d" % (stem, suffix), suffix + 1
    ok, out = git(root, config, "checkout", "-q", "-b", name)
    if not ok:
        return "could not create branch %s: %s" % (name, out)
    state.update(branch=name, baseRef=base, preexistingUntracked=untracked)
    return None


def check_feature_branch(root, config, state):
    """Return an error message when the current branch is not the run's branch, else None."""
    ok, current = git(root, config, "symbolic-ref", "-q", "--short", "HEAD")
    if ok and current == state["branch"]:
        return None
    return "this feature run works on branch %s, but the current branch is %s. Run `git checkout %s` first." % (
        state["branch"], current if ok else "a detached HEAD or unknown (%s)" % (current or "git failed"),
        state["branch"])


def read_plan(root, config, state):
    """Validate run/steps.json again. Return (data, None), or (None, message) when the plan is invalid."""
    plan_config = config["feature"]["planPhases"].get(config["feature"].get("planPhase", "feature-plan"), {})
    result, _, full = check_phase(root, config, plan_config, state)
    if result != "pass":
        return None, "The plan cannot be approved yet:\n%s\n\nFix it, then run %s again." % (
            full, cmd_name(config, "go"))
    with open(os.path.join(root, check_path(plan_config["check"], state)), encoding="utf-8") as f:
        return json.load(f), None


def approve_plan(root, config, state, data):
    """Store a validated plan as approvedSteps and start step 1."""
    state["approvedSteps"] = data
    state.pop("awaitingApproval", None)
    state["status"] = "active"
    start_next_step(root, config, state)


def steps_file_violation(root, state):
    """Compare run/steps.json with approvedSteps during a step. Return the failure details, or None."""
    rel = _rel("run", "steps.json")
    restore = ("Restore it from approvedSteps in %s; only specloop changes step status, commits and finished "
               "steps." % _rel("state.json"))
    try:
        with open(os.path.join(run_dir(root), "steps.json"), encoding="utf-8") as f:
            current = json.load(f)
    except FileNotFoundError:
        return "%s is missing. %s" % (rel, restore)
    except ValueError as e:
        return "%s is not valid JSON (%s). %s" % (rel, e, restore)
    errors = check_steps_immutable(state.get("approvedSteps") or {}, current)
    if not errors:
        return None
    return "%s may only be changed by specloop: %s\n%s\n\n%s" % (rel, errors[0], "\n".join(errors[1:]), restore)


def replan_candidate(root, state):
    """Load run/steps.json as a proposed replan. Return (data, errors); errors is empty when it may be adopted."""
    rel = _rel("run", "steps.json")
    try:
        with open(os.path.join(run_dir(root), "steps.json"), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, ["%s is missing" % rel]
    except ValueError as e:
        return None, ["%s is not valid JSON: %s" % (rel, e)]
    return data, validate_steps(data) or check_steps_immutable(state.get("approvedSteps") or {}, data)


def archive_replan(root, state):
    """Move the attempt's replan.md to run/replans/replan-N.md with the next free N. A run that stalled on a
    request before attempts existed still has it at run/replan.md."""
    sources = [replan_path(root, state), os.path.join(run_dir(root), REPLAN_FILE)]
    src = next((p for p in sources if os.path.exists(p)), None)
    if src is None:
        return
    dest = os.path.join(run_dir(root), "replans")
    os.makedirs(dest, exist_ok=True)
    numbers = [int(m.group(1)) for m in (re.match(r"replan-(\d+)\.md$", n) for n in os.listdir(dest)) if m]
    os.replace(src, os.path.join(dest, "replan-%d.md" % (max(numbers or [0]) + 1)))


def adopt_replan(root, config, state, data):
    """Store a validated replan as approvedSteps and continue. Return 'restart' (the current step starts over),
    'next' (the current step was removed and the next one started) or 'done' (no steps remain)."""
    state["approvedSteps"] = data
    state.pop("replanRequested", None)
    archive_replan(root, state)
    clear_failure(root)
    if current_step(state)[1] is not None:
        state.update(iter=0, lastFailure=None)
        enter_phase(root, config, state, config["feature"].get("stepInitialPhase", "implement"))
        save_steps(root, state)
        return "restart"
    if start_next_step(root, config, state) is None:
        save_steps(root, state)
        state.update(status="done", phase=DONE)
        return "done"
    return "next"


def replan_adopted(root, config, state, kind):
    """Stop-hook result after a replan was adopted."""
    if kind == "done":
        return {"systemMessage": "[specloop] Replan adopted; no steps remain. " + completion_message(config, state)}
    what = "Restarting" if kind == "restart" else "The current step was removed; starting"
    return {"decision": "block", "reason": "Replan adopted. %s %s.\n\n%s" % (
        what, step_label(state)[0].lower(), phase_prompt(root, config, state))}


def request_replan(root, config, state, session=None):
    """Claude wrote replan.md during a step: enter the replan phase (auto) or stall for the user."""
    feature = config["feature"]
    add_history(state, session, phase=state["phase"], result="replan")
    count, cap = state.get("replanCount", 0), feature.get("maxReplans", 3)
    auto = feature.get("replan", "stall") == "auto"
    request = _rel("run", ATTEMPTS_DIR, attempt_key(state), REPLAN_FILE)
    if auto and count < cap:
        clear_failure(root)
        state.update(replanCount=count + 1, phase=feature.get("replanPhase", "feature-replan"), iter=0,
                     lastFailure=None)
        return {"decision": "block", "reason": "Replan requested in %s. Entering phase '%s' (auto-replan %d of %d)."
                "\n\n%s" % (request, state["phase"], count + 1, cap, phase_prompt(root, config, state))}
    state.update(status="stalled", replanRequested=True)
    cap_note = " The cap of %d auto-replans per run is reached, so this one needs you." % cap if auto else ""
    return {"systemMessage": (
        "[specloop] Claude requested a replan of the remaining steps.%s Read %s, optionally edit the pending or "
        "active steps in %s, then run %s. The current step then restarts with the adopted plan; uncommitted work "
        "stays in the working tree." % (cap_note, request, _rel("run", "steps.json"), cmd_name(config, "go")))}


def save_steps(root, state):
    """Write approvedSteps (including engine-owned status/commit changes) back to run/steps.json."""
    atomic_write(os.path.join(run_dir(root), "steps.json"), json.dumps(state["approvedSteps"], indent=2) + "\n")


def archive_step_files(root, step_id):
    """Move the finished step's review*.md and last-failure.md into run/steps/<id>/."""
    names = [n for n in os.listdir(run_dir(root))
             if (n.startswith("review") and n.endswith(".md")) or n == FAILURE_FILE]
    if not names:
        return
    dest = os.path.join(run_dir(root), "steps", str(step_id))
    os.makedirs(dest, exist_ok=True)
    for name in names:
        os.replace(os.path.join(run_dir(root), name), os.path.join(dest, name))


def start_next_step(root, config, state):
    """Archive the previous step's files and make the next pending step active. Return it, or None."""
    if state.get("stepId") is not None:
        archive_step_files(root, state["stepId"])
    state.pop("commitBase", None)
    step = next((s for s in state["approvedSteps"]["steps"] if s.get("status") == "pending"), None)
    if step is None:
        state["stepId"] = None
        return None
    step["status"] = "active"
    state.update(stepId=step["id"], iter=0, lastFailure=None)
    enter_phase(root, config, state, config["feature"].get("stepInitialPhase", "implement"))
    save_steps(root, state)
    return step


def step_label(state):
    index, step = current_step(state)
    return "Step %d/%d" % (index + 1, len(state["approvedSteps"]["steps"])), step


def _matches(path, prefixes):
    return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def commit_step(root, config, state):
    """Stage and commit the current step. Return ('committed', sha), ('empty', None) or ('failed', output)."""
    ok, top = git(root, config, "rev-parse", "--show-toplevel")
    if not ok:
        return "failed", "git rev-parse --show-toplevel failed:\n" + top
    ok, prefix = git(root, config, "rev-parse", "--show-prefix")
    if not ok:
        return "failed", "git rev-parse --show-prefix failed:\n" + prefix
    ok, out = git(top, config, "add", "-A")
    if not ok:
        return "failed", "git add -A failed:\n" + out
    ok, out = git(top, config, "diff", "--cached", "--name-only", "-z", raw=True)
    if not ok:
        return "failed", "git diff --cached failed:\n" + out
    excluded = [prefix + p for p in config["feature"].get("commitExclude", [])]
    preexisting = set(state.get("preexistingUntracked", []))
    unstage = [p for p in out.split("\0") if p and (p in preexisting or _matches(p, excluded))]
    for start in range(0, len(unstage), 100):
        ok, out = git(top, config, "reset", "-q", "--", *unstage[start:start + 100])
        if not ok:
            return "failed", "git reset failed:\n" + out
    if git(top, config, "diff", "--cached", "--quiet")[0]:
        return "empty", None
    message = _render(config["feature"].get("commitMessage", "{type}: {step_title}"), state)
    ok, out = git(top, config, "commit", "-q", "-m", message)
    if not ok:
        return "failed", "git commit failed:\n" + out
    ok, sha = git(top, config, "rev-parse", "HEAD")
    return ("committed", sha) if ok else ("failed", "git rev-parse HEAD failed:\n" + sha)


def head_sha(root, config):
    ok, sha = git(root, config, "rev-parse", "HEAD")
    return sha if ok else None


def run_commit(root, config, state):
    """commit_step for a state that recorded commitBase. A commit whose result was never recorded leaves nothing
    to commit, so 'empty' with HEAD past commitBase means that commit is this step's."""
    commit = commit_step(root, config, state)
    base = state.get("commitBase")
    if commit[0] == "empty" and base:
        sha = head_sha(root, config)
        if sha and sha != base:
            return "committed", sha
    return commit


def completion_message(config, state):
    lines = ["%s %s" % (s["commit"][:7] if s.get("commit") else "(empty)", s["title"])
             for s in state["approvedSteps"]["steps"]]
    return ("All %d steps are finished on branch %s:\n%s\n\nReview the branch (git log --oneline %s..%s) "
            "and open a pull request when it is ready; specloop never pushes." % (
                len(lines), state.get("branch"), "\n".join("  " + l for l in lines),
                (state.get("baseRef") or "")[:7], state.get("branch")))


def finish_step(root, config, state, commit, session=None):
    """Apply the outcome of commit_step and start the next step. A pause that arrived meanwhile is kept.

    Return (kind, text): 'failed' (text is the git output, the step is back in stepInitialPhase),
    'next' (text summarizes the commit; the next step is active), 'paused' or 'done' (text is the message)."""
    label, step = step_label(state)
    result, value = commit
    state.pop("commitBase", None)
    if result == "failed":
        enter_phase(root, config, state, config["feature"].get("stepInitialPhase", "implement"))
        state["lastFailure"] = tail(value, 500)
        write_failure(root, value)
        return "failed", value
    if result == "empty":
        step["status"] = "empty"
        add_history(state, session, phase=COMMIT, result="empty",
                    warning="step %s had no changes; nothing was committed" % step["id"])
        summary = "%s \"%s\" had no changes; nothing was committed." % (label, step["title"])
    else:
        step.update(status="done", commit=value)
        add_history(state, session, phase=COMMIT, result="committed", commit=value)
        summary = "%s committed (%s): %s." % (label, value[:7], step["title"])
    clear_failure(root)
    save_steps(root, state)
    if start_next_step(root, config, state) is None:
        state.update(status="done", phase=DONE)
        return "done", summary + "\n\n" + completion_message(config, state)
    if state.get("status") == "paused" or config["feature"].get("pauseBetweenSteps"):
        state["status"] = "paused"
        return "paused", "%s Paused before %s. Run %s to continue." % (
            summary, step_label(state)[0].lower(), cmd_name(config, "go"))
    return "next", summary


def await_approval(root, config, state):
    """Pause after planning so the user can review the plan. Return the message for the user."""
    state.update(status="paused", awaitingApproval=True)
    return ("The plan is ready. Review or edit %s and %s, then run %s to approve it." % (
        _rel("run", "plan.md"), _rel("run", "steps.json"), cmd_name(config, "go")))


# --- hook handlers ---------------------------------------------------------------

def work_in_flight(hook_input):
    """True while a subagent, workflow or teammate is running. Shell tasks and monitors do not count: a dev
    server must not hold a phase open forever."""
    tasks = hook_input.get("background_tasks")
    return isinstance(tasks, list) and any(isinstance(t, dict) and t.get("type") in IN_FLIGHT_TASKS for t in tasks)


def gate_mark(state):
    """What must stay the same between a check and recording its result."""
    return (state.get("status"), (state.get("owner") or {}).get("session"), state.get("phase"), state.get("stepId"),
            state.get("attempt", 0), state.get("createdAt"))


def take_gate(root, config, hook_input):
    """Record that this Stop runs the check. Return (reloaded state, None), or (None, Stop output) when it must
    not run."""
    def apply(state):
        if state.get("status") != "active" or not is_owner(state, hook_input):
            raise Unchanged((None, None))
        running = state.get("gate") or {}
        if seconds_since(running.get("at")) < config.get("checkTimeoutSec", 900) + 60:
            raise Unchanged((None, {"systemMessage": (
                "[specloop] a check is marked as running since %s, so this stop ran none. If no check is running, "
                "%s clears the mark." % (running.get("at"), cmd_name(config, "go")))}))
        state["gate"] = {"session": hook_input.get("session_id"), "at": now()}
        return state, None
    return update_state(root, apply)


def release_gate(root):
    def apply(state):
        if "gate" not in state:
            raise Unchanged()
        del state["gate"]
    try:
        update_state(root, apply)
    except (LockBusy, StateCorrupt):
        pass


def evaluate_gate(root, config, state):
    """Steps file checks, phase check and replan validation. Holds no lock and records nothing."""
    replanning = in_replan(config, state)
    if in_step(state) and not replanning and os.path.exists(replan_path(root, state)):
        return {"result": "replan"}
    limit = config.get("maxOutputChars", 4000)
    violation = steps_file_violation(root, state) if in_step(state) and not replanning else None
    if violation:
        return {"result": "fail", "details": head(violation, limit), "full": violation}
    result, details, full = check_phase(root, config, phase_graph(config, state)[state["phase"]], state)
    outcome = {"result": result, "details": details, "full": full}
    if result == "pass" and replanning:
        data, errors = replan_candidate(root, state)
        if errors:
            full = "%s may only change pending or active steps: %s\n%s" % (
                _rel("run", "steps.json"), errors[0], "\n".join(errors[1:]))
            return {"result": "fail", "details": head(full, limit), "full": full}
        outcome["replan"] = data
    elif (result == "pass" and is_plan_phase(config, state)
          and not config["feature"].get("requirePlanApproval", True)):
        outcome["plan"], outcome["planError"] = read_plan(root, config, state)
    return outcome


def is_plan_phase(config, state):
    return is_feature(state) and not in_step(state) and state["phase"] == config["feature"].get("planPhase")


def handle_stop(root, hook_input):
    state = load_state(root)
    if not state or state.get("status") != "active":
        return None
    if not is_owner(state, hook_input) or work_in_flight(hook_input):
        return None
    config = load_config(root)
    state, output = take_gate(root, config, hook_input)
    if state is None:
        return output
    try:
        output = run_gate(root, config, state, hook_input)
    except Exception:
        release_gate(root)
        raise
    return add_completion_notice(root, output)


def run_gate(root, config, state, hook_input):
    """Check with no lock, record the result under the lock if it is still current, then commit with no lock."""
    mark, phase = gate_mark(state), state["phase"]
    outcome = evaluate_gate(root, config, state)
    head = head_sha(root, config) if in_step(state) and outcome["result"] == "pass" else None

    def apply(current):
        if gate_mark(current) != mark:
            current.pop("gate", None)
            print("[specloop] dropped the result of phase '%s': the workflow changed during the check" % phase,
                  file=sys.stderr)
            return None
        result = apply_gate(root, config, current, outcome, hook_input)
        if result is not COMMIT_PENDING:
            current.pop("gate", None)
        elif head:
            current.setdefault("commitBase", head)  # a retry keeps the base from before the first commit
        return result

    out = update_state(root, apply)
    if out is not COMMIT_PENDING:
        return out
    state = load_state(root) or state
    commit = run_commit(root, config, state)
    return update_state(root, lambda current: apply_commit(root, config, current, commit, state, hook_input))


def apply_gate(root, config, state, outcome, hook_input):
    """Record the result of a gate on the reloaded state. Return the Stop hook output, or COMMIT_PENDING."""
    session = hook_input.get("session_id")
    if outcome["result"] == "replan":
        return request_replan(root, config, state, session)
    result, details, full = outcome["result"], outcome["details"], outcome["full"]
    phase = state["phase"]
    graph = phase_graph(config, state)
    state["iter"] = state.get("iter", 0) + 1
    add_history(state, session, phase=phase, result=result, iter=state["iter"])
    if result in ("pass", "reject") and verdict_check(config, state):
        seal_attempt(root, config, state)

    if result == "pass":
        clear_failure(root)
        state["lastFailure"] = None
        if in_replan(config, state):
            return replan_adopted(root, config, state, adopt_replan(root, config, state, outcome["replan"]))
        if is_plan_phase(config, state):
            if config["feature"].get("requirePlanApproval", True):
                return {"systemMessage": "[specloop] " + await_approval(root, config, state)}
            if outcome.get("planError"):
                return {"systemMessage": "[specloop] " + outcome["planError"]}
            approve_plan(root, config, state, outcome["plan"])
            return {"decision": "block", "reason": "Plan approved. %s starts now.\n\n%s" % (
                step_label(state)[0], phase_prompt(root, config, state))}
        next_phase = graph[phase].get("next", DONE)
        if in_step(state) and next_phase in (COMMIT, DONE):
            return COMMIT_PENDING
        if next_phase == DONE:
            state.update(phase=DONE, status="done")
            return {"systemMessage": "[specloop] all phases passed - done."}
        enter_phase(root, config, state, next_phase)
        max_items = size_limit(config, "maxPlanItems")
        if (phase == "plan" and not is_feature(state) and max_items is not None
                and not state.get("largePlanAcknowledged")):
            items = count_plan_items(root)
            if items > max_items:
                state["status"] = "paused"
                state["largePlanItems"] = items
                return {"systemMessage": (
                    "[specloop] the plan has %d checklist items, above the single-mode limit of %d. "
                    "Paused before phase '%s'. Run %s and then %s to split the work into commits, "
                    "or %s to continue anyway." % (
                        items, max_items, next_phase, cmd_name(config, "reset"),
                        cmd_name(config, "feature"), cmd_name(config, "go")))}
        return {"decision": "block", "reason": "Phase '%s' passed (%s). Now entering phase '%s'.\n\n%s" % (
            phase, details, next_phase, phase_prompt(root, config, state))}

    write_failure(root, full)
    failure_file = _rel("run", FAILURE_FILE)
    if result == "reject":
        archive_review(root, config, state)
        enter_phase(root, config, state, graph[phase]["check"].get("onReject", "implement"))
        state["lastFailure"] = "review rejected"
        if is_delegated(config, state):
            reason = "Review requested changes. Back to phase '%s'. Full review: %s\n\n%s" % (
                state["phase"], failure_file, phase_prompt(root, config, state))
        else:
            reason = "Review requested changes. Back to phase '%s'. Address every point below:\n\n%s\n\n%s" % (
                state["phase"], details, phase_instructions(root, config, state))
    else:
        state["lastFailure"] = tail(details, 500)
        if is_delegated(config, state):
            lines = details.strip().splitlines()
            reason = "Phase '%s' is not complete yet: %s. Full details: %s\n\n%s" % (
                phase, lines[0].rstrip(":.") if lines else "check failed", failure_file,
                phase_prompt(root, config, state))
        else:
            reason = "Phase '%s' is not complete yet. %s\n\nKeep working on phase '%s'.\n\n%s" % (
                phase, details, phase, phase_instructions(root, config, state))
    return block_or_stall(root, config, state, phase, reason)


def block_or_stall(root, config, state, phase, reason):
    """Block the stop with `reason`, or stall when the iteration limit is reached."""
    if state["iter"] >= config.get("maxIterations", 15):
        state["status"] = "stalled"
        return {"systemMessage": "[specloop] stalled after %d iterations in phase '%s'. "
                                 "Inspect, then run %s to resume or %s to skip the phase." % (
                                     state["iter"], phase, cmd_name(config, "go"), cmd_name(config, "advance"))}
    return {"decision": "block", "reason": reason}


def commit_failed_reason(config, root, state, label, output):
    if is_delegated(config, state):
        lines = output.strip().splitlines()
        return "Committing %s failed: %s. Full details: %s\n\n%s" % (
            label.lower(), lines[0].rstrip(":.") if lines else "git failed", _rel("run", FAILURE_FILE),
            phase_prompt(root, config, state))
    return "Committing %s failed. Back to phase '%s'. Fix the problem below:\n\n%s\n\n%s" % (
        label.lower(), state["phase"], tail(output, config.get("maxOutputChars", 4000)),
        phase_instructions(root, config, state))


def apply_commit(root, config, state, commit, before, hook_input):
    """The commit of the last step phase has run: apply it to the reloaded state (Stop hook result).

    `before` is the state the commit started from. The commit stands whatever happened meanwhile, but only the
    chat that still drives the workflow gets the next brief."""
    state.pop("gate", None)
    if state.get("stepId") != before.get("stepId") or state.get("createdAt") != before.get("createdAt"):
        print("[specloop] the workflow moved on during the step commit", file=sys.stderr)
        return None
    label = step_label(state)[0]
    kind, text = finish_step(root, config, state, commit, hook_input.get("session_id"))
    drives = state.get("status") == "active" and is_owner(state, hook_input)
    if kind == "failed":
        reason = commit_failed_reason(config, root, state, label, text)
        return block_or_stall(root, config, state, before["phase"], reason) if drives else {
            "systemMessage": "[specloop] committing %s failed; see %s." % (label.lower(), _rel("run", FAILURE_FILE))}
    if kind in ("done", "paused") or not drives:
        return {"systemMessage": "[specloop] " + text}
    return {"decision": "block", "reason": "%s Now starting %s.\n\n%s" % (
        text, step_label(state)[0].lower(), phase_prompt(root, config, state))}


def deny(reason):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "[specloop] " + reason}}


def handle_pretool(root, hook_input):
    state = load_state(root)
    if not state or state.get("status") != "active":
        return None
    config = load_config(root)
    tool = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input") or {}
    phase = state["phase"]
    owner = is_owner(state, hook_input)

    if tool in EDIT_TOOLS:
        rel = relative_to_project(root, tool_input.get("file_path") or tool_input.get("notebook_path"))
        protected = [_rel(n) for n in ("state.json", "config.json", "config.local.json",
                                                "defaults.json", "engine.py")]
        if rel in protected:
            return deny("%s is managed by specloop; use the %s* commands." % (rel, cmd_name(config, "")))
        in_run = (rel or "").startswith(_rel("run") + "/")
        if not owner:
            if in_run and not hook_input.get("agent_id"):
                return deny("%s/ belongs to the chat that drives the workflow. Run %s here to take it over." % (
                    _rel("run"), cmd_name(config, "go")))
            return None
        allowed = phase_graph(config, state).get(phase, {}).get("allowEdits")
        if allowed is not None and (rel is None or not any(rel.startswith(p) for p in allowed)):
            return deny("phase '%s' only allows editing %s, not %s." % (
                phase, ", ".join(allowed), rel or "a path outside the project"))
        # Claude Code sets agent_id in hook input only for tool calls made inside a subagent.
        if (config["feature"].get("enforceDelegation") and is_delegated(config, state)
                and not hook_input.get("agent_id") and not in_run):
            return deny("phase '%s' is delegated: start one subagent with the phase brief and let it make this "
                        "edit. The main session may only edit %s/." % (phase, _rel("run")))

    if tool == "Bash":
        command = tool_input.get("command", "")
        for blocked in config.get("blockedCommandsUntilDone", []) if owner else []:
            if blocked in command:
                return deny("'%s' is not allowed until the workflow is done (phase '%s')." % (blocked, phase))
        if is_feature(state):
            for blocked in config["feature"].get("blockedCommands", []):
                if blocked in command:
                    return deny("'%s' is not allowed while a feature run is active: specloop owns the feature "
                                "branch %s and makes the step commits itself. Run %s first if the user wants to "
                                "do this by hand." % (blocked, state.get("branch", "-"), cmd_name(config, "pause")))
    return None


def typed_command(config, hook_input):
    """Name of the specloop command in the prompt, when its first token is one; only a typed command counts."""
    prompt = hook_input.get("prompt")
    tokens = prompt.split(None, 1) if isinstance(prompt, str) else []
    for name in ("go",) + OWNER_ONLY_COMMANDS:
        if tokens and tokens[0] == cmd_name(config, name):
            return name
    return None


def claim_workflow(root, session):
    """Store this chat as the owner. A different owner is recorded for cmd_go, which reports the takeover."""
    def apply(state):
        previous = (state.get("owner") or {}).get("session")
        if state.get("status") not in CLAIMABLE_STATUSES or previous == session:
            raise Unchanged()
        if previous:
            state["ownerChangedFrom"] = previous
        state["owner"] = {"session": session, "since": now()}
        add_history(state, session, phase=state.get("phase"), result="takeover" if previous else "claimed")
    update_state(root, apply)


def handle_prompt_command(root, config, state, hook_input):
    """UserPromptSubmit runs before a typed command's shell line. Return a block, or None to let it run."""
    command = typed_command(config, hook_input)
    session = hook_input.get("session_id")
    if command == "go" and session and state.get("status") in CLAIMABLE_STATUSES:
        if (state.get("owner") or {}).get("session") == session:
            return None
        if is_feature(state) and state.get("branch") and check_feature_branch(root, config, state):
            return None  # cmd_go refuses and reports it, so the workflow stays with its owner
        try:
            claim_workflow(root, session)
        except LockBusy:
            return {"decision": "block", "reason": "[specloop] specloop is busy. Run %s again." % cmd_name(config, "go")}
    elif command in OWNER_ONLY_COMMANDS and state.get("status") == "active" and not is_owner(state, hook_input):
        return {"decision": "block", "reason": "[specloop] another chat drives this workflow. Run %s here to take it "
                "over, or %s to stop it first." % (cmd_name(config, "go"), cmd_name(config, "pause"))}
    return None


def handle_context(root, event, hook_input=None):
    hook_input = hook_input or {}
    state = load_state(root)
    output = workflow_context(root, event, state, hook_input)
    if event == "SessionStart":
        notice = safe_update_notice(root, state)
        if notice:
            output = dict(output or {}, systemMessage=notice)
    return output


def workflow_context(root, event, state, hook_input):
    if not state:
        return None
    config = load_config(root)
    if event == "UserPromptSubmit":
        blocked = handle_prompt_command(root, config, state, hook_input)
        if blocked:
            return blocked
        state = load_state(root)
        if not state:
            return None
    status = state.get("status")
    if status == "active":
        if not is_owner(state, hook_input):
            if event != "SessionStart":
                return None
            notice = "[specloop] another chat owns the workflow on %s. Run %s here to take it over." % (
                state.get("branch") or state.get("spec", "?"), cmd_name(config, "go"))
            return {"systemMessage": notice,
                    "hookSpecificOutput": {"hookEventName": event, "additionalContext": notice}}
        text = header(state)
        if is_delegated(config, state):
            text += ("\n[specloop] Phase '%s' is delegated: start one subagent with the phase brief, relay a short "
                     "summary, and finish your turn; do not do the phase's work yourself. If the user's message is "
                     "about something else, answer that first and say the workflow is waiting." % state["phase"])
        if event == "SessionStart" or state.get("iter", 0) == 0:
            text += "\n\n" + phase_prompt(root, config, state)
        elif state.get("lastFailure"):
            text += "\nLast check failure: " + state["lastFailure"]
    elif status == "ready":
        text = "[specloop] spec %s is loaded; development starts when the user runs %s." % (
            state.get("spec"), cmd_name(config, "go"))
    else:
        return None
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


# --- manual commands ---------------------------------------------------------------

def cmd_init(root, spec, mode="single"):
    config = load_config(root)
    if not spec or not os.path.exists(os.path.join(root, spec)):
        return "Spec file not found: %r" % spec
    feature = mode == "feature"
    phase = config["feature"].get("planPhase", "feature-plan") if feature else config.get("initialPhase", "plan")

    def apply(state):
        if state.get("status") in RUNNING_STATUSES:
            first = "%s, then " % cmd_name(config, "pause") if state["status"] == "active" else ""
            raise Unchanged("A workflow is %s, so no spec was loaded. Run %s%s first." % (
                state["status"], first, cmd_name(config, "reset")))
        # Stale plan.md / review.md from an earlier run would pass the new run's gates.
        if os.path.isdir(run_dir(root)):
            shutil.rmtree(run_dir(root))
        os.makedirs(run_dir(root))
        state.clear()
        state.update({"status": "ready", "mode": mode, "phase": phase, "spec": spec.replace("\\", "/"),
                      "iter": 0, "attempt": 0, "history": [], "createdAt": now()})

    refused = update_state(root, apply)
    if refused:
        return refused
    if feature:
        return "Spec loaded in feature mode: %s. Run %s to create the feature branch and start planning." % (
            spec, cmd_name(config, "go"))
    out = "Spec loaded: %s. Run %s to start development." % (spec, cmd_name(config, "go"))
    warning = spec_size_warning(root, config, spec)
    return out + "\n\n" + warning if warning else out


def cmd_go(root):
    config = load_config(root)
    state = load_state(root)
    if not state:
        return "No spec loaded. Run %s <path> first." % cmd_name(config, "spec")
    if state["status"] == "done":
        return "Workflow already done. Run %s and %s to start a new one." % (
            cmd_name(config, "reset"), cmd_name(config, "spec"))
    plan, replan = None, None
    if is_feature(state):
        if state.get("branch"):
            error = check_feature_branch(root, config, state)
        else:
            error = start_feature_branch(root, config, state)
        if error:
            return "Not started: %s" % error
        if state["status"] == "paused" and state.get("awaitingApproval"):
            plan, error = read_plan(root, config, state)
            if error:
                return error
        if state["status"] == "stalled" and state.get("replanRequested"):
            replan, errors = replan_candidate(root, state)
            if errors:
                return "The replan cannot be adopted yet:\n%s\n\nFix %s, then run %s again." % (
                    "\n".join(errors), _rel("run", "steps.json"), cmd_name(config, "go"))
    branch = {k: state[k] for k in ("branch", "baseRef", "preexistingUntracked") if k in state}

    def apply(current):
        if not current or current["status"] == "done":
            raise Unchanged("The workflow changed while %s ran. Run %s to see it." % (
                cmd_name(config, "go"), cmd_name(config, "status")))
        current.update(branch)
        return resume(root, config, current, plan, replan)
    out = update_state(root, apply)
    warning = stale_override_warning(root, config, state)
    return out + "\n\n" + warning if warning else out


def resume(root, config, state, plan, replan):
    """Everything cmd_go changes in the state, on the reloaded state. Return the command's output."""
    state.pop("gate", None)  # left behind by a killed Stop hook, it would hold every later check back
    if plan is not None:
        approve_plan(root, config, state, plan)
    if replan is not None and adopt_replan(root, config, state, replan) == "done":
        state.pop("ownerChangedFrom", None)
        return "Replan adopted; no steps remain. " + completion_message(config, state)
    if not is_feature(state) and state["status"] == "ready" and not state.get("largeSpecAcknowledged"):
        warning = spec_size_warning(root, config, state.get("spec", ""))
        if warning and not state.get("largeSpecWarned"):
            state["largeSpecWarned"] = True
            return ("%s\n\nThe workflow has not started. Do not plan or implement; show this warning to the user "
                    "and wait. Running %s again proceeds in single mode anyway." % (warning, cmd_name(config, "go")))
        if warning:
            state["largeSpecAcknowledged"] = True
    if state["status"] == "stalled":
        state["iter"] = 0
    if state["status"] == "paused" and state.get("largePlanItems") and not state.get("largePlanAcknowledged"):
        state["largePlanAcknowledged"] = True
    state["status"] = "active"
    takeover = "ownerChangedFrom" in state
    state.pop("ownerChangedFrom", None)
    if takeover:
        new_attempt(root, config, state)
    # A workflow paused across the upgrade to attempts has no directory yet.
    os.makedirs(attempt_path(root, state), exist_ok=True)
    out = header(state) + "\n\n" + phase_prompt(root, config, state)
    if takeover:
        return ("[specloop] This chat now drives the workflow. Stop any subagent still running in the previous "
                "chat.\n" + out)
    return out


def cmd_pause(root):
    config = load_config(root)

    def apply(state):
        if state.get("status") not in RUNNING_STATUSES:
            raise Unchanged("Nothing to pause.")
        state["status"] = "paused"
        return "Workflow paused at phase '%s'. Run %s to resume." % (state["phase"], cmd_name(config, "go"))
    return update_state(root, apply)


def cmd_advance(root):
    state = load_state(root)
    if not state or state["phase"] == DONE:
        return "Nothing to advance."
    config = load_config(root)
    head = head_sha(root, config) if in_step(state) else None
    replan = None
    if in_replan(config, state):
        # Force-passing the replan phase adopts the current steps.json if it is acceptable.
        replan, errors = replan_candidate(root, state)
        if errors:
            return "The replan cannot be adopted:\n%s" % "\n".join(errors)

    def apply(current):
        if not current or current["phase"] == DONE:
            raise Unchanged("Nothing to advance.")
        if in_replan(config, current) and replan is not None:
            add_history(current, None, phase=current["phase"], result="skipped")
            if adopt_replan(root, config, current, replan) == "done":
                return "Replan adopted; no steps remain. " + completion_message(config, current)
            return "Replan adopted. Now in %s, phase '%s'." % (step_label(current)[0].lower(), current["phase"])
        clear_failure(root)
        add_history(current, None, phase=current["phase"], result="skipped")
        if verdict_check(config, current):
            seal_attempt(root, config, current)  # advancing past a review uses it, so it is not late
        if is_plan_phase(config, current):
            return await_approval(root, config, current)
        next_phase = phase_graph(config, current)[current["phase"]].get("next", DONE)
        if in_step(current) and next_phase in (COMMIT, DONE):
            # Advancing out of the last step phase commits the step; it never skips the commit.
            current["iter"] = current.get("iter", 0) + 1
            if head:
                current.setdefault("commitBase", head)
            return COMMIT_PENDING
        if next_phase == DONE:
            current.update(phase=DONE, status="done")
        else:
            enter_phase(root, config, current, next_phase)
        return "Advanced to phase '%s'." % current["phase"]

    out = update_state(root, apply)
    return advance_commit(root, config) if out is COMMIT_PENDING else out


def advance_commit(root, config):
    """Commit the step /advance moved past. git runs with no lock, then the result goes to the reloaded state."""
    state = load_state(root)
    step_id, label = state.get("stepId"), step_label(state)[0]
    commit = run_commit(root, config, state)

    def apply(current):
        if current.get("stepId") != step_id:
            raise Unchanged("The commit stands, but the workflow moved on while it ran.")
        kind, text = finish_step(root, config, current, commit)
        if kind == "failed":
            return "Committing %s failed; the step is back in phase '%s'.\n\n%s" % (
                label.lower(), current["phase"], tail(text, config.get("maxOutputChars", 4000)))
        if kind == "next":
            return "%s Now in %s, phase '%s'." % (text, step_label(current)[0].lower(), current["phase"])
        return text
    return update_state(root, apply)


def cmd_status(root):
    state = load_state(root)
    if not state:
        return "No workflow state."
    late = late_reviews(root, load_config(root), state)
    if is_feature(state):
        return "\n".join([feature_status(root, state)] + late)
    return json.dumps(dict(state, lateReviews=late) if late else state, indent=2)


def feature_status(root, state):
    """Readable summary of a feature run: branch, phase and one line per step."""
    lines = ["[specloop] feature run, status=%s" % state.get("status"), "spec: %s" % state.get("spec")]
    if state.get("branch"):
        lines.append("branch: %s (base %s)" % (state["branch"], (state.get("baseRef") or "")[:7]))
    else:
        lines.append("branch: - (created by the first go)")
    lines.append("phase: %s, iteration %d" % (state.get("phase"), state.get("iter", 0)))
    owner = state.get("owner")
    lines.append("owner: %s (since %s)" % (owner["session"][:8], owner["since"]) if owner
                 else "owner: none (every chat drives this workflow)")
    steps, note = (state.get("approvedSteps") or {}).get("steps"), ""
    if steps is None:
        try:
            with open(os.path.join(run_dir(root), "steps.json"), encoding="utf-8") as f:
                steps, note = json.load(f)["steps"], "not approved yet, "
            steps = [s for s in steps if isinstance(s, dict)]
        except (OSError, ValueError, KeyError, TypeError):
            lines.append("steps: no plan yet")
            return "\n".join(lines)
    done = sum(1 for s in steps if s.get("status") in LOCKED_STATUSES)
    lines.append("steps (%s%d/%d done):" % (note, done, len(steps)))
    rows = [(str(s.get("id", "?")), str(s.get("status", "?")), (s.get("commit") or "-")[:7], str(s.get("title", "")))
            for s in steps]
    width_id = max([len(r[0]) for r in rows] or [0])
    width_status = max([len(r[1]) for r in rows] or [0])
    for step_id, status, sha, title in rows:
        lines.append("  %s  %s  %-7s  %s" % (step_id.rjust(width_id), status.ljust(width_status), sha, title))
    return "\n".join(lines)


def cmd_reset(root):
    # state.json is deleted unread, so that reset also clears a file nothing can parse.
    with FileLock(_path(root, STATE_LOCK), STATE_LOCK_STALE_SEC):
        for target in (_path(root, "state.json"), run_dir(root)):
            if os.path.isdir(target):
                shutil.rmtree(target)
            elif os.path.exists(target):
                _retry_locked(os.remove, target)
    return "Workflow state reset."


def update_usage(config):
    update = cmd_name(config, "update")
    return "\n".join([
        "Usage: %s [check|skip|never]" % update,
        "  %s         check now, then update when a newer version is available" % update,
        "  %s check   check now and report; changes nothing" % update,
        "  %s skip    do not announce the newest available version again" % update,
        "  %s never   skip it and turn update checks off (update.mode in config.local.json)" % update,
    ])


def update_unavailable(source):
    """Why update checks cannot work for this install, or None when they can."""
    if source is None:
        return ("this install does not record where it came from; reinstall once with install.py <project> "
                "from your specloop clone to enable update checks")
    if source.get("checkable") is False:
        return source.get("reason") or "the install source is not checkable"
    if not all(isinstance(source.get(key), str) and source.get(key) for key in ("path", "remote", "branch")):
        return "the recorded source has no path, remote or branch; reinstall with install.py"
    return None


def update_report(root, source, data, version):
    """Lines that describe the versions of the last check."""
    lines = ["installed: %s" % (data.get("installed") or _read_version_file(_path(root, "VERSION")) or "unknown"),
             "clone: %s (%s)" % (_read_version_file(os.path.join(source["path"], "VERSION")) or "unknown",
                                 source["path"])]
    if data.get("error"):
        lines.append("remote: check failed (%s)" % data["error"])
    else:
        origin = "%s/%s" % (source["remote"], source["branch"])
        url = display_url(source.get("url"))
        lines.append("remote: %s (%s%s)" % (data.get("remote") or "unknown", origin, ", " + url if url else ""))
    if version:
        lines.append("available: %s" % version)
    elif data.get("skipped"):
        lines.append("available: nothing newer than the installed version (%s skipped)" % data["skipped"])
    else:
        lines.append("available: nothing newer than the installed version")
    return lines


def check_now(root, config, source):
    """Run the network check synchronously, ignoring intervalHours. Return (lines, version):
    the report and the available version. version is None with a busy lock (lines say so)."""
    if not run_update_check_locked(root, config):
        return ["A background update check is running. Try again in a moment; nothing was changed."], None
    data = load_update_state(root)
    version = available_version(root, source, data)
    return update_report(root, source, data, version), version


def set_update_mode_off(root):
    """Set update.mode to 'off' in config.local.json, keeping its other keys. Return an error text
    (and write nothing) when the file exists but cannot be used as it is."""
    path = _path(root, "config.local.json")
    try:
        with open(path, encoding="utf-8") as f:
            local = json.load(f)
    except FileNotFoundError:
        local = {}
    except (OSError, ValueError) as e:
        return "%s is not valid JSON (%s). Fix it first; nothing was changed." % (_rel("config.local.json"), e)
    if not isinstance(local, dict):
        return "%s is not a JSON object. Fix it first; nothing was changed." % _rel("config.local.json")
    if not isinstance(local.get("update"), dict):
        local["update"] = {}
    local["update"]["mode"] = "off"
    os.makedirs(_path(root), exist_ok=True)
    atomic_write(path, json.dumps(local, indent=2, ensure_ascii=False) + "\n")
    return None


def cmd_update(root, args):
    """engine.py update [check|skip|never]: manual update commands."""
    config = load_config(root)
    if len(args) > 1 or (args and args[0] not in ("check", "skip", "never")):
        return update_usage(config)
    action = args[0] if args else "apply"
    state = load_state(root)
    if action == "apply" and is_busy(state):
        return ("[specloop] A workflow is busy (status %s), so the kit is not updated now. Let it finish, or run "
                "%s to end it, then run %s again." % (state.get("status"), cmd_name(config, "reset"),
                                                      cmd_name(config, "update")))
    source = read_manifest_source(root)
    reason = update_unavailable(source)
    if reason:
        return "[specloop] Update checks are unavailable: %s." % reason.rstrip(".")
    if action == "check":
        lines, _ = check_now(root, config, source)
        return "\n".join(["[specloop] Update check:"] + lines)
    if action in ("skip", "never"):
        if action == "never":
            error = set_update_mode_off(root)
            if error:
                return "[specloop] " + error
        version = available_version(root, source, load_update_state(root))
        lines = []
        if version:
            merge_update_state(root, skipped=version)
            lines.append("[specloop] %s will not be announced or applied again; a later version is announced "
                         "as usual." % version)
        else:
            lines.append("[specloop] No newer version is available, so nothing was skipped.")
        if action == "never":
            lines.append("Update checks are off: update.mode is \"off\" in %s. To turn them back on, set "
                         "update.mode to \"notify\" or \"auto\" there, or remove the key." % _rel("config.local.json"))
        return "\n".join(lines)
    lines, version = check_now(root, config, source)
    if version:
        lines.append("[specloop] %s is available (installed %s). Applying updates is not available yet." % (
            version, _read_version_file(_path(root, "VERSION"))))
    return "\n".join(["[specloop] Update:"] + lines)


def corrupt_message(root):
    return ("[specloop] state.json is unreadable, so specloop is not enforcing anything. Run %s to clear it." %
            cmd_name(load_config(root), "reset"))


def read_hook_input():
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def run_hook(command, args):
    if os.environ.get("SPECLOOP_DISABLED") == "1":
        return None
    hook_input = read_hook_input()
    root = project_dir(hook_input)
    if not os.path.exists(_path(root, "defaults.json")):
        return None
    try:
        if command == "stop":
            return handle_stop(root, hook_input)
        if command == "pretool":
            return handle_pretool(root, hook_input)
        return handle_context(root, args[0] if args else "UserPromptSubmit", hook_input)
    except LockBusy as e:
        print("[specloop] state is locked, nothing written: %s" % e, file=sys.stderr)
        if command == "stop":
            return {"systemMessage": "[specloop] specloop was busy, so the result of this stop was not recorded. "
                                     "Run %s to continue." % cmd_name(load_config(root), "go")}
        return None
    except StateCorrupt:
        if command == "pretool":
            print(corrupt_message(root), file=sys.stderr)
            return None
        return {"systemMessage": corrupt_message(root)}


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    command, args = argv[0], argv[1:]
    if command in ("stop", "pretool", "context"):
        try:
            out = run_hook(command, args)
        except Exception as e:  # A broken hook must never break the user's session.
            print("[specloop] hook error: %s" % e, file=sys.stderr)
            return 0
        if out:
            print(json.dumps(out))
        return 0
    root = project_dir()
    if command == "update-check":
        cmd_update_check(root)
        return 0
    handlers = {
        "init": lambda: cmd_init(root, " ".join(args).strip().strip('"')),
        "init-feature": lambda: cmd_init(root, " ".join(args).strip().strip('"'), mode="feature"),
        "go": lambda: cmd_go(root),
        "pause": lambda: cmd_pause(root),
        "advance": lambda: cmd_advance(root),
        "status": lambda: cmd_status(root),
        "reset": lambda: cmd_reset(root),
        "update": lambda: cmd_update(root, args),
    }
    if command not in handlers:
        print("Unknown command: " + command)
        return 1
    try:
        print(handlers[command]())
    except LockBusy:
        print("[specloop] specloop is busy and nothing was changed. Try again.")
    except StateCorrupt:
        print(corrupt_message(root))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
