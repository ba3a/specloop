"""The development tools (ruff, mypy) are pinned and configured; the kit never imports them."""

import ast
import configparser
import os
import re
import subprocess
import unittest

try:
    import tomllib as _toml  # Python 3.11+
except ImportError:  # the kit supports 3.8; there the config is checked as text
    _toml = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV_TOOLS = ("ruff", "mypy")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class RequirementsDevTest(unittest.TestCase):
    def pins(self):
        out = {}
        for line in read("requirements-dev.txt").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                m = re.fullmatch(r"([A-Za-z0-9_.-]+)==(\d+(?:\.\d+)+)", line)
                self.assertIsNotNone(m, "not an exact pin: %r" % line)
                out[m.group(1).lower()] = m.group(2)
        return out

    def test_ruff_and_mypy_are_pinned_exactly(self):
        self.assertEqual(set(DEV_TOOLS), set(self.pins()))

    def test_kit_and_installer_do_not_import_dev_tools(self):
        files = [os.path.join(ROOT, "install.py")]
        for base, _dirs, names in os.walk(os.path.join(ROOT, "kit")):
            files += [os.path.join(base, n) for n in names if n.endswith(".py")]
        for path in files:
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    self.assertNotIn(name.split(".")[0], DEV_TOOLS, path)


class RuffConfigTest(unittest.TestCase):
    def config(self):
        text = read("ruff.toml")
        if _toml is not None:
            return _toml.loads(text)
        select = re.search(r"^select\s*=\s*\[(.*?)\]", text, re.M | re.S)
        target = re.search(r'^target-version\s*=\s*"([^"]+)"', text, re.M)
        ignore = re.search(r"^(?:extend-)?ignore\s*=\s*\[(.*?)\]", text, re.M | re.S)
        return {
            "target-version": target and target.group(1),
            "lint": {
                "select": re.findall(r'"([^"]+)"', select.group(1)) if select else [],
                "ignore": re.findall(r'"([^"]+)"', ignore.group(1)) if ignore else [],
            },
        }

    def test_targets_py38(self):
        self.assertEqual("py38", self.config()["target-version"])

    def test_rule_sets(self):
        self.assertEqual({"E", "F", "W", "I", "B"}, set(self.config()["lint"]["select"]))

    def test_every_ignore_has_a_comment(self):
        lines = read("ruff.toml").splitlines()
        for i, line in enumerate(lines):
            if re.match(r"\s*\"[A-Z]+\d*\"\s*[,\]]", line) or re.match(r"\s*(?:extend-)?ignore\s*=", line) \
                    or re.match(r"\s*\"[^\"]+\"\s*=\s*\[", line):
                if "select" in line:
                    continue
                self.assertTrue(
                    "#" in line or (i > 0 and lines[i - 1].lstrip().startswith("#")),
                    "ignore without a comment: %r" % line,
                )


class MypyConfigTest(unittest.TestCase):
    def sections(self):
        parser = configparser.ConfigParser()
        parser.read_string(read("mypy.ini"))
        return parser

    def test_runs_on_engine_installer_and_tools_without_arguments(self):
        files = [f.strip() for f in self.sections()["mypy"]["files"].split(",")]
        self.assertEqual(["kit/specloop/engine.py", "install.py", "tools"], files)
        for f in files:
            self.assertTrue(os.path.exists(os.path.join(ROOT, f)), f)

    def test_checks_untyped_defs(self):
        self.assertTrue(self.sections()["mypy"].getboolean("check_untyped_defs"))

    def test_every_silenced_error_has_a_comment(self):
        lines = read("mypy.ini").splitlines()
        for i, line in enumerate(lines):
            if re.match(r"\s*(?:disable_error_code|ignore_errors|ignore_missing_imports|follow_imports)\s*=", line) \
                    or line.startswith("[mypy-"):
                self.assertTrue(
                    "#" in line or (i > 0 and lines[i - 1].lstrip().startswith("#")),
                    "silenced without a comment: %r" % line,
                )

    def test_sources_carry_no_type_ignore(self):
        for path in ("install.py", os.path.join("kit", "specloop", "engine.py"), os.path.join("tools", "check_rules.py")):
            self.assertNotIn("# type: ignore", read(path), path)


class GitignoreTest(unittest.TestCase):
    def ignored(self, path):
        # --no-index: answer from the patterns alone, whatever is tracked or exists.
        return subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT).returncode == 0

    def test_claude_dir_is_ignored_except_specloop(self):
        lines = read(".gitignore").splitlines()
        self.assertIn(".claude/*", lines)
        self.assertIn("!.claude/specloop/", lines)
        self.assertNotIn(".claude/", lines)

    def test_runtime_files_and_commands_stay_ignored(self):
        for path in (".claude/specloop/state.json", ".claude/specloop/run/steps.json", ".claude/commands/sl-go.md",
                     ".claude/settings.local.json"):
            self.assertTrue(self.ignored(path), path)

    def test_project_owned_specloop_files_are_not_ignored(self):
        for path in (".claude/specloop/config.json", ".claude/specloop/overrides/implement.md"):
            self.assertFalse(self.ignored(path), path)


class ClaudeMdTest(unittest.TestCase):
    def section(self, title):
        text = read("CLAUDE.md")
        start = text.index("## " + title)
        end = text.find("\n## ", start + 1)
        return text[start:end if end != -1 else len(text)]

    def test_commands_list_dev_requirements_and_gates(self):
        commands = self.section("Commands")
        for cmd in ("pip install -r requirements-dev.txt", "python tools/check_rules.py", "python -m ruff check .", "python -m mypy"):
            self.assertIn(cmd, commands)

    def test_layout_lists_quality_files(self):
        layout = self.section("Layout")
        for name in ("kit/specloop/presets/", "kit/commands/quality.md", "tools/", "requirements-dev.txt", "ruff.toml", "mypy.ini"):
            self.assertIn(name, layout)
        self.assertEqual(layout.count("tools/check_rules.py"), 1)

    def test_installed_layout_mentions_presets_and_quality_runtime_files(self):
        text = read("CLAUDE.md")
        self.assertIn("presets/", text.split("Installed layout in a target")[1])
        self.assertIn("run/quality", text)


if __name__ == "__main__":
    unittest.main()
