"""Regression for the AWS run-rate UNDER-PRICING bug (found 2026-09-05), two distinct root causes, both of
which silently biased the standing run-rate LOW and forced aws-medium-b runs to be excluded (run11/12 ALBs
priced $0, run14 RDS priced $0):

  A. The tag-INDEPENDENT enumerators were dead. The 1.6.5 MCP port (cli_command -> run_script/boto3) added an
     explicit CLI->boto3 translation table but OMITTED `resource-explorer-2 search` and
     `configservice select-resource-config`, so each raised "no boto3 translation", the enumerator caught it
     and fell through, and the WHOLE study ran on the tag-GATED RGT alone -> an untagged ALB/RDS was missed.
     The existing suites mock mcp_call at the CLI-STRING level, so they never exercise the translation table
     and could not catch this; these tests exercise `_cli_to_boto3` directly.

  B. The Lightsail branch short-circuited. A Lightsail container is live-priced on its own (it is absent from
     the uniform inventory), but a co-provisioned RDS IS in the inventory and the old branch never enumerated
     it, so a Lightsail+RDS deploy priced only the container.
"""
import json
import unittest

from acspeed.adapters.aws_runrate import _cli_to_boto3, _lightsail_run_rate, AwsRunRateAdapter


class TestTagIndependentEnumeratorsTranslate(unittest.TestCase):
    """Bug A: every enumerator command the adapter issues MUST translate to boto3, or that enumerator is
    silently dead in production. The CLI-string mocks in the other suites cannot catch this."""

    def test_resource_explorer_search_translates(self):
        svc, op, params, region = _cli_to_boto3(
            'aws resource-explorer-2 search --query-string "acs9f9f9f9f" --region us-east-1')
        self.assertEqual(svc, "resource-explorer-2")          # boto3 client name == CLI name here
        self.assertEqual(op, "Search")
        self.assertEqual(params, {"QueryString": "acs9f9f9f9f"})
        self.assertEqual(region, "us-east-1")

    def test_config_select_translates_and_remaps_service(self):
        expr = "SELECT arn WHERE resourceName LIKE '%acs9f9f9f9f%'"
        svc, op, params, region = _cli_to_boto3(
            f'aws configservice select-resource-config --expression "{expr}" --region us-east-1')
        self.assertEqual(svc, "config")                       # CLI `configservice` -> boto3 client `config`
        self.assertEqual(op, "SelectResourceConfig")
        self.assertEqual(params, {"Expression": expr})
        self.assertEqual(region, "us-east-1")

    def test_the_dead_commands_no_longer_raise(self):
        # before the fix each raised "no boto3 translation for ..." -> the enumerator fell through to RGT
        for cli in ('aws resource-explorer-2 search --query-string "acs1" --region us-east-1',
                    'aws configservice select-resource-config --expression "x" --region us-east-1'):
            try:
                _cli_to_boto3(cli)
            except RuntimeError as e:  # noqa: PERF203
                self.fail(f"still untranslatable, enumerator would be silently dead: {e}")


# --- Bug B: a Lightsail container with a co-provisioned RDS -----------------------------------------------
# run14's real shape: the Lightsail URL's first label carries the run token, and the RDS name carries it too.
_LS_URL = "https://umami-acsd16c075c.w4a4v7yp5swqw.us-east-1.cs.amazonlightsail.com"
_RDS_RES = [{"arn": "arn:aws:rds:us-east-1:1:db:umami-db-acsd16c075c", "service": "rds",
             "resource_type": "db", "region": "us-east-1",
             "attrs": {"instanceType": "db.t4g.micro", "databaseEngine": "PostgreSQL",
                       "deploymentOption": "Single-AZ", "volumeType": "General Purpose"},
             "quantity": {"gb": 20.0}, "count": 1}]


def _price_list(usd, unit, sku="S1"):
    term, rate = f"{sku}.JRTCKXETXF", f"{sku}.JRTCKXETXF.6YS6EN2CT7"
    prod = {"product": {"productFamily": "X", "attributes": {}, "sku": sku}, "serviceCode": "X",
            "terms": {"OnDemand": {term: {"priceDimensions": {rate: {
                "unit": unit, "beginRange": "0", "endRange": "Inf", "description": "list",
                "pricePerUnit": {"USD": f"{usd:.10f}"}, "rateCode": rate}},
                "sku": sku, "offerTermCode": "JRTCKXETXF"}}}}
    return {"PriceList": [json.dumps(prod)], "FormatVersion": "aws_v1"}


def _ls_and_rds_mcp(cli):
    if "get-container-services" in cli:
        return {"containerServices": [{"containerServiceName": "umami-acsd16c075c", "power": "medium",
                                       "powerId": "medium-1", "scale": 1, "state": "RUNNING",
                                       "url": _LS_URL + "/"}]}
    if "get-container-service-powers" in cli:
        return {"powers": [{"powerId": "medium-1", "price": 40.0, "name": "medium"}]}
    if "pricing get-products" in cli and "AmazonRDS" in cli and "Database Instance" in cli:
        return _price_list(0.016, "Hrs")                      # db.t4g.micro on-demand
    if "pricing get-products" in cli and "AmazonRDS" in cli and "Database Storage" in cli:
        return _price_list(0.115, "GB-Mo")
    return {}


class TestLightsailPricesCoProvisionedBackend(unittest.TestCase):
    """Bug B: a Lightsail container deploy with a separate RDS must price BOTH, not just the container."""

    def test_lightsail_plus_rds_priced_together(self):
        adapter = AwsRunRateAdapter(mcp_call=_ls_and_rds_mcp,
                                    enumerate_resources=lambda *_: list(_RDS_RES))
        rr = adapter.run_rate(_LS_URL, capture_date="2026-09-05")
        self.assertIsNotNone(rr)
        names = [c.name for c in rr.components]
        self.assertIn("compute:lightsail-container", names)   # the container (live-priced)
        self.assertIn("compute:rds", names)                   # the co-provisioned db instance
        self.assertIn("storage:rds", names)                   # and its storage
        self.assertGreater(rr.monthly_usd(), 40.0)            # more than the container alone (RDS not dropped)
        self.assertIn("co-provisioned", rr.price_source)      # the merge is disclosed

    def test_old_short_circuit_would_have_dropped_the_rds(self):
        # the pre-fix path (container alone) proves the gap the merge closes
        ls_only = _lightsail_run_rate(_ls_and_rds_mcp, _LS_URL, "us-east-1", "2026-09-05")
        self.assertEqual([c.name for c in ls_only.components], ["compute:lightsail-container"])
        self.assertAlmostEqual(ls_only.monthly_usd(), 40.0, places=2)

    def test_no_backend_is_still_just_the_container(self):
        # a Lightsail deploy with no co-provisioned resource must still return the container run-rate
        adapter = AwsRunRateAdapter(mcp_call=_ls_and_rds_mcp, enumerate_resources=lambda *_: [])
        rr = adapter.run_rate(_LS_URL, capture_date="2026-09-05")
        self.assertIsNotNone(rr)
        self.assertEqual([c.name for c in rr.components], ["compute:lightsail-container"])


def _two_container_mcp(cli):
    # a medium-b Lightsail deploy: TWO container services (site A + site B), both carrying the run token
    if "get-container-services" in cli:
        return {"containerServices": [
            {"containerServiceName": "umami-acsd16c075c", "power": "medium", "powerId": "medium-1",
             "scale": 1, "state": "RUNNING", "url": _LS_URL + "/"},
            {"containerServiceName": "alcove-acsd16c075c", "power": "small", "powerId": "small-1",
             "scale": 1, "state": "RUNNING",
             "url": "https://alcove-acsd16c075c.w4a4v7yp5swqw.us-east-1.cs.amazonlightsail.com/"}]}
    if "get-container-service-powers" in cli:
        return {"powers": [{"powerId": "medium-1", "price": 40.0, "name": "medium"},
                           {"powerId": "small-1", "price": 15.0, "name": "small"}]}
    return {}


class TestLightsailMultiContainer(unittest.TestCase):
    """run09/run10: a medium-b Lightsail deploy stands up a SECOND app (site B) as its own container service;
    pricing only the URL-matched one under-prices the run (the audit caught this in the published set)."""

    def test_both_run_token_containers_are_priced(self):
        rr = _lightsail_run_rate(_two_container_mcp, _LS_URL, "us-east-1", "2026-09-05")
        self.assertIsNotNone(rr)
        self.assertEqual([c.name for c in rr.components],                     # BOTH containers, not one
                         ["compute:lightsail-container", "compute:lightsail-container"])
        self.assertAlmostEqual(rr.monthly_usd(), 55.0, places=2)             # medium $40 + small $15
        self.assertIn("mediumx1+smallx1", rr.flavor)

    def test_another_runs_container_is_not_pulled_in(self):
        def mcp(cli):
            if "get-container-services" in cli:
                return {"containerServices": [
                    {"containerServiceName": "umami-acsd16c075c", "power": "medium", "powerId": "medium-1",
                     "scale": 1, "url": _LS_URL + "/"},
                    {"containerServiceName": "umami-acs00000000", "power": "large", "powerId": "large-1",
                     "scale": 1, "url": "https://umami-acs00000000.x.us-east-1.cs.amazonlightsail.com/"}]}
            if "get-container-service-powers" in cli:
                return {"powers": [{"powerId": "medium-1", "price": 40.0, "name": "medium"},
                                   {"powerId": "large-1", "price": 80.0, "name": "large"}]}
            return {}
        rr = _lightsail_run_rate(mcp, _LS_URL, "us-east-1", "2026-09-05")
        self.assertAlmostEqual(rr.monthly_usd(), 40.0, places=2)             # only THIS run token's container


if __name__ == "__main__":
    unittest.main()


# --- Bug D (2026-09-05, found ingesting aws-medium-b runs 15 to 19) ---------------------------------------
# Two further ways the co-provisioned datastore went unpriced, BOTH measured on real runs, and both of which
# the Bug B fix above did NOT cover because it enumerated one region and dropped every Lightsail row:
#
#   D1 run17: the container ran in us-east-2 while its RDS lived in us-east-1
#             (umami-db-acsb845f10e.<...>.us-east-1.rds.amazonaws.com). Enumeration only ever asked the
#             container's own region, so the datastore was invisible and the run priced $10.00/mo against
#             the cell's $65 to $101 band.
#   D2 run19: the datastore was a LIGHTSAIL RELATIONAL DATABASE. The old filter dropped every
#             service == "lightsail" row on the theory that the live path prices it, but the live path
#             prices CONTAINER SERVICES only, so it fell through both and the run priced $15.00/mo.
_LS_URL_E2 = "https://umami-acsb845f10e.h2hemnvjsvbzm.us-east-2.cs.amazonlightsail.com"
_RDS_E1 = [{"arn": "arn:aws:rds:us-east-1:1:db:umami-db-acsb845f10e", "service": "rds",
            "resource_type": "db", "region": "us-east-1",
            "attrs": {"instanceType": "db.t4g.micro", "databaseEngine": "PostgreSQL",
                      "deploymentOption": "Single-AZ", "volumeType": "General Purpose"},
            "quantity": {"gb": 20.0}, "count": 1}]


def _ls_e2_mcp(cli):
    if "get-container-services" in cli:
        return {"containerServices": [{"containerServiceName": "umami-acsb845f10e", "power": "micro",
                                       "powerId": "micro-1", "scale": 1, "state": "RUNNING",
                                       "url": _LS_URL_E2 + "/"}]}
    if "get-container-service-powers" in cli:
        return {"powers": [{"powerId": "micro-1", "price": 10.0, "name": "micro"}]}
    if "pricing get-products" in cli and "AmazonRDS" in cli and "Database Instance" in cli:
        return _price_list(0.016, "Hrs")
    if "pricing get-products" in cli and "AmazonRDS" in cli and "Database Storage" in cli:
        return _price_list(0.115, "GB-Mo")
    return {}


class TestBackendFoundInAnotherRegion(unittest.TestCase):
    """D1: the datastore is only visible in its OWN region, so enumeration must sweep the candidates."""

    @staticmethod
    def _region_scoped_enumerator(_call, _url, region):
        return list(_RDS_E1) if region == "us-east-1" else []   # the RDS exists ONLY in us-east-1

    def test_cross_region_rds_is_priced(self):
        adapter = AwsRunRateAdapter(mcp_call=_ls_e2_mcp,
                                    enumerate_resources=self._region_scoped_enumerator)
        rr = adapter.run_rate(_LS_URL_E2, capture_date="2026-09-05")   # region derived from URL = us-east-2
        self.assertIsNotNone(rr)
        names = [c.name for c in rr.components]
        self.assertIn("compute:lightsail-container", names)
        self.assertIn("compute:rds", names)                    # would be ABSENT before the sweep: run17's bug
        self.assertGreater(rr.monthly_usd(), 10.0,
                           "priced the container alone, reproducing run17's $10.00/mo under-price")

    def test_single_region_enumeration_reproduces_the_bug(self):
        # the pre-fix behaviour, asserted so the regression is unambiguous: ask only the container's region
        self.assertEqual(self._region_scoped_enumerator(None, _LS_URL_E2, "us-east-2"), [])

    def test_no_duplicate_when_the_enumerator_ignores_region(self):
        # an enumerator that returns the same list for every region must NOT be counted once per region
        adapter = AwsRunRateAdapter(mcp_call=_ls_e2_mcp, enumerate_resources=lambda *_: list(_RDS_E1))
        rr = adapter.run_rate(_LS_URL_E2, capture_date="2026-09-05")
        self.assertEqual([c.name for c in rr.components].count("compute:rds"), 1)


class TestLightsailDatabaseIsNotDropped(unittest.TestCase):
    """D2: only the Lightsail CONTAINER SERVICE is priced by the live path, so only it may be dropped."""

    _LS_DB = [{"arn": "arn:aws:lightsail:us-east-1:1:RelationalDatabase/umami-db-acs7171e9dc",
               "service": "lightsail", "resource_type": "RelationalDatabase", "region": "us-east-1",
               "attrs": {"bundleId": "micro_2_0"}, "quantity": {}, "count": 1}]

    def test_lightsail_database_is_priced_or_flagged_never_dropped(self):
        adapter = AwsRunRateAdapter(mcp_call=_ls_e2_mcp, enumerate_resources=lambda *_: list(self._LS_DB))
        rr = adapter.run_rate(_LS_URL_E2, capture_date="2026-09-05")
        self.assertIsNotNone(rr)
        priced = any("lightsail" in c.name and "container" not in c.name for c in rr.components)
        flagged = any("lightsail" in u.lower() for u in adapter.unpriced_resources)
        self.assertTrue(priced or flagged,
                        "the Lightsail relational database was silently dropped: run19's $15.00/mo bug. "
                        "It must be priced, or surfaced in unpriced_resources so the fairness filter "
                        "rejects the run, but never omitted in silence")

    def test_container_service_row_is_still_dropped_no_double_count(self):
        container_row = [{"arn": "arn:aws:lightsail:us-east-2:1:ContainerService/umami-acsb845f10e",
                          "service": "lightsail", "resource_type": "ContainerService",
                          "region": "us-east-2", "attrs": {}, "quantity": {}, "count": 1}]
        adapter = AwsRunRateAdapter(mcp_call=_ls_e2_mcp, enumerate_resources=lambda *_: list(container_row))
        rr = adapter.run_rate(_LS_URL_E2, capture_date="2026-09-05")
        self.assertEqual([c.name for c in rr.components], ["compute:lightsail-container"])
        self.assertAlmostEqual(rr.monthly_usd(), 10.0, places=2)   # counted once, by the live path
