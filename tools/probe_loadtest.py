#!/usr/bin/env python3
"""Deterministic, open-loop app-level capability probe (Part 1 capability, portable arm).

This is the INSTRUMENT, not the agent. It measures the delivered capability of whatever the
agent built, on a representative workload, in a way that is reproducible run to run and portable
across clouds (it only needs the app's public URL, never a cloud API). It is run on the FROZEN
live deployment, after the agent has finished operating and before the agent tears it down.

Grounding (see paper Part 1 + the literature-grounding briefing):

* OPEN-LOOP, not closed-loop. Arrival times for the whole run are drawn UP FRONT from a seeded
  Poisson process and never depend on when earlier responses come back. A closed-loop client
  (fire, wait for reply, fire again) cannot generate a queue, so it silently hides the tail it
  is meant to find. (Schroeder, NSDI 2006; wrk2/Tene.)
* LATENCY FROM INTENDED SEND TIME. Each request's latency is measured from the time it was
  SCHEDULED to leave, not from when a worker actually got to it. This is the coordinated-omission
  fix: if the system stalls, every request that piled up behind the stall keeps its full intended
  latency instead of being dropped from the sample. (wrk2; Brooker.)
* CORRECTNESS SEPARATED FROM TIMING. Functional correctness is a different predicate
  (functional_check.sh); this probe only measures capacity/latency and reports an error RATE, so
  validation never taxes the measurement. (MLPerf LoadGen: performance mode vs accuracy mode.)
* MINIMUM DURATION + WARMUP. Default >= 60s; the first ``warmup_s`` seconds are reported
  separately so a cold cache/JIT does not contaminate steady state. (MLPerf; Georges 2007.)
* REPRODUCIBLE. A single ``--seed`` fixes both the arrival schedule and which parameters each
  request carries, so two runs of the probe against the same SUT are comparable.

Percentiles here are exact nearest-rank over the raw sample (fine at these sample sizes); an
HdrHistogram is the drop-in tightening if the sample ever gets large enough to matter.

Usage:
  probe_loadtest.py --url https://x.redu.cloud --duration 90 --rate 25 --seed 42 --out probe.json
Exit code is always 0 unless the arguments are unusable: an unreachable SUT is a RESULT
(``probe_ok:false``), not a harness error, so it never aborts the batch.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# APP-AGNOSTIC default workload: hit the app's root. A speed test must run on ANY app/repo cold, so
# the probe must NOT know app-specific endpoints. The root serving under load is the universal
# capacity signal. A profile may override with --workload for an app where a heavier path is wanted.
WORKLOAD = [
    ("root", 1, "/"),
]


def _now() -> float:
    return time.monotonic()


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_workload(path: str | None) -> list[tuple[str, float, str]]:
    """Workload as (name, weight, path) tuples. Default = the built-in DSB mix; --workload points at a
    JSON list of {name, weight, path} so the same probe serves any app (portable across clouds/apps)."""
    if not path:
        return list(WORKLOAD)
    spec = json.load(open(path))
    return [(w["name"], float(w.get("weight", 1)), w["path"]) for w in spec]


def build_schedule(rate: float, duration: float, seed: int,
                   workload: list[tuple[str, float, str]]) -> list[tuple[float, str, str]]:
    """Draw the whole open-loop schedule up front: a seeded Poisson arrival process, each arrival
    assigned an endpoint by seeded weighted choice. Returns [(offset_s, endpoint_name, url_path)]."""
    rng = random.Random(seed)
    names = [w[0] for w in workload]
    weights = [w[1] for w in workload]
    paths = {w[0]: w[2] for w in workload}
    sched: list[tuple[float, str, str]] = []
    t = 0.0
    while True:
        t += rng.expovariate(rate)          # inter-arrival ~ Exp(rate) => Poisson process
        if t >= duration:
            break
        name = rng.choices(names, weights=weights, k=1)[0]
        sched.append((t, name, paths[name]))
    return sched


def percentiles(xs: list[float], ps=(50, 90, 95, 99, 99.9)) -> dict:
    if not xs:
        return {f"p{p}": None for p in ps}
    s = sorted(xs)
    out = {}
    for p in ps:
        # nearest-rank: rank = ceil(p/100 * N), 1-indexed
        k = max(0, min(len(s) - 1, math.ceil(p / 100.0 * len(s)) - 1))
        out[f"p{p}"] = round(s[k] * 1000, 1)     # ms
    return out


def run_probe(base: str, rate: float, duration: float, seed: int, warmup_s: float,
              timeout_s: float, max_workers: int,
              workload: list[tuple[str, float, str]] | None = None) -> dict:
    base = base.rstrip("/")
    workload = workload or list(WORKLOAD)
    sched = build_schedule(rate, duration, seed, workload)
    results: list[dict] = []

    def fire(intended: float, offset: float, name: str, path: str) -> None:
        # workers ONLY do the request; scheduling is the dispatcher's job, so a worker slot is held
        # for the request duration, never for idle sleeping (which would starve concurrency).
        url = base + path
        code = 0
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as r:
                r.read(2048)                      # touch the body; we do not validate it here
                code = r.getcode()
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            code = 0                              # timeout / conn refused / DNS
        done = _now()
        results.append({
            "offset": offset, "endpoint": name, "code": code,
            "latency": done - intended,           # FROM INTENDED SEND TIME (coordinated-omission-safe)
            "steady": offset >= warmup_s,
            "ok": 200 <= code < 400,
        })

    t0 = _now()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for offset, name, path in sched:
            # DISPATCHER paces submission to the fixed schedule (open-loop): sleep to the intended
            # departure, then submit. submit() returns at once, so a slow SUT backs up in the pool
            # queue and never pushes the schedule out; the queue wait shows up in latency-from-intended.
            intended = t0 + offset
            d = intended - _now()
            if d > 0:
                time.sleep(d)
            ex.submit(fire, intended, offset, name, path)
        # executor context exit waits for every scheduled request (including any still stalled)

    steady = [r for r in results if r["steady"]]
    steady_lat = [r["latency"] for r in steady if r["ok"]]
    completed = [r for r in results if r["ok"]]
    # goodput over the REAL completion window, not the nominal arrival window: under backlog (the
    # overload case this probe exists to find) completions drain past `duration`, so completed/duration
    # would overstate delivered throughput. Aggregates are all we store, so this must be right here.
    finishes = [r["offset"] + r["latency"] for r in completed]
    wall = max(finishes) if finishes else duration
    errs = [r for r in results if not r["ok"]]
    by_ep: dict[str, dict] = {}
    for name, _, _ in workload:
        lat = [r["latency"] for r in steady if r["endpoint"] == name and r["ok"]]
        by_ep[name] = {"n": sum(1 for r in results if r["endpoint"] == name),
                       "ok": len(lat), **percentiles(lat, (50, 99))}
    return {
        "probe_ok": len(completed) > 0,
        "scheduled": len(sched),
        "completed": len(completed),
        "errors": len(errs),
        "error_rate": round(len(errs) / len(results), 4) if results else None,
        "target_rate_rps": rate,
        "achieved_throughput_rps": round(len(completed) / wall, 2) if wall else None,
        "duration_s": duration,
        "warmup_s": warmup_s,
        "steady_samples": len(steady_lat),
        "latency_ms": percentiles(steady_lat),
        "by_endpoint": by_ep,
        "seed": seed,
        "measured_at": _iso(),
        "note": "open-loop Poisson arrivals, latency from intended send time; correctness is a "
                "separate predicate (functional_check.sh); percentiles nearest-rank over steady sample.",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--duration", type=float, default=90.0)
    ap.add_argument("--rate", type=float, default=25.0, help="target arrivals/sec (open-loop)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--warmup", type=float, default=10.0)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--max-workers", type=int, default=256)
    ap.add_argument("--workload", default=None, help="JSON list of {name,weight,path}; default = DSB mix")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.duration < 60:
        print(f"[probe] WARNING: duration {a.duration}s is below the 60s minimum", file=sys.stderr)
    res = run_probe(a.url, a.rate, a.duration, a.seed, a.warmup, a.timeout, a.max_workers,
                    workload=load_workload(a.workload))
    text = json.dumps(res, indent=2)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
