"""acspeed command-line interface.

Run the Part 1 decomposition on a JSON trace:

    python -m acspeed agent-time examples/example_trace.json
    python -m acspeed critical-path examples/example_trace.json
"""
from __future__ import annotations

import argparse
import json

from .agenttime import decompose
from .criticalpath import critical_path, owner_split
from .traceio import load_trace


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="acspeed",
                                description="Agent-cloud speed baseline (Part 1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("agent-time", help="decompose a JSON trace into raw/critical agent-time")
    a.add_argument("trace")

    c = sub.add_parser("critical-path", help="print the critical path and owner split")
    c.add_argument("trace")

    args = p.parse_args(argv)
    spans = load_trace(args.trace)

    if args.cmd == "agent-time":
        print(json.dumps(decompose(spans), indent=2))
    elif args.cmd == "critical-path":
        chain = [s.id for s in critical_path(spans)]
        print(json.dumps({"critical_path": chain, **owner_split(spans)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
