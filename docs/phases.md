[Documentation](README.md) › Phases

# Phases

The single-mode workflow is a graph of phases in the `phases` key of the [configuration](configuration.md); the shipped graph is `plan` → `implement` → `review` (see [How it works](how-it-works.md#phases)). [Feature mode](feature-mode.md) has its own graphs in `feature.planPhases` and `feature.stepPhases`, with the same keys.

## The phase graph

Each phase has `next` (a phase name or `done`), an optional `allowEdits` list of path prefixes (omit it to allow all edits), an optional `instructions` file, an optional `delegate` flag (see [Delegating a phase](delegation.md)) and an optional `check`.

## Check types

| `check.type` | Fields | Passes when |
|--------------|--------|-------------|
| `file` | `path` | the file exists and is non-empty |
| `command` | `command` | the command exits 0 (run with `shell`, default `bash`, from the project root) |
| `verdict` | `path`, `approve`, `reject`, `onReject` | the file contains `approve`. If it contains `reject`, the flow jumps to `onReject`. |
| `steps` | `path`, `requireAllPending` | `steps.json` parses and matches the schema (feature mode planning; see [Feature mode](feature-mode.md#planning)). With `requireAllPending` every step must be `pending`. |

A check's `path` may use `{attempt_dir}`; see [Review attempts](review-attempts.md). A phase with no check passes immediately.

## Quality gates after the check

When the check passes, the [quality gates](quality-gates.md#running-gates) that name the phase in their `phases` list run, in declared order. The phase passes only if every `block` gate passes; otherwise the stop is blocked as for a failed check, with every failed gate in one message. A failed check ends the stop before any gate runs, and planning phases never run gates.

## Adding and removing phases

To remove a phase, set it to `null` and re-point the previous phase's `next`. For example, to skip review:

```json
{ "phases": { "implement": { "next": "done" }, "review": null } }
```

To add a phase, for example `docs` after `implement`, define it in `config.json`, point `implement.next` at it, and put the instructions in `overrides/docs.md`.

## Instruction files

Shipped instructions are in `phases/` and are replaced on update; a file of the same name in `overrides/` takes precedence. The shipped files are `plan.md`, `implement.md` and `review.md` (single mode), `feature-plan.md`, `feature-replan.md`, `step-implement.md` and `step-review.md` (feature mode, see [Feature mode](feature-mode.md#step-cycle)) and `delegate.md` (the orchestration preamble, see [Delegating a phase](delegation.md#the-orchestrator-and-the-phase-brief)).

In instruction files, `{spec}` becomes the spec path, `{run_dir}` becomes `.claude/specloop/run`, `{attempt_dir}` becomes the current attempt's directory, and `{review_file}` becomes the path the current phase's verdict check reads, which is `{attempt_dir}/review.md` in a phase without one. `{quality_gates}` becomes a Markdown list of the [quality gates](quality-gates.md#what-the-agent-and-the-reviewer-see) that run in the current phase and step, with the protected paths and forbidden patterns, and nothing when no gate applies; the shipped `implement.md` and `step-implement.md` use it, and an override without it keeps working. Feature-mode step placeholders are listed under [Step cycle](feature-mode.md#step-cycle).

## `run/last-failure.md`

Whenever a check or a quality gate fails or a review requests changes, the full details (the complete check output, every failed gate's output or the review text, not truncated) are written to `.claude/specloop/run/last-failure.md`. The file is removed when a phase passes and on `/sl-advance`.

## See also

- [How it works](how-it-works.md): the shipped phases and what the hooks do with them.
- [Review attempts](review-attempts.md): `{attempt_dir}`, `{review_file}` and late reviews.
- [Delegating a phase](delegation.md): the `delegate` flag and the phase brief.
- [Feature mode](feature-mode.md): the step phases and the `commit` pseudo-phase.
- [Quality gates](quality-gates.md): named checks that a phase runs after its `check`.
