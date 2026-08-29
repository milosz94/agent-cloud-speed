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

import dataclasses
import json
import re
import time
import urllib.parse
import urllib.request
from typing import Callable, List, Optional, Tuple
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


# =====================================================================================================
# UNIVERSAL resource-group sweep (completeness): discover EVERY resource this run provisioned from
# Azure's OWN complete inventory, then price each BY ITS ARM TYPE, so a resource TYPE we never
# anticipated (a Storage account, a Redis cache, a Log Analytics workspace, a public IP, an ACR, or a
# brand-new type) is STILL caught with NO new code. This is the Azure counterpart of the AWS uniform
# tag-inventory sweep (aws_runrate._enumerate): discovery is EXHAUSTIVE, pricing is
# best-effort-with-disclosure. Nothing a run created is ever a silent $0: every discovered resource is
# either priced or DISCLOSED by name.
#
# The anchor is that EACH acspeed run creates its OWN resource group named ``rg-<run_token>`` (the run
# token is ``acs<hex>``, carried into the deployment's public hostname by autorun's NAMING_INSTRUCTION),
# and ``az resource list -g <rg> -o json`` (equivalently ``az graph query -q "Resources | where
# resourceGroup =~ '<rg>'"``) returns EVERY ARM resource in it, of ANY type. Retail Prices has a meter
# for each. The enumerator is INJECTABLE (``run_cmd``) so the whole path is tested offline with a canned
# inventory; ``az`` may be unauthenticated here, so the offline injected path is the tested deliverable.

# a resource-list resolver returns the raw ARM resource dicts (``az resource list -o json`` shape) for a
# deployment; injectable so tests feed a canned inventory with no cloud.
RgResourcesResolver = Callable[[object], List[dict]]

# the two ARM types the serverless path already prices, skipped by the sweep so they are never
# double-counted: Container Apps (priced as the usage schedule) and Postgres Flexible Server (priced by
# the injected postgres fold).
_TYPE_CONTAINERAPPS = "Microsoft.App/containerApps"
_TYPE_POSTGRES_FLEX = "Microsoft.DBforPostgreSQL/flexibleServers"

_RUN_TOKEN_RE = re.compile(r"acs[0-9a-f]{6,}")


def run_token_from_ref(deployment_ref: object) -> Optional[str]:
    """The harness run token (``acs<hex>``) carried in the deployment's hostname, or None. This token is
    the run's identity: its resource group and every resource it created carry it (NAMING_INSTRUCTION)."""
    ref = str(deployment_ref)
    host = (urlparse(ref if "://" in ref else f"https://{ref}").hostname or "").lower()
    m = _RUN_TOKEN_RE.search(host) or _RUN_TOKEN_RE.search(ref.lower())
    return m.group(0) if m else None


def resource_group_from_url(deployment_ref: object) -> Optional[str]:
    """Derive this run's resource group ``rg-<run_token>`` (e.g. a Container Apps URL
    ``umami-acs1a2b3c4d.<envid>.westeurope.azurecontainerapps.io`` -> ``rg-acs1a2b3c4d``) from the
    deploy's URL / run token. None when no token is present (disclosed upstream, never faked into a
    wrong RG that would sweep another run's resources)."""
    tok = run_token_from_ref(deployment_ref)
    return f"rg-{tok}" if tok else None


def azure_rg_resources(resource_group: str, run_cmd=None) -> List[dict]:
    """COMPLETE enumerator: list EVERY resource of ANY type in ``resource_group`` via
    ``az resource list -g <rg> -o json`` (the ARM inventory returns all types uniformly:
    Microsoft.App/containerApps, Microsoft.DBforPostgreSQL/flexibleServers,
    Microsoft.Storage/storageAccounts, Microsoft.Cache/Redis, Microsoft.Network/publicIPAddresses,
    Microsoft.ContainerRegistry/registries, Microsoft.OperationalInsights/workspaces, ...). The
    equivalent Resource Graph form is ``az graph query -q "Resources | where resourceGroup =~
    '<rg>'"``. ``run_cmd`` is INJECTABLE so tests run offline with a canned inventory; returns [] on any
    failure or an empty/absent RG (disclosed upstream, never faked)."""
    if not resource_group:
        return []
    data = _az_json(["resource", "list", "-g", resource_group], run_cmd)
    return data if isinstance(data, list) else []


def azure_resources_for_token(token: str, run_cmd=None) -> List[dict]:
    """UNIVERSAL discovery, RG-name-agnostic: EVERY resource this run created, found by the run token in
    the resource name OR its resource group. The agent stamps the token into both (NAMING_INSTRUCTION), and
    each run gets its own resource group, so this works whether the RG is ``rg-<token>`` (Container Apps) or
    ``rg-umami-<token>`` (App Service) or anything else. ``az resource list`` over the subscription, filtered
    by token. Injectable; [] offline. This is the azure analogue of the gcp Cloud-Asset / aws Resource-
    Explorer sweep - discover, then price every discovered resource by its type via ``_RG_DISPATCH``."""
    if not token:
        return []
    data = _az_json(["resource", "list"], run_cmd) or []
    t = token.lower()
    return [r for r in data if isinstance(r, dict)
            and (t in str(r.get("name", "")).lower() or t in str(r.get("resourceGroup", "")).lower())]


# --- per-type pricers (best-effort; each returns a standing RateComponent or None -> disclosed) --------

def _arm_sku(res: dict) -> dict:
    """The ARM ``sku`` object as a dict (``az resource list`` gives {name, tier, family, capacity, ...}
    or, occasionally, a bare string). Empty dict when absent."""
    sku = res.get("sku")
    if isinstance(sku, dict):
        return sku
    return {"name": str(sku)} if sku else {}


def _pg_storage_gb(res: dict) -> float:
    """Provisioned Postgres storage GB from the resource's properties, if present (``az resource list``
    is often shallow, so this is best-effort: 0.0 when absent, which just omits the storage line)."""
    st = (res.get("properties") or {}).get("storage") or {}
    for k in ("storageSizeGB", "storageSizeGb", "storageSizeInGB"):
        v = st.get(k)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    v = res.get("storage_gb")
    try:
        return float(v) if v else 0.0
    except (TypeError, ValueError):
        return 0.0


def _price_postgres_flex(res: dict, region: str, *, fetch=None) -> Optional[RateComponent]:
    """Standing $/hr for a Postgres Flexible Server, via the existing ``postgres_flexible_hourly``."""
    sku = _arm_sku(res).get("name") or ""
    if not sku:
        return None
    h = postgres_flexible_hourly(res.get("location") or region, str(sku), _pg_storage_gb(res), fetch=fetch)
    if h is None:
        return None
    return RateComponent(name="compute:postgres-flexible", hourly_usd=h, raw_unit_price=h,
                         native_unit="instance-hour", quantity=1.0)


def _price_redis(res: dict, region: str, *, fetch=None) -> Optional[RateComponent]:
    """Standing $/hr for an Azure Cache for Redis node, from the public Retail Prices per-hour cache
    meter (sku family+capacity -> size, e.g. C0 / C1 / P1). Disclosed (None) if the meter cannot be
    matched."""
    sku = _arm_sku(res)
    tier = str(sku.get("name") or "")
    fam = str(sku.get("family") or "")
    cap = sku.get("capacity")
    size = f"{fam}{cap}" if fam and cap is not None else ""
    price = retail_hourly_usd("Redis Cache", res.get("location") or region,
                              product_contains=(tier or None), meter_contains=(size or None),
                              unit="1 Hour", fetch=fetch)
    if price is None:
        return None
    return RateComponent(name="compute:redis", hourly_usd=float(price), raw_unit_price=float(price),
                         native_unit="instance-hour", quantity=1.0)


def _price_public_ip(res: dict, region: str, *, fetch=None) -> Optional[RateComponent]:
    """Standing $/hr for a public IPv4 address (a Standard static address bills per hour). Disclosed
    (None) if the meter cannot be matched (e.g. a dynamic address with no standing charge is disclosed,
    not faked to $0)."""
    tier = str(_arm_sku(res).get("name") or "Standard")
    price = retail_hourly_usd("Virtual Network", res.get("location") or region,
                              product_contains="IP Addresses",
                              meter_contains=("Standard" if "standard" in tier.lower() else None),
                              unit="1 Hour", fetch=fetch)
    if price is None:
        return None
    return RateComponent(name="public_ip", hourly_usd=float(price), raw_unit_price=float(price),
                         native_unit="hour", quantity=1.0)


def _price_acr(res: dict, region: str, *, fetch=None) -> Optional[RateComponent]:
    """Standing $/hr for an Azure Container Registry: a per-day fixed charge by SKU tier
    (Basic/Standard/Premium), converted to hourly (/24). Disclosed (None) if the tier meter cannot be
    matched."""
    sku = _arm_sku(res)
    tier = str(sku.get("name") or sku.get("tier") or "")
    if not tier:
        return None
    per_day = retail_hourly_usd("Container Registry", res.get("location") or region,
                                meter_contains=f"{tier} Registry", unit="1/Day", fetch=fetch)
    if per_day is None:
        return None
    return RateComponent(name="container_registry", hourly_usd=float(per_day) / 24.0,
                         raw_unit_price=float(per_day), native_unit="registry-day", quantity=1.0)


# The DISPATCH table: ARM resource type (lowercased) -> a pricer callable, or a sentinel. Per-type
# knowledge is DATA (a table row), NOT a code branch, exactly like aws_cost._DIMENSIONS:
#   _SERVERLESS : priced by the Container Apps usage schedule (not a standing line; never double-counted)
#   _USAGE      : usage-priced (pay per GB stored / per operation / per ingested GB); $0 standing,
#                 DISCLOSED by name, consistent with run-rate-not-cost-to-complete
#   _FREE       : this resource TYPE carries no standing charge; DISCLOSED (a disclosed $0, never silent)
#   <callable>  : a best-effort standing pricer (returns a RateComponent or None -> disclosed)
# A type ABSENT from this table is NOT dropped: it goes to ``unpriced_resources`` WITH its type string,
# so an UNANTICIPATED resource type is always surfaced. Discovery is exhaustive; pricing is best-effort.
_SERVERLESS = object()
_USAGE = object()
_FREE = object()

def _price_app_service_plan(res: dict, region: str, *, fetch=None) -> Optional[RateComponent]:
    """Standing App Service Plan line: price its sku tier (B1/S1/P1v3/...) from the Linux plan meters. The
    plan is a fixed hourly resource, like a VM; the web app (Microsoft.Web/sites) bills via its plan, not
    itself. None if the sku is absent or unpriceable (disclosed by the sweep, never faked)."""
    sku = _arm_sku(res).get("name")
    if not sku:
        return None
    h = app_service_hourly(res.get("location") or region, str(sku), fetch=fetch)
    if h is None:
        return None
    return RateComponent(name=f"compute:app-service-plan:{sku}", hourly_usd=h,
                         raw_unit_price=h, native_unit="plan-hour")


_RG_DISPATCH = {
    "microsoft.app/containerapps": _SERVERLESS,
    "microsoft.web/serverfarms": _price_app_service_plan,   # App Service Plan (standing tier), a DATA row
    "microsoft.web/sites": _FREE,                            # the web app bills via its plan, not itself
    "microsoft.dbforpostgresql/flexibleservers": _price_postgres_flex,
    "microsoft.cache/redis": _price_redis,
    "microsoft.network/publicipaddresses": _price_public_ip,
    "microsoft.containerregistry/registries": _price_acr,
    # usage-priced: no standing hourly rate accrues on the idle allocation (billed per GB stored / per
    # operation / per ingested GB); $0 standing, DISCLOSED by name.
    "microsoft.storage/storageaccounts": _USAGE,
    "microsoft.operationalinsights/workspaces": _USAGE,
    "microsoft.insights/components": _USAGE,
    "microsoft.keyvault/vaults": _USAGE,
    "microsoft.servicebus/namespaces": _USAGE,
    "microsoft.eventhub/namespaces": _USAGE,
    # no standing charge for the resource type itself (the workload it fronts bills elsewhere), DISCLOSED
    "microsoft.app/managedenvironments": _FREE,
    "microsoft.network/virtualnetworks": _FREE,
    "microsoft.network/networksecuritygroups": _FREE,
    "microsoft.managedidentity/userassignedidentities": _FREE,
}


@dataclasses.dataclass(frozen=True)
class RgSweep:
    """Result of the universal resource-group sweep. ``components`` are priced standing hourly lines;
    ``unpriced_resources`` DISCLOSES, by ARM type + name, EVERY discovered resource that is not a priced
    standing line (usage-priced, free, unrecognized, or recognized-but-unpriceable), never a silent
    drop. ``serverless_types`` are the Container Apps handled by the usage schedule; ``priced_types``
    are the ARM types that produced a standing line."""
    components: Tuple[RateComponent, ...] = ()
    unpriced_resources: Tuple[str, ...] = ()
    priced_types: Tuple[str, ...] = ()
    serverless_types: Tuple[str, ...] = ()


def price_rg_resources(resources, *, region: str, fetch=None, skip_types=()) -> RgSweep:
    """UNIVERSAL dispatcher: for EACH discovered resource, route by its ARM ``type`` to a pricer, and
    put anything with no pricer (or that cannot be priced) into ``unpriced_resources`` WITH its type
    string. Discovery is EXHAUSTIVE (every resource is visited and accounted for); pricing is
    best-effort-with-disclosure (nothing is a silent $0, and an unknown type never fails the whole
    cost). ``skip_types`` are types priced elsewhere (Container Apps + Postgres on the serverless path)
    so the sweep never double-counts them."""
    skip = {str(t).lower() for t in skip_types}
    comps: List[RateComponent] = []
    unpriced: List[str] = []
    priced_types: List[str] = []
    serverless: List[str] = []
    for res in resources or []:
        rtype = str(res.get("type", "")).strip()
        name = str(res.get("name", "")) or "(unnamed)"
        label = f"{rtype} '{name}'" if rtype else f"(untyped) '{name}'"
        key = rtype.lower()
        if key in skip:
            continue
        handler = _RG_DISPATCH.get(key)
        if handler is _SERVERLESS:
            serverless.append(label)
            continue
        if handler is _USAGE:
            unpriced.append(f"{label}: usage-priced (billed per usage; $0 standing, disclosed)")
            continue
        if handler is _FREE:
            unpriced.append(f"{label}: no standing charge for this resource type (disclosed)")
            continue
        if handler is None:
            unpriced.append(f"{label}: unrecognized ARM resource type, no pricer (disclosed, not dropped)")
            continue
        comp = handler(res, region, fetch=fetch)
        if comp is None:
            unpriced.append(f"{label}: recognized but not priceable "
                            "(pricing attributes unavailable, disclosed)")
        else:
            comps.append(comp)
            priced_types.append(rtype)
    return RgSweep(tuple(comps), tuple(unpriced), tuple(priced_types), tuple(serverless))


# --- Azure App Service plan pricing (used by the _RG_DISPATCH data row, NOT a per-URL branch) ----------
#
# The agent deploys umami to azure as Container Apps (serverless schedule) OR App Service, non-
# deterministically. An App Service Plan is a fixed hourly tier (B1/S1/P1v3/...) - a standing resource, like
# a VM - so it is priced as a standing line and DISCOVERED by the universal resource sweep, exactly like the
# managed Postgres, a public IP, or any other resource: one _RG_DISPATCH row, never a hand-coded branch.

def app_service_hourly(region: str, sku: str, *, fetch=None) -> Optional[float]:
    """Standing $/hr for a Linux Azure App Service Plan tier (B1/S1/P1v3/...) from the public Retail Prices
    API. Basic/Standard/Premium plans bill hourly like a VM (no per-request charge). Matches the LINUX plan
    meter whose skuName equals the plan sku (spaces/case normalized, e.g. 'P1 v3' == 'P1v3'), dropping the
    Windows meter that shares the sku. None if not found (disclosed, never faked)."""
    if not sku:
        return None
    want = str(sku).replace(" ", "").lower()
    flt = f"serviceName eq 'Azure App Service' and armRegionName eq '{region}' and priceType eq 'Consumption'"
    cands = []
    for it in _retail_query(flt, fetch=fetch):
        if not _is_on_demand_linux(it):                          # drops the Windows meter
            continue
        pn = str(it.get("productName", "")).lower()
        if "linux" not in pn or "plan" not in pn:                # App Service PLAN, Linux
            continue
        if str(it.get("skuName", "")).replace(" ", "").lower() != want:
            continue
        if str(it.get("unitOfMeasure", "")).strip().lower() not in ("1 hour", "1hour"):
            continue
        p = it.get("retailPrice", it.get("unitPrice"))
        if isinstance(p, (int, float)) and p >= 0:
            cands.append(float(p))
    return min(cands) if cands else None


class AzureRunRateAdapter(RunRateAdapter):
    def __init__(self, resolve_bundle: Optional[BundleResolver] = None, call_tool=None,
                 postgres_resolver: Optional[PostgresResolver] = None,
                 scale_resolver: Optional[ScaleResolver] = None,
                 rg_resources_resolver: Optional[RgResourcesResolver] = None):
        # default: resolve a standing VM deploy via `az vm list` (serverless URLs never reach the resolver;
        # they take the usage-schedule branch in run_rate). Injectable for offline tests.
        self._resolve = resolve_bundle or _az_vm_resolver()
        # serverless (Container Apps) seams, both injectable so the fold is tested offline (GAP 6 + GAP 7):
        #  - the managed-Postgres standing floor (default: live `az postgres flexible-server list`)
        #  - the min-replica idle floor's scale (default: live `az containerapp list` + FQDN match)
        self._postgres = postgres_resolver or azure_postgres_extras
        self._scale = scale_resolver or _az_containerapp_scale_resolver()
        # UNIVERSAL completeness sweep: list EVERY resource in this run's resource group (rg-<run_token>)
        # and price/disclose each, so a type OUTSIDE {Container Apps, Postgres} is never silently missed.
        # Injectable so tests feed a canned inventory; the default derives the RG from the URL and runs
        # `az resource list` (which returns [] offline, keeping the tool non-fatal without credentials).
        self._rg_resources = rg_resources_resolver or (
            lambda ref: azure_resources_for_token(run_token_from_ref(ref) or "", None))
        self.rg_unpriced: List[str] = []   # disclosed non-priced resources, readable after run_rate

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
            # UNIVERSAL sweep: price every OTHER resource this run created (a Storage account, a Redis
            # cache, a public IP, an ACR, a Log Analytics workspace, or a type we have never seen) from
            # the run's resource group, or DISCLOSE it by name. Skip the two types already priced above
            # (Container Apps by the schedule, Postgres by the fold) so nothing is double-counted.
            # Best-effort-with-disclosure: an unpriceable/unknown resource is disclosed, never a silent
            # $0, and never fails the whole cost (unlike the strict Container Apps completeness guard).
            sweep = price_rg_resources(self._rg_resources(deployment_ref) or [], region=reg,
                                       skip_types=(_TYPE_CONTAINERAPPS, _TYPE_POSTGRES_FLEX))
            self.rg_unpriced = list(sweep.unpriced_resources)
            for c in sweep.components:
                floor += c.hourly_usd
                notes.append(f"resource-group sweep priced {c.name}: ${c.hourly_usd:.4f}/hr "
                             "(folded into the standing floor)")
            notes.extend(sweep.unpriced_resources)
            scale = self._scale(deployment_ref)
            return container_apps_usage_rate(capture_date, region=reg, standing_floor_hourly=floor,
                                             scale=scale, extra_notes=tuple(notes))
        # Non-serverless deploy (App Service, a VM, or any standing resource): the UNIVERSAL standing path.
        # Discover every resource this run created (by run token, ANY resource group) and price each via the
        # sweep's data table - App Service Plan, Postgres, storage, public IP, ... are all _RG_DISPATCH rows,
        # never a per-type branch here. Sum the priced standing lines into a run-rate; the sweep discloses
        # every resource it could not price by type + name (best-effort, never a silent $0). This is the same
        # discover-then-price-or-disclose shape as the gcp / aws universal sweeps.
        resources = self._rg_resources(deployment_ref) or []
        if resources:
            reg = next((str(r.get("location") or "") for r in resources if r.get("location")), "eastus2")
            sweep = price_rg_resources(resources, region=reg)
            self.rg_unpriced = list(sweep.unpriced_resources)
            if sweep.components:
                return compose_run_rate(list(sweep.components), provider="azure", region=reg,
                                        flavor="+".join(sorted(set(sweep.priced_types))) or "standing",
                                        capture_date=capture_date,
                                        price_source="Azure Retail Prices API (universal resource sweep, "
                                                     "public list, USD)")
        # fallback: a VM deploy the token sweep could not enumerate -> the classic injected bundle resolver
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
