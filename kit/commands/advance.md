---
description: "specloop: force-pass the current phase"
disable-model-invocation: true
allowed-tools: Bash(sh .claude/specloop/run.sh:*)
---
!`sh .claude/specloop/run.sh advance`

Acknowledge in one line. If the workflow is still active, continue with the new phase.
