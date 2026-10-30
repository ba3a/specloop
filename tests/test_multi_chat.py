import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(os.path.dirname(HERE), "kit", "specloop")
ENGINE = os.path.join(KIT, "engine.py")
sys.path.insert(0, KIT)

import engine as workflow  # noqa: E402

OWNER, OTHER = "owner-session-0001", "other-session-0002"


def force_rmtree(path):
    """Remove a tree, including git's read-only object files on Windows."""
    def make_writable(func, target, _):
        os.chmod(target, 0o700)
        func(target)
    if os.path.exists(path):
        shutil.rmtree(path, onerror=make_writable)


def steps():
    return {"feature": "F", "steps": [
        {"id": i, "title": "Step %d" % i, "description": "d", "acceptance": ["a"], "status": "pending"}
        for i in (1, 2)]}


class ProjectTest(unittest.TestCase):
    """A throwaway project with the kit copied in and a git repository."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(self.root)
        self.base = os.path.join(self.root, ".claude", "specloop")
        shutil.copytree(KIT, self.base)
        self.write("spec.md", "# Spec\nDo the thing.\n")
        self.set_config({})

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

    def init_git(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", "spec.md")
        self.git("commit", "-q", "-m", "initial")

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def set_config(self, config):
        with open(os.path.join(self.base, "config.json"), "w") as f:
            json.dump(config, f)

    def state(self):
        return workflow.load_state(self.root)

    def state_bytes(self):
        with open(os.path.join(self.base, "state.json"), "rb") as f:
            return f.read()

    def run_path(self, *parts):
        return os.path.join(self.base, "run", *parts)

    def attempt_file(self, name):
        return os.path.join(workflow.attempt_path(self.root, self.state()), name)

    def write_attempt(self, name, text):
        with open(self.attempt_file(name), "w", encoding="utf-8") as f:
            f.write(text)

    def start(self):
        workflow.cmd_init(self.root, "spec.md")
        return workflow.cmd_go(self.root)

    def start_feature(self, config=None):
        """A feature run in its first step's implement phase."""
        self.init_git()
        self.set_config(config or {"phases": {"implement": {"check": {"type": "command", "command": "exit 0"}}}})
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        workflow.cmd_go(self.root)
        with open(self.run_path("steps.json"), "w") as f:
            json.dump(steps(), f)
        with open(self.run_path("plan.md"), "w") as f:
            f.write("plan")
        workflow.handle_stop(self.root, {})
        workflow.cmd_go(self.root)

    def claim(self, session=OWNER, command="/sl-go"):
        return workflow.handle_context(self.root, "UserPromptSubmit", {"session_id": session, "prompt": command})

    def stop(self, session=OTHER, **fields):
        return workflow.handle_stop(self.root, dict(fields, session_id=session))

    def pretool(self, session, tool, tool_input, **fields):
        hook_input = dict(fields, session_id=session, tool_name=tool, tool_input=tool_input)
        out = workflow.handle_pretool(self.root, hook_input)
        return out["hookSpecificOutput"]["permissionDecision"] if out else None

    def main(self, *argv, stdin=""):
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

    def stalled(self):
        self.set_config({"maxIterations": 1})
        self.start()
        workflow.handle_stop(self.root, {})
        self.assertEqual("stalled", self.state()["status"])


class OwnerTest(ProjectTest):

    def test_go_claims_for_ready_active_paused_and_stalled(self):
        workflow.cmd_init(self.root, "spec.md")
        self.claim()
        self.assertEqual(OWNER, self.state()["owner"]["session"])
        self.assertEqual("ready", self.state()["status"])

        workflow.cmd_go(self.root)
        self.claim(OTHER)
        self.assertEqual(OTHER, self.state()["owner"]["session"])
        workflow.cmd_pause(self.root)
        self.claim(OWNER)
        self.assertEqual(OWNER, self.state()["owner"]["session"])

    def test_go_claims_when_stalled(self):
        self.stalled()
        self.claim()
        self.assertEqual(OWNER, self.state()["owner"]["session"])

    def test_go_does_not_claim_when_done_or_without_state(self):
        self.assertIsNone(self.claim())
        self.assertIsNone(self.state())
        self.start()
        for _ in range(3):
            workflow.cmd_advance(self.root)
        self.assertEqual("done", self.state()["status"])
        self.claim()
        self.assertNotIn("owner", self.state())

    def test_only_the_first_token_claims(self):
        self.start()
        for prompt in ("please run /sl-go later", "/sl-gone", "sl-go", "", None):
            self.claim(command=prompt)
            self.assertNotIn("owner", self.state())
        self.claim(command="  /sl-go  now")
        self.assertEqual(OWNER, self.state()["owner"]["session"])

    def test_claim_uses_the_command_prefix(self):
        self.set_config({"commandPrefix": "x-"})
        self.start()
        self.claim(command="/sl-go")
        self.assertNotIn("owner", self.state())
        self.claim(command="/x-go")
        self.assertEqual(OWNER, self.state()["owner"]["session"])

    def test_no_session_id_does_not_claim(self):
        self.start()
        workflow.handle_context(self.root, "UserPromptSubmit", {"prompt": "/sl-go"})
        self.assertNotIn("owner", self.state())

    def test_cmd_go_never_changes_owner(self):
        self.start()
        self.claim()
        before = self.state()["owner"]
        workflow.cmd_pause(self.root)
        workflow.cmd_go(self.root)
        self.assertEqual(before, self.state()["owner"])

    def test_owner_typing_go_again_writes_nothing(self):
        self.start()
        self.claim()
        before = self.state_bytes()
        self.claim()
        self.assertEqual(before, self.state_bytes())

    def test_first_claim_adds_no_takeover_line(self):
        workflow.cmd_init(self.root, "spec.md")
        self.claim()
        out = workflow.cmd_go(self.root)
        self.assertTrue(out.startswith("[specloop] phase=plan"), out[:80])
        self.assertEqual(0, self.state().get("attempt", 0))

    def test_takeover_reports_once_and_opens_a_new_attempt(self):
        self.start()
        self.claim()
        attempt = self.state()["attempt"]
        self.claim(OTHER)
        self.assertEqual(OWNER, self.state()["ownerChangedFrom"])
        out = workflow.cmd_go(self.root)
        self.assertTrue(out.startswith("[specloop] This chat now drives the workflow."), out[:80])
        self.assertIn("subagent", out.splitlines()[0])
        self.assertIn("phase=plan", out.splitlines()[1])
        state = self.state()
        self.assertNotIn("ownerChangedFrom", state)
        self.assertEqual(attempt + 1, state["attempt"])
        self.assertTrue(os.path.isdir(workflow.attempt_path(self.root, state)))
        self.assertTrue(workflow.cmd_go(self.root).startswith("[specloop] phase=plan"))

    def test_owner_stays_across_pause_stall_and_done(self):
        self.set_config({"maxIterations": 1})
        self.start()
        self.claim()
        workflow.cmd_pause(self.root)
        self.assertIn("owner", self.state())
        workflow.cmd_go(self.root)
        self.stop(OWNER)
        self.assertEqual("stalled", self.state()["status"])
        self.assertIn("owner", self.state())
        for _ in range(3):
            workflow.cmd_advance(self.root)
        self.assertEqual("done", self.state()["status"])
        self.assertIn("owner", self.state())

    def test_reset_and_a_new_spec_remove_the_owner(self):
        self.start()
        self.claim()
        for _ in range(3):
            workflow.cmd_advance(self.root)
        workflow.cmd_init(self.root, "spec.md")
        self.assertNotIn("owner", self.state())
        self.claim()
        workflow.cmd_reset(self.root)
        self.assertIsNone(self.state())

    def test_delegated_context_tells_the_chat_to_answer_other_questions_first(self):
        self.set_config({"phases": {"plan": {"delegate": True}}})
        self.start()
        self.claim()
        self.stop(OWNER)
        text = self.claim()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("answer that first", text)


class OtherChatTest(ProjectTest):

    def setUp(self):
        super().setUp()
        self.marker = os.path.join(self.root, "check-ran")

    def run_in_implement(self):
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": "echo x >> check-ran"}}}})
        self.start()
        workflow.cmd_advance(self.root)
        self.claim()

    def test_stop_returns_nothing_and_touches_nothing(self):
        self.run_in_implement()
        before = self.state_bytes()
        self.assertIsNone(self.stop(OTHER))
        self.assertEqual(before, self.state_bytes())
        self.assertFalse(os.path.exists(self.marker))
        self.assertFalse(os.path.exists(self.run_path("last-failure.md")))
        self.assertEqual("block", self.stop(OWNER)["decision"])
        self.assertTrue(os.path.exists(self.marker))

    def test_session_start_is_one_line_without_phase_text(self):
        self.start_feature()
        self.claim()
        out = workflow.handle_context(self.root, "SessionStart", {"session_id": OTHER})
        expected = ("[specloop] another chat owns the workflow on %s. Run /sl-go here to take it over."
                    % self.state()["branch"])
        self.assertEqual(expected, out["systemMessage"])
        self.assertEqual(expected, out["hookSpecificOutput"]["additionalContext"])

    def test_owner_session_start_keeps_the_phase_text(self):
        self.start()
        self.claim()
        text = workflow.handle_context(self.root, "SessionStart", {"session_id": OWNER})
        self.assertIn("## Phase: plan", text["hookSpecificOutput"]["additionalContext"])

    def test_prompt_adds_no_context(self):
        self.start()
        self.claim()
        self.assertIsNone(workflow.handle_context(
            self.root, "UserPromptSubmit", {"session_id": OTHER, "prompt": "write a spec"}))

    def test_paused_workflow_shows_other_chats_nothing(self):
        self.start()
        self.claim()
        workflow.cmd_pause(self.root)
        self.assertIsNone(workflow.handle_context(self.root, "SessionStart", {"session_id": OTHER}))

    def test_edit_rules(self):
        self.start()  # phase plan: allowEdits is limited to run/
        self.claim()
        source = os.path.join(self.root, "src", "x.py")
        self.assertEqual("deny", self.pretool(OWNER, "Edit", {"file_path": source}))
        self.assertIsNone(self.pretool(OTHER, "Edit", {"file_path": source}))
        self.assertEqual("deny", self.pretool(OTHER, "Write", {"file_path": os.path.join(self.base, "state.json")}))
        self.assertEqual("deny", self.pretool(OTHER, "Edit", {"file_path": os.path.join(self.base, "engine.py")}))
        review = self.run_path("review.md")
        self.assertEqual("deny", self.pretool(OTHER, "Write", {"file_path": review}))
        self.assertIsNone(self.pretool(OTHER, "Write", {"file_path": review}, agent_id="a1"))
        self.assertIsNone(self.pretool(OWNER, "Write", {"file_path": review}))

    def test_enforce_delegation_is_not_applied(self):
        self.set_config({"feature": {"enforceDelegation": True}, "phases": {"plan": {"delegate": True, "allowEdits": None}}})
        self.start()
        self.claim()
        source = os.path.join(self.root, "src", "x.py")
        self.assertEqual("deny", self.pretool(OWNER, "Edit", {"file_path": source}))
        self.assertIsNone(self.pretool(OTHER, "Edit", {"file_path": source}))

    def test_bash_rules(self):
        self.start_feature()
        self.claim()
        self.assertEqual("deny", self.pretool(OTHER, "Bash", {"command": "git checkout main"}))
        self.assertEqual("deny", self.pretool(OTHER, "Bash", {"command": "git stash"}))
        self.assertIsNone(self.pretool(OTHER, "Bash", {"command": "gh pr create"}))
        self.assertEqual("deny", self.pretool(OWNER, "Bash", {"command": "gh pr create"}))

    def test_advance_and_reset_are_blocked_while_active(self):
        self.start()
        self.claim()
        for command in ("/sl-advance", "/sl-reset"):
            out = self.claim(OTHER, command)
            self.assertEqual("block", out["decision"])
            self.assertIn("/sl-go", out["reason"])
            self.assertIn("/sl-pause", out["reason"])
            self.assertNotEqual("block", (self.claim(OWNER, command) or {}).get("decision"))

    def test_pause_and_status_are_never_blocked(self):
        self.start()
        self.claim()
        for command in ("/sl-pause", "/sl-status"):
            self.assertIsNone(self.claim(OTHER, command))

    def test_advance_and_reset_pass_while_paused(self):
        self.start()
        self.claim()
        workflow.cmd_pause(self.root)
        for command in ("/sl-advance", "/sl-reset"):
            self.assertIsNone(self.claim(OTHER, command))

    def test_no_owner_or_no_session_behaves_as_before(self):
        self.start()
        source = {"file_path": os.path.join(self.root, "src", "x.py")}
        self.assertEqual("deny", self.pretool(OTHER, "Edit", source))  # no owner
        self.assertIn("phase=plan", workflow.handle_context(self.root, "SessionStart", {"session_id": OTHER})
                      ["hookSpecificOutput"]["additionalContext"])
        self.assertEqual("block", workflow.handle_stop(self.root, {"session_id": OTHER})["decision"])
        self.claim()
        hook_input = {"tool_name": "Edit", "tool_input": source}  # no session id
        self.assertIsNotNone(workflow.handle_pretool(self.root, hook_input))
        self.assertEqual("block", workflow.handle_stop(self.root, {})["decision"])
        self.assertIsNone(workflow.handle_context(self.root, "UserPromptSubmit", {"session_id": OTHER}))


class InitGuardTest(ProjectTest):

    def assertRefused(self, *needles):
        before = self.state_bytes()
        self.write(os.path.join(".claude", "specloop", "run", "marker"), "keep")
        out = workflow.cmd_init(self.root, "spec.md")
        for needle in needles:
            self.assertIn(needle, out)
        self.assertEqual(before, self.state_bytes())
        self.assertTrue(os.path.exists(self.run_path("marker")))

    def test_refused_while_active(self):
        self.start()
        self.claim()
        self.assertRefused("/sl-pause", "/sl-reset")

    def test_refused_while_paused(self):
        self.start()
        workflow.cmd_pause(self.root)
        self.assertRefused("/sl-reset")

    def test_refused_while_stalled(self):
        self.stalled()
        self.assertRefused("/sl-reset")

    def test_feature_mode_is_refused_too(self):
        self.start()
        before = self.state_bytes()
        self.assertIn("/sl-reset", workflow.cmd_init(self.root, "spec.md", mode="feature"))
        self.assertEqual(before, self.state_bytes())

    def test_works_on_ready_done_and_no_state(self):
        self.assertIn("Spec loaded", workflow.cmd_init(self.root, "spec.md"))
        self.assertIn("Spec loaded", workflow.cmd_init(self.root, "spec.md"))
        workflow.cmd_go(self.root)
        for _ in range(3):
            workflow.cmd_advance(self.root)
        self.assertIn("Spec loaded", workflow.cmd_init(self.root, "spec.md"))


class AtomicWriteTest(ProjectTest):

    def path(self):
        return os.path.join(self.root, "file.json")

    def test_failed_replace_keeps_the_original_and_removes_the_temporary_file(self):
        self.write("file.json", "original")
        with mock.patch("os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                workflow.atomic_write(self.path(), "new")
        with open(self.path()) as f:
            self.assertEqual("original", f.read())
        self.assertEqual([], [n for n in os.listdir(self.root) if n.endswith(".tmp")])

    def test_permission_error_is_retried(self):
        real, calls = os.replace, []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) <= 2:
                raise PermissionError("in use")
            real(src, dst)

        with mock.patch("os.replace", side_effect=flaky):
            workflow.atomic_write(self.path(), "new")
        self.assertEqual(3, len(calls))
        with open(self.path()) as f:
            self.assertEqual("new", f.read())

    def test_a_lasting_permission_error_gives_up(self):
        self.write("file.json", "original")
        with mock.patch("os.replace", side_effect=PermissionError("in use")), \
                mock.patch.object(workflow, "FILE_RETRY_SEC", 0.1):
            with self.assertRaises(PermissionError):
                workflow.atomic_write(self.path(), "new")
        self.assertEqual([], [n for n in os.listdir(self.root) if n.endswith(".tmp")])

    def test_save_state_and_save_steps_keep_their_formatting(self):
        state = {"a": 1, "approvedSteps": {"steps": []}}
        os.makedirs(self.run_path())
        workflow.save_state(self.root, state)
        workflow.save_steps(self.root, state)
        with open(os.path.join(self.base, "state.json")) as f:
            self.assertEqual(json.dumps(state, indent=2), f.read())
        with open(self.run_path("steps.json")) as f:
            self.assertEqual(json.dumps(state["approvedSteps"], indent=2) + "\n", f.read())

    def test_reader_never_sees_invalid_json_while_state_is_saved(self):
        workflow.save_state(self.root, {"status": "ready", "padding": "x" * 200000})
        writer = subprocess.Popen([sys.executable, "-c", (
            "import sys, time\n"
            "sys.path.insert(0, %r)\n"
            "import engine\n"
            "end = time.monotonic() + 1.5\n"
            "n = 0\n"
            "while time.monotonic() < end:\n"
            "    n += 1\n"
            "    engine.save_state(%r, {'status': 'ready', 'n': n, 'padding': 'x' * 200000})\n" % (KIT, self.root))])
        reads = 0
        try:
            while writer.poll() is None:
                try:
                    with open(os.path.join(self.base, "state.json"), encoding="utf-8") as f:
                        json.load(f)
                    reads += 1
                except PermissionError:
                    pass  # os.replace is in progress on Windows; load_state retries this
                time.sleep(0.001)  # a spinning reader would keep os.replace out on Windows
        finally:
            writer.wait()
        self.assertEqual(0, writer.returncode)
        self.assertGreater(reads, 0)

    def test_eight_concurrent_pauses_raise_rev_by_eight(self):
        self.start()
        rev = self.state()["rev"]
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.root)
        procs = [subprocess.Popen([sys.executable, ENGINE, "pause"], env=env, stdout=subprocess.PIPE)
                 for _ in range(8)]
        outputs = [p.communicate()[0].decode() for p in procs]
        self.assertEqual([0] * 8, [p.returncode for p in procs])
        self.assertTrue(all("paused" in o for o in outputs), outputs)
        self.assertEqual(rev + 8, self.state()["rev"])


class CorruptStateTest(ProjectTest):

    def truncate(self):
        with open(os.path.join(self.base, "state.json"), "w") as f:
            f.write('{"status": "act')

    def assertUnreadableMessage(self, text):
        self.assertIn("state.json is unreadable", text)
        self.assertIn("/sl-reset", text)

    def test_hooks_report_it_and_pretool_stays_silent(self):
        self.start()
        self.truncate()
        self.assertUnreadableMessage(json.loads(self.main("stop", stdin="{}"))["systemMessage"])
        for event in ("SessionStart", "UserPromptSubmit"):
            self.assertUnreadableMessage(json.loads(self.main("context", event, stdin="{}"))["systemMessage"])
        self.assertEqual("", self.main("pretool", stdin="{}"))

    def test_manual_commands_print_the_message_and_reset_removes_it(self):
        self.start()
        self.truncate()
        for command in ("pause", "go", "status", "advance"):
            self.assertUnreadableMessage(self.main(command))
        self.main("reset")
        self.assertFalse(os.path.exists(os.path.join(self.base, "state.json")))

    def test_a_json_value_that_is_not_an_object_counts_as_corrupt(self):
        with open(os.path.join(self.base, "state.json"), "w") as f:
            f.write("[]")
        with self.assertRaises(workflow.StateCorrupt):
            workflow.load_state(self.root)

    def test_a_short_gap_is_retried(self):
        self.start()
        good = self.state_bytes()
        self.truncate()

        def repair():
            time.sleep(0.05)
            with open(os.path.join(self.base, "state.json"), "wb") as f:
                f.write(good)

        thread = threading.Thread(target=repair)
        thread.start()
        try:
            self.assertEqual("active", self.state()["status"])
        finally:
            thread.join()


class LockTest(ProjectTest):

    def lock_path(self):
        return os.path.join(self.base, "state.lock")

    def fresh_lock(self):
        lock = workflow.FileLock(self.lock_path(), 30)
        lock.__enter__()
        self.addCleanup(lock.release)
        return lock

    def stale_lock(self, path=None):
        with open(path or self.lock_path(), "w") as f:
            json.dump({"token": "old", "at": time.time() - 3600}, f)

    def short_wait(self):
        patcher = mock.patch.object(workflow, "LOCK_WAIT_SEC", 0.1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_fresh_lock_makes_pause_report_busy_and_change_nothing(self):
        self.start()
        before = self.state_bytes()
        self.short_wait()
        self.fresh_lock()
        self.assertIn("busy", self.main("pause"))
        self.assertEqual(before, self.state_bytes())

    def test_a_fresh_lock_blocks_a_claim(self):
        self.start()
        before = self.state_bytes()
        self.short_wait()
        self.fresh_lock()
        out = self.claim()
        self.assertEqual("block", out["decision"])
        self.assertIn("/sl-go", out["reason"])
        self.assertEqual(before, self.state_bytes())

    def test_a_hook_that_cannot_lock_writes_nothing_and_exits_zero(self):
        self.start()
        before = self.state_bytes()
        self.short_wait()
        self.fresh_lock()
        self.assertEqual("", self.main("stop", stdin="{}"))
        self.assertEqual(before, self.state_bytes())

    def test_reset_waits_for_the_lock_too(self):
        self.start()
        self.short_wait()
        self.fresh_lock()
        self.assertIn("busy", self.main("reset"))
        self.assertIsNotNone(self.state())

    def test_a_stale_lock_is_replaced_and_released(self):
        self.start()
        self.stale_lock()
        self.assertIn("paused", workflow.cmd_pause(self.root))
        self.assertFalse(os.path.exists(self.lock_path()))
        self.assertEqual([], [n for n in os.listdir(self.base) if ".stale" in n])

    def test_exactly_one_of_two_processes_replaces_a_stale_lock(self):
        self.stale_lock()
        barrier = os.path.join(self.root, "go")
        code = (
            "import os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "import engine\n"
            "engine.LOCK_WAIT_SEC = 0.5\n"
            "while not os.path.exists(%r):\n"
            "    time.sleep(0.01)\n"
            "try:\n"
            "    with engine.FileLock(%r, 30):\n"
            "        print('got', flush=True)\n"
            "        time.sleep(2)\n"
            "except engine.LockBusy:\n"
            "    print('busy', flush=True)\n" % (KIT, barrier, self.lock_path()))
        procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE) for _ in range(2)]
        time.sleep(0.5)
        self.write("go", "")
        outputs = sorted(p.communicate()[0].decode().strip() for p in procs)
        self.assertEqual(["busy", "got"], outputs)

    def test_release_keeps_another_process_lock(self):
        lock = workflow.FileLock(self.lock_path(), 30)
        lock.__enter__()
        with open(self.lock_path(), "w") as f:
            json.dump({"token": "someone-else", "at": time.time()}, f)
        lock.release()
        self.assertTrue(os.path.exists(self.lock_path()))
        os.remove(self.lock_path())

    def test_a_newer_lock_moved_aside_by_mistake_is_given_back(self):
        first = workflow.FileLock(self.lock_path(), 30)
        first.__enter__()
        racer = workflow.FileLock(self.lock_path(), 30)
        with mock.patch.object(racer, "_is_stale", side_effect=[True, False]):
            racer._remove_if_stale()
        with open(self.lock_path()) as f:
            self.assertEqual(first.token, json.load(f)["token"])
        self.assertEqual([], [n for n in os.listdir(self.base) if ".stale" in n])
        first.release()


class StopOrderTest(ProjectTest):

    def engine_call(self, command):
        return "'%s' '%s' %s" % (sys.executable.replace("\\", "/"), ENGINE.replace("\\", "/"), command)

    def implement_with_check(self, command):
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": command}}}})
        self.start()
        workflow.cmd_advance(self.root)

    def test_a_pause_during_a_check_survives_and_the_result_is_dropped(self):
        self.implement_with_check(self.engine_call("pause") + " > pause-output; exit 1")
        state = self.state()
        self.assertIsNone(workflow.handle_stop(self.root, {}))
        after = self.state()
        self.assertEqual("paused", after["status"])
        self.assertEqual(state.get("iter"), after.get("iter"))
        self.assertEqual(state["history"], after["history"])
        self.assertNotIn("gate", after)
        self.assertFalse(os.path.exists(self.run_path("last-failure.md")))
        with open(os.path.join(self.root, "pause-output")) as f:
            self.assertIn("paused", f.read())  # the lock was free while the check ran

    def test_a_takeover_during_a_check_drops_the_result(self):
        self.implement_with_check("sleep 1; exit 1")
        self.claim()
        thread = threading.Thread(target=lambda: time.sleep(0.3) or (self.claim(OTHER), workflow.cmd_go(self.root)))
        thread.start()
        try:
            self.assertIsNone(self.stop(OWNER))
        finally:
            thread.join()
        self.assertEqual(0, self.state().get("iter", 0))
        self.assertEqual(OTHER, self.state()["owner"]["session"])

    def test_a_failed_check_is_recorded_when_nothing_changed(self):
        self.implement_with_check("exit 1")
        self.assertEqual("block", workflow.handle_stop(self.root, {})["decision"])
        self.assertEqual(1, self.state()["iter"])
        self.assertNotIn("gate", self.state())

    def test_two_concurrent_stops_run_one_check(self):
        self.implement_with_check("echo x >> ran; sleep 1; exit 1")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.root)
        procs = [subprocess.Popen([sys.executable, ENGINE, "stop"], env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE) for _ in range(2)]
        for p in procs:
            p.stdin.write(b"{}")
            p.stdin.close()
        outputs = [p.stdout.read().decode() for p in procs]
        for p in procs:
            p.stdout.close()
            p.wait()
        self.assertEqual([0, 0], [p.returncode for p in procs])
        with open(os.path.join(self.root, "ran")) as f:
            self.assertEqual(1, len(f.read().split()))
        self.assertEqual(1, sum(1 for o in outputs if o.strip()))

    def test_an_old_gate_does_not_block_the_check(self):
        self.implement_with_check("exit 1")
        stamp = "2000-01-01T00:00:00+00:00"
        workflow.update_state(self.root, lambda s: s.update(gate={"session": "x", "at": stamp}))
        self.assertEqual("block", workflow.handle_stop(self.root, {})["decision"])

    def test_a_young_gate_makes_stop_return_nothing(self):
        self.implement_with_check("exit 1")
        workflow.update_state(self.root, lambda s: s.update(gate={"session": "x", "at": workflow.now()}))
        before = self.state_bytes()
        self.assertIsNone(workflow.handle_stop(self.root, {}))
        self.assertEqual(before, self.state_bytes())

    def test_a_crash_in_the_gate_clears_it(self):
        self.implement_with_check("exit 1")
        with mock.patch.object(workflow, "check_phase", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                workflow.handle_stop(self.root, {})
        self.assertNotIn("gate", self.state())

    def test_the_lock_is_free_during_a_check(self):
        self.implement_with_check("exit 0")
        seen = []

        def spy(*args):
            seen.append(os.path.exists(os.path.join(self.base, "state.lock")))
            return "pass", "ok", ""

        with mock.patch.object(workflow, "check_phase", side_effect=spy):
            workflow.handle_stop(self.root, {})
        self.assertEqual([False], seen)


class FeatureStopTest(ProjectTest):

    def pre_commit(self, script):
        hook = os.path.join(self.root, ".git", "hooks", "pre-commit")
        with open(hook, "w", newline="\n") as f:
            f.write("#!/bin/sh\n" + script + "\n")
        os.chmod(hook, 0o755)

    def to_review(self):
        self.write("a.py", "a = 1\n")
        workflow.handle_stop(self.root, {})
        self.assertEqual("review", self.state()["phase"])

    def test_a_pause_during_a_commit_survives(self):
        self.start_feature()
        self.to_review()
        self.pre_commit("'%s' '%s' pause" % (sys.executable.replace("\\", "/"), ENGINE.replace("\\", "/")))
        self.write_attempt("review.md", "VERDICT: APPROVE")
        out = workflow.handle_stop(self.root, {})
        self.assertNotIn("decision", out)
        self.assertIn("/sl-go", out["systemMessage"])
        state = self.state()
        self.assertEqual("paused", state["status"])
        self.assertEqual(2, state["stepId"])
        self.assertEqual("done", state["approvedSteps"]["steps"][0]["status"])
        self.assertEqual("active", state["approvedSteps"]["steps"][1]["status"])
        self.assertNotIn("gate", state)
        self.assertEqual("1", self.git("rev-list", "--count", state["baseRef"] + "..HEAD"))
        self.assertIn("a.py", self.git("show", "--name-only", "--format=", "HEAD"))
        self.assertIsNone(workflow.handle_stop(self.root, {}))

    def test_a_failed_commit_keeps_a_pause_that_arrived_meanwhile(self):
        self.start_feature()
        self.to_review()
        self.pre_commit("'%s' '%s' pause\nexit 1" % (sys.executable.replace("\\", "/"), ENGINE.replace("\\", "/")))
        self.write_attempt("review.md", "VERDICT: APPROVE")
        out = workflow.handle_stop(self.root, {})
        self.assertNotIn("decision", out)
        self.assertEqual(("paused", "implement", 1), tuple(self.state()[k] for k in ("status", "phase", "stepId")))

    def test_the_commit_runs_without_the_lock(self):
        self.start_feature()
        self.to_review()
        self.pre_commit("test ! -e .claude/specloop/state.lock")
        self.write_attempt("review.md", "VERDICT: APPROVE")
        workflow.handle_stop(self.root, {})
        self.assertEqual("done", self.state()["approvedSteps"]["steps"][0]["status"])

    def test_a_takeover_during_a_commit_gives_the_next_brief_to_nobody(self):
        self.start_feature()
        self.claim()
        self.to_review_as_owner()
        self.pre_commit("exit 0")
        self.write_attempt("review.md", "VERDICT: APPROVE")
        real = workflow.commit_step

        def commit_then_takeover(*args):
            result = real(*args)
            self.claim(OTHER)
            return result

        with mock.patch.object(workflow, "commit_step", side_effect=commit_then_takeover):
            out = self.stop(OWNER)
        self.assertNotIn("decision", out)
        self.assertEqual(2, self.state()["stepId"])

    def to_review_as_owner(self):
        self.write("a.py", "a = 1\n")
        self.stop(OWNER)
        self.assertEqual("review", self.state()["phase"])

    def test_a_replan_request_follows_the_same_rules(self):
        self.start_feature()
        self.claim()
        self.write_attempt("replan.md", "wrong plan")
        before = self.state_bytes()
        self.assertIsNone(self.stop(OTHER))
        self.assertEqual(before, self.state_bytes())
        self.assertIn("replan", self.stop(OWNER)["systemMessage"])
        self.assertEqual("stalled", self.state()["status"])
        self.assertEqual("replan", self.state()["history"][-1]["result"])
        self.assertNotIn("gate", self.state())

    def test_a_late_replan_request_does_not_affect_a_later_step(self):
        self.start_feature()
        late = self.attempt_file("replan.md")
        self.to_review()
        self.write_attempt("review.md", "VERDICT: APPROVE")
        workflow.handle_stop(self.root, {})
        self.assertEqual(2, self.state()["stepId"])
        with open(late, "w") as f:
            f.write("too late")
        self.assertIn("entering phase 'review'", workflow.handle_stop(self.root, {})["reason"])
        self.assertEqual("active", self.state()["status"])

    def test_a_replan_request_is_found_in_the_replan_phase_brief(self):
        self.start_feature({"feature": {"replan": "auto"},
                            "phases": {"implement": {"check": {"type": "command", "command": "exit 0"}}}})
        self.write_attempt("replan.md", "wrong plan")
        out = workflow.handle_stop(self.root, {})
        self.assertIn(os.path.basename(workflow.attempt_path(self.root, self.state())) + "/replan.md", out["reason"])
        self.assertEqual(self.state()["phase"], "feature-replan")


class AttemptTest(ProjectTest):

    def to_review(self):
        self.start()
        workflow.cmd_advance(self.root)
        workflow.cmd_advance(self.root)
        self.assertEqual("review", self.state()["phase"])

    def test_entering_a_verdict_phase_opens_a_new_attempt_and_its_directory(self):
        self.start()
        self.assertEqual(0, self.state()["attempt"])
        workflow.cmd_advance(self.root)
        self.assertEqual(0, self.state()["attempt"])
        workflow.cmd_advance(self.root)
        self.assertEqual(1, self.state()["attempt"])
        self.assertTrue(os.path.isdir(self.run_path("attempts", "single-1")))

    def test_a_failed_check_and_a_pause_keep_the_attempt(self):
        self.to_review()
        workflow.handle_stop(self.root, {})
        workflow.cmd_pause(self.root)
        workflow.cmd_go(self.root)
        self.assertEqual(1, self.state()["attempt"])

    def test_the_review_stays_in_a_rejected_attempt_and_the_next_review_is_a_new_one(self):
        self.to_review()
        self.write_attempt("review.md", "Fix it.\nVERDICT: CHANGES_REQUESTED")
        out = workflow.handle_stop(self.root, {})
        self.assertIn("Fix it", out["reason"])
        self.assertEqual("implement", self.state()["phase"])
        self.assertTrue(os.path.exists(self.run_path("attempts", "single-1", "review.md")))
        workflow.handle_stop(self.root, {})
        self.assertEqual(("review", 2), (self.state()["phase"], self.state()["attempt"]))
        self.assertIn("is missing", workflow.handle_stop(self.root, {})["reason"])

    def test_rendered_paths_use_the_attempt(self):
        self.start()
        workflow.cmd_advance(self.root)
        text = workflow.cmd_advance(self.root)
        self.assertEqual("Advanced to phase 'review'.", text)
        prompt = workflow.phase_instructions(self.root, workflow.load_config(self.root), self.state())
        self.assertIn(".claude/specloop/run/attempts/single-1/review.md", prompt)
        self.assertNotIn("{attempt_dir}", prompt)

    def test_a_leftover_review_at_the_old_path_does_not_count(self):
        self.to_review()
        self.write(os.path.join(".claude", "specloop", "run", "review.md"), "VERDICT: APPROVE")
        self.assertEqual("block", workflow.handle_stop(self.root, {})["decision"])
        self.assertEqual("review", self.state()["phase"])

    def test_a_verdict_in_a_closed_attempt_has_no_effect_and_status_lists_it(self):
        self.to_review()
        closed = self.attempt_file("review.md")
        self.claim(OWNER)
        self.claim(OTHER)
        workflow.cmd_go(self.root)  # the takeover closes attempt 1
        self.assertEqual(2, self.state()["attempt"])
        with open(closed, "w") as f:
            f.write("VERDICT: APPROVE")
        self.assertEqual("block", self.stop(OTHER)["decision"])
        self.assertEqual("review", self.state()["phase"])
        self.assertIn("late review (attempt 1): APPROVE, .claude/specloop/run/attempts/single-1/review.md",
                      workflow.cmd_status(self.root))

    def test_a_rewritten_review_after_the_approval_is_listed_as_late(self):
        self.init_git()
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": "exit 0"}}}})
        workflow.cmd_init(self.root, "spec.md", mode="feature")
        workflow.cmd_go(self.root)
        with open(self.run_path("steps.json"), "w") as f:
            json.dump(steps(), f)
        workflow.handle_stop(self.root, {})
        workflow.cmd_go(self.root)
        self.write("a.py", "a = 1\n")
        workflow.handle_stop(self.root, {})
        approved = self.attempt_file("review.md")
        self.write_attempt("review.md", "VERDICT: APPROVE")
        workflow.handle_stop(self.root, {})
        self.assertEqual(2, self.state()["stepId"])
        self.assertNotIn("late review", workflow.cmd_status(self.root))
        with open(approved, "w") as f:
            f.write("Too late.\nVERDICT: CHANGES_REQUESTED")
        self.assertIn("late review for step 1 (attempt 1): CHANGES_REQUESTED, ", workflow.cmd_status(self.root))
        self.assertEqual(2, self.state()["stepId"])

    def test_an_open_attempt_is_not_late(self):
        self.to_review()
        self.write_attempt("review.md", "VERDICT: APPROVE")
        self.assertNotIn("late review", workflow.cmd_status(self.root))

    def fixed_path(self):
        self.set_config({"phases": {"review": {"check": {
            "type": "verdict", "path": ".claude/specloop/run/custom.md", "approve": "VERDICT: APPROVE",
            "reject": "VERDICT: CHANGES_REQUESTED", "onReject": "implement"}}}})

    def test_a_fixed_check_path_keeps_working(self):
        self.fixed_path()
        self.to_review()
        self.write(os.path.join(".claude", "specloop", "run", "custom.md"), "VERDICT: APPROVE")
        workflow.handle_stop(self.root, {})
        self.assertEqual("done", self.state()["status"])

    def test_a_fixed_check_path_is_archived_on_entering_the_phase(self):
        self.fixed_path()
        self.start()
        workflow.cmd_advance(self.root)
        custom = os.path.join(".claude", "specloop", "run", "custom.md")
        self.write(custom, "VERDICT: APPROVE")
        workflow.cmd_advance(self.root)
        self.assertFalse(os.path.exists(os.path.join(self.root, custom)))
        self.assertEqual("block", workflow.handle_stop(self.root, {})["decision"])
        self.assertIn("late review (attempt 1): APPROVE", workflow.cmd_status(self.root))

    def test_a_rejected_review_at_a_fixed_path_does_not_count_again(self):
        self.fixed_path()
        self.to_review()
        custom = os.path.join(".claude", "specloop", "run", "custom.md")
        self.write(custom, "VERDICT: CHANGES_REQUESTED")
        workflow.handle_stop(self.root, {})
        self.assertEqual("implement", self.state()["phase"])
        self.assertFalse(os.path.exists(os.path.join(self.root, custom)))
        self.assertTrue(any(n.startswith("custom-") for n in os.listdir(self.run_path())))

    def test_go_creates_the_directory_for_a_run_paused_across_the_upgrade(self):
        self.to_review()
        workflow.cmd_pause(self.root)
        state = self.state()
        del state["attempt"], state["rev"]
        workflow.save_state(self.root, state)
        shutil.rmtree(self.run_path("attempts"))
        out = workflow.cmd_go(self.root)
        self.assertIn(".claude/specloop/run/attempts/single-0/review.md", out)
        self.assertTrue(os.path.isdir(self.run_path("attempts", "single-0")))
        self.assertEqual(1, self.state()["rev"])
        self.write_attempt("review.md", "VERDICT: APPROVE")
        workflow.handle_stop(self.root, {})
        self.assertEqual("done", self.state()["status"])

    def test_a_replan_requested_before_the_upgrade_is_still_adopted(self):
        self.start_feature()
        self.write_attempt("replan.md", "wrong plan")
        workflow.handle_stop(self.root, {})
        os.replace(self.attempt_file("replan.md"), self.run_path("replan.md"))
        workflow.cmd_go(self.root)
        self.assertEqual("active", self.state()["status"])
        self.assertTrue(os.path.exists(self.run_path("replans", "replan-1.md")))


class InFlightTest(ProjectTest):

    def test_a_running_task_holds_the_gate_in_any_phase(self):
        self.set_config({"phases": {"implement": {"check": {"type": "command", "command": "echo x >> ran; exit 1"}}}})
        self.start()
        for _ in range(3):
            for kind in ("subagent", "workflow", "teammate"):
                before = self.state_bytes()
                self.assertIsNone(self.stop(background_tasks=[{"id": "1", "type": "shell"}, {"id": "2", "type": kind}]))
                self.assertEqual(before, self.state_bytes())
            workflow.cmd_advance(self.root)
        self.assertFalse(os.path.exists(os.path.join(self.root, "ran")))
        self.assertFalse(os.path.exists(self.run_path("last-failure.md")))

    def test_other_tasks_and_a_missing_field_run_the_check(self):
        self.start()
        for fields in ({"background_tasks": [{"type": "shell"}]}, {"background_tasks": [{"type": "monitor"}]},
                       {"background_tasks": []}, {}, {"background_tasks": "subagent"},
                       {"background_tasks": ["subagent"]}, {"background_tasks": None}):
            iterations = self.state().get("iter", 0)
            self.assertEqual("block", self.stop(**fields)["decision"], fields)
            self.assertEqual(iterations + 1, self.state()["iter"])

    def test_delegate_preamble_says_to_wait(self):
        self.set_config({"phases": {"plan": {"delegate": True}}})
        self.assertIn("Never start a second one", self.start())


class AttributionTest(ProjectTest):

    def test_history_names_the_session_for_gates_and_claims(self):
        self.start()
        self.claim()
        self.claim(OTHER)
        self.assertEqual((("claimed", OWNER[:8]), ("takeover", OTHER[:8])), tuple(
            (h["result"], h["session"]) for h in self.state()["history"]))
        self.stop(OTHER)
        last = self.state()["history"][-1]
        self.assertEqual(("fail", OTHER[:8]), (last["result"], last["session"]))

    def test_manual_entries_have_no_session(self):
        self.start()
        self.claim()
        workflow.cmd_advance(self.root)
        self.assertNotIn("session", self.state()["history"][-1])

    def test_commit_entries_name_the_session(self):
        self.start_feature()
        self.claim()
        self.write("a.py", "a = 1\n")
        self.stop(OWNER)
        self.write_attempt("review.md", "VERDICT: APPROVE")
        self.stop(OWNER)
        entries = [h for h in self.state()["history"] if h.get("result") == "committed"]
        self.assertEqual(OWNER[:8], entries[0]["session"])

    def test_feature_status_shows_the_owner(self):
        self.start_feature()
        self.assertIn("owner: none (every chat drives this workflow)", workflow.cmd_status(self.root))
        self.claim()
        self.assertIn("owner: %s (since %s)" % (OWNER[:8], self.state()["owner"]["since"]),
                      workflow.cmd_status(self.root))

    def test_single_mode_status_contains_the_owner(self):
        self.start()
        self.claim()
        self.assertEqual(OWNER, json.loads(workflow.cmd_status(self.root))["owner"]["session"])


if __name__ == "__main__":
    unittest.main()
