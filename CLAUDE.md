# CLAUDE.md

Guidance for developing specloop itself. User-facing documentation lives in `docs/`; `README.md` is only the short introduction.

## What this repository is

specloop is a kit that gets installed into projects. It adds a spec-driven state machine (plan → implement → review) to Claude Code, built from hooks and slash commands. `install.py` copies `kit/` into a target project's `.claude/` directory.

specloop is installed in this repository too, to develop itself. Editing `kit/` does not change the installed copy under `.claude/specloop/`, which can lag behind until it is reinstalled.

## Layout

```
install.py, install.sh, install.cmd   installer (Python) and its shell / cmd wrappers
VERSION                               kit version, copied into targets
kit/specloop/engine.py                state machine: hook handlers + manual commands
kit/specloop/run.sh                   portable launcher used by hooks and commands (finds Python)
kit/specloop/defaults.json            default config and phase graph (replaced on update)
kit/specloop/phases/*.md              shipped phase instructions (replaced on update):
  plan.md, implement.md, review.md      single-mode phases
  feature-plan.md, feature-replan.md    feature-mode planning and replanning
  step-implement.md, step-review.md     feature-mode step cycle
  delegate.md                           orchestration preamble for delegated phases ({brief})
kit/commands/*.md                     slash command templates; {{prefix}} is substituted on install
                                      (spec, feature, go, pause, advance, status, reset, update)
                                      kit/commands/update.md is /sl-update (check, skip, never)
tests/                                unittest suites for the engine, the installer and the docs
README.md                             short introduction, at most 80 lines
CHANGELOG.md                          release history (Keep a Changelog); repository root only, not installed
docs/README.md                        documentation index: every page listed once, in four groups
docs/<topic>.md                       one page per topic; the owner of the facts it describes
```

Installed layout in a target: `.claude/specloop/{engine.py,run.sh,defaults.json,phases/,VERSION,manifest.json}` belong to the kit. `manifest.json` also records `source`: the clone that ran `install.py` (its path, version, remote, branch and `installArgs`, or `checkable: false` with a reason), which the update checker uses. `config.json` and `overrides/` belong to the project. `config.local.json` belongs to the person: a personal layer merged over `config.json`, git-ignored, and never created, changed or removed by the installer (the engine writes it only for `/sl-update never`). `state.json`, `state.lock`, `update.json`, `update.lock` and `run/` (including `run/attempts/`) are runtime state; `update.json` is the update checker's semaphore file.

## Commands

```bash
python -m unittest discover -s tests          # run all tests
python -m unittest tests.test_engine -v       # one suite
./install.sh /path/to/sandbox-project         # manual end-to-end check
```

## Rules

- **Python standard library only, compatible with Python 3.8.** Friends install this on arbitrary machines.
- **Hooks must never break a session.** Hook entry points catch every exception, log to stderr and exit 0. `run.sh` exits 0 when no Python is found.
- **Every state write goes through `update_state`.** It locks, reloads, applies the change, raises `rev` and saves. Never hold it during a phase check or a git call: run those first or afterwards, and recheck the reloaded state before using their result.
- **Never overwrite project-owned files on update.** That means `config.json`, `overrides/`, and any settings or hooks that specloop did not create. specloop hook groups are identified by the `.claude/specloop/run.sh` marker in their command.
- **Backward compatibility of `config.json`.** Add new keys with defaults in `defaults.json`. Do not rename or repurpose existing keys without a migration in `install.py`.
- **Cross-platform.** Paths written into settings and commands use forward slashes and `$CLAUDE_PROJECT_DIR`. Hooks run through `sh`, which is Git Bash on Windows. Keep `.sh` files LF and `.cmd` files CRLF (see `.gitattributes`). On Windows, stage scripts with `git add --chmod=+x`.
- **User-visible command names go through the prefix.** In the engine use `cmd_name(config, "go")`; in command templates use `/{{prefix}}go`. Never hard-code `/sl-go`.
- Every behavior change gets a unit test in `tests/`. Tests build throwaway projects in temp directories and must not depend on the developer's machine. Use `exit 0` / `exit 1` as check commands.
- **Documentation and changelog.** `tests/test_docs.py` enforces most of this.
  - Every behavior change updates the docs page that owns it, or adds a new page for a new feature. Each fact has one home; other pages link to it.
  - A new page is added to `docs/README.md` under one of its groups (Getting started, Concepts, Features, Reference), starts with `[Documentation](README.md) › <Title>`, ends with a `## See also` section, and is linked from the pages it relates to; each of those pages links back.
  - New config keys get a row in the table of the owning page and, for top-level keys, in `configuration.md`. New commands get an entry in `commands.md`. New phase files are mentioned in `phases.md`.
  - `README.md` changes only when the pitch, the quick start or the list of main pages changes, and stays within 80 lines.
  - Every user-visible change adds a `CHANGELOG.md` entry under `Unreleased` in the same commit. A release renames `Unreleased` to `## [x.y.z] - YYYY-MM-DD`, adds a new empty `Unreleased` section and bumps `VERSION` (semver) in the same commit. A documentation-only change does not bump `VERSION`.
  - A reviewer checks the same list: a step that changes behavior without the matching docs and changelog change is not approved.

## Hook protocol reference

- Stop: print `{"decision": "block", "reason": "..."}` to keep Claude working, or `{"systemMessage": "..."}` / nothing to let it stop. The input has `background_tasks` when the task registry is reachable: one entry per in-flight task, with a `type` such as `subagent`, `workflow`, `teammate`, `shell` or `monitor`. A subagent that waits on its own background work has ended its turn, so it is not listed and only its shell is; `SubagentStop` fires when it parks and `SubagentStart` when it resumes.
- PreToolUse: print `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}` to block a tool call.
- SessionStart / UserPromptSubmit: print `{"hookSpecificOutput": {"hookEventName": "<event>", "additionalContext": "..."}}`.
- UserPromptSubmit: `prompt` holds a typed command as typed. Print `{"decision": "block", "reason": "..."}` to stop the prompt, and with it the command's shell lines.
- Every hook receives `session_id`; hooks inside a subagent also receive `agent_id`. Manual commands run through `` !`...` `` and receive no hook input.
