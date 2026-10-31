[Documentation](README.md) › Tips

# Tips

- Write specs with explicit acceptance criteria. The plan, the tests and the review all key off them.
- If you commit `.claude/settings.json` with `--scope project`, every teammate who opens the repo gets the hooks. Without a loaded spec the hooks do nothing, so that's harmless.
- A long test suite runs on every Stop during `implement`. Point `check.command` at a fast subset if needed.
- Claude can't stop to ask you questions while a phase is active. Use `/sl-pause`, or let it record assumptions in `plan.md`. The plan instructions already ask for that.

## See also

- [Installation](installation.md#installer-options): the `--scope` option.
- [Phases](phases.md#check-types): the `command` check.
- [Commands](commands.md#sl-pause): pausing the workflow.
