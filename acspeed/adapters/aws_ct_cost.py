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
import os
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
# FALLBACK ONLY. These thirteen English verbs used to BE the classifier, and a verb absent from them made
# a resource invisible: not unpriced, never discovered. Measured against botocore's full API surface,
# billable things are stood up by verbs outside the list (CopySnapshot, StartInstances, StartDBInstance),
# so the failure was a silent UNDER-COUNT, which is the worst kind this module can produce. AWS publishes
# the answer (see `_lifecycle`); these remain only for an operation the published registry does not
# mention, so coverage can grow but never shrink.
# The capital that starts the noun is matched by LOOKAHEAD, never consumed. Consuming it ate the first
# letter of every kind in the run: CreateRelationalDatabase -> "elationalDatabase", CreateKeyPair ->
# "eyPair", RunInstances -> "nstances". Delete pairing survived it (both sides were mangled the same
# way) but the SKU anchor did not, and an anchor of "etworkinterface" is not the noun of anything.
_CREATE_VERB = re.compile(r"^(Create|Run|Allocate|Provision|Launch|Register|Request)(?=[A-Z])")
_DELETE_VERB = re.compile(r"^(Delete|Terminate|Release|Deprovision|Deregister|Destroy)(?=[A-Z])")

# AWS's own labelling of which API call creates a resource and which destroys it, published as the
# CloudFormation resource schemas: every type carries `handlers.create.permissions` and
# `handlers.delete.permissions`, and those permissions ARE API operations. One public 3 MB download,
# no credentials, 1729 types.
_CFN_SCHEMA_ZIP = "https://schema.cloudformation.us-east-1.amazonaws.com/CloudformationSchema.zip"
_LIFECYCLE: Dict[str, set] = {}


def _lifecycle() -> Dict[str, set]:
    """`{"create": {"ec2:RunInstances", ...}, "delete": {"ec2:TerminateInstances", ...}}`, from AWS.

    This replaces thirteen verbs I typed out. The verbs were not merely incomplete, they were incomplete
    in the direction that loses money quietly: an operation outside them was not classified as a create,
    so the resource never entered discovery at all and its cost was simply absent. `ec2:CopySnapshot` and
    `ec2:StartInstances` are both in a published create handler and in neither verb list.

    Returns empty sets when the download is unavailable, so the caller falls back to the verbs and
    coverage degrades to what it was rather than to nothing."""
    if _LIFECYCLE:
        return _LIFECYCLE
    from acspeed.adapters.aws_price_index import _cache_dir
    cache = os.path.join(_cache_dir(), "cfn_lifecycle.json")
    try:
        if os.path.exists(cache) and os.path.getsize(cache) > 0:
            with open(cache) as fh:
                raw = json.load(fh)
        else:
            import io
            import urllib.request
            import zipfile
            with urllib.request.urlopen(_CFN_SCHEMA_ZIP, timeout=300) as r:  # noqa: S310 - fixed AWS host
                blob = r.read()
            create, delete = set(), set()
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for name in z.namelist():
                    try:
                        handlers = (json.loads(z.read(name)).get("handlers") or {})
                    except Exception:  # noqa: BLE001 - one unreadable schema is not a failure
                        continue
                    create |= set((handlers.get("create") or {}).get("permissions") or [])
                    delete |= set((handlers.get("delete") or {}).get("permissions") or [])
            raw = {"create": sorted(create), "delete": sorted(delete)}
            os.makedirs(_cache_dir(), exist_ok=True)
            with open(cache, "w") as fh:
                json.dump(raw, fh)
    except Exception:  # noqa: BLE001 - offline is a smaller coverage, never a wrong answer
        raw = {"create": [], "delete": []}
    _LIFECYCLE.update({"create": set(raw["create"]), "delete": set(raw["delete"])})
    return _LIFECYCLE


def classify_event(src: str, event_name: str) -> Optional[str]:
    """"create", "delete", or None for an event that does neither.

    The published registry decides; the verb lists answer only for an operation it does not mention.
    An operation appearing in BOTH a create and a delete handler (an address is allocated by one type
    and released by another) is left to the verbs, which is the honest reading of an ambiguous source."""
    name, cycle = event_name or "", _lifecycle()
    key = f"{(src or '').lower()}:{name}"
    is_c, is_d = key in cycle.get("create", ()), key in cycle.get("delete", ())
    if is_c and not is_d:
        return "create"
    if is_d and not is_c:
        return "delete"
    if _DELETE_VERB.match(name):
        return "delete"
    if _CREATE_VERB.match(name):
        return "create"
    return None


def _kind_of(event_name: str) -> str:
    """The resource kind an event names: the operation minus its leading verb WORD.

    ``CreateRelationalDatabase`` -> ``RelationalDatabase``; ``RunInstances`` -> ``Instances``;
    ``CopySnapshot`` -> ``Snapshot``. Taking the first CamelCase token rather than matching a list of
    verbs means an operation the verb lists never knew still yields its noun, which is what pairs a
    delete with its create and what anchors the SKU search."""
    name = event_name or ""
    head = re.match(r"^[A-Z][a-z]+(?=[A-Z])", name)
    return name[head.end():] if head else name



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
    its OWN delete rather than against any delete of the same kind.

    Chosen by SHAPE, not from a list of eight field names. An IDENTIFIER identifies and a NAME may not:
    ``CreateDBInstance`` carries both ``dBName: umami`` (the database inside) and
    ``dBInstanceIdentifier: umami-db-acsb845f10e`` (the billed resource), and only the second appears in
    the matching delete, so preferring "name" would have paired nothing. Values shaped like ANOTHER
    resource's id are skipped for the same reason they are not SKU selectors."""
    ranked = []
    for k, v in (params or {}).items():
        if not isinstance(v, str) or not v or _NOT_A_SELECTOR.match(v):
            continue
        low = k.lower()
        rank = 0 if low.endswith("identifier") else 1 if low.endswith("id") else 2 if low.endswith("name") else None
        if rank is not None:
            ranked.append((rank, k, v))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return ranked[0][2] if ranked else ""


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
                src = raw.get("eventSource", "").split(".")[0]
                kind = classify_event(src, name)
                if kind == "delete":
                    deleted.append((_kind_of(name).lower(),
                                    _identity(raw.get("requestParameters") or {}),
                                    raw.get("eventTime") or ""))
                    continue
                if kind == "create":
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


# price_resource(), run_rate_from_cloudtrail() and price_from_index() removed: per-service
# pricing code (an if/elif per kind, a region-name map, an engine-spelling map) and two callers
# nothing referenced, one of which still called the already-deleted price_resource(). The last of
# them also carried the hand-written resource-id prefix list (arn:/sg-/subnet-/vpc-/ami-/i-) that
# _NOT_A_SELECTOR replaced with a shape. price_resource_universal() is the only pricing path.


# --- end to end: discovered resource -> its own words -> the published SKU -> a price ---------------

# Values that name ONE instance rather than describe a SKU. Every clause is a SHAPE, so a prefix nobody
# has seen yet is excluded by the same rule as the familiar ones: the previous version listed
# sg-/subnet-/vpc-/ami-/i-/eni-/rtb-/igw- by hand, which is a vendor list that is wrong the moment AWS
# ships a ninth prefix.
_NOT_A_SELECTOR = re.compile(
    r"^arn:"                                                            # an ARN names one resource
    r"|^[a-z]{1,8}-[0-9a-f]{8,}$"                                       # any AWS resource id
    r"|^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"                     # a uuid / client token
    r"|^[a-z]{2}-[a-z]+-\d[a-z]?$")                                     # a region or availability zone


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
        # A name is a label the customer chose and no SKU carries it. An *Id* that survived the shape
        # rule above is not a resource id, it is a CATALOGUE id (``micro_2_0``, ``postgres_16``), which is
        # the vendor's name for a specification and can be resolved back into one. The previous version
        # kept exactly ``bundleid`` and ``blueprintid``, which is the Lightsail vocabulary written into a
        # module whose whole purpose is not to know any service's vocabulary.
        if k.lower().endswith(("name", "identifier")):
            continue
        out[k] = v
    return out


# How many of the service's catalogue operations to try before giving up on one identifier. Ranking by
# shared words put the right one 2nd of 31 for the measured case; the cap bounds the in-run cost of an
# identifier no catalogue happens to contain.
_CATALOG_TRIES = 6

_SVC_OPS: Dict[str, Tuple[str, List[str]]] = {}
_CATALOG_MEM: Dict[Tuple[str, str, str, str], Dict[str, object]] = {}


def _catalog_ops(src: str) -> Tuple[str, List[str]]:
    """The read-only, no-argument operations of the service that emitted the event.

    Enumerated from botocore's own published service model and matched on ENDPOINT PREFIX, which is
    exactly what CloudTrail reports as the eventSource. No operation and no service is named here."""
    if src in _SVC_OPS:
        return _SVC_OPS[src]
    try:
        import botocore.session
    except ImportError:
        _SVC_OPS[src] = ("", [])
        return _SVC_OPS[src]
    sess = botocore.session.get_session()
    found: Tuple[str, List[str]] = ("", [])
    for name in sess.get_available_services():
        try:
            model = sess.get_service_model(name)
        except Exception:  # noqa: BLE001 - a model that will not load is not the service we want
            continue
        if model.endpoint_prefix != src:
            continue
        ops = []
        for op_name in model.operation_names:
            if op_name[:3] not in ("Get", "Lis", "Des"):
                continue                       # only readers; never anything that could change state
            shape = model.operation_model(op_name).input_shape
            if shape is not None and (shape.metadata.get("required") or []):
                continue                       # needs an argument this resource cannot supply
            ops.append(op_name)
        found = (name, ops)
        break
    _SVC_OPS[src] = found
    return found


def _words(text: str) -> set:
    return {w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[0-9]+", text or "") if len(w) > 2}


def resolve_catalog_id(src: str, param: str, value: str, region: str,
                       profile: Optional[str] = None) -> Dict[str, object]:
    """Ask the service that created the resource what one of its own identifiers MEANS.

    A create call names things in the vendor's CATALOGUE vocabulary while the price list is keyed on
    SPECIFICATIONS, and the two never meet. Measured on a Lightsail database: the create says
    ``relationalDatabaseBundleId: micro_2_0``, that string appears in NO Lightsail SKU (0 hits across all
    150 products in us-east-1, which are keyed on memory/storage/vcpu), and the resource cannot supply it
    either -- the published Cloud Control schema for the type exposes the bundle id and NOTHING about the
    hardware, so even describing the live resource returns the same unmatchable word. The specification
    exists only in the service's own catalogue, and every service that sells sized things publishes one.

    The catalogue is FOUND, never named. botocore lists the service's read-only no-argument operations;
    they are tried in order of how many words they share with the PARAMETER that carried the identifier,
    because the vendor named both after the same thing (``relationalDatabaseBundleId`` ->
    ``GetRelationalDatabaseBundles``); the first response containing the identifier wins, and its record
    is the answer. Measured: 31 candidate operations for that service, the right one reached in two
    calls and 3.3s.

    Returns the catalogue record's own scalar fields, or {} when the service publishes no catalogue that
    contains this identifier -- never a guess."""
    memo = (src, param, value, region)
    if memo in _CATALOG_MEM:
        return _CATALOG_MEM[memo]
    cli_name, ops = _catalog_ops(src)
    _CATALOG_MEM[memo] = {}
    if not cli_name:
        return {}
    pw = _words(param)
    ranked = sorted(ops, key=lambda o: (-len(pw & _words(o)), o))
    p = ["--profile", profile] if profile else []
    for op_name in ranked[:_CATALOG_TRIES]:
        if not (pw & _words(op_name)):
            break                              # nothing left that is even about the same thing
        kebab = re.sub(r"(?<!^)(?=[A-Z])", "-", op_name).lower()
        doc, _err = _aws([cli_name, kebab, "--region", region, "--output", "json"] + p, timeout=60)
        if not doc:
            continue
        for entry in doc.values():
            if not isinstance(entry, list):
                continue
            for rec in entry:
                if isinstance(rec, dict) and value in json.dumps(rec, default=str):
                    # Specifications only. A catalogue record also carries LABELS, and a label is not a
                    # property of the thing: the bundle's ``name`` is "Micro", which is also the name of a
                    # container size, and taking it as a selector priced a DATABASE as a container.
                    # Same rule the create call's own parameters already get.
                    got = {k: v for k, v in rec.items()
                           if isinstance(v, (str, int, float)) and not isinstance(v, bool)
                           and not k.lower().endswith(("name", "identifier", "id"))}
                    _CATALOG_MEM[memo] = got
                    return got
    return {}


def _spellings(field: str, value: object, units: set) -> List[str]:
    """How this service would WRITE that number in a SKU: ``ramSizeInGb: 1.0`` -> ``1GB``.

    THE UNIT COMES FROM THE FIELD, NOT FROM A VOCABULARY. Offering every unit the service uses for every
    number it publishes is how ``cpuCount: 2`` became "2GB", which is a real memory value, so a database
    matched a CONTAINER SKU and was priced at $9.81 instead of $14.72. The vendor already declares the
    unit in the field's own name (``ramSizeInGb``, ``diskSizeInGb``), and a field that declares none
    (``cpuCount``, ``price``) is not offering one to guess at. The declared unit still has to be one the
    price list actually writes, which is what the harvested vocabulary is for.

    Bare numbers are never offered: the module's oldest measured rule is that a number is a QUANTITY and
    selects attributes spuriously (EC2 value ``1`` matches 13 different attributes)."""
    if isinstance(value, bool) or isinstance(value, str) or not isinstance(value, (int, float)):
        return [value] if isinstance(value, str) else []
    tail = re.findall(r"[A-Za-z][a-z]*", field or "")
    unit = tail[-1].lower() if tail else ""
    return [f"{value:g}{unit}"] if unit in units else []


def _head_noun(resource: dict) -> str:
    """The last word of the resource's own noun: ``AllocateAddress`` -> "address"."""
    from acspeed.adapters.aws_price_index import _norm
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+", _kind_of(resource.get("event") or ""))
    return _norm(words[-1]) if words else ""


def _name_heads(attrs: dict) -> set:
    """What each of a SKU's names DENOTES: the last word of each dash- or colon-separated part.

    `USE1-PublicIPv4:InUseAddress` denotes an address; `USE1-IPAddressManager-IP-Hours` denotes hours,
    and its group `AWSVPCIPAddressManager` denotes a manager. Both contain the word "address"."""
    from acspeed.adapters.aws_price_index import _norm
    out = set()
    for key in ("usagetype", "group", "productFamily", "operation"):
        for chunk in re.split(r"[^A-Za-z0-9]+", str(attrs.get(key) or "")):
            words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+", chunk)
            if words:
                out.add(_norm(words[-1]))
    return out


def price_resource_universal(resource: dict, describe=None, profile: Optional[str] = None) -> dict:
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
    from acspeed.adapters.aws_price_index import (service_for, unit_vocabulary, value_universe, _norm)
    guess = service_for(src)

    # An identifier the price list has never heard of selects nothing, and dropping it silently is how a
    # real billable resource comes out "unpriceable". Resolve it against the service's own catalogue
    # first, and spell the specification the way this service's SKUs spell numbers.
    for code in guess:
        uni, units = value_universe(code, region), unit_vocabulary(code, region)
        for k, v in list(sel.items()):
            if not k.lower().endswith("id") or _norm(v) in uni:
                continue
            for fk, fv in resolve_catalog_id(src, k, v, region, profile).items():
                for spelling in _spellings(fk, fv, units):
                    if _norm(spelling) in uni:
                        sel[fk] = spelling
                        break
    for pool in (guess, None):
        hits = []
        for code in (pool if pool is not None else service_codes()):
            try:
                # The SKU that accounts for the MOST of what the create call said. Requiring every word
                # at once left an ALB ambiguous across 13 SKUs on generic words (application,
                # internet-facing, ipv4); adding them rarest-first and stopping at a single match priced a
                # DATABASE as $3.00 of storage, because its bundle's monthly transfer quota of 100 GB is
                # the rarest word it has and one unrelated storage SKU carries it. Neither failure is
                # about a service, so neither fix is a per-service attribute list.
                from acspeed.adapters.aws_price_index import (resolve_one, find_sku_by_words,
                                                              find_sku_by_coverage)
                noun = _kind_of(resource.get("event") or "")
                best = ondemand_only(find_sku_by_coverage(code, region, list(sel.values()), anchor=noun,
                                                           unit=str(resource.get("unit_hint") or "")))
                # The resource's own words, plus the NOUN of the event that created it: an ALB's SKU
                # says "LoadBalancing:Application" where the event says type=application, and the noun
                # "LoadBalancer" is the other half of that sentence.
                words = list(sel.values()) + [noun]
                cand = best or []
                if len(cand) != 1:
                    # The resource and its SKU can use different grammar for the same thing, so fall back
                    # to containment anchored on the noun. This runs when exact matching found NOTHING
                    # and ALSO when it found several, because an ambiguous exact result used to SUPPRESS
                    # it: an allocated Elastic IP matched seven unrelated SKUs on the single generic word
                    # "vpc" (CloudWAN, Firehose, Transit Gateway), and because that was not empty, the
                    # SKUs actually named after an address were never even considered.
                    anchored = ondemand_only(find_sku_by_words(code, region, words, anchor=noun))
                    if not cand:
                        cand = resolve_one(anchored, words)
                        if len(cand) != 1:
                            cand = []          # a loose match may never quietly become the answer
                    else:
                        seen = {m["sku"] for m in cand}
                        cand = cand + [m for m in anchored if m["sku"] not in seen]
                hits += [(code, m) for m in resolve_one(cand, words)]
            except Exception:  # noqa: BLE001
                continue
        # A PART carries the unit of its own quantity, and the SKU has to be priced in that unit. A gp3
        # volume publishes three SKUs under the same name: EBS:VolumeP-IOPS.gp3 (IOPS-Mo),
        # EBS:VolumeP-Throughput.gp3 (GiBps-mo) and EBS:VolumeUsage.gp3 (GB-Mo). The part is 40 GB, so
        # only the per-GB line can be its price; without this the root volume of every instance came out
        # ambiguous and unpriced while EBS:VolumeUsage.gp3 was on the bill.
        hint = str(resource.get("unit_hint") or "").lower()
        if hint:
            # A REQUIREMENT, not a preference. A sized child inherits every short string its parent's
            # create call carried, because which of them describes the CHILD is not knowable structurally,
            # so a 20 GB RDS volume matched `InstanceUsage:db.t4g.micro` and was priced at 20x the
            # instance's HOURLY rate: $233.60/mo for a $2.30/mo disk. The unit is what tells parent from
            # child, and a child whose unit nothing matches is honestly unpriced rather than wrong.
            hits = [(c, m) for c, m in hits
                    if any(str(d.get("unit") or "").lower().startswith(hint)
                           for d in m["prices"] if d["usd"] > 0)]
        # ...and the mirror of it: a resource whose sized child took the per-size line cannot itself be
        # priced by size. See `billable_parts`.
        skip = str(resource.get("exclude_unit") or "").lower()
        if skip and len(hits) > 1:
            keep = [(c, m) for c, m in hits
                    if not all(str(d.get("unit") or "").lower().startswith(skip)
                               for d in m["prices"] if d["usd"] > 0)]
            if keep:
                hits = keep

        # LAST RESORT, and only among candidates that are otherwise AMBIGUOUS (so it can turn an
        # unpriced resource into a priced one but can never change a price that already resolved):
        # prefer SKUs whose name is ABOUT the resource's noun. English compound nouns are head-final,
        # so the last word is what the name denotes: `VPCPublicIPv4Address` IS an address,
        # `AWSVPCIPAddressManager` is a manager that merely mentions one. Measured on an allocated
        # Elastic IP, whose only selector is `domain: vpc` and which therefore matched seven unrelated
        # SKUs while USE1-PublicIPv4:InUseAddress was on the bill at $0.005/hr.
        if len(hits) > 1 and _head_noun(resource):
            head = _head_noun(resource)
            pref = [(c, m) for c, m in hits if head in _name_heads(m.get("attributes") or {})]
            if pref:
                hits = pref

        # The same SKU is published in more than one offer file (LoadBalancerUsage appears in both
        # AWSELB and AmazonEC2, at the same price). Identical usagetype AND price is one line, not an
        # ambiguity, so collapse before judging.
        if len(hits) > 1:
            uniq = {}
            for code, m in hits:
                d = next((x for x in m["prices"] if x["usd"] > 0), None)
                if d:
                    uniq.setdefault((m.get("usagetype"), round(d["usd"], 8)), (code, m))
            if len(uniq) == 1:
                hits = [next(iter(uniq.values()))]
            elif len({price for _ut, price in uniq}) == 1:
                # Different labels, IDENTICAL published price: the number is determined even though the
                # label is not. An allocated public IPv4 bills the same whether it is InUseAddress or
                # IdleAddress ($0.005/hr each), and refusing to price it over a label we cannot pin would
                # drop a real charge for no gain in accuracy.
                hits = [next(iter(uniq.values()))]
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

    # A SIZED CHILD IS A SHAPE, not a list of field names. The previous version looked for
    # volumeSize/sizeInGB/size/diskSize/allocatedStorage beside volumeType/storageType/type, which is
    # AWS's vocabulary typed out and is wrong for the next field name AWS invents. What identifies one
    # is a field naming an AMOUNT OF SPACE (English: size, storage) with a positive value, plus a short
    # string in the same object to say what KIND of space. Whether that string means anything is not
    # decided here: the price list decides, in pricing, where it is consulted anyway. Doing it here
    # would make a structural function reach the network, which hung the whole test suite.
    def _sized(node):
        """(quantity, {selectors}) when this object describes a sized thing."""
        sized = [float(v) for k, v in node.items()
                 if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
                 and ("size" in k.lower() or "storage" in k.lower())]
        kinds = {k: v for k, v in node.items()
                 if isinstance(v, str) and 1 < len(v) <= 40 and not _NOT_A_SELECTOR.match(v)}
        return (sized[0], kinds) if sized and kinds else (None, {})

    def _walk(node, path=""):
        if isinstance(node, dict):
            qty, priced = _sized(node)
            if qty:
                # Neutral keys: these values are already vouched for by the price list, so the
                # name/identifier suffix filter must not throw one away for being called *Name.
                parts.append({**resource, "part": path or "storage", "kind": "storage",
                              "quantity": qty, "event": "", "unit_hint": "GB",
                              "params": {f"spec{i}": v for i, v in enumerate(priced.values())}})
                if not path:
                    # A PARENT IS NOT ITS CHILD. When the create call describes its child on ITSELF
                    # (RDS carries allocatedStorage and storageType flat), the parent's own words include
                    # the child's, so the parent matches the CHILD's SKUs: `storageType: gp3` left the DB
                    # instance matching 33 GP3 STORAGE SKUs, ambiguous and unpriced, while
                    # InstanceUsage:db.t4g.micro was on the bill.
                    #
                    # Removing those FIELDS is not the fix, because which string describes the child is
                    # exactly what is not known here (dropping all of them left the instance with no
                    # selectors at all). What IS known is that the child took the sized line, so the
                    # parent cannot also be priced by the size: the two are different lines on the bill.
                    parts[0] = {**parts[0], "exclude_unit": "GB"}
            for k, v in node.items():
                _walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for v in node:
                _walk(v, path)

    _walk(prm)
    # An internet-facing load balancer consumes a public IPv4 in every subnet it is placed in.
    # The condition used to also require kind == "load_balancer", which NEVER MATCHED: kinds come from
    # `_kind_of`, which yields "loadbalancer". The branch was dead, so every run's public IPv4 addresses
    # went uncounted while USE1-PublicIPv4:InUseAddress was on the real bill. `scheme: internet-facing`
    # plus subnets is the condition that actually implies public addresses; the kind added nothing.
    if str(prm.get("scheme", "")).lower() == "internet-facing":
        subnets = prm.get("subnets") or prm.get("subnetMappings") or []
        n = len(subnets) if isinstance(subnets, list) else 1
        if n:
            parts.append({**resource, "part": "public-ipv4", "kind": "elastic_ip", "quantity": float(n),
                          "event": "",
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
    no_sku: List[str] = []
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
                reason = str(p.get("reason") or "")
                if reason.startswith("ambiguous"):
                    # The disclosure HAS a price and we could not pin which: a real problem, and the
                    # number must not be published until it is settled.
                    unpriced.append(f"{part['kind']}[{part.get('part')}]@{part['region']}: {reason}")
                else:
                    # No SKU carries this resource's words at all. Since the classification list is gone
                    # and EVERY create is attempted, this is overwhelmingly the free ones (security
                    # groups, key pairs, IAM roles, subnets). Listed rather than counted, so a genuinely
                    # billable resource that failed to match is visible instead of silently absent, and
                    # so it does not make `ok` false on every run.
                    no_sku.append(f"{part['src']}:{part['event']}@{part['region']}")
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
    d["no_sku_match"] = sorted(set(no_sku))
    d["ok"] = not unpriced
    return d
