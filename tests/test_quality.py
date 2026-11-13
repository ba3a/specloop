"""Tests for quality gates: the `quality` configuration block and its validation."""

import contextlib
import copy
import hashlib
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


# A small Python script that stands in for the tools of the spec's section 3.2. `kover` prints the coverage found in
# kover.txt like koverLog does; `detekt` writes a JSON report with as many issues as issues.txt says and exits 2
# when there are any, like detekt with its default maxIssues.
FAKE_TOOLS = """
import json, os, sys

def number(name):
    with open(name) as f:
        return f.read().strip()

if sys.argv[1] == "kover":
    print("> Task :koverLog")
    print("coverage: %s%%" % number("kover.txt"))
elif sys.argv[1] == "detekt":
    total = int(number("issues.txt"))
    os.makedirs("build/reports/detekt", exist_ok=True)
    with open("build/reports/detekt/detekt.json", "w") as f:
        json.dump({"summary": {"total": total}}, f)
    if total:
        print("Foo.kt:3 MagicNumber (%d issues in total)" % total)
        sys.exit(2)
"""


def fake_command(tool):
    return '"%s" fake_tools.py %s' % (sys.executable.replace("\\", "/"), tool)


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

    def accept_quality(self):
        """Take the quality block as it is now as the run's fingerprint, as a person's /sl-quality accept will."""
        workflow.update_state(self.root, lambda s: s["qualityFingerprints"].update(
            quality=workflow.quality_block_fingerprint(self.config())))

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


class QualityReportTest(GateProject):
    """run/quality.md after a gate run, in single mode."""

    def report(self):
        return self.read(self.run_path("quality.md"))

    def test_one_row_per_gate_with_result_duration_and_metric(self):
        self.configure({"lint": {"command": "exit 0"},
                        "cov": {"command": "echo coverage: 91.5%", "metric": {"pattern": "coverage: ([0-9.]+)%", "min": 80}},
                        "tests": {"command": "exit 1"}})
        self.start_implement()
        workflow.handle_stop(self.root, {})
        rows = [line for line in self.report().splitlines() if line.startswith("| ")][1:]
        self.assertEqual(3, len(rows))
        cells = [[c.strip() for c in row.strip("|").split("|")] for row in rows]
        self.assertEqual(["lint", "pass"], cells[0][:2])
        self.assertEqual(["cov", "pass"], cells[1][:2])
        self.assertEqual(["tests", "fail"], cells[2][:2])
        for row in cells:
            self.assertRegex(row[2], r"^\d+(\.\d+)?s$")
        self.assertEqual(["-", "91.5", "-"], [row[3] for row in cells])

    def test_a_warn_result_shows_the_end_of_its_output(self):
        self.configure({"style": {"command": "echo style-output; exit 1", "level": "warn"}}, maxOutputChars=60)
        self.start_implement()
        workflow.handle_stop(self.root, {})
        report = self.report()
        self.assertIn("| style | warn |", report)
        self.assertIn("style-output", report)
        self.assertIn("quality/style.log", report)

    def test_a_long_warn_output_is_cut_to_its_end(self):
        self.configure({"style": {"command": "i=0; while [ $i -lt 200 ]; do echo line$i; i=$((i+1)); done; exit 1",
                                  "level": "warn"}}, maxOutputChars=100)
        self.start_implement()
        workflow.handle_stop(self.root, {})
        report = self.report()
        self.assertIn("line199", report)
        self.assertNotIn("line0\n", report)
        self.assertLess(len(report), 700)

    def test_a_passing_gate_has_no_warning_section(self):
        self.configure({"tests": {"command": "echo fine; exit 0"}})
        self.start_implement()
        workflow.handle_stop(self.root, {})
        self.assertNotIn("Warning", self.report())
        self.assertNotIn("fine", self.report())

    def test_the_report_is_rewritten_on_each_run(self):
        self.configure({"tests": {"command": "test -f ok"}})
        self.start_implement()
        workflow.handle_stop(self.root, {})
        self.assertIn("| tests | fail |", self.report())
        with open(os.path.join(self.root, "ok"), "w") as f:
            f.write("x")
        workflow.handle_stop(self.root, {})
        self.assertIn("| tests | pass |", self.report())
        self.assertNotIn("fail", self.report())

    def test_a_broken_gate_run_is_reported_too(self):
        self.configure({"lint": {"command": "exit 0"}, "tests": {"command": "exit 127"}})
        self.start_implement()
        workflow.handle_stop(self.root, {})
        self.assertIn("| lint | pass |", self.report())
        self.assertIn("| tests | broken |", self.report())

    def test_without_gates_no_report_is_written(self):
        self.set_config({})
        self.start_implement()
        workflow.handle_stop(self.root, {})
        self.assertFalse(os.path.exists(self.run_path("quality.md")))

    def test_a_dropped_result_writes_no_report(self):
        self.configure({"tests": {"command": "exit 0"}})
        self.start_implement()
        original = workflow.run_limited

        def pause_then_run(*args, **kwargs):
            workflow.cmd_pause(self.root)
            return original(*args, **kwargs)

        with mock.patch.object(workflow, "run_limited", side_effect=pause_then_run):
            self.assertIsNone(workflow.handle_stop(self.root, {}))
        self.assertFalse(os.path.exists(self.run_path("quality.md")))
        self.assertNotIn("qualityResults", self.state())

    def test_the_report_has_hooks_for_the_sections_later_steps_add(self):
        self.configure({"tests": {"command": "exit 0"}})
        self.start_implement()
        with mock.patch.object(workflow, "quality_waived_lines", lambda state: ["## Waived", "- lint"]),                 mock.patch.object(workflow, "quality_accepted_lines", lambda state: ["## Accepted"]),                 mock.patch.object(workflow, "quality_baseline_lines", lambda config, state: ["No baseline."]):
            workflow.handle_stop(self.root, {})
        report = self.report()
        self.assertLess(report.index("No baseline."), report.index("## Waived"))
        self.assertLess(report.index("## Waived"), report.index("## Accepted"))


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


class MetricGateTest(GateProject):
    """passExitCodes and metric: the exit code decides, then a number may add a limit."""

    def stop_gate(self, gate, **top):
        """Run one gate `g` on a Stop of implement and return the Stop's output."""
        self.configure({"g": gate}, **top)
        self.start_implement()
        self.iter0 = self.state()["iter"]
        return workflow.handle_stop(self.root, {})

    def assertEntersReview(self, out):
        self.assertIn("entering phase 'review'", out.get("reason", ""))
        self.assertEqual("review", self.state()["phase"])

    def assertFails(self, out, fragment=None):
        self.assertEqual("block", out.get("decision"), out)
        self.assertEqual("implement", self.state()["phase"])
        self.assertEqual("fail", self.state()["qualityResults"]["g"]["result"])
        if fragment:
            self.assertIn(fragment, out["reason"])

    def assertBroken(self, out, fragment, gate="g"):
        state = self.state()
        self.assertNotIn("decision", out)
        self.assertEqual("paused", state["status"])
        self.assertEqual(gate, state["pausedFor"]["gate"])
        self.assertIn(fragment, state["pausedFor"]["reason"])
        self.assertEqual("broken", state["qualityResults"][gate]["result"])
        self.assertEqual(self.iter0, state["iter"])
        self.assertNotIn("metric", state["qualityResults"][gate])
        self.assertIn(fragment, out["systemMessage"])

    def reset(self):
        self.tearDown()
        self.setUp()

    # passExitCodes

    def test_a_listed_exit_code_counts_as_a_pass(self):
        self.assertEntersReview(self.stop_gate({"command": "exit 2", "passExitCodes": [0, 2]}))
        result = self.state()["qualityResults"]["g"]
        self.assertEqual(("pass", 2), (result["result"], result["exitCode"]))

    def test_an_unlisted_exit_code_fails(self):
        self.assertFails(self.stop_gate({"command": "exit 3", "passExitCodes": [0, 2]}), "Quality gates failed: g")

    def test_exit_code_zero_fails_when_it_is_not_listed(self):
        self.assertFails(self.stop_gate({"command": "exit 0", "passExitCodes": [2]}))

    def test_exit_code_zero_is_the_default(self):
        self.assertFails(self.stop_gate({"command": "exit 2"}))

    def test_a_failing_exit_code_at_level_warn_only_warns(self):
        self.assertEntersReview(self.stop_gate({"command": "exit 3", "passExitCodes": [2], "level": "warn"}))
        self.assertEqual("warn", self.state()["qualityResults"]["g"]["result"])

    def test_127_stays_broken_even_with_pass_exit_codes(self):
        out = self.stop_gate({"command": "exit 127", "passExitCodes": [0, 2]})
        self.assertBroken(out, "command not found (exit 127)")

    # pattern

    def test_pattern_uses_the_first_group_of_the_last_match(self):
        command = "echo 'coverage: 95% of 7'; echo 'coverage: 85.5% of 3'"
        out = self.stop_gate({"command": command, "metric": {"pattern": r"coverage: ([0-9.]+)% of (\d)", "min": 80}})
        self.assertEntersReview(out)
        self.assertEqual(85.5, self.state()["qualityResults"]["g"]["metric"])

    def test_pattern_takes_the_last_match_not_the_best(self):
        command = "echo 'coverage: 95%'; echo 'coverage: 60%'"
        out = self.stop_gate({"command": command, "metric": {"pattern": "coverage: ([0-9.]+)%", "min": 80}})
        self.assertFails(out, "metric 60 is below the minimum 80")
        self.assertEqual(60, self.state()["qualityResults"]["g"]["metric"])

    def test_pattern_reads_stderr_too(self):
        out = self.stop_gate({"command": "echo 'value: 5' >&2", "metric": {"pattern": r"value: (\d+)", "max": 5}})
        self.assertEntersReview(out)

    # json and file

    def test_json_follows_a_dotted_path_in_the_output(self):
        command = """echo '{"summary": {"total": 4}, "list": [7, 8]}'"""
        self.assertFails(self.stop_gate({"command": command, "metric": {"json": "summary.total", "max": 3}}),
                         "metric 4 is above the maximum 3")

    def test_json_path_may_index_a_list(self):
        command = """echo '{"list": [7, 8]}'"""
        self.assertEntersReview(self.stop_gate({"command": command, "metric": {"json": "list.1", "min": 8}}))
        self.assertEqual(8, self.state()["qualityResults"]["g"]["metric"])

    def test_file_is_read_instead_of_the_output(self):
        with open(os.path.join(self.root, "report.txt"), "w") as f:
            f.write("coverage: 90%")
        metric = {"file": "report.txt", "pattern": r"coverage: (\d+)%", "min": 80}
        self.assertEntersReview(self.stop_gate({"command": "echo 'coverage: 10%'", "metric": metric}))
        self.assertEqual(90, self.state()["qualityResults"]["g"]["metric"])

    def test_file_is_relative_to_the_project_root(self):
        os.makedirs(os.path.join(self.root, "build", "reports"))
        with open(os.path.join(self.root, "build", "reports", "r.json"), "w") as f:
            f.write('{"n": 2}')
        out = self.stop_gate({"command": "exit 0", "metric": {"file": "build/reports/r.json", "json": "n", "max": 2}})
        self.assertEntersReview(out)

    # limits

    def test_limits_are_inclusive(self):
        for value, limits, passes in ((80, {"min": 80}, True), (79.9, {"min": 80}, False),
                                      (3, {"max": 3}, True), (3.1, {"max": 3}, False),
                                      (5, {"min": 5, "max": 5}, True)):
            with self.subTest(value=value, limits=limits):
                self.reset()
                metric = dict(limits, pattern="v=([0-9.]+)")
                out = self.stop_gate({"command": "echo v=%s" % value, "metric": metric})
                if passes:
                    self.assertEntersReview(out)
                else:
                    self.assertFails(out)

    def test_the_message_shows_value_and_limit(self):
        out = self.stop_gate({"command": "echo 'coverage: 75.25%'", "hint": "Add tests.",
                              "metric": {"pattern": "coverage: ([0-9.]+)%", "min": 80}})
        self.assertFails(out, "metric 75.25 is below the minimum 80")
        self.assertIn("Add tests.", out["reason"])
        self.assertIn("metric 75.25 is below the minimum 80", self.read(self.run_path("last-failure.md")))
        self.assertIn("metric 75.25 is below the minimum 80", self.read(self.run_path("quality", "g.log")))
        self.assertEqual(self.iter0 + 1, self.state()["iter"])

    def test_a_value_outside_the_limits_at_level_warn_only_warns(self):
        out = self.stop_gate({"command": "echo v=9", "level": "warn", "metric": {"pattern": r"v=(\d)", "max": 1}})
        self.assertEntersReview(out)
        self.assertEqual("warn", self.state()["qualityResults"]["g"]["result"])
        self.assertEqual(9, self.state()["qualityResults"]["g"]["metric"])

    def test_the_metric_is_recorded_for_a_passing_gate(self):
        self.assertEntersReview(self.stop_gate({"command": "echo v=7", "metric": {"pattern": r"v=(\d)", "min": 1}}))
        result = self.state()["qualityResults"]["g"]
        self.assertEqual(("pass", 7), (result["result"], result["metric"]))

    def test_a_gate_without_a_metric_records_none(self):
        self.assertEntersReview(self.stop_gate({"command": "exit 0"}))
        self.assertNotIn("metric", self.state()["qualityResults"]["g"])

    def test_the_metric_is_not_read_when_the_exit_code_fails(self):
        out = self.stop_gate({"command": "exit 1", "metric": {"file": "missing.json", "json": "n", "max": 0}})
        self.assertFails(out)
        self.assertNotIn("pausedFor", self.state())

    # broken

    def test_a_missing_file_is_broken(self):
        out = self.stop_gate({"command": "exit 0", "metric": {"file": "build/none.json", "json": "n", "max": 0}})
        self.assertBroken(out, "build/none.json")

    def test_a_file_outside_the_project_is_broken(self):
        out = self.stop_gate({"command": "exit 0", "metric": {"file": "../x.json", "json": "n", "max": 0}})
        self.assertBroken(out, "not inside the project")

    def test_no_match_is_broken(self):
        out = self.stop_gate({"command": "echo nothing here", "metric": {"pattern": r"coverage: (\d+)", "min": 1}})
        self.assertBroken(out, "matches nothing")

    def test_a_group_that_is_not_a_number_is_broken(self):
        out = self.stop_gate({"command": "echo coverage: n/a", "metric": {"pattern": r"coverage: (\S+)", "min": 1}})
        self.assertBroken(out, "not a number")

    def test_a_group_that_did_not_take_part_is_broken(self):
        out = self.stop_gate({"command": "echo a", "metric": {"pattern": "a|(b)", "min": 1}})
        self.assertBroken(out, "not a number")

    def test_invalid_json_is_broken(self):
        out = self.stop_gate({"command": "echo 'not json'", "metric": {"json": "n", "max": 0}})
        self.assertBroken(out, "not valid JSON")

    def test_a_missing_key_is_broken(self):
        out = self.stop_gate({"command": """echo '{"a": {"b": 1}}'""", "metric": {"json": "a.c", "max": 0}})
        self.assertBroken(out, "a.c")

    def test_a_json_value_that_is_not_a_number_is_broken(self):
        for text in ('"4"', "true", "null", "[1]", "NaN"):
            with self.subTest(text=text):
                self.reset()
                command = """echo '{"n": %s}'""" % text
                out = self.stop_gate({"command": command, "metric": {"json": "n", "max": 0}})
                self.assertBroken(out, "not a number")

    def test_a_broken_metric_at_level_warn_pauses_too(self):
        out = self.stop_gate({"command": "echo nothing", "level": "warn", "metric": {"pattern": r"(\d+)", "max": 1}})
        self.assertBroken(out, "matches nothing")

    def test_a_broken_metric_is_logged_and_go_retries(self):
        out = self.stop_gate({"command": "echo nothing", "metric": {"pattern": r"(\d+)", "max": 1}})
        self.assertBroken(out, "matches nothing")
        self.assertIn("matches nothing", self.read(self.run_path("quality", "g.log")))
        self.assertFalse(os.path.exists(self.run_path("last-failure.md")))
        self.configure({"g": {"command": "echo 3", "metric": {"pattern": r"(\d+)", "max": 5}}})
        self.accept_quality()
        workflow.cmd_go(self.root)
        self.assertEntersReview(workflow.handle_stop(self.root, {}))

    # the spec's examples (section 3.2), each with a stand-in for its tool

    def write_tools(self, kover=None, issues=None):
        with open(os.path.join(self.root, "fake_tools.py"), "w") as f:
            f.write(FAKE_TOOLS)
        self.set_tool(kover, issues)

    def set_tool(self, kover=None, issues=None):
        for name, value in (("kover.txt", kover), ("issues.txt", issues)):
            if value is not None:
                with open(os.path.join(self.root, name), "w") as f:
                    f.write(str(value))

    def test_spec_example_coverage_gate(self):
        self.configure({"coverage": dict(COVERAGE, command=fake_command("kover"))})
        self.write_tools(kover="79.9")
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("metric 79.9 is below the minimum 80", out["reason"])
        self.assertEqual(79.9, self.state()["qualityResults"]["coverage"]["metric"])
        self.set_tool(kover="80.0")
        self.assertEntersReview(workflow.handle_stop(self.root, {}))
        self.assertEqual(80, self.state()["qualityResults"]["coverage"]["metric"])

    def test_spec_example_issues_gate_blocks_implement_until_the_report_shows_zero(self):
        self.configure({"issues": dict(ISSUES, command=fake_command("detekt"))})
        self.write_tools(issues=3)
        self.start_implement()
        iter0 = self.state()["iter"]
        for expected_iter in (iter0 + 1, iter0 + 2):
            out = workflow.handle_stop(self.root, {})
            self.assertEqual("block", out["decision"])
            self.assertIn("MagicNumber (3 issues in total)", out["reason"])
            self.assertIn("metric 3 is above the maximum 0", out["reason"])
            self.assertEqual(("implement", expected_iter), (self.state()["phase"], self.state()["iter"]))
        self.set_tool(issues=1)
        self.assertIn("metric 1 is above", workflow.handle_stop(self.root, {})["reason"])
        self.set_tool(issues=0)
        self.assertEntersReview(workflow.handle_stop(self.root, {}))
        result = self.state()["qualityResults"]["issues"]
        self.assertEqual(("pass", 0, 0), (result["result"], result["exitCode"], result["metric"]))

    def test_spec_example_issues_gate_without_a_report_is_broken(self):
        self.configure({"issues": dict(ISSUES, command="exit 2")})
        self.start_implement()
        self.iter0 = self.state()["iter"]
        self.assertBroken(workflow.handle_stop(self.root, {}), "detekt.json", gate="issues")


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

    def test_the_step_instructions_list_the_gates_that_run_on_this_step(self):
        self.begin({"each": {"command": "exit 0", "hint": "Run it."}, "final": {"command": "exit 0", "steps": "last"}})
        text = workflow.phase_instructions(self.root, self.config(), self.state())
        self.assertIn("- `each` (block): `exit 0`. Hint: Run it.", text)
        self.assertNotIn("`final`", text)
        self.finish_step()
        text = workflow.phase_instructions(self.root, self.config(), self.state())
        self.assertIn("`each`", text)
        self.assertIn("`final`", text)

    def test_the_step_instructions_render_nothing_without_gates(self):
        self.begin({"final": {"command": "exit 0", "steps": "last"}})
        text = workflow.phase_instructions(self.root, self.config(), self.state())
        self.assertNotIn("{quality_gates}", text)
        self.assertNotIn("Quality gates", text)
        self.assertNotIn("\n\n\n", text)

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

    def write_value(self, value):
        self.write("value.txt", str(value))

    def test_the_value_is_measured_on_the_stop_that_passes_feature_plan(self):
        self.write_value(70)
        self.begin({"cov": ratchet_gate()})
        self.assertEqual({"cov": 70}, self.state()["qualityBaseline"])
        self.write_value(65)
        self.write("work.txt", "work\n")
        self.assertIn("Quality gates failed: cov", self.stop()["reason"])
        with open(os.path.join(self.base, "run", "last-failure.md"), encoding="utf-8") as f:
            self.assertIn("metric 65 is below the minimum 80 and below the baseline 70 (ratchet)", f.read())
        self.write_value(72)
        self.assertIn("entering phase 'review'", self.stop()["reason"])

    def test_a_replan_does_not_measure(self):
        self.write_value(70)
        self.begin({"cov": ratchet_gate()}, feature={"replan": "auto"})
        workflow.update_state(self.root, lambda current: current.pop("qualityBaseline"))
        self.write_value(50)
        self.assertIn("feature-replan", self.request_replan()["reason"])
        data = self.steps_file()
        data["steps"][1]["description"] = "Rewritten."
        self.write(self.run_file("steps.json"), json.dumps(data))
        self.assertIn("Replan adopted", self.stop()["reason"])
        self.assertNotIn("qualityBaseline", self.state())

    def test_an_unreadable_value_pauses_feature_plan(self):
        self.configure({"cov": ratchet_gate(command="echo none")})
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        workflow.cmd_go(self.root)
        self.write(self.run_file("plan.md"), "# Plan")
        self.write(self.run_file("steps.json"), json.dumps(self.steps()))
        out = self.stop()
        state = self.state()
        self.assertNotIn("decision", out)
        self.assertEqual(("paused", "feature-plan", 0), (state["status"], state["phase"], state["iter"]))
        self.assertFalse(state.get("awaitingApproval"))
        self.assertEqual("cov", state["pausedFor"]["gate"])


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


    def test_spec_example_issues_gate_blocks_a_step_until_the_report_shows_zero(self):
        self.write("fake_tools.py", FAKE_TOOLS)
        self.begin({"issues": dict(ISSUES, command=fake_command("detekt"))})
        self.write("issues.txt", "2")
        out = self.stop()
        self.assertEqual("block", out["decision"])
        self.assertIn("Quality gates failed: issues", out["reason"])
        with open(os.path.join(self.base, "run", "last-failure.md"), encoding="utf-8") as f:
            findings = f.read()
        self.assertIn("MagicNumber (2 issues in total)", findings)
        self.assertIn("metric 2 is above the maximum 0", findings)
        state = self.state()
        self.assertEqual(("implement", 1, 1), (state["phase"], state["stepId"], state["iter"]))
        self.write("issues.txt", "0")
        self.assertIn("entering phase 'review'", self.stop()["reason"])
        result = self.state()["qualityResults"]["issues"]
        self.assertEqual(("pass", 0, 0, 1), (result["result"], result["exitCode"], result["metric"],
                                             result["stepId"]))
        self.assertEqual("review", self.state()["phase"])

    def test_quality_files_are_archived_with_the_step(self):
        self.begin({"tests": self.counting("tests")})
        self.assertIn("entering phase 'review'", self.write_work_and_stop())
        run = os.path.join(self.base, "run")
        self.assertTrue(os.path.exists(os.path.join(run, "quality.md")))
        self.assertTrue(os.path.exists(os.path.join(run, "quality", "tests.log")))
        self.approve_step()
        self.assertEqual(2, self.state()["stepId"])
        self.assertFalse(os.path.exists(os.path.join(run, "quality.md")))
        self.assertFalse(os.path.exists(os.path.join(run, "quality")))
        archived = os.path.join(run, "steps", "1")
        self.assertIn("| tests | pass |", self.read_file(os.path.join(archived, "quality.md")))
        self.assertTrue(os.path.exists(os.path.join(archived, "quality", "tests.log")))

    def test_archiving_replaces_an_older_quality_directory(self):
        self.begin({"tests": self.counting("tests")})
        stale = os.path.join(self.base, "run", "steps", "1", "quality")
        os.makedirs(stale)
        self.write(os.path.relpath(os.path.join(stale, "old.log"), self.root), "old")
        self.write_work_and_stop()
        self.approve_step()
        archived = os.path.join(self.base, "run", "steps", "1", "quality")
        self.assertEqual(["tests.log"], os.listdir(archived))

    def read_file(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def write_work_and_stop(self):
        self.write("work-%d.txt" % self.state()["stepId"], "work\n")
        return self.stop()["reason"]

    def test_the_next_step_starts_without_the_previous_report(self):
        self.begin({"final": self.counting("final", steps="last")})
        self.write_work_and_stop()
        self.assertFalse(os.path.exists(os.path.join(self.base, "run", "quality.md")))
        self.approve_step()
        self.write_work_and_stop()
        self.assertIn("| final | pass |", self.read_file(os.path.join(self.base, "run", "quality.md")))

    def test_a_broken_metric_pauses_a_step(self):
        self.begin({"cov": {"command": "echo nothing", "metric": {"pattern": r"(\d+)", "min": 1}}})
        self.stop()
        state = self.state()
        self.assertEqual(("paused", "cov", 0), (state["status"], state["pausedFor"]["gate"], state["iter"]))
        self.assertIn("matches nothing", state["pausedFor"]["reason"])

    def test_status_has_a_line_per_gate_and_the_waived_gates(self):
        self.begin({"lint": self.counting("lint", exit_code=1), "tests": self.counting("tests")})
        self.stop()
        out = workflow.cmd_status(self.root)
        self.assertIn("[specloop] feature run", out)
        self.assertIn("quality: lint fail (phase implement, step 1)", out)
        self.assertIn("quality: tests pass (phase implement, step 1)", out)
        workflow.update_state(self.root, lambda s: s.update(qualityWaived=[{"gate": "lint", "phase": "implement",
                                                                            "stepId": 1}]))
        self.assertIn("quality waived: lint in phase implement, step 1", workflow.cmd_status(self.root))


    def test_advance_records_the_waived_gates_with_the_step(self):
        self.begin({"tests": self.counting("tests", 127)})
        self.stop()
        self.assertEqual("paused", self.state()["status"])
        workflow.cmd_advance(self.root)
        state = self.state()
        self.assertEqual("review", state["phase"])
        self.assertNotIn("pausedFor", state)
        self.assertEqual("skipped", state["history"][-1]["result"])
        self.assertEqual(1, len(state["qualityWaived"]))
        entry = state["qualityWaived"][0]
        self.assertEqual(("tests", "implement", 1), (entry["gate"], entry["phase"], entry["stepId"]))
        self.assertIn("quality waived: tests in phase implement, step 1", workflow.cmd_status(self.root))
        with open(os.path.join(self.base, "run", "quality.md"), encoding="utf-8") as f:
            self.assertIn("tests in phase implement, step 1 (waived ", f.read())

    def test_a_last_gate_is_waived_only_on_the_last_step(self):
        self.begin({"final": self.counting("final", steps="last"), "each": self.counting("each")})
        workflow.cmd_advance(self.root)
        self.assertEqual(["each"], [w["gate"] for w in self.state()["qualityWaived"]])
        self.assertEqual(0, self.marker("final"))
        self.assertEqual(0, self.marker("each"))

    def test_the_last_step_waives_the_last_gate(self):
        self.begin({"final": self.counting("final", steps="last")}, count=1)
        workflow.cmd_advance(self.root)
        self.assertEqual([("final", 1)], [(w["gate"], w["stepId"]) for w in self.state()["qualityWaived"]])


@unittest.skipUnless(gitfixture.git_available(), "git not available")
class ChangedFilesProject(unittest.TestCase):
    """A committed git repository with the kit installed in `self.root`. Holds no tests itself."""

    subdir = ""

    def setUp(self):
        self.fx = gitfixture.GitFixture()
        self.addCleanup(self.fx.cleanup)
        self.repo = self.fx.target
        self.root = os.path.join(self.repo, self.subdir) if self.subdir else self.repo
        os.makedirs(self.root, exist_ok=True)
        self.base = os.path.join(self.root, ".claude", "specloop")
        shutil.copytree(KIT, self.base)
        self.write("spec.md", "# Changed files\n\nDo things.\n")
        self.write("tracked.txt", "one\n")
        self.write("gone.txt", "bye\n")
        self.git("add", "spec.md", "tracked.txt", "gone.txt")
        self.git("commit", "-q", "-m", "initial")

    def git(self, *args):
        return self.fx.git(self.root, *args)

    def write(self, rel, text="x\n"):
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

    def set_config(self, config):
        with open(os.path.join(self.base, "config.json"), "w") as f:
            json.dump(config, f)

    def gate_log(self, name):
        with open(os.path.join(self.base, "run", "quality", name + ".log"), encoding="utf-8") as f:
            return f.read()

    def stop(self):
        return workflow.handle_stop(self.root, {})

    def changed(self, **gate):
        return workflow.changed_files(self.root, self.config(), self.state(), dict(command="exit 0", **gate))

    def marker(self, name):
        """How often the gate `name` ran: it appends a line to a marker inside .git, outside the work tree."""
        path = os.path.join(self.repo, ".git", "ran-" + name)
        if not os.path.exists(path):
            return 0
        with open(path) as f:
            return len(f.read().split())

    def counting(self, name, command, **extra):
        marker = os.path.join(self.repo, ".git", "ran-" + name).replace("\\", "/")
        return dict(extra, command="echo x >> '%s'; %s" % (marker, command))


class SingleModeBaselineTest(ChangedFilesProject):

    def start(self):
        workflow.cmd_init(self.root, "spec.md")
        return workflow.cmd_go(self.root)

    def test_first_go_records_the_base_and_untracked_files_on_a_dirty_tree(self):
        self.write("tracked.txt", "modified\n")
        self.write("scratch/notes.txt")
        self.write("other.txt")
        head = self.git("rev-parse", "HEAD")
        self.start()
        state = self.state()
        self.assertEqual("active", state["status"])
        self.assertEqual(head, state["baseRef"])
        self.assertEqual(["other.txt", "scratch/notes.txt"], state["preexistingUntracked"])

    def test_the_kit_directory_is_not_recorded_as_untracked(self):
        self.start()
        self.assertEqual([], self.state()["preexistingUntracked"])

    def test_a_later_go_keeps_the_recorded_base(self):
        self.start()
        base = self.state()["baseRef"]
        self.write("more.txt")
        self.git("add", "more.txt")
        self.git("commit", "-q", "-m", "more")
        workflow.cmd_pause(self.root)
        workflow.cmd_go(self.root)
        self.assertEqual(base, self.state()["baseRef"])
        self.assertEqual([], self.state()["preexistingUntracked"])

    def test_a_repository_without_commits_records_nothing(self):
        root = os.path.join(self.repo, "empty")
        self.fx.git(self.repo, "init", "-q", "empty")
        self.fx.configure(root)
        shutil.copytree(KIT, os.path.join(root, ".claude", "specloop"))
        with open(os.path.join(root, "spec.md"), "w") as f:
            f.write("# Spec\n")
        workflow.cmd_init(root, "spec.md")
        workflow.cmd_go(root)
        state = workflow.load_state(root)
        self.assertEqual("active", state["status"])
        self.assertNotIn("baseRef", state)
        self.assertNotIn("preexistingUntracked", state)

    def test_outside_git_nothing_is_recorded(self):
        root = tempfile.mkdtemp()
        previous = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(root)

        def restore():
            if previous is None:
                os.environ.pop("GIT_CEILING_DIRECTORIES", None)
            else:
                os.environ["GIT_CEILING_DIRECTORIES"] = previous
            shutil.rmtree(root, ignore_errors=True)
        self.addCleanup(restore)
        shutil.copytree(KIT, os.path.join(root, ".claude", "specloop"))
        with open(os.path.join(root, "spec.md"), "w") as f:
            f.write("# Spec\n")
        workflow.cmd_init(root, "spec.md")
        workflow.cmd_go(root)
        state = workflow.load_state(root)
        self.assertEqual("active", state["status"])
        self.assertNotIn("baseRef", state)
        self.assertNotIn("preexistingUntracked", state)

    def test_changed_files_lists_tracked_and_new_files_only(self):
        self.write("preexisting.txt")
        self.write("kept/old.txt")
        self.start()
        self.write("tracked.txt", "modified\n")
        self.write("new.txt")
        self.write("src/deep/created.txt")
        self.write("staged.txt")
        self.git("add", "staged.txt")
        os.remove(os.path.join(self.root, "gone.txt"))
        self.write("kept/old.txt", "edited but still untracked\n")
        self.assertEqual(["new.txt", "src/deep/created.txt", "staged.txt", "tracked.txt"], self.changed())

    def test_the_kit_directory_is_left_out(self):
        self.start()
        self.write(".claude/specloop/run/extra.txt")
        self.write(".claude/specloop/notes.md")
        self.write("new.txt")
        self.assertEqual(["new.txt"], self.changed())

    def test_a_tracked_file_that_was_dirty_at_the_start_counts_as_changed(self):
        self.write("tracked.txt", "dirty before the run\n")
        self.start()
        self.assertEqual(["tracked.txt"], self.changed())

    def test_single_mode_compares_with_the_base_ref_after_commits(self):
        self.start()
        self.write("committed.txt")
        self.git("add", "committed.txt")
        self.git("commit", "-q", "-m", "work")
        self.write("pending.txt")
        self.assertEqual(["committed.txt", "pending.txt"], self.changed())
        self.assertEqual(["committed.txt", "pending.txt"], self.changed(steps="each"))

    def test_changed_glob_narrows_the_set(self):
        self.start()
        for name in ("a.py", "docs/b.md", "src/c.py", "d.txt"):
            self.write(name)
        self.assertEqual(["a.py", "d.txt", "docs/b.md", "src/c.py"], self.changed())
        self.assertEqual(["a.py", "src/c.py"], self.changed(changedGlob=["*.py"]))
        self.assertEqual(["a.py", "docs/b.md"], self.changed(changedGlob=["a.*", "docs/*.md"]))
        self.assertEqual([], self.changed(changedGlob=["*.kt"]))

    def test_a_missing_base_ref_falls_back_to_head(self):
        self.start()
        self.write("new.txt")
        state = dict(self.state())
        state.pop("baseRef")
        self.assertEqual(["new.txt"], workflow.changed_files(self.root, self.config(), state, {}))

    def test_an_unknown_base_ref_is_an_error(self):
        self.start()
        state = dict(self.state(), baseRef="0" * 40)
        with self.assertRaises(workflow.ChangedFilesError):
            workflow.changed_files(self.root, self.config(), state, {})


class ProjectInSubdirectoryTest(ChangedFilesProject):
    subdir = "proj"

    def test_paths_are_relative_to_the_project_and_files_outside_it_are_left_out(self):
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        self.write("../outside.txt")
        self.write("lib/new.txt")
        self.write("tracked.txt", "modified\n")
        self.assertEqual(["lib/new.txt", "tracked.txt"], self.changed())

    def test_preexisting_untracked_files_are_excluded(self):
        self.write("early.txt")
        self.write("../early-outside.txt")
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        self.assertEqual(["early-outside.txt", "proj/early.txt"], self.state()["preexistingUntracked"])
        self.write("late.txt")
        self.assertEqual(["late.txt"], self.changed())


class ChangedFilesGateTest(ChangedFilesProject):
    """The placeholders in a gate's command, in single mode."""

    def start_implement(self, gates):
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": "exit 0"}}},
                         "quality": {"gates": gates}})
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        self.write(self.run_file("plan.md"), "- [ ] step")
        self.assertIn("entering phase 'implement'", self.stop()["reason"])

    def report(self):
        with open(os.path.join(self.base, "run", "quality.md"), encoding="utf-8") as f:
            return f.read()

    def test_the_list_is_expanded_in_the_command(self):
        self.start_implement({"lint": {"command": 'for f in {changed_files}; do echo "[$f]"; done'}})
        self.write("a.py")
        self.write("dir/b.py")
        self.assertIn("entering phase 'review'", self.stop()["reason"])
        self.assertEqual("[a.py]\n[dir/b.py]\n", self.gate_log("lint"))

    def test_paths_with_spaces_are_quoted(self):
        self.start_implement({"lint": {"command": 'for f in {changed_files}; do echo "[$f]"; done'}})
        self.write("my file.py")
        self.write("it's.py")
        self.stop()
        self.assertEqual("[it's.py]\n[my file.py]\n", self.gate_log("lint"))

    def test_an_empty_set_skips_the_gate_without_running_it(self):
        self.start_implement({"lint": self.counting("lint", "echo ran {changed_files}")})
        out = self.stop()
        self.assertIn("entering phase 'review'", out["reason"])
        self.assertEqual(0, self.marker("lint"))
        result = self.state()["qualityResults"]["lint"]
        self.assertEqual("skipped", result["result"])
        self.assertIn("no changed files", result["reason"])
        self.assertIn("did not run", self.gate_log("lint"))
        self.assertEqual("active", self.state()["status"])

    def test_a_glob_without_a_match_skips_the_gate(self):
        self.start_implement({"lint": self.counting("lint", "echo ran {changed_files}", changedGlob=["*.kt"])})
        self.write("a.py")
        self.stop()
        self.assertEqual(0, self.marker("lint"))
        result = self.state()["qualityResults"]["lint"]
        self.assertEqual("skipped", result["result"])
        self.assertIn("changedGlob", result["reason"])

    def test_a_glob_narrows_what_the_command_gets(self):
        self.start_implement({"lint": {"command": "echo {changed_files}", "changedGlob": ["*.kt"]}})
        self.write("a.kt")
        self.write("b.py")
        self.stop()
        self.assertEqual("a.kt\n", self.gate_log("lint"))

    def test_a_command_without_placeholders_runs_when_nothing_changed(self):
        self.start_implement({"tests": self.counting("tests", "exit 0", changedGlob=["*.kt"])})
        self.stop()
        self.assertEqual(1, self.marker("tests"))
        self.assertEqual("pass", self.state()["qualityResults"]["tests"]["result"])

    def test_a_skipped_gate_does_not_hide_a_failing_one(self):
        self.start_implement({"lint": {"command": "echo {changed_files}"}, "tests": {"command": "exit 1"}})
        out = self.stop()
        self.assertIn("Quality gates failed: tests", out["reason"])
        self.assertEqual("skipped", self.state()["qualityResults"]["lint"]["result"])
        self.assertIn("| lint | skipped |", self.report())

    def test_a_list_over_the_limit_makes_the_gate_broken(self):
        self.start_implement({"lint": self.counting("lint", "echo {changed_files}")})
        for number in range(100):
            self.write("src/%s-%03d.py" % ("n" * 90, number))
        self.stop()
        state = self.state()
        self.assertEqual("paused", state["status"])
        self.assertEqual("lint", state["pausedFor"]["gate"])
        self.assertIn("{changed_files_file}", state["pausedFor"]["reason"])
        self.assertEqual(0, self.marker("lint"))
        self.assertEqual("broken", state["qualityResults"]["lint"]["result"])
        self.assertEqual("implement", state["phase"])

    def test_a_list_within_the_limit_still_runs(self):
        self.start_implement({"lint": {"command": "echo {changed_files}"}})
        for number in range(75):
            self.write("d/%03d-%s.py" % (number, "f" * 90))
        size = len(" ".join(self.changed()))
        self.assertTrue(7000 < size <= workflow.MAX_CHANGED_FILES_CHARS, size)
        self.stop()
        self.assertEqual("active", self.state()["status"])
        self.assertEqual("pass", self.state()["qualityResults"]["lint"]["result"])

    def test_the_file_placeholder_names_a_file_with_one_path_per_line(self):
        self.start_implement({"lint": {"command": "echo {changed_files_file}; cat {changed_files_file}"}})
        self.write("my file.py")
        self.write("dir/b.py")
        self.stop()
        lines = self.gate_log("lint").splitlines()
        self.assertEqual(".claude/specloop/run/quality/lint.files", lines[0])
        self.assertEqual(["dir/b.py", "my file.py"], lines[1:])
        with open(os.path.join(self.base, "run", "quality", "lint.files"), "rb") as f:
            self.assertEqual(b"dir/b.py\nmy file.py\n", f.read())

    def test_the_file_placeholder_works_for_a_list_over_the_limit(self):
        self.start_implement({"lint": {"command": "wc -l < {changed_files_file}"}})
        for number in range(100):
            self.write("src/%s-%03d.py" % ("n" * 90, number))
        self.stop()
        self.assertEqual("active", self.state()["status"])
        self.assertEqual(100, int(self.gate_log("lint").strip()))

    def test_both_placeholders_can_be_used_together(self):
        self.start_implement({"lint": {"command": "echo {changed_files} :: {changed_files_file}"}})
        self.write("a.py")
        self.stop()
        self.assertEqual("a.py :: .claude/specloop/run/quality/lint.files\n", self.gate_log("lint"))

    def test_a_git_failure_makes_the_gate_broken(self):
        self.start_implement({"lint": self.counting("lint", "echo {changed_files}")})
        self.write("a.py")

        def corrupt(state):
            state["baseRef"] = "0" * 40
        workflow.update_state(self.root, corrupt)
        self.stop()
        state = self.state()
        self.assertEqual("paused", state["status"])
        self.assertIn("cannot be determined", state["pausedFor"]["reason"])
        self.assertEqual(0, self.marker("lint"))


class FeatureChangedFilesTest(ChangedFilesProject):
    """The comparison point in feature mode: HEAD for `each`, baseRef for `last`."""

    def begin(self):
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": "exit 0"}}},
                         "quality": {"gates": {
                             "each": {"command": "echo {changed_files}"},
                             "last": {"command": "echo {changed_files}", "steps": "last"},
                             "later": {"command": "cat {changed_files_file}", "steps": "last"}}}})
        self.write("early-untracked.txt")
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        workflow.cmd_go(self.root)
        self.write(self.run_file("plan.md"), "# Plan")
        steps = {"feature": "F", "steps": [step_entry(1), step_entry(2)]}
        self.write(self.run_file("steps.json"), json.dumps(steps))
        self.stop()
        workflow.cmd_go(self.root)
        self.assertEqual(("implement", 1), (self.state()["phase"], self.state()["stepId"]))

    def changed_by(self, gate_name):
        gate = self.config()["quality"]["gates"][gate_name]
        return workflow.changed_files(self.root, self.config(), self.state(), gate)

    def test_each_compares_with_head_and_last_with_the_base_ref(self):
        self.begin()
        self.write("one.txt")
        self.assertEqual(["one.txt"], self.changed_by("each"))
        self.assertEqual(["one.txt"], self.changed_by("last"))
        self.assertEqual("review", (self.stop(), self.state())[1]["phase"])
        attempt = os.path.relpath(workflow.attempt_path(self.root, self.state()), self.root)
        self.write(os.path.join(attempt, "review.md"), "Fine.\nVERDICT: APPROVE")
        self.stop()
        self.assertEqual(2, self.state()["stepId"])
        self.write("two.txt")
        self.assertEqual(["two.txt"], self.changed_by("each"))
        self.assertEqual(["one.txt", "two.txt"], self.changed_by("last"))

    def test_files_untracked_before_the_run_are_left_out(self):
        self.begin()
        self.assertEqual([], self.changed_by("each"))
        self.assertEqual([], self.changed_by("last"))
        self.assertIn("early-untracked.txt", self.state()["preexistingUntracked"])

    def test_the_list_reaches_the_gate_command(self):
        self.begin()
        self.write("one.txt")
        self.stop()
        self.assertEqual("review", self.state()["phase"])
        self.assertEqual("one.txt\n", self.gate_log("each"))
        self.assertNotIn("last", self.state()["qualityResults"])
        run = os.path.join(self.base, "run", "quality")
        self.assertEqual(["each.log"], sorted(os.listdir(run)))


class ForbidTest(ChangedFilesProject):
    """`forbid`: added lines that match a pattern fail the gate before its command runs."""

    PATTERN = {"pattern": "@Suppress\\(", "glob": ["*.kt", "*.kts"], "message": "Fix the finding instead."}
    OLD = 'fun a() {}\n@Suppress("old")\nfun b() {}\n'

    def setUp(self):
        super().setUp()
        self.write("src/Old.kt", self.OLD)
        self.git("add", "src/Old.kt")
        self.git("commit", "-q", "-m", "legacy")

    def start_implement(self, forbid=None, **extra):
        gate = self.counting("lint", "exit 0", forbid=[self.PATTERN] if forbid is None else forbid, **extra)
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": "exit 0"}}},
                         "quality": {"gates": {"lint": gate}}})
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        self.write(self.run_file("plan.md"), "- [ ] step")
        self.assertIn("entering phase 'implement'", self.stop()["reason"])

    def hits(self, **kwargs):
        return workflow.forbid_hits(self.root, self.config(), self.state(),
                                    self.config()["quality"]["gates"]["lint"], **kwargs)

    def places(self, hits):
        return [(h["path"], h["line"]) for h in hits]

    def test_an_added_line_fails_the_gate_with_path_line_and_message(self):
        self.start_implement()
        self.write("src/Old.kt", self.OLD + '    @Suppress("new")\n')
        out = self.stop()
        self.assertEqual("block", out["decision"])
        self.assertIn("Quality gates failed: lint", out["reason"])
        self.assertIn('src/Old.kt:4: @Suppress("new")', out["reason"])
        self.assertIn("Fix the finding instead.", out["reason"])
        self.assertIn("/sl-quality accept", out["reason"])
        self.assertEqual(0, self.marker("lint"))
        self.assertIn("src/Old.kt:4", self.gate_log("lint"))
        state = self.state()
        self.assertEqual("implement", state["phase"])
        self.assertEqual("fail", state["qualityResults"]["lint"]["result"])
        self.assertTrue(os.path.exists(os.path.join(self.base, "run", "last-failure.md")))

    def test_line_numbers_follow_the_hunks(self):
        self.start_implement()
        self.write("src/Old.kt", '// top\nfun a() {}\n@Suppress("old")\n@Suppress("x")\nfun b() {}\n@Suppress("y")\n')
        found = [(h["path"], h["line"], h["text"]) for h in self.hits()]
        self.assertEqual([("src/Old.kt", 4, '@Suppress("x")'), ("src/Old.kt", 6, '@Suppress("y")')], found)

    def test_lines_that_existed_before_the_comparison_point_do_not_count(self):
        self.start_implement()
        self.write("src/Old.kt", self.OLD + "fun c() {}\n")
        self.assertEqual([], self.hits())
        out = self.stop()
        self.assertIn("entering phase 'review'", out["reason"])
        self.assertEqual(1, self.marker("lint"))

    def test_a_line_in_a_new_untracked_file_counts(self):
        self.start_implement()
        self.write("src/New.kt", 'fun n() {}\n\n@Suppress("n")\n')
        out = self.stop()
        self.assertIn('src/New.kt:3: @Suppress("n")', out["reason"])
        self.assertEqual(0, self.marker("lint"))

    def test_a_file_untracked_before_the_run_does_not_count(self):
        self.write("src/Early.kt", '@Suppress("early")\n')
        self.start_implement()
        self.assertEqual([], self.hits())
        self.assertIn("entering phase 'review'", self.stop()["reason"])

    def test_a_committed_addition_still_counts_against_the_base_ref(self):
        self.start_implement()
        self.write("src/New.kt", '@Suppress("n")\n')
        self.git("add", "src/New.kt")
        self.git("commit", "-q", "-m", "sneaky")
        self.assertEqual([("src/New.kt", 1)], self.places(self.hits()))

    def test_glob_limits_a_pattern_to_matching_files(self):
        self.start_implement()
        self.write("notes.txt", '@Suppress("in a note")\n')
        self.write("src/build.gradle.kts", '@Suppress("kts")\n')
        self.assertEqual(["src/build.gradle.kts"], [h["path"] for h in self.hits()])
        self.write("src/build.gradle.kts", "ok\n")
        self.assertIn("entering phase 'review'", self.stop()["reason"])

    def test_an_entry_without_glob_applies_to_every_file(self):
        self.start_implement(forbid=["TODO"])
        self.write("a.txt", "x\nTODO later\n")
        self.assertEqual([("a.txt", 2)], self.places(self.hits()))

    def test_every_matching_entry_reports_its_own_message(self):
        self.start_implement(forbid=[{"pattern": "TODO", "message": "No TODOs."},
                                     {"pattern": "FIXME", "message": "No FIXMEs."}])
        self.write("a.txt", "TODO one\nFIXME two\n")
        out = self.stop()
        self.assertIn("a.txt:1: TODO one\n  No TODOs.", out["reason"])
        self.assertIn("a.txt:2: FIXME two\n  No FIXMEs.", out["reason"])

    def test_a_hit_in_the_accepted_list_is_not_reported(self):
        self.start_implement()
        self.write("src/A.kt", '@Suppress("a")\n')
        self.write("src/B.kt", '@Suppress("b")\n')
        digest = workflow.forbid_hit_hash("src/A.kt", '@Suppress("a")')
        self.assertEqual(hashlib.sha256(b'src/A.kt\0@Suppress("a")').hexdigest(), digest)
        workflow.update_state(self.root, lambda s: s.update(qualityAccepted=[digest]))
        self.assertEqual(["src/B.kt"], [h["path"] for h in self.hits()])
        self.assertEqual(["src/A.kt", "src/B.kt"], [h["path"] for h in self.hits(include_accepted=True)])
        reason = self.stop()["reason"]
        self.assertIn("src/B.kt:1", reason)
        self.assertNotIn("src/A.kt", reason)

    def test_when_every_hit_is_accepted_the_gate_passes(self):
        self.start_implement()
        self.write("src/A.kt", '@Suppress("a")\n')
        digest = workflow.forbid_hit_hash("src/A.kt", '@Suppress("a")')
        workflow.update_state(self.root, lambda s: s.update(qualityAccepted=[digest]))
        self.assertIn("entering phase 'review'", self.stop()["reason"])
        self.assertEqual(1, self.marker("lint"))

    def test_an_accepted_line_in_another_file_is_still_reported(self):
        self.start_implement()
        self.write("src/B.kt", '@Suppress("a")\n')
        digest = workflow.forbid_hit_hash("src/A.kt", '@Suppress("a")')
        workflow.update_state(self.root, lambda s: s.update(qualityAccepted=[digest]))
        self.assertEqual(["src/B.kt"], [h["path"] for h in self.hits()])

    def test_a_warn_gate_reports_a_hit_without_blocking(self):
        self.start_implement(level="warn")
        self.write("src/A.kt", '@Suppress("a")\n')
        out = self.stop()
        self.assertIn("entering phase 'review'", out["reason"])
        self.assertEqual(0, self.marker("lint"))
        state = self.state()
        self.assertEqual("warn", state["qualityResults"]["lint"]["result"])
        self.assertEqual("active", state["status"])
        with open(os.path.join(self.base, "run", "quality.md"), encoding="utf-8") as f:
            self.assertIn("src/A.kt:1", f.read())

    def test_protected_changes_and_forbidden_lines_are_reported_together(self):
        self.write("rules.yml", "a\n")
        self.git("add", "rules.yml")
        self.git("commit", "-q", "-m", "rules")
        self.start_implement(protect=["rules.yml"])
        self.write("rules.yml", "b\n")
        self.write("src/A.kt", '@Suppress("a")\n')
        out = self.stop()
        self.assertIn("rules.yml (changed)", out["reason"])
        self.assertIn("src/A.kt:1", out["reason"])
        self.assertEqual(0, self.marker("lint"))

    def test_binary_files_are_skipped_and_line_endings_removed(self):
        self.start_implement(forbid=["TODO"])
        with open(os.path.join(self.root, "blob.bin"), "wb") as f:
            f.write(b"\0TODO\0")
        with open(os.path.join(self.root, "dos.txt"), "wb") as f:
            f.write(b"ok\r\nTODO\r\n")
        self.assertEqual([("dos.txt", 2, "TODO")], [(h["path"], h["line"], h["text"]) for h in self.hits()])

    def test_a_binary_tracked_file_is_skipped(self):
        with open(os.path.join(self.root, "blob.bin"), "wb") as f:
            f.write(b"\0\1\2")
        self.git("add", "blob.bin")
        self.git("commit", "-q", "-m", "bin")
        self.start_implement(forbid=["TODO"])
        with open(os.path.join(self.root, "blob.bin"), "wb") as f:
            f.write(b"\0TODO\0")
        self.assertEqual([], self.hits())

    def test_a_gate_without_forbid_scans_nothing(self):
        self.assertEqual([], workflow.forbid_hits(self.root, self.config(), {}, {"command": "exit 0"}))

    def test_a_git_failure_makes_the_gate_broken(self):
        self.start_implement()
        self.write("src/A.kt", '@Suppress("a")\n')
        workflow.update_state(self.root, lambda s: s.update(baseRef="0" * 40))
        self.stop()
        state = self.state()
        self.assertEqual("paused", state["status"])
        self.assertIn("added lines cannot be determined", state["pausedFor"]["reason"])
        self.assertEqual(0, self.marker("lint"))

    def test_fail_fast_stops_after_a_forbid_failure(self):
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": "exit 0"}}},
                         "quality": {"failFast": True, "gates": {
                             "lint": self.counting("lint", "exit 0", forbid=["TODO"]),
                             "tests": self.counting("tests", "exit 0")}}})
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        self.write(self.run_file("plan.md"), "- [ ] step")
        self.stop()
        self.write("a.txt", "TODO\n")
        self.stop()
        self.assertEqual(0, self.marker("tests"))


class FeatureForbidTest(ChangedFilesProject):
    """In feature mode a gate that runs on each step compares with HEAD: earlier steps' lines do not count."""

    def test_only_lines_added_in_the_current_step_count(self):
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": "exit 0"}}},
                         "quality": {"gates": {"lint": {"command": "exit 0", "forbid": ["TODO"]},
                                               "whole": {"command": "exit 0", "forbid": ["TODO"], "steps": "last"}}}})
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        workflow.cmd_go(self.root)
        self.write(self.run_file("plan.md"), "# Plan")
        self.write(self.run_file("steps.json"), json.dumps({"feature": "F", "steps": [step_entry(1), step_entry(2)]}))
        self.stop()
        workflow.cmd_go(self.root)
        self.write("one.txt", "TODO one\n")
        config, state = self.config(), self.state()

        def places(name):
            hits = workflow.forbid_hits(self.root, config, state, config["quality"]["gates"][name])
            return [(h["path"], h["line"]) for h in hits]
        self.assertEqual([("one.txt", 1)], places("lint"))
        self.git("add", "one.txt")
        self.git("commit", "-q", "-m", "step 1")
        self.assertEqual([], places("lint"))
        self.assertEqual([("one.txt", 1)], places("whole"))


def value_gate(**extra):
    """A gate whose metric is the number in value.txt, read from its output."""
    return dict({"command": "echo value: $(cat value.txt)", "metric": {"pattern": r"value: ([0-9.]+)"}}, **extra)


def ratchet_gate(limit_key="min", limit=80, **extra):
    gate = value_gate(legacy="ratchet", **extra)
    gate["metric"] = dict(gate["metric"], **{limit_key: limit})
    return gate


class RatchetTest(GateProject):
    """legacy: "ratchet" gates are measured on the Stop that passes plan and compared with that value."""

    def write_value(self, value):
        with open(os.path.join(self.root, "value.txt"), "w") as f:
            f.write(str(value))

    def start_plan(self, gates, quality=None, value=70):
        self.configure(gates, quality)
        self.write_value(value)
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        with open(self.run_path("plan.md"), "w") as f:
            f.write("- [ ] step")

    def start_implement_ratchet(self, gates, quality=None, value=70):
        self.start_plan(gates, quality, value)
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'implement'", out["reason"])

    def stop_with(self, value):
        self.write_value(value)
        return workflow.handle_stop(self.root, {})

    def gate_result(self, name="cov"):
        return self.state()["qualityResults"][name]

    # legacy

    def test_legacy_defaults_to_baseline(self):
        self.configure({"g": {"command": "exit 0"}})
        self.assertEqual("baseline", workflow.gate_legacy(self.config(), {"command": "exit 0"}))

    def test_legacy_can_be_set_per_gate_and_overrides_the_block(self):
        self.configure({}, {"legacy": "ratchet"})
        config = self.config()
        self.assertEqual("ratchet", workflow.gate_legacy(config, {"command": "x"}))
        self.assertEqual("baseline", workflow.gate_legacy(config, {"command": "x", "legacy": "baseline"}))
        self.configure({}, {"legacy": "baseline"})
        self.assertEqual("ratchet", workflow.gate_legacy(self.config(), {"command": "x", "legacy": "ratchet"}))

    def test_the_block_default_makes_a_gate_with_a_metric_a_ratchet(self):
        self.start_implement_ratchet({"cov": value_gate(metric={"pattern": r"value: ([0-9.]+)", "min": 80})},
                                     {"legacy": "ratchet"})
        self.assertEqual({"cov": 70}, self.state()["qualityBaseline"])

    def test_a_baseline_gate_is_not_measured_and_keeps_its_limit(self):
        self.start_implement_ratchet({"cov": value_gate(metric={"pattern": r"value: ([0-9.]+)", "min": 80})})
        state = self.state()
        self.assertNotIn("qualityBaseline", state)
        self.assertNotIn("qualityResults", state)
        self.assertFalse(os.path.exists(self.run_path("quality", "cov.log")))
        out = workflow.handle_stop(self.root, {})
        self.assertIn("metric 70 is below the minimum 80", out["reason"])
        self.assertNotIn("baseline", out["reason"])

    # measuring

    def test_the_value_is_measured_on_the_stop_that_passes_plan(self):
        self.start_plan({"cov": ratchet_gate()})
        self.assertNotIn("qualityBaseline", self.state())
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'implement'", out["reason"])
        state = self.state()
        self.assertEqual({"cov": 70}, state["qualityBaseline"])
        self.assertNotIn("qualityResults", state)

    def test_a_failing_plan_check_measures_nothing(self):
        self.start_plan({"cov": ratchet_gate()})
        os.remove(self.run_path("plan.md"))
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertNotIn("qualityBaseline", self.state())

    def test_the_value_is_measured_once_per_run(self):
        self.start_implement_ratchet({"cov": ratchet_gate()})
        self.write_value(90)
        workflow.handle_stop(self.root, {})
        self.assertEqual({"cov": 70}, self.state()["qualityBaseline"])

    def test_a_value_that_is_already_stored_is_kept(self):
        outcome_state = {"qualityBaseline": {"a": 5}}
        workflow.record_baseline(outcome_state, {"a": 9, "b": 7})
        self.assertEqual({"a": 5, "b": 7}, outcome_state["qualityBaseline"])
        workflow.record_baseline(outcome_state, None)
        self.assertEqual({"a": 5, "b": 7}, outcome_state["qualityBaseline"])

    def test_every_ratchet_gate_is_measured_and_no_other(self):
        gates = {"first": ratchet_gate(), "other": ratchet_gate("max", 90, steps="last"),
                 "plain": value_gate(metric={"pattern": r"value: ([0-9.]+)", "min": 1})}
        self.start_implement_ratchet(gates)
        self.assertEqual({"first": 70, "other": 70}, self.state()["qualityBaseline"])

    def test_the_measurement_does_not_apply_the_limit_or_the_exit_code(self):
        gate = ratchet_gate(command="echo value: $(cat value.txt); exit 1")
        self.start_implement_ratchet({"cov": gate})
        self.assertEqual({"cov": 70}, self.state()["qualityBaseline"])

    def test_the_gate_that_is_skipped_for_lack_of_changed_files_is_not_measured(self):
        self.git_init()
        gate = ratchet_gate(command="echo value: $(cat value.txt) {changed_files}", changedGlob=["*.nothing"])
        self.start_implement_ratchet({"cov": gate})
        self.assertNotIn("qualityBaseline", self.state())

    # the comparison

    def test_a_min_gate_passes_at_the_baseline_and_above(self):
        self.start_implement_ratchet({"cov": ratchet_gate()})
        out = self.stop_with(70)
        self.assertIn("entering phase 'review'", out["reason"])
        result = self.gate_result()
        self.assertEqual(("pass", 70, 70), (result["result"], result["metric"], result["baseline"]))
        self.assertIn("metric 70 is below the minimum 80, but no worse than the baseline 70 (ratchet)",
                      self.read(self.run_path("quality", "cov.log")))

    def test_a_min_gate_passes_above_the_baseline(self):
        self.start_implement_ratchet({"cov": ratchet_gate()})
        self.assertIn("entering phase 'review'", self.stop_with(75)["reason"])

    def test_a_min_gate_passes_when_the_limit_is_met_even_below_the_baseline(self):
        self.start_implement_ratchet({"cov": ratchet_gate()}, value=95)
        self.assertIn("entering phase 'review'", self.stop_with(85)["reason"])

    def test_a_min_gate_fails_below_the_baseline_with_value_limit_and_baseline(self):
        self.start_implement_ratchet({"cov": ratchet_gate()})
        before = self.state()["iter"]
        out = self.stop_with(65)
        self.assertEqual("block", out["decision"])
        self.assertIn("metric 65 is below the minimum 80 and below the baseline 70 (ratchet)", out["reason"])
        state = self.state()
        self.assertEqual(("implement", before + 1), (state["phase"], state["iter"]))
        self.assertEqual("fail", self.gate_result()["result"])

    def test_a_max_gate_passes_at_the_baseline_and_below_and_fails_above(self):
        self.start_implement_ratchet({"cov": ratchet_gate("max", 3)}, value=10)
        out = self.stop_with(11)
        self.assertIn("metric 11 is above the maximum 3 and above the baseline 10 (ratchet)", out["reason"])
        self.assertIn("entering phase 'review'", self.stop_with(10)["reason"])

    def test_a_max_gate_passes_below_the_baseline(self):
        self.start_implement_ratchet({"cov": ratchet_gate("max", 3)}, value=10)
        self.assertIn("entering phase 'review'", self.stop_with(4)["reason"])

    def test_a_ratchet_gate_at_level_warn_only_warns_below_the_baseline(self):
        self.start_implement_ratchet({"cov": ratchet_gate(level="warn")})
        out = self.stop_with(60)
        self.assertIn("entering phase 'review'", out["reason"])
        self.assertEqual("warn", self.gate_result()["result"])

    def test_the_exit_code_still_decides_for_a_ratchet_gate(self):
        gate = ratchet_gate(command="echo value: $(cat value.txt); exit 1")
        self.start_implement_ratchet({"cov": gate})
        out = self.stop_with(90)
        self.assertEqual("block", out["decision"])
        self.assertIn("Quality gates failed: cov", out["reason"])

    # an unreadable baseline

    def test_an_unreadable_value_pauses_the_run_before_the_plan_passes(self):
        gate = ratchet_gate(command="echo nothing here")
        self.start_plan({"cov": gate})
        out = workflow.handle_stop(self.root, {})
        self.assertNotIn("decision", out)
        state = self.state()
        self.assertEqual(("paused", "plan", 0), (state["status"], state["phase"], state["iter"]))
        self.assertEqual("cov", state["pausedFor"]["gate"])
        self.assertIn("baseline could not be measured before the first edit", state["pausedFor"]["reason"])
        self.assertIn("matches nothing", state["pausedFor"]["reason"])
        self.assertNotIn("qualityBaseline", state)
        self.assertEqual("broken", state["qualityResults"]["cov"]["result"])
        self.assertIn("cov", out["systemMessage"])
        self.assertFalse(os.path.exists(self.run_path("last-failure.md")))

    def test_go_retries_the_measurement(self):
        self.start_plan({"cov": ratchet_gate(command="cat missing.txt")})
        workflow.handle_stop(self.root, {})
        self.assertEqual("paused", self.state()["status"])
        self.configure({"cov": ratchet_gate()})
        workflow.cmd_go(self.root)
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'implement'", out["reason"])
        self.assertEqual({"cov": 70}, self.state()["qualityBaseline"])

    def test_advance_skips_the_measurement_and_the_gate_uses_its_limit_only(self):
        self.start_plan({"cov": ratchet_gate(command="cat missing.txt")})
        workflow.handle_stop(self.root, {})
        workflow.cmd_advance(self.root)
        workflow.cmd_go(self.root)
        state = self.state()
        self.assertEqual("implement", state["phase"])
        self.assertNotIn("qualityBaseline", state)

    def test_values_measured_before_a_broken_gate_are_kept(self):
        gates = {"good": ratchet_gate(), "bad": ratchet_gate(command="echo none")}
        self.start_plan(gates)
        workflow.handle_stop(self.root, {})
        state = self.state()
        self.assertEqual("bad", state["pausedFor"]["gate"])
        self.assertEqual({"good": 70}, state["qualityBaseline"])

    def test_a_command_that_is_not_found_pauses_the_run(self):
        self.start_plan({"cov": ratchet_gate(command="no-such-tool-for-specloop")})
        workflow.handle_stop(self.root, {})
        self.assertIn("command not found", self.state()["pausedFor"]["reason"])

    # no baseline

    def test_without_a_baseline_the_gate_uses_its_limit_only_and_the_report_says_so(self):
        self.start_implement_ratchet({"plain": {"command": "exit 0"}})
        self.configure({"cov": ratchet_gate()})
        self.accept_quality()
        out = workflow.handle_stop(self.root, {})
        self.assertIn("metric 70 is below the minimum 80", out["reason"])
        self.assertNotIn("baseline", out["reason"])
        self.assertNotIn("qualityBaseline", self.state())
        report = self.read(self.run_path("quality.md"))
        self.assertIn("## Ratchet baselines", report)
        self.assertIn("- cov: no baseline was measured, so the gate uses its limit only.", report)

    def test_the_report_lists_the_measured_baseline(self):
        self.start_implement_ratchet({"cov": ratchet_gate()})
        workflow.handle_stop(self.root, {})
        report = self.read(self.run_path("quality.md"))
        self.assertIn("- cov: baseline 70, measured before the first edit.", report)

    def test_the_report_has_no_baseline_section_without_ratchet_gates(self):
        self.start_implement_ratchet({"cov": value_gate(metric={"pattern": r"value: ([0-9.]+)", "min": 1})})
        self.stop_with(70)
        self.assertNotIn("Ratchet baselines", self.read(self.run_path("quality.md")))

    def test_a_graph_without_a_plan_phase_has_no_baseline(self):
        self.configure({"cov": ratchet_gate()})
        self.write_value(70)
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        state = self.state()
        self.assertTrue(workflow.baseline_phase(self.config(), dict(state, phase="plan")))
        self.assertFalse(workflow.baseline_phase(self.config(), dict(state, phase="implement")))
        self.assertFalse(workflow.baseline_phase(self.config(), dict(state, phase="review")))

    # the time budget

    def test_the_measurement_runs_within_the_quality_budget(self):
        self.start_plan({"cov": ratchet_gate()}, {"timeoutSec": 7})
        timeouts = []
        original = workflow.run_limited

        def spy(args, timeout, *a, **kw):
            timeouts.append(timeout)
            return original(args, timeout, *a, **kw)

        with mock.patch.object(workflow, "run_limited", side_effect=spy):
            out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'implement'", out["reason"])
        self.assertEqual([7], timeouts)

    def test_the_measurements_share_one_budget(self):
        self.start_plan({"first": ratchet_gate(), "second": ratchet_gate()}, {"timeoutSec": 4})
        clock = iter([0, 0, 10])  # start, first gate, second gate: the first gate "took" 10 s
        with mock.patch.object(workflow, "_clock", side_effect=lambda: next(clock)):
            out = workflow.handle_stop(self.root, {})
        state = self.state()
        self.assertNotIn("decision", out)
        self.assertEqual("second", state["pausedFor"]["gate"])
        self.assertIn("time budget", state["pausedFor"]["reason"])
        self.assertEqual({"first": 70}, state["qualityBaseline"])

    def test_a_hung_measurement_is_killed_and_pauses_the_run(self):
        sleeper = '"%s" -c "import time; time.sleep(30)"' % sys.executable.replace("\\", "/")
        self.start_plan({"cov": ratchet_gate(command=sleeper)}, {"timeoutSec": 1})
        started = time.monotonic()
        workflow.handle_stop(self.root, {})
        self.assertLess(time.monotonic() - started, 20)
        self.assertIn("timed out", self.state()["pausedFor"]["reason"])

    # a dropped result

    def test_a_dropped_result_stores_no_baseline(self):
        self.start_plan({"cov": ratchet_gate()})
        original = workflow.run_quality_gate

        def change_the_workflow(*args, **kwargs):
            result = original(*args, **kwargs)
            workflow.update_state(self.root, lambda current: current.update(attempt=current.get("attempt", 0) + 1))
            return result

        with mock.patch.object(workflow, "run_quality_gate", side_effect=change_the_workflow):
            workflow.handle_stop(self.root, {})
        self.assertNotIn("qualityBaseline", self.state())
        self.assertEqual("plan", self.state()["phase"])


class ProtectionTest(GateProject):
    """`protect` and the quality block are fingerprinted on the first /go and compared before a gate's command."""

    def setUp(self):
        super().setUp()
        self.write("detekt.yml", "maxIssues: 0\n")
        self.write("config/detekt/a.yml", "a\n")
        self.write("config/detekt/sub/b.yml", "b\n")

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def sha(self, text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def ran(self, name="ran.txt"):
        return self.exists(name)

    def protecting(self, protect=("detekt.yml", "config/detekt/", "missing.yml"), **extra):
        return dict(extra, command="echo x >> ran.txt", protect=list(protect))

    def fingerprints(self):
        return self.state()["qualityFingerprints"]

    # what the first /go stores

    def test_first_go_stores_hashes_of_files_directories_and_missing_files(self):
        self.configure({"lint": self.protecting()})
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        protected = self.fingerprints()["protected"]
        self.assertEqual({"detekt.yml": self.sha("maxIssues: 0\n")}, protected["detekt.yml"])
        self.assertEqual({"config/detekt/a.yml": self.sha("a\n"), "config/detekt/sub/b.yml": self.sha("b\n")},
                         protected["config/detekt"])
        self.assertEqual({"missing.yml": None}, protected["missing.yml"])

    def test_first_go_stores_the_fingerprint_of_the_quality_block(self):
        self.configure({"lint": self.protecting()})
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        text = json.dumps(self.config()["quality"], sort_keys=True)
        self.assertEqual(self.sha(text), self.fingerprints()["quality"])

    def test_a_gate_set_to_null_does_not_change_the_block_fingerprint(self):
        self.configure({"lint": {"command": "exit 0"}})
        before = workflow.quality_block_fingerprint(self.config())
        self.write_json("config.local.json", {"quality": {"gates": {"extra": None}}})
        self.assertEqual(before, workflow.quality_block_fingerprint(self.config()))
        self.write_json("config.local.json", {"quality": {"gates": {"extra": {"command": "exit 0"}}}})
        self.assertNotEqual(before, workflow.quality_block_fingerprint(self.config()))

    def test_a_project_without_gates_still_stores_the_block_fingerprint(self):
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        self.assertEqual({}, self.fingerprints()["protected"])
        self.assertIsInstance(self.fingerprints()["quality"], str)

    def test_a_later_go_keeps_the_fingerprints_of_the_first(self):
        self.configure({"lint": self.protecting()})
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        first = self.fingerprints()
        self.write("detekt.yml", "maxIssues: 99\n")
        workflow.cmd_pause(self.root)
        workflow.cmd_go(self.root)
        self.assertEqual(first, self.fingerprints())

    def test_a_run_with_no_fingerprints_gets_them_on_a_later_go(self):
        self.configure({"lint": self.protecting()})
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        workflow.update_state(self.root, lambda s: s.pop("qualityFingerprints"))
        workflow.cmd_pause(self.root)
        workflow.cmd_go(self.root)
        self.assertEqual({"detekt.yml": self.sha("maxIssues: 0\n")}, self.fingerprints()["protected"]["detekt.yml"])

    # protected files

    def test_unchanged_protected_files_let_the_command_run(self):
        self.configure({"lint": self.protecting()})
        self.start_implement()
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'review'", out["reason"])
        self.assertTrue(self.ran())

    def test_a_direct_write_to_a_protected_file_fails_the_gate_and_skips_the_command(self):
        self.configure({"lint": self.protecting()})
        self.start_implement()
        self.write("detekt.yml", "maxIssues: 1000\n")  # as sed or > through Bash would
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("Quality gates failed: lint", out["reason"])
        self.assertIn("detekt.yml (changed)", out["reason"])
        self.assertIn("/sl-quality accept", out["reason"])
        self.assertFalse(self.ran())
        self.assertIn("detekt.yml (changed)", self.read(self.run_path("quality", "lint.log")))
        self.assertEqual("implement", self.state()["phase"])

    def test_creating_a_missing_protected_file_fails_the_gate(self):
        self.configure({"lint": self.protecting()})
        self.start_implement()
        self.write("missing.yml", "new\n")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("missing.yml (created)", out["reason"])
        self.assertFalse(self.ran())

    def test_adding_a_file_to_a_protected_directory_fails_the_gate(self):
        self.configure({"lint": self.protecting()})
        self.start_implement()
        self.write("config/detekt/sub/new.yml", "x\n")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("config/detekt/sub/new.yml (created)", out["reason"])
        self.assertFalse(self.ran())

    def test_deleting_a_protected_file_fails_the_gate(self):
        self.configure({"lint": self.protecting()})
        self.start_implement()
        os.remove(os.path.join(self.root, "config", "detekt", "a.yml"))
        out = workflow.handle_stop(self.root, {})
        self.assertIn("config/detekt/a.yml (deleted)", out["reason"])
        self.assertFalse(self.ran())

    def test_a_protection_failure_is_an_ordinary_failure_with_one_iteration(self):
        self.configure({"lint": self.protecting(), "other": self.protecting(protect=["detekt.yml"])})
        self.start_implement()
        before = self.state()["iter"]
        self.write("detekt.yml", "changed\n")
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("Quality gates failed: lint, other", out["reason"])
        state = self.state()
        self.assertEqual("active", state["status"])
        self.assertNotIn("pausedFor", state)
        self.assertEqual(before + 1, state["iter"])
        self.assertEqual("fail", state["qualityResults"]["lint"]["result"])
        self.assertTrue(os.path.exists(self.run_path("last-failure.md")))

    def test_restoring_the_file_lets_the_gate_pass_again(self):
        self.configure({"lint": self.protecting()})
        self.start_implement()
        self.write("detekt.yml", "changed\n")
        workflow.handle_stop(self.root, {})
        self.write("detekt.yml", "maxIssues: 0\n")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'review'", out["reason"])

    def test_a_gate_without_protect_runs_beside_a_protected_one(self):
        self.configure({"lint": self.protecting(), "tests": {"command": "echo x >> tests-ran.txt"}})
        self.start_implement()
        self.write("detekt.yml", "changed\n")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("Quality gates failed: lint\n", out["reason"])
        self.assertFalse(self.ran())
        self.assertTrue(self.ran("tests-ran.txt"))
        self.assertEqual("pass", self.state()["qualityResults"]["tests"]["result"])

    def test_a_warn_gate_with_changed_protected_files_warns_and_skips_its_command(self):
        self.configure({"lint": self.protecting(level="warn")})
        self.start_implement()
        self.write("detekt.yml", "changed\n")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'review'", out["reason"])
        self.assertEqual("warn", self.state()["qualityResults"]["lint"]["result"])
        self.assertFalse(self.ran())
        self.assertIn("detekt.yml (changed)", self.read(self.run_path("quality.md")))

    def test_fail_fast_stops_after_a_protection_failure(self):
        self.configure({"lint": self.protecting(), "tests": {"command": "echo x >> tests-ran.txt"}},
                       {"failFast": True})
        self.start_implement()
        self.write("detekt.yml", "changed\n")
        workflow.handle_stop(self.root, {})
        self.assertFalse(self.ran("tests-ran.txt"))

    def test_a_run_without_fingerprints_is_not_checked(self):
        self.configure({"lint": self.protecting()})
        self.start_implement()
        workflow.update_state(self.root, lambda s: s.pop("qualityFingerprints"))
        self.write("detekt.yml", "changed\n")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'review'", out["reason"])

    # the quality block

    def test_config_json_written_directly_fails_every_gate_including_warn(self):
        gates = {"lint": {"command": "echo x >> ran.txt"}, "soft": {"command": "echo x >> ran.txt", "level": "warn"}}
        self.configure(gates)
        self.start_implement()
        gates["lint"]["command"] = "exit 0"  # the agent weakens the harness
        self.configure(gates)
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("Quality gates failed: lint, soft", out["reason"])
        self.assertIn("/sl-quality accept", out["reason"])
        self.assertFalse(self.ran())
        state = self.state()
        self.assertEqual("implement", state["phase"])
        self.assertEqual(["fail", "fail"], [state["qualityResults"][n]["result"] for n in ("lint", "soft")])

    def test_config_local_json_written_directly_fails_every_gate(self):
        self.configure({"lint": {"command": "echo x >> ran.txt"}, "tests": {"command": "echo x >> ran.txt"}})
        self.start_implement()
        self.write_json("config.local.json", {"quality": {"gates": {"lint": {"command": "exit 0"}}}})
        out = workflow.handle_stop(self.root, {})
        self.assertEqual("block", out["decision"])
        self.assertIn("Quality gates failed: lint, tests", out["reason"])
        self.assertIn("config.local.json", out["reason"])
        self.assertFalse(self.ran())
        self.assertEqual("fail", self.state()["qualityResults"]["tests"]["result"])

    def test_a_block_change_is_one_iteration_and_no_pause(self):
        self.configure({"lint": {"command": "exit 0"}})
        self.start_implement()
        before = self.state()["iter"]
        self.configure({"lint": {"command": "exit 0"}}, {"failFast": True})
        workflow.handle_stop(self.root, {})
        state = self.state()
        self.assertEqual(before + 1, state["iter"])
        self.assertEqual("active", state["status"])
        self.assertNotIn("pausedFor", state)

    def test_a_block_change_without_gates_in_the_phase_does_not_block(self):
        self.start_implement()
        self.configure({}, {"failFast": True})
        out = workflow.handle_stop(self.root, {})
        self.assertIn("entering phase 'review'", out["reason"])

    def test_the_command_prefix_names_the_accept_command(self):
        self.configure({"lint": {"command": "exit 0"}}, commandPrefix="x-")
        self.start_implement()
        self.configure({"lint": {"command": "exit 1"}}, commandPrefix="x-")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("/x-quality accept", out["reason"])
        self.assertNotIn("/sl-quality", out["reason"])



class PretoolProtectionTest(GateProject):
    """PreToolUse gives early feedback: protected paths cannot be edited and the user's commands cannot be run."""

    PROTECT = ["detekt.yml", "config/detekt/"]

    def setUp(self):
        super().setUp()
        self.configure({"lint": {"command": "exit 0", "protect": self.PROTECT}})

    def pretool(self, tool, path=None, command=None, **extra):
        tool_input = {"command": command} if command is not None else {"file_path": path}
        if tool == "NotebookEdit":
            tool_input = {"notebook_path": path}
        return workflow.handle_pretool(self.root, dict(extra, tool_name=tool, tool_input=tool_input))

    def assertDenied(self, out, *fragments):
        self.assertIsNotNone(out)
        decision = out["hookSpecificOutput"]
        self.assertEqual("deny", decision["permissionDecision"])
        for fragment in fragments:
            self.assertIn(fragment, decision["permissionDecisionReason"])

    def full(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def test_edit_tools_are_denied_on_a_protected_file_in_every_phase(self):
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        phases = [self.state()["phase"]]
        with open(self.run_path("plan.md"), "w") as f:
            f.write("- [ ] step")
        workflow.handle_stop(self.root, {})
        phases.append(self.state()["phase"])
        self.assertEqual(["plan", "implement"], phases)
        for phase in phases:
            workflow.update_state(self.root, lambda s: s.update(phase=phase))
            for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                with self.subTest(phase=phase, tool=tool):
                    self.assertDenied(self.pretool(tool, self.full("detekt.yml")), "detekt.yml", "/sl-quality accept")

    def test_the_review_phase_is_covered_too(self):
        self.start_implement()
        workflow.update_state(self.root, lambda s: s.update(phase="review"))
        self.assertDenied(self.pretool("Edit", self.full("detekt.yml")), "/sl-quality accept")

    def test_a_file_under_a_protected_directory_is_denied(self):
        self.start_implement()
        for rel in ("config/detekt/a.yml", "config/detekt/sub/deep/b.yml", "config/detekt/new.yml"):
            with self.subTest(rel=rel):
                self.assertDenied(self.pretool("Write", self.full(rel)), rel, "/sl-quality accept")

    def test_a_relative_path_and_odd_spelling_are_denied(self):
        self.start_implement()
        for path in ("detekt.yml", "./detekt.yml", "config/../detekt.yml", "config/detekt/a.yml"):
            with self.subTest(path=path):
                self.assertDenied(self.pretool("Edit", path), "/sl-quality accept")

    def test_paths_next_to_protected_ones_are_allowed(self):
        self.start_implement()
        for rel in ("src/detekt.yml", "detekt.yml.bak", "config/detektx/a.yml", "config/other.yml", "src/Main.kt"):
            with self.subTest(rel=rel):
                self.assertIsNone(self.pretool("Edit", self.full(rel)))

    def test_the_reason_uses_the_command_prefix(self):
        self.configure({"lint": {"command": "exit 0", "protect": self.PROTECT}}, commandPrefix="x-")
        self.start_implement()
        out = self.pretool("Edit", self.full("detekt.yml"))
        self.assertDenied(out, "/x-quality accept")
        self.assertNotIn("/sl-", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_edits_are_allowed_when_no_run_is_active(self):
        self.assertIsNone(self.pretool("Edit", self.full("detekt.yml")))  # no run at all
        self.start_implement()
        workflow.cmd_pause(self.root)
        self.assertEqual("paused", self.state()["status"])
        self.assertIsNone(self.pretool("Edit", self.full("detekt.yml")))
        self.assertIsNone(self.pretool("Write", self.full("config/detekt/a.yml")))

    def test_nothing_is_protected_without_protect_entries(self):
        self.configure({"lint": {"command": "exit 0"}})
        self.start_implement()
        self.assertIsNone(self.pretool("Edit", self.full("detekt.yml")))

    def test_the_entries_of_every_gate_count(self):
        self.configure({"lint": {"command": "exit 0", "protect": ["a.yml"]},
                        "types": {"command": "exit 0", "protect": ["b.ini"]}})
        self.start_implement()
        self.assertDenied(self.pretool("Edit", self.full("a.yml")), "/sl-quality accept")
        self.assertDenied(self.pretool("Edit", self.full("b.ini")), "/sl-quality accept")

    def test_an_entry_naming_the_project_root_protects_nothing(self):
        self.start_implement()
        self.configure({"lint": {"command": "exit 0", "protect": [".", "", "./", ".."]}})
        self.assertIsNone(self.pretool("Edit", self.full("src/Main.kt")))

    def test_another_chat_may_edit_protected_files(self):
        self.start_implement()
        workflow.update_state(self.root, lambda s: s.update(owner={"session": "owner-chat"}))
        self.assertIsNone(self.pretool("Edit", self.full("detekt.yml"), session_id="other-chat"))
        self.assertDenied(self.pretool("Edit", self.full("detekt.yml"), session_id="owner-chat"),
                          "/sl-quality accept")

    def test_a_subagent_of_the_owner_is_denied_too(self):
        self.start_implement()
        self.assertDenied(self.pretool("Edit", self.full("detekt.yml"), agent_id="sub-1"), "/sl-quality accept")

    def test_bash_commands_for_advance_and_accept_are_denied(self):
        self.start_implement()
        for command in ("sh .claude/specloop/run.sh advance",
                        'sh "$CLAUDE_PROJECT_DIR/.claude/specloop/run.sh" advance',
                        "sh .claude/specloop/run.sh quality accept",
                        "sh .claude/specloop/run.sh   quality   accept --all",
                        "cd x && sh .claude/specloop/run.sh advance; echo done"):
            with self.subTest(command=command):
                self.assertDenied(self.pretool("Bash", command=command), "only the user", "/sl-advance",
                                  "/sl-quality accept")

    def test_the_bash_denial_applies_in_every_chat(self):
        self.start_implement()
        workflow.update_state(self.root, lambda s: s.update(owner={"session": "owner-chat"}))
        self.assertDenied(self.pretool("Bash", command="sh run.sh advance", session_id="other-chat"), "only the user")

    def test_other_run_sh_forms_are_allowed(self):
        self.start_implement()
        for command in ("sh .claude/specloop/run.sh quality run", "sh .claude/specloop/run.sh status",
                        "sh .claude/specloop/run.sh quality", "echo advance", "git status"):
            with self.subTest(command=command):
                self.assertIsNone(self.pretool("Bash", command=command))

    def test_the_bash_denial_applies_only_while_a_run_is_active(self):
        self.start_implement()
        workflow.cmd_pause(self.root)
        self.assertIsNone(self.pretool("Bash", command="sh .claude/specloop/run.sh advance"))

    def test_the_prefix_names_the_commands_in_the_bash_reason(self):
        self.configure({"lint": {"command": "exit 0"}}, commandPrefix="x-")
        self.start_implement()
        self.assertDenied(self.pretool("Bash", command="sh run.sh advance"), "/x-advance", "/x-quality accept")

    def test_advance_keeps_disabling_model_invocation(self):
        with open(os.path.join(os.path.dirname(KIT), "commands", "advance.md"), encoding="utf-8") as f:
            self.assertIn("disable-model-invocation: true\n", f.read())

    def test_malformed_protect_never_raises(self):
        self.start_implement()
        for protect in (5, "detekt.yml", [None], [1, "a"], {"a": 1}):
            with self.subTest(protect=protect):
                self.configure({"lint": {"command": "exit 0", "protect": protect}})
                self.assertIsNone(self.pretool("Edit", self.full("detekt.yml")))
        for block in (None, ["x"], "gates", {"gates": ["lint"]}, {"gates": {"lint": "exit 0"}}):
            with self.subTest(block=block):
                self.set_config({"quality": block})
                self.assertIsNone(self.pretool("Edit", self.full("detekt.yml")))
                self.assertIsNone(self.pretool("Bash", command="git status"))


class QualityCommandTest(GateProject):
    """/sl-quality without arguments: the gates, their last results, and problems with the setup."""

    def quality(self, args=""):
        return workflow.cmd_quality(self.root, args)

    def set_manifest(self, **data):
        self.write_json("manifest.json", data)

    def test_the_template_is_for_the_user_and_runs_quality_with_the_arguments(self):
        with open(os.path.join(os.path.dirname(KIT), "commands", "quality.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("disable-model-invocation: true\n", text)
        self.assertIn("allowed-tools: Bash(sh .claude/specloop/run.sh:*)", text)
        self.assertIn("argument-hint:", text)
        self.assertIn('!`sh .claude/specloop/run.sh quality "$ARGUMENTS"`', text)
        self.assertNotIn("/sl-", text)
        self.assertIn("{{prefix}}", text)

    def test_main_runs_quality(self):
        self.configure({"lint": {"command": "exit 0"}})
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.root}), contextlib.redirect_stdout(out):
            self.assertEqual(0, workflow.main(["quality"]))
        self.assertIn("lint", out.getvalue())

    def test_no_gates(self):
        self.assertIn("No quality gates are configured", self.quality())

    def test_lists_gates_in_order_with_phases_level_and_steps(self):
        self.configure({"zeta": {"command": "exit 0"},
                        "alpha": {"command": "exit 0", "phases": ["implement", "review"], "level": "warn",
                                  "steps": "last"}})
        out = self.quality()
        self.assertLess(out.index("zeta"), out.index("alpha"))
        self.assertIn("zeta: phases implement, level block, steps each, last result: not run", out)
        self.assertIn("alpha: phases implement, review, level warn, steps last, last result: not run", out)

    def test_lists_the_last_result_with_the_metric_and_the_reason(self):
        self.configure({"cov": {"command": "exit 0"}, "tool": {"command": "exit 0"}, "ok": {"command": "exit 0"}})
        workflow.cmd_init(self.root, "spec.md")
        workflow.update_state(self.root, lambda s: s.update(qualityResults={
            "cov": {"result": "fail", "metric": 71.5, "phase": "implement"},
            "tool": {"result": "broken", "reason": "command not found (exit 127)", "phase": "implement"},
            "ok": {"result": "pass", "phase": "implement"}}))
        out = self.quality()
        self.assertIn("last result: fail, metric 71.5", out)
        self.assertIn("last result: broken (command not found (exit 127))", out)
        self.assertIn("last result: pass\n", out + "\n")

    def test_prints_every_validation_error(self):
        self.configure({"a": {"command": "exit 0", "level": "loud"}, "b": {"command": "exit 0", "steps": "never"}})
        out = self.quality()
        self.assertIn("Configuration errors:", out)
        self.assertIn("level", out)
        self.assertIn("steps", out)

    def test_no_errors_section_for_a_valid_block(self):
        self.configure({"a": {"command": "exit 0"}})
        self.assertNotIn("Configuration errors", self.quality())

    def test_warns_when_the_budget_is_above_the_installed_hook(self):
        self.configure({"a": {"command": "exit 0"}}, {"timeoutSec": 5000})
        self.set_manifest(stopTimeoutSec=900 + 600 + 60,
                          source={"path": "/clones/specloop", "installArgs": {"scope": "local", "gitignore": False}})
        out = self.quality()
        self.assertIn("quality.timeoutSec is 5000 s", out)
        self.assertIn("600 s", out)
        self.assertIn("python install.py %s --scope local --no-gitignore" % self.root.replace(" ", ""), out.replace('"', ""))
        self.assertIn("/clones/specloop", out)

    def test_the_budget_warning_without_a_recorded_source_names_the_clone(self):
        self.configure({"a": {"command": "exit 0"}}, {"timeoutSec": 5000})
        self.set_manifest(stopTimeoutSec=900 + 600 + 60)
        out = self.quality()
        self.assertIn("python install.py", out)
        self.assertIn("the specloop clone", out)

    def test_no_budget_warning_when_the_budget_fits_or_the_manifest_is_old(self):
        self.configure({"a": {"command": "exit 0"}}, {"timeoutSec": 5000})
        self.assertNotIn("Warning", self.quality())
        self.set_manifest(stopTimeoutSec=900 + 5000 + 60)
        self.assertNotIn("Warning", self.quality())

    def test_reports_tools_that_are_not_on_path(self):
        os.makedirs(os.path.join(self.root, "tools"))
        with open(os.path.join(self.root, "tools", "check.sh"), "w") as f:
            f.write("exit 0\n")
        with open(os.path.join(self.root, "gradlew"), "w") as f:
            f.write("exit 0\n")
        self.configure({"gone": {"command": "no-such-tool-xyz --flag"},
                        "env": {"command": "FOO=1 no-such-tool-abc run"},
                        "wrapper": {"command": "./gradlew detekt"},
                        "script": {"command": "tools/check.sh"},
                        "missing": {"command": "./nowhere/run"},
                        "builtin": {"command": "exit 1"},
                        "echo": {"command": "echo hi && no-such-tool-later"},
                        "python": {"command": '"%s" -c pass' % sys.executable.replace("\\", "/")}})
        out = self.quality()
        self.assertIn("gone: no-such-tool-xyz", out)
        self.assertIn("env: no-such-tool-abc", out)
        self.assertIn("missing: ./nowhere/run", out)
        tools = out.split("Tools not found")[1]
        for name in ("wrapper:", "script:", "builtin:", "echo:", "python:"):
            self.assertNotIn(name, tools)

    def test_unknown_forms_print_the_usage(self):
        for args in ("bogus", "init kotlin", "accept now", "--help"):
            with self.subTest(args=args):
                out = self.quality(args)
                self.assertIn("Usage: /sl-quality", out)
                for form in ("run", "init", "accept"):
                    self.assertIn("/sl-quality " + form, out)

    def test_usage_uses_the_prefix(self):
        self.configure({"a": {"command": "exit 0"}}, commandPrefix="x-")
        self.assertIn("Usage: /x-quality", self.quality("bogus"))

    def test_unbalanced_quotes_print_a_usage_error(self):
        out = self.quality('run "lint')
        self.assertIn("Cannot read the arguments", out)
        self.assertIn("Usage: /sl-quality", out)

    def test_never_raises_on_malformed_config_or_state(self):
        shapes = [None, [], "gates", {"gates": []}, {"gates": {"a": None}}, {"gates": {"a": "exit 0"}},
                  {"gates": {"a": {"command": 5, "phases": 3}}}, {"gates": {"a": {"command": "exit 0"}}, "timeoutSec": "x"},
                  {"gates": {"a": {"command": "'unbalanced"}}}]
        for block in shapes:
            with self.subTest(block=block):
                self.set_config({"quality": block})
                self.assertIsInstance(self.quality(), str)
        self.set_config("not an object")
        self.assertIsInstance(self.quality(), str)
        self.set_gates({"a": {"command": "exit 0"}})
        with open(os.path.join(self.base, "state.json"), "w") as f:
            f.write("{broken")
        self.assertIn("a: phases", self.quality())
        with open(os.path.join(self.base, "state.json"), "w") as f:
            json.dump({"qualityResults": ["x", {"a": 1}]}, f)
        self.assertIsInstance(self.quality(), str)

    def test_an_internal_error_is_a_message(self):
        with mock.patch.object(workflow, "quality_overview", side_effect=RuntimeError("boom")):
            out = self.quality()
        self.assertIn("/sl-quality failed: boom", out)

    def test_it_changes_nothing(self):
        self.configure({"a": {"command": "exit 0"}})
        workflow.cmd_init(self.root, "spec.md")
        before = self.state_bytes()
        self.quality()
        self.assertEqual(before, self.state_bytes())


class QualityAcceptTest(ChangedFilesProject):
    """/sl-quality accept: a person allows protected-file changes, a changed quality block and forbidden lines."""

    FORBID = [{"pattern": "TODO", "message": "No TODOs."}]

    def setUp(self):
        super().setUp()
        self.write("rules.yml", "maxIssues: 0\n")
        self.git("add", "rules.yml")
        self.git("commit", "-q", "-m", "rules")

    def lint(self, **extra):
        return self.counting("lint", "exit 0", **dict({"protect": ["rules.yml"], "forbid": self.FORBID}, **extra))

    def start_implement(self, gates=None, quality=None, **top):
        gates = gates if gates is not None else {"lint": self.lint()}
        self.set_config(dict(top, phases={"implement": {"check": {"type": "command", "command": "exit 0"}}},
                             quality=dict(quality or {}, gates=gates)))
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        self.write(self.run_file("plan.md"), "- [ ] step")
        self.assertIn("entering phase 'implement'", self.stop()["reason"])

    def accept(self, args="accept"):
        return workflow.cmd_quality(self.root, args)

    def report(self):
        with open(os.path.join(self.base, "run", "quality.md"), encoding="utf-8") as f:
            return f.read()

    def state_bytes(self):
        with open(os.path.join(self.base, "state.json"), "rb") as f:
            return f.read()

    # refusals

    def test_without_a_workflow_it_refuses_and_changes_nothing(self):
        self.set_config({"quality": {"gates": {"lint": self.lint()}}})
        out = self.accept()
        self.assertIn("Nothing was accepted", out)
        self.assertIn("no workflow is loaded", out)
        self.assertIn("/sl-go", out)
        self.assertFalse(os.path.exists(os.path.join(self.base, "state.json")))
        self.assertFalse(os.path.exists(os.path.join(self.base, "run", "quality.md")))

    def test_a_loaded_spec_that_has_not_started_is_refused(self):
        self.set_config({"quality": {"gates": {"lint": self.lint()}}})
        workflow.cmd_init(self.root, "spec.md")
        before = self.state_bytes()
        out = self.accept()
        self.assertIn("the workflow is ready", out)
        self.assertEqual(before, self.state_bytes())

    def test_a_finished_workflow_is_refused(self):
        self.start_implement()
        workflow.update_state(self.root, lambda s: s.update(status="done"))
        before = self.state_bytes()
        self.assertIn("the workflow is done", self.accept())
        self.assertEqual(before, self.state_bytes())

    def test_the_refusal_uses_the_command_prefix(self):
        self.set_config({"commandPrefix": "x-", "quality": {"gates": {}}})
        out = self.accept()
        self.assertIn("/x-quality accept", out)
        self.assertIn("/x-go", out)
        self.assertNotIn("/sl-", out)

    def test_extra_arguments_print_the_usage_and_change_nothing(self):
        self.start_implement()
        before = self.state_bytes()
        self.assertIn("Usage: /sl-quality", self.accept("accept now"))
        self.assertEqual(before, self.state_bytes())

    # protected files and the quality block

    def test_a_changed_protected_file_fails_until_accepted(self):
        self.start_implement()
        self.write("rules.yml", "maxIssues: 99\n")
        self.assertIn("rules.yml (changed)", self.stop()["reason"])
        self.assertEqual(0, self.marker("lint"))
        out = self.accept()
        self.assertIn("Accepted for this run:", out)
        self.assertIn("- protected file rules.yml (changed)", out)
        self.assertIn("entering phase 'review'", self.stop()["reason"])
        self.assertEqual(1, self.marker("lint"))

    def test_the_fingerprint_is_replaced_so_a_later_change_fails_again(self):
        self.start_implement()
        self.write("rules.yml", "maxIssues: 99\n")
        self.accept()
        self.assertEqual(workflow.protected_fingerprint(self.root, "rules.yml"),
                         self.state()["qualityFingerprints"]["protected"]["rules.yml"])
        self.write("rules.yml", "maxIssues: 100\n")
        self.assertIn("rules.yml (changed)", self.stop()["reason"])

    def test_a_created_and_a_deleted_protected_file_are_accepted(self):
        self.start_implement({"lint": self.lint(protect=["rules.yml", "extra.yml"])})
        self.write("extra.yml", "new\n")
        os.remove(os.path.join(self.root, "rules.yml"))
        out = self.accept()
        self.assertIn("rules.yml (deleted)", out)
        self.assertIn("extra.yml (created)", out)
        self.assertIn("entering phase 'review'", self.stop()["reason"])

    def test_a_changed_quality_block_fails_every_gate_until_accepted(self):
        self.start_implement({"lint": self.lint(), "soft": self.counting("soft", "exit 0", level="warn")})
        self.set_config(dict(phases={"implement": {"check": {"type": "command", "command": "exit 0"}}},
                             quality={"failFast": True, "gates": {"lint": self.lint(),
                                                                  "soft": self.counting("soft", "exit 0", level="warn")}}))
        self.assertIn("Quality gates failed: lint, soft", self.stop()["reason"])
        out = self.accept()
        self.assertIn("- the quality block (config.json or config.local.json)", out)
        self.assertIn("entering phase 'review'", self.stop()["reason"])
        self.assertEqual(1, self.marker("lint"))
        self.assertEqual(1, self.marker("soft"))

    def test_a_change_made_through_config_local_json_is_accepted_too(self):
        self.start_implement()
        with open(os.path.join(self.base, "config.local.json"), "w") as f:
            json.dump({"quality": {"gates": {"lint": {"timeoutSec": 5}}}}, f)
        self.assertIn("config.local.json", self.stop()["reason"])
        self.accept()
        self.assertIn("entering phase 'review'", self.stop()["reason"])

    def test_nothing_is_accepted_beyond_what_changed(self):
        self.start_implement({"lint": self.lint(), "other": self.counting("other", "exit 0", protect=["rules.yml"])})
        self.write("rules.yml", "changed\n")
        out = self.accept()
        self.assertEqual(1, out.count("rules.yml (changed)"))  # one change, however many gates protect the file
        self.assertNotIn("quality block", out)
        self.assertNotIn("forbidden line src", out)

    # forbidden lines

    def test_accepted_forbid_hits_are_not_reported_again(self):
        self.start_implement()
        self.write("src/a.txt", "ok\nTODO one\nTODO two\n")
        reason = self.stop()["reason"]
        self.assertIn("src/a.txt:2: TODO one", reason)
        out = self.accept()
        self.assertIn("- forbidden line src/a.txt:2: TODO one", out)
        self.assertIn("- forbidden line src/a.txt:3: TODO two", out)
        accepted = self.state()["qualityAccepted"]
        self.assertEqual({workflow.forbid_hit_hash("src/a.txt", "TODO one"),
                          workflow.forbid_hit_hash("src/a.txt", "TODO two")}, set(accepted))
        self.assertEqual(2, len(accepted))
        self.assertIn("entering phase 'review'", self.stop()["reason"])

    def test_a_new_hit_after_accept_is_still_reported(self):
        self.start_implement()
        self.write("src/a.txt", "TODO one\n")
        self.accept()
        self.write("src/a.txt", "TODO one\nTODO fresh\n")
        self.write("src/b.txt", "TODO one\n")  # the same text in another file is a new hit
        reason = self.stop()["reason"]
        self.assertIn("src/a.txt:2: TODO fresh", reason)
        self.assertIn("src/b.txt:1: TODO one", reason)
        self.assertNotIn("src/a.txt:1", reason)
        self.assertEqual(0, self.marker("lint"))

    def test_a_second_accept_adds_only_the_new_hits(self):
        self.start_implement()
        self.write("src/a.txt", "TODO one\n")
        self.accept()
        self.write("src/a.txt", "TODO one\nTODO two\n")
        out = self.accept()
        self.assertIn("src/a.txt:2: TODO two", out)
        self.assertNotIn("TODO one", out)
        self.assertEqual(2, len(self.state()["qualityAccepted"]))

    def test_protected_files_and_hits_are_accepted_together(self):
        self.start_implement()
        self.write("rules.yml", "changed\n")
        self.write("src/a.txt", "TODO one\n")
        out = self.accept()
        self.assertIn("rules.yml (changed)", out)
        self.assertIn("src/a.txt:1: TODO one", out)
        self.assertIn("entering phase 'review'", self.stop()["reason"])

    def test_nothing_to_accept_changes_nothing(self):
        self.start_implement()
        before = self.state_bytes()
        out = self.accept()
        self.assertIn("Nothing to accept", out)
        self.assertEqual(before, self.state_bytes())
        self.assertFalse(os.path.exists(os.path.join(self.base, "run", "quality.md")))

    def test_a_run_without_fingerprints_gets_them(self):
        self.start_implement()
        workflow.update_state(self.root, lambda s: s.pop("qualityFingerprints"))
        out = self.accept()
        self.assertIn("recorded", out)
        self.assertIn("rules.yml", self.state()["qualityFingerprints"]["protected"])

    # statuses

    def test_it_works_in_a_paused_and_a_stalled_run(self):
        for status in ("paused", "stalled"):
            with self.subTest(status=status):
                workflow.cmd_reset(self.root)
                self.write("rules.yml", "maxIssues: 0\n")
                self.start_implement()
                self.write("rules.yml", "changed %s\n" % status)
                workflow.update_state(self.root, lambda s: s.update(status=status))
                self.assertIn("rules.yml (changed)", self.accept())
                self.assertEqual(status, self.state()["status"])

    # state

    def test_it_writes_through_update_state(self):
        self.start_implement()
        self.write("rules.yml", "changed\n")
        rev = self.state()["rev"]
        with mock.patch.object(workflow, "update_state", wraps=workflow.update_state) as spy:
            self.accept()
        self.assertEqual(1, spy.call_count)
        self.assertEqual(rev + 1, self.state()["rev"])

    def test_it_holds_no_lock_while_it_scans(self):
        self.start_implement()
        self.write("src/a.txt", "TODO one\n")
        seen = []
        original = workflow.forbid_hits

        def spy(*args, **kwargs):
            seen.append(os.path.exists(os.path.join(self.base, "state.lock")))
            return original(*args, **kwargs)
        with mock.patch.object(workflow, "forbid_hits", side_effect=spy):
            self.accept()
        self.assertEqual([False], seen)

    def test_a_run_that_ended_meanwhile_is_not_touched(self):
        self.start_implement()
        self.write("rules.yml", "changed\n")
        original = workflow.quality_fingerprints

        def end_the_run(*args, **kwargs):
            result = original(*args, **kwargs)
            workflow.update_state(self.root, lambda s: s.update(status="done"))
            return result
        with mock.patch.object(workflow, "quality_fingerprints", side_effect=end_the_run):
            out = self.accept()
        self.assertIn("the workflow is done", out)
        state = self.state()
        self.assertNotIn("qualityAccepted", state)
        self.assertNotIn("rules.yml (changed)", json.dumps(state))
        self.assertNotEqual(workflow.protected_fingerprint(self.root, "rules.yml"),
                            state["qualityFingerprints"]["protected"]["rules.yml"])

    def test_a_workflow_that_changed_meanwhile_is_not_accepted_from_stale_data(self):
        self.start_implement()
        self.write("src/a.txt", "TODO one\n")
        original = workflow.current_forbid_hits

        def move_on(*args, **kwargs):
            result = original(*args, **kwargs)
            workflow.update_state(self.root, lambda s: s.update(attempt=s.get("attempt", 0) + 1))
            return result
        with mock.patch.object(workflow, "current_forbid_hits", side_effect=move_on):
            out = self.accept()
        self.assertIn("changed while /sl-quality accept ran", out)
        self.assertNotIn("qualityAccepted", self.state())

    def test_git_failing_accepts_nothing(self):
        self.start_implement()
        self.write("rules.yml", "changed\n")
        workflow.update_state(self.root, lambda s: s.update(baseRef="0" * 40))
        before = self.state_bytes()
        out = self.accept()
        self.assertIn("Nothing was accepted: the added lines cannot be determined", out)
        self.assertEqual(before, self.state_bytes())

    # the report

    def test_quality_md_lists_the_accepted_changes(self):
        self.start_implement()
        self.write("rules.yml", "changed\n")
        self.write("src/a.txt", "TODO one\n")
        self.accept()
        text = self.report()
        self.assertIn("## Accepted changes", text)
        self.assertIn("- protected file rules.yml (changed) (accepted ", text)
        self.assertIn("- forbidden line src/a.txt:1: TODO one (accepted ", text)

    def test_the_section_is_kept_when_a_gate_run_rewrites_the_report(self):
        self.start_implement()
        self.write("rules.yml", "changed\n")
        self.accept()
        self.assertIn("entering phase 'review'", self.stop()["reason"])
        text = self.report()
        self.assertIn("| lint | pass |", text)
        self.assertEqual(1, text.count("## Accepted changes"))
        self.assertIn("rules.yml (changed)", text)

    def test_a_second_accept_extends_one_section(self):
        self.start_implement()
        self.write("rules.yml", "one\n")
        self.accept()
        self.assertIn("entering phase 'review'", self.stop()["reason"])
        workflow.update_state(self.root, lambda s: s.update(phase="implement"))
        self.write("rules.yml", "two\n")
        self.accept()
        text = self.report()
        self.assertEqual(1, text.count("## Accepted changes"))
        self.assertEqual(2, text.count("protected file rules.yml (changed)"))
        self.assertIn("| lint | pass |", text)

    def test_the_log_in_state_is_bounded_and_survives_garbage(self):
        self.start_implement()
        workflow.update_state(self.root, lambda s: s.update(qualityAcceptedLog="garbage", qualityAccepted="x"))
        self.write("rules.yml", "changed\n")
        self.assertIn("rules.yml (changed)", self.accept())
        self.assertEqual(1, len(self.state()["qualityAcceptedLog"]))
        self.assertEqual([], workflow.quality_accepted_lines({"qualityAcceptedLog": [1, {"changes": 3}]}))

    def test_it_works_in_a_feature_mode_step(self):
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": "exit 0"}}},
                         "quality": {"gates": {"lint": self.lint(), "whole": self.counting(
                             "whole", "exit 0", steps="last", forbid=["FIXME"])}}})
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        workflow.cmd_go(self.root)
        self.write(self.run_file("plan.md"), "# Plan")
        self.write(self.run_file("steps.json"), json.dumps({"feature": "F", "steps": [step_entry(1), step_entry(2)]}))
        self.stop()
        workflow.cmd_go(self.root)
        self.write("rules.yml", "changed\n")
        self.write("one.txt", "TODO one\nFIXME two\n")
        out = self.accept()
        self.assertIn("rules.yml (changed)", out)
        self.assertIn("one.txt:1: TODO one", out)
        self.assertIn("one.txt:2: FIXME two", out)  # the gate of the last step is accepted too
        self.assertIn("entering phase 'review'", self.stop()["reason"])


class QualityStatusTest(GateProject):
    """/sl-status shows the last result of each gate and the waived gates."""

    def test_single_mode_status_has_a_line_per_gate(self):
        self.configure({"lint": {"command": "echo found; exit 1"}, "cov": {"command": "echo 'coverage: 70%'",
                       "metric": {"pattern": "coverage: ([0-9.]+)%", "min": 80}}})
        self.start_implement()
        workflow.handle_stop(self.root, {})
        out = workflow.cmd_status(self.root)
        self.assertIn("quality: lint fail (phase implement)", out)
        self.assertIn("quality: cov fail, metric 70 (phase implement)", out)

    def test_no_quality_lines_without_a_gate_run(self):
        self.start_implement()
        self.assertNotIn("quality:", workflow.cmd_status(self.root))

    def test_waived_gates_are_listed(self):
        self.start_implement()
        workflow.update_state(self.root, lambda s: s.update(qualityWaived=[
            {"gate": "lint", "phase": "implement", "stepId": 2, "at": "now"}, "tests"]))
        out = workflow.cmd_status(self.root)
        self.assertIn("quality waived: lint in phase implement, step 2", out)
        self.assertIn("quality waived: tests", out)

    def test_malformed_results_do_not_break_status(self):
        self.start_implement()
        workflow.update_state(self.root, lambda s: s.update(qualityResults=["x"], qualityWaived="x"))
        self.assertIn("phase", workflow.cmd_status(self.root))
        workflow.update_state(self.root, lambda s: s.update(qualityResults={"a": 1, "b": {"result": "pass"}}))
        self.assertIn("quality: b pass", workflow.cmd_status(self.root))

    def test_status_output_keeps_the_update_line_last(self):
        self.configure({"lint": {"command": "exit 1"}})
        self.start_implement()
        workflow.handle_stop(self.root, {})
        lines = workflow.status_output(self.root).splitlines()
        self.assertTrue(lines[-1].startswith("update:"))
        self.assertTrue(any(line.startswith("quality: lint") for line in lines))


class WaivedGatesTest(GateProject):
    """/sl-advance skips the gates of the phase it passes and records them as waived."""

    def waived(self):
        return self.state().get("qualityWaived")

    def test_a_broken_gate_pauses_and_advance_skips_to_review_with_a_waiver(self):
        self.configure({"detekt": {"command": "exit 127"}, "tests": {"command": "exit 0"}})
        self.start_implement()
        workflow.handle_stop(self.root, {})
        self.assertEqual(("paused", "detekt"), (self.state()["status"], self.state()["pausedFor"]["gate"]))
        out = workflow.cmd_advance(self.root)
        state = self.state()
        self.assertIn("Advanced to phase 'review'", out)
        self.assertEqual("review", state["phase"])
        self.assertNotIn("pausedFor", state)
        self.assertEqual("skipped", state["history"][-1]["result"])
        self.assertEqual(["detekt", "tests"], [w["gate"] for w in state["qualityWaived"]])
        for entry in state["qualityWaived"]:
            self.assertEqual("implement", entry["phase"])
            self.assertNotIn("stepId", entry)
            self.assertTrue(entry["at"])

    def test_status_and_the_report_list_the_waived_gates(self):
        self.configure({"detekt": {"command": "exit 127"}})
        self.start_implement()
        workflow.handle_stop(self.root, {})
        workflow.cmd_advance(self.root)
        self.assertIn("quality waived: detekt in phase implement", workflow.cmd_status(self.root))
        report = self.read(self.run_path("quality.md"))
        self.assertIn("## Waived gates", report)
        self.assertIn("- detekt in phase implement (waived ", report)
        self.assertIn("| detekt | broken |", report)

    def test_the_report_is_created_when_no_gate_ran(self):
        self.configure({"lint": {"command": "exit 0"}})
        self.start_implement()
        workflow.cmd_advance(self.root)
        self.assertIn("- lint in phase implement", self.read(self.run_path("quality.md")))

    def test_a_gate_run_report_keeps_the_waived_section(self):
        self.configure({"lint": {"command": "exit 0"}})
        self.start_implement()
        workflow.cmd_advance(self.root)
        records = [{"name": "lint", "result": "pass", "durationSec": 0}]
        report = workflow.quality_report(self.config(), self.state(), records, {})
        self.assertIn("## Waived gates", report)
        self.assertIn("- lint in phase implement (waived ", report)

    def test_advance_without_gates_records_nothing(self):
        self.start_implement()
        workflow.cmd_advance(self.root)
        self.assertNotIn("qualityWaived", self.state())
        self.assertFalse(os.path.exists(self.run_path("quality.md")))

    def test_advancing_the_planning_phase_waives_nothing(self):
        self.configure({"lint": {"command": "exit 1"}, "early": {"command": "exit 1", "phases": ["plan"]}})
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        workflow.cmd_advance(self.root)
        self.assertNotIn("qualityWaived", self.state())

    def test_advance_runs_no_gate(self):
        self.configure({"lint": {"command": "echo x >> ran.txt; exit 1"}})
        self.start_implement()
        workflow.cmd_advance(self.root)
        self.assertFalse(self.exists("ran.txt"))

    def test_waivers_add_up_and_a_malformed_value_is_replaced(self):
        self.configure({"lint": {"command": "exit 0"}})
        self.start_implement()
        workflow.update_state(self.root, lambda s: s.update(qualityWaived="x"))
        workflow.cmd_advance(self.root)
        self.assertEqual(["lint"], [w["gate"] for w in self.waived()])


class QualityPromptTest(ChangedFilesProject):
    """{quality_gates} in the implement instructions, quality.md in the review instructions and the brief."""

    GATES = {
        "detekt": {"command": "./gradlew detekt", "hint": "Fix the finding;\n do not suppress it.",
                   "protect": ["detekt.yml", "config/detekt/"],
                   "forbid": [{"pattern": "@Suppress\\(", "glob": ["*.kt"], "message": "No suppressions."}, "TODO"]},
        "tests": {"command": "exit 0", "level": "warn"},
    }

    def configure(self, gates, **top):
        self.set_config(dict(top, phases={"implement": {"check": {"type": "command", "command": "exit 0"}}},
                             quality={"gates": gates}))

    def run_path(self, *parts):
        return os.path.join(self.base, "run", *parts)

    def start_implement(self):
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        self.write(self.run_file("plan.md"), "- [ ] step")
        self.assertIn("entering phase 'implement'", self.stop()["reason"])

    def instructions(self):
        return workflow.phase_instructions(self.root, self.config(), self.state())

    def test_implement_lists_the_gates_with_hints_protected_paths_and_forbidden_patterns(self):
        self.configure(self.GATES)
        self.start_implement()
        text = self.instructions()
        self.assertNotIn("{quality_gates}", text)
        self.assertIn("- `detekt` (block): `./gradlew detekt`. Hint: Fix the finding; do not suppress it.", text)
        self.assertIn("- `tests` (warn): `exit 0`", text)
        self.assertIn("`detekt.yml`, `config/detekt`", text)
        self.assertIn("`/sl-quality accept`", text)
        self.assertIn("- `@Suppress\\(` in `*.kt` (gate `detekt`): No suppressions.", text)
        self.assertIn("- `TODO` (gate `detekt`)", text)
        self.assertTrue(text.index("Quality gates") < text.index("Finish your turn"))

    def test_the_placeholder_renders_as_nothing_without_gates_and_leaves_no_gap(self):
        self.start_implement()
        text = self.instructions()
        self.assertNotIn("{quality_gates}", text)
        self.assertNotIn("Quality gates", text)
        self.assertNotIn("\n\n\n", text)
        self.assertIn("pull requests.\n\nFinish your turn", text)

    def test_planning_renders_nothing(self):
        self.configure(self.GATES)
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        self.assertEqual("[]", workflow.render_phase("[{quality_gates}]", self.config(), self.state()))

    def test_an_override_with_the_placeholder_renders_it_and_one_without_still_renders(self):
        self.configure(self.GATES)
        self.start_implement()
        os.makedirs(os.path.join(self.base, "overrides"))
        override = os.path.join(self.base, "overrides", "implement.md")
        with open(override, "w") as f:
            f.write("Mine for {spec}: {quality_gates}")
        self.assertIn("Mine for spec.md: ### Quality gates", self.instructions())
        with open(override, "w") as f:
            f.write("Mine for {spec}.")
        self.assertEqual("Mine for spec.md.", self.instructions())

    def test_a_malformed_gate_does_not_break_the_instructions(self):
        self.configure({"ok": {"command": "exit 0"}})
        self.start_implement()
        config = self.config()
        config["quality"]["gates"].update({"bad": "not an object", "list": {"command": ["x"], "phases": 7},
                                           "odd": {"command": "exit 0", "protect": 3, "forbid": [7, "("]}})
        text = workflow.render_phase("{quality_gates}", config, self.state())
        self.assertIn("- `ok` (block): `exit 0`", text)
        config["quality"]["gates"] = "broken"
        self.assertEqual("", workflow.render_phase("{quality_gates}", config, self.state()))

    def test_the_gate_text_is_not_expanded_again(self):
        self.configure({"g": {"command": "exit 0", "hint": "See {spec} and {run_dir}"}})
        self.start_implement()
        self.assertIn("Hint: See {spec} and {run_dir}", self.instructions())

    def test_the_prefix_names_the_accept_command(self):
        self.configure(self.GATES, commandPrefix="x-")
        self.start_implement()
        self.assertIn("`/x-quality accept`", self.instructions())

    def test_review_tells_the_reviewer_to_read_the_report(self):
        for name in ("review.md", "step-review.md"):
            with open(os.path.join(KIT, "phases", name), encoding="utf-8") as f:
                text = f.read()
            self.assertIn("{run_dir}/quality.md", text)
            self.assertIn("warning", text)
            self.assertIn("accepted change", text)
            self.assertIn("waived gate is a finding", text)

    def test_the_brief_lists_the_report_only_when_it_exists(self):
        self.configure({"lint": {"command": "exit 0"}})
        self.start_implement()
        report = ".claude/specloop/run/quality.md"
        self.assertNotIn(report, workflow.phase_brief(self.root, self.config(), self.state()))
        workflow.cmd_advance(self.root)
        self.assertTrue(os.path.exists(self.run_path("quality.md")))
        files = workflow.phase_brief(self.root, self.config(), self.state()).split("## Files to read")[1]
        self.assertIn("- `%s`" % report, files)

    def test_the_shipped_implement_phases_have_the_placeholder(self):
        for name in ("implement.md", "step-implement.md"):
            with open(os.path.join(KIT, "phases", name), encoding="utf-8") as f:
                self.assertIn("{quality_gates}", f.read())


class QualityRunTest(ChangedFilesProject):
    """/sl-quality run: gates tried now, outside the workflow."""

    def quality(self, args="run"):
        return workflow.cmd_quality(self.root, args)

    def state_bytes(self):
        with open(os.path.join(self.base, "state.json"), "rb") as f:
            return f.read()

    def start_implement(self, gates, **quality):
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": "exit 0"}}},
                         "quality": dict(quality, gates=gates)})
        workflow.cmd_init(self.root, "spec.md")
        workflow.cmd_go(self.root)
        self.write(self.run_file("plan.md"), "- [ ] step")
        self.assertIn("entering phase 'implement'", self.stop()["reason"])

    def manual_log(self, name):
        with open(os.path.join(self.base, "run", "quality-manual", name + ".log"), encoding="utf-8") as f:
            return f.read()

    def line(self, out, name):
        return [l for l in out.splitlines() if l.startswith("  %s: " % name)][0]

    def test_the_template_tells_claude_to_run_it_through_bash_with_a_timeout(self):
        with open(os.path.join(os.path.dirname(KIT), "commands", "quality.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("quality run", text)
        self.assertIn("Bash", text)
        self.assertIn("timeout", text)
        self.assertIn("sh .claude/specloop/run.sh quality run", text)

    def test_runs_every_gate_in_declared_order_with_result_duration_and_metric(self):
        self.set_config({"quality": {"gates": {
            "zeta": {"command": "echo ok"},
            "alpha": {"command": "echo coverage: 91.5%", "metric": {"pattern": "coverage: ([0-9.]+)%", "min": 80}},
            "bad": {"command": "echo boom; exit 1"}}}})
        out = self.quality()
        self.assertLess(out.index("zeta"), out.index("alpha"))
        self.assertLess(out.index("alpha"), out.index("bad"))
        zeta, alpha, bad = (self.line(out, n) for n in ("zeta", "alpha", "bad"))
        self.assertRegex(zeta, r"^  zeta: pass, [0-9.]+s, log ")
        self.assertRegex(alpha, r"^  alpha: pass, [0-9.]+s, metric 91\.5, log ")
        self.assertRegex(bad, r"^  bad: fail, [0-9.]+s, log ")
        self.assertIn(".claude/specloop/run/quality-manual/alpha.log", alpha)

    def test_named_gates_only_in_declared_order(self):
        self.set_config({"quality": {"gates": {name: self.counting(name, "exit 0") for name in ("a", "b", "c")}}})
        out = self.quality("run c a")
        self.assertEqual([1, 0, 1], [self.marker(n) for n in ("a", "b", "c")])
        self.assertLess(out.index("  a: "), out.index("  c: "))
        self.assertNotIn("  b: ", out)

    def test_logs_go_to_quality_manual_and_quality_is_untouched(self):
        self.start_implement({"lint": {"command": "echo lint-ran; exit 1"}})
        self.assertEqual("block", self.stop()["decision"])
        self.assertIn("lint-ran", self.gate_log("lint"))
        run = os.path.join(self.base, "run")
        with open(os.path.join(run, "quality", "lint.log"), "rb") as f:
            before = f.read()
        before_files = sorted(os.listdir(os.path.join(run, "quality")))
        self.set_config({"quality": {"gates": {"lint": {"command": "echo manual-ran"}}}})
        self.quality()
        self.assertIn("manual-ran", self.manual_log("lint"))
        with open(os.path.join(run, "quality", "lint.log"), "rb") as f:
            self.assertEqual(before, f.read())
        self.assertEqual(before_files, sorted(os.listdir(os.path.join(run, "quality"))))

    def test_without_a_run_no_quality_directory_appears(self):
        self.set_config({"quality": {"gates": {"lint": {"command": "echo hi"}}}})
        self.quality()
        self.assertTrue(os.path.exists(os.path.join(self.base, "run", "quality-manual", "lint.log")))
        self.assertFalse(os.path.exists(os.path.join(self.base, "run", "quality")))
        self.assertFalse(os.path.exists(os.path.join(self.base, "run", "quality.md")))

    def test_state_is_unchanged_during_a_run_and_without_one(self):
        self.set_config({"quality": {"gates": {"lint": {"command": "exit 1"}, "t": {"command": "exit 127"}}}})
        self.quality()
        self.assertFalse(os.path.exists(os.path.join(self.base, "state.json")))
        self.start_implement({"lint": {"command": "exit 0"}, "t": {"command": "exit 0"}})
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": "exit 1"}}},
                         "quality": {"gates": {"lint": {"command": "exit 1"}, "t": {"command": "exit 127"}}}})
        before = self.state_bytes()
        self.quality()
        self.assertEqual(before, self.state_bytes())
        self.assertEqual("active", self.state()["status"])

    def test_an_unknown_gate_lists_the_valid_names(self):
        self.set_config({"quality": {"gates": {"a": self.counting("a", "exit 0"), "b": {"command": "exit 0"}}}})
        out = self.quality("run a nope")
        self.assertIn("Unknown gate: nope", out)
        self.assertIn("a, b", out)
        self.assertEqual(0, self.marker("a"))
        self.assertFalse(os.path.exists(os.path.join(self.base, "run", "quality-manual")))

    def test_a_failing_gate_shows_the_end_of_its_output(self):
        self.set_config({"maxOutputChars": 300, "quality": {"gates": {
            "lint": {"command": "for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 "
                                "29 30; do echo line-$i-padding-padding; done; exit 3"},
            "fine": {"command": "echo fine-output"}}}})
        out = self.quality()
        self.assertIn("### lint (exit code 3)", out)
        self.assertIn("line-30-padding", out)
        self.assertNotIn("line-1-padding", out)
        self.assertNotIn("fine-output", out)
        self.assertIn("line-1-padding", self.manual_log("lint"))

    def test_a_broken_gate_is_reported_and_the_rest_still_run(self):
        self.set_config({"quality": {"gates": {"gone": {"command": "exit 127"}, "ok": self.counting("ok", "exit 0")}}})
        out = self.quality()
        self.assertRegex(self.line(out, "gone"), r"^  gone: broken \(.*127.*\)")
        self.assertIn("### gone", out)
        self.assertEqual(1, self.marker("ok"))

    def test_an_invalid_gate_is_broken_and_does_not_run(self):
        self.set_config({"quality": {"gates": {"bad": self.counting("bad", "exit 0", level="loud")}}})
        out = self.quality()
        self.assertIn("bad: broken (invalid definition", out)
        self.assertEqual(0, self.marker("bad"))

    def test_a_warning_gate_is_a_warn(self):
        self.set_config({"quality": {"gates": {"style": {"command": "echo meh; exit 1", "level": "warn"}}}})
        out = self.quality()
        self.assertIn("  style: warn", out)
        self.assertIn("meh", out)

    def test_steps_and_phases_are_not_honoured(self):
        self.start_implement({"late": self.counting("late", "exit 0", steps="last")})
        workflow.cmd_advance(self.root)
        self.assertEqual("review", self.state()["phase"])
        self.quality()
        self.assertEqual(1, self.marker("late"))

    def test_protected_files_and_forbidden_lines_are_not_checked(self):
        self.write("rules.yml", "a\n")
        self.git("add", "rules.yml")
        self.git("commit", "-q", "-m", "rules")
        gate = self.counting("lint", "exit 0", protect=["rules.yml"], forbid=["TODO"])
        self.start_implement({"lint": gate})
        self.write("rules.yml", "changed\n")
        self.write("new.txt", "TODO\n")
        out = self.quality()
        self.assertIn("  lint: pass", out)
        self.assertEqual(1, self.marker("lint"))

    def test_the_time_budget_applies(self):
        self.set_config({"quality": {"timeoutSec": 1, "gates": {"slow": {"command": "sleep 5"}}}})
        out = self.quality()
        self.assertIn("slow: broken (timed out", out)

    def test_a_ratchet_gate_uses_the_recorded_baseline(self):
        gate = {"command": "echo n: 7", "legacy": "ratchet", "metric": {"pattern": "n: ([0-9]+)", "max": 3}}
        self.set_config({"quality": {"gates": {"count": gate}}})
        self.assertIn("  count: fail", self.quality())
        workflow.cmd_init(self.root, "spec.md")
        workflow.update_state(self.root, lambda s: s.update(qualityBaseline={"count": 9}))
        self.assertIn("  count: pass", self.quality())

    def test_changed_files_expand_against_the_base_ref(self):
        self.write("tracked.txt", "before-run\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-q", "-m", "second")
        base = self.git("rev-parse", "HEAD~1")
        self.set_config({"quality": {"gates": {"files": {"command": "echo {changed_files}"}}}})
        workflow.cmd_init(self.root, "spec.md")
        workflow.update_state(self.root, lambda s: s.update(baseRef=base, preexistingUntracked=[]))
        self.write("fresh.txt")
        out = self.quality()
        self.assertIn("  files: pass", out)
        log = self.manual_log("files")
        self.assertIn("tracked.txt", log)
        self.assertIn("fresh.txt", log)
        self.assertNotIn("gone.txt", log)

    def test_changed_files_file_lives_in_quality_manual(self):
        self.set_config({"quality": {"gates": {"files": {"command": "cat {changed_files_file}"}}}})
        workflow.cmd_init(self.root, "spec.md")
        workflow.update_state(self.root, lambda s: s.update(baseRef=self.git("rev-parse", "HEAD"),
                                                           preexistingUntracked=[]))
        self.write("fresh.txt")
        self.quality()
        self.assertIn("fresh.txt", self.manual_log("files"))
        self.assertTrue(os.path.exists(os.path.join(self.base, "run", "quality-manual", "files.files")))
        self.assertFalse(os.path.exists(os.path.join(self.base, "run", "quality")))

    def test_changed_files_without_a_base_ref_is_skipped_with_a_note(self):
        self.set_config({"quality": {"gates": {"files": self.counting("files", "echo {changed_files}")}}})
        out = self.quality()
        self.assertIn("files: skipped (no baseRef", out)
        self.assertEqual(0, self.marker("files"))
        self.assertIn("did not run", self.manual_log("files"))

    def test_no_gates(self):
        self.assertIn("No quality gates are configured", self.quality())

    def test_the_prefix_is_used_in_the_usage_not_in_run_output(self):
        self.set_config({"commandPrefix": "x-", "quality": {"gates": {"a": {"command": "exit 0"}}}})
        self.assertIn("  a: pass", self.quality())
        self.assertIn("Usage: /x-quality", self.quality("bogus"))

    def test_main_runs_quality_run(self):
        self.set_config({"quality": {"gates": {"lint": {"command": "exit 0"}}}})
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.root}), contextlib.redirect_stdout(out):
            self.assertEqual(0, workflow.main(["quality", "run", "lint"]))
        self.assertIn("  lint: pass", out.getvalue())


if __name__ == "__main__":
    unittest.main()
