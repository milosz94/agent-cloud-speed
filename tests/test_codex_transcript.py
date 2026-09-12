"""Codex rollouts must measure through the SAME code path as Claude Code transcripts.

The attribution rule is about lanes and gaps, not about either vendor's JSON, so Codex rows are
normalized at load time and everything downstream runs unchanged. These tests pin that: the lane
assignment, the wall == attributed identity, and that Claude parsing is untouched.
"""
import json
import os
import tempfile
import unittest

from acspeed import transcript as tx


def _codex(rows):
    """Write a Codex-shaped rollout: {"timestamp","type","payload"} per line."""
    fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for ts, rtype, payload in rows:
        fh.write(json.dumps({"timestamp": ts, "type": rtype, "payload": payload}) + "\n")
    fh.close()
    return fh.name


class TestCodexRollout(unittest.TestCase):
    def setUp(self):
        # 10 s of model work, then 20 s of tool execution, then 5 s more model work.
        self.path = _codex([
            ("2026-09-13T00:00:00.000Z", "session_meta", {"id": "s1"}),
            ("2026-09-13T00:00:00.000Z", "event_msg", {"type": "user_message"}),
            ("2026-09-13T00:00:10.000Z", "response_item", {"type": "reasoning"}),
            ("2026-09-13T00:00:30.000Z", "response_item",
             {"type": "function_call_output", "call_id": "c1"}),
            ("2026-09-13T00:00:35.000Z", "event_msg", {"type": "agent_message"}),
        ])

    def tearDown(self):
        os.unlink(self.path)

    def test_detected_as_codex_and_normalized(self):
        rows = tx._load_rows(self.path)
        self.assertEqual([r["type"] for r in rows], ["user", "assistant", "user", "assistant"])

    def test_lanes_follow_the_event_that_ends_the_gap(self):
        s = tx.lane_summary(self.path)
        self.assertAlmostEqual(s["lanes"]["agent"], 15.0, places=1)      # 10 s + 5 s
        self.assertAlmostEqual(s["lanes"]["platform"], 20.0, places=1)   # the tool gap

    def test_attributed_equals_wall(self):
        """The identity the whole split rests on: every second charged exactly once."""
        s = tx.lane_summary(self.path)
        self.assertAlmostEqual(s["attributed_s"], s["wall_s"], places=1)

    def test_a_tool_output_is_platform_not_human(self):
        rows = tx._load_rows(self.path)
        names = tx._tool_names(rows)
        self.assertEqual(tx.lane_of(rows[2], names), tx.PLATFORM)

    def test_claude_transcripts_are_unaffected(self):
        """A Claude Code transcript must not be mistaken for a rollout."""
        fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for ts, t in (("2026-09-13T00:00:00.000Z", "user"), ("2026-09-13T00:00:12.000Z", "assistant")):
            fh.write(json.dumps({"timestamp": ts, "type": t, "message": {"content": []}}) + "\n")
        fh.close()
        try:
            self.assertFalse(tx._is_codex_rollout([json.loads(l) for l in open(fh.name)]))
            self.assertAlmostEqual(tx.lane_summary(fh.name)["lanes"]["agent"], 12.0, places=1)
        finally:
            os.unlink(fh.name)


if __name__ == "__main__":
    unittest.main()
