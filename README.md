# specloop

A spec-driven development loop for [Claude Code](https://claude.com/claude-code), built only from hooks, slash commands and a small Python state machine. You drop in a specification, start development by hand, and Claude is walked through **plan → implement → review** phases. A phase only completes when its check passes: the plan file exists, the tests are green, or the reviewer approves.

It runs inside your normal Claude Code session (CLI, desktop app or IDE), so it uses your Claude subscription. You don't need API keys or a separate orchestrator.

```
/sl-spec specs/login.md      load the spec (Claude summarizes it, nothing else)
/sl-go                       start: plan -> implement -> review -> done
```

## How it works

| Phase       | Claude does                                        | Gate (checked by the Stop hook)                 | Edits allowed            |
|-------------|----------------------------------------------------|--------------------------------------------------|--------------------------|
| `plan`      | Reads the spec, writes `.claude/specloop/run/plan.md` | plan file exists and is non-empty              | only `.claude/specloop/run/` |
| `implement` | Test-first implementation of the plan              | your test command exits 0                        | anything                 |
| `review`    | Reviews the diff (ideally via a subagent), writes `review.md` | `VERDICT: APPROVE` → done; `VERDICT: CHANGES_REQUESTED` → back to implement | only `.claude/specloop/run/` |

The engine plugs into four hooks:

- **Stop**: each time Claude tries to end its turn, it runs the current phase's check. If the check fails, the stop is blocked and Claude gets the failure (for example the tail of the test output) and keeps working. If it passes, the workflow moves to the next phase and Claude gets that phase's instructions.
- **PreToolUse**: enforces per-phase edit restrictions. It blocks `git push` and `gh pr create`/`merge` until the workflow is done, and stops Claude from editing specloop's own state.
- **SessionStart / UserPromptSubmit**: put the current phase and its instructions into context, so a new or resumed session picks up where the last one stopped.

Safety valves:
- After `maxIterations` blocked stops (default 15) the workflow goes to `stalled` and Claude is allowed to stop. `/sl-go` resumes it.
- `/sl-pause` turns the hooks off when you want to talk to Claude in between.
- A spec or plan that looks too big for one plan → implement → review cycle triggers a warning (see [Large specs and plans](#large-specs-and-plans)).
- Setting `SPECLOOP_DISABLED=1` makes every hook a no-op.
- If the engine itself crashes, the hook logs to stderr and exits 0, so a crash never breaks your session.
- An unreadable `state.json` is reported instead of ignored: Stop and the context hooks say that specloop is not enforcing anything, and `/sl-reset` clears it.

## Requirements

- Claude Code
- Python 3.8+ (stdlib only)
- `sh`, which on Windows means Git Bash. Claude Code on Windows needs it anyway.
- git (the review phase reads `git diff`)

## Install into a project

Clone once, then install into as many projects as you like:

```bash
git clone https://github.com/ba3a/specloop.git ~/tools/specloop
~/tools/specloop/install.sh ~/code/my-project
```

On Windows (cmd or PowerShell):

```bat
install.cmd C:\code\my-project
```

Then start a **new** Claude Code session in that project. Hooks are loaded at session start.

The installer:
- copies the kit into `<project>/.claude/specloop/`;
- creates `config.json` and tries to detect the test command (Gradle, Maven, npm, pytest, Go, Cargo, .NET);
- installs the slash commands into `.claude/commands/`;
- merges the hooks into `.claude/settings.json` and leaves your other settings and hooks alone;
- if the project is in a git work tree and `.gitignore` is writable, keeps a marked `# >>> specloop` block in `.gitignore`. The block ignores the installed kit, the runtime state and the installed slash commands. `config.json` and `overrides/` stay tracked so the team can share them. Re-running the installer rewrites the block, and `--uninstall` removes it.

### Installer options

| Option | Meaning |
|--------|---------|
| `--test-command "<cmd>"` | Command that must pass to leave `implement` (overrides detection). |
| `--scope project\|local` | `project` (default) writes hooks to `.claude/settings.json`, which you commit and share with your team. `local` writes them to `.claude/settings.local.json`, which stays personal. |
| `--prefix <p>` | Slash command prefix, default `sl-`. Use `--prefix ""` for plain `/spec`, `/go`, … |
| `--force` | Recreate `config.json` from scratch. |
| `--no-gitignore` | Don't touch `.gitignore`. |
| `--uninstall [--keep-config]` | Remove specloop: kit, commands, hooks. Other settings are kept. |

### Updating

```bash
cd ~/tools/specloop && git pull
./install.sh ~/code/my-project
```

An update replaces the engine, launcher, `defaults.json` and the shipped `phases/`. Your `config.json`, `config.local.json` and `overrides/` are never touched. Instead of doing this by hand, you can let specloop [check for updates and apply them](#updates).

An override written for 0.2.0 or earlier may still send a review to `{run_dir}/review.md` or a replan request to `{run_dir}/replan.md`. The gate no longer reads those files, so the phase would never pass. `/sl-go` prints a warning naming each such file in use. Change it to `{review_file}` for a review and `{attempt_dir}/replan.md` for a replan request.

### Git worktrees

The kit is git-ignored, so a new `git worktree` does not get it. Run the installer once per worktree. If the project commits its hook settings, a worktree without the kit has the hooks but no `run.sh`: each hook fails with exit code 127, which Claude Code reports as a hook error and otherwise ignores, so nothing is enforced there.

## Commands

| Command | What it does |
|---------|--------------|
| `/sl-spec <file>` | Load a spec; status `ready`. Clears the previous run's `run/` artifacts (plan, reviews). Refused while a workflow is `active`, `paused` or `stalled`; run `/sl-reset` first. Claude summarizes the spec and waits. |
| `/sl-feature <file>` | Load a spec in [feature mode](#feature-mode) (one commit per planned step). Also clears `run/`, and is refused in the same cases. |
| `/sl-go` | Start, or resume after pause/stall. The chat that types it becomes the [owner](#several-chats-in-one-project) of the workflow. |
| `/sl-pause` | Stop driving the session; keep the phase. Works from any chat. A workflow that has not started or is done stays as it is. |
| `/sl-status` | Show the state: the raw state JSON in single mode, a step summary in feature mode. Works from any chat, and lists reviews that arrived too late to count. |
| `/sl-advance` | Force-pass the current phase. Blocked in a chat that is not the owner while the workflow is `active`. |
| `/sl-reset` | Delete the state and the `run/` artifacts. Blocked in a chat that is not the owner while the workflow is `active`. |
| `/sl-update [check\|skip\|never]` | Check for a newer kit and apply it, or only report (`check`), or silence a version (`skip`, `never`). Refused while a workflow is busy. See [Updates](#updates). |

Claude cannot run `/sl-spec`, `/sl-feature`, `/sl-go`, `/sl-advance`, `/sl-pause`, `/sl-reset` or `/sl-update` itself: their templates set `disable-model-invocation: true`, so taking over a workflow or force-passing a phase is always your decision.

If specloop is busy (another process holds `state.lock`), a command prints one line asking you to retry and changes nothing.

## Several chats in one project

Hooks are configured per project, so they fire in every chat open in that folder. specloop keeps one workflow per checkout, and only one chat drives it.

**Who drives.** The chat that last typed `/sl-go` is the workflow's owner, stored in `state.json` as `owner`. Only a typed command claims it: a prompt that merely mentions `/sl-go` does not, and neither do `/sl-spec` and `/sl-feature`. The owner stays across pause, stall and done; `/sl-reset` and loading a new spec remove it. With no owner, for example in a workflow started by an older version, every chat drives it as before until someone types `/sl-go`. `/sl-status` shows the owner in feature mode; in single mode it is in the state JSON.

**What other chats see.** While the workflow is `active`, a chat that is not the owner is left alone:

| Hook | In a chat that is not the owner |
|------|---------------------------------|
| Stop | Does nothing: no check, no iteration, no failure file. |
| SessionStart | One line saying that another chat owns the workflow and how to take it over. No phase instructions. |
| UserPromptSubmit | Adds no context. |
| PreToolUse | Phase `allowEdits`, `feature.enforceDelegation` and `blockedCommandsUntilDone` do not apply. |

**What stays blocked there, and why.** One checkout is still one working tree, so these apply in every chat:

- specloop's own files (`state.json`, `config.json`, `defaults.json`, `engine.py`) cannot be edited.
- `feature.blockedCommands` stay denied, because a `git checkout` or `git stash` in any chat would break the run.
- Edits under `.claude/specloop/run/` are denied outside a subagent, so a stray review file cannot affect a gate.
- `/sl-advance` and `/sl-reset` are blocked while `active`; the message names `/sl-go` and `/sl-pause`. A paused workflow whose chat is gone can still be advanced or reset from anywhere.

**Takeover.** `/sl-go` in another chat replaces the owner. This is how you recover from a closed chat. It is also needed after `/clear`, which gives the chat a new session id; `--resume` and compaction keep it. The output starts with a line saying that this chat now drives the workflow. Stop any subagent still running in the previous chat: the takeover opens a new [review attempt](#review-attempts), so its result no longer counts. There is no heartbeat or timeout. A `/sl-go` refused because the current branch is not the feature run's branch leaves the owner as it is.

**State files.** `state.json` and `run/steps.json` are replaced atomically, and every state change takes `state.lock` and reloads the file first, so two processes cannot overwrite each other and a `/sl-pause` is never lost. The lock is not held while a check or git runs. A Stop that finds its workflow changed during the check (paused, taken over, moved to another phase) drops the result. A pause during a step commit is kept: the commit stands, the next step is prepared and the workflow stays paused. If two Stops overlap, for example during a takeover, only one runs the check, and the other says that a check is marked as running. A Stop hook that was killed leaves that mark behind for `checkTimeoutSec` plus a minute; `/sl-go` clears it at once. A Stop that cannot record its result because the lock stays taken says so, and `/sl-go` continues. A step commit whose result was lost that way still counts: the next Stop finds HEAD past `commitBase` in the state and records that commit for the step. `history` entries written by hooks carry `session`, the first 8 characters of the session id, and claims and takeovers are recorded there too.

**Limits.**

- This is not isolation. Two chats editing the same tracked files still collide, and `git add -A` in a step commit picks up new files from any chat. Use one `git worktree` per task if chats must not touch each other's work.
- Claiming and blocking rely on `UserPromptSubmit` running before the command's shell line. Claude Code 2.1.309 does that. Claude Code 2.1.252 runs the shell line first and a block does not stop it, so there a takeover line appears only on the next `/sl-go`, and `/sl-advance` and `/sl-reset` are not blocked. Other versions were not tested.
- A hook input without `session_id` counts as the owner, so older Claude Code versions keep working.

## Configuration

Files in `<project>/.claude/specloop/`:

| File | Owner | Purpose |
|------|-------|---------|
| `defaults.json` | kit (replaced on update) | Default settings and phase graph. |
| `config.json` | **project** | Your overrides, deep-merged over `defaults.json`. |
| `config.local.json` | **person** | Your personal overrides, deep-merged over `config.json`. Git-ignored. See [Personal configuration](#personal-configuration-configlocaljson). |
| `phases/<phase>.md` | kit (replaced on update) | Shipped phase instructions. |
| `overrides/<phase>.md` | **project** | Your phase instructions; take precedence over `phases/`. |
| `state.json`, `state.lock`, `run/` | runtime | Current state, its lock, plan and review files. |
| `update.json`, `update.lock` | runtime | Result of the last update check and the lock of a running check. See [Updates](#updates). |
| `manifest.json` | kit | What the installer wrote, including `source`, the clone the kit was installed from. |

Everything in this directory except `config.json` and `overrides/` is git-ignored.

`config.json` example:

```json
{
  "commandPrefix": "sl-",
  "maxIterations": 20,
  "checkTimeoutSec": 1200,
  "blockedCommandsUntilDone": ["git push", "gh pr create", "gh pr merge", "npm publish"],
  "phases": {
    "implement": {
      "check": { "type": "command", "command": "./gradlew test -q && ./gradlew lint -q" }
    }
  }
}
```

Top-level keys (the `feature` block is described under [Feature mode](#feature-mode)):

| Key | Default | Meaning |
|-----|---------|---------|
| `commandPrefix` | `"sl-"` | Prefix of the slash commands, as set by the installer's `--prefix`. Used in every message the engine prints. |
| `initialPhase` | `"plan"` | First phase of a single-mode run. |
| `maxIterations` | `15` | Blocked stops per phase (per step in feature mode) before the run goes to `stalled`. |
| `checkTimeoutSec` | `900` | Timeout for a `command` check. |
| `maxOutputChars` | `4000` | Check output and review text are truncated to this many characters in stop messages (`run/last-failure.md` keeps everything). |
| `shell` | `"bash"` | Shell that runs `command` checks (Git Bash on Windows). |
| `blockedCommandsUntilDone` | `git push`, `gh pr create`, `gh pr merge` | Bash commands denied while a run is active, in both modes. |
| `largeSpecWarning` | `maxLines` 200, `maxWords` 2000, `maxPlanItems` 12 | Single-mode size warnings; see [Large specs and plans](#large-specs-and-plans). |
| `update` | `mode` `"notify"`, `intervalHours` 24, `timeoutSec` 20 | Update checks for the kit: `mode` is `"off"`, `"notify"` or `"auto"`; `intervalHours` is the minimum time between network checks; `timeoutSec` limits each git call. Checks are off when `CI` is set or `SPECLOOP_DISABLED=1`. |
| `phases` | `plan` → `implement` → `review` | The single-mode phase graph; see below. |
| `feature` | see [Feature mode](#feature-mode) | Feature-mode settings and phase graphs. |

### Personal configuration: config.local.json

`config.json` is shared with your team through git. For settings that are yours alone, for example how you want updates handled, create `.claude/specloop/config.local.json`. It takes any key that `config.json` takes, with the same structure:

```json
{ "update": { "mode": "auto" }, "maxIterations": 30 }
```

The effective configuration is `defaults.json`, then `config.json`, then `config.local.json`, deep-merged in that order, so a later file wins key by key, nested keys included. A missing file is the same as an empty one. A file that cannot be read or is not valid JSON is ignored, with a warning on stderr.

- It is git-ignored: the managed `.gitignore` block covers everything in `.claude/specloop/` except `config.json` and `overrides/`.
- The installer never creates, changes or removes it. `--force` recreates `config.json` only, and `--uninstall --keep-config` keeps it together with `config.json` and `overrides/`.
- specloop itself writes to it only for `/sl-update never`, which sets `update.mode` to `"off"` and keeps your other keys.

### Phase graph

Each phase has `next` (a phase name or `done`), an optional `allowEdits` list of path prefixes (omit it to allow all edits), an optional `instructions` file, and an optional `check`:

| `check.type` | Fields | Passes when |
|--------------|--------|-------------|
| `file` | `path` | the file exists and is non-empty |
| `command` | `command` | the command exits 0 (run with `shell`, default `bash`, from the project root) |
| `verdict` | `path`, `approve`, `reject`, `onReject` | the file contains `approve`. If it contains `reject`, the flow jumps to `onReject`. |

A check's `path` may use `{attempt_dir}`. A phase with no check passes immediately. To remove a phase, set it to `null` and re-point the previous phase's `next`. For example, to skip review:

```json
{ "phases": { "implement": { "next": "done" }, "review": null } }
```

To add a phase, for example `docs` after `implement`, define it in `config.json`, point `implement.next` at it, and put the instructions in `overrides/docs.md`.

In instruction files, `{spec}` becomes the spec path, `{run_dir}` becomes `.claude/specloop/run`, `{attempt_dir}` becomes the current attempt's directory, and `{review_file}` becomes the path the current phase's verdict check reads, which is `{attempt_dir}/review.md` in a phase without one.

### Review attempts

A review belongs to one attempt, so a result that arrives late never counts for the wrong step. `attempt` in the state goes up each time a phase with a `verdict` check is entered and on every takeover. A failed check or a pause and resume keeps it, so a subagent that is still running can finish.

`{attempt_dir}` is `.claude/specloop/run/attempts/<step id or "single">-<attempt>`. The default `check.path` of both review phases is `{attempt_dir}/review.md`, and a replan request goes to `{attempt_dir}/replan.md`. A rejected attempt's review stays in its directory, and the phase brief lists the reviews of the current step that a gate used, plus the open attempt's file. `/sl-advance` past a review counts as using it.

A file written into a closed attempt never affects the workflow and stays out of the brief, and `/sl-status` lists it, for example `late review for step 5 (attempt 1): CHANGES_REQUESTED, <path>`. In single mode, where `/sl-status` prints JSON, these lines are in a `lateReviews` key. A commit is never undone automatically; you decide what to do about a late rejection.

`check.path` stays configurable. With a fixed path that does not use `{attempt_dir}`, the shipped review instructions use that path through `{review_file}`. A rejected review is renamed to `<name>-<iteration>` as before. When the phase starts, a review a gate already used moves back to its own attempt, and any other file already there moves to `{attempt_dir}/late-review.md`.

Whenever a check fails or a review requests changes, the full details (the complete check output or review text, not truncated) are written to `.claude/specloop/run/last-failure.md`. The file is removed when a phase passes and on `/sl-advance`.

### Delegating a phase to a subagent

Set `"delegate": true` on any phase (default `false`; no shipped phase enables it) to run that phase in a fresh context:

```json
{ "phases": { "implement": { "delegate": true } } }
```

In a delegated phase the main session acts as an orchestrator. Instead of the bare phase instructions, `/sl-go`, the Stop hook and the session context give it an orchestration preamble (`phases/delegate.md`, overridable with `overrides/delegate.md`; `{brief}` marks where the brief goes). The preamble tells it to start exactly one subagent with a self-contained **phase brief**, not to read or edit files itself, to wait for a subagent that is already running instead of starting a second one, and to finish its turn with a summary of at most 5 lines. The brief holds the rendered phase instructions and the files the subagent should read: the spec, `run/plan.md`, the reviews of the current step, and `run/last-failure.md`. When a check fails, the Stop hook's message stays short and points at `run/last-failure.md` instead of including the full output.

Gates still run only when the main session stops; specloop registers no `SubagentStop` hook. A phase is not checked while its subagent runs: when Stop's `background_tasks` lists a `subagent`, `workflow` or `teammate`, in any phase, the Stop hook does nothing and costs no iteration. The subagent's completion wakes the chat, and the next Stop runs the gate. Shell tasks and monitors do not count, because a dev server must not hold a phase open forever. On a Claude Code version that does not send `background_tasks`, the check runs as usual.

There is one gap. A subagent that ends its turn and waits on its own background work is not listed as a `subagent`: Stop sees only its shell, as `type: shell`, until the work finishes and the subagent resumes. The check therefore runs while that subagent is unfinished, as it would on a version with no `background_tasks`. A failed check costs an iteration, and a passing check can advance the phase before the subagent reports. A subagent stays listed for as long as it keeps working, so the shipped preamble's brief tells it to run long commands in the foreground and not to end its turn while it waits on background work. An override of `delegate.md` should keep that line.

Note that a phase named `delegate` would share its instructions file with the preamble.

### Large specs and plans

A single plan → implement → review cycle works best for changes that fit in one commit. specloop warns when a spec or plan looks bigger than that, and recommends `/sl-feature` (feature mode, which splits work into multiple commits) instead.

| Key | Default | Check |
|-----|---------|-------|
| `largeSpecWarning.maxLines` | `200` | spec has more lines than this |
| `largeSpecWarning.maxWords` | `2000` | spec has more words than this |
| `largeSpecWarning.maxPlanItems` | `12` | `run/plan.md` has more checklist items (`- [ ]` / `- [x]`) than this |

- **Spec size.** `/sl-spec` prints a warning with the spec's line and word counts and the limits. The first `/sl-go` of that run does not start; it repeats the warning. Running `/sl-go` again starts in single mode anyway (state records `largeSpecAcknowledged`). Resuming a run that has already started never warns.
- **Plan size.** When the `plan` phase passes with too many checklist items, the workflow pauses before `implement` instead of continuing. Either run `/sl-reset` and then `/sl-feature`, or run `/sl-go` to continue in `implement` (state records `largePlanAcknowledged`).

Set a key to `null` to disable that check, or the whole block to disable all of them:

```json
{ "largeSpecWarning": { "maxPlanItems": null } }
```

### Environment variables

| Variable | Effect |
|----------|--------|
| `SPECLOOP_DISABLED=1` | All hooks become no-ops. |
| `SPECLOOP_PYTHON=/path/to/python` | Interpreter for the launcher and installer (skips auto-detection). |
| `CI` (any value) | Update checks behave as `off`. Headless runs must not modify themselves. |

Settings that are yours belong in [`config.local.json`](#personal-configuration-configlocaljson), not in environment variables; the variables above describe the environment, not a preference.

## Feature mode

For features too big for one plan → implement → review cycle, load the spec with `/sl-feature` instead of `/sl-spec`. A planning subagent splits the spec into small ordered steps. Each step then runs through its own implement → review cycle and ends in one commit on a dedicated branch.

```
/sl-feature specs/big-feature.md   load the spec in feature mode (Claude summarizes it)
/sl-go                             create the feature branch, run the planning phase, pause for approval
/sl-go                             approve the plan; step 1: implement -> review -> (rework ...) -> commit
                                   step 2: ... until the last step is committed -> done
```

End to end: `/sl-feature` loads the spec → the first `/sl-go` creates the branch and a planning subagent writes `run/plan.md` and `run/steps.json` → you review and edit the plan, and `/sl-go` approves it → every step runs implement → review in fresh subagents, and the engine commits it → after the last step the run is `done` and you get a list of the commits to review and open a pull request from. `/sl-status` shows the progress at any time.

**Single mode or feature mode?**

| | Single mode (`/sl-spec`) | Feature mode (`/sl-feature`) |
|-|--------------------------|------------------------------|
| Good for | A change that fits one reviewable commit | A feature that needs several commits |
| Result | One uncommitted diff for you to commit | A branch with one commit per step |
| Plan | `run/plan.md` (free text) | `run/plan.md` plus `run/steps.json`, approved by you |
| Review | Once, over the whole change | Per step, over that step's diff |
| Context | One session does everything (delegation optional) | Each phase runs in a fresh subagent by default |
| Git | Untouched; you commit | Needs a clean git work tree; the engine owns the branch and commits |

The [size warnings](#large-specs-and-plans) suggest feature mode when a single-mode spec or plan gets large.

**Branch.** The first `/sl-go` of a feature run creates and checks out `feature.branchPrefix` + a slug of the spec's first `# ` heading (or its file name): lowercase ASCII, `-` separators, at most 50 characters. If that branch exists, `-2`, `-3`, … is appended. It refuses, without changing anything, when the directory is not a git repository, the repository has no commits, `HEAD` is detached, or tracked files have uncommitted changes (staged or not; changes under `.claude/specloop/` are ignored). State records `branch`, `baseRef` (the starting commit) and `preexistingUntracked` (untracked files that existed at start). Later `/sl-go` calls refuse if the current branch is not the run's branch.

**Planning.** The `feature-plan` phase is delegated to a subagent (see [Delegating a phase](#delegating-a-phase-to-a-subagent)). It writes `run/plan.md` (goal, acceptance criteria, assumptions, step-order rationale) and `run/steps.json`:

```json
{
  "feature": "Short feature title",
  "steps": [
    { "id": 1, "title": "At most 72 characters", "type": "feat", "description": "What and why.",
      "acceptance": ["Observable criterion"], "files": ["src/x.py"], "status": "pending", "commit": null }
  ]
}
```

Edits are restricted to `.claude/specloop/run/`. The gate passes when `steps.json` parses, matches the schema and every step is `pending`; otherwise the errors are written to `run/last-failure.md` and the subagent keeps working.

**Approval.** When the plan passes its gate, the run pauses. Review or edit `run/plan.md` and `run/steps.json`, then run `/sl-go`: it validates `steps.json` again (refusing with the errors if it is invalid), stores the approved copy in state as `approvedSteps`, and starts step 1.

**Step cycle.** Each step runs through `feature.stepPhases`, starting at `feature.stepInitialPhase`. The default cycle mirrors single mode: `implement` (delegated, no edit restriction, checked with your test command from `phases.implement.check`), then `review` (delegated, edits limited to `run/`, verdict on `{attempt_dir}/review.md`; `CHANGES_REQUESTED` goes back to `implement`). A step phase without a `check` reuses the check of the top-level phase with the same name. You can add phases (for example a per-step `lint`) or remove one with `null`, as in the top-level graph. Instructions come from `phases/step-<phase>.md` (`step-implement.md`, `step-review.md`), overridable in `overrides/`, with these placeholders on top of `{spec}` and `{run_dir}`: `{step_index}` (1-based), `{step_count}`, `{step_id}`, `{step_title}`, `{step_description}`, `{step_acceptance}` (a bullet list) and `{steps_file}`. `maxIterations` applies per step.

When a step starts, the engine marks it `active`, resets the iteration counter, and moves the previous step's `run/last-failure.md` (and any `run/review*.md` from a fixed `check.path`) into `run/steps/<id>/`. Reviews stay in `run/attempts/`. Status and commit changes are written both to `run/steps.json` and to `approvedSteps`.

**Commits.** `commit` is a reserved pseudo-phase at the end of the step cycle (the default `review.next`). When a step reaches it, the Stop hook:

1. runs `git add -A`, then unstages everything under `feature.commitExclude` and every `preexistingUntracked` path;
2. if nothing is staged, marks the step `empty` (no commit, a warning in history) and moves on;
3. otherwise commits with `feature.commitMessage` (never `--no-verify`), stores the SHA in the step's `commit`, marks it `done`, and starts the next step.

If the commit fails (for example a pre-commit hook rejects it), the step goes back to `feature.stepInitialPhase` with the output in `run/last-failure.md` and in the stop reason; the attempt counts as an iteration. `/sl-advance` force-passes the current step phase; advancing out of the last one commits the step rather than skipping it. With `feature.pauseBetweenSteps`, the run pauses after each commit until `/sl-go`.

**Completion.** After the last step, the run is `done` and the final message lists every step as `<short sha> <title>` (or `(empty)`) with the branch name. Review the branch and open a pull request yourself: specloop never pushes, and `/sl-reset` never deletes the branch or its commits.

**Replanning.** A step can reveal that the rest of the plan is wrong. During any step phase Claude may write `{attempt_dir}/replan.md` explaining what is wrong with the remaining plan and proposing a change (the step instructions describe this). The Stop hook looks for it before running the phase check:

- `feature.replan: "stall"` (default): the run stalls. Read the `replan.md` the message names, optionally edit the pending or active steps in `run/steps.json`, then run `/sl-go`. The engine validates the file (schema plus the steps file protection rules against `approvedSteps`) and refuses with the errors if it fails, changing nothing. Otherwise it stores the new plan, archives the request to `run/replans/replan-N.md`, and restarts the current step at `feature.stepInitialPhase` with the iteration counter reset. If you removed the current step, the next pending step starts instead (or the run completes when none remain).
- `feature.replan: "auto"`: the engine enters `feature.replanPhase` (default `feature-replan`, defined in `feature.planPhases`, delegated, edits limited to `run/`, instructions in `phases/feature-replan.md`). A planning subagent rewrites only pending or active steps. The gate is the `steps` check (without `requireAllPending`) plus the steps file protection rules; when it passes, the engine continues as in the stall case. At most `feature.maxReplans` auto-replans run per run; a request beyond the cap stalls with a message saying so.

The current step's uncommitted work stays in the working tree either way; the engine never stashes, resets or checks out. `/sl-advance` in the replan phase adopts the current `steps.json` if it passes the same checks.

**Git guard rails.** While a feature run is `active` (planning or a step), PreToolUse denies Bash commands that contain any entry of `feature.blockedCommands` (default: `git commit`, `git checkout`, `git switch`, `git reset`, `git rebase`, `git merge`, `git stash`, `git branch -D`, `git push`), because the engine owns the branch and the commits. Matching is by substring, like `blockedCommandsUntilDone`, which still applies in both modes. Single mode is not affected. `/sl-pause` lifts the guard if you need to run one of these yourself.

**Steps file protection.** On every Stop during a step, before the phase check runs, the engine compares `run/steps.json` with its approved copy (`approvedSteps` in state). Changing a `done` or `empty` step, any step's `status` or `commit`, removing or reordering finished steps, or adding a non-pending step blocks the stop with the errors and keeps the phase; a missing or unparsable file is blocked too. Other edits (for example a pending step's description) pass the check but are not adopted: only the engine updates `approvedSteps`, and it rewrites `run/steps.json` from it at the next commit. Planning and plan approval are not affected: any `steps.json` that passes validation at approval becomes `approvedSteps`.

**Context and status.** In feature mode the context header shows progress, for example `[specloop] feature step 3/7 "Add schema validation", phase=review, iteration 2, branch=specloop/big-feature`, or `[specloop] feature planning, phase=feature-plan, iteration 1, branch=…` while planning. `/sl-status` prints a summary instead of raw JSON:

```
[specloop] feature run, status=active
spec: specs/big-feature.md
branch: specloop/big-feature (base 1a2b3c4)
phase: review, iteration 2
owner: 3f2a9c1e (since 2026-10-30T07:03:47+00:00)
steps (1/3 done):
  1  done     5d6e7f8  Add schema validation
  2  active   -        Wire the check
  3  pending  -        Document it
```

The single-mode size warnings do not apply in feature mode.

| Key | Default | Meaning |
|-----|---------|---------|
| `feature.branchPrefix` | `"specloop/"` | Prefix of the feature branch name. |
| `feature.planPhase` | `"feature-plan"` | First phase of a feature run. |
| `feature.requirePlanApproval` | `true` | Pause for approval after planning. When `false`, the plan is approved as soon as its gate passes. |
| `feature.gitTimeoutSec` | `60` | Timeout for every git call the engine makes. |
| `feature.planPhases` | `feature-plan` phase | Phase graph for planning, with the same keys as `phases` (`delegate`, `allowEdits`, `check`, `instructions`). |
| `feature.stepInitialPhase` | `"implement"` | First phase of every step. |
| `feature.stepPhases` | `implement` → `review` → `commit` | Phase graph of the step cycle; `commit` is the reserved terminal pseudo-phase. |
| `feature.commitExclude` | `[".claude/specloop/"]` | Paths never included in step commits. |
| `feature.commitMessage` | `"{type}: {step_title}\n\nspecloop step {step_index}/{step_count} ({step_id}) of {spec}"` | Commit message template; `{type}` is the step's `type` (default `feat`). |
| `feature.pauseBetweenSteps` | `false` | Pause after each step's commit. |
| `feature.replan` | `"stall"` | What a `replan.md` request does: `"stall"` for the user, or `"auto"` to run the replan phase. |
| `feature.replanPhase` | `"feature-replan"` | Phase (in `feature.planPhases`) that rewrites the remaining steps in `"auto"` mode. |
| `feature.maxReplans` | `3` | Auto-replans allowed per run; later requests stall. |
| `feature.enforceDelegation` | `false` | In delegated phases, deny the main session's edit tools outside `run/` (see [Hacks and limitations](#hacks-and-limitations)). |
| `feature.blockedCommands` | `git commit`, `checkout`, `switch`, `reset`, `rebase`, `merge`, `stash`, `branch -D`, `push` | Bash commands denied while a feature run is active. |

## Tips

- Write specs with explicit acceptance criteria. The plan, the tests and the review all key off them.
- If you commit `.claude/settings.json` with `--scope project`, every teammate who opens the repo gets the hooks. Without a loaded spec the hooks do nothing, so that's harmless.
- A long test suite runs on every Stop during `implement`. Point `check.command` at a fast subset if needed.
- Claude can't stop to ask you questions while a phase is active. Use `/sl-pause`, or let it record assumptions in `plan.md`. The plan instructions already ask for that.

## Hacks and limitations

- **Delegation is instruction-only and not guaranteed.** Hooks cannot start subagents, so `delegate: true` only *tells* the main session to hand the phase to a subagent. It may do the work itself anyway, for example after context compaction or when it judges the phase to be small. The gates still protect the result either way; only the fresh-context benefit is lost.
  - *How to notice:* in a delegated phase, the main session reads source files, edits files or runs the tests itself instead of starting one subagent.
  - *How to recover:* run `/sl-pause`, then `/clear`, then `/sl-go`. `/sl-go` re-sends the orchestrator instructions with the phase brief to a clean context.
- **`feature.enforceDelegation` can block, not force.** Claude Code adds `agent_id` (and `agent_type`) to hook input when a tool call comes from inside a subagent, and leaves them out for the main conversation ([hooks reference](https://code.claude.com/docs/en/hooks), "Common input fields"). With `enforceDelegation: true`, PreToolUse uses that to deny the main session's `Edit`, `Write`, `MultiEdit` and `NotebookEdit` calls outside `.claude/specloop/run/` in any delegated phase, and tells it to start a subagent. Subagent calls are never affected. Limits:
  - it cannot make the main session start a subagent; it only stops it from editing source files itself;
  - read-only phases such as review (and planning), which already edit only `run/`, cannot be forced into a subagent at all: the main session can still read the code and write the review itself;
  - edits made through Bash (for example `sed -i` or `>` redirects) are not covered;
  - `agent_type` alone is not trusted, because it is also present in the main conversation of a session started with `--agent`; in-process teammates carry an `agent_id` and are treated like subagents.

## Updates

specloop can tell you when a newer kit is available and, if you ask for it, install it. It checks the clone that ran `install.py` and the remote branch that clone tracks. No repository URL is built in: a fork's clone tracks the fork, so a fork follows its own upstream, not the original repository. Only a higher `VERSION` counts as an update.

**Modes.** Set `update.mode`, preferably in your own [`config.local.json`](#personal-configuration-configlocaljson):

| `update.mode` | Meaning |
|---------------|---------|
| `off` | No checks, no git calls, no notices. |
| `notify` (default) | Check, then show a notice at a safe point. You run `/sl-update` to apply it. |
| `auto` | Check, then apply the update at a safe point and print one line saying what changed. Falls back to the `notify` notice, with the reason, when an automatic update is not safe. |

The checks are also off when the environment variable `CI` is set or `SPECLOOP_DISABLED=1`, whatever the configuration says.

| Key | Default | Meaning |
|-----|---------|---------|
| `update.mode` | `"notify"` | `off`, `notify` or `auto`, as above. |
| `update.intervalHours` | `24` | Minimum time between network checks. The same version is also announced at most once per interval. |
| `update.timeoutSec` | `20` | Timeout for each git call of a check. A check lock older than twice this is stale and is replaced. |

**How a check works.** At every SessionStart specloop compares the `VERSION` of the clone with the installed one (one file read, no network): this finds an update you pulled but did not install into this project. When `intervalHours` have passed since the last network check, it also starts `engine.py update-check` as a detached background process, so SessionStart returns at once. That process takes `update.lock`, runs `git fetch` in the clone with the timeout, and writes the result to `update.json` (the semaphore file: time of the check, installed, clone and remote versions, the remote commit, a skipped version, and `error` if the check failed). A failed check is not retried before the next interval, and its error appears only in `/sl-update` and `/sl-status`, never as a session notice. Both files are runtime state: git-ignored, and kept by `/sl-reset`. The result of a background check is used at the next safe point.

**Safe points.** The kit is never changed, and no notice is shown, while a workflow is busy: a state file exists and its status is not `done` (so `active`, `paused`, `stalled` and a feature plan awaiting approval all count). A newer engine may read an old state differently, and in feature mode it would change the rules in the middle of a branch. The safe points are:

1. SessionStart while no workflow is busy. The notice is a message to you and does not enter Claude's context.
2. The completion of a workflow: the notice is added to the final message.
3. `/sl-update`, which you can run whenever no workflow is busy.

A notice looks like `[specloop] 0.4.0 is available (installed 0.3.0, from github.com/you/specloop main). Run /sl-update to update.` and mentions `skip` and `never`.

**Major versions are never applied automatically.** When the available version has a higher major number than the installed one, `auto` only notifies and says that `/sl-update` is needed. While the major number is `0`, a higher minor number counts as major too (`0.3.0` to `0.4.0`), because semver treats `0.y` as unstable. Updates do not migrate `state.json` or `config.json`.

**`/sl-update`.**

| Form | What it does |
|------|--------------|
| `/sl-update` | Check now, ignoring `intervalHours`, and apply a newer version. Refused while a workflow is busy; the message names the command that ends or resets it. |
| `/sl-update check` | Check now and report. Changes nothing. |
| `/sl-update skip` | Remember the newest available version as skipped. It is never announced or auto-applied again; a later version is handled as usual. |
| `/sl-update never` | Does what `skip` does and sets `update.mode` to `"off"` in `config.local.json`. The output says how to turn checks back on: set `update.mode` there, or remove the key. |

Applying an update fast-forwards the clone (a fast-forward-only merge of `<remote>/<branch>`), runs the clone's `install.py` for this project with the options recorded at install time, prints the old and new versions and up to 20 `git log` lines, and tells you to start a new session so that changed hooks and commands are loaded. It refuses, and changes nothing, when the clone has uncommitted changes, is not on the recorded branch, or cannot be fast-forwarded (a fork with local commits, for example). It never pulls with a merge or rebase, stashes, or switches branches in the clone.

**Enabling checks on an old install.** A kit installed by a version before 0.3.0 does not record where it came from, so it cannot check for updates, and the notice says so. Rerun the installer once from the clone, `install.py <project>`, to record the source. The same limit applies when the clone is not a git work tree (a downloaded zip) or its current branch has no upstream; the installer prints one line saying why.

## Uninstall

```bash
~/tools/specloop/install.sh ~/code/my-project --uninstall
```

## License

[0BSD](LICENSE): do anything with it, no attribution required, no warranty.
