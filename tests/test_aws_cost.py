"""AWS run-rate resolver (acspeed/adapters/aws_cost.py): the generalization proof for the Part-4 cost axis.

These tests demonstrate what the redu handle-based resolver cannot: an arbitrary MIXED multi-service AWS
resource set priced to a standing hourly run-rate with NO per-service code -- classification is a DATA table
lookup and pricing is uniform per billing dimension. The Pricing client is mocked with the REAL AWS Price
List Query API response shape (escaped-JSON PriceList strings; terms.OnDemand -> priceDimensions ->
pricePerUnit.USD), so the parsing/extraction that would run against live AWS is exercised here.
"""
import json
import unittest

from acspeed.adapters import aws_cost
from acspeed.adapters.aws_cost import AwsRunRateAdapter, HOURS_PER_MONTH


def _sku(usd, unit, *, sku="SKU1", begin="0", end="Inf"):
    """One PriceList element in the exact verified Price List Query shape, as the ESCAPED JSON STRING the
    real API returns (dynamic sku.term / sku.term.rate keys; on-demand term code JRTCKXETXF)."""
    term = f"{sku}.JRTCKXETXF"
    rate = f"{term}.6YS6EN2CT7"
    product = {
        "product": {"productFamily": "X", "attributes": {}, "sku": sku},
        "serviceCode": "X",
        "terms": {"OnDemand": {term: {"priceDimensions": {rate: {
            "unit": unit, "beginRange": begin, "endRange": end,
            "description": f"${usd} per unit", "appliesTo": [], "rateCode": rate,
            "pricePerUnit": {"USD": f"{usd:.10f}"}}}, "sku": sku, "offerTermCode": "JRTCKXETXF",
            "termAttributes": {}}}},
    }
    return json.dumps(product)


# realistic us-east-1 on-demand list prices, keyed by (service_code, product_family, discriminator)
_PRICES = {
    ("AmazonEC2", "Compute Instance", "m5.large"): (0.096, "Hrs"),
    ("AmazonRDS", "Database Instance", "db.m5.large|Single-AZ"): (0.171, "Hrs"),
    ("AmazonRDS", "Database Instance", "db.m5.large|Multi-AZ"): (0.340, "Hrs"),   # separate SKU, NOT 2x=0.342
    ("AmazonRDS", "Database Storage", "Single-AZ"): (0.115, "GB-Mo"),
    ("AmazonRDS", "Database Storage", "Multi-AZ"): (0.230, "GB-Mo"),
    ("AmazonElastiCache", "Cache Instance", "cache.r6g.large"): (0.206, "Hrs"),
    ("AmazonEC2", "Storage", "gp3"): (0.08, "GB-Mo"),
    ("AmazonEC2", "System Operation", "gp3"): (0.005, "IOPS-Mo"),
    ("AmazonEC2", "Provisioned Throughput", "gp3"): (0.04, "MBps-Mo"),
    ("AWSELB", "Load Balancer-Application", ""): (0.0225, "Hrs"),
    ("AmazonVPC", "IP Address", ""): (0.005, "Hrs"),
    ("AmazonOpenSearchService", "Compute Instance", "r6g.large.search"): (0.167, "Hrs"),  # NEW-service test
}


def _field(filters, name):
    return next((f["Value"] for f in filters if f["Field"] == name), "")


def _mock_products(service_code, filters):
    """Route (service_code, filters) to a canned SKU, proving the resolver builds the right filters. Keys
    off productFamily (which the resolver now sends, disambiguating EBS storage/iops/throughput) plus the
    discriminating attribute (instanceType/volumeApiName, and deploymentOption for RDS)."""
    fam = _field(filters, "productFamily")
    itype = _field(filters, "instanceType") or _field(filters, "volumeApiName")
    deploy = _field(filters, "deploymentOption")
    for (sc, pf, disc), (usd, unit) in _PRICES.items():
        if sc != service_code or pf != fam:
            continue
        if disc == "":                                  # singleton (LB, IP)
            return {"PriceList": [_sku(usd, unit)], "FormatVersion": "aws_v1"}
        if "|" in disc:                                 # RDS instance: instanceType|deploymentOption
            if disc == f"{itype}|{deploy}":
                return {"PriceList": [_sku(usd, unit)], "FormatVersion": "aws_v1"}
            continue
        if disc in ("Single-AZ", "Multi-AZ"):           # RDS storage keyed by AZ
            if disc == deploy:
                return {"PriceList": [_sku(usd, unit)], "FormatVersion": "aws_v1"}
            continue
        if disc == itype:                               # instance / volume keyed by type
            return {"PriceList": [_sku(usd, unit)], "FormatVersion": "aws_v1"}
    return {"PriceList": [], "FormatVersion": "aws_v1"}


def _h(usd_per_gb_month, qty):
    return usd_per_gb_month * qty / HOURS_PER_MONTH


class TestAwsMixedBundle(unittest.TestCase):
    def _adapter(self, resources):
        return AwsRunRateAdapter(enumerate_resources=lambda ref: resources, get_products=_mock_products)

    def test_prices_a_mixed_multi_service_set_with_no_per_service_code(self):
        resources = [
            {"arn": "arn:aws:ec2:us-east-1:123:instance/i-1", "service": "ec2",
             "resource_type": "instance", "region": "us-east-1",
             "attrs": {"instanceType": "m5.large", "operatingSystem": "Linux"}},
            {"arn": "arn:aws:rds:us-east-1:123:db/app", "service": "rds", "resource_type": "db",
             "region": "us-east-1",
             "attrs": {"instanceType": "db.m5.large", "databaseEngine": "PostgreSQL",
                       "deploymentOption": "Single-AZ"}, "quantity": {"gb": 100}},
            {"arn": "arn:aws:elasticache:us-east-1:123:cluster/redis", "service": "elasticache",
             "resource_type": "cluster", "region": "us-east-1",
             "attrs": {"instanceType": "cache.r6g.large", "cacheEngine": "Redis"}},
            {"arn": "arn:aws:ec2:us-east-1:123:volume/vol-1", "service": "ec2", "resource_type": "volume",
             "region": "us-east-1", "attrs": {"volumeApiName": "gp3"},
             "quantity": {"gb": 200, "iops": 4000, "throughput_mbps": 250}},
            {"arn": "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/x",
             "service": "elasticloadbalancing", "resource_type": "loadbalancer", "region": "us-east-1",
             "attrs": {}},
            {"arn": "arn:aws:ec2:us-east-1:123:elastic-ip/eip-1", "service": "ec2",
             "resource_type": "elastic-ip", "region": "us-east-1", "attrs": {}},
            {"arn": "arn:aws:lambda:us-east-1:123:function/f", "service": "lambda",
             "resource_type": "function", "region": "us-east-1", "attrs": {}},        # usage -> $0 disclosed
            {"arn": "arn:aws:kafka:us-east-1:123:cluster/msk", "service": "kafka",
             "resource_type": "cluster", "region": "us-east-1", "attrs": {}},         # unknown -> disclosed
        ]
        rr = self._adapter(resources).run_rate("dep-1", capture_date="2026-08-27")
        self.assertIsNotNone(rr)
        self.assertEqual(rr.provider, "aws")
        names = [c.name for c in rr.components]
        # compute (EC2) + compute:rds + storage:rds + compute:redis + gp3 (storage/iops/throughput) + LB + IP
        self.assertEqual(names, ["compute", "compute:rds", "storage:rds", "compute:redis",
                                 "storage", "storage:iops", "storage:throughput",
                                 "load_balancer", "public_ip"])
        expected = (0.096 + 0.171 + _h(0.115, 100) + 0.206
                    + _h(0.08, 200) + _h(0.005, 4000 - 3000) + _h(0.04, 250 - 125)
                    + 0.0225 + 0.005)
        self.assertAlmostEqual(rr.all_in_hourly_usd, round(expected, 6))
        # usage-priced + unknown are DISCLOSED, never a faked $0 line
        self.assertIn("lambda:function (usage-priced, $0 standing)", rr.price_source)
        self.assertIn("kafka:cluster (unclassified)", rr.price_source)

    def test_multi_az_is_a_separate_sku_not_double_the_single_az(self):
        res = [{"arn": "arn:aws:rds:us-east-1:123:db/ha", "service": "rds", "resource_type": "db",
                "region": "us-east-1",
                "attrs": {"instanceType": "db.m5.large", "databaseEngine": "PostgreSQL",
                          "deploymentOption": "Multi-AZ"}, "quantity": {"gb": 100}}]
        rr = self._adapter(res).run_rate("dep", capture_date="2026-08-27")
        comp = next(c for c in rr.components if c.name == "compute:rds")
        self.assertAlmostEqual(comp.hourly_usd, 0.340)          # read from the Multi-AZ SKU directly...
        self.assertNotAlmostEqual(comp.hourly_usd, 0.171 * 2)   # ...NOT computed as Single-AZ x 2 (=0.342)

    def test_gp3_below_baseline_iops_and_throughput_are_free(self):
        # a gp3 at exactly the 3000 IOPS / 125 MBps baseline bills ONLY storage
        res = [{"arn": "arn:aws:ec2:us-east-1:123:volume/v", "service": "ec2", "resource_type": "volume",
                "region": "us-east-1", "attrs": {"volumeApiName": "gp3"},
                "quantity": {"gb": 50, "iops": 3000, "throughput_mbps": 125}}]
        rr = self._adapter(res).run_rate("dep", capture_date="2026-08-27")
        self.assertEqual([c.name for c in rr.components], ["storage"])   # no iops / throughput lines
        self.assertAlmostEqual(rr.all_in_hourly_usd, round(_h(0.08, 50), 6))

    def test_adding_a_new_service_is_a_data_row_not_code(self):
        # THE GENERALIZATION PROOF: an OpenSearch domain -- a service the resolver has never seen -- is
        # priced through the SAME loop with ZERO code change, purely by adding a _DIMENSIONS row. This is
        # why 250 services do not need 250 mappings.
        res = [{"arn": "arn:aws:es:us-east-1:123:domain/logs", "service": "opensearch",
                "resource_type": "domain", "region": "us-east-1",
                "attrs": {"instanceType": "r6g.large.search", "operatingSystem": "Linux"}}]
        adapter = self._adapter(res)
        # not yet classifiable -> disclosed, not crashed
        rr0 = adapter.run_rate("dep", capture_date="2026-08-27")
        self.assertIsNone(rr0)                                  # nothing priceable yet
        # add ONE data row (no resolver code touched) and it prices
        aws_cost._DIMENSIONS[("opensearch", "domain")] = {"dimensions": [{
            "name": "compute:search", "service_code": "AmazonOpenSearchService",
            "product_family": "Compute Instance",
            "attr_filters": {"instanceType": "instanceType"}, "fixed_filters": {},
            "unit": "Hrs", "quantity": None}]}
        try:
            rr = adapter.run_rate("dep", capture_date="2026-08-27")
            self.assertIsNotNone(rr)
            self.assertEqual([c.name for c in rr.components], ["compute:search"])
            self.assertAlmostEqual(rr.all_in_hourly_usd, 0.167)
        finally:
            del aws_cost._DIMENSIONS[("opensearch", "domain")]

    def test_unpriceable_everything_returns_none_disclosed(self):
        res = [{"arn": "arn:aws:glue:us-east-1:123:job/j", "service": "glue", "resource_type": "job",
                "region": "us-east-1", "attrs": {}}]
        rr = self._adapter(res).run_rate("dep", capture_date="2026-08-27")
        self.assertIsNone(rr)                                   # disclosed by returning None, never faked


class TestAwsExtractionShape(unittest.TestCase):
    def test_escaped_json_pricelist_and_unit_rules(self):
        from acspeed.adapters.aws_cost import _price_list_hourly_usd
        hrs = {"PriceList": [_sku(0.278, "Hrs")], "FormatVersion": "aws_v1"}
        self.assertAlmostEqual(_price_list_hourly_usd(hrs, "Hrs", 1), 0.278)
        mo = {"PriceList": [_sku(7.30, "GB-Mo")], "FormatVersion": "aws_v1"}
        self.assertAlmostEqual(_price_list_hourly_usd(mo, "GB-Mo", 10), 7.30 * 10 / 730.0)
        usage = {"PriceList": [_sku(0.20, "Requests")], "FormatVersion": "aws_v1"}
        self.assertIsNone(_price_list_hourly_usd(usage, "Requests", 1000))   # usage unit -> no standing rate
        placeholder = {"PriceList": [_sku(0.0, "Hrs")], "FormatVersion": "aws_v1"}
        self.assertIsNone(_price_list_hourly_usd(placeholder, "Hrs", 1))     # $0 placeholder guarded

    def test_no_per_service_branches_in_the_resolver(self):
        # structural proof: the run_rate loop classifies via the _DIMENSIONS dict, and the module carries no
        # per-service-name if/elif branch. Service names appear only as DATA (dict keys), never as code tests.
        import inspect
        src = inspect.getsource(aws_cost.AwsRunRateAdapter.run_rate)
        self.assertIn("_DIMENSIONS.get(", src)
        for svc in ("ec2", "rds", "lambda", "elasticache", "elasticloadbalancing"):
            self.assertNotIn(f'== "{svc}"', src)
            self.assertNotIn(f"== '{svc}'", src)


if __name__ == "__main__":
    unittest.main()
