"""Tests for tools/check_rules.py: every rule reports `path:line: rule: message` and exits 1."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, "tools", "check_rules.py")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import check_rules  # noqa: E402


class RepoCase(unittest.TestCase):
    """A throwaway git repository per test."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="check-rules-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=self.root, check=True)

    def add(self, rel, content, track=True):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        with open(path, "wb") as f:
            f.write(data)
        if track:
            subprocess.run(["git", "add", "--", rel], cwd=self.root, check=True)

    def run_tool(self):
        """Run the script as a process: (exit code, output lines)."""
        r = subprocess.run([sys.executable, TOOL, self.root], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        return r.returncode, r.stdout.splitlines()


class TestClean(RepoCase):
    def test_clean_repository_exits_zero_and_prints_nothing(self):
        self.add("install.py", "import os\nimport json\n")
        self.add("kit/specloop/engine.py", "import sys\nimport engine_helper\n\nprint(sys.argv)\n")
        self.add("kit/specloop/engine_helper.py", "X = 1\n")
        self.add("kit/specloop/run.sh", b"#!/bin/sh\nexit 0\n")
        self.add("install.cmd", b"@echo off\r\nexit /b 0\r\n")
        self.add("kit/commands/go.md", "Run /{{prefix}}go.\n")
        self.assertEqual(self.run_tool(), (0, []))

    def test_untracked_files_are_ignored(self):
        self.add("install.py", "import requests\n", track=False)
        self.assertEqual(self.run_tool(), (0, []))

    def test_this_repository_passes(self):
        r = subprocess.run([sys.executable, TOOL], cwd=ROOT, stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_the_tool_parses_with_python_38_grammar(self):
        with open(TOOL, "rb") as f:
            tree, problems = check_rules.parse_py("tools/check_rules.py", f.read())
        self.assertEqual(problems, [])
        self.assertIsNotNone(tree)


class TestGrammar(RepoCase):
    def test_syntax_error_is_reported_with_line(self):
        self.add("kit/specloop/engine.py", "x = 1\ndef broken(:\n    pass\n")
        code, lines = self.run_tool()
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("kit/specloop/engine.py:2: py38-grammar: "), lines[0])

    def test_syntax_newer_than_python_38_is_reported(self):
        # Only an interpreter newer than 3.8 can tell the grammars apart: the `match` statement parses there, but
        # not with feature_version=(3, 8).
        source = "match 1:\n    case 1:\n        pass\n"
        if sys.version_info < (3, 10):
            self.skipTest("the interpreter does not know the match statement")
        self.add("tests/test_x.py", source)
        code, lines = self.run_tool()
        self.assertEqual(code, 1)
        # Python reports the error on the line it fails at, which differs between versions.
        self.assertRegex(lines[0], r"^tests/test_x\.py:\d+: py38-grammar: ")

    def test_grammar_is_checked_in_all_four_locations(self):
        for rel in ("kit/specloop/engine.py", "install.py", "tools/x.py", "tests/test_x.py"):
            self.add(rel, "def broken(:\n")
        code, lines = self.run_tool()
        self.assertEqual(code, 1)
        self.assertEqual(sorted(line.split(":")[0] for line in lines),
                         ["install.py", "kit/specloop/engine.py", "tests/test_x.py", "tools/x.py"])

    def test_python_outside_the_checked_locations_is_ignored(self):
        self.add("marketing/logo/build.py", "def broken(:\n")
        self.assertEqual(self.run_tool(), (0, []))


class TestImports(RepoCase):
    def test_third_party_import_is_reported_with_line(self):
        self.add("install.py", "import os\n\nimport requests\n")
        code, lines = self.run_tool()
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("install.py:3: import-allowlist: "), lines[0])
        self.assertIn("requests", lines[0])

    def test_from_import_and_nested_import_are_reported(self):
        self.add("tools/x.py", "from yaml import safe_load\n\n\ndef f():\n    import numpy.linalg\n")
        code, lines = self.run_tool()
        self.assertEqual(code, 1)
        self.assertEqual([line.split(": ")[0] for line in lines], ["tools/x.py:1", "tools/x.py:5"])

    def test_dev_tools_are_not_allowed_in_the_kit(self):
        self.add("kit/specloop/engine.py", "import ruff\nimport mypy\n")
        code, lines = self.run_tool()
        self.assertEqual((code, len(lines)), (1, 2))

    def test_in_kit_and_relative_imports_are_allowed(self):
        self.add("kit/specloop/engine.py", "import sibling\nfrom . import other\nfrom .sibling import x\n")
        self.add("kit/specloop/sibling.py", "x = 1\n")
        self.assertEqual(self.run_tool(), (0, []))

    def test_tests_may_import_anything(self):
        self.add("tests/test_x.py", "import unittest\nimport pytest\n")
        self.assertEqual(self.run_tool(), (0, []))

    def test_allowlist_has_the_modules_the_kit_uses(self):
        for name in ("argparse", "json", "subprocess", "unicodedata", "fnmatch", "math", "typing"):
            self.assertIn(name, check_rules.STDLIB_3_8)
        for name in ("tomllib", "zoneinfo", "graphlib", "yaml"):
            self.assertNotIn(name, check_rules.STDLIB_3_8)


class TestPrefix(RepoCase):
    def test_string_literal_in_engine_is_reported_with_line(self):
        self.add("kit/specloop/engine.py", 'x = 1\nmessage = "run /sl-go now"\n')
        code, lines = self.run_tool()
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("kit/specloop/engine.py:2: hardcoded-prefix: "), lines[0])

    def test_f_string_in_engine_is_reported(self):
        self.add("kit/specloop/engine.py", 'name = "a"\nmessage = f"{name}: /sl-go"\n')
        code, lines = self.run_tool()
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith("kit/specloop/engine.py:2: hardcoded-prefix: "), lines[0])

    def test_comments_and_docstrings_in_engine_may_mention_the_prefix(self):
        self.add("kit/specloop/engine.py",
                 '"""Module doc: /sl-go."""\n\n# /sl-go in a comment\n\n\n'
                 'def f():\n    """/sl-go in a docstring."""\n    return 1\n\n\n'
                 'class C:\n    """/sl-go"""\n')
        self.assertEqual(self.run_tool(), (0, []))

    def test_other_python_files_are_not_checked_for_the_prefix(self):
        self.add("tests/test_x.py", 'X = "/sl-go"\n')
        self.add("install.py", 'X = "/sl-go"\n')
        self.assertEqual(self.run_tool(), (0, []))

    def test_command_template_is_reported_with_line(self):
        self.add("kit/commands/go.md", "Intro\n\nRun /sl-go to continue.\n")
        code, lines = self.run_tool()
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("kit/commands/go.md:3: hardcoded-prefix: "), lines[0])

    def test_markdown_elsewhere_may_mention_the_prefix(self):
        self.add("docs/commands.md", "`/sl-go`\n")
        self.add("kit/specloop/phases/plan.md", "/sl-go\n")
        self.assertEqual(self.run_tool(), (0, []))


class TestLineEndings(RepoCase):
    def test_crlf_in_sh_is_reported_with_line(self):
        self.add("kit/specloop/run.sh", b"#!/bin/sh\r\nexit 0\r\n")
        code, lines = self.run_tool()
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("kit/specloop/run.sh:1: line-endings: "), lines[0])

    def test_lf_in_cmd_is_reported_with_line(self):
        self.add("install.cmd", b"@echo off\r\necho hi\nexit /b 0\r\n")
        code, lines = self.run_tool()
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("install.cmd:2: line-endings: "), lines[0])

    def test_lf_sh_and_crlf_cmd_pass(self):
        self.add("a.sh", b"echo a\necho b\n")
        self.add("a.cmd", b"echo a\r\necho b\r\n")
        self.assertEqual(self.run_tool(), (0, []))

    def test_cmd_without_final_newline_passes(self):
        self.add("a.cmd", b"echo a\r\necho b")
        self.assertEqual(self.run_tool(), (0, []))


class TestMain(RepoCase):
    def test_violations_are_sorted_and_formatted(self):
        self.add("install.py", "import requests\n")
        self.add("a.sh", b"echo\r\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_rules.main(["check_rules.py", self.root])
        self.assertEqual(code, 1)
        lines = out.getvalue().splitlines()
        self.assertEqual([line.split(":")[0] for line in lines], ["a.sh", "install.py"])
        for line in lines:
            self.assertRegex(line, r"^[^:]+:\d+: [a-z0-9-]+: .+$")

    def test_not_a_git_repository_exits_two(self):
        empty = tempfile.mkdtemp(prefix="check-rules-")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = check_rules.main(["check_rules.py", empty])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
