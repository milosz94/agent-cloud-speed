"""A tiny curl-backed HTTP helper for tier-instance verify predicates.

The suite uses ``curl`` via subprocess exactly as the off-clock success oracle does
(``autorun.verify_served_content``), rather than adding a ``requests`` dependency: the harness runs
in a variety of environments (host and microVM) and ``curl`` is the one client guaranteed present.
Verify predicates are the observe primitive; they must never raise into the timed operation, so every
call here returns a structured result and swallows transport errors."""
from __future__ import annotations

import json
import subprocess
from typing import Optional, Tuple


def http(
    method: str,
    url: str,
    *,
    body: Optional[dict] = None,
    bearer: Optional[str] = None,
    headers: Optional[dict] = None,
    timeout_s: int = 20,
) -> Tuple[Optional[int], str, Optional[object]]:
    """Make one HTTP request with curl. Returns ``(status_code, body_text, parsed_json_or_none)``.
    ``status_code`` is None on a transport failure. Never raises."""
    cmd = [
        "curl", "-sS", "-L", "-X", method.upper(), "-m", str(timeout_s),
        "-w", "\n%{http_code}", url,
    ]
    hdrs = dict(headers or {})
    # A real browser-ish User-Agent: some app ingest endpoints (umami /api/send) reject a blank UA.
    hdrs.setdefault("User-Agent", "acspeed-suite/1.0")
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
        cmd += ["--data", json.dumps(body)]
    if bearer:
        hdrs["Authorization"] = f"Bearer {bearer}"
    for k, v in hdrs.items():
        cmd += ["-H", f"{k}: {v}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 5)
    except Exception:  # noqa: BLE001 - a verify predicate must never raise into the operation
        return None, "", None
    out = r.stdout
    if "\n" in out:
        text, code_s = out.rsplit("\n", 1)
    else:
        text, code_s = out, ""
    code: Optional[int]
    try:
        code = int(code_s.strip())
    except ValueError:
        code = None
    parsed: Optional[object]
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    return code, text, parsed
