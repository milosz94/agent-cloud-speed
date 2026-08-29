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
Pareto frontier, never from dividing cost by a per-provider baseline. USD is the single reporting currency;
a provider that lists in another currency (redu prices in GBP) is converted to USD ONCE at a dated PUBLIC
FX rate (``FxConversion``, disclosed), which is a units conversion applied identically to all of that
provider's lines, NOT a per-provider baseline division, so the common-unit argument still holds.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

HOURS_PER_MONTH = 730.0   # the paper's month convention (storage $/GB-month / 730 -> $/GB-hour)

# Standard human-facing cost estimate: the TOTAL monthly bill at a small, a growing, and a busy request
# volume, so a reader knows what to expect at their scale without reading a schedule. The three volumes
# are grid points in REFERENCE_REQUEST_GRID below, so a usage schedule already prices them exactly; a
# standing (fixed-resource) deploy is flat across them because a VM has no separate per-request charge.
TRAFFIC_TIERS = (("low", 10_000), ("medium", 500_000), ("high", 10_000_000))  # name -> requests/month

# Disclosed assumption for folding a usage-metered service's ACTIVE compute (driver='other', priced per
# vCPU-second / GiB-second) into a per-request cost. The served URL reveals neither per-request duration
# nor the CPU/memory allocation, so a fixed, stated request profile is used; it scales the serverless
# usage estimate linearly, so a study can change it. Without this fold, a serverless estimate would omit
# the dominant variable cost (compute time) and understate the bill -- a fixed-floor comparison would be
# invalid. Fixed-resource (RunRate) deploys are unaffected: they have no per-request charge.
DEFAULT_REQUEST_SECONDS = 0.1   # avg wall-time a request occupies the container
DEFAULT_REQUEST_VCPUS = 1.0     # assumed vCPU allocation while serving one request
DEFAULT_REQUEST_GIB = 0.5       # assumed memory allocation (GiB) while serving one request

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
class FxConversion:
    """Disclosed dated FX conversion, present when a provider lists in a non-USD currency (redu prices in
    GBP). USD stays the single reporting currency; the provider's native figures are converted at ONE
    dated PUBLIC rate applied identically to all of that provider's lines. This is a units conversion, not
    a per-provider baseline division, so an absolute $/hr remains the common cross-cloud unit; the rate,
    the date it applies to, and its public source are disclosed so the number stays reproducible (C19)."""
    native_currency: str          # the provider's listing currency, e.g. "GBP"
    reporting_currency: str       # always "USD" here
    rate: float                   # native -> USD multiplier (1 native currency unit = ``rate`` USD)
    rate_date: str                # the date the published rate applies to (may differ from capture_date)
    source: str                   # dated public source, e.g. "ECB via frankfurter.app"

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError(f"fx rate must be positive, got {self.rate}")


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
    fx: Optional[FxConversion] = None        # disclosed dated FX when the provider lists in non-USD

    def monthly_usd(self) -> float:
        """The standing monthly bill = the hourly run-rate x 730 (the paper's month convention)."""
        return self.all_in_hourly_usd * HOURS_PER_MONTH

    def traffic_estimate(self) -> dict:
        """Total $/mo at the standard low/medium/high request volumes. A fixed-resource deploy has no
        per-request charge, so the bill is the same standing figure at every tier (round to cents)."""
        m = round(self.monthly_usd(), 2)
        return {name: m for name, _ in TRAFFIC_TIERS}

    def to_dict(self) -> dict:
        return {
            "kind": "standing",
            "all_in_hourly_usd": self.all_in_hourly_usd,
            "monthly_usd": round(self.monthly_usd(), 4),
            "traffic_estimate": self.traffic_estimate(),
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
            "fx": (None if self.fx is None else
                   {"native_currency": self.fx.native_currency,
                    "reporting_currency": self.fx.reporting_currency,
                    "rate": self.fx.rate, "rate_date": self.fx.rate_date, "source": self.fx.source}),
        }


# --- usage-metered services: report a per-usage SCHEDULE, not a single number (C19 extension) -----
#
# A serverless / usage-metered surface (CloudFront, App Runner, Lambda, API Gateway, ...) has NO standing
# hourly bill: its cost is a function of usage (requests, GB egress, GB-seconds). A single $/hr would be
# meaningless or fabricated. The honest, comparable report is the price at a FIXED, DISCLOSED grid of
# usage levels -- "10k requests/mo -> $A, 100k -> $B, 1M -> $C" -- so it sits on the same (wall-clock,
# cost) frontier as a standing VM: two compute services serving the same app, each priced on its OWN
# basis, both disclosed. This is the same principle that already holds egress separate (usage has no
# native hourly unit); here it is generalized from egress to any usage-metered service. Grounding is the
# same as RunRate (Kondo 2009 line-item composition; Sochat & Milroy 2025 decomposed billing units;
# Armbrust 2010 usage-metered network); a per-usage price schedule is exactly how the providers list these
# services, so no basis is invented.

REFERENCE_REQUEST_GRID = (10_000, 50_000, 100_000, 500_000, 1_000_000, 10_000_000)  # requests / month
DEFAULT_AVG_RESPONSE_KB = 50.0   # disclosed: egress GB derived from request count x this average size


def _fmt_requests(n: float) -> str:
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:g}M"
    if n >= 1_000:
        return f"{n / 1_000:g}k"
    return f"{n:g}"


@dataclass(frozen=True)
class UsageComponent:
    """One usage-metered price line, already normalized to a per-single-unit USD rate. ``driver`` says how
    it scales so ``compose_usage_rate`` can fold it: 'requests' (per request), 'egress' (per GB out),
    'standing' (an always-on floor, per hour) or 'other' (e.g. per vCPU-second active compute, which
    depends on per-request duration and is reported as a rate but NOT folded into the monthly totals)."""
    name: str                 # "requests" | "egress" | "compute-provisioned" | "compute-active" | ...
    per_unit_usd: float       # normalized: $/request, $/GB, $/hour (standing), or $/native-unit (other)
    unit: str                 # human unit, e.g. "per request", "per GB", "GB-hour", "vCPU-second"
    driver: str               # "requests" | "egress" | "standing" | "other"
    raw_unit_price: float = 0.0   # the provider's list figure before normalization
    native_unit: str = ""         # e.g. "per 10k requests", "per GB (first tier)"
    tier: str = ""

    def __post_init__(self) -> None:
        if self.per_unit_usd < 0:
            raise ValueError(f"usage component {self.name!r} has negative per_unit_usd")
        if self.driver not in ("requests", "egress", "standing", "other"):
            raise ValueError(f"usage component {self.name!r} has unknown driver {self.driver!r}")


@dataclass(frozen=True)
class UsagePoint:
    """One row of the schedule: a monthly request volume and the resulting estimated monthly cost."""
    label: str
    requests_per_month: float
    egress_gb: float
    usd_per_month: float


@dataclass(frozen=True)
class UsageRate:
    """A usage-metered service's cost as a SCHEDULE over a fixed grid of usage levels, the per-usage
    counterpart of RunRate (C19). Reports the normalized per-unit rates AND the cost at each grid point,
    with every assumption disclosed."""
    provider: str
    region: str
    service: str
    capture_date: str
    components: Tuple[UsageComponent, ...]
    schedule: Tuple[UsagePoint, ...]
    assumptions: Tuple[str, ...] = ()
    price_source: str = "public on-demand list"
    exclusions: Tuple[str, ...] = DEFAULT_EXCLUSIONS
    price_urls: Tuple[str, ...] = ()
    fx: Optional[FxConversion] = None

    def monthly_at(self, requests_per_month: float) -> Optional[float]:
        for p in self.schedule:
            if p.requests_per_month == requests_per_month:
                return p.usd_per_month
        return None

    def traffic_estimate(self) -> dict:
        """Total $/mo at the standard low/medium/high request volumes, read off this rate's priced
        schedule (each tier is a grid point). A tier is None if it is absent from this rate's grid."""
        out = {}
        for name, reqs in TRAFFIC_TIERS:
            m = self.monthly_at(reqs)
            out[name] = None if m is None else round(m, 2)
        return out

    def to_dict(self) -> dict:
        return {
            "kind": "usage",
            "traffic_estimate": self.traffic_estimate(),
            "provider": self.provider, "region": self.region, "service": self.service,
            "capture_date": self.capture_date,
            "components": [
                {"name": c.name, "per_unit_usd": c.per_unit_usd, "unit": c.unit, "driver": c.driver,
                 "raw_unit_price": c.raw_unit_price, "native_unit": c.native_unit, "tier": c.tier}
                for c in self.components
            ],
            "schedule": [
                {"label": p.label, "requests_per_month": p.requests_per_month,
                 "egress_gb": p.egress_gb, "usd_per_month": p.usd_per_month} for p in self.schedule
            ],
            "assumptions": list(self.assumptions),
            "price_source": self.price_source, "price_urls": list(self.price_urls),
            "exclusions": list(self.exclusions),
            "fx": (None if self.fx is None else
                   {"native_currency": self.fx.native_currency,
                    "reporting_currency": self.fx.reporting_currency,
                    "rate": self.fx.rate, "rate_date": self.fx.rate_date, "source": self.fx.source}),
        }


def compose_usage_rate(components: Sequence[UsageComponent], *, provider: str, region: str, service: str,
                       capture_date: str, standing_floor_hourly_usd: float = 0.0,
                       request_grid: Sequence[int] = REFERENCE_REQUEST_GRID,
                       avg_response_kb: float = DEFAULT_AVG_RESPONSE_KB,
                       assume_request_seconds: float = DEFAULT_REQUEST_SECONDS,
                       assume_request_vcpus: float = DEFAULT_REQUEST_VCPUS,
                       assume_request_gib: float = DEFAULT_REQUEST_GIB,
                       price_source: str = "public on-demand list",
                       price_urls: Tuple[str, ...] = (), fx: Optional[FxConversion] = None) -> UsageRate:
    """Build the usage schedule: at each request level in the grid, monthly cost = the always-on floor
    (standing components + ``standing_floor_hourly_usd``) x 730 + per-request charges x requests + per-GB
    egress x the egress derived from the request count. 'other' components (per-second active compute) are
    FOLDED into the per-request charge using the disclosed request profile (vCPU + GiB held for
    ``assume_request_seconds`` per request); without that fold a serverless estimate omits its dominant
    variable cost and a floor-only comparison is invalid."""
    per_req = sum(c.per_unit_usd for c in components if c.driver == "requests")
    # fold active compute (driver 'other', priced per vCPU-second / GiB-second) into the per-request cost
    for c in components:
        if c.driver != "other":
            continue
        u = c.unit.lower()
        if "vcpu" in u:
            per_req += c.per_unit_usd * assume_request_vcpus * assume_request_seconds
        elif "gib" in u or "gb" in u:
            per_req += c.per_unit_usd * assume_request_gib * assume_request_seconds
    per_gb = sum(c.per_unit_usd for c in components if c.driver == "egress")
    floor_hourly = standing_floor_hourly_usd + sum(c.per_unit_usd for c in components if c.driver == "standing")
    floor_month = floor_hourly * HOURS_PER_MONTH
    schedule = []
    for r in request_grid:
        egress_gb = float(r) * avg_response_kb / (1024.0 * 1024.0)   # KB/req x reqs -> KB -> GB
        usd = floor_month + per_req * float(r) + per_gb * egress_gb
        schedule.append(UsagePoint(label=_fmt_requests(r), requests_per_month=float(r),
                                   egress_gb=round(egress_gb, 4), usd_per_month=round(usd, 4)))
    assumptions = (
        f"egress GB estimated as requests x {avg_response_kb:g} KB average response size",
        f"request grid (per month): {', '.join(_fmt_requests(r) for r in request_grid)}",
        f"active compute (driver='other') folded into the per-request cost assuming {assume_request_vcpus:g}"
        f" vCPU + {assume_request_gib:g} GiB held {assume_request_seconds:g}s per request; always-on / "
        "provisioned components folded as a flat monthly floor",
    )
    return UsageRate(provider=provider, region=region, service=service, capture_date=capture_date,
                     components=tuple(components), schedule=tuple(schedule), assumptions=assumptions,
                     price_source=price_source, price_urls=tuple(price_urls), fx=fx)


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
                     price_urls: Tuple[str, ...] = (),
                     fx: Optional[FxConversion] = None) -> RunRate:
    """Compose the all-in standing hourly run-rate = sum of the always-on components' $/hour. Egress is
    passed through as a SEPARATE field and is deliberately NOT added to the sum (C19). ``fx`` discloses the
    dated FX conversion when the provider lists in a non-USD currency (components must ALREADY be $/hour)."""
    all_in = sum(c.hourly_usd for c in components)
    return RunRate(
        all_in_hourly_usd=round(all_in, 6),
        components=tuple(components),
        provider=provider, region=region, flavor=flavor, capture_date=capture_date,
        egress=egress, exclusions=tuple(exclusions), price_source=price_source,
        price_urls=tuple(price_urls), fx=fx,
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
