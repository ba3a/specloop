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
| `steps` | `"each"` | Feature mode: `"each"` runs on every step, `"last"` only when no step is pending after the current one (see [Feature mode](#feature-mode)). Ignored in single mode. |
| `level` | `"block"` | `"block"` fails the phase; `"warn"` reports and lets it pass. |
| `timeoutSec` | `checkTimeoutSec` | Limit for this gate's command, a positive number of seconds. |
| `hint` | none | Text added to the failure message, for example how to fix a finding properly. |
| `passExitCodes` | `[0]` | Exit codes that count as success. 126 and 127 can never be listed; see [Pass criteria](#pass-criteria). |
| `metric` | none | A threshold on a number the gate reports; see [Pass criteria](#pass-criteria). |
| `changedGlob` | none | List of `fnmatch` patterns that narrow `{changed_files}` and `{changed_files_file}`; see [Changed files](#changed-files). |

## Running gates

On a Stop, after the phase's `check` passes, the engine runs every gate whose `phases` list names the current phase, in declared order. Planning phases (`plan`, `feature-plan`, `feature-replan`) never run gates, and a failed `check` stops the Stop before any gate runs. Each gate runs its `command` with `shell` from the project root, with stderr merged into stdout, for at most its `timeoutSec` and the [time budget](#time-budget) that is left. No lock is held while gates run.

- An exit code listed in `passExitCodes` (by default only `0`) is a pass. Any other exit code fails the gate at level `block` and gives a warning at level `warn`, except 126 and 127, which make it [broken](#broken-gates). A `metric` can still fail a gate that passed by exit code; see [Pass criteria](#pass-criteria).
- With `failFast: false` every gate runs, so all failures are reported at once. With `failFast: true` the first failing `block` gate ends the run: later gates do not run and keep their previous results. A failing `warn` gate never ends the run.
- The phase passes only if every `block` gate passes. Failing `warn` gates are named, with their log paths, in the message that announces the next phase.

## Feature mode

In feature mode the gates run in the `implement` step phase, before the step enters `review`, and a step that passes them is reviewed and committed as usual. A failing `block` gate keeps the step in `implement`; `maxIterations` applies per step. Planning and replanning (`feature-plan`, `feature-replan`) never run gates. See [Feature mode](feature-mode.md#step-cycle).

`steps` chooses the steps a gate runs on:

- `"each"` (the default) runs on every step.
- `"last"` runs only when no step after the current one in the approved plan is still `pending`, so it runs once, on the last step, for example a slow end-to-end suite. A step whose status is not `pending` (`done`, `empty` or `active`) does not count as a later step.

The engine reads the approved plan on each Stop, so a [replan](replanning.md) that adds a step moves a `"last"` gate to the new last step: it does not run on the step that used to be last, and runs on the new one. A replan that removes the steps after the current one does the opposite. `steps` is ignored in single mode, where every gate that names the phase runs.

## Failure reporting

A failed gate run counts as one failed check, however many gates failed: the stop is blocked, the phase stays current, the iteration counter rises by one, and `maxIterations` stalls the run as for any check.

The message starts with one line that names every failed gate, for example `Quality gates failed: lint, tests`, so a [delegated phase](delegation.md), which shows only that line, still names them all. Then it lists each failed gate with its `hint` and the end of its output, followed by failing `warn` gates; the whole message stays within `maxOutputChars`. The complete text, with every gate's full output, goes to `run/last-failure.md` (see [Phases](phases.md#runlast-failuremd)).

Each gate's complete output is written to `.claude/specloop/run/quality/<gate>.log`, replaced each time the gate runs. The last result of each gate is stored in `state.json` under `qualityResults`: its `result` (`pass`, `fail`, `warn`, `skipped` or `broken`, with a `reason` when skipped or broken), `exitCode`, `metric` (the value read, when the gate has a [metric](#pass-criteria)), `durationSec`, the `phase` and `stepId` it ran for, and `finishedAt`. A result is recorded only if the workflow did not change while the gates ran (see [Several chats in one project](multiple-chats.md#state-files)); a dropped result may leave logs on disk, but state stays as it was.

### Results file

After each gate run, `.claude/specloop/run/quality.md` is rewritten from that run's results: a table with one row per gate that ran (name, `result`, duration and the metric value, or `-` when the gate has none), then, for each `warn` result, a section with the end of its output, bounded by `maxOutputChars` across all warnings. The reviewer reads this file. It is written together with `qualityResults`, so a dropped result writes nothing, and a run without gates never creates it.

In a feature-mode run, `run/quality.md` and `run/quality/` move to `run/steps/<id>/` when the step finishes, with the step's review files; the next step starts without them. In single mode they stay in `run/`.

## Pass criteria

By default the exit code decides, so a tool's own threshold works unchanged, for example detekt's `maxIssues`. Two keys refine that:

- `passExitCodes` lists the exit codes that count as success, `[0]` by default. A tool that exits 2 when it finds issues but writes a report can list `[0, 2]`, so that the report, not the exit code, decides. 126 and 127 can never be listed: they stay [broken](#broken-gates).
- `metric` adds a number and its limits. It is read only when the exit code is a pass; an exit code that is not listed fails the gate before any metric is read.

| `metric` key | Meaning |
|--------------|---------|
| `pattern` | Regular expression whose first group is the number. The last match counts, since summaries come last. The text is the gate's output (stdout and stderr), or the file named by `file`. |
| `json` | Dotted key path into a JSON document, for example `summary.total`. A numeric segment indexes a list. The document is the gate's output, or the file named by `file`. |
| `file` | Optional project-relative path (forward slashes, inside the project). Without it the gate's output is read. |
| `min`, `max` | Inclusive limits; at least one is required. |

Exactly one of `pattern` and `json` is required. A value outside the limits fails the gate at the gate's `level`, and the message and `run/quality/<gate>.log` show the value and the limit, for example `metric 79.9 is below the minimum 80`. A metric that cannot be read makes the gate [broken](#broken-gates), because the agent cannot fix a harness that stopped reporting. The value read is stored in the gate's `qualityResults` entry as `metric`.

A Kotlin project can hold coverage and detekt findings like this. `koverLog` prints a summary, so a pattern reads it; detekt exits 2 when it finds issues, and its JSON report decides:

```json
{
  "quality": {
    "gates": {
      "coverage": {
        "command": "./gradlew koverLog",
        "metric": { "pattern": "coverage: ([0-9.]+)%", "min": 80 },
        "hint": "Add tests for the code you changed."
      },
      "issues": {
        "command": "./gradlew detekt",
        "passExitCodes": [0, 2],
        "metric": { "file": "build/reports/detekt/detekt.json", "json": "summary.total", "max": 0 },
        "hint": "Fix the findings; do not suppress them."
      }
    }
  }
}
```

With the `issues` gate the phase cannot be left, in single mode or in a feature step, while the report shows issues: the agent receives the findings and works on them, and review starts once the report shows 0. If detekt does not write the report, the gate is broken and the run pauses.

## Changed files

A command may name the files the run changed, so that a linter checks only those. Two placeholders are replaced before the command runs:

| Placeholder | Becomes |
|-------------|---------|
| `{changed_files}` | The paths separated by spaces, each quoted for the shell (`shlex.quote`), so a name with spaces or quotes stays one argument. |
| `{changed_files_file}` | The project-relative path of `run/quality/<gate>.files`, a file with one path per line, rewritten each time the gate runs. |

Paths are relative to the project root and use forward slashes, also when the project is a subdirectory of its repository. The set is:

- tracked files that differ from the comparison point, staged or not (deleted files are left out), plus untracked files that were not already untracked when the run started (`preexistingUntracked` in `state.json`);
- never anything under `.claude/specloop/`.

The comparison point is `HEAD` for a feature-mode gate with `steps: "each"`, so a step's gate sees what this step changed since the previous step's commit. It is `baseRef`, the commit the run started from, for a gate with `steps: "last"` and in single mode, so these see everything the run changed.

`changedGlob` keeps the paths that match at least one of its patterns. Matching is `fnmatch`, case-sensitive, with `*` also matching `/`: `*.kt` matches `src/Main.kt`, and `src/*` matches everything below `src`.

- When a command uses a placeholder and the set is empty, after `changedGlob`, the gate is `skipped`: its command does not run, `qualityResults` shows `skipped` with the reason, `run/quality.md` shows the result `skipped`, and it never blocks the phase. A command with neither placeholder is not affected by `changedGlob` and always runs.
- `{changed_files}` is limited to 8000 characters, because command lines are short on Windows. A longer list makes the gate [broken](#broken-gates) with a message that points to `{changed_files_file}`, which has no limit: `ruff check $(cat {changed_files_file})`, or the tool's own option to read a list from a file.
- A gate is also broken when git cannot answer, for example when `baseRef` is no longer a commit.

Single mode records `baseRef` (`HEAD`) and `preexistingUntracked` on the first `/sl-go` of a run in a git repository. It does not require a clean tree, so single mode starts on a dirty tree as before. The limit: a tracked file that was already modified at that moment counts as changed, because the comparison is with the commit, not with the tree as it was. Outside a git repository, or in one with no commits, nothing is recorded and the run starts as before. Outside git, [validation](#validation) rejects a gate that uses a placeholder; in a repository with no commits the comparison point is `HEAD`, and everything staged or untracked counts as changed.

## Broken gates

A gate is `broken` when it cannot give an answer, so neither the agent nor a retry of the same code can fix it:

- its command exits with 126 or 127, the shell's codes for "not executable" and "not found", for example a tool that is not installed;
- it runs longer than its `timeoutSec` and is killed with every process it started;
- its command cannot be started at all, for example because the `shell` does not exist;
- its [metric](#pass-criteria) cannot be read: the file is missing, the pattern matches nothing, the document is not valid JSON, the key is missing or the value is not a number;
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
- [Feature mode](feature-mode.md): the step cycle that runs the gates before review.
- [Replanning](replanning.md): how a replan changes which step is the last.
