"""UNIVERSAL AWS discovery: price EVERY resource the deploy provisioned, from AWS's OWN complete inventory.

The tag inventory (Resource Groups Tagging API) is TAG-GATED, so any resource the agent did not tag -- it
tags inconsistently, and auto-created resources (public IPv4, inline root EBS) are untaggable -- is silently
missed, biasing the run-rate LOW. AWS's equivalent of redu's "enumerate every resource we provisioned" is
Resource Explorer 2, whose index spans ALL services and is NOT tag-gated. This suite proves, entirely
offline with an INJECTED Resource Explorer search + Price List:

  (a) a mixed canned inventory (ECS/Fargate + RDS + ALB + an S3 bucket + a brand-new type) prices the
      priceable resources and DISCLOSES the bucket / unknown BY TYPE (never a silent $0);
  (b) a resource present in Resource Explorer but NOT tagged is still discovered -- the whole point: the tag
      path alone would have missed it;
  (c) a brand-new / unrecognized (service, type) is surfaced in ``unpriced_resources``, never dropped.

No live AWS is used (the aws MCP proxy is not reachable this session); the injected mcp_call returns exactly
the JSON the live CLIs would print, so the enumeration + dispatch + disclose logic is exercised as-is.
"""
import json
import unittest

from acspeed.adapters.aws_runrate import (AwsRunRateAdapter, _enumerate, _enumerate_via_tags,
                                          _enumerate_via_resource_explorer)

TOKEN = "acs9f9f9f9f"
# an internet-facing ALB DNS name carries the ELB numeric hash; the resource ARNs carry only the base name
URL = f"http://shop-{TOKEN}-1531808006.us-east-1.elb.amazonaws.com"

_ALB_ARN = f"arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/app/shop-{TOKEN}/abcd"
_ECS_ARN = f"arn:aws:ecs:us-east-1:1:service/shop-{TOKEN}-cluster/shop-{TOKEN}"
_RDS_ARN = f"arn:aws:rds:us-east-1:1:db:shop-db-{TOKEN}"          # token-only name (different base)
_S3_ARN = f"arn:aws:s3:::shop-{TOKEN}-assets"                     # no region/account/type segment
_NEW_ARN = f"arn:aws:sagemaker:us-east-1:1:notebook-instance/nb-{TOKEN}"  # a type never anticipated here

# the FULL inventory Resource Explorer returns (tagged or not); the tag inventory returns only the ALB
_ALL_ARNS = [_ALB_ARN, _ECS_ARN, _RDS_ARN, _S3_ARN, _NEW_ARN]
_TAGGED_ARNS = [_ALB_ARN]

_ALB_PROPS = {"Scheme": "internet-facing", "Subnets": ["subnet-a", "subnet-b"]}
_RDS_PROPS = {"DBInstanceClass": "db.t4g.micro", "Engine": "postgres", "MultiAZ": False,
              "StorageType": "gp3", "AllocatedStorage": 20}


def _price_list(usd, unit, sku="S1"):
    """One PriceList element in the real Price List Query shape (escaped-JSON string, dynamic term keys)."""
    term = f"{sku}.JRTCKXETXF"
    rate = f"{term}.6YS6EN2CT7"
    prod = {"product": {"productFamily": "X", "attributes": {}, "sku": sku}, "serviceCode": "X",
            "terms": {"OnDemand": {term: {"priceDimensions": {rate: {
                "unit": unit, "beginRange": "0", "endRange": "Inf", "description": "list",
                "pricePerUnit": {"USD": f"{usd:.10f}"}, "rateCode": rate}},
                "sku": sku, "offerTermCode": "JRTCKXETXF"}}}}
    return {"PriceList": [json.dumps(prod)], "FormatVersion": "aws_v1"}


def _route_price(cli):
    if "AmazonVPC" in cli and "PublicIPv4:InUseAddress" in cli:
        return _price_list(0.005, "Hrs")
    if "AmazonECS" in cli and "Fargate-vCPU-Hours" in cli:
        return _price_list(0.04048, "hours")
    if "AmazonECS" in cli and "Fargate-GB-Hours" in cli:
        return _price_list(0.004445, "hours")
    if "AmazonRDS" in cli and "Database Instance" in cli:
        return _price_list(0.016, "Hrs")               # db.t4g.micro on-demand
    if "AmazonRDS" in cli and "Database Storage" in cli:
        return _price_list(0.115, "GB-Mo")
    if "AWSELB" in cli:
        return _price_list(0.0225, "Hrs")
    return {"PriceList": [], "FormatVersion": "aws_v1"}


def _cc(props):
    return {"ResourceDescription": {"Properties": json.dumps(props)}}


def make_fake_mcp(re_arns=_ALL_ARNS, tagged_arns=_TAGGED_ARNS, calls=None):
    """A full offline aws MCP: Resource Explorer 2 search (the COMPLETE, non-tag-gated inventory) + the
    tag inventory (deliberately incomplete) + Cloud Control describes + ECS + Price List. ``calls`` (if a
    list) records every issued CLI so a test can assert which enumerator the default path ran."""
    def call(cli):
        if calls is not None:
            calls.append(cli)
        if "resource-explorer-2 search" in cli:
            # NOT tag-gated: returns every resource whose name/ARN carries the queried token, tagged or not
            if TOKEN in cli:
                return {"Resources": [{"Arn": a, "ResourceType": a.split(":")[2]} for a in re_arns]}
            return {"Resources": []}
        if "resourcegroupstaggingapi get-resources" in cli:
            return {"ResourceTagMappingList": [{"ResourceARN": a, "Tags": [{"Key": "Name",
                     "Value": f"shop-{TOKEN}"}]} for a in tagged_arns]}
        if "cloudcontrol get-resource" in cli:
            if "ElasticLoadBalancingV2" in cli:
                return _cc(_ALB_PROPS)
            if "AWS::RDS::DBInstance" in cli:
                return _cc(_RDS_PROPS)
            return {}                                  # S3 / SageMaker: no props needed to classify/disclose
        if "ecs describe-services" in cli:
            return {"services": [{"launchType": "FARGATE", "desiredCount": 1, "taskDefinition": "shop:1",
                                  "networkConfiguration": {"awsvpcConfiguration": {"assignPublicIp":
                                                                                   "ENABLED"}}}]}
        if "ecs describe-task-definition" in cli:
            return {"taskDefinition": {"cpu": "512", "memory": "1024"}}
        if "pricing get-products" in cli:
            return _route_price(cli)
        return {}
    return call


def _names(rr):
    return [c.name for c in rr.components]


class TestDefaultEnumeratorRunsResourceExplorer(unittest.TestCase):
    """RETURN (b): the DEFAULT complete enumerator issues the Resource Explorer 2 search, scoped by token."""

    def test_default_enumerator_issues_resource_explorer_search(self):
        calls = []
        AwsRunRateAdapter(mcp_call=make_fake_mcp(calls=calls)).run_rate(URL, capture_date="2026-08-29")
        re_calls = [c for c in calls if "resource-explorer-2 search" in c]
        self.assertTrue(re_calls, "the default enumerator must run Resource Explorer 2")
        # the exact CLI: search by the harness run-token as the free-text query (not tag-gated)
        self.assertIn(f'aws resource-explorer-2 search --query-string "{TOKEN}"', re_calls[0])

    def test_resource_explorer_is_tried_before_the_tag_inventory(self):
        calls = []
        _enumerate(make_fake_mcp(calls=calls), URL, "us-east-1")
        order = [c for c in calls if ("resource-explorer-2 search" in c
                                      or "resourcegroupstaggingapi get-resources" in c)]
        self.assertTrue(order[0].startswith("aws resource-explorer-2 search"),
                        "Resource Explorer must be the default source, tags only the fallback")


class TestUniversalDiscoveryPricesAndDiscloses(unittest.TestCase):
    """(a) a mixed inventory prices the priceable resources and discloses the bucket / unknown BY TYPE."""

    def test_mixed_inventory_prices_priceable_and_discloses_the_rest(self):
        adapter = AwsRunRateAdapter(mcp_call=make_fake_mcp())
        rr = adapter.run_rate(URL, capture_date="2026-08-29")
        self.assertIsNotNone(rr)                                    # priceable resources -> a real RunRate
        names = set(_names(rr))
        # the priceable resources are priced through the SAME dimension engine, no per-type code
        self.assertIn("load_balancer", names)                      # ALB hour
        self.assertIn("compute:fargate-vcpu", names)               # Fargate task compute
        self.assertIn("compute:fargate-mem", names)
        self.assertIn("compute:rds", names)                        # database instance
        self.assertIn("storage:rds", names)                        # database storage
        self.assertIn("public_ip", names)                          # synthesized untaggable public IPv4
        ip_hourly = sum(c.hourly_usd for c in rr.components if c.name == "public_ip")
        self.assertGreater(ip_hourly, 0)                           # Fargate(1) + ALB(2 AZ) = 3 x $0.005/hr

        # the S3 bucket and the brand-new type are DISCLOSED by type, never a silent $0 line
        unpriced_blob = " ".join(adapter.unpriced_resources)
        self.assertIn("s3:bucket", unpriced_blob)                  # the bucket, by type
        self.assertIn("usage-priced, $0 standing", unpriced_blob)  # disclosed as usage-priced, not dropped
        self.assertIn("sagemaker:notebook-instance", unpriced_blob)  # the unknown type, by type
        self.assertIn("unclassified", unpriced_blob)
        # the same disclosures are carried in price_source (the existing behavior is not regressed)
        self.assertIn("s3:bucket", rr.price_source)
        self.assertIn("sagemaker:notebook-instance", rr.price_source)


class TestUntaggedResourceStillDiscovered(unittest.TestCase):
    """(b) THE POINT: a resource in Resource Explorer but NOT tagged is still discovered."""

    def test_untagged_rds_is_discovered_by_resource_explorer(self):
        fake = make_fake_mcp()                                     # only the ALB is tagged; RDS/ECS are not
        resources = _enumerate(fake, URL, "us-east-1")
        kinds = {(r["service"], r["resource_type"]) for r in resources}
        self.assertIn(("rds", "db"), kinds)                        # discovered despite carrying no tag
        self.assertIn(("ecs-fargate", "task"), kinds)              # ditto the untagged Fargate service

    def test_tag_inventory_alone_would_have_missed_the_untagged_rds(self):
        # prove the gap Resource Explorer closes: the tag path returns only the tagged ALB (+ its IPv4)
        fake = make_fake_mcp()
        tag_only = _enumerate_via_tags(fake, URL, "us-east-1")
        tag_kinds = {(r["service"], r["resource_type"]) for r in tag_only}
        self.assertNotIn(("rds", "db"), tag_kinds)                 # tag-gated: the untagged RDS is missed
        self.assertNotIn(("ecs-fargate", "task"), tag_kinds)

    def test_resource_explorer_path_returns_the_full_set(self):
        fake = make_fake_mcp()
        re_only = _enumerate_via_resource_explorer(fake, URL, "us-east-1")
        kinds = {(r["service"], r["resource_type"]) for r in re_only}
        for expect in (("rds", "db"), ("ecs-fargate", "task"),
                       ("elasticloadbalancing", "loadbalancer"), ("s3", "bucket")):
            self.assertIn(expect, kinds)


class TestBrandNewTypeNeverDropped(unittest.TestCase):
    """(c) a brand-new / unrecognized (service, type) is surfaced in unpriced_resources, never dropped."""

    def test_unknown_type_is_surfaced_by_type_not_dropped(self):
        adapter = AwsRunRateAdapter(mcp_call=make_fake_mcp())
        rr = adapter.run_rate(URL, capture_date="2026-08-29")
        self.assertIsNotNone(rr)
        # the never-anticipated SageMaker notebook is surfaced by its (service, type), not silently dropped,
        # and is NOT invented as a priced $0 component
        self.assertTrue(any("sagemaker:notebook-instance" in u for u in adapter.unpriced_resources))
        self.assertNotIn("sagemaker:notebook-instance", [c.name for c in rr.components])

    def test_a_type_with_no_dimension_row_is_disclosed_even_when_it_is_the_only_resource(self):
        # an inventory of ONLY an unknown type prices to None (nothing priceable) but STILL discloses the type
        fake = make_fake_mcp(re_arns=[_NEW_ARN], tagged_arns=[])
        adapter = AwsRunRateAdapter(mcp_call=fake)
        rr = adapter.run_rate(URL, capture_date="2026-08-29")
        self.assertIsNone(rr)                                      # not a faked $0 RunRate
        self.assertTrue(any("sagemaker:notebook-instance" in u for u in adapter.unpriced_resources))
        self.assertTrue(any("unclassified" in u for u in adapter.unpriced_resources))


if __name__ == "__main__":
    unittest.main()
