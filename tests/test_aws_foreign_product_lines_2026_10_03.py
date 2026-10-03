"""A resource is priced only from its OWN product, and a per-event unit has no standing hourly rate (2026-10-03).

The 23 Sep review found 63 of the 261 published AWS cost lines (26 of 32 records) priced from a product that bills FOR a
resource rather than the resource: VPC Network Access Analyzer per network interface assessed, IoT positioning per
position solved (a target group), DevOps Guru per analysed CloudFront distribution or S3 bucket per hour. None was used.
Removing them moves no printed cost coordinate or frontier result; reading the 14 DevOps Guru lines' unit as hourly
without the product rule would add real charges that do not exist, so the product rule is the fix and the unit rule only
its guard. The VPC API is logged under ec2, so an AmazonVPC line stays an ec2 resource's own product. A refused line
keeps its reason in no_sku_match.
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
        self.assertIsNone(ct._standing_hourly("Requests", 0.4))
        self.assertEqual(ct._standing_hourly("ACU-Hr", 0.12), 0.12)              # Aurora Serverless v2: hourly


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


PX = "acspeed.adapters.aws_price_index"


def _price(resource, own, own_hits, other, other_hits):
    """Price `resource` whose own service `own` offers `own_hits` and the sweep's other service `other` offers
    `other_hits`."""
    def coverage(code, _region, _values, anchor="", unit=""):
        return own_hits if code == own else other_hits
    with mock.patch(f"{PX}.service_for", return_value=[own]), \
         mock.patch(f"{PX}.service_codes", return_value=[own, other]), \
         mock.patch(f"{PX}.value_universe", return_value=set()), \
         mock.patch(f"{PX}.unit_vocabulary", return_value=set()), \
         mock.patch(f"{PX}.find_sku_by_coverage", side_effect=coverage), \
         mock.patch(f"{PX}.find_sku_by_words", return_value=[]), \
         mock.patch(f"{PX}.ondemand_only", side_effect=lambda m, **k: m), \
         mock.patch(f"{PX}.resolve_one", side_effect=lambda m, _w: m):
        return ct.price_resource_universal(resource)


class AtThePointOfUse(unittest.TestCase):
    """Both rules pinned where a price is computed, not only in the helpers."""

    def test_devops_guru_distribution_is_not_priced(self):
        cf = {"kind": "distribution", "event": "CreateDistribution", "region": "us-east-1", "src": "cloudfront",
              "params": {"priceClass": "PriceClass_All"}}
        got = _price(cf, "AmazonCloudFront", [], "AmazonDevOpsGuru",
                     [_sku("USE1-CloudFront-Distribution-GroupB", 0.0042, "usagehours")])
        self.assertFalse(got["priced"], got)

    def test_per_event_unit_in_own_service_has_no_standing_rate(self):
        tg = {"kind": "targetgroup", "event": "CreateTargetGroup", "region": "us-east-1",
              "src": "elasticloadbalancing", "params": {"protocol": "HTTP"}}
        got = _price(tg, "AWSELB", [_sku("USE1-TargetGroup-Requests", 0.4, "Requests")], "AWSIoT", [])
        self.assertFalse(got["priced"], got)
        self.assertIn("no standing hourly rate", got.get("reason", ""))

    def test_usagehours_in_own_service_is_hourly(self):
        tg = {"kind": "targetgroup", "event": "CreateTargetGroup", "region": "us-east-1",
              "src": "elasticloadbalancing", "params": {"protocol": "HTTP"}}
        got = _price(tg, "AWSELB", [_sku("USE1-Thing-Group", 0.0042, "usagehours")], "AWSIoT", [])
        self.assertTrue(got["priced"], got)
        self.assertEqual(got["hourly_usd"], 0.0042)

    def test_load_balancer_public_ipv4_stays_priced(self):
        part = {"kind": "elastic_ip", "part": "public-ipv4", "event": "", "region": "us-east-1",
                "src": "elasticloadbalancing", "params": {"usagetype": "USE1-PublicIPv4:InUseAddress"}}
        got = _price(part, "AWSELB", [], "AmazonVPC", [_sku("USE1-PublicIPv4:InUseAddress", 0.005, "Hrs")])
        self.assertTrue(got["priced"], got)
        self.assertEqual(got["hourly_usd"], 0.005)

    def test_vpc_endpoint_from_ec2_is_priced_from_amazonvpc(self):
        ep = {"kind": "vpcendpoint", "event": "CreateVpcEndpoint", "region": "us-east-1", "src": "ec2",
              "params": {"vpcEndpointType": "Interface"}}
        got = _price(ep, "AmazonEC2", [], "AmazonVPC", [_sku("USE1-VpcEndpoint-Hours", 0.01, "Hrs")])
        self.assertTrue(got["priced"], got)
        self.assertEqual(got["hourly_usd"], 0.01)

    def test_amazonvpc_line_is_not_another_service_s_own(self):
        tg = {"kind": "targetgroup", "event": "CreateTargetGroup", "region": "us-east-1",
              "src": "elasticloadbalancing", "params": {"protocol": "HTTP"}}
        got = _price(tg, "AWSELB", [], "AmazonVPC", [_sku("USE1-VpcEndpoint-Hours", 0.01, "Hrs")])
        self.assertFalse(got["priced"], got)

    def test_eni_assessment_from_amazonvpc_is_still_refused(self):
        eni = {"kind": "networkinterface", "event": "CreateNetworkInterface", "region": "us-east-1", "src": "ec2",
               "params": {"interfaceType": "interface"}}
        got = _price(eni, "AmazonEC2", [], "AmazonVPC",
                     [_sku("USE1-NetworkInterface-Assessment", 0.002, "NetworkInterface-Assessment")])
        self.assertFalse(got["priced"], got)
        self.assertIn("no standing hourly rate", got.get("reason", ""))


class RefusalIsRecorded(unittest.TestCase):
    def test_refused_line_keeps_its_reason_in_no_sku_match(self):
        res = {"kind": "targetgroup", "event": "CreateTargetGroup", "region": "us-east-1",
               "src": "elasticloadbalancing", "params": {}}
        refused = {"priced": False, "reason": "USE1-Solved-IP-Positions (AWSIoT) is not this resource's own line"}
        with mock.patch.object(ct, "discover", return_value=([res], [])), \
             mock.patch.object(ct, "collapse_task_definitions", side_effect=lambda r: (r, [])), \
             mock.patch.object(ct, "billable_parts", side_effect=lambda r: [dict(r, part="self")]), \
             mock.patch.object(ct, "price_resource_universal", return_value=refused):
            d = ct.run_rate_universal("tok", "2026-10-03", regions=["us-east-1"], describe=lambda r: {})
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["no_sku_match"]), 1)
        self.assertIn("is not this resource's own line", d["no_sku_match"][0])


if __name__ == "__main__":
    unittest.main()
