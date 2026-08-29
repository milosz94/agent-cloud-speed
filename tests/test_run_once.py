"""End-to-end wiring of run_once with the agent, the network and the cloud all stubbed.

Pins the Isso regression at the driver level: the app serves (with a 400) WHILE the agent is still
working, so time-to-serving must be the poller's t1, not the agent's finish, the record must carry
the external clock, the split must be clipped at t1, and NO repair prompt may be sent.
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import autorun

RUN_TOKEN = "acstok01"                         # pinned so the mocked URL carries this run's token
URL = f"https://isso-{RUN_TOKEN}.redu.cloud"   # the agent named the deployment with the token (real flow)


def _row(offset_s, kind, content, base):
    ts = (base + timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")
    if kind == "assistant":
        return {"type": "assistant", "timestamp": ts, "requestId": f"r{offset_s}", "uuid": f"u{offset_s}",
                "message": {"role": "assistant", "content": content}}
    return {"type": "user", "timestamp": ts, "uuid": f"u{offset_s}", "message": {"content": content}}


class TestRunOnceIssoRegression(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.app = os.path.join(self.root, "app")
        os.makedirs(self.app)
        with open(os.path.join(self.app, "docker-compose.yml"), "w") as fh:
            fh.write("services: {}\n")
        self.work = os.path.join(self.root, "work")
        self.out = os.path.join(self.root, "out")
        self.slug = os.path.join(self.root, "projects")
        os.makedirs(self.slug)
        self.tx = os.path.join(self.slug, "sess1.jsonl")
        self.prof = {"task_prompt": "Deploy the application in this directory.",
                     "mcp_config": "/dev/null", "url_re": r"https://[a-z0-9.-]+\.redu\.cloud",
                     "cloud": "redu", "task": "app", "app_dir": self.app, "run_cwd": self.work,
                     "out_dir": self.out, "copy_ignore": [], "keep": False, "run_token": RUN_TOKEN}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _fake_claude(self, prompt, *, cwd, mcp, model, resume=None, **kw):
        """The 'agent': writes its transcript live, the URL appears early, then it keeps 'verifying'
        for a while after the app has served. Records every prompt so repairs can be asserted."""
        self.prompts.append(prompt)
        if resume:
            return {"session_id": "sess1", "result": "repaired", "total_cost_usd": 1.0}
        base = datetime.now(timezone.utc)
        rows = [
            _row(0.0, "assistant", [{"type": "tool_use", "id": "t1", "name": "deploy_compose", "input": {}}], base),
            _row(0.2, "user", [{"type": "tool_result", "tool_use_id": "t1",
                                "content": f'{{"access_point":"{URL}","status":"building"}}'}], base),
        ]
        with open(self.tx, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        self.t_url = time.monotonic()                    # the URL exists from here; the app boots ~0.3s
        self.url_written.set()
        time.sleep(0.9)                                  # the agent 'verifies' after the app served
        rows2 = [
            _row(0.9, "assistant", [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}], base),
            _row(1.0, "user", [{"type": "tool_result", "tool_use_id": "t2", "content": "200"}], base),
            _row(1.1, "assistant", [{"type": "text", "text": f"Deployed at {URL}"}], base),
        ]
        with open(self.tx, "a") as fh:
            for r in rows2:
                fh.write(json.dumps(r) + "\n")
        return {"session_id": "sess1", "result": f"Deployed at {URL}", "total_cost_usd": 2.0,
                "is_error": False}

    def _fake_is_serving(self, url):
        # 502 until the URL has been in the transcript for a moment, then Isso's documented 400
        if self.url_written.is_set() and time.monotonic() - self.t_url >= 0.3:
            return "400", True
        return "502", False

    def test_served_while_agent_still_working_no_repair(self):
        self.prompts = []
        self.url_written = threading.Event()
        self.t_url = time.monotonic()
        with mock.patch.object(autorun, "_claude", side_effect=self._fake_claude), \
             mock.patch.object(autorun, "is_serving", side_effect=self._fake_is_serving), \
             mock.patch.object(autorun, "transcript_dir_for", return_value=self.slug), \
             mock.patch.object(autorun, "find_transcript", return_value=self.tx), \
             mock.patch.object(autorun, "SERVING_POLL_S", 0.05), \
             mock.patch.object(autorun, "deprovision_agent",
                               return_value={"session": "td", "cost": 0.1, "transcript": None}) as dep, \
             mock.patch.object(autorun, "curl_dead", return_value={"dead": True, "http_code": "000"}):
            rec = autorun.run_once(1, self.prof, "claude-test", max_rounds=4)

        self.assertEqual(rec["outcome"], "SUCCESS")
        self.assertTrue(rec["first_attempt_success"])
        self.assertEqual(len(self.prompts), 1, "no repair prompt may be sent when the app served")
        self.assertEqual(rec["url"], URL)
        self.assertEqual(rec["url_source"], "external-poll")
        self.assertEqual(rec["health_code"], "400")
        s = rec["serving"]
        self.assertEqual(s["caught_by"], "concurrent-poller")
        self.assertLess(rec["time_to_serving_s"], s["agent_finished_at_s"])
        self.assertGreater(s["agent_after_serving_s"], 0.0)
        self.assertIn("< 500", s["predicate"])
        self.assertIsNotNone(rec["split"])
        self.assertIsNone(rec["screenshot"])            # --screenshot not set: no capture, no PNG
        self.assertFalse(os.path.exists(os.path.join(self.out, "run01.png")))
        # trace clipped at t1: only the two pre-serving rows are inside the operation (~0.2s + boot)
        self.assertLess(rec["split"]["makespan_s"], 0.9)
        self.assertGreaterEqual(rec["split"]["post_handoff_boot_s"], 0.0)
        dep.assert_called_once()
        self.assertTrue(os.path.exists(os.path.join(self.out, "run01.json")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "run01_workdir", "docker-compose.yml")))

    def test_never_serving_triggers_repair_then_failure(self):
        self.prompts = []
        self.url_written = threading.Event()
        self.t_url = time.monotonic()
        with mock.patch.object(autorun, "_claude", side_effect=self._fake_claude), \
             mock.patch.object(autorun, "is_serving", return_value=("502", False)), \
             mock.patch.object(autorun, "transcript_dir_for", return_value=self.slug), \
             mock.patch.object(autorun, "find_transcript", return_value=self.tx), \
             mock.patch.object(autorun, "SERVING_POLL_S", 0.05), \
             mock.patch.object(autorun, "READINESS_TIMEOUT_S", 0.1), \
             mock.patch.object(autorun, "deprovision_agent",
                               return_value={"session": "td", "cost": 0.1, "transcript": None}), \
             mock.patch.object(autorun, "curl_dead", return_value={"dead": True, "http_code": "000"}):
            rec = autorun.run_once(1, self.prof, "claude-test", max_rounds=2)
        self.assertEqual(rec["outcome"], "FAILURE-never-served")
        self.assertFalse(rec["first_attempt_success"])
        self.assertEqual(len(self.prompts), 2)                 # one deploy + one repair (max_rounds=2)
        self.assertIn("HTTP 502", self.prompts[1])
        self.assertIn("3xx/4xx", self.prompts[1])
        self.assertIsNone(rec["time_to_serving_s"])
        self.assertEqual(rec["serving"]["caught_by"], None)


if __name__ == "__main__":
    unittest.main()
