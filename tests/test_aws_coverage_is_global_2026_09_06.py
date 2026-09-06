"""Coverage was maximised PER SERVICE, so every service submitted its best guess as an equal.

Measured on run23 (2026-09-06 03:37, a live agent-driven run): `describe_live` returns the whole running
RDS instance, including its maintenance windows, status, CA certificate and parameter group. Among those,
"standard", "region" and "available" are genuine attribute values SOMEWHERE in the disclosure, so 864
SKUs from Pinpoint, MediaLive, Chime and ElastiCache each matched ONE word and arrived alongside the two
that matched TWO -- InstanceUsage:db.t3.micro and RDS:GP2-Storage, both in the resource's own service.
The run recorded ok:false with its database unpriced and a cost of $23.89 instead of ~$37.03.

⛔ Invisible offline: with the instance torn down the describe returns nothing, the selector set is just
the create call's four words, and the instance prices correctly. Every re-pricing of a finished run
showed exactly that.
"""
import unittest
from unittest import mock

from acspeed.adapters import aws_ct_cost as ct


def _sku(usagetype, usd, coverage, unit="Hrs"):
    return {"sku": usagetype, "usagetype": usagetype, "operation": "", "coverage": coverage,
            "attributes": {"usagetype": usagetype},
            "prices": [{"unit": unit, "usd": usd, "description": usagetype}]}


_DB = {"kind": "dbinstance", "event": "CreateDBInstance", "region": "us-east-1", "src": "rds",
       "params": {"dBInstanceClass": "db.t3.micro", "engine": "postgres", "status": "available",
                  "networkType": "region"}}


class TestTheStrongerEvidenceWins(unittest.TestCase):

    def _price(self, per_service, resource=None):
        px = "acspeed.adapters.aws_price_index"

        def coverage(code, _region, _values, anchor="", unit=""):
            return per_service.get(code, [])

        with mock.patch(f"{px}.service_for", return_value=["AmazonRDS"]), \
             mock.patch(f"{px}.service_codes", return_value=sorted(per_service)), \
             mock.patch(f"{px}.value_universe", return_value={"dbt3micro", "available", "region"}), \
             mock.patch(f"{px}.unit_vocabulary", return_value=set()), \
             mock.patch(f"{px}.find_sku_by_coverage", side_effect=coverage), \
             mock.patch(f"{px}.find_sku_by_words", return_value=[]), \
             mock.patch(f"{px}.ondemand_only", side_effect=lambda m, **k: m), \
             mock.patch(f"{px}.resolve_one", side_effect=lambda m, _w: m):
            return ct.price_resource_universal(resource or _DB)

    def test_two_matched_words_beat_a_hundred_single_word_strangers(self):
        strangers = {f"Amazon{n}": [_sku(f"{n}-Usage", 0.5 + i, 1)]
                     for i, n in enumerate(("Pinpoint", "MediaLive", "Chime", "ElastiCache"))}
        got = self._price({"AmazonRDS": [_sku("InstanceUsage:db.t3.micro", 0.018, 2)], **strangers})
        self.assertTrue(got.get("priced"), got)
        self.assertEqual(got["usagetype"], "InstanceUsage:db.t3.micro")

    def test_a_best_of_one_is_not_discriminating_and_changes_nothing(self):
        """A resource identified by exactly ONE word must be left to the rules below. An allocated
        Elastic IP says only `domain: vpc`, matches unrelated SKUs at coverage 1, and is resolved by the
        noun-anchored search -- a coverage filter here would throw those candidates away."""
        # The resource's own service offers nothing, so the whole-disclosure sweep runs: exactly the
        # Elastic IP's situation, where the SKUs that price it are reached by the noun, not by a value.
        got = self._price({"AmazonRDS": [],
                           "AmazonOther": [_sku("A-Usage", 0.01, 1)],
                           "AmazonThird": [_sku("B-Usage", 0.02, 1)]})
        self.assertFalse(got.get("priced"))
        self.assertIn("ambiguous: 2", got["reason"],
                      "a best coverage of 1 must not filter anything away")

    def test_a_tie_at_the_top_is_still_a_tie(self):
        got = self._price({"AmazonRDS": [_sku("InstanceUsage:db.t3.micro", 0.018, 2),
                                         _sku("RDS:GP2-Storage", 0.115, 2, unit="GB-Mo")],
                           "AmazonNoise": [_sku("Noise-Usage", 9.0, 1)]})
        self.assertFalse(got.get("priced"))
        self.assertIn("ambiguous: 2", got["reason"], "the coverage-1 stranger must be gone")

    def test_the_units_then_separate_a_parent_from_its_child(self):
        """The tie above is exactly the live RDS case, and `exclude_unit` settles it: the storage child
        took the per-GB line, so the instance cannot also be priced by size."""
        got = self._price({"AmazonRDS": [_sku("InstanceUsage:db.t3.micro", 0.018, 2),
                                         _sku("RDS:GP2-Storage", 0.115, 2, unit="GB-Mo")]},
                          resource={**_DB, "exclude_unit": "GB"})
        self.assertTrue(got.get("priced"), got)
        self.assertEqual(got["usagetype"], "InstanceUsage:db.t3.micro")


if __name__ == "__main__":
    unittest.main()
