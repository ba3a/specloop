[Documentation](README.md) › Quality gates

# Quality gates

A quality gate is a named command, such as a linter, the test suite or a coverage report, that must pass before the workflow leaves a phase. Gates are declared in the `quality` block of the [configuration](configuration.md) and are held by phases of the [phase graph](phases.md), by default `implement`. A phase's own `check` keeps working and runs first; a gate never replaces it. An empty `gates` block, the default, changes nothing.

```json
{
  "quality": {
    "gates": {
      "lint": { "command": "./gradlew detekt", "hint": "Fix the findings; do not add suppressions." },
      "tests": { "command": "./gradlew test -q", "timeoutSec": 1200 }
    }
  }
}
```

## The quality block

| Key | Default | Meaning |
|-----|---------|---------|
| `quality.timeoutSec` | `1800` | Time limit for all gates of one stop together; see [Time budget](#time-budget). Run the installer again after changing it. |
| `quality.failFast` | `false` | `false` runs every gate, so all failures are reported at once. `true` stops at the first failing `block` gate. |
| `quality.legacy` | `"baseline"` | How existing problems are treated, for gates that do not set `legacy` themselves: `"baseline"` or `"ratchet"`. |
| `quality.gates` | `{}` | The gates, by name, in the order they run. |

Gate names match `[A-Za-z0-9][A-Za-z0-9_-]*`, because they become file names.

## Gate keys

| Key | Default | Meaning |
|-----|---------|---------|
| `command` | required | Run with `shell` from the project root. |
| `phases` | `["implement"]` | Phases whose exit is held by this gate, matched by name in the active graph. Each must exist in `phases` or `feature.stepPhases` and must not set `allowEdits`, because the agent could not fix a failure there. |
| `steps` | `"each"` | Feature mode: `"each"` runs on every step, `"last"` only when no step is pending after the current one. Ignored in single mode. |
| `level` | `"block"` | `"block"` fails the phase; `"warn"` reports and lets it pass. |
| `timeoutSec` | `checkTimeoutSec` | Limit for this gate's command, a positive number of seconds. |
| `hint` | none | Text added to the failure message, for example how to fix a finding properly. |

## Running gates

On a Stop, after the phase's `check` passes, the engine runs every gate whose `phases` list names the current phase, in declared order. Planning phases (`plan`, `feature-plan`, `feature-replan`) never run gates, and a failed `check` stops the Stop before any gate runs. Each gate runs its `command` with `shell` from the project root, with stderr merged into stdout, for at most its `timeoutSec` and the [time budget](#time-budget) that is left. No lock is held while gates run.

- Exit code `0` is a pass. Any other exit code fails the gate at level `block` and gives a warning at level `warn`, except 126 and 127, which make it [broken](#broken-gates).
- With `failFast: false` every gate runs, so all failures are reported at once. With `failFast: true` the first failing `block` gate ends the run: later gates do not run and keep their previous results. A failing `warn` gate never ends the run.
- The phase passes only if every `block` gate passes. Failing `warn` gates are named, with their log paths, in the message that announces the next phase.

## Failure reporting

A failed gate run counts as one failed check, however many gates failed: the stop is blocked, the phase stays current, the iteration counter rises by one, and `maxIterations` stalls the run as for any check.

The message starts with one line that names every failed gate, for example `Quality gates failed: lint, tests`, so a [delegated phase](delegation.md), which shows only that line, still names them all. Then it lists each failed gate with its `hint` and the end of its output, followed by failing `warn` gates; the whole message stays within `maxOutputChars`. The complete text, with every gate's full output, goes to `run/last-failure.md` (see [Phases](phases.md#runlast-failuremd)).

Each gate's complete output is written to `.claude/specloop/run/quality/<gate>.log`, replaced each time the gate runs. The last result of each gate is stored in `state.json` under `qualityResults`: its `result` (`pass`, `fail`, `warn` or `broken`, with a `reason` when broken), `exitCode`, `durationSec`, the `phase` and `stepId` it ran for, and `finishedAt`. A result is recorded only if the workflow did not change while the gates ran (see [Several chats in one project](multiple-chats.md#state-files)); a dropped result may leave logs on disk, but state stays as it was.

## Broken gates

A gate is `broken` when it cannot give an answer, so neither the agent nor a retry of the same code can fix it:

- its command exits with 126 or 127, the shell's codes for "not executable" and "not found", for example a tool that is not installed;
- it runs longer than its `timeoutSec` and is killed with every process it started;
- its command cannot be started at all, for example because the `shell` does not exist;
- its definition is invalid when the Stop hook runs it. `/sl-go` validates the block (see [Validation](#validation)), but `config.json` or `config.local.json` may change afterwards. A gate that is not an object, or whose `phases` is not a list of phase names, counts in every phase that runs gates. An invalid key of the block itself, such as `failFast`, makes every gate of the phase broken, and a `quality` or `gates` value that is not an object pauses the run in every such phase.

Any other exit code is an ordinary failure, even when its cause is a missing plugin or a broken build script: a Gradle wrapper that exits 1 fails the gate, and the agent handles it.

A broken gate, at level `block` or `warn`, pauses the run instead of failing the phase. The status becomes `paused`, `state.json` records the first broken gate and its reason under `pausedFor`, for example `{"gate": "detekt", "reason": "command not found (exit 127)"}`, and the gate's last result is `broken` with the same `reason`. No iteration is used and nothing is written to `run/last-failure.md`. The stop is not blocked: the message is for you, and it names each broken gate with its reason. Invalid definitions are all reported at once and no gate command runs. A gate that breaks while running ends the run of gates; later gates do not run and keep their previous results.

Fix the gate or install its tool, then:

- `/sl-go` resumes the run and clears `pausedFor`. The next stop runs every gate of the phase again.
- `/sl-advance` skips the phase, as it does from any pause, and clears `pausedFor`. The run stays paused until `/sl-go`.

## Time budget

`quality.timeoutSec` is one budget for all gates of a stop together, not a limit per gate. Gates run one after another, and each gets `min(its own timeoutSec, time left)` as its timeout, so a later gate may be killed earlier than its own limit says. A gate that cannot start because the budget is used up is [broken](#broken-gates) and its reason says so. A hung gate is killed with every process it started, and the stop hook returns normally.

The budget has to fit in the stop hook that hosts it. The hook's timeout is written into `settings.json` when specloop is installed, as `checkTimeoutSec + quality.timeoutSec + 60` from the merged defaults and `config.json`, and the same number is recorded in `manifest.json` as `stopTimeoutSec`. The effective budget is the smaller of `quality.timeoutSec` and `stopTimeoutSec - checkTimeoutSec - 60`, so a raised setting cannot outlive the hook. A manifest without `stopTimeoutSec`, written by an older installer, uses the configured value.

After changing `checkTimeoutSec` or `quality.timeoutSec`, run the install command again (`./install.sh <project>` from the clone; `/sl-update` does it too) so that the hook is sized for the new values. Until then the old hook timeout and `stopTimeoutSec` stay in force. `--test-command` is unchanged: it still writes `phases.implement.check`.

A gate mark left by a stop that was killed counts as stale after the same sum, `checkTimeoutSec + quality.timeoutSec + 60` seconds; `/sl-go` clears it at once (see [Several chats in one project](multiple-chats.md#state-files)).

## Changing gates in config.local.json

`gates` is an object, not a list, so the deep merge of [`config.local.json`](configuration.md#personal-configuration-configlocaljson) works per gate and per key. Set one key to change it, or set a gate to `null` to remove it for yourself:

```json
{ "quality": { "gates": { "tests": { "timeoutSec": 3000 }, "lint": null } } }
```

A gate that only you define is added after the shared ones.

## Validation

`/sl-go` checks the merged `quality` block before it starts or resumes a run. It reports every problem it finds and changes nothing until they are fixed. The Stop hook checks the gates it runs again and treats an invalid one as [broken](#broken-gates). The problems are: an unknown key or a wrong type, an unknown `steps`, `level` or `legacy` value, an invalid gate name or regular expression, a phase in `phases` that does not exist or sets `allowEdits`, and a project that is not a git work tree while a gate uses `{changed_files}`, `{changed_files_file}` or `forbid`.

## See also

- [Configuration](configuration.md): the configuration files, their layering and `checkTimeoutSec`.
- [Phases](phases.md): the phase graph and the `check` that runs before the gates.
