""""Every billable resource is priced" must be decided by the ACCOUNT, not by me reading the output.

Cost Explorer reports what was CHARGED keyed on usagetype; every published SKU carries the same
usagetype; so a resource the pricer never reached is a billed usagetype it never produced. That join
needs no list of billable things -- the billable set is whatever AWS charged for -- which is the
difference from `aws_audit`, whose table of billable CLI verbs is blind to whatever it has not heard of.

⛔ IT CANNOT SEE A QUANTITY ERROR. If three Fargate tasks ran and one was priced, every billed usagetype
is still present and this passes. It proves nothing is MISSING, not that nothing is UNDER-COUNTED.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from acspeed import aws_conformance as conf


_BILLED = {"BoxUsage:t3.medium": 0.2368, "USE1-PublicIPv4:InUseAddress": 0.2019,
           "RDS:GP3-Storage": 0.0073, "DataTransfer-Out-Bytes": 0.0011,
           "Requests-Tier1": 0.0006, "LCUUsage": 0.0004, "USE1-Free-Thing": 0.0}


class TestTheAccountDecides(unittest.TestCase):

    def _run(self, produced):
        with mock.patch.object(conf, "billed_usagetypes", return_value=_BILLED):
            return conf.conformance(produced, "2026-09-06", "2026-09-07")

    def test_a_billed_line_the_pricer_never_produced_is_a_miss(self):
        got = self._run(["BoxUsage:t3.medium", "USE1-PublicIPv4:InUseAddress"])
        self.assertFalse(got["ok"])
        self.assertEqual([m[0] for m in got["missing"]], ["RDS:GP3-Storage"])

    def test_everything_produced_passes(self):
        got = self._run(["BoxUsage:t3.medium", "USE1-PublicIPv4:InUseAddress", "RDS:GP3-Storage"])
        self.assertTrue(got["ok"], got["missing"])

    def test_metered_lines_are_out_of_scope_not_misses(self):
        """C19 measures a standing hourly run-rate; per-request and per-byte lines vary with traffic and
        the paper reports egress separately. Named so the exclusion cannot quietly widen."""
        got = self._run(["BoxUsage:t3.medium", "USE1-PublicIPv4:InUseAddress", "RDS:GP3-Storage"])
        metered = [m[0] for m in got["out_of_scope_metered"]]
        self.assertIn("DataTransfer-Out-Bytes", metered)
        self.assertIn("Requests-Tier1", metered)
        self.assertIn("LCUUsage", metered)

    def test_a_zero_charge_is_not_evidence_either_way(self):
        got = self._run([])
        self.assertNotIn("USE1-Free-Thing", [m[0] for m in got["missing"]])

    def test_it_refuses_rather_than_passing_when_the_bill_is_unreadable(self):
        """A conformance PASS that was never checked is worse than no check."""
        with mock.patch.object(conf, "billed_usagetypes", side_effect=RuntimeError("CE unreadable")):
            with self.assertRaises(RuntimeError):
                conf.conformance(["anything"], "2026-09-06", "2026-09-07")


class TestTheComparisonIsCompleteByConstruction(unittest.TestCase):
    """Comparing SOME runs against the WHOLE account's bill reports the other runs' charges as misses,
    which is noise that hides the real ones."""

    def test_tokens_are_read_from_the_results_directory(self):
        with tempfile.TemporaryDirectory() as d:
            for name, token, when in (("run1.json", "acsaaa", "2026-09-06T11:00:00Z"),
                                      ("run2.json", "acsbbb", "2026-09-06T13:00:00Z"),
                                      ("run3.json", "acsccc", "2026-09-04T13:00:00Z")):
                with open(os.path.join(d, name), "w") as fh:
                    json.dump({"run_token": token, "measured_at": when}, fh)
            with open(os.path.join(d, "tables.json"), "w") as fh:
                json.dump({"not": "a run"}, fh)
            got = conf.tokens_in(d, "2026-09-06", "2026-09-07")
        self.assertEqual(sorted(got), ["acsaaa", "acsbbb"], "only runs inside the window")

    def test_an_unreadable_record_does_not_abort_the_listing(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "run1.json"), "w") as fh:
                fh.write("{ truncated")
            with open(os.path.join(d, "run2.json"), "w") as fh:
                json.dump({"run_token": "acsok", "measured_at": "2026-09-06T11:00:00Z"}, fh)
            self.assertEqual(conf.tokens_in(d, "2026-09-06", "2026-09-07"), ["acsok"])


if __name__ == "__main__":
    unittest.main()
