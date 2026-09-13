#!/usr/bin/env python3
"""Run ONE agent operation inside a fresh Firecracker microVM: the hermetic, agent-agnostic per-task
substrate (PAPER C9). The microVM holds ONLY the target cloud's credentials + the agent's own auth,
so the reachable cloud is bounded by the injected creds; the prompt names the target cloud (autorun
CONFIG.task_prompt_template) so a folder's own deploy docs for another provider cannot misdirect it.

CONCURRENCY: each run CLAIMS a free network slot (tap acspeed-tap<k> on its own /24) via an flock'd
lockfile, so multiple acspeed runs launched at once each take a DISTINCT tap and never collide on
Resource-busy. net-setup.sh creates the pool (N slots); a single-tap host still works as slot 0.

Everything is rootless: the base rootfs is copied per run, the job drive is built with `mke2fs -d`,
and results are read back with `debugfs dump`. Host networking (the tap pool + NAT) is set up once
by net-setup.sh. Readiness is polled from the HOST (a neutral vantage, outside every cloud) by the
caller, never from inside the VM.

This module defines no metric; it only relocates the agent's execution into the microVM. The returned
transcript is byte-identical in format to a host `claude -p` run, so every acspeed measurement is
unchanged.
"""
from __future__ import annotations

import fcntl
import glob
import json
import os
import shutil
import subprocess
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_FC = os.path.join(HERE, "bin", "firecracker")
KERNEL = os.path.join(HERE, "images", "vmlinux")
BASE_ROOTFS = os.path.join(HERE, "images", "rootfs.ext4")

import sys
if os.path.dirname(HERE) not in sys.path:   # repo root (acspeed package), for standalone runs
    sys.path.insert(0, os.path.dirname(HERE))
from acspeed import procguard   # process-group guard: clean Ctrl-C (kill the VM, do not eat the signal)

DNS = "1.1.1.1"
TAP_PREFIX = "acspeed-tap"
_SLOT_LOCK_DIR = os.path.join(tempfile.gettempdir(), "acspeed-vm-slots")


def _slot_net(k: int) -> dict:
    """Per-slot host/guest network params so CONCURRENT microVMs never share a tap/IP/MAC (a shared
    tap is opened exclusively by Firecracker -> `Resource busy`, the 2026-08-27 collision). Slot k =
    tap acspeed-tap<k> on its own /24 172.20.<k>.0/24 (host .1, guest .2), MAC 06:00:ac:14:<k>:02."""
    host_ip, guest_ip = f"172.20.{k}.1", f"172.20.{k}.2"
    return {"slot": k, "tap": f"{TAP_PREFIX}{k}", "host_ip": host_ip, "guest_ip": guest_ip,
            "guest_mac": f"06:00:ac:14:{k:02x}:02",
            # kernel ip= : client:server:gw:netmask:hostname:device:autoconf
            "boot_ip": f"ip={guest_ip}::{host_ip}:255.255.255.0::eth0:off"}


# Back-compat: slot-0 tap name that older docs / callers still reference.
TAP = f"{TAP_PREFIX}0"


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def discover_slots() -> list[int]:
    """Slot indices whose tap currently exists (created by net-setup.sh), contiguous from 0. An old
    single-tap host yields [0]; a pool of N yields [0..N-1]; nothing set up yields []."""
    slots = []
    for k in range(256):
        if _run(["ip", "-o", "link", "show", f"{TAP_PREFIX}{k}"]).returncode != 0:
            break
        slots.append(k)
    return slots


def _claim_slot(timeout: float = 1800.0):
    """Claim a FREE VM slot for this run's lifetime via a NON-BLOCKING flock on a per-slot lockfile, so
    N concurrently-launched acspeed processes each take a DISTINCT tap. Returns (net, lock_fd); the
    caller closes lock_fd to release. Blocks (polling every 1s) up to `timeout` when every slot is
    busy, so launching more runs than slots just queues the extras instead of failing."""
    slots = discover_slots()
    if not slots:
        raise RuntimeError("no acspeed tap devices found; run: sudo bash sandbox/net-setup.sh [N]")
    os.makedirs(_SLOT_LOCK_DIR, exist_ok=True)
    start = time.monotonic()
    while True:
        for k in slots:
            fd = os.open(os.path.join(_SLOT_LOCK_DIR, f"slot{k}.lock"), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return _slot_net(k), fd
            except OSError:
                os.close(fd)
        if time.monotonic() - start > timeout:
            raise RuntimeError(f"all {len(slots)} acspeed VM slots busy after {timeout:.0f}s")
        time.sleep(1.0)


def scoped_claude_dir(dst: str, keep_server_names: list[str]) -> None:
    """Write dst/.credentials.json = the real one with mcpOAuth filtered to ONLY the given cloud
    server names (keep claudeAiOauth + organizationUuid). This is the credential-scoping that makes
    other clouds unauthable inside the VM. Verified 2026-08-25 to keep the subscription working."""
    src = os.path.expanduser("~/.claude/.credentials.json")
    d = json.load(open(src))
    mo = d.get("mcpOAuth") or {}
    kept = {k: v for k, v in mo.items()
            if isinstance(v, dict) and v.get("serverName") in keep_server_names}
    d["mcpOAuth"] = kept
    # The VM runs `claude` under the host's SUBSCRIPTION login (claudeAiOauth), refresh token INCLUDED so a
    # long run / overnight batch can refresh when the ~8h access token expires. The catch: an OAuth refresh
    # rotates the refresh-token family server-side, invalidating the HOST's copy. We handle that by syncing
    # the VM's refreshed token BACK to the host after the run (see sync_claude_login_from_vm), so the host
    # ends up holding whatever the VM rotated to instead of being silently logged out.
    os.makedirs(dst, exist_ok=True)
    p = os.path.join(dst, ".credentials.json")
    with open(p, "w") as fh:
        json.dump(d, fh)
    os.chmod(p, 0o600)


def _mkext4(src_dir: str, out_path: str, size_blocks: int, block: int = 4096) -> None:
    if os.path.exists(out_path):
        os.remove(out_path)
    r = _run(["mke2fs", "-q", "-t", "ext4", "-d", src_dir, "-b", str(block), out_path, str(size_blocks)])
    if r.returncode != 0:
        raise RuntimeError(f"mke2fs failed: {r.stderr[-400:]}")


def _debugfs_dump(img: str, vm_path: str, host_path: str) -> bool:
    r = _run(["debugfs", "-R", f"dump {vm_path} {host_path}", img])
    return os.path.exists(host_path) and os.path.getsize(host_path) >= 0 and "File not found" not in (r.stderr or "")


def sync_claude_login_from_vm(rootfs_img: str) -> bool:
    """After a VM run, adopt the VM's (possibly refreshed) Claude login onto the host. The VM runs `claude`
    under the host's subscription token WITH its refresh token, so a long run / overnight batch can refresh
    when the ~8h access token expires - but an OAuth refresh ROTATES the refresh-token family server-side and
    invalidates the host's copy, silently logging the host out. This reads the VM's ~/.claude/.credentials
    .json out of the rootfs image and, IF its claudeAiOauth is newer than the host's (i.e. the VM refreshed),
    writes ONLY that key back to the host file (re-reading first so nothing else is clobbered). Best-effort:
    any failure is a no-op and the host is no worse off. Call it BEFORE the rootfs image is deleted."""
    host_path = os.path.expanduser("~/.claude/.credentials.json")
    tmp = rootfs_img + ".vmcred.json"
    try:
        if not os.path.exists(rootfs_img):
            return False
        got = any(_debugfs_dump(rootfs_img, vp, tmp) and os.path.getsize(tmp) > 0
                  for vp in ("/home/agent/.claude/.credentials.json", "/root/.claude/.credentials.json"))
        if not got:
            return False
        vm = (json.load(open(tmp)).get("claudeAiOauth") or {})
        if not (isinstance(vm, dict) and vm.get("accessToken") and vm.get("refreshToken")):
            return False
        cur = json.load(open(host_path))
        host = cur.get("claudeAiOauth") or {}
        if float(vm.get("expiresAt") or 0) <= float(host.get("expiresAt") or 0):
            return False                                   # VM did not refresh; the host token is still current
        cur["claudeAiOauth"] = vm                          # host adopts the token the VM rotated to
        t2 = host_path + ".acspeed.tmp"
        with open(t2, "w") as fh:
            json.dump(cur, fh)
        os.chmod(t2, 0o600)
        os.replace(t2, host_path)                          # atomic
        return True
    except Exception:                                      # noqa: BLE001 - best-effort; never break the run
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _toml_str(s: str) -> str:
    """A TOML basic string. Codex config is TOML, not JSON, so every value we carry across has to be
    escaped for TOML rather than reused from the JSON verbatim."""
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"' + out + '"'


def mcp_json_to_codex_toml(mcp_config: str) -> str:
    """Translate a Claude-style mcp.json into the [mcp_servers.*] TOML codex reads.

    Claude takes --mcp-config on the command line; codex has no such flag and loads MCP servers from
    ~/.codex/config.toml. Without this the codex path would run with no cloud tools at all, which is
    the silent version of not supporting codex. Handles both transports the adapters use: a spawned
    command (aws/gcp/azure) and a hosted HTTP endpoint (redu).
    """
    with open(mcp_config) as fh:
        servers = (json.load(fh) or {}).get("mcpServers") or {}
    lines = []
    for name, spec in servers.items():
        lines.append(f"[mcp_servers.{name}]")
        if spec.get("url"):                       # http transport (redu)
            lines.append(f"url = {_toml_str(spec['url'])}")
        if spec.get("command"):
            lines.append(f"command = {_toml_str(spec['command'])}")
        if spec.get("args"):
            inner = ", ".join(_toml_str(a) for a in spec["args"])
            lines.append(f"args = [{inner}]")
        env = spec.get("env") or {}
        if env:
            inner = ", ".join(f"{k} = {_toml_str(str(v))}" for k, v in env.items())
            lines.append(f"env = {{ {inner} }}")
        lines.append("")
    return "\n".join(lines)


def scoped_codex_dir(dst: str, mcp_config: str | None) -> None:
    """Stage ~/.codex for the guest: the agent's own auth plus ONLY this run's MCP servers.

    The host's ~/.codex also holds history, memories, goal and log databases, caches and every MCP
    server the user has ever configured. None of that may enter the microVM: it is the user's data,
    and a second cloud's MCP server in there would break the per-cloud scoping the substrate exists
    to provide. So this copies auth.json and writes a fresh config.toml, and nothing else.
    """
    os.makedirs(dst, exist_ok=True)
    src_auth = os.path.expanduser("~/.codex/auth.json")
    if os.path.exists(src_auth):
        shutil.copy(src_auth, os.path.join(dst, "auth.json"))
    toml = mcp_json_to_codex_toml(mcp_config) if (mcp_config and os.path.exists(mcp_config)) else ""
    with open(os.path.join(dst, "config.toml"), "w") as fh:
        fh.write(toml)


def build_job_drive(job_ext4: str, *, prompt: str, model: str | None, mcp_config: str | None,
                    app_dir: str, claude_dir: str, aws_dir: str | None, max_turns: int,
                    system: str | None = None, resume_sid: str | None = None,
                    creds_mounts: dict | None = None, agent: str = "claude",
                    codex_dir: str | None = None) -> None:
    """Assemble the per-run job drive: task + scoped creds + MCP config + the app, as an ext4."""
    with tempfile.TemporaryDirectory() as staging:
        def _w(name, val):
            with open(os.path.join(staging, name), "w") as fh:
                fh.write(val)
        _w("prompt.txt", prompt)
        _w("agent.txt", agent)          # vm-runner reads this to pick the CLI it launches
        _w("model.txt", model or "")
        _w("max_turns.txt", str(max_turns))
        _w("resolv.conf", f"nameserver {DNS}\n")
        if system:
            _w("system.txt", system)
        if resume_sid:
            _w("resume.txt", resume_sid)
        if mcp_config and os.path.exists(mcp_config):
            shutil.copy(mcp_config, os.path.join(staging, "mcp.json"))
        shutil.copytree(claude_dir, os.path.join(staging, "dot-claude"))
        if codex_dir and os.path.isdir(codex_dir):
            shutil.copytree(codex_dir, os.path.join(staging, "dot-codex"))
        if aws_dir and os.path.isdir(aws_dir):
            shutil.copytree(aws_dir, os.path.join(staging, "dot-aws"))
        # generalized creds mounts (gcp: dot-config-gcloud -> ~/.config/gcloud; azure: dot-azure -> ~/.azure).
        # vm-runner places these; only the target cloud's creds are staged (hermetic scoping holds).
        for guest_name, host_dir in (creds_mounts or {}).items():
            if host_dir and os.path.isdir(host_dir):
                shutil.copytree(host_dir, os.path.join(staging, guest_name))
        # the app under test, tarred (extracted to /home/agent/app inside the VM)
        subprocess.run(["tar", "-cf", os.path.join(staging, "app.tar"), "-C", app_dir, "."], check=True)
        used = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(staging) for f in fs)
        blocks = max(16384, int(used * 3 / 4096) + 8192)   # 4k blocks
        _mkext4(staging, job_ext4, blocks)


def run_vm_job(*, prompt: str, model: str | None, mcp_config: str | None, app_dir: str,
               keep_claude_tokens: list[str], aws_dir: str | None, creds_mounts: dict | None = None,
               agent: str = "claude",
               max_turns: int, timeout: int,
               vcpus: int = 2, mem_mib: int = 4096, work_dir: str | None = None,
               boot_log: str | None = None, system: str | None = None,
               resume_sid: str | None = None, resume_transcript: str | None = None,
               session_store: str | None = None) -> dict:
    """Boot a fresh microVM that runs the agent once, and return {result, transcripts_dir, exit_code,
    boot_log, session_id}. `keep_claude_tokens` = the mcpOAuth server names to keep (['redu'] for a
    redu run, [] for aws which authenticates via ~/.aws). `result` is the parsed `claude -p` JSON.
    `boot_log` (caller-provided) is written live so the host readiness poller can tail it for the
    URL relay. `resume_sid`+`resume_transcript` continue a prior session. `session_store` is a host
    dir the returned transcripts are merged into so find_transcript works across the whole run."""
    work = work_dir or tempfile.mkdtemp(prefix="acspeed-vm-")
    os.makedirs(work, exist_ok=True)
    rootfs = os.path.join(work, "rootfs.ext4")
    job = os.path.join(work, "job.ext4")
    cfg = os.path.join(work, "vm.json")
    boot_log = boot_log or os.path.join(work, "boot.log")
    claude_dir = os.path.join(work, "dot-claude")
    codex_dir = os.path.join(work, "dot-codex") if agent == "codex" else None

    shutil.copy(BASE_ROOTFS, rootfs)                       # fresh, writable rootfs per run
    scoped_claude_dir(claude_dir, keep_claude_tokens)      # only target cloud's token + subscription
    if codex_dir:
        # codex authenticates from ~/.codex/auth.json and reads its MCP servers from config.toml,
        # so its scoped home is built the same way claude's is, from different files.
        scoped_codex_dir(codex_dir, mcp_config)
    if resume_transcript and resume_sid and os.path.exists(resume_transcript):
        # inject the prior session so --resume finds it inside the VM (slug = the in-VM app cwd)
        pdir = os.path.join(claude_dir, "projects", "-home-agent-app")
        os.makedirs(pdir, exist_ok=True)
        shutil.copy(resume_transcript, os.path.join(pdir, f"{resume_sid}.jsonl"))
    build_job_drive(job, prompt=prompt, model=model, mcp_config=mcp_config, app_dir=app_dir,
                    claude_dir=claude_dir, aws_dir=aws_dir, max_turns=max_turns,
                    system=system, resume_sid=resume_sid, creds_mounts=creds_mounts,
                    agent=agent, codex_dir=codex_dir)

    net, slot_fd = _claim_slot()    # a free tap slot for this VM's lifetime (concurrent-run safe)
    try:
        config = {
            "boot-source": {"kernel_image_path": KERNEL,
                            "boot_args": ("console=ttyS0 reboot=k panic=1 pci=off "
                                          "i8042.noaux i8042.nomux i8042.nopnp i8042.dumbkbd "
                                          f"{net['boot_ip']} init=/usr/local/bin/vm-runner")},
            "drives": [
                {"drive_id": "rootfs", "path_on_host": rootfs, "is_root_device": True, "is_read_only": False},
                {"drive_id": "job", "path_on_host": job, "is_root_device": False, "is_read_only": False},
            ],
            "network-interfaces": [
                {"iface_id": "eth0", "host_dev_name": net["tap"], "guest_mac": net["guest_mac"]},
            ],
            "machine-config": {"vcpu_count": vcpus, "mem_size_mib": mem_mib},
        }
        with open(cfg, "w") as fh:
            json.dump(config, fh)

        with open(boot_log, "w") as lf:
            # stdin detached (procguard default) so Firecracker's ttyS0 console cannot swallow the
            # user's Ctrl-C; spawned as a process-group leader so the host signal handler can kill the
            # VM. The guest still powers off on its own at the end (vm-runner `reboot -f`).
            proc = procguard.spawn([BIN_FC, "--no-api", "--config-file", cfg],
                                   stdout=lf, stderr=subprocess.STDOUT)
            try:
                proc.wait(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                procguard.kill_group(proc.pid)
                try:
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    pass
                timed_out = True
            finally:
                procguard.untrack(proc.pid)
    finally:
        os.close(slot_fd)   # release the slot the instant the VM exits (the readback below needs no tap)

    # read results back from the job drive (rootless)
    out = {"result": None, "exit_code": None, "transcripts_dir": None,
           "boot_log": boot_log, "timed_out": timed_out, "work_dir": work}
    outj = os.path.join(work, "out.json")
    if _debugfs_dump(job, "/out.json", outj) and os.path.getsize(outj) > 0:
        try:
            out["result"] = json.load(open(outj))
        except ValueError:
            out["result"] = {"is_error": True, "result": open(outj).read()[-2000:]}
    ecf = os.path.join(work, "exit_code")
    if _debugfs_dump(job, "/exit_code", ecf) and os.path.getsize(ecf) > 0:
        out["exit_code"] = open(ecf).read().strip()
    ttar = os.path.join(work, "transcripts.tar")
    if _debugfs_dump(job, "/transcripts.tar", ttar) and os.path.getsize(ttar) > 0:
        tdir = os.path.join(work, "transcripts")
        os.makedirs(tdir, exist_ok=True)
        subprocess.run(["tar", "-xf", ttar, "-C", tdir], check=False)
        out["transcripts_dir"] = tdir
        # merge into the host session store so find_transcript works across the whole run
        if session_store:
            src = os.path.join(tdir, "projects")
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(session_store, "projects"), dirs_exist_ok=True)
    # codex writes its rollouts to ~/.codex/sessions/YYYY/MM/DD/ instead of ~/.claude/projects/, so it
    # comes back on its own artifact. Without this the guest wrote the transcript and the host never
    # collected it, leaving a codex run with NO transcript and therefore no platform/agent split at all.
    ctar = os.path.join(work, "codex_sessions.tar")
    if _debugfs_dump(job, "/codex_sessions.tar", ctar) and os.path.getsize(ctar) > 0:
        tdir = out.get("transcripts_dir") or os.path.join(work, "transcripts")
        os.makedirs(tdir, exist_ok=True)
        subprocess.run(["tar", "-xf", ctar, "-C", tdir], check=False)
        out["transcripts_dir"] = tdir
        if session_store:
            src = os.path.join(tdir, "sessions")
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(session_store, "sessions"), dirs_exist_ok=True)
    # recover the deploy's SSH key(s) (the per-app keypair the deploy minted lives only in the VM) so the
    # host can reach the deploy VM for the off-clock capability pass. The caller installs them to ~/.ssh.
    star = os.path.join(work, "agent_ssh.tar")
    if _debugfs_dump(job, "/agent_ssh.tar", star) and os.path.getsize(star) > 0:
        sdir = os.path.join(work, "agent_ssh")
        os.makedirs(sdir, exist_ok=True)
        subprocess.run(["tar", "-xf", star, "-C", sdir], check=False)
        out["agent_ssh_dir"] = sdir
    out["session_id"] = (out.get("result") or {}).get("session_id")
    return out


def find_transcript_in(transcripts_dir: str | None, session_id: str) -> str | None:
    """Locate this session's transcript in a recovered tree, for either agent.

    Claude names the file exactly ``<session_id>.jsonl``. Codex names it
    ``rollout-<timestamp>-<thread_id>.jsonl`` and reports the thread_id as the session id, so the
    id is a SUFFIX of the stem rather than the whole stem. Matching only the exact name silently
    found nothing for every codex run.
    """
    if not transcripts_dir or not session_id:
        return None
    hits = glob.glob(os.path.join(transcripts_dir, "**", f"{session_id}.jsonl"), recursive=True)
    if hits:
        return hits[0]
    hits = glob.glob(os.path.join(transcripts_dir, "**", f"*{session_id}.jsonl"), recursive=True)
    return sorted(hits)[0] if hits else None


if __name__ == "__main__":
    # smoke: a trivial no-cloud task to prove auth + round-trip inside the VM (needs net-setup.sh done)
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default="Reply with exactly one word: READY")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()
    app = tempfile.mkdtemp(prefix="acspeed-app-")
    open(os.path.join(app, "note.txt"), "w").write("smoke")
    res = run_vm_job(prompt=a.prompt, model=a.model, mcp_config=None, app_dir=app,
                     keep_claude_tokens=[], aws_dir=None, max_turns=2, timeout=a.timeout)
    r = res.get("result") or {}
    print("timed_out:", res["timed_out"], "| exit_code:", res["exit_code"])
    print("is_error:", r.get("is_error"), "| result:", repr((r.get("result") or "")[:80]))
    print("session:", r.get("session_id"), "| transcript:",
          find_transcript_in(res.get("transcripts_dir"), r.get("session_id")))
    print("boot_log tail:")
    print("\n".join(open(res["boot_log"]).read().splitlines()[-6:]))
