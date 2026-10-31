[Documentation](README.md) › Feature mode

# Feature mode

For features too big for one plan → implement → review cycle, load the spec with `/sl-feature` instead of `/sl-spec`. A planning subagent splits the spec into small ordered steps. Each step then runs through its own implement → review cycle and ends in one commit on a dedicated branch.

```
/sl-feature specs/big-feature.md   load the spec in feature mode (Claude summarizes it)
/sl-go                             create the feature branch, run the planning phase, pause for approval
/sl-go                             approve the plan; step 1: implement -> review -> (rework ...) -> commit
                                   step 2: ... until the last step is committed -> done
```

End to end: `/sl-feature` loads the spec → the first `/sl-go` creates the branch and a planning subagent writes `run/plan.md` and `run/steps.json` → you review and edit the plan, and `/sl-go` approves it → every step runs implement → review in fresh subagents, and the engine commits it → after the last step the run is `done` and you get a list of the commits to review and open a pull request from. `/sl-status` shows the progress at any time.

## Single mode or feature mode?

| | Single mode (`/sl-spec`) | Feature mode (`/sl-feature`) |
|-|--------------------------|------------------------------|
| Good for | A change that fits one reviewable commit | A feature that needs several commits |
| Result | One uncommitted diff for you to commit | A branch with one commit per step |
| Plan | `run/plan.md` (free text) | `run/plan.md` plus `run/steps.json`, approved by you |
| Review | Once, over the whole change | Per step, over that step's diff |
| Context | One session does everything (delegation optional) | Each phase runs in a fresh subagent by default |
| Git | Untouched; you commit | Needs a clean git work tree; the engine owns the branch and commits |

The [size warnings](size-warnings.md) suggest feature mode when a single-mode spec or plan gets large.

## Branch

The first `/sl-go` of a feature run creates and checks out `feature.branchPrefix` + a slug of the spec's first `# ` heading (or its file name): lowercase ASCII, `-` separators, at most 50 characters. If that branch exists, `-2`, `-3`, … is appended. It refuses, without changing anything, when the directory is not a git repository, the repository has no commits, `HEAD` is detached, or tracked files have uncommitted changes (staged or not; changes under `.claude/specloop/` are ignored). State records `branch`, `baseRef` (the starting commit) and `preexistingUntracked` (untracked files that existed at start). Later `/sl-go` calls refuse if the current branch is not the run's branch.

## Planning

The `feature-plan` phase is delegated to a subagent (see [Delegating a phase](delegation.md)). It writes `run/plan.md` (goal, acceptance criteria, assumptions, step-order rationale) and `run/steps.json`:

```json
{
  "feature": "Short feature title",
  "steps": [
    { "id": 1, "title": "At most 72 characters", "type": "feat", "description": "What and why.",
      "acceptance": ["Observable criterion"], "files": ["src/x.py"], "status": "pending", "commit": null }
  ]
}
```

Edits are restricted to `.claude/specloop/run/`. The gate passes when `steps.json` parses, matches the schema and every step is `pending`; otherwise the errors are written to `run/last-failure.md` (see [Phases](phases.md#runlast-failuremd)) and the subagent keeps working.

## Approval

When the plan passes its gate, the run pauses. Review or edit `run/plan.md` and `run/steps.json`, then run `/sl-go`: it validates `steps.json` again (refusing with the errors if it is invalid), stores the approved copy in state as `approvedSteps`, and starts step 1. The engine then guards that copy: see [Steps file protection](guard-rails.md#steps-file-protection).

## Step cycle

Each step runs through `feature.stepPhases`, starting at `feature.stepInitialPhase`. The default cycle mirrors single mode: `implement` (delegated, no edit restriction, checked with your test command from `phases.implement.check`), then `review` (delegated, edits limited to `run/`, verdict on `{attempt_dir}/review.md`; `CHANGES_REQUESTED` goes back to `implement`). A step phase without a `check` reuses the check of the top-level phase with the same name. You can add phases (for example a per-step `lint`) or remove one with `null`, as in the top-level graph. Instructions come from `phases/step-<phase>.md` (`step-implement.md`, `step-review.md`), overridable in `overrides/`, with these placeholders on top of `{spec}` and `{run_dir}`: `{step_index}` (1-based), `{step_count}`, `{step_id}`, `{step_title}`, `{step_description}`, `{step_acceptance}` (a bullet list) and `{steps_file}`. `maxIterations` applies per step.

When a step starts, the engine marks it `active`, resets the iteration counter, and moves the previous step's `run/last-failure.md` (and any `run/review*.md` from a fixed `check.path`) into `run/steps/<id>/`. Reviews stay in `run/attempts/` (see [Review attempts](review-attempts.md)). Status and commit changes are written both to `run/steps.json` and to `approvedSteps`.

A step can reveal that the rest of the plan is wrong; see [Replanning](replanning.md).

## Commits

`commit` is a reserved pseudo-phase at the end of the step cycle (the default `review.next`). When a step reaches it, the Stop hook:

1. runs `git add -A`, then unstages everything under `feature.commitExclude` and every `preexistingUntracked` path;
2. if nothing is staged, marks the step `empty` (no commit, a warning in history) and moves on;
3. otherwise commits with `feature.commitMessage` (never `--no-verify`), stores the SHA in the step's `commit`, marks it `done`, and starts the next step.

If the commit fails (for example a pre-commit hook rejects it), the step goes back to `feature.stepInitialPhase` with the output in `run/last-failure.md` and in the stop reason; the attempt counts as an iteration. `/sl-advance` force-passes the current step phase; advancing out of the last one commits the step rather than skipping it. With `feature.pauseBetweenSteps`, the run pauses after each commit until `/sl-go`.

## Completion

After the last step, the run is `done` and the final message lists every step as `<short sha> <title>` (or `(empty)`) with the branch name. Review the branch and open a pull request yourself: specloop never pushes, and `/sl-reset` never deletes the branch or its commits.

## Context and status

In feature mode the context header shows progress, for example `[specloop] feature step 3/7 "Add schema validation", phase=review, iteration 2, branch=specloop/big-feature`, or `[specloop] feature planning, phase=feature-plan, iteration 1, branch=…` while planning. `/sl-status` prints a summary instead of raw JSON:

```
[specloop] feature run, status=active
spec: specs/big-feature.md
branch: specloop/big-feature (base 1a2b3c4)
phase: review, iteration 2
owner: 3f2a9c1e (since 2026-10-30T07:03:47+00:00)
steps (1/3 done):
  1  done     5d6e7f8  Add schema validation
  2  active   -        Wire the check
  3  pending  -        Document it
```

The single-mode size warnings do not apply in feature mode.

## Configuration keys

The `feature` block of the [configuration](configuration.md). The keys for replanning, delegation and git guard rails are described on [their own pages](#see-also).

| Key | Default | Meaning |
|-----|---------|---------|
| `feature.branchPrefix` | `"specloop/"` | Prefix of the feature branch name. |
| `feature.planPhase` | `"feature-plan"` | First phase of a feature run. |
| `feature.requirePlanApproval` | `true` | Pause for approval after planning. When `false`, the plan is approved as soon as its gate passes. |
| `feature.gitTimeoutSec` | `60` | Timeout for every git call the engine makes. |
| `feature.planPhases` | `feature-plan` phase | Phase graph for planning, with the same keys as `phases` (`delegate`, `allowEdits`, `check`, `instructions`). |
| `feature.stepInitialPhase` | `"implement"` | First phase of every step. |
| `feature.stepPhases` | `implement` → `review` → `commit` | Phase graph of the step cycle; `commit` is the reserved terminal pseudo-phase. |
| `feature.commitExclude` | `[".claude/specloop/"]` | Paths never included in step commits. |
| `feature.commitMessage` | `"{type}: {step_title}\n\nspecloop step {step_index}/{step_count} ({step_id}) of {spec}"` | Commit message template; `{type}` is the step's `type` (default `feat`). |
| `feature.pauseBetweenSteps` | `false` | Pause after each step's commit. |

## See also

- [Replanning](replanning.md): changing the remaining steps while a run is in progress (`feature.replan`, `feature.replanPhase`, `feature.maxReplans`).
- [Guard rails](guard-rails.md): blocked git commands (`feature.blockedCommands`) and the protection of the steps file.
- [Delegating a phase](delegation.md): why feature phases run in subagents (`feature.enforceDelegation`).
- [Size warnings](size-warnings.md): when to choose feature mode.
- [Commands](commands.md): `/sl-feature`, `/sl-go` and `/sl-status`.
