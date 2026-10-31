[Documentation](README.md) › Size warnings

# Large specs and plans

A single plan → implement → review cycle works best for changes that fit in one commit. specloop warns when a spec or plan looks bigger than that, and recommends `/sl-feature` ([feature mode](feature-mode.md), which splits work into multiple commits) instead.

| Key | Default | Check |
|-----|---------|-------|
| `largeSpecWarning.maxLines` | `200` | spec has more lines than this |
| `largeSpecWarning.maxWords` | `2000` | spec has more words than this |
| `largeSpecWarning.maxPlanItems` | `12` | `run/plan.md` has more checklist items (`- [ ]` / `- [x]`) than this |

- **Spec size.** `/sl-spec` prints a warning with the spec's line and word counts and the limits. The first `/sl-go` of that run does not start; it repeats the warning. Running `/sl-go` again starts in single mode anyway (state records `largeSpecAcknowledged`). Resuming a run that has already started never warns.
- **Plan size.** When the `plan` phase passes with too many checklist items, the workflow pauses before `implement` instead of continuing. Either run `/sl-reset` and then `/sl-feature`, or run `/sl-go` to continue in `implement` (state records `largePlanAcknowledged`).

Set a key to `null` to disable that check, or the whole block to disable all of them:

```json
{ "largeSpecWarning": { "maxPlanItems": null } }
```

## See also

- [Feature mode](feature-mode.md): the alternative for big specs.
- [Commands](commands.md): `/sl-spec`, `/sl-feature` and `/sl-go`.
- [Configuration](configuration.md): where the `largeSpecWarning` block is set.
