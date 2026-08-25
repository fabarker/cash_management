"""
CashManager — Interactive Trade Evaluation and Comparison
=========================================================
Wraps the ``CashOptimizer`` in a stateful manager that lets you:

  1. Inspect the pre-trade cash ladder and projected cash flows.
  2. Solve for the optimal set of trades via ``solve_optimal()``.
  3. Manually specify your own trades via ``execute_trades()`` and
     see the full cost breakdown — using the **exact same cost model**
     as the optimizer.
  4. Compare the two side-by-side with ``compare()``.

The cost model applied to manual trades mirrors the optimizer's
objective function exactly:

  - Credit carry differential (opportunity cost / benefit)
  - Debit carry (overdraft charge, per-currency)
  - FX exposure penalty
  - Commission per trade

Usage::

    from scripts.cash_optimizer_poc.cash_manager import CashManager, ManualTrade
    from scripts.cash_optimizer_poc.cash_optimizer_poc import (
        Config, CashFlowSet, Direction,
    )

    cfg = Config(...)
    cf  = CashFlowSet(...)
    mgr = CashManager(cfg, cf, opening_balances={...})

    # 1. Inspect
    mgr.print_cash_ladder()

    # 2. Optimal solution
    optimal = mgr.solve_optimal()
    optimal.print_summary()

    # 3. Your own idea
    my_result = mgr.execute_trades([
        ManualTrade("USD", day=0, tenor="T0", direction=Direction.SELL, amount=100_000),
    ])
    my_result.print_summary()

    # 4. Head-to-head
    mgr.compare(my_result)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

from scripts.cash_optimizer_poc.models import (
    BalanceSnapshot,
    CashFlowEntry,
    CashFlowSet,
    CashLadderEntry,
    CommissionTier,
    Config,
    ConstraintFlags,
    CostBreakdown,
    Direction,
    FXTenorQuote,
    ManualTrade,
    Trade,
)
from scripts.cash_optimizer_poc.optimizer import CashOptimizer
from scripts.cash_optimizer_poc.result import Result
from scripts.cash_optimizer_poc.utils import (
    format_balances_pivoted,
    format_ladder_pivoted,
)

log = logging.getLogger("cash_manager")

COL_W = 14  # width of each day column in pivoted tables

# Fallback credit carry rates (annualised %) used when the Interest
# Engine service is unreachable.  Debit = 1.5× credit.
_FALLBACK_CREDIT_PA: Dict[str, float] = {
    "USD": 3.50,
    "GBP": 3.75,
    "EUR": 2.20,
    "JPY": 0.75,
    "CHF": 0.00,
}
_FALLBACK_DEBIT_MULTIPLIER: float = 1.5

# Use the shared formatters — keep private names for internal use
_format_ladder_pivoted = format_ladder_pivoted
_format_balances_pivoted = format_balances_pivoted
_fmt_bal_pivot = format_balances_pivoted


@dataclass
class ManualResult:
    """
    Structured result from evaluating a set of manual trades.
    Mirrors ``Result`` closely so the user can compare them.
    """

    trades: List[Trade]
    balances: List[BalanceSnapshot]
    cost_breakdown: CostBreakdown
    pre_trade_ladder: List[CashLadderEntry]
    cash_flows: List[CashFlowEntry]
    label: str = "Manual"

    @property
    def total_cost(self) -> float:
        return self.cost_breakdown.total_cost

    def format_summary(self) -> str:
        """Return formatted summary string, same layout as ``Result``."""
        lines: List[str] = []
        w = 70

        # ── Header ──
        lines.append(f"\n{'=' * w}")
        lines.append(f"  {self.label.upper()} — TRADE EVALUATION")
        lines.append(f"  TOTAL COST    : {self.total_cost:,.4f} (base ccy)")
        lines.append(f"{'=' * w}")

        # ── Cash Ladder BEFORE Trades ──
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append("  CASH LADDER — BEFORE TRADES (do-nothing projection)")
        lines.append(f"  {'─' * (w - 4)}")
        lines.extend(_format_ladder_pivoted(self.pre_trade_ladder))

        # ── Projected Cash Flows ──
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append("  PROJECTED CASH FLOWS")
        lines.append(f"  {'─' * (w - 4)}")
        non_zero = [cf for cf in self.cash_flows if abs(cf.amount) > 0.005]
        if non_zero:
            lines.append(f"  {'CCY':<12} {'Day':<5} {'Amount':>14}")
            lines.append(f"  {'-' * 33}")
            for cf in non_zero:
                lines.append(f"  {cf.ccy:<12} {cf.day:<5} {cf.amount:>14,.2f}")
        else:
            lines.append("  (no exogenous cash flows)")

        # ── Manual Trades ──
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append(f"  {self.label.upper()} TRADES")
        lines.append(f"  {'─' * (w - 4)}")
        if self.trades:
            lines.append(
                f"  {'CCY':<12} {'Day':<5} {'Tenor':<6} {'Dir':<6} "
                f"{'Amount':>14} {'Settle':>7}"
            )
            lines.append(f"  {'-' * 56}")
            for t in self.trades:
                lines.append(
                    f"  {t.ccy:<12} {t.day:<5} {t.tenor:<6} "
                    f"{t.direction.value:<6} {t.amount:>14,.2f} {t.settle_day:>7}"
                )
        else:
            lines.append("  NO TRADES SPECIFIED")

        # ── Cash Ladder AFTER Trades ──
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append(f"  CASH LADDER — AFTER TRADES ({self.label.lower()})")
        lines.append(f"  {'─' * (w - 4)}")
        lines.extend(_format_balances_pivoted(self.balances))

        # ── Cost Breakdown ──
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append("  COST BREAKDOWN")
        lines.append(f"  {'─' * (w - 4)}")
        lines.append(self.cost_breakdown.format())

        lines.append(f"\n{'=' * w}\n")
        return "\n".join(lines)

    def print_summary(self) -> None:
        """Print formatted summary to stdout."""
        print(self.format_summary())

    def compute_after_cost_balances(self, cfg: Config) -> List[BalanceSnapshot]:
        """Build after-cost balances with per-currency interest accrual.

        Delegates to :meth:`Result.compute_after_cost_balances` by
        constructing a lightweight ``Result`` wrapper — the logic is
        identical.

        Parameters
        ----------
        cfg : Config
            Configuration used when the trades were evaluated.
        """
        # Build a thin Result wrapper to reuse the same calculation
        wrapper = Result(
            status="Manual",
            total_cost=self.total_cost,
            trades=self.trades,
            balances=self.balances,
            pre_trade_ladder=self.pre_trade_ladder,
            cash_flows=self.cash_flows,
        )
        return wrapper.compute_after_cost_balances(cfg)

    def format_after_cost_ladder(self, cfg: Config) -> str:
        """Return a formatted after-cost cash ladder string."""
        ac_balances = self.compute_after_cost_balances(cfg)
        w = 70
        lines: List[str] = []
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append(
            "  CASH LADDER — AFTER COSTS "
            "(base ccy adjusted for all economic costs)"
        )
        lines.append(f"  {'─' * (w - 4)}")
        lines.extend(_fmt_bal_pivot(ac_balances))
        return "\n".join(lines)

    def print_after_cost_ladder(self, cfg: Config) -> None:
        """Print the after-cost cash ladder to stdout."""
        print(self.format_after_cost_ladder(cfg))


# ────────────────────────────────────────────
# CashManager
# ──────────────────────────���─────────────────

class CashManager:
    """
    Interactive cash management workbench.

    Encapsulates a scenario (configuration, cash flows, opening balances,
    opening balances) and provides two ways to evaluate trades:

    - ``solve_optimal()`` — delegates to ``CashOptimizer`` for the
      mathematically optimal solution.
    - ``execute_trades(trades)`` — evaluates a user-specified list of
      ``ManualTrade`` objects using the exact same cost model, producing
      a fully comparable ``ManualResult``.

    Properties expose the scenario state for inspection:
    ``base_ccy``, ``foreign_currencies``, ``opening_balances``,
    ``cash_ladder``, ``projected_cash_flows``.

    Parameters
    ----------
    cfg : Config
        Full optimizer configuration (currencies, rates, spreads, etc.).
    cashflows : CashFlowSet
        Exogenous cash flows over the horizon.
    opening_balances : dict, optional
        Opening balance per currency. Defaults to zero for missing keys.
    constraints : ConstraintFlags, optional
        Override which optimizer constraints are active.  If omitted,
        uses ``cfg.constraints``.  The manager takes a copy of *cfg* when
        overriding, so the object you pass in is never modified and two
        managers built from one Config stay independent.
    """

    def __init__(
        self,
        cfg: Config,
        cashflows: CashFlowSet,
        opening_balances: Optional[Dict[str, float]] = None,
        constraints: Optional[ConstraintFlags] = None,
    ) -> None:
        self._cfg = cfg
        self._cashflows = cashflows
        self._opening = opening_balances or {}
        # Match the optimizer's universe: declared currencies plus anything
        # that turns up in the opening balances or the cash flows, so a
        # discovered currency appears in the ladder and the manual evaluator
        # rather than silently missing from both.
        universe = list(cfg.currencies)
        for ccy in list(self._opening) + [c for c, _, _ in cashflows.all_entries()]:
            if ccy not in universe:
                universe.append(ccy)
        self._foreign_ccys = [c for c in universe if c != cfg.base_ccy]

        # Take a copy when overriding, rather than writing through to the
        # caller's object.  Assigning self._cfg.constraints would reach back
        # out and reconfigure the Config that was passed in — so building two
        # managers from one Config to compare settings silently changed the
        # first one, and which flags a manager ended up with depended on the
        # order they were constructed in.  dataclasses.replace shares
        # everything else, which is what the docstring above promised.
        if constraints is not None:
            self._cfg = replace(cfg, constraints=constraints)

        # Cached results
        self._optimal_result: Optional[Result] = None

    # ── Properties ─────────────────────────

    @property
    def config(self) -> Config:
        """The full optimizer configuration."""
        return self._cfg

    @property
    def base_ccy(self) -> str:
        """Base (home) currency."""
        return self._cfg.base_ccy

    @property
    def foreign_currencies(self) -> List[str]:
        """List of foreign currencies in the scenario."""
        return list(self._foreign_ccys)

    @property
    def currencies(self) -> List[str]:
        """All currencies (base + foreign)."""
        return list(self._cfg.currencies)

    @property
    def horizon_days(self) -> int:
        """Number of days in the optimization horizon."""
        return self._cfg.horizon_days

    @property
    def opening_balances(self) -> Dict[str, float]:
        """Opening balance per currency (zero if not specified)."""
        return {c: self._opening.get(c, 0.0) for c in self._cfg.currencies}

    @property
    def cashflows(self) -> CashFlowSet:
        """The exogenous cash flow set."""
        return self._cashflows

    @property
    def cash_ladder(self) -> List[CashLadderEntry]:
        """Pre-trade (do-nothing) cash ladder across the horizon."""
        return self._build_pre_trade_ladder()

    @property
    def projected_cash_flows(self) -> List[CashFlowEntry]:
        """All exogenous cash flows as typed entries."""
        return self._build_cash_flow_entries()

    @property
    def closing_balances_no_action(self) -> Dict[str, float]:
        """
        Closing balance per currency at end of horizon if no trades
        are executed — i.e. the do-nothing outcome.
        """
        ladder = self._build_pre_trade_ladder()
        last_day = self._cfg.horizon_days - 1
        result: Dict[str, float] = {}
        for entry in ladder:
            if entry.day == last_day:
                # Normalise label: "GBP (Base)" → "GBP"
                ccy = entry.ccy.split(" ")[0]
                result[ccy] = entry.closing
        return result

    @property
    def optimal_result(self) -> Optional[Result]:
        """The cached optimal result, or ``None`` if not yet solved."""
        return self._optimal_result

    # ── Pre-trade ladder / cash flow builders ──
    # (replicate the logic from CashOptimizer so CashManager can
    #  produce these without running the solver)

    def _to_base(self, ccy: str, amount: float) -> float:
        """Convert a foreign currency amount to base ccy."""
        if ccy == self._cfg.base_ccy:
            return amount
        return self._cfg.fx_spot_mid(ccy) * amount

    def _build_pre_trade_ladder(self) -> List[CashLadderEntry]:
        """Build do-nothing cash ladder (opening + exogenous flows only)."""
        ladder: List[CashLadderEntry] = []
        base = self._cfg.base_ccy

        # Base currency
        running = self._opening.get(base, 0.0)
        for d in range(self._cfg.horizon_days):
            cf = self._cashflows.get(base, d)
            opening = round(running, 2)
            closing = round(running + cf, 2)
            ladder.append(CashLadderEntry(
                ccy=f"{base} (Base)", day=d,
                opening=opening,
                cash_flow=round(cf, 2),
                closing=closing,
            ))
            running += cf

        # Foreign currencies
        for ccy in self._foreign_ccys:
            running = self._opening.get(ccy, 0.0)
            for d in range(self._cfg.horizon_days):
                cf = self._cashflows.get(ccy, d)
                opening = round(running, 2)
                closing = round(running + cf, 2)
                ladder.append(CashLadderEntry(
                    ccy=ccy, day=d,
                    opening=opening,
                    cash_flow=round(cf, 2),
                    closing=closing,
                ))
                running += cf
        return ladder

    def _build_cash_flow_entries(self) -> List[CashFlowEntry]:
        """Build typed list of all exogenous cash flows."""
        entries: List[CashFlowEntry] = []
        base = self._cfg.base_ccy
        for d in range(self._cfg.horizon_days):
            amt = self._cashflows.get(base, d)
            entries.append(CashFlowEntry(
                ccy=f"{base} (Base)", day=d, amount=round(amt, 2),
            ))
        for ccy in self._foreign_ccys:
            for d in range(self._cfg.horizon_days):
                amt = self._cashflows.get(ccy, d)
                entries.append(CashFlowEntry(
                    ccy=ccy, day=d, amount=round(amt, 2),
                ))
        return entries

    # ── Insert cash flow ───────────────────

    def insert_cash_flow(self, currency: str, day: int, amount: float) -> None:
        """
        Insert an ad-hoc cash flow into the cash ladder.

        The flow is added to the underlying ``CashFlowSet`` (cumulative
        with any existing flow for the same currency/day) and the cached
        optimal result is invalidated so that subsequent calls to
        ``solve_optimal()`` reflect the new projection.

        Parameters
        ----------
        currency : str
            ISO currency code — must be one of the currencies in the
            current configuration (``self._cfg.currencies``).
        day : int
            Day index within the horizon (0-based).
        amount : float
            Cash flow amount in *currency* units.  Positive = inflow,
            negative = outflow.

        Raises
        ------
        ValueError
            If *currency* is not in the scenario or *day* is out of range.
        """
        if currency not in self._cfg.currencies:
            raise ValueError(
                f"Currency '{currency}' is not in the scenario. "
                f"Valid currencies: {list(self._cfg.currencies)}"
            )
        if day < 0 or day >= self._cfg.horizon_days:
            raise ValueError(
                f"Day {day} is outside the horizon [0, {self._cfg.horizon_days})."
            )
        self._cashflows.add(currency, day, amount)
        # Invalidate any previously cached optimal solution
        self._optimal_result = None
        log.info(
            "Inserted cash flow: %s %.2f on day %d", currency, amount, day
        )

    # ── Solve optimal ──────────────────────

    def solve_optimal(
        self,
        solver_name: Optional[str] = None,
        time_limit: int = 120,
    ) -> Result:
        """
        Solve for the mathematically optimal set of trades.

        Creates a fresh ``CashOptimizer`` instance, solves, caches the
        result on this manager, and returns it.

        Returns
        -------
        Result
            The optimizer's structured result (status, trades, balances, cost).
        """
        optimizer = CashOptimizer(
            cfg=self._cfg,
            cashflows=self._cashflows,
            opening_balances=self._opening,
        )
        result = optimizer.solve(solver_name=solver_name, time_limit=time_limit)
        self._optimal_result = result
        return result

    # ── Execute manual trades ──────────────

    def execute_trades(
        self,
        trades: List[ManualTrade],
        label: Optional[str] = None,
    ) -> ManualResult:
        """
        Evaluate a set of user-specified trades using the **exact same
        cost model** as the optimizer, without running the solver.

        This deterministically computes:
          - Balance evolution across the horizon (opening + cash flows +
            trade settlements).
          - Credit carry differential, debit carry, FX exposure penalty,
            fixed commission, and spread cost — all using the same
            formulas and rates as ``CashOptimizer._build_objective()``.

        Parameters
        ----------
        trades : list of ManualTrade
            The trades to evaluate. May be an empty list (produces the
            do-nothing cost).
        label : str, optional
            A descriptive name for this scenario (e.g. "SELL 80k USD T0").
            If omitted, a label is auto-generated from the trade list.

        Returns
        -------
        ManualResult
            Full result with balances, cost breakdown, and formatted output.

        Raises
        ------
        ValueError
            If any trade references an unknown currency, tenor, or has a
            settlement day outside the horizon.
        """
        self._validate_manual_trades(trades)

        # Resolve Trade objects with computed settle_day
        resolved_trades = self._resolve_trades(trades)

        # Auto-generate label if not provided
        if label is None:
            label = self._auto_label(trades)

        # Build settlement schedule: (ccy, settle_day) -> net foreign amount
        # BUY adds foreign ccy, SELL removes it
        settle_schedule: Dict[Tuple[str, int], float] = {}
        base_impact: Dict[int, float] = {}
        for t in resolved_trades:
            key = (t.ccy, t.settle_day)
            delta = t.amount if t.direction == Direction.BUY else -t.amount
            settle_schedule[key] = settle_schedule.get(key, 0.0) + delta

            # What the trade does to base cash, at the rate it is actually
            # dealt at: buying foreign costs base at the ask, selling
            # generates base at the bid.  This has to be worked out per
            # trade, because the settlement schedule above nets amounts
            # together and loses the tenor each came from.
            if t.direction == Direction.BUY:
                moved = -self._cfg.fx_ask(t.ccy, t.tenor) * t.amount
            else:
                moved = self._cfg.fx_bid(t.ccy, t.tenor) * t.amount
            base_impact[t.settle_day] = base_impact.get(t.settle_day, 0.0) + moved

        # ── Balance evolution ──
        balances = self._compute_balances(settle_schedule, base_impact)

        # ── Cost computation ──
        cost = self._compute_cost(balances, resolved_trades)

        return ManualResult(
            trades=resolved_trades,
            balances=balances,
            cost_breakdown=cost,
            pre_trade_ladder=self._build_pre_trade_ladder(),
            cash_flows=self._build_cash_flow_entries(),
            label=label,
        )

    @staticmethod
    def _auto_label(trades: List[ManualTrade]) -> str:
        """Generate a concise human-readable label from a trade list."""
        if not trades:
            return "Do Nothing"
        if len(trades) == 1:
            t = trades[0]
            return f"{t.direction.value} {t.amount:,.0f} {t.ccy} {t.tenor} D{t.day}"
        return f"{len(trades)} trades"

    def _validate_manual_trades(self, trades: List[ManualTrade]) -> None:
        """Validate all manual trades against the current configuration."""
        valid_ccys = set(self._foreign_ccys)
        valid_tenors = set(self._cfg.tenors.keys())

        for i, t in enumerate(trades):
            if t.ccy not in valid_ccys:
                raise ValueError(
                    f"Trade {i}: currency {t.ccy!r} is not a foreign currency. "
                    f"Valid: {valid_ccys}"
                )
            if t.tenor not in valid_tenors:
                raise ValueError(
                    f"Trade {i}: tenor {t.tenor!r} is not valid. "
                    f"Valid: {valid_tenors}"
                )
            settle = t.day + self._cfg.tenors[t.tenor]
            if t.day < 0 or t.day >= self._cfg.horizon_days:
                raise ValueError(
                    f"Trade {i}: trade day {t.day} is outside horizon "
                    f"[0, {self._cfg.horizon_days})"
                )
            if settle >= self._cfg.horizon_days:
                raise ValueError(
                    f"Trade {i}: settlement day {settle} (day={t.day}, "
                    f"tenor={t.tenor}, lag={self._cfg.tenors[t.tenor]}) "
                    f"falls outside horizon [0, {self._cfg.horizon_days})"
                )
            if t.amount > self._cfg.max_trade:
                raise ValueError(
                    f"Trade {i}: amount {t.amount:,.2f} exceeds max_trade "
                    f"{self._cfg.max_trade:,.2f}"
                )

    def _resolve_trades(self, trades: List[ManualTrade]) -> List[Trade]:
        """Convert ManualTrade list to Trade objects with settle_day."""
        return [
            Trade(
                ccy=t.ccy,
                day=t.day,
                tenor=t.tenor,
                direction=t.direction,
                amount=t.amount,
                settle_day=t.day + self._cfg.tenors[t.tenor],
            )
            for t in trades
        ]

    def _compute_balances(
        self,
        settle_schedule: Dict[Tuple[str, int], float],
        base_impact: Optional[Dict[int, float]] = None,
    ) -> List[BalanceSnapshot]:
        """
        Compute end-of-day balances for all currencies across the horizon,
        incorporating opening balances, exogenous cash flows, and the
        settlement schedule from manual trades.
        """
        balances: List[BalanceSnapshot] = []
        base = self._cfg.base_ccy

        # ── Base currency balance ──
        # Base is impacted by the base-equivalent of foreign trade settlements
        base_running = self._opening.get(base, 0.0)
        for d in range(self._cfg.horizon_days):
            base_cf = self._cashflows.get(base, d)
            # Base impact from foreign trades settling today, at the rate
            # each was dealt at.  This used to convert the netted foreign
            # amount at spot mid, which is not a rate anyone deals on: the
            # same trade priced here and by the optimizer differed by the
            # half-spread, and always in the manual plan's favour, so
            # compare() flattered whichever side was entered by hand.
            base_trade_impact = (base_impact or {}).get(d, 0.0)

            bal = base_running + base_cf + base_trade_impact
            bal_r = round(bal, 2)
            balances.append(BalanceSnapshot(
                ccy=f"{base} (Base)", day=d,
                balance=bal_r,
                credit=round(max(bal_r, 0.0), 2),
                debit=round(abs(min(bal_r, 0.0)), 2),
            ))
            base_running = bal

        # ── Foreign currency balances ──
        for ccy in self._foreign_ccys:
            running = self._opening.get(ccy, 0.0)
            for d in range(self._cfg.horizon_days):
                cf = self._cashflows.get(ccy, d)
                trade_settle = settle_schedule.get((ccy, d), 0.0)
                bal = running + cf + trade_settle
                bal_r = round(bal, 2)
                balances.append(BalanceSnapshot(
                    ccy=ccy, day=d,
                    balance=bal_r,
                    credit=round(max(bal_r, 0.0), 2),
                    debit=round(abs(min(bal_r, 0.0)), 2),
                ))
                running = bal

        return balances

    def _compute_cost(
        self,
        balances: List[BalanceSnapshot],
        trades: List[Trade],
    ) -> CostBreakdown:
        """
        Compute the total cost of a set of trades using the exact same
        cost model as ``CashOptimizer._build_objective()``.

        This is a deterministic arithmetic evaluation — no LP solver involved.
        """
        cfg = self._cfg
        cost = CostBreakdown()

        # Index balances for fast lookup: (ccy_label, day) -> snapshot
        bal_idx: Dict[Tuple[str, int], BalanceSnapshot] = {
            (b.ccy, b.day): b for b in balances
        }

        base = cfg.base_ccy
        base_label = f"{base} (Base)"
        base_credit_rate = cfg.credit_carry_bps_per_day[base]
        base_debit_rate = cfg.debit_carry_bps_per_day[base]
        carry_start = cfg.credit_carry_start_day

        # ── Base currency debit carry ──
        for d in range(cfg.horizon_days):
            snap = bal_idx.get((base_label, d))
            if snap is None:
                continue
            bn = snap.debit
            cost.debit_carry_cost += base_debit_rate / 1e4 * bn

        # ── Foreign currency carry / debit / exposure ──
        for ccy in self._foreign_ccys:
            foreign_credit_rate = cfg.credit_carry_bps_per_day[ccy]
            net_carry_bps = base_credit_rate - foreign_credit_rate
            debit_rate_bps = cfg.debit_carry_bps_per_day[ccy]

            spot_bid = cfg.fx_spot_bid(ccy)
            spot_ask = cfg.fx_spot_ask(ccy)
            spot_mid = cfg.fx_spot_mid(ccy)

            for d in range(cfg.horizon_days):
                snap = bal_idx.get((ccy, d))
                if snap is None:
                    continue

                bp = snap.credit   # bal_pos equivalent
                bn = snap.debit    # bal_neg equivalent

                # Credit carry: value positive balance at bid (exit rate)
                if d >= carry_start:
                    cost.credit_carry_cost += (
                        net_carry_bps / 1e4 * spot_bid * bp
                    )

                # Debit carry: value negative balance at ask (entry rate)
                cost.debit_carry_cost += (
                    debit_rate_bps / 1e4 * spot_ask * bn
                )


        # ── Trade costs (commission only) ──
        # Group trades by (ccy, day, tenor) to count distinct activations
        buy_activations: Dict[Tuple[str, int, str], float] = {}
        sell_activations: Dict[Tuple[str, int, str], float] = {}

        for t in trades:
            key = (t.ccy, t.day, t.tenor)
            if t.direction == Direction.BUY:
                buy_activations[key] = (
                    buy_activations.get(key, 0.0) + t.amount
                )
            else:
                sell_activations[key] = (
                    sell_activations.get(key, 0.0) + t.amount
                )

        # Tiered commission schedule
        def _tiered_commission(amount_foreign: float) -> float:
            remaining = abs(amount_foreign)
            total = 0.0
            prev_threshold = 0.0
            for tier in cfg.commission_tiers:
                band = tier.threshold - prev_threshold
                fill = min(remaining, band)
                total += fill * (tier.rate_bps / 1e4)
                remaining -= fill
                prev_threshold = tier.threshold
                if remaining <= 0:
                    break
            return total

        # Commission on buys: notional at ask rate
        for (ccy, day, tenor), amt in buy_activations.items():
            ask_rate = cfg.fx_ask(ccy, tenor)
            cost.commission_cost += ask_rate * _tiered_commission(amt)
            if cfg.value_trade_rates:
                cost.spread_cost += (ask_rate - cfg.fx_spot_mid(ccy)) * amt

        # Commission on sells: notional at bid rate
        for (ccy, day, tenor), amt in sell_activations.items():
            bid_rate = cfg.fx_bid(ccy, tenor)
            cost.commission_cost += bid_rate * _tiered_commission(amt)
            if cfg.value_trade_rates:
                cost.spread_cost += (cfg.fx_spot_mid(ccy) - bid_rate) * amt

        # Anything still held at the horizon has to be unwound eventually;
        # charging that here stops a leftover position looking free.
        if cfg.value_trade_rates:
            last = cfg.horizon_days - 1
            unwind_bps = cfg.commission_tiers[0].rate_bps / 1e4
            for ccy in self._foreign_ccys:
                snap = bal_idx.get((ccy, last))
                if snap is None:
                    continue
                s_bid = cfg.fx_spot_bid(ccy)
                s_ask = cfg.fx_spot_ask(ccy)
                s_mid = cfg.fx_spot_mid(ccy)
                cost.terminal_unwind_cost += (
                    ((s_mid - s_bid) + unwind_bps * s_bid) * snap.credit
                    + ((s_ask - s_mid) + unwind_bps * s_ask) * snap.debit
                )

        return cost

    def constraint_violations(self, result) -> List[str]:
        """Which of the optimizer's rules a hand-entered plan breaks.

        ``execute_trades`` deliberately prices whatever it is given: that is
        the point of a workbench.  It checks only currency, tenor, horizon
        and size, so a manual plan can use trades the optimizer was
        forbidden to consider — and then appear to beat it.

        The old warning called that outcome impossible. It is not; it is
        routine, and the useful thing is to say which rule was broken.
        """
        cfg = self._cfg
        flags = cfg.constraints
        violations: List[str] = []
        trades = result.trades

        if flags.no_loop:
            settle: Dict[Tuple[str, int], set] = {}
            for t in trades:
                settle.setdefault((t.ccy, t.settle_day), set()).add(
                    t.direction.value)
            for (ccy, day), directions in sorted(settle.items()):
                if len(directions) > 1:
                    violations.append(
                        f"no_loop: {ccy} is both bought and sold for "
                        f"settlement on day {day}")

        if flags.holding_ceiling:
            optimizer = CashOptimizer(cfg, self._cashflows,
                                      opening_balances=self._opening)
            for ccy in optimizer.active_foreign_ccys:
                ceiling = optimizer._holding_ceiling(ccy)
                for b in result.balances:
                    if b.ccy != ccy:
                        continue
                    cap = ceiling[b.day]
                    if b.balance > cap + max(cap * 1e-6, 1.0):
                        violations.append(
                            f"holding_ceiling: {ccy} holds {b.balance:,.2f} "
                            f"on day {b.day} against a ceiling of {cap:,.2f}")
                        break

        if flags.sweep_opening_surplus:
            optimizer = CashOptimizer(cfg, self._cashflows,
                                      opening_balances=self._opening)
            for ccy in optimizer.active_foreign_ccys:
                surplus = optimizer.day_zero_surplus(ccy)
                floor = cfg.min_trade_in(ccy)
                if surplus <= cfg.trade_report_floor:
                    continue
                if floor and surplus < floor:
                    continue
                dealt = sum(t.amount for t in trades
                            if t.ccy == ccy and t.day == 0
                            and t.direction == Direction.SELL)
                if dealt + 1e-6 < surplus:
                    violations.append(
                        f"sweep_opening_surplus: {ccy} has {surplus:,.2f} "
                        f"unearmarked today but only {dealt:,.2f} is dealt on "
                        f"day 0")

        if cfg.min_trade:
            for t in trades:
                if t.amount + 1e-6 < cfg.min_trade_in(t.ccy):
                    violations.append(
                        f"min_trade: {t.amount:,.2f} {t.ccy} on day {t.day} is "
                        f"below the {cfg.min_trade_in(t.ccy):,.2f} minimum")

        if flags.terminal_sweep:
            last = cfg.horizon_days - 1
            for b in result.balances:
                if b.day == last and not b.ccy.startswith(cfg.base_ccy) \
                        and abs(b.balance) > 0.01:
                    violations.append(
                        f"terminal_sweep: {b.ccy} closes at {b.balance:,.2f} "
                        f"rather than zero")

        return violations

    # ── Comparison ─────────────────────────

    def _compute_optimal_cost_breakdown(self, opt: Result) -> CostBreakdown:
        """
        Compute an itemised ``CostBreakdown`` for the optimizer's result
        using the same deterministic arithmetic as ``_compute_cost()``.

        This lets us compare each cost component head-to-head against
        a manual trade evaluation.
        """
        return self._compute_cost(opt.balances, opt.trades)

    def compare(
        self,
        manual: ManualResult,
        optimal: Optional[Result] = None,
    ) -> None:
        """
        Print a side-by-side comparison of the optimal solution vs the
        user's manual trades, with an itemised cost breakdown table
        showing each cost component for both strategies and the difference.

        If ``optimal`` is not provided, uses the cached result from the
        most recent ``solve_optimal()`` call.

        Parameters
        ----------
        manual : ManualResult
            Result from ``execute_trades()``.
        optimal : Result, optional
            Result from ``solve_optimal()``.  If omitted, the cached
            result is used.

        Raises
        ------
        RuntimeError
            If no optimal result is available (neither passed nor cached).
        """
        opt = optimal or self._optimal_result
        if opt is None:
            raise RuntimeError(
                "No optimal result available. Call solve_optimal() first "
                "or pass the result explicitly."
            )

        # Compute itemised cost breakdown for the optimal result
        opt_cb = self._compute_optimal_cost_breakdown(opt)
        man_cb = manual.cost_breakdown
        man_label = manual.label

        w = 70
        lines: List[str] = []

        lines.append(f"\n{'=' * w}")
        lines.append(f"  COMPARISON: OPTIMAL vs {man_label.upper()}")
        lines.append(f"{'=' * w}")

        # ── Trade comparison ──
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append("  TRADES")
        lines.append(f"  {'─' * (w - 4)}")

        lines.append(f"\n  Optimal ({len(opt.trades)} trade(s)):")
        if opt.trades:
            lines.append(
                f"    {'CCY':<8} {'Day':<5} {'Tenor':<6} {'Dir':<6} "
                f"{'Amount':>14} {'Settle':>7}"
            )
            lines.append(f"    {'-' * 52}")
            for t in opt.trades:
                lines.append(
                    f"    {t.ccy:<8} {t.day:<5} {t.tenor:<6} "
                    f"{t.direction.value:<6} {t.amount:>14,.2f} {t.settle_day:>7}"
                )
        else:
            lines.append("    NO TRADES")

        lines.append(f"\n  {man_label} ({len(manual.trades)} trade(s)):")
        if manual.trades:
            lines.append(
                f"    {'CCY':<8} {'Day':<5} {'Tenor':<6} {'Dir':<6} "
                f"{'Amount':>14} {'Settle':>7}"
            )
            lines.append(f"    {'-' * 52}")
            for t in manual.trades:
                lines.append(
                    f"    {t.ccy:<8} {t.day:<5} {t.tenor:<6} "
                    f"{t.direction.value:<6} {t.amount:>14,.2f} {t.settle_day:>7}"
                )
        else:
            lines.append("    NO TRADES")

        # ── Side-by-side cost breakdown ──
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append("  COST BREAKDOWN (base ccy)")
        lines.append(f"  {'─' * (w - 4)}")

        # Truncate label for column header if too long
        col_label = man_label if len(man_label) <= 12 else man_label[:11] + "…"

        # Build rows: (label, optimal_value, manual_value)
        cost_rows = [
            ("Credit carry (diff.)", opt_cb.credit_carry_cost, man_cb.credit_carry_cost),
            ("Debit carry (o/d)",    opt_cb.debit_carry_cost,  man_cb.debit_carry_cost),
            ("Commission",           opt_cb.commission_cost,   man_cb.commission_cost),
            ("Rate vs reference",     opt_cb.spread_cost,       man_cb.spread_cost),
            ("Terminal unwind",       opt_cb.terminal_unwind_cost,
                                      man_cb.terminal_unwind_cost),
        ]

        header = (
            f"  {'Component':<22} {'Optimal':>12} {col_label:>12} "
            f"{'Diff':>12} {'':>8}"
        )
        lines.append(header)
        lines.append(f"  {'-' * (w - 4)}")

        for label, o_val, m_val in cost_rows:
            diff = m_val - o_val
            # Arrow indicator for quick scanning
            if abs(diff) < 1e-6:
                indicator = ""
            elif diff > 0:
                indicator = "▲ worse"
            else:
                indicator = "▼ better"
            lines.append(
                f"  {label:<22} {o_val:>12,.4f} {m_val:>12,.4f} "
                f"{diff:>12,.4f} {indicator:>8}"
            )

        # Total row
        lines.append(f"  {'─' * (w - 4)}")
        total_diff = man_cb.total_cost - opt_cb.total_cost
        total_indicator = ""
        if abs(total_diff) >= 1e-6:
            total_indicator = "▲ worse" if total_diff > 0 else "▼ better"
        lines.append(
            f"  {'TOTAL':<22} {opt_cb.total_cost:>12,.4f} "
            f"{man_cb.total_cost:>12,.4f} "
            f"{total_diff:>12,.4f} {total_indicator:>8}"
        )

        # ── Verdict ──
        if abs(total_diff) < 1e-6:
            lines.append(f"\n  ✓  Costs are equal — {man_label} matches optimal.")
        elif total_diff > 0:
            pct = (
                total_diff / abs(opt_cb.total_cost) * 100
                if abs(opt_cb.total_cost) > 1e-10 else 0.0
            )
            lines.append(
                f"\n  ⚠  {man_label} costs {total_diff:,.4f} MORE "
                f"than optimal ({pct:+.2f}%)."
            )
        else:
            lines.append(
                f"\n  ⚠  {man_label} costs {abs(total_diff):,.4f} LESS "
                f"than optimal."
            )
            broken = self.constraint_violations(manual)
            if broken:
                lines.append(
                    "     It uses trades the optimizer was not allowed to "
                    "consider:"
                )
                for v in broken:
                    lines.append(f"       - {v}")
                lines.append(
                    "     Relax the rule if it is wrong, or discard the plan."
                )
            else:
                lines.append(
                    "     It breaks none of the active constraints, so the "
                    "optimizer"
                )
                lines.append(
                    "     should have found it — treat the optimal result as "
                    "suspect."
                )

        # ── Terminal balance comparison ──
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append("  TERMINAL BALANCES (end of horizon)")
        lines.append(f"  {'─' * (w - 4)}")
        last_day = self._cfg.horizon_days - 1

        opt_terminal = {
            b.ccy: b.balance for b in opt.balances if b.day == last_day
        }
        man_terminal = {
            b.ccy: b.balance for b in manual.balances if b.day == last_day
        }
        all_labels = sorted(
            set(opt_terminal.keys()) | set(man_terminal.keys())
        )

        lines.append(
            f"  {'CCY':<14} {'Optimal':>14} {col_label:>14} {'Diff':>14}"
        )
        lines.append(f"  {'-' * 58}")
        for label in all_labels:
            o = opt_terminal.get(label, 0.0)
            m = man_terminal.get(label, 0.0)
            lines.append(
                f"  {label:<14} {o:>14,.2f} {m:>14,.2f} {m - o:>14,.2f}"
            )


        lines.append(f"\n{'=' * w}\n")
        print("\n".join(lines))

    # ── Convenience display ────────────────

    def print_cash_ladder(self) -> None:
        """Print the pre-trade (do-nothing) cash ladder."""
        w = 70
        lines: List[str] = [
            f"\n{'=' * w}",
            "  CASH LADDER — PRE-TRADE (do-nothing projection)",
            f"{'=' * w}",
        ]
        ladder = self._build_pre_trade_ladder()
        lines.extend(_format_ladder_pivoted(ladder))

        lines.append(f"\n  Closing balances (no action):")
        for ccy, bal in self.closing_balances_no_action.items():
            lines.append(f"    {ccy:<8} {bal:>14,.2f}")
        lines.append(f"{'=' * w}\n")
        print("\n".join(lines))

    def print_projected_cash_flows(self) -> None:
        """Print the projected exogenous cash flows."""
        w = 70
        non_zero = [cf for cf in self.projected_cash_flows if abs(cf.amount) > 0.005]
        lines = [
            f"\n{'=' * w}",
            "  PROJECTED CASH FLOWS",
            f"{'=' * w}",
        ]
        if non_zero:
            lines.append(f"  {'CCY':<12} {'Day':<5} {'Amount':>14}")
            lines.append(f"  {'-' * 33}")
            for cf in non_zero:
                lines.append(f"  {cf.ccy:<12} {cf.day:<5} {cf.amount:>14,.2f}")
        else:
            lines.append("  (no exogenous cash flows)")
        lines.append(f"{'=' * w}\n")
        print("\n".join(lines))

    def __repr__(self) -> str:
        return (
            f"CashManager(base_ccy={self.base_ccy!r}, "
            f"foreign={self._foreign_ccys}, "
            f"horizon={self._cfg.horizon_days}d, "
            f"opening={self.opening_balances}, "
            f"constraints=[{self._cfg.constraints.summary()}])"
        )

    # ── Construction from CashProjectionsManager ──
    #
    # @classmethod
    # def from_cash_projections(
    #     cls,
    #     projections: "CashProjectionsManager",
    #     *,
    #     fx_quotes: Optional[Dict[str, Dict[str, FXTenorQuote]]] = None,
    #     credit_carry_pa: Optional[Dict[str, float]] = None,
    #     debit_carry_pa: Optional[Dict[str, float]] = None,
    #     commission_tiers: Optional[List[CommissionTier]] = None,
    #     fx_exposure_bps_per_day: float = 0.0,
    #     constraints: Optional[ConstraintFlags] = None,
    #     **config_overrides,
    # ) -> "CashManager":
    #     """Create a ``CashManager`` from a :class:`CashProjectionsManager`.
    #
    #     The balance table from the projections is decomposed into:
    #
    #     * **Opening balances** — the balance for each currency on the
    #       first projection date.
    #     * **Cash flows** — the day-over-day change in balance for each
    #       currency on each subsequent date.
    #     * **Horizon** — the number of projection dates.
    #     * **Currencies** — all currencies present in the projections
    #       (base currency taken from ``projections.base_currency``).
    #
    #     FX quotes and carry parameters cannot be inferred from the
    #     projections and must be supplied explicitly for any foreign
    #     currencies present.
    #
    #     Parameters
    #     ----------
    #     projections : CashProjectionsManager
    #         The cash-flow projections to build from.
    #     fx_quotes : dict[str, dict[str, FXTenorQuote]], optional
    #         Outright bid/ask FX quotes per (foreign_ccy, tenor).
    #         Required for every foreign currency in the projections.
    #     credit_carry_pa : dict[str, float], optional
    #         Credit carry rate per currency (annualised %).  E.g. ``4.5``
    #         means 4.5% p.a.  Defaults to 0.3% for all currencies.
    #     debit_carry_pa : dict[str, float], optional
    #         Debit carry rate per currency (annualised %).  E.g. ``5.0``
    #         means 5% p.a.  Defaults to 5.0% for all currencies.
    #     commission_tiers : list of CommissionTier, optional
    #         Commission schedule — list of tiers ordered by threshold.
    #     fx_exposure_bps_per_day : float
    #         FX exposure penalty rate (bps/day).
    #     constraints : ConstraintFlags, optional
    #         Constraint toggles for the optimizer.
    #     **config_overrides
    #         Additional keyword arguments passed through to ``Config()``.
    #
    #     Returns
    #     -------
    #     CashManager
    #
    #     Raises
    #     ------
    #     ValueError
    #         If ``projections`` has no dates, no base currency, or if
    #         ``fx_quotes`` is missing for foreign currencies.
    #
    #     Examples
    #     --------
    #     ::
    #
    #         from pmg_core.dataModel.vendor.maxis.Models import CashProjectionsManager
    #
    #         mgr = CashProjectionsManager.from_sd_cash_projections(...)
    #         cm = CashManager.from_cash_projections(
    #             mgr,
    #             fx_quotes={"USD": {"T0": FXTenorQuote(0.789, 0.791), ...}},
    #         )
    #         cm.print_cash_ladder()
    #         result = cm.solve_optimal()
    #         result.print_summary()
    #     """
    #     from pmg_core.dataModel.vendor.maxis.Models import CashProjectionsManager as CPM
    #
    #     if not isinstance(projections, CPM):
    #         raise TypeError(
    #             f"Expected CashProjectionsManager, got {type(projections).__name__}"
    #         )
    #
    #     base_ccy = projections.base_currency
    #     if not base_ccy:
    #         raise ValueError(
    #             "CashProjectionsManager.base_currency must be set "
    #             "to identify the base currency."
    #         )
    #
    #     proj_dates = projections.dates
    #     if not proj_dates:
    #         raise ValueError(
    #             "CashProjectionsManager has no projection dates."
    #         )
    #
    #     all_ccys = [
    #         c for c in projections.currencies
    #         if "(Net Base)" not in c
    #     ]
    #     #if not all_ccys:
    #     #    raise ValueError(
    #     #        "CashProjectionsManager has no currencies."
    #     #    )
    #
    #     horizon_days = len(proj_dates)
    #     foreign_ccys = [c for c in all_ccys if c.upper() != base_ccy.upper()]
    #
    #     # ── Build date-to-day-index mapping ──
    #     date_to_day = {d: i for i, d in enumerate(proj_dates)}
    #
    #     # ── Extract opening balances and cash flows ──
    #     # First date's balance = opening balance.
    #     # Subsequent dates: cash flow = balance[day] - balance[day-1].
    #     opening_balances: Dict[str, float] = {}
    #     cashflows = CashFlowSet(horizon_days=horizon_days)
    #
    #     for ccy in all_ccys:
    #         balances = projections.get_balances_by_currency(ccy)
    #         if not balances:
    #             continue
    #
    #         # Build a day-indexed balance series
    #         bal_by_day: Dict[int, float] = {}
    #         for b in balances:
    #             if b.balance_date in date_to_day:
    #                 bal_by_day[date_to_day[b.balance_date]] = b.amount or 0.0
    #
    #         # Opening balance = balance on day 0
    #         opening_balances[ccy] = bal_by_day.get(0, 0.0)
    #
    #         # Cash flows = day-over-day deltas (day 0 has no intra-day flow
    #         # since its balance IS the opening balance)
    #         for d in range(1, horizon_days):
    #             prev = bal_by_day.get(d - 1, 0.0)
    #             curr = bal_by_day.get(d, 0.0)
    #             delta = curr - prev
    #             if abs(delta) > 1e-9:
    #                 cashflows.add(ccy, d, delta)
    #
    #     # ── Validate FX quotes ──
    #     if fx_quotes is None:
    #         fx_quotes = {}
    #     missing_fx = set(foreign_ccys) - set(fx_quotes.keys())
    #     if missing_fx:
    #         raise ValueError(
    #             f"fx_quotes must be supplied for all foreign currencies. "
    #             f"Missing: {missing_fx}"
    #         )
    #
    #     # ── Build carry rates with defaults (annualised %) ──
    #     default_credit = 0.3   # 0.3% p.a.
    #     default_debit = 5.0    # 5.0% p.a.
    #     if credit_carry_pa is None:
    #         credit_carry_pa = {c: default_credit for c in all_ccys}
    #     else:
    #         for c in all_ccys:
    #             credit_carry_pa.setdefault(c, default_credit)
    #
    #     if debit_carry_pa is None:
    #         debit_carry_pa = {c: default_debit for c in all_ccys}
    #     else:
    #         for c in all_ccys:
    #             debit_carry_pa.setdefault(c, default_debit)
    #
    #     # ── Build commission schedule ──
    #     if commission_tiers is None:
    #         commission_tiers = [
    #             CommissionTier(threshold=500_000, rate_bps=20.0),
    #             CommissionTier(threshold=500_000_000, rate_bps=10.0),
    #         ]
    #
    #     # ── Compute sensible max_trade from data (if not overridden) ──
    #     # A huge default big-M causes numerical instability and solver hangs.
    #     if "max_trade" not in config_overrides:
    #         total_abs = sum(abs(v) for v in opening_balances.values())
    #         for ccy, day, amt in cashflows.all_entries():
    #             total_abs += abs(amt)
    #         # 2× headroom; floor at 10M to avoid degenerate small values
    #         config_overrides["max_trade"] = max(10_000_000.0, total_abs * 2.0)
    #
    #     # ── Assemble Config ──
    #     cfg = Config(
    #         base_ccy=base_ccy,
    #         currencies=list(all_ccys),
    #         horizon_days=horizon_days,
    #         fx_quotes=fx_quotes,
    #         commission_tiers=commission_tiers,
    #         credit_carry_pa=credit_carry_pa,
    #         debit_carry_pa=debit_carry_pa,
    #         fx_exposure_bps_per_day=fx_exposure_bps_per_day,
    #         **config_overrides,
    #     )
    #
    #     return cls(
    #         cfg=cfg,
    #         cashflows=cashflows,
    #         opening_balances=opening_balances,
    #         constraints=constraints,
    #     )
    #
    # # ── Live FX rate fetching ──
    #
    # @staticmethod
    # def _fetch_fx_rates(
    #     base_ccy: str,
    #     foreign_ccys: List[str],
    #     fx_universe: Optional[List[str]] = None,
    #     fx_service: Optional[object] = None,
    # ) -> Dict[str, Dict[str, FXTenorQuote]]:
    #     """Fetch live FX quotes from Refinitiv.
    #
    #     Builds pairs as ``FOREIGN + BASE`` (e.g. ``EURGBP`` when
    #     base=GBP) so that each quote gives the rate
    #     "1 unit of foreign = X base".
    #
    #     Only currencies present in both *foreign_ccys* and
    #     *fx_universe* (default ``['GBP', 'EUR', 'USD', 'JPY']``)
    #     are fetched, excluding the base currency.
    #
    #     Returns
    #     -------
    #     Dict[str, Dict[str, FXTenorQuote]]
    #         ``{foreign_ccy: {tenor_name: FXTenorQuote}}``.
    #         Each ``FXTenorQuote`` has ``.bid`` and ``.ask`` in base
    #         ccy per 1 foreign.
    #     """
    #     from pmg_core.dataModel.vendor.refinitiv.FXForwardPrices import (
    #         RefinitivFXService,
    #         Tenor,
    #     )
    #
    #     if fx_universe is None:
    #         fx_universe = ["GBP", "EUR", "USD", "JPY", "CHF"]
    #
    #     ccys_to_fetch = [
    #         c for c in foreign_ccys
    #         if c.upper() in {u.upper() for u in fx_universe}
    #         and c.upper() != base_ccy.upper()
    #     ]
    #
    #     if not ccys_to_fetch:
    #         return {}
    #
    #     pairs = [f"{ccy}{base_ccy}" for ccy in ccys_to_fetch]
    #
    #     owns_service = fx_service is None
    #     if owns_service:
    #         # Don't connect yet — _ensure_legs will defer the connection
    #         # until after checking in-memory and daily disk caches.
    #         # This avoids the slow Refinitiv session open when all
    #         # rates are already cached.
    #         fx_service = RefinitivFXService(pairs=pairs)
    #
    #     try:
    #         tenor_map = {
    #             Tenor.SPOT: "T2",
    #             Tenor.TN: "T1",
    #             Tenor.ON: "T0",
    #         }
    #
    #         fx_quotes: Dict[str, Dict[str, FXTenorQuote]] = {}
    #
    #         for pair, ccy in zip(pairs, ccys_to_fetch):
    #             tenors = fx_service.get_all_tenors(pair)
    #             ccy_quotes: Dict[str, FXTenorQuote] = {}
    #
    #             for tenor_enum, quote in tenors.items():
    #                 tenor_name = tenor_map.get(tenor_enum)
    #                 if tenor_name is None:
    #                     continue
    #                 if (quote.bid is not None
    #                         and quote.ask is not None
    #                         and quote.bid < quote.ask):
    #                     ccy_quotes[tenor_name] = FXTenorQuote(
    #                         bid=quote.bid,
    #                         ask=quote.ask,
    #                     )
    #
    #             if ccy_quotes:
    #                 fx_quotes[ccy] = ccy_quotes
    #
    #             log.info(
    #                 "FX %s: %s",
    #                 pair,
    #                 {t: f"bid={q.bid:.6f} ask={q.ask:.6f}"
    #                  for t, q in ccy_quotes.items()},
    #             )
    #
    #         return fx_quotes
    #
    #     finally:
    #         if owns_service:
    #             fx_service.disconnect()
    #
    # # ── Live interest rate fetching ──
    #
    # @staticmethod
    # def _fetch_credit_debit_rates(
    #     account_number: str,
    #     currencies: List[str],
    #     interest_engine_service: Optional[object] = None,
    # ) -> Tuple[Dict[str, float], Dict[str, float]]:
    #     """Fetch credit and debit carry rates from the Interest Engine.
    #
    #     Calls ``InterestEngineService.get_carry_rates`` for the given
    #     account number with product_type ``'UKBD'`` and the specified
    #     currencies.  Returns two dicts suitable for ``Config``::
    #
    #         credit_carry_pa = {"GBP": 4.5, "USD": 3.0, ...}
    #         debit_carry_pa  = {"GBP": 6.0, "USD": 5.5, ...}
    #
    #     Parameters
    #     ----------
    #     account_number : str
    #         Sub-account / funding account number (e.g. from
    #         ``projection.account_balances.account_id``).
    #     currencies : list of str
    #         Currencies to fetch rates for.
    #     interest_engine_service : InterestEngineService, optional
    #         Pre-existing service instance.  A fresh one is created
    #         when *None* is supplied.
    #
    #     Returns
    #     -------
    #     (credit_carry_pa, debit_carry_pa) : tuple of dict
    #     """
    #     from pmg_core.dataModel.vendor.interest_engine.InterestEngineService import (
    #         InterestEngineService,
    #     )
    #
    #     if interest_engine_service is None:
    #         interest_engine_service = InterestEngineService()
    #
    #     mgr = interest_engine_service.get_carry_rates(
    #         account_number,
    #         currency=currencies,
    #         product_type=["CASH", "UKBD"],
    #     )
    #
    #     credit = mgr.to_credit_carry_pa()
    #     debit = mgr.to_debit_carry_pa()
    #
    #     log.info(
    #         "Interest rates for %s (UKBD): credit=%s, debit=%s",
    #         account_number, credit, debit,
    #     )
    #
    #     return credit, debit

    # ── Construction from group account number ──

    # @classmethod
    # def from_group_number(
    #     cls,
    #     group_number: str,
    #     *,
    #     fx_quotes: Optional[Dict[str, Dict[str, FXTenorQuote]]] = None,
    #     credit_carry_pa: Optional[Dict[str, float]] = None,
    #     debit_carry_pa: Optional[Dict[str, float]] = None,
    #     commission_tiers: Optional[List[CommissionTier]] = None,
    #     fx_exposure_bps_per_day: float = 1.0,
    #     constraints: Optional[ConstraintFlags] = None,
    #     svc: Optional[object] = None,
    #     fx_universe: Optional[List[str]] = None,
    #     fx_service: Optional[object] = None,
    #     interest_engine_service: Optional[object] = None,
    #     **config_overrides,
    # ) -> "CashManager":
    #     """Create a ``CashManager`` by fetching projections for a group account.
    #
    #     Uses :class:`ClientPortfoliosService` to fetch cash-flow
    #     projections via
    #     :meth:`get_cash_flow_projections_from_group_account`, then
    #     auto-populates any missing parameters:
    #
    #     - **FX quotes** — fetched from Refinitiv when ``fx_quotes``
    #       is not supplied.  Returns outright bid/ask per (ccy, tenor).
    #     - **Carry rates** — fetched from the Interest Engine (UKBD
    #       product type) when ``credit_carry_pa`` and
    #       ``debit_carry_pa`` are not supplied.
    #
    #     Finally delegates to :meth:`from_cash_projections` for the
    #     actual ``CashManager`` construction.
    #
    #     Parameters
    #     ----------
    #     group_number : str
    #         Group account number, e.g. ``'C88A37694'``.
    #     fx_quotes : dict[str, dict[str, FXTenorQuote]], optional
    #         Outright bid/ask FX quotes per (foreign_ccy, tenor).
    #         If omitted, quotes are fetched live from Refinitiv.
    #     credit_carry_pa : dict[str, float], optional
    #         Credit carry rate per currency (annualised %).
    #     debit_carry_pa : dict[str, float], optional
    #         Debit carry rate per currency (annualised %).
    #     commission_tiers : list of CommissionTier, optional
    #         Commission schedule.
    #     fx_exposure_bps_per_day : float
    #         FX exposure penalty rate (bps/day).
    #     constraints : ConstraintFlags, optional
    #         Constraint toggles for the optimizer.
    #     svc : ClientPortfoliosService, optional
    #         Pre-existing service instance.  A fresh one is created
    #         when *None* is supplied.
    #     fx_universe : list of str, optional
    #         Currencies to fetch FX rates for (excluding the base).
    #         Defaults to ``['GBP', 'EUR', 'USD', 'JPY']``.
    #     fx_service : RefinitivFXService, optional
    #         Pre-existing FX service instance.  A fresh one is created
    #         and connected when *None* is supplied.
    #     interest_engine_service : InterestEngineService, optional
    #         Pre-existing Interest Engine service instance.  A fresh one
    #         is created when *None* is supplied.
    #     **config_overrides
    #         Additional keyword arguments passed through to ``Config()``.
    #
    #     Returns
    #     -------
    #     CashManager
    #     """
    #     from pmg_core.dataModel.vendor.maxis.clientPortfolios.ClientPortfolios import (
    #         ClientPortfoliosClient,
    #         ClientPortfoliosService,
    #     )
    #
    #     if svc is None:
    #         client = ClientPortfoliosClient()
    #         svc = ClientPortfoliosService(client)
    #
    #     # ── Fetch projections via ClientPortfoliosService ──
    #     projections = svc.get_cash_flow_projections_from_group_account(
    #         group_number,
    #     )
    #
    #     base_ccy = projections.base_currency
    #     if not base_ccy:
    #         raise ValueError(
    #             f"No base currency found for group account {group_number!r}"
    #         )
    #
    #     all_ccys = [
    #         c for c in projections.currencies
    #         if "(Net Base)" not in c
    #     ]
    #     foreign_ccys = set([c for c in all_ccys if c.upper() != base_ccy.upper()])
    #
    #     log.info(
    #         "Loaded projections for group %s: base=%s, currencies=%s, "
    #         "dates=%s",
    #         group_number, base_ccy, foreign_ccys, projections.dates,
    #     )
    #
    #     # ── Auto-fetch FX quotes from Refinitiv ──
    #     if fx_quotes is None:
    #         fx_quotes = cls._fetch_fx_rates(
    #             base_ccy=base_ccy,
    #             foreign_ccys=foreign_ccys,
    #             fx_universe=fx_universe,
    #             fx_service=fx_service,
    #         )
    #
    #     # ── Auto-fetch carry rates from Interest Engine ──
    #     if credit_carry_pa is None and debit_carry_pa is None:
    #         funding_account = projections.funding_id
    #         if funding_account:
    #             try:
    #                 live_credit, live_debit = cls._fetch_credit_debit_rates(
    #                     account_number=funding_account,
    #                     currencies=list(all_ccys),
    #                     interest_engine_service=interest_engine_service,
    #                 )
    #                 credit_carry_pa = live_credit
    #                 debit_carry_pa = live_debit
    #             except Exception as exc:
    #                 log.warning(
    #                     "Failed to fetch interest rates for %s, "
    #                     "falling back to hardcoded defaults: %s",
    #                     funding_account, exc,
    #                 )
    #                 credit_carry_pa = {
    #                     c: _FALLBACK_CREDIT_PA.get(c, 1.0)
    #                     for c in all_ccys
    #                 }
    #                 debit_carry_pa = {
    #                     c: _FALLBACK_CREDIT_PA.get(c, 1.0) * _FALLBACK_DEBIT_MULTIPLIER
    #                     for c in all_ccys
    #                 }
    #                 log.info(
    #                     "Using fallback rates — credit: %s, debit: %s",
    #                     credit_carry_pa, debit_carry_pa,
    #                 )
    #         else:
    #             log.warning(
    #                 "No funding account ID on projection — "
    #                 "using hardcoded default carry rates"
    #             )
    #             credit_carry_pa = {
    #                 c: _FALLBACK_CREDIT_PA.get(c, 1.0)
    #                 for c in all_ccys
    #             }
    #             debit_carry_pa = {
    #                 c: _FALLBACK_CREDIT_PA.get(c, 1.0) * _FALLBACK_DEBIT_MULTIPLIER
    #                 for c in all_ccys
    #             }
    #
    #     # ── Delegate to from_cash_projections ──
    #     return cls.from_cash_projections(
    #         projections,
    #         fx_quotes=fx_quotes,
    #         credit_carry_pa=credit_carry_pa,
    #         debit_carry_pa=debit_carry_pa,
    #         commission_tiers=commission_tiers,
    #         fx_exposure_bps_per_day=fx_exposure_bps_per_day,
    #         constraints=constraints,
    #         **config_overrides,
    #     )


if __name__ == "__main__":

    mgr = CashManager.from_group_number('C88B60874')