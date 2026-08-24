"""Worked example: decompose a deploy trace and demonstrate the discriminator.

Run from the repo root:  python examples/run_example.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acspeed.agenttime import decompose  # noqa: E402
from acspeed.criticalpath import critical_path, owner_split  # noqa: E402
from acspeed.discriminator import fit_fixed_variable, plane_shares  # noqa: E402
from acspeed.traceio import load_trace  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    spans = load_trace(os.path.join(HERE, "example_trace.json"))

    print("== The spine: critical path ==")
    print("critical path:", [s.id for s in critical_path(spans)])
    print(json.dumps(owner_split(spans), indent=2))

    print("\n== Agent-time decomposition ==")
    print(json.dumps(decompose(spans), indent=2))

    print("\n== Platform discriminator ==")
    # Provisioning time on instances of rising delivered capability (rate).
    rates = [1.0, 2.0, 4.0, 8.0]
    times = [2.0 + 20.0 / r for r in rates]  # true control-plane floor 2s, data-plane work 20
    fit = fit_fixed_variable(rates, times)
    print("fit T = t_fixed + W/rate:", fit)
    print("plane shares @ rate=2:", plane_shares(fit, 2.0))


if __name__ == "__main__":
    main()
