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
| `quality.timeoutSec` | `1800` | Time limit for all gates of one stop together. |
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

## Changing gates in config.local.json

`gates` is an object, not a list, so the deep merge of [`config.local.json`](configuration.md#personal-configuration-configlocaljson) works per gate and per key. Set one key to change it, or set a gate to `null` to remove it for yourself:

```json
{ "quality": { "gates": { "tests": { "timeoutSec": 3000 }, "lint": null } } }
```

A gate that only you define is added after the shared ones.

## Validation

`/sl-go` checks the merged `quality` block before it starts or resumes a run. It reports every problem it finds and changes nothing until they are fixed: an unknown key or a wrong type, an unknown `steps`, `level` or `legacy` value, an invalid gate name or regular expression, a phase in `phases` that does not exist or sets `allowEdits`, and a project that is not a git work tree while a gate uses `{changed_files}`, `{changed_files_file}` or `forbid`.

## See also

- [Configuration](configuration.md): the configuration files, their layering and `checkTimeoutSec`.
- [Phases](phases.md): the phase graph and the `check` that runs before the gates.
