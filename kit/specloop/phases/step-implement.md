## Phase: implement (step {step_index}/{step_count})

Implement **only** step {step_index} of {step_count} (id {step_id}) of the feature described in `{spec}`. The full plan is in `{steps_file}` and `{run_dir}/plan.md`; read them for context, but do not start later steps.

**{step_title}**

{step_description}

Acceptance criteria for this step:

{step_acceptance}

- Work test-first: add or update unit tests for this step's acceptance criteria, then make them pass.
- Keep the change limited to this step. After it, the project must build and the test command must pass.
- Do not commit, push, or open pull requests. specloop commits the step itself after the review approves it.
- Do not edit `status` or `commit` in `{steps_file}`; specloop owns those fields.
- If `{run_dir}/last-failure.md` exists, it holds the last failed check, rejected review or failed commit. Address it first. Reviews of earlier attempts at this step are `{run_dir}/attempts/{step_id}-*/review.md`; address every point in them.

If this step shows that the rest of the plan is wrong (for example the step cannot be done as described, or later steps must change first), do not work around it. Write `{attempt_dir}/replan.md` explaining what is wrong with the remaining plan, why the current step cannot be finished as planned, and the change you propose to the pending steps; then finish your turn. Your uncommitted work stays in place.

Finish your turn when you believe the step is complete. The workflow then runs the test command; if it fails, you will receive the output and must fix it.
