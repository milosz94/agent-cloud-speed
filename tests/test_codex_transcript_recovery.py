"""A codex run must come back with its transcript, and the transcript must be findable.

Two defects this pins, both of which made a codex run produce NO transcript and therefore no
platform/agent split, with every other check still green:

1. vm-runner.sh wrote codex_sessions.tar to the job drive and vmjob.run_vm_job never dumped it back.
2. Codex names its rollout `rollout-<ts>-<thread_id>.jsonl` and reports the thread_id as the session
   id, so the id is a SUFFIX of the stem. A finder matching only `<session_id>.jsonl` never hit.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sandbox"))

import vmjob  # noqa: E402
import autorun  # noqa: E402

THREAD = "019e520f-dc04-7691-b2a1-6cf6ef9bbe4a"
ROLLOUT = f"rollout-2026-05-23T01-40-31-{THREAD}.jsonl"


def _codex_tree(base):
    d = os.path.join(base, "sessions", "2026", "05", "23")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, ROLLOUT)
    with open(p, "w") as fh:
        fh.write('{"timestamp":"2026-05-23T01:40:31Z","type":"session_meta","payload":{}}\n')
    return p


class FindsCodexRollouts(unittest.TestCase):

    def test_find_transcript_in_matches_a_rollout_by_thread_id(self):
        with tempfile.TemporaryDirectory() as d:
            want = _codex_tree(d)
            got = vmjob.find_transcript_in(d, THREAD)
            self.assertEqual(got, want, "thread id is a suffix of the rollout stem, not the stem")

    def test_find_transcript_in_still_matches_claude_exact_names(self):
        with tempfile.TemporaryDirectory() as d:
            pdir = os.path.join(d, "projects", "-home-agent-app")
            os.makedirs(pdir)
            sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            want = os.path.join(pdir, f"{sid}.jsonl")
            open(want, "w").write("{}\n")
            self.assertEqual(vmjob.find_transcript_in(d, sid), want)

    def test_exact_match_wins_over_suffix_match(self):
        """A claude session whose id happens to be a suffix of some other file must not be shadowed."""
        with tempfile.TemporaryDirectory() as d:
            sid = "1234"
            os.makedirs(os.path.join(d, "a"))
            exact = os.path.join(d, "a", f"{sid}.jsonl")
            open(exact, "w").write("{}\n")
            open(os.path.join(d, "a", f"rollout-x-{sid}.jsonl"), "w").write("{}\n")
            self.assertEqual(vmjob.find_transcript_in(d, sid), exact)

    def test_host_side_finder_searches_the_codex_tree(self):
        with tempfile.TemporaryDirectory() as store:
            want = _codex_tree(store)
            old = autorun._SESSION_STORE
            try:
                autorun._SESSION_STORE = store
                self.assertEqual(autorun.find_transcript(THREAD), want)
            finally:
                autorun._SESSION_STORE = old

    def test_recovery_pulls_the_codex_artifact_back(self):
        """vmjob must ask for /codex_sessions.tar, not only /transcripts.tar."""
        src = open(os.path.join(ROOT, "sandbox", "vmjob.py")).read()
        self.assertIn('"/codex_sessions.tar"', src,
                      "the guest writes codex_sessions.tar; the host must dump it back")
        self.assertIn('"/transcripts.tar"', src)

    def test_the_guest_writes_the_artifact_the_host_asks_for(self):
        """The two filenames must agree; they are the whole contract between guest and host."""
        runner = open(os.path.join(ROOT, "sandbox", "rootfs", "vm-runner.sh")).read()
        host = open(os.path.join(ROOT, "sandbox", "vmjob.py")).read()
        for artifact in ("codex_sessions.tar", "transcripts.tar", "out.json", "exit_code"):
            self.assertIn(artifact, runner, f"guest never writes {artifact}")
            self.assertIn(artifact, host, f"host never collects {artifact}")


if __name__ == "__main__":
    unittest.main()
