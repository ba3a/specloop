# specloop documentation

[Back to the project README](../README.md). Changes between releases are in the [changelog](../CHANGELOG.md).

## Getting started

- [Installation](installation.md): requirements, installing into a project, installer options, updating and uninstalling.

## Concepts

- [How it works](how-it-works.md): the phases, the four hooks, the safety valves and the files specloop keeps in a project.
- [Review attempts](review-attempts.md): how a review is tied to one attempt, and what happens to a late result.

## Features

- [Feature mode](feature-mode.md): split a big spec into steps with one commit each on a dedicated branch.
- [Replanning](replanning.md): change the remaining steps of a feature run when a step shows the plan is wrong.
- [Guard rails](guard-rails.md): blocked commands, per-phase edit restrictions, the feature-mode git guard and steps file protection.
- [Delegating a phase](delegation.md): run a phase in a fresh subagent, the phase brief and `feature.enforceDelegation`.
- [Size warnings](size-warnings.md): the warnings for specs and plans that are too big for single mode.
- [Several chats in one project](multiple-chats.md): who drives the workflow, what other chats see and how to take over.
- [Updates](updates.md): update checks, safe points and the `/sl-update` command.

## Reference

- [Commands](commands.md): every slash command, its arguments and effect.
- [Configuration](configuration.md): configuration files and layering, `config.local.json`, top-level keys and environment variables.
- [Phases](phases.md): the phase graph, check types, adding and removing phases, instruction placeholders and `run/last-failure.md`.
- [Tips](tips.md): practical advice for writing specs and running the loop.
- [Hacks and limitations](limitations.md): what delegation cannot guarantee and where specloop works around Claude Code.
