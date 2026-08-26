"""Part 4 cost input: the standing hourly RUN-RATE of the provisioned bundle (CR C19).

The cost axis is the ONGOING hourly run-rate (per-hour, hence the monthly bill), NOT the cost accumulated
during a short test and NOT the one-time provisioning cost: cost-to-complete (price/hr x running time)
would fold in the wall-clock this framework already scores separately and double-count it. It sums every
ALWAYS-ON rate component (compute + block storage + public IP + ancillary), each unit-converted to
dollars per hour, from the provider's PUBLIC ON-DEMAND LIST price. Usage-metered EGRESS is held SEPARATE
(never summed into the flat baseline; it may or may not occur, has no native hourly unit, and its markup
would distort an otherwise-comparable rate). Account-specific discounts / spot / reserved / free-tier are
DELIBERATELY EXCLUDED (each user's own off-chart adjustment). Prices are DATED to the test day.

Grounded: all-in composition from separately-tabled list line items follows Kondo et al. (IPDPS 2009);
the decomposed billing units (compute/storage/network) follow Sochat & Milroy (2025); egress-as-a-separate
line follows Armbrust (CACM 2010); the run-rate-not-cost-to-complete choice mirrors SPECpower's ongoing
watt denominator. No cross-cloud cost normalization is applied: an absolute $/hr is already a common
currency unit, and comparability comes from pricing the SAME task on each cloud and the (wall-clock, $/hr)
Pareto frontier, never from dividing cost by a per-provider baseline.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

HOURS_PER_MONTH = 730.0   # the paper's month convention (storage $/GB-month / 730 -> $/GB-hour)

# Stated by default on every RunRate (the objective public baseline; users apply their own discounts
# off-chart for a strictly better result). Egress is listed here AND carried in its own field.
DEFAULT_EXCLUSIONS = (
    "account-specific discounts",
    "spot",
    "reserved / committed-use",
    "free-tier / credits",
    "usage-metered egress (reported separately, not in the baseline)",
)


@dataclass(frozen=True)
class RateComponent:
    """One always-on rate line, already unit-converted to dollars per hour. ``raw_unit_price`` +
    ``native_unit`` disclose the provider's list figure before conversion (e.g. 0.10 per GB-month)."""
    name: str                 # "compute" | "storage" | "public_ip" | "<ancillary>"
    hourly_usd: float         # the component's contribution to the standing rate, in $/hour
    raw_unit_price: float     # the provider's public list price in its native unit
    native_unit: str          # "instance-hour" | "GB-month" | "hour" | ...
    quantity: float = 1.0     # e.g. provisioned GB for storage; 1 for a per-hour line

    def __post_init__(self) -> None:
        if self.hourly_usd < 0:
            raise ValueError(f"component {self.name!r} has negative hourly_usd {self.hourly_usd}")


@dataclass(frozen=True)
class EgressRate:
    """Egress reported SEPARATELY (never in the all-in baseline): a dated per-GB rate with its pricing
    tier named, because egress is non-linear (a single $/GB is meaningless without its tier/volume)."""
    per_gb_usd: float
    tier: str                 # the named tier / volume band the rate applies to
    note: str = ""


@dataclass(frozen=True)
class RunRate:
    """The provisioned bundle's standing hourly run-rate (CR C19). ``all_in_hourly_usd`` is the sum of
    the always-on components ONLY; ``egress`` is carried separately and is never folded in."""
    all_in_hourly_usd: float
    components: Tuple[RateComponent, ...]
    provider: str
    region: str
    flavor: str
    capture_date: str                       # dated to the test day, e.g. "2026-08-26"
    egress: Optional[EgressRate] = None      # SEPARATE per-GB rate, not part of all_in_hourly_usd
    exclusions: Tuple[str, ...] = DEFAULT_EXCLUSIONS
    price_source: str = "public on-demand list"
    price_urls: Tuple[str, ...] = ()         # dated pricing-page sources, for the disclosed artifact

    def monthly_usd(self) -> float:
        """The standing monthly bill = the hourly run-rate x 730 (the paper's month convention)."""
        return self.all_in_hourly_usd * HOURS_PER_MONTH

    def to_dict(self) -> dict:
        return {
            "all_in_hourly_usd": self.all_in_hourly_usd,
            "monthly_usd": round(self.monthly_usd(), 4),
            "components": [
                {"name": c.name, "hourly_usd": c.hourly_usd, "raw_unit_price": c.raw_unit_price,
                 "native_unit": c.native_unit, "quantity": c.quantity} for c in self.components
            ],
            "egress": (None if self.egress is None else
                       {"per_gb_usd": self.egress.per_gb_usd, "tier": self.egress.tier,
                        "note": self.egress.note, "in_baseline": False}),
            "provider": self.provider, "region": self.region, "flavor": self.flavor,
            "capture_date": self.capture_date, "price_source": self.price_source,
            "price_urls": list(self.price_urls), "exclusions": list(self.exclusions),
        }


# --- unit conversion (every flat component must reach $/hour before summing) -------------------

def storage_gb_month_to_hourly(price_per_gb_month: float, gb: float) -> float:
    """Block storage list price (per GB-month) -> dollars per hour for ``gb`` provisioned GB. Storage is
    billed on PROVISIONED capacity, not used, so ``gb`` is the volume size."""
    if price_per_gb_month < 0 or gb < 0:
        raise ValueError("storage price and GB must be non-negative")
    return price_per_gb_month * gb / HOURS_PER_MONTH


def compose_run_rate(components: Sequence[RateComponent], *, provider: str, region: str, flavor: str,
                     capture_date: str, egress: Optional[EgressRate] = None,
                     exclusions: Tuple[str, ...] = DEFAULT_EXCLUSIONS,
                     price_source: str = "public on-demand list",
                     price_urls: Tuple[str, ...] = ()) -> RunRate:
    """Compose the all-in standing hourly run-rate = sum of the always-on components' $/hour. Egress is
    passed through as a SEPARATE field and is deliberately NOT added to the sum (C19)."""
    all_in = sum(c.hourly_usd for c in components)
    return RunRate(
        all_in_hourly_usd=round(all_in, 6),
        components=tuple(components),
        provider=provider, region=region, flavor=flavor, capture_date=capture_date,
        egress=egress, exclusions=tuple(exclusions), price_source=price_source,
        price_urls=tuple(price_urls),
    )


# --- the thin per-provider boundary (mirrors CapabilityAdapter / C13) --------------------------

class RunRateAdapter(abc.ABC):
    """Per-cloud boundary for the cost axis. Its only job is to resolve the deployment's provisioned
    resources and their PUBLIC LIST prices into a provider-independent ``RunRate``. Everything above it
    (composition, the Part-4 frontier) is provider-agnostic. A cloud with no public price API falls back
    to the operation's agent computing the run-rate off-clock (disclosed)."""

    @abc.abstractmethod
    def run_rate(self, deployment_ref: object) -> Optional[RunRate]:
        """Return the deployment's standing hourly RunRate, or None if it cannot be priced (disclosed,
        never faked)."""
        raise NotImplementedError
