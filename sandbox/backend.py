#!/usr/bin/env python3
"""THE PER-TURN SUBSTRATE BACKEND INTERFACE.

acspeed runs every agent turn in a throwaway machine that holds ONLY the target cloud's credentials
(PAPER C9). Today that machine is a Firecracker microVM, which is Linux-only because Firecracker
requires /dev/kvm. This module is the seam that lets macOS (Virtualization.framework, via vfkit) and
Windows (WSL2 --import-in-place, or QEMU+WHPX) provide the SAME per-turn machine, so autorun.py calls
one function and never learns which host it is on.

WHAT IS AND IS NOT NEGOTIABLE

  Non-negotiable, because it is the instrument: the host is the clock. autorun takes t0 on the host,
  polls readiness with curl FROM the host, and joins that host clock against guest-written transcript
  timestamps. Every backend here runs its guest as a CHILD PROCESS ON THE SAME MACHINE. A remote
  runner cannot implement this interface and must not be added to it.

  Non-negotiable, because autorun tails it: `boot_log` is a plain host file that GROWS as the guest
  emits lines. ReadinessPoller (autorun.py ReadinessPoller._source_text) re-reads that file every
  poll interval, looking for `ACSPEED_URL <url>`. Any backend must make guest console/stdout bytes
  land in that file, live. The poller is not changed by any backend.

  Negotiable: everything else. There is no requirement that the job arrive on a block device, that
  results come back via debugfs, or that the guest power itself off; those are Firecracker idioms and
  the interface states the CONTRACT (a job goes in, artifacts come out, the turn ends) rather than
  the mechanism.

THE SEAM IN autorun.py. Measured, not assumed: the only consumer of run_vm_job today is
`_claude_vm` (autorun.py:540-611). It reads `res["result"]`, `res.get("agent_ssh_dir")`,
`res["boot_log"]`, `res.get("exit_code")`, `res.get("timed_out")`, `res.get("work_dir")`, and then
reaches into Firecracker internals twice:

    vmjob.sync_claude_login_from_vm(os.path.join(wd, "rootfs.ext4"))          # autorun.py:589
    vmjob._debugfs_dump(os.path.join(wd, "job.ext4"), "/err.txt", ...)        # autorun.py:599
    for big in ("rootfs.ext4", "job.ext4", "agent_ssh.tar", "transcripts.tar"): os.remove(...)

`rootfs.ext4`, `job.ext4` and `debugfs` do not exist on macOS or Windows. Those three are therefore
part of the interface (`adopt_agent_login`, `collect_diagnostics`, `reclaim`), not of autorun. Also
`sandbox_available()` (autorun.py:124-136) probes vmjob.BIN_FC / KERNEL / BASE_ROOTFS / /dev/kvm /
tap slots directly; that becomes `preflight()` + `capacity()`.
"""
from __future__ import annotations

import abc
import dataclasses
import sys
from typing import Any, Protocol, TypedDict, runtime_checkable


# ---------------------------------------------------------------------------------------------
# The returned dict. EXACTLY what vmjob.run_vm_job returns today, so _claude_vm keeps working.
# ---------------------------------------------------------------------------------------------
class JobResult(TypedDict, total=False):
    """The per-turn result. Key-for-key identical to what `vmjob.run_vm_job` returns at HEAD.

    Always present (every backend MUST set all seven, even to None):

      result: dict | None
          The agent's parsed output. For claude, the single JSON object `claude -p --output-format
          json` printed, read back from the job artifact `out.json`. For codex, whatever the guest
          wrote to out.json (autorun.parse_agent_result normalizes the JSONL form). None when the
          guest never produced one, which autorun treats as a BOOT FLAKE and retries in a fresh
          machine (VM_BOOT_RETRIES). Never fabricate a dict here: a synthesized result would be
          retried never, and a boot flake would be recorded as an agent failure.
          When out.json exists but is not JSON, set {"is_error": True, "result": <last 2000 chars>}
          -- that is what vmjob does and autorun relies on it being a dict.

      exit_code: str | None
          The agent process's exit status as the guest wrote it (a decimal STRING, not an int:
          vmjob reads the `exit_code` artifact and `.strip()`s it; autorun only formats it into a
          log line). None when the artifact is missing.

      transcripts_dir: str | None
          Host directory holding the extracted `projects/` tree (claude) written by the guest, i.e.
          the tree `find_transcript_in(dir, session_id)` globs for `<session_id>.jsonl`. None when
          no transcript came back.

      boot_log: str
          Path to the host file carrying live guest console/stdout. The CALLER usually supplies it
          (autorun.py:1899 creates it empty before t0 so the poller can tail from t0); when the
          caller passes None the backend picks `<work_dir>/boot.log`. Always echoed back, never
          None.

      timed_out: bool
          True when the backend had to kill the machine because `timeout` elapsed. A timed-out turn
          usually also has result=None.

      work_dir: str
          The per-turn host scratch directory. autorun deletes it on success and prunes it on
          failure via `reclaim()`. MUST have a basename starting with `acspeed-vm-` when the backend
          created it itself; autorun guards on that prefix so a caller-provided work_dir is never
          touched (autorun.py:596).

      session_id: str | None
          Convenience copy of `(result or {}).get("session_id")`.

    Present only when the artifact existed (absent key, not None -- autorun uses .get()):

      agent_ssh_dir: str
          Host directory of the extracted `~/.ssh` from inside the machine, so the host can reach a
          deploy VM whose keypair the agent minted in-guest (off-clock capability pass).
    """
    result: dict | None
    exit_code: str | None
    transcripts_dir: str | None
    boot_log: str
    timed_out: bool
    work_dir: str
    session_id: str | None
    agent_ssh_dir: str


# ---------------------------------------------------------------------------------------------
# The job. Every field is what vmjob.run_vm_job takes today, in the same names.
# ---------------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class JobSpec:
    """One agent turn's inputs. Field names match `vmjob.run_vm_job`'s keyword arguments exactly, so
    `backend.run(JobSpec(**kwargs))` and `run_vm_job(**kwargs)` take the same call."""

    prompt: str                                   # the task text handed to the agent
    app_dir: str                                  # host dir tarred in and extracted to the guest cwd
    keep_claude_tokens: list[str]                 # mcpOAuth serverNames to KEEP (['redu'] / [] for aws)
    max_turns: int
    timeout: int                                  # host-side wall clock; exceeded -> kill, timed_out=True
    model: str | None = None
    mcp_config: str | None = None                 # host path to mcp.json; codex gets it as config.toml
    aws_dir: str | None = None                    # ~/.aws to stage, or None
    creds_mounts: dict[str, str] | None = None    # {guest_stage_name: host_dir}, e.g. dot-azure
    agent: str = "claude"                         # "claude" | "codex"; picks the CLI inside the guest
    vcpus: int = 2                                # see CAPABILITY 9 note in the ABC: WSL2 cannot honour this
    mem_mib: int = 4096                           # ditto
    work_dir: str | None = None
    boot_log: str | None = None                   # caller-created, tailed from t0; None -> work_dir/boot.log
    system: str | None = None
    resume_sid: str | None = None
    resume_transcript: str | None = None
    session_store: str | None = None              # host dir the transcripts are merged into


# ---------------------------------------------------------------------------------------------
# The interface.
# ---------------------------------------------------------------------------------------------
@runtime_checkable
class SubstrateBackend(Protocol):
    """Structural form of the interface, for type-checking third-party backends without subclassing.
    The authoritative documentation is `BaseSubstrateBackend` below; this Protocol only restates the
    signatures so `isinstance(obj, SubstrateBackend)` is a usable check."""

    name: str
    isolation: str

    def preflight(self) -> tuple[bool, str]: ...
    def capacity(self) -> int: ...
    def run(self, spec: JobSpec) -> JobResult: ...
    def adopt_agent_login(self, result: JobResult) -> bool: ...
    def collect_diagnostics(self, result: JobResult) -> None: ...
    def reclaim(self, result: JobResult, *, keep_diagnostics: bool) -> None: ...
    def describe(self) -> dict[str, Any]: ...


class BaseSubstrateBackend(abc.ABC):
    """One throwaway machine per agent turn, on THIS host.

    A backend must satisfy the eight capabilities the Firecracker backend provides today. They are
    numbered here and referenced by number in every concrete backend's docstring, with each one
    marked NATIVE (the host mechanism does it directly), EMULATED (a different mechanism meets the
    same contract) or IMPOSSIBLE (it cannot be met; the run must be recorded as a different
    substrate and must not be pooled with VM-level rows).

      C1  A FRESH, writable Linux root filesystem per turn, discarded afterwards.
      C2  Credential scoping: ONLY the target cloud's creds plus the agent's own auth are reachable.
      C3  A job channel carrying prompt.txt, model.txt, agent.txt, max_turns.txt, mcp.json,
          dot-claude/ or dot-codex/, per-cloud creds dirs, and app.tar.
      C4  The agent runs headless as a NON-ROOT user, cwd = the extracted app.
      C5  Artifacts come back over that same channel: out.json, err.txt, exit_code, transcripts.tar,
          codex_sessions.tar, agent_ssh.tar.
      C6  A LIVE URL RELAY: the guest echoes any URL it sees in the transcript, and the HOST reads
          those lines WHILE the turn runs and polls the URL itself from its own neutral vantage.
      C7  Outbound network, so the agent can reach the cloud's API.
      C8  A definite end-of-turn signal, so the host knows the turn is over and the artifacts are
          complete.

    And one measurement property that is not a capability but is load-bearing for cross-OS
    comparability:

      C9  A FIXED, per-turn resource envelope (`vcpus`, `mem_mib`) that does not change with how many
          turns run concurrently. Firecracker gives each VM exactly 2 vCPU / 4096 MiB. A backend that
          cannot honour this changes the agent's compute budget between rows, and its rows are not
          comparable with rows from a backend that can. Say so in `describe()["resource_envelope"]`.
    """

    #: short id used in logs and recorded into every run's manifest ("firecracker", "vfkit", "wsl2")
    name: str = "abstract"
    #: "vm" when each turn gets its own virtual machine; "namespace" when turns share a kernel.
    #: Recorded per run. A "namespace" row must never be pooled with a "vm" row without saying so.
    isolation: str = "vm"

    # -- readiness ------------------------------------------------------------------------------
    @abc.abstractmethod
    def preflight(self) -> tuple[bool, str]:
        """Is this backend usable RIGHT NOW on this host? Returns (True, "") or (False, reason).

        Replaces the body of autorun.sandbox_available() (autorun.py:124-136), whose checks are all
        Firecracker-specific: BIN_FC/KERNEL/BASE_ROOTFS on disk, /dev/kvm readable+writable, at least
        one acspeed tap up. The reason string is printed verbatim in the REFUSING TO RUN block
        (autorun.py:2479-2493), so it must name the exact command that fixes it on THIS OS.

        This is a capability probe, not an inference: check the thing (open /dev/kvm, read
        `sysctl kern.hv_support`, run `wsl.exe --version`), never the OS name alone.
        """

    @abc.abstractmethod
    def capacity(self) -> int:
        """How many turns this backend can run CONCURRENTLY on this host right now.

        Replaces `vmjob.discover_slots()` in the concurrency log line (autorun.py:2592). Firecracker
        returns the number of taps in the pool, because a tap is opened exclusively. Backends using
        host NAT have no shared exclusive device, so their limit is memory, not slots: return the
        honest number (>=1), and if you have not MEASURED a ceiling, return the memory-derived one
        and say `describe()["capacity_basis"] == "memory"` rather than claiming unlimited.
        """

    # -- the turn -------------------------------------------------------------------------------
    @abc.abstractmethod
    def run(self, spec: JobSpec) -> JobResult:
        """Run ONE agent turn in a fresh machine and return when the turn has ended.

        Contract, in order:
          1. Create `spec.work_dir` (or an `acspeed-vm-` temp dir) and a fresh writable root (C1).
          2. Stage the scoped credentials (C2) -- reuse `vmjob.scoped_claude_dir` /
             `vmjob.scoped_codex_dir`, which are pure host-side file shuffling and portable as-is.
          3. Assemble the job channel from the same staging tree (C3).
          4. Start the guest as a CHILD PROCESS of this process, via `acspeed.procguard.spawn` so a
             host Ctrl-C group-kills it. Its console/stdout MUST be written to `spec.boot_log` live
             (C6): open that path and hand the file object to the child as stdout, or point the
             VMM's serial-log option at it.
          5. Wait up to `spec.timeout`. On expiry, kill the machine (and, where the guest outlives
             the launcher process -- WSL2 does -- terminate the guest too), and set timed_out=True.
          6. Read the artifacts back (C5) and return a JobResult.

        MUST NOT raise on a guest-side failure: a turn that produced nothing returns
        result=None, which autorun retries as a boot flake. Raise only when the HOST is
        misconfigured, which preflight() should have caught.

        MUST NOT block on anything off this machine. The turn's wall time is inside acspeed's
        measured window (autorun takes t0 at :1934 and only then calls this), so any per-turn setup
        this method does is billed to t1-t0. Two consequences: keep setup cheap and, more
        importantly, keep it COMPARABLE -- if one backend's per-turn setup differs from another's by
        more than a second or two, cross-OS numbers are not comparable until that difference is
        either hoisted before t0 or recorded as its own field.
        """

    # -- post-turn, backend-private paths (these are why autorun currently leaks) ----------------
    @abc.abstractmethod
    def adopt_agent_login(self, result: JobResult) -> bool:
        """Adopt any Claude-login refresh the guest performed, onto the host. Best-effort; True when
        the host file was updated.

        Replaces the `vmjob.sync_claude_login_from_vm(os.path.join(wd, "rootfs.ext4"))` call at
        autorun.py:589, which hardcodes both the filename and ext4. The guest runs `claude` under the
        host's subscription token WITH its refresh token, so a long run can renew the ~8h access
        token; an OAuth refresh ROTATES the refresh-token family server-side and invalidates the
        host's copy, silently logging the host out. Each backend reads
        `/home/agent/.claude/.credentials.json` out of its OWN root (debugfs dump for an ext4 image,
        a plain read from `\\\\wsl.localhost\\<distro>\\...` for WSL2) and writes back only when the
        guest's `expiresAt` is newer.

        MUST be called before `reclaim()` destroys the root. Never raise.
        """

    @abc.abstractmethod
    def collect_diagnostics(self, result: JobResult) -> None:
        """On a FAILED turn, pull the small diagnostics into `work_dir` so the cause stays knowable
        after the big images are dropped: at minimum `err.txt` (agent stderr) alongside the already-
        present `out.json` and the boot log.

        Replaces the `vmjob._debugfs_dump(os.path.join(wd, "job.ext4"), "/err.txt", ...)` call at
        autorun.py:599 -- a private function, on an ext4 path, with a Linux-only tool. Never raise.
        """

    @abc.abstractmethod
    def reclaim(self, result: JobResult, *, keep_diagnostics: bool) -> None:
        """Free this turn's bulk storage.

        keep_diagnostics=False: remove `work_dir` entirely (the success path; transcripts are already
        merged into session_store and SSH keys installed).
        keep_diagnostics=True: remove only the bulk (root image, job image, recovered tars, an
        unregistered distro's VHDX) and KEEP out.json / err.txt / boot.log.

        Replaces the hardcoded `("rootfs.ext4", "job.ext4", "agent_ssh.tar", "transcripts.tar")`
        loop at autorun.py:602. Unbounded per-turn roots are what filled the disk and starved a cloud
        mid-batch, so this is not optional housekeeping. Never raise.
        """

    # -- provenance -----------------------------------------------------------------------------
    @abc.abstractmethod
    def describe(self) -> dict[str, Any]:
        """Machine-readable provenance to record in every run's manifest, so a row can never be read
        as though it came from a different substrate than it did. Required keys:

            {"backend": self.name,                  # "firecracker" | "vfkit" | "wsl2" | "qemu-whpx"
             "isolation": self.isolation,           # "vm" | "namespace"
             "host_os": sys.platform,
             "host_arch": platform.machine(),
             "guest_arch": ...,                     # x86_64 / aarch64 -- differs from host on nothing
                                                    # today, but the Mac path needs an arm64 guest
             "hypervisor": ...,                     # "kvm" | "Virtualization.framework" | "Hyper-V"
             "vmm_version": ...,                    # measured by running the binary, not assumed
             "kernel": ...,                         # image id/version actually booted
             "resource_envelope": {"vcpus": int | None, "mem_mib": int | None, "fixed": bool},
             "capacity_basis": "slots" | "memory" | "measured",
             "caveats": [str, ...]}                 # e.g. "shared network namespace between turns"

        `resource_envelope["fixed"]` is False, and the caveat is mandatory, for any backend that
        cannot pin per-turn CPU/RAM (C9).
        """


# ---------------------------------------------------------------------------------------------
# Backwards-compatible free function, so autorun's existing call site is untouched.
# ---------------------------------------------------------------------------------------------
_BACKEND: BaseSubstrateBackend | None = None


def get_backend(force: str | None = None) -> BaseSubstrateBackend:
    """Return the backend for this host (cached). `force` (or ACSPEED_BACKEND) overrides selection,
    which exists so a Linux host can exercise the QEMU backend in CI.

    Selection is by MEASURED capability, not by OS name alone: each candidate's preflight() runs and
    the first that passes wins, so a Linux box without /dev/kvm falls through to QEMU rather than
    refusing. The chosen name is logged and lands in describe(), because a run whose substrate is
    unknown is a run that cannot be published.
    """
    raise NotImplementedError("wire up once the concrete backends land")


def run_vm_job(**kwargs: Any) -> JobResult:
    """Shim keeping `vmjob.run_vm_job(...)`'s exact call shape. autorun.py:552 calls this by keyword
    only, so `JobSpec(**kwargs)` accepts it unchanged."""
    return get_backend().run(JobSpec(**kwargs))


__all__ = ["JobSpec", "JobResult", "SubstrateBackend", "BaseSubstrateBackend", "get_backend",
           "run_vm_job"]

assert sys.version_info >= (3, 10), "JobSpec uses PEP 604 unions in annotations"
