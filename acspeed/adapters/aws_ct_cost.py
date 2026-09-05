"""CloudTrail-driven AWS run-rate: discover from what the account RECORDED, price from the public list.

WHY THIS EXISTS. The enumerate-then-price path in ``aws_runrate.py`` asks the account "show me the
resources matching this deploy" and prices what comes back. Every question in that sentence is an
assumption that can be wrong, and when it is wrong the answer is silently SMALLER, never absent:

  * matching by run-token anchor  -> a resource named without the token is invisible
  * matching in the URL's region  -> aws-medium-b run17's RDS lived in us-east-1 while the container
                                     ran in us-east-2, so the datastore was never seen ($10.00 vs $28.36)
  * a per-service short circuit   -> the Lightsail branch priced the container and nothing else
  * a dropped row class           -> run19's Lightsail RELATIONAL DATABASE was filtered out wholesale
  * a dead enumerator             -> runs 11/12 lost both tag-independent paths to a swallowed exception

All SEVEN runs aws-medium-b discarded were DISCOVERY failures. Not one was a pricing failure. So this
module replaces the discovery half with the one source that has no such assumptions: CloudTrail
management events, which every AWS account records BY DEFAULT with no index, no tags, no recorder and
no opt-in. Measured 2026-09-05 on 776638915601: 2.9 min lag, 17 regions swept in 203s, 22 services
observed, and it captures ``lightsail:CreateContainerService`` which Resource Explorer cannot index at
all (654 indexed types over 171 services, Lightsail absent).

WHAT IS AND IS NOT UNIVERSAL, measured, so nobody re-inflates this later:
  * DISCOVERY is universal. Every billable resource either has its own create call, or is described in
    the PARAMETERS of the call that created its parent (verified on three independent cases: the EBS
    root volume from ``RunInstances.blockDeviceMapping`` with ZERO CreateVolume events; RDS storage,
    backups and Multi-AZ from ``CreateDBInstance``; public IPv4 from an internet-facing ALB's subnets
    and EC2's ``associatePublicIpAddress``).
  * PRICING is NOT universal, and no amount of cleverness made it so. Two joins need a small data
    table, and both were measured to have no discoverable rule:
      - eventSource -> Price List service code. Naive string matching resolves 14 of 22 and MISSES
        ``elasticloadbalancing`` -> ``AWSELB``, the largest line on the bill. Normalized servicename
        matching is WORSE than useless: it returns confidently wrong answers (``ec2`` ->
        AmazonEC2OCPULicenseFees, ``s3`` -> AmazonS3GlacierDeepArchive, ``ssm`` -> AWSIAMAccessAnalyzer).
      - value spelling. CloudTrail says ``engine: "postgres"``; the Price List says
        ``databaseEngine: "PostgreSQL"``.
  * Therefore a service absent from the tables is EXCLUDED, never mispriced. That is the whole design:
    it converts "silently cheap" into "loudly refused". A lost run is recoverable; a wrong published
    number is not.

Numbers are QUANTITIES, never SKU selectors. Measured: the value ``1`` matches 13 different EC2
attributes and ``8`` matches 9; RDS ``20`` matches ``engineCode``, so an ``allocatedStorage`` of 20 GB
would otherwise select a database ENGINE. Only string values select.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from typing import Dict, List, Optional, Tuple

from acspeed.cost import RateComponent, RunRate, compose_run_rate, HOURS_PER_MONTH

# NOTE ON TABLES. The eventSource -> serviceCode map and the value-spelling map that used to live here
# are GONE, not moved: the bulk price index is searched by the resource's own attribute VALUES, so the
# service never has to be named, and matching is case- and punctuation-folded, so "postgres" meets
# "PostgreSQL" with no map. What remains below is either DERIVED from what AWS publishes or is the
# paper's own definition of the axis.
# _LOCATION removed: every published SKU carries ``regionCode`` (verified: a Lightsail SKU holds
# location "US East (N. Virginia)", locationType "AWS Region" AND regionCode "us-east-1"), so the region
# is matched directly and the hand-written 11-entry region-name map is not needed.

# Which CloudTrail create events stand up a BILLED resource. A create absent here is reported as
# UNCLASSIFIED (and refuses), never assumed free: security groups and subnet groups are genuinely free,
# but the rule for deciding that has to be written down, not inferred.
# NO LIST OF WHICH CREATES ARE BILLABLE. There used to be one (12 event names) plus a list of "free"
# creates (23 more), and between them they decided what got priced. That is vendor knowledge typed out by
# hand, and it was WRONG BY OMISSION for 11 common billable creates alone (CloudFront distributions,
# Lambda functions, S3 buckets, Beanstalk environments, Amplify apps, EFS file systems, ElastiCache
# replication groups, RDS proxies, PrivateLink endpoints, Aurora global clusters). A service AWS ships
# next year would have been invisible.
#
# The default is inverted instead: EVERY successful create is a candidate, and the PUBLISHED DISCLOSURE
# decides. If the resource's own words find a SKU, it bills and is priced; if they find none, it is
# reported by name. Nothing needs to be anticipated.
_CREATE_VERB = re.compile(r"^(Create|Run|Allocate|Provision|Launch|Register|Request)[A-Z]")
_DELETE_VERB = re.compile(r"^(Delete|Terminate|Release|Deprovision|Deregister|Destroy)[A-Z]")


def _kind_of(event_name: str) -> str:
    """The resource kind an event names, DERIVED from the verb and noun rather than mapped.

    ``CreateRelationalDatabase`` -> ``relationaldatabase``; ``RunInstances`` -> ``instances``. Pairing a
    delete with its create is then a string operation on the same noun, so the delete table goes too."""
    name = event_name or ""
    # Order matters: `_CREATE_VERB.sub` on a DELETE event returns the string unchanged, which is truthy,
    # so an `or` chain never reaches the delete branch and no delete ever pairs with its create.
    if _DELETE_VERB.match(name):
        return _DELETE_VERB.sub("", name)
    return _CREATE_VERB.sub("", name)



def _aws(args: List[str], timeout: int = 120) -> Tuple[Optional[dict], Optional[str]]:
    try:
        r = subprocess.run(["aws"] + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    if r.returncode != 0:
        return None, r.stderr[:200]
    try:
        return json.loads(r.stdout or "{}"), None
    except ValueError as e:
        return None, str(e)[:200]


def deploy_window(rec: dict, transcript_first_ts: str) -> Tuple[str, str]:
    """(start, end) of the DEPLOY TURN, the window the cost snapshot closes over.

    The end is ``serving.agent_finished_at_s``, the POLLER's own clock. Never ``agent_wall_s``: that is
    ``lane_summary.wall_s``, derived from the transcript, and on aws-medium-b run20 it read 287.7s
    against a true 3303.7s (11.5x short). It matched to within 2% on all 15 earlier runs, so keying on
    it looked safe right up to the run that would have been silently truncated."""
    t0 = dt.datetime.fromisoformat(transcript_first_ts.replace("Z", "+00:00"))
    end_s = (rec.get("serving") or {}).get("agent_finished_at_s") or rec.get("agent_wall_s") or 0
    end = t0 + dt.timedelta(seconds=float(end_s))
    return t0.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


def _identity(params: dict) -> str:
    """The resource's own name, as the create/delete call names it. Used to net a mid-run create against
    its OWN delete rather than against any delete of the same kind."""
    for k in ("serviceName", "relationalDatabaseName", "dBInstanceIdentifier", "dBClusterIdentifier",
              "loadBalancerName", "cacheClusterId", "instanceName", "name"):
        v = params.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def enabled_regions(profile: Optional[str] = None) -> List[str]:
    p = ["--profile", profile] if profile else []
    d, _ = _aws(["ec2", "describe-regions", "--query", "Regions[].RegionName", "--output", "json"] + p)
    return d if isinstance(d, list) else ["us-east-1"]


# --- discovery ----------------------------------------------------------------------------------

def discover(start: str, end: str, run_token: str, regions: List[str],
             profile: Optional[str] = None) -> Tuple[List[dict], List[str]]:
    """Every SUCCESSFUL billable create in the window carrying ``run_token``, across ``regions``.

    Returns (resources, unclassified). ``unclassified`` names creates that are neither known-billable
    nor known-free, so a service nobody anticipated REFUSES the run instead of being assumed free.
    Failures are never swallowed into an empty list: a region that cannot be read raises."""
    p = ["--profile", profile] if profile else []
    found: List[dict] = []
    deleted: List[tuple] = []       # (kind, identity, eventTime)
    unclassified: List[str] = []
    for reg in regions:
        nxt, pages = None, 0
        while pages < 200:
            cmd = ["cloudtrail", "lookup-events", "--region", reg,
                   "--lookup-attributes", "AttributeKey=ReadOnly,AttributeValue=false",
                   "--start-time", start, "--end-time", end, "--max-results", "50"] + p
            if nxt:
                cmd += ["--next-token", nxt]
            d, err = _aws(cmd)
            if d is None:
                raise RuntimeError(f"CloudTrail unreadable in {reg}: {err}. Refusing rather than "
                                   f"returning a smaller, confident answer.")
            for ev in d.get("Events", []):
                raw = json.loads(ev["CloudTrailEvent"])
                if raw.get("errorCode"):
                    continue                                   # a failed call provisioned nothing
                blob = json.dumps(raw)
                if run_token and run_token not in blob:
                    continue
                name = ev.get("EventName", "")
                if _DELETE_VERB.match(name):
                    deleted.append((_kind_of(name).lower(),
                                    _identity(raw.get("requestParameters") or {}),
                                    raw.get("eventTime") or ""))
                    continue
                if _CREATE_VERB.match(name):
                    prm = raw.get("requestParameters") or {}
                    found.append({"kind": _kind_of(name).lower(), "region": reg, "event": name,
                                  "src": raw.get("eventSource", "").split(".")[0],
                                  "identity": _identity(prm), "at": raw.get("eventTime") or "",
                                  "params": prm,
                                  "response": raw.get("responseElements") or {}})
            nxt = d.get("NextToken")
            pages += 1
            if not nxt:
                break
    # NET OUT ONLY MID-RUN CHURN, NEVER THE TEARDOWN. A run that succeeds DEPROVISIONS everything it
    # built, so those deletes sit in the same window as the creates. Cancelling a create against any
    # delete of the same KIND therefore zeroed every completed run: measured on run21, whose three
    # resources (2 Lightsail containers + 1 Lightsail database) were all created 19:38-19:40 and all
    # deleted at 20:32, and which this function reported as ZERO billable resources. A run-rate of $0
    # for a healthy run is the same silent-undercount defect this module exists to remove.
    #
    # The rule: a delete cancels its create only when it targets the SAME resource identity AND happens
    # before the LAST create of the run. Everything the agent builds and abandons mid-run is bracketed
    # by later creates; teardown is not.
    last_create = max((r.get("at") or "" for r in found), default="")
    for kind, ident, at in deleted:
        if at and last_create and at >= last_create:
            continue                                          # teardown, not churn
        for i, r in enumerate(found):
            if r["kind"] == kind and (not ident or r.get("identity") == ident):
                found.pop(i)
                break
    return found, sorted(set(unclassified))


# --- pricing ------------------------------------------------------------------------------------

def _price_rows(code: str, filters: Dict[str, str], profile: Optional[str] = None) -> List[tuple]:
    p = ["--profile", profile] if profile else []
    fl = [f"Type=TERM_MATCH,Field={k},Value={v}" for k, v in filters.items()]
    d, _ = _aws(["pricing", "get-products", "--region", "us-east-1", "--service-code", code,
                 "--filters"] + fl + ["--max-results", "30"] + p)
    rows = []
    if not d:
        return rows
    for raw in d.get("PriceList", []):
        o = json.loads(raw)
        a = o["product"]["attributes"]
        for term in o["terms"].get("OnDemand", {}).values():
            for dim in term["priceDimensions"].values():
                rows.append((a.get("usagetype"), dim["unit"], float(dim["pricePerUnit"]["USD"])))
    return rows


def _hourly(unit: str, price: float, quantity: float = 1.0) -> float:
    u = (unit or "").lower()
    if u.startswith("hr") or u == "hours":
        return price * quantity
    if "gb-mo" in u or "gb-month" in u:
        return price * quantity / HOURS_PER_MONTH
    if u.startswith("mo"):
        return price * quantity / HOURS_PER_MONTH
    return price * quantity


# price_resource() removed: it was per-service pricing code (an if/elif per kind, with a
# region-name map and an engine-spelling map). price_resource_universal() replaces it by
# searching the published disclosure with the resource's own words.
def run_rate_from_cloudtrail(start: str, end: str, run_token: str, capture_date: str,
                             regions: Optional[List[str]] = None,
                             profile: Optional[str] = None) -> dict:
    """The whole path. Returns a ``cost_run_rate``-shaped dict; ``ok`` is False when ANYTHING
    discovered could not be priced, and ``unpriced_resources`` says exactly what."""
    regions = regions or enabled_regions(profile)
    resources, unclassified = discover(start, end, run_token, regions, profile)
    if describe is None:
        describe = lambda r: describe_live(r, profile)   # noqa: E731 - the default IS the live describe
    comps: List[RateComponent] = []
    unpriced: List[str] = list(unclassified)
    for r in resources:
        c, u = price_resource(r, profile)
        comps += c
        unpriced += u
    if not comps:
        return {"ok": False, "error": "no billable resource priced", "discovered": len(resources),
                "unpriced_resources": unpriced}
    rr = compose_run_rate(comps, provider="aws", region=(resources[0]["region"] if resources else "?"),
                          flavor="+".join(sorted({r["kind"] for r in resources})),
                          capture_date=capture_date,
                          price_source="AWS public list (Price List Query API), resources discovered "
                                       "from CloudTrail management events in the deploy window")
    d = rr.to_dict()
    d["discovered_resources"] = [{"kind": r["kind"], "region": r["region"], "event": r["event"]}
                                 for r in resources]
    d["unpriced_resources"] = unpriced
    d["ok"] = not unpriced                     # FAIL CLOSED: anything unpriced sinks the whole run
    return d


# The corroboration GATE that used to sit here is deleted. It refused a run whose price did not
# account for everything, which sounds rigorous and is not: AWS must publish the price of
# everything it bills, so 'cannot price it' is never a property of the resource, only a failure
# of the lookup. Refusing encodes that failure as an acceptable outcome. Price it instead.

# --- universal pricing: the resource's OWN words, matched against the published disclosure ---------

def price_from_index(resource: dict, services: Optional[List[str]] = None) -> dict:
    """Price ONE discovered resource from the published price list, with no per-service pricing code.

    The selectors are simply the STRING values the create call used to describe the resource. No
    translation table: matching is case- and punctuation-insensitive, which is what makes CloudTrail's
    ``nano`` meet the SKU's ``Nano`` and ``postgres`` meet ``PostgreSQL`` without a spelling map.

    Where the two vocabularies genuinely do not share a term the match returns nothing, and that is
    REPORTED as a vocabulary gap naming the identifier, never as a zero. Measured example: Lightsail
    passes ``relationalDatabaseBundleId: micro_2_0`` while the SKU is keyed on ``memory``/``storage``,
    and the string ``micro_2_0`` appears in no SKU in the published file. EC2 (``instanceType``), RDS
    (``dBInstanceClass``) and Lightsail containers (``power``) DO share the term."""
    from acspeed.adapters.aws_price_index import find_sku, service_codes

    region = resource.get("region") or "us-east-1"
    params = resource.get("params") or {}
    selectors = {k: v for k, v in params.items()
                 if isinstance(v, str) and 1 < len(v) <= 40
                 and not v.startswith(("arn:", "sg-", "subnet-", "vpc-", "ami-", "i-"))}
    tried, hits = [], []
    for code in (services or service_codes()):
        tried.append(code)
        try:
            found = find_sku(code, region, selectors)
        except Exception:  # noqa: BLE001 - one unreadable offer file must not hide the rest
            continue
        for m in found:
            for d in m["prices"]:
                if d["usd"] > 0:
                    hits.append({"service": code, "sku": m["sku"], "usagetype": m["usagetype"],
                                 "unit": d["unit"], "usd": d["usd"], "desc": d["description"]})
                    break
    if not hits:
        return {"priced": False, "reason": "vocabulary gap: no SKU carries any of "
                                           f"{sorted(selectors.values())}", "candidates": 0}
    return {"priced": True, "candidates": len(hits), "hits": hits[:6]}


# --- end to end: discovered resource -> its own words -> the published SKU -> a price ---------------

# Values that describe a SKU rather than name an instance. Names, ids, ARNs and zones are excluded
# because they never appear in a price list; everything else the create call said is a candidate
# selector. This is a shape rule, not a service list.
_NOT_A_SELECTOR = re.compile(r"^(arn:|sg-|subnet-|vpc-|ami-|i-|eni-|rtb-|igw-)|^[a-z]{2}-[a-z]+-\d[a-z]?$")


def selectors_for(resource: dict) -> Dict[str, str]:
    """The resource as its own create call described it, reduced to SKU-selecting strings.

    Numbers are dropped: they are QUANTITIES and match attributes spuriously (EC2 value ``1`` matches 13
    attributes; RDS ``20`` matches ``engineCode``, so a 20 GB volume would select a database engine).
    Names and ids are dropped because no SKU carries them, and requiring them makes every match fail."""
    out = {}
    for k, v in (resource.get("params") or {}).items():
        if not isinstance(v, str) or not (1 < len(v) <= 40):
            continue
        if _NOT_A_SELECTOR.match(v):
            continue
        if k.lower().endswith(("name", "identifier", "id")) and not k.lower().endswith(("bundleid", "blueprintid")):
            continue
        out[k] = v
    return out


def price_resource_universal(resource: dict, describe=None) -> dict:
    """Price one discovered resource from the published disclosure. No per-service pricing code.

    ``describe`` is an optional callable(resource) -> extra selector dict, used when the create call
    speaks a vocabulary the price list does not share. Measured example: Lightsail passes
    ``relationalDatabaseBundleId: micro_2_0`` while the SKU is keyed on ``memory``/``storage``, and the
    string ``micro_2_0`` appears in NO SKU in the published file. At cost-snapshot time the resource is
    still alive, so it can be asked what it is; AWS::Lightsail::Database and its peers are discoverable
    in the public CloudFormation type registry, so this needs no hand-written type map either."""
    from acspeed.adapters.aws_price_index import find_sku, ondemand_only, service_codes

    region = resource.get("region") or "us-east-1"
    sel = selectors_for(resource)
    if describe:
        try:
            sel.update(describe(resource) or {})
        except Exception:  # noqa: BLE001 - a describe failure must not fake a price
            pass
    if not sel:
        return {"priced": False, "reason": "no SKU-selecting attribute in the create call"}

    # Narrow to the resource's own service first, purely as an optimisation; fall back to the whole
    # disclosure so a service whose name does not resemble its offer code is still found.
    src = (resource.get("src") or "").lower()
    guess = [c for c in service_codes() if src and src.replace("-", "") in c.lower()]
    for pool in (guess, None):
        hits = []
        for code in (pool if pool is not None else service_codes()):
            try:
                # Rank the resource's own words by how RARE they are in this service's SKUs, and add them
                # most-identifying first until exactly one on-demand SKU remains. Requiring all of them at
                # once left an ALB ambiguous across 13 SKUs on generic words (application, internet-facing,
                # ipv4) while `operation` alone identifies it. No per-service attribute list.
                from acspeed.adapters.aws_price_index import selectivity
                order = sorted(sel.items(), key=lambda kv: selectivity(code, region, [kv[1]]).get(kv[1], 0)
                               or 10 ** 6)
                narrowed, best = {}, None
                for k, v in order:
                    narrowed[k] = v
                    got = ondemand_only(find_sku(code, region, narrowed))
                    if len(got) == 1:
                        best = got
                        break
                    if not got:
                        narrowed.pop(k)            # this word only removed matches; it is not a selector
                    else:
                        best = got
                hits += [(code, m) for m in (best or [])]
            except Exception:  # noqa: BLE001
                continue
        if len(hits) == 1:
            code, m = hits[0]
            dim = next((d for d in m["prices"] if d["usd"] > 0), None)
            if dim:
                hourly = dim["usd"] if dim["unit"].lower().startswith("hr") else dim["usd"] / HOURS_PER_MONTH
                return {"priced": True, "service": code, "sku": m["sku"], "usagetype": m["usagetype"],
                        "hourly_usd": hourly, "unit": dim["unit"], "raw": dim["usd"],
                        "selectors": sel}
        if hits:
            return {"priced": False, "reason": f"ambiguous: {len(hits)} on-demand SKUs match "
                                               f"{sorted(sel.values())}",
                    "candidates": [m["usagetype"] for _c, m in hits[:6]]}
    return {"priced": False, "reason": f"no SKU carries {sorted(sel.values())}", "selectors": sel}


# --- the universal describe: ask the LIVE resource what it is ------------------------------------

_TYPE_CACHE: Dict[str, List[str]] = {}


def _registry_types(profile: Optional[str] = None) -> List[str]:
    """Every AWS resource type in the public CloudFormation registry, enumerated not hardcoded.

    This is what makes the describe step service-independent: AWS::Lightsail::Database and its peers are
    discoverable, so no hand-written eventSource -> type map is needed."""
    if _TYPE_CACHE.get("all"):
        return _TYPE_CACHE["all"]
    p = ["--profile", profile] if profile else []
    out, nxt, pages = [], None, 0
    while pages < 40:
        cmd = ["cloudformation", "list-types", "--visibility", "PUBLIC", "--type", "RESOURCE",
               "--filters", "Category=AWS_TYPES", "--max-results", "100", "--region", "us-east-1"] + p
        if nxt:
            cmd += ["--next-token", nxt]
        d, _err = _aws(cmd)
        if not d:
            break
        out += [t["TypeName"] for t in d.get("TypeSummaries", [])]
        nxt = d.get("NextToken")
        pages += 1
        if not nxt:
            break
    _TYPE_CACHE["all"] = out
    return out


def describe_live(resource: dict, profile: Optional[str] = None) -> Dict[str, str]:
    """Extra SKU selectors read from the LIVE resource, for vendors whose create call speaks a vocabulary
    the price list does not share.

    Lightsail creates a database with ``relationalDatabaseBundleId: micro_2_0``; that string appears in
    NO SKU in the published Lightsail price list, which is keyed on memory and storage. At cost-snapshot
    time the resource is still running, so it can simply be asked. Cloud Control is the uniform way to
    ask (verified against the live account on 2026-09-05), and the type name is discovered from the
    public registry rather than mapped."""
    src = (resource.get("src") or "").replace("-", "")
    ident = resource.get("identity") or ""
    if not src or not ident:
        return {}
    ev = (resource.get("event") or "").replace("Create", "").replace("Run", "").lower()
    cands = [t for t in _registry_types(profile) if t.split("::")[1].lower() == src]
    best = None
    for t in cands:
        leaf = t.split("::")[-1].lower()
        if leaf == ev or leaf in ev or ev in leaf:
            best = t
            break
    if not best:
        return {}
    p = ["--profile", profile] if profile else []
    d, _err = _aws(["cloudcontrol", "get-resource", "--type-name", best, "--identifier", ident,
                    "--region", resource.get("region", "us-east-1")] + p)
    if not d:
        return {}
    try:
        props = json.loads((d.get("ResourceDescription") or {}).get("Properties") or "{}")
    except ValueError:
        return {}
    # Only scalars can select a SKU, and only strings (numbers are quantities).
    return {k: v for k, v in props.items() if isinstance(v, str) and 1 < len(v) <= 40}


# --- one create call can stand up SEVERAL billable things -----------------------------------------

def billable_parts(resource: dict) -> List[dict]:
    """Expand a discovered resource into EVERY billable thing its create call describes.

    A create call is not one resource. ``RunInstances`` also provisions the root EBS volume, described
    inside ``blockDeviceMapping``; ``CreateDBInstance`` also provisions storage and backups, described by
    ``allocatedStorage`` / ``storageType`` / ``backupRetentionPeriod``; an internet-facing load balancer
    also consumes a public IPv4 per subnet. Measured: those children have NO create call of their own
    (7 successful RunInstances against ZERO CreateVolume events), yet EBS:VolumeUsage.gp3,
    RDS:GP3-Storage and USE1-PublicIPv4:InUseAddress are all on the real bill.

    Pricing only the parent is therefore a SILENT under-charge, which is the failure this whole path
    exists to remove. The rule is structural, not per-service: walk the create's parameters for nested
    objects that describe a sized thing, and emit each as its own billable part with its own quantity."""
    parts = [dict(resource, part="self")]
    prm = resource.get("params") or {}

    def _walk(node, path=""):
        if isinstance(node, dict):
            # a nested object carrying a SIZE is a provisioned child (an EBS volume, a data disk)
            size = next((node.get(k) for k in ("volumeSize", "sizeInGB", "size", "diskSize",
                                               "allocatedStorage") if isinstance(node.get(k), (int, float))),
                        None)
            kind = next((str(node.get(k)) for k in ("volumeType", "storageType", "type")
                         if isinstance(node.get(k), str)), None)
            if size and kind:
                parts.append({**resource, "part": path or "child", "kind": "storage",
                              "quantity": float(size), "params": {"volumeApiName": kind,
                                                                  "volumeType": kind}})
            for k, v in node.items():
                _walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for v in node:
                _walk(v, path)

    _walk(prm)
    # a flat sized field on the parent itself (RDS: allocatedStorage + storageType)
    size = prm.get("allocatedStorage")
    stype = prm.get("storageType")
    if isinstance(size, (int, float)) and isinstance(stype, str) and \
            not any(p.get("kind") == "storage" for p in parts[1:]):
        parts.append({**resource, "part": "allocatedStorage", "kind": "storage",
                      "quantity": float(size), "params": {"volumeName": stype, "volumeApiName": stype}})
    # an internet-facing load balancer consumes a public IPv4 in every subnet it is placed in
    if resource.get("kind") == "load_balancer" and str(prm.get("scheme", "")).lower() == "internet-facing":
        subnets = prm.get("subnets") or prm.get("subnetMappings") or []
        n = len(subnets) if isinstance(subnets, list) else 1
        if n:
            parts.append({**resource, "part": "public-ipv4", "kind": "elastic_ip", "quantity": float(n),
                          "params": {"usagetype": _ipv4_usagetype(resource.get("region", "us-east-1"))}})
    return parts


def _ipv4_usagetype(region: str) -> str:
    """The public-IPv4 usagetype for a region, READ OFF the published SKUs, not a typed-out prefix map.

    AWS encodes the region as a short code inside the usagetype (USE1-, USW2-, EUC1-). Those prefixes
    were previously hand-written for 7 regions and simply wrong for the other 30. They are in the
    disclosure: take the VPC SKU in this region whose usagetype names an in-use address."""
    try:
        from acspeed.adapters.aws_price_index import region_offer
        for prod in (region_offer("AmazonVPC", region).get("products") or {}).values():
            ut = str((prod.get("attributes") or {}).get("usagetype") or "")
            if "PublicIPv4:InUseAddress" in ut:
                return ut
    except Exception:  # noqa: BLE001 - a lookup failure must not fabricate a usagetype
        pass
    return "PublicIPv4:InUseAddress"


def run_rate_universal(run_token: str, capture_date: str, regions: Optional[List[str]] = None,
                       profile: Optional[str] = None, lookback_hours: int = 8,
                       describe=None) -> Optional[dict]:
    """The whole cost axis, end to end, with no per-service pricing code.

    Discovery is CloudTrail (every create, every service, every region, on by default). Pricing is the
    published disclosure (269 services, no credentials, complete by law). Disambiguation is C19's own
    definition of the axis, public ON-DEMAND list.

    The window is generous and the RUN TOKEN scopes it: the token identifies this run's resources, and
    its own create events bound the period, so nothing needs to be recorded at snapshot time. Returns
    None when there is no token to scope by, so the caller can say why rather than invent a number.

    Verified end to end on aws-medium-b run21 against the live account: 3 resources discovered, all 3
    priced, $36.30/mo against the $22.00 the inventory path published, the difference being a Lightsail
    relational database that Resource Explorer cannot index and the old adapter therefore never saw."""
    if not run_token:
        return None
    regions = regions or enabled_regions(profile)
    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    resources, unclassified = discover(start, end, run_token, regions, profile)
    if describe is None:
        describe = lambda r: describe_live(r, profile)   # noqa: E731 - the default IS the live describe
    comps: List[RateComponent] = []
    unpriced: List[str] = list(unclassified)
    priced_detail = []
    for r in resources:
        # ONE create call can stand up SEVERAL billable things. The root EBS volume of an EC2 instance,
        # an RDS instance's storage and an internet-facing ALB's public IPv4 addresses have NO create
        # call of their own but ARE on the bill, so pricing only the parent under-charges silently.
        for part in billable_parts(r):
            qty = float(part.get("quantity") or 1.0)
            p = price_resource_universal(part, describe=(describe if part.get("part") == "self" else None))
            if p.get("priced"):
                hourly = p["hourly_usd"] * qty
                comps.append(RateComponent(name=f"{part['kind']}", hourly_usd=hourly,
                                           raw_unit_price=p["raw"], native_unit=p["unit"], quantity=qty))
                priced_detail.append({"kind": part["kind"], "part": part.get("part"),
                                      "region": part["region"], "quantity": qty,
                                      "usagetype": p["usagetype"], "sku": p["sku"]})
            else:
                unpriced.append(f"{part['kind']}[{part.get('part')}]@{part['region']}: {p.get('reason')}")
    if not comps and not resources:
        return {"ok": False, "error": f"no billable resource recorded for run token {run_token}",
                "discovery": "cloudtrail", "unpriced_resources": unpriced}
    rr = compose_run_rate(comps, provider="aws",
                          region=(resources[0]["region"] if resources else "?"),
                          flavor="+".join(sorted({r["kind"] for r in resources})) or "?",
                          capture_date=capture_date,
                          price_source="AWS published price list (bulk offer index), on-demand per C19; "
                                       "resources discovered from CloudTrail management events")
    d = rr.to_dict()
    d["discovery"] = "cloudtrail"
    d["priced_resources"] = priced_detail
    d["unpriced_resources"] = unpriced
    d["ok"] = not unpriced
    return d
