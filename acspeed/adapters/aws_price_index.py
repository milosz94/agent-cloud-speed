"""The published AWS price list as a searchable index: every billable thing HAS a price, so find it.

AWS is required to publish the price of everything it bills, and it does, at
``https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/index.json``: 269 services, every SKU, every
region, no credentials, dated daily. So "unpriceable" is never a property of a resource. It is only ever
a failure of the lookup, and treating it as a terminal state encodes our own blind spot as a law of
nature. This module exists so that stops being an option.

WHY NOT THE LIVE Price List QUERY API (which the older adapter uses): ``get-products`` requires the
SERVICE CODE up front, so it can only answer "what does THIS service charge". That forces an
eventSource -> serviceCode map, and no derivable rule exists for it: naive string matching resolves 14
of 22 observed services and misses ``elasticloadbalancing`` -> ``AWSELB`` (the largest line on the
bill), while normalized service-name matching returns confidently WRONG answers (``ec2`` ->
AmazonEC2OCPULicenseFees, ``s3`` -> AmazonS3GlacierDeepArchive, ``ssm`` -> AWSIAMAccessAnalyzer).

The bulk index removes the question. You do not ask a service what it charges; you search the whole
disclosure for the SKU whose ATTRIBUTES match the resource that was created. A service nobody
anticipated is in the index by law, so it is findable without any per-service code.

Matching rule, measured 2026-09-05:
  * Only STRING values select a SKU. Numbers are QUANTITIES: the value ``1`` matches 13 different EC2
    attributes and ``8`` matches 9, and RDS ``20`` matches ``engineCode``, so an ``allocatedStorage`` of
    20 GB would otherwise select a database ENGINE.
  * String selectors resolve UNIQUELY within a service: ``t3.medium`` -> instanceType, ``gp3`` ->
    volumeApiName (EC2) / volumeName (RDS), ``Linux`` -> operatingSystem, ``db.t4g.micro`` ->
    instanceType, ``nano`` -> power. Zero collisions across 83 EC2 and 48 RDS attributes.
  * Matching is case-insensitive: CloudTrail sends ``nano``, the SKU holds ``Nano``; CloudTrail sends
    ``postgres``, the SKU holds ``PostgreSQL``. Normalizing both sides removes the value-spelling table.

Every SKU carries a ``usagetype`` (e.g. ``USE1-DatabaseUsage:1GB``), which is the SAME string the bill
reports. That is the join that lets a price be checked against what AWS actually charged.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Dict, List, Optional, Tuple

_BASE = "https://pricing.us-east-1.amazonaws.com"
_INDEX = f"{_BASE}/offers/v1.0/aws/index.json"
_CACHE = os.path.expanduser("~/.cache/acspeed-pricing")


def _norm(v) -> str:
    """Compare values the way AWS spells them inconsistently: case and punctuation folded.
    ``postgres`` vs ``PostgreSQL`` and ``nano`` vs ``Nano`` are the two measured cases."""
    return re.sub(r"[^a-z0-9]", "", str(v).lower())


def _fetch(path: str, timeout: int = 300) -> dict:
    os.makedirs(_CACHE, exist_ok=True)
    key = os.path.join(_CACHE, re.sub(r"[^A-Za-z0-9]", "_", path)[-180:] + ".json")
    if os.path.exists(key) and os.path.getsize(key) > 0:
        with open(key) as fh:
            return json.load(fh)
    url = path if path.startswith("http") else _BASE + path
    with urllib.request.urlopen(url, timeout=timeout) as r:      # noqa: S310 - fixed AWS host
        data = json.loads(r.read().decode())
    with open(key, "w") as fh:
        json.dump(data, fh)
    return data


def service_codes() -> List[str]:
    """Every service AWS publishes a price list for. This is the disclosure; it is complete by law."""
    return sorted(_fetch("/offers/v1.0/aws/index.json").get("offers", {}))


def region_offer(service_code: str, region: str) -> dict:
    """One service's SKUs for ONE region. Region-scoped so the whole disclosure stays tractable."""
    idx = _fetch(f"/offers/v1.0/aws/{service_code}/current/region_index.json")
    ent = (idx.get("regions") or {}).get(region)
    if not ent:
        return {}
    return _fetch(ent["currentVersionUrl"])


def find_sku(service_code: str, region: str, selectors: Dict[str, object],
             require: Optional[Dict[str, str]] = None) -> List[dict]:
    """SKUs in ``service_code``/``region`` whose attribute VALUES contain every string selector.

    ``selectors`` is the resource as the create call described it; non-string values are ignored because
    numbers select nothing (they are quantities). ``require`` pins extra attributes by name when the
    caller genuinely knows one (e.g. productFamily), and is never needed to make a match happen.
    Returns every match with its price dimensions, so an AMBIGUOUS result is visible to the caller
    rather than silently resolved to the first row."""
    offer = region_offer(service_code, region)
    if not offer:
        return []
    wanted = {_norm(v) for v in selectors.values() if isinstance(v, str) and len(str(v)) > 1}
    if not wanted:
        return []
    terms = (offer.get("terms") or {}).get("OnDemand", {})
    out = []
    for sku, prod in (offer.get("products") or {}).items():
        attrs = prod.get("attributes") or {}
        vals = {_norm(v) for v in attrs.values()}
        if not wanted <= vals:
            continue
        if require and any(_norm(attrs.get(k)) != _norm(v) for k, v in require.items()):
            continue
        dims = []
        for term in terms.get(sku, {}).values():
            for dim in (term.get("priceDimensions") or {}).values():
                dims.append({"unit": dim.get("unit"),
                             "usd": float(dim.get("pricePerUnit", {}).get("USD", 0) or 0),
                             "description": dim.get("description", "")})
        out.append({"sku": sku, "attributes": attrs, "usagetype": attrs.get("usagetype"),
                    "operation": attrs.get("operation"), "prices": dims})
    return out


def search_all(region: str, selectors: Dict[str, object],
               services: Optional[List[str]] = None) -> List[Tuple[str, dict]]:
    """The universal lookup: find the SKU ANYWHERE in the disclosure, with no service known in advance.

    This is what removes the eventSource -> serviceCode table. ``services`` narrows the search purely as
    an optimisation; omitting it searches everything AWS publishes."""
    hits = []
    for code in (services or service_codes()):
        try:
            for m in find_sku(code, region, selectors):
                hits.append((code, m))
        except Exception:  # noqa: BLE001 - one unreadable offer file must not hide the rest
            continue
    return hits


# --- C19's own definition, expressed universally --------------------------------------------------
#
# The paper's cost axis is the PUBLIC ON-DEMAND LIST price. That is not a per-service opinion, it is the
# definition, and it is what disambiguates a resource down to one SKU. Measured on EC2 us-east-1:
# instanceType=t3.medium has SIXTY SKUs, and the naive "first match" picked UnusedBox:t3.medium at
# $43.80/mo when the on-demand line is BoxUsage:t3.medium at $30.37/mo. Getting a WRONG price is worse
# than getting none.
#
# The non-standard purchase options announce themselves in the USAGETYPE, which every service has, so the
# filter needs no per-service attribute names: reserved capacity, unused capacity, dedicated tenancy and
# dedicated hosts are all excluded by their usagetype qualifier. What remains is the on-demand line.
_NON_ONDEMAND = ("unused", "reservation", "reserved", "dedicated", "hostbox", "hostusage",
                 "spot", "ded:", "res:", "commit")

# Attribute-level pins, applied ONLY when the service publishes that attribute. These are purchase-option
# and platform choices (C19 excludes reservations and committed use), never per-service pricing code.
_ONDEMAND_PINS = {
    "capacitystatus": "Used",
    "tenancy": "Shared",
    "licenseModel": "No License required",
    "preInstalledSw": "NA",
    "deploymentOption": "Single-AZ",
}


def ondemand_only(matches: List[dict], platform: str = "Linux",
                  high_availability: bool = False) -> List[dict]:
    """Reduce candidate SKUs to the public ON-DEMAND list line, per C19.

    ``platform`` pins the operating system where the service prices by it (EC2 sells the same instance
    at different rates for Linux / Windows / RHEL, and the usagetype does NOT distinguish them). Linux is
    the disclosed default because that is what the benchmark's agents deploy."""
    out = []
    for m in matches:
        ut = str(m.get("usagetype") or "").lower()
        if any(bad in ut for bad in _NON_ONDEMAND):
            continue
        a = m.get("attributes") or {}
        # Compare pins CASE-FOLDED. AWS is not internally consistent about its own spelling: RDS writes
        # licenseModel "No license required" while EC2 writes "No License required", and an exact compare
        # silently discarded every RDS on-demand SKU.
        if any(k in a and _norm(a[k]) != _norm(v) for k, v in _ONDEMAND_PINS.items()):
            continue
        if "operatingSystem" in a and _norm(a["operatingSystem"]) != _norm(platform):
            continue
        # High availability is a CHOICE the deploy makes, not the default. Where the service prices HA
        # separately, take the non-HA line unless the caller says otherwise, and disclose it.
        if not high_availability and _norm(a.get("highAvailability", "No")) != _norm("No"):
            continue
        if not any(d.get("usd", 0) > 0 for d in m.get("prices") or []):
            continue
        out.append(m)
    return out
