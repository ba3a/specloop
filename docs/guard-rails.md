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

## Protected quality-gate files

A gate's `protect` list names files and directories the run may not change (see [Protected files](quality-gates.md#protected-files)). While the owner's run is `active`, in every phase, PreToolUse denies `Edit`, `Write`, `MultiEdit` and `NotebookEdit` on any path named by the `protect` list of any gate: the file itself, or anything under a protected directory. The reason names `/sl-quality accept`, the command with which a person allows a change. Nothing is denied when no run is active (paused, done or not started), and a chat that does not own the run is not restricted. A protect entry that names the project root (`.`) protects nothing, and a malformed `quality` block never makes the hook fail.

This is early feedback only. The real protection is the fingerprint check before each gate; see [Why PreToolUse is only early feedback](limitations.md#pretooluse-is-only-early-feedback).

## Commands only the user may run

While a run is `active`, in any chat, PreToolUse denies Bash commands that contain `run.sh advance` or `run.sh quality accept` (whatever the whitespace between the words). The reason says that only the user may run them and names `/sl-advance` and `/sl-quality accept`. Other forms, such as `run.sh quality run` and `run.sh status`, stay allowed. The `advance` command template keeps `disable-model-invocation: true`, so Claude cannot invoke it as a slash command either.

A command template's own `!` line is not a tool call: Claude Code expands it when the user types the command, without calling PreToolUse, so `/sl-advance` and `/sl-quality accept` still work for a person. Unit tests cover the engine side. The Claude Code side needs a signed-in Claude Code CLI and is not tested automatically. To check it by hand in a sandbox project with specloop installed: start a run with `/sl-go`, then type `/sl-advance`. The phase advances, and the denial message never appears. Asking Claude to run `sh .claude/specloop/run.sh advance` itself is denied.

## Steps file protection

On every Stop during a step, before the phase check runs, the engine compares `run/steps.json` with its approved copy (`approvedSteps` in state). Changing a `done` or `empty` step, any step's `status` or `commit`, removing or reordering finished steps, or adding a non-pending step blocks the stop with the errors and keeps the phase; a missing or unparsable file is blocked too. Other edits (for example a pending step's description) pass the check but are not adopted: only the engine updates `approvedSteps`, and it rewrites `run/steps.json` from it at the next commit. Planning and plan approval are not affected: any `steps.json` that passes validation at approval becomes `approvedSteps`.

## See also

- [Feature mode](feature-mode.md): the run these rules protect.
- [Replanning](replanning.md): how the approved steps change legitimately.
- [Delegating a phase](delegation.md): `feature.enforceDelegation` denies the main session's edits in delegated phases.
- [Several chats in one project](multiple-chats.md): which rules apply in other chats.
