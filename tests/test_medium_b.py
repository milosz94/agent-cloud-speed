"""Medium B (DISCLOSED regime): the SAME umami Medium workload with the full plan disclosed up front.
Only the information regime differs from Medium A; a Medium A run vs a Medium B run gives the paired
value-of-plan-lookahead gap M_online - M_disclosed (Part-3 Sec-5), NOT the Sec-4 selection excess."""
import unittest

from acspeed import suites as acs_suites
from acspeed.suite import full_plan_preamble


class TestMediumB(unittest.TestCase):
    def test_registered_and_launchable(self):
        self.assertIn("umami-medium-b", acs_suites.instance_names())

    def test_medium_a_is_the_online_regime(self):
        a = acs_suites.get_instance("umami-medium")
        self.assertFalse(a.plan_upfront)          # online: operations revealed one at a time
        self.assertEqual(a.name, "umami-medium")

    def test_medium_b_is_the_disclosed_regime(self):
        b = acs_suites.get_instance("umami-medium-b")
        self.assertTrue(b.plan_upfront)           # disclosed: full plan up front
        self.assertEqual(b.name, "umami-medium-b")
        self.assertEqual(b.tier, "medium")

    def test_same_workload_as_medium_a(self):
        # identical operation graph, durability goal, and teardown target -> only the regime differs,
        # which is what makes M_online - M_disclosed a clean paired measurement.
        a = acs_suites.get_instance("umami-medium")
        b = acs_suites.get_instance("umami-medium-b")
        self.assertEqual([o.op_id for o in a.operations], [o.op_id for o in b.operations])
        self.assertEqual([o.op_type for o in a.operations], [o.op_type for o in b.operations])
        self.assertEqual([o.max_attempts for o in a.operations], [o.max_attempts for o in b.operations])
        self.assertEqual(a.durability.op_id, b.durability.op_id)
        self.assertEqual(a.durability.max_iters, b.durability.max_iters)
        self.assertEqual(a.teardown_hint, b.teardown_hint)

    def test_full_plan_preamble_discloses_every_operation(self):
        b = acs_suites.get_instance("umami-medium-b")
        pre = full_plan_preamble(b, token="acsdeadbeef")
        # every operation's goal (and the restart-durability goal) is disclosed
        for op in b.operations:
            self.assertIn(op.task.strip()[:48], pre, f"op {op.op_id} not disclosed in the preamble")
        self.assertIn(b.durability.task.strip()[:48], pre)
        # discloses the plan, tells the agent to schedule with lookahead, and carries the run token
        self.assertIn("COMPLETE PLAN UP FRONT", pre)
        self.assertIn("concurrently", pre)
        self.assertIn("acsdeadbeef", pre)

    def test_online_regime_carries_no_preamble(self):
        # Medium A must NOT be plan_upfront, so autorun never prepends the disclosure to its deploy prompt.
        a = acs_suites.get_instance("umami-medium")
        self.assertFalse(a.plan_upfront)

    def test_preamble_without_token_omits_naming_clause(self):
        b = acs_suites.get_instance("umami-medium-b")
        pre = full_plan_preamble(b)   # no token
        self.assertNotIn("carry this run's token", pre)

    def test_preamble_pins_both_site_names(self):
        # the disclosed prompt names each site '<name>-<token>' so the concurrently provisioned primary and
        # second site never collide; the harness then selects the primary by name.
        b = acs_suites.get_instance("umami-medium-b")
        pre = full_plan_preamble(b, token="acsdeadbeef")
        self.assertIn("umami-acsdeadbeef", pre)
        self.assertIn("alcove-acsdeadbeef", pre)

    def test_both_medium_regimes_name_both_sites(self):
        # both regimes carry the two URL names, so online (A) and disclosed (B) disambiguate the same way.
        for nm in ("umami-medium", "umami-medium-b"):
            inst = acs_suites.get_instance(nm)
            self.assertEqual(inst.primary_url_name, "umami")
            self.assertEqual(inst.second_site_url_name, "alcove")


if __name__ == "__main__":
    unittest.main()
