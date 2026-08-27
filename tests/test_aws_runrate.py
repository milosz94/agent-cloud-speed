"""General AWS run-rate via the aws MCP (acspeed/adapters/aws_runrate.py): uniform-inventory enumerate +
Price-List dimension pricing (no per-service price code), with Lightsail as the one live-priced exception.
Mocked against the real CLI response shapes."""
import json
import unittest

from acspeed.adapters.aws_runrate import (AwsRunRateAdapter, _region_from_url, _app_name, _unwrap,
                                          _general_run_rate, _lightsail_run_rate, HOURS_PER_MONTH)

_LS_URL = "https://it-tools.w4a4v7yp5swqw.us-east-1.cs.amazonlightsail.com"
_POWERS = {"powers": [{"powerId": "small-1", "price": 15.0, "name": "small"},
                      {"powerId": "large-1", "price": 80.0, "name": "large"}]}


def _ls_mcp(power="small", power_id="small-1", scale=1, name="it-tools", url=_LS_URL + "/"):
    def call(cli):
        if "get-container-services" in cli:
            return {"containerServices": [{"containerServiceName": name, "power": power, "powerId": power_id,
                                           "scale": scale, "state": "RUNNING", "url": url}]}
        if "get-container-service-powers" in cli:
            return _POWERS
        return {}
    return call


class TestLightsailException(unittest.TestCase):
    def test_priced_from_live_power_price(self):
        rr = _lightsail_run_rate(_ls_mcp("small", "small-1", 1), _LS_URL, "us-east-1", "2026-08-27")
        self.assertIsNotNone(rr)
        self.assertEqual(rr.provider, "aws")
        self.assertEqual([c.name for c in rr.components], ["compute:lightsail-container"])
        self.assertAlmostEqual(rr.all_in_hourly_usd, round(15.0 / HOURS_PER_MONTH, 6))
        self.assertAlmostEqual(rr.monthly_usd(), 15.0, places=2)
        self.assertIn("Lightsail", rr.price_source)

    def test_scale_and_power(self):
        rr = _lightsail_run_rate(_ls_mcp("large", "large-1", 3), _LS_URL, "us-east-1", "2026-08-27")
        self.assertAlmostEqual(rr.all_in_hourly_usd, round(80.0 * 3 / HOURS_PER_MONTH, 6))
        self.assertEqual(rr.flavor, "lightsail-container:largex3")

    def test_unmatched_none(self):
        rr = _lightsail_run_rate(_ls_mcp(name="other", url="https://other.x.us-east-1.cs.amazonlightsail.com/"),
                                 _LS_URL, "us-east-1", "2026-08-27")
        self.assertIsNone(rr)

    def test_adapter_routes_lightsail(self):
        rr = AwsRunRateAdapter(mcp_call=_ls_mcp()).run_rate(_LS_URL, capture_date="2026-08-27")
        self.assertIsNotNone(rr)
        self.assertEqual(rr.region, "us-east-1")


class TestGeneralPath(unittest.TestCase):
    """The uniform RGT -> Cloud Control -> Price List pipeline (reuses aws_cost's dimension engine). Attrs are
    injected already in Price-List keys; the CFN-property -> Price-List-attr name bridge is the live-hardening
    seam noted in the module, exercised on the first real EC2 deploy."""

    def _mcp(self, price_usd=0.096):
        arn = "arn:aws:ec2:us-east-1:123:instance/i-abc"

        def call(cli):
            if "resourcegroupstaggingapi get-resources" in cli:
                return {"ResourceTagMappingList": [{"ResourceARN": arn,
                                                    "Tags": [{"Key": "Name", "Value": "it-tools"}]}]}
            if "cloudcontrol get-resource" in cli:
                return {"ResourceDescription": {"Properties": json.dumps(
                    {"instanceType": "m5.large", "operatingSystem": "Linux"})}}
            if "pricing get-products" in cli:
                sku = "SKU1"
                term = f"{sku}.JRTCKXETXF"
                rate = f"{term}.6YS6EN2CT7"
                prod = {"product": {"productFamily": "Compute Instance",
                                    "attributes": {"instanceType": "m5.large"}, "sku": sku},
                        "serviceCode": "AmazonEC2",
                        "terms": {"OnDemand": {term: {"priceDimensions": {rate: {
                            "unit": "Hrs", "beginRange": "0", "endRange": "Inf",
                            "pricePerUnit": {"USD": f"{price_usd:.10f}"}, "rateCode": rate}},
                            "sku": sku, "offerTermCode": "JRTCKXETXF"}}}}
                return {"FormatVersion": "aws_v1", "PriceList": [json.dumps(prod)]}
            return {}
        return call

    def test_enumerates_and_prices_via_pricelist_by_dimension(self):
        # app name "it-tools" is carried by the front's host label and matches the resource's Name tag; the
        # ip-based EC2 public-DNS case (label != app name) is the enumeration-matching seam hardened live.
        rr = _general_run_rate(self._mcp(0.096), "https://it-tools.us-east-1.elb.amazonaws.com",
                               "us-east-1", "2026-08-27")
        self.assertIsNotNone(rr)
        self.assertEqual(rr.provider, "aws")
        self.assertEqual([c.name for c in rr.components], ["compute"])   # EC2 instance-hour dimension
        self.assertAlmostEqual(rr.all_in_hourly_usd, round(0.096, 6))

    def test_no_matching_resources_returns_none_disclosed(self):
        def empty(cli):
            if "get-resources" in cli:
                return {"ResourceTagMappingList": []}
            return {}
        rr = _general_run_rate(empty, "https://ec2-1-2-3-4.compute-1.amazonaws.com", "us-east-1", "2026-08-27")
        self.assertIsNone(rr)                             # nothing enumerated -> disclosed, not faked


class TestHelpers(unittest.TestCase):
    def test_region_and_app_name(self):
        self.assertEqual(_region_from_url(_LS_URL), "us-east-1")
        self.assertEqual(_app_name(_LS_URL), "it-tools")
        self.assertEqual(_region_from_url("https://x.y.eu-west-2.compute.amazonaws.com"), "eu-west-2")

    def test_unwrap_and_error(self):
        env = {"content": [{"type": "text", "text": json.dumps({"a": 1})}], "isError": False}
        self.assertEqual(_unwrap(env), {"a": 1})
        with self.assertRaises(RuntimeError):
            _unwrap({"content": [], "isError": True})


if __name__ == "__main__":
    unittest.main()
