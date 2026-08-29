"""The microVM rootfs copy (acspeed-vm-*, ~4 GB) must be reclaimed after each agent turn -- but a FAILED
run must KEEP its small diagnostics (out.json = claude's own error, err.txt, boot.log) so the cause is
knowable, while still dropping the ~4 GB rootfs so the disk cannot fill.

run_vm_job copies BASE_ROOTFS fresh per turn into an acspeed-vm-* dir and never removes it; unbounded
accumulation is what filled the disk and starved a cloud in an n=10 x 4 batch. The first version of the
fix deleted the WHOLE dir on every run, which also deleted the error output and left us blind when runs
started failing. This pins the corrected contract:
  * success (result dict, no is_error) -> whole dir removed
  * failure (no dict, or is_error=True) -> dir KEPT with out.json/boot.log, but rootfs.ext4 removed
  * a work_dir that is not ours (wrong prefix) -> never touched
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import autorun

_SANDBOX = {"keep_claude_tokens": [], "session_store": None}


def _fake_res(work_dir, result):
    return {"result": result, "exit_code": "0", "boot_log": None, "timed_out": False,
            "work_dir": work_dir, "agent_ssh_dir": None, "session_id": None}


@unittest.skipUnless(getattr(autorun, "vmjob", None) is not None,
                     "sandbox/vmjob.py not present (private microVM substrate; gitignored)")
class TestVmWorkdirCleanup(unittest.TestCase):
    def _call(self, result, prefix="acspeed-vm-"):
        wd = tempfile.mkdtemp(prefix=prefix)
        for f in ("rootfs.ext4", "job.ext4"):
            open(os.path.join(wd, f), "w").close()       # stand-ins for the big files
        for f in ("out.json", "boot.log"):
            open(os.path.join(wd, f), "w").close()       # the small diagnostics
        with mock.patch.object(autorun.vmjob, "run_vm_job", return_value=_fake_res(wd, result)), \
                mock.patch.object(autorun.vmjob, "_debugfs_dump", return_value=False), \
                mock.patch.object(autorun, "_install_recovered_ssh_keys"):
            out = autorun._claude_vm(
                "p", app_dir="/x", mcp=None, model=None, resume=None, max_turns=1, timeout=1,
                system=None, sandbox=_SANDBOX, boot_log=None, resume_transcript=None)
        return wd, out

    def test_success_removes_whole_workdir(self):
        wd, out = self._call({"session_id": "s1"})       # dict, no is_error -> success
        self.assertFalse(os.path.exists(wd), "work_dir leaked on the success path")
        self.assertEqual(out, {"session_id": "s1"})

    def test_failure_no_result_keeps_diagnostics_drops_rootfs(self):
        wd, out = self._call(None)                       # no result dict -> failure
        self.assertTrue(out["is_error"])
        self.assertTrue(os.path.isdir(wd), "a FAILED run's work_dir must be kept for diagnosis")
        self.assertFalse(os.path.exists(os.path.join(wd, "rootfs.ext4")), "the ~4 GB rootfs must still be dropped")
        self.assertTrue(os.path.exists(os.path.join(wd, "out.json")), "out.json (claude's error) must be kept")
        shutil.rmtree(wd, ignore_errors=True)

    def test_failure_is_error_dict_keeps_diagnostics(self):
        # the exact session=None / is_error=True case we could not diagnose
        wd, out = self._call({"is_error": True, "session_id": None})
        self.assertTrue(os.path.isdir(wd), "an is_error run's work_dir must be kept for diagnosis")
        self.assertFalse(os.path.exists(os.path.join(wd, "rootfs.ext4")))
        self.assertTrue(os.path.exists(os.path.join(wd, "out.json")))
        shutil.rmtree(wd, ignore_errors=True)

    def test_foreign_workdir_never_touched(self):
        wd, out = self._call({"session_id": "s"}, prefix="caller-owned-")
        self.assertTrue(os.path.exists(os.path.join(wd, "rootfs.ext4")), "a non-acspeed-vm work_dir was wrongly touched")
        shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
