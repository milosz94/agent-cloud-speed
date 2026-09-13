"""Load a benchmark from a JSON file, so adding operations is not a code change.

The built-in suites are Python because their postconditions need real logic (log into umami, resolve
a website id, read a pageview metric). Most benchmarks need nothing of the sort: deploy it, do a
thing, then read a URL back and look for a string. That case should not require writing a module,
so ``--custom my-benchmark.json`` builds the same ``TierInstance`` the Python suites build.

The verify is declarative on purpose. Its whole job is the observe primitive: the RUNNER reads the
deployed system itself and never trusts what the agent reported. A JSON verify cannot accidentally
ask the agent, which is the mistake most worth making impossible.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from ..operation import OPERATION_TYPES
from ..suite import DurabilityGoal, OpContext, TierInstance, TierOperation, VerifyResult
from ._http import http

_VERIFY_KEYS = {"method", "path", "status", "status_below", "body_contains", "body_matches",
                "json_contains", "bearer", "headers", "timeout_s"}
_OP_KEYS = {"op_id", "op_type", "task", "verify", "depends_on", "durable", "max_attempts",
            "fresh_session"}
_TOP_KEYS = {"name", "tier", "operations", "teardown_hint", "durability", "plan_upfront"}


class CustomSuiteError(ValueError):
    """A problem in the JSON, reported before a run starts rather than after money is spent."""


def _fail(where: str, msg: str) -> None:
    raise CustomSuiteError(f"{where}: {msg}")


def _unknown(where: str, got: Dict[str, Any], allowed: set) -> None:
    extra = sorted(set(got) - allowed)
    if extra:
        _fail(where, f"unknown key(s) {extra}; allowed: {sorted(allowed)}")


def _join(base: Optional[str], path: str) -> Optional[str]:
    if not base:
        return None
    if path.startswith(("http://", "https://")):
        return path
    return base.rstrip("/") + "/" + path.lstrip("/")


def _json_contains(parsed: Any, want: Dict[str, Any]) -> bool:
    """True if every wanted key/value appears in the parsed body, at the top level of an object or
    of any element of a list. Deliberately shallow: a deep query language here would be a worse
    version of writing a Python verify."""
    def one(obj: Any) -> bool:
        return isinstance(obj, dict) and all(obj.get(k) == v for k, v in want.items())
    if one(parsed):
        return True
    return isinstance(parsed, list) and any(one(el) for el in parsed)


def _build_verify(spec: Dict[str, Any], where: str):
    if not isinstance(spec, dict):
        _fail(where, "verify must be an object")
    _unknown(where, spec, _VERIFY_KEYS)
    if "path" not in spec:
        _fail(where, 'verify needs a "path" (use "/" for the site root)')

    method = str(spec.get("method", "GET")).upper()
    path = str(spec["path"])
    exact = spec.get("status")
    below = spec.get("status_below", 500)
    contains = spec.get("body_contains")
    if isinstance(contains, str):
        contains = [contains]
    if contains is not None and not (isinstance(contains, list)
                                     and all(isinstance(s, str) for s in contains)):
        _fail(where, '"body_contains" must be a string or a list of strings')
    pattern = spec.get("body_matches")
    if pattern is not None:
        try:
            pattern = re.compile(pattern)
        except re.error as e:
            _fail(where, f'"body_matches" is not a valid regex: {e}')
    jwant = spec.get("json_contains")
    if jwant is not None and not isinstance(jwant, dict):
        _fail(where, '"json_contains" must be an object')
    bearer = spec.get("bearer")
    headers = spec.get("headers")
    timeout_s = int(spec.get("timeout_s", 20))

    def verify(ctx: OpContext) -> VerifyResult:
        url = _join(ctx.url, path)
        if not url:
            return VerifyResult(False, "no serving URL to read", unverifiable=True)
        status, body, parsed = http(method, url, bearer=bearer, headers=headers,
                                    timeout_s=timeout_s)
        # curl reports a refused connection as 0, NOT None. Checking `status is None`, or checking
        # `status < 500` alone, passes a deployment that does not exist.
        if not status:
            return VerifyResult(False, f"{method} {url}: nothing answered")
        reasons: List[str] = []
        ok = True
        if exact is not None and status != exact:
            ok = False; reasons.append(f"status {status} != {exact}")
        if exact is None and status >= int(below):
            ok = False; reasons.append(f"status {status} >= {below}")
        for s in (contains or []):
            if s not in body:
                ok = False; reasons.append(f"body missing {s!r}")
        if pattern is not None and not pattern.search(body):
            ok = False; reasons.append(f"body does not match /{pattern.pattern}/")
        if jwant is not None and not _json_contains(parsed, jwant):
            ok = False; reasons.append(f"json does not contain {jwant}")
        detail = f"{method} {path} -> {status}" + (f"; {'; '.join(reasons)}" if reasons else "")
        return VerifyResult(ok, detail, measured={"status": status, "bytes": len(body)})

    return verify


def _build_operation(raw: Dict[str, Any], idx: int) -> TierOperation:
    where = f"operations[{idx}]"
    if not isinstance(raw, dict):
        _fail(where, "each operation must be an object")
    _unknown(where, raw, _OP_KEYS)
    for req in ("op_id", "op_type", "task", "verify"):
        if req not in raw:
            _fail(where, f'missing required key "{req}"')
    if raw["op_type"] not in OPERATION_TYPES:
        _fail(where, f'op_type {raw["op_type"]!r} must be one of {list(OPERATION_TYPES)}')
    if not str(raw["task"]).strip():
        _fail(where, '"task" is empty; it is the instruction handed to the agent')
    return TierOperation(
        op_id=str(raw["op_id"]),
        op_type=str(raw["op_type"]),
        task=str(raw["task"]),
        verify=_build_verify(raw["verify"], f"{where}.verify"),
        depends_on=tuple(raw.get("depends_on", ()) or ()),
        durable=bool(raw.get("durable", True)),
        max_attempts=int(raw.get("max_attempts", 1)),
        fresh_session=bool(raw.get("fresh_session", False)),
    )


def build_from_dict(doc: Dict[str, Any], source: str = "<dict>") -> TierInstance:
    if not isinstance(doc, dict):
        _fail(source, "the top level must be an object")
    _unknown(source, doc, _TOP_KEYS)
    for req in ("name", "operations"):
        if req not in doc:
            _fail(source, f'missing required key "{req}"')
    ops_raw = doc["operations"]
    if not isinstance(ops_raw, list) or not ops_raw:
        _fail(source, '"operations" must be a non-empty list')

    ops = [_build_operation(o, i) for i, o in enumerate(ops_raw)]
    seen: set = set()
    for op in ops:
        if op.op_id in seen:
            _fail(source, f"duplicate op_id {op.op_id!r}")
        seen.add(op.op_id)
    for op in ops:
        for dep in op.depends_on:
            if dep not in seen:
                _fail(source, f"{op.op_id!r} depends_on {dep!r}, which no operation defines")

    dur = None
    draw = doc.get("durability")
    if draw is not None:
        if not isinstance(draw, dict) or "task" not in draw:
            _fail(source, '"durability" must be an object with a "task"')
        dur = DurabilityGoal(task=str(draw["task"]),
                             op_id=str(draw.get("op_id", "restart-durability")),
                             max_iters=int(draw.get("max_iters", 4)),
                             liveness=_build_verify(draw["liveness"], f"{source}.durability.liveness")
                             if draw.get("liveness") else None)

    return TierInstance(
        name=str(doc["name"]),
        tier=str(doc.get("tier", "custom")),
        operations=ops,
        teardown_hint=str(doc.get("teardown_hint", "")),
        durability=dur,
        plan_upfront=bool(doc.get("plan_upfront", False)),
    )


def load(path: str) -> TierInstance:
    """Build a TierInstance from a JSON file. Raises CustomSuiteError with a pointed message."""
    if not os.path.exists(path):
        raise CustomSuiteError(f"no such custom benchmark file: {path}")
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise CustomSuiteError(f"{path} is not valid JSON: {e}") from None
    return build_from_dict(doc, source=os.path.basename(path))
