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

An update replaces the engine, launcher, `defaults.json` and the shipped `phases/`. Your `config.json` and `overrides/` are never touched.

## Commands

| Command | What it does |
|---------|--------------|
| `/sl-spec <file>` | Load a spec; status `ready`. Clears the previous run's `run/` artifacts (plan, reviews). Claude summarizes the spec and waits. |
| `/sl-feature <file>` | Load a spec in [feature mode](#feature-mode) (one commit per planned step). Also clears `run/`. |
| `/sl-go` | Start, or resume after pause/stall. |
| `/sl-pause` | Stop driving the session; keep the phase. |
| `/sl-status` | Show the state: the raw state JSON in single mode, a step summary in feature mode. |
| `/sl-advance` | Force-pass the current phase. |
| `/sl-reset` | Delete the state and the `run/` artifacts. |

## Configuration

Files in `<project>/.claude/specloop/`:

| File | Owner | Purpose |
|------|-------|---------|
| `defaults.json` | kit (replaced on update) | Default settings and phase graph. |
| `config.json` | **project** | Your overrides, deep-merged over `defaults.json`. |
| `phases/<phase>.md` | kit (replaced on update) | Shipped phase instructions. |
| `overrides/<phase>.md` | **project** | Your phase instructions; take precedence over `phases/`. |
| `state.json`, `run/` | runtime | Current state, plan and review files. |

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
| `phases` | `plan` → `implement` → `review` | The single-mode phase graph; see below. |
| `feature` | see [Feature mode](#feature-mode) | Feature-mode settings and phase graphs. |

### Phase graph

Each phase has `next` (a phase name or `done`), an optional `allowEdits` list of path prefixes (omit it to allow all edits), an optional `instructions` file, and an optional `check`:

| `check.type` | Fields | Passes when |
|--------------|--------|-------------|
| `file` | `path` | the file exists and is non-empty |
| `command` | `command` | the command exits 0 (run with `shell`, default `bash`, from the project root) |
| `verdict` | `path`, `approve`, `reject` (optional: `null` drops the default), `onReject` | the last verdict line of the file is `approve`. A verdict line is a whole line equal to `approve` or `reject` once surrounding whitespace, Markdown emphasis, `-`/`+`/`*` bullets, heading and quote markers and a trailing period are removed from both (markers are single-line; ones that become empty or identical are compared as written); a marker inside a sentence or a fenced code block does not count, and an unclosed fence hides every verdict. If the last verdict line is `reject`, the file is archived and the flow jumps to `onReject`. |

A phase with no check passes immediately. To remove a phase, set it to `null` and re-point the previous phase's `next`. For example, to skip review:

```json
{ "phases": { "implement": { "next": "done" }, "review": null } }
```

To add a phase, for example `docs` after `implement`, define it in `config.json`, point `implement.next` at it, and put the instructions in `overrides/docs.md`.

In instruction files, `{spec}` becomes the spec path and `{run_dir}` becomes `.claude/specloop/run`.

Whenever a check fails or a review requests changes, the full details (the complete check output or review text, not truncated) are written to `.claude/specloop/run/last-failure.md`. The file is removed when a phase passes and on `/sl-advance`.

### Delegating a phase to a subagent

Set `"delegate": true` on any phase (default `false`; no shipped phase enables it) to run that phase in a fresh context:

```json
{ "phases": { "implement": { "delegate": true } } }
```

In a delegated phase the main session acts as an orchestrator. Instead of the bare phase instructions, `/sl-go`, the Stop hook and the session context give it an orchestration preamble (`phases/delegate.md`, overridable with `overrides/delegate.md`; `{brief}` marks where the brief goes). The preamble tells it to start exactly one subagent with a self-contained **phase brief**, not to read or edit files itself, and to finish its turn with a summary of at most 5 lines. The brief holds the rendered phase instructions and the files the subagent should read: the spec, `run/plan.md`, any `run/review*.md`, and `run/last-failure.md`. When a check fails, the Stop hook's message stays short and points at `run/last-failure.md` instead of including the full output.

Gates still run only when the main session stops; specloop registers no `SubagentStop` hook. Note that a phase named `delegate` would share its instructions file with the preamble.

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

**Step cycle.** Each step runs through `feature.stepPhases`, starting at `feature.stepInitialPhase`. The default cycle mirrors single mode: `implement` (delegated, no edit restriction, checked with your test command from `phases.implement.check`), then `review` (delegated, edits limited to `run/`, verdict on `run/review.md`; `CHANGES_REQUESTED` goes back to `implement`). A step phase without a `check` reuses the check of the top-level phase with the same name. You can add phases (for example a per-step `lint`) or remove one with `null`, as in the top-level graph. Instructions come from `phases/step-<phase>.md` (`step-implement.md`, `step-review.md`), overridable in `overrides/`, with these placeholders on top of `{spec}` and `{run_dir}`: `{step_index}` (1-based), `{step_count}`, `{step_id}`, `{step_title}`, `{step_description}`, `{step_acceptance}` (a bullet list) and `{steps_file}`. `maxIterations` applies per step.

When a step starts, the engine marks it `active`, resets the iteration counter, and moves the previous step's `run/review*.md` and `run/last-failure.md` into `run/steps/<id>/`. Status and commit changes are written both to `run/steps.json` and to `approvedSteps`.

**Commits.** `commit` is a reserved pseudo-phase at the end of the step cycle (the default `review.next`). When a step reaches it, the Stop hook:

1. runs `git add -A`, then unstages everything under `feature.commitExclude` and every `preexistingUntracked` path;
2. if nothing is staged, marks the step `empty` (no commit, a warning in history) and moves on;
3. otherwise commits with `feature.commitMessage` (never `--no-verify`), stores the SHA in the step's `commit`, marks it `done`, and starts the next step.

If the commit fails (for example a pre-commit hook rejects it), the step goes back to `feature.stepInitialPhase` with the output in `run/last-failure.md` and in the stop reason; the attempt counts as an iteration. `/sl-advance` force-passes the current step phase; advancing out of the last one commits the step rather than skipping it. With `feature.pauseBetweenSteps`, the run pauses after each commit until `/sl-go`.

**Completion.** After the last step, the run is `done` and the final message lists every step as `<short sha> <title>` (or `(empty)`) with the branch name. Review the branch and open a pull request yourself: specloop never pushes, and `/sl-reset` never deletes the branch or its commits.

**Replanning.** A step can reveal that the rest of the plan is wrong. During any step phase Claude may write `run/replan.md` explaining what is wrong with the remaining plan and proposing a change (the step instructions describe this). The Stop hook looks for it before running the phase check:

- `feature.replan: "stall"` (default): the run stalls. Read `run/replan.md`, optionally edit the pending or active steps in `run/steps.json`, then run `/sl-go`. The engine validates the file (schema plus the steps file protection rules against `approvedSteps`) and refuses with the errors if it fails, changing nothing. Otherwise it stores the new plan, archives the request to `run/replans/replan-N.md`, and restarts the current step at `feature.stepInitialPhase` with the iteration counter reset. If you removed the current step, the next pending step starts instead (or the run completes when none remain).
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
| `feature.replan` | `"stall"` | What a `run/replan.md` request does: `"stall"` for the user, or `"auto"` to run the replan phase. |
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

## Uninstall

```bash
~/tools/specloop/install.sh ~/code/my-project --uninstall
```

## License

[0BSD](LICENSE): do anything with it, no attribution required, no warranty.
