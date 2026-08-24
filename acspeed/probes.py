"""Probe output parsers (Part 1, Section 3).

Pure parsing of the output produced by the standard delivered-capability probes,
so the parsing logic is unit-tested offline on captured output. The runner
wrappers that actually shell out to the tools live in ``runners.py`` and require
the tools installed on the target VM.

Axis mapping: compute = sysbench events/s; memory = STREAM Triad GB/s;
disk = fio IOPS / bandwidth; network = iperf3 bits/s.
"""
from __future__ import annotations

import json
import re
from typing import Dict


def parse_sysbench_cpu(output: str) -> float:
    """Events per second from ``sysbench cpu ... run`` output."""
    m = re.search(r"events per second:\s*([0-9]+\.?[0-9]*)", output)
    if not m:
        raise ValueError("could not find 'events per second' in sysbench output")
    return float(m.group(1))


def parse_stream(output: str) -> Dict[str, float]:
    """GB/s per kernel from STREAM output (Copy/Scale/Add/Triad).

    STREAM reports MB/s; values are converted to GB/s.
    """
    out: Dict[str, float] = {}
    for kernel in ("Copy", "Scale", "Add", "Triad"):
        m = re.search(rf"^{kernel}:\s+([0-9]+\.?[0-9]*)", output, re.MULTILINE)
        if m:
            out[kernel.lower()] = float(m.group(1)) / 1000.0
    if not out:
        raise ValueError("no STREAM kernels found in output")
    return out


def parse_fio(output: str) -> Dict[str, float]:
    """IOPS and bandwidth (KB/s) from ``fio --output-format=json`` output."""
    data = json.loads(output)
    jobs = data.get("jobs", [])
    if not jobs:
        raise ValueError("no fio jobs in output")
    j = jobs[0]
    read = j.get("read", {})
    write = j.get("write", {})
    return {
        "read_iops": float(read.get("iops", 0.0)),
        "write_iops": float(write.get("iops", 0.0)),
        "read_bw_kbps": float(read.get("bw", 0.0)),
        "write_bw_kbps": float(write.get("bw", 0.0)),
    }


def parse_iperf3(output: str) -> Dict[str, float]:
    """Bits per second from ``iperf3 -J`` (JSON) output."""
    data = json.loads(output)
    end = data.get("end", {})
    recv = end.get("sum_received", {})
    sent = end.get("sum_sent", {})
    return {
        "received_bps": float(recv.get("bits_per_second", 0.0)),
        "sent_bps": float(sent.get("bits_per_second", 0.0)),
    }
