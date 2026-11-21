[Documentation](README.md) › Delegating a phase

# Delegating a phase to a subagent

Set `"delegate": true` on any phase (default `false`; no shipped phase enables it) to run that phase in a fresh context:

```json
{ "phases": { "implement": { "delegate": true } } }
```

In [feature mode](feature-mode.md) every phase of the step cycle and of planning is delegated by default.

## The orchestrator and the phase brief

In a delegated phase the main session acts as an orchestrator. Instead of the bare phase instructions, `/sl-go`, the Stop hook and the session context give it an orchestration preamble (`phases/delegate.md`, overridable with `overrides/delegate.md`; `{brief}` marks where the brief goes and `{subagent}` the phase's `subagent`). The preamble tells it to start exactly one subagent with the Agent tool, `subagent_type` set to the phase's `subagent` (see [Choosing the subagent](#choosing-the-subagent)), and a self-contained **phase brief** as its prompt, not to read or edit files itself, to wait for a subagent that is already running instead of starting a second one, and to finish with the subagent's summary. The session context line and the `enforceDelegation` denial name the same `subagent_type`. An override without `{subagent}` keeps working. The brief holds the rendered phase instructions and the files the subagent should read: the spec, `run/plan.md`, the reviews of the current step, and `run/last-failure.md` (see [Phases](phases.md#runlast-failuremd)). When a check fails, the Stop hook's message stays short and points at `run/last-failure.md` instead of including the full output.

Note that a phase named `delegate` would share its instructions file with the preamble.

## Choosing the subagent

A phase's `subagent` names the subagent definition that runs it: a file `.claude/agents/<name>.md` or a built-in agent such as `general-purpose`. The shipped phases default to the [agents the installer adds](how-it-works.md#files-and-ownership):

| Phases | Default `subagent` |
|--------|--------------------|
| `phases.plan`, `feature.planPhases.feature-plan`, `feature.planPhases.feature-replan` | `specloop-planner` |
| `phases.implement`, `feature.stepPhases.implement` | `specloop-implementer` |
| `phases.review`, `feature.stepPhases.review` | `specloop-reviewer` |

A phase you add yourself has no default and uses `general-purpose`. A `config.json` without the key keeps these defaults. To run a phase with your project's own agent, put its definition in `.claude/agents/` and name it:

```json
{ "feature": { "stepPhases": { "implement": { "subagent": "my-implementer" } } } }
```

The name must match `^[A-Za-z0-9][A-Za-z0-9_-]*$`. `/sl-go` refuses to start or resume while any phase in `phases`, `feature.planPhases` or `feature.stepPhases` has another value, lists the errors, and asks you to fix `config.json` or `config.local.json`.

When `/sl-go` starts or resumes a run and a delegated phase of its graphs names a subagent with no file in `.claude/agents/` of the project or in `~/.claude/agents/`, it appends a `[specloop] Note:` naming each missing agent and its phase. The note says the Agent tool will fail for that agent until the installer runs again. It does not stop the run. `general-purpose` is built in and never named, and a phase that is not delegated is not checked.

The role rules (work test-first, no debug code left behind, follow the project's conventions, look for correctness bugs and edge cases) live in the agent bodies in `kit/agents/`, not in the shipped phase files. The phase files keep only what the engine reads or the step needs. A phase that is not delegated therefore no longer carries these rules. The shipped implement instructions and `specloop-implementer` tell the agent to run only targeted tests (the tests that cover its change, plus the linter and type checker) and not the full suite. The [quality gates](quality-gates.md#running-gates) run the full suite when the turn ends and are the guarantee, so the suite does not run twice.

## Gates while a subagent runs

Gates still run only when the main session stops; specloop registers no `SubagentStop` hook. A phase is not checked while its subagent runs: when Stop's `background_tasks` lists a `subagent`, `workflow` or `teammate`, in any phase, the Stop hook does nothing and costs no iteration. The subagent's completion wakes the chat, and the next Stop runs the gate. Shell tasks and monitors do not count, because a dev server must not hold a phase open forever. On a Claude Code version that does not send `background_tasks`, the check runs as usual.

There is one gap. A subagent that ends its turn and waits on its own background work is not listed as a `subagent`: Stop sees only its shell, as `type: shell`, until the work finishes and the subagent resumes. The check therefore runs while that subagent is unfinished, as it would on a version with no `background_tasks`. A failed check costs an iteration, and a passing check can advance the phase before the subagent reports. A subagent stays listed for as long as it keeps working, so the shipped preamble's brief tells it to run long commands in the foreground and not to end its turn while it waits on background work. An override of `delegate.md` should keep that line.

## The orchestrator agent route

Status: **experimental**.

Start the session as the orchestrator:

```bash
claude --agent specloop-orchestrator
```

The main thread then has only `Agent`, `Read`, `Grep` and `Glob`. It cannot edit files, so the tool list enforces delegation: the main thread has no way to do the phase itself. The cost is that the main thread cannot run Bash. Work that needs a shell must go to a subagent.

The route is experimental because the by-hand checks below have not been reported yet. These must be verified:

- hooks still fire in a session started with `--agent`;
- `/sl-go` works in that session;
- `agent_id` is absent from hook input for the main thread and present for subagents;
- `subagent_type` resolves to the specloop agents after a restart;
- the SessionStart hook runs after `/compact`.

Once these pass, this page will name the tested Claude Code version.

Use `feature.enforceDelegation` (next section) for sessions that are not started with `--agent`.

## Enforcing delegation

Delegation is an instruction to the main session, not a guarantee; see [Hacks and limitations](limitations.md#delegation-is-instruction-only-and-not-guaranteed) for how to notice and recover. For a session not started with `--agent`, `feature.enforceDelegation` makes the hook deny the main session's own edits in delegated phases; what it can and cannot do is in [its limitations](limitations.md#featureenforcedelegation-can-block-not-force).

| Key | Default | Meaning |
|-----|---------|---------|
| `feature.enforceDelegation` | `false` | In delegated phases, deny the main session's edit tools outside `run/` and tell it to start a subagent. |

## See also

- [Feature mode](feature-mode.md): the default user of delegated phases.
- [Phases](phases.md): the `delegate` and `subagent` fields in the phase graph and the instruction placeholders.
- [Hacks and limitations](limitations.md): what delegation cannot guarantee.
- [Native Claude Code features](native-features.md): agent definitions, `/goal`, workflows and the limits that keep the engine.
- [Guard rails](guard-rails.md): other PreToolUse rules.
