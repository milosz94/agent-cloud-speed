#!/usr/bin/env python3
"""Part-5 validation DRIVER for the agent-cloud efficiency paper.

This is the practical implementation of the paper in `../PAPER/`. The driver does ORCHESTRATION and
I/O only; every MEASUREMENT and every NUMBER flows through the `acspeed` package (the paper's Parts
1-4 implementation). Nothing here defines a metric. One run:

  [measured] agent deploys (session A; t0 = deploy request), while an EXTERNAL poller runs
        CONCURRENTLY: it tails the session's live transcript for the deployment URL and polls that
        URL from the moment it appears (ReadinessPoller)
    ->  t1 = the first response the app itself gives (status < 500; see serving_predicate), the
        EXTERNAL stop signal. The agent's own "I'm done" is never the boundary (the paper rejects
        self-reported done), and the agent finishing AFTER t1 (post-serving verification, notes) is
        outside the operation: the trace is clipped at t1 (acspeed.transcript.clip_rows)
    ->  [measured] agent repairs, resuming A, ONLY if the URL never serves (liveness gate, not an
        app-specific endpoint/auth check: success = the app ANSWERS, app-agnostic, per PAPER p2 s4)
    ->  build the Part-2 Operation from the clipped trace + one platform-owned boot span, and read
        the SPLIT off acspeed (operation.split + agenttime.decompose): wall = critical_platform +
        critical_agent, in SECONDS (no percentages, no floor/ceiling)
    ->  (optional, off-clock, --screenshot) save a picture of the working app to run<NN>.png
    ->  [measured] agent deprovisions ITS OWN deployment (session B) -> curl read-verify the URL is dead
    ->  record everything

Grounded pieces kept: clock start at request, stop at external readiness poll (boot inside the clock);
reject self-reported done; first-attempt liveness vs recovery kept separate; fresh env per run on one
account; warmth fingerprinted from the transcript.

Instrument defects this file has already produced, kept here so they are not re-made:
  2026-08-25 Umami: an app-specific functional check ('/' + login) drove fake repairs, 12 -> 26.5 min.
  2026-08-25 Isso : the 200-399 rule on '/' called Isso's documented 400 an outage, twice; the agent
                   then built an nginx landing page to satisfy the probe. Served at 181 s, agent done
                   at 403 s, recorded 1443 s. Fixed by serving_predicate (< 500) + the concurrent poller.

NOT in this run (deferred, never faked): capability C (raw-infra micro-probes sysbench/STREAM/fio/
iperf3 over SSH, off-clock -> acspeed.capability) and the competitive-ratio EFFICIENCY (needs the
Part-3 per-task reference-optimal gold). The old HTTP "/" load probe was NOT the paper's capability C
and has been removed from the run (see the paper-change-request list; it may return as the secondary
app-capability axis once the paper sanctions it).

Usage:  cd <your app folder> && acspeed-run --adapter redu --model claude-opus-5
        (results land in ./acspeed-results/<cloud>/ so clouds sit side by side; rebuild a table with
         `python3 build_tables.py --dir ./acspeed-results/<cloud>`)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acspeed import transcript as acs  # noqa: E402
from acspeed import operation as acs_op  # noqa: E402
from acspeed import agenttime as acs_at  # noqa: E402
from acspeed.types import Span, PLATFORM  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# The hermetic per-task substrate (PAPER C9): each agent turn runs in a fresh Firecracker microVM
# holding ONLY the target cloud's credentials. Same CLI; the microVM is transparent. Auto-enabled
# when the sandbox is built and KVM + the tap are present; --no-sandbox forces the host path.
sys.path.insert(0, os.path.join(HERE, "sandbox"))
try:
    import vmjob  # noqa: E402
except Exception:  # noqa: BLE001
    vmjob = None


def sandbox_available() -> tuple[bool, str]:
    """Is the microVM substrate usable right now? Returns (ok, reason-if-not)."""
    if vmjob is None:
        return False, "sandbox/vmjob.py not importable"
    for p in (vmjob.BIN_FC, vmjob.KERNEL, vmjob.BASE_ROOTFS):
        if not os.path.exists(p):
            return False, f"missing {os.path.basename(p)} (build the sandbox: see sandbox/STATE.md)"
    if not os.path.exists("/dev/kvm"):
        return False, "/dev/kvm absent (sudo modprobe kvm_amd)"
    r = subprocess.run(["ip", "-o", "addr", "show", vmjob.TAP], capture_output=True, text=True)
    if r.returncode != 0 or vmjob.TAP not in r.stdout:
        return False, f"tap {vmjob.TAP} down (sudo bash sandbox/net-setup.sh)"
    return True, ""
DATA = "/home/milos/Desktop/research_paper_data"

# --adapter selects the cloud. Same cloud names as acspeed.adapters.profiles (redu, aws, gcp, azure);
# the analysis is identical across clouds, only this per-cloud driver config differs: which MCP server
# the agent connects to, and the URL pattern its deployments use. The hyperscalers are stubs to fill
# when wiring Part 5 (their MCP config file + deployed-URL regex).
#
# `substrate_hosts` = the cloud's OWN control-plane hostnames (its API, dashboard, MCP endpoint,
# docs). These MUST NOT be measured as the deployed app: they are permanent services that always
# answer, and they leak into the transcript because the deploy guide (redu.md) and MCP results name
# them (e.g. `claude mcp add ... https://mcp.redu.cloud/mcp`). Measured 2026-08-25 (jotty run): the
# poller latched `mcp.redu.cloud` (always 200) and stopped the clock at 20s, before deploy_compose
# was even called. The substrate is a disclosed detail of the method, never the app under test.
ADAPTERS = {
    # keep_claude_tokens = the mcpOAuth server names the microVM keeps (its cloud auth); aws_creds =
    # a host dir mounted read into the VM. This is the substrate scoping: redu keeps only its own token,
    # aws keeps none (it authenticates via ~/.aws), so neither can reach the other cloud.
    "redu": {"mcp_config": f"{DATA}/_config/redu.mcp.json",
             "keep_claude_tokens": ["redu"],
             "url_re": r"https://[a-z0-9.-]+\.redu\.cloud",
             "substrate_hosts": r"^https://(?:mcp|api|console|dashboard|docs|www|register)\.redu\.cloud"
                                r"|^https://redu\.cloud/?$"},
    # AWS: mcp_config points at the AWS MCP server (aws-mcp: uvx mcp-proxy-for-aws; generic call_aws,
    # so the agent orchestrates the whole deploy over raw AWS APIs). url_re is a strict WHITELIST of
    # AWS's APP-HOSTING URL suffixes ONLY. This is load-bearing: a broad `amazonaws.com` also matches
    # AWS SERVICE/API endpoints (ec2.<region>.amazonaws.com, sts.amazonaws.com, <acct>.dkr.ecr...,
    # s3...) which answer <500 to a bare GET and would falsely stop the clock at ~0s (the
    # mcp.redu.cloud defect, but pervasive on AWS). Only real app fronts are listed: App Runner, ALB/ELB,
    # EC2 public DNS (compute[-1]), Lightsail containers, Elastic Beanstalk, CloudFront, API Gateway.
    "aws":   {"mcp_config": f"{DATA}/_config/aws.mcp.json",
              "keep_claude_tokens": [], "aws_creds": "~/.aws",
              "url_re": r"https?://[a-z0-9.-]+\.(?:"
                        r"awsapprunner\.com"                      # App Runner
                        r"|[a-z0-9-]+\.elb\.amazonaws\.com"       # ALB/ELB (name.region.elb...)
                        r"|compute\.amazonaws\.com"               # EC2 public DNS (most regions)
                        r"|compute-1\.amazonaws\.com"             # EC2 public DNS (us-east-1 legacy)
                        r"|cs\.amazonlightsail\.com"              # Lightsail containers
                        r"|elasticbeanstalk\.com"                 # Elastic Beanstalk
                        r"|cloudfront\.net"                       # CloudFront
                        r"|execute-api\.[a-z0-9-]+\.amazonaws\.com"  # API Gateway
                        r")(?::\d+)?",
              # console/docs/signin are never the app; the MCP endpoint (api.aws) is not in url_re anyway.
              "substrate_hosts": r"^https?://(?:[a-z0-9.-]*\.)?(?:console|signin|docs|health|status)\.aws\.amazon\.com"
                                 r"|^https?://aws\.amazon\.com"},
    # "gcp":   {"mcp_config": "<path>", "url_re": "<deployed-URL regex>", "substrate_hosts": "<own hosts>"},
    # "azure": {"mcp_config": "<path>", "url_re": "<deployed-URL regex>", "substrate_hosts": "<own hosts>"},
}

# App-agnostic by design: it deploys the CURRENT folder (or --app-dir) once. No named app, no per-app
# check. The prompt is universal (nothing about WHAT, HOW, or WHICH CLOUD): "this directory" IS the app
# under test. Which cloud the agent uses must come from the SESSION'S CAPABILITY (only the target
# cloud's tools/creds are reachable), NOT from prompt instructions, which would contaminate the
# benchmark. How to isolate the target environment is under revalidation (grounded in the paper + the
# prior-benchmark literature), not the rejected prompt-binding approach.
CONFIG = {
    "probe": f"{HERE}/probe_loadtest.py",            # unused now; kept for the future app-capability axis (C4)
    "task_prompt": "Deploy the application in this directory.",
}
# Cloud-agnostic autonomy wrapper (proceed non-interactively). NOT task guidance.
AUTONOMY = (
    "This is a fully autonomous, non-interactive session. Never ask the user questions and never "
    "wait for confirmation: make reasonable default choices and proceed to completion. Whenever a "
    "tool or workflow offers an automatic / default / 'yolo' option, choose it. Keep going until the "
    "task is genuinely done or you have exhausted your options, then stop."
)
DEPLOY_TIMEOUT_S = 3000    # a full build can run long; the agent's own turn budget
TEARDOWN_TIMEOUT_S = 500
READINESS_TIMEOUT_S = 300   # after the agent hands off, how long to let the app finish booting/serving
SERVING_POLL_S = 5.0        # external readiness poll interval (t1 resolution is +/- this)
PROBE_DURATION_S = 90
PROBE_RATE = 25
PROBE_SEED = 42


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def _claude(prompt: str, *, cwd: str, mcp: str, model: str | None, resume: str | None = None,
            max_turns: int = 200, timeout: int = DEPLOY_TIMEOUT_S, system: str | None = AUTONOMY,
            sandbox: dict | None = None, boot_log: str | None = None,
            resume_transcript: str | None = None) -> dict:
    """Run one headless `claude -p` turn; return the parsed JSON result object. When `sandbox` is set,
    the turn runs inside a fresh microVM (the hermetic substrate) instead of on the host; the return
    shape is identical, so callers are unchanged."""
    if sandbox:
        return _claude_vm(prompt, app_dir=cwd, mcp=mcp, model=model, resume=resume, max_turns=max_turns,
                          timeout=timeout, system=system, sandbox=sandbox, boot_log=boot_log,
                          resume_transcript=resume_transcript)
    cmd = ["claude", "-p", prompt, "--mcp-config", mcp, "--strict-mcp-config",
           "--permission-mode", "bypassPermissions", "--output-format", "json",
           "--max-turns", str(max_turns)]
    if system:
        cmd += ["--append-system-prompt", system]
    if model:
        cmd += ["--model", model]
    if resume:
        cmd += ["--resume", resume]
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return {"is_error": True, "result": f"TIMEOUT after {timeout}s", "session_id": None,
                "stdout": (e.stdout or "")[-2000:] if isinstance(e.stdout, str) else ""}
    out = p.stdout.strip()
    try:
        return json.loads(out)
    except ValueError:
        return {"is_error": True, "result": out[-2000:], "stderr": p.stderr[-2000:], "session_id": None}


def _install_recovered_ssh_keys(ssh_dir: str | None) -> None:
    """Copy SSH keys recovered from the microVM into the host ~/.ssh (0600) so a later capability pass can
    reach the deploy VM: the deploy may mint a per-app keypair (redu-<app>-deploy) whose private key exists
    ONLY in the microVM, and get_ssh_command hands back `-i ~/.ssh/<keypair>`. User's own keys, own host."""
    if not ssh_dir or not os.path.isdir(ssh_dir):
        return
    dst = os.path.expanduser("~/.ssh")
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(ssh_dir):
        src = os.path.join(ssh_dir, name)
        if not os.path.isfile(src):
            continue
        try:
            shutil.copy(src, os.path.join(dst, name))
            os.chmod(os.path.join(dst, name), 0o644 if name.endswith(".pub") else 0o600)
        except OSError:
            pass


def _claude_vm(prompt: str, *, app_dir: str, mcp: str | None, model: str | None, resume: str | None,
               max_turns: int, timeout: int, system: str | None, sandbox: dict,
               boot_log: str | None, resume_transcript: str | None) -> dict:
    """Run one agent turn inside a fresh microVM (vmjob) and return the same JSON shape as the host
    path. Credential scoping (only the target cloud's tokens) comes from the adapter's sandbox config."""
    res = vmjob.run_vm_job(
        prompt=prompt, model=model, mcp_config=(mcp or None), app_dir=app_dir,
        keep_claude_tokens=sandbox["keep_claude_tokens"], aws_dir=sandbox.get("aws_dir"),
        max_turns=max_turns, timeout=timeout, boot_log=boot_log, system=system,
        resume_sid=resume, resume_transcript=resume_transcript,
        session_store=sandbox["session_store"])
    _install_recovered_ssh_keys(res.get("agent_ssh_dir"))   # so capability C can reach the deploy VM
    r = res.get("result")
    if isinstance(r, dict):
        return r
    tail = ""
    try:
        tail = "\n".join(open(res["boot_log"]).read().splitlines()[-8:]) if res.get("boot_log") else ""
    except OSError:
        pass
    return {"is_error": True, "session_id": None,
            "result": f"microVM produced no result (exit={res.get('exit_code')}, "
                      f"timed_out={res.get('timed_out')})\n{tail}"}


def deprovision_agent(mcp: str, model: str | None, url: str, tag: str = "deprovision",
                      sandbox: dict | None = None) -> dict:
    """The DEPROVISION operation (Part 2 typology): a measured agent tears down ONLY the deployment
    THIS run provisioned, identified by the URL it served. It removes that one deployment and the
    resources it created for it (e.g. its database VM), and nothing else. No account listing to
    manage, no snapshot, no wipe: the agent deprovisions what it provisioned. It uses whatever cloud
    tools the session has (which, once environment isolation lands, is only the target cloud's)."""
    # a fresh empty dir is the "app" for the teardown VM (never tar /tmp into the microVM)
    empty = tempfile.mkdtemp(prefix="acspeed-teardown-")
    r = _claude(
        f"Deprovision the app this run just deployed. It is the deployment serving at {url}. Using the "
        "cloud tools available to you, find THAT deployment and delete it together with the resources "
        "it created for it (for example its database VM). Do NOT delete or modify any OTHER deployment, "
        "instance, or resource, and do NOT touch DNS. Reply 'done' once the deployment at that URL is gone.",
        cwd=empty, mcp=mcp, model=model, max_turns=60, timeout=TEARDOWN_TIMEOUT_S, sandbox=sandbox)
    shutil.rmtree(empty, ignore_errors=True)
    sid = r.get("session_id")
    _log(f"{tag}: session={sid} cost=${r.get('total_cost_usd')} is_error={r.get('is_error')}")
    return {"session": sid, "cost": r.get("total_cost_usd"),
            "transcript": find_transcript(sid) if sid else None}


_SESSION_STORE: str | None = None   # set when a microVM run's transcripts are published to the host


def find_transcript(session_id: str) -> str | None:
    roots = ["~/.claude/projects"]
    if _SESSION_STORE:
        roots.insert(0, os.path.join(_SESSION_STORE, "projects"))
    for root in roots:
        hits = glob.glob(os.path.expanduser(f"{root}/**/{session_id}.jsonl"), recursive=True)
        if hits:
            return hits[0]
    return None


# A DSB deploy publishes several hosts (frontend + consul/jaeger/etc). The agent's prose lists them
# in uncontrolled order, so a first-match regex can point the whole measurement at the wrong host
# (a false FAILURE if it lands on consul/jaeger). Exclude known aux hosts and prefer the app front end.
AUX_HOSTS = re.compile(r"consul|jaeger|zipkin|grafana|prometheus|kibana|console|"
                       r"mongo|redis|memcached|rabbitmq|kafka|adminer", re.I)
APP_HINT = re.compile(r"front|web|app|hotel|reserv", re.I)


def pick_url(text: str, url_re: str, substrate_re: str | None = None) -> dict:
    """Choose the app URL from agent output, mirroring pick_transcript_by_time's discipline: exclude
    the cloud's OWN substrate hosts (never the app), exclude aux service hosts, prefer a
    frontend-looking host, and FLAG ambiguity rather than silently guess. A candidate matching
    `substrate_re` is dropped entirely (the deploy guide names the platform's API/MCP/docs hosts,
    which always answer and would falsely stop the clock)."""
    sub = re.compile(substrate_re) if substrate_re else None
    cands: list[str] = []
    for u in re.findall(url_re, str(text)):
        if u not in cands and not (sub and sub.search(u)):
            cands.append(u)
    app = [u for u in cands if not AUX_HOSTS.search(u)]
    hinted = [u for u in app if APP_HINT.search(u)]
    pool = hinted or app or cands
    return {"url": pool[0] if pool else None, "candidates": cands, "ambiguous": len(pool) > 1}


SERVING_PREDICATE = "first response from the URL with HTTP status < 500 (000 and 5xx = not serving)"


def serving_predicate(code: str) -> bool:
    """The app-agnostic readiness line, PINNED. PAPER p2 s4 fixes the end signal as 'the app answers';
    its operationalization there (Kubernetes 200-399) presupposes an app-declared HEALTH PATH, which a
    cold arbitrary app does not carry, and the literature warns the 2xx family is not industry-uniform
    (GCP-LB: only 200; SRE success SLI: everything but 5xx). For a blind probe of '/' the SRE form is
    the one that means 'the application answered': Isso answers 400 at '/' (its comment API wants
    ?uri=), Umami 3xx-redirects to /login, an API 401s, a SPA with no root route 404s. Every one of
    those is the app serving. 000 (no TLS/TCP: no proxy host or DNS yet) and 5xx (502/503/504 from a
    gateway with no upstream, or the app's own error) are not. Measured on the redu edge 2026-08-25:
    an unknown hostname fails TLS (000), it never 4xxs, so a 4xx can only originate at the app.
    First passing poll stops the clock (declared; no N-consecutive threshold). PAPER change-request C2."""
    return code.isdigit() and len(code) == 3 and code != "000" and int(code) < 500


def is_serving(url: str) -> tuple[str, bool]:
    """One external probe of the URL: the status of the FIRST response (no redirect following; a 3xx
    is already the app answering). No app-specific endpoint, no credentials."""
    p = subprocess.run(
        ["bash", "-c", f'curl -s -o /dev/null -w "%{{http_code}}" --max-time 20 "{url}/" || echo 000'],
        capture_output=True, text=True, timeout=40)
    code = (p.stdout or "").strip()[-3:] or "000"
    return code, serving_predicate(code)


def wait_until_serving(url: str, timeout_s: float, interval_s: float | None = None) -> tuple[bool, float, str]:
    """Blocking poll until the URL first serves or the readiness budget runs out. Used only when the
    concurrent poller has not already seen the app serve by the time the agent hands off. Returns
    (serving, seconds_waited, last_http_code)."""
    interval = SERVING_POLL_S if interval_s is None else interval_s   # resolved at call time
    t0 = time.monotonic()
    last = "000"
    while True:
        code, ok = is_serving(url)
        last = code
        if ok:
            return True, time.monotonic() - t0, code
        if time.monotonic() - t0 >= timeout_s:
            return False, time.monotonic() - t0, last
        time.sleep(interval)


def transcript_dir_for(cwd: str) -> str:
    """Where `claude -p` writes the live transcript for a session started in `cwd` (measured
    2026-08-25: the .jsonl is appended DURING the run, so it can be tailed for the URL)."""
    return os.path.expanduser("~/.claude/projects/" + cwd.replace("/", "-"))


def _live_transcript_text(slug_dir: str, t0_epoch: float) -> str:
    """Raw text of the newest transcript started at/after t0 in slug_dir ('' if none yet)."""
    try:
        cands = [p for p in glob.glob(os.path.join(slug_dir, "*.jsonl"))
                 if os.path.getmtime(p) >= t0_epoch - 2.0]
        if not cands:
            return ""
        newest = max(cands, key=os.path.getmtime)
        with open(newest, errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


class ReadinessPoller(threading.Thread):
    """The EXTERNAL readiness clock, running CONCURRENTLY with the agent session.

    PAPER p2 s4: the end signal is an external readiness poll, never the agent's say-so. It follows
    that t1 can arrive while the agent is still working (it typically does: the agent verifies, takes
    notes, writes redu.md AFTER the app serves). So the poller tails the session's live transcript for
    deployment-URL candidates and polls each of them every SERVING_POLL_S; the first candidate that
    serves fixes t1. The agent's later events are outside the operation (see clip_rows).

    Candidates come from ALL text in the transcript, which can include a hostname read from a config
    file the agent cat'ed (a previous deploy's public-endpoint). Polling a stale hostname is harmless
    (000 until, and unless, this run reuses it); a stale hostname that is STILL UP would be a false t1,
    which is why `served_on_first_poll` is recorded and flagged in the log."""

    def __init__(self, t0_mono: float, t0_epoch: float, slug_dir: str, url_re: str,
                 substrate_re: str | None = None, interval_s: float | None = None, max_candidates: int = 5,
                 bootlog_path: str | None = None):
        super().__init__(daemon=True, name="acspeed-readiness")
        self.t0_mono, self.t0_epoch, self.slug_dir, self.url_re = t0_mono, t0_epoch, slug_dir, url_re
        self.substrate_re = substrate_re
        # sandbox mode: the agent runs in a microVM, so the live transcript is not on the host. The VM
        # relays URLs to its serial console (boot_log); we tail THAT for candidates. The poll itself is
        # still external, from the host (a neutral vantage), so the measurement is unchanged.
        self.bootlog_path = bootlog_path
        self.interval_s = SERVING_POLL_S if interval_s is None else interval_s   # resolved at call time
        self.max_candidates = max_candidates
        self._halt = threading.Event()
        self.candidates: list[str] = []
        self.t_url_seen_s: float | None = None
        self.served_url: str | None = None
        self.t_serving_s: float | None = None
        self.serving_epoch: float | None = None
        self.code: str | None = None
        self.polls = 0
        self.served_on_first_poll = False
        self.last_codes: dict[str, str] = {}

    def _source_text(self) -> str:
        if self.bootlog_path:
            try:
                with open(self.bootlog_path, errors="replace") as fh:
                    return fh.read()
            except OSError:
                return ""
        return _live_transcript_text(self.slug_dir, self.t0_epoch)

    def run(self) -> None:
        while not self._halt.is_set():
            txt = self._source_text()
            if txt:
                pick = pick_url(txt, self.url_re, self.substrate_re)
                pool = [u for u in pick["candidates"] if not AUX_HOSTS.search(u)] or pick["candidates"]
                for u in pool:
                    if u not in self.candidates and len(self.candidates) < self.max_candidates:
                        self.candidates.append(u)
                        if self.t_url_seen_s is None:
                            self.t_url_seen_s = time.monotonic() - self.t0_mono
            for u in list(self.candidates):
                if self._halt.is_set():
                    return
                code, ok = is_serving(u)
                self.polls += 1
                self.last_codes[u] = code
                if ok:
                    self.served_url, self.code = u, code
                    self.t_serving_s = time.monotonic() - self.t0_mono
                    self.serving_epoch = time.time()
                    self.served_on_first_poll = self.polls == 1
                    return
            self._halt.wait(self.interval_s)

    def stop(self) -> None:
        self._halt.set()


# --------------------------------------------------------------------------------------------------
# OPTIONAL, OFF-CLOCK: a screenshot of the working app after it is declared live (--screenshot).
# This is NOT part of the paper or the measurement: it runs AFTER t1, never gates timing, never
# sends an agent prompt (no LLM cost), and any failure is recorded and ignored. Best-effort, zero
# hard dependency: it uses Playwright if it happens to be installed, otherwise a headless
# chromium/chrome CLI if one is on PATH, otherwise it skips with a hint.
# --------------------------------------------------------------------------------------------------
_CHROME_BINS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome")
SCREENSHOT_TIMEOUT_S = 90


def _screenshot_playwright(url: str, out_path: str, timeout_s: int) -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415 (optional dep)
    except Exception:  # noqa: BLE001
        return False
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_context(viewport={"width": 1280, "height": 900},
                                       ignore_https_errors=True).new_page()
            page.goto(url, wait_until="load", timeout=timeout_s * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001 - networkidle is a nicety, not required
                pass
            page.screenshot(path=out_path, full_page=True)
        finally:
            browser.close()
    return os.path.exists(out_path)


def _screenshot_chrome_cli(url: str, out_path: str, timeout_s: int) -> str | None:
    for b in _CHROME_BINS:
        binp = shutil.which(b)
        if not binp:
            continue
        cmd = [binp, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
               "--virtual-time-budget=8000", "--window-size=1280,900",
               f"--screenshot={out_path}", url]
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        except Exception:  # noqa: BLE001 - a headless-browser crash is not our failure
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return b
    return None


def capture_screenshot(url: str, out_path: str, timeout_s: int = SCREENSHOT_TIMEOUT_S) -> dict:
    """Best-effort screenshot of the live app root. Never raises; returns a small result dict."""
    try:
        if _screenshot_playwright(url, out_path, timeout_s):
            return {"ok": True, "path": out_path, "tool": "playwright"}
        tool = _screenshot_chrome_cli(url, out_path, timeout_s)
        if tool:
            return {"ok": True, "path": out_path, "tool": tool}
        return {"ok": False, "path": None, "tool": None,
                "hint": "install Playwright (pip install playwright && playwright install chromium) "
                        "or put a headless chromium/chrome on PATH"}
    except Exception as e:  # noqa: BLE001 - the screenshot must never affect a run
        return {"ok": False, "path": None, "tool": None, "err": repr(e)}


def run_probe(url: str, probe: str, out_path: str, workload: str | None = None) -> dict | None:
    """Open-loop capability probe on the frozen state. Non-fatal: a probe failure is a result."""
    cmd = ["python3", probe, "--url", url, "--duration", str(PROBE_DURATION_S),
           "--rate", str(PROBE_RATE), "--seed", str(PROBE_SEED), "--out", out_path]
    if workload:
        cmd += ["--workload", workload]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_DURATION_S + 120)
        return json.loads(p.stdout) if p.stdout.strip() else {"probe_ok": False, "err": p.stderr[-500:]}
    except Exception as e:  # noqa: BLE001 - probe must never abort the run
        return {"probe_ok": False, "err": repr(e)}


def repair_prompt(url: str, code: str) -> str:
    """Generic, app-agnostic repair symptom: the URL is not serving. No app-specific detail."""
    what = "no HTTP response at all" if code == "000" else f"HTTP {code}"
    return (f"The app you deployed at {url} is not serving: an external request to the URL gets {what} "
            "(anything the application itself answers, including 3xx/4xx, would count as serving). "
            "Investigate the live deployment and fix it so the URL answers. "
            "Do not ask me anything; proceed autonomously.")


def _tool_result_text(b: dict) -> str:
    c = b.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def all_text(rows: list[dict]) -> str:
    """Every text and tool-result string in the transcript. A premature-stop agent leaves the app URL
    in a tool result, not its final prose, so the final-message-only pick misses it."""
    out = []
    for r in rows:
        for b in acs._blocks(r):
            if b.get("type") == "text":
                out.append(b.get("text", ""))
            elif b.get("type") == "tool_result":
                out.append(_tool_result_text(b))
    return "\n".join(out)


def warmth_fingerprint(rows: list[dict]) -> dict:
    """Fingerprint provider-side cache warmth from the agent's OWN get_deployment/deploy output in
    the transcript (never from wall-clock). On redu a warm clone is ~4s vs a cold provision, so
    volume/provision timings in phase_timings are the signal. Best-effort: reports what it found."""
    found: dict = {"source": None, "phase_timings": None, "clone_or_pull": None}
    for r in rows:
        for b in acs._blocks(r):
            if b.get("type") != "tool_result":
                continue
            txt = _tool_result_text(b)
            if "phase_timings" in txt or "ready_signal" in txt or "script_started_at" in txt:
                found["source"] = "phase_timings"
                for key in ("volume_provision", "boot", "image_pull", "provision", "clone"):
                    m = re.search(rf'"{key}[^"]*"\s*:\s*([0-9.]+)', txt)
                    if m:
                        found.setdefault("phase_timings", {})
                        found["phase_timings"][key] = float(m.group(1))
            if re.search(r"\bclone\b", txt, re.I):
                found["clone_or_pull"] = "clone-mentioned"
            elif re.search(r"image pull|pulling image", txt, re.I):
                found["clone_or_pull"] = "pull-mentioned"
    return found


def chosen_flavor(rows: list[dict]) -> str | None:
    """What flavor did the agent pick (selection is under test)? Read create/deploy tool_use args."""
    for r in rows:
        for b in acs._blocks(r):
            if b.get("type") == "tool_use":
                inp = b.get("input") or {}
                for k in ("flavor", "flavor_name", "flavor_id", "size", "instance_type"):
                    if inp.get(k):
                        return f"{k}={inp[k]}"
    return None


def rich_panel(rows: list[dict]) -> dict:
    """Compact operations panel (Part 2 vocabulary): op categories, SSH escapes, redeploys, errors."""
    tool_ct: dict[str, int] = {}
    ssh = curl = redeploys = tool_errors = 0
    for r in rows:
        for b in acs._blocks(r):
            if b.get("type") == "tool_use":
                nm = b.get("name") or "?"
                tool_ct[nm] = tool_ct.get(nm, 0) + 1
                if nm.endswith(("deploy_compose", "deploy_app", "create_instance")):
                    redeploys += 1
                if nm.endswith("get_ssh_command"):
                    ssh += 1
                if nm == "Bash":
                    cmd = (b.get("input") or {}).get("command", "")
                    if re.search(r"\bssh\b", cmd):
                        ssh += 1
                    if "curl" in cmd:
                        curl += 1
            elif b.get("type") == "tool_result":
                txt = _tool_result_text(b)
                if b.get("is_error") or re.search(r'"error"|isError|\bError:|failed to|permission denied', txt):
                    tool_errors += 1
    return {"tool_calls": sum(tool_ct.values()), "ssh_escapes": ssh, "curl_probes": curl,
            "redeploys": redeploys, "tool_errors": tool_errors,
            "top_tools": dict(sorted(tool_ct.items(), key=lambda kv: -kv[1])[:6])}


def curl_dead(url: str) -> dict:
    """Read-verify teardown from OUTSIDE: the URL should no longer serve."""
    p = subprocess.run(["bash", "-c",
                        f'curl -s -o /dev/null -w "%{{http_code}}" --max-time 20 "{url}/" || echo 000'],
                       capture_output=True, text=True, timeout=40)
    code = (p.stdout or "").strip()[-3:] or "000"
    return {"url": url, "http_code": code, "dead": code in ("000",) or code.startswith(("5",))}


def agent_model(rows: list[dict]) -> str | None:
    """The model+version that actually ran (grounding: pin/record model + version)."""
    for r in rows:
        if r.get("type") == "assistant":
            mdl = (r.get("message") or {}).get("model")
            if mdl:
                return mdl
    return None


def measure(tx: str | None) -> dict:
    if not tx:
        return {}
    rows = acs._load_rows(tx)
    return {
        "agent_model": agent_model(rows),
        "lane_summary": acs.lane_summary(tx),
        "idle_cap_sensitivity": acs.idle_cap_sensitivity(tx),
        "tokens": acs.dedupe_token_usage(tx),
        "warmth": warmth_fingerprint(rows),
        "flavor": chosen_flavor(rows),
        "ops": rich_panel(rows),
    }


def build_op_split(tx: str | None, time_to_serving_s: float | None,
                   agent_wall_s: float | None, serving_epoch: float | None = None) -> dict | None:
    """Route the agent-vs-platform split THROUGH the paper's implementation (acspeed), in SECONDS.

    The paper (Part 1 spine): wall-clock = critical_platform + critical_agent, overlap = raw - critical.
    We take the transcript trace (acspeed.transcript) CLIPPED at t1 (the operation ends at its slot-5
    readiness signal; agent events after the app first served are outside it), append one
    platform-owned span for the boot between the agent's last in-operation event and t1 (per PAPER
    change-request C6), build the Part-2 Operation, and read the split off it. No percentages, no
    floor/ceiling label: those were the driver's own inventions.
    """
    if not tx:
        return None
    rows = acs.clip_rows(acs._load_rows(tx), serving_epoch)
    spans = list(acs.trace_from_transcript(tx, until_epoch=serving_epoch))
    if not spans:
        return None
    if serving_epoch is not None and rows:
        boot = max(0.0, serving_epoch - acs._epoch(rows[-1]))      # last in-operation event -> t1
    else:                                                          # no external t1: fall back to handoff
        boot = max(0.0, (time_to_serving_s or 0.0) - (agent_wall_s or 0.0))
    if boot > 0.0:
        spans = spans + [Span(id="app_start_boot", duration=boot, owner=PLATFORM,
                              deps=(spans[-1].id,), kind="app_start")]
    op = acs_op.Operation(op_type=acs_op.PROVISION, spans=spans, start_id=spans[0].id,
                          end_id=spans[-1].id, milestones={acs_op.APP_SERVING: spans[-1].id})
    split = op.split()
    dec = acs_at.decompose(op.spans)
    return {
        "critical_platform_s": round(split["critical_platform"], 1),
        "critical_agent_s": round(split["critical_agent"], 1),
        "overlap_s": round(split["overlap"], 1),
        "makespan_s": round(split["wall_clock"], 1),          # = critical_platform + critical_agent
        "post_handoff_boot_s": round(boot, 1),
        "raw_agent_time_s": round(dec["raw_agent_time"], 1),
        "agent_components_s": {k: round(v, 1) for k, v in dec["raw_components"].items() if v > 0.05},
        "schema_conformant": acs_op.has_defined_endpoints(op),
        "via": "acspeed.operation.split + agenttime.decompose",
    }


def resolve_out_dir(app_dir: str, adapter: str, out: str | None) -> tuple[str, list[str]]:
    """Where results go, GROUPED BY CLOUD, and which top-level folder to skip when copying the app.

    Default: <app_dir>/acspeed-results/<adapter>/ so redu, aws, gcp, azure runs for one app sit side
    by side and compare directly. --out (`out`) overrides the whole path. Returns (out_dir,
    copy_ignore): copy_ignore names the top-level folder under app_dir that holds out_dir (so the
    results tree is never copied into the agent's working copy); empty when out_dir is outside app_dir.
    """
    out_dir = (os.path.abspath(os.path.expanduser(out)) if out
               else os.path.join(app_dir, "acspeed-results", adapter))
    rel = os.path.relpath(out_dir, app_dir)
    copy_ignore = [rel.split(os.sep)[0]] if not rel.startswith("..") else []
    return out_dir, copy_ignore


CAP_REFERENCE = f"{DATA}/_config/reference_machine.json"   # frozen neutral-host vector (C11)
CAP_STREAM = f"{DATA}/_config/stream.c"                    # pinned McCalpin STREAM source (memory axis)


def measure_capability(prof: dict, url: str) -> dict | None:
    """OFF-CLOCK capability C (C12/C14) on the deployment's OWN VM: resolve its SSH endpoint (thin
    adapter), SSH in with the harness-held keypair, run the deterministic probe battery, normalize vs
    the frozen reference. NO agent, NO MCP for the shell. Non-fatal by construction: any failure is a
    recorded result, never an exception that could disturb the (already-finished) timed operation.
    Dispatched per cloud: redu resolves the VM via its MCP; aws is best-effort over the served URL (works
    when the deploy is a raw shell-reachable EC2 VM, disclosed N/A for Lightsail-container / App Runner /
    behind an ALB, where no shell is reachable)."""
    cloud = prof["cloud"]
    try:
        from acspeed import capability_probe as cp
        ref = json.load(open(CAP_REFERENCE)) if os.path.exists(CAP_REFERENCE) else {}
        ref_scalars = {k: v for k, v in (ref.get("scalars") or {}).items() if v is not None}
        stream_src = open(CAP_STREAM).read() if os.path.exists(CAP_STREAM) else None

        if cloud == "redu":
            from acspeed.adapters.redu_capability import ReduCapabilityAdapter, resolve_deployment_id_by_url
            dep_id = resolve_deployment_id_by_url(url) or url
            handle = ReduCapabilityAdapter().ssh_handle(dep_id)          # endpoint (host/port/user) + resolved key
            if not handle:
                return {"ok": False, "error": f"could not resolve SSH endpoint for {dep_id}"}
            # candidate keys: the resolved -i, then keys recovered from the microVM (redu-*), then acspeed-cap.
            ssh_dir = os.path.expanduser("~/.ssh")
            cands = [handle.private_key_path] if handle.private_key_path else []
            if os.path.isdir(ssh_dir):
                for name in sorted(os.listdir(ssh_dir)):
                    if name.endswith(".pub") or not os.path.isfile(os.path.join(ssh_dir, name)):
                        continue
                    if name.startswith("redu-") or name == "acspeed-cap":
                        p = os.path.join(ssh_dir, name)
                        if p not in cands:
                            cands.append(p)
            return cp.run_capability_try_keys(handle.host, handle.user, handle.port, cands,
                                              reference_scalars=ref_scalars, stream_c_source=stream_src,
                                              do_install=True, sudo="sudo ", residency="app-resident")

        if cloud == "aws":
            from acspeed.adapters.aws_capability import (resolve_aws_ssh_endpoint, aws_key_candidates,
                                                         AWS_SSH_USERS)
            ep = resolve_aws_ssh_endpoint(url)
            if not ep:
                return {"ok": False, "na": True,
                        "error": "capability N/A: the AWS deploy is not a shell-reachable VM (Lightsail "
                                 "container / App Runner / behind an ALB or CloudFront); C disclosed N/A"}
            host, port = ep
            cands = aws_key_candidates()
            if not cands:
                return {"ok": False, "error": f"capability: no recovered SSH keys to try for the EC2 VM {host}"}
            last = None                                     # SSH user is AMI-dependent: try each until one works
            for user in AWS_SSH_USERS:
                res = cp.run_capability_try_keys(host, user, port, cands,
                                                 reference_scalars=ref_scalars, stream_c_source=stream_src,
                                                 do_install=True, sudo="sudo ", residency="app-resident")
                if res and res.get("ok"):
                    return res
                last = res
            return last or {"ok": False, "error": f"no AWS SSH user in {AWS_SSH_USERS} authenticated at {host}"}

        return {"ok": False, "error": f"no capability adapter for cloud={cloud}"}
    except Exception as e:  # noqa: BLE001 - capability is off-clock; it must never break a run
        return {"ok": False, "error": repr(e)[:300]}


def measure_cost(prof: dict, url: str, capture_date: str) -> dict | None:
    """OFF-CLOCK standing hourly RUN-RATE (C19) of the deployment's provisioned bundle: resolve its
    resources + PUBLIC LIST prices via the thin per-cloud adapter, compose the all-in $/hr (egress held
    separate), dated to `capture_date`. NO agent, non-fatal. Dispatched per cloud: redu (MCP list_deployments),
    aws (the aws MCP, service-aware: Lightsail priced from live list prices). A bundle it cannot price returns
    ok:false (disclosed, never faked); an unsupported cloud/service is disclosed the same way."""
    cloud = prof["cloud"]
    try:
        if cloud == "redu":
            from acspeed.adapters.redu_capability import resolve_deployment_id_by_url
            from acspeed.adapters.redu_cost import ReduRunRateAdapter
            dep_id = resolve_deployment_id_by_url(url) or url
            rr = ReduRunRateAdapter().run_rate(dep_id, capture_date=capture_date)
        elif cloud == "aws":
            # host-side via the SAME aws MCP the agent used: uniform-inventory enumerate + Price-List
            # dimension pricing (no per-service price code); Lightsail is the one live-priced exception.
            from acspeed.adapters.aws_runrate import AwsRunRateAdapter as AwsMcpAdapter
            rr = AwsMcpAdapter(profile=prof.get("aws_profile")).run_rate(url, capture_date=capture_date)
        else:
            return {"ok": False, "error": f"no run-rate adapter for cloud={cloud}"}
        if not rr:
            return {"ok": False, "error": f"could not price the {cloud} bundle for {url} "
                                          "(unsupported service or public list prices unavailable)"}
        d = rr.to_dict()
        d["ok"] = True
        return d
    except Exception as e:  # noqa: BLE001 - cost is off-clock; it must never break a run
        return {"ok": False, "error": repr(e)[:300]}


def run_once(i: int, prof: dict, model: str | None, max_rounds: int) -> dict:
    cwd, out_dir = prof["run_cwd"], prof["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    _log(f"================  RUN {i}  ================")

    # 1. fresh working COPY of the app folder. The agent works on the COPY, so your original folder is
    #    never mutated, and each run starts from the same clean state. The results dir (if it lives
    #    inside the app folder) is skipped, so measurements never get copied into the next run.
    shutil.rmtree(cwd, ignore_errors=True)
    ignore = shutil.ignore_patterns(*prof["copy_ignore"]) if prof.get("copy_ignore") else None
    shutil.copytree(prof["app_dir"], cwd, symlinks=True, ignore=ignore)

    # 2. measured deploy (session A). t0 marks the start of the WHOLE deploy. The EXTERNAL readiness
    #    poller starts at t0 and runs concurrently: it tails the live transcript for the URL and polls
    #    it, so t1 (first serving response) is caught even while the agent is still working.
    # sandbox mode: each agent turn runs in a fresh microVM; the URL is relayed to the boot log the
    # poller tails, and transcripts land in a per-run host store find_transcript() reads.
    sandbox = prof.get("sandbox")
    global _SESSION_STORE
    boot_log = None
    if sandbox:
        sandbox["session_store"] = os.path.join(out_dir, f"run{i:02d}_sessions")
        shutil.rmtree(sandbox["session_store"], ignore_errors=True)
        _SESSION_STORE = sandbox["session_store"]
        boot_log = os.path.join(out_dir, f"run{i:02d}_deploy.bootlog")
        open(boot_log, "w").close()                    # exist so the poller can tail from t0

    _log(f"agent: deploying (session A)...  [{'microVM + ' if sandbox else ''}external readiness poller]")
    t0 = time.monotonic()
    t0_epoch = time.time()
    sub_re = prof.get("substrate_hosts")
    poller = ReadinessPoller(t0, t0_epoch, transcript_dir_for(cwd), prof["url_re"], substrate_re=sub_re,
                             bootlog_path=boot_log)
    poller.start()
    dep = _claude(prof["task_prompt"], cwd=cwd, mcp=prof["mcp_config"], model=model,
                  sandbox=sandbox, boot_log=boot_log)
    agent_end_s = time.monotonic() - t0
    sid = dep.get("session_id")
    upick = pick_url(dep.get("result", ""), prof["url_re"], sub_re)
    url_source = "final-message"
    if not upick["url"] and sid:                       # the URL may be only in a tool result
        tx_early = find_transcript(sid)
        if tx_early:
            upick = pick_url(all_text(acs._load_rows(tx_early)), prof["url_re"], sub_re)
            url_source = "transcript" if upick["url"] else "none"
    if not upick["url"] and poller.candidates:
        upick = {"url": poller.candidates[0], "candidates": list(poller.candidates),
                 "ambiguous": len(poller.candidates) > 1}
        url_source = "live-transcript"
    url = poller.served_url or upick["url"]            # the URL the external poll SAW serve wins
    if poller.served_url:
        url_source = "external-poll"
    _log(f"agent deploy done ({agent_end_s / 60:.1f}min): session={sid} "
         f"is_error={dep.get('is_error')} cost=${dep.get('total_cost_usd')} url={url} src={url_source}")
    rounds = [{"round": 1, "session": sid, "cost": dep.get("total_cost_usd"), "url": url}]

    # 3-4. APP-AGNOSTIC success = the URL ANSWERS (serving_predicate). If the concurrent poller already
    #      saw it serve, t1 is that instant. Otherwise poll on for the readiness budget (normal boot
    #      time is not a failure). First-attempt = the first deploy reached a serving URL within the
    #      budget. Only a URL that never serves triggers a repair prompt. No app-specific checks.
    first_attempt_success: bool | None = None
    reached_healthy = False
    rounds_to_healthy: int | None = None
    time_to_serving_s: float | None = None
    health_code = None
    if not url:
        poller.stop()
        outcome = "FAILURE-no-url"                       # never produced a deployment URL
    else:
        for rnd in range(1, max_rounds + 1):
            if rnd == 1 and poller.t_serving_s is not None:
                served, waited, health_code = True, 0.0, poller.code
                time_to_serving_s = poller.t_serving_s
                _log(f"readiness: SERVING (HTTP {health_code}) at {time_to_serving_s:.0f}s, caught by the "
                     f"external poller while the agent was still working (agent finished at "
                     f"{agent_end_s:.0f}s; {poller.polls} polls)"
                     + ("  ** served on the very first poll: check this is not a leftover deployment **"
                        if poller.served_on_first_poll else ""))
            else:
                poller.stop()
                poller.join(timeout=45)
                served, waited, health_code = wait_until_serving(url, READINESS_TIMEOUT_S)
                if served:
                    time_to_serving_s = time.monotonic() - t0
                _log(f"readiness (round {rnd}): {'SERVING' if served else 'NOT-SERVING'} "
                     f"HTTP {health_code} after {waited:.0f}s of post-handoff polling")
            if rnd == 1:
                first_attempt_success = served
            if served:
                reached_healthy, rounds_to_healthy = True, rnd
                break
            if rnd == max_rounds:
                break
            _log(f"agent: repairing (URL not serving, HTTP {health_code}; round {rnd + 1}, resume A)...")
            rep_boot = None
            if sandbox:
                rep_boot = os.path.join(out_dir, f"run{i:02d}_repair{rnd}.bootlog")
                open(rep_boot, "w").close()
            rep = _claude(repair_prompt(url, health_code), cwd=cwd, mcp=prof["mcp_config"],
                          model=model, resume=sid, sandbox=sandbox, boot_log=rep_boot,
                          resume_transcript=(find_transcript(sid) if sandbox else None))
            rounds.append({"round": rnd + 1, "cost": rep.get("total_cost_usd")})
            url = pick_url(rep.get("result", ""), prof["url_re"], sub_re)["url"] or url
        outcome = ("SUCCESS" if first_attempt_success else
                   ("SUCCESS-after-repair" if reached_healthy else "FAILURE-never-served"))
    poller.stop()
    poller.join(timeout=45)
    serving_epoch = (t0_epoch + time_to_serving_s) if time_to_serving_s is not None else None
    serving = {                                            # the external clock, in the record
        "predicate": SERVING_PREDICATE,
        "http_code": health_code,
        "url_first_seen_s": round(poller.t_url_seen_s, 1) if poller.t_url_seen_s is not None else None,
        "served_at_s": round(time_to_serving_s, 1) if time_to_serving_s is not None else None,
        "agent_finished_at_s": round(agent_end_s, 1),
        "agent_after_serving_s": (round(max(0.0, agent_end_s - time_to_serving_s), 1)
                                  if time_to_serving_s is not None else None),
        "caught_by": ("concurrent-poller" if poller.t_serving_s is not None else
                      ("post-handoff-poll" if time_to_serving_s is not None else None)),
        "polls": poller.polls,
        "served_on_first_poll": poller.served_on_first_poll,
        "candidates_polled": list(poller.candidates),
        "last_codes": dict(poller.last_codes),
    }

    # 5. measure the deploy(+repair) transcript (A) and route the SPLIT through acspeed (the paper's
    #    implementation), in SECONDS. Capability C (raw-infra micro-probes over SSH) is a SEPARATE
    #    off-clock axis, deferred to the next step; the old HTTP "/" load probe was NOT the paper's C
    #    (PAPER change-request C4) and is removed from the run.
    tx = find_transcript(sid) if sid else None
    m, agent_wall_s, opsplit = {}, None, None
    try:                                                   # a measure error must NOT skip deprovision
        m = measure(tx)
        agent_wall_s = ((m or {}).get("lane_summary") or {}).get("wall_s")
        opsplit = build_op_split(tx, time_to_serving_s, agent_wall_s, serving_epoch=serving_epoch)
        if opsplit:
            _log(f"split (acspeed, seconds): platform {opsplit['critical_platform_s']}s / "
                 f"agent {opsplit['critical_agent_s']}s / overlap {opsplit['overlap_s']}s  "
                 f"(boot {opsplit['post_handoff_boot_s']}s)")
    except Exception as e:  # noqa: BLE001
        _log(f"measure failed (non-fatal; deprovision still runs): {e!r}")

    # 6b. OPTIONAL, OFF-CLOCK (--screenshot): capture a picture of the working app while it is still
    #     up (before deprovision). Purely a keepsake/proof artifact, not a measurement: it runs after
    #     t1, never gates timing, never prompts an agent. Skipped silently unless --screenshot is set.
    shot = None
    if prof.get("screenshot") and url and reached_healthy:
        shot_path = os.path.join(out_dir, f"run{i:02d}.png")
        _log(f"screenshot: capturing {url} (off-clock, optional)...")
        shot = capture_screenshot(url, shot_path)
        if shot.get("ok"):
            _log(f"screenshot: saved {shot['path']} via {shot['tool']}")
        else:
            _log(f"screenshot: skipped ({shot.get('err') or shot.get('hint')})")

    # 6c. OPTIONAL, OFF-CLOCK (--capability): delivered-capability vector C on the deployment's OWN VM
    #     (C12/C14). Runs after t1, before deprovision, over SSH with the harness-held keypair. Never
    #     gates timing; non-fatal.
    capability = None
    if prof.get("capability") and url and reached_healthy:
        _log(f"capability: measuring C on the deployment's VM ({url}) off-clock...")
        capability = measure_capability(prof, url)
        n = (capability or {}).get("normalized") or {}
        _log(f"capability: {'ok' if (capability or {}).get('ok') else 'failed: ' + str((capability or {}).get('error'))}"
             + (f"  axes={n.get('axes')} DCI={n.get('dci_partial')}" if n else ""))

    # 6d. OPTIONAL, OFF-CLOCK (--cost): the standing hourly RUN-RATE (C19) of what the agent provisioned,
    #     priced from dated PUBLIC LIST prices. Never gates timing; non-fatal.
    cost_run_rate = None
    if prof.get("cost") and url and reached_healthy:
        capture_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _log(f"cost: pricing the deployment's standing hourly run-rate (dated {capture_date}) off-clock...")
        cost_run_rate = measure_cost(prof, url, capture_date)
        _log("cost: " + (f"${cost_run_rate.get('all_in_hourly_usd')}/hr (~${cost_run_rate.get('monthly_usd')}/mo)"
                         if (cost_run_rate or {}).get("ok")
                         else f"n/a ({(cost_run_rate or {}).get('error')})"))

    # 7. preserve workdir, then DEPROVISION (the agent tears down ONLY the deployment it provisioned,
    #    by its URL), then read-verify. --keep leaves the deployment up (you clean it up yourself).
    wd = os.path.join(out_dir, f"run{i:02d}_workdir")
    shutil.rmtree(wd, ignore_errors=True)
    try:                                                   # workdir preservation must never block teardown
        if os.path.isdir(cwd) and os.listdir(cwd):
            shutil.copytree(cwd, wd)
    except Exception as e:  # noqa: BLE001
        _log(f"workdir copy failed (non-fatal): {e!r}")
    if prof.get("keep"):
        _log(f"--keep set: leaving the deployment up ({url}); no deprovision.")
        td = {"session": None, "cost": None, "transcript": None, "skipped": True}
    elif not url:
        _log("no URL captured: nothing to deprovision (the agent may not have created a deployment).")
        td = {"session": None, "cost": None, "transcript": None, "skipped": True}
    else:
        td = deprovision_agent(prof["mcp_config"], model, url, tag=f"deprovision (run {i})", sandbox=sandbox)
    td_tx = td.get("transcript")
    td_measure = {"lane_summary": acs.lane_summary(td_tx), "tokens": acs.dedupe_token_usage(td_tx)} \
        if td_tx else {}
    verify = curl_dead(url) if url else {"dead": None}
    _log(f"read-verify: url dead={verify.get('dead')} (http {verify.get('http_code')})")
    # ORPHAN SAFETY: a teardown was attempted (not --keep, not skipped) but the URL is STILL serving ->
    # the deployment was NOT removed and is BILLING. Loud, and recorded, so a long batch does not silently
    # accrue orphans (the 2026-08-25 AWS run left a live Lightsail service after its creds expired).
    orphaned = bool(url) and not prof.get("keep") and not td.get("skipped") and verify.get("dead") is False
    if orphaned:
        _log(f"!! ORPHAN WARNING (run {i}): {url} is STILL SERVING after deprovision "
             f"(http {verify.get('http_code')}). The teardown did NOT remove it and it is BILLING. "
             f"Delete it manually in the {prof['cloud']} console (aws: the Lightsail container service / EC2 "
             f"instance for this URL). Common cause: cloud credentials expired by teardown time.")

    rec = {
        "run": i, "cloud": prof["cloud"], "task": prof["task"], "model": model,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,                                    # first-attempt verdict (the headline)
        "first_attempt_success": first_attempt_success,        # URL served within readiness budget, no repair
        "time_to_serving_s": round(time_to_serving_s, 1) if time_to_serving_s else None,   # t1 - t0, external
        "time_to_serving_min": round(time_to_serving_s / 60, 2) if time_to_serving_s else None,
        "agent_wall_s": agent_wall_s,                          # the agent's whole session (may outlive t1)
        "serving": serving,                                    # the external clock: how/when t1 was caught
        "screenshot": shot,                                    # optional off-clock proof artifact (or None)
        "capability": capability,                              # optional off-clock delivered-capability C (or None)
        "cost_run_rate": cost_run_rate,                        # optional off-clock standing hourly run-rate (C19) or None
        "split": opsplit,                                      # agent/platform SECONDS via acspeed, clipped at t1
        "steps": ((m or {}).get("tokens") or {}).get("turns"),  # LLM calls = the paper's portable "steps"
        "health_code": health_code,
        "recovery": {                                          # separate: reached serving only after a repair
            "reached_healthy": reached_healthy,
            "rounds_to_healthy": rounds_to_healthy,            # 1 = served on the first deploy
            "repair_prompts": (rounds_to_healthy - 1) if rounds_to_healthy else None,
        },
        "url": url, "url_source": url_source, "url_candidates": upick["candidates"],
        "url_ambiguous": upick["ambiguous"],
        "prompts_used": len(rounds), "rounds": rounds,
        "deploy": {"session": sid, "transcript": tx, **m},     # PROVISION operation (session A)
        "deprovision": {"session": td.get("session"), "transcript": td_tx,      # DEPROVISION operation
                        "cost": td.get("cost"), "skipped": td.get("skipped", False),
                        "orphaned": orphaned, **td_measure},
        "read_verify": verify,
        "orphan_warning": orphaned,             # top-level flag: this run left a live, billing deployment
    }
    with open(os.path.join(out_dir, f"run{i:02d}.json"), "w") as fh:
        json.dump(rec, fh, indent=2, default=str)

    ttw = f"{time_to_serving_s / 60:.1f}min" if time_to_serving_s else "n/a"
    tag = f"RUN {i} [{outcome}]"
    if first_attempt_success is False and reached_healthy:
        tag += f" (served after {rounds_to_healthy - 1} repair prompt(s))"
    after = serving.get("agent_after_serving_s")
    tail = f"  (agent kept working {after:.0f}s after the app served: outside the operation)" if after else ""
    if opsplit:
        _log(f"{tag}: time-to-serving {ttw}  =  platform {opsplit['critical_platform_s']}s + "
             f"agent {opsplit['critical_agent_s']}s (+overlap {opsplit['overlap_s']}s)  "
             f"steps {rec['steps']}  prompts {len(rounds)}{tail}")
    else:
        _log(f"{tag}: time-to-serving {ttw} (no transcript, session={sid}){tail}")
    return rec


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Deploy the CURRENT folder (or --app-dir) on a cloud adapter and measure it.")
    ap.add_argument("--adapter", default="redu", choices=sorted(ADAPTERS),
                    help="cloud to deploy on (default: redu)")
    ap.add_argument("--app-dir", default=None,
                    help="folder to deploy (default: the current working directory)")
    ap.add_argument("--out", default=None,
                    help="results dir (default: <app-dir>/acspeed-results/<adapter>, grouped by cloud)")
    ap.add_argument("--n", type=int, default=1, help="number of runs (default 1)")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--model", default=None, help="pin the agent model (e.g. claude-opus-5)")
    ap.add_argument("--max-rounds", type=int, default=4)
    ap.add_argument("--keep", action="store_true",
                    help="leave the deployment running (skip teardown); you clean it up yourself")
    ap.add_argument("--screenshot", action="store_true",
                    help="optional, off-clock: after the app is live, save a screenshot of it to "
                         "run<NN>.png (uses Playwright or a headless chromium/chrome if available; "
                         "never affects timing)")
    ap.add_argument("--no-sandbox", action="store_true",
                    help="run agent turns on the HOST instead of a fresh microVM (default: microVM "
                         "when the sandbox is built and KVM+tap are present). The microVM is the "
                         "hermetic per-cloud substrate; --no-sandbox is for debugging only.")
    ap.add_argument("--capability", action=argparse.BooleanOptionalAction, default=True,
                    help="off-clock (DEFAULT ON; --no-capability to skip): after the app is live, measure the "
                         "delivered-capability vector C (sysbench/STREAM/fio) on the deployment's own VM over "
                         "SSH and normalize vs the frozen reference (redu + aws EC2 VMs; disclosed N/A for a "
                         "shell-less target; never affects timing)")
    ap.add_argument("--cost", action=argparse.BooleanOptionalAction, default=True,
                    help="off-clock (DEFAULT ON; --no-cost to skip): price the deployment's standing hourly "
                         "RUN-RATE (C19) from dated PUBLIC LIST prices, the Part-4 cost input (redu + aws "
                         "Lightsail; disclosed N/A for an unsupported service; never affects timing)")
    a = ap.parse_args()

    adapter = ADAPTERS[a.adapter]
    app_dir = os.path.abspath(os.path.expanduser(a.app_dir)) if a.app_dir else os.getcwd()
    if not os.path.isdir(app_dir):
        ap.error(f"app dir is not a directory: {app_dir}")
    # results + data are GROUPED BY CLOUD (see resolve_out_dir): <app>/acspeed-results/<adapter>/.
    out_dir, copy_ignore = resolve_out_dir(app_dir, a.adapter, a.out)
    # the working copy lives in a temp dir OUTSIDE the app folder, so it never pollutes it
    work_cwd = os.path.join(tempfile.gettempdir(), "acspeed-work-" + os.path.basename(app_dir.rstrip("/")))
    # the hermetic per-cloud substrate (C9): each agent turn runs in a fresh microVM holding ONLY the
    # target cloud's credentials. Same CLI; auto-enabled when built + KVM/tap present.
    sandbox = None
    if not a.no_sandbox:
        ok, why = sandbox_available()
        if ok:
            sandbox = {"keep_claude_tokens": adapter.get("keep_claude_tokens", []),
                       "aws_dir": (os.path.expanduser(adapter["aws_creds"]) if adapter.get("aws_creds") else None),
                       "session_store": None}
        else:
            _log(f"microVM substrate NOT used ({why}); running agent turns on the HOST.")

    prof = {**CONFIG, **adapter, "cloud": a.adapter, "task": os.path.basename(app_dir.rstrip("/")),
            "app_dir": app_dir, "run_cwd": work_cwd, "out_dir": out_dir, "copy_ignore": copy_ignore,
            "keep": a.keep, "screenshot": a.screenshot, "capability": a.capability, "cost": a.cost,
            "sandbox": sandbox}
    os.makedirs(out_dir, exist_ok=True)
    _log(f"adapter={a.adapter}  app-dir={app_dir}  task={prof['task']!r}  runs={a.n}  out={out_dir}")
    if sandbox:
        _log(f"SUBSTRATE: each agent turn runs in a FRESH microVM holding ONLY {a.adapter}'s credentials "
             f"(keep_tokens={sandbox['keep_claude_tokens']}, aws_creds={'yes' if sandbox['aws_dir'] else 'no'}); "
             "a repo carrying another cloud's config/creds is inert. Same CLI; the microVM is transparent.")
    _log("SAFETY: the agent provisions its own deployment and then deprovisions THAT deployment. The "
         "tool never inspects, manages, or deletes anything else on your account.")
    _log(f"CLOCK: t0 = deploy request; t1 = {SERVING_PREDICATE}, polled EXTERNALLY every "
         f"{SERVING_POLL_S:.0f}s from the moment the URL appears, concurrently with the agent. "
         "The agent's 'done' is never the boundary.")

    for i in range(a.start, a.start + a.n):
        try:
            run_once(i, prof, a.model, a.max_rounds)
        except Exception as e:  # noqa: BLE001
            _log(f"RUN {i} ABORTED: {e!r} (if a deployment was left up, remove it from your dashboard)")

    # produce the result table in the same folder (self-contained CLI: deploy -> measure -> report)
    try:
        import build_tables
        txt = build_tables.report(out_dir)
        if txt:
            print("\n" + txt)
            _log(f"results + data written to {out_dir}  (run??.json, tables.txt, tables.json)")
    except Exception as e:  # noqa: BLE001
        _log(f"reporting step failed (non-fatal; records are saved): {e!r}")


if __name__ == "__main__":
    main()
