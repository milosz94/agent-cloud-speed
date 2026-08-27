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

from ..cost import RateComponent, RunRate, compose_run_rate
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
    return host.split(".")[0]


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


def _enumerate(mcp_call: McpCall, url: str, region: str) -> List[dict]:
    """Uniform enumeration of THIS deployment's billed resources via the Resource Groups Tagging API, matched
    by the app name appearing in an ARN or a tag value. Each is shaped for aws_cost's pricer."""
    app = _app_name(url)
    try:
        r = mcp_call(f"aws resourcegroupstaggingapi get-resources --region {region}")
    except Exception:  # noqa: BLE001 - enumeration failure is disclosed by an empty bundle, never faked
        return []
    out = []
    for m in (r.get("ResourceTagMappingList") or []):
        arn = m.get("ResourceARN", "")
        tagvals = " ".join(str(t.get("Value", "")) for t in (m.get("Tags") or []))
        if app and app not in arn and app not in tagvals:
            continue                                     # not this deployment's resource
        service, reg, restype = aws_cost._parse_arn(arn)
        if not service:
            continue
        ident = arn.split("/")[-1].split(":")[-1]
        attrs = _describe_attrs(mcp_call, service, restype, ident, reg or region)
        out.append({"arn": arn, "service": service, "resource_type": restype,
                    "region": reg or region, "attrs": attrs, "quantity": {}, "count": 1})
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


class AwsRunRateAdapter:
    """General AWS run-rate via the aws MCP: uniform-inventory enumerate + Price-List dimension pricing, with
    Lightsail as the single live-priced enumeration exception. Injectable ``mcp_call`` for offline tests."""

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
        return _general_run_rate(call, url, reg, capture_date)
