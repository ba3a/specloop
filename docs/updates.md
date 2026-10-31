[Documentation](README.md) › Updates

# Updates

specloop can tell you when a newer kit is available and, if you ask for it, install it. It checks the clone that ran `install.py` and the remote branch that clone tracks. No repository URL is built in: a fork's clone tracks the fork, so a fork follows its own upstream, not the original repository. Only a higher `VERSION` counts as an update.

## Modes

Set `update.mode`, preferably in your own [`config.local.json`](configuration.md#personal-configuration-configlocaljson):

| `update.mode` | Meaning |
|---------------|---------|
| `off` | No checks, no git calls, no notices. |
| `notify` (default) | Check, then show a notice at a safe point. You run `/sl-update` to apply it. |
| `auto` | Check, then apply the update at a safe point and print one line saying what changed. Falls back to the `notify` notice, with the reason, when an automatic update is not safe. |

The checks are also off when the environment variable `CI` is set or `SPECLOOP_DISABLED=1`, whatever the configuration says.

| Key | Default | Meaning |
|-----|---------|---------|
| `update.mode` | `"notify"` | `off`, `notify` or `auto`, as above. |
| `update.intervalHours` | `24` | Minimum time between network checks. The same version is also announced at most once per interval. |
| `update.timeoutSec` | `20` | Timeout for each git call of a check. A check lock older than twice this is stale and is replaced. |

## How a check works

At every SessionStart specloop compares the `VERSION` of the clone with the installed one (one file read, no network): this finds an update you pulled but did not install into this project. When `intervalHours` have passed since the last network check, it also starts `engine.py update-check` as a detached background process, so SessionStart returns at once. That process takes `update.lock`, runs `git fetch` in the clone with the timeout, and writes the result to `update.json` (the semaphore file: time of the check, installed, clone and remote versions, the remote commit, a skipped version, and `error` if the check failed). A failed check is not retried before the next interval, and its error appears only in `/sl-update` and `/sl-status`, never as a session notice. Both files are runtime state: git-ignored, and kept by `/sl-reset`. The result of a background check is used at the next safe point.

## Safe points

The kit is never changed, and no notice is shown, while a workflow is busy: a state file exists and its status is not `done` (so `active`, `paused`, `stalled` and a feature plan awaiting approval all count). A newer engine may read an old state differently, and in feature mode it would change the rules in the middle of a branch. The safe points are:

1. SessionStart while no workflow is busy. The notice is a message to you and does not enter Claude's context.
2. The completion of a workflow: the notice is added to the final message.
3. `/sl-update`, which you can run whenever no workflow is busy.

A notice looks like `[specloop] 0.4.0 is available (installed 0.3.0, from github.com/you/specloop main). Run /sl-update to update.` and mentions `skip` and `never`.

## Major versions

Major versions are never applied automatically. When the available version has a higher major number than the installed one, `auto` only notifies and says that `/sl-update` is needed. While the major number is `0`, a higher minor number counts as major too (`0.3.0` to `0.4.0`), because semver treats `0.y` as unstable. Updates do not migrate `state.json` or `config.json`.

## The /sl-update command

| Form | What it does |
|------|--------------|
| `/sl-update` | Check now, ignoring `intervalHours`, and apply a newer version. Refused while a workflow is busy; the message names the command that ends or resets it. |
| `/sl-update check` | Check now and report. Changes nothing. |
| `/sl-update skip` | Remember the newest available version as skipped. It is never announced or auto-applied again; a later version is handled as usual. |
| `/sl-update never` | Does what `skip` does and sets `update.mode` to `"off"` in `config.local.json`. The output says how to turn checks back on: set `update.mode` there, or remove the key. |

Applying an update fast-forwards the clone (a fast-forward-only merge of `<remote>/<branch>`), runs the clone's `install.py` for this project with the options recorded at install time, prints the old and new versions and up to 20 `git log` lines, and tells you to start a new session so that changed hooks and commands are loaded. It refuses, and changes nothing, when the clone has uncommitted changes, is not on the recorded branch, or cannot be fast-forwarded (a fork with local commits, for example). It never pulls with a merge or rebase, stashes, or switches branches in the clone.

## Enabling checks on an old install

A kit installed by a version before 0.3.0 does not record where it came from, so it cannot check for updates, and the notice says so. Rerun the installer once from the clone, `install.py <project>`, to record the source. The same limit applies when the clone is not a git work tree (a downloaded zip) or its current branch has no upstream; the installer prints one line saying why.

## See also

- [Installation](installation.md#updating): updating by hand.
- [Configuration](configuration.md): `config.local.json` and the `CI` and `SPECLOOP_DISABLED` variables.
- [Commands](commands.md#sl-update): the command in the list of all commands.
- [How it works](how-it-works.md#files-and-ownership): `update.json`, `update.lock` and `manifest.json`.
