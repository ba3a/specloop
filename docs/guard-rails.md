[Documentation](README.md) › Guard rails

# Guard rails

The PreToolUse hook (see [How it works](how-it-works.md#hooks)) denies tool calls that would break a run. Some rules apply to every run, some only to [feature mode](feature-mode.md).

## Blocked commands in both modes

While a run is active, Bash commands that contain any entry of `blockedCommandsUntilDone` are denied. The default list is `git push`, `gh pr create` and `gh pr merge`. Matching is by substring. The list is a top-level key; see [Configuration](configuration.md).

| Key | Default | Meaning |
|-----|---------|---------|
| `blockedCommandsUntilDone` | `git push`, `gh pr create`, `gh pr merge` | Bash commands denied while a run is active, in both modes. |

## Edit restrictions per phase

A phase with an `allowEdits` list of path prefixes allows edits only under those paths; without it all edits are allowed (see [Phases](phases.md#the-phase-graph)). In the shipped graph `plan` and `review` may edit only `.claude/specloop/run/`. PreToolUse also stops Claude from editing specloop's own state.

[Several chats in one project](multiple-chats.md#what-stays-blocked-in-other-chats) lists which of these rules still apply in a chat that does not own the workflow.

## Git guard rails in feature mode

While a feature run is `active` (planning or a step), PreToolUse denies Bash commands that contain any entry of `feature.blockedCommands` (default: `git commit`, `git checkout`, `git switch`, `git reset`, `git rebase`, `git merge`, `git stash`, `git branch -D`, `git push`), because the engine owns the branch and the commits. Matching is by substring, like `blockedCommandsUntilDone`, which still applies in both modes. Single mode is not affected. `/sl-pause` lifts the guard if you need to run one of these yourself.

| Key | Default | Meaning |
|-----|---------|---------|
| `feature.blockedCommands` | `git commit`, `checkout`, `switch`, `reset`, `rebase`, `merge`, `stash`, `branch -D`, `push` | Bash commands denied while a feature run is active. |

## Steps file protection

On every Stop during a step, before the phase check runs, the engine compares `run/steps.json` with its approved copy (`approvedSteps` in state). Changing a `done` or `empty` step, any step's `status` or `commit`, removing or reordering finished steps, or adding a non-pending step blocks the stop with the errors and keeps the phase; a missing or unparsable file is blocked too. Other edits (for example a pending step's description) pass the check but are not adopted: only the engine updates `approvedSteps`, and it rewrites `run/steps.json` from it at the next commit. Planning and plan approval are not affected: any `steps.json` that passes validation at approval becomes `approvedSteps`.

## See also

- [Feature mode](feature-mode.md): the run these rules protect.
- [Replanning](replanning.md): how the approved steps change legitimately.
- [Delegating a phase](delegation.md): `feature.enforceDelegation` denies the main session's edits in delegated phases.
- [Several chats in one project](multiple-chats.md): which rules apply in other chats.
