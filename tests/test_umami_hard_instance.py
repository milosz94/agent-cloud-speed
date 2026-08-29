"""Structure + fail-safety tests for the umami Hard instance. No network: with no served URL every verify
must return a clean VerifyResult(False), never raise. Live correctness of the capacity/fault predicates is
validated by a live run, by design (a verify that cannot fail is not a control)."""
import unittest

from acspeed.operation import OPERATE_MUTATE, PROVISION
from acspeed.suite import OpContext
from acspeed.suites import get_instance, instance_names
from acspeed.suites import umami_hard


class TestUmamiHardStructure(unittest.TestCase):
    def test_registered(self):
        self.assertIn("umami-hard", instance_names())
        inst = get_instance("umami-hard")
        self.assertEqual(inst.tier, "hard")
        self.assertEqual([o.op_id for o in inst.operations],
                         ["deploy-serve", "mutate:register", "integrate", "harden", "capacity", "fault"])

    def test_op_types_and_durability(self):
        by = {o.op_id: o for o in get_instance("umami-hard").operations}
        self.assertEqual(by["deploy-serve"].op_type, PROVISION)
        for op in ("mutate:register", "integrate", "harden", "capacity", "fault"):
            self.assertEqual(by[op].op_type, OPERATE_MUTATE)
        # durable = re-verified in the terminal conjunction (after the fault) = integrity-at-scale
        for op in ("deploy-serve", "mutate:register", "integrate", "capacity"):
            self.assertTrue(by[op].durable, f"{op} should be durable")
        # harden/fault are actions whose only postcondition is 'still serves'; their payoff is the terminal
        for op in ("harden", "fault"):
            self.assertFalse(by[op].durable, f"{op} should not be durable")

    def test_dependencies(self):
        by = {o.op_id: o for o in get_instance("umami-hard").operations}
        self.assertEqual(tuple(by["capacity"].depends_on), ("deploy-serve", "integrate"))
        self.assertEqual(tuple(by["fault"].depends_on), ("deploy-serve", "harden"))

    def test_capacity_target_in_task_and_configurable(self):
        default = get_instance("umami-hard")
        cap = next(o for o in default.operations if o.op_id == "capacity")
        self.assertIn("10000", cap.task)                       # default 10^4
        bigger = umami_hard.build(capacity_target=100000)
        cap2 = next(o for o in bigger.operations if o.op_id == "capacity")
        self.assertIn("100000", cap2.task)                     # the spec full-study target, configurable

    def test_teardown_hint_covers_hardening_extras(self):
        inst = get_instance("umami-hard")
        self.assertIn("database", inst.teardown_hint)
        self.assertIn("second website", inst.teardown_hint)


class TestUmamiHardFailSafe(unittest.TestCase):
    def test_verifies_fail_cleanly_with_no_url(self):
        inst = get_instance("umami-hard")
        ctx = OpContext(url=None)
        for op in inst.operations:
            res = op.verify(ctx)
            self.assertFalse(res.ok, f"{op.op_id} unexpectedly ok with no url")
            self.assertIsInstance(res.detail, str)


if __name__ == "__main__":
    unittest.main()
