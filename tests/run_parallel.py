"""Run the test suite on several worker processes.

    python tests/run_parallel.py [-j N] [-s START] [--durations N]

Runs the same tests as `python -m unittest discover -s START`, one test class at a time, on N
long-lived worker processes (default: the CPU count). Classes are handed out as workers become
free, largest first. Exits 0 when every test passed, 1 otherwise.

A worker reads class names from stdin and answers each with one JSON line on a private copy of
its stdout. Its real stdout is pointed at stderr, so nothing a test or a child process prints can
corrupt the answers.
"""

import argparse
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def class_name(test):
    return "%s.%s" % (type(test).__module__, type(test).__name__)


def iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for test in iter_tests(item):
                yield test
        else:
            yield item


def discover(start):
    """Return ({class name: test count}, suite of tests that failed to load)."""
    counts, broken = {}, unittest.TestSuite()
    for test in iter_tests(unittest.defaultTestLoader.discover(start)):
        if type(test).__module__ == "unittest.loader":  # _FailedTest: a module that failed to import
            broken.addTest(test)
        else:
            counts[class_name(test)] = counts.get(class_name(test), 0) + 1
    return counts, broken


# --- worker --------------------------------------------------------------------------------

def run_class(name):
    stream = io.StringIO()
    started = time.perf_counter()
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(name)
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        return {"name": name, "run": result.testsRun, "failures": len(result.failures),
                "errors": len(result.errors), "skipped": len(result.skipped),
                "ok": result.wasSuccessful(), "output": stream.getvalue(),
                "seconds": time.perf_counter() - started}
    except Exception as e:  # the class could not be loaded
        return {"name": name, "run": 0, "failures": 0, "errors": 1, "skipped": 0, "ok": False,
                "output": "%s: %s: %s\n" % (name, type(e).__name__, e),
                "seconds": time.perf_counter() - started}


def worker(start):
    answers = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.path.insert(0, os.path.abspath(start))
    for line in sys.stdin:
        name = line.strip()
        if name:
            answers.write(json.dumps(run_class(name)) + "\n")
            answers.flush()


# --- parent --------------------------------------------------------------------------------

def crashed(name, why):
    return {"name": name, "run": 0, "failures": 0, "errors": 1, "skipped": 0, "ok": False,
            "output": "%s: %s\n" % (name, why), "seconds": 0.0}


def feed(start, pending, results, lock):
    """Run one worker process, giving it classes from `pending` until none are left or it dies."""
    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker", start],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, universal_newlines=True,
                            encoding="utf-8")
    try:
        while True:
            try:
                name = pending.get_nowait()
            except queue.Empty:
                return
            try:
                proc.stdin.write(name + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                result = json.loads(line) if line else crashed(name, "the worker process exited")
            except (OSError, ValueError) as e:
                result, line = crashed(name, "the worker process failed: %s" % e), ""
            with lock:
                results.append(result)
                print("%-6s %s (%d tests, %.1fs)" % ("ok" if result["ok"] else "FAILED", name,
                                                     result["run"], result["seconds"]), flush=True)
            if not line:
                return
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the test suite on several worker processes.")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                        help="worker processes (default: the CPU count)")
    parser.add_argument("-s", "--start", default=HERE, help="directory to discover tests in")
    parser.add_argument("--durations", type=int, default=0, metavar="N",
                        help="list the N slowest test classes")
    parser.add_argument("--worker", metavar="START", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        worker(args.worker)
        return 0

    started = time.perf_counter()
    counts, broken = discover(args.start)
    if broken.countTestCases():
        unittest.TextTestRunner(verbosity=0).run(broken)
        return 1
    pending = queue.Queue()
    for name in sorted(counts, key=lambda n: (-counts[n], n)):
        pending.put(name)
    jobs = max(1, min(args.jobs, len(counts)))
    results, lock = [], threading.Lock()
    threads = [threading.Thread(target=feed, args=(args.start, pending, results, lock))
               for _ in range(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    while not pending.empty():  # every worker died
        results.append(crashed(pending.get_nowait(), "not run: no worker process was left"))

    for result in results:
        if not result["ok"]:
            print("\n" + result["output"].rstrip())
    if args.durations:
        print("\nSlowest test classes:")
        for result in sorted(results, key=lambda r: -r["seconds"])[:args.durations]:
            print("%8.1fs  %s" % (result["seconds"], result["name"]))
    total = {key: sum(r[key] for r in results) for key in ("run", "failures", "errors", "skipped")}
    print("\nRan %d tests in %.1fs on %d workers" % (total["run"], time.perf_counter() - started, jobs))
    if all(r["ok"] for r in results):
        print("OK" + (" (skipped=%d)" % total["skipped"] if total["skipped"] else ""))
        return 0
    print("FAILED (failures=%d, errors=%d)" % (total["failures"], total["errors"]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
