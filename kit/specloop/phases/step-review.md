## Phase: review (step {step_index}/{step_count})

Review **only** the changes of step {step_index} of {step_count} (id {step_id}) of the feature described in `{spec}`:

**{step_title}**

{step_description}

Acceptance criteria for this step:

{step_acceptance}

The step's changes are everything since the previous step's commit: `git diff HEAD` plus new untracked files. Ignore untracked files that existed before the feature started; they are listed under `preexistingUntracked` in `.claude/specloop/state.json`. Do not modify source code in this phase.

Check that:
- every acceptance criterion of this step is implemented and covered by a test;
- the change stays within this step and does not start later steps from `{steps_file}`;
- there are no correctness bugs, unhandled edge cases, or leftover debug code;
- the code follows the project's conventions (CLAUDE.md, linters, existing patterns).

Write `{review_file}` with the findings (list each required change), and end it with exactly one verdict line:
- `VERDICT: APPROVE` if the step is ready to be committed, or
- `VERDICT: CHANGES_REQUESTED` if anything must be fixed.

If the review shows that the remaining plan itself is wrong (not just this step's implementation), write `{attempt_dir}/replan.md` instead of a verdict: explain what is wrong with the remaining plan and propose the change to the pending steps.

Then finish your turn. When the review approves, specloop commits the step and starts the next one.
