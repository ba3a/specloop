---
description: "specloop: list the quality gates and set them up"
argument-hint: [run [gate...]|init [preset]|accept]
disable-model-invocation: true
allowed-tools: Bash(sh .claude/specloop/run.sh:*)
---
!`sh .claude/specloop/run.sh quality "$ARGUMENTS"`

Show the output above as it is, then summarize in one line what needs the user's attention, if anything. Do not run any other command or change any file, except as the next paragraph says. The forms of /{{prefix}}quality are run, init and accept.

`quality run` may outlast the time limit of a `!` line, so when the arguments start with `run`, do not rely on the output above. Run `sh .claude/specloop/run.sh quality run` with the same gate names through the Bash tool instead, in the foreground, with a `timeout` long enough for the gates (up to 600000 ms), and show that output.
