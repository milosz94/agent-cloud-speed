"""Azure RunRateAdapter (C19 cost axis): price a deployment's provisioned bundle from the PUBLIC Azure
Retail Prices API into a provider-independent RunRate. Thin per-provider boundary, like ReduRunRateAdapter.

Two halves, kept separate so the reproducible one is testable offline:

1. PRICING (this module's core, live-verifiable, no credentials): the Azure Retail Prices API
   (``https://prices.azure.com/api/retail/prices``) is PUBLIC and unauthenticated and lists prices in USD,
   so no FX is needed. ``vm_hourly_usd`` and ``retail_hourly_usd`` resolve a VM SKU or a managed-service
   meter to its on-demand $/hr, dropping the Windows / Spot / Low Priority meters that share the same SKU
   (the API has no OS field: the only disambiguator is the product / sku / meter name string). Verified live
   2026-08-28.

2. BUNDLE RESOLUTION (the injected ``resolve_bundle``, the live seam): WHAT the agent provisioned (which VM
   SKU + region, whether a managed Postgres) needs a cloud query (the Azure MCP / Resource Graph, with
   credentials). It is injectable exactly like ReduRunRateAdapter's resolver so the pricing + composition are
   tested offline; the default best-effort resolver queries via the Azure MCP and returns None (disclosed,
   never faked) when it cannot resolve the app compute.

Only PUBLIC on-demand list prices are used; discounts / spot / reserved / low-priority are excluded by C19.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Callable, List, Optional
from urllib.parse import urlparse

from ..cost import (RateComponent, RunRate, RunRateAdapter, EgressRate, UsageComponent, UsageRate,
                    compose_run_rate, compose_usage_rate, storage_gb_month_to_hourly)

_RETAIL_ENDPOINT = "https://prices.azure.com/api/retail/prices"
# meter-name substrings that mark a NON on-demand-Linux line sharing the same SKU (the API has no OS field)
_EXCLUDE_MARKERS = ("windows", "spot", "low priority")

# a resolver returns a priced-bundle dict (see _mcp_resolver's shape) or None if it cannot price it
BundleResolver = Callable[[object], Optional[dict]]


def _retail_query(filter_str: str, *, api_version: str = "2023-01-01-preview",
                  fetch=None, retries: int = 3) -> List[dict]:
    """Query the public Retail Prices API with an OData $filter, following @nextPageLink, with
    retry-on-429 backoff (the endpoint rate-limits). Returns the flat list of price Items. ``fetch`` is
    injectable for offline tests. Never raises on a transport error: returns what it has (a partial or
    empty list), so cost stays non-fatal."""
    if fetch is None:
        fetch = _http_get_json
    items: List[dict] = []
    url = f"{_RETAIL_ENDPOINT}?api-version={api_version}&$filter={urllib.parse.quote(filter_str)}"
    seen_pages = 0
    while url and seen_pages < 50:                       # hard page cap; the filters below return few pages
        data = None
        for attempt in range(retries):
            data = fetch(url)
            if data is not None:
                break
            time.sleep(1.5 * (attempt + 1))             # 429 backoff
        if not isinstance(data, dict):
            break
        items.extend(data.get("Items", []) or [])
        url = data.get("NextPageLink") or data.get("nextPageLink")
        seen_pages += 1
    return items


def _http_get_json(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "acspeed-cost/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:   # off-clock, host-side; never in the VM
            return json.load(r)
    except Exception:  # noqa: BLE001 - pricing is best-effort; a failure is disclosed, never faked
        return None


def _is_on_demand_linux(item: dict) -> bool:
    """Drop Windows / Spot / Low Priority meters (the API has no OS field; only the name strings separate
    them) and keep only the Consumption price type."""
    if str(item.get("type", "")).lower() != "consumption":
        return False
    blob = " ".join(str(item.get(k, "")) for k in ("productName", "skuName", "meterName")).lower()
    return not any(m in blob for m in _EXCLUDE_MARKERS)


def vm_hourly_usd(region: str, arm_sku_name: str, *, fetch=None) -> Optional[float]:
    """The on-demand Linux $/hr for an Azure VM SKU (e.g. ``Standard_D2s_v5``) in ``region`` (armRegionName,
    e.g. ``westeurope``), from the public Retail Prices API. None if no matching hourly meter is found."""
    flt = (f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' "
           f"and armSkuName eq '{arm_sku_name}' and priceType eq 'Consumption'")
    cands = []
    for it in _retail_query(flt, fetch=fetch):
        if not _is_on_demand_linux(it):
            continue
        if str(it.get("unitOfMeasure", "")).strip().lower() not in ("1 hour", "1hour"):
            continue
        price = it.get("retailPrice", it.get("unitPrice"))
        if isinstance(price, (int, float)) and price > 0:
            cands.append(float(price))
    return min(cands) if cands else None                 # the base Linux on-demand line is the lowest


def retail_hourly_usd(service_name: str, region: str, *, product_contains: Optional[str] = None,
                      meter_contains: Optional[str] = None, unit: str = "1 hour",
                      fetch=None) -> Optional[float]:
    """Generic public-price lookup: the on-demand $/hr for a service's meter in a region, optionally
    narrowed by a productName / meterName substring (e.g. Flexible Server vCore for managed Postgres).
    Returns the matching per-``unit`` price, or None. Used for managed datastores and ancillary meters."""
    flt = f"serviceName eq '{service_name}' and armRegionName eq '{region}' and priceType eq 'Consumption'"
    cands = []
    for it in _retail_query(flt, fetch=fetch):
        if not _is_on_demand_linux(it):
            continue
        blob = " ".join(str(it.get(k, "")) for k in ("productName", "skuName", "meterName")).lower()
        if product_contains and product_contains.lower() not in blob:
            continue
        if meter_contains and meter_contains.lower() not in blob:
            continue
        if unit and str(it.get("unitOfMeasure", "")).strip().lower() not in (unit.lower(), unit.replace(" ", "").lower()):
            continue
        price = it.get("retailPrice", it.get("unitPrice"))
        if isinstance(price, (int, float)) and price > 0:
            cands.append(float(price))
    return min(cands) if cands else None


def is_container_apps_url(url: str) -> bool:
    host = urlparse(url if "://" in (url or "") else f"https://{url}").hostname or ""
    return host.endswith(".azurecontainerapps.io")


def container_apps_region_from_url(url: str) -> Optional[str]:
    """armRegion out of a Container Apps URL ``<app>.<envid>.<region>.azurecontainerapps.io`` (the segment
    before the suffix is the region, already in armRegion form e.g. ``westeurope``). None if absent."""
    host = urlparse(url if "://" in (url or "") else f"https://{url}").hostname or ""
    suffix = ".azurecontainerapps.io"
    if not host.endswith(suffix):
        return None
    left = host[: -len(suffix)]
    return left.rsplit(".", 1)[1] if "." in left else None


def azure_postgres_extras(run_cmd=None):
    """Enumerate the subscription's Azure Database for PostgreSQL Flexible Servers so a Container Apps deploy
    with a managed DB is fully priced, not silently under-counted (the serverless app's persistent state
    lives here). Returns [{region, sku, storage_gb}]. ``run_cmd`` injectable for offline tests."""
    servers = _az_json(["postgres", "flexible-server", "list"], run_cmd) or []
    out = []
    for s in servers:
        sku = ((s.get("sku") or {}).get("name")) or s.get("skuName") or ""
        gb = float(((s.get("storage") or {}).get("storageSizeGb")) or s.get("storageSizeGb") or 0) or 0.0
        out.append({"region": s.get("location", ""), "sku": sku, "storage_gb": gb})
    return out


def postgres_flexible_hourly(region: str, sku: str, storage_gb: float, *, fetch=None) -> Optional[float]:
    """Best-effort standing $/hr for an Azure Database for PostgreSQL Flexible Server: the compute meter for
    the SKU's size (e.g. Standard_B1ms -> 'B1ms') from public Retail Prices, plus per-GB-month storage.
    Returns None if the compute meter cannot be matched (the caller then DISCLOSES it, never omits silently)."""
    svc = "Azure Database for PostgreSQL Flexible Server"
    size = str(sku or "").split("_")[-1]                  # Standard_B1ms -> B1ms
    compute = retail_hourly_usd(svc, region, meter_contains=size) if size else None
    if compute is None:
        return None
    hourly = compute
    sto = retail_hourly_usd(svc, region, meter_contains="storage", unit="1/month")
    if sto and storage_gb:
        hourly += float(storage_gb) * sto / 730.0
    return hourly


def container_apps_usage_rate(capture_date: str, *, region: str = "westeurope",
                              standing_floor_hourly: float = 0.0, extra_notes: tuple = (),
                              fetch=None) -> Optional[UsageRate]:
    """Azure Container Apps priced as a per-usage SCHEDULE (C21) from the PUBLIC Retail Prices API (no
    credentials), the serverless counterpart of the standing run-rate. Uses the Consumption plan's Standard
    meters: Requests (folds into the schedule), vCPU Active per second and Memory Active per GiB-second
    (driver 'other': unit rates reported, not folded, since they depend on per-request duration + the app's
    CPU/memory allocation, which the served URL does not reveal). Region defaults to westeurope (the meters
    are near-uniform across regions; the region is disclosed)."""
    items = _retail_query(f"serviceName eq 'Azure Container Apps' and armRegionName eq '{region}' "
                          "and priceType eq 'Consumption'", fetch=fetch)

    def _price(meter_sub: str, unit_sub: str) -> Optional[float]:
        for it in items:
            if not _is_on_demand_linux(it):
                continue
            mn = str(it.get("meterName", "")).lower()
            un = str(it.get("unitOfMeasure", "")).lower()
            if meter_sub in mn and unit_sub in un and "gpu" not in mn:
                p = it.get("retailPrice", it.get("unitPrice"))
                if isinstance(p, (int, float)) and p > 0:
                    return float(p)
        return None

    req_1m = _price("standard requests", "1m")            # $ per 1M requests
    vcpu_s = _price("standard vcpu active", "second")     # $ per vCPU-second
    mem_s = _price("standard memory active", "gib second")  # $ per GiB-second
    comps: List[UsageComponent] = []
    if req_1m is not None:
        comps.append(UsageComponent(name="requests", per_unit_usd=req_1m / 1_000_000.0, unit="per request",
                                    driver="requests", raw_unit_price=req_1m, native_unit="per 1M requests"))
    if vcpu_s is not None:
        comps.append(UsageComponent(name="compute-active-cpu", per_unit_usd=vcpu_s, unit="vCPU-second",
                                    driver="other", raw_unit_price=vcpu_s, native_unit="per vCPU-second"))
    if mem_s is not None:
        comps.append(UsageComponent(name="compute-active-mem", per_unit_usd=mem_s, unit="GiB-second",
                                    driver="other", raw_unit_price=mem_s, native_unit="per GiB-second"))
    if not any(c.driver == "requests" for c in comps):
        return None
    ur = compose_usage_rate(comps, provider="azure", region=region, service="Azure Container Apps",
                            capture_date=capture_date, standing_floor_hourly_usd=standing_floor_hourly,
                            price_source="Azure Retail Prices API (Consumption plan, public list, USD)",
                            price_urls=(_RETAIL_ENDPOINT,))
    if extra_notes:
        import dataclasses
        ur = dataclasses.replace(ur, assumptions=tuple(ur.assumptions) + tuple(extra_notes))
    return ur


def _az_json(args: List[str], run_cmd=None):
    """Run an `az ... -o json` command host-side (off-clock, never in the VM) and return the parsed JSON,
    or None on any failure. ``run_cmd`` injectable for offline tests."""
    if run_cmd is not None:
        return run_cmd(args)
    import subprocess
    try:
        r = subprocess.run(["az", *args, "-o", "json"], capture_output=True, text=True, timeout=45)
        return json.loads(r.stdout) if r.returncode == 0 and (r.stdout or "").strip() else None
    except Exception:  # noqa: BLE001 - cost is best-effort; a failure is disclosed, never faked
        return None


def _managed_disk_per_gb_month(region: str, sku: str, *, fetch=None) -> Optional[float]:
    """Azure managed-disk $/GB-month from public Retail Prices, for a Standard HDD/SSD tier (per-GB metered).
    Premium/Ultra are per-disk-tier, not per-GB, so they return None (disclosed, not folded)."""
    fam = "Standard SSD" if "StandardSSD" in sku else ("Standard HDD" if sku.startswith("Standard") else None)
    if not fam:
        return None
    for it in _retail_query(f"serviceName eq 'Storage' and armRegionName eq '{region}' "
                            "and priceType eq 'Consumption'", fetch=fetch):
        blob = " ".join(str(it.get(k, "")) for k in ("productName", "meterName", "skuName")).lower()
        if fam.lower() in blob and "disk" in blob and "/month" in str(it.get("unitOfMeasure", "")).lower():
            p = it.get("retailPrice", it.get("unitPrice"))
            if isinstance(p, (int, float)) and p > 0:
                return float(p)
    return None


def _az_vm_resolver(run_cmd=None) -> BundleResolver:
    """Resolve a standing Azure VM deploy to its priced bundle by finding the VM whose public IP or
    ``*.cloudapp.azure.com`` FQDN serves the deployed URL (`az vm list --show-details`), then reading its
    ``vmSize`` + ``location`` + OS-disk size/sku. Compute is priced exactly by ``vm_hourly_usd`` (live
    Retail Prices); a Standard-tier OS disk is priced per-GB-month, other tiers disclosed. Returns None if
    no VM matches (disclosed, never faked)."""
    def resolve(deployment_ref: object) -> Optional[dict]:
        host = urlparse(str(deployment_ref) if "://" in str(deployment_ref)
                        else f"https://{deployment_ref}").hostname or ""
        vms = _az_json(["vm", "list", "--show-details"], run_cmd) or []
        def _match(v):
            ips = str(v.get("publicIps", "") or "").split(",")
            fqdns = str(v.get("fqdns", "") or "").split(",")
            return host in [x.strip() for x in ips + fqdns if x.strip()]
        vm = next((v for v in vms if _match(v)), None)
        if not vm:
            return None
        size = (vm.get("hardwareProfile", {}) or {}).get("vmSize") or vm.get("hardwareProfile")
        region = vm.get("location")
        if not size or not region:
            return None
        osdisk = ((vm.get("storageProfile", {}) or {}).get("osDisk", {}) or {})
        gb = float(osdisk.get("diskSizeGb") or 0) or 0.0
        disk_sku = ((osdisk.get("managedDisk", {}) or {}).get("storageAccountType") or "")
        spm = _managed_disk_per_gb_month(region, disk_sku) if (gb and disk_sku) else None
        return {"region": region, "vm_sku": size, "vm_count": 1,
                "storage_gb": gb, "storage_per_gb_month": spm, "managed": []}
    return resolve


class AzureRunRateAdapter(RunRateAdapter):
    def __init__(self, resolve_bundle: Optional[BundleResolver] = None, call_tool=None):
        # default: resolve a standing VM deploy via `az vm list` (serverless URLs never reach the resolver;
        # they take the usage-schedule branch in run_rate). Injectable for offline tests.
        self._resolve = resolve_bundle or _az_vm_resolver()

    def run_rate(self, deployment_ref: object, *, capture_date: str):
        """Price the deployment. A serverless Container Apps URL (``*.azurecontainerapps.io``) is priced as
        a per-usage SCHEDULE from the public Retail Prices Consumption meters (no bundle resolution needed:
        the URL is the whole input); anything else is a standing hourly run-rate from its resolved bundle
        (the VM path, which still needs the injected resolver). Prices are USD (Retail Prices lists in USD),
        so no FX. Returns a UsageRate, a RunRate, or None (disclosed, never faked)."""
        if isinstance(deployment_ref, str) and is_container_apps_url(deployment_ref):
            reg = container_apps_region_from_url(deployment_ref) or "westeurope"
            # the serverless app's persistent state lives in a managed Postgres (always-on); enumerate and
            # fold it as a standing floor, or DISCLOSE it -- never silently omit (the pre-fix bug).
            floor, notes = 0.0, []
            for pg in (azure_postgres_extras() or []):
                h = postgres_flexible_hourly(pg.get("region") or reg, pg.get("sku", ""), pg.get("storage_gb", 0))
                if h is not None:
                    floor += h
                else:
                    notes.append("cost EXCLUDES an unpriced Azure Database for PostgreSQL "
                                 f"(sku {pg.get('sku') or 'unknown'}): compute meter unavailable, disclosed not faked")
            return container_apps_usage_rate(capture_date, region=reg,
                                             standing_floor_hourly=floor, extra_notes=tuple(notes))
        b = self._resolve(deployment_ref)
        if not b:
            return None
        region = str(b.get("region", ""))
        comps: List[RateComponent] = []

        # app compute (VM): price the SKU from the public API unless a price is supplied
        vm_sku = b.get("vm_sku")
        vm_price = b.get("vm_hourly_usd")
        if vm_price is None and vm_sku:
            vm_price = vm_hourly_usd(region, str(vm_sku))
        if vm_price is None:
            return None                                   # cannot price app compute -> disclose, do not fake
        vm_count = int(b.get("vm_count", 1) or 1)
        comps.append(RateComponent(name="compute", hourly_usd=float(vm_price) * vm_count,
                                   raw_unit_price=float(vm_price), native_unit="instance-hour",
                                   quantity=float(vm_count)))

        # managed datastores: each its OWN instance-hours (Part 4 per-resource cost)
        for m in b.get("managed", []) or []:
            price = m.get("hourly_usd")
            if price is None and m.get("service_name"):
                vc = retail_hourly_usd(m["service_name"], region,
                                       product_contains=m.get("product_contains"),
                                       meter_contains=m.get("meter_contains"))
                if vc is not None:
                    price = vc * float(m.get("vcores", 1) or 1)
            if price is not None:
                comps.append(RateComponent(name="compute:" + str(m.get("role", "datastore")),
                                           hourly_usd=float(price), raw_unit_price=float(price),
                                           native_unit="instance-hour", quantity=float(m.get("vcores", 1) or 1)))

        # storage (block, per GB-month) + public IP, if the resolver supplied them
        gb = float(b.get("storage_gb", 0) or 0)
        spm = b.get("storage_per_gb_month")
        if spm is not None and gb:
            comps.append(RateComponent(name="storage",
                                       hourly_usd=storage_gb_month_to_hourly(float(spm), gb),
                                       raw_unit_price=float(spm), native_unit="GB-month", quantity=gb))
        ip = b.get("public_ip_hourly")
        if ip is not None:
            comps.append(RateComponent(name="public_ip", hourly_usd=float(ip),
                                       raw_unit_price=float(ip), native_unit="hour"))

        eg = b.get("egress")
        egress = EgressRate(per_gb_usd=eg["per_gb_usd"], tier=eg.get("tier", "unspecified")) if eg else None
        return compose_run_rate(comps, provider="azure", region=region,
                                flavor=str(vm_sku or b.get("flavor", "")), capture_date=capture_date,
                                egress=egress, price_source="Azure Retail Prices API (public list, USD)",
                                price_urls=(_RETAIL_ENDPOINT,))
