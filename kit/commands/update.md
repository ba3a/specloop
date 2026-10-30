---
description: "specloop: check for and apply kit updates"
argument-hint: [check|skip|never]
disable-model-invocation: true
allowed-tools: Bash(sh .claude/specloop/run.sh:*)
---
!`sh .claude/specloop/run.sh update $ARGUMENTS`

Summarize the output above in a few lines: the installed and available versions, what changed or was refused, and what the user should do next. Do not run any other command or change any file. The forms of /{{prefix}}update are check, skip and never.
