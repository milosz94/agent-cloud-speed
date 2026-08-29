"""Usage-metered cost reported as a per-usage SCHEDULE (cost.py UsageRate + the AWS serverless-front
path in aws_runrate.py). A CloudFront/App Runner/Lambda deploy has no standing $/hr, so we price it over a
fixed request grid ("10k -> $A, 1M -> $B"). Mocked against the Price List response shape."""
import json
import unittest

from acspeed.cost import (UsageComponent, compose_usage_rate, REFERENCE_REQUEST_GRID,
                          HOURS_PER_MONTH, _fmt_requests, DEFAULT_REQUEST_VCPUS, DEFAULT_REQUEST_SECONDS)
from acspeed.adapters.aws_runrate import (AwsRunRateAdapter, _usage_front, _classify_usage_unit,
                                          _usage_components, _usage_rate)


class TestComposeUsageRate(unittest.TestCase):
    def test_requests_scale_linearly(self):
        comps = [UsageComponent("requests", per_unit_usd=1e-6, unit="per request", driver="requests")]
        ur = compose_usage_rate(comps, provider="aws", region="us-east-1", service="cloudfront",
                                capture_date="2026-08-27")
        self.assertEqual(ur.to_dict()["kind"], "usage")
        # cost at each grid point = requests * 1e-6 (no floor, no egress)
        self.assertAlmostEqual(ur.monthly_at(10_000), round(10_000 * 1e-6, 4))
        self.assertAlmostEqual(ur.monthly_at(1_000_000), round(1_000_000 * 1e-6, 4))
        # strictly increasing across the grid
        vals = [p.usd_per_month for p in ur.schedule]
        self.assertEqual(vals, sorted(vals))
        self.assertEqual([p.label for p in ur.schedule],
                         [_fmt_requests(r) for r in REFERENCE_REQUEST_GRID])

    def test_egress_derived_from_requests(self):
        comps = [UsageComponent("egress", per_unit_usd=0.085, unit="per GB out", driver="egress")]
        ur = compose_usage_rate(comps, provider="aws", region="us-east-1", service="cloudfront",
                                capture_date="2026-08-27", avg_response_kb=50.0)
        egress_gb = 1_000_000 * 50.0 / (1024.0 * 1024.0)
        self.assertAlmostEqual(ur.monthly_at(1_000_000), round(0.085 * egress_gb, 4))

    def test_standing_floor_flat_and_active_compute_folded(self):
        comps = [
            UsageComponent("mem", per_unit_usd=0.008, unit="GB-hour", driver="standing"),  # $/hr floor
            UsageComponent("vcpu", per_unit_usd=0.064, unit="vCPU-second", driver="other"),  # folded per request
        ]
        ur = compose_usage_rate(comps, provider="aws", region="us-east-1", service="apprunner",
                                capture_date="2026-08-27")
        floor = 0.008 * HOURS_PER_MONTH
        # active compute (driver 'other') is now FOLDED into the per-request cost, using the disclosed
        # profile (1 vCPU held DEFAULT_REQUEST_SECONDS per request), so the schedule is NOT flat.
        per_req = 0.064 * DEFAULT_REQUEST_VCPUS * DEFAULT_REQUEST_SECONDS
        for p in ur.schedule:
            self.assertAlmostEqual(p.usd_per_month, round(floor + per_req * p.requests_per_month, 4), places=3)
        self.assertGreater(ur.schedule[-1].usd_per_month, ur.schedule[0].usd_per_month)  # high > idle
        self.assertIn("vcpu", [c.name for c in ur.components])   # still disclosed as a component

    def test_negative_rate_rejected(self):
        with self.assertRaises(ValueError):
            UsageComponent("x", per_unit_usd=-1.0, unit="per request", driver="requests")


class TestUnitClassifier(unittest.TestCase):
    def test_drivers(self):
        self.assertEqual(_classify_usage_unit("Requests", "Request", "")[0], "requests")
        self.assertEqual(_classify_usage_unit("GB", "Data Transfer", "DataTransfer-Out-Bytes")[0], "egress")
        self.assertEqual(_classify_usage_unit("vCPU-Hours", "AWS App Runner", "")[0], "other")
        self.assertEqual(_classify_usage_unit("GB-Hours", "AWS App Runner", "")[0], "standing")
        self.assertEqual(_classify_usage_unit("GB-Seconds", "AWS Lambda", "")[0], "other")
        self.assertEqual(_classify_usage_unit("Quantity", "Something", "")[0], None)


class TestFrontDetection(unittest.TestCase):
    def test_hosts(self):
        self.assertEqual(_usage_front("d1r3bmffwg4u8o.cloudfront.net")[1], "cloudfront")
        self.assertEqual(_usage_front("yyptcvqewm.us-east-1.awsapprunner.com")[1], "apprunner")
        self.assertEqual(_usage_front("abc.execute-api.us-east-1.amazonaws.com")[1], "apigateway")
        self.assertEqual(_usage_front("ec2-1-2-3-4.compute-1.amazonaws.com")[0], None)


def _cloudfront_products():
    def prod(pf, unit, usd, desc=""):
        return json.dumps({
            "product": {"productFamily": pf, "attributes": {"location": "US East (N. Virginia)"}},
            "terms": {"OnDemand": {"T": {"priceDimensions": {"R": {
                "unit": unit, "description": desc, "pricePerUnit": {"USD": f"{usd:.10f}"}}}}}}})
    return {"PriceList": [
        prod("Request", "Requests", 1.0e-6, "HTTPS requests"),
        prod("Data Transfer", "GB", 0.085, "DataTransfer-Out-Bytes"),
        prod("Something Else", "Quantity", 5.0, "not a usage meter"),   # ignored
    ]}


class TestAwsUsageRate(unittest.TestCase):
    def test_cloudfront_builds_rising_schedule(self):
        def call(cli):
            self.assertIn("pricing get-products", cli)
            self.assertIn("AmazonCloudFront", cli)
            return _cloudfront_products()
        ur = _usage_rate(call, "https://d1r3.cloudfront.net", "us-east-1", "2026-08-27",
                         "AmazonCloudFront", "cloudfront")
        self.assertIsNotNone(ur)
        drivers = sorted(c.driver for c in ur.components)
        self.assertEqual(drivers, ["egress", "requests"])
        vals = [p.usd_per_month for p in ur.schedule]
        self.assertEqual(vals, sorted(vals))            # rises with requests
        self.assertGreater(vals[-1], vals[0])

    def test_adapter_routes_cloudfront_to_usage(self):
        adapter = AwsRunRateAdapter(mcp_call=lambda cli: _cloudfront_products())
        rr = adapter.run_rate("https://d1r3bmffwg4u8o.cloudfront.net", capture_date="2026-08-27")
        self.assertIsNotNone(rr)
        self.assertEqual(rr.to_dict()["kind"], "usage")

    def test_no_priceable_dimension_returns_none_disclosed(self):
        empty = {"PriceList": [json.dumps({"product": {"productFamily": "X", "attributes": {}},
                 "terms": {"OnDemand": {"T": {"priceDimensions": {"R": {
                     "unit": "Quantity", "pricePerUnit": {"USD": "1.0"}}}}}}})]}
        ur = _usage_rate(lambda cli: empty, "https://x.cloudfront.net", "us-east-1", "2026-08-27",
                         "AmazonCloudFront", "cloudfront")
        self.assertIsNone(ur)                            # nothing classifiable -> disclosed, not faked

    def test_apprunner_without_compute_is_unpriced_not_partial(self):
        """Completeness guard: App Runner is a compute service, so a schedule that priced only its request
        line (compute meter missing) would understate the bill. It must return None (UNPRICED), never a
        request-fee-only partial. CloudFront, a CDN, has no compute and is unaffected (tested above)."""
        def prod(pf, unit, usd, desc=""):
            return json.dumps({"product": {"productFamily": pf, "attributes": {"location": "US East (N. Virginia)"}},
                "terms": {"OnDemand": {"T": {"priceDimensions": {"R": {
                    "unit": unit, "description": desc, "pricePerUnit": {"USD": f"{usd:.10f}"}}}}}}})
        requests_only = {"PriceList": [prod("AWS App Runner", "Requests", 1.0e-6, "requests")]}
        self.assertIsNone(_usage_rate(lambda cli: requests_only, "https://x.us-east-1.awsapprunner.com",
                                      "us-east-1", "2026-08-27", "AWSAppRunner", "apprunner"))
        with_compute = {"PriceList": [prod("AWS App Runner", "Requests", 1.0e-6, "requests"),
                                      prod("AWS App Runner", "vCPU-Hours", 0.064, "provisioned vCPU")]}
        self.assertIsNotNone(_usage_rate(lambda cli: with_compute, "https://x.us-east-1.awsapprunner.com",
                                         "us-east-1", "2026-08-27", "AWSAppRunner", "apprunner"))


if __name__ == "__main__":
    unittest.main()
