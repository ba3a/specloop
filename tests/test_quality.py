"""Tests for quality gates: the `quality` configuration block and its validation."""

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(os.path.dirname(HERE), "kit", "specloop")
sys.path.insert(0, KIT)

import engine as workflow  # noqa: E402

sys.path.insert(0, HERE)
import gitfixture  # noqa: E402

# The two example gates of the spec (section 3.2).
COVERAGE = {"command": "./gradlew koverLog", "metric": {"pattern": "coverage: ([0-9.]+)%", "min": 80}}
ISSUES = {"command": "./gradlew detekt", "passExitCodes": [0, 2],
          "metric": {"file": "build/reports/detekt/detekt.json", "json": "summary.total", "max": 0}}


class QualityProject(unittest.TestCase):
    """A throwaway project with the kit installed and no git repository."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(self.root)
        self.base = os.path.join(self.root, ".claude", "specloop")
        shutil.copytree(KIT, self.base)
        with open(os.path.join(self.root, "spec.md"), "w") as f:
            f.write("# Spec\nDo the thing.")

    def tearDown(self):
        if self.ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = self.ceiling
        shutil.rmtree(self.root, ignore_errors=True)

    def write_json(self, name, data):
        with open(os.path.join(self.base, name), "w") as f:
            json.dump(data, f)

    def set_config(self, config):
        self.write_json("config.json", config)

    def set_gates(self, gates, **quality):
        self.set_config({"quality": dict(quality, gates=gates)})

    def config(self):
        return workflow.load_config(self.root)

    def errors(self, gates=None, **quality):
        if gates is not None or quality:
            self.set_gates(gates or {}, **quality)
        return workflow.validate_quality(self.root, self.config())

    def assertError(self, fragment, gates=None, **quality):
        errors = self.errors(gates, **quality)
        self.assertTrue(any(fragment in e for e in errors), "%r not in %r" % (fragment, errors))
        return errors

    def git_init(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)

    def state_bytes(self):
        with open(os.path.join(self.base, "state.json"), "rb") as f:
            return f.read()


class DefaultsTest(QualityProject):

    def test_defaults_have_the_quality_block(self):
        with open(os.path.join(KIT, "defaults.json"), encoding="utf-8") as f:
            defaults = json.load(f)
        self.assertEqual({"timeoutSec": 1800, "failFast": False, "legacy": "baseline", "gates": {}},
                         defaults["quality"])

    def test_default_block_is_valid(self):
        self.assertEqual([], self.errors())
        self.assertEqual({}, workflow.quality_gates(self.config()))

    def test_spec_example_gates_are_valid(self):
        self.assertEqual([], self.errors({"coverage": COVERAGE, "issues": ISSUES}))

    def test_project_without_quality_block_starts_as_before(self):
        workflow.cmd_init(self.root, "spec.md")
        out = workflow.cmd_go(self.root)
        self.assertNotIn("quality", out)
        self.assertEqual("active", workflow.load_state(self.root)["status"])

    def test_config_without_quality_key_is_valid(self):
        self.assertEqual([], workflow.validate_quality(self.root, {"phases": {}}))


class MergeTest(QualityProject):

    def test_gates_keep_declared_order(self):
        names = ["zeta", "alpha", "mid-1", "b_2"]
        self.set_gates({n: {"command": "exit 0"} for n in names})
        self.assertEqual(names, list(workflow.quality_gates(self.config())))

    def test_null_in_config_local_removes_a_gate(self):
        self.set_gates({"lint": {"command": "exit 0"}, "tests": {"command": "exit 0"}})
        self.write_json("config.local.json", {"quality": {"gates": {"lint": None}}})
        config = self.config()
        self.assertEqual(["tests"], list(workflow.quality_gates(config)))
        self.assertEqual([], workflow.validate_quality(self.root, config))

    def test_config_local_changes_one_key_of_one_gate(self):
        self.set_gates({"lint": {"command": "exit 0", "hint": "keep"}, "tests": {"command": "exit 0"}})
        self.write_json("config.local.json", {"quality": {"gates": {"lint": {"level": "warn"}}}})
        gates = workflow.quality_gates(self.config())
        self.assertEqual({"command": "exit 0", "hint": "keep", "level": "warn"}, gates["lint"])
        self.assertEqual({"command": "exit 0"}, gates["tests"])
        self.assertEqual(["lint", "tests"], list(gates))

    def test_config_local_adds_a_gate_after_the_shared_ones(self):
        self.set_gates({"lint": {"command": "exit 0"}})
        self.write_json("config.local.json", {"quality": {"gates": {"mine": {"command": "exit 0"}}}})
        self.assertEqual(["lint", "mine"], list(workflow.quality_gates(self.config())))


class BlockErrorsTest(QualityProject):

    def test_quality_not_an_object(self):
        self.set_config({"quality": ["x"]})
        self.assertEqual(["quality must be an object"], self.errors())

    def test_unknown_quality_key(self):
        self.assertError("unknown key 'timeout'", {}, timeout=5)

    def test_quality_timeout_type(self):
        for value in (0, -1, "10", 1.5, True):
            self.assertError("quality.timeoutSec", {}, timeoutSec=value)

    def test_fail_fast_type(self):
        self.assertError("quality.failFast", {}, failFast="yes")

    def test_unknown_quality_legacy(self):
        self.assertError("quality.legacy", {}, legacy="strict")

    def test_gates_not_an_object(self):
        self.set_config({"quality": {"gates": ["a"]}})
        self.assertError("quality.gates must be an object")

    def test_invalid_gate_name(self):
        for name in ("-lint", "a b", "a/b", "_x", "x.y", "ä"):
            self.assertError("gate name %r" % name, {name: {"command": "exit 0"}})
        self.assertEqual([], self.errors({"A9_b-c": {"command": "exit 0"}}))


class GateErrorsTest(QualityProject):

    def gate(self, **keys):
        return {"g": dict({"command": "exit 0"}, **keys)}

    def test_gate_not_an_object(self):
        self.assertError("quality.gates.g must be an object", {"g": "exit 0"})

    def test_unknown_gate_key(self):
        self.assertError("unknown key 'onFail'", self.gate(onFail="review"))

    def test_command_required(self):
        for gate in ({}, {"command": ""}, {"command": "  "}, {"command": ["exit", "0"]}):
            self.assertError("quality.gates.g.command is required", {"g": gate})

    def test_phases_type(self):
        for value in ("implement", [1], [""], None):
            self.assertError("quality.gates.g.phases must be a list", self.gate(phases=value))

    def test_unknown_phase(self):
        self.assertError("names 'docs', which is in neither", self.gate(phases=["docs"]))

    def test_step_phase_is_known(self):
        self.set_config({"feature": {"stepPhases": {"verify": {"next": "review"}}},
                         "quality": {"gates": self.gate(phases=["verify"])}})
        self.assertEqual([], self.errors())

    def test_phase_with_allow_edits(self):
        errors = self.assertError("sets allowEdits", self.gate(phases=["review"]))
        self.assertTrue(any("phases.review" in e for e in errors))
        self.assertTrue(any("feature.stepPhases.review" in e for e in errors))

    def test_default_phase_must_exist(self):
        self.set_config({"phases": {"implement": None, "plan": {"next": "done"}},
                         "feature": {"stepPhases": {"implement": None}},
                         "quality": {"gates": self.gate()}})
        self.assertError("names 'implement', which is in neither")

    def test_unknown_steps(self):
        self.assertError("quality.gates.g.steps must be one of", self.gate(steps="first"))

    def test_unknown_level(self):
        self.assertError("quality.gates.g.level must be one of", self.gate(level="error"))

    def test_unknown_legacy(self):
        self.assertError("quality.gates.g.legacy must be one of", self.gate(legacy="none"))

    def test_timeout_type(self):
        for value in (0, -5, "60", 2.5, False):
            self.assertError("quality.gates.g.timeoutSec", self.gate(timeoutSec=value))

    def test_hint_type(self):
        self.assertError("quality.gates.g.hint must be a string", self.gate(hint=["x"]))

    def test_pass_exit_codes_type(self):
        for value in (0, [], ["0"], [True], [1.0]):
            self.assertError("passExitCodes must be a non-empty list", self.gate(passExitCodes=value))

    def test_pass_exit_codes_never_126_or_127(self):
        for code in (126, 127):
            self.assertError("may not contain 126 or 127", self.gate(passExitCodes=[0, code]))

    def test_glob_and_protect_types(self):
        for key in ("changedGlob", "protect"):
            for value in ("*.kt", [3], [""]):
                self.assertError("quality.gates.g.%s must be a list" % key, self.gate(**{key: value}))

    def test_valid_gate_with_every_simple_key(self):
        self.assertEqual([], self.errors(self.gate(
            phases=["implement"], steps="last", level="warn", timeoutSec=60, hint="h", passExitCodes=[0, 1],
            legacy="baseline", changedGlob=["*.py"], protect=["ruff.toml", "config/"])))


class MetricErrorsTest(QualityProject):

    def metric(self, **metric):
        return {"g": {"command": "exit 0", "metric": metric}}

    def test_metric_not_an_object(self):
        self.assertError("quality.gates.g.metric must be an object", {"g": {"command": "x", "metric": "80"}})

    def test_metric_needs_exactly_one_source(self):
        self.assertError("exactly one of 'pattern' and 'json'", self.metric(min=1))
        self.assertError("exactly one of 'pattern' and 'json'", self.metric(pattern="(1)", json="a", min=1))

    def test_metric_needs_a_limit(self):
        self.assertError("at least one of 'min' and 'max'", self.metric(json="a.b"))

    def test_metric_limits_are_numbers(self):
        self.assertError("metric.min must be a number", self.metric(json="a", min="80"))
        self.assertError("metric.max must be a number", self.metric(json="a", max=True))

    def test_metric_invalid_regex(self):
        self.assertError("metric.pattern is not a valid regular expression", self.metric(pattern="([0-9", min=1))

    def test_metric_pattern_needs_a_group(self):
        self.assertError("needs a group", self.metric(pattern="[0-9]+", min=1))

    def test_metric_unknown_key_and_types(self):
        self.assertError("metric has an unknown key 'path'", self.metric(json="a", min=1, path="x"))
        self.assertError("metric.file must be a non-empty string", self.metric(json="a", min=1, file=3))
        self.assertError("metric.json must be a non-empty", self.metric(json="", min=1))

    def test_ratchet_gate_needs_exactly_one_limit(self):
        gate = {"g": {"command": "x", "legacy": "ratchet", "metric": {"json": "a", "min": 1, "max": 2}}}
        self.assertError("exactly one of 'min' (higher is better) and 'max'", gate)
        gate["g"]["metric"].pop("max")
        self.assertEqual([], self.errors(gate))

    def test_ratchet_from_quality_default(self):
        gate = {"g": {"command": "x", "metric": {"json": "a", "min": 1, "max": 2}}}
        self.assertError("uses legacy \"ratchet\"", gate, legacy="ratchet")
        self.assertEqual([], self.errors(gate, legacy="baseline"))

    def test_gate_level_ratchet_needs_a_metric(self):
        self.assertError("needs a metric", {"g": {"command": "x", "legacy": "ratchet"}})
        # Inherited from quality.legacy, a gate without a metric is judged by its exit code alone.
        self.assertEqual([], self.errors({"g": {"command": "x"}}, legacy="ratchet"))


class ForbidErrorsTest(QualityProject):

    def setUp(self):
        super().setUp()
        self.git_init()

    def forbid(self, *entries):
        return {"g": {"command": "exit 0", "forbid": list(entries)}}

    def test_valid_forbid_entries(self):
        self.assertEqual([], self.errors(self.forbid(
            "# noqa", {"pattern": "@Suppress\\(", "glob": ["*.kt", "*.kts"], "message": "Fix it."})))

    def test_forbid_not_a_list(self):
        self.assertError("quality.gates.g.forbid must be a list", {"g": {"command": "x", "forbid": "# noqa"}})

    def test_forbid_invalid_regex(self):
        self.assertError("forbid[0] is not a valid regular expression", self.forbid("(unclosed"))
        self.assertError("forbid[1].pattern is not a valid regular expression",
                         self.forbid("ok", {"pattern": "[x"}))

    def test_forbid_entry_types(self):
        self.assertError("forbid[0] must be a non-empty string", self.forbid(3))
        self.assertError("forbid[0].pattern must be a non-empty string", self.forbid({"glob": ["*.py"]}))
        self.assertError("forbid[0].glob must be a list", self.forbid({"pattern": "x", "glob": "*.py"}))
        self.assertError("forbid[0].message must be a string", self.forbid({"pattern": "x", "message": 1}))
        self.assertError("forbid[0] has an unknown key 'files'", self.forbid({"pattern": "x", "files": []}))


class GitRequirementTest(QualityProject):

    def test_changed_files_outside_git(self):
        for command in ("lint {changed_files}", "lint --list {changed_files_file}"):
            self.assertError("need a git work tree", {"g": {"command": command}})

    def test_forbid_outside_git(self):
        self.assertError("need a git work tree", {"g": {"command": "exit 0", "forbid": ["# noqa"]}})

    def test_protect_does_not_need_git(self):
        self.assertEqual([], self.errors({"g": {"command": "exit 0", "protect": ["ruff.toml"]}}))

    def test_changed_files_and_forbid_in_git(self):
        self.git_init()
        self.assertEqual([], self.errors({"g": {"command": "lint {changed_files}", "forbid": ["# noqa"]}}))


class NeverRaisesTest(QualityProject):

    def test_malformed_input_at_every_level(self):
        bad_values = [None, True, 0, 1.5, "x", [], [None], {}, {"x": None}, ["a", 1]]
        shapes = []
        for value in bad_values:
            shapes.append({"quality": value})
            shapes.append({"quality": {"gates": value}})
            shapes.append({"quality": {"timeoutSec": value, "failFast": value, "legacy": value}})
            shapes.append({"quality": {"gates": {"g": value}}})
            for key in workflow.GATE_KEYS:
                shapes.append({"quality": {"gates": {"g": {"command": "exit 0", key: value}}}})
            for key in workflow.METRIC_KEYS:
                shapes.append({"quality": {"gates": {"g": {"command": "x", "metric": {"json": "a", key: value}}}}})
            for key in workflow.FORBID_KEYS:
                shapes.append({"quality": {"gates": {"g": {"command": "x", "forbid": [{"pattern": "a", key: value}]}}}})
            shapes.append({"quality": {"gates": {"g": {"command": "x", "legacy": "ratchet", "metric": value}}}})
            shapes.append({"phases": value, "quality": {"gates": {"g": {"command": "x"}}}})
            shapes.append({"phases": {"implement": value}, "feature": value,
                           "quality": {"gates": {"g": {"command": "x"}}}})
        shapes.extend([None, [], "config", {"quality": {"gates": {"g": {"command": "x", "forbid": [[]]}}}}])
        for config in shapes:
            errors = workflow.validate_quality(self.root, copy.deepcopy(config))
            self.assertIsInstance(errors, list, config)
            self.assertTrue(all(isinstance(e, str) for e in errors), config)

    def test_quality_gates_tolerates_malformed_input(self):
        for config in (None, [], {"quality": None}, {"quality": []}, {"quality": {"gates": "x"}}):
            self.assertEqual({}, workflow.quality_gates(config))


class GoRefusesTest(QualityProject):

    BAD = {"broken": {"command": "", "level": "fatal"}}

    def test_go_refuses_to_start(self):
        workflow.cmd_init(self.root, "spec.md")
        before = self.state_bytes()
        self.set_gates(self.BAD)
        out = workflow.cmd_go(self.root)
        self.assertIn("quality configuration is invalid", out)
        self.assertIn("quality.gates.broken.command", out)
        self.assertIn("quality.gates.broken.level", out)
        self.assertIn("/sl-go", out)
        self.assertEqual(before, self.state_bytes())
        self.assertEqual("ready", workflow.load_state(self.root)["status"])

    def test_go_refuses_to_resume(self):
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        workflow.cmd_pause(self.root)
        before = self.state_bytes()
        self.write_json("config.local.json", {"quality": {"gates": self.BAD}})
        out = workflow.cmd_go(self.root)
        self.assertIn("quality configuration is invalid", out)
        self.assertEqual(before, self.state_bytes())
        self.assertEqual("paused", workflow.load_state(self.root)["status"])

    def test_go_starts_once_fixed(self):
        workflow.cmd_init(self.root, "spec.md")
        self.set_gates(self.BAD)
        workflow.cmd_go(self.root)
        self.write_json("config.local.json", {"quality": {"gates": {"broken": None}}})
        workflow.cmd_go(self.root)
        self.assertEqual("active", workflow.load_state(self.root)["status"])

    def test_feature_go_refuses_before_creating_a_branch(self):
        self.git_init()
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        before = self.state_bytes()
        self.set_gates(self.BAD)
        out = workflow.cmd_go(self.root)
        self.assertIn("quality configuration is invalid", out)
        self.assertEqual(before, self.state_bytes())
        self.assertNotIn("branch", workflow.load_state(self.root))


def lines(prefix, count):
    """A shell command that prints `count` numbered lines."""
    return "for i in $(seq 1 %d); do echo %s-$i; done" % (count, prefix)


class GateProject(QualityProject):
    """Helpers for tests that run gates in a workflow. Holds no tests itself."""

    def configure(self, gates, quality=None, **top):
        config = dict(top, quality=dict(quality or {}, gates=gates))
        self.set_config(config)

    def start_implement(self):
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        with open(self.run_path("plan.md"), "w") as f:
            f.write("- [ ] step")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'implement'", out["reason"])

    def state(self):
        return workflow.load_state(self.root)

    def run_path(self, *parts):
        return os.path.join(self.base, "run", *parts)

    def exists(self, name):
        return os.path.exists(os.path.join(self.root, name))

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()


class GateRunTest(GateProject):
    """Gates run after a phase's check passes, in single mode."""

    def test_failing_block_gate_keeps_implement(self):
        self.configure({"tests": {"command": "echo boom-output; exit 1"}})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("boom-output", out["reason"])
        self.assertIn("Quality gates failed: tests", out["reason"])
        state = self.state()
        self.assertEqual("implement", state["phase"])
        self.assertEqual("fail", state["qualityResults"]["tests"]["result"])
        self.assertEqual(1, state["qualityResults"]["tests"]["exitCode"])

    def test_passing_gate_enters_review_and_records_result(self):
        self.configure({"tests": {"command": "exit 0"}})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'review'", out["reason"])
        result = self.state()["qualityResults"]["tests"]
        self.assertEqual("pass", result["result"])
        self.assertEqual(0, result["exitCode"])
        self.assertEqual("implement", result["phase"])
        self.assertIsNone(result["stepId"])
        self.assertIsInstance(result["durationSec"], (int, float))
        self.assertGreaterEqual(result["durationSec"], 0)
        self.assertTrue(result["finishedAt"])
        self.assertFalse(os.path.exists(self.run_path("last-failure.md")))

    def test_all_failures_reported_in_one_message_with_one_iteration(self):
        self.configure({"lint": {"command": "echo lint-output; exit 1", "hint": "Fix lint properly."},
                        "tests": {"command": "echo tests-output; exit 2", "hint": "Do not skip tests."}})
        self.start_implement()
        before = self.state()["iter"]
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        for text in ("Quality gates failed: lint, tests", "lint-output", "tests-output", "Fix lint properly.",
                     "Do not skip tests.", "exit code 2"):
            self.assertIn(text, out["reason"])
        state = self.state()
        self.assertEqual(before + 1, state["iter"])
        self.assertEqual(["fail", "fail"], [state["qualityResults"][n]["result"] for n in ("lint", "tests")])

    def test_first_line_names_every_failed_gate_in_a_delegated_phase(self):
        self.configure({"lint": {"command": "exit 1"}, "tests": {"command": "exit 1"}},
                       phases={"implement": {"delegate": True}})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertIn("not complete yet: Quality gates failed: lint, tests. Full details", out["reason"])

    def test_fail_fast_stops_at_the_first_failing_block_gate(self):
        self.configure({"lint": {"command": "exit 1"}, "tests": {"command": "echo ran > marker; exit 0"}},
                       {"failFast": True})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertFalse(self.exists("marker"))
        self.assertEqual(["lint"], list(self.state()["qualityResults"]))
        self.assertFalse(os.path.exists(self.run_path("quality", "tests.log")))

    def test_fail_fast_continues_after_a_failing_warn_gate(self):
        self.configure({"style": {"command": "exit 1", "level": "warn"},
                        "tests": {"command": "echo ran > marker; exit 0"}}, {"failFast": True})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'review'", out["reason"])
        self.assertTrue(self.exists("marker"))

    def test_without_fail_fast_every_gate_runs(self):
        self.configure({"lint": {"command": "exit 1"}, "tests": {"command": "echo ran > marker; exit 0"}})
        self.start_implement()
        workflow.handle_stop(self.root, {})
        self.assertTrue(self.exists("marker"))
        self.assertEqual("pass", self.state()["qualityResults"]["tests"]["result"])

    def test_failing_warn_gate_does_not_block_and_is_reported(self):
        self.configure({"style": {"command": "echo style-output; exit 1", "level": "warn", "hint": "Style hint."},
                        "tests": {"command": "exit 0"}})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'review'", out["reason"])
        self.assertIn("style", out["reason"])
        self.assertIn("warn", out["reason"])
        self.assertIn("quality/style.log", out["reason"])
        self.assertEqual("review", self.state()["phase"])
        self.assertEqual("warn", self.state()["qualityResults"]["style"]["result"])

    def test_warn_gate_is_listed_beside_a_blocking_failure(self):
        self.configure({"style": {"command": "echo style-output; exit 1", "level": "warn"},
                        "tests": {"command": "exit 1"}})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertIn("Quality gates failed: tests\n", out["reason"])
        self.assertIn("style-output", out["reason"])

    def test_repeated_failures_stall_at_max_iterations(self):
        self.configure({"tests": {"command": "exit 1"}}, maxIterations=3)
        self.start_implement()
        self.assertEqual(1, self.state()["iter"])  # the plan phase used one
        self.assertEqual("block", workflow.handle_stop(self.root, {})["decision"])
        out = workflow.handle_stop(self.root, {})
        self.assertIn("stalled after 3 iterations in phase 'implement'", out["systemMessage"])
        self.assertEqual("stalled", self.state()["status"])
        self.assertEqual(3, self.state()["iter"])

    def test_logs_and_last_failure_hold_the_full_output(self):
        self.configure({"lint": {"command": lines("lint", 400) + "; exit 1"},
                        "tests": {"command": lines("tests", 400) + "; echo on-stderr >&2; exit 1"}},
                       maxOutputChars=600)
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        lint_log = self.read(self.run_path("quality", "lint.log"))
        tests_log = self.read(self.run_path("quality", "tests.log"))
        self.assertIn("lint-1\n", lint_log)
        self.assertIn("lint-400", lint_log)
        self.assertIn("tests-1\n", tests_log)
        self.assertIn("on-stderr", tests_log)
        failure = self.read(self.run_path("last-failure.md"))
        for text in ("Quality gates failed: lint, tests", "lint-1\n", "lint-400", "tests-1\n", "tests-400"):
            self.assertIn(text, failure)
        self.assertNotIn("lint-1\n", out["reason"])
        self.assertIn("tests-400", out["reason"])

    def test_details_stay_within_max_output_chars(self):
        self.configure({"lint": {"command": lines("lint", 400) + "; exit 1", "hint": "h" * 50},
                        "tests": {"command": lines("tests", 400) + "; exit 1"}}, maxOutputChars=500)
        self.start_implement()
        outcome = workflow.evaluate_gate(self.root, self.config(), self.state())
        self.assertEqual("fail", outcome["result"])
        self.assertLessEqual(len(outcome["details"]), 500)
        self.assertTrue(outcome["details"].startswith("Quality gates failed: lint, tests\n"))
        self.assertIn("lint-400", outcome["details"])
        self.assertIn("tests-400", outcome["details"])

    def test_log_is_replaced_on_each_run(self):
        with open(os.path.join(self.root, "message"), "w") as f:
            f.write("first-run\n")
        self.configure({"tests": {"command": "cat message; exit 1"}})
        self.start_implement()
        workflow.handle_stop(self.root, {})
        with open(os.path.join(self.root, "message"), "w") as f:
            f.write("second-run\n")
        workflow.handle_stop(self.root, {})
        log = self.read(self.run_path("quality", "tests.log"))
        self.assertIn("second-run", log)
        self.assertNotIn("first-run", log)

    def test_result_is_dropped_when_the_gate_mark_changes(self):
        self.configure({"tests": {"command": "echo dropped-output; exit 1"}})
        self.start_implement()
        before = self.state()
        original = workflow.run_limited

        def pause_then_run(*args, **kwargs):
            workflow.cmd_pause(self.root)
            return original(*args, **kwargs)

        with mock.patch.object(workflow, "run_limited", side_effect=pause_then_run):
            self.assertIsNone(workflow.handle_stop(self.root, {}))
        after = self.state()
        self.assertEqual("paused", after["status"])
        self.assertEqual(before["iter"], after["iter"])
        self.assertEqual(before["phase"], after["phase"])
        self.assertEqual(before["history"], after["history"])
        self.assertNotIn("qualityResults", after)
        self.assertNotIn("gate", after)
        self.assertFalse(os.path.exists(self.run_path("last-failure.md")))
        self.assertIn("dropped-output", self.read(self.run_path("quality", "tests.log")))

    def test_phase_check_runs_first_and_gates_skip_when_it_fails(self):
        self.configure({"tests": {"command": "echo ran > marker; exit 0"}},
                       phases={"implement": {"check": {"type": "command", "command": "echo check-output; exit 1"}}})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertIn("check-output", out["reason"])
        self.assertFalse(self.exists("marker"))
        self.assertNotIn("qualityResults", self.state())

    def test_gates_run_after_a_passing_phase_check(self):
        self.configure({"tests": {"command": "test -f checked"}},
                       phases={"implement": {"check": {"type": "command", "command": "echo > checked"}}})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'review'", out["reason"])
        self.assertEqual("pass", self.state()["qualityResults"]["tests"]["result"])

    def test_gate_runs_only_in_its_phases(self):
        self.configure({"docs": {"command": "echo ran > marker; exit 1", "phases": ["docs"]}},
                       phases={"implement": {"next": "docs"}, "docs": {"next": "review"}})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'docs'", out["reason"])
        self.assertFalse(self.exists("marker"))
        self.assertNotIn("qualityResults", self.state())
        out = workflow.handle_stop(self.root, {})
        self.assertIn("Quality gates failed: docs", out["reason"])
        self.assertTrue(self.exists("marker"))
        self.assertEqual("docs", self.state()["qualityResults"]["docs"]["phase"])

    def test_plan_phase_never_runs_gates(self):
        self.configure({"tests": {"command": "echo ran > marker; exit 1", "phases": ["plan"]}},
                       phases={"plan": {"allowEdits": None}})
        self.start_implement()
        self.assertFalse(self.exists("marker"))
        self.assertNotIn("qualityResults", self.state())

    def test_empty_gates_block_behaves_as_before(self):
        self.configure({})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertIn("Phase 'implement' passed (no check configured). Now entering phase 'review'", out["reason"])
        self.assertNotIn("qualityResults", self.state())
        self.assertFalse(os.path.exists(self.run_path("quality")))


class BrokenGateTest(GateProject):
    """A gate that cannot give an answer pauses the run and uses no iteration."""

    def assertPaused(self, out, gate, reason=None):
        self.assertNotIn("decision", out)
        state = self.state()
        self.assertEqual("paused", state["status"])
        self.assertEqual(gate, state["pausedFor"].get("gate"))
        if reason is not None:
            self.assertEqual(reason, state["pausedFor"]["reason"])
        if gate is not None:
            self.assertEqual("broken", state["qualityResults"][gate]["result"])
            self.assertEqual(state["pausedFor"]["reason"], state["qualityResults"][gate]["reason"])
        return state

    def stop_broken(self, gates, gate, reason=None, **top):
        self.configure(gates, **top)
        self.start_implement()
        before = self.state()
        out = workflow.handle_stop(self.root, {})
        state = self.assertPaused(out, gate, reason)
        self.assertEqual(before["iter"], state["iter"])
        self.assertEqual("implement", state["phase"])
        self.assertFalse(os.path.exists(self.run_path("last-failure.md")))
        self.assertEqual("broken", state["history"][-1]["result"])
        return out, state

    def test_exit_127_pauses_the_run(self):
        out, _ = self.stop_broken({"detekt": {"command": "exit 127"}}, "detekt", "command not found (exit 127)")
        self.assertEqual(127, self.state()["qualityResults"]["detekt"]["exitCode"])

    def test_missing_tool_is_broken(self):
        self.stop_broken({"detekt": {"command": "no-such-tool-specloop --check"}}, "detekt",
                         "command not found (exit 127)")

    def test_exit_126_is_broken(self):
        self.stop_broken({"lint": {"command": "exit 126"}}, "lint", "command not executable (exit 126)")

    def test_timeout_is_broken(self):
        started = time.monotonic()
        _, state = self.stop_broken({"slow": {"command": "sleep 30", "timeoutSec": 1}}, "slow",
                                    "timed out after 1 seconds")
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(state["qualityResults"]["slow"]["timedOut"])

    def test_os_error_is_broken(self):
        missing = os.path.join(self.root, "no-such-shell")
        with mock.patch.object(workflow, "resolve_shell", return_value=missing):
            _, state = self.stop_broken({"tests": {"command": "exit 0"}}, "tests")
        self.assertIn("could not be run", state["pausedFor"]["reason"])

    def test_exit_1_is_an_ordinary_failure(self):
        self.configure({"tests": {"command": "exit 1"}})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        state = self.state()
        self.assertEqual("active", state["status"])
        self.assertNotIn("pausedFor", state)
        self.assertEqual("fail", state["qualityResults"]["tests"]["result"])

    def test_broken_warn_gate_pauses_too(self):
        self.stop_broken({"style": {"command": "exit 127", "level": "warn"}}, "style")

    def test_pause_message_names_gate_reason_and_commands(self):
        out, _ = self.stop_broken({"detekt": {"command": "exit 127", "hint": "Run detekt properly."}}, "detekt")
        message = out["systemMessage"]
        for text in ("detekt", "command not found (exit 127)", "/sl-go", "/sl-advance", "no iteration"):
            self.assertIn(text, message)
        self.assertNotIn("Keep working", message)
        self.assertNotIn("Run detekt properly.", message)

    def test_pause_message_uses_the_command_prefix(self):
        out, _ = self.stop_broken({"detekt": {"command": "exit 127"}}, "detekt", commandPrefix="x-")
        self.assertIn("/x-go", out["systemMessage"])
        self.assertIn("/x-advance", out["systemMessage"])

    def test_broken_gate_wins_over_a_failing_gate(self):
        _, state = self.stop_broken({"lint": {"command": "exit 1"}, "detekt": {"command": "exit 127"}}, "detekt")
        self.assertEqual("fail", state["qualityResults"]["lint"]["result"])

    def test_gates_after_a_broken_gate_do_not_run(self):
        self.stop_broken({"detekt": {"command": "exit 127"}, "tests": {"command": "echo ran > marker"}}, "detekt")
        self.assertFalse(self.exists("marker"))
        self.assertNotIn("tests", self.state()["qualityResults"])

    def test_broken_gate_in_a_delegated_phase_pauses(self):
        self.stop_broken({"detekt": {"command": "exit 127"}}, "detekt", phases={"implement": {"delegate": True}})

    def test_go_resumes_and_the_next_stop_runs_the_gates_again(self):
        self.stop_broken({"detekt": {"command": "test -f tool || exit 127; echo ran >> runs"}}, "detekt")
        out = workflow.cmd_go(self.root)
        self.assertIn("phase=implement", out)
        state = self.state()
        self.assertEqual("active", state["status"])
        self.assertNotIn("pausedFor", state)
        with open(os.path.join(self.root, "tool"), "w") as f:
            f.write("")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'review'", out["reason"])
        self.assertEqual("pass", self.state()["qualityResults"]["detekt"]["result"])
        self.assertTrue(self.exists("runs"))

    def test_go_after_a_pause_that_is_still_broken_pauses_again(self):
        self.stop_broken({"detekt": {"command": "exit 127"}}, "detekt")
        iter_before = self.state()["iter"]
        workflow.cmd_go(self.root)
        self.assertPaused(workflow.handle_stop(self.root, {}), "detekt")
        self.assertEqual(iter_before, self.state()["iter"])

    def test_advance_skips_the_phase_and_clears_paused_for(self):
        self.stop_broken({"detekt": {"command": "exit 127"}}, "detekt")
        out = workflow.cmd_advance(self.root)
        self.assertIn("Advanced to phase 'review'", out)
        state = self.state()
        self.assertNotIn("pausedFor", state)
        self.assertEqual("paused", state["status"])
        workflow.cmd_go(self.root)
        self.assertEqual("active", self.state()["status"])

    def test_gate_made_invalid_after_go_is_broken(self):
        self.configure({"lint": {"command": "echo ran > marker"}})
        self.start_implement()
        self.configure({"lint": {"command": "echo ran > marker", "level": "fatal"}})
        out = workflow.handle_stop(self.root, {})
        state = self.assertPaused(out, "lint")
        self.assertIn("invalid definition", state["pausedFor"]["reason"])
        self.assertIn("quality.gates.lint.level", state["pausedFor"]["reason"])
        self.assertFalse(self.exists("marker"))

    def test_invalid_definitions_are_all_reported_and_nothing_runs(self):
        self.start_implement()  # /sl-go refuses invalid gates, so they change after the run started
        self.configure({"ok": {"command": "echo ran > marker"}, "nocommand": {"hint": "x"},
                        "badtimeout": {"command": "exit 0", "timeoutSec": "soon"},
                        "../escape": {"command": "echo ran > marker; exit 1"}})
        out = workflow.handle_stop(self.root, {})
        self.assertPaused(out, "nocommand")
        for text in ("nocommand", "badtimeout", "../escape", "command is required", "timeoutSec"):
            self.assertIn(text, out["systemMessage"])
        self.assertFalse(self.exists("marker"))
        self.assertNotIn("ok", self.state()["qualityResults"])
        self.assertFalse(os.path.exists(os.path.join(self.base, "run", "escape.log")))
        self.assertEqual(["badtimeout.log", "nocommand.log"], sorted(os.listdir(self.run_path("quality"))))

    def test_gate_that_is_not_an_object_is_broken(self):
        self.start_implement()
        self.configure({"lint": "exit 0"})
        self.assertPaused(workflow.handle_stop(self.root, {}), "lint")

    def test_gate_with_unreadable_phases_is_broken(self):
        self.start_implement()
        self.configure({"lint": {"command": "exit 0", "phases": "implement"}})
        self.assertPaused(workflow.handle_stop(self.root, {}), "lint")

    def test_invalid_block_key_breaks_the_selected_gates(self):
        self.start_implement()
        self.configure({"lint": {"command": "exit 0"}}, {"failFast": "yes"})
        state = self.assertPaused(workflow.handle_stop(self.root, {}), "lint")
        self.assertIn("quality.failFast", state["pausedFor"]["reason"])

    def test_gates_that_are_not_an_object_pause_the_run(self):
        self.start_implement()
        self.set_config({"quality": {"gates": ["lint"]}})
        out = workflow.handle_stop(self.root, {})
        state = self.assertPaused(out, None)
        self.assertIn("quality.gates must be an object", state["pausedFor"]["reason"])
        self.assertIn("/sl-go", out["systemMessage"])

    def test_quality_that_is_not_an_object_pauses_the_run(self):
        self.start_implement()
        self.set_config({"quality": None})
        state = self.assertPaused(workflow.handle_stop(self.root, {}), None)
        self.assertIn("quality must be an object", state["pausedFor"]["reason"])

    def test_planning_phase_ignores_a_malformed_block(self):
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        self.set_config({"quality": {"gates": ["lint"]}})
        with open(self.run_path("plan.md"), "w") as f:
            f.write("- [ ] step")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'implement'", out["reason"])

    def test_dropped_when_the_gate_mark_changes(self):
        self.configure({"detekt": {"command": "exit 127"}})
        self.start_implement()
        original = workflow.run_limited

        def reset_then_run(*args, **kwargs):
            workflow.cmd_pause(self.root)
            return original(*args, **kwargs)

        with mock.patch.object(workflow, "run_limited", side_effect=reset_then_run):
            self.assertIsNone(workflow.handle_stop(self.root, {}))
        state = self.state()
        self.assertNotIn("pausedFor", state)
        self.assertNotIn("qualityResults", state)


class BudgetTest(GateProject):
    """quality.timeoutSec is one time budget for all gates of a Stop, capped by the installed Stop hook."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.root, "sleep.py"), "w") as f:
            f.write("import sys, time\ntime.sleep(float(sys.argv[1]))\n")

    def sleeper(self, seconds, marker=None):
        command = '"%s" sleep.py %s' % (sys.executable.replace("\\", "/"), seconds)
        return command + (" && echo ran > %s" % marker if marker else "")

    def set_manifest(self, **data):
        self.write_json("manifest.json", data)

    def budget(self, **top):
        self.set_config(top)
        return workflow.quality_budget(self.root, self.config())

    def test_without_stop_timeout_in_the_manifest_the_configured_value_is_used(self):
        self.assertEqual(1800, self.budget())
        self.assertEqual(77, self.budget(quality={"timeoutSec": 77}))
        self.set_manifest(commands=[], settings="x")
        self.assertEqual(77, self.budget(quality={"timeoutSec": 77}))

    def test_a_hook_that_fits_the_configured_value_leaves_it_unchanged(self):
        self.set_manifest(stopTimeoutSec=900 + 1800 + 60)
        self.assertEqual(1800, self.budget())
        self.set_manifest(stopTimeoutSec=900 + 5000 + 60)
        self.assertEqual(1800, self.budget())

    def test_a_raised_setting_cannot_outlive_the_installed_hook(self):
        self.set_manifest(stopTimeoutSec=900 + 600 + 60)
        self.assertEqual(600, self.budget(quality={"timeoutSec": 3000}))

    def test_the_budget_follows_check_timeout_sec(self):
        self.set_manifest(stopTimeoutSec=100 + 200 + 60)
        self.assertEqual(200, self.budget(checkTimeoutSec=100, quality={"timeoutSec": 3000}))
        self.assertEqual(50, self.budget(checkTimeoutSec=250, quality={"timeoutSec": 3000}))

    def test_a_manifest_value_that_is_not_a_number_is_ignored(self):
        for value in ("big", None, True, [3]):
            self.set_manifest(stopTimeoutSec=value)
            self.assertEqual(1800, self.budget(), value)

    def test_unreadable_manifest_and_malformed_quality_do_not_raise(self):
        with open(os.path.join(self.base, "manifest.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(1800, self.budget(quality=["x"]))
        self.assertEqual(1800, self.budget(quality={"timeoutSec": "soon"}))

    def run_stop(self, gates, quality=None):
        self.configure(gates, quality)
        self.start_implement()
        timeouts = []
        original = workflow.run_limited

        def spy(args, timeout, *a, **kw):
            timeouts.append(timeout)
            return original(args, timeout, *a, **kw)

        started = time.monotonic()
        with mock.patch.object(workflow, "run_limited", side_effect=spy):
            out = workflow.handle_stop(self.root, {})
        return out, timeouts, time.monotonic() - started

    def test_a_later_gate_receives_only_the_time_that_is_left(self):
        self.set_manifest(stopTimeoutSec=900 + 4 + 60)
        clock = iter([0, 0, 1.5])  # start, first gate, second gate: the first gate "took" 1.5 s
        with mock.patch.object(workflow, "_clock", side_effect=lambda: next(clock)):
            out, timeouts, elapsed = self.run_stop({"first": {"command": "exit 0", "timeoutSec": 100},
                                                    "second": {"command": self.sleeper(60), "timeoutSec": 100}})
        self.assertEqual(2, len(timeouts))
        self.assertEqual(4, timeouts[0])
        self.assertEqual(2.5, timeouts[1])
        self.assertLess(elapsed, 30)
        state = self.state()
        self.assertEqual("pass", state["qualityResults"]["first"]["result"])
        second = state["qualityResults"]["second"]
        self.assertEqual("broken", second["result"])
        self.assertTrue(second["timedOut"])
        self.assertIn("timed out after", second["reason"])
        self.assertIn("time budget", second["reason"])
        self.assertEqual("paused", state["status"])
        self.assertEqual("second", state["pausedFor"]["gate"])

    def test_a_gate_with_a_shorter_timeout_keeps_its_own(self):
        out, timeouts, _ = self.run_stop({"quick": {"command": "exit 0", "timeoutSec": 7}})
        self.assertEqual([7], timeouts)

    def test_a_gate_that_cannot_start_because_the_budget_is_used_up_is_broken(self):
        marker = "second-ran"
        clock = iter([0, 1, 50])  # start, first gate, second gate
        with mock.patch.object(workflow, "_clock", side_effect=lambda: next(clock)):
            out, timeouts, _ = self.run_stop({"first": {"command": "exit 0"},
                                              "second": {"command": "echo ran > " + marker},
                                              "third": {"command": "echo ran > third-ran"}},
                                             {"timeoutSec": 5})
        self.assertEqual(1, len(timeouts))
        self.assertNotIn("decision", out)
        state = self.state()
        self.assertEqual("pass", state["qualityResults"]["first"]["result"])
        second = state["qualityResults"]["second"]
        self.assertEqual("broken", second["result"])
        self.assertIn("budget", second["reason"])
        self.assertIn("exhausted", second["reason"])
        self.assertEqual("second", state["pausedFor"]["gate"])
        self.assertEqual("paused", state["status"])
        self.assertFalse(self.exists(marker))
        self.assertFalse(self.exists("third-ran"))
        self.assertNotIn("third", state["qualityResults"])
        self.assertIn("exhausted", self.read(self.run_path("quality", "second.log")))
        self.assertIn("exhausted", out["systemMessage"])

    def test_a_used_up_budget_pauses_without_using_an_iteration(self):
        self.configure({"only": {"command": "exit 0"}}, {"timeoutSec": 5})
        self.start_implement()
        before = self.state()
        clock = iter([0, 9])
        with mock.patch.object(workflow, "_clock", side_effect=lambda: next(clock)):
            workflow.handle_stop(self.root, {})
        state = self.state()
        self.assertEqual(before["iter"], state["iter"])
        self.assertEqual("paused", state["status"])
        self.assertEqual("only", state["pausedFor"]["gate"])

    def test_a_hung_gate_is_killed_at_its_timeout_and_the_hook_returns(self):
        out, timeouts, elapsed = self.run_stop({"hung": {"command": self.sleeper(60), "timeoutSec": 1}})
        self.assertEqual([1], timeouts)
        self.assertLess(elapsed, 30)
        self.assertNotIn("decision", out)
        state = self.state()
        self.assertEqual("timed out after 1 seconds", state["pausedFor"]["reason"])
        self.assertEqual("paused", state["status"])
        self.assertNotIn("gate", state)

    def test_a_hung_gate_is_killed_at_the_installed_budget(self):
        self.set_manifest(stopTimeoutSec=900 + 2 + 60)
        out, timeouts, elapsed = self.run_stop({"hung": {"command": self.sleeper(60), "timeoutSec": 100}})
        self.assertLessEqual(timeouts[0], 2)
        self.assertLess(elapsed, 30)
        self.assertEqual("broken", self.state()["qualityResults"]["hung"]["result"])


class MalformedConfigHooksTest(GateProject):
    """No shape of the quality block makes a hook entry point fail."""

    SHAPES = [
        {"quality": None}, {"quality": ["x"]}, {"quality": "gates"}, {"quality": {"gates": ["lint"]}},
        {"quality": {"gates": "lint"}}, {"quality": {"gates": {"lint": "exit 0"}}},
        {"quality": {"gates": {"lint": ["exit 0"]}}}, {"quality": {"gates": {"lint": 5}}},
        {"quality": {"gates": {"lint": {"command": 5, "phases": "implement", "hint": 3, "level": []}}}},
        {"quality": {"gates": {"lint": {"command": "exit 0", "phases": [None], "timeoutSec": {}}}}},
        {"quality": {"gates": {"lint": {"command": "exit 0", "metric": "x", "forbid": "y", "protect": 1}}}},
        {"quality": {"timeoutSec": "x", "failFast": [], "legacy": {}, "gates": {"lint": {"command": "exit 0"}}}},
    ]

    def run_main(self, *argv, stdin="{}"):
        old_stdin, sys.stdin = sys.stdin, io.StringIO(stdin)
        os.environ["CLAUDE_PROJECT_DIR"] = self.root
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = workflow.main(list(argv))
        finally:
            sys.stdin = old_stdin
            del os.environ["CLAUDE_PROJECT_DIR"]
        return code, out.getvalue(), err.getvalue()

    def test_hook_entry_points_exit_0_without_errors(self):
        self.start_implement()
        state = self.state_bytes()
        pretool = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.root, "a.txt")}})
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                with open(os.path.join(self.base, "state.json"), "wb") as f:
                    f.write(state)
                self.set_config(shape)
                for argv, stdin in ((["context", "SessionStart"], "{}"), (["context", "UserPromptSubmit"], "{}"),
                                    (["pretool"], pretool), (["stop"], "{}")):
                    code, out, err = self.run_main(*argv, stdin=stdin)
                    self.assertEqual(0, code, argv)
                    self.assertNotIn("Traceback", err, argv)
                    self.assertNotIn("hook error", err, argv)
                    if out.strip():
                        json.loads(out)
                self.assertEqual("paused", self.state()["status"])



def step_entry(number):
    return {"id": number, "title": "Step %d" % number, "description": "Work %d." % number,
            "acceptance": ["It works"], "status": "pending"}


@unittest.skipUnless(gitfixture.git_available(), "git not available")
class FeatureGateTest(unittest.TestCase):
    """Gates in feature mode: the implement step phase runs them before review."""

    def setUp(self):
        self.fx = gitfixture.GitFixture()
        self.addCleanup(self.fx.cleanup)
        self.root = self.fx.target
        self.base = os.path.join(self.root, ".claude", "specloop")
        shutil.copytree(KIT, self.base)
        self.write("spec.md", "# Gated feature\n\nDo things.\n")
        self.fx.git(self.root, "add", "spec.md")
        self.fx.git(self.root, "commit", "-q", "-m", "initial")

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def run_file(self, name):
        return os.path.join(".claude", "specloop", "run", name)

    def state(self):
        return workflow.load_state(self.root)

    def config(self):
        return workflow.load_config(self.root)

    def marker(self, name):
        """How often the gate `name` ran: it appends a line to a marker inside .git, outside the work tree."""
        path = os.path.join(self.root, ".git", "ran-" + name)
        if not os.path.exists(path):
            return 0
        with open(path) as f:
            return len(f.read().split())

    def counting(self, name, exit_code=0, **extra):
        return dict(extra, command="echo x >> .git/ran-%s; echo %s-output; exit %d" % (name, name, exit_code))

    def configure(self, gates, feature=None):
        config = {"phases": {"implement": {"check": {"type": "command", "command": "exit 0"}}},
                  "quality": {"gates": gates}}
        if feature:
            config["feature"] = feature
        with open(os.path.join(self.base, "config.json"), "w") as f:
            json.dump(config, f)

    def steps(self, count=2):
        return {"feature": "Gated", "steps": [step_entry(n) for n in range(1, count + 1)]}

    def steps_file(self):
        with open(os.path.join(self.base, "run", "steps.json"), encoding="utf-8") as f:
            return json.load(f)

    def begin(self, gates, count=2, feature=None):
        """Start a feature run and approve its plan; step 1 is in implement."""
        self.configure(gates, feature)
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        workflow.cmd_go(self.root)
        self.write(self.run_file("plan.md"), "# Plan")
        self.write(self.run_file("steps.json"), json.dumps(self.steps(count)))
        workflow.handle_stop(self.root, {})
        workflow.cmd_go(self.root)
        self.assertEqual(("implement", 1), (self.state()["phase"], self.state()["stepId"]))

    def stop(self):
        return workflow.handle_stop(self.root, {})

    def write_attempt(self, name, text):
        attempt = os.path.relpath(workflow.attempt_path(self.root, self.state()), self.root)
        self.write(os.path.join(attempt, name), text)

    def approve_step(self):
        """The reviewer approves the current step in review, and it is committed."""
        self.assertEqual("review", self.state()["phase"])
        self.write_attempt("review.md", "Fine.\nVERDICT: APPROVE")
        return self.stop()

    def finish_step(self):
        """Stop in implement with a change to commit, then approve the review."""
        self.write("work-%d.txt" % self.state()["stepId"], "work\n")
        out = self.stop()
        self.assertIn("entering phase 'review'", out["reason"])
        return self.approve_step()

    def request_replan(self):
        self.write("wip.txt", "wip\n")
        self.write_attempt("replan.md", "Rework the plan.\n")
        return self.stop()

    def test_failing_block_gate_keeps_the_step_in_implement(self):
        self.begin({"tests": self.counting("tests", 1, hint="Fix it.")})
        out = self.stop()
        self.assertEqual("block", out["decision"])
        self.assertIn("Quality gates failed: tests", out["reason"])
        with open(os.path.join(self.base, "run", "last-failure.md"), encoding="utf-8") as f:
            self.assertIn("tests-output", f.read())
        state = self.state()
        self.assertEqual(("implement", 1, 1), (state["phase"], state["stepId"], state["iter"]))
        self.assertEqual("fail", state["qualityResults"]["tests"]["result"])
        self.assertEqual(1, state["qualityResults"]["tests"]["stepId"])

    def test_passing_block_gate_enters_review(self):
        self.begin({"tests": self.counting("tests")})
        out = self.stop()
        self.assertIn("entering phase 'review'", out["reason"])
        self.assertEqual("review", self.state()["phase"])
        self.assertEqual("pass", self.state()["qualityResults"]["tests"]["result"])
        self.assertEqual(1, self.marker("tests"))

    def test_gate_runs_on_every_step_by_default(self):
        self.begin({"tests": self.counting("tests")})
        self.finish_step()
        self.assertEqual(2, self.state()["stepId"])
        self.finish_step()
        self.assertEqual(2, self.marker("tests"))

    def test_last_gate_runs_once_on_the_last_step_only(self):
        self.begin({"final": self.counting("final", steps="last")})
        self.assertIn("entering phase 'review'", self.stop()["reason"])
        self.assertEqual(0, self.marker("final"))
        self.assertNotIn("final", self.state().get("qualityResults", {}))
        self.assertFalse(os.path.exists(os.path.join(self.base, "run", "quality", "final.log")))
        self.approve_step()
        self.assertEqual(2, self.state()["stepId"])
        self.write("work.txt", "work\n")
        self.assertIn("entering phase 'review'", self.stop()["reason"])
        self.assertEqual(1, self.marker("final"))
        self.assertEqual(2, self.state()["qualityResults"]["final"]["stepId"])
        self.approve_step()
        self.assertEqual("done", self.state()["status"])
        self.assertEqual(1, self.marker("final"))

    def test_failing_last_gate_blocks_only_the_last_step(self):
        self.begin({"final": self.counting("final", 1, steps="last")})
        self.assertIn("entering phase 'review'", self.stop()["reason"])
        self.approve_step()
        out = self.stop()
        self.assertIn("Quality gates failed: final", out["reason"])
        self.assertEqual(("implement", 2), (self.state()["phase"], self.state()["stepId"]))

    def test_last_gate_moves_to_the_step_a_replan_adds(self):
        self.begin({"final": self.counting("final", steps="last"), "each": self.counting("each")})
        self.finish_step()
        self.assertEqual(2, self.state()["stepId"])
        # Step 2 asks for a replan that adds a third step, so step 2 is no longer the last one.
        self.assertEqual("stalled", (self.request_replan(), self.state())[1]["status"])
        data = self.steps_file()
        data["steps"].append(step_entry(3))
        self.write(self.run_file("steps.json"), json.dumps(data))
        workflow.cmd_go(self.root)
        self.assertEqual(("implement", 2), (self.state()["phase"], self.state()["stepId"]))
        self.assertIn("entering phase 'review'", self.stop()["reason"])
        self.assertEqual(0, self.marker("final"))
        self.assertEqual(2, self.marker("each"))
        self.approve_step()
        self.assertEqual(3, self.state()["stepId"])
        self.assertIn("entering phase 'review'", self.stop()["reason"])
        self.assertEqual(1, self.marker("final"))
        self.assertEqual(3, self.state()["qualityResults"]["final"]["stepId"])
        self.approve_step()
        self.assertEqual(("done", 1), (self.state()["status"], self.marker("final")))

    def test_steps_is_ignored_in_single_mode(self):
        self.configure({"final": self.counting("final", 1, steps="last")})
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        self.write(self.run_file("plan.md"), "- [ ] step")
        self.assertIn("entering phase 'implement'", self.stop()["reason"])
        out = self.stop()
        self.assertIn("Quality gates failed: final", out["reason"])
        self.assertEqual(1, self.marker("final"))

    def test_gates_never_run_in_feature_plan(self):
        self.configure({"all": self.counting("all", 1)})
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        workflow.cmd_go(self.root)
        self.assertEqual("feature-plan", self.state()["phase"])
        self.assertEqual([], workflow.selected_gates(self.config(), self.state()))
        self.write(self.run_file("plan.md"), "# Plan")
        self.write(self.run_file("steps.json"), json.dumps(self.steps()))
        self.stop()
        self.assertEqual("paused", self.state()["status"])
        self.assertEqual(0, self.marker("all"))
        self.assertNotIn("qualityResults", self.state())

    def test_gates_never_run_in_feature_replan(self):
        self.begin({"tests": self.counting("tests", 1)}, feature={"replan": "auto"})
        self.assertIn("Quality gates failed", self.stop()["reason"])
        self.assertEqual(1, self.marker("tests"))
        self.assertIn("feature-replan", self.request_replan()["reason"])
        self.assertEqual("feature-replan", self.state()["phase"])
        self.assertEqual([], workflow.selected_gates(self.config(), self.state()))
        data = self.steps_file()
        data["steps"][1]["description"] = "Rewritten."
        self.write(self.run_file("steps.json"), json.dumps(data))
        self.assertIn("Replan adopted", self.stop()["reason"])
        self.assertEqual(1, self.marker("tests"))
        self.assertEqual("implement", self.state()["phase"])


if __name__ == "__main__":
    unittest.main()
