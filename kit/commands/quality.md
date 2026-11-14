---
description: "specloop: list the quality gates and set them up"
argument-hint: [run [gate...]|init [preset]|accept]
disable-model-invocation: true
allowed-tools: Bash(sh .claude/specloop/run.sh:*)
---
!`sh .claude/specloop/run.sh quality "$ARGUMENTS"`

Show the output above as it is, then summarize in one line what needs the user's attention, if anything. Do not run any other command or change any file, except as the next paragraph and the guided setup below say. The forms of /{{prefix}}quality are run, init and accept.

`quality run` may outlast the time limit of a `!` line, so when the arguments start with `run`, do not rely on the output above. Run `sh .claude/specloop/run.sh quality run` with the same gate names through the Bash tool instead, in the foreground, with a `timeout` long enough for the gates (up to 600000 ms), and show that output.

## Guided setup: `init`

When the arguments start with `init`, the output above is a starting point, not the result. If it says nothing was set up because a run is going, stop there. Otherwise set up the `quality` block of this project with the user:

1. Read the project's build files (for example `build.gradle.kts`, `pom.xml`, `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`) and the tool configuration files next to them. If no preset was printed, or the project fits none, build the block from what the project has. Never invent a tool the project does not use.
2. Adapt the preset. Keep only the gates whose tasks and tools exist: for Gradle, check with `./gradlew tasks --all` that a task such as `detekt` exists; drop the gates for tools that are not set up, and add a gate for a tool the project has that the preset lacks. Adjust commands to the project's real setup.
3. Propose `protect` entries (the tool's configuration files and directories that exist) and `forbid` entries (the pattern that suppresses the tool's findings, with a `glob` and a `message`). Forbidding a pattern only helps if the project does not already use it legitimately.
4. Run every gate once with `sh .claude/specloop/run.sh quality run`, through the Bash tool in the foreground with a `timeout` up to 600000 ms. If the project is not clean today, suggest a baseline of the tool itself (for example a detekt baseline file) or `legacy: "ratchet"` on a gate with a `metric`, instead of a gate that can never pass. Fix commands that are `broken` (missing tool, wrong path) before going on.
5. Show the user the proposed block and the results of the run, and ask whether to write it. Write nothing before the user approves.
6. After approval, write the block to `.claude/specloop/config.json` as its `quality` key. Change only that key and keep every other key of the file as it is. Never touch `.claude/specloop/config.local.json`.
7. If the block's `timeoutSec` is above the budget `/{{prefix}}quality` reports as the one in force, say that the install command it prints must be run again, because the Stop hook's timeout is written at install.
