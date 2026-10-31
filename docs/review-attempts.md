[Documentation](README.md) › Review attempts

# Review attempts

A review belongs to one attempt, so a result that arrives late never counts for the wrong step. `attempt` in the state goes up each time a phase with a `verdict` check is entered and on every takeover (see [Several chats in one project](multiple-chats.md#takeover)). A failed check or a pause and resume keeps it, so a subagent that is still running can finish.

`{attempt_dir}` is `.claude/specloop/run/attempts/<step id or "single">-<attempt>`. The default `check.path` of both review phases is `{attempt_dir}/review.md`, and a replan request goes to `{attempt_dir}/replan.md` (see [Replanning](replanning.md)). A rejected attempt's review stays in its directory, and the phase brief lists the reviews of the current step that a gate used, plus the open attempt's file. `/sl-advance` past a review counts as using it.

A file written into a closed attempt never affects the workflow and stays out of the brief, and `/sl-status` lists it, for example `late review for step 5 (attempt 1): CHANGES_REQUESTED, <path>`. In single mode, where `/sl-status` prints JSON, these lines are in a `lateReviews` key. A commit is never undone automatically; you decide what to do about a late rejection.

`check.path` stays configurable (see [Phases](phases.md#check-types)). With a fixed path that does not use `{attempt_dir}`, the shipped review instructions use that path through `{review_file}`. A rejected review is renamed to `<name>-<iteration>` as before. When the phase starts, a review a gate already used moves back to its own attempt, and any other file already there moves to `{attempt_dir}/late-review.md`.

## See also

- [How it works](how-it-works.md): the phases and the gates that read reviews.
- [Phases](phases.md): check types and the `{attempt_dir}` and `{review_file}` placeholders.
- [Several chats in one project](multiple-chats.md): takeovers open a new attempt.
