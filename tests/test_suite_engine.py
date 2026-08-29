"""Tests for the benchmark suite ENGINE (acspeed/suite.py): sequencing, resume-session wiring, URL
propagation, and the terminal conjunction that decides pass/fail. The engine is app-independent, so
these use synthetic operations with toggleable verify predicates and a fake agent executor. No live
agent, no live app, no network.

The load-bearing test is `test_durable_sentinel_lost_after_restart_fails`: an operation that verified
mid-run is broken by a later operation (a simulated redeploy/restart wiping state), and the terminal
re-verify must catch it. That is CP7 (restart-durability) and the whole reason the terminal
conjunction exists.
"""
import unittest

from acspeed.operation import DEPROVISION, OPERATE_MUTATE, PROVISION
from acspeed.suite import (
    OpContext,
    TierInstance,
    TierOperation,
    VerifyResult,
    run_tier,
)


def _op(op_id, verify, *, op_type=OPERATE_MUTATE, depends_on=(), durable=True, fresh_session=False):
    return TierOperation(
        op_id=op_id, op_type=op_type, task=f"do {op_id}", verify=verify,
        depends_on=depends_on, durable=durable, fresh_session=fresh_session,
    )


def _ok(_ctx):
    return VerifyResult(True, "ok")


class TestInstanceValidation(unittest.TestCase):
    def test_rejects_bad_op_type(self):
        with self.assertRaises(ValueError):
            TierOperation(op_id="x", op_type="not-a-type", task="t", verify=_ok)

    def test_rejects_non_callable_verify(self):
        with self.assertRaises(ValueError):
            TierOperation(op_id="x", op_type=PROVISION, task="t", verify="nope")

    def test_rejects_empty_instance(self):
        with self.assertRaises(ValueError):
            TierInstance(name="i", tier="medium", operations=[])

    def test_rejects_forward_dependency(self):
        # b depends on a, but a is listed AFTER b -> malformed
        b = _op("b", _ok, depends_on=("a",))
        a = _op("a", _ok)
        with self.assertRaises(ValueError):
            TierInstance(name="i", tier="medium", operations=[b, a])

    def test_rejects_unknown_dependency(self):
        b = _op("b", _ok, depends_on=("ghost",))
        with self.assertRaises(ValueError):
            TierInstance(name="i", tier="medium", operations=[b])

    def test_rejects_duplicate_op_id(self):
        with self.assertRaises(ValueError):
            TierInstance(name="i", tier="medium", operations=[_op("a", _ok), _op("a", _ok)])

    def test_accepts_well_formed(self):
        inst = TierInstance(
            name="i", tier="medium",
            operations=[_op("a", _ok, op_type=PROVISION), _op("b", _ok, depends_on=("a",))],
        )
        self.assertEqual(len(inst.operations), 2)


class TestHappyPath(unittest.TestCase):
    def test_all_pass(self):
        inst = TierInstance(
            name="i", tier="medium",
            operations=[_op("deploy", _ok, op_type=PROVISION), _op("mutate", _ok, depends_on=("deploy",))],
        )
        run = run_tier(inst, OpContext(), run_agent=lambda op, ctx, sid: {"session_id": "s1"})
        self.assertTrue(run.passed)
        self.assertEqual(run.failures(), [])
        self.assertEqual(len(run.outcomes), 2)
        # both durable -> both re-verified at terminal
        self.assertEqual(set(run.terminal), {"deploy", "mutate"})


class TestFailures(unittest.TestCase):
    def test_mid_run_failure_fails_the_tier(self):
        inst = TierInstance(
            name="i", tier="medium",
            operations=[_op("deploy", _ok, op_type=PROVISION),
                        _op("mutate", lambda ctx: VerifyResult(False, "no state"))],
        )
        run = run_tier(inst, OpContext(), run_agent=lambda op, ctx, sid: {"session_id": "s1"})
        self.assertFalse(run.passed)
        self.assertIn("mutate (mid-run)", run.failures())

    def test_durable_sentinel_lost_after_restart_fails(self):
        # register verifies against a shared 'alive' flag. deploy sets it True; the restart turn
        # (a simulated redeploy) sets it False. register passes mid-run, then the terminal re-verify
        # catches the loss. This is CP7.
        def run_agent(op, ctx, sid):
            if op.op_id == "deploy":
                ctx.state["alive"] = True
                return {"session_id": "s1", "url": "http://x"}
            if op.op_id == "restart":
                ctx.state["alive"] = False  # the restart/redeploy wiped the durable state
            return {"session_id": "s1"}

        alive = lambda ctx: VerifyResult(bool(ctx.state.get("alive")), f"alive={ctx.state.get('alive')}")
        inst = TierInstance(
            name="i", tier="medium",
            operations=[
                _op("deploy", alive, op_type=PROVISION),
                _op("register", alive, depends_on=("deploy",)),
                _op("restart", _ok, depends_on=("deploy",), durable=False),
            ],
        )
        run = run_tier(inst, OpContext(), run_agent=run_agent)
        # mid-run every op passed (register saw alive=True before the restart)
        self.assertTrue(all(o.ok for o in run.outcomes))
        # but the terminal conjunction fails: register (and deploy) re-read alive=False
        self.assertFalse(run.passed)
        self.assertIn("register (terminal)", run.failures())
        # the non-durable restart is NOT in the terminal re-verify
        self.assertNotIn("restart", run.terminal)


class TestWiring(unittest.TestCase):
    def test_resume_session_and_fresh_session(self):
        seen = {}

        def run_agent(op, ctx, sid):
            seen[op.op_id] = sid
            return {"session_id": "deploy-sid"} if op.op_id == "deploy" else {"session_id": "ignored"}

        inst = TierInstance(
            name="i", tier="medium",
            operations=[
                _op("deploy", _ok, op_type=PROVISION),
                _op("mutate", _ok, depends_on=("deploy",)),
                _op("audit", _ok, depends_on=("deploy",), fresh_session=True),
            ],
        )
        run_tier(inst, OpContext(), run_agent=run_agent)
        self.assertIsNone(seen["deploy"])        # first turn: new session
        self.assertEqual(seen["mutate"], "deploy-sid")  # resumes the deploy session
        self.assertIsNone(seen["audit"])         # fresh_session -> new session, not a resume

    def test_url_propagates_from_deploy_turn(self):
        captured = {}

        def run_agent(op, ctx, sid):
            if op.op_id == "deploy":
                return {"session_id": "s1", "url": "http://served.example"}
            captured["url_seen_by_later_op"] = ctx.url
            return {"session_id": "s1"}

        inst = TierInstance(
            name="i", tier="medium",
            operations=[_op("deploy", _ok, op_type=PROVISION), _op("mutate", _ok, depends_on=("deploy",))],
        )
        ctx = OpContext()
        run_tier(inst, ctx, run_agent=run_agent)
        self.assertEqual(ctx.url, "http://served.example")
        self.assertEqual(captured["url_seen_by_later_op"], "http://served.example")

    def test_none_agent_result_does_not_crash(self):
        inst = TierInstance(name="i", tier="medium", operations=[_op("deploy", _ok, op_type=PROVISION)])
        run = run_tier(inst, OpContext(), run_agent=lambda op, ctx, sid: None)
        self.assertTrue(run.passed)  # verify is _ok; a null agent turn is tolerated


class TestSummary(unittest.TestCase):
    def test_summary_shape(self):
        inst = TierInstance(
            name="umami-x", tier="medium",
            operations=[_op("deploy", _ok, op_type=PROVISION),
                        _op("restart", _ok, durable=False, depends_on=("deploy",))],
        )
        run = run_tier(inst, OpContext(),
                       run_agent=lambda op, ctx, sid: {"session_id": "s", "total_cost_usd": 0.5})
        s = run.summary()
        self.assertEqual(s["instance"], "umami-x")
        self.assertEqual(s["tier"], "medium")
        self.assertTrue(s["passed"])
        self.assertEqual(len(s["operations"]), 2)
        self.assertIn("deploy", s["terminal_reverify"])
        self.assertNotIn("restart", s["terminal_reverify"])  # non-durable


if __name__ == "__main__":
    unittest.main()
