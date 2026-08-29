"""Minimal host-side client for redu's Streamable-HTTP MCP, used ONLY to RESOLVE endpoints (C12/C13:
a provider API call may resolve host/port/user; it never obtains the shell -- that is SSH + our key).

Reads the redu OAuth token from ~/.claude/.credentials.json (the same token scoped into the microVM).
The access token is short-lived and a long deploy can outlive it, so this client REFRESHES it via the
OAuth (OIDC) refresh-token grant when it is expired or a call returns 401, and writes the new token back so
the interactive session and later calls stay valid. No third-party deps (urllib only).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

MCP_URL = "https://mcp.redu.cloud/mcp"
CRED_PATH = os.path.expanduser("~/.claude/.credentials.json")


def _load_oauth():
    d = json.load(open(CRED_PATH))
    mo = d.get("mcpOAuth") or {}
    for k, v in mo.items():
        if isinstance(v, dict) and v.get("serverName") == "redu":
            return d, k, v
    raise RuntimeError("no redu OAuth entry in ~/.claude/.credentials.json")


def _write_oauth(d: dict, key: str, entry: dict) -> None:
    # RE-READ the file immediately before writing and update ONLY our own mcpOAuth entry. Writing back a
    # whole dict captured earlier would clobber claudeAiOauth (the Claude login) or another MCP server's
    # token that Claude Code / another tool rotated since we last read - and because OAuth refresh tokens
    # are single-use, restoring a stale claudeAiOauth silently invalidates the login and forces a re-auth.
    # We own only mcpOAuth[key]; everything else on disk wins.
    try:
        with open(CRED_PATH) as fh:
            cur = json.load(fh)
        if not isinstance(cur, dict):
            cur = d
    except (OSError, ValueError):
        cur = d
    cur.setdefault("mcpOAuth", {})[key] = entry
    tmp = CRED_PATH + ".acspeed.tmp"
    with open(tmp, "w") as fh:
        json.dump(cur, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CRED_PATH)                              # atomic


def _refresh(entry: dict) -> dict:
    """OIDC refresh_token grant -> a fresh access token. Returns the updated entry (unwritten)."""
    issuer = entry["issuer"].rstrip("/")
    form = urllib.parse.urlencode({"grant_type": "refresh_token",
                                   "refresh_token": entry["refreshToken"],
                                   "client_id": entry["clientId"]}).encode()
    req = urllib.request.Request(f"{issuer}/protocol/openid-connect/token", data=form,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"},
                                 method="POST")
    tok = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    e = dict(entry)
    e["accessToken"] = tok["access_token"]
    if tok.get("refresh_token"):
        e["refreshToken"] = tok["refresh_token"]           # persist a rotated refresh token
    e["expiresAt"] = int(time.time() * 1000) + int(tok.get("expires_in", 300)) * 1000
    return e


def _valid_token(force_refresh: bool = False) -> str:
    d, key, entry = _load_oauth()
    now = int(time.time() * 1000)
    if force_refresh or entry.get("expiresAt", 0) < now + 60_000:   # expired, or within 60s
        entry = _refresh(entry)
        _write_oauth(d, key, entry)
    return entry["accessToken"]


def _post(payload: dict, token: str, session: Optional[str], timeout: int = 30):
    body = json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(MCP_URL, data=body, headers=headers, method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout)
    sid = resp.headers.get("Mcp-Session-Id") or session
    raw = resp.read().decode()
    obj = None
    if "text/event-stream" in resp.headers.get("Content-Type", ""):    # SSE: last "data: {json}" line
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                except ValueError:
                    pass
    elif raw.strip():
        obj = json.loads(raw)
    return obj, sid


def _call_once(name: str, arguments: dict, token: str) -> dict:
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "acspeed", "version": "0"}}}
    _, sid = _post(init, token, None)
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token, sid)
    obj, _ = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}}, token, sid)
    if not obj or "result" not in obj:
        raise RuntimeError(f"MCP call {name} failed: {json.dumps(obj)[:300] if obj else 'no response'}")
    res = obj["result"]
    if isinstance(res.get("structuredContent"), dict):
        return res["structuredContent"]
    for block in res.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except ValueError:
                return {"text": block["text"]}
    return res


def call_tool(name: str, arguments: dict, *, token: Optional[str] = None) -> dict:
    """Initialize an MCP session and call one tool; return the parsed result. Refreshes the OAuth token
    proactively (on expiry) and reactively (once, on a 401), and retries a few times with backoff on a
    TRANSIENT server/gateway blip (502/503/504/500/429) or a network error, so one hiccup does not lose a
    whole off-clock cost pass (measured: a 502 Bad Gateway during pricing zeroed a run's cost)."""
    saw_401 = False
    for attempt in range(5):
        tok = token or _valid_token(force_refresh=saw_401)
        try:
            return _call_once(name, arguments, tok)
        except urllib.error.HTTPError as e:
            if e.code == 401 and token is None and not saw_401:
                saw_401 = True                             # token went stale mid-flight: refresh + retry
                continue
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(1.5 * (attempt + 1))            # transient gateway/server blip: backoff + retry
                continue
            raise
        except urllib.error.URLError:                      # transient network (conn reset / timeout)
            if attempt < 4:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"call_tool({name!r}) exhausted retries")


def get_ssh_command(deployment_ref: str, keypair_name: Optional[str] = None) -> Optional[str]:
    """Resolve a deployment/instance's SSH command string (endpoint only)."""
    args = {"instance_id": deployment_ref}
    if keypair_name:
        args["keypair_name"] = keypair_name
    return call_tool("get_ssh_command", args).get("ssh_command")


if __name__ == "__main__":
    import sys
    tool = sys.argv[1] if len(sys.argv) > 1 else "whoami"
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(call_tool(tool, args), indent=2)[:1500])
