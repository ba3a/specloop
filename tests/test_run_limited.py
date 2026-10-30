import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(os.path.dirname(HERE), "kit", "specloop")
sys.path.insert(0, KIT)
sys.path.insert(0, HERE)

import engine as workflow  # noqa: E402
import gitfixture  # noqa: E402

CHILD_SECONDS = 12
MARGIN = 3  # Seconds a bounded call may take beyond its timeout.

CHILD_CODE = (
    "import os, sys, time\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    f.write(str(os.getpid()))\n"
    "time.sleep(%d)\n" % CHILD_SECONDS
)


def process_alive(pid):
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A killed child can linger as a zombie until its parent reaps it; count that as gone.
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def sh_quote(text):
    return "'" + text.replace("\\", "/").replace("'", "'\\''") + "'"


class HangingCommand(object):
    """Mixin: a stand-in command that starts a long-running child and waits for it, so the child
    holds the command's output streams. A .cmd file on Windows, a shell script elsewhere; the child is
    started through sys.executable and records its pid."""

    def make_command(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pidfile = os.path.join(self.dir, "child.pid")
        self.child = os.path.join(self.dir, "child.py")
        with open(self.child, "w") as f:
            f.write(CHILD_CODE)
        if os.name == "nt":
            self.command = os.path.join(self.dir, "fake-git.cmd")
            body = '@echo off\r\necho started\r\n"%s" "%s" "%s"\r\n' % (sys.executable, self.child, self.pidfile)
        else:
            self.command = os.path.join(self.dir, "fake-git.sh")
            body = '#!/bin/sh\necho started\n"%s" "%s" "%s"\n' % (sys.executable, self.child, self.pidfile)
        with open(self.command, "w", newline="") as f:
            f.write(body)
        os.chmod(self.command, os.stat(self.command).st_mode | stat.S_IXUSR)

    def child_pid(self):
        with open(self.pidfile) as f:
            return int(f.read())

    def wait_gone(self, pid):
        deadline = time.monotonic() + 5
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        return not process_alive(pid)


class HangingCommandTest(HangingCommand, unittest.TestCase):

    def setUp(self):
        self.make_command()

    def test_returns_near_the_timeout_and_kills_the_tree(self):
        started = time.monotonic()
        result = workflow.run_limited([self.command], 1)
        self.assertLess(time.monotonic() - started, 1 + MARGIN)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertTrue(self.wait_gone(self.child_pid()), "the child process survived the timeout")

    def test_merged_output_survives_a_timeout(self):
        result = workflow.run_limited([self.command], 1, merge_stderr=True)
        self.assertTrue(result.timed_out)
        self.assertIn(b"started", result.stdout)
        self.assertEqual(b"", result.stderr)
        self.assertTrue(self.wait_gone(self.child_pid()))

    def test_update_git_reports_a_timeout(self):
        saved = workflow.GIT
        workflow.GIT = self.command
        try:
            started = time.monotonic()
            ok, output, error = workflow.update_git(self.dir, 1, "fetch", "--quiet")
            elapsed = time.monotonic() - started
        finally:
            workflow.GIT = saved
        self.assertLess(elapsed, 1 + MARGIN)
        self.assertEqual((False, "", "git fetch timed out after 1s"), (ok, output, error))
        self.assertTrue(self.wait_gone(self.child_pid()))

    def test_command_check_times_out(self):
        script = " ".join(sh_quote(part) for part in (sys.executable, self.child, self.pidfile))
        config = {"checkTimeoutSec": 1, "maxOutputChars": 4000}
        started = time.monotonic()
        result, details, _ = workflow.check_phase(
            self.dir, config, {"check": {"type": "command", "command": script}}, {})
        self.assertLess(time.monotonic() - started, 1 + MARGIN)
        self.assertEqual(("fail", "Check command timed out: %s" % script), (result, details))
        self.assertTrue(self.wait_gone(self.child_pid()))

    def test_git_helper_times_out(self):
        config = {"feature": {"gitTimeoutSec": 1}}
        saved = workflow.run_limited
        workflow.run_limited = lambda args, *a, **kw: saved([self.command] + list(args[1:]), *a, **kw)
        try:
            started = time.monotonic()
            ok, output = workflow.git(self.dir, config, "status")
            elapsed = time.monotonic() - started
        finally:
            workflow.run_limited = saved
        self.assertLess(elapsed, 1 + MARGIN)
        self.assertEqual((False, "git status timed out"), (ok, output))
        self.assertTrue(self.wait_gone(self.child_pid()))


class RunLimitedTest(unittest.TestCase):

    def test_captures_output_and_exit_code(self):
        code = "import sys; print('out'); sys.stderr.write('err'); sys.exit(3)"
        result = workflow.run_limited([sys.executable, "-c", code], 30)
        self.assertEqual((3, False), (result.returncode, result.timed_out))
        self.assertEqual("out", result.stdout.decode().strip())
        self.assertEqual("err", result.stderr.decode().strip())

    def test_merge_stderr(self):
        code = "import sys; print('out'); sys.stdout.flush(); sys.stderr.write('err')"
        result = workflow.run_limited([sys.executable, "-c", code], 30, merge_stderr=True)
        self.assertEqual(0, result.returncode)
        self.assertEqual(["err", "out"], sorted(result.stdout.decode().split()))
        self.assertEqual(b"", result.stderr)

    def test_stdin_is_the_null_device(self):
        code = "import sys; print(repr(sys.stdin.read()))"
        result = workflow.run_limited([sys.executable, "-c", code], 30)
        self.assertEqual("''", result.stdout.decode().strip())

    def test_cwd_and_env(self):
        cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cwd, True)
        code = "import os; print(os.getcwd()); print(os.environ['RUN_LIMITED_VAR'])"
        result = workflow.run_limited([sys.executable, "-c", code], 30, cwd=cwd,
                                      env=dict(os.environ, RUN_LIMITED_VAR="marker"))
        lines = result.stdout.decode().split()
        self.assertEqual(os.path.realpath(cwd), os.path.realpath(lines[0]))
        self.assertEqual("marker", lines[1])

    def test_missing_executable_raises_oserror(self):
        with self.assertRaises(OSError):
            workflow.run_limited([os.path.join(tempfile.gettempdir(), "specloop-no-such-executable")], 5)

    def test_git_helper_reports_a_missing_executable(self):
        saved = workflow.run_limited

        def missing(*args, **kwargs):
            raise FileNotFoundError("no git")

        workflow.run_limited = missing
        try:
            ok, output = workflow.git(tempfile.gettempdir(), {}, "status")
        finally:
            workflow.run_limited = saved
        self.assertFalse(ok)
        self.assertIn("git could not be run", output)

    def test_kill_failure_does_not_raise(self):
        class Gone(object):
            pid = 2 ** 22 + 12345

            def kill(self):
                raise OSError("already gone")

        workflow._kill_process_tree(Gone())


@unittest.skipUnless(gitfixture.git_available(), "git not available")
class UpdateCheckTimeoutTest(HangingCommand, unittest.TestCase):
    """update-check with a git that hangs records a timeout error within about a second of timeoutSec."""

    def setUp(self):
        self.make_command()
        self.fx = gitfixture.GitFixture()
        self.addCleanup(self.fx.cleanup)
        result = self.fx.run_install("--test-command", "exit 0")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_update_check_records_a_timeout_error(self):
        config_path = os.path.join(self.fx.target, ".claude", "specloop", "config.json")
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        config.setdefault("update", {})["timeoutSec"] = 1
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)
        saved = workflow.GIT
        workflow.GIT = self.command
        try:
            started = time.monotonic()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                workflow.cmd_update_check(self.fx.target)
            elapsed = time.monotonic() - started
        finally:
            workflow.GIT = saved
        self.assertLess(elapsed, 1 + MARGIN)
        self.assertIn("timed out", workflow.load_update_state(self.fx.target)["error"])
        self.assertTrue(self.wait_gone(self.child_pid()))


if __name__ == "__main__":
    unittest.main()
