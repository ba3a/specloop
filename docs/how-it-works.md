[Documentation](README.md) › How it works

# How it works

specloop is a spec-driven development loop for [Claude Code](https://claude.com/claude-code), built only from hooks, slash commands and a small Python state machine. You drop in a specification, start development by hand, and Claude is walked through **plan → implement → review** phases. A phase only completes when its check passes: the plan file exists, the tests are green, or the reviewer approves.

It runs inside your normal Claude Code session (CLI, desktop app or IDE), so it uses your Claude subscription. You don't need API keys or a separate orchestrator.

## Phases

| Phase       | Claude does                                        | Gate (checked by the Stop hook)                 | Edits allowed            |
|-------------|----------------------------------------------------|--------------------------------------------------|--------------------------|
| `plan`      | Reads the spec, writes `.claude/specloop/run/plan.md` | plan file exists and is non-empty              | only `.claude/specloop/run/` |
| `implement` | Test-first implementation of the plan              | your test command exits 0                        | anything                 |
| `review`    | Reviews the diff (ideally via a subagent), writes `review.md` | `VERDICT: APPROVE` → done; `VERDICT: CHANGES_REQUESTED` → back to implement | only `.claude/specloop/run/` |

The phases are a configurable graph: see [Phases](phases.md) for the check types and for adding or removing a phase. A spec that is too big for one cycle is better run in [feature mode](feature-mode.md), which splits it into steps with one commit each.

## Hooks

The engine plugs into four hooks:

- **Stop**: each time Claude tries to end its turn, it runs the current phase's check. If the check fails, the stop is blocked and Claude gets the failure (for example the tail of the test output) and keeps working. If it passes, the workflow moves to the next phase and Claude gets that phase's instructions.
- **PreToolUse**: enforces per-phase edit restrictions. It blocks `git push` and `gh pr create`/`merge` until the workflow is done, and stops Claude from editing specloop's own state. See [Guard rails](guard-rails.md).
- **SessionStart / UserPromptSubmit**: put the current phase and its instructions into context, so a new or resumed session picks up where the last one stopped.

## Safety valves

- After `maxIterations` blocked stops (default 15) the workflow goes to `stalled` and Claude is allowed to stop. `/sl-go` resumes it.
- `/sl-pause` turns the hooks off when you want to talk to Claude in between.
- A spec or plan that looks too big for one plan → implement → review cycle triggers a warning (see [Size warnings](size-warnings.md)).
- Setting `SPECLOOP_DISABLED=1` makes every hook a no-op.
- If the engine itself crashes, the hook logs to stderr and exits 0, so a crash never breaks your session.
- An unreadable `state.json` is reported instead of ignored: Stop and the context hooks say that specloop is not enforcing anything, and `/sl-reset` clears it.

## Files and ownership

Files in `<project>/.claude/specloop/`:

| File | Owner | Purpose |
|------|-------|---------|
| `defaults.json` | kit (replaced on update) | Default settings and phase graph. |
| `config.json` | **project** | Your overrides, deep-merged over `defaults.json`. |
| `config.local.json` | **person** | Your personal overrides, deep-merged over `config.json`. Git-ignored. See [Configuration](configuration.md#personal-configuration-configlocaljson). |
| `phases/<phase>.md` | kit (replaced on update) | Shipped phase instructions. |
| `overrides/<phase>.md` | **project** | Your phase instructions; take precedence over `phases/`. |
| `state.json`, `state.lock`, `run/` | runtime | Current state, its lock, plan and review files. |
| `update.json`, `update.lock` | runtime | Result of the last update check and the lock of a running check. See [Updates](updates.md). |
| `manifest.json` | kit | What the installer wrote, including `source`, the clone the kit was installed from. |

Everything in this directory except `config.json` and `overrides/` is git-ignored.

## See also

- [Phases](phases.md): the phase graph, check types and instruction placeholders.
- [Review attempts](review-attempts.md): how a review is tied to one attempt.
- [Configuration](configuration.md): how the configuration files are layered.
- [Commands](commands.md): the slash commands that drive the workflow.
