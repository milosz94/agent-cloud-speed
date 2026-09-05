"""Three charges that were on the real bill and produced no number at all.

Measured on account 776638915601 for 2026-09-05: `USE1-PublicIPv4:InUseAddress` ($0.0778),
`RDS:GP3-Storage` and the EBS root volume were all billed, and all three came out of the pricer as
either "ambiguous" or nothing. None of them was a pricing error in the sense of a wrong number; each
was a resource whose price the lookup declined to reach.
"""
import unittest
from unittest import mock

from acspeed.adapters import aws_ct_cost as ct


_ALB = {"kind": "loadbalancer", "event": "CreateLoadBalancer", "region": "us-east-1",
        "src": "elasticloadbalancing",
        "params": {"scheme": "internet-facing", "type": "application",
                   "subnets": ["subnet-0a1b2c3d4e5f60718", "subnet-1a1b2c3d4e5f60718"]}}

_EC2 = {"kind": "instances", "event": "RunInstances", "region": "us-east-1", "src": "ec2",
        "params": {"instanceType": "t3.medium",
                   "blockDeviceMapping": {"items": [
                       {"deviceName": "/dev/xvda",
                        "ebs": {"volumeSize": 40, "volumeType": "gp3"}}]}}}


class TestAChildPartIsNotItsParent(unittest.TestCase):

    def test_a_part_does_not_inherit_the_parent_event(self):
        """The public-IPv4 part kept `event: CreateLoadBalancer`, so its SKU search was anchored on the
        noun "LoadBalancer" and matched the load balancer's OWN SKU alongside the address. Two candidates
        at different prices do not collapse, so the address came out ambiguous and unpriced."""
        parts = {p.get("part"): p for p in ct.billable_parts(_ALB)}
        self.assertIn("public-ipv4", parts)
        self.assertEqual(parts["public-ipv4"].get("event"), "")
        self.assertEqual(parts["self"].get("event"), "CreateLoadBalancer")

    def test_one_public_address_per_subnet(self):
        parts = {p.get("part"): p for p in ct.billable_parts(_ALB)}
        self.assertEqual(parts["public-ipv4"]["quantity"], 2.0)

    def test_the_condition_is_the_scheme_not_the_kind(self):
        """It also required kind == "load_balancer" while kinds come from `_kind_of`, which yields
        "loadbalancer". The branch never ran, so no run ever counted a public IPv4."""
        internal = {**_ALB, "params": {**_ALB["params"], "scheme": "internal"}}
        self.assertNotIn("public-ipv4", [p.get("part") for p in ct.billable_parts(internal)])
        odd_kind = {**_ALB, "kind": "somethingelse"}
        self.assertIn("public-ipv4", [p.get("part") for p in ct.billable_parts(odd_kind)])

    def test_a_sized_child_carries_the_unit_of_its_quantity(self):
        parts = [p for p in ct.billable_parts(_EC2) if p.get("kind") == "storage"]
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["quantity"], 40.0)
        self.assertEqual(parts[0]["unit_hint"], "GB")


class TestTheHeadNounIsWhatANameDenotes(unittest.TestCase):
    """English compound nouns are head-final, so the last word is what the name means. This is the only
    thing separating `VPCPublicIPv4Address` (an address) from `AWSVPCIPAddressManager` (a manager), both
    of which contain the word "address"."""

    def test_the_resources_own_head_noun(self):
        self.assertEqual(ct._head_noun({"event": "AllocateAddress"}), "address")
        self.assertEqual(ct._head_noun({"event": "CreateRelationalDatabase"}), "database")
        self.assertEqual(ct._head_noun({"event": ""}), "")

    def test_a_sku_that_merely_mentions_the_word_is_not_about_it(self):
        addr = ct._name_heads({"usagetype": "USE1-PublicIPv4:InUseAddress",
                               "group": "VPCPublicIPv4Address"})
        ipam = ct._name_heads({"usagetype": "USE1-IPAddressManager-IP-Hours",
                               "group": "AWSVPCIPAddressManager", "operation": "IPAM-Active-IP"})
        self.assertIn("address", addr)
        self.assertNotIn("address", ipam, "IPAddressManager denotes a manager, not an address")
        self.assertIn("manager", ipam)


class TestTheUnitOfAQuantityPicksTheLine(unittest.TestCase):
    """gp3 publishes three SKUs under one name: EBS:VolumeP-IOPS.gp3 (IOPS-Mo),
    EBS:VolumeP-Throughput.gp3 (GiBps-mo) and EBS:VolumeUsage.gp3 (GB-Mo). A 40 GB volume can only be
    priced by the per-GB line."""

    def _sku(self, ut, unit, usd):
        return {"sku": ut, "usagetype": ut, "attributes": {"usagetype": ut, "volumeApiName": "gp3"},
                "prices": [{"unit": unit, "usd": usd, "description": ut}]}

    def test_only_the_matching_unit_survives(self):
        cands = [self._sku("EBS:VolumeP-IOPS.gp3", "IOPS-Mo", 0.005),
                 self._sku("EBS:VolumeP-Throughput.gp3", "GiBps-mo", 40.96),
                 self._sku("EBS:VolumeUsage.gp3", "GB-Mo", 0.08)]
        part = {"kind": "storage", "event": "", "region": "us-east-1", "src": "ec2",
                "quantity": 40.0, "unit_hint": "GB",
                "params": {"volumeApiName": "gp3", "volumeType": "gp3"}}
        with mock.patch("acspeed.adapters.aws_price_index.find_sku_by_coverage", return_value=cands), \
             mock.patch("acspeed.adapters.aws_price_index.ondemand_only", side_effect=lambda m, **k: m), \
             mock.patch("acspeed.adapters.aws_price_index.service_for", return_value=["AmazonEC2"]), \
             mock.patch("acspeed.adapters.aws_price_index.value_universe", return_value={"gp3"}), \
             mock.patch("acspeed.adapters.aws_price_index.unit_vocabulary", return_value={"gb"}):
            got = ct.price_resource_universal(part)
        self.assertTrue(got.get("priced"), got)
        self.assertEqual(got["usagetype"], "EBS:VolumeUsage.gp3")
        self.assertEqual(got["unit"], "GB-Mo")

    def test_giBps_is_not_gb(self):
        """A prefix compare that folded GiBps into GB would pick the $40.96 line over the $0.08 one."""
        self.assertFalse("gibps-mo".startswith("gb"))


if __name__ == "__main__":
    unittest.main()
