"""Probe output parsers (Part 1, Section 3).

Pure parsing of the output produced by the standard delivered-capability probes,
so the parsing logic is unit-tested offline on captured output. The runner
wrappers that actually shell out to the tools live in ``runners.py`` and require
the tools installed on the target VM.

Axis mapping: compute = sysbench events/s; memory = STREAM Triad GB/s;
disk = fio IOPS / bandwidth; network = VM-to-VM iperf3 bits/s + ping RTT ms (C17).
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


def _first_json(output: str) -> dict:
    """Parse the first JSON object in `output`, tolerating leading/trailing non-JSON (a login-shell
    banner, an MOTD, or a tool warning printed before the JSON). fio and iperf3 emit strict JSON but a
    remote shell can prepend text; regex-based parsers survive that, strict json.loads does not."""
    i = output.find("{")
    if i < 0:
        raise ValueError(f"no JSON object in output: {output[:160]!r}")
    obj, _ = json.JSONDecoder().raw_decode(output[i:])
    return obj


def parse_fio(output: str) -> Dict[str, float]:
    """IOPS and bandwidth (KB/s) from ``fio --output-format=json`` output."""
    data = _first_json(output)
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


def parse_iperf3(output: str) -> Dict[str, object]:
    """Bits per second from ``iperf3 -J`` (JSON) output, plus the per-interval throughput series so the
    network axis can report a DISTRIBUTION, not just the mean (C17 E1: report the distribution). The
    scalar ``received_bps`` stays the headline; ``intervals_bps`` is the disclosed distribution."""
    data = _first_json(output)
    end = data.get("end", {})
    recv = end.get("sum_received", {})
    sent = end.get("sum_sent", {})
    intervals = []
    for iv in data.get("intervals", []):
        s = iv.get("sum", {})
        if "bits_per_second" in s:
            intervals.append(float(s["bits_per_second"]))
    return {
        "received_bps": float(recv.get("bits_per_second", 0.0)),
        "sent_bps": float(sent.get("bits_per_second", 0.0)),
        "intervals_bps": intervals,
    }


def parse_ping(output: str) -> Dict[str, float]:
    """Round-trip time in milliseconds from ``ping`` summary output. Reads the summary line
    ``rtt min/avg/max/mdev = a/b/c/d ms`` (Linux iputils) or ``round-trip min/avg/max = a/b/c ms``
    (BSD). ``min_ms`` is the physical path floor (the tail is queueing), so the RTT sub-metric of the
    network axis is reported as the minimum (C17)."""
    m = re.search(r"=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)(?:/([0-9.]+))?\s*ms", output)
    if not m:
        raise ValueError("could not find an 'rtt min/avg/max' summary in ping output")
    out = {
        "min_ms": float(m.group(1)),
        "avg_ms": float(m.group(2)),
        "max_ms": float(m.group(3)),
    }
    if m.group(4) is not None:      # mdev (Linux) -> the RTT spread, part of the disclosed distribution
        out["mdev_ms"] = float(m.group(4))
    return out
