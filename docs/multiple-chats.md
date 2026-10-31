[Documentation](README.md) › Several chats in one project

# Several chats in one project

Hooks are configured per project, so they fire in every chat open in that folder. specloop keeps one workflow per checkout, and only one chat drives it.

## Who drives

The chat that last typed `/sl-go` is the workflow's owner, stored in `state.json` as `owner`. Only a typed command claims it: a prompt that merely mentions `/sl-go` does not, and neither do `/sl-spec` and `/sl-feature`. The owner stays across pause, stall and done; `/sl-reset` and loading a new spec remove it. With no owner, for example in a workflow started by an older version, every chat drives it as before until someone types `/sl-go`. `/sl-status` shows the owner in feature mode; in single mode it is in the state JSON.

## What other chats see

While the workflow is `active`, a chat that is not the owner is left alone:

| Hook | In a chat that is not the owner |
|------|---------------------------------|
| Stop | Does nothing: no check, no iteration, no failure file. |
| SessionStart | One line saying that another chat owns the workflow and how to take it over. No phase instructions. |
| UserPromptSubmit | Adds no context. |
| PreToolUse | Phase `allowEdits`, `feature.enforceDelegation` and `blockedCommandsUntilDone` do not apply. |

## What stays blocked in other chats

One checkout is still one working tree, so these apply in every chat:

- specloop's own files (`state.json`, `config.json`, `defaults.json`, `engine.py`) cannot be edited.
- `feature.blockedCommands` stay denied, because a `git checkout` or `git stash` in any chat would break the run.
- Edits under `.claude/specloop/run/` are denied outside a subagent, so a stray review file cannot affect a gate.
- `/sl-advance` and `/sl-reset` are blocked while `active`; the message names `/sl-go` and `/sl-pause`. A paused workflow whose chat is gone can still be advanced or reset from anywhere.

## Takeover

`/sl-go` in another chat replaces the owner. This is how you recover from a closed chat. It is also needed after `/clear`, which gives the chat a new session id; `--resume` and compaction keep it. The output starts with a line saying that this chat now drives the workflow. Stop any subagent still running in the previous chat: the takeover opens a new [review attempt](review-attempts.md), so its result no longer counts. There is no heartbeat or timeout. A `/sl-go` refused because the current branch is not the feature run's branch leaves the owner as it is.

## State files

`state.json` and `run/steps.json` are replaced atomically, and every state change takes `state.lock` and reloads the file first, so two processes cannot overwrite each other and a `/sl-pause` is never lost. The lock is not held while a check or git runs. A Stop that finds its workflow changed during the check (paused, taken over, moved to another phase) drops the result. A pause during a step commit is kept: the commit stands, the next step is prepared and the workflow stays paused. If two Stops overlap, for example during a takeover, only one runs the check, and the other says that a check is marked as running. A Stop hook that was killed leaves that mark behind for `checkTimeoutSec` plus a minute; `/sl-go` clears it at once. A Stop that cannot record its result because the lock stays taken says so, and `/sl-go` continues. A step commit whose result was lost that way still counts: the next Stop finds HEAD past `commitBase` in the state and records that commit for the step. `history` entries written by hooks carry `session`, the first 8 characters of the session id, and claims and takeovers are recorded there too.

## Limits

- This is not isolation. Two chats editing the same tracked files still collide, and `git add -A` in a step commit picks up new files from any chat. Use one `git worktree` per task if chats must not touch each other's work.
- Claiming and blocking rely on `UserPromptSubmit` running before the command's shell line. Claude Code 2.1.309 does that. Claude Code 2.1.252 runs the shell line first and a block does not stop it, so there a takeover line appears only on the next `/sl-go`, and `/sl-advance` and `/sl-reset` are not blocked. Other versions were not tested.
- A hook input without `session_id` counts as the owner, so older Claude Code versions keep working.

## See also

- [Review attempts](review-attempts.md): why a takeover invalidates a running subagent's result.
- [Guard rails](guard-rails.md): the rules that stay in force in every chat.
- [Commands](commands.md): `/sl-go`, `/sl-advance` and `/sl-reset`.
- [Installation](installation.md#git-worktrees): installing the kit into each worktree.
