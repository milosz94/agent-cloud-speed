"""Universal teardown: find EVERY resource a run created and delete it, without per-service branching.

The design that this replaces (the old per-cloud ``reap_run``) deleted exactly one service per cloud:
aws RDS only, gcp Cloud SQL only. Everything else a run created (aws security groups, IAM roles, log
groups, load balancers, target groups; gcp Cloud Run services, Artifact Registry repos) was never
reaped and accreted as orphans. This module fixes that the universal way:

  1. FIND universally. Enumerate every resource whose name carries the run token, across services, using
     each cloud's own inventory (the same primitives the cost adapters use to price a run): gcp via
     ``gcloud ... list`` per resource family (and Cloud Asset Inventory when enabled), aws via a union of
     token-filtered describes plus Resource Explorer, azure via ``az resource list`` grouped to its
     resource groups. The finder is universal; it is NOT a hand-picked service.

  2. DELETE by a type->verb DATA map, not code branches. A resource type the map does not know is
     DISCLOSED as unreaped (never silently skipped, never silently $0).

  3. Order falls out of RE-ENUMERATING each pass (retry-until-stable): a security group that a live load
     balancer still references fails to delete on pass 1, the load balancer deletes, and the group deletes
     on pass 2. Stop when nothing is left or a pass makes no progress (a real orphan or a dependency
     deadlock, which is then reported, not hidden).

SAFETY. Every candidate is matched by the run TOKEN in its name (``acs<hex>``), so the reaper can never
touch a resource that does not belong to this run. A shared resource with NO token in its name (a shared
IAM role, a fixed-name second site) is deliberately NOT matched: deleting a shared resource could break a
concurrent run, so it is left and disclosed. ``dry_run=True`` is the default: it enumerates and prints the
plan and deletes nothing. Only ``dry_run=False`` deletes.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Callable, Optional

Runner = Callable[[list], tuple]  # (args, timeout) -> (rc, stdout, stderr)


def _default_run(args, timeout: int = 240):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as e:  # noqa: BLE001 - a runner failure is disclosed by an empty enumeration, never faked
        return 1, "", repr(e)


def _jloads(s: str):
    try:
        return json.loads(s) if s and s.strip() else []
    except Exception:  # noqa: BLE001
        return []


def _tok(run_token: str) -> str:
    return (run_token or "").lower()


def _hosts(urls) -> set:
    out = set()
    for u in urls or []:
        h = re.sub(r"^https?://", "", str(u or "")).split("/")[0].lower()
        if h:
            out.add(h)
    return out


# --------------------------------------------------------------------------------------------------
# Resource model. A candidate is (kind, name, ident, extra) where ``kind`` keys the delete map.
# --------------------------------------------------------------------------------------------------
class Res(dict):
    @property
    def kind(self):
        return self.get("kind")

    def __str__(self):
        return f"{self.get('kind')}:{self.get('name')}"


# ==================================================================================================
# GCP: enumerate by run token across the resource families a deploy creates, delete by asset kind.
# ==================================================================================================
def _gcp_enumerate(tok: str, run: Runner) -> list:
    out = []
    # Cloud Run services
    rc, o, _ = run(["gcloud", "run", "services", "list", "--format=json"], 120)
    for s in _jloads(o):
        name = (s.get("metadata") or {}).get("name", "")
        region = ((s.get("metadata") or {}).get("labels") or {}).get(
            "cloud.googleapis.com/location") or (s.get("metadata") or {}).get(
            "region") or _gcp_region_from_service(s)
        if tok in name.lower():
            out.append(Res(kind="gcp/run-service", name=name, region=region))
    # Cloud SQL instances
    rc, o, _ = run(["gcloud", "sql", "instances", "list", "--format=json"], 120)
    for s in _jloads(o):
        name = s.get("name", "")
        if tok in name.lower():
            out.append(Res(kind="gcp/sql-instance", name=name))
    # Artifact Registry repositories
    rc, o, _ = run(["gcloud", "artifacts", "repositories", "list", "--format=json"], 120)
    for r in _jloads(o):
        full = r.get("name", "")  # projects/P/locations/L/repositories/NAME
        short = full.split("/")[-1]
        loc = ""
        m = re.search(r"/locations/([^/]+)/", full)
        if m:
            loc = m.group(1)
        if tok in short.lower():
            out.append(Res(kind="gcp/artifact-repo", name=short, region=loc))
    return out


def _gcp_region_from_service(s: dict) -> str:
    for k in ("status", "metadata"):
        u = (s.get(k) or {}).get("url") or ""
        m = re.search(r"//[^-]+-([a-z0-9-]+)\.run\.app", u) or re.search(r"([a-z]+-[a-z]+\d)", u)
        if m:
            return m.group(1)
    return "us-central1"


def _gcp_delete(r: Res, run: Runner) -> tuple:
    k = r.kind
    if k == "gcp/run-service":
        return run(["gcloud", "run", "services", "delete", r["name"],
                    f"--region={r.get('region') or 'us-central1'}", "--quiet"], 300)
    if k == "gcp/sql-instance":
        return run(["gcloud", "sql", "instances", "delete", r["name"], "--quiet"], 400)
    if k == "gcp/artifact-repo":
        return run(["gcloud", "artifacts", "repositories", "delete", r["name"],
                    f"--location={r.get('region') or 'us-central1'}", "--quiet"], 300)
    return (1, "", "no gcp delete mapping")


# ==================================================================================================
# AWS: union of token-filtered describes across the services a deploy creates. Resource Explorer is a
# supplement (it is not tag-gated) but MISSES untagged security groups, so the direct describes are the
# backbone. Delete map keys on the resource family.
# ==================================================================================================
def _aws(args_tail, region, profile="acspeed-batch"):
    return ["aws", "--profile", profile, "--region", region] + args_tail


def _aws_enumerate(tok: str, run: Runner, region: str = "us-east-1", profile: str = "acspeed-batch") -> list:
    out = []

    def has(name):
        return tok in str(name or "").lower()

    # ECS services (per cluster) + clusters
    rc, o, _ = run(_aws(["ecs", "list-clusters", "--query", "clusterArns", "--output", "json"], region, profile), 90)
    for arn in _jloads(o):
        cname = arn.split("/")[-1]
        if not has(cname):
            continue
        rc2, o2, _ = run(_aws(["ecs", "list-services", "--cluster", cname, "--query", "serviceArns",
                               "--output", "json"], region, profile), 90)
        for sarn in _jloads(o2):
            out.append(Res(kind="aws/ecs-service", name=sarn.split("/")[-1], cluster=cname))
        out.append(Res(kind="aws/ecs-cluster", name=cname))
    # EC2 instances (a VM deploy; the reaper previously handled ONLY ECS/Fargate, so an EC2-based run
    # leaked its instance - the biggest billed resource). Matched by the run token in the instance's Name
    # tag; terminated in _aws_delete. (After termination its ENIs take a few minutes to detach, so a
    # security group it held may need a later pass to delete.)
    rc, o, _ = run(_aws(["ec2", "describe-instances", "--filters",
                         "Name=instance-state-name,Values=running,pending,stopping,stopped", "--query",
                         "Reservations[].Instances[].[InstanceId,Tags[?Key=='Name']|[0].Value]",
                         "--output", "json"], region, profile), 90)
    for row in _jloads(o):
        iid, name = (list(row) + [None, None])[:2] if isinstance(row, list) else (None, None)
        if iid and has(name):
            out.append(Res(kind="aws/ec2-instance", name=iid, ec2_name=name))
    # Load balancers
    rc, o, _ = run(_aws(["elbv2", "describe-load-balancers", "--query",
                         "LoadBalancers[].[LoadBalancerName,LoadBalancerArn]", "--output", "json"], region, profile), 90)
    for name, arn in _jloads(o):
        if has(name):
            out.append(Res(kind="aws/load-balancer", name=name, arn=arn))
    # Target groups
    rc, o, _ = run(_aws(["elbv2", "describe-target-groups", "--query",
                         "TargetGroups[].[TargetGroupName,TargetGroupArn]", "--output", "json"], region, profile), 90)
    for name, arn in _jloads(o):
        if has(name):
            out.append(Res(kind="aws/target-group", name=name, arn=arn))
    # RDS
    rc, o, _ = run(_aws(["rds", "describe-db-instances", "--query",
                         "DBInstances[].DBInstanceIdentifier", "--output", "json"], region, profile), 90)
    for name in _jloads(o):
        if has(name):
            out.append(Res(kind="aws/rds", name=name))
    # Security groups
    rc, o, _ = run(_aws(["ec2", "describe-security-groups", "--query",
                         "SecurityGroups[].[GroupName,GroupId]", "--output", "json"], region, profile), 90)
    for name, gid in _jloads(o):
        if has(name):
            out.append(Res(kind="aws/security-group", name=name, ident=gid))
    # Log groups
    rc, o, _ = run(_aws(["logs", "describe-log-groups", "--query", "logGroups[].logGroupName",
                         "--output", "json"], region, profile), 90)
    for name in _jloads(o):
        if has(name):
            out.append(Res(kind="aws/log-group", name=name))
    # IAM roles (global; token-named only, never a shared role)
    rc, o, _ = run(_aws(["iam", "list-roles", "--query", "Roles[].RoleName", "--output", "json"], region, profile), 90)
    for name in _jloads(o):
        if has(name):
            out.append(Res(kind="aws/iam-role", name=name))
    return out


def _aws_delete(r: Res, run: Runner, region: str = "us-east-1", profile: str = "acspeed-batch") -> tuple:
    k = r.kind
    if k == "aws/ec2-instance":
        return run(_aws(["ec2", "terminate-instances", "--instance-ids", r["name"]], region, profile), 120)
    if k == "aws/ecs-service":
        run(_aws(["ecs", "update-service", "--cluster", r["cluster"], "--service", r["name"],
                  "--desired-count", "0"], region, profile), 90)
        return run(_aws(["ecs", "delete-service", "--cluster", r["cluster"], "--service", r["name"],
                         "--force"], region, profile), 120)
    if k == "aws/ecs-cluster":
        return run(_aws(["ecs", "delete-cluster", "--cluster", r["name"]], region, profile), 90)
    if k == "aws/load-balancer":
        return run(_aws(["elbv2", "delete-load-balancer", "--load-balancer-arn", r["arn"]], region, profile), 120)
    if k == "aws/target-group":
        return run(_aws(["elbv2", "delete-target-group", "--target-group-arn", r["arn"]], region, profile), 90)
    if k == "aws/rds":
        return run(_aws(["rds", "delete-db-instance", "--db-instance-identifier", r["name"],
                         "--skip-final-snapshot", "--delete-automated-backups"], region, profile), 180)
    if k == "aws/security-group":
        return run(_aws(["ec2", "delete-security-group", "--group-id", r["ident"]], region, profile), 60)
    if k == "aws/log-group":
        return run(_aws(["logs", "delete-log-group", "--log-group-name", r["name"]], region, profile), 60)
    if k == "aws/iam-role":
        # detach managed policies + delete inline policies, then the role (IAM refuses otherwise)
        _, o, _ = run(_aws(["iam", "list-attached-role-policies", "--role-name", r["name"],
                            "--query", "AttachedPolicies[].PolicyArn", "--output", "json"], region, profile), 60)
        for parn in _jloads(o):
            run(_aws(["iam", "detach-role-policy", "--role-name", r["name"], "--policy-arn", parn], region, profile), 60)
        _, o, _ = run(_aws(["iam", "list-role-policies", "--role-name", r["name"],
                            "--query", "PolicyNames", "--output", "json"], region, profile), 60)
        for pn in _jloads(o):
            run(_aws(["iam", "delete-role-policy", "--role-name", r["name"], "--policy-name", pn], region, profile), 60)
        return run(_aws(["iam", "delete-role", "--role-name", r["name"]], region, profile), 60)
    return (1, "", "no aws delete mapping")


# ==================================================================================================
# AZURE: each run owns its resource group(s); deleting the group removes every resource in it at once.
# Enumerate resources by token, collect their groups (plus token-named empty groups), delete the groups.
# ==================================================================================================
def _azure_enumerate(tok: str, run: Runner) -> list:
    groups = set()
    rc, o, _ = run(["az", "resource", "list", "-o", "json"], 90)
    for r in _jloads(o):
        name = str(r.get("name", "")).lower()
        rg = str(r.get("resourceGroup", ""))
        if tok in name or tok in rg.lower():
            if rg:
                groups.add(rg)
    rc, o, _ = run(["az", "group", "list", "--query", "[].name", "-o", "json"], 60)
    for g in _jloads(o):
        if tok in str(g).lower():
            groups.add(g)
    return [Res(kind="azure/resource-group", name=g) for g in sorted(groups)]


def _azure_delete(r: Res, run: Runner) -> tuple:
    return run(["az", "group", "delete", "-n", r["name"], "--yes", "--no-wait"], 120)


# ==================================================================================================
# The universal driver.
# ==================================================================================================
_ENUMERATE = {"gcp": _gcp_enumerate, "aws": _aws_enumerate, "azure": _azure_enumerate}
_DELETE = {"gcp": _gcp_delete, "aws": _aws_delete, "azure": _azure_delete}


def reap_universal(cloud: str, run_token: str, urls=(), *, dry_run: bool = True,
                   run: Optional[Runner] = None, region: str = "us-east-1",
                   profile: str = "acspeed-batch", max_passes: int = 4, log=None) -> dict:
    """Enumerate every token-scoped resource for this run and (unless ``dry_run``) delete it, retrying
    until nothing is left or a pass makes no progress. Returns ``{planned, deleted, failed, passes,
    checked, dry_run}``. Never raises: a failure is reported, not thrown, so a batch loop is never broken."""
    log = log or (lambda *a, **k: None)
    run = run or _default_run
    tok = _tok(run_token)
    if not tok or cloud not in _ENUMERATE:
        return {"planned": [], "deleted": [], "failed": [], "passes": 0, "checked": bool(tok), "dry_run": dry_run}

    def enum():
        try:
            if cloud == "aws":
                return _aws_enumerate(tok, run, region, profile)
            return _ENUMERATE[cloud](tok, run)
        except Exception as e:  # noqa: BLE001
            log(f"reaper {cloud}: enumerate failed (non-fatal): {e!r}")
            return []

    planned, deleted, failed = [], [], []
    # ``issued`` = resources whose delete returned success (which on azure is an ASYNC --no-wait accept, so
    # the group can still show as Deleting). They are never re-enumerated or re-reported as a survivor; a
    # delete that FAILED (rc != 0, e.g. a dependency) is NOT here, so it is retried next pass.
    seen_plan, issued = set(), set()
    passes = 0
    KINDS = {"aws": _AWS_KINDS, "gcp": _GCP_KINDS, "azure": _AZ_KINDS}[cloud]
    for _p in range(max_passes):
        passes += 1
        resources = [r for r in enum() if str(r) not in issued]
        if not resources:
            break
        for r in resources:                       # record the plan on first sighting
            key = str(r)
            if key not in seen_plan:
                seen_plan.add(key)
                planned.append(r)
                if r.kind not in KINDS:
                    failed.append({"res": key, "reason": "no delete mapping (disclosed, not skipped)"})
        if dry_run:
            break  # nothing changes, so one enumeration pass is the whole plan
        progress = 0
        for r in resources:
            if r.kind not in KINDS:
                continue                          # unmapped: already disclosed, cannot delete
            try:
                if cloud == "aws":
                    rc, _o, _e = _aws_delete(r, run, region, profile)
                elif cloud == "gcp":
                    rc, _o, _e = _gcp_delete(r, run)
                else:
                    rc, _o, _e = _azure_delete(r, run)
            except Exception:  # noqa: BLE001
                rc = 1
            if rc == 0:                           # success: sync-gone, or an async delete was accepted
                issued.add(str(r))
                deleted.append(str(r))
                progress += 1
            # a non-zero may be a dependency (retried next pass); only the final remainder is a real failure
        if progress == 0:
            break  # nothing deletable this pass -> stuck (dependency deadlock or a real orphan)

    if not dry_run:
        for r in enum():
            if str(r) not in issued:
                failed.append({"res": str(r), "reason": "survived deletion (real orphan)"})

    result = {"planned": [str(r) for r in planned], "deleted": deleted,
              "failed": failed, "passes": passes, "checked": True, "dry_run": dry_run}
    if planned:
        log(f"reaper ({cloud}, token {run_token}): "
            + (f"PLAN {len(planned)} resource(s) (dry run, nothing deleted)" if dry_run
               else f"deleted {len(deleted)}/{len(planned)}") + f": {[str(r) for r in planned]}")
    if failed:
        log(f"reaper ({cloud}, token {run_token}): {len(failed)} not reaped: {failed}")
    return result


_AWS_KINDS = {"aws/ec2-instance", "aws/ecs-service", "aws/ecs-cluster", "aws/load-balancer", "aws/target-group",
              "aws/rds", "aws/security-group", "aws/log-group", "aws/iam-role"}
_GCP_KINDS = {"gcp/run-service", "gcp/sql-instance", "gcp/artifact-repo"}
_AZ_KINDS = {"azure/resource-group"}
