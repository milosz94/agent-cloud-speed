"""procguard: children spawn as process-group leaders detached from the terminal, and kill_group /
kill_all take down the child AND its descendants. This is what makes Ctrl-C clean (no surviving MCP
servers, no orphaned microVM, no terminal-grab)."""
import subprocess
import time
import unittest

from acspeed import procguard


def _alive(proc) -> bool:
    return proc.poll() is None


class TestProcguard(unittest.TestCase):
    def test_spawn_detaches_stdin(self):
        # stdin is DEVNULL by default so a child cannot capture the controlling terminal
        p = procguard.spawn(["sleep", "5"])
        try:
            self.assertIsNone(p.stdin)
        finally:
            procguard.kill_group(p.pid)

    def test_kill_group_kills_the_child_and_its_children(self):
        # a shell that spawns a grandchild sleep; killing the GROUP must take both down
        p = procguard.spawn(["bash", "-c", "sleep 30 & sleep 30"])
        time.sleep(0.2)
        self.assertTrue(_alive(p))
        procguard.kill_group(p.pid)
        # the group leader dies promptly
        for _ in range(50):
            if not _alive(p):
                break
            time.sleep(0.05)
        self.assertFalse(_alive(p), "child survived kill_group")
        self.assertEqual(procguard.tracked_count(), 0)

    def test_kill_all_signals_every_tracked_group(self):
        ps = [procguard.spawn(["sleep", "30"]) for _ in range(3)]
        self.assertEqual(procguard.tracked_count(), 3)
        n = procguard.kill_all()
        self.assertEqual(n, 3)
        self.assertEqual(procguard.tracked_count(), 0)
        for p in ps:
            for _ in range(50):
                if not _alive(p):
                    break
                time.sleep(0.05)
            self.assertFalse(_alive(p))

    def test_untrack_after_normal_exit(self):
        p = procguard.spawn(["true"])
        p.wait(timeout=5)
        procguard.untrack(p.pid)
        self.assertEqual(procguard.tracked_count(), 0)


if __name__ == "__main__":
    unittest.main()
