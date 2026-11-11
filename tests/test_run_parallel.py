import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "run_parallel.py")

PASSING = (
    "import subprocess, sys, unittest\n"
    "class First(unittest.TestCase):\n"
    "    def test_one(self):\n"
    "        pass\n"
    "    def test_prints(self):\n"
    "        print('{\"noise\": 1}')\n"
    "        subprocess.run([sys.executable, '-c', 'print(\"child noise\")'], check=True)\n"
    "class Second(unittest.TestCase):\n"
    "    def test_two(self):\n"
    "        pass\n"
    "    @unittest.skip('not now')\n"
    "    def test_skipped(self):\n"
    "        pass\n"
)
FAILING = (
    "import unittest\n"
    "class Broken(unittest.TestCase):\n"
    "    def test_bad(self):\n"
    "        self.assertEqual(1, 2, 'the marker message')\n"
)
DYING = (
    "import os, unittest\n"
    "class Dies(unittest.TestCase):\n"
    "    def test_exit(self):\n"
    "        os._exit(3)\n"
)


class RunParallelTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
            f.write(text)

    def run_suite(self, *args):
        return subprocess.run([sys.executable, RUNNER, "-s", self.root] + list(args),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)

    def test_passing_suite_exits_zero_and_counts_every_test(self):
        self.write("test_a.py", PASSING)
        result = self.run_suite("-j", "2")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Ran 4 tests", result.stdout)
        self.assertIn("on 2 workers", result.stdout)
        self.assertIn("OK (skipped=1)", result.stdout)

    def test_output_of_tests_goes_to_stderr_and_does_not_break_the_run(self):
        self.write("test_a.py", PASSING)
        result = self.run_suite("-j", "1")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("noise", result.stdout)
        self.assertIn('{"noise": 1}', result.stderr)
        self.assertIn("child noise", result.stderr)

    def test_failing_test_exits_one_and_shows_the_failure(self):
        self.write("test_a.py", PASSING)
        self.write("test_b.py", FAILING)
        result = self.run_suite("-j", "2")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("FAILED test_b.Broken", result.stdout)
        self.assertIn("the marker message", result.stdout)
        self.assertIn("Ran 5 tests", result.stdout)
        self.assertIn("FAILED (failures=1, errors=0)", result.stdout)

    def test_a_dead_worker_fails_the_run_and_the_other_classes_still_run(self):
        self.write("test_a.py", PASSING)
        self.write("test_c.py", DYING)
        result = self.run_suite("-j", "2")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("test_c.Dies: the worker process exited", result.stdout)
        self.assertIn("ok     test_a.First", result.stdout)
        self.assertIn("ok     test_a.Second", result.stdout)

    def test_a_module_that_fails_to_import_exits_one(self):
        self.write("test_a.py", PASSING)
        self.write("test_d.py", "import no_such_module_anywhere\n")
        result = self.run_suite()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("no_such_module_anywhere", result.stderr)

    def test_durations_lists_the_slowest_classes(self):
        self.write("test_a.py", PASSING)
        result = self.run_suite("--durations", "1")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        tail = result.stdout.split("Slowest test classes:")[1]
        self.assertEqual(1, sum(1 for line in tail.splitlines() if "test_a." in line))


if __name__ == "__main__":
    unittest.main()
