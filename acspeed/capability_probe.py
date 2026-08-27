"""Cloud-AGNOSTIC delivered-capability probe runner (Part 1, Section 3; change-requests C11 + C17/C18).

This module turns a shell into a measured capability vector. It is deliberately blind to *which* cloud
the shell belongs to: it takes an ``exec_fn`` that runs one shell command somewhere, and the SAME code
measures the neutral reference host (``local_exec``) or any cloud's provisioned VM (``ssh_exec``). The
only cloud-specific thing an adapter supplies is how to open that shell; the probes, the commands, the
reduction to ``CVector`` and the DCI are identical for every cloud. That is what makes the capability
number comparable across clouds.

Grounding (do not drift from this without amending the paper):
  * compute  = ``sysbench cpu`` events/s, single-task AND all-core (SPECspeed/SPECrate split). The
               CVector.compute scalar is the ALL-CORE throughput; single-task is disclosed.
  * memory   = STREAM Triad GB/s (McCalpin). Source is compiled on the target from a pinned stream.c.
  * disk     = ``fio`` grid, O_DIRECT: 4KiB random @ QD32 -> IOPS (the CVector.disk scalar),
               1MiB sequential @ QD32 -> bandwidth, QD1 -> latency (both disclosed). QDs are C11.
  * network  = intra-cloud VM-to-VM on the tenant private network (C17): ``iperf3`` TCP throughput +
               ``ping`` RTT measured BETWEEN TWO of the operation's OWN instances over their PRIVATE
               IPs. TCP bps = CVector.network; RTT (min) disclosed. SCORED only for a >=2-VM operation;
               for a single-VM operation it is NOT-APPLICABLE (disclosed, not an error) and the nominal
               NIC is a human-facing constraint, not a score (C18). SUPERSEDES the C10 neutral-vantage
               design (an external endpoint is a path property, not a cloud capability; C16/C17): do NOT
               reintroduce an iperf client aimed at a fixed external/neutral host.

Off the clock. This is a post-provision calibration pass (BUNGEE two-phase); it runs AFTER readiness
(t1 already recorded) and never touches time-to-serving. Every probe is failure-tolerant: a probe that
fails records an error and leaves its axis ``None`` (the paper keeps C a vector; a missing axis is
disclosed, not fatal), so the calibration can never abort or corrupt a run.
"""
from __future__ import annotations

import abc
import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import probes
from .types import CVector

# exec_fn(command, timeout_s) -> (returncode, stdout, stderr)
ExecFn = Callable[[str, int], Tuple[int, str, str]]

# --- exact probe commands (grounded; keep in lockstep with the docstring + change-requests) ---
_MAX_PRIME = 20000
_FIO_RUNTIME = 15
_FIO_FILE = "/tmp/acspeed.fio.dat"
_FIO_SIZE = "1G"
_IPERF_SECONDS = 10
_IPERF_PORT = 5201
_PING_COUNT = 20

# A bare $(nproc) reads the HOST core count inside a cpu-limited container / gVisor sandbox and overstates
# all-core compute; derive the EFFECTIVE core count from the cgroup CPU quota (v2 cpu.max, else v1
# cfs_quota/period), floored at 1, falling back to nproc when unconstrained ("max"/-1) or unreadable.
_EFFECTIVE_CORES = (
    "$(q=''; p=''; "
    "if [ -r /sys/fs/cgroup/cpu.max ]; then read q p < /sys/fs/cgroup/cpu.max; "
    "elif [ -r /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]; then "
    "q=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us); p=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us); fi; "
    "if [ -n \"$q\" ] && [ -n \"$p\" ] && [ \"$q\" != max ] && "
    "[ \"$q\" -gt 0 ] 2>/dev/null && [ \"$p\" -gt 0 ] 2>/dev/null; "
    "then c=$(( (q + p - 1) / p )); [ \"$c\" -lt 1 ] && c=1; echo \"$c\"; "
    "else nproc; fi)"
)

SYSBENCH_SINGLE = f"sysbench cpu --cpu-max-prime={_MAX_PRIME} --threads=1 run"
SYSBENCH_ALLCORE = f"sysbench cpu --cpu-max-prime={_MAX_PRIME} --threads={_EFFECTIVE_CORES} run"


def _fio_cmd(name: str, rw: str, bs: str, iodepth: int) -> str:
    return (f"fio --name={name} --filename={_FIO_FILE} --size={_FIO_SIZE} --direct=1 "
            f"--ioengine=libaio --rw={rw} --bs={bs} --iodepth={iodepth} --numjobs=1 "
            f"--runtime={_FIO_RUNTIME} --time_based --group_reporting --output-format=json")


FIO_IOPS = _fio_cmd("iops", "randread", "4k", 32)     # 4KiB random QD32 -> IOPS
FIO_BW = _fio_cmd("bw", "read", "1M", 32)             # 1MiB sequential QD32 -> bandwidth
FIO_LAT = _fio_cmd("lat", "randread", "4k", 1)        # QD1 -> latency


# --- network axis: intra-cloud VM-to-VM on the tenant private network (C17/C18) ----------------
# SUPERSEDES the C10 neutral-vantage design. The network axis is the tenant private network's
# VM-to-VM path capacity, measured BETWEEN TWO of the operation's OWN instances over their PRIVATE
# IPs (iperf3 TCP + ping RTT). It is a SCORED DCI axis ONLY for a >=2-VM operation; a single-VM
# operation reports it NOT-APPLICABLE (disclosed, never an error) with the nominal NIC as a
# human-facing constraint (C18). An external / neutral-vantage endpoint is a PATH property, not a
# cloud capability, so it is never used here (C16/C17). Do NOT reintroduce iperf_server=<neutral host>.

def _iperf_client_cmd(server_ip: str, *, streams: int = 1, window: Optional[str] = None) -> str:
    """TCP throughput from the client VM to the peer's PRIVATE IP (no UDP, no neutral vantage). The
    stream count (``-P``) and TCP window (``-w``) are explicit and disclosed with the result (C17 E1):
    a single flow can under-report a multi-Gbps NIC, so both are recorded, not hidden."""
    parts = ["iperf3 -J -c", shlex.quote(server_ip), f"-p {_IPERF_PORT}", f"-t {_IPERF_SECONDS}"]
    if streams and int(streams) > 1:
        parts.append(f"-P {int(streams)}")
    if window:
        parts.append(f"-w {shlex.quote(window)}")
    return " ".join(parts)


def _iperf_server_cmd() -> str:
    """One-shot iperf3 server on the peer VM (``-1`` exits after a single client), backgrounded."""
    return (f"pkill -f 'iperf3 -s' >/dev/null 2>&1; "
            f"(iperf3 -s -1 -p {_IPERF_PORT} >/tmp/acspeed_iperf3s.log 2>&1 &) ; sleep 1")


def _ping_cmd(server_ip: str) -> str:
    """RTT from the client VM to the peer's PRIVATE IP."""
    return f"ping -n -c {_PING_COUNT} -w {_PING_COUNT + 10} {shlex.quote(server_ip)}"


# --- exec_fn factories ------------------------------------------------------------------------

def local_exec(command: str, timeout_s: int) -> Tuple[int, str, str]:
    """Run a shell command on THIS host (the neutral reference machine)."""
    import subprocess
    p = subprocess.run(["bash", "-lc", command], capture_output=True, text=True, timeout=timeout_s)
    return p.returncode, p.stdout, p.stderr


# --- the provider-agnostic access boundary (C12 / C13) ---------------------------------------
# The ONLY thing a cloud-specific adapter produces is this handle. Everything below it (keypair,
# ssh argv, retry, first-SSH readiness, the probe battery) is identical on every cloud. A VM behind
# NAT or a provider edge is the generic bastion case: fill `proxy_jump` / `port`, no special code.

@dataclass(frozen=True)
class SSHHandle:
    """A normalized, provider-independent way to reach an instance's shell. `provider_timestamps`
    (e.g. t_request/t_active from provider tags) are DISCLOSURE ONLY, never the readiness signal
    (readiness is first-successful-SSH, Mao&Humphrey 2012 / Hao 2021)."""
    host: str
    user: str
    private_key_path: str
    port: int = 22
    proxy_jump: Optional[str] = None            # "user@bastion:port" (ProxyJump); None if directly reachable
    instance_id: Optional[str] = None
    provider_timestamps: dict = field(default_factory=dict)


# hardened, non-interactive ssh options (mirrors PerfKitBenchmarker GetSshOptions)
_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
    "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
    "-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no",
    "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
]


def _ssh_argv(h: SSHHandle) -> List[str]:
    """The ssh command prefix for one handle, built in the CORE so no provider-specifics leak in."""
    argv = ["ssh", *_SSH_OPTS, "-i", h.private_key_path, "-p", str(h.port)]
    if h.proxy_jump:
        argv += ["-o", f"ProxyJump={h.proxy_jump}"]
    argv.append(f"{h.user}@{h.host}")
    return argv


def ssh_exec(handle: SSHHandle, *, ssh_retries: int = 3) -> ExecFn:
    """Build an exec_fn that runs each command on `handle`'s VM over SSH, deterministically. Retries
    ONLY on ssh transport failure (exit 255) -- a real command's non-zero rc returns immediately
    (RobustRemoteCommand pattern). No provider API, no agent, no MCP: the credential is the key the
    harness injected at provision, resolved into `handle` by the thin adapter."""
    prefix = _ssh_argv(handle)

    def _fn(command: str, timeout_s: int) -> Tuple[int, str, str]:
        last = (255, "", "")
        for attempt in range(ssh_retries):
            argv = prefix + ["bash -lc " + shlex.quote(command)]
            try:
                p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
            except subprocess.TimeoutExpired as e:
                return 124, "", f"timeout after {timeout_s}s: {e}"
            if p.returncode != 255:            # a real command result (0 or the command's own rc)
                return p.returncode, p.stdout, p.stderr
            last = (255, p.stdout, p.stderr)   # ssh transport failure -> retry
        return last

    return _fn


def ssh_exec_prefix(ssh_prefix: str) -> ExecFn:
    """Back-compat / testing shim: build an exec_fn from a raw ssh-prefix string. Prefer ssh_exec(handle);
    this exists only so callers/tests that already hold a prefix string keep working."""
    parts = shlex.split(ssh_prefix)

    def _fn(command: str, timeout_s: int) -> Tuple[int, str, str]:
        argv = parts + ["bash -lc " + shlex.quote(command)]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        return p.returncode, p.stdout, p.stderr

    return _fn


def generate_run_keypair(dir_: str, name: str = "acspeed_id") -> Tuple[str, str]:
    """Mint ONE run-scoped keypair the HARNESS holds; the adapter injects the PUBLIC half at provision.
    This is the load-bearing correction: the harness never asks the cloud (API/MCP/agent) for a shell.
    Returns (private_key_path, public_key_string)."""
    os.makedirs(dir_, exist_ok=True)
    priv = os.path.join(dir_, name)
    if os.path.exists(priv):
        os.remove(priv)
    if os.path.exists(priv + ".pub"):
        os.remove(priv + ".pub")
    r = subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-m", "PEM", "-q", "-f", priv],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(priv + ".pub"):
        raise RuntimeError(f"ssh-keygen failed: {(r.stderr or r.stdout)[-300:]}")
    os.chmod(priv, 0o600)
    with open(priv + ".pub") as fh:
        pub = fh.read().strip()
    return priv, pub


def wait_first_ssh(handle: SSHHandle, *, timeout_s: float, poll_interval: float = 3.0,
                   clock: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep) -> Optional[float]:
    """Poll `ssh ... true` until the FIRST success; return the monotonic time of that success (the
    readiness signal for the capability pass), or None on timeout. This is provider-agnostic (first-SSH
    is identical everywhere, Mao&Humphrey 2012 / Hao 2021), so it lives in the core, not any adapter."""
    exec_fn = ssh_exec(handle, ssh_retries=1)
    start = clock()
    while clock() - start < timeout_s:
        rc, _, _ = exec_fn("true", 20)
        if rc == 0:
            return clock()
        sleep(poll_interval)
    return None


def parse_ssh_command(ssh_command: str, private_key_path: Optional[str] = None) -> SSHHandle:
    """Turn a provider's `get_ssh_command`-style string (e.g. "ssh -i ~/.ssh/k -o IdentitiesOnly=yes
    -p 22004 ubuntu@host.redu.cloud") into a normalized SSHHandle. `-i` in the string wins; else
    `private_key_path` is used (the harness's own key). This is the thin adapter's whole job: resolve
    the endpoint, never obtain the shell (that is ssh_exec + our key)."""
    toks = shlex.split(ssh_command)
    port, user, host, key, target = 22, None, None, private_key_path, None
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "-p" and i + 1 < len(toks):
            port = int(toks[i + 1]); i += 2; continue
        if t == "-i" and i + 1 < len(toks):
            key = os.path.expanduser(toks[i + 1]); i += 2; continue
        if t == "-o" and i + 1 < len(toks):
            i += 2; continue
        if t == "ssh" or t.startswith("-"):
            i += 1; continue
        if "@" in t:
            target = t
        i += 1
    if not target:
        raise ValueError(f"no user@host found in ssh command: {ssh_command!r}")
    user, host = target.split("@", 1)
    if not key:
        raise ValueError("no private key: neither -i in the command nor private_key_path given")
    return SSHHandle(host=host, user=user, private_key_path=key, port=port)


@dataclass(frozen=True)
class NetworkPair:
    """Two of the operation's OWN instances on the tenant private network (C17). ``client`` runs the
    iperf3 client + ping; ``server`` runs the one-shot iperf3 server; ``server_private_ip`` is the
    server's PRIVATE address on the tenant network (NOT its public SSH host). Provide this ONLY for a
    >=2-VM operation; its absence means the network axis is not-applicable (single-VM, C18), disclosed,
    never failed. No neutral-vantage / external endpoint is ever used (C16/C17)."""
    client: SSHHandle
    server: SSHHandle
    server_private_ip: str
    # C17 E1 placement control: two VMs on the SAME host never touch the physical fabric (vswitch /
    # shared memory) and over-state throughput. The suite should provision the pair with soft
    # anti-affinity; whatever it knows is disclosed here. None => "undetermined" (a tenant cannot
    # observe the hypervisor host), NOT "co-resident".
    co_residency: Optional[str] = None   # "anti-affinity" | "same-host" | None(=undetermined)


def measure_network_pair(pair: NetworkPair, *, do_install: bool = True, sudo: str = "sudo ",
                         streams: int = 1, tcp_window: Optional[str] = None) -> dict:
    """VM-to-VM network on the tenant private network (C17): iperf3 TCP throughput + ping RTT between
    the operation's own instances over PRIVATE IPs. Records the C17 E1 disclosure controls the paper
    promises: VM placement / co-residency, the stream count and TCP window, and the measured
    DISTRIBUTION (iperf per-interval series, ping min/avg/max/mdev). Any metric may be ``None`` (a
    failed sub-metric is disclosed, never faked). Off-clock and failure-tolerant like every probe."""
    errors: Dict[str, str] = {}
    server_exec = ssh_exec(pair.server)
    client_exec = ssh_exec(pair.client)
    if do_install:
        install_tools(server_exec, sudo=sudo)
        install_tools(client_exec, sudo=sudo)
    # start the one-shot iperf3 server on the peer's private interface (best-effort; failure surfaces
    # as the client's connect error below, which is the honest signal).
    server_exec(_iperf_server_cmd(), 30)
    tcp = _try(errors, "network",
               lambda: probes.parse_iperf3(_ok(client_exec(
                   _iperf_client_cmd(pair.server_private_ip, streams=streams, window=tcp_window), 60))))
    rtt = _try(errors, "network_rtt",
               lambda: probes.parse_ping(_ok(client_exec(_ping_cmd(pair.server_private_ip), 60))))
    server_exec("pkill -f 'iperf3 -s' >/dev/null 2>&1; true", 20)
    return {
        "tcp_bps": (tcp or {}).get("received_bps"),
        "tcp_bps_distribution": (tcp or {}).get("intervals_bps"),   # C17 E1: distribution, not one flow
        "rtt_ms_min": (rtt or {}).get("min_ms"),                    # min = the physical path floor
        "rtt_ms_avg": (rtt or {}).get("avg_ms"),
        "rtt_ms_max": (rtt or {}).get("max_ms"),
        "rtt_ms_mdev": (rtt or {}).get("mdev_ms"),
        "stream_count": int(streams),                              # C17 E1: disclosed
        "tcp_window": tcp_window,                                  # C17 E1: disclosed (None = OS default)
        "placement": {                                            # C17 E1: co-residency control
            "client_instance_id": pair.client.instance_id,
            "server_instance_id": pair.server.instance_id,
            "co_residency": pair.co_residency or "undetermined",
        },
        "raw": {"iperf_tcp": tcp, "ping_rtt": rtt},
        "errors": errors,
    }


def normalize_network_rtt(rtt_ms: Optional[float], reference_rtt_ms: Optional[float]) -> Optional[float]:
    """SPECspeed-style latency ratio for the network axis (C17): ``r_rtt = RTT_ref / RTT_measured``
    (reference on top, so a lower measured RTT gives a higher ratio -- the same 'higher is better'
    orientation as the throughput axes, and reference-invariant per Fleming). Returns None until a
    reference VM-pair RTT is frozen. RTT stays a DISCLOSED sub-metric; it is never folded into the DCI
    composite (C17 genre note: the network is reported separately)."""
    if not rtt_ms or not reference_rtt_ms or rtt_ms <= 0:
        return None
    return reference_rtt_ms / rtt_ms


def normalize_against_reference(scalars: Dict[str, Optional[float]],
                                reference_scalars: Dict[str, Optional[float]]) -> dict:
    """Per-axis ratio (vm / reference) over the axes present in BOTH, plus the DCI = geometric mean of
    those ratios (invariant to the reference choice, fleming1986). Partial vectors are honest: only the
    shared present axes are used, and which axes counted is disclosed."""
    import math
    ratios: Dict[str, float] = {}
    for a, v in scalars.items():
        r = reference_scalars.get(a)
        if v is not None and r:
            ratios[a] = v / r
    dci = (math.exp(sum(math.log(x) for x in ratios.values()) / len(ratios))) if ratios else None
    return {"ratios": ratios, "dci_partial": dci, "axes": sorted(ratios)}


def run_capability_on_handle(handle: SSHHandle, *, reference_scalars: Optional[Dict] = None,
                             stream_c_source: Optional[str] = None,
                             network_pair: Optional[NetworkPair] = None,
                             nominal_nic: Optional[str] = None,
                             do_install: bool = True, sudo: str = "sudo ",
                             residency: str = "app-resident", ssh_ready_timeout: float = 180.0) -> dict:
    """Full off-clock capability pass on one instance: wait for first SSH, measure, normalize vs the
    reference. Provider-agnostic (takes an SSHHandle). ``network_pair`` is present only for a >=2-VM
    operation and drives the VM-to-VM network axis (C17); when None the network axis is N/A (single-VM,
    C18) and ``nominal_nic`` is disclosed as a constraint. Returns a JSON-able report; never raises on a
    probe failure (a failed axis is recorded and disclosed)."""
    t = wait_first_ssh(handle, timeout_s=ssh_ready_timeout)
    if t is None:
        return {"ok": False, "error": f"no SSH within {ssh_ready_timeout}s",
                "endpoint": f"{handle.user}@{handle.host}:{handle.port}"}
    network = measure_network_pair(network_pair, do_install=do_install, sudo=sudo) if network_pair else None
    res = run_capability(ssh_exec(handle), network=network, nominal_nic=nominal_nic,
                         stream_c_source=stream_c_source,
                         do_install=do_install, sudo=sudo, residency=residency)
    report = {"ok": True, "endpoint": f"{handle.user}@{handle.host}:{handle.port}", **res.to_dict()}
    if reference_scalars:
        report["normalized"] = normalize_against_reference(res.scalars, reference_scalars)
    return report


def run_capability_try_keys(host: str, user: str, port: int, candidate_keys: List[str], *,
                            reference_scalars: Optional[Dict] = None, stream_c_source: Optional[str] = None,
                            network_pair: Optional[NetworkPair] = None, nominal_nic: Optional[str] = None,
                            do_install: bool = True, sudo: str = "sudo ",
                            residency: str = "app-resident", total_timeout: float = 180.0,
                            clock: Callable[[], float] = time.monotonic,
                            sleep: Callable[[float], None] = time.sleep) -> dict:
    """Find which candidate key authenticates to host:port, then run the capability pass with it. Needed
    because a deploy may store its private key under a name that does not match get_ssh_command's -i (the
    microVM had no openssh-client and improvised), so we try the recovered keys until one connects. One
    loop handles both 'which key' and 'SSH not up yet' (the VM may still be finishing boot)."""
    keys = [os.path.expanduser(k) for k in candidate_keys if k and os.path.exists(os.path.expanduser(k))]
    tried = [os.path.basename(k) for k in keys]
    if not keys:
        return {"ok": False, "error": "no candidate private keys on the host",
                "endpoint": f"{user}@{host}:{port}"}
    start = clock()
    while clock() - start < total_timeout:
        for kp in keys:
            h = SSHHandle(host=host, user=user, private_key_path=kp, port=port)
            rc, _, _ = ssh_exec(h, ssh_retries=1)("true", 20)
            if rc == 0:
                return run_capability_on_handle(h, reference_scalars=reference_scalars,
                                                stream_c_source=stream_c_source, network_pair=network_pair,
                                                nominal_nic=nominal_nic,
                                                do_install=do_install, sudo=sudo, residency=residency,
                                                ssh_ready_timeout=30.0)
        sleep(5)
    return {"ok": False, "error": f"no SSH within {int(total_timeout)}s (keys tried: {tried})",
            "endpoint": f"{user}@{host}:{port}"}


class CapabilityAdapter(abc.ABC):
    """The thin per-provider boundary (C13). Capability C is measured on the OPERATION'S OWN VM (C14),
    so the adapter does NOT provision a benchmark VM; it only RESOLVES the existing deployment's shell
    into a provider-independent SSHHandle (endpoint + the harness's injected key). Everything above this
    interface is byte-identical across clouds and never sees the provider."""

    @abc.abstractmethod
    def ssh_handle(self, deployment_ref: object) -> Optional[SSHHandle]:
        """Return an SSHHandle to the instance that ran `deployment_ref`, or None if it exposes no host
        (e.g. a managed container service -- capability C is then N/A, disclosed, not faked)."""
        raise NotImplementedError


# --- result container -------------------------------------------------------------------------

@dataclass
class CapabilityResult:
    cvector: Optional[CVector] = None            # the 4 headline scalars (None axes allowed)
    raw: Dict[str, object] = field(default_factory=dict)      # full multi-metric detail (disclosed)
    errors: Dict[str, str] = field(default_factory=dict)      # axis -> failure reason
    host: Dict[str, str] = field(default_factory=dict)        # nproc, mem, kernel (disclosure)

    scalars: Dict[str, Optional[float]] = field(default_factory=dict)  # the 4 axes, None where missing
    disclosure: Dict[str, object] = field(default_factory=dict)        # C14 residency/regime disclosure

    def to_dict(self) -> dict:
        return {
            "cvector": self.cvector.as_dict() if self.cvector else None,
            "scalars": self.scalars,
            "axes_present": sorted(a for a, v in self.scalars.items() if v is not None),
            "raw": self.raw,
            "errors": self.errors,
            "host": self.host,
            "disclosure": self.disclosure,
        }


# --- fio latency (probes.parse_fio gives IOPS/BW; latency lives elsewhere in the json) ---------

def parse_fio_latency_us(output: str) -> float:
    """Mean read completion latency in microseconds from fio --output-format=json."""
    data = probes._first_json(output)
    jobs = data.get("jobs", [])
    if not jobs:
        raise ValueError("no fio jobs in output")
    read = jobs[0].get("read", {})
    # fio reports clat/lat in ns; prefer completion latency (clat_ns), fall back to total lat_ns
    lat = read.get("clat_ns") or read.get("lat_ns") or {}
    mean_ns = float(lat.get("mean", 0.0))
    return mean_ns / 1000.0


# --- the runner -------------------------------------------------------------------------------

def _try(errors: Dict[str, str], axis: str, fn):
    """Run one probe; on any failure record the reason under ``axis`` and return None (off-clock: a
    probe never aborts the calibration, and a missing axis is disclosed rather than faked)."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - a probe failure is a recorded result, not a crash
        errors[axis] = repr(e)[:300]
        return None


def collect_host_facts(exec_fn: ExecFn) -> Dict[str, str]:
    rc, out, _ = exec_fn("echo nproc=$(nproc); "
                         "echo mem_kb=$(awk '/MemTotal/{print $2}' /proc/meminfo); "
                         "echo kernel=$(uname -r); "
                         "echo cpu=\"$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs)\"",
                         60)
    facts: Dict[str, str] = {}
    for line in (out or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            facts[k.strip()] = v.strip()
    return facts


def install_tools(exec_fn: ExecFn, *, sudo: str = "sudo ", timeout_s: int = 600) -> Tuple[bool, str]:
    """Idempotently install the probe tools (Debian/Ubuntu target). Returns (ok, log)."""
    cmd = (f"{sudo}DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
           f"{sudo}DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
           f"sysbench fio iperf3 gcc make >/dev/null 2>&1 && echo INSTALL_OK")
    rc, out, err = exec_fn(cmd, timeout_s)
    ok = "INSTALL_OK" in (out or "")
    return ok, (out or "") + (err or "")


def build_stream(exec_fn: ExecFn, stream_c_source: str, *, array_size: int = 40_000_000,
                 timeout_s: int = 300) -> Tuple[bool, str]:
    """Compile McCalpin STREAM on the target from a pinned stream.c (array >= 4x LLC per the method)."""
    src_q = shlex.quote(stream_c_source)
    cmd = (f"printf '%s' {src_q} > /tmp/stream.c && "
           f"gcc -O3 -fopenmp -DSTREAM_ARRAY_SIZE={array_size} -DNTIMES=20 "
           f"/tmp/stream.c -o /tmp/stream_bin 2>&1 && echo BUILD_OK")
    rc, out, err = exec_fn(cmd, timeout_s)
    return ("BUILD_OK" in (out or "")), (out or "") + (err or "")


def run_capability(exec_fn: ExecFn, *, network: Optional[dict] = None,
                   nominal_nic: Optional[str] = None,
                   stream_c_source: Optional[str] = None, do_install: bool = True,
                   sudo: str = "sudo ", residency: str = "app-resident") -> CapabilityResult:
    """Measure the delivered-capability vector through ``exec_fn`` (local host or a VM over SSH).

    ``network`` is a pre-measured VM-to-VM result (from :func:`measure_network_pair`) for a >=2-VM
    operation; when None the network axis is NOT-APPLICABLE (single-VM, C18), disclosed rather than
    errored, and ``nominal_nic`` (if given) is recorded as a human-facing constraint, not a score.
    ``stream_c_source`` is the pinned McCalpin source; if None the memory axis is skipped.
    ``residency`` records the measurement condition for the C14 disclosure ("app-resident" for the
    in-situ pass on the operation's own VM, "quiescent" or "idle" otherwise). All probes are off-clock
    and failure-tolerant.
    """
    res = CapabilityResult()
    res.disclosure = {
        "residency": residency,
        "note": ("C is delivered capability UNDER RESIDENCY measured in situ on the operation's own VM "
                 "(CR C12/C14), not a clean-boot peak; on contention-bound axes it can read low by "
                 "~10-18%. Probe in a quiescent post-serving window; the cross-platform ratio cancels "
                 "the resident-app term as common mode (Duet)."),
    }
    res.host = _try(res.errors, "host", lambda: collect_host_facts(exec_fn)) or {}

    if do_install:
        ok, log = install_tools(exec_fn, sudo=sudo)
        if not ok:
            res.errors["install"] = log[-300:]

    # compute: sysbench single-task + all-core
    single = _try(res.errors, "compute_single",
                  lambda: probes.parse_sysbench_cpu(_ok(exec_fn(SYSBENCH_SINGLE, 180))))
    allcore = _try(res.errors, "compute",
                   lambda: probes.parse_sysbench_cpu(_ok(exec_fn(SYSBENCH_ALLCORE, 180))))
    res.raw["sysbench_single_eps"] = single
    res.raw["sysbench_allcore_eps"] = allcore

    # memory: STREAM Triad
    triad = None
    if stream_c_source:
        built, blog = build_stream(exec_fn, stream_c_source)
        if built:
            stream = _try(res.errors, "memory",
                          lambda: probes.parse_stream(_ok(exec_fn("/tmp/stream_bin", 180))))
            triad = (stream or {}).get("triad")
            res.raw["stream_gbps"] = stream
        else:
            res.errors["memory"] = "stream build failed: " + blog[-200:]
    else:
        res.errors["memory"] = "no stream.c source provided"

    # disk: fio grid (IOPS @ QD32, BW @ QD32, latency @ QD1), O_DIRECT
    iops = _try(res.errors, "disk",
                lambda: probes.parse_fio(_ok(exec_fn(FIO_IOPS, _FIO_RUNTIME + 90))))
    bw = _try(res.errors, "disk_bw",
              lambda: probes.parse_fio(_ok(exec_fn(FIO_BW, _FIO_RUNTIME + 90))))
    lat = _try(res.errors, "disk_lat",
               lambda: parse_fio_latency_us(_ok(exec_fn(FIO_LAT, _FIO_RUNTIME + 90))))
    exec_fn(f"rm -f {_FIO_FILE}", 30)
    res.raw["fio_iops"] = iops
    res.raw["fio_bw_kbps"] = bw
    res.raw["fio_lat_us_qd1"] = lat

    # network: intra-cloud VM-to-VM on the tenant private network (C17/C18). SCORED as an axis ONLY for
    # a >=2-VM operation (a NetworkPair was measured and passed in as `network`); for a single-VM
    # operation it is NOT-APPLICABLE -- disclosed, NOT an error -- and the nominal NIC is a human-facing
    # constraint like RAM/disk GB, never a capability score.
    net_tcp = None
    if network is not None:
        net_tcp = network.get("tcp_bps")
        res.raw["net_vm_to_vm"] = network.get("raw", network)
        for a, msg in (network.get("errors") or {}).items():
            res.errors[a] = msg
        res.disclosure["network"] = {
            "applicable": True,
            "regime": "vm_to_vm_private",
            "rtt_ms_min": network.get("rtt_ms_min"),
            "rtt_ms_avg": network.get("rtt_ms_avg"),
            "rtt_ms_max": network.get("rtt_ms_max"),
            "rtt_ms_mdev": network.get("rtt_ms_mdev"),
            "tcp_bps_distribution": network.get("tcp_bps_distribution"),
            "stream_count": network.get("stream_count"),
            "tcp_window": network.get("tcp_window"),
            "placement": network.get("placement"),
        }
    else:
        res.disclosure["network"] = {
            "applicable": False,
            "reason": ("single-VM operation: network is not a scored DCI axis (C18). The nominal NIC "
                       "is a disclosed constraint, not a capability score."),
            "nominal_nic": nominal_nic,
        }

    # reduce to the 4 headline scalars (None where a probe failed; disclosed via axes_present)
    res.scalars = {
        "compute": allcore,
        "memory": triad,
        "disk": (iops or {}).get("read_iops") if iops else None,
        "network": net_tcp,
    }
    # a CVector (for the DCI headline) exists only when every axis is present; otherwise C stays a
    # disclosed partial vector (the paper keeps C a vector, DCI is the OPTIONAL headline scalar).
    if all(v is not None for v in res.scalars.values()):
        res.cvector = CVector(**res.scalars)  # type: ignore[arg-type]
    return res


def _ok(result: Tuple[int, str, str]) -> str:
    """Return stdout, raising with stderr if the command failed or produced nothing."""
    rc, out, err = result
    if rc != 0:
        raise RuntimeError(f"exit {rc}: {(err or out)[-200:]}")
    if not (out or "").strip():
        raise RuntimeError(f"empty output (stderr: {(err or '')[-200:]})")
    return out
