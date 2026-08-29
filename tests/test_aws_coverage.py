"""AWS cost-coverage gaps found by the 8-agent audit of the standing run-rate (aws_runrate.py + aws_cost.py).

The audited umami architecture: a Fargate service (assignPublicIp=ENABLED) behind an internet-facing ALB
plus an RDS db.t4g.micro; some runs used an EC2 t3.medium variant whose RunInstances carried a gp3 20 GB
root; ECS created untagged /ecs/... log groups. Four real charges were missed or mis-handled:

  GAP 8  auto-assigned public IPv4 (Fargate task + internet-facing ALB per-AZ) - not an Elastic IP, not
         taggable, so RGT never lists it, yet $0.005/hr each since 2024-02-01.
  GAP 9  the EC2 root EBS volume (inline in RunInstances BlockDeviceMappings), usually untagged.
  GAP 10 RGT get-resources is TAG-GATED: an untagged resource is silently omitted -> a low/zero number.
  GAP 11 CloudWatch Logs (logs:log-group) was dumped as "(unclassified)" instead of disclosed usage.

Everything is driven OFFLINE with an injected mcp_call / get_products returning canned Price List + describe
responses, exactly like the live path would see. No live AWS is required.
"""
import json
import unittest

from acspeed.adapters import aws_cost, aws_runrate
from acspeed.adapters.aws_cost import AwsRunRateAdapter as CostAdapter, HOURS_PER_MONTH
from acspeed.adapters.aws_runrate import (AwsRunRateAdapter, _enumerate, _general_run_rate,
                                          _completeness_note, _synthesize_public_ipv4, _ebs_from_instance,
                                          _alb_ipv4_hint)

TOKEN = "acs1234ab"
FARGATE_URL = f"http://umami-{TOKEN}.us-east-1.elb.amazonaws.com"

_ALB_ARN = f"arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/app/umami-{TOKEN}/9a8b"
_ECS_ARN = f"arn:aws:ecs:us-east-1:1:service/umami-{TOKEN}-cluster/umami-{TOKEN}"
_RDS_ARN = f"arn:aws:rds:us-east-1:1:db:umami-db-{TOKEN}"
_EC2_ARN = f"arn:aws:ec2:us-east-1:1:instance/i-{TOKEN}"


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
    """Route a `pricing get-products` cli to a canned list-price SKU by the dimension it asks for."""
    if "AmazonVPC" in cli and "PublicIPv4:InUseAddress" in cli:
        return _price_list(0.005, "Hrs")
    if "AmazonEC2" in cli and "Compute Instance" in cli:
        return _price_list(0.0416, "Hrs")          # t3.medium on-demand
    if "AmazonEC2" in cli and "Storage" in cli:
        return _price_list(0.08, "GB-Mo")          # gp3 storage $/GB-mo
    if "AmazonECS" in cli and "Fargate-vCPU-Hours" in cli:
        return _price_list(0.04048, "hours")
    if "AmazonECS" in cli and "Fargate-GB-Hours" in cli:
        return _price_list(0.004445, "hours")
    if "AmazonRDS" in cli and "Database Instance" in cli:
        return _price_list(0.016, "Hrs")           # db.t4g.micro
    if "AmazonRDS" in cli and "Database Storage" in cli:
        return _price_list(0.115, "GB-Mo")
    if "AWSELB" in cli:
        return _price_list(0.0225, "Hrs")
    return {"PriceList": [], "FormatVersion": "aws_v1"}


def _cc(props):
    return {"ResourceDescription": {"Properties": json.dumps(props)}}


_ALB_PROPS = {"Scheme": "internet-facing", "Subnets": ["subnet-a", "subnet-b"]}
_EC2_PROPS = {"InstanceType": "t3.medium",
              "BlockDeviceMappings": [{"DeviceName": "/dev/xvda",
                                       "Ebs": {"VolumeSize": 20, "VolumeType": "gp3",
                                               "Iops": 3000, "Throughput": 125}}]}
_RDS_PROPS = {"DBInstanceClass": "db.t4g.micro", "Engine": "postgres", "MultiAZ": False,
              "StorageType": "gp3", "AllocatedStorage": 20}


def _fake_mcp(rgt_arns, scheme="internet-facing", assign="ENABLED", desired=1):
    """A full offline aws MCP: RGT inventory + Cloud Control describes + ECS + Price List, for the audited
    umami architecture. ``rgt_arns`` selects which resources are TAGGED (and therefore enumerated)."""
    alb_props = dict(_ALB_PROPS, Scheme=scheme)

    def call(cli):
        if "resourcegroupstaggingapi get-resources" in cli:
            return {"ResourceTagMappingList": [{"ResourceARN": a, "Tags": [{"Key": "Name",
                     "Value": f"umami-{TOKEN}"}]} for a in rgt_arns]}
        if "cloudcontrol get-resource" in cli:
            if "ElasticLoadBalancingV2" in cli:
                return _cc(alb_props)
            if "AWS::EC2::Instance" in cli:
                return _cc(_EC2_PROPS)
            if "AWS::RDS::DBInstance" in cli:
                return _cc(_RDS_PROPS)
            return {}
        if "ecs describe-services" in cli:
            return {"services": [{"launchType": "FARGATE", "desiredCount": desired,
                                  "taskDefinition": "umami:1",
                                  "networkConfiguration": {"awsvpcConfiguration": {"assignPublicIp": assign}}}]}
        if "ecs describe-task-definition" in cli:
            return {"taskDefinition": {"cpu": "512", "memory": "1024"}}
        if "pricing get-products" in cli:
            return _route_price(cli)
        return {}
    return call


def _names(rr):
    return [c.name for c in rr.components]


class TestPublicIpv4Synthesis(unittest.TestCase):
    """GAP 8: Fargate assignPublicIp=ENABLED and an internet-facing ALB each add public-IPv4 line(s)."""

    def test_enumerate_synthesizes_fargate_and_alb_public_ipv4(self):
        res = _enumerate(_fake_mcp([_ALB_ARN, _ECS_ARN, _RDS_ARN]), FARGATE_URL, "us-east-1")
        ips = [r for r in res if (r["service"], r["resource_type"]) == ("ec2", "elastic-ip")]
        # one synthetic row for the Fargate task (count 1) + one for the 2-AZ internet-facing ALB (count 2)
        self.assertEqual(sorted(r["count"] for r in ips), [1, 2])
        self.assertEqual(sum(r["count"] for r in ips), 3)          # 3 in-use public IPv4 total
        self.assertTrue(all("public-ipv4" in r["arn"] for r in ips))
        self.assertTrue(any("Fargate" in r["_synthetic"] for r in ips))
        self.assertTrue(any("ALB" in r["_synthetic"] for r in ips))

    def test_fargate_public_ipv4_prices_and_appears_as_a_line(self):
        rr = AwsRunRateAdapter(mcp_call=_fake_mcp([_ALB_ARN, _ECS_ARN, _RDS_ARN])).run_rate(
            FARGATE_URL, capture_date="2026-08-29")
        self.assertIsNotNone(rr)
        ip_comps = [c for c in rr.components if c.name == "public_ip"]
        self.assertEqual(len(ip_comps), 2)                          # Fargate(1) + ALB(2 AZ)
        self.assertAlmostEqual(sum(c.hourly_usd for c in ip_comps), 0.015, places=6)  # 3 x $0.005/hr
        self.assertAlmostEqual(sum(c.quantity for c in ip_comps), 3)

    def test_assign_disabled_adds_no_fargate_ip(self):
        # a Fargate task WITHOUT a public IP still leaves the ALB's 2 AZ addresses, but no task address
        res = _enumerate(_fake_mcp([_ALB_ARN, _ECS_ARN], assign="DISABLED"), FARGATE_URL, "us-east-1")
        ips = [r for r in res if (r["service"], r["resource_type"]) == ("ec2", "elastic-ip")]
        self.assertEqual(sum(r["count"] for r in ips), 2)          # ALB only

    def test_internal_alb_has_no_public_ipv4(self):
        self.assertIsNone(_alb_ipv4_hint({"Scheme": "internal", "Subnets": ["s1", "s2"]}, "us-east-1", "a"))
        hint = _alb_ipv4_hint(_ALB_PROPS, "us-east-1", _ALB_ARN)
        self.assertEqual(hint["count"], 2)

    def test_synthesize_helper_skips_zero_counts(self):
        out = _synthesize_public_ipv4([{"count": 0, "region": "us-east-1", "arn": "x"},
                                       {"count": 2, "region": "us-east-1", "arn": "y"}])
        self.assertEqual([r["count"] for r in out], [2])


class TestEbsRootSynthesis(unittest.TestCase):
    """GAP 9: the EC2 variant's inline 20 GB gp3 root EBS volume is priced as a storage line."""

    def test_ebs_from_instance_builds_a_volume_resource(self):
        vols = _ebs_from_instance(_EC2_PROPS, "us-east-1", _EC2_ARN)
        self.assertEqual(len(vols), 1)
        v = vols[0]
        self.assertEqual((v["service"], v["resource_type"]), ("ec2", "volume"))
        self.assertEqual(v["attrs"]["volumeApiName"], "gp3")
        self.assertEqual(v["quantity"]["gb"], 20)

    def test_ec2_variant_prices_the_20gb_gp3_root(self):
        rr = AwsRunRateAdapter(mcp_call=_fake_mcp([_EC2_ARN])).run_rate(FARGATE_URL, capture_date="2026-08-29")
        self.assertIsNotNone(rr)
        self.assertIn("compute", _names(rr))                       # the instance-hour line
        storage = [c for c in rr.components if c.name == "storage"]
        self.assertEqual(len(storage), 1)                          # the synthesized root-EBS line
        self.assertAlmostEqual(storage[0].hourly_usd, 0.08 * 20 / HOURS_PER_MONTH, places=6)  # ~$1.6/mo
        self.assertAlmostEqual(storage[0].quantity, 20)

    def test_gp3_root_at_baseline_charges_only_storage_not_iops(self):
        # 3000 IOPS / 125 MBps is the free baseline, so only the GB line bills (no iops/throughput line)
        rr = AwsRunRateAdapter(mcp_call=_fake_mcp([_EC2_ARN])).run_rate(FARGATE_URL, capture_date="2026-08-29")
        self.assertNotIn("storage:iops", _names(rr))
        self.assertNotIn("storage:throughput", _names(rr))


class TestLogsClassification(unittest.TestCase):
    """GAP 11: logs:log-group is disclosed as usage-priced, not dumped as unclassified."""

    def test_dimension_row_marks_logs_as_usage(self):
        self.assertEqual(aws_cost._DIMENSIONS[("logs", "log-group")], {"kind": "usage"})

    def test_log_group_is_disclosed_as_usage_priced_not_unclassified(self):
        res = [{"arn": _EC2_ARN, "service": "ec2", "resource_type": "instance", "region": "us-east-1",
                "attrs": {"instanceType": "t3.medium", "operatingSystem": "Linux"}},
               {"arn": "arn:aws:logs:us-east-1:1:log-group:/ecs/umami:*", "service": "logs",
                "resource_type": "log-group", "region": "us-east-1", "attrs": {}}]

        def price(service_code, filters):
            return _price_list(0.0416, "Hrs") if service_code == "AmazonEC2" else {"PriceList": []}
        rr = CostAdapter(enumerate_resources=lambda _r: res, get_products=price).run_rate(
            "dep", capture_date="2026-08-29")
        self.assertIsNotNone(rr)
        self.assertIn("logs:log-group (usage-priced, $0 standing)", rr.price_source)
        self.assertNotIn("logs:log-group (unclassified)", rr.price_source)


class TestTagGatedDisclosure(unittest.TestCase):
    """GAP 10: a tag-gated enumeration that returns nothing (or an obviously incomplete set) is DISCLOSED,
    never reported as a confident low/zero number."""

    def test_empty_enumeration_yields_a_disclosed_note_not_a_zero(self):
        note = _completeness_note([], FARGATE_URL)
        self.assertTrue(note)
        self.assertIn("NO resources", note)
        self.assertIn("not $0", note)

    def test_empty_enumeration_prices_to_none_and_records_the_disclosure(self):
        empty = _fake_mcp([])                                       # nothing tagged -> RGT returns []
        adapter = AwsRunRateAdapter(mcp_call=empty)
        rr = adapter.run_rate(FARGATE_URL, capture_date="2026-08-29")
        self.assertIsNone(rr)                                       # NOT a silent $0 RunRate
        self.assertTrue(adapter.disclosures)                       # the gap is surfaced, not swallowed
        self.assertIn("tag-gated", adapter.disclosures[0])

    def test_partial_bundle_missing_compute_is_flagged_as_a_lower_bound(self):
        # only the ALB is tagged; the Fargate compute + RDS behind it are untagged and omitted
        rr = _general_run_rate(_fake_mcp([_ALB_ARN]), FARGATE_URL, "us-east-1", "2026-08-29")
        self.assertIsNotNone(rr)                                    # the ALB itself priced
        self.assertIn("LOWER BOUND", rr.price_source)
        self.assertIn("INCOMPLETE", rr.price_source)

    def test_complete_bundle_has_no_incompleteness_note(self):
        rr = _general_run_rate(_fake_mcp([_ALB_ARN, _ECS_ARN, _RDS_ARN]), FARGATE_URL, "us-east-1",
                               "2026-08-29")
        self.assertIsNotNone(rr)
        self.assertNotIn("LOWER BOUND", rr.price_source)           # compute is present -> not flagged


class TestAuditedArchitectureEndToEnd(unittest.TestCase):
    """The whole Fargate + ALB + RDS bundle, priced through the injected MCP, carries every real charge."""

    def test_all_charges_present_including_the_previously_missing_ones(self):
        rr = AwsRunRateAdapter(mcp_call=_fake_mcp([_ALB_ARN, _ECS_ARN, _RDS_ARN])).run_rate(
            FARGATE_URL, capture_date="2026-08-29")
        self.assertIsNotNone(rr)
        names = set(_names(rr))
        self.assertIn("load_balancer", names)                      # ALB hour
        self.assertIn("compute:fargate-vcpu", names)               # task compute
        self.assertIn("compute:rds", names)                        # database
        self.assertIn("public_ip", names)                          # GAP 8: the newly-priced IPv4 lines
        # the public IPv4 charge is a real, non-zero contribution to the standing rate
        ip_hourly = sum(c.hourly_usd for c in rr.components if c.name == "public_ip")
        self.assertGreater(ip_hourly, 0)


if __name__ == "__main__":
    unittest.main()
