"""Built-vs-priced AUDIT for an AWS run: does the standing run-rate price every billable resource the
agent actually PROVISIONED?

Motivation (2026-09-05): the run-rate adapter under-priced silently when its enumerator missed a resource
(a tag-gated fallback missing an untagged ALB; a Lightsail short-circuit skipping a co-provisioned RDS).
That is invisible in the cost number itself. This audit is the independent cross-check: it reads what the
agent BUILT straight from its transcript (the actual tool-call boto3 / CLI create operations, never a text
grep over possibly-stale workdir content) and compares it to what the run-rate PRICED. A billable resource
built but absent from the priced components is UNDER-PRICED -- exclude-or-disclose it, never publish it.

Not a pricing path: it enumerates nothing live and prices nothing. It only reconciles two records the run
already produced (the transcript and ``cost_run_rate``), so it is safe to run over torn-down historical runs.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

# A BILLABLE creation, in both forms the agent uses: an ``aws <svc> <verb>`` CLI (Bash tool) and a boto3
# ``operation_name='Op'`` (aws___run_script tool). Value = the canonical billable KIND the run-rate must
# carry a component for. Non-billable creates (security groups, target groups, subnets, listeners, roles,
# an ECS cluster) are deliberately absent: they cost nothing, so their omission from the price is correct.
_CLI_CREATE = {
    "elbv2 create-load-balancer": "load_balancer",
    "elb create-load-balancer": "load_balancer",
    "ec2 run-instances": "ec2",
    "rds create-db-instance": "rds",
    "rds create-db-cluster": "rds",
    "lightsail create-container-service": "lightsail",
    "elasticache create-cache-cluster": "elasticache",
    "elasticache create-replication-group": "elasticache",
    "ec2 create-nat-gateway": "nat_gateway",
    "ec2 allocate-address": "elastic_ip",
    "ecs create-service": "fargate",
    "apprunner create-service": "apprunner",
    "ec2 create-volume": "volume",
}
_BOTO_CREATE = {
    "CreateLoadBalancer": "load_balancer",
    "RunInstances": "ec2",
    "CreateDBInstance": "rds",
    "CreateDBCluster": "rds",
    "CreateContainerService": "lightsail",
    "CreateCacheCluster": "elasticache",
    "CreateReplicationGroup": "elasticache",
    "CreateNatGateway": "nat_gateway",
    "AllocateAddress": "elastic_ip",
    "CreateService": "fargate",
    "CreateVolume": "volume",
}

_CLI_RE = re.compile(r"\baws\s+(" + "|".join(re.escape(k) for k in _CLI_CREATE) + r")\b")
# The op name is matched as a BARE STRING LITERAL, not only as ``operation_name='X'``. The agent is not
# obliged to use the keyword form: aws-medium-b run17 wrapped boto3 in a helper and called
# ``go("lightsail","CreateContainerService")``, so the keyword-anchored pattern matched NOTHING across all
# 47 of its tool calls, the audit reported ``built={}``, and a run that was under-priced 6x scored "ok".
# Anchoring on a calling convention the agent chooses is not a control; the operation NAME is the invariant.
_BOTO_RE = re.compile(r"""['"](""" + "|".join(_BOTO_CREATE) + r""")['"]""")

# The matching DELETES, so a resource CREATED and then torn down within the same run (the online regime
# explores architectures: build EC2, abandon it, switch to Fargate) is not mistaken for an unpriced standing
# resource. A kind that was built, never deleted in-run, and never priced is the high-confidence under-price.
_CLI_DELETE = {
    "elbv2 delete-load-balancer": "load_balancer",
    "ec2 terminate-instances": "ec2",
    "rds delete-db-instance": "rds",
    "rds delete-db-cluster": "rds",
    "lightsail delete-container-service": "lightsail",
    "elasticache delete-cache-cluster": "elasticache",
    "elasticache delete-replication-group": "elasticache",
    "ec2 delete-nat-gateway": "nat_gateway",
    "ec2 release-address": "elastic_ip",
    "ecs delete-service": "fargate",
    "apprunner delete-service": "apprunner",
    "ec2 delete-volume": "volume",
}
_BOTO_DELETE = {
    "DeleteLoadBalancer": "load_balancer", "TerminateInstances": "ec2", "DeleteDBInstance": "rds",
    "DeleteDBCluster": "rds", "DeleteContainerService": "lightsail", "DeleteCacheCluster": "elasticache",
    "DeleteReplicationGroup": "elasticache", "DeleteNatGateway": "nat_gateway", "ReleaseAddress": "elastic_ip",
    "DeleteService": "fargate", "DeleteVolume": "volume",
}
_CLI_DEL_RE = re.compile(r"\baws\s+(" + "|".join(re.escape(k) for k in _CLI_DELETE) + r")\b")
_BOTO_DEL_RE = re.compile(r"""['"](""" + "|".join(_BOTO_DELETE) + r""")['"]""")

# Priced component name (cost_run_rate.components[].name) -> the billable KIND it covers. A run-rate that
# carries any of these has priced that kind. ``storage``/``public_ip`` are auto-synthesized parts of a
# parent (EC2 root EBS, an internet-facing address), not standalone creates, so they gate no "missing" flag.
_PRICED_KIND = {
    "load_balancer": "load_balancer",
    "compute": "ec2",
    "compute:fargate-vcpu": "fargate",           # a Fargate task prices as two lines, vCPU-hours + GB-hours
    "compute:fargate-mem": "fargate",
    "compute:rds": "rds",
    "storage:rds": "rds",
    "compute:redis": "elasticache",
    "compute:lightsail-container": "lightsail",
    "storage": "volume",                         # the EC2 root/attached EBS line
    "public_ip": "elastic_ip",
    "elastic-ip": "elastic_ip",
    "nat_gateway": "nat_gateway",
}

# Kinds whose ABSENCE from the price, when built, is a real under-pricing. (``ec2`` and ``fargate`` are
# standalone compute; ``volume``/``elastic_ip`` alone are cheap and often synthesized, so they are reported
# but do not by themselves fail the audit unless the intent is strict.)
_STANDALONE_BILLABLE = {"load_balancer", "rds", "lightsail", "elasticache", "nat_gateway", "ec2", "fargate"}


# Whether a call's RESULT marks a DEFINITIVE failure, so a create/delete that provisioned/tore-down nothing
# does not inflate the count (the online regime issues many failed EC2 attempts before switching to Fargate:
# expired creds set is_error; a sandbox error sets the run_script wrapper's top-level status to "error"). ONLY
# these STRUCTURED signals are used, never a fuzzy text match: run14's real RDS create returned
# is_error=False / status=success but its stderr echoed another call's "Error ...", and a text match on that
# wrongly silenced a genuine under-price. A false negative (a missed under-price) is worse than a false flag,
# so the rule stays conservative: a call is failed ONLY when the harness or the sandbox says so.
_STATUS_RE = re.compile(r'"status"\s*:\s*"(\w+)"')


# Identifiers that AWS only ever emits for a resource that EXISTS: an assigned instance id, a service
# endpoint, a DNS name. Used to CORROBORATE a count shortfall, because counting create CALLS cannot tell a
# real provision from a probe the agent expected to fail: aws-easy run9 wrapped RunInstances in a try/except
# and labelled the branch "unexpected success", so the script returned success, the call counted, and the run
# was flagged 2-built-vs-1-priced when exactly ONE instance was ever created. A probe that failed leaves no
# identifier behind, so the identifier count is the honest one.
_IDENT = {
    "ec2": re.compile(r"\bi-[0-9a-f]{8,17}\b"),
    "lightsail": re.compile(r"https?://([a-z0-9-]+)\.[a-z0-9]+\.[a-z0-9-]+\.cs\.amazonlightsail\.com"),
    "rds": re.compile(r"\b([a-z0-9-]+)\.[a-z0-9]+\.[a-z0-9-]+\.rds\.amazonaws\.com\b"),
    "load_balancer": re.compile(r"\b([a-z0-9-]+-\d+)\.[a-z0-9-]+\.elb\.amazonaws\.com\b"),
}


def _result_text(content) -> str:
    """The tool result as PLAIN text. Never ``json.dumps``: that renders a newline as the two characters
    backslash + n, and ``\\b`` then matches between them, so an identifier that starts a line is captured
    with a leading 'n' and counted as a SECOND distinct resource ('umami-x-1' -> 'numami-x-1'). Two ALBs
    read as three, and under a re-price-on-mismatch policy that spurious count would trigger a re-price."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                out.append(str(c.get("text") or c.get("content") or ""))
            else:
                out.append(str(c))
        return "\n".join(out)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or content)
    return str(content or "")


def observed_identities(transcript_path: str, run_token: str, wall_s: Optional[float] = None) -> Dict[str, set]:
    """kind -> the DISTINCT resource identities the deploy turn's tool RESULTS prove existed. Scoped to
    identities carrying this run's token so a stale workdir log from a PRIOR run cannot inflate the count."""
    out: Dict[str, set] = {k: set() for k in _IDENT}
    t0 = cutoff = None
    with open(transcript_path) as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            ts = o.get("timestamp")
            if ts and t0 is None and wall_s is not None:
                try:
                    t0 = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    cutoff = t0 + _dt.timedelta(seconds=wall_s)
                except ValueError:
                    cutoff = None
            if ts and cutoff is not None:
                try:
                    if _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) > cutoff:
                        continue
                except ValueError:
                    pass
            msg = o.get("message") or o
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                text = _result_text(b.get("content"))
                for kind, pat in _IDENT.items():
                    for m in pat.finditer(text):
                        ident = m.group(1) if m.groups() else m.group(0)
                        window = text[max(0, m.start() - 90):m.end() + 90]
                        if run_token and run_token not in (ident if m.groups() else window):
                            continue
                        out[kind].add(ident)
    return out


def _deploy_turn_s(rec: dict) -> Optional[float]:
    """Seconds from t0 to the END of the deploy turn, which is the window the cost snapshot closes over.

    ``serving.agent_finished_at_s`` is the poller's OWN clock and is authoritative. ``agent_wall_s`` is
    ``lane_summary.wall_s``, derived from the transcript, and it can be far short of the real span when
    the lane analysis sees only part of the session: on aws-medium-b run20 it reported 287.7s against a
    true 3303.7s, an 11.5x truncation. It agreed to within 2% on all 15 earlier runs, which is exactly
    why keying on it looked safe and would have silently truncated the next run's window."""
    s = (rec.get("serving") or {}).get("agent_finished_at_s")
    if isinstance(s, (int, float)) and s > 0:
        return float(s)
    w = rec.get("agent_wall_s")
    return float(w) if isinstance(w, (int, float)) and w > 0 else None


def _result_failed(text: str, is_error: bool) -> bool:
    if is_error:                                          # the harness's own tool-failure flag
        return True
    m = _STATUS_RE.search(text or "")                    # the run_script wrapper's top-level status field
    return bool(m and m.group(1) == "error")


def _tool_blobs(transcript_path: str):
    """Yield the executed text of every tool call: a Bash ``command`` or an aws___run_script ``code``. This
    is what the agent actually RAN, never file content it merely read, so a stale workdir cannot spoof it."""
    for _id, _name, blob in _tool_calls(transcript_path):
        yield blob


def _tool_calls(transcript_path: str, wall_s: Optional[float] = None):
    """Yield (tool_use_id, tool_name, executed_text) for every tool call.

    ``wall_s`` restricts the scan to the DEPLOY TURN (the first timestamp plus ``agent_wall_s``), which
    is the window the cost snapshot closes over: ``measure_cost`` runs at autorun.py:1857, after the
    deploy turn ends and BEFORE ``drive_suite`` at :1879. Without this cut the scan also counts what the
    SUITE provisions (site B above all), which the snapshot never priced and never should have. Measured:
    on aws-medium-a the uncut scan flags runs 7, 9 and 10 for an unpriced EC2 that the suite created,
    all three FALSE; with the cut that cell flags nothing and aws-medium-b still flags exactly its seven
    hand-confirmed under-priced runs."""
    t0 = cutoff = None
    with open(transcript_path) as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            ts = o.get("timestamp")
            if ts and t0 is None and wall_s is not None:
                try:
                    t0 = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    cutoff = t0 + _dt.timedelta(seconds=wall_s)
                except ValueError:
                    cutoff = None
            if ts and cutoff is not None:
                try:
                    if _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) > cutoff:
                        continue
                except ValueError:
                    pass
            msg = o.get("message") or o
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                name, inp = b.get("name", ""), b.get("input", {})
                if not isinstance(inp, dict):
                    continue
                blob = inp.get("command", "") if name == "Bash" else (inp.get("code", "") if "run_script" in name else "")
                yield b.get("id"), name, (blob or "")


def _result_failures(transcript_path: str) -> dict:
    """tool_use_id -> True when its result marks a failed call. Ids with no result are absent (counted)."""
    failed: Dict[str, bool] = {}
    with open(transcript_path) as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            msg = o.get("message") or o
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                txt = b.get("content", "")
                if isinstance(txt, list):
                    txt = " ".join(x.get("text", "") for x in txt if isinstance(x, dict))
                failed[b.get("tool_use_id")] = _result_failed(txt, bool(b.get("is_error")))
    return failed


def _scan_ops(transcript_path: str, wall_s: Optional[float] = None) -> Tuple[Counter, Counter]:
    """(creates, deletes) counted from SUCCESSFUL calls only: a create/delete whose result marks a failure
    (or, for creates, an abandoned/errored attempt) does not count. A call with no matching result counts."""
    failed = _result_failures(transcript_path)
    creates: Counter = Counter()
    deletes: Counter = Counter()
    for tid, _name, blob in _tool_calls(transcript_path, wall_s):
        if failed.get(tid, False):
            continue
        for m in _CLI_RE.finditer(blob):
            creates[_CLI_CREATE[m.group(1)]] += 1
        for m in _BOTO_RE.finditer(blob):
            creates[_BOTO_CREATE[m.group(1)]] += 1
        for m in _CLI_DEL_RE.finditer(blob):
            deletes[_CLI_DELETE[m.group(1)]] += 1
        for m in _BOTO_DEL_RE.finditer(blob):
            deletes[_BOTO_DELETE[m.group(1)]] += 1
    return creates, deletes


def billable_creates_from_transcript(transcript_path: str) -> Counter:
    """The BILLABLE resource kinds the agent SUCCESSFULLY provisioned, counted from the transcript's actual
    create calls (CLI + boto3), excluding calls whose result marks a failure."""
    return _scan_ops(transcript_path)[0]


def billable_deletes_from_transcript(transcript_path: str) -> Counter:
    """The billable resource kinds the agent TORE DOWN within the run (create/delete cancel out, so a
    superseded resource is not read as an unpriced standing one)."""
    return _scan_ops(transcript_path)[1]


def priced_kinds_from_components(components: List[dict]) -> Counter:
    """The billable kinds the run-rate priced, counted from cost_run_rate.components."""
    priced: Counter = Counter()
    for c in components or []:
        kind = _PRICED_KIND.get((c or {}).get("name", ""))
        if kind:
            priced[kind] += 1
    return priced


def _resolve_transcript(run_json_path: str, d: dict) -> Optional[str]:
    """The recorded ``deploy.transcript`` path, or, when it is stale (the staged cells were renamed, e.g.
    aws-medium -> aws-medium-a, so the absolute path no longer exists), the session's .jsonl located
    relative to the run's OWN directory by its session id. Keeps the audit working after a data move."""
    import glob
    import os
    recorded = (d.get("deploy") or {}).get("transcript")
    if recorded and os.path.isfile(recorded):
        return recorded
    session = (d.get("deploy") or {}).get("session")
    if session:
        base = os.path.dirname(os.path.abspath(run_json_path))
        hits = glob.glob(os.path.join(base, "**", f"{session}.jsonl"), recursive=True)
        if hits:
            return hits[0]
    return recorded  # unresolved: audit_run will report it unreadable rather than guess


def audit_run(run_json_path: str, transcript_path: Optional[str] = None) -> dict:
    """Reconcile one run's built vs priced. Returns a verdict dict; ``underpriced`` is True when a standalone
    billable kind was built but not priced (the exclude-or-disclose signal)."""
    with open(run_json_path) as fh:
        d = json.load(fh)
    tpath = transcript_path or _resolve_transcript(run_json_path, d)
    crr = d.get("cost_run_rate") or {}
    result = {"run": d.get("run"), "run_token": d.get("run_token"), "url": d.get("url"),
              "price_source": (crr.get("price_source") or "")[:80],
              "usage_metered": crr.get("kind") == "usage" or crr.get("usage_metered", False)}
    if not tpath:
        result.update(built={}, priced={}, missing=[], underpriced=False, note="no transcript recorded")
        return result
    try:
        built, deleted = _scan_ops(tpath, _deploy_turn_s(d))
    except OSError as e:
        result.update(built={}, priced={}, missing=[], underpriced=False, note=f"transcript unreadable: {e}")
        return result
    priced = priced_kinds_from_components(crr.get("components"))
    # HIGH-CONFIDENCE under-price: a standalone billable built, NEVER torn down in-run, and NOT priced. The
    # in-run delete guard drops the online regime's build-then-abandon exploration (create EC2, switch to
    # Fargate, terminate the EC2), which is correctly unpriced because nothing standing remains.
    missing = sorted(k for k, n in built.items()
                     if k in _STANDALONE_BILLABLE and n > 0 and deleted.get(k, 0) == 0 and priced.get(k, 0) == 0)
    # UNCERTAIN: built AND deleted in-run, and unpriced; the net standing count is not decidable from call
    # counts alone (one delete call can tear down several), so it is surfaced for manual confirmation, not failed.
    uncertain = sorted(k for k, n in built.items()
                       if k in _STANDALONE_BILLABLE and n > 0 and deleted.get(k, 0) > 0 and priced.get(k, 0) == 0)
    # A COUNT shortfall (2 built, 1 priced) FAILS. It used to be "reported but not failing", which is how
    # aws-medium-b run10 and run19 printed ``shortfall={'lightsail': (2,1)}`` / ``(3,1)`` next to the verdict
    # "ok" while being under-priced 4x. Pricing one of two live container services is not a softer kind of
    # wrong than pricing none of one. Measured over the cell: making this fail adds runs 10, 17 and 19 and
    # introduces ZERO false alarms on the 8 published rows.
    # CORROBORATED by identities seen in the results: a shortfall stands only if at least as many DISTINCT
    # resources were actually observed as the call count claims. Where no identifier pattern exists for a
    # kind, the call count stands on its own.
    ident = observed_identities(tpath, d.get("run_token") or "", d.get("agent_wall_s"))
    shortfall = {}
    for k in built:
        if k not in _STANDALONE_BILLABLE or not priced.get(k, 0) or deleted.get(k, 0):
            continue
        n_built = built[k]
        if k in ident:
            n_built = min(n_built, max(len(ident[k]), priced.get(k, 0)))
        if n_built > priced.get(k, 0):
            shortfall[k] = (built[k], priced.get(k, 0))
    # ABSENCE OF EVIDENCE IS NOT A PASS. An empty ``built`` means the scanner recognised nothing in the
    # transcript, which is a statement about the SCANNER, not about the run. Reporting that as "ok" is a
    # check that cannot fail. It is UNVERIFIABLE, and a run nobody can verify is not publishable unless a
    # human looks. (Before the bare-literal fix above this hit runs 15, 17 and 18; run17 was under-priced.)
    unverifiable = not built
    result.update(built=dict(built), priced=dict(priced), deleted=dict(deleted), missing=missing,
                  uncertain=uncertain, count_shortfall=shortfall, unverifiable=unverifiable,
                  underpriced=bool(missing) or bool(shortfall))
    return result


def _iter_run_jsons(path: str) -> List[str]:
    import os
    if os.path.isfile(path):
        return [path]
    out = []
    for name in sorted(os.listdir(path)):
        if re.fullmatch(r"run\d+\.json", name):
            out.append(os.path.join(path, name))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Audit AWS runs: every billable resource built must be priced.")
    ap.add_argument("path", help="a runNN.json, or a cell directory containing runNN.json files")
    args = ap.parse_args(argv)
    rows = [audit_run(p) for p in _iter_run_jsons(args.path)]
    bad = [r for r in rows if r.get("underpriced")]
    unver = [r for r in rows if r.get("unverifiable")]
    for r in rows:
        flag = ("UNDER-PRICED" if r["underpriced"] else
                "UNVERIFIABLE" if r.get("unverifiable") else
                ("usage" if r["usage_metered"] else "ok"))
        extra = f"  MISSING={r['missing']}" if r["missing"] else ""
        unc = f"  uncertain(built+deleted,unpriced)={r['uncertain']}" if r.get("uncertain") else ""
        sf = f"  shortfall={r['count_shortfall']}" if r.get("count_shortfall") else ""
        print(f"  run{r['run']:<3} [{flag:<12}] built={r['built']} priced={r['priced']}{extra}{unc}{sf}")
    unc_runs = [r["run"] for r in rows if r.get("uncertain")]
    print(f"\n{len(bad)}/{len(rows)} under-priced" + (f": runs {[r['run'] for r in bad]}" if bad else "")
          + (f"  |  {len(unc_runs)} uncertain: runs {unc_runs}" if unc_runs else "")
          + (f"  |  {len(unver)} UNVERIFIABLE: runs {[r['run'] for r in unver]}" if unver else ""))
    return 1 if (bad or unver) else 0


if __name__ == "__main__":
    raise SystemExit(main())
