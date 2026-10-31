[Documentation](README.md) › Commands

# Commands

`sl-` is the default prefix of the slash commands. The installer's `--prefix` option sets another one (`--prefix ""` gives plain `/spec`, `/go`, …; see [Installation](installation.md#installer-options)), which is stored as `commandPrefix` in the [configuration](configuration.md). This documentation always writes the default.

## `/sl-spec`

`/sl-spec <file>` loads a spec; status `ready`. Clears the previous run's `run/` artifacts (plan, reviews). Refused while a workflow is `active`, `paused` or `stalled`; run `/sl-reset` first. Claude summarizes the spec and waits. A spec that looks too big prints a [size warning](size-warnings.md).

## `/sl-feature`

`/sl-feature <file>` loads a spec in [feature mode](feature-mode.md) (one commit per planned step). Also clears `run/`, and is refused in the same cases.

## `/sl-go`

Start, or resume after pause/stall. The chat that types it becomes the [owner](multiple-chats.md#who-drives) of the workflow.

## `/sl-pause`

Stop driving the session; keep the phase. Works from any chat. A workflow that has not started or is done stays as it is.

## `/sl-status`

Show the state: the raw state JSON in single mode, a step summary in feature mode (see [Context and status](feature-mode.md#context-and-status)). Works from any chat, and lists reviews that arrived too late to count (see [Review attempts](review-attempts.md)).

## `/sl-advance`

Force-pass the current phase. Blocked in a chat that is not the owner while the workflow is `active`.

## `/sl-reset`

Delete the state and the `run/` artifacts. Blocked in a chat that is not the owner while the workflow is `active`.

## `/sl-update`

`/sl-update [check|skip|never]` checks for a newer kit and applies it, or only reports (`check`), or silences a version (`skip`, `never`). Refused while a workflow is busy. See [Updates](updates.md#the-sl-update-command) for the forms.

## Notes

Claude cannot run `/sl-spec`, `/sl-feature`, `/sl-go`, `/sl-advance`, `/sl-pause`, `/sl-reset` or `/sl-update` itself: their templates set `disable-model-invocation: true`, so taking over a workflow or force-passing a phase is always your decision.

If specloop is busy (another process holds `state.lock`), a command prints one line asking you to retry and changes nothing.

## See also

- [How it works](how-it-works.md): the phases the commands move through.
- [Several chats in one project](multiple-chats.md): which chat may run which command.
- [Updates](updates.md): the forms of `/sl-update`.
- [Configuration](configuration.md): `commandPrefix`.
