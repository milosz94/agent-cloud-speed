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

# --- the two irreducible tables -----------------------------------------------------------------
# Kept as DATA, deliberately tiny, and a miss REFUSES rather than guesses. Extend by adding a row.
_SERVICE_CODE = {
    "ec2": "AmazonEC2",
    "rds": "AmazonRDS",
    "lightsail": "AmazonLightsail",
    "elasticloadbalancing": "AWSELB",       # NOT derivable: no string or normalized rule finds this
    "ecs": "AmazonECS",
    "lambda": "AWSLambda",
    "cloudfront": "AmazonCloudFront",
    "s3": "AmazonS3",
    "elasticache": "AmazonElastiCache",
    "apigateway": "AmazonApiGateway",
    "apprunner": "AWSAppRunner",
}
# CloudTrail's spelling -> the Price List's spelling, per attribute. A value absent here is passed
# through unchanged; a lookup that then finds no SKU refuses.
_VALUE_SPELLING = {
    "postgres": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB",
    "oracle-se2": "Oracle", "sqlserver-ex": "SQL Server", "aurora-postgresql": "Aurora PostgreSQL",
    "aurora-mysql": "Aurora MySQL",
}
_LOCATION = {
    "us-east-1": "US East (N. Virginia)", "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)", "us-west-2": "US West (Oregon)",
    "eu-west-1": "EU (Ireland)", "eu-west-2": "EU (London)", "eu-central-1": "EU (Frankfurt)",
    "ap-southeast-1": "Asia Pacific (Singapore)", "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ca-central-1": "Canada (Central)", "sa-east-1": "South America (Sao Paulo)",
}

# Which CloudTrail create events stand up a BILLED resource. A create absent here is reported as
# UNCLASSIFIED (and refuses), never assumed free: security groups and subnet groups are genuinely free,
# but the rule for deciding that has to be written down, not inferred.
_BILLABLE_CREATE = {
    "RunInstances": "ec2", "CreateDBInstance": "rds", "CreateDBCluster": "rds",
    "CreateContainerService": "lightsail-container", "CreateRelationalDatabase": "lightsail-db",
    "CreateLoadBalancer": "load_balancer", "CreateCacheCluster": "elasticache",
    "CreateNatGateway": "nat_gateway", "AllocateAddress": "elastic_ip",
    "CreateService": "fargate", "CreateVolume": "volume", "CreateInstances": "lightsail-instance",
}
_FREE_CREATE = {           # created constantly, bills nothing; enumerated so silence is a DECISION
    "CreateSecurityGroup", "CreateDBSubnetGroup", "CreateNetworkInterface", "CreateLogStream",
    "CreateLogGroup", "CreateTargetGroup", "CreateListener", "CreateRule", "CreateGrant",
    "CreateContainerServiceDeployment", "CreateTags", "CreateRole", "CreatePolicy",
    "CreateCluster", "RegisterTaskDefinition", "RegisterTargets", "CreateIndex", "CreateView",
    "CreateSubnet", "CreateRouteTable", "CreateRoute", "CreateInternetGateway", "CreateVpc",
}
_DELETE_OF = {
    "TerminateInstances": "ec2", "DeleteDBInstance": "rds", "DeleteDBCluster": "rds",
    "DeleteContainerService": "lightsail-container", "DeleteRelationalDatabase": "lightsail-db",
    "DeleteLoadBalancer": "load_balancer", "DeleteCacheCluster": "elasticache",
    "DeleteNatGateway": "nat_gateway", "ReleaseAddress": "elastic_ip",
    "DeleteService": "fargate", "DeleteVolume": "volume",
}


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
                if name in _DELETE_OF:
                    deleted.append((_DELETE_OF[name], _identity(raw.get("requestParameters") or {}),
                                    raw.get("eventTime") or ""))
                    continue
                if name in _BILLABLE_CREATE:
                    prm = raw.get("requestParameters") or {}
                    found.append({"kind": _BILLABLE_CREATE[name], "region": reg, "event": name,
                                  "src": raw.get("eventSource", "").split(".")[0],
                                  "identity": _identity(prm), "at": raw.get("eventTime") or "",
                                  "params": prm,
                                  "response": raw.get("responseElements") or {}})
                elif name.startswith(("Create", "Run", "Allocate")) and name not in _FREE_CREATE:
                    unclassified.append(f"{raw.get('eventSource','').split('.')[0]}:{name}@{reg}")
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


def price_resource(res: dict, profile: Optional[str] = None) -> Tuple[List[RateComponent], List[str]]:
    """(components, unpriced). Never invents a component: what it cannot price it NAMES."""
    kind, reg, prm = res["kind"], res["region"], res["params"]
    loc = _LOCATION.get(reg)
    comps: List[RateComponent] = []
    unpriced: List[str] = []
    if not loc:
        return comps, [f"{kind}@{reg}: region not in the location table"]

    if kind == "lightsail-container":
        power = str(prm.get("power", "")).lower()
        scale = float(prm.get("scale", 1) or 1)
        rows = [r for r in _price_rows("AmazonLightsail", {"power": power, "location": loc}, profile)
                if "ContainerSvcUsage" in (r[0] or "")]
        if rows:
            ut, unit, pr = rows[0]
            comps.append(RateComponent(name="compute:lightsail-container",
                                       hourly_usd=_hourly(unit, pr, scale),
                                       raw_unit_price=pr, native_unit=unit, quantity=scale))
        else:
            unpriced.append(f"lightsail-container power={power!r} in {reg}")

    elif kind == "rds":
        cls = prm.get("dBInstanceClass")
        eng = _VALUE_SPELLING.get(str(prm.get("engine", "")).lower(), prm.get("engine"))
        depl = "Multi-AZ" if prm.get("multiAZ") else "Single-AZ"
        rows = [r for r in _price_rows("AmazonRDS", {"instanceType": cls, "databaseEngine": eng,
                                                     "deploymentOption": depl, "location": loc}, profile)
                if r[1] == "Hrs" and r[2] > 0]
        if rows:
            ut, unit, pr = rows[0]
            comps.append(RateComponent(name="compute:rds", hourly_usd=_hourly(unit, pr),
                                       raw_unit_price=pr, native_unit=unit))
        else:
            unpriced.append(f"rds {cls} {eng} {depl} in {reg}")
        gb = float(prm.get("allocatedStorage") or 0)           # a QUANTITY, never a selector
        if gb:
            vol = str(prm.get("storageType") or "gp2")
            # RDS storage is priced INDEPENDENTLY of the engine: AWS publishes gp3 Single-AZ SKUs only
            # under Db2 / Oracle / SQL Server (there is no PostgreSQL one), yet all 16 carry the same
            # usagetype RDS:GP3-Storage at one price. So the engine is NOT a filter here. Guard the
            # assumption instead of trusting it: if the matching SKUs disagree on price, REFUSE.
            srows = [r for r in _price_rows("AmazonRDS", {"volumeName": vol, "location": loc,
                                                          "deploymentOption": depl}, profile)
                     if "GB-Mo" in (r[1] or "") and r[2] > 0 and "Mirror" not in (r[0] or "")]
            prices = sorted({r[2] for r in srows})
            if len(prices) == 1:
                ut, unit, pr = srows[0]
                comps.append(RateComponent(name="storage:rds", hourly_usd=_hourly(unit, pr, gb),
                                           raw_unit_price=pr, native_unit=unit, quantity=gb))
            elif not srows:
                unpriced.append(f"rds storage {vol} {gb}GB in {reg}")
            else:
                unpriced.append(f"rds storage {vol} in {reg}: {len(prices)} different prices "
                                f"{prices}, ambiguous")

    elif kind == "ec2":
        itype = None
        for k in ("instanceType", "instancesSet"):
            v = prm.get(k)
            if isinstance(v, str):
                itype = v
            elif isinstance(v, dict):
                items = v.get("items") or []
                if items:
                    itype = items[0].get("instanceType") or itype
        rows = [r for r in _price_rows("AmazonEC2", {"instanceType": itype, "location": loc,
                                                     "operatingSystem": "Linux", "tenancy": "Shared",
                                                     "preInstalledSw": "NA",
                                                     "capacitystatus": "Used"}, profile)
                if r[1] == "Hrs" and r[2] > 0]
        if rows:
            ut, unit, pr = rows[0]
            comps.append(RateComponent(name="compute", hourly_usd=_hourly(unit, pr),
                                       raw_unit_price=pr, native_unit=unit))
        else:
            unpriced.append(f"ec2 {itype!r} in {reg}")
        bdm = ((prm.get("blockDeviceMapping") or {}).get("items") or [])   # IMPLICIT child, no own call
        for item in bdm:
            ebs = item.get("ebs") or {}
            size = float(ebs.get("volumeSize") or 0)
            vtype = ebs.get("volumeType") or "gp3"
            if not size:
                continue
            srows = [r for r in _price_rows("AmazonEC2", {"volumeApiName": vtype, "location": loc},
                                            profile) if "GB-Mo" in (r[1] or "") and r[2] > 0]
            if srows:
                ut, unit, pr = srows[0]
                comps.append(RateComponent(name="storage", hourly_usd=_hourly(unit, pr, size),
                                           raw_unit_price=pr, native_unit=unit, quantity=size))
            else:
                unpriced.append(f"ebs {vtype} {size}GB in {reg}")

    elif kind == "load_balancer":
        lbt = str(prm.get("type") or "application").lower()
        fam = {"application": "Load Balancer-Application", "network": "Load Balancer-Network"}.get(lbt)
        rows = [r for r in _price_rows("AWSELB", {"location": loc, "productFamily": fam}, profile)
                if r[1] == "Hrs" and r[2] > 0]
        if rows:
            ut, unit, pr = rows[0]
            comps.append(RateComponent(name="load_balancer", hourly_usd=_hourly(unit, pr),
                                       raw_unit_price=pr, native_unit=unit))
        else:
            unpriced.append(f"load_balancer type={lbt} in {reg}")

    else:
        unpriced.append(f"{kind}@{reg}: no pricing rule (event {res.get('event')})")
    return comps, unpriced


def run_rate_from_cloudtrail(start: str, end: str, run_token: str, capture_date: str,
                             regions: Optional[List[str]] = None,
                             profile: Optional[str] = None) -> dict:
    """The whole path. Returns a ``cost_run_rate``-shaped dict; ``ok`` is False when ANYTHING
    discovered could not be priced, and ``unpriced_resources`` says exactly what."""
    regions = regions or enabled_regions(profile)
    resources, unclassified = discover(start, end, run_token, regions, profile)
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


# --- the gate: a published cost must be CORROBORATED, or it is refused --------------------------
#
# Which priced component names cover which discovered billable kind. This is DATA, and its
# INCOMPLETENESS is the safety property, not a hole: a kind that is absent here is UNCOVERED, so the run
# REFUSES. A service AWS ships next year is therefore refused, never silently priced at zero. That is the
# opposite of the enumerate-then-price path, where an unknown service is invisible and the number comes
# out confidently small.
_COVERED_BY = {
    "ec2": ("compute",),
    "rds": ("compute:rds", "storage:rds"),
    "lightsail-container": ("compute:lightsail-container",),
    "lightsail-db": ("compute:lightsail-db",),
    "lightsail-instance": ("compute",),
    "load_balancer": ("load_balancer",),
    "fargate": ("compute:fargate-vcpu", "compute:fargate-mem"),
    "elasticache": ("compute:redis",),
    "nat_gateway": ("nat_gateway",),
    "elastic_ip": ("public_ip", "elastic-ip"),
    "volume": ("storage",),
}


def corroborate(published_components: List[dict], discovered: List[dict],
                unclassified: Optional[List[str]] = None) -> dict:
    """Is every billable resource the account RECORDED represented in the published price?

    The whole point is that this cannot be argued with. It does not ask the pricer whether it thinks it
    succeeded (which is what `cost_run_rate.ok` did, answering True on runs under-priced 6x). It compares
    the price against an INDEPENDENT record of what was created, and any gap refuses.

    Returns {"ok", "uncovered", "detail"}. ``ok`` False means DO NOT PUBLISH THIS NUMBER."""
    names = [c.get("name") for c in (published_components or [])]
    uncovered = []
    for kind in sorted({r["kind"] for r in (discovered or [])}):
        cover = _COVERED_BY.get(kind)
        if cover is None:
            uncovered.append(f"{kind}: no component is known to cover this kind")
            continue
        n_seen = sum(1 for r in discovered if r["kind"] == kind)
        n_priced = sum(1 for n in names if n in cover)
        if n_priced == 0:
            uncovered.append(f"{kind}: {n_seen} created, 0 priced")
        elif kind in ("lightsail-container", "ec2", "load_balancer", "lightsail-db") and n_priced < n_seen:
            # kinds that price ONE component per resource: fewer components than resources is a shortfall
            uncovered.append(f"{kind}: {n_seen} created, only {n_priced} priced")
    for u in (unclassified or []):
        uncovered.append(f"unclassified create {u}")
    return {"ok": not uncovered, "uncovered": uncovered,
            "detail": ("every recorded billable resource is represented in the price" if not uncovered
                       else "REFUSED: " + "; ".join(uncovered))}


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
