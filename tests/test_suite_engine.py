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
    DurabilityGoal,
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


class TestOpRetry(unittest.TestCase):
    def test_op_recovers_within_max_attempts(self):
        # verify fails on attempt 1, passes on attempt 2; the retry turn carries the failure detail.
        state = {"n": 0}
        seen_tasks = []

        def run_agent(op, ctx, sid):
            seen_tasks.append(op.task)
            state["n"] += 1
            ctx.state["ok"] = state["n"] >= 2
            return {"session_id": "s"}

        v = lambda ctx: VerifyResult(bool(ctx.state.get("ok")), "needs redeploy" if not ctx.state.get("ok") else "ok")
        inst = TierInstance(name="i", tier="medium",
                            operations=[TierOperation(op_id="integrate", op_type=OPERATE_MUTATE,
                                                      task="wire it", verify=v, max_attempts=3)])
        run = run_tier(inst, OpContext(), run_agent=run_agent)
        self.assertTrue(run.passed)
        self.assertEqual(state["n"], 2)                      # recovered on the 2nd attempt
        self.assertIn("DID NOT SATISFY", seen_tasks[1])      # retry turn got the failure detail

    def test_op_gives_up_after_max_attempts(self):
        def run_agent(op, ctx, sid):
            return {"session_id": "s"}
        inst = TierInstance(name="i", tier="medium",
                            operations=[TierOperation(op_id="x", op_type=OPERATE_MUTATE, task="t",
                                                      verify=lambda c: VerifyResult(False, "no"), max_attempts=2)])
        run = run_tier(inst, OpContext(), run_agent=run_agent)
        self.assertFalse(run.passed)
        self.assertEqual(len([o for o in run.outcomes if o.op.op_id == "x"]), 1)  # one outcome, not per-attempt

    def test_max_attempts_default_is_one(self):
        calls = {"n": 0}
        def run_agent(op, ctx, sid):
            calls["n"] += 1
            return {"session_id": "s"}
        inst = TierInstance(name="i", tier="medium",
                            operations=[TierOperation(op_id="x", op_type=PROVISION, task="t",
                                                      verify=lambda c: VerifyResult(False, "no"))])
        run_tier(inst, OpContext(), run_agent=run_agent)
        self.assertEqual(calls["n"], 1)  # no retry by default


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


class TestScoring(unittest.TestCase):
    def test_full_completion_gets_partial_1_plus_bonus(self):
        inst = TierInstance(
            name="i", tier="medium",
            operations=[_op("deploy", _ok, op_type=PROVISION), _op("wire", _ok, depends_on=("deploy",))],
        )
        run = run_tier(inst, OpContext(), run_agent=lambda op, ctx, sid: {"session_id": "s"})
        sc = run.score()
        self.assertEqual(sc["partial_credit"], 1.0)
        self.assertTrue(sc["completed"])
        self.assertEqual(sc["score"], 1.0 + run.FULL_COMPLETION_BONUS)
        self.assertEqual(sc["break_point"], [])

    def test_partial_credit_and_break_point_when_a_checkpoint_fails(self):
        # provisions but the wire/integrate checkpoint fails -> still scores, break point located.
        inst = TierInstance(
            name="i", tier="medium",
            operations=[_op("deploy", _ok, op_type=PROVISION),
                        _op("integrate", lambda ctx: VerifyResult(False, "no pageview"), depends_on=("deploy",))],
        )
        run = run_tier(inst, OpContext(), run_agent=lambda op, ctx, sid: {"session_id": "s"})
        sc = run.score()
        self.assertFalse(sc["completed"])
        self.assertGreater(sc["partial_credit"], 0.0)   # deploy checkpoint still earns credit
        self.assertLess(sc["partial_credit"], 1.0)
        self.assertEqual(sc["full_completion_bonus"], 0.0)
        self.assertIn("integrate (mid-run)", sc["break_point"])

    def test_durability_goal_is_a_scored_checkpoint(self):
        def run_agent(op, ctx, sid):
            ctx.state["alive"] = op.op_id != "restart-durability" or True  # durable on first restart
            return {"session_id": "s"}
        alive = lambda ctx: VerifyResult(bool(ctx.state.get("alive")), "")
        inst = TierInstance(
            name="i", tier="medium", operations=[_op("deploy", alive, op_type=PROVISION)],
            durability=DurabilityGoal(task="restart", max_iters=3),
        )
        run = run_tier(inst, OpContext(), run_agent=run_agent)
        names = [c["name"] for c in run.score()["checkpoints"]]
        self.assertIn("restart-durability", names)
        self.assertTrue(run.score()["completed"])


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


class TestDurabilityGoal(unittest.TestCase):
    """Restart-durability as a GOAL: the agent iterates (re-architecting) until the durable state
    survives a restart, up to a budget. Non-survival is not a failure to score, it is another cycle."""

    def _alive(self):
        return lambda ctx: VerifyResult(bool(ctx.state.get("alive")), f"alive={ctx.state.get('alive')}")

    def test_durable_on_first_cycle(self):
        inst = TierInstance(
            name="i", tier="medium",
            operations=[_op("deploy", _ok, op_type=PROVISION)],
            durability=DurabilityGoal(task="restart", max_iters=4),
        )
        run = run_tier(inst, OpContext(), run_agent=lambda op, ctx, sid: {"session_id": "s"})
        self.assertTrue(run.passed)
        self.assertEqual(run.durability_achieved, 1)
        self.assertEqual(run.durability_iters, 1)
        self.assertEqual(len(run.durability_outcomes), 1)

    def test_iterates_until_durable(self):
        # ephemeral: each restart wipes the state UNTIL the agent has 'fixed' it (cycle >= 2).
        state = {"cycles": 0, "durable_after": 2}

        def run_agent(op, ctx, sid):
            if op.op_id == "restart-durability":
                state["cycles"] += 1
                ctx.state["alive"] = state["cycles"] >= state["durable_after"]
            else:
                ctx.state["alive"] = True
            return {"session_id": "s"}

        inst = TierInstance(
            name="i", tier="medium",
            operations=[_op("deploy", self._alive(), op_type=PROVISION)],
            durability=DurabilityGoal(task="restart", max_iters=4),
        )
        run = run_tier(inst, OpContext(), run_agent=run_agent)
        self.assertEqual(run.durability_achieved, 2)  # survived only on the 2nd restart
        self.assertEqual(run.durability_iters, 2)
        self.assertTrue(run.passed)
        self.assertEqual(len(run.durability_outcomes), 2)

    def test_budget_exhausted_is_not_durable(self):
        def run_agent(op, ctx, sid):
            ctx.state["alive"] = op.op_id != "restart-durability"  # every restart wipes it
            return {"session_id": "s"}

        inst = TierInstance(
            name="i", tier="medium",
            operations=[_op("deploy", self._alive(), op_type=PROVISION)],
            durability=DurabilityGoal(task="restart", max_iters=3),
        )
        run = run_tier(inst, OpContext(), run_agent=run_agent)
        self.assertIsNone(run.durability_achieved)
        self.assertEqual(run.durability_iters, 3)  # used the whole budget
        self.assertFalse(run.passed)               # a lost sentinel at the end is not a pass
        self.assertIn("deploy (terminal)", run.failures())

    def test_retry_task_names_the_lost_sentinel(self):
        tasks = []

        def run_agent(op, ctx, sid):
            if op.op_id == "restart-durability":
                tasks.append(op.task)
                ctx.state["alive"] = len(tasks) >= 2
            else:
                ctx.state["alive"] = True
            return {"session_id": "s"}

        inst = TierInstance(
            name="i", tier="medium",
            operations=[_op("deploy", self._alive(), op_type=PROVISION)],
            durability=DurabilityGoal(task="restart", max_iters=4),
        )
        run_tier(inst, OpContext(), run_agent=run_agent)
        self.assertNotIn("deploy", tasks[0])  # first cycle: no feedback yet
        self.assertIn("deploy", tasks[1])      # second cycle: names the lost 'deploy' sentinel to repair

    def test_liveness_read_recorded_per_cycle(self):
        serves = {"ok": False}

        def run_agent(op, ctx, sid):
            if op.op_id == "restart-durability":
                serves["ok"] = True  # comes back after the restart
                ctx.state["alive"] = True
            else:
                ctx.state["alive"] = True
            return {"session_id": "s"}

        inst = TierInstance(
            name="i", tier="medium",
            operations=[_op("deploy", self._alive(), op_type=PROVISION)],
            durability=DurabilityGoal(task="restart", max_iters=4,
                                      liveness=lambda ctx: VerifyResult(serves["ok"], "serves")),
        )
        run = run_tier(inst, OpContext(), run_agent=run_agent)
        self.assertTrue(run.durability_outcomes[0].verify.ok)  # R6 liveness verified each cycle
        s = run.summary()
        self.assertTrue(s["durability"]["achieved"])
        self.assertEqual(s["durability"]["achieved_on_iter"], 1)

    def test_rejects_durability_without_any_durable_op(self):
        with self.assertRaises(ValueError):
            TierInstance(
                name="i", tier="medium",
                operations=[_op("restart", _ok, op_type=PROVISION, durable=False)],
                durability=DurabilityGoal(task="restart"),
            )

    def test_rejects_zero_iter_budget(self):
        with self.assertRaises(ValueError):
            DurabilityGoal(task="restart", max_iters=0)

    def test_reestablish_runs_after_a_lost_cycle(self):
        # harness-generated state a restart wipes until the agent makes storage durable (cycle 2). The
        # reestablish hook re-creates it between cycles so the loop can converge.
        state = {"cycles": 0}
        called = []

        def run_agent(op, ctx, sid):
            if op.op_id == "restart-durability":
                state["cycles"] += 1
                if state["cycles"] < 2:
                    ctx.state["present"] = False  # early restart wipes the harness state
            else:
                ctx.state["present"] = True
            return {"session_id": "s"}

        def reestablish(ctx, lost):
            called.append(list(lost))
            ctx.state["present"] = True  # re-create it on the (now durable) storage

        present = lambda ctx: VerifyResult(bool(ctx.state.get("present")), "")
        inst = TierInstance(
            name="i", tier="medium",
            operations=[_op("integrate", present, op_type=OPERATE_MUTATE)],
            durability=DurabilityGoal(task="restart", max_iters=4, reestablish=reestablish),
        )
        run = run_tier(inst, OpContext(), run_agent=run_agent)
        self.assertTrue(run.passed)
        self.assertEqual(called, [["integrate"]])       # ran once, after the cycle-1 loss
        self.assertEqual(run.durability_achieved, 2)


if __name__ == "__main__":
    unittest.main()
