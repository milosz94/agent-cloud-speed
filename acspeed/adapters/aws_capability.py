"""AWS delivered-capability (C) SSH-endpoint resolution.

Capability C is measured SSH-in-situ on the deployment's own VM (same probe battery as redu). On AWS this
is possible ONLY when the agent deployed to a shell-reachable VM whose public DNS the served URL exposes,
i.e. a raw EC2 instance (or an EC2 behind nothing). A Lightsail CONTAINER service, App Runner, or an EC2
sitting behind an ALB/CloudFront exposes an app front, not the instance's SSH host, so C is disclosed N/A
there (the substrate-scope rule: a substrate with no reachable shell yields no C, never a faked one).

This module only RESOLVES the endpoint + the user/key candidates; the actual probe reuses
acspeed.capability_probe. The SSH user is AMI-dependent (ec2-user on Amazon Linux, ubuntu on Ubuntu, ...),
so the caller tries the candidates in order until one authenticates, exactly as redu tries key candidates.
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple
from urllib.parse import urlparse

# EC2 public DNS: ec2-<ip>.compute-1.amazonaws.com (us-east-1) or <..>.<region>.compute.amazonaws.com
_EC2_DNS_RE = re.compile(r"\.compute(?:-1)?\.amazonaws\.com$", re.I)

# AMI-dependent default SSH users, most common first; the caller tries each until one works.
AWS_SSH_USERS: List[str] = ["ec2-user", "ubuntu", "admin", "centos", "debian", "root"]


def resolve_aws_ssh_endpoint(url: str) -> Optional[Tuple[str, int]]:
    """(host, 22) when the served URL is a raw EC2 public DNS we can SSH to; None otherwise (Lightsail
    container / App Runner / behind an ALB or CloudFront -> no reachable shell -> capability disclosed N/A)."""
    host = urlparse(url if "://" in (url or "") else f"https://{url}").hostname
    if host and _EC2_DNS_RE.search(host):
        return host, 22
    return None


def aws_key_candidates(ssh_dir: str = "~/.ssh") -> List[str]:
    """Private-key files the harness recovered from the run's microVM. AWS keypair names are not known in
    advance (the agent may `aws ec2 create-key-pair` into an arbitrarily named .pem), so unlike redu's
    ``redu-*`` filter we offer EVERY private key in ~/.ssh and let the probe try them until one authenticates."""
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
