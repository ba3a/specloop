[Documentation](README.md) › Configuration

# Configuration

The effective configuration is `defaults.json` (shipped with the kit and replaced on update), deep-merged with the project's `config.json`, then with the person's `config.local.json`. A later file wins key by key, nested keys included. Instruction files can also be overridden per phase in `overrides/`. Who owns which file is listed in [Files and ownership](how-it-works.md#files-and-ownership); everything in `.claude/specloop/` except `config.json` and `overrides/` is git-ignored.

`config.json` example:

```json
{
  "commandPrefix": "sl-",
  "maxIterations": 20,
  "checkTimeoutSec": 1200,
  "blockedCommandsUntilDone": ["git push", "gh pr create", "gh pr merge", "npm publish"],
  "phases": {
    "implement": {
      "check": { "type": "command", "command": "./gradlew test -q && ./gradlew lint -q" }
    }
  }
}
```

## Top-level keys

Nested blocks have one row here and are described on the page of the feature that owns them.

| Key | Default | Meaning |
|-----|---------|---------|
| `commandPrefix` | `"sl-"` | Prefix of the slash commands, as set by the installer's `--prefix`. Used in every message the engine prints. |
| `initialPhase` | `"plan"` | First phase of a single-mode run. |
| `maxIterations` | `15` | Blocked stops per phase (per step in feature mode) before the run goes to `stalled`. |
| `checkTimeoutSec` | `900` | Timeout for a `command` check. |
| `maxOutputChars` | `4000` | Check output and review text are truncated to this many characters in stop messages (`run/last-failure.md` keeps everything). |
| `shell` | `"bash"` | Shell that runs `command` checks (Git Bash on Windows). |
| `blockedCommandsUntilDone` | `git push`, `gh pr create`, `gh pr merge` | Bash commands denied while a run is active, in both modes; see [Guard rails](guard-rails.md#blocked-commands-in-both-modes). |
| `largeSpecWarning` | `maxLines` 200, `maxWords` 2000, `maxPlanItems` 12 | Single-mode size warnings; see [Size warnings](size-warnings.md). |
| `update` | `mode` `"notify"`, `intervalHours` 24, `timeoutSec` 20 | Update checks for the kit; see [Updates](updates.md). |
| `phases` | `plan` → `implement` → `review` | The single-mode phase graph; see [Phases](phases.md). |
| `feature` | see [Feature mode](feature-mode.md#configuration-keys) | Feature-mode settings and phase graphs. |

## Personal configuration: config.local.json

`config.json` is shared with your team through git. For settings that are yours alone, for example how you want updates handled, create `.claude/specloop/config.local.json`. It takes any key that `config.json` takes, with the same structure:

```json
{ "update": { "mode": "auto" }, "maxIterations": 30 }
```

It is merged last, so it wins key by key over `config.json`. A missing file is the same as an empty one. A file that cannot be read or is not valid JSON is ignored, with a warning on stderr.

- It is git-ignored: the managed `.gitignore` block covers everything in `.claude/specloop/` except `config.json` and `overrides/`.
- The installer never creates, changes or removes it. `--force` recreates `config.json` only, and `--uninstall --keep-config` keeps it together with `config.json` and `overrides/`.
- specloop itself writes to it only for `/sl-update never`, which sets `update.mode` to `"off"` and keeps your other keys.

## Environment variables

| Variable | Effect |
|----------|--------|
| `SPECLOOP_DISABLED=1` | All hooks become no-ops. |
| `SPECLOOP_PYTHON=/path/to/python` | Interpreter for the launcher and installer (skips auto-detection). |
| `CI` (any value) | Update checks behave as `off`. Headless runs must not modify themselves. |

Settings that are yours belong in [`config.local.json`](#personal-configuration-configlocaljson), not in environment variables; the variables above describe the environment, not a preference.

## See also

- [How it works](how-it-works.md): the files in `.claude/specloop/` and who owns them.
- [Installation](installation.md): the installer options that create `config.json`.
- [Phases](phases.md): the phase graph that `phases` configures.
- [Updates](updates.md): the `update` block and the `CI` variable.
