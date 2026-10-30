## Phase: feature-replan

While working on step {step_index} of {step_count} ("{step_title}"), a replan was requested in `{attempt_dir}/replan.md`. Rewrite the remaining plan for the feature described in `{spec}`. Do not modify source code in this phase; only edit files in `{run_dir}/`. Uncommitted work of the current step stays in the working tree; do not stash, reset or discard it.

1. Read `{attempt_dir}/replan.md`, the specification, `{run_dir}/plan.md` and `{steps_file}`.
2. Edit `{steps_file}`. You may change, add, remove or reorder only steps whose `status` is `pending` or `active`:
   - never change steps that are `done` or `empty`, and keep them in their current order;
   - never change any step's `status` or `commit`; specloop owns those fields;
   - new steps get a new unique `id`, `"status": "pending"` and `"commit": null`;
   - removing the active step is allowed when its work is no longer needed; specloop then starts the next pending step.
3. Update the rationale in `{run_dir}/plan.md` to explain the new step order.
4. Leave `{attempt_dir}/replan.md` in place; specloop archives it when the new plan is adopted.

The planning rules still apply: one coherent commit per step, the project builds and tests pass after every step, each step depends only on earlier steps, steps of about 300 changed lines or fewer, and every acceptance criterion of the spec is covered.

When `{steps_file}` is updated, finish your turn. specloop validates it and checks it against the approved plan; if a rule is broken you get the errors and must fix them. When it passes, the current step restarts with the new plan.
