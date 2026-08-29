"""Azure delivered-capability (C) SSH-endpoint resolution.

Capability C is measured SSH-in-situ on the deployment's own VM (same probe battery as redu/aws/gcp). On
Azure this is possible ONLY when the agent deployed to a shell-reachable Virtual Machine, whose app is
served on the public IP or its ``*.cloudapp.azure.com`` DNS label. Container Apps
(``*.azurecontainerapps.io``), App Service (``*.azurewebsites.net``), and Container Instances
(``*.azurecontainer.io``) are serverless/managed: no persistent instance shell (ACI's ``az container exec``
is a container exec, not an instance shell), so C is disclosed N/A there, exactly as AWS App Runner is.

This module only RESOLVES the endpoint + the user/key candidates; the actual probe reuses
acspeed.capability_probe. Azure's admin-username convention is ``azureuser`` (reserved names like root/admin
are rejected at create); the caller tries the candidates until one authenticates.
"""
from __future__ import annotations

import ipaddress
import os
import re
from typing import List, Optional, Tuple
from urllib.parse import urlparse

# A VM app is served on the public IP or its cloudapp DNS label; the managed suffixes are serverless and
# never match here.
_VM_DNS_RE = re.compile(r"\.cloudapp\.azure\.com$", re.I)

# Azure's create-time admin-username convention is azureuser; a few fallbacks for hand-set names.
AZURE_SSH_USERS: List[str] = ["azureuser", "ubuntu", "azadmin", "adminuser"]


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def resolve_azure_ssh_endpoint(url: str) -> Optional[Tuple[str, int]]:
    """(host, 22) when the served URL is an Azure VM public IP or its ``*.cloudapp.azure.com`` DNS label we
    can SSH to; None otherwise (Container Apps / App Service / Container Instances -> no reachable instance
    shell -> capability disclosed N/A)."""
    host = urlparse(url if "://" in (url or "") else f"https://{url}").hostname
    if not host:
        return None
    if _is_ip(host) or _VM_DNS_RE.search(host):
        return host, 22
    return None


def azure_key_candidates(ssh_dir: str = "~/.ssh") -> List[str]:
    """Private-key files the harness recovered from the run's microVM. Azure keypair names are not known in
    advance (``az vm create --generate-ssh-keys`` or a hand-named key), so we offer EVERY private key in
    ~/.ssh and let the probe try them until one authenticates, like aws/gcp."""
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
