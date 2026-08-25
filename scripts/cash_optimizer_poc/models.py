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

    The three original flags default to ``True`` (active);
    ``settle_on_need_only`` is a tightening rather than one of the standing
    rules and defaults to ``False``.  Structural constraints
    (balance evolution, balance decomposition, activation linking,
    commission-tier linking) are always active — they define the model
    mechanics and cannot be disabled.

    Attributes
    ----------
    terminal_sweep : bool
        Force all foreign balances to zero at the end of the horizon.
    no_loop : bool
        Forbid buying and selling the same currency for the same
        settlement day.  It exists for the case where a wash trade is
        forced rather than chosen: a need below the minimum ticket can
        otherwise be dealt as two legal tickets netting to an illegal
        amount, which the holding ceiling cannot see because the position
        nets to zero every day.
    holding_ceiling : bool
        Cap the foreign balance held at the end of each day at what the
        near-term ladder can justify: the funding hole a trade dealt that
        day could still settle against, plus whatever part of the
        do-nothing holding a remaining outflow will consume.

        Expressed on the holding rather than on the purchases, which is
        what lets one rule do the work of three.  It caps what is bought,
        because a purchase has to land somewhere.  It forces a sweep once
        obligations have passed, because the ceiling falls to zero.  And
        it also reaches currency that arrived as a receipt, which a cap on
        purchases cannot see at all.
    settle_on_need_only : bool
        Narrow the holding ceiling from a settlement window to the single
        day.  Off by default, and it only means anything while
        ``holding_ceiling`` is on.

        The ceiling normally admits the deepest hole anywhere in
        ``d .. d + max_settlement_lag``, so currency may be held from the
        moment the obligation comes within dealing reach.  With this on the
        window closes to ``d`` alone: the balance may only be positive on a
        day the do-nothing ladder is itself overdrawn.

        The practical effect is stronger than it sounds.  A purchase must
        then settle on the very day the money leaves, so the currency is
        never held overnight at all — bought for value on the day it is
        paid away.  That forfeits the carry the wider window collects, and
        it collapses the choice of tenor: each dealing day admits exactly
        the one tenor that lands on the obligation.

        It is a policy, not a correction.  The wider window is deliberate
        (see ``CashOptimizer._holding_ceiling``), and exists so the model
        keeps a free choice of tenor rather than being forced onto the
        shortest one.  Turn this on when no overnight position is
        acceptable at any price, and expect both a higher cost and more
        infeasibility against a minimum ticket, because a surplus has no
        neighbouring day left to sit in.
    """

    terminal_sweep: bool = True
    no_loop: bool = True
    holding_ceiling: bool = True
    settle_on_need_only: bool = False

    def summary(self) -> str:
        """Return a compact one-line summary of active/inactive flags."""
        flags = {
            "terminal_sweep": self.terminal_sweep,
            "no_loop": self.no_loop,
            "holding_ceiling": self.holding_ceiling,
            "settle_on_need_only": self.settle_on_need_only,
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

    # Smallest dealable ticket, in BASE currency and converted per currency.
    #
    # Zero by default, which leaves behaviour unchanged.  Set it and the
    # model must either trade at least this much or not trade at all, which
    # is what the activation binaries were built for — they have never
    # carried a cost or imposed a floor, so nothing stopped the optimizer
    # emitting a ticket nobody could deal.
    #
    # Be aware this can make a scenario infeasible: if the funding required
    # is below the minimum and the anti-speculative cap will not permit
    # buying more, there is no legal ticket. That is a true answer about a
    # real dealing constraint, not a defect.
    min_trade: float = 0.0

    def min_trade_in(self, ccy: str) -> float:
        """``min_trade`` expressed in *ccy* units."""
        if not self.min_trade:
            return 0.0
        if ccy == self.base_ccy:
            return self.min_trade
        try:
            mid = self.fx_spot_mid(ccy)
            if mid > 0:
                return self.min_trade / mid
        except (KeyError, AttributeError):
            pass
        return self.min_trade

    # A trade below this is treated as rounding and left out of the
    # reported plan.  Expressed in BASE currency and converted per currency,
    # because a flat figure means wildly different things across them: 0.01
    # of a yen is far below quotable precision, 0.01 of a dollar is not.
    # The same mistake was found and fixed once before, in the sign-pin
    # threshold that has since been removed with its constraint.
    trade_report_floor: float = 0.01

    def trade_report_floor_in(self, ccy: str) -> float:
        """``trade_report_floor`` expressed in *ccy* units."""
        if ccy == self.base_ccy:
            return self.trade_report_floor
        try:
            mid = self.fx_spot_mid(ccy)
            if mid > 0:
                return self.trade_report_floor / mid
        except (KeyError, AttributeError):
            pass
        return self.trade_report_floor

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
    # Slack on the holding corridor, applied to both bounds.
    #
    # This was tried at zero on the reasoning that a bound on a balance has
    # no pin for the solver to trip over, unlike the cumulative purchase cap
    # it replaced.  That was wrong.  A ladder whose deepest overdraft is
    # exactly the floor -- which is what "do nothing" always produces -- puts
    # the bound precisely on the value the plan needs, and the solver cannot
    # certify it: a five-day scenario went Infeasible that solved at six
    # days, and one hundredth of a currency unit was enough to flip it back.
    #
    # One unit is negligible against any dealable amount.  Where a bound is
    # zero it stays exactly zero (see _add_holding_ceiling), so a currency
    # with nothing to justify still cannot hold a penny.
    holding_tolerance: float = 1e-6
    holding_min_slack: float = 1.0

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

    def __post_init__(self) -> None:
        self.validate()

    # ── Derived values ─────────────────────────────────────────
    #
    # Computed on access rather than stored.  They used to be filled in by
    # __post_init__ and never refreshed, so changing max_trade, the
    # commission schedule or the tenors afterwards left them describing the
    # configuration as it was at construction — silently, since validate()
    # did not re-run either.  Nothing is cached now, so nothing can go stale.

    @property
    def big_m(self) -> float:
        """Big-M for binary trade activation.

        Sized to the largest trade the model can actually place, which is
        bounded by the commission schedule as well as by ``max_trade``.  NOT
        used to bound balances — see ``max_balance`` and
        ``CashOptimizer._compute_balance_bounds``.
        """
        if self.commission_tiers:
            return min(self.max_trade, self.commission_tiers[-1].threshold) + 1.0
        return self.max_trade + 1.0

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

        if self.min_trade < 0:
            raise ValueError(f"min_trade must be >= 0, got {self.min_trade}")

        if self.trade_report_floor < 0:
            raise ValueError(
                f"trade_report_floor must be >= 0, "
                f"got {self.trade_report_floor}"
            )

        if self.holding_tolerance < 0:
            raise ValueError(
                f"holding_tolerance must be >= 0, got {self.holding_tolerance}"
            )
        if self.holding_min_slack < 0:
            raise ValueError(
                f"holding_min_slack must be >= 0, got {self.holding_min_slack}"
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
            + self.commission_cost
            + self.spread_cost
            + self.terminal_unwind_cost
        )

    def format(self, indent: str = "  ") -> str:
        """Return a formatted multi-line string."""
        lines = [
            f"{indent}Credit carry (differential) : {self.credit_carry_cost:>14,.4f}",
            f"{indent}Debit carry (overdraft)     : {self.debit_carry_cost:>14,.4f}",
            f"{indent}Commission                  : {self.commission_cost:>14,.4f}",
            f"{indent}Rate vs reference           : {self.spread_cost:>14,.4f}",
            f"{indent}Terminal unwind             : {self.terminal_unwind_cost:>14,.4f}",
            f"{indent}{'─' * 44}",
            f"{indent}TOTAL COST                  : {self.total_cost:>14,.4f}",
        ]
        return "\n".join(lines)

