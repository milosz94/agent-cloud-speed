"""Structure + fail-safety tests for the umami Medium instance. These do NOT hit the network: with no
served URL the verify predicates must return a clean VerifyResult(False), never raise. The predicates'
CORRECTNESS against a live umami is validated separately (a live run), by design: a verify that cannot
fail is not a control, so it is trusted only after it has been seen to fail and pass against real umami.
"""
import unittest

from acspeed.operation import OPERATE_MUTATE, PROVISION
from acspeed.suite import OpContext
from acspeed.suites import get_instance, instance_names
from acspeed.suites import umami_medium


class TestUmamiMediumStructure(unittest.TestCase):
    def test_registry(self):
        self.assertIn("umami-medium", instance_names())
        inst = get_instance("umami-medium")
        self.assertEqual(inst.tier, "medium")
        self.assertEqual([o.op_id for o in inst.operations],
                         ["deploy-serve", "mutate:register", "integrate", "restart"])

    def test_unknown_instance_raises(self):
        with self.assertRaises(KeyError):
            get_instance("does-not-exist")

    def test_operation_types_and_durability(self):
        inst = get_instance("umami-medium")
        by_id = {o.op_id: o for o in inst.operations}
        self.assertEqual(by_id["deploy-serve"].op_type, PROVISION)
        self.assertEqual(by_id["mutate:register"].op_type, OPERATE_MUTATE)
        self.assertEqual(by_id["integrate"].op_type, OPERATE_MUTATE)
        self.assertEqual(by_id["restart"].op_type, OPERATE_MUTATE)
        # restart is a liveness action; durability is the terminal conjunction of the others
        self.assertFalse(by_id["restart"].durable)
        self.assertTrue(by_id["deploy-serve"].durable)
        self.assertTrue(by_id["mutate:register"].durable)
        self.assertTrue(by_id["integrate"].durable)

    def test_sentinel_embedded_in_task_text(self):
        probe = umami_medium._make_probe()
        inst = umami_medium.build(probe=probe)
        by_id = {o.op_id: o for o in inst.operations}
        # the exact sentinel the runner will look for must be handed to the agent
        self.assertIn(probe["username"], by_id["mutate:register"].task)
        self.assertIn(probe["password"], by_id["mutate:register"].task)
        self.assertIn(probe["website_name"], by_id["integrate"].task)
        self.assertIn(probe["sentinel_path"], by_id["integrate"].task)

    def test_dependencies(self):
        inst = get_instance("umami-medium")
        by_id = {o.op_id: o for o in inst.operations}
        self.assertEqual(tuple(by_id["mutate:register"].depends_on), ("deploy-serve",))
        self.assertEqual(tuple(by_id["integrate"].depends_on), ("deploy-serve", "mutate:register"))

    def test_teardown_hint_lives_in_the_instance_not_autorun(self):
        # the app-specific teardown text (second site, managed DB) belongs to the instance so autorun
        # stays app-agnostic; it must be present and mention the second site.
        inst = get_instance("umami-medium")
        self.assertTrue(inst.teardown_hint)
        self.assertIn("second website", inst.teardown_hint)
        self.assertIn("database", inst.teardown_hint)


class TestUmamiPredicatesFailSafe(unittest.TestCase):
    def test_verifies_fail_cleanly_with_no_url(self):
        inst = get_instance("umami-medium")
        ctx = OpContext(url=None)
        for op in inst.operations:
            res = op.verify(ctx)  # must not raise, must be a clean fail
            self.assertFalse(res.ok, f"{op.op_id} unexpectedly ok with no url")
            self.assertIsInstance(res.detail, str)


if __name__ == "__main__":
    unittest.main()
