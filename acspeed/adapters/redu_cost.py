"""redu RunRateAdapter (C19 cost axis): resolve a deployment's provisioned bundle + its PUBLIC LIST
prices into a provider-independent RunRate. Thin per-provider boundary, like ReduCapabilityAdapter.

The pricing resolution is injectable (``resolve_bundle``) so the composition is tested offline; the
default resolver best-effort-fetches from the redu MCP (list_flavors for the per-hour rate) and prices
ONE line per provisioned billed resource: the app instance PLUS every managed datastore the deployment
stood up. The datastores are found by a UNIVERSAL, data-driven sweep (``_sweep_managed_stores`` over the
``_REDU_STORES`` table), not a per-type ``db_id``/``redis_id`` branch: it enumerates EVERY managed-store
list tool and attributes a row to the deployment by the deploy's private ``network_id`` (all its VMs share
it, so a store the deployment row never references - managed ClickHouse, a future type - is still caught)
or by a referenced id, then prices each by its flavor at the flat ``pricing_rules`` rate (the SAME source
Stripe bills from, surfaced through ``list_flavors.hourly_rate``), with HA members counted as that many
instance-hours. Adding a new managed type is ONE row in ``_REDU_STORES``, never a code branch. This is the
paper's per-resource cost (Part 4): a managed datastore is
priced as its OWN instance-hours, never folded into the app VM, so a two-VM deploy is not under-counted
(the docmost app + managed Postgres case). A resource it cannot price is disclosed
(``unpriced_resources`` + a note in ``price_source``) rather than faked as zero; the resolver returns
None only when the load-bearing app compute itself cannot be priced, exactly like the capability pass.
Only PUBLIC ON-DEMAND LIST prices are used; discounts/spot/reserved are excluded by C19.

CURRENCY: redu (a UK Ltd) lists in GBP -- the MCP list_flavors returns the per-hour price under
``hourly_rate`` IN GBP, and a redu flavor's price BUNDLES its disk (no separate storage line). USD is the
single reporting currency, so the adapter converts the native GBP figure to USD ONCE at a dated PUBLIC FX
rate (``_dated_to_usd``: ECB via frankfurter.app, or an env override), and discloses the native figure,
the rate, its date and its source on the RunRate (``FxConversion``). The FX conversion lives in the adapter
(where ``capture_date`` is), never in cost.py, which stays USD-only.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable, NamedTuple, Optional, Tuple

from ..cost import (RateComponent, RunRate, RunRateAdapter, EgressRate, FxConversion,
                    compose_run_rate, storage_gb_month_to_hourly)
from . import redu_mcp_http

# a resolver returns a priced-bundle dict (see _default_resolver's shape) or None if it cannot price it
BundleResolver = Callable[[object], Optional[dict]]
# an FX resolver returns (native->USD rate, the date it applies to, its public source) or None
FxResolver = Callable[[str, str], Optional[Tuple[float, str, str]]]


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _price_flavor(flavors, flavor_ref) -> Tuple[Optional[float], str, Optional[str]]:
    """(native per-hour price, flavor name, native currency) for a flavor id-or-name, or (None, name,
    None) if it is absent or unpriced. redu's list_flavors prices under ``hourly_rate`` (GBP)."""
    fl = next((f for f in flavors
               if str(flavor_ref) in (str(f.get("id")), str(f.get("name")))), None)
    name = str(_first(fl or {}, "name", "id", default=flavor_ref))
    if not fl:
        return None, name, None
    native = _first(fl, "hourly_rate", "price_per_hour", "usd_per_hour", "rate_hourly")
    if native is None:
        return None, name, None
    return float(native), name, str(_first(fl, "currency", default="GBP"))


def _price_store_row(row: dict, flavors: list, role: str) -> Optional[dict]:
    """Price ONE managed-datastore row the sweep already holds: read its flavor (``flavor_id`` / ``flavor``
    / ``flavor_name``), price it at the flat ``list_flavors`` hourly_rate (the ``pricing_rules`` rate, the
    SAME source Stripe bills from), and count HA members as separate instance-hours (an ``ha`` cluster is
    ``len(member_ips)`` members, or 3 when the row does not enumerate them). Returns
    {role, flavor, native_price, count}, or None when the row carries no flavor or its flavor is not in
    ``list_flavors`` (the caller then DISCLOSES it by type+id, never fakes a zero)."""
    native, fname, _ = _price_flavor(flavors, _first(row, "flavor_id", "flavor", "flavor_name"))
    if native is None:
        return None
    members = row.get("member_ips")
    count = (len(members) if row.get("ha") and isinstance(members, list) and members
             else (3 if row.get("ha") else 1))
    return {"role": role, "flavor": fname, "native_price": native, "count": count}


class _StoreKind(NamedTuple):
    """One redu billable managed-store primitive, as DATA. ``list_tool``/``key`` are the exact MCP list
    tool + its structuredKey; ``role`` is the compute:<role> component suffix and the disclosure type;
    ``ref_field`` is the deployment field that points at this store's id (the fallback attribution handle
    for a store whose list row does not expose network_id), or None for a store the deployment never
    references (ClickHouse), which is attributed only by shared network_id."""
    list_tool: str
    key: str
    role: str
    ref_field: Optional[str]


# THE DATA TABLE. Adding a future managed type (managed Qdrant, a new list_* tool, anything) is ONE row
# here, never a code branch - the redu analogue of the AWS adapter's _DIMENSIONS dispatch table. Every
# name/key was confirmed against redu-mcp buildServer.js (the registerAuthedGetTool definitions) and the
# backend list controllers: list_databases->'databases' (Postgres, carries flavor_id + ha/member_ips),
# list_relational_databases->'relational_databases' (MySQL/MariaDB), list_clickhouse_databases->
# 'clickhouse' (carries flavor_id + engine + ha/member_ips; NOT referenced by the deployment row, so it
# rides the network_id path), list_redis->'redis', list_media_spaces->'media_spaces' (carries flavor_id +
# network_id + size_gb; its VM flavor is priced, else the row is disclosed).
_REDU_STORES = (
    _StoreKind("list_databases",            "databases",            "postgres",    "db_id"),
    _StoreKind("list_relational_databases", "relational_databases", "mysql",       "db_id"),
    _StoreKind("list_clickhouse_databases", "clickhouse",           "clickhouse",  None),
    _StoreKind("list_redis",                "redis",                "redis",       "redis_id"),
    _StoreKind("list_media_spaces",         "media_spaces",         "media_space", "media_space_id"),
)

# How a DANGLING deployment reference (a db_id/redis_id/media_space_id that no enumerated row satisfied) is
# disclosed. db_id is engine-agnostic on the deployment row, so it discloses as the neutral "database".
_REF_LABELS = {"db_id": "database", "redis_id": "redis", "media_space_id": "media_space"}


def _sweep_managed_stores(call_tool, dep: dict, flavors: list) -> Tuple[list, list]:
    """Universal, data-driven enumeration of every managed datastore the deployment provisioned, with NO
    per-type code branch. For each primitive in ``_REDU_STORES`` it lists the resource kind and attributes
    a row to THIS deployment when the row shares the deployment's private ``network_id`` (PREFERRED: every
    VM the deploy stood up sits on that one network, so a store the deployment row never references -
    ClickHouse, a future type - is still caught) OR when the deployment references the row's id through the
    primitive's ``ref_field`` (the fallback handle for stores whose list row does not carry network_id).
    Each attributed row is priced by its flavor; a row that is found but cannot be priced is disclosed by
    type+id, and a referenced id that no row satisfied is disclosed by its label - never a silent omission.
    Returns (priced_resources, unpriced_labels)."""
    dep_net = _first(dep, "network_id")
    priced: list = []
    unpriced: list = []
    seen: set = set()                          # (list_tool, id): price a store once even if net AND ref match
    resolved_refs: set = set()                 # ref_fields the sweep actually satisfied (for dangling detect)
    for kind in _REDU_STORES:
        ref_id = _first(dep, kind.ref_field) if kind.ref_field else None
        rows = (call_tool(kind.list_tool, {}) or {}).get(kind.key, []) or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = row.get("id")
            row_net = _first(row, "network_id")
            by_net = dep_net is not None and row_net is not None and str(row_net) == str(dep_net)
            # a ref match only counts until that ref is resolved once, so an id that happens to collide
            # across the two db_id tables (databases vs relational_databases) is not billed twice.
            by_ref = (ref_id is not None and rid is not None and str(rid) == str(ref_id)
                      and kind.ref_field not in resolved_refs)
            if not (by_net or by_ref):
                continue
            marker = (kind.list_tool, str(rid))
            if marker in seen:
                continue
            seen.add(marker)
            if by_ref and kind.ref_field:
                resolved_refs.add(kind.ref_field)
            res = _price_store_row(row, flavors, kind.role)
            if res:
                priced.append(res)
            else:
                unpriced.append(f"{kind.role}:{rid}")      # found but unpriceable -> disclosed by type+id
    # a deployment reference that no enumerated row satisfied is a DANGLING pointer -> disclose it by label
    # (a managed store deleted out from under the deployment, or one the current API cannot enumerate yet).
    for ref_field, label in _REF_LABELS.items():
        if _first(dep, ref_field) and ref_field not in resolved_refs and label not in unpriced:
            unpriced.append(label)
    return priced, unpriced


# --- dated public FX (USD is the reporting currency; non-USD list prices are converted once) ----

def _fetch_frankfurter(native_currency: str, date: str) -> Optional[Tuple[float, str, str]]:
    """Dated GBP->USD (or any->USD) from ECB reference rates via Frankfurter. Returns
    (rate, rate_date, source) or None. Frankfurter returns the nearest published rate on/before the
    requested date and reports the actual date it used, which we disclose as ``rate_date``. Endpoint is
    api.frankfurter.dev/v1 (the old api.frankfurter.app 301-redirects here); an explicit User-Agent is
    set because the default Python-urllib UA can be blocked upstream."""
    url = f"https://api.frankfurter.dev/v1/{date}?base={native_currency}&symbols=USD"
    req = urllib.request.Request(url, headers={"User-Agent": "acspeed-cost/1.0",
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:     # off-clock, host-side; never in the VM
            d = json.load(r)
    except Exception:  # noqa: BLE001 - FX is best-effort; a failure is disclosed, never faked
        return None
    rate = (d.get("rates") or {}).get("USD")
    if rate is None:
        return None
    return (float(rate), str(d.get("date", date)), f"ECB via frankfurter.dev ({url})")


def _dated_to_usd(native_currency: str, date: str,
                  fetch: Callable[[str, str], Optional[Tuple[float, str, str]]] = _fetch_frankfurter
                  ) -> Optional[Tuple[float, str, str]]:
    """1 unit of ``native_currency`` in USD on ``date``, as a dated PUBLIC rate. An env override
    ``ACSPEED_FX_<CCY>_USD`` pins the rate for offline/fully-reproducible runs. Returns None if no rate
    can be obtained (the bundle is then disclosed as unpriceable, never faked)."""
    if native_currency == "USD":
        return (1.0, date, "identity")
    override = os.environ.get(f"ACSPEED_FX_{native_currency}_USD")
    if override:
        try:
            return (float(override), date, f"env:ACSPEED_FX_{native_currency}_USD")
        except ValueError:
            pass
    return fetch(native_currency, date)


def _default_resolver(call_tool=redu_mcp_http.call_tool) -> BundleResolver:
    """Best-effort redu resolver: find the deployment, its flavor's public list hourly rate (GBP), its
    volume GB, and whether it holds a public IP. Returns None if it cannot price the compute (the
    load-bearing term). Field names are parsed defensively. redu's list_flavors prices under
    ``hourly_rate`` (GBP) and BUNDLES the flavor's disk, so there is normally no separate storage line."""
    def resolve(deployment_ref: object) -> Optional[dict]:
        deps = (call_tool("list_deployments", {}) or {}).get("deployments", [])
        dep = next((d for d in deps
                    if str(_first(d, "id", "instance_id", "name")) == str(deployment_ref)), None)
        # get_deployment (by numeric id) carries the REAL provisioned flavor; use it when the list row is
        # missing or does not name the flavor (the user may have up-sized after deploy).
        if dep is None or not _first(dep, "flavor", "flavor_name", "flavor_id"):
            det = {}
            try:
                det = call_tool("get_deployment", {"id": int(deployment_ref)}) or {}
            except (ValueError, TypeError):
                det = {}
            det = (det.get("deployment") if isinstance(det, dict) and isinstance(det.get("deployment"), dict)
                   else det if isinstance(det, dict) else {})
            if det:
                dep = {**(dep or {}), **det}
        if not dep:
            return None
        flavor = _first(dep, "flavor", "flavor_name", "flavor_id", default="")
        region = _first(dep, "region", default="")
        flavors = (call_tool("list_flavors", {}) or {}).get("flavors", [])
        app_native, app_name, native_ccy = _price_flavor(flavors, flavor)
        if app_native is None:
            return None                                   # cannot price app compute -> disclose, do not fake
        native_ccy = native_ccy or "GBP"                  # redu lists in GBP

        # one priced line per provisioned billed resource: the app instance PLUS every managed datastore
        # the deployment stood up. The managed stores come from a UNIVERSAL, data-driven sweep
        # (_sweep_managed_stores over _REDU_STORES) that REPLACES the old db_id/redis_id/media_space_id
        # branches: it enumerates EVERY managed-store list tool, attributes rows by the deploy's private
        # network_id (so a store the deployment row never references - ClickHouse, a future type - is still
        # caught) or by a referenced id, prices each by its flavor, counts HA members, and discloses
        # anything unpriceable. Each managed store is its OWN instance-hours (Part 4 per-resource cost);
        # a new managed type is ONE row in _REDU_STORES, no code here.
        resources = [{"role": "app", "flavor": app_name, "native_price": app_native, "count": 1}]
        managed, unpriced = _sweep_managed_stores(call_tool, dep, flavors)
        resources.extend(managed)

        app_fl = next((f for f in flavors
                       if str(flavor) in (str(f.get("id")), str(f.get("name")))), {}) or {}
        gb = float(_first(dep, "volume_gb", "storage_gb", "disk_gb", default=0) or 0)
        storage_per_gb_month = _first(app_fl, "storage_per_gb_month", "storage_usd_per_gb_month")
        ip_native = (_first(dep, "public_ip_hourly", "public_ip_hourly_usd")
                     if _first(dep, "public_ip", "floating_ip") else None)
        price_source = "redu public on-demand list"
        if unpriced:
            price_source += " (excludes unpriced: " + ", ".join(unpriced) + ")"
        return {
            "flavor": str(app_name), "region": str(region),
            "native_currency": native_ccy,
            "resources": resources,                       # per-resource compute lines, HA members counted
            "storage_gb": gb,
            "storage_native_per_gb_month": (None if storage_per_gb_month is None else float(storage_per_gb_month)),
            "public_ip_native_hourly": (None if ip_native is None else float(ip_native)),
            "ancillary": [],                              # {name, hourly_usd, raw_unit_price, native_unit}
            "egress": None,                               # {per_gb_usd, tier}
            "unpriced_resources": unpriced,               # disclosure, never a faked zero
            "price_source": price_source,
            "price_urls": [],
        }
    return resolve


class ReduRunRateAdapter(RunRateAdapter):
    def __init__(self, resolve_bundle: Optional[BundleResolver] = None, call_tool=None,
                 fx: Optional[FxResolver] = None):
        self._resolve = resolve_bundle or _default_resolver(call_tool or redu_mcp_http.call_tool)
        self._fx = fx or _dated_to_usd

    def run_rate(self, deployment_ref: object, *, capture_date: str) -> Optional[RunRate]:
        """Compose the deployment's standing hourly run-rate from its priced bundle. ``capture_date`` is
        passed in (the harness stamps the test day) so the module never reads the clock; it also dates the
        FX conversion. A bundle already denominated in USD (``compute_hourly_usd``, no ``native_currency``)
        is composed as-is; a native-currency bundle is converted once at the dated FX rate and disclosed."""
        b = self._resolve(deployment_ref)
        if not b:
            return None

        native_ccy = str(b.get("native_currency", "USD"))
        if native_ccy == "USD":
            rate, fxobj = 1.0, None
        else:
            conv = self._fx(native_ccy, capture_date)
            if conv is None:
                return None                               # no dated rate -> unpriceable, disclosed not faked
            rate, rate_date, source = conv
            fxobj = FxConversion(native_currency=native_ccy, reporting_currency="USD",
                                 rate=rate, rate_date=rate_date, source=source)
        storage_native = (b.get("storage_usd_per_gb_month") if native_ccy == "USD"
                          else b.get("storage_native_per_gb_month"))
        ip_native = b.get("public_ip_hourly_usd") if native_ccy == "USD" else b.get("public_ip_native_hourly")

        # every line: hourly_usd = (native $/hr) x FX; raw_unit_price discloses the native figure.
        # One compute line PER provisioned resource when the resolver enumerated them (app + managed
        # datastores, HA members counted); the single-line ``compute_*`` shape is kept for injected
        # bundles and any resolver that prices only the app VM.
        comps = []
        resources = b.get("resources")
        if resources:
            for r in resources:
                cnt = int(r.get("count", 1) or 1)
                name = "compute" if r.get("role") == "app" else "compute:" + str(r.get("role"))
                comps.append(RateComponent(
                    name=name, hourly_usd=float(r["native_price"]) * cnt * rate,
                    raw_unit_price=float(r["native_price"]), native_unit="instance-hour",
                    quantity=float(cnt)))
        else:
            compute_native = b["compute_hourly_usd"] if native_ccy == "USD" else b["compute_native_price"]
            comps.append(RateComponent(name="compute", hourly_usd=float(compute_native) * rate,
                                       raw_unit_price=float(compute_native), native_unit="instance-hour"))
        if storage_native is not None and b.get("storage_gb"):
            comps.append(RateComponent(
                name="storage",
                hourly_usd=storage_gb_month_to_hourly(storage_native, b["storage_gb"]) * rate,
                raw_unit_price=storage_native, native_unit="GB-month", quantity=b["storage_gb"]))
        if ip_native is not None:
            comps.append(RateComponent(name="public_ip", hourly_usd=ip_native * rate,
                                       raw_unit_price=ip_native, native_unit="hour"))
        for a in b.get("ancillary", []):
            comps.append(RateComponent(name=a["name"], hourly_usd=a["hourly_usd"],
                                       raw_unit_price=a.get("raw_unit_price", a["hourly_usd"]),
                                       native_unit=a.get("native_unit", "hour")))

        eg = b.get("egress")
        egress = EgressRate(per_gb_usd=eg["per_gb_usd"], tier=eg.get("tier", "unspecified")) if eg else None
        return compose_run_rate(comps, provider="redu", region=b.get("region", ""), flavor=b["flavor"],
                                capture_date=capture_date, egress=egress,
                                price_source=b.get("price_source", "redu public on-demand list"),
                                price_urls=tuple(b.get("price_urls", ())), fx=fxobj)
