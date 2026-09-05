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
import hashlib
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
from acspeed import suite as acs_suite  # noqa: E402
from acspeed import suites as acs_suites  # noqa: E402
from acspeed import procguard  # noqa: E402
from acspeed.types import Span, PLATFORM, AGENT  # noqa: E402
from acspeed.criticalpath import owner_split as _owner_split  # noqa: E402
import signal  # noqa: E402

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
    if not vmjob.discover_slots():
        return False, "no acspeed tap(s) up (sudo bash sandbox/net-setup.sh [N])"
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
             "cloud_label": "redu.cloud",
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
              "cloud_label": "AWS",
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
    # GCP: mcp_config points at the Cloud Run MCP (@google-cloud/cloud-run-mcp), the one official
    # create-capable GCP MCP; Compute Engine / GKE / App Engine go through `gcloud` over Bash. Auth = ADC
    # (a service-account key for a batch run), mounted into the microVM as ~/.config/gcloud. url_re is a
    # strict WHITELIST of GCP's APP-HOSTING suffixes ONLY (a broad `googleapis.com` matches every GCP API
    # endpoint and would falsely stop the clock; `googleusercontent.com` is NOT excluded because it doubles
    # as the GCE reverse-DNS host). Verified 2026-08-28.  LIVE SEAM: no delete tool in the MCP, so the
    # agent tears down via `gcloud run services delete` (Bash); confirm on first run.
    "gcp":   {"mcp_config": f"{DATA}/_config/gcp.mcp.json",
              "keep_claude_tokens": [], "gcp_creds": "~/.config/gcloud",
              "cloud_label": "GCP",
              "url_re": r"https?://[a-z0-9.-]+\.(?:"
                        r"run\.app"                                # Cloud Run (svc-projnum.region.run.app + hash.run.app)
                        r"|appspot\.com"                           # App Engine (project.REGION.r.appspot.com + legacy)
                        r"|bc\.googleusercontent\.com"             # Compute Engine reverse-DNS PTR (weak fallback; GCE serves on raw IP)
                        r")(?::\d+)?",
              # console/docs/auth + ALL API endpoints (*.googleapis.com) + registries are never the app.
              "substrate_hosts": r"^https?://(?:[a-z0-9.-]*\.)?(?:console\.cloud|cloud|accounts)\.google\.com"
                                 r"|^https?://[a-z0-9-]+\.googleapis\.com"
                                 r"|^https?://(?:[a-z0-9-]+\.)?pkg\.dev"
                                 r"|^https?://(?:[a-z0-9-]+\.)?gcr\.io"},
    # Azure: mcp_config points at the Azure MCP (@azure/mcp); native compute/appservice tools are largely
    # read/query, so the create/deploy/teardown path is the `extension` namespace running `az`/`azd` (LIVE
    # SEAM: exact execute-tool name confirmed on first run; `az`/`azd` over Bash is the guaranteed fallback).
    # Auth = DefaultAzureCredential, mounted into the microVM as ~/.azure (SP env vars for a batch run).
    # url_re whitelists Azure's APP-HOSTING suffixes; NOTE cloudapp.azure.com is a real VM app host, so a
    # blanket `azure.com` exclude is WRONG (see substrate_hosts, which enumerates infra hosts instead).
    # Verified 2026-08-28.
    "azure": {"mcp_config": f"{DATA}/_config/azure.mcp.json",
              "keep_claude_tokens": [], "azure_creds": "~/.azure",
              "cloud_label": "Azure",
              "url_re": r"https?://[a-z0-9.-]+\.(?:"
                        r"azurecontainerapps\.io"                  # Container Apps
                        r"|azurewebsites\.net"                     # App Service / Functions
                        r"|azurecontainer\.io"                     # Container Instances
                        r"|cloudapp\.azure\.com"                   # VM public-IP DNS name
                        r")(?::\d+)?",
              # portal/ARM/auth/docs/registry are infra; Kudu (scm.azurewebsites.net) is the deploy console,
              # not the app. Never blanket-exclude azure.com (cloudapp.azure.com is a real app host).
              "substrate_hosts": r"^https?://(?:[a-z0-9.-]*\.)?portal\.azure\.com"
                                 r"|^https?://(?:[a-z0-9-]+\.)?management\.azure\.com"
                                 r"|^https?://login\.microsoft(?:online)?\.com"
                                 r"|^https?://graph\.microsoft\.com"
                                 r"|^https?://[a-z0-9.-]+\.scm\.azurewebsites\.net"
                                 r"|^https?://[a-z0-9-]+\.azurecr\.io"},
}

# App-agnostic by design: it deploys the CURRENT folder (or --app-dir) once. No named app, no per-app
# check. The prompt names the TARGET CLOUD and nothing else about WHAT or HOW: "this directory" IS the
# app under test, and {cloud} is the ONLY per-adapter substitution (identical template every cloud, so
# the cross-cloud comparison stays controlled and symmetric). Earlier design left the cloud implicit
# ("only the reachable cloud's creds are present, so the agent must use it"); that premise is empirically
# FALSE (verified 2026-08-27 AWS run): real app folders are NOT cloud-neutral -- they carry other
# providers' deploy docs/config (redu.md, vercel, ...) the user legitimately keeps, and a strong
# cloud-specific doc CAPTURES the agent's plan. That run had aws-mcp + call_aws AVAILABLE and UNUSED:
# the agent followed the folder's redu.md, could not auth redu (no token in the VM, by design), and
# stopped without ever trying AWS. Stripping the folder is rejected (it is the user's). So the target
# cloud is stated EXPLICITLY and symmetrically in the prompt: a disclosed, controlled instruction, not a
# contaminant (the paper's C9 "prompt stays pure" note is updated to "prompt names only the target cloud").
CONFIG = {
    "probe": f"{HERE}/probe_loadtest.py",            # unused now; kept for the future app-capability axis (C4)
    # {cloud} is filled from the adapter's cloud_label at prof assembly. SAME template for every cloud.
    "task_prompt_template": ("Deploy the application in this directory to {cloud}, using the {cloud} "
                             "tools and credentials available in this session. If the directory contains "
                             "configuration or notes for other providers, ignore them and deploy only to "
                             "{cloud}."),
}

# CLOUD-AGNOSTIC run identity (fixes the wrong-VM capture). The harness mints a per-run token and asks
# the agent to put it in the deployment's NAME, so the deployment's own hostname carries it; the URL
# capture then accepts ONLY hostnames bearing this run's token and can never latch onto a pre-existing
# or concurrent deployment on the account (measured 2026-08-28: the poller grabbed a foreign
# `origin-web` URL that a `list_deployments` result had put in the transcript). No cloud specifics live
# here: "does the hostname contain the token" is a pure string test, and every per-cloud detail (url_re,
# substrate hosts) stays in the adapter. ADAPTER SEAM: a cloud whose created URL cannot carry the name
# (AWS App Runner random ids, EC2 IP-DNS) needs a per-cloud token->URL resolver in the adapter; redu /
# GCP Cloud Run / Azure Container Apps + App Service + VMs all put the chosen name in the hostname.
NAMING_INSTRUCTION = (
    " Name the deployment so that its public hostname contains the exact token '{token}' (for example "
    "name it '{token}' or '<app>-{token}'). This token is how this run identifies the one deployment it "
    "created among any others already on the account, so it MUST appear in the deployment's URL and must "
    "not be reused for anything else. If the folder's notes suggest a specific deployment name, hostname, "
    "or dname, OVERRIDE it for this run: whatever name you choose, its hostname must still contain this "
    "exact token."
)
# The SAME token-naming rule, applied to a LATER provision (Medium's second site). Without it the second
# site was deployed under a fixed name (e.g. 'alcove'), so successive runs collided on the same shared
# service (broke run-independence) and the token-scoped reaper could not find it to tear it down. Reusing
# the identical mechanism the primary already relies on makes the second site collision-free and reapable
# on any cloud (naming is platform-agnostic). Enforced by the SITE_B_URL token check in drive_suite.
SECOND_SITE_NAMING = (
    " Name this second site so that its public hostname is '{name}-{token}': its own name '{name}' followed "
    "by this run's token '{token}' (the SAME token already in the first app's URL). The token is how this "
    "run finds and tears down the second site and keeps it distinct from other runs; the name '{name}' keeps "
    "it distinct from the first app when both are running at once. Use exactly this hostname prefix; if the "
    "application's source suggests a different fixed name, override it to '{name}-{token}'."
)
# generic fallback when the suite did not name the second site (a non-umami second site): token only, no
# marker; the primary is still picked by its own name, so a token-only second site is simply not the primary.
SECOND_SITE_NAMING_GENERIC = (
    " Name this second site so that its public hostname carries this run's token '{token}' (the SAME token "
    "already in the first app's URL, so this run finds and tears it down); it must be a DIFFERENT hostname "
    "from the first app."
)
# Cloud-agnostic autonomy wrapper (proceed non-interactively). NOT task guidance.
AUTONOMY = (
    "This is a fully autonomous, non-interactive session. Never ask the user questions and never "
    "wait for confirmation: make reasonable default choices and proceed to completion. Whenever a "
    "tool or workflow offers an automatic / default / 'yolo' option, choose it. Keep going until the "
    "task is genuinely done or you have exhausted your options, then stop. "
    "You get exactly ONE session and will NOT be re-invoked, so complete the WHOLE task within this "
    "turn: never arm a background waiter, monitor, or scheduled wake-up and then stop, and never hand "
    "off unfinished work expecting to be resumed. If a resource you created is still provisioning (a "
    "managed database in particular can take several minutes to become ready), wait for it IN THE "
    "FOREGROUND (sleep, then re-check, in a loop) and then finish the remaining steps yourself. A "
    "deployment is not done until its public URL actually responds, so do not stop before you have "
    "created every part and confirmed the URL serves."
)
DEPLOY_TIMEOUT_S = 3000    # a full build can run long; the agent's own turn budget
TEARDOWN_TIMEOUT_S = 1200   # the AGENT-driven teardown must have time to finish removing EVERYTHING it built
                            # (measured 2026-08-31: aws ~510s / azure ~513s Container Apps teardowns were killed
                            # at the old 500s -> the turn returned session=None mid-delete, leaving orphaned RGs).
                            # Agent-driven teardown is the universal path; the reaper is only the backstop.
READINESS_TIMEOUT_S = 300   # after the agent hands off, how long to let the app finish booting/serving
VM_BOOT_RETRIES = 2         # a microVM turn that yields NO agent result (empty out.json / session=None, the
                            # guest torn down before claude wrote its result - exit 143) is an infra boot-flake,
                            # not an agent outcome; retry it in a fresh VM this many times. Measured 2026-08-31:
                            # one flake on the no-retry deploy-site-b op sank an otherwise-authenticated run.
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
    # spawn in its OWN session (process-group leader) with stdin detached, so Ctrl-C reaches the host
    # and the signal handler can group-kill claude AND its MCP-server grandchildren (procguard).
    proc = procguard.spawn(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out_s, err_s = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        procguard.kill_group(proc.pid)          # kill claude AND the MCP servers it spawned
        try:
            out_s, err_s = proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            out_s, err_s = "", ""
        return {"is_error": True, "result": f"TIMEOUT after {timeout}s", "session_id": None,
                "stdout": (out_s or "")[-2000:]}
    finally:
        procguard.untrack(proc.pid)
    out = (out_s or "").strip()
    try:
        return json.loads(out)
    except ValueError:
        return {"is_error": True, "result": out[-2000:], "stderr": (err_s or "")[-2000:], "session_id": None}


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
    path. Credential scoping (only the target cloud's tokens) comes from the adapter's sandbox config.

    A microVM occasionally yields NO agent result: the guest is torn down before `claude` writes out.json
    (empty out.json, session=None, exit 143). That is an infrastructure boot-flake, not an agent outcome -
    the agent never ran to completion, so nothing was provisioned - so a fresh VM is retried up to
    VM_BOOT_RETRIES times. A turn that returns a real result (a session id, even an error) is NEVER retried,
    so a genuine agent failure is never masked and a partial deploy is never double-provisioned."""
    last: dict = {"is_error": True, "session_id": None, "result": "microVM never produced a result"}
    for attempt in range(1, VM_BOOT_RETRIES + 2):
        res = vmjob.run_vm_job(
            prompt=prompt, model=model, mcp_config=(mcp or None), app_dir=app_dir,
            keep_claude_tokens=sandbox["keep_claude_tokens"], aws_dir=sandbox.get("aws_dir"),
            creds_mounts=sandbox.get("creds_mounts"),
            max_turns=max_turns, timeout=timeout, boot_log=boot_log, system=system,
            resume_sid=resume, resume_transcript=resume_transcript,
            session_store=sandbox["session_store"])
        _install_recovered_ssh_keys(res.get("agent_ssh_dir"))   # so capability C can reach the deploy VM
        try:
            r = res.get("result")
            if isinstance(r, dict):
                return r
            tail = ""
            try:
                tail = "\n".join(open(res["boot_log"]).read().splitlines()[-8:]) if res.get("boot_log") else ""
            except OSError:
                pass
            last = {"is_error": True, "session_id": None,
                    "result": f"microVM produced no result (exit={res.get('exit_code')}, "
                              f"timed_out={res.get('timed_out')})\n{tail}"}
        finally:
            # Reclaim this run's ~4 GB rootfs copy. run_vm_job copies BASE_ROOTFS fresh per turn into an
            # acspeed-vm-* dir and never removes it; unbounded accumulation is what filled the disk and
            # starved a cloud in an n=10 x 4 batch. Guard on the prefix so a caller-provided work_dir is
            # never touched.
            #   SUCCESS -> drop the whole dir (transcripts are already merged into session_store, SSH keys
            #             installed; nothing left we need).
            #   FAILURE -> KEEP the small diagnostics (out.json = claude's own error, boot.log = console,
            #             err.txt = agent stderr) so the cause is knowable, but still drop the ~4 GB rootfs +
            #             job drive so the disk cannot fill. Deleting these on failure is what left us blind.
            wd = res.get("work_dir")
            if wd:
                # Adopt any Claude-login refresh the VM did (it holds the refresh token so a long run can renew
                # the ~8h token), so the host is not left holding a rotated-out token -> no mid-work re-auth.
                # Must run BEFORE the rootfs image below is deleted.
                try:
                    vmjob.sync_claude_login_from_vm(os.path.join(wd, "rootfs.ext4"))
                except Exception:  # noqa: BLE001 - best-effort; never break the run
                    pass
            if wd and os.path.basename(wd).startswith("acspeed-vm-"):
                r = res.get("result")
                succeeded = isinstance(r, dict) and not r.get("is_error")
                if succeeded:
                    shutil.rmtree(wd, ignore_errors=True)
                else:
                    try:
                        vmjob._debugfs_dump(os.path.join(wd, "job.ext4"), "/err.txt", os.path.join(wd, "err.txt"))
                    except Exception:  # noqa: BLE001
                        pass
                    for big in ("rootfs.ext4", "job.ext4", "agent_ssh.tar", "transcripts.tar"):
                        try:
                            os.remove(os.path.join(wd, big))
                        except OSError:
                            pass
                    _log(f"run FAILED -> kept diagnostics in {wd} (out.json / err.txt / boot.log); dropped the rootfs copy")
        if attempt <= VM_BOOT_RETRIES:
            _log(f"microVM produced no agent result (boot-flake, exit={res.get('exit_code')}); retrying "
                 f"the VM turn in a fresh microVM (attempt {attempt + 1}/{VM_BOOT_RETRIES + 1})")
    return last


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


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def drive_suite(inst, prof: dict, model: str | None, deploy_sid: str | None, url: str,
                sandbox: dict | None, out_dir: str, i: int, cwd: str) -> dict | None:
    """Drive a TIER instance's operations on top of the already-serving deployment (operation #1,
    deploy-serve, was performed by run_once). Operations #2..N are resumed agent turns (session A) so
    the deployment context persists; after EACH, the RUNNER verifies that operation's postcondition
    independently (the observe primitive, never the agent's report). The terminal conjunction then
    re-checks every DURABLE sentinel AFTER the restart (CP7). The agent OWNS the method for every
    mutation; only the verified result is scored. Off-clock relative to t1. Returns the TierRun
    summary dict. The instance is built by the caller (run_once) and shared with the teardown, so the
    same per-run sentinel drives both. autorun knows nothing app-specific: the instance carries it."""
    first_id = inst.operations[0].op_id

    def run_agent(op, ctx, resume_sid):
        if op.op_id == first_id:            # the deploy run_once already did: reuse it, do NOT re-deploy
            return {"session_id": deploy_sid, "url": url}
        rsid = resume_sid or deploy_sid     # every later op resumes session A (full deployment context)
        op_boot = None
        if sandbox:
            op_boot = os.path.join(out_dir, f"run{i:02d}_op_{_slug(op.op_id)}.bootlog")
            open(op_boot, "w").close()
        task = op.task
        if op.op_type == acs_op.PROVISION and op.op_id != first_id and prof.get("run_token"):
            # a LATER provision is Medium's second site: name it '<its own name>-<token>' (one name, no
            # marker) so it is collision-free across runs, reapable by token, and distinct from the primary,
            # which the harness selects by ITS name. If the suite did not name it, fall back to token-only.
            second_name = prof.get("second_site_url_name") or ""
            task = op.task + (SECOND_SITE_NAMING.format(name=second_name, token=prof["run_token"])
                              if second_name else SECOND_SITE_NAMING_GENERIC.format(token=prof["run_token"]))
        r = _claude(task, cwd=cwd, mcp=prof["mcp_config"], model=model, resume=rsid,
                    sandbox=sandbox, boot_log=op_boot,
                    resume_transcript=(find_transcript(rsid) if sandbox else None),
                    max_turns=120, timeout=DEPLOY_TIMEOUT_S)
        # A later PROVISION operation (e.g. Medium's second site) stands up a NEW public URL. Resolve it
        # from the agent's final message the same cloud-agnostic way run_once resolves the first URL
        # (prof url_re whitelist + substrate exclusion), but keep the FIRST app's URL: pick the candidate
        # whose hostname differs from the umami/deploy URL, and hand it to the engine as this op's url.
        if op.op_type == acs_op.PROVISION and op.op_id != first_id:
            def _hn(u: str) -> str:
                return re.sub(r"^https?://", "", str(u)).split("/")[0].lower()
            res_text = str(r.get("result", ""))
            # DETERMINISTIC first: the op asked the agent to end with `SITE_B_URL: <url>`. This is
            # cloud-agnostic (no url_re guessing) so a second site on any host - S3, a bucket, App
            # Runner, a container - is captured even if its suffix is not in the deploy url_re whitelist.
            m = re.search(r"(?im)^\s*SITE_B_URL:\s*(https?://\S+)\s*$", res_text)
            newu = m.group(1).strip() if m else None
            if newu and _hn(newu) == _hn(url or ""):
                newu = None  # guard: must be distinct from the umami/deploy URL
            if not newu:
                # fallback: a cloud-pattern URL in the report that is not the umami/deploy URL
                picked = pick_url(res_text, prof["url_re"], prof.get("substrate_hosts"))
                newu = next((u for u in picked["candidates"] if _hn(u) != _hn(url or "")), None)
            # ENFORCE the token: a second-site URL that does not carry this run's token is a collision risk
            # (a fixed / shared name) and is not reapable, so reject it. The op then re-verifies and the agent,
            # which was told to token-name the site, redeploys it correctly. Mirrors the primary require_token.
            rt = (prof.get("run_token") or "").lower()
            if newu and rt and rt not in newu.lower():
                _log(f"suite op {op.op_id}: SITE_B_URL {newu} does NOT carry the run token '{rt}' "
                     f"(collision risk / not reapable); rejecting so the op re-verifies for a token-named site")
                newu = None
            if newu:
                r["url"] = newu
                _log(f"suite op {op.op_id}: resolved a new provision URL {newu}")
            else:
                _log(f"suite op {op.op_id}: no distinct new URL in the agent's report "
                     f"(no SITE_B_URL line; candidates="
                     f"{pick_url(res_text, prof['url_re'], prof.get('substrate_hosts'))['candidates']})")
        # FOLLOW a site-B re-home on a LATER (non-provision) op: the agent re-fronts site B with TLS during
        # integrate (aws sslip.io) to make the beacon fire, moving it off the deploy-time URL. Update
        # site_b_url so the visit + wiring reads target what the agent finalized, not the abandoned URL. Strict
        # no-op for a single-URL deploy (gcp/azure/redu emit one SITE_B_URL). See _rehomed_site_b.
        if op.op_id != first_id and op.op_type != acs_op.PROVISION:
            cand = _rehomed_site_b(r.get("result", ""), ctx.state.get("site_b_url"), url, prof.get("run_token"))
            if cand:
                code, ok = is_serving(cand)
                if ok:
                    prev = ctx.state.get("site_b_url")
                    ctx.state["site_b_url"] = cand
                    _log(f"suite op {op.op_id}: site B re-homed to {cand} (following the agent off {prev})")
                else:
                    _log(f"suite op {op.op_id}: agent's new SITE_B_URL {cand} not serving (http {code}); "
                         f"kept {ctx.state.get('site_b_url')}")
        _log(f"suite op {op.op_id}: agent session={r.get('session_id')} "
             f"is_error={r.get('is_error')} cost=${r.get('total_cost_usd')}")
        return r

    _log(f"suite: driving tier '{inst.tier}' instance '{inst.name}' "
         f"({len(inst.operations)} operations) on the serving deployment ({url}) ...")
    ctx = acs_suite.OpContext(url=url)
    if prof.get("run_token"):
        ctx.state["run_token"] = prof["run_token"]  # lets a verify match a service by identity across its alternate URLs
    run = acs_suite.run_tier(inst, ctx, run_agent=run_agent, log=_log)
    _log(f"suite: tier {inst.tier} / {inst.name}: "
         f"{'PASSED' if run.passed else 'FAILED (' + ', '.join(run.failures()) + ')'}")
    summ = run.summary()

    # SLOT 7 (Part 2): every operation gets a critical-platform vs critical-agent split, not just deploy.
    # The deploy op keeps run_once's own split (its window is empty here - it was performed before the
    # suite). Each later op and each restart cycle is sliced from session A by its wall window.
    txA = find_transcript(deploy_sid) if deploy_sid else None
    if txA:
        for row in summ.get("operations", []):
            if row["op_id"] == first_id:
                continue  # deploy-serve: the main run_once split is authoritative
            sp = op_window_split(txA, row.get("started_at"), row.get("verified_at"))
            if sp:
                row["split"] = sp
        for row in (summ.get("durability") or {}).get("cycles", []):
            sp = op_window_split(txA, row.get("started_at"), row.get("verified_at"))
            if sp:
                row["split"] = sp
    # expose the second-site URL so the teardown can VERIFY it stopped serving (a deleted deploy stops
    # answering), not just trust the agent's "done".
    summ["site_b_url"] = ctx.state.get("site_b_url")
    return summ


def deprovision_suite_agent(prof: dict, model: str | None, url: str, sid: str | None,
                            sandbox: dict | None, out_dir: str, i: int, cwd: str,
                            teardown_hint: str = "", verify_urls: Sequence[str] = ()) -> dict:
    """Teardown for a TIER run. A Medium/Hard deployment creates MULTIPLE resources, so teardown
    RESUMES session A (the agent that built them all) and removes everything this run created: a fresh
    session cannot reliably identify a second site, but the builder can. WHAT to remove comes from the
    instance's ``teardown_hint`` (app-specific), so this orchestrator stays app-agnostic.

    The agent's 'done' is NOT trusted (a run was seen reporting done while every resource stayed live):
    after each teardown turn we VERIFY by polling every known public URL (the primary + any second site),
    since a deleted deployment stops answering. Any URL still serving triggers a forceful re-delete turn,
    up to a few rounds; a survivor after that is flagged ``orphaned`` with the live URLs for cleanup."""
    what = teardown_hint or "the application, its datastore, and any other resources you created"
    urls = [u for u in ([url] + list(verify_urls)) if u]

    # Teardown runs on the HOST, never in the hermetic microVM, even for a sandboxed run. Teardown is NOT the
    # measured operation, so it needs no isolation; and the deprovision microVM was the ONE thing that failed
    # (its boot flaked under 4-way contention and returned an empty result, session=None, orphaning azure/aws
    # 2026-08-31). On the host the cloud creds + MCP are present and the builder session is staged for
    # --resume, so the agent-driven teardown -- which removes whatever it built, ANY service or cloud -- is
    # reliable and UNIVERSAL, not a per-service reaper. `sandbox` is intentionally ignored for the turn.
    def _turn(prompt: str, resume: str | None) -> dict:
        _stage_transcript_for_resume(resume, cwd)   # host-side `claude --resume` must find a microVM session
        return _claude(prompt, cwd=cwd, mcp=prof["mcp_config"], model=model, resume=resume,
                       sandbox=None, max_turns=80, timeout=TEARDOWN_TIMEOUT_S)

    r = _turn(
        "Tear down EVERYTHING you deployed during this run, so nothing is left running or billing: "
        f"{what} (the primary deployment is serving at {url}), together with every VM / instance / "
        "managed database / volume / floating IP / proxy host you created for them. Use your cloud tools "
        "to find and delete each one. Do NOT delete or modify any deployment or resource you did not "
        "create in THIS run. Reply 'done' once they are all gone.", sid)
    sid2 = r.get("session_id")
    cost = r.get("total_cost_usd") or 0.0
    orphaned = False
    for attempt in range(3):
        time.sleep(12)                                        # let the deletions take effect
        # teardown liveness uses curl_dead (404/000/5xx = gone), NOT the readiness predicate: a deleted
        # SERVERLESS route answers 404, which the <500 readiness rule would misread as still-alive and
        # cry a FALSE orphan on every clean Cloud Run / App Runner teardown (seen on gcp 2026-08-30).
        alive = [u for u in urls if not curl_dead(u)["dead"]]
        if not alive:
            break
        if attempt == 2:
            orphaned = True
            _log(f"deprovision (suite, run {i}): ORPHAN WARNING - still serving after 3 turns: {alive}")
            break
        _log(f"deprovision (suite, run {i}): still serving {alive} - forcing another teardown turn")
        rr = _turn(
            f"These deployments are STILL LIVE and serving after your teardown: {alive}. You did NOT "
            "delete them. Use your cloud tools to DELETE each one NOW - the deployment, its VM/instance, "
            "its MANAGED DATABASE, its volume, and its floating IP - and confirm each stops responding.",
            sid2 or sid)
        sid2 = rr.get("session_id") or sid2
        cost += rr.get("total_cost_usd") or 0.0
    _log(f"deprovision (suite, run {i}): session={sid2} cost=${cost} "
         f"is_error={r.get('is_error')} orphaned={orphaned}")
    return {"session": sid2, "cost": cost, "orphaned": orphaned,
            "live_urls": [u for u in urls if not curl_dead(u)["dead"]] if orphaned else [],
            "transcript": find_transcript(sid2) if sid2 else None}


def reap_run(cloud: str, run_token: str, urls=(), *, log=None, sh=None, redu=None) -> dict:
    """LAST-LINE teardown, AFTER the agent's own teardown, using each cloud's RESOURCE API (never a URL
    poll). This matters because URL-death != resource-deletion: on a VM cloud the agent can stop the app or
    remove the proxy (so the URL goes 000) while the deployment VM, its managed DB, and the second site's VM
    keep running and billing - and curl_dead reads 000 as 'gone' and misses them (measured on redu
    2026-08-30: a run reported orphaned=False with the umami VM AND the site-B VM still live). So the reaper
    enumerates what this run actually created and deletes it. SCOPED to this run, two ways: the run TOKEN in
    a resource's name, OR a resource whose hostname equals one of this run's known URLs - the second site is
    named with the probe suffix, not the run token, so URL-host matching is what catches it. redu deletes are
    RE-VERIFIED against a fresh list, so a delete call that TIMES OUT but actually succeeded is counted
    reaped, not a false orphan (measured: delete_database timed out yet the DB was gone). Never raises; a
    resource that genuinely survives deletion is a REAL orphan the batch surfaces. ``sh``/``redu`` are
    injectable for tests; ``urls`` is the primary URL plus any second-site URL (a bare string is accepted)."""
    log = log or _log
    if isinstance(urls, str):
        urls = [urls]
    if not run_token:
        return {"reaped": [], "failed": [], "checked": False}
    tok = run_token.lower()

    def _host(v):
        return re.sub(r"^https?://", "", str(v or "")).split("/")[0].lower()

    hosts = {_host(u) for u in urls if u}
    reaped: list[str] = []
    failed: list[str] = []

    def _sh(args, timeout=180):
        if sh is not None:
            return sh(args, timeout)
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout or ""), (p.stderr or "")
        except Exception as e:  # noqa: BLE001
            return 1, "", repr(e)

    try:
        if cloud == "redu":
            rmh = redu
            if rmh is None:
                from acspeed.adapters import redu_mcp_http as rmh  # type: ignore

            def _deps():
                return (rmh.call_tool("list_deployments", {}) or {}).get("deployments", [])

            def _dbs():
                return (rmh.call_tool("list_databases", {}) or {}).get("databases", [])

            def _gone(kind, rid):
                try:
                    if kind == "db":
                        return str(rid) not in {str(x.get("id")) for x in _dbs()}
                    return int(rid) not in {int(x.get("id")) for x in _deps()}
                except Exception:  # noqa: BLE001
                    return False

            def _del(kind, tool, args, label):
                try:
                    rmh.call_tool(tool, args)
                    reaped.append(label)
                except Exception as e:  # noqa: BLE001 - a timeout may have deleted it anyway: re-verify
                    if _gone(kind, args["id"]):
                        reaped.append(label + " (confirmed gone after a timeout)")
                    else:
                        failed.append(f"{label}: {e!r}")

            # every deployment this run created: token in name/dname/access_point, OR hostname == a known
            # URL (catches the second site, whose name carries the probe suffix, not the run token).
            matched_db_ids: set[str] = set()
            try:
                for r in _deps():
                    blob = f"{r.get('name','')} {r.get('dname','')} {r.get('access_point','')}".lower()
                    by_host = _host(r.get("dname")) in hosts or _host(r.get("access_point")) in hosts
                    if tok in blob or by_host:
                        if r.get("db_id"):
                            matched_db_ids.add(str(r.get("db_id")))
                        _del("dep", "delete_deployment", {"id": int(r.get("id"))},
                             f"redu deployment {r.get('name')} (#{r.get('id')})")
            except Exception as e:  # noqa: BLE001
                log(f"reap redu: list_deployments failed (non-fatal): {e!r}")
            # managed DBs: token-named OR attached to a deployment we just matched (the proven no-URL gap).
            try:
                for r in _dbs():
                    if tok in f"{r.get('name','')}".lower() or str(r.get("id")) in matched_db_ids:
                        _del("db", "delete_database", {"id": str(r.get("id"))},
                             f"redu db {r.get('name')} (#{r.get('id')})")
            except Exception as e:  # noqa: BLE001
                log(f"reap redu: list_databases failed (non-fatal): {e!r}")

        else:
            # aws / gcp / azure: UNIVERSAL token-scoped teardown (acspeed/reaper.py). It enumerates EVERY
            # resource carrying the run token across services (each cloud's own inventory) and deletes it in a
            # retry-until-stable loop, replacing the old per-service branches that reaped only RDS / Cloud SQL /
            # one resource-group pattern and leaked everything else (security groups, IAM roles, log groups,
            # load balancers, Cloud Run services, Artifact Registry). ``sh`` is threaded through for tests.
            from acspeed.reaper import reap_universal
            ru = reap_universal(cloud, run_token, list(urls), dry_run=False,
                                run=(sh if sh is not None else None), log=log)
            reaped.extend(ru["deleted"])
            for f in ru["failed"]:
                failed.append(f"{f['res']} ({f['reason']})" if isinstance(f, dict) else str(f))
    except Exception as e:  # noqa: BLE001 - the reaper must never break the run loop
        log(f"reap {cloud}: unexpected error (non-fatal): {e!r}")

    if reaped:
        log(f"reaper ({cloud}, token {run_token}): deleted {len(reaped)} leftover resource(s) the agent "
            f"teardown MISSED (would otherwise bill unattended): {reaped}")
    if failed:
        log(f"reaper ({cloud}, token {run_token}): FAILED to delete {len(failed)} resource(s) - REAL "
            f"ORPHAN, clean up manually: {failed}")
    return {"reaped": reaped, "failed": failed, "checked": True}


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


def _stage_transcript_for_resume(sid: str | None, cwd: str) -> None:
    """Make a session that ran INSIDE a microVM resumable by a HOST-side ``claude --resume <sid>``: copy its
    published transcript (located by find_transcript, e.g. in the per-run session store) into the host cwd's
    project dir. No-op if there is no sid/transcript or it is already staged there. Best-effort; never raises.
    This is what lets teardown run on the host (see deprovision_suite_agent) instead of a flaky microVM."""
    if not sid:
        return
    tx = find_transcript(sid)
    if not tx:
        return
    dst = os.path.join(transcript_dir_for(cwd), f"{sid}.jsonl")
    try:
        if os.path.abspath(tx) == os.path.abspath(dst):
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(tx, dst)
    except OSError:
        pass


# A DSB deploy publishes several hosts (frontend + consul/jaeger/etc). The agent's prose lists them
# in uncontrolled order, so a first-match regex can point the whole measurement at the wrong host
# (a false FAILURE if it lands on consul/jaeger). Exclude known aux hosts and prefer the app front end.
AUX_HOSTS = re.compile(r"consul|jaeger|zipkin|grafana|prometheus|kibana|console|"
                       r"mongo|redis|memcached|rabbitmq|kafka|adminer", re.I)
APP_HINT = re.compile(r"front|web|app|hotel|reserv", re.I)


def pick_url(text: str, url_re: str, substrate_re: str | None = None,
             require_token: str | None = None, primary_name: str | None = None) -> dict:
    """Choose the app URL from agent output, mirroring pick_transcript_by_time's discipline: exclude
    the cloud's OWN substrate hosts (never the app), exclude aux service hosts, prefer a
    frontend-looking host, and FLAG ambiguity rather than silently guess. A candidate matching
    `substrate_re` is dropped entirely (the deploy guide names the platform's API/MCP/docs hosts,
    which always answer and would falsely stop the clock).

    `require_token`: when set, ONLY hostnames carrying this run's token survive (the harness asked the
    agent to name the deployment with it, see NAMING_INSTRUCTION). This is the cloud-agnostic fix for
    latching onto a pre-existing / concurrent-run deployment the agent merely LISTED: a foreign URL
    does not carry this run's token, so it is never a candidate. A pure substring test, no cloud
    knowledge; the per-cloud url_re/substrate stay in the adapter.

    `primary_name`: when set, the PRIMARY app is selected positively by its own name (the agent was told
    to name it '<primary_name>-<token>'). Medium stands up a SECOND site that carries the same token, so a
    token-only pick cannot tell the two apart when both are up at once (Medium B provisions them
    concurrently); matching the primary's name selects it and never the second site, and returns None (keep
    polling) when the primary has not served yet. The second site simply has a different name."""
    sub = re.compile(substrate_re) if substrate_re else None
    cands: list[str] = []
    for u in re.findall(url_re, str(text)):
        if u in cands or (sub and sub.search(u)):
            continue
        if require_token and require_token not in u:   # only THIS run's deployment (its hostname carries the token)
            continue
        cands.append(u)
    app = [u for u in cands if not AUX_HOSTS.search(u)]
    if primary_name:
        # positively select THIS run's primary app by its own name; the second site (a different name) is
        # never the primary, and an empty pool -> None keeps the poller waiting for the primary to serve.
        pool = [u for u in app if primary_name.lower() in u.lower()]
    else:
        hinted = [u for u in app if APP_HINT.search(u)]
        pool = hinted or app or cands
    return {"url": pool[0] if pool else None, "candidates": cands, "ambiguous": len(pool) > 1}


def _rehomed_site_b(result_text: str, current_site_b: str | None, primary_url: str | None,
                    run_token: str | None) -> str | None:
    """A LATER op re-homed site B: return the agent's newest SITE_B_URL if it is a real re-home, else None.

    aws ALBs serve HTTP only, so the agent re-fronts site B with TLS (e.g. via sslip.io) DURING integrate to
    make the tracking beacon fire; that moves site B off its deploy-time URL. If the harness keeps visiting
    the abandoned URL, the visit never registers (integrate 0->0). This detects the move so the caller can
    follow it. A re-home is a SITE_B_URL that (a) carries this run's token, (b) is a different host from the
    PRIMARY app, and (c) is a different host from the CURRENT site B. It is a strict no-op for a single-URL
    deploy (gcp/azure/redu get HTTPS free and emit one SITE_B_URL, so cand == current -> None). The CALLER
    must still confirm the returned URL actually serves before switching. Pure/deterministic for tests."""
    if not (current_site_b and run_token):
        return None
    m = re.search(r"(?im)^\s*SITE_B_URL:\s*(https?://\S+)\s*$", str(result_text))
    if not m:
        return None
    cand = m.group(1).strip()

    def _hn(u: str) -> str:
        return re.sub(r"^https?://", "", str(u)).split("/")[0].lower()

    if (run_token.lower() in cand.lower()
            and _hn(cand) != _hn(primary_url or "")
            and _hn(cand) != _hn(current_site_b)):
        return cand
    return None


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


# --- response-origin check for a 4xx (PAPER p2 s4) -------------------------------------------------
#
# serving_predicate's 4xx branch rests on one measured assumption: "an unknown hostname fails TLS (000),
# it never 4xxs, so a 4xx can only originate at the app". That was measured on ONE edge and does not hold
# on every cloud. MEASURED 2026-09-03 across the batch data: an AWS Lightsail container-service hostname
# answers 404 from the moment DNS exists, so all four Lightsail runs stopped the clock on the FIRST poll
# (t1 160-250s) while the same workload over an ALB, whose t1 is a real app 200, took 436-867s; a GCP
# Cloud Run URL answers 403 from its IAM edge the same way. In each case the off-clock oracle later found
# the real app. The paper already requires the fix: "any adapter whose edge answers 4xx for an unwired
# host must check response origin."
#
# The table is an explicit DENYLIST of edge-generated 4xx bodies/headers. The default is unchanged
# ("a 4xx is the app answering"), so Isso's 400, an API's 401 and a SPA's 404 still stop the clock; only a
# response positively identified as a platform edge is rejected. Every 4xx's evidence is recorded in the
# run json (serving.origin), so an edge NOT in this table surfaces in the data instead of silently
# becoming a fast t1.
EDGE_4XX_SIGNATURES: tuple[tuple[str, str | None, str | None], ...] = (
    # (label, lowercased header substring, lowercased body substring)
    ("aws-alb-no-upstream", "server: awselb", None),
    ("gcp-iam-forbidden", None, "your client does not have permission"),
    ("gcp-frontend-error", None, "error 403 (forbidden)"),
    ("azure-app-unavailable", None, "web app - unavailable"),
    ("azure-app-error", None, ":( application error"),
)


def probe_origin(url: str) -> dict:
    """Headers + a small body sample for a response, so a 4xx can be attributed to the app or to the
    platform edge. Off the critical path in effect: it runs only when a 4xx appears."""
    p = subprocess.run(
        ["bash", "-c",
         f'curl -sS -D - -o - --max-time 20 "{url}/" 2>/dev/null | head -c 4000 || true'],
        capture_output=True, text=True, timeout=40)
    raw = (p.stdout or "")
    head, _, body = raw.partition("\r\n\r\n")
    if not body:
        head, _, body = raw.partition("\n\n")
    low_head, low_body = head.lower(), body.lower()
    edge = None
    for label, hdr, bdy in EDGE_4XX_SIGNATURES:
        if hdr and hdr in low_head and (not bdy or bdy in low_body):
            edge = label
            break
        if bdy and not hdr and bdy in low_body:
            edge = label
            break
    return {"edge": edge, "server": next((l.split(":", 1)[1].strip() for l in head.split("\n")
                                          if l.lower().startswith("server:")), None),
            "body_bytes": len(body), "body_sample": body[:200]}


def is_serving(url: str) -> tuple[str, bool]:
    """One external probe of the URL: the status of the FIRST response (no redirect following; a 3xx
    is already the app answering). No app-specific endpoint, no credentials."""
    p = subprocess.run(
        ["bash", "-c", f'curl -s -o /dev/null -w "%{{http_code}}" --max-time 20 "{url}/" || echo 000'],
        capture_output=True, text=True, timeout=40)
    code = (p.stdout or "").strip()[-3:] or "000"
    return code, serving_predicate(code)


def is_serving_ex(url: str) -> tuple[str, bool, dict | None]:
    """`is_serving` plus the response-origin check on a 4xx: (status, serving?, origin evidence).
    Delegates the probe to `is_serving`, which stays the single probe seam. A 4xx that a platform edge
    generated is NOT serving; anything else keeps `is_serving`'s verdict."""
    code, ok = is_serving(url)
    origin = None
    if ok and code.isdigit() and 400 <= int(code) < 500:
        try:
            origin = probe_origin(url)
        except Exception:  # noqa: BLE001 - a failed origin probe must never fake a t1; keep the 4xx
            origin = {"edge": None, "error": "origin probe failed"}
        if origin.get("edge"):
            ok = False        # the platform edge answered, not the app: keep polling
    return code, ok, origin


def fingerprint_response(url: str, timeout_s: int = 15) -> dict:
    """A cheap body signature for one serving response: (bytes, sig). No redirect follow, so it matches
    `is_serving`'s view of '/'. Used by the durable-serve rule (PAPER C24) to tell one response from
    another WITHOUT knowing the app or the cloud: an edge placeholder and the real app differ in this
    signature. `sig` is a hash of a stable body prefix (forensic only; matching uses code + size)."""
    try:
        p = subprocess.run(["bash", "-c", f'curl -s -m {timeout_s} "{url}/" | head -c 65536'],
                           capture_output=True, text=True, timeout=timeout_s + 5)
        body = p.stdout or ""
    except Exception:  # noqa: BLE001 - a fingerprint failure must never break the poll loop
        return {"bytes": 0, "sig": None}
    return {"bytes": len(body),
            "sig": hashlib.sha1(body[:2048].encode("utf-8", "replace")).hexdigest()[:12]}


def durable_serving(poll_log: list[dict]) -> tuple[float | None, str | None]:
    """PAPER C24: from the log of SERVING polls, return (t_s, code) of the earliest poll whose response
    matches the app's stable FINAL response (the last serving poll). A transient edge placeholder that
    preceded the app differs in (code, body size) and is skipped; a stable app response (even a 4xx at
    root) matches its own final response, so its first poll stands. Match = same status code AND body
    size within tolerance (dynamic content such as CSRF tokens moves the size a little, an edge page moves
    it a lot). Returns (None, None) for an empty log."""
    if not poll_log:
        return None, None
    app = poll_log[-1]                                   # still serving at the end = the app
    tol = max(256, int(0.05 * max(app.get("bytes", 0), 1)))
    for p in poll_log:
        if p.get("code") == app.get("code") and abs(p.get("bytes", 0) - app.get("bytes", 0)) <= tol:
            return p.get("t_s"), p.get("code")
    return app.get("t_s"), app.get("code")               # unreachable (app matches itself), defensive


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
                 bootlog_path: str | None = None, require_token: str | None = None,
                 primary_name: str | None = None):
        super().__init__(daemon=True, name="acspeed-readiness")
        self.t0_mono, self.t0_epoch, self.slug_dir, self.url_re = t0_mono, t0_epoch, slug_dir, url_re
        self.substrate_re = substrate_re
        self.require_token = require_token   # only poll hostnames carrying this run's token (cloud-agnostic)
        self.primary_name = primary_name    # when set, poll ONLY the primary app by name, never the second site
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
        self.origin_checks: list[dict] = []   # every 4xx's response-origin evidence (PAPER p2 s4)
        self.rejected_edge_4xx = 0            # 4xx responses attributed to the platform edge, not the app
        self.poll_log: list[dict] = []        # every SERVING poll {t_s, url, code, bytes, sig} (PAPER C24)

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
                pick = pick_url(txt, self.url_re, self.substrate_re, require_token=self.require_token,
                                primary_name=self.primary_name)
                cand = pick["candidates"]
                if self.primary_name:   # never poll the second site: restrict to the primary app by name
                    cand = [u for u in cand if self.primary_name.lower() in u.lower()]
                pool = [u for u in cand if not AUX_HOSTS.search(u)] or cand
                for u in pool:
                    if u not in self.candidates and len(self.candidates) < self.max_candidates:
                        self.candidates.append(u)
                        if self.t_url_seen_s is None:
                            self.t_url_seen_s = time.monotonic() - self.t0_mono
            for u in list(self.candidates):
                if self._halt.is_set():
                    return
                code, ok, origin = is_serving_ex(u)
                self.polls += 1
                self.last_codes[u] = code
                if origin is not None:
                    # Every 4xx's origin verdict is kept, including the rejections, so a run whose clock
                    # was NOT stopped by an edge 4xx still carries the evidence for that decision.
                    self.origin_checks.append({"url": u, "code": code,
                                               "t_s": round(time.monotonic() - self.t0_mono, 1), **origin})
                    if origin.get("edge"):
                        self.rejected_edge_4xx += 1
                if ok:
                    t_s = time.monotonic() - self.t0_mono
                    fp = fingerprint_response(u)
                    # t_s at full-ish precision so a clean run's durable t1 (derived here) equals the
                    # first-serve time to within rounding, and the recorded served_at_s cannot shift.
                    self.poll_log.append({"t_s": round(t_s, 3), "url": u, "code": code,
                                          "bytes": fp["bytes"], "sig": fp["sig"]})
                    if self.t_serving_s is None:       # FIRST serve: recorded for transparency + fallback
                        self.served_url, self.code = u, code
                        self.t_serving_s = t_s
                        self.serving_epoch = time.time()
                        self.served_on_first_poll = self.polls == 1
                    # PAPER C24: do NOT return on the first serve. Keep polling so a later real-app
                    # response can supersede a transient edge placeholder; the durable t1 is derived
                    # from poll_log after the poller is halted at the end of the deploy turn.
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
    """Read-verify teardown from OUTSIDE: the URL should no longer serve. 'dead' (= torn down, no orphan)
    means the deployment is gone. This is NOT the liveness predicate: for teardown a 404 counts as dead,
    because a deleted SERVERLESS service answers 404 on its now-nonexistent route (Cloud Run's
    `<svc>-<projnum>.<region>.run.app`, and App Engine, return a 404 'page not found' once the service is
    deleted), which the <500 liveness rule would misread as still-alive and flag a FALSE orphan on every
    clean serverless teardown. A 404 on the same URL that served during the run means the route is gone.
    Other 4xx (401/403) are NOT dead: the resource is up but access-gated, a possible lingering orphan;
    5xx and a connection failure (000) are dead as before."""
    p = subprocess.run(["bash", "-c",
                        f'curl -s -o /dev/null -w "%{{http_code}}" --max-time 20 "{url}/" || echo 000'],
                       capture_output=True, text=True, timeout=40)
    code = (p.stdout or "").strip()[-3:] or "000"
    return {"url": url, "http_code": code, "dead": code in ("000", "404") or code.startswith("5")}


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


def op_window_split(tx: str | None, started_at: float | None, verified_at: float | None) -> dict | None:
    """Slot-7 split for a NON-deploy operation (Part 2): slice session A's transcript to the operation's
    wall window [started_at, verified_at] (its slot-1 start and slot-5 end signals, recorded by the suite
    engine) and decompose that slice into critical-platform vs critical-agent, the same spine used for the
    deploy op. The operate-mutate ops (register, integrate) and the restart cycles run inside the resumed
    deploy session, so they are isolated by TIME, not by a separate transcript. Returns None if the window
    holds no timed events."""
    if not tx or started_at is None or verified_at is None:
        return None
    # idle_is_platform: inside a single operation's window, an agent gap too long to be model generation
    # is the agent idle while the cloud does the operation's work -> platform, not agent (PAPER C24/C5).
    spans = acs.trace_from_transcript(tx, since_epoch=started_at, until_epoch=verified_at,
                                      idle_is_platform=True)
    if not spans:
        return None
    split = _owner_split(spans)
    owners = split["owners"]
    cp = owners.get(PLATFORM, {}).get("critical", 0.0)
    ca = owners.get(AGENT, {}).get("critical", 0.0)
    return {
        "critical_platform_s": round(cp, 1),
        "critical_agent_s": round(ca, 1),
        "makespan_s": round(split["makespan"], 1),   # = cp + ca + any held-out idle in the window
        "overlap_s": round(sum(v["overlap"] for v in owners.values()), 1),
        "wall_s": round(max(0.0, verified_at - started_at), 1),
        "via": "acspeed.transcript window + owner_split",
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
    # idle_is_platform: before the app serves, an agent gap too long to be model generation is the agent
    # idle while the cloud provisions -> platform, not agent (PAPER C24/C5; fixes the run10 false floor).
    spans = list(acs.trace_from_transcript(tx, until_epoch=serving_epoch, idle_is_platform=True))
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


def _looks_like_non_app(low: str) -> str | None:
    """Precise, CONSERVATIVE signatures for an infrastructure error / default page that the app-agnostic
    liveness predicate (<500) would still count as 'serving'. Returns the reason, or None if the body
    looks like a real app (a legitimate app 4xx -- Isso's 400, an API's 401 JSON -- is NEVER matched)."""
    if "welcome to nginx" in low:
        return "default nginx page"
    if "apache2 default page" in low or ("it works" in low and "apache" in low):
        return "default apache page"
    if "<error" in low and ("accessdenied" in low or "nosuchbucket" in low or "nosuchkey" in low):
        return "S3/CloudFront access error"
    if "the request could not be satisfied" in low:            # CloudFront generic error page
        return "CloudFront generic error"
    if any(g in low for g in ("502 bad gateway", "503 service temporarily", "504 gateway time")):
        return "gateway error page"
    return None


def verify_served_content(url: str, timeout_s: int = 20) -> dict:
    """OFF-CLOCK SUCCESS ORACLE, SEPARATE from the liveness clock (t1). The <500 predicate correctly stops
    the clock when the HTTP server answers, but it cannot tell the deployed APP from an infrastructure
    error / default page (a CloudFront/S3 AccessDenied, a default nginx page, a gateway error) that also
    answers <500. This fetches the served root and records whether the content is the real app. CONSERVATIVE
    by design: only a known infra-error/default-page signature (or an empty body) sets content_verified
    False; a legitimate app 4xx is not rejected. Never raises; never affects timing. Addresses the
    liveness-only / false-success trap (DeployBench premature-completion, the S3-fronted-hasura 403)."""
    try:
        r = subprocess.run(["curl", "-sS", "-L", "-m", str(timeout_s), "-w", "\n%{http_code}", url],
                           capture_output=True, text=True, timeout=timeout_s + 5)
        out = r.stdout
        code = out.rsplit("\n", 1)[-1].strip() if "\n" in out else ""
        content = out.rsplit("\n", 1)[0] if "\n" in out else out
    except Exception as e:  # noqa: BLE001
        return {"content_verified": None, "error": repr(e)[:120]}
    low = content.lower()
    hit = _looks_like_non_app(low)
    empty = len(content.strip()) < 20
    verified = not hit and not empty
    return {"content_verified": verified, "http_code": code, "bytes": len(content), "signature": hit,
            "note": (hit if hit else ("empty body" if empty else "app content")),
            "disclosure": "off-clock success oracle, SEPARATE from the liveness clock (t1); conservative, "
                          "flags only infra-error/default pages + empty bodies, never a legitimate app 4xx"}


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
            from acspeed.adapters.redu_capability import (ReduCapabilityAdapter,
                                                          resolve_deployment_id_by_url, resolve_network_pair)
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
            # C17 network axis: pair the app VM with the deployment's managed datastore VM (reached by
            # ProxyJump through the app VM to its private IP, same keypair); None only for a true single-VM
            # deploy -> N/A disclosed. Full VM-to-VM iperf3+ping over the tenant private network.
            net_pair = None
            try:
                net_pair = resolve_network_pair(dep_id, handle)
            except Exception:  # noqa: BLE001 - network is one off-clock axis; its failure is disclosed
                net_pair = None
            return cp.run_capability_try_keys(handle.host, handle.user, handle.port, cands,
                                              reference_scalars=ref_scalars, stream_c_source=stream_src,
                                              network_pair=net_pair,
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

        if cloud in ("gcp", "azure"):
            # Both hyperscalers: capability is measurable ONLY on a raw shell-reachable VM (a GCE instance
            # served on its external IP; an Azure VM on its public IP / cloudapp label). The serverless
            # flagships (Cloud Run, Container Apps, App Service, Container Instances) expose an app front,
            # not an instance shell, so C is disclosed N/A there, exactly as AWS App Runner. SSH user is
            # not fixed (metadata-key on GCE; azureuser on Azure), so try each candidate.
            if cloud == "gcp":
                from acspeed.adapters.gcp_capability import (resolve_gcp_ssh_endpoint as _resolve_ep,
                                                             gcp_key_candidates as _keys, GCP_SSH_USERS as _users)
                na_reason = ("capability N/A: the GCP deploy is not a shell-reachable VM (Cloud Run / App "
                             "Engine / serverless); C disclosed N/A")
            else:
                from acspeed.adapters.azure_capability import (resolve_azure_ssh_endpoint as _resolve_ep,
                                                               azure_key_candidates as _keys, AZURE_SSH_USERS as _users)
                na_reason = ("capability N/A: the Azure deploy is not a shell-reachable VM (Container Apps / "
                             "App Service / Container Instances); C disclosed N/A")
            ep = _resolve_ep(url)
            if not ep:
                return {"ok": False, "na": True, "error": na_reason}
            host, port = ep
            cands = _keys()
            if not cands:
                return {"ok": False, "error": f"capability: no recovered SSH keys to try for the {cloud} VM {host}"}
            last = None
            for user in _users:
                res = cp.run_capability_try_keys(host, user, port, cands,
                                                 reference_scalars=ref_scalars, stream_c_source=stream_src,
                                                 do_install=True, sudo="sudo ", residency="app-resident")
                if res and res.get("ok"):
                    return res
                last = res
            return last or {"ok": False, "error": f"no {cloud} SSH user in {_users} authenticated at {host}"}

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
            # DISCOVER from CloudTrail, PRICE from the published disclosure. The old path asked the
            # account's INVENTORY which resources matched this deploy, and every filter in that question
            # (run-token anchor, the URL's region, a per-service short circuit) silently SHRANK the answer
            # instead of failing: it is why aws-medium-b discarded 7 runs, all DISCOVERY failures, and why
            # run21 published $22.00/mo while a live Lightsail database sat beside its containers
            # (measured true cost $36.30). Resource Explorer indexes 654 types over 171 services and does
            # not index Lightsail AT ALL, so no amount of inventory work could have seen it.
            #
            # CloudTrail records every create by default in every account, and AWS must publish the price
            # of everything it bills: of the 22 usagetypes this account was actually billed 2026-09-01..06,
            # 22 are findable in the published price list. So neither half needs per-service code.
            from acspeed.adapters.aws_ct_cost import run_rate_universal
            rr_d = run_rate_universal(prof.get("run_token") or "", capture_date=capture_date,
                                      profile=prof.get("aws_profile"))
            if rr_d is not None:
                return rr_d
            # No token (a single ad-hoc run) leaves nothing to scope discovery by; fall back to the
            # inventory adapter and SAY SO, rather than reporting a confident number from a path whose
            # blind spots are documented above.
            from acspeed.adapters.aws_runrate import AwsRunRateAdapter as AwsMcpAdapter
            rr = AwsMcpAdapter(profile=prof.get("aws_profile")).run_rate(url, capture_date=capture_date)
            if rr is not None:
                d = rr.to_dict()
                d["ok"] = True
                d["discovery"] = "inventory-adapter (no run token to scope CloudTrail); see aws_ct_cost"
                return d
        elif cloud == "azure":
            # Azure Retail Prices API is PUBLIC (no creds) and USD-native, so the PRICING is live-verified;
            # the BUNDLE resolution (what was provisioned) needs the Azure MCP / Resource Graph, the live
            # seam. An unresolved bundle returns ok:false (disclosed), never a faked zero.
            from acspeed.adapters.azure_cost import AzureRunRateAdapter
            rr = AzureRunRateAdapter().run_rate(url, capture_date=capture_date)
        elif cloud == "gcp":
            # GCP Cloud Billing Catalog API needs an API key (GCP_BILLING_API_KEY); without it the bundle
            # is disclosed unpriceable. Compute svc 6F81-5844-456A, Cloud SQL 9662-B51E-5089 (componentized
            # vCPU + RAM SKUs). Bundle resolution via the Cloud Run MCP / gcloud is the live seam.
            from acspeed.adapters.gcp_cost import GcpRunRateAdapter
            rr = GcpRunRateAdapter().run_rate(url, capture_date=capture_date)
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


def _next_run_index(out_dir: str) -> int:
    """The run index a fresh ``--n`` batch APPENDS at: one past the highest existing ``runNN.json`` in
    ``out_dir`` (1 when the folder has none). Matching the RESULT json (not the bootlog) means an incomplete
    run that never produced a result -- e.g. a stale leftover from a prior, differently-sized batch -- has its
    slot REUSED and overwritten, rather than surviving forever as contamination. So repeated ``acspeed-run``
    invocations accumulate clean runs; ``--start N`` overrides this (``--start 1`` restarts a batch)."""
    import glob
    hi = 0
    for p in glob.glob(os.path.join(out_dir, "run*.json")):
        m = re.match(r"run(\d+)\.json$", os.path.basename(p))
        if m:
            hi = max(hi, int(m.group(1)))
    return hi + 1


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

    # CLOUD-AGNOSTIC run identity: mint a per-run token and ask the agent (via the same template for
    # every cloud) to put it in the deployment name, so ONLY this run's hostname is captured. This is
    # the fix for latching onto a foreign/concurrent deployment the agent merely listed. Per-run random
    # (distinct token every run, so a sequential run never matches a prior run's leftover); a caller may
    # pin `prof["run_token"]` for reproducibility / tests.
    run_token = prof.get("run_token") or ("acs" + os.urandom(4).hex())
    prof["run_token"] = run_token   # share it with drive_suite (second-site naming + enforcement) and reap
    # Build the tier instance ONCE, here, so (a) a plan_upfront (Medium B, DISCLOSED) instance discloses the
    # full plan in the deploy prompt below (the agent then schedules with lookahead), and (b) the SAME
    # instance and per-run sentinel drive the suite and the teardown later. Online (Medium A) or no suite:
    # nothing extra is added to the deploy prompt.
    suite_inst = acs_suites.get_instance(prof["suite"]) if prof.get("suite") else None
    # The primary app's URL name (Medium): the harness selects the primary BY NAME so the concurrently
    # provisioned second site (same token, different name) is never mistaken for it. Shared with drive_suite
    # (second-site naming) and the readiness poll via prof.
    primary_name = (getattr(suite_inst, "primary_url_name", "") or None) if suite_inst is not None else None
    prof["primary_url_name"] = primary_name or ""
    prof["second_site_url_name"] = (
        (getattr(suite_inst, "second_site_url_name", "") or "") if suite_inst is not None else "")
    task_prompt = prof["task_prompt"] + NAMING_INSTRUCTION.format(token=run_token)
    if suite_inst is not None and getattr(suite_inst, "plan_upfront", False):
        # DISCLOSED (Medium B): full_plan_preamble already pins BOTH site names and the plan.
        task_prompt = task_prompt + acs_suite.full_plan_preamble(suite_inst, run_token)
    elif primary_name:
        # ONLINE (Medium A) or any suite that names its primary: pin the primary's hostname so it can be
        # selected by name; the second site, named at its later provision op, is then unambiguously not it.
        task_prompt = task_prompt + f" Use '{primary_name}-{run_token}' as this first app's public hostname."
    _log(f"agent: deploying (session A)...  [{'microVM + ' if sandbox else ''}external readiness poller]  "
         f"run-token={run_token} (only a hostname carrying it is this run's deployment)")
    t0 = time.monotonic()
    t0_epoch = time.time()
    sub_re = prof.get("substrate_hosts")
    poller = ReadinessPoller(t0, t0_epoch, transcript_dir_for(cwd), prof["url_re"], substrate_re=sub_re,
                             bootlog_path=boot_log, require_token=run_token, primary_name=primary_name)
    poller.start()
    dep = _claude(task_prompt, cwd=cwd, mcp=prof["mcp_config"], model=model,
                  sandbox=sandbox, boot_log=boot_log)
    agent_end_s = time.monotonic() - t0
    sid = dep.get("session_id")
    upick = pick_url(dep.get("result", ""), prof["url_re"], sub_re, require_token=run_token, primary_name=primary_name)
    url_source = "final-message"
    if not upick["url"] and sid:                       # the URL may be only in a tool result
        tx_early = find_transcript(sid)
        if tx_early:
            upick = pick_url(all_text(acs._load_rows(tx_early)), prof["url_re"], sub_re,
                             require_token=run_token, primary_name=primary_name)
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
        _log(f"no URL carrying this run's token '{run_token}' appeared. Either the deploy did not "
             "reach a public URL, or the agent did not put the token in the deployment name (so the "
             "run cannot safely tell its own deployment from others on the account). This is a SAFE "
             "failure: better than measuring the wrong VM.")
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
            url = pick_url(rep.get("result", ""), prof["url_re"], sub_re,
                           require_token=run_token, primary_name=primary_name)["url"] or url
        outcome = ("SUCCESS" if first_attempt_success else
                   ("SUCCESS-after-repair" if reached_healthy else "FAILURE-never-served"))
    poller.stop()
    poller.join(timeout=45)
    # PAPER C24: the durable-serve clock. The poller ran to the end of the deploy turn and logged every
    # serving poll; t1 is the earliest poll whose response matches the app's stable final response, so a
    # transient edge placeholder that stopped the first-serve clock early is superseded by the first real
    # app response. When the first serve WAS the app (74 of 78 published runs), the durable time equals
    # the first-serve time and nothing moves. Only applied when the poller actually logged a serve; the
    # post-handoff blocking-poll path keeps its own value.
    first_serve_s = round(poller.t_serving_s, 1) if poller.t_serving_s is not None else None
    first_serve_code = poller.code
    t1_method = "first-serve"
    if poller.poll_log and time_to_serving_s is not None and poller.t_serving_s is not None:
        d_t, d_code = durable_serving(poller.poll_log)
        if d_t is not None:
            time_to_serving_s = d_t
            health_code = d_code or health_code
            t1_method = "durable-serve/1.0"
    serving_epoch = (t0_epoch + time_to_serving_s) if time_to_serving_s is not None else None
    serving = {                                            # the external clock, in the record
        "predicate": SERVING_PREDICATE,
        "http_code": health_code,
        "t1_method": t1_method,                            # PAPER C24: first-serve vs durable-serve
        "first_serve_at_s": first_serve_s,                 # the earliest <500, kept for transparency
        "first_serve_code": first_serve_code,
        "poll_log": list(poller.poll_log)[:60],            # serving polls {t_s,url,code,bytes,sig}, capped
        "poll_log_len": len(poller.poll_log),              # full length (derivation used all of them)
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
        # Response-origin evidence for every 4xx seen (PAPER p2 s4). `rejected_edge_4xx` counts the ones
        # attributed to the platform edge, whose clock-stop was refused; an empty `origin` list means no
        # 4xx was ever seen, not that the check did not run.
        "origin": list(poller.origin_checks),
        "rejected_edge_4xx": poller.rejected_edge_4xx,
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

    # 6a. OFF-CLOCK SUCCESS ORACLE (always on): separate from the liveness clock, records whether the
    #     served root is the real app vs an infra error/default page the <500 predicate still counts as
    #     serving. Never gates timing; the outcome stays liveness-based (app-agnostic, p2 s4), but a
    #     content-unverified run is FLAGGED so a false-success is visible.
    success_oracle = None
    if url and reached_healthy:
        success_oracle = verify_served_content(url)
        cv = success_oracle.get("content_verified")
        if cv is False:
            _log(f"success-oracle: WARNING content NOT verified ({success_oracle.get('note')}, "
                 f"http {success_oracle.get('http_code')}); served <500 but looks like an infra/default page")
        else:
            _log(f"success-oracle: content {'verified (app content)' if cv else 'inconclusive'} "
                 f"(http {success_oracle.get('http_code')}, {success_oracle.get('bytes')} bytes)")

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
        if (cost_run_rate or {}).get("ok") and cost_run_rate.get("kind") == "usage":
            sched = cost_run_rate.get("schedule") or []
            pts = ", ".join(f"{p['label']} req -> ${p['usd_per_month']}/mo" for p in sched[:4])
            _log(f"cost: usage-metered {cost_run_rate.get('service')} schedule ({pts}, ...)")
        elif (cost_run_rate or {}).get("ok"):
            _log(f"cost: ${cost_run_rate.get('all_in_hourly_usd')}/hr (~${cost_run_rate.get('monthly_usd')}/mo)")
        if (cost_run_rate or {}).get("ok"):
            te = cost_run_rate.get("traffic_estimate") or {}
            _log(f"cost by traffic (est total $/mo): low ${te.get('low')} (10k req/mo), "
                 f"medium ${te.get('medium')} (500k req/mo), high ${te.get('high')} (10M req/mo)")
        else:
            _log(f"cost: n/a ({(cost_run_rate or {}).get('error')})")

    # 6e. OPTIONAL (--suite): drive a TIER instance's operations (Medium/Hard) on the serving
    #     deployment. The agent OWNS the method for each mutation (resumed session-A turns); the RUNNER
    #     verifies each postcondition independently, and the terminal conjunction re-checks every durable
    #     sentinel AFTER the restart (CP7). Off-clock relative to t1; runs before deprovision so the
    #     capability/cost axes above measured the un-perturbed deployment. Never skips deprovision.
    tier_run = None
    if suite_inst is not None and url and reached_healthy:   # suite_inst built once, above (before deploy)
        try:
            tier_run = drive_suite(suite_inst, prof, model, sid, url, sandbox, out_dir, i, cwd)
        except Exception as e:  # noqa: BLE001 - a suite error must never skip deprovision
            _log(f"suite: aborted (non-fatal; deprovision still runs): {e!r}")

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
    elif prof.get("suite"):
        # a tier run created MULTIPLE resources (app + datastore + second site): resume the builder
        # session so the teardown removes everything this run created, not only the primary URL. WHAT to
        # remove comes from the instance (app-specific), keeping this call app-agnostic.
        td = deprovision_suite_agent(prof, model, url, sid, sandbox, out_dir, i, cwd,
                                     teardown_hint=(suite_inst.teardown_hint if suite_inst else ""),
                                     verify_urls=([tier_run.get("site_b_url")] if tier_run else []))
    else:
        td = deprovision_agent(prof["mcp_config"], model, url, tag=f"deprovision (run {i})", sandbox=sandbox)
    td_tx = td.get("transcript")
    td_measure = {"lane_summary": acs.lane_summary(td_tx), "tokens": acs.dedupe_token_usage(td_tx)} \
        if td_tx else {}
    verify = curl_dead(url) if url else {"dead": None}
    _log(f"read-verify: url dead={verify.get('dead')} (http {verify.get('http_code')})")
    # LAST-LINE teardown: after the agent's own teardown and the URL read-verify, reap any token-scoped
    # resource a URL poll cannot see - above all a managed DATABASE (redu keeps it by design; azure leaves
    # it in the run's resource group). This is what lets an 8h UNATTENDED batch not quietly accrue DB cost.
    reap = {"reaped": [], "failed": [], "checked": False}
    if url and not prof.get("keep") and not td.get("skipped"):
        known_urls = [url] + ([tier_run.get("site_b_url")] if tier_run else [])
        reap = reap_run(prof["cloud"], run_token, known_urls)
    # ORPHAN SAFETY: a teardown was attempted (not --keep, not skipped) but the URL is STILL serving ->
    # the deployment was NOT removed and is BILLING. Loud, and recorded, so a long batch does not silently
    # accrue orphans (the 2026-08-25 AWS run left a live Lightsail service after its creds expired).
    # the primary URL still serving OR the suite teardown flagged a survivor OR the reaper could not delete
    # a no-URL resource (a managed DB) is an orphan.
    orphaned = (bool(url) and not prof.get("keep") and not td.get("skipped") and verify.get("dead") is False) \
        or bool(td.get("orphaned")) or bool(reap.get("failed"))
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
        "success_oracle": success_oracle,                      # off-clock content check, separate from the liveness clock (M1)
        "capability": capability,                              # optional off-clock delivered-capability C (or None)
        "cost_run_rate": cost_run_rate,                        # optional off-clock standing hourly run-rate (C19) or None
        "tier_run": tier_run,                                  # optional --suite: Medium/Hard operation-graph result (or None)
        "split": opsplit,                                      # agent/platform SECONDS via acspeed, clipped at t1
        "steps": ((m or {}).get("tokens") or {}).get("turns"),  # LLM calls = the paper's portable "steps"
        "health_code": health_code,
        "recovery": {                                          # separate: reached serving only after a repair
            "reached_healthy": reached_healthy,
            "rounds_to_healthy": rounds_to_healthy,            # 1 = served on the first deploy
            "repair_prompts": (rounds_to_healthy - 1) if rounds_to_healthy else None,
        },
        "url": url, "url_source": url_source, "url_candidates": upick["candidates"],
        "url_ambiguous": upick["ambiguous"], "run_token": run_token,   # only a hostname carrying this is this run's

        "prompts_used": len(rounds), "rounds": rounds,
        "deploy": {"session": sid, "transcript": tx, **m},     # PROVISION operation (session A)
        "deprovision": {"session": td.get("session"), "transcript": td_tx,      # DEPROVISION operation
                        "cost": td.get("cost"), "skipped": td.get("skipped", False),
                        "orphaned": orphaned, **td_measure},
        "reaped": reap,                         # last-line token-scoped reaper: no-URL leftovers (DBs) it removed / could not
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


def _aws_profile_from_mcp(mcp_config: str) -> str | None:
    """The --profile the AWS MCP signs with (aws.mcp.json), so the preflight checks the SAME creds the
    deploy/deprovision will use. None if the config names no profile (default credential chain)."""
    try:
        args = json.load(open(mcp_config))["mcpServers"]["aws-mcp"]["args"]
        return args[args.index("--profile") + 1] if "--profile" in args else None
    except Exception:  # noqa: BLE001
        return None


def preflight_credentials(prof: dict) -> tuple[bool, str]:
    """Fail FAST, before the agent burns a deploy, if the target cloud's credentials are unusable (a dead
    aws login session, a misconfigured / rotated / missing profile). This is what prevents a deploy that
    then CANNOT be torn down -> a live, billing orphan (the 2026-08-27 AWS incident). AWS: host-side
    `sts get-caller-identity` with the SAME profile the microVM's MCP signs with (the VM copies ~/.aws,
    so valid on the host == valid in the VM). Other clouds: no check yet (returns ok)."""
    cloud = prof.get("cloud")
    if cloud not in ("aws", "gcp", "azure"):
        return True, ""
    env = {**os.environ, "PATH": os.environ.get("PATH", "") + ":/tmp/awsv2-bin:" + os.path.expanduser("~/.local/bin")}

    if cloud == "gcp":
        # ADC / gcloud must be able to mint a token, so the agent can deploy AND deprovision (the Cloud Run
        # MCP has no delete tool; teardown is `gcloud run services delete`). Fail fast otherwise.
        try:
            r = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True,
                               timeout=30, env=env)
        except Exception as e:  # noqa: BLE001
            return False, (f"GCP credential preflight could not run `gcloud` ({e}); install the Cloud SDK "
                           "and `gcloud auth login` (or set GOOGLE_APPLICATION_CREDENTIALS to a service-account key).")
        if r.returncode != 0 or not (r.stdout or "").strip():
            return False, ("GCP credentials are unusable (`gcloud auth print-access-token` failed): "
                           f"{(r.stderr or r.stdout).strip()[:200]}. Run `gcloud auth login` + "
                           "`gcloud config set project <id>` (or set GOOGLE_APPLICATION_CREDENTIALS) before "
                           "running -- refusing to deploy and risk an un-deletable orphan.")
        return True, ""

    if cloud == "azure":
        # `az account show` proves a usable DefaultAzureCredential (az login or SP env vars), so deploy AND
        # `az group delete` teardown can authenticate. Fail fast otherwise.
        try:
            r = subprocess.run(["az", "account", "show", "--output", "none"], capture_output=True, text=True,
                               timeout=30, env=env)
        except Exception as e:  # noqa: BLE001
            return False, (f"Azure credential preflight could not run `az` ({e}); install the Azure CLI and "
                           "`az login` (or set AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET).")
        if r.returncode != 0:
            return False, ("Azure credentials are unusable (`az account show` failed): "
                           f"{(r.stderr or r.stdout).strip()[:200]}. Run `az login` (or set the "
                           "service-principal env vars) before running -- refusing to deploy and risk an "
                           "un-deletable orphan.")
        return True, ""

    profile = _aws_profile_from_mcp(prof["mcp_config"])
    cmd = ["aws", "sts", "get-caller-identity", "--output", "text"]
    if profile:
        cmd += ["--profile", profile]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    except Exception as e:  # noqa: BLE001
        return False, f"AWS credential preflight could not run `aws` ({e}); is the AWS CLI installed on PATH?"
    if r.returncode != 0:
        return False, (f"AWS credentials for profile '{profile or 'default'}' are unusable: "
                       f"{(r.stderr or r.stdout).strip()[:200]}. Set up the static-key profile (README: "
                       "'AWS setup') before running -- refusing to deploy and risk an un-deletable orphan.")
    return True, ""


def preflight_claude_auth() -> tuple[bool, str]:
    """Fail FAST, before any deploy, if the host's Claude login is dead or expired. The microVM seeds the
    host's claudeAiOauth (access + refresh token) and the host-side teardown runs `claude` under the SAME
    login; if it cannot authenticate, EVERY agent turn returns 'OAuth session expired and could not be
    refreshed' -> the deploy fails with no URL AND a partial deploy is left un-torn-down (a billing orphan).
    This was the root cause of the 4-cloud batch that failed 2026-08-31 (every run: an expired token, and
    nothing failed fast so it burned deploys and orphaned RDS/ECS/CloudSQL/an Azure RG). We do a REAL host
    round-trip (the exact auth path the run uses) plus a token-TTL read, so a batch that would outlive the
    ~8h token is flagged up front rather than discovered halfway through."""
    creds = os.path.expanduser("~/.claude/.credentials.json")
    ttl_min = None
    try:
        o = (json.load(open(creds)).get("claudeAiOauth") or {})
        exp = float(o.get("expiresAt") or 0) / 1000.0
        if exp:
            ttl_min = (exp - time.time()) / 60.0
    except Exception:  # noqa: BLE001 - a missing/odd creds file just skips the TTL hint; the round-trip still decides
        pass
    try:
        r = subprocess.run(["claude", "-p", "reply with the single word READY",
                            "--output-format", "json", "--max-turns", "1"],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        return False, f"Claude auth preflight could not run `claude` ({e}); is Claude Code installed on PATH?"
    try:
        d = json.loads((r.stdout or "").strip() or "{}")
    except ValueError:
        d = {}
    result_s = str(d.get("result") or "")
    dead = (r.returncode != 0 or bool(d.get("is_error"))
            or "oauth" in result_s.lower() or "authenticate" in result_s.lower())
    if dead:
        detail = (result_s or (r.stderr or "") or (r.stdout or "")).strip()[-200:]
        return False, ("Claude login is unusable (a headless `claude -p` did not authenticate): "
                       f"{detail}. Run `claude auth login` (interactive) or `claude setup-token` (a "
                       "long-lived token that survives an overnight batch) before running -- refusing to "
                       "deploy on a dead login and risk agent turns that fail after a partial, "
                       "un-torn-down deploy.")
    msg = "host Claude login OK"
    if ttl_min is not None:
        msg += f" (~{ttl_min:.0f} min token TTL)"
        if ttl_min < 90:
            msg += (" -- LOW: this token may expire mid-batch and a single lost cross-VM refresh bricks the "
                    "rest of the run; for a long/unattended batch run `claude setup-token` first.")
    return True, msg


def _install_signal_guards() -> None:
    """SIGINT (Ctrl-C) / SIGTERM kill every child process group (agent turns, microVMs) and hard-exit,
    so an interrupt never leaves a nested claude, its MCP servers, or a Firecracker VM running with the
    terminal stuck. Children are spawned detached from the terminal (procguard.spawn), so the signal
    reaches the host process reliably; the handler then group-kills them."""
    def _handler(signum, _frame):
        n = procguard.kill_all()
        _log(f"signal {signum}: killed {n} child process group(s) (agent turns / microVMs). A "
             "deployment a run had already created may still be up: check your dashboard. Exiting.")
        os._exit(130)
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _handler)
        except (ValueError, OSError):   # not the main thread (e.g. under a test runner): skip
            pass


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
    ap.add_argument("--start", type=int, default=None,
                    help="run index to start at. DEFAULT: APPEND after the highest existing runNN.json in the "
                         "output folder (1 if none), so repeated invocations accumulate instead of overwriting "
                         "from run01. Pass --start 1 to force a fresh batch from the top.")
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
    ap.add_argument("--suite", default=None, choices=acs_suites.instance_names(),
                    help="run a benchmark TIER instance (e.g. umami-medium) instead of Easy deploy-only: "
                         "after the deploy serves, the agent performs the tier's operations "
                         "(mutate/integrate/restart) as resumed turns and the runner verifies each "
                         "postcondition, with a terminal durability re-check (CP7). Off-clock relative to "
                         "t1; teardown resumes the builder session to remove ALL resources it created. "
                         "Default None = Easy (deploy-only).")
    a = ap.parse_args()
    _install_signal_guards()   # Ctrl-C now kills agent turns + microVMs and returns the terminal

    adapter = ADAPTERS[a.adapter]
    app_dir = os.path.abspath(os.path.expanduser(a.app_dir)) if a.app_dir else os.getcwd()
    if not os.path.isdir(app_dir):
        ap.error(f"app dir is not a directory: {app_dir}")
    # results + data are GROUPED BY CLOUD (see resolve_out_dir): <app>/acspeed-results/<adapter>/.
    out_dir, copy_ignore = resolve_out_dir(app_dir, a.adapter, a.out)
    # the working copy lives in a temp dir OUTSIDE the app folder, so it never pollutes it. The CLOUD is
    # in the path so the SAME app can run on multiple clouds CONCURRENTLY without clobbering one shared
    # working copy (the overnight parallel Easy batch: umami on redu/aws/gcp/azure at once).
    work_cwd = os.path.join(tempfile.gettempdir(),
                            f"acspeed-work-{os.path.basename(app_dir.rstrip('/'))}-{a.adapter}")
    # the hermetic per-cloud substrate (C9): each agent turn runs in a fresh microVM holding ONLY the
    # target cloud's credentials. Same CLI; auto-enabled when built + KVM/tap present.
    sandbox = None
    if not a.no_sandbox:
        ok, why = sandbox_available()
        if ok:
            # generalized credential mounting: aws keeps ~/.aws (dot-aws, the existing path); gcp/azure
            # mount ~/.config/gcloud / ~/.azure as dot-config-gcloud / dot-azure. Only the TARGET cloud's
            # creds are staged, so the hermetic-substrate scoping (C9) holds across all clouds. The guest
            # placement of the new dirs activates on the next rootfs rebuild (README); --no-sandbox uses
            # host creds directly and needs no mount.
            sandbox = {"keep_claude_tokens": adapter.get("keep_claude_tokens", []),
                       "aws_dir": (os.path.expanduser(adapter["aws_creds"]) if adapter.get("aws_creds") else None),
                       "creds_mounts": {name: os.path.expanduser(adapter[key])
                                        for key, name in (("gcp_creds", "dot-config-gcloud"),
                                                          ("azure_creds", "dot-azure"))
                                        if adapter.get(key)},
                       "session_store": None}
        else:
            _log(f"microVM substrate NOT used ({why}); running agent turns on the HOST.")

    cloud_label = adapter.get("cloud_label", a.adapter)
    prof = {**CONFIG, **adapter, "cloud": a.adapter, "task": os.path.basename(app_dir.rstrip("/")),
            "task_prompt": CONFIG["task_prompt_template"].format(cloud=cloud_label),
            "app_dir": app_dir, "run_cwd": work_cwd, "out_dir": out_dir, "copy_ignore": copy_ignore,
            "keep": a.keep, "screenshot": a.screenshot, "capability": a.capability, "cost": a.cost,
            "suite": a.suite, "sandbox": sandbox}
    # The off-clock COST read must sign with the SAME static profile as the deploy/deprovision (the aws
    # MCP's --profile), never the host default credential chain: an expired `aws login` SSO session there
    # made cost N/A with "refresh token has expired" (2026-08-28 run). None for non-aws clouds.
    prof["aws_profile"] = _aws_profile_from_mcp(prof["mcp_config"])
    os.makedirs(out_dir, exist_ok=True)
    _log(f"adapter={a.adapter}  app-dir={app_dir}  task={prof['task']!r}  runs={a.n}  out={out_dir}")
    if a.suite:
        _log(f"SUITE: tier instance '{a.suite}' - after the deploy serves, the agent performs the tier's "
             "operations (the agent owns the method) and the runner verifies each postcondition, with a "
             "terminal durability re-check after a restart (CP7). Teardown removes ALL resources created.")
    if sandbox:
        n_slots = len(vmjob.discover_slots())
        _log(f"SUBSTRATE: each agent turn runs in a FRESH microVM holding ONLY {a.adapter}'s credentials "
             f"(keep_tokens={sandbox['keep_claude_tokens']}, aws_creds={'yes' if sandbox['aws_dir'] else 'no'}); "
             f"the prompt names {cloud_label} as the target and other providers' config in the folder is "
             "ignored. Same CLI; the microVM is transparent.")
        _log(f"CONCURRENCY: {n_slots} tap slot(s) available -> up to {n_slots} acspeed run(s) at once; this "
             "run claims a free slot (queues if all busy). Add slots: sudo bash sandbox/net-setup.sh <N>.")
    _log("SAFETY: the agent provisions its own deployment and then deprovisions THAT deployment. The "
         "tool never inspects, manages, or deletes anything else on your account.")
    _log(f"CLOCK: t0 = deploy request; t1 = {SERVING_PREDICATE}, polled EXTERNALLY every "
         f"{SERVING_POLL_S:.0f}s from the moment the URL appears, concurrently with the agent. "
         "The agent's 'done' is never the boundary.")

    cred_ok, cred_why = preflight_credentials(prof)
    if not cred_ok:
        _log(f"PREFLIGHT FAILED: {cred_why}")
        sys.exit(2)
    if prof.get("cloud") == "aws":
        _log(f"PREFLIGHT: AWS credentials valid (profile '{_aws_profile_from_mcp(prof['mcp_config']) or 'default'}'); "
             "deploy + deprovision will authenticate, so a run cannot orphan on expired creds.")
    elif prof.get("cloud") in ("gcp", "azure"):
        _log(f"PREFLIGHT: {prof['cloud']} credentials valid; deploy + deprovision will authenticate, so a run "
             "cannot orphan on expired creds. NOTE: the hermetic microVM mounts these creds only after a "
             "rootfs rebuild picks up the generalized vm-runner (see README); until then run "
             f"--no-sandbox for {prof['cloud']} (agent turns on the host, using the host's creds directly).")

    # Claude-login preflight: the microVM (and the host-side teardown) run `claude` under the host's
    # subscription login. A dead/expired token makes EVERY turn fail 'OAuth session expired' -> no URL and
    # a possible un-torn-down orphan. Fail fast here (the 2026-08-31 4-cloud wipeout), before any spend.
    claude_ok, claude_why = preflight_claude_auth()
    if not claude_ok:
        _log(f"PREFLIGHT FAILED: {claude_why}")
        sys.exit(2)
    _log(f"PREFLIGHT: {claude_why}")

    # Browser preflight: the Medium/Hard integration read (R5) needs a REAL headless browser to drive the
    # tracked visit, and it correctly REFUSES to fake it - so with no working browser every run passes the
    # deploy and then fails integrate (measured 2026-08-31: three runs each burned ~12 min, then failed on
    # 'no headless browser'). Fail fast HERE, with the fix, so a long batch never wastes hours + spend.
    if prof.get("suite"):
        from acspeed.suites._visit import headless_visit as _preflight_visit
        _ok_b, _eng_b = _preflight_visit("data:text/html,<h1>acspeed browser preflight</h1>", settle_s=3.0)
        if not _ok_b:
            _log("PREFLIGHT FAILED: no working headless browser on this host, and the integration read "
                 f"(a tracked visit) cannot be faked (engine={_eng_b}). The suite needs Playwright's chromium "
                 "or a NON-snap chrome (a snap chromium is confined and cannot render). Install one for THIS "
                 "python and re-run:\n    pip install playwright && python3 -m playwright install chromium")
            sys.exit(2)
        if _eng_b == "playwright":
            _log(f"PREFLIGHT: headless browser OK (engine={_eng_b}); the integration visit can be driven.")
        else:
            # A DEGRADED engine is not "OK". Measured 2026-09-05 over 96 integrate checks in the results
            # tree: playwright 12/12 passed, chromium-cli 80/84 (4 lost beacons, all in aws-medium-b).
            # Playwright waits for the network to settle; the CLI drives chromium with
            # --virtual-time-budget + --screenshot and exits on the budget. The fallback is SILENT, and
            # that is how the ENTIRE production dataset (every aws/gcp/azure/redu medium run) came to be
            # collected on the weaker engine: `acspeed-run` is a pipx console script whose interpreter
            # had no playwright, so _visit_playwright returned None on every run and nobody noticed
            # because this line said OK. Say it loudly instead.
            _log(f"PREFLIGHT WARNING: headless engine is '{_eng_b}', NOT playwright. Measured loss rate "
                 "4.8% vs 0% for playwright, so a passing deploy can still fail the integration read "
                 "for an instrument reason. Install playwright FOR THE INTERPRETER RUNNING THIS "
                 f"({sys.executable}):\n    {sys.executable} -m pip install playwright && "
                 f"{sys.executable} -m playwright install chromium")

    start = a.start if a.start is not None else _next_run_index(prof["out_dir"])
    if a.start is None and start > 1:
        _log(f"append mode: {start - 1} existing run(s) in {prof['out_dir']}; starting at run{start:02d} "
             "(pass --start 1 to overwrite from run01)")
    for i in range(start, start + a.n):
        if i > start:
            # each batch run must mint its OWN run token: run_once persists prof["run_token"] for its own
            # drive_suite + reaper, and without this the NEXT run would reuse it, so a whole --n batch shared
            # one token/URL (isolation defeated). The first run keeps any caller-pinned token (tests/repro).
            prof.pop("run_token", None)
        try:
            run_once(i, prof, a.model, a.max_rounds)
        except KeyboardInterrupt:            # backstop; the signal handler normally hard-exits first
            _log("interrupted: killing any running agent turn / microVM and stopping.")
            procguard.kill_all()
            break
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
