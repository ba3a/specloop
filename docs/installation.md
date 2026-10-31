[Documentation](README.md) › Installation

# Installation

## Requirements

- Claude Code
- Python 3.8+ (stdlib only)
- `sh`, which on Windows means Git Bash. Claude Code on Windows needs it anyway.
- git (the review phase reads `git diff`)

## Install into a project

Clone once, then install into as many projects as you like:

```bash
git clone https://github.com/ba3a/specloop.git ~/tools/specloop
~/tools/specloop/install.sh ~/code/my-project
```

On Windows (cmd or PowerShell):

```bat
install.cmd C:\code\my-project
```

Then start a **new** Claude Code session in that project. Hooks are loaded at session start.

## What the installer does

- copies the kit into `<project>/.claude/specloop/` (see [Files and ownership](how-it-works.md#files-and-ownership));
- creates `config.json` and tries to detect the test command (Gradle, Maven, npm, pytest, Go, Cargo, .NET);
- installs the slash commands into `.claude/commands/` (see [Commands](commands.md));
- merges the hooks into `.claude/settings.json` and leaves your other settings and hooks alone;
- if the project is in a git work tree and `.gitignore` is writable, keeps a marked `# >>> specloop` block in `.gitignore`. The block ignores the installed kit, the runtime state and the installed slash commands. `config.json` and `overrides/` stay tracked so the team can share them. Re-running the installer rewrites the block, and `--uninstall` removes it.

## Installer options

| Option | Meaning |
|--------|---------|
| `--test-command "<cmd>"` | Command that must pass to leave `implement` (overrides detection). |
| `--scope project\|local` | `project` (default) writes hooks to `.claude/settings.json`, which you commit and share with your team. `local` writes them to `.claude/settings.local.json`, which stays personal. |
| `--prefix <p>` | Slash command prefix, default `sl-`. Use `--prefix ""` for plain `/spec`, `/go`, … |
| `--force` | Recreate `config.json` from scratch. |
| `--no-gitignore` | Don't touch `.gitignore`. |
| `--uninstall [--keep-config]` | Remove specloop: kit, commands, hooks. Other settings are kept. |

## Updating

```bash
cd ~/tools/specloop && git pull
./install.sh ~/code/my-project
```

An update replaces the engine, launcher, `defaults.json` and the shipped `phases/`. Your `config.json`, `config.local.json` and `overrides/` are never touched. Instead of doing this by hand, you can let specloop [check for updates and apply them](updates.md).

An override written for 0.2.0 or earlier may still send a review to `{run_dir}/review.md` or a replan request to `{run_dir}/replan.md`. The gate no longer reads those files, so the phase would never pass. `/sl-go` prints a warning naming each such file in use. Change it to `{review_file}` for a review and `{attempt_dir}/replan.md` for a replan request (see [Review attempts](review-attempts.md)).

## Git worktrees

The kit is git-ignored, so a new `git worktree` does not get it. Run the installer once per worktree. If the project commits its hook settings, a worktree without the kit has the hooks but no `run.sh`: each hook fails with exit code 127, which Claude Code reports as a hook error and otherwise ignores, so nothing is enforced there.

## Uninstall

```bash
~/tools/specloop/install.sh ~/code/my-project --uninstall
```

## See also

- [How it works](how-it-works.md): what the installed files are and who owns them.
- [Configuration](configuration.md): the configuration files the installer creates.
- [Updates](updates.md): let specloop check for and apply updates.
