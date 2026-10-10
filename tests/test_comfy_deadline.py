"""Real child-process tests; no GPU, network or provider billing."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import comfy_deadline as guard


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Process condition did not become true")


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "guard"
        child_file = Path(self.temp.name) / "child.json"
        script = ("import subprocess,sys,time,signal,json,os;"
                  "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                  "p=subprocess.Popen([sys.executable,'-c',"
                  "'import os,signal,time;os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)']);"
                  f"f=open({str(child_file)+'.pending'!r},'w');f.write(json.dumps(p.pid));f.close();"
                  f"os.rename({str(child_file)+'.pending'!r},{str(child_file)!r});time.sleep(60)")
        self.guardian = subprocess.Popen([sys.executable, str(Path(guard.__file__)),
            "--root", str(self.root), sys.executable, "-c", script])
        self.addCleanup(self.stop)
        wait_for(lambda: (self.root / "manifest.json").exists() and child_file.exists())
        self.child = json.loads(child_file.read_text())
        wait_for(lambda: guard.identity(self.child)["session"] == self.child)
        self.manifest = guard.ownership(self.root)

    def stop(self):
        if self.guardian.poll() is None:
            self.guardian.terminate()
            self.guardian.wait(timeout=5)

    def assert_stopped(self):
        wait_for(lambda: json.loads((self.root / "manifest.json").read_text())["stopped"])
        for pid in [self.manifest["renderer"], self.child]:
            with self.assertRaises((OSError, guard.GuardError)):
                guard.identity(pid)

    def test_deadline_kills_inference_and_sigterm_ignoring_descendant(self):
        started = time.monotonic()
        guard.arm("one-job", 1, self.root)
        self.assert_stopped()
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(self.guardian.poll())  # SDK can return without crash/retry.
        with self.assertRaises(guard.GuardError):
            guard.arm("second-job", 1, self.root)

    def test_completed_request_disarms_without_stopping_owned_renderer(self):
        lease = guard.arm("completed-job", 1, self.root)
        guard.finish(lease, success=True, root=self.root)
        time.sleep(1.1)
        self.assertFalse((self.root / "active.json").exists())
        guard.ownership(self.root)

    def test_failure_and_ambiguous_attempt_stop_renderer_without_retry(self):
        lease = guard.arm("failed-job", 5, self.root)
        with self.assertRaises(guard.GuardError):
            guard.arm("competing-job", 5, self.root)
        guard.finish(lease, success=False, root=self.root)
        self.assert_stopped()

    def test_shutdown_kills_descendant_even_when_renderer_leader_has_exited(self):
        os.kill(self.manifest["renderer"], signal.SIGKILL)
        self.assert_stopped()

    def test_handler_death_stops_inference_before_its_deadline(self):
        script = ("from pathlib import Path;import os,comfy_deadline;"
                  f"comfy_deadline.arm('dead-handler',5,Path({str(self.root)!r}));os._exit(0)")
        caller = subprocess.Popen([sys.executable, "-c", script])
        caller.wait(timeout=5)
        self.assert_stopped()

    def test_guardian_shutdown_kills_escaped_inference_children(self):
        self.stop()
        for pid in [self.manifest["renderer"], self.child]:
            with self.assertRaises((OSError, guard.GuardError)):
                guard.identity(pid)

    def test_stale_identity_and_unsafe_manifest_fail_closed(self):
        path = self.root / "manifest.json"
        value = dict(self.manifest, renderer_start="not-the-same-process")
        guard.write(path, value)
        with self.assertRaises(guard.GuardError):
            guard.arm("invalid-ownership", 5, self.root)
        guard.write(path, self.manifest)
        path.chmod(0o644)
        with self.assertRaises(guard.GuardError):
            guard.ownership(self.root)
        path.chmod(0o600)

    def test_invalid_deadlines_never_arm_renderer(self):
        for value in [True, 0, -1, 601, 1.5, "5", None]:
            with self.assertRaises(guard.GuardError):
                guard.arm("invalid-deadline", value, self.root)
        self.assertFalse((self.root / "active.json").exists())


if __name__ == "__main__":
    unittest.main()
