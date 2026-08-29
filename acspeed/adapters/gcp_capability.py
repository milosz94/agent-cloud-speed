"""GCP delivered-capability (C) SSH-endpoint resolution.

Capability C is measured SSH-in-situ on the deployment's own VM (same probe battery as redu/aws). On GCP
this is possible ONLY when the agent deployed to a shell-reachable Compute Engine VM, whose app is served
on the instance's raw external IP. Cloud Run (``*.run.app``) and App Engine (``*.appspot.com``) are
serverless: no reachable shell, so C is disclosed N/A there (the substrate-scope rule: a substrate with no
reachable shell yields no C, never a faked one), exactly as AWS App Runner is handled.

This module only RESOLVES the endpoint + the user/key candidates; the actual probe reuses
acspeed.capability_probe. The SSH user for a GCE VM is whatever the injected metadata key names (the
harness controls it at create time via ``--metadata-from-file ssh-keys=USER:...``); the caller tries the
candidates in order until one authenticates, exactly as aws_capability does.
"""
from __future__ import annotations

import ipaddress
import os
import re
from typing import List, Optional, Tuple
from urllib.parse import urlparse

# A GCE app is served on the instance's raw external IP; the reverse-DNS PTR (bc.googleusercontent.com) is
# a weak fallback and flips after live-migration, so we key on a bare IP host (the poller latches the
# create-time IP). Cloud Run / App Engine hostnames are serverless and never match here.
_GCE_PTR_RE = re.compile(r"\.bc\.googleusercontent\.com$", re.I)

# GCE default SSH users are metadata-key driven (no fixed convention like AWS's ec2-user). The harness
# injects a key for one of these; the caller tries each until one works.
GCP_SSH_USERS: List[str] = ["ubuntu", "debian", "gce", "admin", os.environ.get("USER", "user")]


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def resolve_gcp_ssh_endpoint(url: str) -> Optional[Tuple[str, int]]:
    """(host, 22) when the served URL is a raw Compute Engine external IP (or its reverse-DNS PTR) we can
    SSH to; None otherwise (Cloud Run / App Engine / any serverless surface -> no reachable shell ->
    capability disclosed N/A)."""
    host = urlparse(url if "://" in (url or "") else f"https://{url}").hostname
    if not host:
        return None
    if _is_ip(host) or _GCE_PTR_RE.search(host):
        return host, 22
    return None


def gcp_key_candidates(ssh_dir: str = "~/.ssh") -> List[str]:
    """Private-key files the harness recovered from the run's microVM. GCE keypair names are not known in
    advance (the agent / gcloud may create an arbitrarily named key), so we offer EVERY private key in
    ~/.ssh and let the probe try them until one authenticates, exactly like aws_key_candidates."""
    d = os.path.expanduser(ssh_dir)
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if name.endswith(".pub") or name in ("known_hosts", "authorized_keys", "config") or not os.path.isfile(p):
            continue
        out.append(p)
    return out
