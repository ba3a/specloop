## Orchestrator: this phase is delegated to a subagent

Start exactly one subagent with the Agent tool, `subagent_type` `{subagent}`. Pass it the phase brief below word for word as its prompt, from the `--- BEGIN PHASE BRIEF ---` line to the `--- END PHASE BRIEF ---` line. Do nothing else yourself: do not read source files, edit files or run checks.

If a subagent for this phase is already running, including one the Agent tool reports as running in the background, finish your turn and wait for it. Never start a second one.

When the subagent returns, finish with its summary.

--- BEGIN PHASE BRIEF ---
Run long commands in the foreground, and do not end your turn while you wait on background work: specloop cannot see a subagent that has stopped to wait, and it would check the phase before you finish.

{brief}
--- END PHASE BRIEF ---
