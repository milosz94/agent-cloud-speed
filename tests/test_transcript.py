"""Part 5 harness: transcript -> owner-labelled trace.

The sum-to-wall identity ALONE is a check that cannot fail (it telescopes and passes even with every
row mislabelled), so the labeller is asserted with POSITIVE CONTROLS: synthetic rows must actually
land in the right lane. The trap regressions below each pin a lane error that the identity cannot
catch (background poll -> platform, AskUserQuestion -> human, denominator == wall, token per-block
inflation).
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from acspeed.transcript import (
    AGENT, HUMAN, IDLE, PLATFORM,
    dedupe_token_usage, lane_of, lane_summary, trace_from_transcript,
)
from acspeed.criticalpath import owner_split

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(offset_s):
    return (_BASE + timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")


def _write(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def _assistant(offset, rid, uuid, content):
    return {"type": "assistant", "timestamp": _iso(offset), "requestId": rid, "uuid": uuid,
            "message": {"role": "assistant", "content": content}}


def _tool_use(tid, name):
    return {"type": "tool_use", "id": tid, "name": name, "input": {}}


def _tool_result(offset, tid, uuid=None):
    return {"type": "user", "timestamp": _iso(offset), "uuid": uuid,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid}]}}


def _bg_notification(offset):
    return {"type": "user", "timestamp": _iso(offset), "origin": {"kind": "task-notification"},
            "promptSource": "system", "message": {"content": "done"}}


def _typed(offset):
    return {"type": "user", "timestamp": _iso(offset), "origin": {"kind": "human"},
            "promptSource": "typed", "message": {"content": "hi"}}


class TestLabellerPositiveControls(unittest.TestCase):
    """The four controls, verbatim from the reference engine. If any is inert, the labeller does
    nothing and every derived number is decoration."""

    def test_task_notification_is_platform(self):
        self.assertEqual(lane_of(_bg_notification(0), {}), PLATFORM)

    def test_typed_turn_is_human(self):
        self.assertEqual(lane_of(_typed(0), {}), HUMAN)

    def test_ask_user_question_result_is_human(self):
        row = {"type": "user", "message": {"content": [{"type": "tool_result",
                                                        "tool_use_id": "ask1"}]}}
        self.assertEqual(lane_of(row, {"ask1": "AskUserQuestion"}), HUMAN)

    def test_ordinary_tool_result_is_platform(self):
        row = {"type": "user", "message": {"content": [{"type": "tool_result",
                                                        "tool_use_id": "t1"}]}}
        self.assertEqual(lane_of(row, {"t1": "create_instance"}), PLATFORM)

    def test_assistant_is_agent(self):
        self.assertEqual(lane_of({"type": "assistant", "message": {"content": []}}, {}), AGENT)


class TestTraceReconstruction(unittest.TestCase):
    """A synthetic run with known gaps: 5s platform, 3s agent, 12s human, 5s platform, then a 400s gap
    that ends in an agent poll -- the agent idle while the cloud provisions, so it is platform, not idle."""

    def setUp(self):
        rows = [
            _assistant(0, "r1", "u0", [_tool_use("t1", "create_instance")]),
            _tool_result(5, "t1", uuid="u1"),                       # gap 0->5 ends on tool_result: platform
            _assistant(8, "r2", "u2", [_tool_use("t2", "AskUserQuestion")]),  # gap 5->8 ends on assistant: agent
            _tool_result(20, "t2", uuid="u3"),                      # gap 8->20 ends on Ask result: human
            _bg_notification(25),                                   # gap 20->25 ends on task-notification: platform
            _assistant(425, "r3", "u5", [_tool_use("t3", "get_deployment")]),  # gap 25->425 > MAX_GEN_GAP: idle-wait -> platform
        ]
        self.path = _write(rows)

    def tearDown(self):
        os.unlink(self.path)

    def test_owner_durations(self):
        spans = trace_from_transcript(self.path, cap=300.0, idle_is_platform=True)
        by_owner = {}
        for s in spans:
            by_owner[s.owner] = by_owner.get(s.owner, 0.0) + s.duration
        self.assertAlmostEqual(by_owner[PLATFORM], 410.0, places=6)  # 5 + 5 + the 400s agent idle-wait
        self.assertAlmostEqual(by_owner[AGENT], 3.0, places=6)
        self.assertAlmostEqual(by_owner[HUMAN], 12.0, places=6)
        self.assertAlmostEqual(by_owner.get(IDLE, 0.0), 0.0, places=6)  # no held-out idle: it was cloud-wait

    def test_makespan_equals_wall_including_idle(self):
        spans = trace_from_transcript(self.path, cap=300.0)
        self.assertAlmostEqual(owner_split(spans)["makespan"], 425.0, places=6)

    def test_sum_identity_attributed_plus_idle_equals_wall(self):
        s = lane_summary(self.path, cap=300.0)
        self.assertAlmostEqual(s["attributed_s"] + s["idle_s"], s["wall_s"], places=1)

    def test_denominator_is_wall_not_attributed(self):
        # agent is 3s. Over wall (425): ~0.7%. Over attributed (25): 12%. It MUST be over wall,
        # or a slower platform would mechanically raise the agent's share.
        s = lane_summary(self.path, cap=300.0)
        self.assertAlmostEqual(s["lane_pct_of_wall"][AGENT], 100.0 * 3.0 / 425.0, places=1)
        self.assertLess(s["lane_pct_of_wall"][AGENT], 1.0)

    def test_agent_first_token_split(self):
        # the one agent gap ends on the first row of its turn: it is first-token latency.
        s = lane_summary(self.path, cap=300.0)
        self.assertAlmostEqual(s["agent_split"]["first-token"], 3.0, places=1)
        self.assertAlmostEqual(s["agent_split"]["streaming"], 0.0, places=1)

    def test_chain_is_contiguous(self):
        spans = trace_from_transcript(self.path, cap=300.0)
        # linear chain: each span (after the first) depends on exactly its predecessor.
        self.assertEqual(spans[0].deps, ())
        for prev, cur in zip(spans, spans[1:]):
            self.assertEqual(cur.deps, (prev.id,))


class TestOperationWindowing(unittest.TestCase):
    """Per-operation slot-7 split relies on slicing a resumed session by each op's [start, end] window
    (acspeed.transcript since_epoch/until_epoch). Isolate a later op's turn and confirm the slice carries
    only that op's spans."""

    def test_since_epoch_isolates_a_later_operations_turn(self):
        base = _BASE.timestamp()
        rows = [
            _assistant(0, "r1", "u0", [_tool_use("t1", "x")]),      # op1
            _tool_result(5, "t1", uuid="u1"),                        # op1 platform 5s
            _assistant(10, "r1b", "u2", [{"type": "text", "text": "op1 done"}]),
            _assistant(100, "r2", "v0", [_tool_use("t2", "y")]),     # op2 starts
            _tool_result(108, "t2", uuid="v1"),                      # op2 platform 8s
        ]
        path = _write(rows)
        try:
            spans = trace_from_transcript(path, since_epoch=base + 95, until_epoch=base + 120)
            by = {o: v["critical"] for o, v in owner_split(spans)["owners"].items()}
            self.assertAlmostEqual(by.get(PLATFORM, 0.0), 8.0, places=1)  # op2's tool wait only
            self.assertEqual(by.get(AGENT, 0.0), 0.0)                     # none of op1 leaked in
            # the FULL trace, by contrast, carries both op1 (5s) and op2 (8s) platform
            full = {o: v["critical"] for o, v in owner_split(trace_from_transcript(path))["owners"].items()}
            self.assertAlmostEqual(full.get(PLATFORM, 0.0), 13.0, places=1)
        finally:
            os.unlink(path)


class TestTrapRegressions(unittest.TestCase):
    def test_background_poll_is_platform_not_human(self):
        # a backgrounded readiness poll returns as a bare user row; it is the platform, not a person.
        rows = [_assistant(0, "r1", "u0", [_tool_use("t1", "wait_for_deployment")]),
                _bg_notification(200)]  # 200s of platform wait
        path = _write(rows)
        try:
            s = lane_summary(path, cap=300.0)
            self.assertAlmostEqual(s["lanes"][PLATFORM], 200.0, places=1)
            self.assertAlmostEqual(s["lanes"][HUMAN], 0.0, places=1)
        finally:
            os.unlink(path)

    def test_long_agent_gap_is_platform_idle_wait(self):
        # A gap that ends in an agent turn but is far longer than model generation is the agent sitting
        # idle while the cloud provisions: platform, not agent, and never held-out idle. (Was the run10
        # leak: 191s idle charged to agent deflated platform to 124s and became a false floor.)
        rows = [_assistant(0, "r1", "u0", [_tool_use("t1", "x")]),
                _assistant(500, "r2", "u1", [_tool_use("t2", "y")])]  # 500s > MAX_GEN_GAP
        path = _write(rows)
        try:
            s = lane_summary(path, cap=300.0, idle_is_platform=True)
            self.assertAlmostEqual(s["lanes"][PLATFORM], 500.0, places=1)
            self.assertAlmostEqual(s["lanes"][AGENT], 0.0, places=1)
            self.assertAlmostEqual(s["idle_s"], 0.0, places=1)
            # OUTSIDE an operation window (default) the same gap is an inter-op pause: held-out idle.
            s2 = lane_summary(path, cap=300.0)
            self.assertAlmostEqual(s2["idle_s"], 500.0, places=1)
            self.assertAlmostEqual(s2["lanes"][PLATFORM], 0.0, places=1)
        finally:
            os.unlink(path)

    def test_short_agent_gap_is_generation(self):
        # A gap within MAX_GEN_GAP that ends in an agent turn is genuine model generation: agent (either mode).
        rows = [_tool_result(0, "t0", uuid="u0"),
                _assistant(40, "r2", "u1", [_tool_use("t2", "y")])]  # 40s <= MAX_GEN_GAP
        path = _write(rows)
        try:
            s = lane_summary(path, cap=300.0, idle_is_platform=True)
            self.assertAlmostEqual(s["lanes"][AGENT], 40.0, places=1)
            self.assertAlmostEqual(s["lanes"][PLATFORM], 0.0, places=1)
        finally:
            os.unlink(path)

    def test_long_human_gap_is_still_held_out_idle(self):
        # A long HUMAN (ask-tool) pause is a person, not the cloud: held out as idle even in an op window.
        rows = [_assistant(0, "r1", "u0", [_tool_use("t1", "AskUserQuestion")]),
                _tool_result(500, "t1", uuid="u1")]  # ends on an Ask result (human), 500s > cap
        path = _write(rows)
        try:
            s = lane_summary(path, cap=300.0, idle_is_platform=True)
            self.assertAlmostEqual(s["idle_s"], 500.0, places=1)
            self.assertAlmostEqual(s["lanes"][PLATFORM], 0.0, places=1)
        finally:
            os.unlink(path)

    def test_long_platform_gap_is_platform_not_idle(self):
        # a blocking cloud call (a provisioning / readiness poll) can block for minutes; that gap ends
        # in a platform event, so it is platform critical-path time at ANY length, never held-out idle.
        # (Capping it dropped real serverless provisioning wall from makespan by ~2.5x; the fix.)
        rows = [_assistant(0, "r1", "u0", [_tool_use("t1", "wait_for_deployment")]),
                _bg_notification(600)]  # 600s > cap, but ends on a platform (background) event
        path = _write(rows)
        try:
            s = lane_summary(path, cap=300.0)
            self.assertAlmostEqual(s["lanes"][PLATFORM], 600.0, places=1)
            self.assertAlmostEqual(s["idle_s"], 0.0, places=1)
            # a long gap ending on an ordinary tool_result is likewise platform, not idle.
            rows2 = [_assistant(0, "r1", "u0", [_tool_use("t1", "deploy_app")]),
                     _tool_result(500, "t1", uuid="u1")]
            path2 = _write(rows2)
            try:
                s2 = lane_summary(path2, cap=300.0)
                self.assertAlmostEqual(s2["lanes"][PLATFORM], 500.0, places=1)
                self.assertAlmostEqual(s2["idle_s"], 0.0, places=1)
            finally:
                os.unlink(path2)
        finally:
            os.unlink(path)

    def test_token_usage_deduped_per_turn(self):
        # the same usage object repeats on every block of a turn; summing rows would triple-count.
        u = {"input_tokens": 100, "output_tokens": 50}
        rows = [
            _assistant(0, "rT", "a0", [{"type": "text", "text": "hi"}]),
            _assistant(1, "rT", "a1", [_tool_use("t1", "x")]),   # same turn, repeated usage
            _assistant(2, "rT2", "b0", [{"type": "text", "text": "yo"}]),
        ]
        rows[0]["message"]["usage"] = dict(u)
        rows[1]["message"]["usage"] = dict(u)
        rows[2]["message"]["usage"] = {"input_tokens": 200, "output_tokens": 80}
        path = _write(rows)
        try:
            tot = dedupe_token_usage(path)
            self.assertEqual(tot["turns"], 2)
            self.assertEqual(tot["input_tokens"], 300)   # 100 (turn rT) + 200 (turn rT2), NOT 400
            self.assertEqual(tot["output_tokens"], 130)  # 50 + 80, NOT 180
        finally:
            os.unlink(path)


class TestClipAtReadiness(unittest.TestCase):
    """Part 2: the operation ends at its slot-5 end signal (app-serving, an EXTERNAL poll). Agent
    events after t1 (post-serving verification) are outside the operation, so the trace is clipped.

    Regression pinned 2026-08-25 (Isso run): the app served at 181s, the agent kept verifying until
    403s, and an unclipped trace charged all of it to the deploy."""

    def setUp(self):
        self.rows = [
            _assistant(0, "r1", "u0", [_tool_use("t1", "deploy_compose")]),
            _tool_result(60, "t1", uuid="u1"),                      # platform: deploy call returns
            _assistant(65, "r2", "u2", [_tool_use("t2", "Bash")]),   # agent
            _tool_result(66, "t2", uuid="u3"),                       # platform: curl result
            _assistant(300, "r3", "u4", [{"type": "text", "text": "verified, done"}]),  # after t1
        ]
        self.path = _write(self.rows)

    def tearDown(self):
        os.unlink(self.path)

    def test_unclipped_trace_spans_the_whole_session(self):
        self.assertAlmostEqual(owner_split(trace_from_transcript(self.path))["makespan"], 300.0, places=6)

    def test_clip_at_readiness_drops_post_serving_events(self):
        t1 = _BASE.timestamp() + 70.0                                 # served at 70s: after row 4, before row 5
        spans = trace_from_transcript(self.path, until_epoch=t1)
        self.assertEqual(len(spans), 3)
        self.assertAlmostEqual(owner_split(spans)["makespan"], 66.0, places=6)

    def test_clip_is_inclusive_at_the_boundary(self):
        spans = trace_from_transcript(self.path, until_epoch=_BASE.timestamp() + 66.0)
        self.assertEqual(len(spans), 3)

    def test_clip_before_second_event_is_empty(self):
        self.assertEqual(trace_from_transcript(self.path, until_epoch=_BASE.timestamp() + 1.0), [])


class TestDegenerate(unittest.TestCase):
    def test_fewer_than_two_events_is_empty(self):
        path = _write([_assistant(0, "r1", "u0", [{"type": "text", "text": "hi"}])])
        try:
            self.assertEqual(trace_from_transcript(path), [])
            self.assertIsNone(lane_summary(path))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
