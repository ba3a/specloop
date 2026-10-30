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

Configuration is layered: defaults.json (shipped with the kit, replaced on update)
is deep-merged with config.json (owned by the project, never touched by updates).
Phase instructions are read from overrides/<phase>.md first, then phases/<phase>.md.
Set SPECLOOP_DISABLED=1 to turn every hook into a no-op.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
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
MAX_SLUG_LENGTH = 50
VERDICT_PREFIX = re.compile(r"^(?:\s|>|#+(?=\s)|[-+*](?=\s))+")  # Markdown quote, heading and list markers
VERDICT_WRAP = " \t`*_."  # whitespace, emphasis and a trailing period around a verdict
CODE_FENCE = re.compile(r"(?:[ \t>]|[-+*](?=[ \t])|[0-9]{1,9}[.)](?=[ \t]))*(`{3,}|~{3,})(.*)$")  # opener: run, info
FENCE_CLOSE = re.compile(r"[ \t>]*(`{3,}|~{3,})[ \t]*$")  # closer: a bare run, never behind a list marker
LINE_BREAK = re.compile(r"\r\n|\r|\n")


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


def load_config(root):
    config = deep_merge(_read_json(_path(root, "defaults.json")), _read_json(_path(root, "config.json")))
    # Phases explicitly set to null in config.json are removed.
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


def load_state(root):
    try:
        with open(_path(root, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_state(root, state):
    with open(_path(root, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def run_dir(root):
    return _path(root, "run")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cmd_name(config, name):
    return "/" + config.get("commandPrefix", "") + name


def _kit_text(root, name, custom=None):
    """Read <custom>, overrides/<name>.md or phases/<name>.md, whichever exists first; None if none does."""
    names = [custom] if custom else []
    names += [os.path.join("overrides", name + ".md"), os.path.join("phases", name + ".md")]
    for candidate in names:
        path = candidate if os.path.isabs(candidate) else _path(root, candidate)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    return None


def _render(text, state):
    """Fill {spec}, {run_dir} and, while a step is active, the step placeholders, in a single pass."""
    values = dict(step_values(state), spec=state.get("spec", ""), run_dir=_rel("run"))
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), text)


def phase_instructions(root, config, state):
    phase = state["phase"]
    if phase == DONE:
        return "The workflow is complete."
    custom = phase_graph(config, state).get(phase, {}).get("instructions")
    text = None
    if in_step(state):
        text = _kit_text(root, "step-" + phase, custom)
    if text is None:
        text = _kit_text(root, phase, custom)
    return _render(text if text is not None else "(no instructions file for phase '%s')" % phase, state)


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
    if is_feature(state) and REPLAN_FILE in names:
        files.append(_rel("run", REPLAN_FILE))
    files += [_rel("run", n) for n in names if n.startswith("review") and n.endswith(".md")]
    if FAILURE_FILE in names:
        files.append(_rel("run", FAILURE_FILE))
    return "%s\n\n## Files to read\n\nRead these files from disk first (paths are relative to the project root):\n\n%s" % (
        phase_instructions(root, config, state).rstrip("\n"), "\n".join("- `%s`" % f for f in files))


def phase_prompt(root, config, state):
    """Phase instructions, wrapped in the orchestration preamble when the phase is delegated."""
    if not is_delegated(config, state):
        return phase_instructions(root, config, state)
    brief = phase_brief(root, config, state)
    preamble = _kit_text(root, "delegate")
    return _render(preamble, state).replace("{brief}", brief) if preamble is not None else brief


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

def verdict_line(line):
    """Reduce a review line (or a configured marker) to its bare text: "- `VERDICT: APPROVE`." -> "VERDICT: APPROVE"."""
    return VERDICT_PREFIX.sub("", line.strip()).strip(VERDICT_WRAP)


def code_fence(line):
    """Return the fence run when the line opens a fenced code block, else None (CommonMark: a backtick
    fence may not have a backtick in its info string, that is inline code)."""
    match = CODE_FENCE.match(line)
    if not match or (match.group(1)[0] == "`" and "`" in match.group(2)):
        return None
    return match.group(1)


def find_verdict(text, check):
    """Return 'pass', 'reject', 'unclosed' or None from the verdict lines of a review.

    A verdict line is a whole line whose bare text (see verdict_line) equals check["approve"] or the
    optional check["reject"]; lines inside fenced code blocks are skipped. So a marker quoted inside a
    sentence or a code block never counts. The last verdict line wins, so a reviewer who changes their
    mind is read correctly. A fence still open at the end hides the rest of the file: 'unclosed'."""
    approve, reject, bare = verdict_line(check["approve"]), verdict_line(check.get("reject") or ""), verdict_line
    if not approve or approve == reject or (check.get("reject") and not reject):
        # Markers that are nothing but decoration: compare them as written.
        approve, reject, bare = check["approve"].strip(), (check.get("reject") or "").strip(), str.strip
    verdict, fence = None, ""
    for line in LINE_BREAK.split(text):  # not str.splitlines: Markdown breaks lines only on \n and \r
        if fence:  # only a bare run of the same character, at least as long, closes the block
            close = FENCE_CLOSE.match(line)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= len(fence):
                fence = ""
            continue
        fence = code_fence(line) or ""
        if fence:
            continue
        line = bare(line)
        if line == approve:
            verdict = "pass"
        elif reject and line == reject:
            verdict = "reject"
    return "unclosed" if fence else verdict


def check_phase(root, config, phase_config):
    """Run the check of one phase (its config dict from the phase graph).

    Return (result, details, full) where result is 'pass', 'fail' or 'reject', details is truncated
    to maxOutputChars for messages, and full is the untruncated text for run/last-failure.md."""
    check = phase_config.get("check")
    if not check:
        return "pass", "no check configured", ""
    kind = check["type"]
    limit = config.get("maxOutputChars", 4000)
    if kind == "file":
        path = os.path.join(root, check["path"])
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return "pass", "%s exists" % check["path"], ""
        return _same("fail", "Required file %s is missing or empty." % check["path"])
    if kind == "command":
        try:
            proc = subprocess.run(
                [resolve_shell(config), "-c", check["command"]],
                cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=config.get("checkTimeoutSec", 900))
        except subprocess.TimeoutExpired:
            return _same("fail", "Check command timed out: %s" % check["command"])
        output = proc.stdout.decode("utf-8", errors="replace")
        if proc.returncode == 0:
            return "pass", "`%s` succeeded" % check["command"], ""
        summary = "`%s` exited with %d:\n" % (check["command"], proc.returncode)
        return "fail", summary + tail(output, limit), summary + output
    if kind == "verdict":
        path = os.path.join(root, check["path"])
        if not os.path.exists(path):
            return _same("fail", "Review file %s is missing." % check["path"])
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
        verdict = find_verdict(text, check)
        if verdict == "pass":
            return "pass", "review approved", ""
        if verdict == "reject":
            return "reject", tail(text, limit), text
        if verdict == "unclosed":
            return _same("fail", "%s has a fenced code block that is never closed, so no verdict line counts. "
                         "Close it (a snippet that contains ``` goes inside a ```` fence)." % check["path"])
        markers = " or ".join("'%s'" % m for m in (check["approve"], check.get("reject")) if m)
        return _same("fail", "%s must contain a verdict line: exactly %s on a line of its own." % (
            check["path"], markers))
    if kind == "steps":
        path = os.path.join(root, check["path"])
        if not os.path.exists(path):
            return _same("fail", "Steps file %s is missing." % check["path"])
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except ValueError as e:
            return _same("fail", "Steps file %s is not valid JSON: %s" % (check["path"], e))
        errors = validate_steps(data)
        if not errors and check.get("requireAllPending"):
            errors = ["%s: status must be pending, not %s" % (_step_label(i, s), s["status"])
                      for i, s in enumerate(data["steps"]) if s["status"] != "pending"]
        if errors:
            full = "Steps file %s is invalid:\n%s" % (check["path"], "\n".join(errors))
            return "fail", head(full, limit), full
        return "pass", "%s is valid" % check["path"], ""
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


def count_plan_items(root):
    try:
        with open(os.path.join(run_dir(root), "plan.md"), encoding="utf-8", errors="replace") as f:
            return len(CHECKLIST_ITEM.findall(f.read()))
    except OSError:
        return 0


def archive_review(root, phase_config, state):
    src = os.path.join(root, phase_config["check"]["path"])
    if os.path.exists(src):
        base, ext = os.path.splitext(src)
        os.replace(src, "%s-%d%s" % (base, state.get("iter", 0), ext))


# --- feature mode ----------------------------------------------------------------

def git(root, config, *args, raw=False):
    """Run git with feature.gitTimeoutSec. Return (ok, output); never raises."""
    try:
        proc = subprocess.run(["git"] + list(args), cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=config.get("feature", {}).get("gitTimeoutSec", 60))
    except subprocess.TimeoutExpired:
        return False, "git %s timed out" % " ".join(args)
    except OSError as e:
        return False, "git could not be run: %s" % e
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


def approve_plan(root, config, state):
    """Validate run/steps.json again, store it as approvedSteps and start step 1.

    Return an error message (nothing saved) when the plan is invalid, else None."""
    plan_config = config["feature"]["planPhases"].get(config["feature"].get("planPhase", "feature-plan"), {})
    result, _, full = check_phase(root, config, plan_config)
    if result != "pass":
        return "The plan cannot be approved yet:\n%s\n\nFix it, then run %s again." % (full, cmd_name(config, "go"))
    with open(os.path.join(root, plan_config["check"]["path"]), encoding="utf-8") as f:
        state["approvedSteps"] = json.load(f)
    state.pop("awaitingApproval", None)
    state["status"] = "active"
    start_next_step(root, config, state)
    save_state(root, state)
    return None


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


def archive_replan(root):
    """Move run/replan.md to run/replans/replan-N.md with the next free N."""
    src = os.path.join(run_dir(root), REPLAN_FILE)
    if not os.path.exists(src):
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
    archive_replan(root)
    clear_failure(root)
    if current_step(state)[1] is not None:
        state.update(phase=config["feature"].get("stepInitialPhase", "implement"), iter=0, lastFailure=None)
        save_steps(root, state)
        return "restart"
    if start_next_step(root, config, state) is None:
        save_steps(root, state)
        state.update(status="done", phase=DONE)
        return "done"
    return "next"


def replan_adopted(root, config, state, kind):
    """Stop-hook result after a replan was adopted."""
    save_state(root, state)
    if kind == "done":
        return {"systemMessage": "[specloop] Replan adopted; no steps remain. " + completion_message(config, state)}
    what = "Restarting" if kind == "restart" else "The current step was removed; starting"
    return {"decision": "block", "reason": "Replan adopted. %s %s.\n\n%s" % (
        what, step_label(state)[0].lower(), phase_prompt(root, config, state))}


def request_replan(root, config, state):
    """Claude wrote run/replan.md during a step: enter the replan phase (auto) or stall for the user."""
    feature = config["feature"]
    state.setdefault("history", []).append({"phase": state["phase"], "result": "replan", "at": now()})
    count, cap = state.get("replanCount", 0), feature.get("maxReplans", 3)
    auto = feature.get("replan", "stall") == "auto"
    if auto and count < cap:
        clear_failure(root)
        state.update(replanCount=count + 1, phase=feature.get("replanPhase", "feature-replan"), iter=0,
                     lastFailure=None)
        save_state(root, state)
        return {"decision": "block", "reason": "Replan requested in %s. Entering phase '%s' (auto-replan %d of %d)."
                "\n\n%s" % (_rel("run", REPLAN_FILE), state["phase"], count + 1, cap,
                            phase_prompt(root, config, state))}
    state.update(status="stalled", replanRequested=True)
    save_state(root, state)
    cap_note = " The cap of %d auto-replans per run is reached, so this one needs you." % cap if auto else ""
    return {"systemMessage": (
        "[specloop] Claude requested a replan of the remaining steps.%s Read %s, optionally edit the pending or "
        "active steps in %s, then run %s. The current step then restarts with the adopted plan; uncommitted work "
        "stays in the working tree." % (cap_note, _rel("run", REPLAN_FILE), _rel("run", "steps.json"),
                                       cmd_name(config, "go")))}


def save_steps(root, state):
    """Write approvedSteps (including engine-owned status/commit changes) back to run/steps.json."""
    with open(os.path.join(run_dir(root), "steps.json"), "w", encoding="utf-8") as f:
        json.dump(state["approvedSteps"], f, indent=2)
        f.write("\n")


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
    step = next((s for s in state["approvedSteps"]["steps"] if s.get("status") == "pending"), None)
    if step is None:
        state["stepId"] = None
        return None
    step["status"] = "active"
    state.update(stepId=step["id"], phase=config["feature"].get("stepInitialPhase", "implement"),
                 iter=0, lastFailure=None)
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


def completion_message(config, state):
    lines = ["%s %s" % (s["commit"][:7] if s.get("commit") else "(empty)", s["title"])
             for s in state["approvedSteps"]["steps"]]
    return ("All %d steps are finished on branch %s:\n%s\n\nReview the branch (git log --oneline %s..%s) "
            "and open a pull request when it is ready; specloop never pushes." % (
                len(lines), state.get("branch"), "\n".join("  " + l for l in lines),
                (state.get("baseRef") or "")[:7], state.get("branch")))


def finish_step(root, config, state):
    """Commit the current step and start the next one.

    Return (kind, text): 'failed' (text is the git output, the step is back in stepInitialPhase),
    'next' (text summarizes the commit; the next step is active), 'paused' or 'done' (text is the message)."""
    label, step = step_label(state)
    result, value = commit_step(root, config, state)
    if result == "failed":
        state["phase"] = config["feature"].get("stepInitialPhase", "implement")
        state["lastFailure"] = tail(value, 500)
        write_failure(root, value)
        return "failed", value
    if result == "empty":
        step["status"] = "empty"
        state.setdefault("history", []).append({"phase": COMMIT, "result": "empty", "at": now(), "warning": (
            "step %s had no changes; nothing was committed" % step["id"])})
        summary = "%s \"%s\" had no changes; nothing was committed." % (label, step["title"])
    else:
        step.update(status="done", commit=value)
        state["history"].append({"phase": COMMIT, "result": "committed", "at": now(), "commit": value})
        summary = "%s committed (%s): %s." % (label, value[:7], step["title"])
    clear_failure(root)
    save_steps(root, state)
    if start_next_step(root, config, state) is None:
        state.update(status="done", phase=DONE)
        return "done", summary + "\n\n" + completion_message(config, state)
    if config["feature"].get("pauseBetweenSteps"):
        state["status"] = "paused"
        return "paused", "%s Paused before %s. Run %s to continue." % (
            summary, step_label(state)[0].lower(), cmd_name(config, "go"))
    return "next", summary


def await_approval(root, config, state):
    """Pause after planning so the user can review the plan. Return the message for the user."""
    state.update(status="paused", awaitingApproval=True)
    save_state(root, state)
    return ("The plan is ready. Review or edit %s and %s, then run %s to approve it." % (
        _rel("run", "plan.md"), _rel("run", "steps.json"), cmd_name(config, "go")))


# --- hook handlers ---------------------------------------------------------------

def handle_stop(root, hook_input):
    state = load_state(root)
    if not state or state.get("status") != "active":
        return None
    config = load_config(root)
    phase = state["phase"]
    graph = phase_graph(config, state)
    replanning = in_replan(config, state)
    if in_step(state) and not replanning and os.path.exists(os.path.join(run_dir(root), REPLAN_FILE)):
        return request_replan(root, config, state)
    limit = config.get("maxOutputChars", 4000)
    violation = steps_file_violation(root, state) if in_step(state) and not replanning else None
    if violation:
        result, details, full = "fail", head(violation, limit), violation
    else:
        result, details, full = check_phase(root, config, graph[phase])
        if result == "pass" and replanning:
            errors = replan_candidate(root, state)[1]
            if errors:
                full = "%s may only change pending or active steps: %s\n%s" % (
                    _rel("run", "steps.json"), errors[0], "\n".join(errors[1:]))
                result, details = "fail", head(full, limit)
    state["iter"] = state.get("iter", 0) + 1
    state.setdefault("history", []).append(
        {"phase": phase, "result": result, "at": now(), "iter": state["iter"]})

    if result == "pass":
        clear_failure(root)
        state["lastFailure"] = None
        if replanning:
            kind = adopt_replan(root, config, state, replan_candidate(root, state)[0])
            return replan_adopted(root, config, state, kind)
        if is_feature(state) and not in_step(state) and phase == config["feature"].get("planPhase"):
            if config["feature"].get("requirePlanApproval", True):
                return {"systemMessage": "[specloop] " + await_approval(root, config, state)}
            error = approve_plan(root, config, state)
            if error:
                return {"systemMessage": "[specloop] " + error}
            return {"decision": "block", "reason": "Plan approved. %s starts now.\n\n%s" % (
                step_label(state)[0], phase_prompt(root, config, state))}
        next_phase = graph[phase].get("next", DONE)
        if in_step(state) and next_phase in (COMMIT, DONE):
            return step_finished(root, config, state, phase)
        state["phase"] = next_phase
        if next_phase == DONE:
            state["status"] = "done"
            save_state(root, state)
            return {"systemMessage": "[specloop] all phases passed - done."}
        max_items = size_limit(config, "maxPlanItems")
        if (phase == "plan" and not is_feature(state) and max_items is not None
                and not state.get("largePlanAcknowledged")):
            items = count_plan_items(root)
            if items > max_items:
                state["status"] = "paused"
                state["largePlanItems"] = items
                save_state(root, state)
                return {"systemMessage": (
                    "[specloop] the plan has %d checklist items, above the single-mode limit of %d. "
                    "Paused before phase '%s'. Run %s and then %s to split the work into commits, "
                    "or %s to continue anyway." % (
                        items, max_items, next_phase, cmd_name(config, "reset"),
                        cmd_name(config, "feature"), cmd_name(config, "go")))}
        save_state(root, state)
        return {"decision": "block", "reason": "Phase '%s' passed (%s). Now entering phase '%s'.\n\n%s" % (
            phase, details, next_phase, phase_prompt(root, config, state))}

    write_failure(root, full)
    failure_file = _rel("run", FAILURE_FILE)
    if result == "reject":
        archive_review(root, graph[phase], state)
        state["phase"] = graph[phase]["check"].get("onReject", "implement")
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
        save_state(root, state)
        return {"systemMessage": "[specloop] stalled after %d iterations in phase '%s'. "
                                 "Inspect, then run %s to resume or %s to skip the phase." % (
                                     state["iter"], phase, cmd_name(config, "go"), cmd_name(config, "advance"))}
    save_state(root, state)
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


def step_finished(root, config, state, phase):
    """The last phase of the step cycle passed: commit the step and continue (Stop hook result)."""
    label = step_label(state)[0]
    kind, text = finish_step(root, config, state)
    if kind == "failed":
        return block_or_stall(root, config, state, phase, commit_failed_reason(config, root, state, label, text))
    save_state(root, state)
    if kind in ("done", "paused"):
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

    if tool in EDIT_TOOLS:
        rel = relative_to_project(root, tool_input.get("file_path") or tool_input.get("notebook_path"))
        protected = [_rel(n) for n in ("state.json", "config.json", "defaults.json", "engine.py")]
        if rel in protected:
            return deny("%s is managed by specloop; use the %s* commands." % (rel, cmd_name(config, "")))
        allowed = phase_graph(config, state).get(phase, {}).get("allowEdits")
        if allowed is not None and (rel is None or not any(rel.startswith(p) for p in allowed)):
            return deny("phase '%s' only allows editing %s, not %s." % (
                phase, ", ".join(allowed), rel or "a path outside the project"))
        # Claude Code sets agent_id in hook input only for tool calls made inside a subagent.
        if (config["feature"].get("enforceDelegation") and is_delegated(config, state)
                and not hook_input.get("agent_id") and not (rel or "").startswith(_rel("run") + "/")):
            return deny("phase '%s' is delegated: start one subagent with the phase brief and let it make this "
                        "edit. The main session may only edit %s/." % (phase, _rel("run")))

    if tool == "Bash":
        command = tool_input.get("command", "")
        for blocked in config.get("blockedCommandsUntilDone", []):
            if blocked in command:
                return deny("'%s' is not allowed until the workflow is done (phase '%s')." % (blocked, phase))
        if is_feature(state):
            for blocked in config["feature"].get("blockedCommands", []):
                if blocked in command:
                    return deny("'%s' is not allowed while a feature run is active: specloop owns the feature "
                                "branch %s and makes the step commits itself. Run %s first if the user wants to "
                                "do this by hand." % (blocked, state.get("branch", "-"), cmd_name(config, "pause")))
    return None


def handle_context(root, event):
    state = load_state(root)
    if not state:
        return None
    config = load_config(root)
    status = state.get("status")
    if status == "active":
        text = header(state)
        if is_delegated(config, state):
            text += ("\n[specloop] Phase '%s' is delegated: start one subagent with the phase brief, relay a short "
                     "summary, and finish your turn; do not do the phase's work yourself." % state["phase"])
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
    # Stale plan.md / review.md from an earlier run would pass the new run's gates.
    if os.path.isdir(run_dir(root)):
        shutil.rmtree(run_dir(root))
    os.makedirs(run_dir(root))
    feature = mode == "feature"
    phase = config["feature"].get("planPhase", "feature-plan") if feature else config.get("initialPhase", "plan")
    save_state(root, {"status": "ready", "mode": mode, "phase": phase, "spec": spec.replace("\\", "/"),
                      "iter": 0, "history": [], "createdAt": now()})
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
    if is_feature(state):
        if state.get("branch"):
            error = check_feature_branch(root, config, state)
        else:
            error = start_feature_branch(root, config, state)
        if error:
            return "Not started: %s" % error
        if state["status"] == "paused" and state.get("awaitingApproval"):
            error = approve_plan(root, config, state)
            if error:
                return error
        if state["status"] == "stalled" and state.get("replanRequested"):
            data, errors = replan_candidate(root, state)
            if errors:
                return "The replan cannot be adopted yet:\n%s\n\nFix %s, then run %s again." % (
                    "\n".join(errors), _rel("run", "steps.json"), cmd_name(config, "go"))
            if adopt_replan(root, config, state, data) == "done":
                save_state(root, state)
                return "Replan adopted; no steps remain. " + completion_message(config, state)
    elif state["status"] == "ready" and not state.get("largeSpecAcknowledged"):
        warning = spec_size_warning(root, config, state.get("spec", ""))
        if warning and not state.get("largeSpecWarned"):
            state["largeSpecWarned"] = True
            save_state(root, state)
            return ("%s\n\nThe workflow has not started. Do not plan or implement; show this warning to the user "
                    "and wait. Running %s again proceeds in single mode anyway." % (warning, cmd_name(config, "go")))
        if warning:
            state["largeSpecAcknowledged"] = True
    if state["status"] == "stalled":
        state["iter"] = 0
    if state["status"] == "paused" and state.get("largePlanItems") and not state.get("largePlanAcknowledged"):
        state["largePlanAcknowledged"] = True
    state["status"] = "active"
    save_state(root, state)
    return header(state) + "\n\n" + phase_prompt(root, config, state)


def cmd_pause(root):
    state = load_state(root)
    if not state:
        return "Nothing to pause."
    state["status"] = "paused"
    save_state(root, state)
    return "Workflow paused at phase '%s'. Run %s to resume." % (state["phase"], cmd_name(load_config(root), "go"))


def cmd_advance(root):
    state = load_state(root)
    if not state or state["phase"] == DONE:
        return "Nothing to advance."
    config = load_config(root)
    if in_replan(config, state):
        # Force-passing the replan phase adopts the current steps.json if it is acceptable.
        data, errors = replan_candidate(root, state)
        if errors:
            return "The replan cannot be adopted:\n%s" % "\n".join(errors)
        state.setdefault("history", []).append({"phase": state["phase"], "result": "skipped", "at": now()})
        kind = adopt_replan(root, config, state, data)
        save_state(root, state)
        if kind == "done":
            return "Replan adopted; no steps remain. " + completion_message(config, state)
        return "Replan adopted. Now in %s, phase '%s'." % (step_label(state)[0].lower(), state["phase"])
    clear_failure(root)
    state.setdefault("history", []).append({"phase": state["phase"], "result": "skipped", "at": now()})
    if is_feature(state) and not in_step(state) and state["phase"] == config["feature"].get("planPhase"):
        return await_approval(root, config, state)
    next_phase = phase_graph(config, state)[state["phase"]].get("next", DONE)
    if in_step(state) and next_phase in (COMMIT, DONE):
        # Advancing out of the last step phase commits the step; it never skips the commit.
        label = step_label(state)[0]
        state["iter"] = state.get("iter", 0) + 1
        kind, text = finish_step(root, config, state)
        save_state(root, state)
        if kind == "failed":
            return "Committing %s failed; the step is back in phase '%s'.\n\n%s" % (
                label.lower(), state["phase"], tail(text, config.get("maxOutputChars", 4000)))
        if kind == "next":
            return "%s Now in %s, phase '%s'." % (text, step_label(state)[0].lower(), state["phase"])
        return text
    state["phase"] = next_phase
    if state["phase"] == DONE:
        state["status"] = "done"
    save_state(root, state)
    return "Advanced to phase '%s'." % state["phase"]


def cmd_status(root):
    state = load_state(root)
    if not state:
        return "No workflow state."
    return feature_status(root, state) if is_feature(state) else json.dumps(state, indent=2)


def feature_status(root, state):
    """Readable summary of a feature run: branch, phase and one line per step."""
    lines = ["[specloop] feature run, status=%s" % state.get("status"), "spec: %s" % state.get("spec")]
    if state.get("branch"):
        lines.append("branch: %s (base %s)" % (state["branch"], (state.get("baseRef") or "")[:7]))
    else:
        lines.append("branch: - (created by the first go)")
    lines.append("phase: %s, iteration %d" % (state.get("phase"), state.get("iter", 0)))
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
    for target in (_path(root, "state.json"), run_dir(root)):
        if os.path.isdir(target):
            shutil.rmtree(target)
        elif os.path.exists(target):
            os.remove(target)
    return "Workflow state reset."


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
    if command == "stop":
        return handle_stop(root, hook_input)
    if command == "pretool":
        return handle_pretool(root, hook_input)
    return handle_context(root, args[0] if args else "UserPromptSubmit")


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
    handlers = {
        "init": lambda: cmd_init(root, " ".join(args).strip().strip('"')),
        "init-feature": lambda: cmd_init(root, " ".join(args).strip().strip('"'), mode="feature"),
        "go": lambda: cmd_go(root),
        "pause": lambda: cmd_pause(root),
        "advance": lambda: cmd_advance(root),
        "status": lambda: cmd_status(root),
        "reset": lambda: cmd_reset(root),
    }
    if command not in handlers:
        print("Unknown command: " + command)
        return 1
    print(handlers[command]())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
