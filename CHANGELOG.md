# Changelog

All notable changes to specloop are recorded here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). To update a project, see [Installation](docs/installation.md#updating).

## [Unreleased]

### Added

- The `quality` configuration block for quality gates: named commands such as a linter or the test suite, in declared order, that a gate set to `null` in `config.local.json` removes. `/sl-go` validates the block and refuses to start or resume while it has errors, listing every one. See [Quality gates](docs/quality-gates.md).
- Quality gates run after a phase's `check` passes, by default in `implement`. A failing `block` gate keeps the phase and uses one iteration, however many gates failed; one message lists every failed gate with its hint and the end of its output. `quality.failFast` stops at the first failing `block` gate, and a failing `warn` gate is reported without blocking. Each gate's output goes to `run/quality/<gate>.log` and its last result to `qualityResults` in `state.json`. See [Quality gates](docs/quality-gates.md#running-gates).
- A broken quality gate pauses the run instead of failing the phase: exit code 126 or 127, a timeout, a command that cannot be started, or a gate made invalid after `/sl-go`. The pause uses no iteration, records the gate and the reason in `pausedFor` in `state.json`, and tells you to run `/sl-go` to retry or `/sl-advance` to skip the phase. See [Quality gates](docs/quality-gates.md#broken-gates).
- `quality.timeoutSec` is one time budget for all gates of a stop: each gate gets the smaller of its own timeout and the time left, and a gate that cannot start because the budget is used up is broken. `install.py` sizes the Stop hook as `checkTimeoutSec + quality.timeoutSec + 60` and records it in `manifest.json` as `stopTimeoutSec`; the effective budget never exceeds what that hook leaves, so run the installer again after raising a timeout. A stale gate mark now takes the same sum. See [Quality gates](docs/quality-gates.md#time-budget).
- Quality gates accept `passExitCodes` (exit codes that count as success, default `[0]`) and a `metric`: a number read from the gate's output or a project file with a regular expression (`pattern`) or a dotted JSON path (`json`), held to inclusive `min` and `max` limits. A value outside the limits fails the gate and the message shows value and limit; a metric that cannot be read makes the gate broken. The value is recorded in `qualityResults`. See [Quality gates](docs/quality-gates.md#pass-criteria).
- Quality gates run in feature-mode steps, in the `implement` step phase before review; planning and replanning never run them. A gate with `steps: "last"` runs only when no later step is `pending`, so it runs once on the last step and moves to the new last step when a replan adds one. `steps` is ignored in single mode. See [Quality gates](docs/quality-gates.md#feature-mode).
- `run/quality.md`, rewritten after each gate run: one row per gate with result, duration and metric value, then the end of each warning's output. When a feature-mode step finishes, `quality.md` and `quality/` move to `run/steps/<id>/`. See [Quality gates](docs/quality-gates.md#results-file).
- A gate command may contain `{changed_files}` (the files this run changed, shell-quoted and separated by spaces) and `{changed_files_file}` (a file in `run/quality/` with one path per line). The set is tracked changes plus new untracked files, without `.claude/specloop/`, compared with `HEAD` for a feature-mode gate with `steps: "each"` and with `baseRef` otherwise. `changedGlob` narrows it; an empty set skips the gate without running it, and a list over 8000 characters makes the gate broken and points to `{changed_files_file}`. Single mode now records `baseRef` and `preexistingUntracked` on its first `/sl-go` in a git repository, without requiring a clean tree. See [Quality gates](docs/quality-gates.md#changed-files).
- `legacy` (per gate, default `quality.legacy`, default `"baseline"`) and the `"ratchet"` mode: a gate with a `metric` that has exactly one of `min` and `max` passes when the value meets its limit or is no worse than the value measured before the run. specloop measures it once, before any source edit, on the Stop that passes `plan` (single mode) or `feature-plan` (feature mode), within the quality time budget, and stores it in `qualityBaseline` in `state.json`. A value that cannot be read pauses the run as broken with no iteration used. Without a baseline the gate uses its limit only and `run/quality.md` says so. See [Quality gates](docs/quality-gates.md#existing-problems).
- `protect` (files and directories a run may not change) and fingerprinting of the quality block. The first `/sl-go` of a run stores in `state.json`, under `qualityFingerprints`, the SHA-256 of every protected file (directories are walked; a missing file is stored as `null`) and of the merged `quality` block. Before a gate's command runs, a changed, created or deleted protected file fails that gate at its level, lists the paths and skips the command, however the file was changed, Bash included. A changed `quality` block, for example through `config.json` or `config.local.json`, fails every gate of the phase, `warn` gates too, and tells a person to run `/sl-quality accept`. Both are ordinary failures: one iteration, no pause. See [Quality gates](docs/quality-gates.md#protected-files).
- `forbid` (patterns that new lines may not match: strings, or objects with `pattern`, `glob` and `message`). Before a gate's command runs, the lines the run added are collected (`git diff --unified=0` for tracked files, the whole text of new untracked files; binary files skipped), from the same files and comparison point as `{changed_files}`. A match fails the gate at its level with `path:line: text` and the entry's message, and skips the command. A hit whose hash of path and line text is in `qualityAccepted` in `state.json` is not reported. See [Quality gates](docs/quality-gates.md#forbidden-patterns).
- PreToolUse denies `Edit`, `Write`, `MultiEdit` and `NotebookEdit` on paths named by any gate's `protect` (a file, or anything under a protected directory) in every phase while the owner's run is active, naming `/sl-quality accept`. While a run is active, in any chat, it also denies Bash commands that contain `run.sh advance` or `run.sh quality accept`; `run.sh quality run` stays allowed. These denials are early feedback: the fingerprint check before each gate is the real protection. See [Guard rails](docs/guard-rails.md#protected-quality-gate-files) and [Hacks and limitations](docs/limitations.md#pretooluse-is-only-early-feedback).
- `/sl-quality` lists the merged quality gates in order with their phases, level, steps and last result, then the configuration errors, a warning when `quality.timeoutSec` is above the budget the installed Stop hook leaves (naming the install command that fixes it) and the programs gates start that are not on `PATH`. `/sl-status` prints one line per gate from the last gate run and the gates a person waived, in single and feature mode. See [Quality gates](docs/quality-gates.md#the-sl-quality-command).
- `/sl-advance` skips the gates of the phase it passes, also from a run paused by a broken gate, and records each in `qualityWaived` in `state.json` with the phase, the step id (feature mode) and the time. `/sl-status` and a `## Waived gates` section of `run/quality.md` list them. See [Quality gates](docs/quality-gates.md#waived-gates).
- `/sl-quality accept` lets a person allow changes a run made to its own harness. While a run is `active`, `paused` or `stalled` it refingerprints the protected files and the `quality` block, so they no longer fail the gates, and adds every current `forbid` hit to `qualityAccepted` by hash of path and line text, so unchanged lines are not reported again while a new hit still is. It stores everything through one `update_state` call, prints each accepted change, and adds an `## Accepted changes` section to `run/quality.md`. Without a run it refuses and changes nothing. See [Quality gates](docs/quality-gates.md#accepting-changes).

### Changed

- The documentation moved from one long `README.md` into focused pages under [`docs/`](docs/README.md). The README is now a short introduction. Old README anchors such as `README.md#feature-mode` no longer exist; use the page for the topic, for example [Feature mode](docs/feature-mode.md). Behaviour is unchanged.

## [0.3.0] - 2026-10-31

### Added

- Update checks. specloop compares the installed kit with the clone it was installed from and tells you at a safe point when a newer version exists. See [Updates](docs/updates.md).
- `/sl-update` with the forms `check`, `skip` and `never`. A plain `/sl-update` fast-forwards the clone and reinstalls the kit into the project. See [Commands](docs/commands.md#sl-update).
- The `update` configuration block (`mode`: `off`, `notify` or `auto`; `intervalHours`; `timeoutSec`). Checks are off when `CI` or `SPECLOOP_DISABLED=1` is set.
- `config.local.json`, a personal, git-ignored configuration layer merged over `config.json`. The installer never creates, changes or removes it. See [Configuration](docs/configuration.md#personal-configuration-configlocaljson).
- `manifest.json` records `source`, the clone that ran the installer. A kit installed before 0.3.0 cannot check for updates until you rerun `install.py <project>` once from the clone.
- `/sl-status` shows the update state.

### Changed

- The update notice is added to the final message of a finished workflow.
- Subprocesses started by the engine now honour their timeouts.

## [0.2.1] - 2026-10-30

### Added

- Several chats in one project are safe. A workflow has one owner, the chat that last typed `/sl-go`; hooks in other chats leave it alone, and `/sl-advance` and `/sl-reset` are blocked there while the workflow is active. See [Several chats in one project](docs/multiple-chats.md).
- Reviews and replan requests belong to an attempt under `run/attempts/`, so a late result cannot count for a later step. `/sl-status` lists late reviews. See [Review attempts](docs/review-attempts.md).
- The Stop hook does nothing while the task registry lists a running subagent, workflow or teammate. See [Delegating a phase](docs/delegation.md).

### Changed

- State writes are atomic and locked, so a `/sl-pause` is never lost.
- `/sl-spec` and `/sl-feature` are refused while a workflow is active, paused or stalled.
- Overrides written for 0.2.0 or earlier that send a review to `{run_dir}/review.md` or a replan request to `{run_dir}/replan.md` must switch to `{review_file}` and `{attempt_dir}/replan.md`. `/sl-go` warns about each such file. See [Installation](docs/installation.md#updating).

## [0.2.0] - 2026-10-29

### Added

- Feature mode (`/sl-feature`): a planning subagent splits the spec into steps; each step runs implement → review and ends in one commit on a dedicated branch. See [Feature mode](docs/feature-mode.md).
- Guard rails for feature runs: blocked git commands, protection of the approved steps file. See [Guard rails](docs/guard-rails.md).
- Replanning a feature run when a step shows that the rest of the plan is wrong (`feature.replan`: `stall` or `auto`). See [Replanning](docs/replanning.md).
- Phase delegation (`delegate`), the phase brief, `run/last-failure.md` and `feature.enforceDelegation`. See [Delegating a phase](docs/delegation.md).
- `steps.json` validation and the `steps` check type. See [Phases](docs/phases.md).
- Size warnings for specs and plans that are too big for single mode. See [Size warnings](docs/size-warnings.md).
- The installer keeps a managed `# >>> specloop` block in `.gitignore`; `--no-gitignore` skips it. See [Installation](docs/installation.md).

## [0.1.0] - 2026-10-29

### Added

- First release: the single-mode plan → implement → review loop, driven by the Stop, PreToolUse, SessionStart and UserPromptSubmit hooks.
- Slash commands `/sl-spec`, `/sl-go`, `/sl-pause`, `/sl-status`, `/sl-advance` and `/sl-reset`.
- A project installer for POSIX shells and Windows, with test command detection. See [Installation](docs/installation.md).
