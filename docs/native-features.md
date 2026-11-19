[Documentation](README.md) › Native Claude Code features

# Native Claude Code features

Claude Code has features that look like parts of specloop. This page lists each one, what specloop uses it for and why it does not replace the engine. The engine's work is Stop-hook gates that run real commands, quality gates, `steps.json` protection, commits owned by the engine and state on disk. Claude Code has no counterpart for these.

Checked on 2026-11-19. Claude Code version: 2.1.325. Newer versions can change a limit below; check the [Claude Code documentation](https://code.claude.com/docs/en/) before you rely on a row.

| Feature | What it does | What specloop uses it for | Why it does not replace the engine |
|---------|--------------|---------------------------|------------------------------------|
| `/goal` | A prompt-based Stop hook. A small model judges a condition from the transcript. | Nothing. | The model runs no commands and reads no files, so it cannot run a test or a gate. Idle check-ins are capped, so it does not carry a run over several days. |
| Workflows | Scripted multi-step runs. | Nothing. | A workflow resumes only in the same session or through `--resume`. A fresh session starts over, while specloop keeps its state on disk. |
| Agent teams | Several sessions that work together as teammates. | Nothing. A teammate counts as a running subagent for the Stop hook; see [Gates while a subagent runs](delegation.md#gates-while-a-subagent-runs). | They give no step cycle, no gates and no commits. Two sessions in one checkout have no locking. |
| Deny rules | Permission rules that block a tool call. | Nothing. specloop keeps its own checks in PreToolUse. | They are not a security boundary: `Bash(git push *)` does not match `git -C . push`. They are no better than the substring match in `feature.blockedCommands`; see [Guard rails](guard-rails.md) and [PreToolUse is only early feedback](limitations.md#pretooluse-is-only-early-feedback). |
| Worktrees | `claude -w <name>` creates `.claude/worktrees/<name>` on branch `worktree-<name>`. | `feature.branchMode: "current"` runs a feature on the branch a worktree already has; see [Git worktrees](installation.md#git-worktrees). | A worktree separates checkouts. It does not run phases, gates or commits. specloop does not create worktrees and does not make one the default. |
| Plugins | Ship hooks, skills, agents and commands. They can auto-update. | Nothing. | Commands are namespaced (`/<plugin>:<name>`), so `/sl-*` would change. A plugin cannot write into the project's `.claude/`, so there is no per-project `config.json`. Its data directory is not per project. Auto-update is off for third-party marketplaces. `hooks.json` timeouts are static, so the install-time `stopTimeoutSec` that bounds the quality budget would be lost. |
| Agent definitions | Files in `.claude/agents/` with `tools`, `model` and a system prompt. `claude --agent <name>` makes the main thread that agent. | Yes. specloop ships `specloop-planner`, `specloop-implementer`, `specloop-reviewer` and `specloop-orchestrator`; see [Choosing the subagent](delegation.md#choosing-the-subagent). | They carry the role rules only. They do not decide when a phase starts, ends or passes. The engine does. |

## See also

- [Delegating a phase](delegation.md): the agent definitions specloop ships and how a phase picks one.
- [Hacks and limitations](limitations.md): where specloop works around Claude Code.
- [How it works](how-it-works.md): the engine, the hooks and the files that stay in a project.
- [Feature mode](feature-mode.md#branch): `feature.branchMode` for runs in a worktree.
