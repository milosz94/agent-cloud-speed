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

# a Container Apps scale resolver returns {min_replicas:int, cpu:float, memory_gib:float} for the deployed
# app, or None if the app's scale cannot be enumerated (disclosed, never faked). minReplicas >= 1 means one
# replica is pinned on 24/7 (idle meters bill), minReplicas == 0 means scale-to-zero (no idle floor).
ScaleResolver = Callable[[object], Optional[dict]]

# a Postgres resolver returns [{region, sku, storage_gb}] for the managed Postgres the serverless app uses,
# or [] if none is found - which run_rate DISCLOSES rather than silently treating as a complete $0 floor.
PostgresResolver = Callable[[], List[dict]]


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
    Returns None if the compute meter cannot be matched (the caller then DISCLOSES it, never omits silently).
    NOTE: the Retail Prices serviceName is 'Azure Database for PostgreSQL' (NOT '... Flexible Server'); the
    Flexible Server rows are distinguished by a 'Flexible Server' productName, which also excludes the
    Cosmos-DB-for-PostgreSQL burstable meters that share the vCore name."""
    svc = "Azure Database for PostgreSQL"
    size = str(sku or "").split("_")[-1]                  # Standard_B1ms -> B1ms
    compute = (retail_hourly_usd(svc, region, product_contains="Flexible Server",
                                 meter_contains=size, fetch=fetch) if size else None)
    if compute is None:
        return None
    hourly = compute
    # storage productName is 'Azure Database for PostgreSQL Flex Server Storage', billed per GB/Month
    sto = retail_hourly_usd(svc, region, product_contains="Flex Server Storage",
                            unit="1 GB/Month", fetch=fetch)
    if sto and storage_gb:
        hourly += storage_gb_month_to_hourly(sto, float(storage_gb))
    return hourly


def container_apps_usage_rate(capture_date: str, *, region: str = "westeurope",
                              standing_floor_hourly: float = 0.0, scale: Optional[dict] = None,
                              extra_notes: tuple = (), fetch=None) -> Optional[UsageRate]:
    """Azure Container Apps priced as a per-usage SCHEDULE (C21) from the PUBLIC Retail Prices API (no
    credentials), the serverless counterpart of the standing run-rate. Uses the Consumption plan's Standard
    meters: Requests (per request), vCPU Active per second and Memory Active per GiB-second (driver 'other'),
    all THREE required -- ``compose_usage_rate`` folds the per-second vCPU/Memory rates into the per-request
    cost using the disclosed request profile, so the schedule includes active compute; without all three the
    bill is incomplete and this returns None (UNPRICED), never a partial. Region defaults to westeurope (the
    meters are near-uniform across regions; the region is disclosed).

    ``scale`` (GAP 6) is the resolved {min_replicas, cpu, memory_gib}. When minReplicas >= 1 the app keeps one
    replica ALWAYS ON (not scale-to-zero), so Azure bills the DISTINCT idle meters (Standard vCPU/Memory Idle
    Usage) around the clock on top of the active per-request usage: this adds
    minReplicas x (cpu x idleVcpu + memGiB x idleMem) x 3600 to the standing floor. minReplicas == 0 is
    scale-to-zero (idle floor $0, disclosed). If the idle meters cannot be priced while a replica is pinned on
    this returns None (UNPRICED) rather than a silent understatement. ``scale is None`` (scale not enumerable)
    is disclosed as a note and the idle floor is left out (active per-request usage is still priced)."""
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
    # A Container Apps bill is requests + active vCPU + active memory. If ANY of the three is unpriced the
    # schedule would silently omit a real cost component and understate the bill, so refuse rather than emit
    # a partial (C21 completeness): report UNPRICED, never a plausible-but-incomplete number.
    if req_1m is None or vcpu_s is None or mem_s is None:
        return None

    # GAP 6: an always-on min replica (minReplicas >= 1) is NOT scale-to-zero. Azure bills the DISTINCT idle
    # meters (Standard vCPU/Memory Idle Usage, priced separately from the active meters above) for that pinned
    # replica 24/7. Add minReplicas x (cpu x idleVcpu + memGiB x idleMem) x 3600 to the standing floor. Never
    # price only the active meters when a replica is held on: the idle floor is the dominant always-on cost.
    idle_floor_hourly, idle_notes = 0.0, []
    if scale is not None:
        idle_vcpu_s = _price("standard vcpu idle", "second")     # $ per idle vCPU-second (distinct meter)
        idle_mem_s = _price("standard memory idle", "gib second")  # $ per idle GiB-second (distinct meter)
        mr = int(scale.get("min_replicas", 0) or 0)
        cpu = float(scale.get("cpu", 0) or 0)
        mem_gib = float(scale.get("memory_gib", 0) or 0)
        if mr >= 1:
            if idle_vcpu_s is None or idle_mem_s is None:
                return None       # a real always-on charge we cannot price -> UNPRICED, never a silent $0
            idle_floor_hourly = mr * (cpu * idle_vcpu_s + mem_gib * idle_mem_s) * 3600.0
            idle_notes.append(
                f"Container Apps always-on idle floor: minReplicas={mr} x ({cpu:g} vCPU x "
                f"${idle_vcpu_s:g}/vCPU-s + {mem_gib:g} GiB x ${idle_mem_s:g}/GiB-s) x 3600 = "
                f"${idle_floor_hourly:.4f}/hr (idle meters, billed on top of active per-request usage)")
        else:
            idle_notes.append(
                "Container Apps minReplicas=0: scale-to-zero, no always-on idle replica floor (idle $0)")
    else:
        idle_notes.append(
            "Container Apps min-replica idle floor unavailable: app scale not enumerable "
            "(idle replica cost not folded; active per-request usage still priced)")

    ur = compose_usage_rate(comps, provider="azure", region=region, service="Azure Container Apps",
                            capture_date=capture_date,
                            standing_floor_hourly_usd=float(standing_floor_hourly) + idle_floor_hourly,
                            price_source="Azure Retail Prices API (Consumption plan, public list, USD)",
                            price_urls=(_RETAIL_ENDPOINT,))
    all_notes = tuple(idle_notes) + tuple(extra_notes)
    if all_notes:
        import dataclasses
        ur = dataclasses.replace(ur, assumptions=tuple(ur.assumptions) + all_notes)
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


def _mem_to_gib(mem) -> float:
    """Normalize a Container Apps memory string to GiB. Accepts Kubernetes-style quantities ('2Gi',
    '0.5Gi', '512Mi', '2G', '512M') and bare numbers (assumed GiB). Returns 0.0 on anything unparseable."""
    if mem is None:
        return 0.0
    s = str(mem).strip().lower()
    try:
        if s.endswith("gi"):
            return float(s[:-2])
        if s.endswith("mi"):
            return float(s[:-2]) / 1024.0
        if s.endswith("g"):
            return float(s[:-1])
        if s.endswith("m"):
            return float(s[:-1]) / 1024.0
        return float(s)                                    # bare number: assume GiB
    except ValueError:
        return 0.0


def _az_containerapp_scale_resolver(run_cmd=None) -> ScaleResolver:
    """Resolve the deployed Container App's scale + per-replica resources so the always-on min-replica idle
    floor (GAP 6) can be priced. Lists the subscription's container apps (`az containerapp list`), matches the
    one whose ingress FQDN serves the deployed URL (falling back to a first-segment name match), and reads
    ``minReplicas`` (.properties.template.scale.minReplicas) plus the summed per-replica cpu + memory
    (.properties.template.containers[].resources.cpu / .memory). Returns
    {min_replicas, cpu, memory_gib} or None if the app cannot be enumerated (disclosed, never faked).
    ``run_cmd`` injectable so tests need no cloud."""
    def _props(app: dict) -> dict:
        p = app.get("properties")
        return p if isinstance(p, dict) else app

    def resolve(deployment_ref: object) -> Optional[dict]:
        host = (urlparse(str(deployment_ref) if "://" in str(deployment_ref)
                         else f"https://{deployment_ref}").hostname or "").lower()
        apps = _az_json(["containerapp", "list"], run_cmd) or []

        def _fqdn(a: dict) -> str:
            ing = (((_props(a).get("configuration") or {}).get("ingress")) or {})
            return str(ing.get("fqdn") or "").lower()

        app = next((a for a in apps if _fqdn(a) and _fqdn(a) == host), None)
        if app is None and host:
            seg = host.split(".")[0]
            app = next((a for a in apps if str(a.get("name", "")).lower() == seg), None)
        if app is None:
            return None
        tmpl = (_props(app).get("template") or {})
        scale = tmpl.get("scale") or {}
        min_replicas = scale.get("minReplicas")
        if min_replicas is None:
            return None                                    # scale unknown -> disclosed upstream, not faked
        cpu, mem_gib = 0.0, 0.0
        for c in (tmpl.get("containers") or []):
            res = c.get("resources") or {}
            cpu += float(res.get("cpu") or 0)
            mem_gib += _mem_to_gib(res.get("memory"))
        return {"min_replicas": int(min_replicas), "cpu": cpu, "memory_gib": mem_gib}
    return resolve


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
    def __init__(self, resolve_bundle: Optional[BundleResolver] = None, call_tool=None,
                 postgres_resolver: Optional[PostgresResolver] = None,
                 scale_resolver: Optional[ScaleResolver] = None):
        # default: resolve a standing VM deploy via `az vm list` (serverless URLs never reach the resolver;
        # they take the usage-schedule branch in run_rate). Injectable for offline tests.
        self._resolve = resolve_bundle or _az_vm_resolver()
        # serverless (Container Apps) seams, both injectable so the fold is tested offline (GAP 6 + GAP 7):
        #  - the managed-Postgres standing floor (default: live `az postgres flexible-server list`)
        #  - the min-replica idle floor's scale (default: live `az containerapp list` + FQDN match)
        self._postgres = postgres_resolver or azure_postgres_extras
        self._scale = scale_resolver or _az_containerapp_scale_resolver()

    def run_rate(self, deployment_ref: object, *, capture_date: str):
        """Price the deployment. A serverless Container Apps URL (``*.azurecontainerapps.io``) is priced as
        a per-usage SCHEDULE from the public Retail Prices Consumption meters (no bundle resolution needed:
        the URL is the whole input); anything else is a standing hourly run-rate from its resolved bundle
        (the VM path, which still needs the injected resolver). Prices are USD (Retail Prices lists in USD),
        so no FX. Returns a UsageRate, a RunRate, or None (disclosed, never faked)."""
        if isinstance(deployment_ref, str) and is_container_apps_url(deployment_ref):
            reg = container_apps_region_from_url(deployment_ref) or "westeurope"
            # the serverless app's persistent state lives in a managed Postgres (always-on); enumerate it via
            # the INJECTABLE resolver and fold it as a standing floor, or DISCLOSE its absence -- never
            # silently emit a DB-less number as if complete (GAP 7, the pre-fix bug: the [] case set floor 0
            # with no note). The min-replica idle floor (GAP 6) is folded inside container_apps_usage_rate
            # from the injected scale resolver.
            floor, notes = 0.0, []
            servers = self._postgres() or []
            if not servers:
                notes.append("DB standing floor unavailable: deployment not enumerable "
                             "(no managed Postgres returned by the resolver; a real DB would be under-counted)")
            for pg in servers:
                h = postgres_flexible_hourly(pg.get("region") or reg, pg.get("sku", ""), pg.get("storage_gb", 0))
                if h is None:
                    # A database the deploy provisioned but we cannot price would make the schedule omit a real
                    # standing cost and understate the bill. Refuse the whole cost (UNPRICED, C21 completeness)
                    # rather than return a plausible-but-incomplete number.
                    return None
                floor += h
            scale = self._scale(deployment_ref)
            return container_apps_usage_rate(capture_date, region=reg, standing_floor_hourly=floor,
                                             scale=scale, extra_notes=tuple(notes))
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
