import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(os.path.dirname(HERE), "kit", "specloop")
sys.path.insert(0, KIT)

import engine as workflow  # noqa: E402

sys.path.insert(0, HERE)
import gitfixture  # noqa: E402


class EngineTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.base = os.path.join(self.root, ".claude", "specloop")
        shutil.copytree(KIT, self.base)
        self.set_test_command("exit 0")
        with open(os.path.join(self.root, "spec.md"), "w") as f:
            f.write("# Spec\nDo the thing.")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def set_config(self, config):
        with open(os.path.join(self.base, "config.json"), "w") as f:
            json.dump(config, f)

    def set_test_command(self, command, max_iterations=15):
        self.set_config({"maxIterations": max_iterations,
                         "phases": {"implement": {"check": {"type": "command", "command": command}}}})

    def write_run(self, name, text):
        with open(os.path.join(self.base, "run", name), "w") as f:
            f.write(text)

    def write_override(self, phase, text):
        os.makedirs(os.path.join(self.base, "overrides"), exist_ok=True)
        with open(os.path.join(self.base, "overrides", phase + ".md"), "w") as f:
            f.write(text)

    def start(self):
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)

    def state(self):
        return workflow.load_state(self.root)

    def test_hooks_are_inert_without_state(self):
        self.assertIsNone(workflow.handle_stop(self.root, {}))
        self.assertIsNone(workflow.handle_pretool(self.root, {"tool_name": "Edit", "tool_input": {"file_path": "x"}}))
        self.assertIsNone(workflow.handle_context(self.root, "SessionStart"))

    def test_ready_state_does_not_block_stop(self):
        workflow.cmd_init(self.root, "spec.md")
        self.assertIsNone(workflow.handle_stop(self.root, {}))
        self.assertIn("/sl-go", workflow.handle_context(self.root, "UserPromptSubmit")
                      ["hookSpecificOutput"]["additionalContext"])

    def test_init_rejects_missing_spec(self):
        self.assertIn("not found", workflow.cmd_init(self.root, "missing.md"))
        self.assertIsNone(self.state())

    def test_plan_blocks_until_plan_file_written(self):
        self.start()
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("plan.md", out["reason"])
        self.assertEqual("plan", self.state()["phase"])

    def test_full_happy_path(self):
        self.start()
        self.write_run("plan.md", "- [ ] step")
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("entering phase 'implement'", out["reason"])

        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'review'", out["reason"])

        self.write_run("review.md", "Looks good.\nVERDICT: APPROVE")
        out = workflow.handle_stop(self.root, {})
        self.assertNotIn("decision", out)
        self.assertEqual("done", self.state()["status"])
        self.assertIsNone(workflow.handle_stop(self.root, {}))

    def test_failing_tests_keep_implement_phase_with_output(self):
        self.set_test_command("echo boom-output; exit 3")
        self.start()
        self.write_run("plan.md", "plan")
        workflow.handle_stop(self.root, {})
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("boom-output", out["reason"])
        self.assertIn("exited with 3", out["reason"])
        self.assertEqual("implement", self.state()["phase"])

    def test_rejected_review_returns_to_implement_and_archives(self):
        self.start()
        self.write_run("plan.md", "plan")
        workflow.handle_stop(self.root, {})
        workflow.handle_stop(self.root, {})
        self.write_run("review.md", "Fix null check.\nVERDICT: CHANGES_REQUESTED")
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("Fix null check", out["reason"])
        self.assertEqual("implement", self.state()["phase"])
        run = os.listdir(os.path.join(self.root, ".claude", "specloop", "run"))
        self.assertNotIn("review.md", run)
        self.assertTrue(any(n.startswith("review-") for n in run))

    def test_review_without_verdict_blocks(self):
        self.start()
        self.write_run("plan.md", "plan")
        workflow.handle_stop(self.root, {})
        workflow.handle_stop(self.root, {})
        self.write_run("review.md", "Hmm.")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("must contain", out["reason"])
        self.assertEqual("review", self.state()["phase"])

    def test_stalls_after_max_iterations_and_go_resumes(self):
        self.set_test_command("exit 0", max_iterations=2)
        self.start()
        self.assertEqual("block", workflow.handle_stop(self.root, {})["decision"])
        out = workflow.handle_stop(self.root, {})
        self.assertIn("stalled", out["systemMessage"])
        self.assertEqual("stalled", self.state()["status"])
        self.assertIsNone(workflow.handle_stop(self.root, {}))
        workflow.cmd_go(self.root)
        self.assertEqual("active", self.state()["status"])
        self.assertEqual(0, self.state()["iter"])

    def test_pause_disables_hooks(self):
        self.start()
        workflow.cmd_pause(self.root)
        self.assertIsNone(workflow.handle_stop(self.root, {}))
        self.assertIsNone(workflow.handle_pretool(self.root, {"tool_name": "Bash", "tool_input": {"command": "git push"}}))

    def test_plan_phase_denies_source_edits_but_allows_run_dir(self):
        self.start()
        src = os.path.join(self.root, "src", "Main.java")
        out = workflow.handle_pretool(self.root, {"tool_name": "Edit", "tool_input": {"file_path": src}})
        self.assertEqual("deny", out["hookSpecificOutput"]["permissionDecision"])
        plan = os.path.join(self.root, ".claude", "specloop", "run", "plan.md")
        self.assertIsNone(workflow.handle_pretool(self.root, {"tool_name": "Write", "tool_input": {"file_path": plan}}))

    def test_implement_phase_allows_source_edits_but_not_state(self):
        self.start()
        workflow.cmd_advance(self.root)
        src = os.path.join(self.root, "src", "Main.java")
        self.assertIsNone(workflow.handle_pretool(self.root, {"tool_name": "Edit", "tool_input": {"file_path": src}}))
        state = os.path.join(self.root, ".claude", "specloop", "state.json")
        out = workflow.handle_pretool(self.root, {"tool_name": "Write", "tool_input": {"file_path": state}})
        self.assertEqual("deny", out["hookSpecificOutput"]["permissionDecision"])

    def test_push_blocked_while_active(self):
        self.start()
        out = workflow.handle_pretool(self.root, {"tool_name": "Bash", "tool_input": {"command": "git add . && git push"}})
        self.assertEqual("deny", out["hookSpecificOutput"]["permissionDecision"])
        self.assertIsNone(workflow.handle_pretool(self.root, {"tool_name": "Bash", "tool_input": {"command": "git status"}}))

    def test_session_start_context_includes_phase_instructions(self):
        self.start()
        text = workflow.handle_context(self.root, "SessionStart")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("phase=plan", text)
        self.assertIn("spec.md", text)
        self.assertIn(".claude/specloop/run/plan.md", text)

    def test_config_overrides_are_deep_merged_with_defaults(self):
        config = workflow.load_config(self.root)
        self.assertEqual("exit 0", config["phases"]["implement"]["check"]["command"])
        self.assertEqual("review", config["phases"]["implement"]["next"])
        self.assertEqual("verdict", config["phases"]["review"]["check"]["type"])

    def write_local_config(self, text):
        with open(os.path.join(self.base, "config.local.json"), "w") as f:
            f.write(text)

    def test_local_config_overrides_config_json_for_nested_keys(self):
        self.write_local_config(json.dumps({"phases": {"implement": {"check": {"command": "exit 1"}}}}))
        config = workflow.load_config(self.root)
        check = config["phases"]["implement"]["check"]
        self.assertEqual("exit 1", check["command"])            # config.local.json wins
        self.assertEqual("command", check["type"])              # sibling from config.json kept
        self.assertEqual("review", config["phases"]["implement"]["next"])  # sibling from defaults kept
        self.assertEqual(15, config["maxIterations"])            # untouched config.json key kept

    def test_local_config_overrides_defaults_when_config_json_is_silent(self):
        self.write_local_config(json.dumps({"feature": {"enforceDelegation": True}}))
        config = workflow.load_config(self.root)
        self.assertIs(True, config["feature"]["enforceDelegation"])
        self.assertIn("stepPhases", config["feature"])

    def test_missing_local_config_changes_nothing(self):
        without = workflow.load_config(self.root)
        self.write_local_config("{}")
        self.assertEqual(without, workflow.load_config(self.root))

    def test_invalid_local_config_is_ignored_with_warning(self):
        expected = workflow.load_config(self.root)
        for text in ("{broken", "[1, 2]", "\"text\""):
            self.write_local_config(text)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                config = workflow.load_config(self.root)
            self.assertEqual(expected, config, text)
            self.assertIn("[specloop] ignoring .claude/specloop/config.local.json: ", err.getvalue(), text)
            self.assertEqual(1, len(err.getvalue().strip().splitlines()), text)

    def test_local_config_is_protected_like_config_json(self):
        self.start()
        workflow.cmd_advance(self.root)
        self.assertEqual("implement", self.state()["phase"])
        for name in ("config.json", "config.local.json"):
            path = os.path.join(self.base, name)
            out = workflow.handle_pretool(self.root, {"tool_name": "Edit", "tool_input": {"file_path": path}})
            self.assertEqual("deny", out["hookSpecificOutput"]["permissionDecision"], name)
            self.assertIn(".claude/specloop/" + name, out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_phase_set_to_null_is_removed_and_flow_skips_it(self):
        self.set_config({"phases": {"implement": {"next": "done"}, "review": None}})
        config = workflow.load_config(self.root)
        self.assertNotIn("review", config["phases"])
        self.start()
        workflow.cmd_advance(self.root)
        self.assertEqual("implement", self.state()["phase"])
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("done", self.state()["status"])
        self.assertIn("done", out["systemMessage"])

    def test_override_prompt_wins_over_shipped_phase(self):
        self.write_override("plan", "Custom plan for {spec}")
        self.start()
        text = workflow.handle_context(self.root, "SessionStart")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Custom plan for spec.md", text)
        self.assertNotIn("## Phase: plan", text)

    def test_command_prefix_appears_in_messages(self):
        self.set_config({"commandPrefix": "x-"})
        self.assertIn("/x-go", workflow.cmd_init(self.root, "spec.md"))

    def test_disabled_env_makes_hooks_noop(self):
        self.start()
        os.environ["SPECLOOP_DISABLED"] = "1"
        try:
            self.assertIsNone(workflow.run_hook("stop", []))
        finally:
            del os.environ["SPECLOOP_DISABLED"]

    def test_hook_errors_never_fail_the_session(self):
        self.start()
        with open(os.path.join(self.base, "config.json"), "w") as f:
            f.write("{broken")
        os.environ["CLAUDE_PROJECT_DIR"] = self.root
        stdin = sys.stdin
        try:
            sys.stdin = open(os.devnull)
            self.assertEqual(0, workflow.main(["stop"]))
        finally:
            sys.stdin.close()
            sys.stdin = stdin
            del os.environ["CLAUDE_PROJECT_DIR"]

    def test_single_mode_allows_git_checkout(self):
        self.start()
        self.assertIsNone(workflow.handle_pretool(
            self.root, {"tool_name": "Bash", "tool_input": {"command": "git checkout main"}}))

    def test_enforce_delegation_applies_to_delegated_single_mode_phase(self):
        self.set_config({"feature": {"enforceDelegation": True},
                         "phases": {"implement": {"delegate": True, "check": None}}})
        self.start()
        workflow.cmd_advance(self.root)
        src = {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.root, "src", "Main.java")}}
        out = workflow.handle_pretool(self.root, src)
        self.assertEqual("deny", out["hookSpecificOutput"]["permissionDecision"])
        self.assertIsNone(workflow.handle_pretool(self.root, dict(src, agent_id="a1")))

    def test_single_mode_status_is_json(self):
        self.start()
        self.assertEqual(self.state(), json.loads(workflow.cmd_status(self.root)))

    def test_reset_removes_state(self):
        self.start()
        workflow.cmd_reset(self.root)
        self.assertIsNone(self.state())

    # --- "too big for single mode" safety net ---------------------------------

    def write_spec(self, text):
        with open(os.path.join(self.root, "spec.md"), "w") as f:
            f.write(text)

    def plan_items(self, count):
        marks = ["[ ]", "[x]", "[X]"]
        return "# Plan\n" + "".join("- %s step %d\n" % (marks[i % 3], i) for i in range(count))

    def test_large_spec_by_lines_warns_on_init_refuses_first_go_starts_on_second(self):
        self.write_spec("line\n" * 201)
        out = workflow.cmd_init(self.root, "spec.md")
        self.assertIn("201 lines", out)
        self.assertIn("200", out)
        self.assertIn("/sl-feature", out)

        out = workflow.cmd_go(self.root)
        self.assertIn("201 lines", out)
        self.assertIn("/sl-go", out)
        self.assertIn("again", out)
        self.assertIn("has not started", out)
        self.assertIn("Do not plan or implement", out)
        self.assertEqual("ready", self.state()["status"])
        self.assertIsNone(workflow.handle_stop(self.root, {}))

        out = workflow.cmd_go(self.root)
        self.assertEqual("active", self.state()["status"])
        self.assertTrue(self.state()["largeSpecAcknowledged"])
        self.assertIn("phase=plan", out)
        self.assertNotIn("201 lines", out)

    def test_large_spec_by_words_warns(self):
        self.write_spec("word " * 2001)
        out = workflow.cmd_init(self.root, "spec.md")
        self.assertIn("2001 words", out)
        self.assertIn("2000", out)
        workflow.cmd_go(self.root)
        self.assertEqual("ready", self.state()["status"])

    def test_spec_within_limits_starts_on_first_go(self):
        out = workflow.cmd_init(self.root, "spec.md")
        self.assertNotIn("/sl-feature", out)
        workflow.cmd_go(self.root)
        self.assertEqual("active", self.state()["status"])
        self.assertFalse(any(k.startswith("largeSpec") for k in self.state()))

    def test_large_spec_warning_block_null_disables_checks(self):
        self.set_config({"largeSpecWarning": None})
        self.write_spec("word\n" * 3000)
        self.assertNotIn("/sl-feature", workflow.cmd_init(self.root, "spec.md"))
        workflow.cmd_go(self.root)
        self.assertEqual("active", self.state()["status"])
        self.write_run("plan.md", self.plan_items(50))
        self.assertEqual("block", workflow.handle_stop(self.root, {})["decision"])
        self.assertEqual("implement", self.state()["phase"])

    def test_large_spec_single_key_null_disables_that_check(self):
        self.set_config({"largeSpecWarning": {"maxLines": None}})
        self.write_spec("word\n" * 300)
        self.assertNotIn("/sl-feature", workflow.cmd_init(self.root, "spec.md"))
        self.write_spec("word " * 2001)
        self.assertIn("2001 words", workflow.cmd_init(self.root, "spec.md"))

    def test_non_integer_limits_disable_the_check(self):
        self.set_config({"largeSpecWarning": {"maxLines": False, "maxWords": "many", "maxPlanItems": True}})
        self.write_spec("word\n" * 3000)
        self.assertNotIn("/sl-feature", workflow.cmd_init(self.root, "spec.md"))
        workflow.cmd_go(self.root)
        self.write_run("plan.md", self.plan_items(13))
        self.assertEqual("block", workflow.handle_stop(self.root, {})["decision"])
        self.assertEqual("active", self.state()["status"])

    def test_resumed_run_never_warns_about_spec_size(self):
        self.write_spec("line\n" * 201)
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        workflow.cmd_go(self.root)
        workflow.cmd_pause(self.root)
        out = workflow.cmd_go(self.root)
        self.assertNotIn("201 lines", out)
        self.assertEqual("active", self.state()["status"])

    def test_large_spec_warning_uses_command_prefix(self):
        self.set_config({"commandPrefix": "x-"})
        self.write_spec("line\n" * 201)
        self.assertIn("/x-feature", workflow.cmd_init(self.root, "spec.md"))
        self.assertIn("/x-go", workflow.cmd_go(self.root))

    def test_plan_over_limit_pauses_then_go_continues_in_implement(self):
        self.start()
        self.write_run("plan.md", self.plan_items(13))
        out = workflow.handle_stop(self.root, {})
        self.assertNotIn("decision", out)
        self.assertIn("13", out["systemMessage"])
        for name in ("/sl-reset", "/sl-feature", "/sl-go"):
            self.assertIn(name, out["systemMessage"])
        self.assertEqual("paused", self.state()["status"])
        self.assertEqual("implement", self.state()["phase"])
        self.assertIsNone(workflow.handle_stop(self.root, {}))

        out = workflow.cmd_go(self.root)
        self.assertIn("phase=implement", out)
        self.assertTrue(self.state()["largePlanAcknowledged"])
        self.assertEqual("active", self.state()["status"])
        self.assertIn("entering phase 'review'", workflow.handle_stop(self.root, {})["reason"])

    def test_plan_at_limit_does_not_pause(self):
        self.start()
        self.write_run("plan.md", self.plan_items(12))
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertEqual("active", self.state()["status"])
        self.assertEqual("implement", self.state()["phase"])

    def test_plan_items_single_key_null_disables_pause(self):
        self.set_config({"largeSpecWarning": {"maxPlanItems": None}})
        self.start()
        self.write_run("plan.md", self.plan_items(13))
        self.assertEqual("block", workflow.handle_stop(self.root, {})["decision"])
        self.assertEqual("active", self.state()["status"])

    def test_init_removes_stale_run_directory(self):
        self.start()
        self.write_run("plan.md", "old plan")
        self.write_run("review.md", "VERDICT: APPROVE")
        workflow.cmd_init(self.root, "spec.md")
        self.assertEqual([], os.listdir(os.path.join(self.base, "run")))
        workflow.cmd_go(self.root)
        self.assertEqual("block", workflow.handle_stop(self.root, {})["decision"])
        self.assertEqual("plan", self.state()["phase"])

    def test_init_with_missing_spec_keeps_run_directory(self):
        self.start()
        self.write_run("plan.md", "plan")
        workflow.cmd_init(self.root, "missing.md")
        self.assertTrue(os.path.exists(os.path.join(self.base, "run", "plan.md")))

    # --- steps check type ----------------------------------------------------------

    def use_steps_check(self, require_all_pending=True, **extra):
        check = {"type": "steps", "path": ".claude/specloop/run/steps.json"}
        if require_all_pending is not None:
            check["requireAllPending"] = require_all_pending
        config = {"phases": {"plan": {"check": check}, "implement": {"check": None}}}
        config.update(extra)
        self.set_config(config)
        self.start()

    def test_steps_check_passes_and_enters_next_phase(self):
        self.use_steps_check()
        self.write_run("steps.json", json.dumps(valid_steps()))
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("entering phase 'implement'", out["reason"])

    def test_steps_check_fails_when_missing(self):
        self.use_steps_check()
        out = workflow.handle_stop(self.root, {})
        self.assertIn("steps.json", out["reason"])
        self.assertIn("missing", out["reason"])
        self.assertEqual("plan", self.state()["phase"])

    def test_steps_check_fails_on_invalid_json(self):
        self.use_steps_check()
        self.write_run("steps.json", "{not json")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("not valid JSON", out["reason"])
        self.assertEqual("plan", self.state()["phase"])

    def test_steps_check_fails_on_schema_errors_and_lists_all(self):
        self.use_steps_check()
        data = valid_steps()
        data["feature"] = ""
        data["steps"][1]["title"] = ""
        self.write_run("steps.json", json.dumps(data))
        out = workflow.handle_stop(self.root, {})
        self.assertIn("feature", out["reason"])
        self.assertIn("steps[1] (id 2): title", out["reason"])
        self.assertEqual("plan", self.state()["phase"])

    def test_steps_check_require_all_pending(self):
        data = valid_steps()
        data["steps"][0]["status"] = "done"
        self.use_steps_check()
        self.write_run("steps.json", json.dumps(data))
        out = workflow.handle_stop(self.root, {})
        self.assertIn("steps[0] (id 1): status", out["reason"])
        self.assertIn("pending", out["reason"])
        self.assertEqual("plan", self.state()["phase"])

        self.use_steps_check(require_all_pending=False)
        self.write_run("steps.json", json.dumps(data))
        self.assertIn("entering phase 'implement'", workflow.handle_stop(self.root, {})["reason"])

    def test_steps_check_require_all_pending_defaults_to_false(self):
        data = valid_steps()
        data["steps"][0]["status"] = "done"
        self.use_steps_check(require_all_pending=None)
        self.write_run("steps.json", json.dumps(data))
        self.assertIn("entering phase 'implement'", workflow.handle_stop(self.root, {})["reason"])

    def test_steps_check_details_truncated(self):
        self.use_steps_check(maxOutputChars=100)
        data = {"feature": "F", "steps": [{"id": i + 1} for i in range(20)]}
        self.write_run("steps.json", json.dumps(data))
        out = workflow.handle_stop(self.root, {})
        self.assertIn("...(truncated)", out["reason"])
        self.assertIn("steps[0] (id 1)", out["reason"])
        self.assertNotIn("steps[19]", out["reason"])

    # --- delegation and the failure file ------------------------------------------

    def delegate(self, phase, command="exit 0", **extra):
        phases = {"implement": {"check": {"type": "command", "command": command}}}
        phases.setdefault(phase, {})["delegate"] = True
        config = {"phases": phases}
        config.update(extra)
        self.set_config(config)

    def last_failure(self):
        path = os.path.join(self.base, "run", "last-failure.md")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_delegate_absent_output_is_unchanged(self):
        for config in (None, {"phases": {"implement": {"delegate": False,
                                                       "check": {"type": "command", "command": "exit 1"}}}}):
            if config is None:
                self.set_test_command("exit 1")
            else:
                self.set_config(config)
            workflow.cmd_init(self.root, "spec.md")
            state = self.state()
            state["status"] = "active"
            out = workflow.cmd_go(self.root)
            cfg = workflow.load_config(self.root)
            self.assertEqual(workflow.header(state) + "\n\n" + workflow.phase_instructions(self.root, cfg, state), out)
            self.assertEqual(
                workflow.header(state) + "\n\n" + workflow.phase_instructions(self.root, cfg, state),
                workflow.handle_context(self.root, "SessionStart")["hookSpecificOutput"]["additionalContext"])

            self.write_run("plan.md", "plan")
            reason = workflow.handle_stop(self.root, {})["reason"]
            state = self.state()
            self.assertEqual("Phase 'plan' passed (.claude/specloop/run/plan.md exists). Now entering phase "
                             "'implement'.\n\n" + workflow.phase_instructions(self.root, cfg, state), reason)

            reason = workflow.handle_stop(self.root, {})["reason"]
            self.assertEqual("Phase 'implement' is not complete yet. `exit 1` exited with 1:\n\n\n"
                             "Keep working on phase 'implement'.\n\n"
                             + workflow.phase_instructions(self.root, cfg, state), reason)
            self.assertNotIn("delegated", workflow.handle_context(self.root, "UserPromptSubmit")
                             ["hookSpecificOutput"]["additionalContext"])

    def test_delegated_go_contains_preamble_instructions_and_files(self):
        self.delegate("plan")
        workflow.cmd_init(self.root, "spec.md")
        out = workflow.cmd_go(self.root)
        self.assertIn("phase=plan", out)
        self.assertIn("exactly one subagent", out)
        self.assertIn("## Phase: plan", out)
        self.assertIn("## Files to read", out)
        self.assertIn("- `spec.md`", out)
        self.assertNotIn("{brief}", out)
        self.assertNotIn("plan.md`", out.split("## Files to read")[1])
        text = workflow.handle_context(self.root, "SessionStart")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("exactly one subagent", text)
        self.assertIn("## Files to read", text)

    def test_delegated_stop_reason_on_pass_uses_preamble(self):
        self.delegate("implement")
        self.start()
        self.write_run("plan.md", "plan")
        reason = workflow.handle_stop(self.root, {})["reason"]
        self.assertIn("entering phase 'implement'", reason)
        self.assertIn("exactly one subagent", reason)
        self.assertIn("## Phase: implement", reason)
        self.assertIn("- `.claude/specloop/run/plan.md`", reason)

    def test_delegated_fail_reason_is_short_and_points_to_failure_file(self):
        self.delegate("implement", command="echo line-$((0+1)); echo line-$((1+1)); exit 1")
        self.start()
        self.write_run("plan.md", "plan")
        workflow.handle_stop(self.root, {})
        reason = workflow.handle_stop(self.root, {})["reason"]
        self.assertIn("Phase 'implement' is not complete yet", reason)
        self.assertIn("exited with 1", reason)
        self.assertIn(".claude/specloop/run/last-failure.md", reason)
        self.assertIn("exactly one subagent", reason)
        self.assertNotIn("line-1", reason)
        self.assertNotIn("line-2", reason)
        self.assertIn("line-2", self.last_failure())
        self.assertIn("- `.claude/specloop/run/last-failure.md`", reason)

    def test_delegated_reject_reason_is_short(self):
        self.delegate("implement")
        self.start()
        self.write_run("plan.md", "plan")
        workflow.handle_stop(self.root, {})
        workflow.handle_stop(self.root, {})
        self.write_run("review.md", "SECRET-DETAIL fix the null check.\nVERDICT: CHANGES_REQUESTED")
        reason = workflow.handle_stop(self.root, {})["reason"]
        self.assertIn("Back to phase 'implement'", reason)
        self.assertIn(".claude/specloop/run/last-failure.md", reason)
        self.assertNotIn("SECRET-DETAIL", reason)
        self.assertIn("- `.claude/specloop/run/review-3.md`", reason)
        self.assertIn("exactly one subagent", reason)

    def test_brief_lists_review_files_and_last_failure(self):
        self.start()
        state, config = self.state(), workflow.load_config(self.root)
        brief = workflow.phase_brief(self.root, config, state)
        self.assertNotIn("review", brief.split("## Files to read")[1])
        self.assertNotIn("last-failure", brief)
        self.write_run("plan.md", "plan")
        self.write_run("review-2.md", "old")
        self.write_run("review.md", "new")
        self.write_run("last-failure.md", "boom")
        files = workflow.phase_brief(self.root, config, state).split("## Files to read")[1]
        expected = ["spec.md", ".claude/specloop/run/plan.md", ".claude/specloop/run/review-2.md",
                    ".claude/specloop/run/review.md", ".claude/specloop/run/last-failure.md"]
        self.assertEqual(["- `%s`" % p for p in expected], [l for l in files.splitlines() if l.startswith("- ")])

    def test_delegate_override_preamble_wins(self):
        self.write_override("delegate", "CUSTOM ORCHESTRATOR for {spec}\n{brief}")
        self.delegate("plan")
        workflow.cmd_init(self.root, "spec.md")
        out = workflow.cmd_go(self.root)
        self.assertIn("CUSTOM ORCHESTRATOR for spec.md", out)
        self.assertNotIn("exactly one subagent", out)
        self.assertIn("## Phase: plan", out)

    def test_last_failure_written_on_failed_command_with_full_output(self):
        self.set_config({"maxOutputChars": 50, "phases": {"implement": {"check": {
            "type": "command", "command": "echo FIRST-$((2*3)); printf 'x%.0s' $(seq 1 200); echo; exit 2"}}}})
        self.start()
        self.write_run("plan.md", "plan")
        workflow.handle_stop(self.root, {})
        reason = workflow.handle_stop(self.root, {})["reason"]
        self.assertNotIn("FIRST-6", reason)
        text = self.last_failure()
        self.assertIn("exited with 2", text)
        self.assertIn("FIRST-6", text)
        self.assertIn("x" * 200, text)

    def test_last_failure_written_on_rejected_review(self):
        self.start()
        self.write_run("plan.md", "plan")
        workflow.handle_stop(self.root, {})
        workflow.handle_stop(self.root, {})
        review = "Fix it.\n" + "detail " * 1000 + "\nVERDICT: CHANGES_REQUESTED"
        self.write_run("review.md", review)
        workflow.handle_stop(self.root, {})
        self.assertEqual(review, self.last_failure())

    def test_last_failure_removed_when_phase_passes(self):
        self.start()
        workflow.handle_stop(self.root, {})
        self.assertIn("plan.md", self.last_failure())
        self.write_run("plan.md", "plan")
        workflow.handle_stop(self.root, {})
        self.assertIsNone(self.last_failure())

    def test_last_failure_removed_on_advance(self):
        self.start()
        workflow.handle_stop(self.root, {})
        self.assertIsNotNone(self.last_failure())
        workflow.cmd_advance(self.root)
        self.assertIsNone(self.last_failure())

    def test_delegated_context_states_orchestrator_role(self):
        self.delegate("plan")
        self.start()
        workflow.handle_stop(self.root, {})
        text = workflow.handle_context(self.root, "UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Phase 'plan' is delegated", text)
        self.assertNotIn("## Files to read", text)
        text = workflow.handle_context(self.root, "SessionStart")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Phase 'plan' is delegated", text)


def valid_steps():
    return {
        "feature": "Short feature title",
        "steps": [
            {
                "id": 1,
                "title": "Add steps.json schema validation",
                "type": "feat",
                "description": "What to change and why.",
                "acceptance": ["Observable criteria this step must satisfy"],
                "files": ["kit/specloop/engine.py"],
                "status": "pending",
                "commit": None,
            },
            {
                "id": 2,
                "title": "Document it",
                "description": "Explain the format.",
                "acceptance": ["README mentions steps.json"],
                "status": "pending",
            },
        ],
    }


class StepsValidationTest(unittest.TestCase):

    def errors_for(self, index, field, value, delete=False):
        data = valid_steps()
        if delete:
            del data["steps"][index][field]
        else:
            data["steps"][index][field] = value
        return workflow.validate_steps(data)

    def assertStepError(self, errors, location, field):
        self.assertTrue(errors, "expected errors")
        self.assertTrue(any(e.startswith(location) and field in e for e in errors),
                        "no error for %s %s in %r" % (location, field, errors))

    def test_valid_steps_pass(self):
        self.assertEqual([], workflow.validate_steps(valid_steps()))

    def test_top_level_must_be_object(self):
        self.assertTrue(workflow.validate_steps([]))
        self.assertTrue(workflow.validate_steps(None))

    def test_feature_required_non_empty_string(self):
        for value in (None, "", "  ", 5):
            data = valid_steps()
            data["feature"] = value
            self.assertStepError(workflow.validate_steps(data), "feature", "feature")
        data = valid_steps()
        del data["feature"]
        self.assertStepError(workflow.validate_steps(data), "feature", "feature")

    def test_steps_required_non_empty_list(self):
        for value in (None, [], {}, "x"):
            data = valid_steps()
            data["steps"] = value
            self.assertStepError(workflow.validate_steps(data), "steps", "steps")

    def test_step_must_be_object(self):
        data = valid_steps()
        data["steps"].append("not a step")
        self.assertStepError(workflow.validate_steps(data), "steps[2]", "object")

    def test_id_must_be_positive_integer(self):
        for value in (0, -1, "1", True, 1.5, None):
            self.assertStepError(self.errors_for(0, "id", value), "steps[0]", "id")
        self.assertStepError(self.errors_for(0, "id", None, delete=True), "steps[0]", "id")

    def test_id_must_be_unique(self):
        errors = self.errors_for(1, "id", 1)
        self.assertEqual(1, len(errors))
        self.assertStepError(errors, "steps[1] (id 1)", "id")

    def test_title_required_and_max_72(self):
        for value in ("", "   ", None, 7, "x" * 73):
            self.assertStepError(self.errors_for(0, "title", value), "steps[0] (id 1)", "title")
        self.assertStepError(self.errors_for(0, "title", None, delete=True), "steps[0] (id 1)", "title")
        self.assertEqual([], self.errors_for(0, "title", "x" * 72))

    def test_description_required(self):
        for value in ("", None, ["x"]):
            self.assertStepError(self.errors_for(1, "description", value), "steps[1] (id 2)", "description")
        self.assertStepError(self.errors_for(1, "description", None, delete=True), "steps[1] (id 2)", "description")

    def test_acceptance_non_empty_list_of_non_empty_strings(self):
        for value in ([], "criterion", [""], ["ok", " "], [1], None):
            self.assertStepError(self.errors_for(0, "acceptance", value), "steps[0] (id 1)", "acceptance")
        self.assertStepError(self.errors_for(0, "acceptance", None, delete=True), "steps[0] (id 1)", "acceptance")

    def test_status_must_be_known(self):
        errors = self.errors_for(0, "status", "finished")
        self.assertStepError(errors, "steps[0] (id 1)", "status")
        self.assertIn("pending", errors[0])
        self.assertStepError(self.errors_for(0, "status", None, delete=True), "steps[0] (id 1)", "status")
        for value in ("pending", "active", "done", "empty"):
            self.assertEqual([], self.errors_for(0, "status", value))

    def test_type_must_be_conventional_commit_type(self):
        self.assertStepError(self.errors_for(0, "type", "feature"), "steps[0] (id 1)", "type")
        for value in ("feat", "fix", "refactor", "test", "docs", "chore", "perf", "build", "ci", "style"):
            self.assertEqual([], self.errors_for(0, "type", value))

    def test_files_must_be_list_of_strings(self):
        for value in ("a.py", [1], None):
            self.assertStepError(self.errors_for(0, "files", value), "steps[0] (id 1)", "files")
        self.assertEqual([], self.errors_for(0, "files", []))

    def test_commit_must_be_string_or_null(self):
        self.assertStepError(self.errors_for(0, "commit", 123), "steps[0] (id 1)", "commit")
        self.assertEqual([], self.errors_for(0, "commit", "abc123"))
        self.assertEqual([], self.errors_for(0, "commit", None))

    def test_reports_all_errors_at_once(self):
        data = valid_steps()
        data["steps"][0]["title"] = ""
        data["steps"][1]["status"] = "bogus"
        errors = workflow.validate_steps(data)
        self.assertStepError(errors, "steps[0] (id 1)", "title")
        self.assertStepError(errors, "steps[1] (id 2)", "status")

    # --- immutability ------------------------------------------------------------

    def history(self):
        """Three steps: 1 done, 2 empty, 3 pending, 4 active."""
        data = valid_steps()
        data["steps"][0].update(status="done", commit="abc123")
        data["steps"][1].update(status="empty")
        data["steps"].append({"id": 3, "title": "Third", "description": "d", "acceptance": ["a"],
                              "status": "pending"})
        data["steps"].append({"id": 4, "title": "Fourth", "description": "d", "acceptance": ["a"],
                              "status": "active"})
        return data

    def test_immutable_accepts_pending_edits(self):
        previous = self.history()
        current = self.history()
        current["steps"][2].update(title="Renamed", description="New", acceptance=["x", "y"])
        current["steps"][3]["description"] = "Changed active step"
        current["steps"].append({"id": 9, "title": "New", "description": "d", "acceptance": ["a"],
                                 "status": "pending", "commit": None})
        current["steps"].insert(2, current["steps"].pop())  # move the new step before step 3
        self.assertEqual([], workflow.check_steps_immutable(previous, current))
        del current["steps"][3]  # remove pending step 3
        self.assertEqual([], workflow.check_steps_immutable(previous, current))
        current["steps"][2], current["steps"][3] = current["steps"][3], current["steps"][2]
        self.assertEqual([], workflow.check_steps_immutable(previous, current))

    def test_immutable_rejects_done_step_changed(self):
        current = self.history()
        current["steps"][0]["title"] = "Rewritten history"
        self.assertStepError(workflow.check_steps_immutable(self.history(), current), "steps[0] (id 1)", "done")

    def test_immutable_rejects_empty_step_changed(self):
        current = self.history()
        current["steps"][1]["acceptance"] = ["other"]
        self.assertStepError(workflow.check_steps_immutable(self.history(), current), "steps[1] (id 2)", "empty")

    def test_immutable_rejects_done_step_removed(self):
        current = self.history()
        del current["steps"][0]
        errors = workflow.check_steps_immutable(self.history(), current)
        self.assertTrue(any("id 1" in e and "removed" in e for e in errors), errors)

    def test_immutable_rejects_done_steps_reordered(self):
        current = self.history()
        current["steps"][0], current["steps"][1] = current["steps"][1], current["steps"][0]
        errors = workflow.check_steps_immutable(self.history(), current)
        self.assertTrue(any("reordered" in e for e in errors), errors)

    def test_immutable_rejects_status_change(self):
        current = self.history()
        current["steps"][2]["status"] = "done"
        self.assertStepError(workflow.check_steps_immutable(self.history(), current), "steps[2] (id 3)", "status")

    def test_immutable_rejects_commit_change(self):
        current = self.history()
        current["steps"][2]["commit"] = "def456"
        self.assertStepError(workflow.check_steps_immutable(self.history(), current), "steps[2] (id 3)", "commit")

    def test_immutable_rejects_new_step_not_pending(self):
        new = {"id": 9, "title": "New", "description": "d", "acceptance": ["a"], "status": "active"}
        current = self.history()
        current["steps"].append(new)
        self.assertStepError(workflow.check_steps_immutable(self.history(), current), "steps[4] (id 9)", "status")
        new.update(status="pending", commit="abc999")
        self.assertStepError(workflow.check_steps_immutable(self.history(), current), "steps[4] (id 9)", "commit")


def force_rmtree(path):
    """Remove a tree, including git's read-only object files on Windows."""
    def make_writable(func, target, _):
        os.chmod(target, 0o700)
        func(target)
    if os.path.exists(path):
        shutil.rmtree(path, onerror=make_writable)


class FeatureModeTest(unittest.TestCase):

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        self.root = tempfile.mkdtemp()
        self.ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(self.root)
        self.base = os.path.join(self.root, ".claude", "specloop")
        shutil.copytree(KIT, self.base)
        self.write("spec.md", "# My Big Feature!\n\nDo many things.\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", "spec.md")
        self.git("commit", "-q", "-m", "initial")
        self.start_branch = self.git("symbolic-ref", "--short", "HEAD")

    def tearDown(self):
        if self.ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = self.ceiling
        force_rmtree(self.root)

    def git(self, *args):
        proc = subprocess.run(["git"] + list(args), cwd=self.root, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, check=True)
        return proc.stdout.decode("utf-8").strip()

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def write_run(self, name, text):
        self.write(os.path.join(".claude", "specloop", "run", name), text)

    def set_config(self, config):
        with open(os.path.join(self.base, "config.json"), "w") as f:
            json.dump(config, f)

    def state(self):
        return workflow.load_state(self.root)

    def state_bytes(self):
        path = os.path.join(self.base, "state.json")
        with open(path, "rb") as f:
            return f.read()

    def current_branch(self):
        return self.git("symbolic-ref", "--short", "HEAD")

    def last_failure(self):
        with open(os.path.join(self.base, "run", "last-failure.md"), encoding="utf-8") as f:
            return f.read()

    def start(self):
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        return workflow.cmd_go(self.root)

    def pass_planning(self, data=None):
        self.write_run("plan.md", "# Plan")
        self.write_run("steps.json", json.dumps(data or valid_steps()))
        return workflow.handle_stop(self.root, {})

    def assertRefusedUnchanged(self, needle):
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        before = self.state_bytes()
        out = workflow.cmd_go(self.root)
        self.assertIn(needle, out)
        self.assertEqual(before, self.state_bytes())
        if os.path.isdir(os.path.join(self.root, ".git")):
            self.assertNotIn("specloop/", self.git("branch", "--list"))
        return out

    def test_init_feature_sets_mode_and_plan_phase(self):
        self.write("spec.md", "# Big\n" + "line\n" * 300)
        out = workflow.cmd_init(self.root, "spec.md", mode="feature")
        self.assertNotIn("/sl-feature", out)
        self.assertEqual("feature", self.state()["mode"])
        self.assertEqual("feature-plan", self.state()["phase"])
        self.assertEqual("ready", self.state()["status"])
        workflow.cmd_init(self.root, "spec.md")
        self.assertEqual("single", self.state()["mode"])

    def test_init_feature_command_entry_point(self):
        os.environ["CLAUDE_PROJECT_DIR"] = self.root
        try:
            self.assertEqual(0, workflow.main(["init-feature", "spec.md"]))
        finally:
            del os.environ["CLAUDE_PROJECT_DIR"]
        self.assertEqual("feature", self.state()["mode"])

    def test_go_creates_branch_records_base_and_enters_plan(self):
        head = self.git("rev-parse", "HEAD")
        out = self.start()
        self.assertEqual("specloop/my-big-feature", self.current_branch())
        state = self.state()
        self.assertEqual("specloop/my-big-feature", state["branch"])
        self.assertEqual(head, state["baseRef"])
        self.assertEqual("active", state["status"])
        self.assertEqual("feature-plan", state["phase"])
        self.assertIn("exactly one subagent", out)
        self.assertIn("steps.json", out)

    def test_existing_branch_gets_numeric_suffix(self):
        self.start()
        self.git("checkout", "-q", self.start_branch)
        self.start()
        self.assertEqual("specloop/my-big-feature-2", self.current_branch())
        self.git("checkout", "-q", self.start_branch)
        self.start()
        self.assertEqual("specloop/my-big-feature-3", self.state()["branch"])

    def test_slug_from_heading_file_name_and_limits(self):
        self.assertEqual("my-big-feature", workflow.branch_slug("# My Big Feature!\nx", "spec.md"))
        self.assertEqual("add-login", workflow.branch_slug("no heading\n## Sub\n", "specs/Add_Login.md"))
        self.assertEqual("cafe-deja-vu", workflow.branch_slug("# Café — déjà vu\n", "x.md"))
        self.assertEqual("second", workflow.branch_slug("text\n#hashtag\n# Second\n# Third", "x.md"))
        slug = workflow.branch_slug("# " + "word " * 20, "x.md")
        self.assertLessEqual(len(slug), 50)
        self.assertFalse(slug.endswith("-"))
        self.assertEqual("feature", workflow.branch_slug("# !!!", "???.md"))

    def test_go_refuses_outside_git_repo(self):
        force_rmtree(os.path.join(self.root, ".git"))
        self.assertIn("not a git repository", self.assertRefusedUnchanged("not a git repository"))

    def test_go_refuses_on_detached_head(self):
        self.git("checkout", "-q", "--detach")
        self.assertRefusedUnchanged("detached")

    def test_go_refuses_with_modified_tracked_file(self):
        self.write("spec.md", "# My Big Feature!\nchanged\n")
        self.assertIn("spec.md", self.assertRefusedUnchanged("uncommitted changes"))

    def test_go_refuses_with_staged_new_file(self):
        self.write("new.py", "x = 1\n")
        self.git("add", "new.py")
        self.assertIn("new.py", self.assertRefusedUnchanged("uncommitted changes"))

    def test_changes_under_specloop_dir_do_not_block(self):
        self.set_config({"commandPrefix": "sl-"})
        self.git("add", ".claude/specloop/config.json")
        self.git("commit", "-q", "-m", "config")
        self.set_config({"commandPrefix": "sl-", "maxIterations": 9})
        self.start()
        self.assertEqual("active", self.state()["status"])

    def test_preexisting_untracked_files_recorded(self):
        self.write("notes.txt", "n")
        self.write(os.path.join("dir", "a.txt"), "a")
        self.start()
        self.assertEqual(["dir/a.txt", "notes.txt"], self.state()["preexistingUntracked"])

    def test_invalid_steps_blocks_stop_with_errors(self):
        self.start()
        data = valid_steps()
        data["steps"][0]["title"] = ""
        out = self.pass_planning(data)
        self.assertEqual("block", out["decision"])
        # feature-plan is delegated, so the reason stays short and the errors are in last-failure.md.
        self.assertIn("steps.json is invalid", out["reason"])
        self.assertIn(".claude/specloop/run/last-failure.md", out["reason"])
        self.assertIn("steps[0] (id 1): title", self.last_failure())
        self.assertEqual("feature-plan", self.state()["phase"])
        self.assertEqual("active", self.state()["status"])

    def test_steps_must_all_be_pending(self):
        self.start()
        data = valid_steps()
        data["steps"][1]["status"] = "done"
        self.assertEqual("block", self.pass_planning(data)["decision"])
        self.assertIn("steps[1] (id 2): status must be pending", self.last_failure())

    def test_invalid_steps_reason_lists_errors_when_not_delegated(self):
        self.set_config({"feature": {"planPhases": {"feature-plan": {"delegate": False}}}})
        self.start()
        data = valid_steps()
        data["steps"][0]["title"] = ""
        self.assertIn("steps[0] (id 1): title", self.pass_planning(data)["reason"])

    def test_valid_steps_pause_for_approval_then_go_stores_approved_steps(self):
        self.start()
        out = self.pass_planning()
        self.assertNotIn("decision", out)
        for needle in ("plan.md", "steps.json", "/sl-go"):
            self.assertIn(needle, out["systemMessage"])
        self.assertEqual("paused", self.state()["status"])
        self.assertTrue(self.state()["awaitingApproval"])
        self.assertIsNone(workflow.handle_stop(self.root, {}))

        broken = valid_steps()
        broken["steps"][0]["acceptance"] = []
        self.write_run("steps.json", json.dumps(broken))
        out = workflow.cmd_go(self.root)
        self.assertIn("steps[0] (id 1): acceptance", out)
        self.assertNotIn("approvedSteps", self.state())
        self.assertEqual("paused", self.state()["status"])

        edited = valid_steps()
        edited["steps"][0]["title"] = "Edited by the user"
        self.write_run("steps.json", json.dumps(edited))
        out = workflow.cmd_go(self.root)
        self.assertIn("Edited by the user", out)
        edited["steps"][0]["status"] = "active"
        self.assertEqual(edited, self.state()["approvedSteps"])
        self.assertEqual("active", self.state()["status"])
        self.assertEqual("implement", self.state()["phase"])

    def test_go_refuses_approval_when_steps_file_missing(self):
        self.start()
        self.pass_planning()
        os.remove(os.path.join(self.base, "run", "steps.json"))
        self.assertIn("missing", workflow.cmd_go(self.root))
        self.assertNotIn("approvedSteps", self.state())

    def test_require_plan_approval_false_starts_first_step(self):
        self.set_config({"feature": {"requirePlanApproval": False}})
        self.start()
        out = self.pass_planning()
        self.assertEqual("block", out["decision"])
        self.assertIn("Step 1/2", out["reason"])
        self.assertIn(valid_steps()["steps"][0]["title"], out["reason"])
        self.assertEqual("active", self.state()["status"])
        self.assertEqual("implement", self.state()["phase"])
        self.assertEqual("active", self.state()["approvedSteps"]["steps"][0]["status"])
        self.assertNotIn("awaitingApproval", self.state())

    def test_null_feature_config_does_not_break_loading(self):
        self.set_config({"feature": None})
        config = workflow.load_config(self.root)
        self.assertEqual({}, config["feature"]["planPhases"])
        workflow.cmd_init(self.root, "spec.md")
        self.assertEqual("plan", self.state()["phase"])

    def test_advance_in_plan_phase_enters_approval(self):
        self.start()
        self.write_run("steps.json", json.dumps(valid_steps()))
        workflow.cmd_advance(self.root)
        self.assertTrue(self.state()["awaitingApproval"])
        self.assertEqual("paused", self.state()["status"])
        self.assertEqual("feature-plan", self.state()["phase"])

    def test_resume_on_other_branch_is_refused(self):
        self.start()
        workflow.cmd_pause(self.root)
        self.git("checkout", "-q", self.start_branch)
        before = self.state_bytes()
        out = workflow.cmd_go(self.root)
        self.assertIn("specloop/my-big-feature", out)
        self.assertIn(self.start_branch, out)
        self.assertEqual(before, self.state_bytes())
        self.git("checkout", "-q", "specloop/my-big-feature")
        workflow.cmd_go(self.root)
        self.assertEqual("active", self.state()["status"])

    def test_feature_plan_edits_restricted_to_run_dir(self):
        self.start()
        src = os.path.join(self.root, "src", "x.py")
        out = workflow.handle_pretool(self.root, {"tool_name": "Edit", "tool_input": {"file_path": src}})
        self.assertEqual("deny", out["hookSpecificOutput"]["permissionDecision"])
        steps = os.path.join(self.base, "run", "steps.json")
        self.assertIsNone(workflow.handle_pretool(self.root, {"tool_name": "Write", "tool_input": {"file_path": steps}}))

    def test_context_and_delegation_in_feature_plan(self):
        self.start()
        workflow.handle_stop(self.root, {})
        text = workflow.handle_context(self.root, "UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Phase 'feature-plan' is delegated", text)

    def test_git_errors_never_raise(self):
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        path = os.environ.get("PATH", "")
        os.environ["PATH"] = ""
        try:
            out = workflow.cmd_go(self.root)
        finally:
            os.environ["PATH"] = path
        self.assertIn("git", out)
        self.assertEqual("ready", self.state()["status"])
        ok, output = workflow.git(self.root, {"feature": {"gitTimeoutSec": 30}}, "rev-parse", "HEAD")
        self.assertTrue(ok)
        self.assertEqual(self.git("rev-parse", "HEAD"), output)

    # --- step cycle and commits ----------------------------------------------------

    def two_steps(self):
        data = valid_steps()
        data["steps"][1]["type"] = "docs"
        return data

    def approve(self, data=None, feature=None, implement_command="exit 0"):
        config = {"phases": {"implement": {"check": {"type": "command", "command": implement_command}}}}
        if feature:
            config["feature"] = feature
        self.set_config(config)
        self.start()
        self.pass_planning(data or self.two_steps())
        return workflow.cmd_go(self.root)

    def run_step(self, files=None, verdict="APPROVE"):
        for rel, text in (files or {}).items():
            self.write(rel, text)
        workflow.handle_stop(self.root, {})  # implement -> review
        self.write_run("review.md", "Review notes.\nVERDICT: %s" % verdict)
        return workflow.handle_stop(self.root, {})  # review -> commit (or back to implement)

    def commit_count(self):
        return int(self.git("rev-list", "--count", "HEAD"))

    def steps_file(self):
        with open(os.path.join(self.base, "run", "steps.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_approval_starts_first_step_with_rendered_instructions(self):
        out = self.approve()
        state = self.state()
        self.assertEqual("active", state["status"])
        self.assertEqual("implement", state["phase"])
        self.assertEqual(1, state["stepId"])
        self.assertEqual(0, state["iter"])
        self.assertEqual("active", state["approvedSteps"]["steps"][0]["status"])
        self.assertEqual(state["approvedSteps"], self.steps_file())
        self.assertIn("1/2", out)
        self.assertIn("Add steps.json schema validation", out)
        self.assertIn("- Observable criteria this step must satisfy", out)
        self.assertIn("exactly one subagent", out)
        self.assertIn("- `.claude/specloop/run/steps.json`", out)
        self.assertNotIn("{step_", out)

    def test_two_steps_produce_two_commits_and_complete(self):
        self.approve()
        out = self.run_step({"a.py": "a = 1\n"})
        self.assertEqual("block", out["decision"])
        self.assertIn("Step 1/2 committed", out["reason"])
        self.assertIn("Document it", out["reason"])
        self.assertEqual(2, self.state()["stepId"])
        self.assertTrue(os.path.exists(os.path.join(self.base, "run", "steps", "1", "review.md")))
        self.assertFalse(os.path.exists(os.path.join(self.base, "run", "review.md")))

        out = self.run_step({"README.txt": "docs\n"})
        self.assertEqual(3, self.commit_count())
        self.assertEqual("specloop/my-big-feature", self.current_branch())
        messages = [self.git("log", "--format=%B", "-n", "1", "HEAD~1"), self.git("log", "--format=%B", "-n", "1")]
        self.assertEqual("feat: Add steps.json schema validation\n\nspecloop step 1/2 (1) of spec.md", messages[0])
        self.assertEqual("docs: Document it\n\nspecloop step 2/2 (2) of spec.md", messages[1])

        state = self.state()
        self.assertEqual("done", state["status"])
        shas = [self.git("rev-parse", "HEAD~1"), self.git("rev-parse", "HEAD")]
        for step, sha in zip(state["approvedSteps"]["steps"], shas):
            self.assertEqual(("done", sha), (step["status"], step["commit"]))
        self.assertEqual(state["approvedSteps"], self.steps_file())
        self.assertNotIn("decision", out)
        for sha, title in zip(shas, ("Add steps.json schema validation", "Document it")):
            self.assertIn("%s %s" % (sha[:7], title), out["systemMessage"])
        self.assertIn("specloop/my-big-feature", out["systemMessage"])
        self.assertIsNone(workflow.handle_stop(self.root, {}))

    def test_rejected_review_returns_to_implement_without_commit(self):
        self.approve()
        out = self.run_step({"a.py": "a = 1\n"}, verdict="CHANGES_REQUESTED")
        self.assertEqual("block", out["decision"])
        self.assertEqual("implement", self.state()["phase"])
        self.assertEqual(1, self.state()["stepId"])
        self.assertEqual(1, self.commit_count())

    def test_step_without_changes_is_marked_empty(self):
        self.approve()
        out = self.run_step()
        self.assertIn("no changes", out["reason"])
        self.assertEqual(1, self.commit_count())
        step = self.state()["approvedSteps"]["steps"][0]
        self.assertEqual(("empty", None), (step["status"], step.get("commit")))
        self.assertTrue(any(h.get("result") == "empty" and h.get("warning") for h in self.state()["history"]))
        self.assertEqual(2, self.state()["stepId"])
        out = self.run_step({"b.py": "b = 2\n"})
        self.assertIn("(empty) Add steps.json schema validation", out["systemMessage"])

    def test_preexisting_untracked_and_specloop_files_never_committed(self):
        self.write("notes.txt", "mine")
        self.approve()
        self.write("notes.txt", "mine, edited during the run")
        self.run_step({"a.py": "a = 1\n"})
        files = self.git("show", "--name-only", "--format=", "HEAD").splitlines()
        self.assertEqual(["a.py"], files)
        self.assertIn("notes.txt", self.git("status", "--porcelain", "--untracked-files=all"))

    def test_failing_pre_commit_hook_returns_to_implement(self):
        self.approve()
        hook = os.path.join(self.root, ".git", "hooks", "pre-commit")
        with open(hook, "w", newline="\n") as f:
            f.write("#!/bin/sh\necho HOOK-REJECTED\nexit 1\n")
        os.chmod(hook, 0o755)
        out = self.run_step({"a.py": "a = 1\n"})
        self.assertEqual("block", out["decision"])
        self.assertIn("HOOK-REJECTED", self.last_failure())
        self.assertIn("last-failure.md", out["reason"])
        state = self.state()
        self.assertEqual("implement", state["phase"])
        self.assertEqual("active", state["approvedSteps"]["steps"][0]["status"])
        self.assertEqual(2, state["iter"])
        self.assertEqual(1, self.commit_count())

    def test_failing_commit_reason_has_output_when_not_delegated(self):
        self.approve(feature={"stepPhases": {"implement": {"delegate": False}}})
        hook = os.path.join(self.root, ".git", "hooks", "pre-commit")
        with open(hook, "w", newline="\n") as f:
            f.write("#!/bin/sh\necho HOOK-REJECTED\nexit 1\n")
        os.chmod(hook, 0o755)
        self.assertIn("HOOK-REJECTED", self.run_step({"a.py": "a = 1\n"})["reason"])

    def test_advance_from_review_commits_and_from_implement_moves_to_review(self):
        self.approve()
        self.write("a.py", "a = 1\n")
        workflow.cmd_advance(self.root)
        self.assertEqual("review", self.state()["phase"])
        self.assertEqual(1, self.commit_count())
        out = workflow.cmd_advance(self.root)
        self.assertIn("committed", out)
        self.assertEqual(2, self.commit_count())
        self.assertEqual("done", self.state()["approvedSteps"]["steps"][0]["status"])
        self.assertEqual(("implement", 2), (self.state()["phase"], self.state()["stepId"]))

    def test_pause_between_steps(self):
        self.approve(feature={"pauseBetweenSteps": True})
        out = self.run_step({"a.py": "a = 1\n"})
        self.assertNotIn("decision", out)
        self.assertIn("/sl-go", out["systemMessage"])
        self.assertEqual(("paused", 2), (self.state()["status"], self.state()["stepId"]))
        self.assertIn("Document it", workflow.cmd_go(self.root))
        self.assertEqual("active", self.state()["status"])

    def test_iterations_reset_per_step(self):
        self.approve()
        workflow.handle_stop(self.root, {})
        self.write_run("review.md", "VERDICT: CHANGES_REQUESTED")
        workflow.handle_stop(self.root, {})
        self.assertEqual(2, self.state()["iter"])
        self.run_step({"a.py": "a = 1\n"})
        self.assertEqual((2, 0), (self.state()["stepId"], self.state()["iter"]))

    def test_step_phase_set_to_null_is_removed(self):
        self.approve(feature={"stepPhases": {"implement": {"next": "commit"}, "review": None}})
        self.write("a.py", "a = 1\n")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("Step 1/2 committed", out["reason"])
        self.assertEqual(2, self.commit_count())

    def test_implement_step_inherits_project_test_command(self):
        self.approve(implement_command="echo tests-$((1+1)); exit 1")
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("tests-2", self.last_failure())
        self.assertEqual("implement", self.state()["phase"])

    def test_step_instructions_override(self):
        os.makedirs(os.path.join(self.base, "overrides"), exist_ok=True)
        with open(os.path.join(self.base, "overrides", "step-implement.md"), "w") as f:
            f.write("CUSTOM step {step_index} of {step_count}: {step_title} [{step_id}] {steps_file}")
        out = self.approve()
        self.assertIn("CUSTOM step 1 of 2: Add steps.json schema validation [1] .claude/specloop/run/steps.json", out)

    # --- guard rails, header and status -------------------------------------------

    def bash(self, command):
        out = workflow.handle_pretool(self.root, {"tool_name": "Bash", "tool_input": {"command": command}})
        return out["hookSpecificOutput"]["permissionDecisionReason"] if out else None

    def test_feature_blocked_commands_denied_while_active(self):
        self.start()
        for command in ("git commit -m x", "git checkout main", "git stash",
                        "cd x && git reset --hard", "git switch -c other", "git branch -D old"):
            reason = self.bash(command)
            self.assertIsNotNone(reason, command)
            self.assertIn("specloop/my-big-feature", reason)
        self.assertIsNotNone(self.bash("git push"))  # caught first by blockedCommandsUntilDone
        for command in ("git status", "git diff", "git log --oneline", "git branch -d old"):
            self.assertIsNone(self.bash(command), command)
        self.pass_planning()
        workflow.cmd_go(self.root)
        self.assertIsNotNone(self.bash("git commit -am wip"))

    def test_feature_blocked_commands_not_applied_when_paused(self):
        self.start()
        workflow.cmd_pause(self.root)
        self.assertIsNone(self.bash("git checkout main"))

    def write_steps(self, mutate):
        data = self.steps_file()
        mutate(data)
        self.write_run("steps.json", json.dumps(data))

    def test_steps_file_done_title_change_blocks(self):
        self.approve()
        self.run_step({"a.py": "a = 1\n"})
        self.set_config({"phases": {"implement": {"check": {
            "type": "command", "command": "echo tests-$((40+2)); exit 1"}}}})
        self.write_steps(lambda d: d["steps"][0].update(title="Rewritten"))
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("steps[0] (id 1)", out["reason"])
        self.assertNotIn("tests-42", self.last_failure())
        self.assertIn("steps[0] (id 1): step is done", self.last_failure())
        self.assertEqual(("implement", 2), (self.state()["phase"], self.state()["stepId"]))
        self.assertEqual(1, self.state()["iter"])

    def test_steps_file_status_change_blocks(self):
        self.approve()
        self.write_steps(lambda d: d["steps"][1].update(status="done"))
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("steps[1] (id 2)", out["reason"])
        self.assertIn("status", out["reason"])
        self.assertEqual("implement", self.state()["phase"])

    def test_steps_file_pending_description_edit_accepted(self):
        self.approve()
        approved = self.state()["approvedSteps"]
        self.write_steps(lambda d: d["steps"][1].update(description="Clarified by Claude."))
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'review'", out["reason"])
        self.assertEqual(approved, self.state()["approvedSteps"])

    def test_steps_file_missing_or_invalid_blocks(self):
        self.approve()
        self.write_run("steps.json", "{broken")
        self.assertIn("steps.json", workflow.handle_stop(self.root, {})["reason"])
        os.remove(os.path.join(self.base, "run", "steps.json"))
        out = workflow.handle_stop(self.root, {})
        self.assertIn("missing", out["reason"])
        self.assertEqual("implement", self.state()["phase"])

    def test_steps_file_not_guarded_during_planning(self):
        self.start()
        self.write_run("steps.json", "{broken")
        workflow.handle_stop(self.root, {})
        self.assertNotIn("may only be changed by specloop", self.last_failure())

    def context(self):
        return workflow.handle_context(self.root, "UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]

    def test_feature_header_formats(self):
        out = self.start()
        planning = "[specloop] feature planning, phase=feature-plan, iteration 0, branch=specloop/my-big-feature"
        self.assertTrue(out.startswith(planning + "\n"), out[:200])
        workflow.handle_stop(self.root, {})
        self.assertTrue(self.context().startswith(planning.replace("iteration 0", "iteration 1")))
        self.pass_planning()
        out = workflow.cmd_go(self.root)
        step = ('[specloop] feature step 1/2 "Add steps.json schema validation", phase=implement, '
                'iteration 0, branch=specloop/my-big-feature')
        self.assertTrue(out.startswith(step + "\n"), out[:200])
        self.assertTrue(self.context().startswith(step))
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        self.assertEqual("[specloop] feature planning, phase=feature-plan, iteration 0, branch=-",
                         workflow.header(self.state()))

    def test_feature_status_output(self):
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        self.assertIn("steps: no plan yet", workflow.cmd_status(self.root))
        workflow.cmd_go(self.root)
        self.write_run("steps.json", json.dumps(self.two_steps()))
        self.assertIn("steps (not approved yet, 0/2 done):", workflow.cmd_status(self.root))

        workflow.cmd_reset(self.root)
        self.git("checkout", "-q", self.start_branch)
        self.approve()
        self.run_step({"a.py": "a = 1\n"})
        sha = self.git("rev-parse", "--short=7", "HEAD")
        base = self.state()["baseRef"][:7]
        expected = "\n".join([
            "[specloop] feature run, status=active",
            "spec: spec.md",
            "branch: specloop/my-big-feature-2 (base %s)" % base,
            "phase: implement, iteration 0",
            "steps (1/2 done):",
            "  1  done    %s  Add steps.json schema validation" % sha,
            "  2  active  -        Document it",
        ])
        self.assertEqual(expected, workflow.cmd_status(self.root))

    # --- replanning ----------------------------------------------------------------

    def three_steps(self):
        data = self.two_steps()
        data["steps"].append({"id": 3, "title": "Third step", "description": "More work.",
                              "acceptance": ["It works"], "status": "pending"})
        return data

    def request_replan(self):
        self.write("wip.py", "work = 'in progress'\n")
        self.write_run("replan.md", "Step 2 is wrong: it should be split.\n")
        return workflow.handle_stop(self.root, {})

    def assertWipSurvives(self):
        self.assertTrue(os.path.exists(os.path.join(self.root, "wip.py")))
        self.assertIn("wip.py", self.git("status", "--porcelain", "--untracked-files=all"))

    def test_replan_request_stalls_by_default(self):
        self.approve(implement_command="echo tests-$((40+2)); exit 1")
        out = self.request_replan()
        self.assertNotIn("decision", out)
        for needle in ("replan.md", "steps.json", "/sl-go"):
            self.assertIn(needle, out["systemMessage"])
        state = self.state()
        self.assertEqual(("stalled", "implement", 0), (state["status"], state["phase"], state["iter"]))
        self.assertTrue(state["replanRequested"])
        self.assertFalse(os.path.exists(os.path.join(self.base, "run", "last-failure.md")))
        self.assertIsNone(workflow.handle_stop(self.root, {}))

    def test_stall_resume_adopts_valid_edit_and_restarts_step(self):
        self.approve()
        self.request_replan()
        self.write_steps(lambda d: d["steps"][1].update(title="Split documentation"))
        out = workflow.cmd_go(self.root)
        state = self.state()
        self.assertEqual(("active", "implement", 1, 0), (state["status"], state["phase"], state["stepId"], state["iter"]))
        self.assertEqual("Split documentation", state["approvedSteps"]["steps"][1]["title"])
        self.assertNotIn("replanRequested", state)
        self.assertIn("Add steps.json schema validation", out)
        self.assertTrue(os.path.exists(os.path.join(self.base, "run", "replans", "replan-1.md")))
        self.assertFalse(os.path.exists(os.path.join(self.base, "run", "replan.md")))
        self.assertEqual(state["approvedSteps"], self.steps_file())
        self.assertWipSurvives()

    def test_stall_resume_refuses_invalid_edit_without_changes(self):
        self.approve()
        self.run_step({"a.py": "a = 1\n"})
        self.request_replan()
        self.write_steps(lambda d: d["steps"][0].update(title="Rewritten history"))
        before = self.state_bytes()
        out = workflow.cmd_go(self.root)
        self.assertIn("steps[0] (id 1)", out)
        self.assertEqual(before, self.state_bytes())
        self.assertTrue(os.path.exists(os.path.join(self.base, "run", "replan.md")))

    def test_stall_resume_with_current_step_removed_starts_next_or_completes(self):
        self.approve(self.three_steps())
        self.run_step({"a.py": "a = 1\n"})
        self.request_replan()
        self.write_steps(lambda d: d["steps"].pop(1))
        workflow.cmd_go(self.root)
        self.assertEqual(("active", 3, "implement"),
                         (self.state()["status"], self.state()["stepId"], self.state()["phase"]))

        self.request_replan()
        self.write_steps(lambda d: d["steps"].pop(1))
        out = workflow.cmd_go(self.root)
        self.assertEqual("done", self.state()["status"])
        self.assertIn("specloop/my-big-feature", out)
        self.assertTrue(os.path.exists(os.path.join(self.base, "run", "replans", "replan-2.md")))

    def test_auto_replan_enters_replan_phase(self):
        self.approve(feature={"replan": "auto"})
        out = self.request_replan()
        self.assertEqual("block", out["decision"])
        self.assertIn("feature-replan", out["reason"])
        self.assertIn("- `.claude/specloop/run/replan.md`", out["reason"])
        self.assertIn("## Phase: feature-replan", out["reason"])
        state = self.state()
        self.assertEqual(("active", "feature-replan", 0, 1), (state["status"], state["phase"], state["iter"],
                                                              state["replanCount"]))
        src = os.path.join(self.root, "src", "x.py")
        denied = workflow.handle_pretool(self.root, {"tool_name": "Edit", "tool_input": {"file_path": src}})
        self.assertEqual("deny", denied["hookSpecificOutput"]["permissionDecision"])

    def test_auto_replan_valid_rewrite_returns_to_implement(self):
        self.approve(feature={"replan": "auto"})
        self.request_replan()
        self.write_steps(lambda d: d["steps"][1].update(description="Rewritten by the replan subagent."))
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("Replan adopted", out["reason"])
        state = self.state()
        self.assertEqual(("implement", 1, 0), (state["phase"], state["stepId"], state["iter"]))
        self.assertEqual("Rewritten by the replan subagent.", state["approvedSteps"]["steps"][1]["description"])
        self.assertTrue(os.path.exists(os.path.join(self.base, "run", "replans", "replan-1.md")))
        self.assertWipSurvives()
        self.assertIn("entering phase 'review'", workflow.handle_stop(self.root, {})["reason"])

    def test_auto_replan_done_step_edit_fails_gate(self):
        self.approve(feature={"replan": "auto"})
        self.run_step({"a.py": "a = 1\n"})
        self.request_replan()
        approved = self.state()["approvedSteps"]
        self.write_steps(lambda d: d["steps"][0].update(title="Rewritten history"))
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("steps[0] (id 1)", out["reason"])
        self.assertEqual("feature-replan", self.state()["phase"])
        self.assertEqual(approved, self.state()["approvedSteps"])

    def test_fourth_auto_replan_stalls_at_cap(self):
        self.approve(feature={"replan": "auto", "maxReplans": 3})
        for n in range(1, 4):
            self.request_replan()
            self.assertEqual("feature-replan", self.state()["phase"])
            workflow.handle_stop(self.root, {})
            self.assertEqual("implement", self.state()["phase"])
            self.assertTrue(os.path.exists(os.path.join(self.base, "run", "replans", "replan-%d.md" % n)))
        out = self.request_replan()
        self.assertEqual("stalled", self.state()["status"])
        self.assertIn("3", out["systemMessage"])
        self.assertIn("cap", out["systemMessage"])

    def test_advance_in_replan_phase_adopts_plan(self):
        self.approve(feature={"replan": "auto"})
        self.request_replan()
        self.write_steps(lambda d: d["steps"][1].update(title="Advanced"))
        workflow.cmd_advance(self.root)
        self.assertEqual(("active", "implement"), (self.state()["status"], self.state()["phase"]))
        self.assertEqual("Advanced", self.state()["approvedSteps"]["steps"][1]["title"])

    def test_step_instructions_mention_replan(self):
        self.assertIn(".claude/specloop/run/replan.md", self.approve())
        self.write("a.py", "a = 1\n")
        self.assertIn(".claude/specloop/run/replan.md", workflow.handle_stop(self.root, {})["reason"])

    # --- delegation enforcement --------------------------------------------------

    def edit(self, rel, tool="Edit", agent_id=None, agent_type=None):
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        hook_input = {"tool_name": tool, "tool_input": {key: os.path.join(self.root, rel)}}
        if agent_id:
            hook_input["agent_id"] = agent_id
        if agent_type:
            hook_input["agent_type"] = agent_type
        out = workflow.handle_pretool(self.root, hook_input)
        return out["hookSpecificOutput"]["permissionDecisionReason"] if out else None

    def test_enforce_delegation_denies_main_session_edits(self):
        self.approve(feature={"enforceDelegation": True})
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            reason = self.edit(os.path.join("src", "x.py"), tool=tool)
            self.assertIsNotNone(reason, tool)
            self.assertIn("subagent", reason)

    def test_enforce_delegation_allows_subagent_and_run_dir(self):
        self.approve(feature={"enforceDelegation": True})
        self.assertIsNone(self.edit(os.path.join("src", "x.py"), agent_id="agent-1", agent_type="general-purpose"))
        self.assertIsNone(self.edit(os.path.join(".claude", "specloop", "run", "notes.md")))
        self.assertIsNotNone(self.edit(os.path.join("src", "x.py"), agent_type="my-agent"))

    def test_enforce_delegation_off_by_default_and_ignores_non_delegated_phases(self):
        self.approve()
        self.assertIsNone(self.edit(os.path.join("src", "x.py")))
        workflow.cmd_reset(self.root)
        self.git("checkout", "-q", self.start_branch)
        self.approve(feature={"enforceDelegation": True, "stepPhases": {"implement": {"delegate": False}}})
        self.assertIsNone(self.edit(os.path.join("src", "x.py")))

    def test_enforce_delegation_does_not_block_bash(self):
        self.approve(feature={"enforceDelegation": True})
        self.assertIsNone(self.bash("sed -i s/a/b/ src/x.py"))

    # --- end to end ----------------------------------------------------------------

    def main(self, *argv, stdin=""):
        import contextlib
        import io
        old_stdin, sys.stdin = sys.stdin, io.StringIO(stdin)
        os.environ["CLAUDE_PROJECT_DIR"] = self.root
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                self.assertEqual(0, workflow.main(list(argv)))
        finally:
            sys.stdin = old_stdin
            del os.environ["CLAUDE_PROJECT_DIR"]
        return out.getvalue()

    def stop(self):
        out = self.main("stop", stdin="{}")
        return json.loads(out) if out.strip() else None

    def test_end_to_end_feature_run_through_entry_points(self):
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": "exit 0"}}}})
        base = self.git("rev-parse", "HEAD")
        self.assertIn("feature mode", self.main("init-feature", "spec.md"))
        self.assertIn("feature planning", self.main("go"))
        self.assertEqual("specloop/my-big-feature", self.current_branch())

        self.write_run("plan.md", "# Plan\n")
        self.write_run("steps.json", json.dumps(self.two_steps()))
        self.assertIn("/sl-go", self.stop()["systemMessage"])
        self.assertIn('feature step 1/2 "Add steps.json schema validation"', self.main("go"))

        for name in ("a.py", "b.md"):
            self.write(name, "content of %s\n" % name)
            self.assertIn("entering phase 'review'", self.stop()["reason"])
            self.write_run("review.md", "Fine.\nVERDICT: APPROVE\n")
            final = self.stop()

        self.assertIn("All 2 steps are finished on branch specloop/my-big-feature", final["systemMessage"])
        self.assertEqual("specloop/my-big-feature", self.current_branch())
        self.assertEqual(["docs: Document it", "feat: Add steps.json schema validation"],
                         self.git("log", "--format=%s", base + "..HEAD").splitlines())
        status = self.main("status")
        self.assertIn("[specloop] feature run, status=done", status)
        self.assertIn("steps (2/2 done):", status)
        for sha in self.git("rev-list", base + "..HEAD").splitlines():
            self.assertIn(sha[:7], status)
        self.assertEqual("done", self.state()["status"])
        self.assertIsNone(self.stop())

    def test_replan_file_ignored_during_planning(self):
        self.start()
        self.write_run("replan.md", "not yet")
        self.pass_planning()
        self.assertTrue(self.state()["awaitingApproval"])


class UpdateHelpersTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.base = os.path.join(self.root, ".claude", "specloop")
        shutil.copytree(KIT, self.base)
        self.clone = os.path.join(self.root, "clone")
        os.makedirs(self.clone)
        self.saved_env = {k: os.environ.pop(k, None) for k in ("CI", "SPECLOOP_DISABLED")}

    def tearDown(self):
        for key, value in self.saved_env.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def set_versions(self, installed, clone=None):
        self.write(os.path.join(self.base, "VERSION"), installed + "\n")
        if clone is not None:
            self.write(os.path.join(self.clone, "VERSION"), clone + "\n")

    def source(self):
        return {"path": self.clone.replace("\\", "/"), "remote": "origin", "branch": "main"}

    def test_defaults_contain_update_block(self):
        with open(os.path.join(KIT, "defaults.json"), encoding="utf-8") as f:
            defaults = json.load(f)
        self.assertEqual({"mode": "notify", "intervalHours": 24, "timeoutSec": 20}, defaults["update"])
        self.assertEqual("notify", workflow.load_config(self.root)["update"]["mode"])

    def test_parse_version(self):
        self.assertEqual((0, 2, 0), workflow.parse_version("0.2.0"))
        self.assertEqual((12, 34, 56), workflow.parse_version(" 12.34.56\n"))
        for text in ("", "1.2", "1.2.3.4", "v1.2.3", "1.2.x", "1.2.3-beta", "abc", None, 3):
            self.assertIsNone(workflow.parse_version(text), repr(text))

    def test_is_major_update(self):
        self.assertTrue(workflow.is_major_update("0.2.0", "0.3.0"))
        self.assertTrue(workflow.is_major_update("1.2.0", "2.0.0"))
        self.assertTrue(workflow.is_major_update("0.9.1", "1.0.0"))
        self.assertFalse(workflow.is_major_update("1.2.0", "1.3.0"))
        self.assertFalse(workflow.is_major_update("0.2.0", "0.2.1"))
        self.assertFalse(workflow.is_major_update("1.2.0", "1.2.5"))
        self.assertFalse(workflow.is_major_update("garbage", "2.0.0"))

    def test_read_manifest_source(self):
        manifest = os.path.join(self.base, "manifest.json")
        self.assertIsNone(workflow.read_manifest_source(self.root))          # no manifest
        self.write(manifest, json.dumps({"commands": [], "settings": "x"}))
        self.assertIsNone(workflow.read_manifest_source(self.root))          # old install
        self.write(manifest, "{not json")
        self.assertIsNone(workflow.read_manifest_source(self.root))          # unreadable
        self.write(manifest, json.dumps({"source": self.source()}))
        self.assertEqual(self.source(), workflow.read_manifest_source(self.root))

    def test_update_state_round_trip(self):
        path = os.path.join(self.base, "update.json")
        self.assertEqual({}, workflow.load_update_state(self.root))
        self.write(path, "{broken")
        self.assertEqual({}, workflow.load_update_state(self.root))
        self.write(path, "[1, 2]")
        self.assertEqual({}, workflow.load_update_state(self.root))
        data = {"checkedAt": "2026-10-30T10:00:00+00:00", "remote": "0.3.0", "skipped": None}
        workflow.save_update_state(self.root, data)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(json.dumps(data, indent=2) + "\n", text)
        self.assertEqual(data, workflow.load_update_state(self.root))

    def test_update_mode(self):
        self.assertEqual("notify", workflow.update_mode(workflow.load_config(self.root)))
        for mode in ("off", "notify", "auto"):
            self.assertEqual(mode, workflow.update_mode({"update": {"mode": mode}}))
        self.assertEqual("notify", workflow.update_mode({"update": {"mode": "sometimes"}}))
        self.assertEqual("notify", workflow.update_mode({"update": None}))
        self.assertEqual("notify", workflow.update_mode({}))

        os.environ["CI"] = "true"
        self.assertEqual("off", workflow.update_mode({"update": {"mode": "auto"}}))
        del os.environ["CI"]
        os.environ["SPECLOOP_DISABLED"] = "1"
        self.assertEqual("off", workflow.update_mode({"update": {"mode": "auto"}}))
        del os.environ["SPECLOOP_DISABLED"]

    def test_local_config_overrides_update_mode(self):
        self.write(os.path.join(self.base, "config.json"), json.dumps({"update": {"mode": "notify"}}))
        self.write(os.path.join(self.base, "config.local.json"), json.dumps({"update": {"mode": "auto"}}))
        config = workflow.load_config(self.root)
        self.assertEqual("auto", workflow.update_mode(config))
        self.assertEqual(24, config["update"]["intervalHours"])

    def test_available_version_prefers_highest(self):
        self.set_versions("0.2.0", clone="0.3.0")
        self.assertEqual("0.3.0", workflow.available_version(self.root, self.source(), {}))
        self.assertEqual("0.4.0", workflow.available_version(self.root, self.source(), {"remote": "0.4.0"}))
        self.assertEqual("0.3.0", workflow.available_version(self.root, self.source(), {"remote": "0.2.5"}))
        self.set_versions("0.2.0", clone="0.2.0")
        self.assertEqual("0.2.1", workflow.available_version(self.root, self.source(), {"remote": "0.2.1"}))

    def test_available_version_none_when_not_newer(self):
        self.set_versions("0.2.0", clone="0.2.0")
        self.assertIsNone(workflow.available_version(self.root, self.source(), {}))
        self.assertIsNone(workflow.available_version(self.root, self.source(), {"remote": "0.1.9"}))
        self.set_versions("0.2.0", clone="not a version")
        self.assertIsNone(workflow.available_version(self.root, self.source(), {"remote": "garbage"}))
        shutil.rmtree(self.clone)
        self.assertIsNone(workflow.available_version(self.root, self.source(), {}))
        self.assertEqual("0.3.0", workflow.available_version(self.root, self.source(), {"remote": "0.3.0"}))
        self.assertIsNone(workflow.available_version(self.root, None, {}))

    def test_available_version_without_installed_version(self):
        self.write(os.path.join(self.clone, "VERSION"), "0.3.0\n")
        self.assertIsNone(workflow.available_version(self.root, self.source(), {"remote": "0.3.0"}))

    def test_available_version_ignores_skipped_but_not_later(self):
        self.set_versions("0.2.0", clone="0.3.0")
        self.assertIsNone(workflow.available_version(self.root, self.source(), {"skipped": "0.3.0"}))
        self.assertIsNone(workflow.available_version(self.root, self.source(),
                                                     {"skipped": "0.3.0", "remote": "0.2.9"}))
        self.assertEqual("0.3.1", workflow.available_version(self.root, self.source(),
                                                             {"skipped": "0.3.0", "remote": "0.3.1"}))
        self.set_versions("0.2.0", clone="0.4.0")
        self.assertEqual("0.4.0", workflow.available_version(self.root, self.source(), {"skipped": "0.3.0"}))

    def test_display_url(self):
        cases = {
            "https://github.com/someone/specloop-fork.git": "github.com/someone/specloop-fork",
            "https://user:token@github.com/someone/specloop-fork": "github.com/someone/specloop-fork",
            "git@github.com:someone/specloop-fork.git": "github.com/someone/specloop-fork",
            "ssh://git@gitlab.example.com/team/specloop.git": "gitlab.example.com/team/specloop",
            "https://github.com/someone/specloop/": "github.com/someone/specloop",
            "/home/me/remote.git": "/home/me/remote",
            "C:/repos/remote.git": "C:/repos/remote",
            "": "",
        }
        for url, expected in cases.items():
            self.assertEqual(expected, workflow.display_url(url), url)
        self.assertEqual("", workflow.display_url(None))


@unittest.skipUnless(gitfixture.git_available(), "git not available")
class UpdateCheckTest(unittest.TestCase):
    """engine.py update-check: the background network check, run in-process here."""

    def setUp(self):
        self.saved_env = {k: os.environ.pop(k, None) for k in ("CI", "SPECLOOP_DISABLED")}
        self.addCleanup(self.restore_env)
        self.fx = gitfixture.GitFixture()
        self.addCleanup(self.fx.cleanup)
        result = self.fx.run_install("--test-command", "exit 0")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.root = self.fx.target
        self.base = os.path.join(self.root, ".claude", "specloop")
        self.lock = os.path.join(self.base, "update.lock")

    def restore_env(self):
        for key, value in self.saved_env.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def check(self):
        """Run update-check, assert it prints nothing and releases the lock; return update.json."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            workflow.cmd_update_check(self.root)
        self.assertEqual("", out.getvalue())
        self.assertEqual("", err.getvalue())
        self.assertFalse(os.path.exists(self.lock))
        return workflow.load_update_state(self.root)

    def assert_failed(self, data, installed=None):
        self.assertTrue(data.get("error"))
        self.assertTrue(data.get("checkedAt"))
        self.assertEqual(installed or self.fx.read_version(), data.get("installed"))

    def write_source(self, source):
        path = os.path.join(self.base, "manifest.json")
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
        if source is None:
            manifest.pop("source", None)
        else:
            manifest["source"] = source
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

    def test_records_remote_version_and_commit(self):
        announced = {"version": "0.1.0", "at": "2026-01-01T00:00:00+00:00"}
        workflow.save_update_state(self.root, {"skipped": "0.1.0", "announced": announced})
        installed = self.fx.read_version()
        commit = self.fx.bump_remote_version("0.9.0")
        data = self.check()
        self.assertEqual("0.9.0", data["remote"])
        self.assertEqual(commit, data["remoteCommit"])
        self.assertEqual(installed, data["installed"])
        self.assertEqual(installed, data["clone"])
        self.assertIsNone(data.get("error"))
        self.assertTrue(data["checkedAt"].endswith("+00:00"))
        self.assertEqual("0.1.0", data["skipped"])
        self.assertEqual(announced, data["announced"])
        self.assertEqual(installed, self.fx.read_version())  # a check never changes the clone

    def test_success_clears_previous_error(self):
        workflow.save_update_state(self.root, {"error": "old failure"})
        data = self.check()
        self.assertIsNone(data.get("error"))
        self.assertEqual(self.fx.read_version(), data["remote"])

    def test_unreachable_remote_records_error(self):
        shutil.rmtree(self.fx.remote, onerror=gitfixture._force_remove)
        self.assert_failed(self.check())

    def test_deleted_clone_records_error(self):
        installed = self.fx.read_version()
        shutil.rmtree(self.fx.clone, onerror=gitfixture._force_remove)
        data = self.check()
        self.assert_failed(data, installed)
        self.assertIsNone(data.get("clone"))

    def test_missing_git_records_error(self):
        saved = workflow.GIT
        workflow.GIT = "specloop-no-such-git-executable"
        try:
            data = self.check()
        finally:
            workflow.GIT = saved
        self.assert_failed(data)
        self.assertIn("git", data["error"])

    def test_unparseable_remote_version_records_error(self):
        self.fx.bump_remote_version("not-a-version")
        data = self.check()
        self.assert_failed(data)
        self.assertIn("not-a-version", data["error"])

    def test_not_checkable_or_missing_source_records_error(self):
        self.write_source({"path": self.fx.clone.replace("\\", "/"), "version": "0.2.0",
                           "checkable": False, "reason": "no upstream"})
        self.assertIn("no upstream", self.check()["error"])
        self.write_source(None)
        self.assert_failed(self.check())

    def test_fresh_lock_makes_check_a_no_op(self):
        workflow.save_update_state(self.root, {"skipped": "0.1.0"})
        self.fx.bump_remote_version("0.9.0")
        with open(self.lock, "w") as f:
            f.write("12345 2026-01-01T00:00:00+00:00\n")
        before = workflow.load_update_state(self.root)
        workflow.cmd_update_check(self.root)
        self.assertEqual(before, workflow.load_update_state(self.root))
        self.assertTrue(os.path.exists(self.lock))  # another check's lock is left alone

    def test_stale_lock_is_replaced(self):
        self.fx.bump_remote_version("0.9.0")
        with open(self.lock, "w") as f:
            f.write("12345 2020-01-01T00:00:00+00:00\n")
        old = time.time() - 3600
        os.utime(self.lock, (old, old))
        self.assertEqual("0.9.0", self.check()["remote"])

    def test_lock_is_released_when_the_check_raises(self):
        saved = workflow._run_update_check

        def boom(root, config):
            raise RuntimeError("boom")
        workflow._run_update_check = boom
        try:
            self.check()
        finally:
            workflow._run_update_check = saved

    def test_main_update_check_prints_nothing(self):
        os.environ["CLAUDE_PROJECT_DIR"] = self.root
        self.addCleanup(os.environ.pop, "CLAUDE_PROJECT_DIR", None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(0, workflow.main(["update-check"]))
        self.assertEqual(("", ""), (out.getvalue(), err.getvalue()))
        self.assertTrue(workflow.load_update_state(self.root).get("checkedAt"))


class SpawnUpdateCheckTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(setattr, subprocess, "Popen", subprocess.Popen)

    def test_starts_detached_engine_with_null_streams(self):
        calls = []

        def fake_popen(args, **kwargs):
            calls.append((args, kwargs))
        subprocess.Popen = fake_popen
        self.assertTrue(workflow.spawn_update_check(self.root))
        self.assertEqual(1, len(calls))
        args, kwargs = calls[0]
        self.assertEqual(sys.executable, args[0])
        self.assertEqual("engine.py", os.path.basename(args[1]))
        self.assertEqual("update-check", args[2])
        self.assertEqual(self.root, kwargs["cwd"])
        self.assertEqual(self.root, kwargs["env"]["CLAUDE_PROJECT_DIR"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(subprocess.DEVNULL, kwargs[stream])
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            self.assertEqual(flags, kwargs["creationflags"] & flags)
        else:
            self.assertTrue(kwargs["start_new_session"])

    def test_spawn_failure_is_swallowed(self):
        def failing_popen(args, **kwargs):
            raise OSError("cannot start")
        subprocess.Popen = failing_popen
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(workflow.spawn_update_check(self.root))
        self.assertIn("cannot start", err.getvalue())


if __name__ == "__main__":
    unittest.main()
