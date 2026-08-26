"""redu RunRateAdapter (C19 cost axis): resolve a deployment's provisioned bundle + its PUBLIC LIST
prices into a provider-independent RunRate. Thin per-provider boundary, like ReduCapabilityAdapter.

The pricing resolution is injectable (``resolve_bundle``) so the composition is tested offline; the
default resolver best-effort-fetches from the redu MCP (list_flavors for the compute rate, the
deployment's volumes / public IP), defensively parsed. If it cannot price the bundle it returns None
(disclosed, never faked) -- exactly like the capability pass, and iterated the same way against real
deploys. Only PUBLIC ON-DEMAND LIST prices are used; discounts/spot/reserved are excluded by C19.
"""
from __future__ import annotations

from typing import Callable, Optional

from ..cost import (RateComponent, RunRate, RunRateAdapter, EgressRate,
                    compose_run_rate, storage_gb_month_to_hourly)
from . import redu_mcp_http

# a resolver returns a priced-bundle dict (see _default_resolver's shape) or None if it cannot price it
BundleResolver = Callable[[object], Optional[dict]]


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _default_resolver(call_tool=redu_mcp_http.call_tool) -> BundleResolver:
    """Best-effort redu resolver: find the deployment, its flavor's public list hourly rate, its volume
    GB + storage list rate, and whether it holds a public IP. Returns None if it cannot price the
    compute (the load-bearing term). Field names are parsed defensively; unknown redu pricing fields are
    a TODO to pin against a real deploy (like the capability adapter's field iteration)."""
    def resolve(deployment_ref: object) -> Optional[dict]:
        deps = (call_tool("list_deployments", {}) or {}).get("deployments", [])
        dep = next((d for d in deps
                    if str(_first(d, "id", "instance_id", "name")) == str(deployment_ref)), None)
        if dep is None:
            return None
        flavor = _first(dep, "flavor", "flavor_name", "flavor_id", default="")
        region = _first(dep, "region", default="")
        flavors = (call_tool("list_flavors", {}) or {}).get("flavors", [])
        fl = next((f for f in flavors
                   if str(_first(f, "name", "id")) == str(flavor)), None)
        compute_hr = _first(fl or {}, "price_per_hour", "hourly_usd", "usd_per_hour", "rate_hourly")
        if compute_hr is None:
            return None                                   # cannot price compute -> disclose, do not fake
        gb = float(_first(dep, "volume_gb", "storage_gb", "disk_gb", default=0) or 0)
        storage_gb_month = _first(fl or {}, "storage_usd_per_gb_month", "ebs_usd_per_gb_month")
        ip_hr = _first(dep, "public_ip_hourly_usd") if _first(dep, "public_ip", "floating_ip") else None
        return {
            "flavor": str(flavor), "region": str(region),
            "compute_hourly_usd": float(compute_hr),
            "storage_gb": gb,
            "storage_usd_per_gb_month": (None if storage_gb_month is None else float(storage_gb_month)),
            "public_ip_hourly_usd": (None if ip_hr is None else float(ip_hr)),
            "ancillary": [],                              # {name, hourly_usd, raw_unit_price, native_unit}
            "egress": None,                               # {per_gb_usd, tier}
            "price_source": "redu public on-demand list",
            "price_urls": [],
        }
    return resolve


class ReduRunRateAdapter(RunRateAdapter):
    def __init__(self, resolve_bundle: Optional[BundleResolver] = None, call_tool=None):
        self._resolve = resolve_bundle or _default_resolver(call_tool or redu_mcp_http.call_tool)

    def run_rate(self, deployment_ref: object, *, capture_date: str) -> Optional[RunRate]:
        """Compose the deployment's standing hourly run-rate from its priced bundle. ``capture_date`` is
        passed in (the harness stamps the test day) so the module never reads the clock."""
        b = self._resolve(deployment_ref)
        if not b:
            return None
        comps = [RateComponent(name="compute", hourly_usd=b["compute_hourly_usd"],
                               raw_unit_price=b["compute_hourly_usd"], native_unit="instance-hour")]
        if b.get("storage_usd_per_gb_month") is not None and b.get("storage_gb"):
            comps.append(RateComponent(
                name="storage",
                hourly_usd=storage_gb_month_to_hourly(b["storage_usd_per_gb_month"], b["storage_gb"]),
                raw_unit_price=b["storage_usd_per_gb_month"], native_unit="GB-month",
                quantity=b["storage_gb"]))
        if b.get("public_ip_hourly_usd") is not None:
            comps.append(RateComponent(name="public_ip", hourly_usd=b["public_ip_hourly_usd"],
                                       raw_unit_price=b["public_ip_hourly_usd"], native_unit="hour"))
        for a in b.get("ancillary", []):
            comps.append(RateComponent(name=a["name"], hourly_usd=a["hourly_usd"],
                                       raw_unit_price=a.get("raw_unit_price", a["hourly_usd"]),
                                       native_unit=a.get("native_unit", "hour")))
        eg = b.get("egress")
        egress = EgressRate(per_gb_usd=eg["per_gb_usd"], tier=eg.get("tier", "unspecified")) if eg else None
        return compose_run_rate(comps, provider="redu", region=b.get("region", ""), flavor=b["flavor"],
                                capture_date=capture_date, egress=egress,
                                price_source=b.get("price_source", "redu public on-demand list"),
                                price_urls=tuple(b.get("price_urls", ())))
