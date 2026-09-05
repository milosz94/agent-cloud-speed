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
import sys
import tempfile
import urllib.request
from typing import Dict, List, Optional, Set, Tuple

_BASE = "https://pricing.us-east-1.amazonaws.com"
_INDEX = f"{_BASE}/offers/v1.0/aws/index.json"
def _cache_dir() -> str:
    """Where the disclosure is cached. A TEST NEVER WRITES WHERE A REAL RUN READS.

    Measured 2026-09-06: the suite patches `region_offer` with a four-SKU fixture, a summary derived
    from it was written to the shared cache under the REAL service's name, and the next production pass
    read that fixture back as Amazon's published price list. Lightsail's value universe came out as 8
    values instead of 342 and two live container services silently stopped pricing. Running the tests
    broke production.

    The separation is decided HERE rather than in the test package, because `unittest discover -s tests`
    imports test modules top-level and never executes `tests/__init__.py`: a guard the test harness has
    to opt into is a guard that depends on how someone typed the command."""
    env = os.environ.get("ACSPEED_PRICING_CACHE")
    if env:
        return env
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return os.path.join(tempfile.gettempdir(), "acspeed-pricing-under-test")
    return os.path.expanduser("~/.cache/acspeed-pricing")


_CACHE = _cache_dir()


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


_DIGEST_MEM: Dict[str, dict] = {}


def _digest(service_code: str, region: str) -> dict:
    """A few-kB SUMMARY of one service's price list, so a service can be RULED OUT without parsing it.

    Measured 2026-09-06: pricing one run's resources took EIGHT MINUTES of CPU, all of it re-parsing the
    disclosure. The reason is structural rather than incidental. Since the billable-create list was
    deleted, every create is a candidate and a FREE resource (a key pair, a security group, an IAM role)
    can only be shown to be free by asking every service that publishes a price, so the whole-disclosure
    sweep is the COMMON path, not the rare one. The sweep is right and must stay; what was wrong is
    paying 394 MB of JSON for each of its answers.

    The digest holds the three things the sweep actually asks of a service: which values it uses (the
    exact tier), the text its usagetypes and operations are made of (the containment tier), and the units
    it writes numbers in (turning ``ramSizeInGb: 1.0`` into the ``1GB`` a SKU is keyed on). It is derived
    from the disclosure itself and keyed on the exact published version URL, so it cannot drift from the
    prices it summarises the way a hand-maintained list would."""
    memk = f"{service_code}/{region}"
    if memk in _DIGEST_MEM:
        return _DIGEST_MEM[memk]
    os.makedirs(_CACHE, exist_ok=True)
    key = os.path.join(_CACHE, "digest_" + re.sub(r"[^A-Za-z0-9]", "_", memk) + ".json")
    if os.path.exists(key) and os.path.getsize(key) > 0:
        with open(key) as fh:
            raw = json.load(fh)
    else:
        values, hays, units = set(), set(), set()
        for prod in (region_offer(service_code, region).get("products") or {}).values():
            attrs = prod.get("attributes") or {}
            for v in attrs.values():
                n = _norm(v)
                if n:
                    values.add(n)
                # "1GB" / "40 GB" / "0.25vCPU": the unit is how this service spells a number, and the
                # spelling is what a lookup elsewhere has to match. Harvested, never assumed.
                m = re.fullmatch(r"\s*[0-9]+(?:\.[0-9]+)?\s*([A-Za-z]{1,6})\s*", str(v))
                if m:
                    units.add(m.group(1).lower())
            hays.add(_norm(str(attrs.get("usagetype", "")) + str(attrs.get("operation", "")) +
                           str(attrs.get("productFamily", "")) + str(attrs.get("group", ""))))
        raw = {"values": sorted(values), "hay": "|".join(sorted(hays)), "units": sorted(units)}
        with open(key, "w") as fh:
            json.dump(raw, fh)
    got = {"values": set(raw["values"]), "hay": raw["hay"], "units": set(raw["units"])}
    _DIGEST_MEM[memk] = got
    return got


def value_universe(service_code: str, region: str) -> Set[str]:
    """Every normalised attribute value this service publishes here. A value outside it is the VENDOR's
    vocabulary, not the disclosure's, and has to be resolved before it can select anything."""
    return _digest(service_code, region)["values"]


def unit_vocabulary(service_code: str, region: str) -> Set[str]:
    """The units this service writes numbers in ("gb", "tb", "vcpu"), harvested from its own SKUs."""
    return _digest(service_code, region)["units"]


def find_sku(service_code: str, region: str, selectors: Dict[str, object],
             require: Optional[Dict[str, str]] = None) -> List[dict]:
    """SKUs in ``service_code``/``region`` whose attribute VALUES contain every string selector.

    ``selectors`` is the resource as the create call described it; non-string values are ignored because
    numbers select nothing (they are quantities). ``require`` pins extra attributes by name when the
    caller genuinely knows one (e.g. productFamily), and is never needed to make a match happen.
    Returns every match with its price dimensions, so an AMBIGUOUS result is visible to the caller
    rather than silently resolved to the first row."""
    wanted = {_norm(v) for v in selectors.values() if isinstance(v, str) and len(str(v)) > 1}
    if not wanted:
        return []
    # A value that appears in NO SKU of this service is not a selector FOR this service, so requiring it
    # makes every match fail. Vendors name resources in their own vocabulary: Lightsail creates a database
    # with ``relationalDatabaseBundleId: micro_2_0`` and ``relationalDatabaseBlueprintId: postgres_16``,
    # and NEITHER string exists anywhere in the published Lightsail price list, which is keyed on
    # memory/storage. Dropping the unmatchable terms and requiring the rest is what lets the resource's
    # own words find its SKU without a per-vendor translation table.
    #
    # Asked of the DIGEST, not the offer file, so a service that cannot match is ruled out for a few kB
    # instead of a full parse. Most services can never match most resources, so this is the usual answer.
    wanted = {w for w in wanted if w in value_universe(service_code, region)}
    if not wanted:
        return []
    offer = region_offer(service_code, region)
    if not offer:
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
    # C19 prices the public AWS REGION. Outposts, Local Zones and Wavelength are different products sold
    # at different rates, and their SKUs sit in the same offer file: an ALB search returned
    # Outposts-LoadBalancerUsage and TS-LoadBalancerUsage beside the real LoadBalancerUsage.
    "locationType": "AWS Region",
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


def selectivity(service_code: str, region: str, values: List[str]) -> Dict[str, int]:
    """How many SKUs each candidate value appears in. Fewer = more identifying.

    A create call describes a resource in words of wildly different information content. Measured on
    aws-medium-b run16: an ALB's create carries ``type=application``, ``scheme=internet-facing`` and
    ``ipAddressType=ipv4``, and requiring all three still left THIRTEEN on-demand SKUs, so the resource
    came out UNPRICED as ambiguous. ``operation=LoadBalancing:Application`` identifies it alone.

    Ranking by selectivity is the service-independent way to tell an identifying term from a generic one:
    no list of which attributes matter per service, just how rare each value is in that service's own
    published SKUs."""
    counts = {v: 0 for v in values}
    uni = value_universe(service_code, region)
    if not any(_norm(v) in uni for v in values):
        return counts                     # nothing to count; do not parse the offer to learn that
    for prod in (region_offer(service_code, region).get("products") or {}).values():
        vals = {_norm(x) for x in (prod.get("attributes") or {}).values()}
        for v in values:
            if _norm(v) in vals:
                counts[v] += 1
    return counts


def resolve_one(matches: List[dict], words: List[str]) -> List[dict]:
    """Break a tie between on-demand SKUs using the resource's own words as SUBSTRINGS.

    A resource and its SKU describe the same thing in different grammar: an ALB's create call says
    ``type: application`` while the SKU says ``operation: LoadBalancing:Application``. Exact value
    matching cannot bridge that, which is why an ALB came out UNPRICED as ambiguous across 13 SKUs, and
    substring matching cannot be the PRIMARY filter because a word like "application" appears all over a
    price list. Used only to choose among SKUs that already survived every exact filter, containment is
    both safe and sufficient: the winner must be strictly better than every rival, so an unresolvable tie
    stays a tie rather than silently picking one."""
    if len(matches) <= 1:
        return matches
    ws = [_norm(w) for w in words if isinstance(w, str) and len(w) > 2]
    if not ws:
        return matches
    scored = []
    for m in matches:
        a = m.get("attributes") or {}
        hay = _norm(str(a.get("usagetype", "")) + str(a.get("operation", "")) +
                    str(a.get("productFamily", "")) + str(a.get("group", "")))
        scored.append((sum(1 for w in ws if w in hay), m))
    best = max(s for s, _m in scored)
    if best == 0:
        return matches
    winners = [m for s, m in scored if s == best]
    return _prefer_base_usagetype(winners)


def _prefer_base_usagetype(matches: List[dict]) -> List[dict]:
    """Drop QUALIFIED variants when the base line is also a candidate.

    AWS qualifies an add-on by prefixing the base usagetype: an ALB search returns both
    ``LoadBalancerUsage`` ($16.43/mo, the load balancer) and ``TS-LoadBalancerUsage`` ($3.65/mo, its
    trust store). The qualified one ENDS WITH the base one, which is the shape, not a list of prefixes to
    know. Picking wrongly here is a 4.5x error, and picking the cheaper would bias every number down."""
    uts = {str(m.get("usagetype") or "") for m in matches}
    out = []
    for m in matches:
        ut = str(m.get("usagetype") or "")
        if any(other != ut and ut.endswith(other) for other in uts):
            continue                       # a qualified variant of another candidate: not the base line
        out.append(m)
    return out or matches


def find_sku_by_coverage(service_code: str, region: str, values: List[str],
                         anchor: str = "") -> List[dict]:
    """The SKUs carrying the MOST of the resource's own words, in one pass over the offer.

    Two ways of using the same words were measured to be wrong, both on the same Lightsail database.
    Requiring ALL of them fails as soon as one word belongs to another SKU's vocabulary. Adding them
    rarest-first and stopping when exactly one SKU remains picks the RAREST word, which is not the same
    as the RIGHT one: the bundle carries ``transferPerMonthInGb: 100``, "100GB" appears in exactly one
    Lightsail SKU -- a storage bundle -- so the database was priced at $3.00/mo as storage, while its own
    SKU (memory 1GB, storage 40GB) carried TWO of the words and was thrown away.

    Coverage is the evidence: the SKU that accounts for most of what the create call said. The noun of
    the event breaks the remaining tie but never filters, because a service is free to name a SKU
    something other than its own resource type (``ContainerSvcUsage`` for a ContainerService), and a
    filter on the noun would silently drop those."""
    uni = value_universe(service_code, region)
    wanted = {_norm(v) for v in values if isinstance(v, str) and len(str(v)) > 1}
    wanted = {w for w in wanted if w in uni}
    if not wanted:
        return []
    offer = region_offer(service_code, region)
    terms = (offer.get("terms") or {}).get("OnDemand", {})
    out = []
    for sku, prod in (offer.get("products") or {}).items():
        attrs = prod.get("attributes") or {}
        vals = {_norm(v) for v in attrs.values()}
        cov = len(wanted & vals)
        if not cov:
            continue
        dims = []
        for term in terms.get(sku, {}).values():
            for dim in (term.get("priceDimensions") or {}).values():
                dims.append({"unit": dim.get("unit"),
                             "usd": float(dim.get("pricePerUnit", {}).get("USD", 0) or 0),
                             "description": dim.get("description", "")})
        out.append({"sku": sku, "attributes": attrs, "usagetype": attrs.get("usagetype"),
                    "operation": attrs.get("operation"), "prices": dims, "coverage": cov})
    if not out:
        return []
    best = max(m["coverage"] for m in out)
    out = [m for m in out if m["coverage"] == best]
    if anchor and len(out) > 1:
        a = _norm(anchor)
        pref = [m for m in out if a in _norm(
            str(m["attributes"].get("usagetype", "")) + str(m["attributes"].get("operation", "")) +
            str(m["attributes"].get("productFamily", "")) + str(m["attributes"].get("group", "")))]
        if pref:
            out = pref
    return out


def find_sku_by_words(service_code: str, region: str, words: List[str],
                      anchor: str = "") -> List[dict]:
    """SKUs whose usagetype / operation / family CONTAINS the resource's words.

    The exact-value tier answers "which SKU has this attribute", and for most resources that is enough:
    an EC2 instance's ``t3.medium`` IS an attribute value. But a resource and its SKU sometimes describe
    the same thing in different grammar, and then exact matching finds NOTHING at all. Measured: an
    ALB's create says ``type: application`` / ``scheme: internet-facing``, and not one of those strings
    is an attribute value anywhere in AWSELB or AmazonEC2, while the SKU that bills it says
    ``operation: LoadBalancing:Application``. The word is there, inside a longer one.

    Containment is deliberately the SECOND tier: as a primary filter it is far too loose (a word like
    "application" appears all over a price list), but where exact matching returns nothing it is the
    difference between pricing the largest line on the bill and reporting it unpriceable."""
    ws = [_norm(w) for w in words if isinstance(w, str) and len(str(w)) > 2]
    if not ws:
        return []
    # The digest holds every usagetype/operation/family this service publishes, joined. If the anchor is
    # absent from all of it, no SKU here is about this kind of resource and the offer file is not read.
    blob = _digest(service_code, region)["hay"]
    if anchor and _norm(anchor) not in blob:
        return []
    if not any(w in blob for w in ws):
        return []
    offer = region_offer(service_code, region)
    if not offer:
        return []
    terms = (offer.get("terms") or {}).get("OnDemand", {})
    out = []
    for sku, prod in (offer.get("products") or {}).items():
        a = prod.get("attributes") or {}
        hay = _norm(str(a.get("usagetype", "")) + str(a.get("operation", "")) +
                    str(a.get("productFamily", "")) + str(a.get("group", "")))
        # ANCHOR ON THE RESOURCE'S NOUN. Matching on any word alone let FREE resources find unrelated
        # SKUs once the billable-create list was removed: a key pair matched on "pem", target groups
        # matched 14 SKUs on "HTTP" and "instance". The noun of the event that created the thing
        # ("LoadBalancer", "KeyPair", "TargetGroup") must itself appear, so a resource only matches a SKU
        # that is about that kind of resource. A key pair then matches nothing, which is correct: it is
        # free, and it says so by finding no price rather than by being on a list.
        if anchor and _norm(anchor) not in hay:
            continue
        if not any(w in hay for w in ws):
            continue
        dims = []
        for term in terms.get(sku, {}).values():
            for dim in (term.get("priceDimensions") or {}).values():
                dims.append({"unit": dim.get("unit"),
                             "usd": float(dim.get("pricePerUnit", {}).get("USD", 0) or 0),
                             "description": dim.get("description", "")})
        out.append({"sku": sku, "attributes": a, "usagetype": a.get("usagetype"),
                    "operation": a.get("operation"), "prices": dims})
    return out


def service_for(event_source: str) -> List[str]:
    """Offer codes whose PUBLISHED service name is exactly this CloudTrail eventSource, normalised.

    Derived, not mapped. AWS publishes each service's human name, and normalising both sides makes
    ``elasticloadbalancing`` meet "Elastic Load Balancing" -> AWSELB, which no rule over the offer CODE
    can do (AWSELB shares no substring with it). Only EXACT normalised equality is accepted: substring
    matching on service names was measured to return confidently wrong answers (ec2 ->
    AmazonEC2OCPULicenseFees, s3 -> AmazonS3GlacierDeepArchive, ssm -> AWSIAMAccessAnalyzer).

    An empty result is not a failure; the caller falls back to searching the whole disclosure, which is
    always correct and merely slower."""
    if not event_source:
        return []
    want = _norm(event_source)
    names = _service_names()
    exact = [code for code, nm in names.items() if _norm(nm) == want]
    if exact:
        return exact
    return [c for c in names if want and want in _norm(c)]


def _service_names() -> Dict[str, str]:
    """offer code -> published service name, fetched once and cached on disk."""
    cache = os.path.join(_CACHE, "_servicenames.json")
    if os.path.exists(cache):
        with open(cache) as fh:
            return json.load(fh)
    import subprocess
    out = {}
    for code in service_codes():
        r = subprocess.run(["aws", "pricing", "get-attribute-values", "--region", "us-east-1",
                            "--service-code", code, "--attribute-name", "servicename",
                            "--max-results", "1"], capture_output=True, text=True)
        if r.returncode:
            continue
        try:
            vals = json.loads(r.stdout).get("AttributeValues") or []
        except ValueError:
            continue
        if vals:
            out[code] = vals[0]["Value"]
    os.makedirs(_CACHE, exist_ok=True)
    with open(cache, "w") as fh:
        json.dump(out, fh)
    return out
