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
    engine.py quality [run|init|accept]
                                 list the quality gates, or run, set up or accept them
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
import fnmatch
import hashlib
import io
import json
import math
import os
import posixpath
import re
import shlex
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


def auto_update_notice(root, config, source, version):
    """The auto mode of update_notice, called only at a safe point (the workflow is idle) for a
    newer, not skipped `version`. Apply the update and return a one-line message saying what
    changed. A major update is never applied: it gets the notify notice plus the reason. When
    applying is refused or fails, the notify notice plus 'auto-update skipped: <reason>'. A
    notice is shown once per intervalHours; applying is tried at every safe point."""
    installed = _read_version_file(_path(root, "VERSION"))
    if is_major_update(installed, version):
        if not _announce_once(root, config, version):
            return None
        return "%s %s -> %s is a major update; run %s to apply it." % (
            update_notice_text(root, config, source, version), installed, version, cmd_name(config, "update"))
    try:
        ok, message = apply_update(root, config)
    except Exception as e:
        print("[specloop] auto-update error: %s" % e, file=sys.stderr)
        ok, message = False, "unexpected error: %s" % e
    if ok:
        new_version = _read_version_file(_path(root, "VERSION")) or version
        return ("[specloop] Auto-updated %s -> %s. Start a new Claude Code session to load the new commands "
                "and hooks." % (installed or "unknown", new_version))
    if not _announce_once(root, config, version):
        return None
    reason = " ".join(message.split())
    for prefix in ("[specloop]", "Not updated:"):
        if reason.startswith(prefix):
            reason = reason[len(prefix):].lstrip()
    return "%s auto-update skipped: %s" % (update_notice_text(root, config, source, version), reason)


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
    if version and not is_busy(state):
        if update_mode(config) == "auto":
            notice = auto_update_notice(root, config, source, version)
        elif _announce_once(root, config, version):
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
    """_render for an instruction file, which may also use {review_file} and {quality_gates}. A {quality_gates}
    that renders as nothing takes its line and the blank line after it along, so no gap is left."""
    gates = quality_gates_text(config, state)
    if not gates:
        text = re.sub(r"^\{quality_gates\}[ \t]*\r?\n(?:[ \t]*\r?\n)?", "", text, flags=re.MULTILINE)
    return _render(text, state, review_file=review_file(config, state), quality_gates=gates)


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
    if QUALITY_REPORT in names:
        files.append(_rel("run", QUALITY_REPORT))
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


# --- quality gates ---------------------------------------------------------------

GATE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
QUALITY_KEYS = ("timeoutSec", "failFast", "legacy", "gates")
GATE_KEYS = ("command", "phases", "steps", "level", "timeoutSec", "hint", "passExitCodes", "metric", "legacy",
             "changedGlob", "protect", "forbid")
METRIC_KEYS = ("pattern", "json", "file", "min", "max")
FORBID_KEYS = ("pattern", "glob", "message")
GATE_STEPS = ("each", "last")
GATE_LEVELS = ("block", "warn")
LEGACY_MODES = ("baseline", "ratchet")
# Shell conventions for "not executable" and "not found": a gate exiting so is broken, never passed.
SHELL_EXIT_REASONS = {126: "command not executable (exit 126)", 127: "command not found (exit 127)"}
SHELL_EXIT_CODES = tuple(sorted(SHELL_EXIT_REASONS))
CHANGED_FILES_PLACEHOLDERS = ("{changed_files}", "{changed_files_file}")
MAX_CHANGED_FILES_CHARS = 8000
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DEFAULT_GATE_PHASES = ["implement"]


def quality_block(config):
    block = (config or {}).get("quality") if isinstance(config, dict) else None
    return block if isinstance(block, dict) else {}


def quality_gates(config):
    """The merged gates in declared order. A gate set to null (for example in config.local.json) is removed."""
    gates = quality_block(config).get("gates")
    if not isinstance(gates, dict):
        return collections.OrderedDict()
    return collections.OrderedDict((name, gate) for name, gate in gates.items() if gate is not None)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _string_list(value):
    return isinstance(value, list) and all(_non_empty_string(item) for item in value)


def _regex_error(pattern):
    """None when `pattern` compiles, else the reason."""
    try:
        re.compile(pattern)
    except Exception as e:  # re.error, but also RecursionError or OverflowError on hostile input
        return str(e) or e.__class__.__name__
    return None


def _phase_definitions(config):
    """(where, name, definition) for every phase a gate may name: `phases` and `feature.stepPhases`."""
    found = []
    for where, graph in (("phases", config.get("phases")),
                         ("feature.stepPhases", (config.get("feature") or {}).get("stepPhases")
                          if isinstance(config.get("feature"), dict) else None)):
        if isinstance(graph, dict):
            found.extend((where, name, phase) for name, phase in graph.items() if phase)
    return found


def _validate_metric(where, metric):
    if not isinstance(metric, dict):
        return ["%s.metric must be an object" % where]
    errors = ["%s.metric has an unknown key %r" % (where, key) for key in metric if key not in METRIC_KEYS]
    sources = [key for key in ("pattern", "json") if key in metric]
    if len(sources) != 1:
        errors.append("%s.metric needs exactly one of 'pattern' and 'json'" % where)
    if "pattern" in metric:
        pattern = metric["pattern"]
        if not _non_empty_string(pattern):
            errors.append("%s.metric.pattern must be a non-empty string" % where)
        else:
            reason = _regex_error(pattern)
            if reason:
                errors.append("%s.metric.pattern is not a valid regular expression: %s" % (where, reason))
            elif re.compile(pattern).groups < 1:
                errors.append("%s.metric.pattern needs a group that captures the number" % where)
    if "json" in metric and not _non_empty_string(metric["json"]):
        errors.append("%s.metric.json must be a non-empty dotted key path" % where)
    if "file" in metric and not _non_empty_string(metric["file"]):
        errors.append("%s.metric.file must be a non-empty string" % where)
    limits = [key for key in ("min", "max") if key in metric]
    if not limits:
        errors.append("%s.metric needs at least one of 'min' and 'max'" % where)
    for key in limits:
        if not _is_number(metric[key]):
            errors.append("%s.metric.%s must be a number" % (where, key))
    return errors


def _validate_forbid(where, forbid):
    if not isinstance(forbid, list):
        return ["%s.forbid must be a list" % where]
    errors = []
    for i, entry in enumerate(forbid):
        at = "%s.forbid[%d]" % (where, i)
        if isinstance(entry, dict):
            errors.extend("%s has an unknown key %r" % (at, key) for key in entry if key not in FORBID_KEYS)
            pattern = entry.get("pattern")
            if "glob" in entry and not _string_list(entry["glob"]):
                errors.append("%s.glob must be a list of non-empty strings" % at)
            if "message" in entry and not isinstance(entry["message"], str):
                errors.append("%s.message must be a string" % at)
            at += ".pattern"
        else:
            pattern = entry
        if not _non_empty_string(pattern):
            errors.append("%s must be a non-empty string (a regular expression)" % at)
            continue
        reason = _regex_error(pattern)
        if reason:
            errors.append("%s is not a valid regular expression: %s" % (at, reason))
    return errors


def _validate_gate_phases(where, phases, definitions):
    if not _string_list(phases):
        return ["%s.phases must be a list of phase names" % where]
    errors = []
    for name in phases:
        matches = [(graph, phase) for graph, phase_name, phase in definitions if phase_name == name]
        if not matches:
            errors.append("%s.phases names %r, which is in neither phases nor feature.stepPhases" % (where, name))
        for graph, phase in matches:
            if isinstance(phase, dict) and phase.get("allowEdits"):
                errors.append("%s.phases names %r, but %s.%s sets allowEdits, so the agent could not fix a "
                              "failure there" % (where, name, graph, name))
    return errors


def _validate_gate(where, gate, default_legacy, definitions):
    """Errors of one gate, and whether it needs git ({changed_files} or forbid)."""
    if not isinstance(gate, dict):
        return ["%s must be an object" % where], False
    errors = ["%s has an unknown key %r" % (where, key) for key in gate if key not in GATE_KEYS]
    command = gate.get("command")
    if not _non_empty_string(command):
        errors.append("%s.command is required and must be a non-empty string" % where)
    errors.extend(_validate_gate_phases(where, gate.get("phases", DEFAULT_GATE_PHASES), definitions))
    for key, allowed in (("steps", GATE_STEPS), ("level", GATE_LEVELS), ("legacy", LEGACY_MODES)):
        if key in gate and gate[key] not in allowed:
            errors.append("%s.%s must be one of %s" % (where, key, ", ".join('"%s"' % a for a in allowed)))
    if "timeoutSec" in gate and not (_is_int(gate["timeoutSec"]) and gate["timeoutSec"] > 0):
        errors.append("%s.timeoutSec must be a positive integer" % where)
    if "hint" in gate and not isinstance(gate["hint"], str):
        errors.append("%s.hint must be a string" % where)
    if "passExitCodes" in gate:
        codes = gate["passExitCodes"]
        if not (isinstance(codes, list) and codes and all(_is_int(c) for c in codes)):
            errors.append("%s.passExitCodes must be a non-empty list of integers" % where)
        elif any(c in SHELL_EXIT_CODES for c in codes):
            errors.append("%s.passExitCodes may not contain 126 or 127 (the shell's 'not executable' and "
                          "'not found')" % where)
    for key in ("changedGlob", "protect"):
        if key in gate and not _string_list(gate[key]):
            errors.append("%s.%s must be a list of non-empty strings" % (where, key))
    if "forbid" in gate:
        errors.extend(_validate_forbid(where, gate["forbid"]))
    metric = gate.get("metric")
    if "metric" in gate:
        errors.extend(_validate_metric(where, metric))
    legacy = gate.get("legacy", default_legacy)
    if legacy == "ratchet":
        if not isinstance(metric, dict):
            if "legacy" in gate:
                errors.append('%s sets legacy "ratchet", which needs a metric with exactly one of \'min\' and '
                              '\'max\'' % where)
        elif ("min" in metric) == ("max" in metric):
            errors.append('%s uses legacy "ratchet", so its metric needs exactly one of \'min\' (higher is '
                          'better) and \'max\' (lower is better)' % where)
    needs_git = (isinstance(command, str) and any(p in command for p in CHANGED_FILES_PLACEHOLDERS)) or \
        bool(gate.get("forbid"))
    return errors, needs_git


def is_git_work_tree(root, config):
    ok, out = git(root, config, "rev-parse", "--is-inside-work-tree")
    return ok and out == "true"


def validate_quality(root, config):
    """Validate the merged `quality` block. Return a list of error strings (empty means valid); never raises."""
    try:
        return _validate_quality(root, config)
    except Exception as e:  # validation must never break a hook or a command
        return ["quality could not be validated: %s" % (str(e) or e.__class__.__name__)]


def unreadable_quality(config):
    """Why no gate can be read from the quality block, or None when the gates can be listed."""
    if not isinstance(config, dict) or "quality" not in config:
        return None
    if not isinstance(config["quality"], dict):
        return "quality must be an object"
    if not isinstance(config["quality"].get("gates", {}), dict):
        return "quality.gates must be an object that maps gate names to gates"
    return None


def _quality_key_errors(block):
    """Errors of the quality block's own keys other than `gates`."""
    errors = ["quality has an unknown key %r" % key for key in block if key not in QUALITY_KEYS]
    if "timeoutSec" in block and not (_is_int(block["timeoutSec"]) and block["timeoutSec"] > 0):
        errors.append("quality.timeoutSec must be a positive integer")
    if "failFast" in block and not isinstance(block["failFast"], bool):
        errors.append("quality.failFast must be true or false")
    if block.get("legacy", "baseline") not in LEGACY_MODES:
        errors.append("quality.legacy must be one of %s" % ", ".join('"%s"' % a for a in LEGACY_MODES))
    return errors


def _named_gate_errors(name, gate, block, definitions):
    """Errors of one gate including its name, and whether it needs git."""
    errors = []
    if not (isinstance(name, str) and GATE_NAME.fullmatch(name)):
        errors.append("gate name %r must match [A-Za-z0-9][A-Za-z0-9_-]* (it becomes a file name)" % name)
    gate_errors, needs_git = _validate_gate("quality.gates.%s" % name, gate, block.get("legacy", "baseline"),
                                            definitions)
    return errors + gate_errors, needs_git


def _git_errors(names):
    return ["quality.gates.%s uses {changed_files}, {changed_files_file} or forbid, which need a git work tree, "
            "but the project is not one" % name for name in names]


def _validate_quality(root, config):
    if not isinstance(config, dict) or "quality" not in config:
        return []
    block = config["quality"]
    if not isinstance(block, dict):
        return [unreadable_quality(config)]
    errors = _quality_key_errors(block)
    if unreadable_quality(config):
        return errors + [unreadable_quality(config)]
    definitions = _phase_definitions(config)
    git_users = []
    for name, gate in quality_gates(config).items():
        gate_errors, needs_git = _named_gate_errors(name, gate, block, definitions)
        errors.extend(gate_errors)
        if needs_git:
            git_users.append(name)
    if git_users and not is_git_work_tree(root, config):
        errors.extend(_git_errors(git_users))
    return errors


def gate_definition_errors(root, config, selected):
    """{name: errors} for the selected gates whose definition is invalid now. The config may have changed since
    /sl-go validated it, so the Stop hook checks again; errors of the block's own keys count for every gate.
    Never raises."""
    try:
        block = quality_block(config)
        common = _quality_key_errors(block)
        definitions = _phase_definitions(config)
        problems, git_users = collections.OrderedDict(), []
        for name, gate in selected:
            errors, needs_git = _named_gate_errors(name, gate, block, definitions)
            if needs_git:
                git_users.append(name)
            if common or errors:
                problems[name] = common + errors
        if git_users and not is_git_work_tree(root, config):
            for name, error in zip(git_users, _git_errors(git_users)):
                problems.setdefault(name, []).append(error)
        return problems
    except Exception as e:  # validation must never break a hook
        reason = "could not be validated: %s" % (str(e) or e.__class__.__name__)
        return collections.OrderedDict((name, [reason]) for name, _ in selected)


def quality_errors_message(config, errors):
    return ("Not started: the quality configuration is invalid:\n%s\n\nFix it in %s (or %s), then run %s again." % (
        "\n".join("- " + e for e in errors), _rel("config.json"), _rel("config.local.json"), cmd_name(config, "go")))


QUALITY_DIR = "quality"
QUALITY_MANUAL_DIR = "quality-manual"  # the logs of `quality run`, which never touches QUALITY_DIR
TRUNCATED = "...(truncated)...\n"


STOP_MARGIN_SEC = 60
DEFAULT_QUALITY_TIMEOUT_SEC = 1800


def _seconds(value, default):
    return value if _is_number(value) and value > 0 else default


def quality_timeout(config):
    """quality.timeoutSec as configured: the limit for all gates of one Stop together."""
    return _seconds(quality_block(config).get("timeoutSec"), DEFAULT_QUALITY_TIMEOUT_SEC)


def stop_limit(config):
    """Seconds a Stop may take, as install.py sizes the hook: the check, all gates and a margin. A mark left by a
    Stop that was killed is stale after this long."""
    check = _seconds((config or {}).get("checkTimeoutSec", 900), 900)
    return check + quality_timeout(config) + STOP_MARGIN_SEC


def quality_budget(root, config):
    """Seconds all gates of one Stop may use together: quality.timeoutSec, but never more than the Stop hook
    installed in settings.json leaves after the check. install.py records that hook's timeout in manifest.json as
    stopTimeoutSec; an old manifest has none, and then the configured value is used."""
    configured = quality_timeout(config)
    hosted = _read_json_tolerant(_path(root, "manifest.json")).get("stopTimeoutSec")
    if not _is_number(hosted):
        return configured
    check = _seconds((config or {}).get("checkTimeoutSec", 900), 900)
    return min(configured, hosted - check - STOP_MARGIN_SEC)


def _clock():
    return time.monotonic()


def _seconds_text(value):
    return ("%.1f" % value).rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def planning_phase(config, state):
    """True in a planning or replanning phase, where gates never run."""
    if in_replan(config, state):
        return True
    return not in_step(state) if is_feature(state) else state["phase"] == "plan"


def on_last_step(state):
    """True in a feature step when no step after the current one in approvedSteps is still pending. Read on each
    Stop, so a replan that adds a step moves the answer to the new last step."""
    index, _ = current_step(state)
    if index is None:
        return True
    return not any(isinstance(step, dict) and step.get("status") == "pending"
                   for step in state["approvedSteps"]["steps"][index + 1:])


def gate_runs_on_step(gate, state):
    """False for a `steps: "last"` gate on a feature step that is not the last. `steps` is ignored in single mode."""
    if not (isinstance(gate, dict) and is_feature(state) and in_step(state)):
        return True
    return gate.get("steps", "each") != "last" or on_last_step(state)


def selected_gates(config, state):
    """(name, gate) for every gate that holds the current phase, in declared order. A gate that is not an object
    or whose `phases` is not a list of names holds every phase, so it comes out broken instead of being skipped."""
    if planning_phase(config, state):
        return []
    phase = state["phase"]
    selected = []
    for name, gate in quality_gates(config).items():
        phases = gate.get("phases", DEFAULT_GATE_PHASES) if isinstance(gate, dict) else None
        if (not _string_list(phases) or phase in phases) and gate_runs_on_step(gate, state):
            selected.append((name, gate))
    return selected


def gate_log_path(root, name, directory=QUALITY_DIR):
    return os.path.join(run_dir(root), directory, name + ".log")


def write_gate_log(root, name, raw, directory=QUALITY_DIR):
    """Replace run/<directory>/<name>.log (run/quality/ unless a manual run says otherwise). A name that is not a
    valid gate name gets no file. Never raises."""
    if not (isinstance(name, str) and GATE_NAME.fullmatch(name)):
        return
    try:
        os.makedirs(os.path.dirname(gate_log_path(root, name, directory)), exist_ok=True)
        with open(gate_log_path(root, name, directory), "wb") as f:
            f.write(raw)
    except OSError as e:
        print("[specloop] could not write the log of gate '%s': %s" % (name, e), file=sys.stderr)


def gate_record(state, name, result, exit_code, duration, **extra):
    return dict({"name": name, "result": result, "exitCode": exit_code, "durationSec": round(duration, 2),
                 "phase": state["phase"], "stepId": state.get("stepId"), "finishedAt": now()}, **extra)


def _metric_number(value):
    """The finite number `value` stands for, or None. A bool, a string and null are not numbers."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def _number_text(value):
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else "%.10g" % value
    return str(value)


def _metric_from_pattern(metric, text):
    """(value, None) from the first group of the last match, or (None, why not)."""
    match = None
    for match in re.finditer(metric["pattern"], text):
        pass
    if match is None:
        return None, "pattern %r matches nothing in the %s" % (metric["pattern"], metric_source_text(metric))
    group = match.group(1)
    try:
        value = _metric_number(float(group)) if group is not None else None
    except ValueError:
        value = None
    if value is None:
        return None, "pattern %r captured %r, which is not a number" % (metric["pattern"], group)
    return value, None


def _metric_from_json(metric, text):
    """(value, None) from the dotted key path of a JSON document, or (None, why not)."""
    try:
        node = json.loads(text.lstrip("﻿"))
    except ValueError as e:
        return None, "the %s is not valid JSON (%s)" % (metric_source_text(metric), e)
    path = metric["json"]
    for key in path.split("."):
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return None, "the JSON key %r is missing in the %s" % (path, metric_source_text(metric))
    value = _metric_number(node)
    if value is None:
        return None, "the JSON value at %r is not a number: %s" % (path, json.dumps(node)[:80])
    return value, None


def metric_source_text(metric):
    return "file %s" % metric["file"] if metric.get("file") else "gate output"


def read_metric(root, metric, output):
    """Read the number a gate's `metric` asks for. Return (value, None), or (None, reason) when the gate cannot
    give an answer: a missing or unreadable file, no match, invalid JSON, a missing key or a value that is not a
    number. Never raises."""
    try:
        text = output
        if metric.get("file"):
            rel = metric["file"]
            if os.path.isabs(rel) or os.path.normpath(rel).split(os.sep)[0] == "..":
                return None, "metric file %s is not inside the project" % rel
            try:
                with open(os.path.join(root, rel), "rb") as f:
                    text = f.read().decode("utf-8", errors="replace")
            except OSError as e:
                return None, "metric file %s cannot be read: %s" % (rel, e.strerror or e)
        return _metric_from_pattern(metric, text) if "pattern" in metric else _metric_from_json(metric, text)
    except Exception as e:  # a hostile pattern or document must not raise from the hook
        return None, "the metric cannot be read: %s" % (str(e) or e.__class__.__name__)


def metric_limit_message(metric, value):
    """Why `value` is outside the inclusive limits of `metric`, or None when it is inside."""
    if "min" in metric and value < metric["min"]:
        return "metric %s is below the minimum %s" % (_number_text(value), _number_text(metric["min"]))
    if "max" in metric and value > metric["max"]:
        return "metric %s is above the maximum %s" % (_number_text(value), _number_text(metric["max"]))
    return None


def baseline_values(state):
    """state.qualityBaseline as a dict: {gate: number measured before the first edit}."""
    values = state.get("qualityBaseline")
    return values if isinstance(values, dict) else {}


def gate_legacy(config, gate):
    """How the gate treats existing problems: its own `legacy`, else quality.legacy, else "baseline"."""
    own = gate.get("legacy") if isinstance(gate, dict) else None
    chosen = own if own is not None else quality_block(config).get("legacy", "baseline")
    return chosen if chosen in LEGACY_MODES else "baseline"


def is_ratchet_gate(config, gate):
    """True for a gate whose legacy mode is "ratchet" and whose metric has exactly one numeric limit."""
    metric = gate.get("metric") if isinstance(gate, dict) else None
    return (gate_legacy(config, gate) == "ratchet" and isinstance(metric, dict)
            and ("min" in metric) != ("max" in metric) and _is_number(metric.get("min", metric.get("max"))))


def no_worse_than(metric, value, baseline):
    """True when `value` is at least the baseline for a `min` metric (higher is better), at most for `max`."""
    return value >= baseline if "min" in metric else value <= baseline


def run_quality_gate(root, config, state, name, gate, budget_left=None, command=None, baseline=None, measure=False,
                     log_dir=QUALITY_DIR):
    """Run one valid gate with no lock. Return (record, output): the record goes to state.qualityResults, the
    complete output to run/quality/<name>.log. Never raises. A gate that cannot give an answer (exit code 126 or
    127, a timeout, a command that cannot be started, a used-up time budget) is `broken`, and its record says why
    in `reason`. `budget_left` is what remains of the time budget of this Stop; the gate's timeout is the smaller
    of that and its own. `command` replaces gate["command"] once its placeholders are expanded. `baseline` is the
    value a ratchet gate was measured at before the first edit. With `measure` the gate is only measured: the
    metric is read whatever the exit code is, no limit is applied and the result is `pass` unless the gate is
    broken. The log goes to run/<log_dir>/."""
    timeout = gate.get("timeoutSec", config.get("checkTimeoutSec", 900))
    started = time.monotonic()
    exit_code, timed_out, reason = None, False, None
    limited_by_budget = False
    if not _is_number(timeout) or timeout <= 0:
        reason = "timeoutSec is not a positive number: %r" % (timeout,)
        raw = ("[specloop] %s\n" % reason).encode("utf-8")
    elif budget_left is not None and budget_left <= 0:
        reason = "the time budget for all gates (quality.timeoutSec) is exhausted"
        raw = ("[specloop] %s; the gate did not run\n" % reason).encode("utf-8")
    else:
        if budget_left is not None and budget_left < timeout:
            timeout, limited_by_budget = budget_left, True
        try:
            shell_command = command or gate["command"]
            proc = run_limited([resolve_shell(config), "-c", shell_command], timeout, cwd=root, merge_stderr=True)
            raw, exit_code, timed_out = proc.stdout, proc.returncode, proc.timed_out
            if timed_out:
                reason = "timed out after %s seconds" % _seconds_text(timeout)
                if limited_by_budget:
                    reason += " (all that was left of the time budget for all gates)"
            else:
                reason = SHELL_EXIT_REASONS.get(exit_code)
            if reason:
                raw += ("\n[specloop] %s\n" % reason).encode("utf-8")
        except (OSError, ValueError) as e:
            reason = "the command could not be run: %s" % e
            raw = ("[specloop] %s\n" % reason).encode("utf-8")
    failed_level = "warn" if gate.get("level", "block") == "warn" else "fail"
    value = None
    if reason:
        result = "broken"
    elif measure or exit_code in gate.get("passExitCodes", [0]):
        result = "pass"
        metric = gate.get("metric")
        if isinstance(metric, dict):
            value, why = read_metric(root, metric, raw.decode("utf-8", errors="replace"))
            if why:
                result, reason = "broken", why
                raw += ("\n[specloop] %s\n" % why).encode("utf-8")
            elif not measure:
                limit = metric_limit_message(metric, value)
                held = _metric_number(baseline) if is_ratchet_gate(config, gate) else None
                if limit and held is not None:
                    if no_worse_than(metric, value, held):
                        raw += ("\n[specloop] %s, but no worse than the baseline %s (ratchet)\n" % (
                            limit, _number_text(held))).encode("utf-8")
                        limit = None
                    else:
                        limit = "%s and %s the baseline %s (ratchet)" % (
                            limit, "below" if "min" in metric else "above", _number_text(held))
                if limit:
                    result = failed_level
                    raw += ("\n[specloop] %s\n" % limit).encode("utf-8")
    else:
        result = failed_level
    duration = time.monotonic() - started
    write_gate_log(root, name, raw, log_dir)
    record = gate_record(state, name, result, exit_code, duration)
    if value is not None:
        record["metric"] = value
    if baseline is not None and not measure and is_ratchet_gate(config, gate):
        record["baseline"] = baseline
    if reason:
        record["reason"] = reason
    if timed_out:
        record["timedOut"] = True
    return record, raw.decode("utf-8", errors="replace")


class ChangedFilesError(Exception):
    """The set of changed files cannot be determined."""


def changed_files_base(state, gate):
    """The comparison point of {changed_files}: HEAD for a feature-mode gate that runs on each step (the files
    changed since the last step commit), else the run's baseRef. A run with no baseRef (one that started before
    single mode recorded it) falls back to HEAD."""
    if is_feature(state) and in_step(state) and gate.get("steps", "each") != "last":
        return "HEAD"
    return state.get("baseRef") or "HEAD"


def untracked_files(root, config):
    """Untracked files, as paths relative to the repository's top directory. Raises ChangedFilesError."""
    ok, error, entries = _git_status(root, config)
    if not ok:
        raise ChangedFilesError("git status failed: %s" % error)
    return [path for code, path in entries if code == "??"]


def changed_file_sets(root, config, state, gate):
    """(comparison point, tracked, untracked): the project-relative paths, with forward slashes and sorted, of
    the files this run changed. `tracked` holds tracked changes against the comparison point
    (`changed_files_base`), staged and unstaged, without deleted files; `untracked` holds untracked files that
    were not already untracked when the run started (`preexistingUntracked`). .claude/specloop/ is left out.
    The comparison point is EMPTY_TREE in a repository with no commits. Raises ChangedFilesError when git cannot
    answer. Holds no lock."""
    ok, prefix = git(root, config, "rev-parse", "--show-prefix")
    if not ok:
        raise ChangedFilesError("git rev-parse --show-prefix failed: %s" % prefix)
    point = changed_files_base(state, gate)
    if point == "HEAD" and not git(root, config, "rev-parse", "--verify", "--quiet", "HEAD")[0]:
        point = EMPTY_TREE  # no commits yet: everything staged counts as changed
    ok, out = git(root, config, "diff", "--name-only", "--diff-filter=d", "-z", point, "--", raw=True)
    if not ok:
        raise ChangedFilesError("git diff %s failed: %s" % (point[:12], out.strip()))
    tracked = set(p for p in out.split("\0") if p)
    untracked = set(untracked_files(root, config))
    own_dir = prefix + _rel() + "/"
    preexisting = set(state.get("preexistingUntracked", []))

    def project_paths(found):
        return sorted(p[len(prefix):] for p in found - preexisting if p.startswith(prefix) and not p.startswith(own_dir))
    return point, project_paths(tracked), project_paths(untracked - tracked)


def changed_files(root, config, state, gate):
    """Project-relative paths, with forward slashes and sorted, of the files this run changed, for the
    placeholders {changed_files} and {changed_files_file}: the tracked and untracked files of
    `changed_file_sets`, narrowed by the gate's `changedGlob` patterns. Raises ChangedFilesError when git cannot
    answer. Holds no lock."""
    _, tracked, untracked = changed_file_sets(root, config, state, gate)
    paths = sorted(tracked + untracked)
    patterns = gate.get("changedGlob")
    if patterns:
        paths = [p for p in paths if any(fnmatch.fnmatchcase(p, pattern) for pattern in patterns)]
    return paths


def changed_files_file_path(name, directory=QUALITY_DIR):
    return _rel("run", directory, name + ".files")


def write_changed_files_file(root, name, paths, directory=QUALITY_DIR):
    path = os.path.join(root, *changed_files_file_path(name, directory).split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(p + "\n" for p in paths))


def _unrunnable(state, name, started, result, reason, text):
    """(None, record, log text) for a gate that does not run because of its changed-file set."""
    record = gate_record(state, name, result, None, time.monotonic() - started, reason=reason)
    return None, record, "[specloop] %s\n" % text


def expand_changed_files(root, config, state, name, gate, directory=QUALITY_DIR):
    """Fill {changed_files} and {changed_files_file} in the gate's command. Return (command, record, output):
    for a gate that can run, the expanded command and no record; otherwise the command is None and the record
    is `skipped` (no changed files) or `broken` (git failed, or the list is too long for a command line), with
    the text for its log. A command with neither placeholder is returned as it is. {changed_files_file} lives in
    run/<directory>/. Never raises."""
    command = gate["command"]
    uses_list, uses_file = (p in command for p in ("{changed_files}", "{changed_files_file}"))
    if not (uses_list or uses_file):
        return command, None, None
    started = time.monotonic()
    try:
        paths = changed_files(root, config, state, gate)
        if paths and uses_file:
            write_changed_files_file(root, name, paths, directory)
    except (ChangedFilesError, OSError) as e:
        reason = "the changed files cannot be determined: %s" % (str(e) or e.__class__.__name__)
        return _unrunnable(state, name, started, "broken", reason, reason)
    if not paths:
        reason = "no changed files%s" % (" match changedGlob" if gate.get("changedGlob") else "")
        return _unrunnable(state, name, started, "skipped", reason, "skipped: %s; the command did not run" % reason)
    listing = " ".join(shlex.quote(p) for p in paths)
    if uses_list and len(listing) > MAX_CHANGED_FILES_CHARS:
        reason = ("{changed_files} would be %d characters long, above the limit of %d; use {changed_files_file} "
                  "(a file with one path per line) in the command instead" % (len(listing), MAX_CHANGED_FILES_CHARS))
        return _unrunnable(state, name, started, "broken", reason, reason)
    command = command.replace("{changed_files_file}", shlex.quote(changed_files_file_path(name, directory)))
    return command.replace("{changed_files}", listing), None, None


def invalid_gate_record(root, state, name, errors):
    """The broken record of a gate whose definition is invalid; its command does not run."""
    reason = "invalid definition: %s" % "; ".join(errors)
    write_gate_log(root, name, ("[specloop] the gate did not run: %s\n" % reason).encode("utf-8"))
    return gate_record(state, name, "broken", None, 0, reason=reason)


def broken_outcome(records, paused_for, outputs=None):
    """The outcome of a Stop whose gates could not give an answer. `records` are every gate that ran or was
    judged; the details name each broken gate and its reason. `outputs` maps gate names to their output."""
    broken = [r for r in records if r["result"] == "broken"]
    if broken:
        lines = ["Quality gate '%s' is broken: %s" % (r["name"], r["reason"]) for r in broken]
    else:
        lines = ["The quality configuration is broken: %s" % paused_for["reason"]]
    details = "\n".join(lines)
    return {"result": "broken", "details": details, "full": details, "quality": records, "pausedFor": paused_for,
            "qualityOutputs": outputs or {}}


def _gate_exit(record):
    if record.get("protection"):
        return "protection"
    if record.get("timedOut"):
        return "timed out"
    if record["exitCode"] is None:
        return "could not be run"
    return "exit code %d" % record["exitCode"]


def _shares(lengths, budget):
    """Split budget over texts of the given lengths: short texts take what they need, long ones share the rest."""
    shares = [0] * len(lengths)
    remaining = max(budget, 0)
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for done, i in enumerate(order):
        shares[i] = min(lengths[i], remaining // (len(order) - done))
        remaining -= shares[i]
    return shares


def _clip_tail(text, size):
    if len(text) <= size:
        return text
    return TRUNCATED + text[len(text) - (size - len(TRUNCATED)):] if size > len(TRUNCATED) else ""


def gate_failure(config, runs):
    """(details, full) for a gate run with failed block gates. `runs` is a list of (record, output). The first
    line names every failed gate; details stay within maxOutputChars, full holds every output complete."""
    gates = quality_gates(config)
    failed = [(r, o) for r, o in runs if r["result"] == "fail"]
    warned = [(r, o) for r, o in runs if r["result"] == "warn"]
    first = "Quality gates failed: %s\n" % ", ".join(r["name"] for r, _ in failed)
    first += "The complete output of each gate is in %s.\n" % _rel("run", QUALITY_DIR, "<gate>.log")
    sections = []
    for record, output in failed + warned:
        if record["result"] == "warn":
            title = "\n### %s (%s; a warning, it does not block)\n" % (record["name"], _gate_exit(record))
        else:
            title = "\n### %s (%s)\n" % (record["name"], _gate_exit(record))
        hint = (gates.get(record["name"]) or {}).get("hint")
        if isinstance(hint, str) and hint:
            title += "Hint: %s\n" % hint
        sections.append((title, output.rstrip("\n")))
    full = first + "".join(title + output + "\n" for title, output in sections)
    limit = config.get("maxOutputChars", 4000)
    fixed = len(first) + sum(len(title) + 1 for title, _ in sections)
    shares = _shares([len(output) for _, output in sections], limit - fixed)
    details = first + "".join(title + _clip_tail(output, share) + "\n"
                              for (title, output), share in zip(sections, shares))
    return details[:limit], full


def gate_pass_details(details, runs):
    """The check's details plus a note on the gates that passed and those that only warned."""
    passed = [r["name"] for r, _ in runs if r["result"] == "pass"]
    warned = ["%s (%s, see %s)" % (r["name"], _gate_exit(r), _rel("run", QUALITY_DIR, r["name"] + ".log"))
              for r, _ in runs if r["result"] == "warn"]
    notes = []
    if passed:
        notes.append("quality gates passed: %s" % ", ".join(passed))
    if warned:
        notes.append("quality gate warnings: %s" % ", ".join(warned))
    return "; ".join([details] + notes)


def _outputs_of(runs):
    return {record["name"]: output for record, output in runs}


UNREADABLE = "unreadable"  # stands for the hash of a protected file that cannot be read
FINGERPRINT_CHUNK = 1 << 16
MAX_LISTED_PATHS = 20
MAX_HIT_CHARS = 200
BINARY_SNIFF_BYTES = 8000
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _sha256_file(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(FINGERPRINT_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return UNREADABLE
    return digest.hexdigest()


def protect_key(entry):
    """A `protect` entry as a project-relative path with forward slashes and no trailing slash."""
    key = posixpath.normpath(entry.strip().replace("\\", "/"))
    return key[2:] if key.startswith("./") else key


def protected_entries(config):
    """The distinct `protect` entries of every gate, as `protect_key`s in declared order."""
    keys = []
    for gate in quality_gates(config).values():
        entries = gate.get("protect") if isinstance(gate, dict) else None
        if _string_list(entries):
            keys.extend(k for k in dict.fromkeys(protect_key(e) for e in entries) if k not in keys)
    return keys


def is_protected_path(config, rel):
    """True when the project-relative path `rel` is a `protect` entry of any gate or lies under one."""
    if not rel:
        return False
    rel = os.path.normcase(rel).replace("\\", "/")
    for key in protected_entries(config):
        key = os.path.normcase(key).replace("\\", "/")
        if key in ("", ".", "..") or key.startswith("../"):
            continue
        if rel == key or rel.startswith(key + "/"):
            return True
    return False


GUARDED_COMMANDS = re.compile(r"""run\.sh["']?\s+(?:advance\b|quality\s+accept\b)""")


def protected_fingerprint(root, key):
    """{project-relative path: SHA-256, or None when the path is missing} for one protect entry. A directory is
    walked recursively and contributes each file in it; a missing entry contributes itself with None."""
    full = os.path.join(root, *key.split("/"))
    if not os.path.isdir(full):
        return {key: _sha256_file(full) if os.path.isfile(full) else None}
    found = {}
    for dirpath, dirnames, filenames in os.walk(full):
        dirnames.sort()
        for file_name in sorted(filenames):
            path = os.path.join(dirpath, file_name)
            found[key + "/" + os.path.relpath(path, full).replace(os.sep, "/")] = _sha256_file(path)
    return found


def _without_nulls(value):
    if isinstance(value, dict):
        return dict((k, _without_nulls(v)) for k, v in value.items() if v is not None)
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


def quality_block_fingerprint(config):
    """SHA-256 of the merged quality block with null values removed, so a gate set to null in config.local.json
    and a gate that was never declared are the same."""
    text = json.dumps(_without_nulls(quality_block(config)), sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def quality_fingerprints(root, config):
    """What state.qualityFingerprints holds: the fingerprint of the quality block and, per protected path of any
    gate, the hash of each file. The work grows with the size of the protected trees. Holds no lock. Returns None
    when it cannot be computed, and never raises."""
    try:
        return {"quality": quality_block_fingerprint(config),
                "protected": dict((key, protected_fingerprint(root, key)) for key in protected_entries(config))}
    except Exception as e:
        print("[specloop] could not fingerprint the quality gates: %s" % (str(e) or e.__class__.__name__),
              file=sys.stderr)
        return None


def stored_fingerprints(state):
    stored = state.get("qualityFingerprints")
    return stored if isinstance(stored, dict) else {}


def quality_block_changed(config, state):
    """True when the merged quality block differs from the one fingerprinted at the run's first /go."""
    before = stored_fingerprints(state).get("quality")
    return isinstance(before, str) and before != quality_block_fingerprint(config)


def _path_changes(before, current):
    """Lines `path (changed|created|deleted)` for the files whose hash differs between two {path: hash or None}
    maps."""
    lines = []
    for path in sorted(set(before) | set(current)):
        old, new = before.get(path), current.get(path)
        if old != new:
            lines.append("%s (%s)" % (path, "created" if old is None else "deleted" if new is None else "changed"))
    return lines


def protected_changes(root, state, gate):
    """Lines naming the files in the gate's `protect` paths that differ from the stored fingerprints: changed,
    created (a missing file or a new file in a protected directory) or deleted. Paths whose entry was not
    fingerprinted are not compared, because a changed quality block fails the gate on its own."""
    entries = gate.get("protect") if isinstance(gate, dict) else None
    stored = stored_fingerprints(state).get("protected")
    if not (_string_list(entries) and isinstance(stored, dict)):
        return []
    lines = []
    for key in dict.fromkeys(protect_key(e) for e in entries):
        before = stored.get(key)
        if isinstance(before, dict):
            lines.extend(_path_changes(before, protected_fingerprint(root, key)))
    return lines


def fingerprint_changes(before, after):
    """(protected path lines, quality block changed) between two fingerprint dicts, as state.qualityFingerprints
    holds them. An entry that only one side fingerprinted is not compared, as in `protected_changes`."""
    before_protected, after_protected = before.get("protected"), after.get("protected")
    paths = []
    if isinstance(before_protected, dict) and isinstance(after_protected, dict):
        for key, old in before_protected.items():
            new = after_protected.get(key)
            if isinstance(old, dict) and isinstance(new, dict):
                paths.extend(_path_changes(old, new))
    old_block = before.get("quality")
    return paths, isinstance(old_block, str) and old_block != after.get("quality")


def forbid_entries(gate):
    """The gate's `forbid` entries as (compiled pattern, glob list or None, message or None). An entry that is
    not valid is left out: validation reports it."""
    entries = []
    forbid = gate.get("forbid") if isinstance(gate, dict) else None
    for entry in forbid if isinstance(forbid, list) else []:
        if isinstance(entry, dict):
            pattern, globs, message = entry.get("pattern"), entry.get("glob"), entry.get("message")
        else:
            pattern, globs, message = entry, None, None
        try:
            compiled = re.compile(pattern)
        except (re.error, TypeError):
            continue
        entries.append((compiled, globs if _string_list(globs) else None, message if isinstance(message, str) else None))
    return entries


def _one_line(text):
    return " ".join(text.split())


def quality_gates_text(config, state):
    """The {quality_gates} placeholder: a Markdown list of the gates that run in the current phase and step, then
    the protected paths and the forbidden patterns with their messages. Empty when no gate applies. Never raises."""
    try:
        return _quality_gates_text(config, state)
    except Exception as e:
        print("[specloop] could not list the quality gates: %s" % (str(e) or e.__class__.__name__), file=sys.stderr)
        return ""


def _quality_gates_text(config, state):
    selected = [(name, gate) for name, gate in selected_gates(config, state) if isinstance(gate, dict)]
    if not selected:
        return ""
    lines = ["### Quality gates", "",
             "These gates run when you finish your turn. A `block` gate that fails sends you back; a `warn` gate "
             "only warns, and the reviewer sees the warning.", ""]
    for name, gate in selected:
        command = gate.get("command")
        line = "- `%s` (%s)" % (name, gate.get("level") if gate.get("level") in GATE_LEVELS else "block")
        if isinstance(command, str) and command.strip():
            line += ": `%s`" % _one_line(command)
        hint = gate.get("hint")
        if isinstance(hint, str) and hint.strip():
            line += ". Hint: %s" % _one_line(hint)
        lines.append(line)
    protected = [k for k in protected_entries(config) if k not in ("", ".", "..") and not k.startswith("../")]
    if protected:
        lines += ["", "Protected paths, which you may not change (edits are denied; only a person can allow a "
                      "change, with `%s accept`): %s" % (cmd_name(config, "quality"),
                                                          ", ".join("`%s`" % k for k in protected))]
    forbidden = []
    for name, gate in selected:
        for compiled, globs, message in forbid_entries(gate):
            line = "- `%s`" % compiled.pattern
            if globs:
                line += " in %s" % ", ".join("`%s`" % g for g in globs)
            line += " (gate `%s`)" % name
            if message and message.strip():
                line += ": %s" % _one_line(message)
            forbidden.append(line)
    if forbidden:
        lines += ["", "Forbidden patterns, which fail the gate when you add a matching line:", ""] + forbidden
    return "\n".join(lines)


def _parse_added_lines(diff):
    """[(line number, text)] of the lines a `git diff --unified=0` of one file adds, numbered by the hunk headers."""
    added, number = [], None
    for line in diff.split("\n"):
        if line.startswith("@@"):
            match = HUNK_HEADER.match(line)
            number = int(match.group(1)) if match else None
        elif number is not None and line.startswith("+"):
            added.append((number, line[1:].rstrip("\r")))
            number += 1
    return added


def added_lines(root, config, state, gate):
    """[(project-relative path, line number, text)] of the lines this run added, from the changed-file set of
    `changed_file_sets` (the gate's `changedGlob` does not narrow it): `git diff --unified=0` against the
    comparison point for tracked files, and the whole text of untracked files that were not already untracked
    when the run started. Binary files are skipped. Raises ChangedFilesError when git cannot answer. Holds no
    lock."""
    point, tracked, untracked = changed_file_sets(root, config, state, gate)
    found = []
    for path in tracked:
        ok, out = git(root, config, "diff", "--unified=0", "--no-color", "--no-ext-diff", "--no-renames",
                      point, "--", path, raw=True)
        if not ok:
            raise ChangedFilesError("git diff %s failed for %s: %s" % (point[:12], path, out.strip()))
        found.extend((path, number, text) for number, text in _parse_added_lines(out))
    for path in untracked:
        try:
            with open(os.path.join(root, *path.split("/")), "rb") as f:
                raw = f.read()
        except OSError:
            continue  # gone, a directory or unreadable: there is no text to scan
        if b"\0" in raw[:BINARY_SNIFF_BYTES]:
            continue
        text = raw.decode("utf-8", errors="replace")
        found.extend((path, number, line.rstrip("\r")) for number, line in enumerate(text.split("\n"), 1))
    return found


def forbid_hit_hash(path, text):
    """The hash by which an accepted hit is recorded in state.qualityAccepted: SHA-256 of the project-relative
    path, a NUL character and the text of the line (without its line ending)."""
    return hashlib.sha256((path + "\0" + text).encode("utf-8", errors="replace")).hexdigest()


def accepted_hits(state):
    accepted = state.get("qualityAccepted")
    return set(h for h in accepted if isinstance(h, str)) if isinstance(accepted, (list, dict)) else set()


def forbid_hits(root, config, state, gate, include_accepted=False, lines=None):
    """The added lines that match the gate's `forbid` entries, as dicts with path, line, text, message, pattern
    and hash, in file and line order (a line that matches two entries is listed for each). A hit whose hash is
    in state.qualityAccepted is left out unless `include_accepted` is true. `lines` are the added lines to scan
    when the caller has them already. Raises ChangedFilesError when git cannot answer. Holds no lock."""
    entries = forbid_entries(gate)
    if not entries:
        return []
    if lines is None:
        lines = added_lines(root, config, state, gate)
    accepted = set() if include_accepted else accepted_hits(state)
    hits = []
    for path, number, text in lines:
        digest = None
        for compiled, globs, message in entries:
            if globs and not any(fnmatch.fnmatchcase(path, g) for g in globs):
                continue
            if not compiled.search(text):
                continue
            digest = digest or forbid_hit_hash(path, text)
            if digest in accepted:
                continue
            hits.append({"path": path, "line": number, "text": text, "message": message,
                         "pattern": compiled.pattern, "hash": digest})
    return hits


def forbid_text(hits):
    """The report of forbid hits: one `path:line: text` per hit, with the entry's message under it."""
    lines = []
    for hit in hits[:MAX_LISTED_PATHS]:
        text = hit["text"].strip()
        if len(text) > MAX_HIT_CHARS:
            text = text[:MAX_HIT_CHARS] + "..."
        lines.append("%s:%d: %s%s" % (hit["path"], hit["line"], text, "\n  " + hit["message"] if hit["message"] else ""))
    if len(hits) > len(lines):
        lines.append("... and %d more" % (len(hits) - len(lines)))
    return "Forbidden patterns were added since the comparison point:\n" + "\n".join("- " + l for l in lines)


def protection_record(state, name, result, reason, started):
    return gate_record(state, name, result, None, time.monotonic() - started, reason=reason, protection=True)


def _listing(lines):
    shown = lines[:MAX_LISTED_PATHS]
    if len(lines) > len(shown):
        shown.append("... and %d more" % (len(lines) - len(shown)))
    return "\n".join("- " + line for line in shown)


def protection_text(config, what, lines):
    return "%s\n%s" % (what, _listing(lines))


def protection_footer(config, what):
    return ("The gate's command did not run. %s Only a person can allow the change, by running %s accept."
            % (what, cmd_name(config, "quality")))


def block_changed_runs(root, config, state, selected):
    """(record, output) for every selected gate, all `fail` whatever their level, when the quality block changed
    since the run started. No command runs."""
    text = ("The quality configuration (config.json or config.local.json) changed since the run started, so a "
            "gate may have been weakened. The gate's command did not run. Only a person can allow the change, by "
            "running %s accept." % cmd_name(config, "quality"))
    runs = []
    for name, _ in selected:
        started = time.monotonic()
        write_gate_log(root, name, ("[specloop] %s\n" % text).encode("utf-8"))
        runs.append((protection_record(state, name, "fail", "the quality configuration changed", started), text))
    return runs


def protection_run(root, config, state, name, gate):
    """(record, output) for a gate whose protected files changed or to which forbidden lines were added, or None
    when neither happened. Its command does not run; the failure is at the gate's level. Git failing makes the
    gate broken, because the agent cannot fix it."""
    started = time.monotonic()
    changes = protected_changes(root, state, gate)
    try:
        hits = forbid_hits(root, config, state, gate)
    except (ChangedFilesError, OSError) as e:
        reason = "the added lines cannot be determined: %s" % (str(e) or e.__class__.__name__)
        write_gate_log(root, name, ("[specloop] %s\n" % reason).encode("utf-8"))
        return protection_record(state, name, "broken", reason, started), reason
    if not (changes or hits):
        return None
    parts, reasons, restore = [], [], []
    if changes:
        parts.append(protection_text(config, "Protected files changed since the run started:", changes))
        reasons.append("protected files changed: %s" % ", ".join(changes[:5]))
        restore.append("Restore the files.")
    if hits:
        parts.append(forbid_text(hits))
        reasons.append("forbidden lines added: %s" % ", ".join("%s:%d" % (h["path"], h["line"]) for h in hits[:5]))
        restore.append("Remove the lines.")
    text = "\n".join(parts + [protection_footer(config, " ".join(restore))])
    write_gate_log(root, name, ("[specloop] %s\n" % text).encode("utf-8"))
    result = "warn" if gate.get("level", "block") == "warn" else "fail"
    record = protection_record(state, name, result, "; ".join(reasons), started)
    if hits:
        record["forbidden"] = len(hits)
    return record, text


def baseline_phase(config, state):
    """True in the last planning phase, where ratchet values are measured: `plan` in single mode, the feature's
    planning phase in feature mode. Never feature-replan, and never a graph that has neither."""
    if in_replan(config, state):
        return False
    return is_plan_phase(config, state) if is_feature(state) else state["phase"] == "plan"


def measure_baselines(root, config, state, outcome):
    """On the Stop that passes the last planning phase, measure every ratchet gate that has no baseline yet.
    Planning leaves the source untouched, so the values are those from before the run's first edit. Holds no
    lock; the values go into state.qualityBaseline in apply_gate. Returns the outcome with `qualityBaseline`
    ({gate: value}) added, or a broken outcome when a value cannot be read. The measurements share the time
    budget of the Stop. A gate whose command lists changed files, and finds none, is not measured."""
    if unreadable_quality(config):
        return outcome
    have = baseline_values(state)
    pending = [(name, gate) for name, gate in quality_gates(config).items()
               if is_ratchet_gate(config, gate) and _metric_number(have.get(name)) is None]
    invalid = gate_definition_errors(root, config, pending) if pending else {}
    pending = [(name, gate) for name, gate in pending if name not in invalid]  # run as broken in implement
    if not pending:
        return outcome
    measured, runs = {}, []
    budget = quality_budget(root, config)
    started = _clock()
    for name, gate in pending:
        command, record, output = expand_changed_files(root, config, state, name, gate)
        if record:
            write_gate_log(root, name, output.encode("utf-8"))
            if record["result"] == "skipped":
                continue
        else:
            record, output = run_quality_gate(root, config, state, name, gate, budget - (_clock() - started),
                                              command, measure=True)
        if record["result"] == "broken":
            record["reason"] = "the baseline could not be measured before the first edit: %s" % record["reason"]
            runs.append((record, output))
            broken = broken_outcome([r for r, _ in runs], {"gate": name, "reason": record["reason"]},
                                    _outputs_of(runs))
            return dict(broken, qualityBaseline=measured)
        runs.append((record, output))
        measured[name] = record["metric"]
    return dict(outcome, qualityBaseline=measured) if measured else outcome


def run_quality_gates(root, config, state, outcome):
    """Run the gates of the current phase after its check passed, with no lock. Return the outcome to record.

    Definitions are checked first: an invalid gate is broken and no command runs. The first gate that breaks
    while running ends the run of gates, because the run pauses and every gate runs again after /sl-go."""
    if planning_phase(config, state):
        return measure_baselines(root, config, state, outcome) if baseline_phase(config, state) else outcome
    unreadable = unreadable_quality(config)
    if unreadable:
        return broken_outcome([], {"reason": "invalid quality configuration: %s" % unreadable})
    selected = selected_gates(config, state)
    if not selected:
        return outcome
    invalid = gate_definition_errors(root, config, selected)
    if invalid:
        records = [invalid_gate_record(root, state, name, errors) for name, errors in invalid.items()]
        return broken_outcome(records, {"gate": records[0]["name"], "reason": records[0]["reason"]})
    fail_fast = quality_block(config).get("failFast") is True
    runs = []
    budget = quality_budget(root, config)
    started = _clock()
    baselines = baseline_values(state)
    if quality_block_changed(config, state):
        runs = block_changed_runs(root, config, state, selected)
        details, full = gate_failure(config, runs)
        return {"result": "fail", "details": details, "full": full, "quality": [r for r, _ in runs],
                "qualityOutputs": _outputs_of(runs)}
    for name, gate in selected:
        protected = protection_run(root, config, state, name, gate)
        if protected:
            runs.append(protected)
            if protected[0]["result"] == "broken":
                return broken_outcome([r for r, _ in runs], {"gate": name, "reason": protected[0]["reason"]},
                                      _outputs_of(runs))
            if fail_fast and protected[0]["result"] == "fail":
                break
            continue
        command, record, output = expand_changed_files(root, config, state, name, gate)
        if record:
            write_gate_log(root, name, output.encode("utf-8"))
        else:
            record, output = run_quality_gate(root, config, state, name, gate, budget - (_clock() - started),
                                              command, _metric_number(baselines.get(name)))
        runs.append((record, output))
        if record["result"] == "broken":
            return broken_outcome([r for r, _ in runs], {"gate": name, "reason": record["reason"]},
                                  _outputs_of(runs))
        if fail_fast and record["result"] == "fail":
            break
    records = [record for record, _ in runs]
    if any(record["result"] == "fail" for record in records):
        details, full = gate_failure(config, runs)
        return {"result": "fail", "details": details, "full": full, "quality": records,
                "qualityOutputs": _outputs_of(runs)}
    return dict(outcome, details=gate_pass_details(outcome["details"], runs), quality=records,
                qualityOutputs=_outputs_of(runs))


QUALITY_REPORT = "quality.md"


def quality_report_path(root):
    return os.path.join(run_dir(root), QUALITY_REPORT)


def quality_waived_lines(state):
    """Lines of the report's section on gates a person waived with advance, from state.qualityWaived. Empty when
    there are none."""
    waived = state.get("qualityWaived")
    lines = ["- %s (waived %s)" % (_waived_text(entry), entry.get("at", "earlier") if isinstance(entry, dict) else "earlier")
             for entry in (waived if isinstance(waived, list) else [])]
    if not lines:
        return []
    return ["## Waived gates", "", "A person skipped these with advance, so they never passed. "
            "Treat each as a finding unless the spec allows it.", ""] + lines


def quality_accepted_lines(state):
    """Lines of the report's section on the changes a person accepted with `quality accept`, from
    state.qualityAcceptedLog. Empty when there are none."""
    log = state.get("qualityAcceptedLog")
    lines = []
    for entry in log if isinstance(log, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("changes"), list):
            continue
        lines.extend("- %s (accepted %s)" % (change, entry.get("at", "earlier")) for change in entry["changes"])
    if not lines:
        return []
    return ["## Accepted changes", "", "A person accepted these during the run; judge them in the review.", ""] + lines


def quality_baseline_lines(config, state):
    """Lines on the ratchet gates: the value each was measured at before the first edit, or that it has none and
    uses its limit only (no planning phase, or a run that started without one)."""
    ratchets = [name for name, gate in quality_gates(config).items() if is_ratchet_gate(config, gate)]
    if not ratchets:
        return []
    have = baseline_values(state)
    lines = ["## Ratchet baselines", ""]
    for name in ratchets:
        value = _metric_number(have.get(name))
        if value is None:
            lines.append("- %s: no baseline was measured, so the gate uses its limit only." % name)
        else:
            lines.append("- %s: baseline %s, measured before the first edit." % (name, _number_text(value)))
    return lines


def _fenced(text):
    fence = "```"
    while fence in text:
        fence += "`"
    return "%s\n%s\n%s" % (fence, text, fence)


def quality_report(config, state, records, outputs):
    """The text of run/quality.md: a row per gate that ran, the end of each warning's output, then the sections
    other steps fill. Built only from the records and outputs of one gate run."""
    where = "phase `%s`" % state["phase"]
    if state.get("stepId") is not None:
        where += ", step %s" % state["stepId"]
    lines = ["# Quality gates", "", "Last gate run: %s." % where, "",
             "| Gate | Result | Duration | Metric |", "|------|--------|----------|--------|"]
    for record in records:
        metric = record.get("metric")
        lines.append("| %s | %s | %ss | %s |" % (
            record["name"], record["result"], _seconds_text(record.get("durationSec", 0)),
            "-" if metric is None else _number_text(metric)))
    warned = [r for r in records if r["result"] == "warn"]
    if warned:
        texts = [(outputs.get(r["name"]) or "").rstrip("\n") for r in warned]
        limit = config.get("maxOutputChars", 4000)
        for record, text, share in zip(warned, texts, _shares([len(t) for t in texts], limit)):
            lines += ["", "## Warning: %s" % record["name"], "",
                      "The end of its output; the complete output is in `%s`." % _rel("run", QUALITY_DIR, record["name"] + ".log"),
                      "", _fenced(_clip_tail(text, share))]
    for extra in (quality_baseline_lines(config, state), quality_waived_lines(state), quality_accepted_lines(state)):
        if extra:
            lines += [""] + list(extra)
    return "\n".join(lines) + "\n"


def write_quality_report(root, config, state, outcome):
    """Rewrite run/quality.md from the gate run in `outcome`. Called where the run's result is recorded, so the
    file never comes from a run that state dropped. Without gate records nothing is written. Never raises."""
    records = outcome.get("quality")
    if not records:
        return
    try:
        text = quality_report(config, state, records, outcome.get("qualityOutputs") or {})
        os.makedirs(run_dir(root), exist_ok=True)
        with open(quality_report_path(root), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        print("[specloop] could not write %s: %s" % (QUALITY_REPORT, e), file=sys.stderr)


def record_quality(state, records):
    """Store the last result of each gate that ran in state.qualityResults."""
    results = state.get("qualityResults")
    results = results if isinstance(results, dict) else {}
    for record in records:
        results[record["name"]] = {key: value for key, value in record.items() if key != "name"}
    state["qualityResults"] = results


def record_baseline(state, measured):
    """Store measured ratchet values in state.qualityBaseline. A gate that already has a value keeps it: the
    reloaded state decides, so a value is measured once per run."""
    if not measured:
        return
    stored = dict(baseline_values(state))
    for name, value in measured.items():
        if _metric_number(stored.get(name)) is None:
            stored[name] = value
    state["qualityBaseline"] = stored


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


def single_mode_baseline(root, config):
    """baseRef (HEAD) and preexistingUntracked for a single-mode run's first /go, which {changed_files} compares
    with. It does not need a clean tree: a tracked file that is already modified counts as changed later. Outside
    a git work tree, in a repository with no commits or when git fails nothing is recorded ({} comes back), and
    the run starts as it always did. Holds no lock."""
    try:
        if not is_git_work_tree(root, config):
            return {}
        ok, base = git(root, config, "rev-parse", "--verify", "HEAD")
        ok_prefix, prefix = git(root, config, "rev-parse", "--show-prefix")
        if not (ok and ok_prefix):
            return {}
        own_dir = prefix + _rel() + "/"
        untracked = sorted(p for p in untracked_files(root, config) if not p.startswith(own_dir))
    except ChangedFilesError:
        return {}
    return {"baseRef": base, "preexistingUntracked": untracked}


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
    """Move the finished step's review*.md, last-failure.md, quality.md and quality/ into run/steps/<id>/."""
    names = [n for n in os.listdir(run_dir(root))
             if (n.startswith("review") and n.endswith(".md")) or n in (FAILURE_FILE, QUALITY_REPORT, QUALITY_DIR)]
    if not names:
        return
    dest = os.path.join(run_dir(root), "steps", str(step_id))
    os.makedirs(dest, exist_ok=True)
    for name in names:
        target = os.path.join(dest, name)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        os.replace(os.path.join(run_dir(root), name), target)


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
        if seconds_since(running.get("at")) < stop_limit(config):
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
    """Steps file checks, phase check, quality gates and replan validation. Holds no lock and records nothing."""
    replanning = in_replan(config, state)
    if in_step(state) and not replanning and os.path.exists(replan_path(root, state)):
        return {"result": "replan"}
    limit = config.get("maxOutputChars", 4000)
    violation = steps_file_violation(root, state) if in_step(state) and not replanning else None
    if violation:
        return {"result": "fail", "details": head(violation, limit), "full": violation}
    result, details, full = check_phase(root, config, phase_graph(config, state)[state["phase"]], state)
    outcome = {"result": result, "details": details, "full": full}
    if result == "pass":
        outcome = run_quality_gates(root, config, state, outcome)
        if outcome["result"] != "pass":
            return outcome
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
    record_baseline(state, outcome.get("qualityBaseline"))
    if outcome.get("quality"):
        record_quality(state, outcome["quality"])
        write_quality_report(root, config, state, outcome)
    phase = state["phase"]
    if result == "broken":
        return pause_for_broken_gate(config, state, session, outcome)
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


def pause_for_broken_gate(config, state, session, outcome):
    """A gate could not give an answer: pause with no iteration used and no failure for the agent to act on.
    The message is for the user."""
    phase = state["phase"]
    state.update(status="paused", pausedFor=outcome["pausedFor"])
    add_history(state, session, phase=phase, result="broken", iter=state.get("iter", 0))
    return {"systemMessage": (
        "[specloop] %s\nThe workflow is paused in phase '%s'; no iteration was used. Fix the gate or install "
        "its tool, then run %s to run the gates again, or run %s to skip the phase." % (
            outcome["details"], phase, cmd_name(config, "go"), cmd_name(config, "advance")))}


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
        if is_protected_path(config, rel):
            return deny("%s is protected by a quality gate. Only a person may allow a change, with %s." % (
                rel, cmd_name(config, "quality accept")))
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
        if isinstance(command, str) and GUARDED_COMMANDS.search(command):
            return deny("only the user may run 'run.sh advance' and 'run.sh quality accept'. Ask them to type %s or %s." % (
                cmd_name(config, "advance"), cmd_name(config, "quality accept")))
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
    errors = validate_quality(root, config)
    if errors:
        return quality_errors_message(config, errors)
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
    if not is_feature(state) and "baseRef" not in state:
        branch.update(single_mode_baseline(root, config))
    fingerprints = None if "qualityFingerprints" in state else quality_fingerprints(root, config)

    def apply(current):
        if not current or current["status"] == "done":
            raise Unchanged("The workflow changed while %s ran. Run %s to see it." % (
                cmd_name(config, "go"), cmd_name(config, "status")))
        current.update(branch)
        if fingerprints is not None and "qualityFingerprints" not in current:
            current["qualityFingerprints"] = fingerprints
        return resume(root, config, current, plan, replan)
    out = update_state(root, apply)
    warning = stale_override_warning(root, config, state)
    return out + "\n\n" + warning if warning else out


def resume(root, config, state, plan, replan):
    """Everything cmd_go changes in the state, on the reloaded state. Return the command's output."""
    state.pop("gate", None)  # left behind by a killed Stop hook, it would hold every later check back
    state.pop("pausedFor", None)  # a broken gate runs again on the next stop
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


def waive_gates(root, config, state):
    """Record in state.qualityWaived the gates that advancing past the current phase skips: the ones the Stop would
    run now (same phase and `steps` selection). Reads config and state only; runs no gate and no git call."""
    try:
        selected = selected_gates(config, state)
    except Exception as e:
        print("[specloop] could not select the gates to waive: %s" % e, file=sys.stderr)
        return
    if not selected:
        return
    waived = state.get("qualityWaived")
    waived = waived if isinstance(waived, list) else []
    step_id = state.get("stepId") if is_feature(state) and in_step(state) else None
    for name, _ in selected:
        entry = {"gate": name, "phase": state["phase"], "at": now()}
        if step_id is not None:
            entry["stepId"] = step_id
        waived.append(entry)
    state["qualityWaived"] = waived
    write_person_sections(root, state)


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
        current.pop("pausedFor", None)  # skipping the phase skips its broken gate
        waive_gates(root, config, current)
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
    quality = quality_status_lines(state)
    if is_feature(state):
        return "\n".join([feature_status(root, state)] + late + quality)
    return "\n".join([json.dumps(dict(state, lateReviews=late) if late else state, indent=2)] + quality)


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


def _result_text(record):
    """'pass', 'fail, metric 70' or 'broken (command not found)' for one entry of state.qualityResults."""
    result = str(record.get("result", "?"))
    if _metric_number(record.get("metric")) is not None:
        result += ", metric %s" % _number_text(record["metric"])
    if record.get("reason") and result.split(",")[0] in ("broken", "skipped"):
        result += " (%s)" % str(record["reason"]).strip().rstrip(".")
    return result


def _waived_text(entry):
    if isinstance(entry, dict):
        text = str(entry.get("gate", "?"))
        if entry.get("phase"):
            text += " in phase %s" % entry["phase"]
        if entry.get("stepId") is not None:
            text += ", step %s" % entry["stepId"]
        return text
    return str(entry)


def quality_status_lines(state):
    """Lines of /status on quality gates: one per gate from the last run, then the waived gates. Reads state
    only; never raises."""
    try:
        lines = []
        results = state.get("qualityResults")
        for name, record in (results.items() if isinstance(results, dict) else []):
            if not isinstance(record, dict):
                continue
            where = "phase %s" % record.get("phase")
            if record.get("stepId") is not None:
                where += ", step %s" % record["stepId"]
            lines.append("quality: %s %s (%s)" % (name, _result_text(record), where))
        waived = state.get("qualityWaived")
        for entry in (waived if isinstance(waived, list) else []):
            lines.append("quality waived: %s" % _waived_text(entry))
        return lines
    except Exception as e:
        return ["quality: unknown (%s)" % e]


def relative_age(stamp):
    """'checked 5m ago', 'checked 3h ago' or 'checked 2d ago' for an ISO time; 'never checked' when
    it is missing or invalid. A time in the future counts as just now."""
    try:
        seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return "never checked"
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return "checked %dm ago" % minutes
    if minutes < 24 * 60:
        return "checked %dh ago" % (minutes // 60)
    return "checked %dd ago" % (minutes // (24 * 60))


def update_status_line(root, config):
    """The 'update: ...' line of /status. Reads local files only, makes no git calls and never
    raises."""
    try:
        if update_mode(config) == "off":
            return "update: off"
        source = read_manifest_source(root)
        reason = update_unavailable(source)
        if reason:
            return "update: unavailable (%s)" % reason.rstrip(".")
        data = load_update_state(root)
        age = relative_age(data.get("checkedAt"))
        version = available_version(root, source, data)
        if version:
            installed = _read_version_file(_path(root, "VERSION")) or "unknown"
            return "update: installed %s, available %s (%s)" % (installed, version, age)
        if data.get("error"):
            return "update: last check failed (%s)" % str(data["error"]).strip().rstrip(".")
        return "update: up to date (%s)" % age
    except Exception as e:
        return "update: unknown (%s)" % e


def status_output(root):
    """What `engine.py status` prints: the workflow state, then the update line."""
    output = cmd_status(root)
    try:
        config = load_config(root)
    except Exception as e:
        return "%s\nupdate: unknown (%s)" % (output, e)
    return "%s\n%s" % (output, update_status_line(root, config))


SHELL_WORDS = frozenset((":", ".", "[", "[[", "alias", "case", "cd", "command", "do", "done", "echo", "elif", "else",
                         "eval", "exec", "exit", "export", "false", "fi", "for", "if", "pwd", "printf", "read",
                         "return", "set", "shift", "source", "test", "then", "time", "trap", "true", "type",
                         "ulimit", "umask", "unset", "until", "wait", "while"))
ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def gate_tool(command):
    """The first word of a gate's command that names a program: leading VAR=value words are skipped. None when
    there is none or the command starts with shell syntax."""
    if not isinstance(command, str):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    for word in words:
        if not ENV_ASSIGNMENT.match(word):
            return None if (word in SHELL_WORDS or word[:1] in "$(`{!<>") else word
    return None


def missing_tool(root, command):
    """The program a gate's command starts with when it is neither on PATH nor a file of the project, else None.
    Shell builtins and keywords are not judged."""
    word = gate_tool(command)
    if not word:
        return None
    if "/" in word or "\\" in word:
        path = word if os.path.isabs(word) else os.path.join(root, word)
        return None if os.path.exists(path) else word
    return None if shutil.which(word) else word


def _quoted(text):
    return '"%s"' % text if re.search(r"\s", text) else text


def install_command(root):
    """The command that installs the kit into this project again, as the installer recorded it, and where to run
    it."""
    source = read_manifest_source(root)
    source = source if isinstance(source, dict) else {}
    command = " ".join(["python install.py", _quoted(root)] + installer_args(source))
    path = source.get("path")
    return "%s, run from %s" % (command, _quoted(path) if isinstance(path, str) and path else "the specloop clone")


def quality_budget_warning(root, config):
    """A line when quality.timeoutSec is above what the installed Stop hook leaves, else None."""
    configured, effective = quality_timeout(config), quality_budget(root, config)
    if configured <= effective:
        return None
    return ("Warning: quality.timeoutSec is %s s, but the installed Stop hook leaves %s s for the gates, so that is "
            "the budget in force. Install again to size the hook: %s." % (
                _seconds_text(configured), _seconds_text(effective), install_command(root)))


def quality_list_lines(root, config, state):
    gates, results = quality_gates(config), state.get("qualityResults")
    results = results if isinstance(results, dict) else {}
    if not gates:
        return ["No quality gates are configured (see docs/quality-gates.md)."]
    lines = ["Quality gates (%d, in the order they run):" % len(gates)]
    for name, gate in gates.items():
        if not isinstance(gate, dict):
            lines.append("  %s: invalid (a gate must be an object)" % name)
            continue
        phases = gate.get("phases", DEFAULT_GATE_PHASES)
        last = results.get(name)
        lines.append("  %s: phases %s, level %s, steps %s, last result: %s" % (
            name, ", ".join(map(str, phases)) if isinstance(phases, list) else str(phases),
            gate.get("level", "block"), gate.get("steps", "each"),
            _result_text(last) if isinstance(last, dict) else "not run"))
    return lines


def quality_tool_lines(root, config):
    missing = []
    for name, gate in quality_gates(config).items():
        tool = missing_tool(root, gate.get("command")) if isinstance(gate, dict) else None
        if tool:
            missing.append("  %s: %s" % (name, tool))
    return ["Tools not found on PATH or in the project (gate: program):"] + missing if missing else []


def quality_usage(config):
    quality = cmd_name(config, "quality")
    return "\n".join([
        "Usage: %s [run [gate...]|init [preset]|accept]" % quality,
        "  %s                  list the gates, their last results and problems with the setup" % quality,
        "  %s run [gate...]    run gates now, outside the workflow" % quality,
        "  %s init [preset]    set up gates for this project" % quality,
        "  %s accept           accept changes to protected files, the quality block and forbidden lines" % quality,
    ])


def quality_overview(root, config):
    try:
        state = load_state(root) or {}
    except Exception:  # an unreadable state must not hide the setup
        state = {}
    lines = quality_list_lines(root, config, state)
    reason = unreadable_quality(config)
    errors = validate_quality(root, config)
    if errors:
        lines += ["", "Configuration errors:"] + ["- " + e for e in errors]
    if not reason:
        warning = quality_budget_warning(root, config)
        lines += ([""] + [warning]) if warning else []
        tools = quality_tool_lines(root, config)
        lines += ([""] + tools) if tools else []
    return "\n".join(lines)


ACCEPT_STATUSES = ("active", "paused", "stalled")
ACCEPTED_LOG_LIMIT = 50


def accept_refusal(config, state):
    """Why `quality accept` cannot work on this state, or None when a run is going."""
    if state and state.get("status") in ACCEPT_STATUSES:
        return None
    where = "no workflow is loaded" if not state else "the workflow is %s" % state.get("status", "not started")
    return ("Nothing was accepted: %s, and %s accepts changes made during a run (status active, paused or "
            "stalled). Start or resume one with %s." % (where, cmd_name(config, "quality accept"), cmd_name(config, "go")))


def current_forbid_hits(root, config, state):
    """The forbid hits of every gate that are not accepted yet, whatever the gate's phase, once per path and line
    text. Raises ChangedFilesError when git cannot answer. Holds no lock."""
    found = collections.OrderedDict()
    for gate in quality_gates(config).values():
        for hit in forbid_hits(root, config, state, gate):
            found.setdefault(hit["hash"], hit)
    return list(found.values())


def accept_summary(paths, block_changed, hits):
    """The lines of the accepted-changes log and of the command's output, one per accepted change."""
    changes = ["protected file %s" % path for path in paths]
    if block_changed:
        changes.append("the quality block (config.json or config.local.json)")
    for hit in hits:
        text = hit["text"].strip()
        changes.append("forbidden line %s:%d: %s" % (
            hit["path"], hit["line"], text if len(text) <= MAX_HIT_CHARS else text[:MAX_HIT_CHARS] + "..."))
    return changes


def write_person_sections(root, state):
    """Put the sections a person's commands add (waived gates, accepted changes) into run/quality.md, creating
    the file when no gate has run. Writes nothing when there are none. Never raises."""
    path = quality_report_path(root)
    try:
        lines = []
        for extra in (quality_waived_lines(state), quality_accepted_lines(state)):
            if extra:
                lines += [""] + extra
        if not lines:
            return
        text = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                text = f.read()
        cuts = [c for c in (text.find("\n## Waived gates\n"), text.find("\n## Accepted changes\n")) if c >= 0]
        text = text[:min(cuts)] if cuts else text or "# Quality gates\n\nNo gate has run yet."
        text = text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
        os.makedirs(run_dir(root), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        print("[specloop] could not write %s: %s" % (QUALITY_REPORT, e), file=sys.stderr)


def quality_accept(root, config, args):
    """/sl-quality accept: take the protected files, the quality block and the current forbid hits as the run's
    own. Needs a run. Fingerprints and hits are computed with no lock, then stored through update_state, which
    rechecks the reloaded state."""
    if args:
        return quality_usage(config)
    state = load_state(root)
    refusal = accept_refusal(config, state)
    if refusal:
        return refusal
    fingerprints = quality_fingerprints(root, config)
    if fingerprints is None:
        return "Nothing was accepted: the protected files could not be fingerprinted (see the message above)."
    try:
        hits = current_forbid_hits(root, config, state)
    except (ChangedFilesError, OSError) as e:
        return "Nothing was accepted: the added lines cannot be determined: %s" % (str(e) or e.__class__.__name__)
    seen = state.get("rev")

    def apply(current):
        refusal = accept_refusal(config, current)
        if refusal:
            raise Unchanged(refusal)
        if current.get("rev") != seen:
            raise Unchanged("Nothing was accepted: the workflow changed while %s ran. Run it again." % cmd_name(
                config, "quality accept"))
        paths, block_changed = fingerprint_changes(stored_fingerprints(current), fingerprints)
        changes = accept_summary(paths, block_changed, hits)
        if not changes and stored_fingerprints(current) == fingerprints:
            raise Unchanged("Nothing to accept: the protected files, the quality block and the added lines are "
                            "as the run (or an earlier accept) left them.")
        current["qualityFingerprints"] = fingerprints
        accepted = accepted_hits(current)
        old = current.get("qualityAccepted")
        stored = [h for h in old if isinstance(h, str)] if isinstance(old, list) else sorted(accepted)
        current["qualityAccepted"] = stored + [h["hash"] for h in hits if h["hash"] not in accepted]
        if changes:
            log = current.get("qualityAcceptedLog")
            log = log if isinstance(log, list) else []
            log.append({"at": now(), "changes": changes})
            current["qualityAcceptedLog"] = log[-ACCEPTED_LOG_LIMIT:]
            write_person_sections(root, current)
        if not changes:
            return "Accepted: the fingerprints of this run are recorded now. Nothing differed."
        return ("Accepted for this run:\n%s\nThe gates no longer report these. A later change, or a new forbidden "
                "line, is reported again." % "\n".join("- " + c for c in changes))
    return update_state(root, apply)


def manual_gate_record(name, result, reason, text):
    """(record, output) for a gate that `quality run` judges without running its command."""
    return gate_record({"phase": "manual"}, name, result, None, 0, reason=reason), "[specloop] %s\n" % text


def manual_gate_run(root, config, state, name, gate, definition_errors, budget_left):
    """(record, output) of one gate for `quality run`. The log goes to run/quality-manual/. Protected files and
    forbidden lines are not checked, and nothing is read from or written to the workflow except baseRef,
    preexistingUntracked and the ratchet baseline, which {changed_files} and a ratchet gate need."""
    if definition_errors:
        reason = "invalid definition: %s" % "; ".join(definition_errors)
        record, output = manual_gate_record(name, "broken", reason, "the gate did not run: %s" % reason)
    else:
        command = gate["command"]
        if ("{changed_files}" in command or "{changed_files_file}" in command) and not state.get("baseRef"):
            reason = "no baseRef: the workflow has not started a run in this repository, so there is no comparison point"
            record, output = manual_gate_record(name, "skipped", reason, "skipped: %s; the command did not run" % reason)
        else:
            stand_in = {"phase": "manual", "baseRef": state.get("baseRef"),
                        "preexistingUntracked": state.get("preexistingUntracked", [])}
            command, record, output = expand_changed_files(root, config, stand_in, name, gate, QUALITY_MANUAL_DIR)
            if not record:
                return run_quality_gate(root, config, stand_in, name, gate, budget_left, command,
                                        _metric_number(baseline_values(state).get(name)), log_dir=QUALITY_MANUAL_DIR)
    write_gate_log(root, name, output.encode("utf-8"), QUALITY_MANUAL_DIR)
    return record, output


def manual_gate_line(record):
    text = "  %s: %s" % (record["name"], record["result"])
    if record.get("reason"):
        text += " (%s)" % str(record["reason"]).strip().rstrip(".")
    text += ", %ss" % _seconds_text(record.get("durationSec", 0))
    if _metric_number(record.get("metric")) is not None:
        text += ", metric %s" % _number_text(record["metric"])
    return text + ", log %s" % _rel("run", QUALITY_MANUAL_DIR, record["name"] + ".log")


def manual_run_text(config, runs):
    """The output of `quality run`: a line per gate, then the end of the output of each gate that did not pass."""
    lines = ["Quality gates run now, outside the workflow (nothing was recorded):"]
    lines += [manual_gate_line(record) for record, _ in runs]
    failing = [(r, o.rstrip("\n")) for r, o in runs if r["result"] in ("fail", "warn", "broken")]
    shares = _shares([len(o) for _, o in failing], config.get("maxOutputChars", 4000))
    for (record, output), share in zip(failing, shares):
        lines += ["", "### %s (%s)" % (record["name"], _gate_exit(record)), _clip_tail(output, share)]
    return "\n".join(lines)


def quality_run(root, config, args):
    """/sl-quality run [gate...]: run the named gates, or all, now and in declared order, with the runner, the
    exit-code and metric rules and the time budget of a run, but with no change to the workflow: no state write,
    no iteration, no pause. Logs go to run/quality-manual/, never to run/quality/. `steps` and the phase are not
    honoured. Takes no lock."""
    reason = unreadable_quality(config)
    if reason:
        return "Nothing ran: %s." % reason
    gates = quality_gates(config)
    if not gates:
        return "No quality gates are configured (see docs/quality-gates.md)."
    unknown = [a for a in args if a not in gates]
    if unknown:
        return "Unknown gate%s: %s.\nThe gates are: %s." % ("s" if len(unknown) > 1 else "", ", ".join(unknown),
                                                          ", ".join(gates))
    try:
        state = load_state(root) or {}
    except Exception:  # an unreadable state must not stop a gate from being tried
        state = {}
    selected = [(name, gate) for name, gate in gates.items() if not args or name in args]
    invalid = gate_definition_errors(root, config, selected)
    budget, started = quality_budget(root, config), _clock()
    runs = []
    for name, gate in selected:
        runs.append(manual_gate_run(root, config, state, name, gate, invalid.get(name),
                                    budget - (_clock() - started)))
    return manual_run_text(config, runs)


QUALITY_FORMS = {"accept": quality_accept, "run": quality_run}  # form name -> handler(root, config, args); init adds its own


def cmd_quality(root, args):
    """/sl-quality: `args` is the argument string. Never raises."""
    config = {}
    try:
        config = load_config(root)
        try:
            words = shlex.split(args or "")
        except ValueError as e:
            return "Cannot read the arguments (%s).\n%s" % (e, quality_usage(config))
        if not words:
            return quality_overview(root, config)
        handler = QUALITY_FORMS.get(words[0])
        return handler(root, config, words[1:]) if handler else quality_usage(config)
    except Exception as e:
        return "[specloop] %s failed: %s" % (cmd_name(config, "quality"), str(e) or e.__class__.__name__)


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
    """Run the network check synchronously, ignoring intervalHours. Return (lines, version, checked):
    the report, the available version and whether the check ran (False with a busy lock; lines say so)."""
    if not run_update_check_locked(root, config):
        return ["A background update check is running. Try again in a moment; nothing was changed."], None, False
    data = load_update_state(root)
    version = available_version(root, source, data)
    return update_report(root, source, data, version), version, True


INSTALL_TIMEOUT_SEC = 180  # Upper limit for the clone's install.py run.
UPDATE_LOG_LINES = 20  # Most git log lines printed after an update.


def installer_args(source):
    """Command line options that repeat the recorded install (manifest source.installArgs)."""
    recorded = source.get("installArgs")
    recorded = recorded if isinstance(recorded, dict) else {}
    args = []
    if recorded.get("scope") in ("project", "local"):
        args += ["--scope", recorded["scope"]]
    if recorded.get("gitignore") is False:
        args.append("--no-gitignore")
    return args


def fast_forward_clone(config, source):
    """Fast-forward the clone to its recorded upstream. Return None on success, else the reason it
    was refused. Nothing is changed unless the result is None. Never stashes, switches branches,
    merges or rebases."""
    path, timeout = source["path"], update_timeout(config)
    ref = "%s/%s" % (source["remote"], source["branch"])
    ok, output, error = update_git(path, timeout, "status", "--porcelain")
    if not ok:
        return "cannot read the state of %s (%s). Nothing was changed." % (path, error)
    if output:
        return ("%s has uncommitted changes, so it was not updated. Commit, stash or discard them there, "
                "then run %s again." % (path, cmd_name(config, "update")))
    ok, output, error = update_git(path, timeout, "rev-parse", "--abbrev-ref", "HEAD")
    if not ok:
        return "cannot read the current branch of %s (%s). Nothing was changed." % (path, error)
    if output != source["branch"]:
        return ("%s is on branch %s, not %s, so it was not updated. Check out %s there, then run %s again." % (
            path, output, source["branch"], source["branch"], cmd_name(config, "update")))
    ok, _, error = update_git(path, timeout, "merge", "--ff-only", ref)
    if not ok:
        return ("%s cannot be fast-forwarded to %s (%s). It probably has commits that are not on the remote. "
                "Nothing was changed; bring it up to date yourself, then run %s again." % (
                    path, ref, error, cmd_name(config, "update")))
    return None


def update_changes(config, source, old_commit):
    """Lines of `git log --oneline <old commit>..HEAD` of the clone, at most UPDATE_LOG_LINES; [] when unknown."""
    timeout = update_timeout(config)
    ok, new_commit, _ = update_git(source["path"], timeout, "rev-parse", "HEAD")
    if not ok or not old_commit or old_commit == new_commit:
        return []
    ok, output, _ = update_git(source["path"], timeout, "log", "--oneline", "%s..%s" % (old_commit, new_commit))
    lines = output.splitlines() if ok else []
    if len(lines) > UPDATE_LOG_LINES:
        lines = lines[:UPDATE_LOG_LINES] + ["... and %d more" % (len(lines) - UPDATE_LOG_LINES)]
    return lines


def apply_update(root, config):
    """Update the kit from the recorded clone. Return (ok, message).

    Refuses while a workflow is busy. When the last recorded remote version is ahead of the clone,
    the clone is fast-forwarded first (see fast_forward_clone); then the clone's install.py is run
    for this project with the recorded options."""
    state = load_state(root)
    if is_busy(state):
        return False, ("[specloop] A workflow is busy (status %s), so the kit is not updated now. Let it finish, "
                       "or run %s to end it, then run %s again." % (
                           state.get("status"), cmd_name(config, "reset"), cmd_name(config, "update")))
    source = read_manifest_source(root)
    reason = update_unavailable(source)
    if reason:
        return False, "[specloop] Update checks are unavailable: %s." % reason.rstrip(".")
    data = load_update_state(root)
    installed = _read_version_file(_path(root, "VERSION"))
    if available_version(root, source, data) is None:
        return True, "[specloop] Already up to date (installed %s)." % (installed or "unknown")
    clone_version = _read_version_file(os.path.join(source["path"], "VERSION"))
    remote_version = parse_version(data.get("remote"))
    if not os.path.isdir(source["path"]):
        return False, "[specloop] Not updated: clone not found: %s" % source["path"]
    if remote_version is not None and remote_version > (parse_version(clone_version) or (0, 0, 0)):
        if data.get("error"):
            return False, ("[specloop] Not updated: the last check of the remote failed (%s), so %s was not "
                           "fast-forwarded. Nothing was changed." % (data["error"], source["path"]))
        refusal = fast_forward_clone(config, source)
        if refusal:
            return False, "[specloop] Not updated: " + refusal
    command = [sys.executable, os.path.join(source["path"], "install.py"), root] + installer_args(source)
    try:
        proc = run_limited(command, INSTALL_TIMEOUT_SEC, cwd=source["path"], merge_stderr=True)
    except OSError as e:
        return False, "[specloop] Not updated: the installer could not be started: %s" % e
    output = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.timed_out or proc.returncode != 0:
        return False, ("[specloop] The installer %s in %s; the kit may be partly updated. Run it again by hand: "
                       "%s\n%s" % ("timed out" if proc.timed_out else "failed (exit %s)" % proc.returncode,
                                   source["path"], " ".join(command), tail(output, 800)))
    new_version = _read_version_file(_path(root, "VERSION")) or "unknown"
    merge_update_state(root, installed=new_version, clone=new_version)
    lines = ["[specloop] Updated %s -> %s (from %s)." % (installed or "unknown", new_version, source["path"])]
    changes = update_changes(config, source, source.get("commit"))
    if changes:
        lines += ["Changes:"] + ["  " + line for line in changes]
    lines.append("Start a new Claude Code session so that changed commands and hooks are loaded.")
    return True, "\n".join(lines)


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
        lines = check_now(root, config, source)[0]
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
    lines, _, checked = check_now(root, config, source)
    if checked:
        lines.append(apply_update(root, config)[1])
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
        "status": lambda: status_output(root),
        "reset": lambda: cmd_reset(root),
        "update": lambda: cmd_update(root, args),
        "quality": lambda: cmd_quality(root, " ".join(args)),
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
