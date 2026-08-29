"""GCP RunRateAdapter (C19 cost axis): price a deployment's provisioned bundle from the Cloud Billing
Catalog API into a provider-independent RunRate. Thin per-provider boundary, like ReduRunRateAdapter.

GCP's Cloud Billing Catalog API accepts a bearer ADC token (``gcloud auth print-access-token`` +
``x-goog-user-project``, no separate API key) once the free, read-only Billing API is enabled on the caller
project (``gcloud services enable cloudbilling.googleapis.com``); a ``GCP_BILLING_API_KEY`` is the fallback.
Without either the bundle is disclosed unpriceable (ok:false), never faked. Prices are USD, so no FX.

Verified service IDs (2026-08-28): Cloud Run ``152E-C115-5142`` (serverless -> a per-usage SCHEDULE from the
request-based CPU/Memory/Requests SKUs); Compute Engine ``6F81-5844-456A`` (VM rates are COMPONENTIZED into
per-vCPU-hour and per-GiB-hour Core/Ram SKUs whose region coverage is INCONSISTENT -- a family's Core and Ram
can live in different aggregate/per-city SKUs, and one can be absent for a region code -- so the VM
run-rate is exact where both components cover the region and same-continent-APPROXIMATE, disclosed, where
not); Cloud SQL ``9662-B51E-5089`` (managed Postgres).

Three paths: SERVERLESS Cloud Run usage cost (priced from the URL alone, no bundle resolution); STANDING GCE
run-rate (the injected ``_gce_resolver`` finds the VM by external IP, `describe`s it for vCPU/RAM, prices the
family); both injectable so composition is tested offline.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlparse

from ..cost import (RateComponent, RunRate, RunRateAdapter, EgressRate, UsageComponent, UsageRate,
                    compose_run_rate, compose_usage_rate, storage_gb_month_to_hourly)

_CATALOG = "https://cloudbilling.googleapis.com/v1/services"
COMPUTE_SERVICE_ID = "6F81-5844-456A"
CLOUD_SQL_SERVICE_ID = "9662-B51E-5089"
CLOUD_RUN_SERVICE_ID = "152E-C115-5142"

BundleResolver = Callable[[object], Optional[dict]]


def _api_key() -> Optional[str]:
    return os.environ.get("GCP_BILLING_API_KEY") or os.environ.get("GOOGLE_BILLING_API_KEY")


def _adc_token() -> Optional[str]:
    """Host-side ADC access token via gcloud (off-clock, never in the VM). The Cloud Billing API accepts a
    bearer ADC token (no separate API key) once the API is enabled on the caller project; None if gcloud or
    auth is unavailable, in which case the caller falls back to GCP_BILLING_API_KEY or discloses N/A."""
    try:
        r = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, timeout=20)
        return (r.stdout or "").strip() or None
    except Exception:  # noqa: BLE001 - cost is best-effort; a failure is disclosed, never faked
        return None


def _adc_project() -> Optional[str]:
    try:
        r = subprocess.run(["gcloud", "config", "get-value", "project"], capture_output=True, text=True, timeout=15)
        p = (r.stdout or "").strip()
        return p if p and p != "(unset)" else None
    except Exception:  # noqa: BLE001
        return None


_SKU_CACHE: dict = {}


def catalog_skus_authed(service_id: str, *, token: str, project: Optional[str], fetch=None) -> List[dict]:
    """All SKUs under a Catalog service using a bearer ADC token + x-goog-user-project (no API key). The
    catalog is large and refetched by every pricing call, so a SUCCESSFUL full result is CACHED per service
    for the process (prices are dated to the day) and each page fetch RETRIES on a transient error - so one
    blip does not truncate the catalog, leave a SKU unfound, and zero the cost. A partial (a page that failed
    after retries) is never cached. Only the live path caches; an injected ``fetch`` (tests) never does."""
    live = fetch is None
    if live and service_id in _SKU_CACHE:
        return _SKU_CACHE[service_id]
    if fetch is None:
        def fetch(url):
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if project:
                headers["x-goog-user-project"] = project
            for attempt in range(5):
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=20) as r:
                        return json.load(r)
                except Exception:  # noqa: BLE001 - transient; retry with backoff, then give up (None)
                    time.sleep(min(2.0 * (attempt + 1), 10.0))
            return None
    out: List[dict] = []
    tok, ok = "", True
    for _ in range(25):
        q = {"pageSize": "500"}
        if tok:
            q["pageToken"] = tok
        data = fetch(f"{_CATALOG}/{service_id}/skus?{urllib.parse.urlencode(q)}")
        if not isinstance(data, dict):
            ok = False                                     # a page failed after retries: do NOT cache a partial
            break
        out.extend(data.get("skus", []) or [])
        tok = data.get("nextPageToken") or ""
        if not tok:
            break
    if live and ok and out:
        _SKU_CACHE[service_id] = out
    return out


def is_cloud_run_url(url: str) -> bool:
    host = urlparse(url if "://" in (url or "") else f"https://{url}").hostname or ""
    return host.endswith(".run.app")


def cloud_run_region_from_url(url: str) -> Optional[str]:
    """Region out of a Cloud Run URL: the deterministic form is ``<svc>-<projnum>.<region>.run.app`` (the
    segment before ``run.app`` is the region); the non-deterministic hash form ``<hash>.run.app`` has no
    region, returns None. The region drives the request-based CPU/Memory price TIER (Tier-1 vs Tier-2)."""
    host = urlparse(url if "://" in (url or "") else f"https://{url}").hostname or ""
    if not host.endswith(".run.app"):
        return None
    left = host[: -len(".run.app")]
    return left.rsplit(".", 1)[1] if "." in left else None


def _first_tier_price(sku: dict) -> Optional[float]:
    for pi in sku.get("pricingInfo", []) or []:
        tiers = (pi.get("pricingExpression", {}) or {}).get("tieredRates", []) or []
        if tiers:
            up = tiers[-1].get("unitPrice", {}) or {}
            return float(up.get("units", 0) or 0) + float(up.get("nanos", 0) or 0) / 1e9
    return None


def cloud_run_usage_rate(capture_date: str, *, region: Optional[str] = None, token: Optional[str] = None,
                         project: Optional[str] = None, skus: Optional[List[dict]] = None,
                         standing_floor_hourly: float = 0.0,
                         extra_notes: tuple = (), omit_requests: bool = False) -> Optional[UsageRate]:
    """Cloud Run priced as a per-usage SCHEDULE (C21), the serverless counterpart of the standing run-rate.
    In the DEFAULT request-based mode it uses the request-based billing SKUs: Requests (per request), CPU per
    vCPU-second and Memory per GiB-second (driver 'other'), all THREE required -- ``compose_usage_rate`` folds
    the per-second CPU/Memory rates into the per-request cost using the disclosed request profile, so the
    schedule includes active compute; without all three the bill is incomplete and this returns None
    (UNPRICED), never a partial. The request-based CPU/Memory rates are region-TIERED (Tier-1 vs Tier-2), so
    ``region`` (from the URL) selects the correct tier; without it a Tier-1 reference region is used and
    disclosed. Egress is held separate like every egress line. ``skus`` injectable for offline tests; else
    fetched live via ADC.

    ``omit_requests=True`` switches to INSTANCE-based billing (a service run with ``--no-cpu-throttling``):
    compute is billed by instance lifetime, NOT per request, so there is no per-request Requests fee AND no
    separate per-request active CPU/Memory charge -- all three request-based components are omitted and the
    schedule is the instance-based standing floor (the always-allocated min-instances baseline), which the
    caller computes from the instance-based SKUs and passes in ``standing_floor_hourly``. Above that baseline
    more instances start under load and are billed by instance lifetime; that above-baseline scaling is
    disclosed by the caller as a note rather than modeled per-request here."""
    pricing_region = region or "us-central1"              # a Tier-1 reference region, disclosed below
    comps: List[UsageComponent] = []
    if not omit_requests:
        if skus is None:
            token = token or _adc_token()
            if not token:
                return None
            skus = catalog_skus_authed(CLOUD_RUN_SERVICE_ID, token=token, project=project or _adc_project())
        if not skus:
            return None

        def _find(pred):
            # collect matches, then PREFER the SKU whose serviceRegions covers the deploy region (correct
            # price TIER), else a global SKU, else any -- so a Tier-1 deploy is not priced at the Tier-2 rate.
            matches = [(s, _first_tier_price(s)) for s in skus
                       if pred(s.get("description", "")) and (_first_tier_price(s) or 0) > 0]
            if not matches:
                return None

            def rank(s):
                regs = s.get("serviceRegions") or []
                if pricing_region in regs:
                    return 0
                if "global" in regs:
                    return 1
                return 2
            matches.sort(key=lambda sp: rank(sp[0]))
            return matches[0][1]

        req = _find(lambda d: d.strip() == "Requests" or d.startswith("Requests"))
        cpu = _find(lambda d: "CPU" in d and "Request-based" in d and "Min Instance" not in d)
        mem = _find(lambda d: "Memory" in d and "Request-based" in d and "Min Instance" not in d)
        if req is not None:
            comps.append(UsageComponent(name="requests", per_unit_usd=req, unit="per request",
                                        driver="requests", raw_unit_price=req, native_unit="per request"))
        if cpu is not None:
            comps.append(UsageComponent(name="compute-active-cpu", per_unit_usd=cpu, unit="vCPU-second",
                                        driver="other", raw_unit_price=cpu, native_unit="per vCPU-second"))
        if mem is not None:
            comps.append(UsageComponent(name="compute-active-mem", per_unit_usd=mem, unit="GiB-second",
                                        driver="other", raw_unit_price=mem, native_unit="per GiB-second"))
        # A request-based Cloud Run bill is requests + active CPU + active memory. If ANY of the three is
        # unpriced the schedule would silently omit a real cost component and understate the bill, so refuse
        # rather than emit a partial (C21 completeness): report UNPRICED, never plausible-but-incomplete.
        if req is None or cpu is None or mem is None:
            return None
    reg_label = pricing_region + ("" if region else " (Tier-1 reference; no region in URL)")
    billing_label = "instance-based" if omit_requests else "request-based"
    ur = compose_usage_rate(comps, provider="gcp", region=reg_label, service="Cloud Run",
                            capture_date=capture_date, standing_floor_hourly_usd=standing_floor_hourly,
                            price_source=f"GCP Cloud Billing Catalog ({billing_label} Cloud Run SKUs, ADC)",
                            price_urls=(f"{_CATALOG}/{CLOUD_RUN_SERVICE_ID}/skus",))
    if extra_notes:
        ur = UsageRate(provider=ur.provider, region=ur.region, service=ur.service,
                       capture_date=ur.capture_date, components=ur.components, schedule=ur.schedule,
                       assumptions=tuple(ur.assumptions) + tuple(extra_notes), price_source=ur.price_source,
                       exclusions=ur.exclusions, price_urls=ur.price_urls, fx=ur.fx)
    return ur


def _run_sku_rate(skus: List[dict], region: Optional[str], needle_terms: List[str],
                  exclude_terms: tuple = ()) -> Optional[float]:
    """Pick the price of a Cloud Run SKU whose description contains every ``needle_terms`` token (and none
    of ``exclude_terms``), PREFERRING the SKU whose serviceRegions cover ``region`` (correct price TIER),
    then a global SKU, then any. None if no SKU matches (disclosed by the caller, never faked)."""
    matches = []
    for s in skus:
        d = s.get("description", "")
        if not all(t in d for t in needle_terms) or any(x in d for x in exclude_terms):
            continue
        p = _first_tier_price(s)
        if p and p > 0:
            matches.append((s, p))
    if not matches:
        return None

    def rank(s):
        regs = s.get("serviceRegions") or []
        if region and region in regs:
            return 0
        if "global" in regs:
            return 1
        return 2
    matches.sort(key=lambda sp: rank(sp[0]))
    return matches[0][1]


def cloud_run_scaling_floor_hourly(skus: List[dict], region: Optional[str], *, min_scale: float,
                                   cpu: float, mem_gib: float, instance_based: bool):
    """The always-on Cloud Run compute floor in $/hr for ``min_scale`` always-allocated instances of
    ``cpu`` vCPU + ``mem_gib`` GiB. Request-based (default) uses the Min-Instance idle SKUs
    ("Services Min Instance CPU/Memory (Request-based billing)"); instance-based (--no-cpu-throttling) uses
    the full-lifetime SKUs ("Services CPU/Memory (Instance-based billing)"). Both are billed per
    vCPU-second / GiB-second, so hourly = min_scale * (cpu*cpu_rate + mem_gib*mem_rate) * 3600. Returns
    (hourly, cpu_rate, mem_rate) or None if a rate SKU is unavailable (the caller then DISCLOSES it, never
    silently omits the floor). These are DELIBERATELY the Min-Instance / Instance-based SKUs the per-request
    usage path excludes."""
    pricing_region = region or "us-central1"              # a Tier-1 reference region when the URL has none
    if instance_based:
        cpu_rate = _run_sku_rate(skus, pricing_region, ["CPU (Instance-based billing)"])
        mem_rate = _run_sku_rate(skus, pricing_region, ["Memory (Instance-based billing)"])
    else:
        cpu_rate = _run_sku_rate(skus, pricing_region, ["Min Instance CPU", "Request-based"],
                                 exclude_terms=("Tier 2",) if pricing_region else ())
        mem_rate = _run_sku_rate(skus, pricing_region, ["Min Instance Memory", "Request-based"],
                                 exclude_terms=("Tier 2",) if pricing_region else ())
    if cpu_rate is None or mem_rate is None:
        return None
    hourly = float(min_scale) * (float(cpu) * cpu_rate + float(mem_gib) * mem_rate) * 3600.0
    return hourly, cpu_rate, mem_rate


def _parse_run_cpu(v) -> Optional[float]:
    """Cloud Run cpu limit ("1", "2", "1000m", "500m") -> vCPU count as a float, or None."""
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return float(s[:-1]) / 1000.0 if s.endswith("m") else float(s)
    except ValueError:
        return None


_MEM_FACTOR_GIB = {"Gi": 1.0, "Mi": 1.0 / 1024.0, "Ki": 1.0 / (1024.0 * 1024.0),
                   "G": 1e9 / 1024.0 ** 3, "M": 1e6 / 1024.0 ** 3, "K": 1e3 / 1024.0 ** 3,
                   "": 1.0 / 1024.0 ** 3}


def _parse_run_mem_gib(v) -> Optional[float]:
    """Cloud Run memory limit ("1Gi", "512Mi", "2Gi", "536870912") -> GiB as a float, or None."""
    m = re.match(r"^([0-9.]+)\s*([A-Za-z]*)$", str(v or "").strip())
    if not m:
        return None
    unit = m.group(2)
    if unit not in _MEM_FACTOR_GIB:
        return None
    return float(m.group(1)) * _MEM_FACTOR_GIB[unit]


def _cloud_run_service_region_from_url(url: str):
    """(service_name, region) parsed from a deterministic Cloud Run URL ``<svc>-<projnum>.<region>.run.app``,
    or None for the hash form (``<hash>.run.app``, no service/region embedded)."""
    host = urlparse(url if "://" in (url or "") else f"https://{url}").hostname or ""
    if not host.endswith(".run.app"):
        return None
    left = host[: -len(".run.app")]
    if "." not in left:
        return None
    svcpart, region = left.rsplit(".", 1)
    name, sep, tail = svcpart.rpartition("-")
    service = name if (sep and tail.isdigit()) else svcpart
    return service, region


ScalingResolver = Callable[[object], Optional[dict]]


def _cloud_run_scaling_resolver(run_cmd=None) -> ScalingResolver:
    """Resolve a Cloud Run URL to its autoscaling config via
    ``gcloud run services describe <service> --region <region> --format=json``: minScale (annotation
    ``autoscaling.knative.dev/minScale``), per-container cpu + memory
    (``spec.template.spec.containers[].resources.limits``), and the billing mode (annotation
    ``run.googleapis.com/cpu-throttling == "false"`` => instance-based). Returns
    {min_scale, cpu, mem_gib, instance_based, service, region} or None when the service cannot be described
    (e.g. a torn-down deploy), in which case the caller assumes a 0 floor AND discloses that the scaling
    config was unavailable, never a silent omission. ``run_cmd`` injectable so unit tests need no cloud."""
    def resolve(deployment_ref: object) -> Optional[dict]:
        sr = _cloud_run_service_region_from_url(str(deployment_ref))
        if not sr:
            return None
        service, region = sr
        d = _gcloud_json(["run", "services", "describe", service, "--region", region,
                          "--platform", "managed"], run_cmd)
        if not isinstance(d, dict) or not d:
            return None
        tmpl = ((d.get("spec") or {}).get("template") or {})
        ann = ((tmpl.get("metadata") or {}).get("annotations") or {})
        try:
            min_scale = int(str(ann.get("autoscaling.knative.dev/minScale", "0") or "0"))
        except ValueError:
            min_scale = 0
        instance_based = str(ann.get("run.googleapis.com/cpu-throttling", "true")).lower() == "false"
        containers = ((tmpl.get("spec") or {}).get("containers") or [])
        cpu = mem = None
        if containers:
            limits = ((containers[0].get("resources") or {}).get("limits") or {})
            cpu = _parse_run_cpu(limits.get("cpu"))
            mem = _parse_run_mem_gib(limits.get("memory"))
        # Cloud Run defaults when a limit is unset, disclosed: 1 vCPU, 512Mi (0.5 GiB).
        if cpu is None:
            cpu = 1.0
        if mem is None:
            mem = 0.5
        return {"service": service, "region": region, "min_scale": min_scale,
                "cpu": float(cpu), "mem_gib": float(mem), "instance_based": instance_based}
    return resolve


def _http_get_json(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "acspeed-cost/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:   # off-clock, host-side; never in the VM
            return json.load(r)
    except Exception:  # noqa: BLE001 - pricing is best-effort; a failure is disclosed, never faked
        return None


def catalog_skus(service_id: str, api_key: str, *, fetch=None) -> List[dict]:
    """All SKUs under a Catalog service, following nextPageToken. ``fetch`` injectable for offline tests."""
    if fetch is None:
        fetch = _http_get_json
    out: List[dict] = []
    token = ""
    for _ in range(50):
        q = {"key": api_key, "pageSize": "5000"}
        if token:
            q["pageToken"] = token
        data = fetch(f"{_CATALOG}/{service_id}/skus?{urllib.parse.urlencode(q)}")
        if not isinstance(data, dict):
            break
        out.extend(data.get("skus", []) or [])
        token = data.get("nextPageToken") or ""
        if not token:
            break
    return out


def sku_unit_price_usd(sku: dict) -> Optional[float]:
    """The on-demand $/usageUnit for a SKU, from the last (highest-usage) tier's unitPrice = units +
    nanos/1e9. None if the SKU carries no priced tier."""
    for pinfo in sku.get("pricingInfo", []) or []:
        expr = pinfo.get("pricingExpression", {}) or {}
        tiers = expr.get("tieredRates", []) or []
        if not tiers:
            continue
        up = tiers[-1].get("unitPrice", {}) or {}
        units = float(up.get("units", 0) or 0)
        nanos = float(up.get("nanos", 0) or 0)
        price = units + nanos / 1e9
        if price > 0:
            return price
    return None


def _matches(sku: dict, region: str, *, family: str, desc_terms: List[str], usage: str = "OnDemand") -> bool:
    cat = sku.get("category", {}) or {}
    if cat.get("resourceFamily") != family or cat.get("usageType") != usage:
        return False
    if region and region not in (sku.get("serviceRegions") or []):
        return False
    desc = str(sku.get("description", "")).lower()
    return all(t.lower() in desc for t in desc_terms)


def compute_vcpu_ram_hourly(region: str, *, family: str = "N1", is_custom: bool = False,
                            token: Optional[str] = None, api_key: Optional[str] = None,
                            skus: Optional[List[dict]] = None, fetch=None) -> Optional[tuple]:
    """(vCPU-hour $, RAM-GiB-hour $) for a Compute Engine machine ``family`` (e.g. ``E2``, ``N1``, ``N2``)
    in ``region``, from the componentized Core/Ram SKUs, or None if either is not found. Compose a VM's
    hourly rate as ``vcpus * vcpu_hr + ram_gb * ram_hr``. Auth: an ADC ``token`` (preferred, no key) or a
    Catalog ``api_key``; ``skus`` injectable for offline tests."""
    if skus is None:
        if token:
            skus = catalog_skus_authed(COMPUTE_SERVICE_ID, token=token, project=_adc_project(), fetch=fetch)
        elif api_key:
            skus = catalog_skus(COMPUTE_SERVICE_ID, api_key, fetch=fetch)
        else:
            return None
    return _gce_core_ram_rates(skus, region, family, is_custom)


_SQL_EXCLUDE = ("Plus", "Extended", "Trial", "Developer", "Preemptible", "Commitment", "Committed")


def _cloud_sql_zonal_rate(skus: List[dict], region: str, kind: str):
    """Base Enterprise-Zonal Cloud SQL PostgreSQL rate for ``kind`` in ('vCPU','RAM','Storage'): exact
    region then same-continent fallback, excluding Plus/Extended/Developer editions. (rate, exact) or None."""
    continent = region.split("-", 1)[0] if region else ""

    def _cands(region_pred):
        out = []
        for s in skus:
            c = s.get("category", {}) or {}
            if c.get("usageType") != "OnDemand":
                continue
            d = s.get("description", "")
            if "PostgreSQL" not in d and "Postgres" not in d:
                continue
            if kind == "Storage":
                # the base per-GB CAPACITY line for a Zonal instance (current SKU name is
                # "Zonal - Enterprise Storage Hyperdisk Balanced Capacity", not "Zonal - Storage").
                # Exclude the separately-billed IOPS / Throughput / Data-Cache dimensions.
                if "Zonal" not in d or "Capacity" not in d:
                    continue
                if any(x in d for x in ("IOPS", "Throughput", "Data Cache")):
                    continue
            elif f"Zonal - {kind}" not in d:
                continue
            if any(x in d for x in _SQL_EXCLUDE):
                continue
            if not region_pred(s.get("serviceRegions") or []):
                continue
            p = _first_tier_price(s)
            if p:
                out.append(p)
        return out
    exact = _cands(lambda regs: (not region) or region in regs)
    if exact:
        return min(exact), True
    cont = _cands(lambda regs: any(str(x).startswith(continent) for x in regs))
    return (min(cont), False) if cont else None


def _parse_cloud_sql_tier(tier: str):
    """(vcpus, ram_gb) for a Cloud SQL tier priced per vCPU + RAM, or None (shared-core tiers use their flat
    SKU instead, see _SHARED_TIER). Covers the custom form ``db-custom-N-M`` -> (N, M MB / 1024) AND the
    legacy predefined ``db-n1-{standard,highmem,highcpu}-N``, which mirror the n1 machine ratios (standard
    3.75, highmem 6.5, highcpu 0.9 GB of RAM per vCPU) -- public specs, so the instance is priced, never
    left UNPRICED, via the same componentized vCPU+RAM SKUs as a custom tier."""
    t = tier or ""
    m = re.match(r"db-custom-(\d+)-(\d+)$", t)
    if m:
        return (float(m.group(1)), float(m.group(2)) / 1024.0)
    ratio = {"standard": 3.75, "highmem": 6.5, "highcpu": 0.9}
    m = re.match(r"db-n1-(standard|highmem|highcpu)-(\d+)$", t)
    if m:
        n = float(m.group(2))
        return (n, n * ratio[m.group(1)])
    return None


# shared-core tiers bill a FLAT per-hour SKU ("Micro/Small instance"), not per vCPU+RAM. The catalog also
# carries "Extended support <tier> vNN" (legacy-version surcharge) and "FDC Trial ..." SKUs for the same
# words; exclude both so we price the plain on-demand instance.
_SHARED_TIER = {"db-f1-micro": "Micro instance", "db-g1-small": "Small instance"}
_SQL_SHARED_EXCLUDE = ("Extended support", "FDC Trial")


def _cloud_sql_shared_rate(skus: List[dict], region: str, label: str):
    """Flat $/hr for a SHARED-CORE Cloud SQL PostgreSQL tier (label = 'Micro instance' / 'Small instance'):
    exact region then same-continent fallback, excluding extended-support and trial SKUs. (rate, exact) or None."""
    continent = region.split("-", 1)[0] if region else ""

    def _cands(region_pred):
        out = []
        for s in skus:
            if (s.get("category", {}) or {}).get("usageType") != "OnDemand":
                continue
            d = s.get("description", "")
            if ("PostgreSQL" not in d and "Postgres" not in d) or f"Zonal - {label}" not in d:
                continue
            if any(x in d for x in _SQL_SHARED_EXCLUDE) or any(x in d for x in _SQL_EXCLUDE):
                continue
            if not region_pred(s.get("serviceRegions") or []):
                continue
            p = _first_tier_price(s)
            if p:
                out.append(p)
        return out
    exact = _cands(lambda regs: (not region) or region in regs)
    if exact:
        return min(exact), True
    cont = _cands(lambda regs: any(str(x).startswith(continent) for x in regs))
    return (min(cont), False) if cont else None


def _cloud_sql_ip_rate(skus: List[dict], region: str):
    """Post-2024 GCP bills an in-use external IPv4 on Cloud SQL. The base per-hour "Zonal - IP address
    reservation" PostgreSQL SKU (category resourceGroup 'IpAddress'), exact region then same-continent
    fallback, excluding the FDC Trial variant. (rate, exact) or None (the caller then DISCLOSES the
    unpriced IP, never silently drops a real charge)."""
    continent = region.split("-", 1)[0] if region else ""

    def _cands(region_pred):
        out = []
        for s in skus:
            c = s.get("category", {}) or {}
            if c.get("resourceGroup") != "IpAddress":
                continue
            d = s.get("description", "")
            if ("PostgreSQL" not in d and "Postgres" not in d) or "IP address reservation" not in d:
                continue
            if "Zonal" not in d or "FDC Trial" in d:      # base Zonal (non-HA) reservation, not the trial
                continue
            if not region_pred(s.get("serviceRegions") or []):
                continue
            p = _first_tier_price(s)
            if p:
                out.append(p)
        return out
    exact = _cands(lambda regs: (not region) or region in regs)
    if exact:
        return min(exact), True
    cont = _cands(lambda regs: any(str(x).startswith(continent) for x in regs))
    return (min(cont), False) if cont else None


def cloud_sql_hourly(region: str, tier: str, storage_gb: float, *, token: Optional[str] = None,
                     project: Optional[str] = None, skus: Optional[List[dict]] = None,
                     ipv4_enabled: Optional[bool] = None):
    """Best-effort standing $/hr for a Cloud SQL PostgreSQL instance: db-custom vCPU x base-Zonal-vCPU +
    RAM x base-Zonal-RAM + storage x base-Zonal-Storage/730, PLUS the in-use external IPv4 charge when the
    instance has a public IP (``ipv4_enabled``, post-2024 billing). Returns (hourly, exact_region) or None
    when the tier is shared/predefined-unpriceable, a rate is unavailable, or a public IP is enabled but its
    SKU cannot be found (the caller then DISCLOSES it as an unpriced resource, never omits it silently).
    Edition assumed Enterprise-Zonal (non-HA), disclosed by the caller."""
    if skus is None:
        token = token or _adc_token()
        if not token:
            return None
        skus = catalog_skus_authed(CLOUD_SQL_SERVICE_ID, token=token, project=project or _adc_project())

    def _with_ip(hourly, exact):
        # A public IPv4 is a real standing charge; if it is enabled but unpriceable, refuse (None) rather
        # than silently drop it (C21 completeness).
        if not ipv4_enabled:
            return hourly, exact
        ipr = _cloud_sql_ip_rate(skus, region)
        if ipr is None:
            return None
        return hourly + ipr[0], (exact and ipr[1])

    if tier in _SHARED_TIER:                              # shared-core: one flat instance SKU + storage
        rate = _cloud_sql_shared_rate(skus, region, _SHARED_TIER[tier])
        if not rate:
            return None
        hourly = rate[0]
        sto = _cloud_sql_zonal_rate(skus, region, "Storage")
        if sto and storage_gb:
            hourly += float(storage_gb) * sto[0] / 730.0
        return _with_ip(hourly, rate[1])
    vr = _parse_cloud_sql_tier(tier)
    if not vr:
        return None
    vcpus, ram_gb = vr
    cpu = _cloud_sql_zonal_rate(skus, region, "vCPU")
    ram = _cloud_sql_zonal_rate(skus, region, "RAM") or _cloud_sql_zonal_rate(skus, region, "Memory")
    if not cpu or not ram:
        return None
    hourly = vcpus * cpu[0] + ram_gb * ram[0]
    sto = _cloud_sql_zonal_rate(skus, region, "Storage")
    if sto and storage_gb:
        hourly += float(storage_gb) * sto[0] / 730.0
    return _with_ip(hourly, (cpu[1] and ram[1]))


def gcp_serverless_extras(run_cmd=None):
    """Enumerate the project's Cloud SQL instances and Cloud Run services so a multi-service serverless
    deploy is fully priced, not silently under-counted. Returns
    ([{region, tier, storage_gb, ipv4_enabled}], n_run_services). ``run_cmd`` injectable for offline tests."""
    sqls = _gcloud_json(["sql", "instances", "list"], run_cmd) or []
    runs = _gcloud_json(["run", "services", "list"], run_cmd) or []
    dbs = []
    for i in sqls:
        s = i.get("settings", {}) or {}
        ipcfg = s.get("ipConfiguration", {}) or {}
        dbs.append({"region": i.get("region"), "tier": s.get("tier"),
                    "storage_gb": float(s.get("dataDiskSizeGb") or 0),
                    "ipv4_enabled": bool(ipcfg.get("ipv4Enabled"))})
    return dbs, (len(runs) if isinstance(runs, list) else 0)


def _gcloud_json(args: List[str], run_cmd=None):
    """Run a `gcloud ... --format=json` command host-side (off-clock, never in the VM), return parsed JSON
    or None. ``run_cmd`` injectable for offline tests."""
    if run_cmd is not None:
        return run_cmd(args)
    try:
        # stdin=DEVNULL so a gcloud that would otherwise prompt (e.g. "enable this API? (y/N)") can never
        # block the off-clock cost pass; it declines non-interactively, returns non-zero, and we disclose.
        r = subprocess.run(["gcloud", *args, "--format=json"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=45)
        return json.loads(r.stdout) if r.returncode == 0 and (r.stdout or "").strip() else None
    except Exception:  # noqa: BLE001 - cost is best-effort; a failure is disclosed, never faked
        return None


# machine-type family prefix -> the Catalog description family TOKEN. The Catalog's Core/Ram SKU naming is
# inconsistent across families (E2 predefined is priced on the "E2 Custom Instance Core" SKU; N1 uses "N1
# Predefined Instance"; N2 uses "N2 Instance"), so we match by this token + "Core running"/"Ram running" and
# a robust exclude-list, rather than an exact series string. An unmapped family returns None (disclosed).
_GCE_FAMILY = {"e2": "E2", "n1": "N1", "n2": "N2", "n2d": "N2D", "n4": "N4", "c3": "C3", "c3d": "C3D",
               "c2": "C2", "c2d": "C2D", "t2d": "T2D", "t2a": "T2A"}
# variants that are NOT the plain on-demand rate for a running instance
_GCE_EXCLUDE = ("Sole Tenancy", "Spot", "Preemptible", "Commitment", "Committed", "Extended", "Reserved")


def _gce_family_for(machine_type: str):
    """(family token, is_custom) for a machine type, or None if the family is unmapped. E.g.
    ``e2-medium`` -> (``E2``, False); ``n2-custom-4-8192`` -> (``N2``, True)."""
    fam = (machine_type or "").split("-", 1)[0].lower()
    token = _GCE_FAMILY.get(fam)
    if not token:
        return None
    return token, ("custom" in (machine_type or "").lower())


def _gce_core_ram_rates(skus: List[dict], region: str, family: str, is_custom: bool):
    """(vCPU-hour $, RAM-GiB-hour $, exact_region) for a family, or None if a component is unavailable.
    GCP's Catalog splits a family's Core/Ram SKUs inconsistently between an aggregate region-group SKU
    (e.g. 'running in EMEA') and per-city SKUs, and some (Core or Ram) are missing for a given region code,
    so an EXACT per-region componentized price is not always resolvable. We therefore try the exact region
    first and, if a component is missing there, fall back to the SAME-CONTINENT OnDemand SKUs (region-code
    prefix, e.g. 'europe-*') and flag the result approximate. ``exact_region`` is True only when both
    components came from a SKU covering the deploy region."""
    continent = (region.split("-", 1)[0] if region else "")

    def _cands(kind: str, region_pred):
        out = []
        for s in skus:
            c = s.get("category", {}) or {}
            if c.get("usageType") != "OnDemand" or c.get("resourceFamily") != "Compute":
                continue
            d = s.get("description", "")
            if family not in d or f"{kind} running" not in d or any(x in d for x in _GCE_EXCLUDE):
                continue
            if not region_pred(s.get("serviceRegions") or []):
                continue
            p = sku_unit_price_usd(s)
            if p is not None and p > 0:
                out.append((("Custom" in d), p))
        return out

    def _pick(cands):
        if not cands:
            return None
        want = [p for cust, p in cands if cust == is_custom]
        return min(want) if want else min(p for _c, p in cands)

    exact = True
    rates = {}
    for kind in ("Core", "Ram"):
        r = _pick(_cands(kind, lambda regs: (not region) or region in regs))
        if r is None:                                     # exact region missing this component
            exact = False
            r = _pick(_cands(kind, lambda regs: any(str(x).startswith(continent) for x in regs)))
        rates[kind] = r
    if rates["Core"] is None or rates["Ram"] is None:
        return None
    return (rates["Core"], rates["Ram"], exact)


def _gce_resolver(run_cmd=None) -> BundleResolver:
    """Resolve a standing Compute Engine deploy to its priced bundle: find the instance whose external IP
    serves the deployed URL (`gcloud compute instances list`), read its machineType + zone, `describe` the
    machine type for exact vCPU + RAM, and map the family to a Catalog series. Compute is then composed from
    the per-core + per-GiB request-independent SKUs (a first-order standing rate; E2 shared-core is billed on
    a fraction, so this slightly over-states micro/small/medium, disclosed). Returns None if no instance
    matches or the family is unmapped (disclosed, never faked)."""
    def resolve(deployment_ref: object) -> Optional[dict]:
        host = urlparse(str(deployment_ref) if "://" in str(deployment_ref)
                        else f"https://{deployment_ref}").hostname or ""
        insts = _gcloud_json(["compute", "instances", "list"], run_cmd) or []
        def _ips(i):
            return [ac.get("natIP") for ni in (i.get("networkInterfaces") or [])
                    for ac in (ni.get("accessConfigs") or []) if ac.get("natIP")]
        inst = next((i for i in insts if host in _ips(i)), None)
        if not inst:
            return None
        zone = str(inst.get("zone", "")).rsplit("/", 1)[-1]
        region = zone.rsplit("-", 1)[0] if "-" in zone else zone
        mt = str(inst.get("machineType", "")).rsplit("/", 1)[-1]
        fam = _gce_family_for(mt)
        if not fam:
            return None
        family, is_custom = fam
        d = _gcloud_json(["compute", "machine-types", "describe", mt, "--zone", zone], run_cmd) or {}
        vcpus = d.get("guestCpus")
        ram_gb = (float(d.get("memoryMb") or 0) / 1024.0) or None
        if not vcpus or not ram_gb:
            return None
        return {"region": region, "family": family, "is_custom": is_custom, "machine_type": mt,
                "vcpus": float(vcpus), "ram_gb": round(ram_gb, 3), "managed": []}
    return resolve


# ============================================================================================
# UNIVERSAL asset-inventory sweep: discover EVERY resource this deploy created, of ANY type, from
# GCP's own complete inventory (Cloud Asset Inventory), then dispatch each to a pricer by its
# assetType. This is the GCP counterpart of redu's single complete pricing table: discovery is
# EXHAUSTIVE (the inventory lists every asset, so a type we never anticipated -- a GCS bucket, a
# Memorystore instance, a Pub/Sub topic, a load balancer, a brand-new service -- is still SEEN);
# pricing is best-effort-WITH-DISCLOSURE (an asset whose type has no pricer, or whose pricer cannot
# resolve a needed dimension offline, is added to unpriced_resources WITH its assetType and NEVER
# silently dropped or faked as $0). Adding a pricer for a new type is a one-line registry entry; a
# type with no pricer is caught and disclosed with NO new code, which is the whole point.
# ============================================================================================

CLOUD_RUN_ASSET_TYPE = "run.googleapis.com/Service"
CLOUD_SQL_ASSET_TYPE = "sqladmin.googleapis.com/Instance"
# In the run_rate sweep these two are already priced by the URL/enumeration FAST PATH above, so the
# sweep RECOGNIZES them (never re-sums, never discloses them as unpriced) and prices everything else.
_DEFAULT_FASTPATH_TYPES = (CLOUD_RUN_ASSET_TYPE, CLOUD_SQL_ASSET_TYPE)

# The env flag that activates the sweep on the default (non-injected) adapter, once the Cloud Asset
# API is enabled on the caller project (`gcloud services enable cloudasset.googleapis.com`). It is
# OFF by default so the standing test suite stays hermetic (no gcloud spawn) and so a project without
# the API pays no failed-call tax; the injectable ``resolve_assets`` seam activates it in tests.
_ASSET_SWEEP_ENV = "ACSPEED_GCP_ASSET_SWEEP"

AssetResolver = Callable[[object], Optional[List[dict]]]


def gcp_project_assets(scope: str, query: str, run_cmd=None,
                       asset_types: Optional[List[str]] = None) -> List[dict]:
    """The COMPLETE enumerator: list ALL resources in ``scope`` matching ``query``, of ANY type, via
    Cloud Asset Inventory. Default (run_cmd=None) runs, host-side and off-clock::

        gcloud asset search-all-resources --scope=projects/<project> --query="name:<run-token>" --format=json

    (optionally ``--asset-types=<a,b,...>`` when ``asset_types`` is given). Each result carries at least
    ``assetType`` (e.g. ``run.googleapis.com/Service``, ``compute.googleapis.com/Disk``,
    ``storage.googleapis.com/Bucket``), ``name`` (full resource name), ``location`` and
    ``additionalAttributes`` -- the fields the dispatcher routes and prices on. ``run_cmd`` is injectable
    so tests run OFFLINE with a canned asset list. The Cloud Asset API must be enabled on the caller
    project (`gcloud services enable cloudasset.googleapis.com`); when it is not, gcloud returns non-zero
    and this yields [] (disclosed by the caller as an empty sweep, never faked)."""
    args = ["asset", "search-all-resources", f"--scope={scope}"]
    if query:
        args.append(f"--query={query}")
    if asset_types:
        args.append("--asset-types=" + ",".join(asset_types))
    data = _gcloud_json(args, run_cmd)
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def _asset_query_anchor(deployment_ref: object) -> str:
    """The strongest substring that identifies THIS deploy's resources in a full resource name: the
    harness run-token (``acs<hex>``, carried into resource names by construction) if present, else the
    Cloud Run service / first host label. Used to build the default ``name:<anchor>`` inventory query so
    the sweep scopes to this deploy, not the whole project."""
    ref = str(deployment_ref or "")
    host = (urlparse(ref if "://" in ref else f"https://{ref}").hostname or "").lower()
    m = re.search(r"acs[0-9a-f]{6,}", host) or re.search(r"acs[0-9a-f]{6,}", ref.lower())
    if m:
        return m.group(0)
    sr = _cloud_run_service_region_from_url(ref)
    if sr:
        return sr[0]
    label = host.split(".")[0] if host else ""
    return re.sub(r"-\d+$", "", label)          # strip a trailing -<projnum>/-<hash> enumeration suffix


def gcp_assets_for_deployment(deployment_ref: object, *, project: Optional[str] = None,
                              run_cmd=None) -> List[dict]:
    """Default ``resolve_assets``: enumerate the deploy's resources from Cloud Asset Inventory, scoped to
    the ADC project and queried by this deploy's anchor (run-token / service name). Returns [] when no
    project is resolvable (disclosed as an empty sweep, never faked). ``run_cmd`` injectable for tests."""
    # The default (live) resolver requires the harness run-token to bound the query to THIS deploy; without
    # it we cannot scope the inventory safely, so return an empty sweep rather than sweep the whole project
    # or shell out for a non-deploy ref. This also keeps the default adapter hermetic on test stubs.
    if not re.search(r"acs[0-9a-f]{6,}", str(deployment_ref or "").lower()):
        return []
    project = project or _adc_project()
    if not project:
        return []
    anchor = _asset_query_anchor(deployment_ref)
    query = f"name:{anchor}" if anchor else ""
    return gcp_project_assets(f"projects/{project}", query, run_cmd=run_cmd)


@dataclass(frozen=True)
class AssetSweep:
    """The universal sweep's result. ``floor_hourly_usd`` is the sum of the standing $/hr the sweep could
    price; ``priced`` and ``unpriced_resources`` list what was priced vs DISCLOSED (each unpriced entry
    keeps its ``asset_type`` and ``name`` so nothing is silently dropped); ``notes`` are the human-facing
    disclosure lines to fold into the estimate's assumptions."""
    floor_hourly_usd: float
    priced: Tuple[dict, ...] = ()
    unpriced_resources: Tuple[dict, ...] = ()
    notes: Tuple[str, ...] = ()


class _SweepCtx:
    """Shared context handed to each per-assetType pricer: the region fallback (from the served URL),
    Catalog auth, an injectable follow-up ``run_cmd`` (for a describe a pricer may need), and a LAZY
    Compute-service SKU cache (fetched once, only if a pricer needs it and auth exists)."""
    def __init__(self, url_region, token, project, run_cmd, fetch, compute_skus):
        self.url_region = url_region
        self.token = token
        self.project = project
        self.run_cmd = run_cmd
        self.fetch = fetch
        self._compute_skus = compute_skus
        self._compute_fetched = compute_skus is not None

    def compute_skus(self):
        if not self._compute_fetched:
            self._compute_skus = (catalog_skus_authed(COMPUTE_SERVICE_ID, token=self.token,
                                  project=self.project, fetch=self.fetch) if self.token else None)
            self._compute_fetched = True
        return self._compute_skus


def _short_name(asset: dict) -> str:
    n = str(asset.get("name") or "")
    return n.rsplit("/", 1)[-1] if n else str(asset.get("displayName") or "(unnamed)")


def _asset_region(asset: dict, ctx: "_SweepCtx") -> str:
    """The pricing region for an asset. A zonal location (``europe-west1-b``, 2+ dashes) reduces to its
    region; a regional / global / empty location falls back to the served-URL region."""
    loc = str(asset.get("location") or "")
    if loc and loc.count("-") >= 2:
        return loc.rsplit("-", 1)[0]
    if loc and loc not in ("global",):
        return loc
    return ctx.url_region or loc


def _attrs(asset: dict) -> dict:
    a = asset.get("additionalAttributes")
    return a if isinstance(a, dict) else {}


def _sweep_compute_instance(asset: dict, ctx: "_SweepCtx"):
    """A standing Compute Engine VM the deploy left running: price it from the SAME componentized
    Core/Ram SKUs the URL VM path uses (``_gce_core_ram_rates``). vCPU + RAM + machineType come from the
    inventory's ``additionalAttributes`` when present, else a follow-up ``machine-types describe`` (needs
    ``run_cmd``); if neither resolves the size, or the family is unmapped, or the Core/Ram SKUs are
    unavailable, return (None, reason) so the caller DISCLOSES it, never faked."""
    attrs = _attrs(asset)
    region = _asset_region(asset, ctx)
    loc = str(asset.get("location") or "")
    zone = loc if loc.count("-") >= 2 else ""
    mt = str(attrs.get("machineType") or "").rsplit("/", 1)[-1]
    vcpus = attrs.get("guestCpus") or attrs.get("vcpus")
    mem_mb = attrs.get("memoryMb")
    if (not mt or not vcpus or not mem_mb) and ctx.run_cmd is not None and zone:
        if not mt:
            desc = _gcloud_json(["compute", "instances", "describe", _short_name(asset),
                                 "--zone", zone], ctx.run_cmd) or {}
            mt = str(desc.get("machineType") or "").rsplit("/", 1)[-1] or mt
        if mt and (not vcpus or not mem_mb):
            mtd = _gcloud_json(["compute", "machine-types", "describe", mt, "--zone", zone],
                               ctx.run_cmd) or {}
            vcpus = vcpus or mtd.get("guestCpus")
            mem_mb = mem_mb or mtd.get("memoryMb")
    if not mt:
        return None, "machine type not resolvable from inventory (a compute describe was unavailable)"
    fam = _gce_family_for(mt)
    if not fam:
        return None, f"machine family for '{mt}' is unmapped in the Catalog token table"
    family, is_custom = fam
    if not vcpus or not mem_mb:
        return None, f"vCPU/RAM for '{mt}' not resolvable (a machine-types describe was unavailable)"
    skus = ctx.compute_skus()
    if not skus:
        return None, "Compute Core/Ram SKUs unavailable (no Catalog auth)"
    rates = _gce_core_ram_rates(skus, region, family, is_custom)
    if not rates:
        return None, f"no {family} Core/Ram SKU covers {region}"
    core, ram, exact = rates
    ram_gb = float(mem_mb) / 1024.0
    hourly = float(vcpus) * core + ram_gb * ram
    note = (f"Compute Engine {mt} ({float(vcpus):g} vCPU + {round(ram_gb, 3):g} GiB) in {region}"
            + ("" if exact else ", region-approximate"))
    return hourly, note


def _pd_capacity_gb_month(skus: Optional[List[dict]], region: str) -> Optional[float]:
    """Cheapest per-GB-month Compute persistent-disk / hyperdisk CAPACITY rate for ``region`` (exact then
    same-continent), from the Storage-family OnDemand SKUs. None if unavailable (caller discloses)."""
    if not skus:
        return None
    continent = region.split("-", 1)[0] if region else ""

    def _cands(region_pred):
        out = []
        for s in skus:
            c = s.get("category", {}) or {}
            if c.get("usageType") != "OnDemand" or c.get("resourceFamily") != "Storage":
                continue
            d = str(s.get("description", ""))
            if "Capacity" not in d or not any(t in d for t in ("PD", "Persistent Disk", "Hyperdisk")):
                continue
            if any(x in d for x in ("Snapshot", "Image", "Commitment", "Committed")):
                continue
            if not region_pred(s.get("serviceRegions") or []):
                continue
            p = sku_unit_price_usd(s)
            if p and p > 0:
                out.append(p)
        return out
    exact = _cands(lambda regs: (not region) or region in regs)
    if exact:
        return min(exact)
    cont = _cands(lambda regs: any(str(x).startswith(continent) for x in regs))
    return min(cont) if cont else None


def _sweep_compute_disk(asset: dict, ctx: "_SweepCtx"):
    """A persistent / regional disk: priced GB x per-GB-month capacity rate / 730. Size comes from the
    inventory's ``additionalAttributes.sizeGb`` when present, else a ``disks describe`` (needs run_cmd);
    the rate from the Compute Storage capacity SKUs. If size or rate is unavailable, return (None, reason)
    so the caller DISCLOSES the disk, never a silent $0."""
    attrs = _attrs(asset)
    region = _asset_region(asset, ctx)
    loc = str(asset.get("location") or "")
    zone = loc if loc.count("-") >= 2 else ""
    size = attrs.get("sizeGb") or attrs.get("size_gb")
    if not size and ctx.run_cmd is not None and zone:
        desc = _gcloud_json(["compute", "disks", "describe", _short_name(asset), "--zone", zone],
                            ctx.run_cmd) or {}
        size = desc.get("sizeGb")
    try:
        size_gb = float(size) if size is not None else 0.0
    except (TypeError, ValueError):
        size_gb = 0.0
    if size_gb <= 0:
        return None, "disk size not resolvable from inventory (a disks describe was unavailable)"
    rate = _pd_capacity_gb_month(ctx.compute_skus(), region)
    if rate is None:
        return None, f"persistent-disk capacity SKU unavailable for {region}"
    hourly = storage_gb_month_to_hourly(rate, size_gb)
    return hourly, f"persistent disk {size_gb:g} GB in {region} at ${rate:g}/GB-month"


def _sweep_compute_address(asset: dict, ctx: "_SweepCtx"):
    """A reserved external IP address: priced from the Compute static/external-IP standing hourly SKU when
    the Catalog carries one for the region; else disclosed. Only external addresses carry a charge (an
    internal address is free); an INTERNAL address is disclosed as $0-by-rule, not faked."""
    attrs = _attrs(asset)
    if str(attrs.get("purpose") or "").upper() in ("GCE_ENDPOINT", "DNS_RESOLVER") or \
            str(attrs.get("addressType") or "").upper() == "INTERNAL":
        return 0.0, "internal IP address (no standing charge by GCP rule)"
    region = _asset_region(asset, ctx)
    skus = ctx.compute_skus()
    if not skus:
        return None, "external-IP SKU unavailable (no Catalog auth)"
    continent = region.split("-", 1)[0] if region else ""

    def _cands(region_pred):
        out = []
        for s in skus:
            if (s.get("category", {}) or {}).get("usageType") != "OnDemand":
                continue
            d = str(s.get("description", ""))
            if not (("External IP" in d or "Static Ip" in d or "External Ip" in d) and "IP" in d.upper()):
                continue
            if "Idle" in d or "Unused" in d:                 # the plain in-use external-IP rate
                continue
            if not region_pred(s.get("serviceRegions") or []):
                continue
            p = sku_unit_price_usd(s)
            if p and p > 0:
                out.append(p)
        return out
    exact = _cands(lambda regs: (not region) or region in regs)
    cont = exact or _cands(lambda regs: any(str(x).startswith(continent) for x in regs))
    if not cont:
        return None, f"external-IP standing SKU not found for {region}"
    return min(cont), f"reserved external IP in {region}"


def _sweep_gcs_bucket(asset: dict, ctx: "_SweepCtx"):
    """A Cloud Storage bucket is USAGE-metered (stored GB-month + operations + egress), with no standing
    hourly rate and a stored volume the inventory does not expose. It is DISCLOSED (never faked $0): a
    bucket the deploy created is surfaced with its type and name so a reader knows a usage-metered line
    exists, even though a fixed $/hr would be meaningless for it."""
    return None, "Cloud Storage bucket is usage-metered (stored GB-month + operations + egress); the " \
                 "stored volume is not exposed by the inventory, so no standing $/hr is asserted"


# assetType -> pricer. Adding a type is ONE row here; a type absent from this table is still discovered
# by the sweep and DISCLOSED in unpriced_resources (never dropped), which is the catch-all guarantee.
_ASSET_PRICERS = {
    "compute.googleapis.com/Instance": _sweep_compute_instance,
    "compute.googleapis.com/Disk": _sweep_compute_disk,
    "compute.googleapis.com/RegionDisk": _sweep_compute_disk,
    "compute.googleapis.com/Address": _sweep_compute_address,
    "compute.googleapis.com/GlobalAddress": _sweep_compute_address,
    "storage.googleapis.com/Bucket": _sweep_gcs_bucket,
}


def price_discovered_assets(assets: List[dict], *, capture_date: str = "", url_region: Optional[str] = None,
                            token: Optional[str] = None, project: Optional[str] = None,
                            compute_skus: Optional[List[dict]] = None, run_cmd=None, fetch=None,
                            fastpath_types: Tuple[str, ...] = _DEFAULT_FASTPATH_TYPES) -> AssetSweep:
    """The DISPATCHER: for EACH discovered asset, route to a pricer by its ``assetType`` and either price
    it (folding a standing $/hr into ``floor_hourly_usd``) or DISCLOSE it in ``unpriced_resources`` with
    its assetType + name -- never silently dropped, never faked $0 (Part-4 completeness). ``fastpath_types``
    are the types already priced upstream (Cloud Run + Cloud SQL by the URL/enumeration fast path): they
    are recognized as priced and neither re-summed nor disclosed as unpriced. A type with NO entry in
    ``_ASSET_PRICERS`` -- a resource type this tool never anticipated -- flows straight to
    ``unpriced_resources`` with NO new code, which is the whole point of inventory-driven discovery."""
    ctx = _SweepCtx(url_region, token, project, run_cmd, fetch, compute_skus)
    floor = 0.0
    priced: List[dict] = []
    unpriced: List[dict] = []
    notes: List[str] = []
    for a in assets:
        if not isinstance(a, dict):
            continue
        at = str(a.get("assetType") or "")
        name = str(a.get("name") or a.get("displayName") or "(unnamed)")
        short = _short_name(a)
        if at in fastpath_types:
            priced.append({"asset_type": at, "name": name, "hourly_usd": 0.0,
                           "note": "recognized; priced by the URL/enumeration fast path (not re-summed)"})
            continue
        pricer = _ASSET_PRICERS.get(at)
        if pricer is None:
            unpriced.append({"asset_type": at, "name": name,
                             "reason": "no pricer for this assetType in the adapter"})
            notes.append(f"universal sweep: discovered {at} '{short}' with no pricer; DISCLOSED unpriced, "
                         "never silently $0 (a resource type the tool does not yet price)")
            continue
        try:
            hourly, why = pricer(a, ctx)
        except Exception as e:  # noqa: BLE001 - a pricer failure is disclosed, never faked
            hourly, why = None, f"pricer raised {type(e).__name__}"
        if hourly is None:
            unpriced.append({"asset_type": at, "name": name, "reason": why})
            notes.append(f"universal sweep: discovered {at} '{short}'; {why}; DISCLOSED unpriced, never "
                         "silently $0")
        else:
            floor += float(hourly)
            priced.append({"asset_type": at, "name": name, "hourly_usd": float(hourly), "note": why})
            if float(hourly) > 0:
                notes.append(f"universal sweep: folded {at} '{short}' as a standing "
                             f"${round(float(hourly), 4)}/hr ({why})")
    return AssetSweep(floor_hourly_usd=round(floor, 6), priced=tuple(priced),
                      unpriced_resources=tuple(unpriced), notes=tuple(notes))


def _resolve_assets_for(adapter, deployment_ref) -> Optional[AssetResolver]:
    """The sweep's enumerator for a run_rate call: the injected ``resolve_assets`` if given, else the
    default Cloud Asset Inventory resolver ONLY when the activation env flag is set (so the default
    adapter stays hermetic / cost-free until a project enables the Cloud Asset API)."""
    if adapter._resolve_assets is not None:
        return adapter._resolve_assets
    # Default-on (consistent with the Azure/AWS sweeps): the resolver self-limits to refs carrying the
    # harness run-token and returns [] otherwise, so a test stub never shells out. An explicit
    # ACSPEED_GCP_ASSET_SWEEP=0 disables it (e.g. before the Cloud Asset API is enabled on a project).
    if os.environ.get(_ASSET_SWEEP_ENV, "1") == "0":
        return None
    return gcp_assets_for_deployment


class GcpRunRateAdapter(RunRateAdapter):
    def __init__(self, resolve_bundle: Optional[BundleResolver] = None, call_tool=None,
                 resolve_scaling: Optional[ScalingResolver] = None,
                 resolve_assets: Optional[AssetResolver] = None):
        # default: resolve a standing GCE deploy via `gcloud compute instances list` (serverless run.app URLs
        # never reach the resolver; they take the usage-schedule branch in run_rate). Injectable for tests.
        self._resolve = resolve_bundle or _gce_resolver()
        # default: resolve a Cloud Run service's autoscaling config via `gcloud run services describe`.
        self._resolve_scaling = resolve_scaling or _cloud_run_scaling_resolver()
        # the UNIVERSAL asset-inventory sweep's enumerator. None keeps the default adapter hermetic /
        # cost-free until the ACSPEED_GCP_ASSET_SWEEP env flag opts in (once the Cloud Asset API is
        # enabled); tests inject a canned resolver here to exercise discovery + dispatch offline.
        self._resolve_assets = resolve_assets

    def run_rate(self, deployment_ref: object, *, capture_date: str):
        """Price the deployment. A serverless Cloud Run URL (``*.run.app``) is priced as a per-usage
        SCHEDULE from the Cloud Run SKUs (no bundle resolution needed: the URL is the whole input);
        anything else is priced as a standing hourly run-rate from its resolved bundle (the VM path, which
        still needs the injected resolver + a Catalog key/ADC). Returns a UsageRate, a RunRate, or None
        (disclosed, never faked)."""
        if isinstance(deployment_ref, str) and is_cloud_run_url(deployment_ref):
            # ENUMERATE the deploy's other billable resources so a multi-service serverless deploy is fully
            # priced, not silently under-counted: fold the Cloud Run always-on min-instances / instance-based
            # compute floor and any Cloud SQL (with its public IPv4) as a standing floor on the schedule
            # (best-effort, edition disclosed), and disclose a resource we cannot price rather than drop it.
            region = cloud_run_region_from_url(deployment_ref)
            floor, notes, omit_requests = 0.0, [], False
            run_skus = None
            # -- Cloud Run always-on scaling floor (GAP 1 request-based min-instance, GAP 2 instance-based) --
            try:
                run_token = _adc_token()
                run_skus = (catalog_skus_authed(CLOUD_RUN_SERVICE_ID, token=run_token,
                                                project=_adc_project()) if run_token else None)
                scaling = self._resolve_scaling(deployment_ref)
                if scaling is None:
                    # A torn-down (or otherwise indescribable) service: assume a 0 floor but DISCLOSE it, never
                    # silently omit a min-instances cost the deploy may have carried.
                    notes.append("Cloud Run scaling config was unavailable (service not describable, e.g. a "
                                 "torn-down deploy); the always-on min-instances floor is assumed $0 and may "
                                 "understate a deploy that ran with min-instances>=1")
                else:
                    omit_requests = bool(scaling.get("instance_based"))
                    min_scale = float(scaling.get("min_scale") or 0)
                    cpu = float(scaling.get("cpu") or 0)
                    mem_gib = float(scaling.get("mem_gib") or 0)
                    if min_scale >= 1 and run_skus:
                        fl = cloud_run_scaling_floor_hourly(run_skus, region, min_scale=min_scale, cpu=cpu,
                                                            mem_gib=mem_gib, instance_based=omit_requests)
                        if fl is not None:
                            floor += fl[0]
                            if omit_requests:
                                notes.append(f"Cloud Run uses instance-based billing (--no-cpu-throttling): "
                                             f"{min_scale:g} instance(s) x ({cpu:g} vCPU + {mem_gib:g} GiB) at "
                                             f"the instance-based SKUs = ${round(fl[0], 4)}/hr, billed by "
                                             "instance lifetime (no per-request Requests fee); above this "
                                             "baseline more instances start under load and are billed the "
                                             "same way (above-baseline scaling not modeled per-request here)")
                            else:
                                notes.append(f"folded a Cloud Run min-instances floor: {min_scale:g} min "
                                             f"instance(s) x ({cpu:g} vCPU + {mem_gib:g} GiB) always allocated "
                                             f"at the request-based Min-Instance idle SKUs = "
                                             f"${round(fl[0], 4)}/hr; the per-request usage schedule is on top")
                        else:
                            notes.append("Cloud Run min-instances floor could not be priced (Min-Instance / "
                                         "instance-based SKU absent for the region); disclosed, not silently $0")
                    elif omit_requests:
                        notes.append("Cloud Run uses instance-based billing (--no-cpu-throttling) with "
                                     "min-instances=0: no always-on baseline; instances start under load and "
                                     "are billed by lifetime (not modeled per-request here)")
            except Exception:  # noqa: BLE001 - the floor is best-effort; a failure is disclosed
                notes.append("could not resolve the Cloud Run scaling config (min-instances floor assumed $0)")
            # -- Cloud SQL (with public IPv4, GAP 4) folded as a standing floor --
            try:
                dbs, n_run = gcp_serverless_extras()
                for db in dbs:
                    ipv4 = bool(db.get("ipv4_enabled"))
                    priced = cloud_sql_hourly(str(db.get("region") or ""), str(db.get("tier") or ""),
                                              float(db.get("storage_gb") or 0), ipv4_enabled=ipv4)
                    if priced is not None:
                        floor += priced[0]
                        notes.append(f"folded a Cloud SQL instance ({db.get('tier')}) as a standing floor "
                                     f"${round(priced[0], 4)}/hr (Enterprise-Zonal edition assumed"
                                     f"{'; in-use public IPv4 included' if ipv4 else ''}"
                                     f"{'' if priced[1] else ', region-approximate'})")
                    else:
                        # A database (or its public IP) the deploy provisioned but we cannot price would make
                        # the schedule omit a real standing cost and understate the bill. Refuse the whole cost
                        # (UNPRICED, C21 completeness) rather than return a plausible-but-incomplete number.
                        return None
                if n_run > 1:
                    notes.append(f"{n_run} Cloud Run services exist in the project; only the served URL's "
                                 "service is priced here (additional services not folded)")
            except Exception:  # noqa: BLE001 - enumeration is best-effort; a failure is disclosed
                notes.append("could not enumerate Cloud SQL / other services (cost is the served service only)")
            # -- Artifact Registry image storage (GAP 5): de-minimis, disclosed rather than folded --
            notes.append("Artifact Registry image storage is not folded here: a single app image sits within "
                         "the 0.5 GB free tier (storage beyond that lists at $0.10/GB-month), a de-minimis "
                         "line disclosed rather than silently omitted")
            # -- UNIVERSAL asset-inventory sweep: catch EVERY other resource type the deploy created --
            # The two fast-path types (Cloud Run + Cloud SQL) are already priced above; this sweep folds
            # any OTHER standing resource it can price (a Compute disk/instance/IP, ...) and DISCLOSES any
            # type it cannot (a GCS bucket, a Memorystore, a Pub/Sub topic, a brand-new service) rather
            # than silently missing it. Best-effort and exception-guarded: a sweep failure is disclosed.
            assets_resolver = _resolve_assets_for(self, deployment_ref)
            if assets_resolver is not None:
                try:
                    assets = assets_resolver(deployment_ref) or []
                    sweep = price_discovered_assets(assets, capture_date=capture_date, url_region=region,
                                                    token=_adc_token(), project=_adc_project(),
                                                    fastpath_types=_DEFAULT_FASTPATH_TYPES)
                    floor += sweep.floor_hourly_usd
                    notes.extend(sweep.notes)
                except Exception:  # noqa: BLE001 - the sweep is best-effort; a failure is disclosed
                    notes.append("universal asset-inventory sweep failed (disclosed; Cloud Run + Cloud SQL "
                                 "are still priced above)")
            return cloud_run_usage_rate(capture_date, region=region, skus=run_skus,
                                        standing_floor_hourly=floor, extra_notes=tuple(notes),
                                        omit_requests=omit_requests)
        b = self._resolve(deployment_ref)
        if not b:
            return None
        region = str(b.get("region", ""))
        token = _adc_token()                              # ADC preferred (no key); Catalog key is the fallback
        key = None if token else _api_key()
        comps: List[RateComponent] = []

        price_note = ""
        vm_price = b.get("vm_hourly_usd")
        if vm_price is None and (token or key) and b.get("vcpus") and b.get("ram_gb"):
            rates = compute_vcpu_ram_hourly(region, family=str(b.get("family", "N1")),
                                            is_custom=bool(b.get("is_custom")), token=token, api_key=key)
            if rates:
                vcpu_hr, ram_hr, exact = rates
                vm_price = float(b["vcpus"]) * vcpu_hr + float(b["ram_gb"]) * ram_hr
                if not exact:
                    price_note = (" (compute rate is region-APPROXIMATE: the Catalog lacks a "
                                  f"{b.get('family')} Core/Ram SKU for {region}, so a same-continent rate "
                                  "was used)")
        if vm_price is None:
            return None                                   # cannot price app compute -> disclose, do not fake
        comps.append(RateComponent(name="compute", hourly_usd=float(vm_price),
                                   raw_unit_price=float(vm_price), native_unit="instance-hour"))

        for m in b.get("managed", []) or []:
            price = m.get("hourly_usd")
            if price is not None:
                comps.append(RateComponent(name="compute:" + str(m.get("role", "datastore")),
                                           hourly_usd=float(price), raw_unit_price=float(price),
                                           native_unit="instance-hour"))

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
        src = ("GCP Cloud Billing Catalog (public list, USD, ADC)" if token else
               ("GCP Cloud Billing Catalog (public list, USD, API key)" if key else
                "GCP (bundle supplied; no Catalog auth)")) + price_note
        # -- UNIVERSAL asset-inventory sweep on the standing (VM) path too: the app VM is the fast-path
        # type here, so it is not double-counted; every OTHER resource (extra disks, a reserved IP, a GCS
        # bucket, a Memorystore, ...) is priced-or-disclosed. Best-effort, exception-guarded.
        assets_resolver = _resolve_assets_for(self, deployment_ref)
        if assets_resolver is not None:
            try:
                assets = assets_resolver(deployment_ref) or []
                sweep = price_discovered_assets(assets, capture_date=capture_date, url_region=region,
                                                token=token, project=_adc_project(),
                                                fastpath_types=("compute.googleapis.com/Instance",))
                if sweep.floor_hourly_usd:
                    comps.append(RateComponent(name="asset-sweep", hourly_usd=sweep.floor_hourly_usd,
                                               raw_unit_price=sweep.floor_hourly_usd, native_unit="hour"))
                if sweep.unpriced_resources:
                    src += (" (asset-sweep DISCLOSED unpriced: "
                            + ", ".join(u["asset_type"] for u in sweep.unpriced_resources) + ")")
            except Exception:  # noqa: BLE001 - the sweep is best-effort; a failure is disclosed
                src += " (asset-sweep failed; disclosed)"
        return compose_run_rate(comps, provider="gcp", region=region,
                                flavor=str(b.get("machine_type", b.get("family", b.get("flavor", "")))),
                                capture_date=capture_date, egress=egress, price_source=src,
                                price_urls=(_CATALOG,))
