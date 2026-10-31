# Changelog

All notable changes to specloop are recorded here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). To update a project, see [Installation](docs/installation.md#updating).

## [Unreleased]

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
