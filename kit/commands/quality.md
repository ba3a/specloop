---
description: "specloop: list the quality gates and set them up"
argument-hint: [run [gate...]|init [preset]|accept]
disable-model-invocation: true
allowed-tools: Bash(sh .claude/specloop/run.sh:*)
---
!`sh .claude/specloop/run.sh quality "$ARGUMENTS"`

Show the output above as it is, then summarize in one line what needs the user's attention, if anything. Do not run any other command or change any file. The forms of /{{prefix}}quality are run, init and accept.
