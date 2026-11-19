"""Implement agents run targeted tests; the quality gates run the full suite."""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = (
    ("kit", "agents", "specloop-implementer.md"),
    ("kit", "specloop", "phases", "step-implement.md"),
    ("kit", "specloop", "phases", "implement.md"),
)
# Wording that tells the agent to run the whole suite or the full test command.
FULL_SUITE = re.compile(
    r"test command must pass|(?<!not )runs? the (full |whole |entire )?test (suite|command)"
    r"|(?<!not )run the (full|whole|entire) (test )?(suite|test command)",
    re.IGNORECASE,
)


def read(parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class TargetedTests(unittest.TestCase):
    def test_no_file_asks_for_the_full_suite(self):
        for parts in FILES:
            match = FULL_SUITE.search(read(parts))
            self.assertIsNone(match, "%s: %s" % ("/".join(parts), match and match.group(0)))

    def test_every_file_asks_for_targeted_tests_and_names_the_gates(self):
        for parts in FILES:
            text = read(parts).lower()
            self.assertIn("targeted", text, "/".join(parts))
            self.assertIn("quality gates", text, "/".join(parts))

    def test_detector_catches_the_old_wording(self):
        self.assertTrue(FULL_SUITE.search("the test command must pass."))
        self.assertTrue(FULL_SUITE.search("The workflow then runs the test suite;"))


if __name__ == "__main__":
    unittest.main()
