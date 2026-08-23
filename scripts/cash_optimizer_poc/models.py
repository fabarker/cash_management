"""
Data models for the cash optimizer.

Contains all dataclasses, enums, and configuration objects used across
the optimizer, result, and cash manager modules.
"""
from __future__ import annotations

import math

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


# ────────────────────────────────────────────
# Enums
# ────────────────────────────────────────────

class Direction(str, Enum):
    """Trade direction — avoids raw string literals."""
    BUY = "BUY"
    SELL = "SELL"


# ────────────────────────────────────────────
# Constraint Flags
# ────────────────────────────────────────────

@dataclass
class ConstraintFlags:
    """
    Toggle individual optimizer constraints on or off.

    All flags default to ``True`` (active).  Structural constraints
    (balance evolution, balance decomposition, activation linking,
    commission-tier linking) are always active — they define the model
    mechanics and cannot be disabled.

    Attributes
    ----------
    terminal_sweep : bool
        Force all foreign balances to zero at the end of the horizon.
    no_loop : bool
        Forbid simultaneous buy and sell of the same currency on the
        same settlement day.
    anti_speculative : bool
        Cap cumulative buys per currency, day by day, to the deepest
        funding shortfall the cash ladder actually reaches within the
        settlement window ahead — preventing speculative carry trades
        without blocking genuine pre-funding of a timing gap.
    no_carry_trade : bool
        Enforce monotonic drawdown of foreign balances on days with no
        future cashflow activity, preventing the optimizer from holding
        foreign currency purely for yield (carry-trade behaviour).
    """

    terminal_sweep: bool = True
    no_loop: bool = True
    anti_speculative: bool = True
    no_carry_trade: bool = True

    def summary(self) -> str:
        """Return a compact one-line summary of active/inactive flags."""
        flags = {
            "terminal_sweep": self.terminal_sweep,
            "no_loop": self.no_loop,
            "anti_speculative": self.anti_speculative,
            "no_carry_trade": self.no_carry_trade,
        }
        on = [k for k, v in flags.items() if v]
        off = [k for k, v in flags.items() if not v]
        parts = []
        if on:
            parts.append(f"ON: {', '.join(on)}")
        if off:
            parts.append(f"OFF: {', '.join(off)}")
        return " | ".join(parts)


# ────────────────────────────────────────────
# Commission Tiers
# ────────────────────────────────────────────

@dataclass
class CommissionTier:
    """A single tier in the commission schedule.

    Tiers are ordered smallest-to-largest by ``threshold``.  The first
    tier covers ``[0, threshold]``; subsequent tiers cover the band
    between the previous tier's threshold and their own.

    Parameters
    ----------
    threshold : float
        Upper notional bound of this tier (in foreign ccy units).
    rate_bps : float
        Commission rate in bps for volume within this tier.
    """
    threshold: float
    rate_bps: float


# ────────────────────────────────────────────
# FX Tenor Quotes
# ────────────────────────────────────────────

@dataclass
class FXTenorQuote:
    """Bid/ask FX quote for a specific currency and tenor.

    Rates are expressed as *base currency per 1 unit of foreign
    currency*.  For example, if base=GBP and foreign=USD:

    - ``bid = 0.7895`` → you receive 0.7895 GBP when selling 1 USD
    - ``ask = 0.7905`` → you pay 0.7905 GBP to buy 1 USD

    The **bid** is used when the optimizer **sells** foreign currency
    (converting foreign → base).  The **ask** is used when the
    optimizer **buys** foreign currency (converting base → foreign).

    Parameters
    ----------
    bid : float
        Rate at which you sell foreign (lower, you receive this).
    ask : float
        Rate at which you buy foreign (higher, you pay this).
    """
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        """Mid-market rate (average of bid and ask)."""
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        """Absolute spread (ask − bid)."""
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        """Spread in basis points relative to mid."""
        m = self.mid
        if m == 0:
            return 0.0
        return self.spread / m * 10_000.0


# ────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────

@dataclass
class Config:
    """All tuneable parameters in one place — no magic numbers."""

    base_ccy: str = "GBP"
    currencies: List[str] = field(default_factory=lambda: ["GBP", "USD", "EUR"])
    horizon_days: int = 5

    # Tenor name -> settlement lag in days
    tenors: Dict[str, int] = field(default_factory=lambda: {
        "T0": 0, "T1": 1, "T2": 2,
    })

    # FX quotes: {foreign_ccy: {tenor: FXTenorQuote}}
    # Each quote gives the outright bid and ask in base ccy per 1 foreign.
    fx_quotes: Dict[str, Dict[str, FXTenorQuote]] = field(default_factory=lambda: {
        "USD": {
            "T0": FXTenorQuote(bid=0.7895, ask=0.7905),
            "T1": FXTenorQuote(bid=0.7897, ask=0.7903),
            "T2": FXTenorQuote(bid=0.7898, ask=0.7902),
        },
        "EUR": {
            "T0": FXTenorQuote(bid=1.1695, ask=1.1705),
            "T1": FXTenorQuote(bid=1.1697, ask=1.1703),
            "T2": FXTenorQuote(bid=1.1698, ask=1.1702),
        },
    })

    # Commission schedule — list of tiers ordered smallest-to-largest.
    # Each tier defines a notional band and the bps rate charged on
    # volume within that band.  The last tier's threshold must be ≥
    # max_trade.
    #
    # Default: 20 bps on the first 500k, 10 bps above 500k.
    commission_tiers: List[CommissionTier] = field(default_factory=lambda: [
        CommissionTier(threshold=500_000, rate_bps=20.0),
        CommissionTier(threshold=500_000_000, rate_bps=10.0),
    ])

    # Carry rates (annualised percentage), keyed by currency.
    credit_carry_pa: Dict[str, float] = field(default_factory=lambda: {
        "GBP": 3.65, "USD": 0.5, "EUR": 0.2, "JPY": 0.01,
    })

    # The spot tenor
    spot_tenor: Optional[str] = "T2"

    # Day index from which the credit carry differential applies.
    credit_carry_start_day: int = 0

    # Debit / overdraft charge (annualised percentage), keyed by currency.
    debit_carry_pa: Dict[str, float] = field(default_factory=lambda: {
        "GBP": 5.0, "USD": 5.0, "EUR": 4.5, "JPY": 3.0,
    })

    # Money-market day count per currency: how many days the market treats
    # as a year when accruing interest.
    #
    # This is a convention, not a fact, and it differs by currency — it is
    # how the interest is actually contracted.  Dividing an annual rate by
    # 365 when the market divides by 360 understates every day's interest
    # by 365/360 - 1 = 1.39%, which is charged in full on an overdraft.
    #
    # It is NOT "sterling is 365 and everything else is 360": yen, Canadian
    # and Australian dollars and several Asian currencies are also ACT/365.
    # Check this map against whatever supplies your rates before relying on
    # it for a currency that matters.
    day_count_basis: Dict[str, int] = field(default_factory=lambda: {
        # ACT/365
        "GBP": 365, "JPY": 365, "AUD": 365, "NZD": 365,
        "CAD": 365, "HKD": 365, "SGD": 365, "ZAR": 365,
        # ACT/360
        "USD": 360, "EUR": 360, "CHF": 360,
        "SEK": 360, "NOK": 360, "DKK": 360,
    })

    # Basis for a currency absent from the map above.  360 is the more
    # common convention worldwide, so it is the safer default — but a
    # currency you rely on belongs in the map, not on this fallback.
    default_day_count: int = 360

    def day_count(self, ccy: str) -> int:
        """Days-in-a-year basis used to accrue interest in *ccy*."""
        return self.day_count_basis.get(ccy, self.default_day_count)

    def currencies_on_default_day_count(self) -> List[str]:
        """Currencies falling back rather than named in the map.

        Worth surfacing: a fallback is a guess about a market convention,
        and a wrong guess mis-states that currency's interest by 1.39%.
        """
        named = set(self.day_count_basis)
        seen = set(self.credit_carry_pa) | set(self.debit_carry_pa)
        return sorted(seen - named)

    @property
    def credit_carry_bps_per_day(self) -> Dict[str, float]:
        """Daily credit carry in bps, on each currency's own day count."""
        return {ccy: rate * 100.0 / self.day_count(ccy)
                for ccy, rate in self.credit_carry_pa.items()}

    @property
    def debit_carry_bps_per_day(self) -> Dict[str, float]:
        """Daily debit carry in bps, on each currency's own day count."""
        return {ccy: rate * 100.0 / self.day_count(ccy)
                for ccy, rate in self.debit_carry_pa.items()}

    # FX exposure penalty (bps per day on absolute position)
    fx_exposure_bps_per_day: float = 1.0

    # Day index from which the FX exposure penalty applies.
    fx_exposure_start_day: Optional[int] = None

    # Max size of a single trade, in foreign ccy units, per
    # (currency, day, tenor).  Must not exceed the total capacity of the
    # commission schedule — ``validate()`` enforces that.
    max_trade: float = 500_000_000.0

    # Hard ceiling on the absolute balance of any one currency on any one
    # day, expressed in BASE ccy and converted per currency at spot mid.
    #
    # This is a genuine position limit: the balance decomposition cannot
    # represent a balance larger than this, so a scenario that breaches it
    # is infeasible.  Leave as ``None`` to have CashOptimizer derive it
    # from the scenario (total gross cash x ``balance_headroom``), which
    # accommodates any cash-management plan while keeping the model's
    # coefficients small enough for the solver to resolve reliably.
    max_balance: Optional[float] = None

    # Multiple of total gross cash used when ``max_balance`` is derived.
    # Larger values permit bigger positions but degrade solver numerics.
    balance_headroom: float = 1.25

    # Floor for the derived balance ceiling, so a scenario with little or
    # no cash still produces a well-formed model.
    min_balance_ceiling: float = 1_000.0

    # Slack on the anti-speculative purchase cap.
    #
    # For a currency with outflows and no inflows the cap is naturally
    # EXACTLY the funding requirement, while the terminal sweep forces
    # total purchases up to that same value from below.  The feasible set
    # for total buys then collapses to a single point, which the solver
    # cannot reliably certify — it reports "Infeasible" for a scenario
    # that plainly has an answer, and which value of ``max_trade`` you
    # happen to pick decides whether it does.
    #
    # A hair of slack leaves the constraint economically identical (a
    # position still cannot be built) while giving the solver room to
    # work.  The allowance is ``max(cap * tolerance, min_slack)``, with
    # ``min_slack`` in the foreign currency's own units.
    anti_speculative_tolerance: float = 1e-6
    anti_speculative_min_slack: float = 1.0

    # ── Trade-rate valuation (finding F4) ─────────────────────
    #
    # Without this, the objective counts every cost of a trade and none of
    # the money it brings in, so the rate actually dealt at is invisible to
    # the optimisation and the model systematically settles too early.
    #
    # Each trade is charged the distance between the rate dealt and a single
    # reference rate for that currency (spot mid).  Because the same
    # reference is used for every tenor, the charge differs across tenors by
    # exactly the forward points — which is the information that was
    # missing.  The reference cancels out of any comparison, so its level
    # does not matter, only that it is common.
    #
    # Any foreign currency still held at the end of the horizon is charged
    # what it would cost to liquidate it, so that leaving a position behind
    # is not mistaken for having avoided a cost.
    #
    # Both terms are spread-scale, which keeps the objective a genuine cost
    # figure and keeps the optimality check in _improve_solution able to
    # resolve differences that matter.
    value_trade_rates: bool = True

    # ── Solver selection and optimality verification ──────────
    #
    # CBC 2.10.3 (the solver PuLP bundles) returns provably suboptimal
    # plans on this model and labels them "Optimal" — it has been observed
    # finding the correct answer, discarding it, and reporting a worse one
    # while its own log still shows the better bound.  No CBC option
    # recovers it.  HiGHS solves the same cases correctly.
    #
    # ``None`` picks the best solver available, preferring HiGHS and
    # falling back to CBC.  Install HiGHS with ``pip install highspy``.
    solver_name: Optional[str] = None

    # After solving, test whether a strictly cheaper plan exists by adding
    # "objective <= returned cost - epsilon" and solving again.  If one is
    # found, the first answer was demonstrably not optimal — and the better
    # plan is adopted.  This is proof-based: it never raises a false alarm,
    # unlike comparing against the LP relaxation bound, which on this model
    # sits 30-50% below the true optimum even when the answer is correct.
    verify_optimality: bool = True

    # How many improvement rounds to attempt.  Each costs one extra solve.
    optimality_passes: int = 2

    # Constraint toggles
    constraints: ConstraintFlags = field(default_factory=ConstraintFlags)

    # Big-M for binary trade activation.  Sized to the largest trade the
    # model can actually place — NOT used to bound balances (see
    # ``max_balance`` and ``CashOptimizer._compute_balance_bounds``).
    big_m: float = field(init=False)

    def __post_init__(self) -> None:
        if self.commission_tiers:
            tier_capacity = self.commission_tiers[-1].threshold
            self.big_m = min(self.max_trade, tier_capacity) + 1.0
        else:
            self.big_m = self.max_trade + 1.0
        if self.fx_exposure_start_day is None:
            self.fx_exposure_start_day = max(self.tenors.values()) + 1
        self.validate()

    # ── FX rate helpers ────────────────────────────────────────

    def fx_bid(self, ccy: str, tenor: str) -> float:
        """Return the bid rate (base per 1 foreign) for selling foreign."""
        return self.fx_quotes[ccy][tenor].bid

    def fx_ask(self, ccy: str, tenor: str) -> float:
        """Return the ask rate (base per 1 foreign) for buying foreign."""
        return self.fx_quotes[ccy][tenor].ask

    def fx_mid(self, ccy: str, tenor: str) -> float:
        """Return the mid rate for a given (ccy, tenor)."""
        return self.fx_quotes[ccy][tenor].mid

    def fx_spot_mid(self, ccy: str) -> float:
        """Return the spot (T2) mid rate for a currency.

        Used for valuation of carry and exposure — not for trade pricing.
        """
        spot = self.spot_tenor or "T2"
        return self.fx_quotes[ccy][spot].mid

    def fx_spot_bid(self, ccy: str) -> float:
        """Return the spot bid rate — conservative exit valuation."""
        spot = self.spot_tenor or "T2"
        return self.fx_quotes[ccy][spot].bid

    def fx_spot_ask(self, ccy: str) -> float:
        """Return the spot ask rate — conservative entry valuation."""
        spot = self.spot_tenor or "T2"
        return self.fx_quotes[ccy][spot].ask

    def validate(self) -> None:
        """Centralised validation — raises ``ValueError`` on bad config."""
        if self.horizon_days <= 0:
            raise ValueError(f"horizon_days must be positive, got {self.horizon_days}")

        if not self.tenors:
            raise ValueError("tenors must be non-empty")

        if self.base_ccy not in self.currencies:
            raise ValueError(f"base_ccy {self.base_ccy!r} not in currencies list")

        # Validate fx_quotes: every foreign ccy must have quotes for every tenor
        foreign = {c for c in self.currencies if c != self.base_ccy}
        missing_fx = foreign - set(self.fx_quotes.keys())
        if missing_fx:
            raise ValueError(f"Missing fx_quotes for foreign currencies: {missing_fx}")

        for ccy in foreign:
            if ccy not in self.fx_quotes:
                continue
            ccy_quotes = self.fx_quotes[ccy]
            missing_tenors = set(self.tenors.keys()) - set(ccy_quotes.keys())
            if missing_tenors:
                raise ValueError(
                    f"Missing fx_quotes tenors for {ccy}: {missing_tenors}"
                )
            for tenor, quote in ccy_quotes.items():
                if not (math.isfinite(quote.bid) and math.isfinite(quote.ask)):
                    raise ValueError(
                        f"fx_quotes[{ccy!r}][{tenor!r}] has a non-finite rate "
                        f"(bid={quote.bid!r}, ask={quote.ask!r}). NaN passes "
                        f"the bid<ask test silently — every comparison against "
                        f"it is false — and then fails deep inside model "
                        f"building with no indication of which quote was bad."
                    )
                if quote.bid >= quote.ask:
                    raise ValueError(
                        f"fx_quotes[{ccy!r}][{tenor!r}]: bid ({quote.bid}) "
                        f"must be strictly less than ask ({quote.ask})"
                    )

        if self.spot_tenor is not None and self.spot_tenor not in self.tenors:
            raise ValueError(
                f"spot_tenor {self.spot_tenor!r} is not in tenors: "
                f"{list(self.tenors.keys())}"
            )

        if self.max_trade <= 0:
            raise ValueError(f"max_trade must be positive, got {self.max_trade}")

        if self.max_balance is not None and self.max_balance <= 0:
            raise ValueError(
                f"max_balance must be positive when set, got {self.max_balance}"
            )

        if self.balance_headroom < 1.0:
            raise ValueError(
                f"balance_headroom must be >= 1.0, got {self.balance_headroom}"
            )

        if self.min_balance_ceiling <= 0:
            raise ValueError(
                f"min_balance_ceiling must be positive, "
                f"got {self.min_balance_ceiling}"
            )

        for ccy, basis in self.day_count_basis.items():
            if basis <= 0:
                raise ValueError(
                    f"day_count_basis[{ccy!r}] must be positive, got {basis}"
                )
        if self.default_day_count <= 0:
            raise ValueError(
                f"default_day_count must be positive, "
                f"got {self.default_day_count}"
            )

        if self.anti_speculative_tolerance < 0:
            raise ValueError(
                f"anti_speculative_tolerance must be >= 0, "
                f"got {self.anti_speculative_tolerance}"
            )

        if self.optimality_passes < 0:
            raise ValueError(
                f"optimality_passes must be >= 0, got {self.optimality_passes}"
            )

        if self.anti_speculative_min_slack < 0:
            raise ValueError(
                f"anti_speculative_min_slack must be >= 0, "
                f"got {self.anti_speculative_min_slack}"
            )

        for name, rates in (("credit_carry_pa", self.credit_carry_pa),
                            ("debit_carry_pa", self.debit_carry_pa)):
            for ccy, rate in rates.items():
                if not math.isfinite(rate):
                    raise ValueError(
                        f"{name}[{ccy!r}] is {rate!r}, which is not a finite "
                        f"number."
                    )

        all_ccys = set(self.currencies)
        missing_credit = all_ccys - set(self.credit_carry_pa.keys())
        if missing_credit:
            raise ValueError(f"Missing credit_carry_pa entries for: {missing_credit}")
        missing_debit = all_ccys - set(self.debit_carry_pa.keys())
        if missing_debit:
            raise ValueError(f"Missing debit_carry_pa entries for: {missing_debit}")

        if not self.commission_tiers:
            raise ValueError("commission_tiers must be non-empty")
        for i, tier in enumerate(self.commission_tiers):
            if tier.threshold <= 0:
                raise ValueError(
                    f"commission_tiers[{i}].threshold must be positive, "
                    f"got {tier.threshold}"
                )
            if i > 0 and tier.threshold <= self.commission_tiers[i - 1].threshold:
                raise ValueError(
                    f"commission_tiers must be strictly ascending by threshold; "
                    f"tier {i} ({tier.threshold}) <= tier {i-1} "
                    f"({self.commission_tiers[i-1].threshold})"
                )

        # The commission schedule is what actually bounds a trade: each
        # trade is split across tier segments whose widths sum to the last
        # threshold.  A max_trade above that is unreachable, and silently
        # so — the model would simply be infeasible for any trade between
        # the two.  Fail here instead, with the numbers in the message.
        tier_capacity = self.commission_tiers[-1].threshold
        if tier_capacity < self.max_trade:
            raise ValueError(
                f"commission_tiers[-1].threshold ({tier_capacity:,.2f}) is below "
                f"max_trade ({self.max_trade:,.2f}), so trades between the two "
                f"cannot be priced and the model would be infeasible for them. "
                f"Raise the final tier threshold to at least max_trade, or lower "
                f"max_trade."
            )


# ────────────────────────────────────────────
# Cash Flows
# ────────────────────────────────────────────

@dataclass
class CashFlowSet:
    """Simple container for known cash flows.

    Parameters
    ----------
    horizon_days : int or None
        If set, ``add()`` will reject day indices outside ``[0, horizon_days)``.
    """

    horizon_days: Optional[int] = None
    _flows: Dict[Tuple[str, int], float] = field(default_factory=dict)

    def add(self, ccy: str, day: int, amount: float) -> None:
        """Register a cash flow.  Validates day bounds when horizon is set."""
        if not math.isfinite(amount):
            raise ValueError(
                f"Cash flow for {ccy} on day {day} is {amount!r}, which is not "
                f"a finite number. A missing or unreadable figure must not be "
                f"passed in as NaN: every comparison against NaN is false, so "
                f"the currency would be judged inactive and silently dropped "
                f"from the model. Supply the real amount, or omit the flow."
            )
        if day < 0:
            raise ValueError(f"Cash flow day must be >= 0, got {day}")
        if self.horizon_days is not None and day >= self.horizon_days:
            raise ValueError(
                f"Cash flow day {day} is outside horizon [0, {self.horizon_days})"
            )
        key = (ccy, day)
        self._flows[key] = self._flows.get(key, 0.0) + amount

    def get(self, ccy: str, day: int) -> float:
        return self._flows.get((ccy, day), 0.0)

    def all_entries(self) -> List[Tuple[str, int, float]]:
        """Return all registered flows as ``(ccy, day, amount)`` triples."""
        return [(c, d, a) for (c, d), a in self._flows.items()]


# ────────────────────────────────────────────
# Result data classes
# ────────────────────────────────────────────

@dataclass
class Trade:
    """A single FX trade recommendation."""
    ccy: str
    day: int
    tenor: str
    direction: Direction
    amount: float
    settle_day: int


@dataclass
class BalanceSnapshot:
    """End-of-day balance breakdown for one (ccy, day)."""
    ccy: str
    day: int
    balance: float
    credit: float
    debit: float


@dataclass
class CashFlowEntry:
    """A single projected exogenous cash flow."""
    ccy: str
    day: int
    amount: float


@dataclass
class CashLadderEntry:
    """One row of the pre-trade (do-nothing) cash ladder."""
    ccy: str
    day: int
    opening: float
    cash_flow: float
    closing: float


# ────────────────────────────────────────────
# Manual trade input (used by CashManager)
# ────────────────────────────────────────────

@dataclass
class ManualTrade:
    """
    A user-specified trade to evaluate against the optimizer's cost model.

    Parameters
    ----------
    ccy : str
        Foreign currency (must be in ``Config.currencies`` and not the base).
    day : int
        Trade day index (0-based, within the horizon).
    tenor : str
        Settlement tenor (must be in ``Config.tenors``).
    direction : Direction
        ``Direction.BUY`` (buy foreign, pay base) or
        ``Direction.SELL`` (sell foreign, receive base).
    amount : float
        Trade size in foreign currency units (must be > 0).
    """

    ccy: str
    day: int
    tenor: str
    direction: Direction
    amount: float

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError(
                f"ManualTrade amount must be positive, got {self.amount}"
            )

    @property
    def settle_day(self) -> int:
        """Cannot be computed without config; placeholder for validation."""
        raise AttributeError(
            "settle_day requires tenor lag — use CashManager._resolve_settle_day()"
        )


@dataclass
class CostBreakdown:
    """
    Itemised cost breakdown for a set of trades, computed using the same
    cost model as the optimizer.

    All values are in base currency.

    This is the single definition.  It used to be declared here *and* again
    in ``cash_manager``, where the second shadowed the first — and the two
    drifted apart, the local copy gaining the rate and unwind terms while
    this one kept summing four components and quietly understating every
    total by the spread.
    """

    credit_carry_cost: float = 0.0
    debit_carry_cost: float = 0.0
    fx_exposure_cost: float = 0.0
    commission_cost: float = 0.0
    # Rate paid away against the reference rate (finding F4).
    spread_cost: float = 0.0
    # Cost of unwinding anything still held at the end of the horizon.
    terminal_unwind_cost: float = 0.0

    @property
    def total_cost(self) -> float:
        """Sum of all cost components."""
        return (
            self.credit_carry_cost
            + self.debit_carry_cost
            + self.fx_exposure_cost
            + self.commission_cost
            + self.spread_cost
            + self.terminal_unwind_cost
        )

    def format(self, indent: str = "  ") -> str:
        """Return a formatted multi-line string."""
        lines = [
            f"{indent}Credit carry (differential) : {self.credit_carry_cost:>14,.4f}",
            f"{indent}Debit carry (overdraft)     : {self.debit_carry_cost:>14,.4f}",
            f"{indent}FX exposure penalty         : {self.fx_exposure_cost:>14,.4f}",
            f"{indent}Commission                  : {self.commission_cost:>14,.4f}",
            f"{indent}Rate vs reference           : {self.spread_cost:>14,.4f}",
            f"{indent}Terminal unwind             : {self.terminal_unwind_cost:>14,.4f}",
            f"{indent}{'─' * 44}",
            f"{indent}TOTAL COST                  : {self.total_cost:>14,.4f}",
        ]
        return "\n".join(lines)

