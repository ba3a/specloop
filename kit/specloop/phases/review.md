## Phase: review

Review the uncommitted changes (`git diff` and untracked files) against the specification at `{spec}` and `{run_dir}/plan.md`. Prefer delegating the review to a subagent so it is done with fresh eyes. Do not modify source code in this phase.

Check that:
- every acceptance criterion is implemented and covered by a test;
- there are no correctness bugs, unhandled edge cases, or leftover debug code;
- the code follows the project's conventions (CLAUDE.md, linters, existing patterns).

Write `{run_dir}/review.md` with the findings (list each required change), and end it with exactly one verdict line:
- `VERDICT: APPROVE` if the change is ready, or
- `VERDICT: CHANGES_REQUESTED` if anything must be fixed.

Put the verdict alone on the last line; a marker inside a sentence or a code block does not count.

Then finish your turn.
