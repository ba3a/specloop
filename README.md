# specloop

A spec-driven **plan → implement → review** loop for [Claude Code](https://claude.com/claude-code), built only from hooks and slash commands. You hand it a specification, and Claude is walked through the phases; a phase only completes when its check passes. It runs inside your normal Claude Code session, so it uses your subscription: no API keys, no separate orchestrator.

```
/sl-spec specs/login.md      load the spec (Claude summarizes it, nothing else)
/sl-go                       start: plan -> implement -> review -> done
```

## How it works

- **Phases.** `plan` writes a plan file, `implement` makes your test command pass, `review` approves the diff or sends it back to `implement`.
- **Gates.** Each time Claude tries to stop, the Stop hook runs the phase's check. If it fails, Claude gets the failure and keeps working; if it passes, the next phase starts.
- **Guard rails.** Hooks restrict edits per phase and block `git push` and pull requests until the work is done.
- **Single or feature mode.** `/sl-spec` gives one reviewable diff; `/sl-feature` splits a big spec into steps, each reviewed and committed on its own branch.

## Quick start

Requires Claude Code, Python 3.8+, `sh` (Git Bash on Windows) and git.

```bash
git clone https://github.com/ba3a/specloop.git ~/tools/specloop
~/tools/specloop/install.sh ~/code/my-project
```

On Windows use `install.cmd C:\code\my-project`. Then start a **new** Claude Code session in the project, load a spec with `/sl-spec <file>` and run `/sl-go`. Options, updating and uninstalling are in [Installation](docs/installation.md).

## Documentation

- [Installation](docs/installation.md): options, Windows, updating, uninstalling.
- [How it works](docs/how-it-works.md): phases, hooks, safety valves and files.
- [Feature mode](docs/feature-mode.md): big specs, one commit per step.
- [Commands](docs/commands.md): every slash command.
- [Configuration](docs/configuration.md): config files, keys and environment variables.

The full index is in [docs/README.md](docs/README.md).

## More

[Changelog](CHANGELOG.md) · [License](LICENSE): 0BSD, do anything with it, no attribution required, no warranty.
