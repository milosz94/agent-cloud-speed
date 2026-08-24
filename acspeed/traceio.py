"""Load and dump execution traces as JSON (a list of span dicts)."""
from __future__ import annotations

import json
from typing import List, Sequence

from .types import Span


def spans_from_dicts(items) -> List[Span]:
    out: List[Span] = []
    for d in items:
        out.append(
            Span(
                id=d["id"],
                duration=float(d["duration"]),
                owner=d["owner"],
                deps=tuple(d.get("deps") or ()),  # tolerate absent, null, or empty deps
                kind=d.get("kind", ""),
            )
        )
    return out


def spans_to_dicts(spans: Sequence[Span]) -> list:
    return [
        {"id": s.id, "duration": s.duration, "owner": s.owner,
         "deps": list(s.deps), "kind": s.kind}
        for s in spans
    ]


def load_trace(path: str) -> List[Span]:
    with open(path) as f:
        return spans_from_dicts(json.load(f))


def dump_trace(spans: Sequence[Span], path: str) -> None:
    with open(path, "w") as f:
        json.dump(spans_to_dicts(spans), f, indent=2)
