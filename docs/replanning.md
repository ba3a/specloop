[Documentation](README.md) › Replanning

# Replanning

A step in [feature mode](feature-mode.md) can reveal that the rest of the plan is wrong. During any step phase Claude may write `{attempt_dir}/replan.md` explaining what is wrong with the remaining plan and proposing a change (the step instructions describe this; see [Review attempts](review-attempts.md) for `{attempt_dir}`). The Stop hook looks for it before running the phase check:

- `feature.replan: "stall"` (default): the run stalls. Read the `replan.md` the message names, optionally edit the pending or active steps in `run/steps.json`, then run `/sl-go`. The engine validates the file (schema plus the [steps file protection](guard-rails.md#steps-file-protection) rules against `approvedSteps`) and refuses with the errors if it fails, changing nothing. Otherwise it stores the new plan, archives the request to `run/replans/replan-N.md`, and restarts the current step at `feature.stepInitialPhase` with the iteration counter reset. If you removed the current step, the next pending step starts instead (or the run completes when none remain).
- `feature.replan: "auto"`: the engine enters `feature.replanPhase` (default `feature-replan`, defined in `feature.planPhases`, delegated, edits limited to `run/`, instructions in `phases/feature-replan.md`). A planning subagent rewrites only pending or active steps. The gate is the `steps` check (without `requireAllPending`) plus the steps file protection rules; when it passes, the engine continues as in the stall case. At most `feature.maxReplans` auto-replans run per run; a request beyond the cap stalls with a message saying so.

The current step's uncommitted work stays in the working tree either way; the engine never stashes, resets or checks out. `/sl-advance` in the replan phase adopts the current `steps.json` if it passes the same checks.

## Configuration keys

| Key | Default | Meaning |
|-----|---------|---------|
| `feature.replan` | `"stall"` | What a `replan.md` request does: `"stall"` for the user, or `"auto"` to run the replan phase. |
| `feature.replanPhase` | `"feature-replan"` | Phase (in `feature.planPhases`) that rewrites the remaining steps in `"auto"` mode. |
| `feature.maxReplans` | `3` | Auto-replans allowed per run; later requests stall. |

## See also

- [Feature mode](feature-mode.md): the step cycle that a replan restarts.
- [Guard rails](guard-rails.md): the rules a replanned `steps.json` must satisfy.
- [Phases](phases.md): the `steps` check type.
