# Changelog

All notable changes to specloop are recorded here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). To update a project, see [Installation](docs/installation.md#updating).

## [Unreleased]

### Added

- A logo. The README shows it, and the logo, favicons and social preview image are in [`assets/brand/`](assets/brand/README.md).
- Quality gates: named commands such as a linter, the test suite or a coverage report that a phase must pass before the workflow leaves it. They are declared in the new `quality` block (`timeoutSec`, `failFast`, `legacy`, `gates`; an empty `gates` block changes nothing). `gates` is an object in declared order, so `config.local.json` can change one key of one gate or set a gate to `null` to remove it. `/sl-go` validates the block and refuses to start or resume while it has errors. See [Quality gates](docs/quality-gates.md).
- Gates run after a phase's `check` passes, by default in `implement`, in single mode and in feature-mode steps (planning and replanning never run them). A failing `block` gate keeps the phase and uses one iteration however many gates failed; one message lists every failure with its `hint` and the end of its output, `quality.failFast` stops at the first, and a failing `warn` gate is only reported. A gate with `steps: "last"` runs once, on the last step, and moves when a replan adds a step. Output goes to `run/quality/<gate>.log` and `run/quality.md`, the last results to `qualityResults` in `state.json`. See [Running gates](docs/quality-gates.md#running-gates) and [Feature mode](docs/quality-gates.md#feature-mode).
- Pass criteria: `passExitCodes` (default `[0]`) and a `metric` read from the output or a project file with a regular expression (`pattern`) or a dotted JSON path (`json`), held to inclusive `min` and `max` limits. See [Pass criteria](docs/quality-gates.md#pass-criteria).
- Broken gates pause the run instead of failing it, using no iteration: exit code 126 or 127, a timeout, a command that cannot be started, an unreadable metric, a used-up budget or an invalid definition. `pausedFor` in `state.json` records the gate and the reason; `/sl-go` retries and `/sl-advance` skips. `quality.timeoutSec` is one time budget for all gates of a stop, and the installer sizes the Stop hook as `checkTimeoutSec + quality.timeoutSec + 60`, records it in `manifest.json` as `stopTimeoutSec` and caps the effective budget by it; run the installer again after raising a timeout. See [Broken gates](docs/quality-gates.md#broken-gates) and [Time budget](docs/quality-gates.md#time-budget).
- Existing problems: a command may contain `{changed_files}` and `{changed_files_file}` (the files this run changed, compared with `HEAD` for a feature-mode `steps: "each"` gate and with `baseRef` otherwise), narrowed by `changedGlob`; single mode now records `baseRef` and `preexistingUntracked` on its first `/sl-go` in a git repository without requiring a clean tree. `legacy: "ratchet"` lets a gate with a `metric` pass when the value is no worse than the one measured before the run, once, on the Stop that passes the planning phase, stored in `qualityBaseline`. See [Changed files](docs/quality-gates.md#changed-files) and [Existing problems](docs/quality-gates.md#existing-problems).
- Protection against gaming a gate: `protect` (files and directories the run may not change) and `forbid` (patterns that new lines may not match). The first `/sl-go` stores SHA-256 fingerprints of the protected files and the `quality` block in `qualityFingerprints`; before a gate's command runs, a changed file, a forbidden added line or a changed `quality` block fails the gate, however the change was made, Bash included. PreToolUse also denies `Edit`, `Write`, `MultiEdit` and `NotebookEdit` on protected paths, and Bash commands containing `run.sh advance` or `run.sh quality accept`, as early feedback. `/sl-quality accept` lets a person allow the current state (`qualityAccepted`). See [Protected files](docs/quality-gates.md#protected-files), [Forbidden patterns](docs/quality-gates.md#forbidden-patterns), [Accepting changes](docs/quality-gates.md#accepting-changes), [Guard rails](docs/guard-rails.md#protected-quality-gate-files) and [Hacks and limitations](docs/limitations.md#pretooluse-is-only-early-feedback).
- The reviewer and the agent see the gates: the `{quality_gates}` placeholder renders the gates of the current phase and step with their protected paths and forbidden patterns, and the shipped `review.md` and `step-review.md` tell the reviewer to read `run/quality.md`, judge warnings and accepted changes, and treat a waived gate as a finding unless the spec allows it. The phase brief lists `run/quality.md`, and feature-mode steps move `quality.md` and `quality/` to `run/steps/<id>/`. `/sl-advance` skips the gates of the phase it passes and records them in `qualityWaived`; `/sl-status` and `run/quality.md` list them. See [What the agent and the reviewer see](docs/quality-gates.md#what-the-agent-and-the-reviewer-see) and [Waived gates](docs/quality-gates.md#waived-gates).
- `/sl-quality`: with no argument it lists the gates with their last results, the configuration errors, a budget warning and the programs that are not on `PATH`; `run [gate...]` runs gates now without any change to the workflow, with logs in `run/quality-manual/`; `accept` is described above; `init [preset]` starts guided setup from the shipped presets `kotlin-gradle`, `java-gradle`, `java-maven`, `python`, `node`, `go` and `rust` (`presets/<name>.json`, replaced on update, never applied by themselves) and writes the `quality` key of `config.json` only after you approve. `/sl-status` prints one line per gate. See [The /sl-quality command](docs/quality-gates.md#the-sl-quality-command), [Presets and guided setup](docs/quality-gates.md#presets-and-guided-setup) and [Commands](docs/commands.md#sl-quality).
- Subagent definitions: the installer copies `specloop-planner.md`, `specloop-implementer.md`, `specloop-reviewer.md` and `specloop-orchestrator.md` to `.claude/agents/`, records them in `manifest.json` under `agents`, lists them in the managed `.gitignore` block, replaces them on every install and removes exactly them on uninstall. Your own files in `.claude/agents/` are never touched. The three role agents carry the standing rules and craft rules of their role; the orchestrator may only use `Agent`, `Read`, `Grep` and `Glob`. Nothing selects them yet. See [Files and ownership](docs/how-it-works.md#files-and-ownership).
- A phase may set `subagent`, the agent that runs it when delegated. The shipped phases default to `specloop-planner` (`plan`, `feature-plan`, `feature-replan`), `specloop-implementer` (`implement`) and `specloop-reviewer` (`review`); a phase you add uses `general-purpose`. `/sl-go` refuses a name that does not match `^[A-Za-z0-9][A-Za-z0-9_-]*$`. Existing `config.json` files keep working unchanged. See [Choosing the subagent](docs/delegation.md#choosing-the-subagent).

### Changed

- The documentation moved from one long `README.md` into focused pages under [`docs/`](docs/README.md). The README is now a short introduction. Old README anchors such as `README.md#feature-mode` no longer exist; use the page for the topic, for example [Feature mode](docs/feature-mode.md). Behaviour is unchanged.

### Fixed

- A `verdict` check no longer approves a review that only mentions `VERDICT: APPROVE`, for example inside a sentence, a code block or a quotation, while requesting changes. The check now reads whole verdict lines and the last one wins; a marker in a fenced or indented code block or a blockquote does not count. `reject` is optional. See [Verdict lines](docs/phases.md#verdict-lines).

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
