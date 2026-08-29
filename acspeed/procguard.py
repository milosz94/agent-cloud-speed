"""Process-group guard for clean Ctrl-C.

The harness spawns long-lived children: the nested ``claude`` agent (which itself spawns MCP servers)
and a Firecracker microVM (which runs the guest agent). Two things make an un-guarded run hang on
Ctrl-C:

  1. A plain ``subprocess`` kill reaches only the DIRECT child, so an agent's MCP-server grandchildren
     survive and hold the terminal.
  2. Firecracker with ``console=ttyS0`` inherits the terminal's stdin and forwards keystrokes to the
     guest, so the user's Ctrl-C goes INTO the VM instead of raising SIGINT on the host.

This module fixes both, with zero cloud specifics:

  * ``spawn`` starts each child in its OWN session (``start_new_session=True``), so it is a process
    group leader and its pgid equals its pid; ``os.killpg(pid, ...)`` then reaches the child AND
    everything it spawned. It also detaches stdin (``DEVNULL`` by default) so a child cannot capture
    the controlling terminal.
  * ``kill_all`` group-kills every tracked child, for a signal handler to call on SIGINT/SIGTERM.

Nothing here knows about any cloud, app, or tier."""
from __future__ import annotations

import os
import signal
import subprocess
import threading

_lock = threading.Lock()
_tracked: set[int] = set()   # pgids; == child pid, since each child is started as a new session leader


def spawn(cmd, **kwargs) -> subprocess.Popen:
    """``subprocess.Popen(cmd)`` started in its own session (process-group leader) and tracked for
    group-kill. stdin defaults to ``DEVNULL`` so the child cannot grab the terminal (Firecracker's
    console would otherwise swallow the user's Ctrl-C). Extra kwargs pass through unchanged."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
    with _lock:
        _tracked.add(proc.pid)
    return proc


def untrack(pid: int) -> None:
    """Forget a child that has exited normally (so kill_all does not signal a recycled pid)."""
    with _lock:
        _tracked.discard(pid)


def kill_group(pid: int, sig: int = signal.SIGKILL) -> None:
    """Kill one tracked child's whole process group, then untrack it. Safe if already dead."""
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, OSError):
        pass
    untrack(pid)


def kill_all(sig: int = signal.SIGKILL) -> int:
    """Kill every tracked child's process group (child + descendants). Returns how many were signalled.
    Called by the SIGINT/SIGTERM handler so an interrupt leaves nothing running."""
    with _lock:
        pids = list(_tracked)
        _tracked.clear()
    for pid in pids:
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, OSError):
            pass
    return len(pids)


def tracked_count() -> int:
    with _lock:
        return len(_tracked)
