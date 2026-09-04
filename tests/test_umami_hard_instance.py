"""Structure + fail-safety tests for the umami Hard instance, pinned to PAPER Part 5's Hard paragraph.

The instance must implement the PRE-REGISTERED protocol, not a weaker one: >= 10^5 events from an
OFF-CLOUD driver with an exact expected count and early/middle/late sentinels, loaded BEFORE a FIXED
four-fault battery, with a per-fault re-verify of the full dataset and a measured recovery time. A
harness that quietly implements less is falsifiable by anyone reading the published tool, so the shape
is asserted here.

No network: with no served URL every verify must return a clean VerifyResult(False) promptly, never raise
and never block. Live correctness of the capacity/fault predicates is validated by a live run, by design
(a verify that cannot fail is not a control).
"""
import unittest
from unittest import mock

from acspeed.operation import OPERATE_MUTATE, PROVISION
from acspeed.suite import OpContext
from acspeed.suites import get_instance, instance_names
from acspeed.suites import umami_hard


FAULTS = ["fault:kill-datastore", "fault:kill-app", "fault:reboot-host",
          "fault:partition-datastore-link"]


class TestUmamiHardStructure(unittest.TestCase):
    def test_registered_with_the_full_battery(self):
        self.assertIn("umami-hard", instance_names())
        inst = get_instance("umami-hard")
        self.assertEqual(inst.tier, "hard")
        self.assertEqual([o.op_id for o in inst.operations],
                         ["deploy-serve", "mutate:register", "integrate", "harden", "capacity"] + FAULTS)

    def test_the_fault_battery_is_fixed_and_matches_the_spec(self):
        """Spec: 'a fixed fault battery (kill the datastore, kill the app, reboot the host, partition the
        datastore link)'. One scored operation per fault, so the resilience score is per-fault."""
        by = {o.op_id: o for o in get_instance("umami-hard").operations}
        for fid in FAULTS:
            self.assertIn(fid, by, f"{fid} missing from the battery")
            self.assertEqual(by[fid].op_type, OPERATE_MUTATE)
            self.assertFalse(by[fid].durable)
        self.assertIn("serverless", by["fault:reboot-host"].task.lower(),
                      "the host fault must carry the serverless boundary-fault fallback")

    def test_op_types_and_durability(self):
        by = {o.op_id: o for o in get_instance("umami-hard").operations}
        self.assertEqual(by["deploy-serve"].op_type, PROVISION)
        for op in ("mutate:register", "integrate", "harden", "capacity"):
            self.assertEqual(by[op].op_type, OPERATE_MUTATE)
        # durable = re-verified in the terminal conjunction (after the battery) = integrity at scale
        for op in ("deploy-serve", "mutate:register", "integrate", "capacity"):
            self.assertTrue(by[op].durable, f"{op} should be durable")
        for op in ["harden"] + FAULTS:
            self.assertFalse(by[op].durable, f"{op} should not be durable")

    def test_every_fault_runs_after_the_capacity_layer(self):
        """Spec: the dataset is loaded 'before the faults', so durability is tested at scale."""
        by = {o.op_id: o for o in get_instance("umami-hard").operations}
        self.assertEqual(tuple(by["capacity"].depends_on), ("deploy-serve", "integrate"))
        for fid in FAULTS:
            self.assertIn("capacity", by[fid].depends_on, f"{fid} must depend on the capacity layer")
        ids = [o.op_id for o in get_instance("umami-hard").operations]
        for fid in FAULTS:
            self.assertGreater(ids.index(fid), ids.index("capacity"))

    def test_capacity_is_harness_driven_not_agent_driven(self):
        """Spec: the load comes 'from an off-cloud driver'. An agent-generated load would make the ground
        truth agent-reported, which the observe primitive forbids."""
        cap = {o.op_id: o for o in get_instance("umami-hard").operations}["capacity"]
        self.assertIsNotNone(cap.harness_action, "the capacity load must be harness-driven")

    def test_capacity_target_defaults_to_the_spec_and_is_configurable(self):
        self.assertEqual(umami_hard.SPEC_CAPACITY_TARGET, 100_000)
        default = get_instance("umami-hard")
        self.assertEqual(default.operations[4].op_id, "capacity")
        smoke = umami_hard.build(capacity_target=25)
        self.assertEqual(smoke.operations[4].op_id, "capacity")

    def test_harden_states_the_criterion_not_the_method(self):
        """Spec: 'The agent is told the criterion, not the method ... the judgement about how to be
        resilient is part of the test.'"""
        task = {o.op_id: o for o in get_instance("umami-hard").operations}["harden"].task.lower()
        self.assertIn("no data loss", task)
        self.assertIn("recover on its own", task)
        self.assertIn("your decision", task)

    def test_teardown_hint_covers_hardening_extras(self):
        inst = get_instance("umami-hard")
        self.assertIn("database", inst.teardown_hint)
        self.assertIn("second website", inst.teardown_hint)


class TestUmamiHardFailSafe(unittest.TestCase):
    def test_verifies_fail_cleanly_and_promptly_with_no_url(self):
        inst = get_instance("umami-hard")
        ctx = OpContext(url=None)
        for op in inst.operations:
            res = op.verify(ctx)
            self.assertFalse(res.ok, f"{op.op_id} unexpectedly ok with no url")
            self.assertIsInstance(res.detail, str)

    def test_recovery_wait_does_not_block_without_a_url(self):
        """A fault verify on a dead deployment must not burn the whole recovery budget."""
        self.assertIsNone(umami_hard._await_serving(OpContext(url=None), budget_s=999))

    def test_capacity_load_fails_closed_without_a_token(self):
        ctx = OpContext(url="https://x.example")
        ctx.state["probe"] = {"website_name": "w", "username": "u", "password": "p"}
        with mock.patch.object(umami_hard.um, "_probe_token", return_value=None):
            out = umami_hard._bulk_load(ctx, 10)
        self.assertFalse(out["ok"])
        self.assertIn("token", out["error"])


class TestCapacityLoadDriver(unittest.TestCase):
    """The off-cloud driver's own behaviour, with the network stubbed."""

    def _ctx(self):
        ctx = OpContext(url="https://umami.example")
        ctx.state["probe"] = {"website_name": "w", "username": "u", "password": "p",
                              "run_token": "acsdeadbeef"}
        return ctx

    def test_places_early_middle_late_sentinels_and_reports_load_reads(self):
        ctx = self._ctx()
        sent = []
        with mock.patch.object(umami_hard.um, "_probe_token", return_value="tok"), \
             mock.patch.object(umami_hard, "_website_id", return_value="wid"), \
             mock.patch.object(umami_hard.um, "_host", return_value="umami.example"), \
             mock.patch.object(umami_hard, "_pageview_count", return_value=7.0), \
             mock.patch.object(umami_hard, "_send_event",
                               side_effect=lambda u, w, h, p: (sent.append(p), (True, 0.01))[1]):
            out = umami_hard._bulk_load(ctx, 10)
        self.assertTrue(out["ok"])
        self.assertEqual(out["accepted"], 10)
        self.assertEqual(out["baseline_count"], 7.0)
        self.assertEqual(out["expected_count"], 17.0, "exact expected count = baseline + accepted")
        # the three ground-truth rows sit at the start, middle and end of the stream
        self.assertEqual(sent[0], "/acs-cap-early-acsdeadbeef")
        self.assertEqual(sent[5], "/acs-cap-middle-acsdeadbeef")
        self.assertEqual(sent[9], "/acs-cap-late-acsdeadbeef")
        # behavior-under-load reads the spec asks for
        self.assertIsNotNone(out["ingest_throughput_eps"])
        self.assertIsNotNone(out["p95_write_latency_s"])

    def test_partial_load_is_not_reported_as_success(self):
        ctx = self._ctx()
        ctx.state["capacity"] = {"ok": True, "target": 10, "accepted": 4}
        res = umami_hard._capacity_verified(ctx)
        self.assertFalse(res.ok)
        self.assertIn("accepted only 4", res.detail)

    def test_a_missing_sentinel_fails_the_dataset_even_when_the_count_is_right(self):
        """Partial loss is exactly what the sentinels exist to catch."""
        ctx = self._ctx()
        ctx.state["capacity"] = {"ok": True, "target": 3, "accepted": 3, "expected_count": 10,
                                 "website_id": "wid", "sentinels": {"early": "/e", "middle": "/m",
                                                                    "late": "/l"},
                                 "ingest_throughput_eps": 1.0, "p95_write_latency_s": 0.01}
        with mock.patch.object(umami_hard.um, "_probe_token", return_value="tok"), \
             mock.patch.object(umami_hard, "_pageview_count",
                               side_effect=lambda u, t, w, path=None: 0.0 if path == "/m" else 10.0):
            res = umami_hard._capacity_verified(ctx)
        self.assertFalse(res.ok, "a lost middle sentinel must fail the capacity check")


class TestFaultVerify(unittest.TestCase):
    def _ctx(self):
        ctx = OpContext(url="https://umami.example")
        ctx.state["probe"] = {"website_name": "w", "username": "u", "password": "p"}
        ctx.state["capacity"] = {"ok": True, "expected_count": 100, "website_id": "wid",
                                 "sentinels": {"early": "/e", "middle": "/m", "late": "/l"}}
        return ctx

    def test_records_recovery_time_and_dataset_size_per_fault(self):
        ctx = self._ctx()
        with mock.patch.object(umami_hard, "_await_serving", return_value=42.0), \
             mock.patch.object(umami_hard.um, "_integration_recorded",
                               return_value=umami_hard.VerifyResult(True, "visit ok")), \
             mock.patch.object(umami_hard, "_read_dataset",
                               return_value={"ok": True, "count": 100, "expected": 100,
                                             "sentinels_present": {"early": True, "middle": True,
                                                                   "late": True},
                                             "all_sentinels": True}):
            res = umami_hard._after_fault("fault:kill-app")(ctx)
        self.assertTrue(res.ok)
        rec = ctx.state["faults"]["fault:kill-app"]
        self.assertEqual(rec["recovery_s"], 42.0)
        self.assertEqual(rec["dataset_size"], 100, "recovery time is reported against the dataset size")
        self.assertTrue(rec["data_intact"])

    def test_data_loss_under_fault_fails_even_if_it_serves_again(self):
        ctx = self._ctx()
        with mock.patch.object(umami_hard, "_await_serving", return_value=10.0), \
             mock.patch.object(umami_hard.um, "_integration_recorded",
                               return_value=umami_hard.VerifyResult(True, "visit ok")), \
             mock.patch.object(umami_hard, "_read_dataset",
                               return_value={"ok": True, "count": 60, "expected": 100,
                                             "sentinels_present": {"early": True, "middle": False,
                                                                   "late": False},
                                             "all_sentinels": False}):
            res = umami_hard._after_fault("fault:kill-datastore")(ctx)
        self.assertFalse(res.ok, "serving again is not enough: the dataset must be intact")
        self.assertFalse(ctx.state["faults"]["fault:kill-datastore"]["data_intact"])

    def test_never_recovering_is_a_clean_failure(self):
        ctx = self._ctx()
        with mock.patch.object(umami_hard, "_await_serving", return_value=None):
            res = umami_hard._after_fault("fault:reboot-host")(ctx)
        self.assertFalse(res.ok)
        self.assertIn("never served again", res.detail)
        self.assertFalse(ctx.state["faults"]["fault:reboot-host"]["recovered"])


if __name__ == "__main__":
    unittest.main()
