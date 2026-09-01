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

def _umami_medium_b(**kwargs) -> TierInstance:
    """Medium B: the SAME umami Medium workload, DISCLOSED regime (full plan up front). Same operations,
    verifies, durability and gold as Medium A; only plan_upfront differs, so a Medium A run and a Medium B
    run on the same cloud/goal give the paired value-of-plan-lookahead gap M_online - M_disclosed."""
    return umami_medium.build(plan_upfront=True, **kwargs)


# name -> factory(**kwargs) -> TierInstance
_REGISTRY: Dict[str, Callable[..., TierInstance]] = {
    "umami-medium": umami_medium.build,
    "umami-medium-b": _umami_medium_b,
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
