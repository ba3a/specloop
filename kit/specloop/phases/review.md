## Phase: review

Review the uncommitted changes (`git diff` and untracked files) against the specification at `{spec}` and `{run_dir}/plan.md`. Prefer delegating the review to a subagent so it is done with fresh eyes. Do not modify source code in this phase.

Check that:
- every acceptance criterion is implemented and covered by a test;
- there are no correctness bugs, unhandled edge cases, or leftover debug code;
- the code follows the project's conventions (CLAUDE.md, linters, existing patterns);
- the quality gates hold: if `{run_dir}/quality.md` exists, read it. Judge each warning (is it a real problem that was left unfixed?) and each accepted change (a protected file, the quality configuration or a forbidden line that a person accepted: is it justified by the specification?). A waived gate is a finding unless the specification allows it.

Write `{review_file}` with the findings (list each required change), and end it with exactly one verdict line:
- `VERDICT: APPROVE` if the change is ready, or
- `VERDICT: CHANGES_REQUESTED` if anything must be fixed.

Put the verdict alone on the last line; a marker inside a sentence, a code block or a quotation does not count.

Then finish your turn.
