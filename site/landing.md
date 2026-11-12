---
layout:
  visual:
    hero: pre
    problem: blockquote
    timeline: ul
    spec-example: pre
    quick-start: pre
  dogfood: gitlog
  when: time
  brand: 4
  strings:
    terminal_title: "claude — my-project"
    hero_terminal: A terminal session in Claude Code, running a feature from a spec to reviewed commits.
    dogfood_terminal: The git log of a feature that specloop built, one commit per step.
    chat: A typical chat without specloop, in which the person keeps asking Claude to continue.
accent:
  light:
    accent: "#c2521c"
    accent-text: "#ae4716"
    accent-soft: "#f6e3d6"
  dark:
    accent: "#f08a50"
    accent-text: "#f08a50"
    accent-soft: "#2e1f17"
---

<div class="section-marker" id="hero"></div>

###### For Claude Code

# Write the spec. Go make tea. Come back to reviewed commits.

specloop turns Claude Code into a spec-driven **plan → implement → review** loop. Claude cannot say "done" until the tests pass and a reviewer approves the diff. You stop babysitting the chat.

[Get it on GitHub →](https://github.com/ba3a/specloop) [Install in 30 seconds](#quick-start)

- Uses your Claude subscription
- No API keys
- macOS · Linux · Windows
- 0BSD licensed

```console
> /sl-feature specs/password-reset.md
[specloop] spec loaded in feature mode. Run /sl-go.
> /sl-go
[specloop] branch specloop/password-reset created
[specloop] planning… 5 steps written to run/steps.json
> /sl-go   # plan looks good, approved

☕  you leave

  1  done     5d6e7f8  Add reset token model
  2  done     a41c09b  Send the reset email
  3  done     e02f7aa  Add the reset endpoint
     ↳ review: CHANGES_REQUESTED (token not single-use)
     ↳ implement → tests green → review: APPROVE
  4  done     9b3d112  Rate-limit reset requests
  5  done     c77e0f4  Document the flow

[specloop] done. 5 commits on specloop/password-reset
```

<div class="section-marker" id="problem"></div>

###### The problem

## Right now, you are the loop

Coding agents are great at the first 80%. Then they stop, declare victory and wait for you. So you sit there, typing the same five messages, for an hour.

The agent is not the bottleneck. The person who has to keep saying "continue" is.

> **Claude:** All done! The feature is implemented. ✅
>
> **You:** did you run the tests?
>
> **Claude:** You're right, 3 tests fail. Fixing now.
>
> **Claude:** Fixed! Everything should work.
>
> **You:** it still fails. also you skipped step 4 of the spec
>
> **Claude:** Apologies! Let me implement step 4…
>
> **You:** continue
>
> **You:** continue

<div class="section-marker" id="how-it-works"></div>

###### How it works

## Every phase ends at a gate. Claude can't talk its way past it.

Each time Claude tries to stop, specloop's Stop hook runs the current phase's check. Fail, and Claude gets the failure back and keeps working. Pass, and the next phase starts on its own.

| Phase | What Claude does | Gate |
|-------|------------------|------|
| **01 · plan** | Reads the spec and writes a plan. Can only edit the plan file, so no "let me just start coding". | The plan file exists |
| **02 · implement** | Implements test-first, free to edit the code. Your linters and coverage can join as quality gates. | Your test command exits 0 |
| **03 · review** | A fresh reviewer reads the diff, read-only, and writes a verdict. Changes requested? Back to implement. | `VERDICT: APPROVE` |

Big spec? `/sl-feature` splits it into steps. Each step runs implement → review in fresh subagents and ends in its own commit on a dedicated branch. If a step shows the plan is wrong, the remaining steps get replanned.

<div class="section-marker" id="timeline"></div>

###### Your afternoon

## Two commands. Then it's tea time.

You do the parts that need a human: deciding what to build and approving the plan. specloop does the grinding in between.

- **14:00** · *you* · Write `specs/password-reset.md`: goal, requirements, acceptance criteria.
- **14:10** · *you* · `/sl-feature`, `/sl-go`. A planner splits it into 5 steps. You skim, approve.
- **14:12** · Step 1: implement, tests go green, reviewer approves, committed.
- **14:40** · Step 3: the reviewer catches a token that can be reused. Back to implement. Fixed, approved, committed.
- **15:20** · Last step committed. The run is `done`.
- **15:25** · *you* · You're back with your tea. One branch, five reviewed commits. You open the PR.

<div class="section-marker" id="spec-example"></div>

###### The input

## A spec is just Markdown

No DSL, no YAML, no ticket system. Say what you want and how you'll know it's done. The clearer the acceptance criteria, the less there is to argue about at review.

Tip: let Claude help you write the spec in a normal chat first. Then hand it to the loop.

```markdown
# Password reset

## Goal
Users who forgot their password can set a
new one through a link sent by email.

## Requirements
1. POST /auth/reset sends a link with a
   single-use token, valid for 30 minutes.
2. At most 3 requests per email per hour.
3. Using the link sets the new password and
   invalidates every other session.

## Acceptance
- A used or expired token returns 410.
- Tests cover the rate limit.
- docs/auth.md describes the flow.
```

<div class="section-marker" id="features"></div>

###### Why you can walk away

## Guard rails, so "unattended" doesn't mean "unsupervised"

- **🚧 Hard gates.** Phases complete on exit codes and verdicts, not on Claude's opinion of its own work.
- **🔍 Independent review.** Review runs in a fresh subagent that never saw the implementation chat. It reads the diff, not the excuses.
- **🧪 Quality gates.** Plug in your linter, tests and coverage as named gates. All failures are reported at once.
- **🔒 Never pushes.** `git push` and PR creation are blocked until the work is done. Opening the PR stays your call.
- **🧾 One commit per step.** Feature mode leaves a clean branch: one reviewed, tested commit per planned step. Easy to read, easy to revert.
- **🛟 Can't run away.** After 15 blocked stops the run stalls and waits for you. A crash in specloop never breaks your session.
- **💸 No extra bill.** It's hooks and slash commands inside your normal Claude Code session. Your subscription, no API keys, no orchestrator.
- **🪶 Tiny and portable.** Pure Python standard library, 3.8+. CLI, desktop app or IDE. macOS, Linux and Windows.

<div class="section-marker" id="dogfood"></div>

###### Dogfooding

## specloop is built by specloop

Since feature mode landed, new specloop features are written as specs and run through specloop itself. The copy installed in the repository plans, implements, reviews and commits the next version of the kit.

- **3** · releases in 3 days: 0.2.0, 0.2.1, 0.3.0
- **14** · commits authored by feature-mode steps for one spec
- **12 → 14** · steps: the run replanned itself midway
- **500+** · unit tests guarding the engine

The real `git log` of the update checker, built from `specs/update-checker.md`:

```console
$ git log --format='%h %s%n    %b'
888ab25 docs: Document updates and config.local.json, release 0.3.0
    specloop step 14/14 (12) of specs/update-checker.md
8eec7a1 feat: Show the update state in /status
    specloop step 13/14 (11) of specs/update-checker.md
ba59562 feat: Apply updates automatically in auto mode at safe points
    specloop step 12/14 (10) of specs/update-checker.md
…
c3d0432 fix: Enforce subprocess timeouts with run_limited
    specloop step 8/14 (14) of specs/update-checker.md
906f9e7 fix: Keep update.json keys and harden the update files
    specloop step 7/14 (13) of specs/update-checker.md
894d8d8 feat: Check for updates and show a notice at SessionStart
    specloop step 6/12 (6) of specs/update-checker.md
…
fae29b7 feat: Add config.local.json as a personal configuration layer
    specloop step 1/12 (1) of specs/update-checker.md
```

Notice the count jump from `6/12` to `7/14`: the reviewer found problems the plan didn't cover, so the run added hardening steps on its own and kept going.

> **And this website, too.** The page you're reading was written as a spec and implemented by specloop. If it has a bug, the reviewer missed it, and we'd love a PR.

<div class="section-marker" id="quick-start"></div>

###### Quick start

## Install into any project

You need Claude Code, Python 3.8+, git and `sh` (Git Bash on Windows). The installer copies the kit into your project's `.claude/` directory and never touches your own settings.

Then start a **new** Claude Code session in the project. Updates are one command: `/sl-update`.

```bash
# once
git clone https://github.com/ba3a/specloop.git ~/tools/specloop
~/tools/specloop/install.sh ~/code/my-project
# Windows: install.cmd C:\code\my-project

# in Claude Code, one reviewable diff
/sl-spec specs/login.md
/sl-go

# or a big feature, one commit per step
/sl-feature specs/password-reset.md
/sl-go        # plan
/sl-go        # approve, then go make tea
```

<div class="section-marker" id="faq"></div>

###### FAQ

## Questions people ask

### Does it cost extra?

No. specloop is hooks and slash commands running inside your normal Claude Code session, so it uses your existing subscription. There is no separate service and no API key.

### Will it push to my repo or open PRs?

Never. Pushing and PR commands are blocked while a run is active, and specloop itself never pushes. You review the branch and open the PR yourself.

### What if it gets stuck in a loop?

After 15 blocked stops (configurable) the run goes to `stalled` and Claude is allowed to stop. `/sl-go` resumes, `/sl-pause` lets you talk to Claude in between, and `/sl-advance` force-passes a phase.

### Can I change the phases?

Yes. The phases are a configurable graph. Add a lint phase, drop review, or write your own phase instructions in `overrides/`. Your config is never overwritten on update.

### Does it work with my stack?

If your tests run from a shell command, yes. specloop doesn't care about the language: the gate is an exit code.

### Windows?

Yes. Hooks run through Git Bash, and there is an `install.cmd` wrapper.

<div class="section-marker" id="final-cta"></div>

## Stop typing "continue"

Write the spec. Run two commands. Put the kettle on.

[★ Star specloop on GitHub](https://github.com/ba3a/specloop) [Read the docs](https://github.com/ba3a/specloop/tree/main/docs)

---

specloop · 0BSD · do anything with it. Not affiliated with Anthropic. Claude Code is a product of Anthropic.
