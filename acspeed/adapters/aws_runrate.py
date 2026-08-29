"""General AWS standing hourly RUN-RATE (C19), reached through the same aws MCP the agent deployed with.

NO per-service price code. Two service-agnostic steps (the aws_cost.py generalization, now MCP-backed):
  1. ENUMERATE the deployment's resources via AWS's OWN COMPLETE inventory. The default is AWS Resource
     Explorer 2 (`resource-explorer-2 search`), whose index spans ALL services and is NOT tag-gated, so a
     resource the agent never tagged is still found; it is scoped to THIS deploy by the harness run-token (then
     app name) as the free-text query. The Resource Groups Tagging API (`get-resources`) is kept as a fallback,
     and AWS Config as a last-ditch fallback. WHY the change: RGT is tag-gated, so any untagged or untaggable
     resource (public IPv4, inline root EBS) is silently missed and the run-rate is biased LOW; redu avoids
     this because its billing enumerates every resource, and Resource Explorer is AWS's equivalent.
  2. PRICE each via the AWS Price List (`pricing get-products`) by BILLING DIMENSION -- 250 services collapse
     to the ~8-row `aws_cost._DIMENSIONS` table (shared across services; adding one is a DATA row, not code).
A resource type not in the dimension table, or a service the Price List does not cover, is DISCLOSED by type
(`unpriced_resources`), never faked -- exactly like the redu adapter. Discovery is exhaustive; pricing is
best-effort-with-disclosure: every discovered resource is priced OR disclosed, never a silent $0.

The ONE unavoidable exception is AWS's own doing: Lightsail is NOT in the Resource Groups Tagging API, so it
is enumerated by its own list call and priced from its own LIVE price API (`get-container-service-powers`).
That is a live fetch, not a hardcoded table, and it is the whole special-casing -- one service AWS excludes
from its uniform inventory, disclosed, not a 250-row price sheet.

This same file serves GCP/Azure by swapping only the enumerate/price CLI verbs (Cloud Asset Inventory +
Billing Catalog; Resource Graph + Retail Prices) -- never per-service logic.
"""
from __future__ import annotations

import dataclasses
import json
import re
import time
from typing import Callable, List, Optional
from urllib.parse import urlparse

from ..cost import (RateComponent, RunRate, compose_run_rate,
                    UsageComponent, UsageRate, compose_usage_rate)
from . import aws_cost   # the general dimension engine: _DIMENSIONS, _parse_arn, _filters, _quantity, _price_list_hourly_usd

HOURS_PER_MONTH = 730.0
McpCall = Callable[[str], dict]

_AWS_MCP_ENDPOINT = "https://aws-mcp.us-east-1.api.aws/mcp"
_REGION_RE = re.compile(r"\.([a-z]{2}-[a-z]+-\d)\.")


def _region_from_url(url: str, default: str = "us-east-1") -> str:
    m = _REGION_RE.search(url or "")
    return m.group(1) if m else default


def _app_name(url: str) -> str:
    host = urlparse(url if "://" in (url or "") else f"https://{url}").hostname or ""
    first = host.split(".")[0]
    # ELB DNS is <lb-name>-<numeric hash>.<region>.elb.amazonaws.com; the resource ARNs carry <lb-name>,
    # NOT <lb-name>-<hash>, so strip the trailing numeric hash or enumeration matches nothing.
    if "elb.amazonaws.com" in host:
        first = re.sub(r"-\d+$", "", first)
    return first


def _match_anchors(url: str) -> List[str]:
    """Substrings that identify THIS deployment's billed resources in an ARN or tag value. The harness
    run-token (acs<hex>, present in the URL by construction and carried into the deployment's resource
    names) is the most reliable anchor; the app base name is the fallback."""
    host = (urlparse(url if "://" in (url or "") else f"https://{url}").hostname or "").lower()
    anchors: List[str] = []
    m = re.search(r"acs[0-9a-f]{6,}", host)          # the run token, the deployment's identity
    if m:
        anchors.append(m.group(0))
    app = _app_name(url)
    if app and app not in anchors:
        anchors.append(app)
    return anchors


def _unwrap(result: object) -> dict:
    """tools/call result -> the AWS CLI JSON it printed. Raises on an MCP error (disclosed, never faked)."""
    if isinstance(result, dict) and result.get("isError"):
        raise RuntimeError(f"aws MCP isError: {str(result)[:200]}")
    if isinstance(result, dict) and isinstance(result.get("structuredContent"), dict):
        sc = result["structuredContent"]
        if sc and set(sc.keys()) != {"result"}:
            return sc
    text = ""
    if isinstance(result, dict):
        for part in (result.get("content") or []):
            if isinstance(part, dict) and part.get("type") == "text":
                text += part.get("text", "")
    return json.loads(text) if text else {}


def _default_mcp_call(profile: Optional[str] = None) -> McpCall:
    from .mcp_client import MCPClient, StdioTransport
    args = ["uvx", "mcp-proxy-for-aws@latest", _AWS_MCP_ENDPOINT, "--metadata", "INSTALL_SOURCE=aws-cli"]
    if profile:
        args += ["--profile", profile]
    client = MCPClient(StdioTransport(args))
    client.initialize()

    def call(cli: str) -> dict:
        # Retry a transient MCP/network error a few times with backoff so one blip does not zero the cost.
        # An auth/permission error just exhausts the retries and raises (same as before) - use a static IAM
        # key for a batch so aws auth cannot lapse mid-run.
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                return _unwrap(client.call_tool("aws___call_aws", {"cli_command": cli}))
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(min(2.0 * (attempt + 1), 8.0))
        raise last if last else RuntimeError("aws mcp call failed")
    return call


# --- the general path: uniform tag inventory -> Price List by dimension (reuses aws_cost.py) -------------

# Cloud Control type-name from an ARN's (service, resource_type): AWS::<Service>::<Type>. A small, shared map
# for the capitalizations that are not a plain title-case (data, not per-service pricing).
_CFN_SERVICE = {"ec2": "EC2", "rds": "RDS", "elasticloadbalancing": "ElasticLoadBalancingV2",
                "elasticache": "ElastiCache", "s3": "S3", "lambda": "Lambda", "dynamodb": "DynamoDB"}
_CFN_TYPE = {"instance": "Instance", "db": "DBInstance", "loadbalancer": "LoadBalancer",
             "volume": "Volume", "cluster": "CacheCluster", "natgateway": "NatGateway"}


def _describe_attrs(mcp_call: McpCall, service: str, restype: str, ident: str, region: str) -> dict:
    """Best-effort UNIFORM describe via Cloud Control (get-resource) for the pricing attributes. Returns {}
    on any failure; the caller then discloses that resource unpriced rather than guessing."""
    svc = _CFN_SERVICE.get(service, service.upper())
    typ = _CFN_TYPE.get(restype, restype.title())
    try:
        r = mcp_call(f"aws cloudcontrol get-resource --type-name AWS::{svc}::{typ} "
                     f"--identifier {ident} --region {region}")
        props = (r.get("ResourceDescription") or {}).get("Properties")
        return json.loads(props) if isinstance(props, str) else (props or {})
    except Exception:  # noqa: BLE001
        return {}


_RDS_ENGINE = {"postgres": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB",
               "aurora-postgresql": "Aurora PostgreSQL", "aurora-mysql": "Aurora MySQL"}
_RDS_VOLTYPE = {"gp2": "General Purpose", "gp3": "General Purpose",
                "io1": "Provisioned IOPS", "io2": "Provisioned IOPS", "standard": "Magnetic"}


def _normalize_attrs(service: str, restype: str, props: dict):
    """Map raw Cloud Control Properties (PascalCase: DBInstanceClass, Engine, MultiAZ, InstanceType) to the
    fields aws_cost needs. Returns ``(filter_attrs, quantity)``: the FILTER fields for GetProducts
    (instanceType/databaseEngine/deploymentOption/volumeType), and the provisioned AMOUNTS (gb/iops/...)
    for _quantity, which reads ``resource['quantity']`` NOT ``attrs``. WITHOUT the filter mapping the
    RDS/EC2 filters never match the props, so GetProducts returns every SKU and the pricer picks a wrong
    (expensive) one -- measured: db.t4g.micro priced $0.50/hr, real $0.016/hr."""
    p = props or {}
    if (service, restype) == ("rds", "db"):
        eng = str(p.get("Engine", "")).lower()
        return ({"instanceType": p.get("DBInstanceClass", ""),
                 "databaseEngine": _RDS_ENGINE.get(eng, eng.title()),
                 "deploymentOption": "Multi-AZ" if p.get("MultiAZ") else "Single-AZ",
                 "volumeType": _RDS_VOLTYPE.get(str(p.get("StorageType", "")).lower(), "General Purpose")},
                {"gb": float(p.get("AllocatedStorage", 0) or 0)})
    if (service, restype) == ("ec2", "instance"):
        return {"instanceType": p.get("InstanceType", ""), "operatingSystem": "Linux"}, {}
    if (service, restype) == ("elasticache", "cluster"):
        return {"instanceType": p.get("CacheNodeType", ""), "cacheEngine": str(p.get("Engine", "")).title()}, {}
    if (service, restype) == ("ec2", "volume"):
        return ({"volumeApiName": p.get("VolumeType", "")},
                {"gb": float(p.get("Size", 0) or 0), "iops": float(p.get("Iops", 0) or 0),
                 "throughput_mbps": float(p.get("StorageThroughput") or p.get("Throughput") or 0)})
    return dict(p), {}   # unknown type: pass the raw props through as filter attrs (best-effort)


def _fargate_resource(mcp_call: McpCall, arn: str, region: str) -> Optional[dict]:
    """An ECS SERVICE bills as Fargate vCPU-hours + GB-hours, but the vCPU/mem live on its TASK DEFINITION,
    not the service. Two hops: the service -> its taskDefinition -> its cpu/memory (x desiredCount). Returns
    a synthetic ('ecs-fargate','task') resource for the pricer, or None if it is EC2-launch / scaled to 0 /
    unresolved (disclosed as unpriced, never faked)."""
    try:
        cluster, service_name = arn.split(":service/", 1)[1].split("/", 1)
        s = mcp_call(f"aws ecs describe-services --cluster {cluster} --services {service_name} --region {region}")
        svc = (s.get("services") or [{}])[0]
        if str(svc.get("launchType", "")).upper() == "EC2":
            return None
        td_ref = svc.get("taskDefinition")
        desired = float(svc.get("desiredCount", 1) or 1)
        if not td_ref or desired <= 0:
            return None
        td = (mcp_call(f"aws ecs describe-task-definition --task-definition {td_ref} --region {region}")
              or {}).get("taskDefinition") or {}
        vcpus = float(td.get("cpu", 0) or 0) / 1024.0        # 512 -> 0.5 vCPU
        gb = float(td.get("memory", 0) or 0) / 1024.0        # 1024 -> 1 GB
        if vcpus <= 0 or gb <= 0:
            return None
        fr = {"arn": arn, "service": "ecs-fargate", "resource_type": "task", "region": region,
              "attrs": {}, "quantity": {"vcpus": vcpus * desired, "gb": gb * desired}, "count": 1}
        # GAP 8(a): a task launched with assignPublicIp=ENABLED gets ONE auto-assigned public IPv4 per task.
        # It is not an Elastic IP and is not taggable, so RGT never returns it -- carry a hint to synthesize
        # its $0.005/hr line after enumeration (see _synthesize_public_ipv4).
        awsvpc = ((svc.get("networkConfiguration") or {}).get("awsvpcConfiguration")) or {}
        if str(awsvpc.get("assignPublicIp", "")).upper() == "ENABLED":
            fr["_ipv4"] = {"count": int(desired), "region": region,
                           "source": "Fargate task assignPublicIp=ENABLED (one public IPv4 per task)"}
        return fr
    except Exception:  # noqa: BLE001
        return None


# --- GAP 8/9: charges that RGT never returns, SYNTHESIZED from resources already enumerated ---------------
#
# Two real standing charges are invisible to the tag inventory:
#   8. an AUTO-ASSIGNED public IPv4 (a Fargate task with assignPublicIp=ENABLED, or an internet-facing ALB's
#      per-AZ address) is NOT an Elastic IP and is NOT taggable, so get-resources never lists it, yet since
#      2024-02-01 every in-use public IPv4 bills $0.005/hr (AmazonVPC "PublicIPv4:InUseAddress");
#   9. an EC2 instance's ROOT EBS volume, created inline by RunInstances BlockDeviceMappings, is usually
#      untagged, so it too is omitted, yet a 20 GB gp3 root is a real ~$0.08/GB-mo standing charge.
# Both are synthesized from the resource that DOES enumerate (the ECS service / ALB / EC2 instance), then
# priced through the SAME dimension engine (the elastic-ip and ec2-volume dimensions already in aws_cost),
# so this adds NO new pricing code -- only synthetic resource rows.

def _alb_az_count(props: dict) -> int:
    """Number of AZs an ALB is mapped into -- one auto-assigned public IPv4 each when internet-facing. Read
    from whichever of Subnets / SubnetMappings / AvailabilityZones the Cloud Control props expose."""
    for key in ("Subnets", "SubnetMappings", "AvailabilityZones"):
        v = (props or {}).get(key)
        if isinstance(v, list) and v:
            return len(v)
    return 0


def _alb_ipv4_hint(props: dict, region: str, arn: str) -> Optional[dict]:
    """GAP 8(b): an INTERNET-FACING ALB consumes one public IPv4 per mapped AZ. An internal ALB has none."""
    if "internet-facing" not in str((props or {}).get("Scheme", "")).lower():
        return None
    azs = _alb_az_count(props)
    if azs <= 0:
        return None
    return {"count": azs, "region": region, "arn": arn,
            "source": "internet-facing ALB (one public IPv4 per mapped AZ)"}


def _synthesize_public_ipv4(hints: List[dict]) -> List[dict]:
    """Turn public-IPv4 hints into synthetic resources priced by the existing ('ec2','elastic-ip') dimension
    (the billing dimension is identical -- AmazonVPC PublicIPv4:InUseAddress -- for auto-assigned and Elastic
    addresses alike). ``count`` carries the number of in-use addresses; each bills $0.005/hr."""
    out = []
    for h in hints:
        n = int(h.get("count", 0) or 0)
        if n <= 0:
            continue
        out.append({"arn": f"{h.get('arn', '')}#public-ipv4", "service": "ec2",
                    "resource_type": "elastic-ip", "region": h.get("region", ""),
                    "attrs": {}, "quantity": {}, "count": n, "_synthetic": h.get("source", "")})
    return out


def _ebs_from_instance(props: dict, region: str, arn: str) -> List[dict]:
    """GAP 9: synthesize the root (and any inline) EBS volume from an EC2 instance's RunInstances
    BlockDeviceMappings, priced by the existing ('ec2','volume') dimension. Synthesized from the instance
    (not describe-volumes) precisely because the auto-created root volume is usually untagged and so is
    invisible to the tag inventory -- pricing it here is what closes the gap without a double count."""
    out = []
    for bdm in (props or {}).get("BlockDeviceMappings") or []:
        ebs = bdm.get("Ebs") or {}
        size = float(ebs.get("VolumeSize", 0) or 0)
        if size <= 0:
            continue
        vtype = str(ebs.get("VolumeType", "gp3") or "gp3")
        out.append({"arn": f"{arn}#ebs:{bdm.get('DeviceName', 'root')}", "service": "ec2",
                    "resource_type": "volume", "region": region,
                    "attrs": {"volumeApiName": vtype},
                    "quantity": {"gb": size, "iops": float(ebs.get("Iops", 0) or 0),
                                 "throughput_mbps": float(ebs.get("Throughput", 0) or 0)},
                    "count": 1, "_synthetic": "EBS root volume from RunInstances BlockDeviceMappings"})
    return out


# --- GAP 10: the tag inventory is TAG-GATED. Disclose an empty/incomplete result, never a confident low $. -

def _completeness_note(resources: List[dict], url: str) -> str:
    """RGT (get-resources with no TagFilters) returns only TAGGED resources; an inconsistently tagged deploy
    can enumerate to nothing, or to a partial set that prices LOW. When the URL clearly resolves to a live
    AWS service, disclose that gap instead of reporting the too-low number as if it were complete."""
    host = (urlparse(url if "://" in (url or "") else f"https://{url}").hostname or "").lower()
    live = any(s in host for s in (".elb.amazonaws.com", ".amazonaws.com", "awsapprunner.com",
                                   "cloudfront.net", "amazonlightsail.com"))
    if not resources:
        if live:
            return ("Resource Groups Tagging API returned NO resources for this deployment, but the URL "
                    "resolves to a live AWS service; RGT is tag-gated, so an untagged Fargate/RDS/ALB is "
                    "silently omitted. Cost is NOT priced here (disclosure, not $0).")
        return ""
    has_compute = any((r.get("service"), r.get("resource_type")) in
                      (("ec2", "instance"), ("ecs-fargate", "task")) for r in resources)
    if live and not has_compute:
        return ("priced bundle may be INCOMPLETE: an internet-facing endpoint with no enumerated backend "
                "compute (RGT is tag-gated; untagged compute is omitted). Treat the figure as a LOWER BOUND.")
    return ""


# --- GAP 12: UNIVERSAL discovery. AWS's own COMPLETE inventory, then price EACH via the dimension engine. --
#
# The Resource Groups Tagging API is TAG-GATED: a resource the agent did not tag (it tags inconsistently, and
# public IPv4 / inline root EBS are untaggable) is silently missed, biasing the run-rate LOW. AWS's equivalent
# of redu's "enumerate every resource we provisioned" is Resource Explorer 2, whose index spans ALL services
# and is NOT tag-gated. So a resource TYPE never anticipated here is still DISCOVERED (from the complete
# inventory) and then priced-or-disclosed by the shared dimension engine -- with NO new code per type. The
# per-resource resolution (Cloud Control describe -> normalize -> ECS->Fargate hop -> synthesize the
# untaggable public-IPv4 / root-EBS lines) is factored out so it is identical for EVERY discovery source.


def _resource_from_arn(mcp_call: McpCall, arn: str, region: str):
    """One discovered ARN -> its priced resource dict(s) + any public-IPv4 hints, via Cloud Control describe
    + attribute normalize, the ECS->Fargate task hop, and inline root-EBS synthesis. SAME resolution for every
    discovery source (Resource Explorer / tags / Config): the enumerator that FINDS a resource is decoupled
    from how it is priced. Returns (resources, ipv4_hints); ([], []) for an ARN that yields no billed line."""
    service, reg, restype = aws_cost._parse_arn(arn)
    if not service:
        return [], []
    reg = reg or region
    if (service, restype) == ("ecs", "service"):        # Fargate app compute: price via the task definition
        fr = _fargate_resource(mcp_call, arn, reg)
        if not fr:
            return [], []
        hint = fr.pop("_ipv4", None)                     # keep the priced dict clean; hint drives synthesis
        return [fr], ([hint] if hint else [])
    ident = arn.split("/")[-1].split(":")[-1]
    props = _describe_attrs(mcp_call, service, restype, ident, reg)
    attrs, quantity = _normalize_attrs(service, restype, props)
    resources = [{"arn": arn, "service": service, "resource_type": restype,
                  "region": reg, "attrs": attrs, "quantity": quantity, "count": 1}]
    hints: List[dict] = []
    if (service, restype) == ("ec2", "instance"):                    # GAP 9: its untagged root EBS volume
        resources.extend(_ebs_from_instance(props, reg, arn))
    elif (service, restype) == ("elasticloadbalancing", "loadbalancer"):  # GAP 8(b): per-AZ public IPv4
        hint = _alb_ipv4_hint(props, reg, arn)
        if hint:
            hints.append(hint)
    return resources, hints


def _resources_from_arns(mcp_call: McpCall, arns: List[str], region: str) -> List[dict]:
    """Resolve a de-duplicated list of discovered ARNs to priced resource dicts, then append the synthesized
    public-IPv4 lines (GAP 8) for the auto-assigned addresses that NO inventory ever lists as a resource, so
    completeness never depends on which discovery source produced the ARNs."""
    out: List[dict] = []
    ipv4_hints: List[dict] = []
    seen = set()
    for arn in arns:
        if not arn or arn in seen:
            continue
        seen.add(arn)
        resources, hints = _resource_from_arn(mcp_call, arn, region)
        out.extend(resources)
        ipv4_hints.extend(hints)
    out.extend(_synthesize_public_ipv4(ipv4_hints))              # GAP 8: auto-assigned public IPv4 lines
    return out


def _enumerate_via_resource_explorer(mcp_call: McpCall, url: str, region: str) -> List[dict]:
    """DEFAULT complete enumeration: AWS Resource Explorer 2 (`resource-explorer-2 search`), whose index spans
    ALL services and is NOT tag-gated, scoped to THIS deploy by the harness run-token (then app name) as the
    free-text query. A resource the tag inventory would miss (untagged, or a type never anticipated here) is
    found here. Returns [] if Resource Explorer is not enabled / errors, so the caller falls back to tags."""
    for anchor in _match_anchors(url):
        try:
            r = mcp_call(f'aws resource-explorer-2 search --query-string "{anchor}" --region {region}')
        except Exception:  # noqa: BLE001 - no RE index in the account: disclosed by falling back, never faked
            continue
        arns = [item.get("Arn") or item.get("arn") for item in (r.get("Resources") or [])]
        arns = [a for a in arns if a]
        if arns:                                          # the run-token anchor is exact; the first hit wins
            return _resources_from_arns(mcp_call, arns, region)
    return []


def _enumerate_via_tags(mcp_call: McpCall, url: str, region: str) -> List[dict]:
    """FALLBACK enumeration via the Resource Groups Tagging API (`get-resources`), matched to THIS deploy by
    the run token / app name in an ARN or tag value. TAG-GATED -- only tagged resources are returned, which is
    exactly why Resource Explorer is the default; kept for an account with no Resource Explorer index."""
    anchors = _match_anchors(url)
    try:
        r = mcp_call(f"aws resourcegroupstaggingapi get-resources --region {region}")
    except Exception:  # noqa: BLE001 - enumeration failure is disclosed by an empty bundle, never faked
        return []
    arns: List[str] = []
    for m in (r.get("ResourceTagMappingList") or []):
        arn = m.get("ResourceARN", "")
        tagvals = " ".join(str(t.get("Value", "")) for t in (m.get("Tags") or []))
        if anchors and not any(a in arn or a in tagvals for a in anchors):
            continue                                     # not this deployment's resource
        arns.append(arn)
    return _resources_from_arns(mcp_call, arns, region)


def _enumerate_via_config(mcp_call: McpCall, url: str, region: str) -> List[dict]:
    """LAST-DITCH complete enumeration via AWS Config, for an account with neither a Resource Explorer index
    nor consistent tags. Config records every supported resource type continuously (NOT tag-gated). We ask its
    advanced-query API for the ARNs whose name/ARN carries this deploy's anchor (`select-resource-config`,
    which returns ARNs directly; the per-type `list-discovered-resources` is the alternative primitive).
    Best-effort; returns [] when Config is not recording, so nothing is faked."""
    for anchor in _match_anchors(url):
        expr = ("SELECT arn, resourceType, resourceName "
                f"WHERE resourceName LIKE '%{anchor}%' OR arn LIKE '%{anchor}%'")
        try:
            r = mcp_call(f'aws configservice select-resource-config --expression "{expr}" --region {region}')
        except Exception:  # noqa: BLE001 - Config not recording: disclosed by falling through, never faked
            continue
        arns: List[str] = []
        for row in (r.get("Results") or []):
            item = json.loads(row) if isinstance(row, str) else row
            a = (item or {}).get("arn") or (item or {}).get("Arn")
            if a:
                arns.append(a)
        if arns:
            return _resources_from_arns(mcp_call, arns, region)
    return []


def _enumerate(mcp_call: McpCall, url: str, region: str) -> List[dict]:
    """THE universal enumerator: AWS's own COMPLETE inventory first (Resource Explorer 2, not tag-gated), then
    the tag inventory, then AWS Config. The first source that returns anything wins; every discovered resource
    (of ANY service/type) is then priced-or-disclosed by the shared dimension engine. The untaggable
    auto-assigned public IPv4 and inline root EBS -- which NO inventory lists -- are synthesized from their
    parent inside _resources_from_arns, so discovery stays complete regardless of the source."""
    for source in (_enumerate_via_resource_explorer, _enumerate_via_tags, _enumerate_via_config):
        resources = source(mcp_call, url, region)
        if resources:
            return resources
    return []


def _general_run_rate(mcp_call: McpCall, url: str, region: str, capture_date: str,
                      disclosures: Optional[List[str]] = None,
                      unpriced_out: Optional[List[str]] = None,
                      enumerate_resources: Optional[Callable[[McpCall, str, str], List[dict]]] = None
                      ) -> Optional[RunRate]:
    """Price the COMPLETELY-enumerated resources via the shared Price List / dimension engine (aws_cost.py).
    Discovery is universal (Resource Explorer 2 -> tags -> Config); pricing is best-effort-with-disclosure.
    A tag-gated / empty-enumeration gap (GAP 10) is disclosed via ``disclosures`` and ``price_source``; every
    discovered-but-unpriced resource (an unknown (service,type), or a usage-priced one) is pushed BY TYPE to
    ``unpriced_out`` when a list is given, so a brand-new type is surfaced, never silently dropped. An
    ``enumerate_resources`` override injects a complete resource list directly (offline tests)."""
    enum = enumerate_resources or _enumerate
    resources = enum(mcp_call, url, region)
    note = _completeness_note(resources, url)
    if note and disclosures is not None:
        disclosures.append(note)

    def get_products(service_code, filters):
        flt = json.dumps(filters).replace("'", "\\'")
        return mcp_call(f"aws pricing get-products --service-code {service_code} "
                        f"--filters '{flt}' --region us-east-1 --output json")
    adapter = aws_cost.AwsRunRateAdapter(
        enumerate_resources=lambda _ref: resources,
        get_products=get_products)
    rr = adapter.run_rate(url, capture_date=capture_date)
    if unpriced_out is not None:                                  # disclosed by type, never silently dropped
        unpriced_out.extend(adapter.unpriced_resources)
    if rr is not None and note:
        rr = dataclasses.replace(rr, price_source=f"{rr.price_source} | NOTE: {note}")
    return rr


# --- the one AWS-forced exception: Lightsail (not in the tag inventory), priced from its LIVE price API ---

def _lightsail_run_rate(mcp_call: McpCall, url: str, region: str, capture_date: str) -> Optional[RunRate]:
    svcs = (mcp_call(f"aws lightsail get-container-services --region {region}") or {}).get(
        "containerServices", [])
    app = _app_name(url)
    u = (url or "").rstrip("/")
    svc = next((s for s in svcs if (s.get("url") or "").rstrip("/") in (u, u + "/") or u in (s.get("url") or "")),
               None) or next((s for s in svcs if s.get("containerServiceName") == app), None)
    if not svc:
        return None
    powers = (mcp_call(f"aws lightsail get-container-service-powers --region {region}") or {}).get("powers", [])
    price_month = next((float(p["price"]) for p in powers
                        if p.get("name") == svc.get("power") or p.get("powerId") == svc.get("powerId")), None)
    if price_month is None:
        return None
    scale = int(svc.get("scale", 1) or 1)
    comp = RateComponent(name="compute:lightsail-container", hourly_usd=price_month * scale / HOURS_PER_MONTH,
                         raw_unit_price=price_month, native_unit="power-month", quantity=float(scale))
    return compose_run_rate([comp], provider="aws", region=region,
                            flavor=f"lightsail-container:{svc.get('power')}x{scale}", capture_date=capture_date,
                            price_source="AWS Lightsail public list price (get-container-service-powers, live; "
                                         "Lightsail is not in the uniform tag inventory)")


# --- usage-metered fronts (serverless): report a per-usage SCHEDULE, not a standing $/hr ------------
#
# A CloudFront / App Runner / Lambda / API Gateway front has no standing hourly bill; its cost scales with
# usage. We price it by the SAME general move as the standing path: read the service's OWN Price List and
# classify each price dimension by its UNIT (Requests -> per-request, GB out -> egress, GB-Hours/instance
# -> standing floor, vCPU-* -> active compute), then emit a schedule over a fixed request grid. Adding a
# service is a DATA row in _USAGE_FRONTS, never per-service pricing code. This is the request-metered
# counterpart of the egress-held-separate rule (C19), generalized from egress to the whole service.

_USAGE_FRONTS = {   # host substring -> (Price List service-code, friendly service name)
    "cloudfront.net": ("AmazonCloudFront", "cloudfront"),
    "awsapprunner.com": ("AWSAppRunner", "apprunner"),
    "execute-api.": ("AmazonApiGateway", "apigateway"),
    "lambda-url.": ("AWSLambda", "lambda"),
    "on.aws": ("AWSLambda", "lambda"),
}

# region code -> acceptable Price List "location" names, most-specific first. Some usage-metered services
# (CloudFront) are priced by EDGE-REGION GROUP ("United States"), not the AWS region, so we accept the
# region-specific name AND its country/continent tier and take the first that matches (data, extend freely).
_REGION_LOCATION = {
    "us-east-1": ("US East (N. Virginia)", "United States"),
    "us-east-2": ("US East (Ohio)", "United States"),
    "us-west-1": ("US West (N. California)", "United States"),
    "us-west-2": ("US West (Oregon)", "United States"),
    "eu-west-1": ("EU (Ireland)", "Europe"),
    "eu-west-2": ("EU (London)", "Europe"),
    "eu-central-1": ("EU (Frankfurt)", "Europe"),
    "ap-southeast-1": ("Asia Pacific (Singapore)", "Asia Pacific"),
    "ap-northeast-1": ("Asia Pacific (Tokyo)", "Japan", "Asia Pacific"),
}

# Default App Runner provisioning (its smallest always-on instance) so the standing floor is concrete;
# disclosed as an assumption. 1 vCPU / 2 GB is App Runner's minimum configuration.
_APPRUNNER_DEFAULT_GB = 2.0

# Per-front completeness: which drivers a valid schedule MUST include, so an unpriced compute meter is
# reported UNPRICED rather than emitted as a plausible-but-incomplete (cheaper-than-real) number (C21).
# "compute" is satisfied by an active per-second rate (driver 'other') OR a provisioned instance floor
# (driver 'standing'). CloudFront (a CDN) and API Gateway have no compute of their own, so they need only
# the request line; App Runner and Lambda are compute services and must price their compute.
_USAGE_REQUIRED = {
    "cloudfront": ("requests",),
    "apigateway": ("requests",),
    "apprunner": ("compute",),
    "lambda": ("requests", "compute"),
}


def _usage_front(host: str):
    for sub, (code, name) in _USAGE_FRONTS.items():
        if sub in host:
            return code, name
    return None, None


def _classify_usage_unit(unit: str, product_family: str, desc: str):
    """Map a Price List price dimension to a usage driver by its UNIT string (general, not per-service).
    Returns (driver, human_unit) or (None, None) to skip a dimension we do not fold."""
    u, pf, d = (unit or "").lower(), (product_family or "").lower(), (desc or "").lower()
    if "request" in u:
        return "requests", "per request"
    if u.startswith("gb") and ("transfer" in pf or "transfer" in d or "data" in pf):
        return "egress", "per GB out"
    if "vcpu" in u:
        return "other", "vCPU-" + ("hour" if "hour" in u else "second")
    if "gb-second" in u:
        return "other", "GB-second"
    if "gb-hour" in u:                     # provisioned memory (App Runner etc.): always-on floor
        return "standing", "GB-hour (provisioned)"
    if u in ("hrs", "hours") and any(k in pf for k in ("instance", "compute", "runner")):
        return "standing", "instance-hour (provisioned)"
    return None, None


def _usage_components(products: dict, region: str, service_name: str) -> list:
    """Build ONE UsageComponent per driver from the service's Price List, picking the right meter:
    pricePerUnit is already per-single-unit (verified: CloudFront requests list at $0.00000075 = the
    per-request form of $0.0075/10k). We SKIP $0 lines and to-Origin transfer (not user-facing egress),
    and SCORE candidates by (location match for the deploy region + preferred meter: HTTPS request, to-
    Internet egress), keeping the best -- CloudFront prices by edge-region GROUP, not the AWS region."""
    locs = _REGION_LOCATION.get(region, ())
    if isinstance(locs, str):
        locs = (locs,)
    best = {}   # driver -> (UsageComponent, score)
    for raw in (products.get("PriceList") or []):
        prod = json.loads(raw) if isinstance(raw, str) else raw
        p = prod.get("product") or {}
        attrs = p.get("attributes") or {}
        pf = p.get("productFamily", "")
        loc = attrs.get("location", "")
        for term in ((prod.get("terms") or {}).get("OnDemand") or {}).values():
            for pdim in (term.get("priceDimensions") or {}).values():
                unit, desc = pdim.get("unit", ""), pdim.get("description", "")
                try:
                    usd = float((pdim.get("pricePerUnit") or {}).get("USD", "0") or 0)
                except ValueError:
                    usd = 0.0
                if usd <= 0:
                    continue
                driver, human = _classify_usage_unit(unit, pf, desc)
                if not driver:
                    continue
                dl = desc.lower()
                if driver == "egress" and "origin" in dl:        # to-Origin transfer, not user egress
                    continue
                score = (4 if loc in locs else (2 if not locs else 0))
                if driver == "requests" and "https" in dl:
                    score += 1
                if driver == "egress" and ("internet" in dl or " out" in dl):
                    score += 1
                if driver not in best or score > best[driver][1]:
                    per_unit = usd * _APPRUNNER_DEFAULT_GB if (driver == "standing" and "gb-hour" in human.lower()) else usd
                    best[driver] = (UsageComponent(name=f"{service_name}:{driver}", per_unit_usd=per_unit,
                                                   unit=human, driver=driver, raw_unit_price=usd,
                                                   native_unit=unit, tier=(loc or "")), score)
    return [best[d][0] for d in ("requests", "egress", "standing", "other") if d in best]


def _usage_rate(mcp_call: McpCall, url: str, region: str, capture_date: str, service_code: str,
                service_name: str) -> Optional[UsageRate]:
    try:
        products = mcp_call(f"aws pricing get-products --service-code {service_code} "
                            f"--region us-east-1 --output json")
    except Exception:  # noqa: BLE001 - unpriceable is disclosed as None, never faked
        return None
    comps = _usage_components(products, region, service_name)
    if not comps:
        return None
    drivers = {c.driver for c in comps}
    for need in _USAGE_REQUIRED.get(service_name, ("requests",)):
        ok = bool(drivers & {"other", "standing"}) if need == "compute" else (need in drivers)
        if not ok:
            return None   # a required cost component is unpriced -> UNPRICED, never a partial schedule
    return compose_usage_rate(comps, provider="aws", region=region, service=service_name,
                              capture_date=capture_date,
                              price_source=f"AWS {service_name} public list price (Price List get-products; "
                                           "usage-metered, priced as a per-usage schedule)")


class AwsRunRateAdapter:
    """General AWS run-rate via the aws MCP: COMPLETE-inventory enumerate (Resource Explorer 2, not tag-gated;
    tags then Config as fallbacks) + Price-List dimension pricing, with Lightsail as the single live-priced
    enumeration exception, and usage-metered fronts (CloudFront/App Runner/Lambda/API Gateway) priced as a
    per-usage SCHEDULE. Injectable ``mcp_call`` for offline tests; an ``enumerate_resources`` override injects
    a complete resource list directly. After run_rate, ``disclosures`` holds enumeration-gap notes and
    ``unpriced_resources`` lists every discovered-but-unpriced resource BY TYPE (never a silent $0)."""

    def __init__(self, mcp_call: Optional[McpCall] = None, profile: Optional[str] = None,
                 enumerate_resources: Optional[Callable[[McpCall, str, str], List[dict]]] = None):
        self._mcp_call = mcp_call
        self._profile = profile
        self._enumerate = enumerate_resources   # inject a COMPLETE resource list directly (offline tests)
        self.disclosures: List[str] = []        # enumeration gaps (GAP 10), readable after run_rate
        self.unpriced_resources: List[str] = [] # discovered-but-unpriced, disclosed by type, after run_rate

    def run_rate(self, deployment_ref: object, *, capture_date: str,
                 region: Optional[str] = None) -> Optional[RunRate]:
        self.disclosures = []
        self.unpriced_resources = []
        url = str(deployment_ref)
        reg = region or _region_from_url(url)
        call = self._mcp_call or _default_mcp_call(self._profile)
        host = (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()
        if "cs.amazonlightsail.com" in host:             # AWS-forced enumeration exception, live-priced
            return _lightsail_run_rate(call, url, reg, capture_date)
        code, name = _usage_front(host)                  # CloudFront/App Runner/Lambda/API Gateway
        if code:                                         # usage-metered -> a per-usage SCHEDULE, not $/hr
            return _usage_rate(call, url, reg, capture_date, code, name)
        return _general_run_rate(call, url, reg, capture_date, disclosures=self.disclosures,
                                 unpriced_out=self.unpriced_resources, enumerate_resources=self._enumerate)
