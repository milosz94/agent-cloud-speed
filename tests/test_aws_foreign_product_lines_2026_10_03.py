"""A resource is priced only from its OWN product, and a per-event unit has no standing hourly rate (2026-10-03).

The 23 Sep review found 63 of the 261 published AWS cost lines (26 of 32 records) priced from a product that bills FOR a
resource rather than the resource: VPC Network Access Analyzer per network interface assessed, IoT positioning per
position solved (a target group), DevOps Guru per analysed CloudFront distribution or S3 bucket per hour. None was used.
Removing them changes no printed figure of the paper; reading their units as hourly without the product rule would add
real charges that do not exist, so the product rule is the fix and the unit rule only its guard.
"""
import unittest
from unittest import mock

from acspeed.adapters import aws_ct_cost as ct


def _sku(usagetype, usd, unit):
    return {"sku": usagetype, "usagetype": usagetype, "prices": [{"usd": usd, "unit": unit}]}


class StandingHourly(unittest.TestCase):
    def test_units(self):
        self.assertEqual(ct._standing_hourly("Hrs", 0.5), 0.5)
        self.assertEqual(ct._standing_hourly("usagehours", 0.0042), 0.0042)     # an hourly unit, not monthly
        self.assertAlmostEqual(ct._standing_hourly("GB-Mo", 7.3), 0.01)
        self.assertIsNone(ct._standing_hourly("Position", 0.001))              # per event: no run-rate
        self.assertIsNone(ct._standing_hourly("NetworkInterface-Assessment", 0.002))


class ForeignProduct(unittest.TestCase):
    def _price(self, resource, other_service_hits):
        px = "acspeed.adapters.aws_price_index"

        def coverage(code, _region, _values, anchor="", unit=""):
            return [] if code == "AWSELB" else other_service_hits

        with mock.patch(f"{px}.service_for", return_value=["AWSELB"]), \
             mock.patch(f"{px}.service_codes", return_value=["AWSELB", "AWSIoT"]), \
             mock.patch(f"{px}.value_universe", return_value={"http", "healthz"}), \
             mock.patch(f"{px}.unit_vocabulary", return_value=set()), \
             mock.patch(f"{px}.find_sku_by_coverage", side_effect=coverage), \
             mock.patch(f"{px}.find_sku_by_words", return_value=[]), \
             mock.patch(f"{px}.ondemand_only", side_effect=lambda m, **k: m), \
             mock.patch(f"{px}.resolve_one", side_effect=lambda m, _w: m):
            return ct.price_resource_universal(resource)

    def test_target_group_is_not_priced_from_iot_positioning(self):
        tg = {"kind": "targetgroup", "event": "CreateTargetGroup", "region": "us-east-1", "src": "elasticloadbalancing",
              "params": {"protocol": "HTTP", "healthCheckPath": "/healthz"}}
        got = self._price(tg, [_sku("USE1-Solved-IP-Positions", 0.001, "Position")])
        self.assertFalse(got["priced"], got)
        self.assertIn("not this resource's own line", got.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
