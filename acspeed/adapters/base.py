"""Cloud adapter contract.

An adapter drives canonical cloud operations through a cloud's MCP server and
emits a timed, owner-labelled trace that the analysis core (criticalpath,
agenttime) consumes. The interface is MCP: to measure a cloud you point the
adapter at that cloud's MCP server (redu, AWS, GCP, Azure). The analysis is
identical across clouds; only the per-cloud profile (tool-name map) differs.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List

from ..types import Span

# Canonical operations; each cloud profile maps these to its own MCP tool names.
PROVISION = "provision"
WAIT_READY = "wait_ready"
STATUS = "status"
TEARDOWN = "teardown"


@dataclass
class OperationSpec:
    """A cloud-agnostic operation request (e.g. an instance to create).

    ``params`` is forwarded as the MCP tool ``arguments``.
    """

    kind: str = "instance"
    params: dict = field(default_factory=dict)


class CloudAdapter(ABC):
    """Executes a canonical operation and returns its trace as a list of Spans."""

    name: str

    @abstractmethod
    def run(self, spec: OperationSpec) -> List[Span]:
        ...
