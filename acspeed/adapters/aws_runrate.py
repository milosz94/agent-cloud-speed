"""General AWS standing hourly RUN-RATE (C19), reached through the same aws MCP the agent deployed with.

NO per-service price code. Two service-agnostic steps (the aws_cost.py generalization, now MCP-backed):
  1. ENUMERATE the deployment's resources via the cloud's UNIFORM inventory (Resource Groups Tagging API,
     `get-resources`), one call for all services, matched to THIS deploy by the app name in the ARN/tags.
  2. PRICE each via the AWS Price List (`pricing get-products`) by BILLING DIMENSION -- 250 services collapse
     to the ~8-row `aws_cost._DIMENSIONS` table (shared across services; adding one is a DATA row, not code).
A resource type not in the dimension table, or a service the Price List does not cover, is DISCLOSED
(`unpriced_resources`), never faked -- exactly like the redu adapter.

The ONE unavoidable exception is AWS's own doing: Lightsail is NOT in the Resource Groups Tagging API, so it
is enumerated by its own list call and priced from its own LIVE price API (`get-container-service-powers`).
That is a live fetch, not a hardcoded table, and it is the whole special-casing -- one service AWS excludes
from its uniform inventory, disclosed, not a 250-row price sheet.

This same file serves GCP/Azure by swapping only the enumerate/price CLI verbs (Cloud Asset Inventory +
Billing Catalog; Resource Graph + Retail Prices) -- never per-service logic.
"""
from __future__ import annotations

import json
import re
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
    return lambda cli: _unwrap(client.call_tool("aws___call_aws", {"cli_command": cli}))


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
        return {"arn": arn, "service": "ecs-fargate", "resource_type": "task", "region": region,
                "attrs": {}, "quantity": {"vcpus": vcpus * desired, "gb": gb * desired}, "count": 1}
    except Exception:  # noqa: BLE001
        return None


def _enumerate(mcp_call: McpCall, url: str, region: str) -> List[dict]:
    """Uniform enumeration of THIS deployment's billed resources via the Resource Groups Tagging API, matched
    by the run token / app name in an ARN or tag value. Attributes are normalized from Cloud Control props to
    the price-list filter fields; an ECS service is resolved to its Fargate task (cpu/mem)."""
    anchors = _match_anchors(url)
    try:
        r = mcp_call(f"aws resourcegroupstaggingapi get-resources --region {region}")
    except Exception:  # noqa: BLE001 - enumeration failure is disclosed by an empty bundle, never faked
        return []
    out = []
    for m in (r.get("ResourceTagMappingList") or []):
        arn = m.get("ResourceARN", "")
        tagvals = " ".join(str(t.get("Value", "")) for t in (m.get("Tags") or []))
        if anchors and not any(a in arn or a in tagvals for a in anchors):
            continue                                     # not this deployment's resource
        service, reg, restype = aws_cost._parse_arn(arn)
        if not service:
            continue
        if (service, restype) == ("ecs", "service"):    # Fargate app compute: price via the task definition
            fr = _fargate_resource(mcp_call, arn, reg or region)
            if fr:
                out.append(fr)
            continue
        ident = arn.split("/")[-1].split(":")[-1]
        attrs, quantity = _normalize_attrs(service, restype,
                                           _describe_attrs(mcp_call, service, restype, ident, reg or region))
        out.append({"arn": arn, "service": service, "resource_type": restype,
                    "region": reg or region, "attrs": attrs, "quantity": quantity, "count": 1})
    return out


def _general_run_rate(mcp_call: McpCall, url: str, region: str, capture_date: str) -> Optional[RunRate]:
    """Price the RGT-enumerated resources via the shared Price List / dimension engine (aws_cost.py)."""
    def get_products(service_code, filters):
        flt = json.dumps(filters).replace("'", "\\'")
        return mcp_call(f"aws pricing get-products --service-code {service_code} "
                        f"--filters '{flt}' --region us-east-1 --output json")
    adapter = aws_cost.AwsRunRateAdapter(
        enumerate_resources=lambda _ref: _enumerate(mcp_call, url, region),
        get_products=get_products)
    return adapter.run_rate(url, capture_date=capture_date)


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
    return compose_usage_rate(comps, provider="aws", region=region, service=service_name,
                              capture_date=capture_date,
                              price_source=f"AWS {service_name} public list price (Price List get-products; "
                                           "usage-metered, priced as a per-usage schedule)")


class AwsRunRateAdapter:
    """General AWS run-rate via the aws MCP: uniform-inventory enumerate + Price-List dimension pricing, with
    Lightsail as the single live-priced enumeration exception, and usage-metered fronts (CloudFront/App
    Runner/Lambda/API Gateway) priced as a per-usage SCHEDULE. Injectable ``mcp_call`` for offline tests."""

    def __init__(self, mcp_call: Optional[McpCall] = None, profile: Optional[str] = None):
        self._mcp_call = mcp_call
        self._profile = profile

    def run_rate(self, deployment_ref: object, *, capture_date: str,
                 region: Optional[str] = None) -> Optional[RunRate]:
        url = str(deployment_ref)
        reg = region or _region_from_url(url)
        call = self._mcp_call or _default_mcp_call(self._profile)
        host = (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()
        if "cs.amazonlightsail.com" in host:             # AWS-forced enumeration exception, live-priced
            return _lightsail_run_rate(call, url, reg, capture_date)
        code, name = _usage_front(host)                  # CloudFront/App Runner/Lambda/API Gateway
        if code:                                         # usage-metered -> a per-usage SCHEDULE, not $/hr
            return _usage_rate(call, url, reg, capture_date, code, name)
        return _general_run_rate(call, url, reg, capture_date)
