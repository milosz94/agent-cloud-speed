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
import urllib.parse
import urllib.request
from typing import Callable, List, Optional
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


def catalog_skus_authed(service_id: str, *, token: str, project: Optional[str], fetch=None) -> List[dict]:
    """All SKUs under a Catalog service using a bearer ADC token + x-goog-user-project (no API key).
    ``fetch`` injectable for offline tests."""
    if fetch is None:
        def fetch(url):
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if project:
                headers["x-goog-user-project"] = project
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as r:
                    return json.load(r)
            except Exception:  # noqa: BLE001
                return None
    out: List[dict] = []
    tok = ""
    for _ in range(25):
        q = {"pageSize": "500"}
        if tok:
            q["pageToken"] = tok
        data = fetch(f"{_CATALOG}/{service_id}/skus?{urllib.parse.urlencode(q)}")
        if not isinstance(data, dict):
            break
        out.extend(data.get("skus", []) or [])
        tok = data.get("nextPageToken") or ""
        if not tok:
            break
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
                         extra_notes: tuple = ()) -> Optional[UsageRate]:
    """Cloud Run priced as a per-usage SCHEDULE (C21), the serverless counterpart of the standing run-rate.
    Uses the DEFAULT request-based billing SKUs: Requests (folds into the schedule), CPU per vCPU-second and
    Memory per GiB-second (driver 'other': reported as unit rates, not folded, because they depend on
    per-request duration + the deployment's CPU/memory allocation, which the served URL does not reveal).
    The request-based CPU/Memory rates are region-TIERED (Tier-1 vs Tier-2), so ``region`` (from the URL)
    selects the correct tier; without it a Tier-1 reference region is used and disclosed. Egress is held
    separate like every egress line. ``skus`` injectable for offline tests; else fetched live via ADC."""
    if skus is None:
        token = token or _adc_token()
        if not token:
            return None
        skus = catalog_skus_authed(CLOUD_RUN_SERVICE_ID, token=token, project=project or _adc_project())
    if not skus:
        return None
    pricing_region = region or "us-central1"              # a Tier-1 reference region, disclosed below

    def _find(pred):
        # collect matches, then PREFER the SKU whose serviceRegions covers the deploy region (correct price
        # TIER), else a global SKU, else any -- so a Tier-1 deploy is not priced at the Tier-2 rate.
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
    comps: List[UsageComponent] = []
    if req is not None:
        comps.append(UsageComponent(name="requests", per_unit_usd=req, unit="per request", driver="requests",
                                    raw_unit_price=req, native_unit="per request"))
    if cpu is not None:
        comps.append(UsageComponent(name="compute-active-cpu", per_unit_usd=cpu, unit="vCPU-second",
                                    driver="other", raw_unit_price=cpu, native_unit="per vCPU-second"))
    if mem is not None:
        comps.append(UsageComponent(name="compute-active-mem", per_unit_usd=mem, unit="GiB-second",
                                    driver="other", raw_unit_price=mem, native_unit="per GiB-second"))
    if not any(c.driver == "requests" for c in comps):
        return None                                   # cannot price the load-bearing request line -> disclose
    reg_label = pricing_region + ("" if region else " (Tier-1 reference; no region in URL)")
    ur = compose_usage_rate(comps, provider="gcp", region=reg_label, service="Cloud Run",
                            capture_date=capture_date, standing_floor_hourly_usd=standing_floor_hourly,
                            price_source="GCP Cloud Billing Catalog (request-based Cloud Run SKUs, ADC)",
                            price_urls=(f"{_CATALOG}/{CLOUD_RUN_SERVICE_ID}/skus",))
    if extra_notes:
        ur = UsageRate(provider=ur.provider, region=ur.region, service=ur.service,
                       capture_date=ur.capture_date, components=ur.components, schedule=ur.schedule,
                       assumptions=tuple(ur.assumptions) + tuple(extra_notes), price_source=ur.price_source,
                       exclusions=ur.exclusions, price_urls=ur.price_urls, fx=ur.fx)
    return ur


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
            if ("PostgreSQL" not in d and "Postgres" not in d) or f"Zonal - {kind}" not in d:
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
    """(vcpus, ram_gb) from a db-custom Cloud SQL tier, or None (shared-core tiers are priced by their
    flat SKU instead, see _SHARED_TIER). ``db-custom-N-M`` -> (N vCPUs, M MB / 1024)."""
    m = re.match(r"db-custom-(\d+)-(\d+)$", tier or "")
    return (float(m.group(1)), float(m.group(2)) / 1024.0) if m else None


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


def cloud_sql_hourly(region: str, tier: str, storage_gb: float, *, token: Optional[str] = None,
                     project: Optional[str] = None, skus: Optional[List[dict]] = None):
    """Best-effort standing $/hr for a Cloud SQL PostgreSQL instance: db-custom vCPU x base-Zonal-vCPU +
    RAM x base-Zonal-RAM + storage x base-Zonal-Storage/730. Returns (hourly, exact_region) or None when
    the tier is shared/predefined or a rate is unavailable (the caller then DISCLOSES it as an unpriced
    resource, never omits it silently). Edition assumed Enterprise-Zonal (non-HA), disclosed by the caller."""
    if skus is None:
        token = token or _adc_token()
        if not token:
            return None
        skus = catalog_skus_authed(CLOUD_SQL_SERVICE_ID, token=token, project=project or _adc_project())
    if tier in _SHARED_TIER:                              # shared-core: one flat instance SKU + storage
        rate = _cloud_sql_shared_rate(skus, region, _SHARED_TIER[tier])
        if not rate:
            return None
        hourly = rate[0]
        sto = _cloud_sql_zonal_rate(skus, region, "Storage")
        if sto and storage_gb:
            hourly += float(storage_gb) * sto[0] / 730.0
        return hourly, rate[1]
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
    return hourly, (cpu[1] and ram[1])


def gcp_serverless_extras(run_cmd=None):
    """Enumerate the project's Cloud SQL instances and Cloud Run services so a multi-service serverless
    deploy is fully priced, not silently under-counted. Returns ([{region, tier, storage_gb}], n_run_services).
    ``run_cmd`` injectable for offline tests."""
    sqls = _gcloud_json(["sql", "instances", "list"], run_cmd) or []
    runs = _gcloud_json(["run", "services", "list"], run_cmd) or []
    dbs = []
    for i in sqls:
        s = i.get("settings", {}) or {}
        dbs.append({"region": i.get("region"), "tier": s.get("tier"),
                    "storage_gb": float(s.get("dataDiskSizeGb") or 0)})
    return dbs, (len(runs) if isinstance(runs, list) else 0)


def _gcloud_json(args: List[str], run_cmd=None):
    """Run a `gcloud ... --format=json` command host-side (off-clock, never in the VM), return parsed JSON
    or None. ``run_cmd`` injectable for offline tests."""
    if run_cmd is not None:
        return run_cmd(args)
    try:
        r = subprocess.run(["gcloud", *args, "--format=json"], capture_output=True, text=True, timeout=45)
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


class GcpRunRateAdapter(RunRateAdapter):
    def __init__(self, resolve_bundle: Optional[BundleResolver] = None, call_tool=None):
        # default: resolve a standing GCE deploy via `gcloud compute instances list` (serverless run.app URLs
        # never reach the resolver; they take the usage-schedule branch in run_rate). Injectable for tests.
        self._resolve = resolve_bundle or _gce_resolver()

    def run_rate(self, deployment_ref: object, *, capture_date: str):
        """Price the deployment. A serverless Cloud Run URL (``*.run.app``) is priced as a per-usage
        SCHEDULE from the request-based Cloud Run SKUs (no bundle resolution needed: the URL is the whole
        input); anything else is priced as a standing hourly run-rate from its resolved bundle (the VM
        path, which still needs the injected resolver + a Catalog key/ADC). Returns a UsageRate, a RunRate,
        or None (disclosed, never faked)."""
        if isinstance(deployment_ref, str) and is_cloud_run_url(deployment_ref):
            # ENUMERATE the deploy's other billable resources so a multi-service serverless deploy is fully
            # priced, not silently under-counted: fold any Cloud SQL as a standing floor on the schedule
            # (best-effort, edition disclosed), and disclose an instance whose tier cannot be priced rather
            # than drop it; note additional Cloud Run services.
            floor, notes = 0.0, []
            try:
                dbs, n_run = gcp_serverless_extras()
                for db in dbs:
                    priced = cloud_sql_hourly(str(db.get("region") or ""), str(db.get("tier") or ""),
                                              float(db.get("storage_gb") or 0))
                    if priced is not None:
                        floor += priced[0]
                        notes.append(f"folded a Cloud SQL instance ({db.get('tier')}) as a standing floor "
                                     f"${round(priced[0], 4)}/hr (Enterprise-Zonal edition assumed"
                                     f"{'' if priced[1] else ', region-approximate'})")
                    else:
                        notes.append(f"cost EXCLUDES an unpriced Cloud SQL instance (tier {db.get('tier')}): "
                                     "shared/predefined tier or rate unavailable, disclosed not faked")
                if n_run > 1:
                    notes.append(f"{n_run} Cloud Run services exist in the project; only the served URL's "
                                 "service is priced here (additional services not folded)")
            except Exception:  # noqa: BLE001 - enumeration is best-effort; a failure is disclosed
                notes.append("could not enumerate Cloud SQL / other services (cost is the served service only)")
            return cloud_run_usage_rate(capture_date, region=cloud_run_region_from_url(deployment_ref),
                                        standing_floor_hourly=floor, extra_notes=tuple(notes))
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
        return compose_run_rate(comps, provider="gcp", region=region,
                                flavor=str(b.get("machine_type", b.get("family", b.get("flavor", "")))),
                                capture_date=capture_date, egress=egress, price_source=src,
                                price_urls=(_CATALOG,))
