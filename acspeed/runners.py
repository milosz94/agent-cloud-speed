"""Thin runners that execute the delivered-capability probes and parse their
output (Part 1, Section 3).

These require the corresponding tool installed on the target VM. They are
intentionally not unit-tested here; the tested logic lives in ``probes.py``.
Measure the vector as a separate post-provision calibration pass, off the
operation-timing critical path (BUNGEE two-phase design).
"""
from __future__ import annotations

import subprocess
from typing import Dict

from . import probes


def _run(cmd) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def run_sysbench_cpu(threads: int = 1, max_prime: int = 20000) -> float:
    out = _run(["sysbench", "cpu", f"--cpu-max-prime={max_prime}",
                f"--threads={threads}", "run"])
    return probes.parse_sysbench_cpu(out)


def run_stream(binary: str = "stream") -> Dict[str, float]:
    return probes.parse_stream(_run([binary]))


def run_fio(config_path: str) -> Dict[str, float]:
    out = _run(["fio", "--output-format=json", config_path])
    return probes.parse_fio(out)


def run_iperf3(server_ip: str, parallel: int = 10, seconds: int = 30) -> Dict[str, float]:
    out = _run(["iperf3", "-J", "-c", server_ip, "-P", str(parallel), "-t", str(seconds)])
    return probes.parse_iperf3(out)
