"""The runner must drive either agent CLI through one result contract.

The measurement is already agent-agnostic (tests/test_codex_transcript.py). These tests pin the other
half: the headless invocation per agent, and normalizing each CLI's very different stdout onto the one
shape every caller reads, {is_error, result, session_id}.
"""
import importlib.util
import json
import os
import unittest

_spec = importlib.util.spec_from_file_location(
    "autorun_under_test", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "autorun.py"))
autorun = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(autorun)
except SystemExit:
    pass


class TestBuildAgentCmd(unittest.TestCase):
    def test_claude_invocation_is_unchanged(self):
        cmd = autorun.build_agent_cmd("claude", "hello", mcp="/m.json", model="claude-opus-5",
                                      resume=None, max_turns=4, system=None)
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertIn("--mcp-config", cmd)
        self.assertIn("--output-format", cmd)

    def test_codex_uses_exec_json(self):
        cmd = autorun.build_agent_cmd("codex", "hello", mcp=None, model="gpt-5",
                                      resume=None, max_turns=4, system=None)
        self.assertEqual(cmd[:3], ["codex", "exec", "--json"])
        self.assertIn("-m", cmd)
        self.assertEqual(cmd[-1], "hello")

    def test_codex_has_no_append_system_flag_so_the_prompt_carries_it(self):
        cmd = autorun.build_agent_cmd("codex", "task", mcp=None, model=None, resume=None,
                                      max_turns=1, system="be terse")
        self.assertTrue(cmd[-1].startswith("be terse"))
        self.assertIn("task", cmd[-1])


class TestParseAgentResult(unittest.TestCase):
    def test_claude_single_json_object(self):
        out = json.dumps({"is_error": False, "result": "done", "session_id": "abc"})
        r = autorun.parse_agent_result("claude", out, "")
        self.assertEqual((r["is_error"], r["session_id"]), (False, "abc"))

    def test_codex_jsonl_success_yields_thread_id_and_last_message(self):
        out = "\n".join(json.dumps(x) for x in [
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "READY"}},
            {"type": "turn.completed"},
        ])
        r = autorun.parse_agent_result("codex", out, "")
        self.assertFalse(r["is_error"])
        self.assertEqual(r["session_id"], "t-1")
        self.assertEqual(r["result"], "READY")

    def test_codex_turn_failure_is_an_error_but_keeps_the_session_id(self):
        """A failed turn must still surface its thread id, or the run cannot be joined to its rollout."""
        out = "\n".join(json.dumps(x) for x in [
            {"type": "thread.started", "thread_id": "t-2"},
            {"type": "turn.failed", "error": {"message": "model not supported"}},
        ])
        r = autorun.parse_agent_result("codex", out, "")
        self.assertTrue(r["is_error"])
        self.assertEqual(r["session_id"], "t-2")
        self.assertIn("model not supported", r["result"])

    def test_codex_garbage_does_not_raise(self):
        r = autorun.parse_agent_result("codex", "not json at all", "")
        self.assertIsNone(r["session_id"])


if __name__ == "__main__":
    unittest.main()
