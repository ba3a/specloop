import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import install  # noqa: E402

sys.path.insert(0, HERE)
import gitfixture  # noqa: E402


class InstallTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        # Keep git from discovering a repository above the temp directory.
        self.ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(self.root)

    def tearDown(self):
        if self.ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = self.ceiling
        shutil.rmtree(self.root, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def read_json(self, *parts):
        with open(self.path(*parts), encoding="utf-8") as f:
            return json.load(f)

    def write(self, rel, text):
        os.makedirs(os.path.dirname(self.path(rel)) or self.root, exist_ok=True)
        with open(self.path(rel), "w", encoding="utf-8") as f:
            f.write(text)

    def specloop_groups(self, settings, event):
        return [g for g in settings.get("hooks", {}).get(event, []) if install.is_specloop_group(g)]

    def test_fresh_install_lays_out_kit_commands_and_hooks(self):
        install.install(self.root, test_command="make test")
        for name in ("engine.py", "run.sh", "defaults.json", "VERSION", "phases/plan.md", "config.json"):
            self.assertTrue(os.path.exists(self.path(".claude", "specloop", name)), name)
        self.assertTrue(os.path.isdir(self.path(".claude", "specloop", "overrides")))
        self.assertTrue(os.path.exists(self.path(".claude", "commands", "sl-go.md")))
        with open(self.path(".claude", "commands", "sl-spec.md"), encoding="utf-8") as f:
            self.assertIn("/sl-go", f.read())
        settings = self.read_json(".claude", "settings.json")
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"):
            self.assertEqual(1, len(self.specloop_groups(settings, event)), event)
        self.assertEqual(900 + 1800 + 60, settings["hooks"]["Stop"][0]["hooks"][0]["timeout"])
        self.assertEqual(900 + 1800 + 60, self.read_json(".claude", "specloop", "manifest.json")["stopTimeoutSec"])
        config = self.read_json(".claude", "specloop", "config.json")
        self.assertEqual("make test", config["phases"]["implement"]["check"]["command"])

    def stop_timeout(self, scope="project"):
        name = "settings.json" if scope == "project" else "settings.local.json"
        return self.read_json(".claude", name)["hooks"]["Stop"][0]["hooks"][0]["timeout"]

    def test_stop_timeout_is_check_plus_quality_budget_plus_a_minute(self):
        self.write(".claude/specloop/config.json", json.dumps({"checkTimeoutSec": 100, "quality": {"timeoutSec": 200}}))
        install.install(self.root)
        manifest = self.read_json(".claude", "specloop", "manifest.json")
        self.assertEqual(100 + 200 + 60, self.stop_timeout())
        self.assertEqual(360, manifest["stopTimeoutSec"])

    def test_stop_timeout_uses_the_defaults_for_missing_keys(self):
        self.write(".claude/specloop/config.json", json.dumps({"checkTimeoutSec": 100}))
        install.install(self.root)
        self.assertEqual(100 + 1800 + 60, self.stop_timeout())
        self.write(".claude/specloop/config.json", json.dumps({"quality": {"timeoutSec": 50}}))
        install.install(self.root)
        self.assertEqual(900 + 50 + 60, self.stop_timeout())
        self.assertEqual(1010, self.read_json(".claude", "specloop", "manifest.json")["stopTimeoutSec"])

    def test_stop_timeout_ignores_malformed_settings(self):
        self.write(".claude/specloop/config.json", json.dumps({"checkTimeoutSec": "x", "quality": {"timeoutSec": -1}}))
        install.install(self.root)
        self.assertEqual(900 + 1800 + 60, self.stop_timeout())
        self.write(".claude/specloop/config.json", json.dumps({"quality": ["x"]}))
        install.install(self.root)
        self.assertEqual(900 + 1800 + 60, self.stop_timeout())

    def test_reinstall_resizes_the_hook_and_the_manifest_together(self):
        install.install(self.root)
        self.write(".claude/specloop/config.json", json.dumps({"quality": {"timeoutSec": 3000}}))
        install.install(self.root)
        self.assertEqual(900 + 3000 + 60, self.stop_timeout())
        manifest = self.read_json(".claude", "specloop", "manifest.json")
        self.assertEqual(self.stop_timeout(), manifest["stopTimeoutSec"])
        self.assertIn("commands", manifest)
        self.assertIn("settings", manifest)
        self.assertIn("source", manifest)

    def test_local_scope_writes_the_same_timeout(self):
        install.install(self.root, scope="local")
        self.assertEqual(2760, self.stop_timeout("local"))
        self.assertEqual(2760, self.read_json(".claude", "specloop", "manifest.json")["stopTimeoutSec"])

    def test_test_command_still_writes_the_implement_check(self):
        install.install(self.root, test_command="make test")
        config = self.read_json(".claude", "specloop", "config.json")
        self.assertEqual({"type": "command", "command": "make test"}, config["phases"]["implement"]["check"])
        self.assertNotIn("quality", config)

    def test_commands_that_change_the_workflow_are_only_for_the_user(self):
        install.install(self.root, test_command="make test")
        for name in ("spec", "feature", "go", "advance", "pause", "reset", "update", "quality"):
            with open(self.path(".claude", "commands", "sl-%s.md" % name), encoding="utf-8") as f:
                self.assertIn("disable-model-invocation: true\n", f.read(), name)
        with open(self.path(".claude", "commands", "sl-status.md"), encoding="utf-8") as f:
            self.assertNotIn("disable-model-invocation", f.read())

    def test_quality_command_installed_with_prefix(self):
        install.install(self.root, test_command="make test", prefix="x-")
        with open(self.path(".claude", "commands", "x-quality.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn('run.sh quality "$ARGUMENTS"', text)
        self.assertIn("/x-quality", text)
        self.assertNotIn("{{prefix}}", text)
        manifest = self.read_json(".claude", "specloop", "manifest.json")
        self.assertIn(".claude/commands/x-quality.md", manifest["commands"])

    def test_update_command_installed_with_prefix(self):
        install.install(self.root, test_command="make test")
        with open(self.path(".claude", "commands", "sl-update.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("run.sh update $ARGUMENTS", text)
        self.assertIn("/sl-update", text)
        self.assertNotIn("{{prefix}}", text)
        manifest = self.read_json(".claude", "specloop", "manifest.json")
        self.assertIn(".claude/commands/sl-update.md", manifest["commands"])

    def test_feature_command_installed_with_prefix(self):
        install.install(self.root, test_command="make test")
        with open(self.path(".claude", "commands", "sl-feature.md"), encoding="utf-8") as f:
            feature = f.read()
        self.assertIn("run.sh init-feature", feature)
        self.assertIn("/sl-go", feature)
        self.assertNotIn("{{prefix}}", feature)
        self.assertTrue(os.path.exists(self.path(".claude", "specloop", "phases", "feature-plan.md")))

    def test_presets_are_installed_and_replaced_on_update(self):
        install.install(self.root, test_command="make test")
        presets = self.path(".claude", "specloop", "presets")
        self.assertEqual(["go", "java-gradle", "java-maven", "kotlin-gradle", "node", "python", "rust"],
                         sorted(n[:-5] for n in os.listdir(presets)))
        self.write(".claude/specloop/presets/go.json", "edited")
        self.write(".claude/specloop/presets/stale.json", "{}")
        install.install(self.root)
        self.assertEqual("go.mod", self.read_json(".claude", "specloop", "presets", "go.json")["detect"][0])
        self.assertFalse(os.path.exists(os.path.join(presets, "stale.json")))

    def test_no_subagent_stop_hook_installed(self):
        install.install(self.root, test_command="make test")
        self.assertTrue(os.path.exists(self.path(".claude", "specloop", "phases", "delegate.md")))
        hooks = self.read_json(".claude", "settings.json")["hooks"]
        self.assertNotIn("SubagentStop", hooks)
        self.assertEqual({"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"},
                         {event for event in hooks if self.specloop_groups({"hooks": hooks}, event)})

    def test_install_preserves_foreign_settings_and_is_idempotent(self):
        foreign = {"permissions": {"allow": ["Bash(ls)"]},
                   "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
        self.write(".claude/settings.json", json.dumps(foreign))
        install.install(self.root, test_command="true")
        install.install(self.root, test_command="true")
        settings = self.read_json(".claude", "settings.json")
        self.assertEqual(["Bash(ls)"], settings["permissions"]["allow"])
        self.assertEqual(2, len(settings["hooks"]["Stop"]))
        self.assertEqual(1, len(self.specloop_groups(settings, "Stop")))

    def test_update_keeps_project_config_and_overrides(self):
        install.install(self.root, test_command="first")
        self.write(".claude/specloop/overrides/plan.md", "mine")
        config = self.read_json(".claude", "specloop", "config.json")
        config["maxIterations"] = 3
        self.write(".claude/specloop/config.json", json.dumps(config))
        install.install(self.root)
        config = self.read_json(".claude", "specloop", "config.json")
        self.assertEqual(3, config["maxIterations"])
        self.assertEqual("first", config["phases"]["implement"]["check"]["command"])
        with open(self.path(".claude", "specloop", "overrides", "plan.md")) as f:
            self.assertEqual("mine", f.read())

    def test_explicit_test_command_updates_existing_config(self):
        install.install(self.root, test_command="first")
        install.install(self.root, test_command="second")
        config = self.read_json(".claude", "specloop", "config.json")
        self.assertEqual("second", config["phases"]["implement"]["check"]["command"])

    def test_prefix_change_replaces_previous_commands(self):
        install.install(self.root, test_command="true")
        install.install(self.root, prefix="")
        commands = sorted(os.listdir(self.path(".claude", "commands")))
        self.assertIn("go.md", commands)
        self.assertNotIn("sl-go.md", commands)
        self.assertEqual("", self.read_json(".claude", "specloop", "config.json")["commandPrefix"])

    def test_local_scope_moves_hooks_out_of_project_settings(self):
        install.install(self.root, test_command="true")
        install.install(self.root, scope="local")
        self.assertEqual([], self.specloop_groups(self.read_json(".claude", "settings.json"), "Stop"))
        local = self.read_json(".claude", "settings.local.json")
        self.assertEqual(1, len(self.specloop_groups(local, "Stop")))

    def git_init(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)

    def read_gitignore(self):
        with open(self.path(".gitignore"), encoding="utf-8") as f:
            return f.read()

    def git_check_ignore(self, rel):
        return subprocess.run(["git", "check-ignore", "-q", rel], cwd=self.root).returncode == 0

    def test_gitignore_ignores_kit_but_not_project_files(self):
        self.git_init()
        install.install(self.root, test_command="true")
        for rel in (".claude/specloop/engine.py", ".claude/specloop/phases/plan.md",
                    ".claude/specloop/state.json", ".claude/specloop/run/x", ".claude/commands/sl-go.md"):
            self.assertTrue(self.git_check_ignore(rel), rel)
        for rel in (".claude/specloop/config.json", ".claude/specloop/overrides/plan.md",
                    ".claude/settings.json", ".claude/commands/mine.md"):
            self.assertFalse(self.git_check_ignore(rel), rel)

    def test_gitignore_block_written_once_and_follows_prefix(self):
        self.git_init()
        self.write(".gitignore", "node_modules/")
        install.install(self.root, test_command="true")
        install.install(self.root, prefix="x-")
        text = self.read_gitignore()
        self.assertTrue(text.startswith("node_modules/\n"))
        self.assertEqual(1, text.count(install.GITIGNORE_BEGIN))
        self.assertIn(".claude/commands/x-go.md", text)
        self.assertNotIn(".claude/commands/sl-go.md", text)

    def test_gitignore_replaces_legacy_runtime_entries(self):
        self.git_init()
        self.write(".gitignore", "build/\n\n# specloop runtime state\n"
                                 ".claude/specloop/state.json\n.claude/specloop/run/\n")
        install.install(self.root, test_command="true")
        text = self.read_gitignore()
        self.assertNotIn("# specloop runtime state", text)
        self.assertTrue(text.startswith("build/\n\n" + install.GITIGNORE_BEGIN))

    def test_gitignore_untouched_outside_git(self):
        install.install(self.root, test_command="true")
        self.assertFalse(os.path.exists(self.path(".gitignore")))

    def test_no_gitignore_option_skips_gitignore(self):
        self.git_init()
        install.install(self.root, test_command="true", gitignore=False)
        self.assertFalse(os.path.exists(self.path(".gitignore")))

    def test_uninstall_removes_gitignore_block(self):
        self.git_init()
        self.write(".gitignore", "node_modules/\n")
        install.install(self.root, test_command="true")
        install.uninstall(self.root)
        self.assertEqual("node_modules/\n", self.read_gitignore())

    def test_uninstall_removes_everything_but_foreign_settings(self):
        self.write(".claude/settings.json", json.dumps({"model": "opus"}))
        self.write(".claude/commands/mine.md", "x")
        install.install(self.root, test_command="true")
        install.uninstall(self.root)
        self.assertFalse(os.path.exists(self.path(".claude", "specloop")))
        self.assertEqual(["mine.md"], os.listdir(self.path(".claude", "commands")))
        self.assertEqual({"model": "opus"}, self.read_json(".claude", "settings.json"))

    AGENTS = ["specloop-implementer.md", "specloop-orchestrator.md", "specloop-planner.md", "specloop-reviewer.md"]

    def agents_dir(self):
        return sorted(os.listdir(self.path(".claude", "agents")))

    def test_fresh_install_copies_agents_and_records_them(self):
        install.install(self.root, test_command="true")
        self.assertEqual(self.AGENTS, self.agents_dir())
        manifest = self.read_json(".claude", "specloop", "manifest.json")
        self.assertEqual([".claude/agents/" + name for name in self.AGENTS], manifest["agents"])

    def frontmatter(self, name):
        with open(self.path(".claude", "agents", name), encoding="utf-8") as f:
            text = f.read()
        head = text.split("---\n")[1]
        return dict(line.split(": ", 1) for line in head.splitlines()), text

    def test_agent_frontmatter(self):
        install.install(self.root, test_command="true")
        for name in self.AGENTS:
            fields, text = self.frontmatter(name)
            self.assertEqual(name[:-3], fields["name"])
            self.assertTrue(fields["description"])
            self.assertEqual("inherit", fields["model"])
            self.assertNotIn("/sl-", text, name)
            self.assertNotIn("{{prefix}}", text, name)
            if name == "specloop-orchestrator.md":
                self.assertEqual("Agent, Read, Grep, Glob", fields["tools"])
            else:
                self.assertNotIn("tools", fields, name)

    def test_reinstall_replaces_modified_agent_file(self):
        install.install(self.root, test_command="true")
        self.write(".claude/agents/specloop-reviewer.md", "changed")
        install.install(self.root)
        with open(self.path(".claude", "agents", "specloop-reviewer.md"), encoding="utf-8") as f:
            self.assertNotEqual("changed", f.read())

    def test_own_agent_survives_install_reinstall_and_uninstall(self):
        self.write(".claude/agents/mine.md", "mine")
        install.install(self.root, test_command="true")
        install.install(self.root)
        self.assertEqual(sorted(self.AGENTS + ["mine.md"]), self.agents_dir())
        install.uninstall(self.root)
        self.assertEqual(["mine.md"], self.agents_dir())
        with open(self.path(".claude", "agents", "mine.md"), encoding="utf-8") as f:
            self.assertEqual("mine", f.read())

    def test_uninstall_keep_config_removes_only_shipped_agents(self):
        self.write(".claude/agents/mine.md", "mine")
        install.install(self.root, test_command="true")
        install.uninstall(self.root, keep_config=True)
        self.assertEqual(["mine.md"], self.agents_dir())

    def test_gitignore_ignores_installed_agents(self):
        self.git_init()
        install.install(self.root, test_command="true")
        for name in self.AGENTS:
            self.assertTrue(self.git_check_ignore(".claude/agents/" + name), name)
        self.assertFalse(self.git_check_ignore(".claude/agents/mine.md"))

    def test_uninstall_keep_config(self):
        install.install(self.root, test_command="true")
        install.uninstall(self.root, keep_config=True)
        self.assertEqual(["config.json", "overrides"], sorted(os.listdir(self.path(".claude", "specloop"))))

    LOCAL_CONFIG = b'{\n  "update": {"mode": "off"},\r\n  "custom": 1\n}\n'

    def write_local_config(self):
        with open(self.path(".claude", "specloop", "config.local.json"), "wb") as f:
            f.write(self.LOCAL_CONFIG)

    def assert_local_config_unchanged(self):
        with open(self.path(".claude", "specloop", "config.local.json"), "rb") as f:
            self.assertEqual(self.LOCAL_CONFIG, f.read())

    def test_install_never_creates_local_config(self):
        install.install(self.root, test_command="true")
        install.install(self.root, force=True)
        self.assertFalse(os.path.exists(self.path(".claude", "specloop", "config.local.json")))

    def test_reinstall_and_force_keep_local_config(self):
        install.install(self.root, test_command="true")
        self.write_local_config()
        install.install(self.root)
        self.assert_local_config_unchanged()
        install.install(self.root, force=True)
        self.assert_local_config_unchanged()

    def test_uninstall_keep_config_keeps_local_config(self):
        install.install(self.root, test_command="true")
        self.write_local_config()
        install.uninstall(self.root, keep_config=True)
        self.assertEqual(["config.json", "config.local.json", "overrides"],
                         sorted(os.listdir(self.path(".claude", "specloop"))))
        self.assert_local_config_unchanged()

    def test_uninstall_without_keep_config_removes_local_config(self):
        install.install(self.root, test_command="true")
        self.write_local_config()
        install.uninstall(self.root)
        self.assertFalse(os.path.exists(self.path(".claude", "specloop")))

    def test_gitignore_ignores_local_config(self):
        self.git_init()
        install.install(self.root, test_command="true")
        self.write_local_config()
        self.assertTrue(self.git_check_ignore(".claude/specloop/config.local.json"))

    def test_detects_test_commands(self):
        cases = [("gradlew", "", "./gradlew test -q"),
                 ("pom.xml", "", "mvn -q test"),
                 ("package.json", '{"scripts": {"test": "jest"}}', "npm test --silent"),
                 ("pyproject.toml", "", "python -m pytest -q"),
                 ("go.mod", "", "go test ./..."),
                 ("Cargo.toml", "", "cargo test -q"),
                 ("App.csproj", "", "dotnet test")]
        for name, content, expected in cases:
            root = tempfile.mkdtemp()
            try:
                with open(os.path.join(root, name), "w") as f:
                    f.write(content)
                self.assertEqual(expected, install.detect_test_command(root), name)
            finally:
                shutil.rmtree(root)

    def test_no_detected_tests_leaves_implement_unchecked(self):
        install.install(self.root)
        config = self.read_json(".claude", "specloop", "config.json")
        self.assertIsNone(config["phases"]["implement"]["check"])

    def test_cli_rejects_missing_target(self):
        self.assertEqual(1, install.main([self.path("missing")]))


@unittest.skipUnless(gitfixture.git_available(), "git not available")
class InstallSourceTest(unittest.TestCase):
    """install.py records the clone it runs from in manifest.json for update checks."""

    def setUp(self):
        self.fx = gitfixture.GitFixture()
        self.addCleanup(self.fx.cleanup)

    def manifest(self):
        with open(os.path.join(self.fx.target, ".claude", "specloop", "manifest.json"), encoding="utf-8") as f:
            return json.load(f)

    def install(self, *args, **kwargs):
        result = self.fx.run_install("--test-command", "exit 0", *args, **kwargs)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def test_records_clone_upstream_and_commit(self):
        result = self.install()
        manifest = self.manifest()
        source = manifest["source"]
        self.assertEqual(self.fx.clone.replace("\\", "/"), source["path"])
        self.assertTrue(os.path.isabs(source["path"]))
        self.assertNotIn("\\", source["path"])
        self.assertEqual("origin", source["remote"])
        self.assertEqual("main", source["branch"])
        self.assertEqual(self.fx.head(), source["commit"])
        self.assertEqual(self.fx.remote, os.path.normpath(source["url"]))
        self.assertEqual(self.fx.read_version(), source["version"])
        self.assertEqual({"scope": "project", "gitignore": True}, source["installArgs"])
        self.assertTrue(source["checkable"])
        self.assertNotIn("update checks unavailable", result.stdout)
        self.assertIn("commands", manifest)
        self.assertIn("settings", manifest)

    def test_records_install_args(self):
        self.install("--scope", "local", "--no-gitignore")
        self.assertEqual({"scope": "local", "gitignore": False}, self.manifest()["source"]["installArgs"])

    def test_commit_follows_clone_head(self):
        commit = self.fx.bump_clone_version("9.9.9")
        self.install()
        source = self.manifest()["source"]
        self.assertEqual(commit, source["commit"])
        self.assertEqual("9.9.9", source["version"])

    def test_branch_without_upstream_is_not_checkable(self):
        self.fx.git(self.fx.clone, "checkout", "-q", "-b", "local-only")
        result = self.install()
        manifest = self.manifest()
        source = manifest["source"]
        self.assertFalse(source["checkable"])
        self.assertIn("upstream", source["reason"])
        self.assertEqual(self.fx.clone.replace("\\", "/"), source["path"])
        self.assertEqual(self.fx.read_version(), source["version"])
        self.assertNotIn("commit", source)
        lines = [ln for ln in result.stdout.splitlines() if "update checks unavailable" in ln]
        self.assertEqual(["! update checks unavailable: " + source["reason"]], lines)
        self.assertIn("commands", manifest)

    def test_non_git_copy_is_not_checkable(self):
        copy = os.path.join(self.fx.root, "zip")
        gitfixture.copy_kit_source(copy)
        result = self.install(source=copy)
        manifest = self.manifest()
        source = manifest["source"]
        self.assertEqual({"path", "version", "checkable", "reason"}, set(source))
        self.assertEqual(copy.replace("\\", "/"), source["path"])
        self.assertFalse(source["checkable"])
        self.assertTrue(source["reason"])
        lines = [ln for ln in result.stdout.splitlines() if "update checks unavailable" in ln]
        self.assertEqual(["! update checks unavailable: " + source["reason"]], lines)
        self.assertIn("commands", manifest)
        self.assertIn("settings", manifest)

    def test_copy_inside_another_work_tree_is_not_checkable(self):
        copy = os.path.join(self.fx.clone, "vendored")
        gitfixture.copy_kit_source(copy)
        result = self.install(source=copy)
        source = self.manifest()["source"]
        self.assertEqual({"path", "version", "checkable", "reason"}, set(source))
        self.assertEqual(copy.replace("\\", "/"), source["path"])
        self.assertFalse(source["checkable"])
        self.assertIn("not the top of a git work tree", source["reason"])
        lines = [ln for ln in result.stdout.splitlines() if "update checks unavailable" in ln]
        self.assertEqual(["! update checks unavailable: " + source["reason"]], lines)


if __name__ == "__main__":
    unittest.main()
