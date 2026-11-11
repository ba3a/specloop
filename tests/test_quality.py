"""Tests for quality gates: the `quality` configuration block and its validation."""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(os.path.dirname(HERE), "kit", "specloop")
sys.path.insert(0, KIT)

import engine as workflow  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
