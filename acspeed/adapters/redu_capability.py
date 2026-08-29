"""redu CapabilityAdapter (C13): the thin per-provider boundary for the capability pass. Its ONLY job
is to RESOLVE an existing deployment/instance's SSH endpoint into a provider-independent SSHHandle. The
credential is the harness-held key (default ~/.ssh/acspeed-cap, the keypair redu auto-selects for
deploys), never obtained from the provider. Everything above this (the probe battery, normalization,
DCI) is provider-agnostic core code.
"""
from __future__ import annotations

import os
from typing import Optional

from ..capability_probe import CapabilityAdapter, NetworkPair, SSHHandle, parse_ssh_command
from . import redu_mcp_http

DEFAULT_KEYPAIR = "acspeed-cap"
DEFAULT_KEY_PATH = os.path.expanduser("~/.ssh/acspeed-cap")


def _first_private_ip(row: dict):
    """Best-effort private-IP extraction from a managed-datastore / instance row (field name varies by
    cloud row; disclosed live-hardening seam confirmed on the first multi-VM run)."""
    for k in ("private_ip", "private_address", "internal_ip", "ip", "address"):
        v = row.get(k)
        if isinstance(v, str) and v:
            return v
    mem = row.get("member_ips")
    if isinstance(mem, list) and mem:
        return mem[0]
    return None


def resolve_network_pair(deployment_ref, client_handle, *, call_tool=None) -> Optional[NetworkPair]:
    """C17 VM-to-VM pair. A redu deployment's managed datastore VM shares the deploy's keypair ("every VM
    is created with the same keypair") and sits on the PRIVATE network with NO floating IP, so it is NOT a
    black box: reach it by ProxyJump THROUGH the app VM (which carries the floating IP), to the datastore's
    private_ip, using the SAME key that opens the app VM. iperf3/ping then run app-VM -> datastore over the
    private network directly. Returns None only when the deploy genuinely has no second VM (true single-VM).
    Best-effort + non-fatal; the private-IP field name + reachability confirm on the first multi-VM run."""
    ct = call_tool or redu_mcp_http.call_tool
    try:
        det = ct("get_deployment", {"id": int(deployment_ref)}) if str(deployment_ref).isdigit() else {}
    except Exception:  # noqa: BLE001
        det = {}
    dep = (det.get("deployment") if isinstance(det, dict) and isinstance(det.get("deployment"), dict)
           else det if isinstance(det, dict) else {})
    for id_key, list_tool, coll in (("db_id", "list_databases", "databases"),
                                    ("redis_id", "list_redis", "redis")):
        rid = dep.get(id_key) or dep.get("database_id")
        if not rid:
            continue
        try:
            rows = (ct(list_tool, {}) or {}).get(coll, [])
        except Exception:  # noqa: BLE001
            rows = []
        row = next((r for r in rows if str(r.get("id")) == str(rid)), None)
        if not row:
            continue
        pip = _first_private_ip(row)
        if not pip:
            continue
        # ProxyJump through the app VM (floating IP) to the datastore's private IP, SAME keypair.
        jump = f"{client_handle.user}@{client_handle.host}:{client_handle.port}"
        server = SSHHandle(host=pip, user=client_handle.user, private_key_path=client_handle.private_key_path,
                           port=22, proxy_jump=jump)
        return NetworkPair(client=client_handle, server=server, server_private_ip=pip, co_residency=None)
    return None


class ReduCapabilityAdapter(CapabilityAdapter):
    def __init__(self, keypair_name: Optional[str] = None, private_key_path: str = DEFAULT_KEY_PATH,
                 get_ssh_command=None):
        # keypair_name=None: let get_ssh_command resolve the deployment's OWN keypair (which the deploy
        # may have minted per-app, e.g. redu-<app>-deploy; its private key is recovered from the microVM
        # into ~/.ssh so the inline `-i` resolves). private_key_path is only the fallback when the
        # resolved ssh command carries no `-i`.
        self.keypair_name = keypair_name
        self.private_key_path = private_key_path
        # injectable for tests; defaults to the real host-side HTTP MCP resolver
        self._get_ssh_command = get_ssh_command or redu_mcp_http.get_ssh_command

    def ssh_handle(self, deployment_ref: object) -> Optional[SSHHandle]:
        cmd = self._get_ssh_command(str(deployment_ref), keypair_name=self.keypair_name)
        if not cmd:
            return None
        return parse_ssh_command(cmd, private_key_path=self.private_key_path)


def resolve_deployment_id_by_url(url: str) -> Optional[str]:
    """Find the deployment whose access_point matches `url`, return its id (for get_ssh_command)."""
    out = redu_mcp_http.call_tool("list_deployments", {})
    host = url.split("//", 1)[-1].split("/", 1)[0]
    for d in out.get("deployments", []):
        ap = (d.get("access_point") or "") + (d.get("dname") or "")
        if host and host in ap:
            return str(d.get("id") or d.get("instance_id") or d.get("name"))
    return None
