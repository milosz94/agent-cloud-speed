"""An ambiguous match is only a WARNING when the resource's own service is even a candidate.

`ok` is false when a resource came out ambiguous, and that is right: the disclosure holds a price we
could not pin, so the number must not be published. But the whole-disclosure sweep asks 269 services,
and a FREE resource can pick up candidates from services that had nothing to do with it. Measured on an
ALB target group: its words ("HTTP", "instance", a health-check path) match ZERO of the 14 SKUs in
AWSELB, the service that created it, while matching thirteen in unrelated services. Treating that as a
missing price made `ok` FALSE on every run containing a load balancer, which would have failed a paid
run over a resource that costs nothing.

This is not a rule that a SKU must live in the service that emitted the event: a public IPv4 is created
by ec2 and billed by AmazonVPC, and that resolves to exactly one SKU, so it never reaches this triage.
"""
import unittest
from unittest import mock

from acspeed.adapters import aws_ct_cost as ct


def _sku(usagetype, usd):
    return {"sku": usagetype, "usagetype": usagetype, "operation": "",
            "attributes": {"usagetype": usagetype},
            "prices": [{"unit": "Hrs", "usd": usd, "description": usagetype}]}


_TARGET_GROUP = {"kind": "targetgroup", "event": "CreateTargetGroup", "region": "us-east-1",
                 "src": "elasticloadbalancing",
                 "params": {"protocol": "HTTP", "healthCheckPath": "/healthz"}}


class TestAmbiguityFromAStrangerIsNotAMissingPrice(unittest.TestCase):

    def _price(self, own_service_hits, other_service_hits):
        px = "acspeed.adapters.aws_price_index"

        def coverage(code, _region, _values, anchor="", unit=""):
            return own_service_hits if code == "AWSELB" else other_service_hits

        with mock.patch(f"{px}.service_for", return_value=["AWSELB"]), \
             mock.patch(f"{px}.service_codes", return_value=["AWSELB", "AmazonStranger"]), \
             mock.patch(f"{px}.value_universe", return_value={"http", "healthz"}), \
             mock.patch(f"{px}.unit_vocabulary", return_value=set()), \
             mock.patch(f"{px}.find_sku_by_coverage", side_effect=coverage), \
             mock.patch(f"{px}.find_sku_by_words", return_value=[]), \
             mock.patch(f"{px}.ondemand_only", side_effect=lambda m, **k: m), \
             mock.patch(f"{px}.resolve_one", side_effect=lambda m, _w: m):
            return ct.price_resource_universal(_TARGET_GROUP)

    def test_candidates_only_from_other_services_are_flagged(self):
        got = self._price([], [_sku("Unrelated-A", 0.01), _sku("Unrelated-B", 0.02)])
        self.assertFalse(got["priced"])
        self.assertFalse(got["from_own_service"])
        self.assertIn("none from the service that created it", got["reason"])

    def test_candidates_from_the_resources_own_service_still_block(self):
        """The guard must not swallow a REAL ambiguity: if the service that created the resource
        publishes several candidate prices, we genuinely cannot pin it and must not publish."""
        got = self._price([_sku("LoadBalancerUsage", 0.0225), _sku("LCUUsage", 0.008)], [])
        self.assertFalse(got["priced"])
        self.assertTrue(got["from_own_service"])
        self.assertNotIn("none from the service", got["reason"])

    def test_only_an_ambiguous_result_is_triaged_at_all(self):
        """A single candidate is a PRICE, wherever it is published: a public IPv4 is created by ec2 and
        billed by AmazonVPC. The triage must never turn that into a refusal."""
        got = self._price([], [_sku("USE1-PublicIPv4:InUseAddress", 0.005)])
        self.assertTrue(got["priced"], got)
        self.assertEqual(got["usagetype"], "USE1-PublicIPv4:InUseAddress")


if __name__ == "__main__":
    unittest.main()
