"""Tests for the Part 2 operation model: schema, typology, registers, the
SSH-ready milestone split, session-as-trace, and the efficiency metric."""
import math
import unittest

from acspeed.operation import (
    AGENT_COGNITION, APP_SERVING, CONTROL_PLANE, DATA, DEPROVISION, IN_GUEST,
    OPERATE_MUTATE, PROVISION, Phase, Operation, SSH_READY,
    has_defined_endpoints, is_schema_conformant, register_of,
)
from acspeed.session import Session, efficiency
from acspeed.types import AGENT, PLATFORM, Span


def provision_op():
    # request(agent) -> provision(control) -> boot -> ssh_ready -> materialize(data) -> app_start
    spans = [
        Span("req", 1.0, AGENT, (), AGENT_COGNITION),
        Span("prov", 3.0, PLATFORM, ("req",), CONTROL_PLANE),
        Span("boot", 5.0, PLATFORM, ("prov",), IN_GUEST),
        Span("ssh", 2.0, PLATFORM, ("boot",), IN_GUEST),
        Span("mat", 6.0, PLATFORM, ("ssh",), IN_GUEST),
        Span("app", 2.0, PLATFORM, ("mat",), IN_GUEST),
    ]
    return Operation(
        PROVISION, spans, start_id="req", end_id="app",
        phases=(Phase("materialize", ("mat",), plane=DATA),),
        milestones={SSH_READY: "ssh", APP_SERVING: "app"},
        label="deploy",
    )


def mutate_op():
    spans = [
        Span("req", 1.0, AGENT, (), AGENT_COGNITION),
        Span("introspect", 3.0, AGENT, ("req",), AGENT_COGNITION),
        Span("diagnose", 4.0, AGENT, ("introspect",), AGENT_COGNITION),
        Span("redeploy", 2.0, PLATFORM, ("diagnose",), CONTROL_PLANE),
        Span("verify", 2.0, AGENT, ("redeploy",), AGENT_COGNITION),
    ]
    return Operation(OPERATE_MUTATE, spans, start_id="req", end_id="verify", label="fix")


def teardown_op():
    spans = [
        Span("req", 1.0, AGENT, (), AGENT_COGNITION),
        Span("del1", 1.0, PLATFORM, ("req",), CONTROL_PLANE),
        Span("del2", 1.0, PLATFORM, ("del1",), CONTROL_PLANE),
    ]
    return Operation(DEPROVISION, spans, start_id="req", end_id="del2", label="teardown")


class TestSchemaAndSplit(unittest.TestCase):
    def test_wall_clock_and_split_sum(self):
        op = provision_op()
        s = op.split()
        self.assertAlmostEqual(op.wall_clock(), 19.0)
        self.assertAlmostEqual(s["critical_platform"] + s["critical_agent"], op.wall_clock())
        self.assertAlmostEqual(s["critical_agent"], 1.0)      # only the request is agent-owned
        self.assertAlmostEqual(s["critical_platform"], 18.0)

    def test_bad_op_type_and_phase_axes_raise(self):
        with self.assertRaises(ValueError):
            Operation("not_a_type", [Span("a", 1.0, PLATFORM)])
        with self.assertRaises(ValueError):
            Phase("p", plane="sideways")
        with self.assertRaises(ValueError):
            Phase("p", synchrony="maybe")


class TestTypologyProfiles(unittest.TestCase):
    def test_provision_is_platform_bound(self):
        self.assertEqual(provision_op().profile(), "platform-bound")

    def test_mutate_is_agent_bound(self):
        self.assertEqual(mutate_op().profile(), "agent-bound")

    def test_teardown_is_control_plane_only(self):
        self.assertEqual(teardown_op().profile(), "control-plane-only")


class TestRegisters(unittest.TestCase):
    def test_register_of_infers_from_owner(self):
        self.assertEqual(register_of(Span("a", 1.0, AGENT)), AGENT_COGNITION)
        self.assertEqual(register_of(Span("b", 1.0, PLATFORM)), IN_GUEST)
        self.assertEqual(register_of(Span("c", 1.0, PLATFORM, (), CONTROL_PLANE)), CONTROL_PLANE)

    def test_invariant_holds_for_clean_trace(self):
        self.assertTrue(provision_op().register_invariant_holds())
        # registers 1+2 raw == platform raw; register 3 raw == agent raw
        rs = provision_op().register_split()["raw"]
        self.assertAlmostEqual(rs[CONTROL_PLANE] + rs[IN_GUEST], 18.0)
        self.assertAlmostEqual(rs[AGENT_COGNITION], 1.0)

    def test_mismatched_register_is_the_fourth_register_falsifier(self):
        # a platform-owned span that declares the agent-cognition register: no
        # consistent owner, i.e. a genuine unmodelled register (Section 8).
        bad = Operation(PROVISION, [Span("x", 1.0, PLATFORM, (), AGENT_COGNITION)],
                        start_id="x", end_id="x")
        self.assertFalse(bad.register_invariant_holds())
        self.assertFalse(is_schema_conformant(bad))


class TestMilestoneSplit(unittest.TestCase):
    def test_ssh_ready_lower_upper_halves(self):
        ms = provision_op().milestone_split(SSH_READY)
        self.assertAlmostEqual(ms["milestone_time"], 11.0)  # 1+3+5+2
        # lower half: request(agent 1) + provision/boot/ssh (platform 10)
        self.assertAlmostEqual(ms["lower"]["critical_agent"], 1.0)
        self.assertAlmostEqual(ms["lower"]["critical_platform"], 10.0)
        # upper half: materialize(6) + app_start(2) = platform 8, agent 0
        self.assertAlmostEqual(ms["upper"]["critical_agent"], 0.0)
        self.assertAlmostEqual(ms["upper"]["critical_platform"], 8.0)
        # halves reconstruct the whole
        s = provision_op().split()
        self.assertAlmostEqual(
            ms["lower"]["critical_platform"] + ms["upper"]["critical_platform"],
            s["critical_platform"])

    def test_missing_milestone_raises(self):
        with self.assertRaises(ValueError):
            provision_op().milestone_split("no_such_milestone")


class TestFalsifiability(unittest.TestCase):
    def test_defined_endpoints(self):
        self.assertTrue(has_defined_endpoints(provision_op()))
        no_end = Operation(PROVISION, [Span("a", 1.0, PLATFORM)], start_id="a", end_id="ghost")
        self.assertFalse(has_defined_endpoints(no_end))

    def test_schema_conformant(self):
        self.assertTrue(is_schema_conformant(provision_op()))


class TestSession(unittest.TestCase):
    def test_sequential_session_wall_clock(self):
        op1 = Operation(PROVISION, [Span("a1", 2.0, PLATFORM), Span("a2", 3.0, PLATFORM, ("a1",))],
                        start_id="a1", end_id="a2", label="one")
        op2 = Operation(DEPROVISION, [Span("b1", 1.0, PLATFORM, ("a2",)), Span("b2", 4.0, PLATFORM, ("b1",))],
                        start_id="b1", end_id="b2", label="two")
        sess = Session([op1, op2])
        self.assertAlmostEqual(sess.wall_clock(), 10.0)  # 2+3+1+4 fully serial

    def test_duplicate_span_id_across_ops_raises(self):
        op1 = Operation(PROVISION, [Span("dup", 1.0, PLATFORM)], start_id="dup", end_id="dup")
        op2 = Operation(DEPROVISION, [Span("dup", 1.0, PLATFORM)], start_id="dup", end_id="dup")
        with self.assertRaises(ValueError):
            Session([op1, op2]).spans()


class TestEfficiency(unittest.TestCase):
    def test_excess_is_selection_plus_execution(self):
        actual = [("deploy", 19.0), ("fix", 12.0), ("teardown", 3.0)]
        optimal = [("deploy", 15.0), ("teardown", 3.0)]
        e = efficiency(actual, optimal)
        self.assertAlmostEqual(e["excess"], 16.0)
        self.assertAlmostEqual(e["execution_excess"], 4.0)   # deploy 19 vs 15
        self.assertAlmostEqual(e["selection_excess"], 12.0)  # extra 'fix'
        self.assertAlmostEqual(e["excess"], e["selection_excess"] + e["execution_excess"])

    def test_selection_only(self):
        e = efficiency([("a", 5.0), ("b", 5.0)], [("a", 5.0)])
        self.assertAlmostEqual(e["selection_excess"], 5.0)
        self.assertAlmostEqual(e["execution_excess"], 0.0)

    def test_execution_only(self):
        e = efficiency([("a", 8.0)], [("a", 5.0)])
        self.assertAlmostEqual(e["selection_excess"], 0.0)
        self.assertAlmostEqual(e["execution_excess"], 3.0)
        self.assertAlmostEqual(e["ratio"], 8.0 / 5.0)

    def test_efficiency_accepts_operations(self):
        e = efficiency([provision_op()], [("deploy", 15.0)])
        self.assertAlmostEqual(e["actual"], 19.0)
        self.assertAlmostEqual(e["execution_excess"], 4.0)


if __name__ == "__main__":
    unittest.main()
