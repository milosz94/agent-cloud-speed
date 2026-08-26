"""redu CapabilityAdapter (C13): the thin per-provider boundary for the capability pass. Its ONLY job
is to RESOLVE an existing deployment/instance's SSH endpoint into a provider-independent SSHHandle. The
credential is the harness-held key (default ~/.ssh/acspeed-cap, the keypair redu auto-selects for
deploys), never obtained from the provider. Everything above this (the probe battery, normalization,
DCI) is provider-agnostic core code.
"""
from __future__ import annotations

import os
from typing import Optional

from ..capability_probe import CapabilityAdapter, SSHHandle, parse_ssh_command
from . import redu_mcp_http

DEFAULT_KEYPAIR = "acspeed-cap"
DEFAULT_KEY_PATH = os.path.expanduser("~/.ssh/acspeed-cap")


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
