[Documentation](README.md) › Hacks and limitations

# Hacks and limitations

## Delegation is instruction-only and not guaranteed

Hooks cannot start subagents, so `delegate: true` only *tells* the main session to hand the phase to a subagent (see [Delegating a phase](delegation.md)). It may do the work itself anyway, for example after context compaction or when it judges the phase to be small. The gates still protect the result either way; only the fresh-context benefit is lost.

- *How to notice:* in a delegated phase, the main session reads source files, edits files or runs the tests itself instead of starting one subagent.
- *How to recover:* run `/sl-pause`, then `/clear`, then `/sl-go`. `/sl-go` re-sends the orchestrator instructions with the phase brief to a clean context.

## `feature.enforceDelegation` can block, not force

Claude Code adds `agent_id` (and `agent_type`) to hook input when a tool call comes from inside a subagent, and leaves them out for the main conversation ([hooks reference](https://code.claude.com/docs/en/hooks), "Common input fields"). With `enforceDelegation: true`, PreToolUse uses that to deny the main session's `Edit`, `Write`, `MultiEdit` and `NotebookEdit` calls outside `.claude/specloop/run/` in any delegated phase, and tells it to start a subagent. Subagent calls are never affected. Limits:

- it cannot make the main session start a subagent; it only stops it from editing source files itself;
- read-only phases such as review (and planning), which already edit only `run/`, cannot be forced into a subagent at all: the main session can still read the code and write the review itself;
- edits made through Bash (for example `sed -i` or `>` redirects) are not covered;
- `agent_type` alone is not trusted, because it is also present in the main conversation of a session started with `--agent`; in-process teammates carry an `agent_id` and are treated like subagents.

## See also

- [Delegating a phase](delegation.md): the key row for `feature.enforceDelegation` and the phase brief.
- [Guard rails](guard-rails.md): the other PreToolUse rules.
- [Several chats in one project](multiple-chats.md#limits): limits of running several chats in one checkout.
