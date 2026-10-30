---
description: "specloop: load a specification for spec-driven development"
argument-hint: <path-to-spec.md>
disable-model-invocation: true
allowed-tools: Bash(sh .claude/specloop/run.sh:*)
---
!`sh .claude/specloop/run.sh init "$ARGUMENTS"`

Read the specification file named above and summarize it in a few lines: goal, acceptance criteria, anything ambiguous. Do not plan or implement yet; development starts when the user runs /{{prefix}}go.
