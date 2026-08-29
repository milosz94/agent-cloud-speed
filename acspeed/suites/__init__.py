"""Tier instances: concrete app scenarios that instantiate a tier's operation graph (see
``acspeed/suite.py`` for the engine and the design contract).

An instance is a ``TierInstance`` built by a factory here. The registry maps a name to a factory so
the runner (and tests) can look one up by ``--suite <name>``. The engine is app-independent; only the
instances know an app's endpoints, and only through the verify predicates the RUNNER owns.
"""
from __future__ import annotations

from typing import Callable, Dict

from ..suite import TierInstance
from . import umami_medium
from . import umami_hard

# name -> factory(**kwargs) -> TierInstance
_REGISTRY: Dict[str, Callable[..., TierInstance]] = {
    "umami-medium": umami_medium.build,
    "umami-hard": umami_hard.build,
}


def get_instance(name: str, **kwargs) -> TierInstance:
    """Build a tier instance by registry name. Extra kwargs are passed to the factory (e.g. a
    per-run ``probe`` sentinel)."""
    if name not in _REGISTRY:
        raise KeyError(f"unknown suite instance {name!r}; known: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


def instance_names() -> list:
    return sorted(_REGISTRY)
