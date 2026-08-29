"""Regression for the 2026-08-29 AWS cost bugs found on a live umami deploy:
  1. RDS priced $0.50/hr (real $0.016): the pricer filtered on price-list field names (instanceType,
     databaseEngine, deploymentOption) but the resource attrs were raw Cloud Control props
     (DBInstanceClass, Engine, MultiAZ) -> no filter matched -> wrong SKU. Fixed by _normalize_attrs.
  2. Fargate app compute (ECS service) missing: not a pricing dimension, and its Price List unit is
     "hours" (lowercase) which the pricer's `== "Hrs"` check rejected. Fixed by a Fargate dimension +
     a case-insensitive hourly-unit check.
"""
import unittest

from acspeed.adapters.aws_runrate import _normalize_attrs
from acspeed.adapters import aws_cost


class TestNormalizeAttrs(unittest.TestCase):
    def test_rds_props_map_to_pricelist_fields(self):
        attrs, qty = _normalize_attrs("rds", "db", {
            "DBInstanceClass": "db.t4g.micro", "Engine": "postgres", "MultiAZ": False,
            "AllocatedStorage": 20, "StorageType": "gp3"})
        self.assertEqual(attrs["instanceType"], "db.t4g.micro")       # was the whole bug: never set
        self.assertEqual(attrs["databaseEngine"], "PostgreSQL")       # postgres -> PostgreSQL
        self.assertEqual(attrs["deploymentOption"], "Single-AZ")      # MultiAZ False
        self.assertEqual(qty["gb"], 20.0)                             # amount goes in quantity, not attrs

    def test_multi_az_maps_to_multi_az(self):
        attrs, _ = _normalize_attrs("rds", "db", {"DBInstanceClass": "db.m5.large", "Engine": "mysql", "MultiAZ": True})
        self.assertEqual(attrs["deploymentOption"], "Multi-AZ")
        self.assertEqual(attrs["databaseEngine"], "MySQL")

    def test_ec2_instance(self):
        attrs, _ = _normalize_attrs("ec2", "instance", {"InstanceType": "t3.small"})
        self.assertEqual(attrs["instanceType"], "t3.small")


def _sku(price, unit):
    return {"terms": {"OnDemand": {"x": {"priceDimensions": {"y": {
        "unit": unit, "pricePerUnit": {"USD": price}}}}}}}


class TestHourlyUnitAndFargate(unittest.TestCase):
    def test_fargate_hours_unit_prices(self):
        # Fargate's unit is "hours" (lowercase); it must still be treated as an hourly rate
        got = aws_cost._price_list_hourly_usd({"PriceList": [_sku("0.0404800000", "hours")]}, "Hrs", 0.5)
        self.assertAlmostEqual(got, 0.02024, places=5)

    def test_capital_hrs_still_works(self):
        got = aws_cost._price_list_hourly_usd({"PriceList": [_sku("0.0160000000", "Hrs")]}, "Hrs", 1)
        self.assertAlmostEqual(got, 0.016, places=4)

    def test_full_bundle_prices_including_fargate(self):
        resources = [
            {"service": "rds", "resource_type": "db", "region": "us-east-1", "count": 1,
             "attrs": {"instanceType": "db.t4g.micro", "databaseEngine": "PostgreSQL",
                       "deploymentOption": "Single-AZ", "volumeType": "General Purpose"},
             "quantity": {"gb": 20.0}, "arn": "arn:aws:rds:us-east-1:1:db:x"},
            {"service": "ecs-fargate", "resource_type": "task", "region": "us-east-1", "count": 1,
             "attrs": {}, "quantity": {"vcpus": 0.5, "gb": 1.0}, "arn": "arn:aws:ecs:us-east-1:1:service/c/s"},
        ]
        def get_products(service_code, filters):
            # crude: key off the usagetype CONTAINS filter / productFamily to pick the SKU
            blob = str(filters)
            if "Fargate-vCPU" in blob: return {"PriceList": [_sku("0.0404800000", "hours")]}
            if "Fargate-GB" in blob: return {"PriceList": [_sku("0.0044450000", "hours")]}
            if "Database Instance" in blob: return {"PriceList": [_sku("0.0160000000", "Hrs")]}
            if "Database Storage" in blob: return {"PriceList": [_sku("0.1150000000", "GB-Mo")]}
            return {"PriceList": []}
        rr = aws_cost.AwsRunRateAdapter(enumerate_resources=lambda _r: resources,
                                        get_products=get_products).run_rate("x", capture_date="2026-08-29")
        names = {c.name: c.hourly_usd for c in rr.components}
        self.assertAlmostEqual(names["compute:rds"], 0.016, places=4)
        self.assertAlmostEqual(names["compute:fargate-vcpu"], 0.02024, places=5)
        self.assertAlmostEqual(names["compute:fargate-mem"], 0.004445, places=5)
        self.assertNotIn("excludes", rr.price_source)   # nothing silently dropped


if __name__ == "__main__":
    unittest.main()
