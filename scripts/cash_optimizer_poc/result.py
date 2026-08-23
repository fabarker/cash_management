"""
Result — Structured output from the cash optimizer.

Extracted from the monolithic ``cash_optimizer_poc.py`` for clarity.
Contains the ``Result`` dataclass with all formatting, cost attribution,
and after-cost ladder methods.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from scripts.cash_optimizer_poc.models import (
    BalanceSnapshot,
    CashFlowEntry,
    CashLadderEntry,
    Config,
    Trade,
)
from scripts.cash_optimizer_poc.utils import (
    format_balances_pivoted,
    format_ladder_pivoted,
)

if TYPE_CHECKING:
    pass


@dataclass
class Result:
    """Structured output from the optimizer."""

    status: str
    total_cost: Optional[float]
    trades: List[Trade]
    balances: List[BalanceSnapshot]
    pre_trade_ladder: List[CashLadderEntry] = field(default_factory=list)
    cash_flows: List[CashFlowEntry] = field(default_factory=list)

    # Optimality evidence.  ``lp_bound`` is a valid floor under the true
    # optimum from the LP relaxation; ``optimality_gap`` is the relative
    # distance of the returned plan above it.  ``optimality_unproven`` is
    # set when that gap exceeds Config.max_optimality_gap, meaning a
    # cheaper plan may exist that the solver failed to find.
    # Book valued at reference rates with no frictions.  When trade rates
    # are valued, ``reference_value - total_cost`` is the net base currency
    # the plan ends with — the number the desk actually manages to.
    reference_value: Optional[float] = None

    lp_bound: Optional[float] = None
    optimality_gap: Optional[float] = None
    optimality_unproven: bool = False
    improved_from: Optional[float] = None
    solver_used: Optional[str] = None

    # The whole book in base currency at the end of the horizon, with any
    # remaining foreign balance converted at the rate it would be dealt at.
    terminal_base_equivalent: Optional[float] = None

    # Insufficient funds: raised only when the figure above is negative —
    # after everything is turned back into base, the account still owes
    # money, so no arrangement of trades could have covered every debit.
    # An infeasible model is NOT a funding shortfall: base overdrafts are
    # permitted, so a lack of cash shows up as a negative closing balance
    # rather than as infeasibility.
    insufficient_funds: bool = False
    shortfall: Optional[float] = None
    shortfall_detail: Dict[str, float] = field(default_factory=dict)

    @property
    def net_terminal_wealth(self) -> Optional[float]:
        """Base currency left at the end, after every cost.

        Exact rather than approximate: the objective measures the total
        value lost to spreads, commission, carry, exposure and any residual
        position, so subtracting it from the frictionless reference value
        gives what the plan actually delivers.
        """
        if self.reference_value is None or self.total_cost is None:
            return None
        return self.reference_value - self.total_cost

    def format_summary(self) -> str:
        """Return formatted summary as a string.

        Layout:
          1. Header (status, total cost)
          2. Cash Ladder BEFORE Trades (do-nothing projection)
          3. Projected Cash Flows
          4. Recommended Trades
          5. Cash Ladder AFTER Trades (optimized balances)
        """
        lines: List[str] = []
        w = 70

        # ── Header ──
        lines.append(f"\n{'=' * w}")
        lines.append(f"  SOLVER STATUS : {self.status}")
        if self.total_cost is not None:
            lines.append(f"  TOTAL COST    : {self.total_cost:,.4f} (base ccy)")
        else:
            lines.append("  TOTAL COST    : N/A")
        if self.terminal_base_equivalent is not None:
            lines.append(
                f"  CLOSING BASE  : {self.terminal_base_equivalent:,.2f} "
                f"(all currencies converted)"
            )
        if self.net_terminal_wealth is not None:
            lines.append(
                f"  NET TERMINAL  : {self.net_terminal_wealth:,.4f} "
                f"(base ccy after all costs)"
            )
        if self.optimality_gap is not None:
            lines.append(
                f"  LOWER BOUND   : {self.lp_bound:,.4f}"
                f"   (gap {self.optimality_gap * 100:.2f}%"
                f"{', UNPROVEN' if self.optimality_unproven else ''})"
            )
        if self.solver_used:
            lines.append(f"  SOLVER        : {self.solver_used}")
        lines.append(f"{'=' * w}")

        if self.optimality_unproven:
            lines.append("")
            lines.append(f"  {'!' * (w - 4)}")
            lines.append("  SOLVER RETURNED A SUB-OPTIMAL PLAN")
            lines.append(f"  {'!' * (w - 4)}")
            lines.append("")
            if self.improved_from is not None:
                lines.append(
                    f"  The solver first reported {self.improved_from:,.4f}. A"
                )
                lines.append(
                    "  cheaper plan was then found and is shown below, so the"
                )
                lines.append(
                    "  solver's own answer was demonstrably not optimal."
                )
                lines.append("")
                lines.append(
                    "  Treat other results from this solver with caution, and"
                )
                lines.append(
                    "  consider installing HiGHS (pip install highspy)."
                )
            else:
                lines.append(
                    "  The reported cost sits below a valid lower bound, which"
                )
                lines.append(
                    "  is impossible. Do not rely on this plan."
                )
            lines.append("")
            lines.append(f"  {'!' * (w - 4)}")

        # ── Insufficient funds ──
        if self.insufficient_funds:
            lines.append("")
            lines.append(f"  {'!' * (w - 4)}")
            lines.append("  INSUFFICIENT FUNDS")
            lines.append(f"  {'!' * (w - 4)}")
            lines.append("")
            lines.append(
                "  With every foreign balance converted back to base currency,"
            )
            lines.append(
                "  the account still closes in debit. No arrangement of trades"
            )
            lines.append(
                "  can cover every obligation; an external inflow is required."
            )
            if self.shortfall is not None:
                lines.append("")
                lines.append(
                    f"  Closing balance, all in base : "
                    f"{self.terminal_base_equivalent:>16,.2f}"
                )
                lines.append(
                    f"  External inflow required     : "
                    f"{self.shortfall:>16,.2f}"
                )
            if self.shortfall_detail:
                lines.append("")
                lines.append(f"  {'CCY':<14} {'Closing, in base':>20}")
                lines.append(f"  {'-' * 35}")
                for ccy, amount in sorted(self.shortfall_detail.items()):
                    flag = " \u25c4 DEFICIT" if amount < -0.01 else ""
                    lines.append(f"  {ccy:<14} {amount:>20,.2f}{flag}")
            lines.append("")
            lines.append(f"  {'!' * (w - 4)}")

        # ── No feasible plan ──
        elif self.status == "Infeasible":
            lines.append("")
            lines.append(f"  {'!' * (w - 4)}")
            lines.append("  NO FEASIBLE PLAN")
            lines.append(f"  {'!' * (w - 4)}")
            lines.append("")
            lines.append(
                "  No set of trades satisfies every active constraint. This is"
            )
            lines.append(
                "  a constraint conflict, not a shortage of cash: the model"
            )
            lines.append(
                "  permits a base-currency overdraft, so insufficient funds"
            )
            lines.append(
                "  would show as a negative closing balance, not as this."
            )
            lines.append("")
            lines.append(
                "  Review the active constraint flags. Switching one off and"
            )
            lines.append(
                "  re-solving will identify which is binding."
            )
            lines.append("")
            lines.append(f"  {'!' * (w - 4)}")

        # ── 1. Cash Ladder BEFORE Trades ──
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append("  CASH LADDER — BEFORE TRADES (do-nothing projection)")
        lines.append(f"  {'─' * (w - 4)}")
        lines.extend(format_ladder_pivoted(self.pre_trade_ladder))

        # ── 2. Projected Cash Flows ──
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append("  PROJECTED CASH FLOWS")
        lines.append(f"  {'─' * (w - 4)}")
        non_zero_flows = [cf for cf in self.cash_flows if abs(cf.amount) > 0.005]
        if non_zero_flows:
            lines.append(f"  {'CCY':<12} {'Day':<5} {'Amount':>14}")
            lines.append(f"  {'-' * 33}")
            for cf in non_zero_flows:
                lines.append(f"  {cf.ccy:<12} {cf.day:<5} {cf.amount:>14,.2f}")
        else:
            lines.append("  (no exogenous cash flows)")

        # ── 3. Recommended Trades ──
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append("  RECOMMENDED TRADES")
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
            lines.append("  NO TRADES RECOMMENDED")

        # ── 4. Cash Ladder AFTER Trades ──
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append("  CASH LADDER — AFTER TRADES (optimized)")
        lines.append(f"  {'─' * (w - 4)}")
        lines.extend(format_balances_pivoted(self.balances))

        lines.append(f"\n{'=' * w}\n")
        return "\n".join(lines)

    def print_summary(self) -> None:
        """Print formatted summary to stdout."""
        print(self.format_summary())

    # ── After-cost cash ladder ──────────────

    def compute_after_cost_balances(
        self,
        cfg: Config,
    ) -> List[BalanceSnapshot]:
        """Build an after-cost cash ladder that adjusts each currency balance
        for interest accrual and deducts trade costs from base.

        Each currency's credit balance earns interest at **its own** credit
        rate.  Each currency's debit balance is charged at its own debit rate.
        Interest is expressed **in that currency** (not converted to base),
        so USD balances grow in USD, GBP balances grow in GBP, etc.

        Trade costs (commission) are deducted from the base currency
        balance on the trade's settlement day.

        This is a **display-only** view — it does NOT affect the objective
        function or the optimizer in any way.

        Parameters
        ----------
        cfg : Config
            Configuration that provides carry rates, FX quotes, and
            commission tiers.

        Returns
        -------
        list of BalanceSnapshot
            After-cost balance snapshots, one per (ccy, day).
        """
        base = cfg.base_ccy
        base_label = f"{base} (Base)"
        horizon = cfg.horizon_days

        # Index raw post-trade balances: (ccy_label, day) -> balance
        bal_idx: Dict[Tuple[str, int], float] = {}
        ccy_labels: List[str] = []
        for b in self.balances:
            bal_idx[(b.ccy, b.day)] = b.balance
            if b.ccy not in ccy_labels:
                ccy_labels.append(b.ccy)

        # ── Compute per-trade costs keyed by settlement day ──
        trade_cost_by_settle: Dict[int, float] = {}

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

        for t in self.trades:
            # Use direction-specific rate for the trade's tenor
            if t.direction.value == "BUY":
                fx_rate = cfg.fx_ask(t.ccy, t.tenor)
            else:
                fx_rate = cfg.fx_bid(t.ccy, t.tenor)
            notional_base = fx_rate * t.amount
            commission = fx_rate * _tiered_commission(t.amount)
            cost = commission
            trade_cost_by_settle[t.settle_day] = (
                trade_cost_by_settle.get(t.settle_day, 0.0) + cost
            )

        # ── Build after-cost balances with per-currency interest ──
        result: List[BalanceSnapshot] = []

        for ccy_label in ccy_labels:
            raw_ccy = ccy_label.split(" ")[0]
            credit_rate = cfg.credit_carry_bps_per_day.get(raw_ccy, 0.0) / 1e4
            debit_rate = cfg.debit_carry_bps_per_day.get(raw_ccy, 0.0) / 1e4
            is_base = (ccy_label == base_label)

            cumulative_interest = 0.0

            for d in range(horizon):
                raw_bal = bal_idx.get((ccy_label, d), 0.0)

                if d > 0:
                    prev_after_cost = (
                        bal_idx.get((ccy_label, d - 1), 0.0)
                        + cumulative_interest
                    )

                    if prev_after_cost > 0:
                        cumulative_interest += prev_after_cost * credit_rate
                    elif prev_after_cost < 0:
                        cumulative_interest -= abs(prev_after_cost) * debit_rate

                adjusted = raw_bal + cumulative_interest
                if is_base:
                    cost_on_day = trade_cost_by_settle.get(d, 0.0)
                    adjusted -= cost_on_day
                    cumulative_interest -= cost_on_day

                adj_r = round(adjusted, 2)
                result.append(BalanceSnapshot(
                    ccy=ccy_label,
                    day=d,
                    balance=adj_r,
                    credit=round(max(adj_r, 0.0), 2),
                    debit=round(abs(min(adj_r, 0.0)), 2),
                ))

        return result

    def format_after_cost_ladder(self, cfg: Config) -> str:
        """Return a formatted after-cost cash ladder as a string."""
        ac_balances = self.compute_after_cost_balances(cfg)
        w = 70
        lines: List[str] = []
        lines.append(f"\n  {'─' * (w - 4)}")
        lines.append(
            "  CASH LADDER — AFTER COSTS "
            "(base ccy adjusted for all economic costs)"
        )
        lines.append(f"  {'─' * (w - 4)}")
        lines.extend(format_balances_pivoted(ac_balances))
        return "\n".join(lines)

    def print_after_cost_ladder(self, cfg: Config) -> None:
        """Print the after-cost cash ladder to stdout."""
        print(self.format_after_cost_ladder(cfg))

    def _compute_balance_costs(
        self, cfg: Config,
    ) -> Tuple[float, float, float]:
        """Compute the three balance-dependent cost components.

        Returns ``(credit_carry, debit_carry, fx_exposure, terminal_unwind)``
        — all in base currency, using the same formulas as the optimizer
        objective.
        """
        bal_idx: Dict[Tuple[str, int], BalanceSnapshot] = {
            (b.ccy, b.day): b for b in self.balances
        }

        base = cfg.base_ccy
        base_label = f"{base} (Base)"
        base_credit_rate = cfg.credit_carry_bps_per_day[base]
        base_debit_rate = cfg.debit_carry_bps_per_day[base]
        fx_start = cfg.fx_exposure_from_day
        carry_start = cfg.credit_carry_start_day
        # Taken from the balances rather than Config.currencies: a currency
        # discovered from the cash flows is in the plan but not in that list,
        # and omitting it here would break the reconciliation.
        foreign_ccys = [c for c in dict.fromkeys(b.ccy for b in self.balances)
                        if c != base and c != base_label]

        credit_carry = 0.0
        debit_carry = 0.0
        fx_exposure = 0.0

        # Base currency debit carry
        for d in range(cfg.horizon_days):
            snap = bal_idx.get((base_label, d))
            if snap is not None:
                debit_carry += base_debit_rate / 1e4 * snap.debit

        # Foreign currency carry / debit / exposure
        for ccy in foreign_ccys:
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
                bp = snap.credit
                bn = snap.debit

                # Credit carry: value positive balance at bid (exit rate)
                if d >= carry_start:
                    credit_carry += net_carry_bps / 1e4 * spot_bid * bp

                # Debit carry: value negative balance at ask (entry rate)
                debit_carry += debit_rate_bps / 1e4 * spot_ask * bn

                # FX exposure: value at spot mid (risk-neutral)
                if d >= fx_start:
                    fx_exposure += (
                        cfg.fx_exposure_bps_per_day / 1e4
                        * spot_mid * (bp + bn)
                    )

        # Cost of unwinding whatever is still held at the horizon.
        terminal_unwind = 0.0
        if cfg.value_trade_rates:
            last = cfg.horizon_days - 1
            unwind_bps = cfg.commission_tiers[0].rate_bps / 1e4
            for ccy in foreign_ccys:
                snap = bal_idx.get((ccy, last))
                if snap is None:
                    continue
                s_bid = cfg.fx_spot_bid(ccy)
                s_ask = cfg.fx_spot_ask(ccy)
                s_mid = cfg.fx_spot_mid(ccy)
                terminal_unwind += (
                    ((s_mid - s_bid) + unwind_bps * s_bid) * snap.credit
                    + ((s_ask - s_mid) + unwind_bps * s_ask) * snap.debit
                )

        return credit_carry, debit_carry, fx_exposure, terminal_unwind

    # ── Tenor cost decomposition helpers ──────

    @staticmethod
    def _tiered_commission_foreign(
        amount_foreign: float, cfg: Config,
    ) -> float:
        """Compute tiered commission in foreign currency units."""
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

    def _tenor_decomposition_row(
        self,
        cfg: Config,
        ccy: str,
        trade_day: int,
        tenor: str,
        direction_value: str,
        n_foreign: float,
        best_rate: float,
    ) -> Dict[str, float]:
        """Compute a single tenor-row for the cost decomposition table.

        The carry model mirrors the optimizer's objective function exactly:

        - **Carry differential cost** =
          ``(base_credit_rate - foreign_credit_rate) / 1e4 * spot_bid
          * N_foreign * days_held_as_foreign``
          (only days >= ``carry_start``).

        - **Debit carry** =
          ``debit_rate / 1e4 * spot_ask * N_foreign
          * days_held_as_negative_foreign``

        - **FX exposure** =
          ``exposure_bps / 1e4 * spot_mid * N_foreign
          * days_held`` (only days >= ``fx_start``).

        - **Commission** =
          ``tiered_commission_foreign * direction_rate``

        The **Obj Cost** column is the sum of commission + carry
        differential + debit carry + FX exposure — the exact value the
        optimizer minimises.  The selected tenor will always have the
        lowest Obj Cost.

        The **Net Realised Value** = Baseline Proceeds + FX Rate Cost
        − Obj Cost.  Higher is better.

        All values are in base currency.
        """
        horizon = cfg.horizon_days
        settle_lag = cfg.tenors[tenor]
        settle_day = trade_day + settle_lag
        carry_start = cfg.credit_carry_start_day
        fx_start = cfg.fx_exposure_from_day

        # Direction-appropriate rate
        if direction_value == "SELL":
            fx_rate = cfg.fx_bid(ccy, tenor)
        else:
            fx_rate = cfg.fx_ask(ccy, tenor)

        # Spot rates used by the optimizer for carry / exposure valuation
        spot_bid = cfg.fx_spot_bid(ccy)
        spot_ask = cfg.fx_spot_ask(ccy)
        spot_mid = cfg.fx_spot_mid(ccy)

        # Daily rates in bps (NOT fractional — divide by 1e4 when used)
        base_credit_bps = cfg.credit_carry_bps_per_day.get(cfg.base_ccy, 0.0)
        foreign_credit_bps = cfg.credit_carry_bps_per_day.get(ccy, 0.0)
        net_carry_bps = base_credit_bps - foreign_credit_bps
        debit_bps = cfg.debit_carry_bps_per_day.get(ccy, 0.0)
        exposure_bps = cfg.fx_exposure_bps_per_day

        # ── Baseline proceeds ──
        baseline = best_rate * n_foreign

        # ── FX rate cost ──
        fx_rate_cost = (fx_rate - best_rate) * n_foreign

        # ── Commission (always a cost → positive in Obj Cost) ──
        comm_foreign = self._tiered_commission_foreign(n_foreign, cfg)
        commission_cost = fx_rate * comm_foreign  # positive

        # ── Carry differential cost ──
        # The optimizer penalises positive foreign balance on each day.
        # For a SELL: N_foreign sits as positive foreign balance from
        # the trade day until the settlement day (settle_lag days).
        # After settlement the foreign balance drops to 0.
        # For a BUY: the foreign balance is negative before settlement
        # → carry differential doesn't apply (bal_pos = 0), but debit
        # carry does.
        #
        # Days the foreign balance is positive due to this trade:
        if direction_value == "SELL":
            # Foreign positive balance exists on days
            # [trade_day .. settle_day-1], i.e. settle_lag days
            carry_days = sum(
                1 for d in range(trade_day, settle_day)
                if d < horizon and d >= carry_start
            )
            carry_diff_cost = net_carry_bps / 1e4 * spot_bid * n_foreign * carry_days
        else:
            carry_diff_cost = 0.0

        # ── Debit carry (foreign overdraft) ──
        if direction_value == "BUY":
            debit_days = sum(
                1 for d in range(trade_day, settle_day)
                if d < horizon
            )
            debit_cost = debit_bps / 1e4 * spot_ask * n_foreign * debit_days
        else:
            debit_cost = 0.0

        # ── FX exposure penalty ──
        if direction_value == "SELL":
            exposure_days = sum(
                1 for d in range(trade_day, settle_day)
                if d < horizon and d >= fx_start
            )
        else:
            exposure_days = sum(
                1 for d in range(trade_day, settle_day)
                if d < horizon and d >= fx_start
            )
        fx_exposure_cost = exposure_bps / 1e4 * spot_mid * n_foreign * exposure_days

        # ── Obj Cost (what the optimizer minimises) ──
        obj_cost = commission_cost + carry_diff_cost + debit_cost + fx_exposure_cost

        # ── Net Realised Value ──
        # Baseline + FX rate adjustment − all costs
        net_realised = baseline + fx_rate_cost - obj_cost

        return {
            "tenor": tenor,
            "settle_day": settle_day,
            "fx_rate": fx_rate,
            "baseline_proceeds": baseline,
            "fx_rate_cost": fx_rate_cost,
            "commission_cost": commission_cost,
            "carry_diff_cost": carry_diff_cost,
            "debit_cost": debit_cost,
            "fx_exposure_cost": fx_exposure_cost,
            "obj_cost": obj_cost,
            "net_realised_value": net_realised,
        }

    def format_cost(self, cfg: Config) -> str:
        """Return a cost attribution table with one row per executed trade.

        Each row shows the cost breakdown for the trade at its selected
        tenor.  A totals row at the bottom sums all components.

        Columns
        -------
        - **CCY** — foreign currency.
        - **Dir** — BUY or SELL.
        - **Amount** — trade notional in foreign currency.
        - **Day** — trade day.
        - **Tenor** — settlement tenor chosen by the optimizer.
        - **Stl** — settlement day.
        - **Rate** — FX rate used (bid for sells, ask for buys).
        - **Proceeds** — ``rate × notional`` (base ccy received/paid).
        - **Commission** — tiered commission cost in base ccy.
        - **Carry Diff** — credit carry differential cost.
        - **Debit** — foreign debit carry cost.
        - **FX Expos** — FX exposure penalty.
        - **Trade Cost** — ``Commission + Carry Diff + Debit + FX Expos``.
        """
        lines: List[str] = []
        col = 14  # numeric column width
        w = 148

        lines.append(f"\n{'=' * w}")
        lines.append("  COST ATTRIBUTION BY TRADE")
        if self.total_cost is not None:
            lines.append(f"  TOTAL OPTIMIZER COST : {self.total_cost:,.4f} (base ccy)")
        lines.append(f"{'=' * w}")

        if not self.trades:
            lines.append("  NO TRADES — nothing to decompose.")
            lines.append(f"{'=' * w}\n")
            return "\n".join(lines)

        # ── Column headers ──
        # Only commission and rate-vs-reference are attributable to an
        # individual trade.  Carry and exposure depend on the balances a
        # plan leaves behind, not on any single ticket; the previous version
        # of this table apportioned them per trade anyway and then added
        # them to the balance-level figures, which is why it never
        # reconciled against the objective (finding F21).
        hdr = (
            f"  {'CCY':<5} {'Dir':<5} {'Amount':>14} {'Day':>4} "
            f"{'Tenor':<6} {'Stl':>4} {'Rate':>10}"
            f"  {'Proceeds':>{col}}"
            f"  {'Commission':>{col}}"
            f"  {'Rate vs ref':>{col}}"
            f"  {'Trade Cost':>{col}}"
        )
        lines.append(hdr)
        lines.append(f"  {'-' * (w - 4)}")

        tot_proceeds = 0.0
        tot_commission = 0.0
        tot_spread = 0.0

        for t in self.trades:
            n_foreign = t.amount
            dir_val = t.direction.value
            if dir_val == "SELL":
                fx_rate = cfg.fx_bid(t.ccy, t.tenor)
                spread = (cfg.fx_spot_mid(t.ccy) - fx_rate) * n_foreign
            else:
                fx_rate = cfg.fx_ask(t.ccy, t.tenor)
                spread = (fx_rate - cfg.fx_spot_mid(t.ccy)) * n_foreign
            if not cfg.value_trade_rates:
                spread = 0.0

            proceeds = fx_rate * n_foreign
            commission = fx_rate * self._tiered_commission_foreign(n_foreign, cfg)

            tot_proceeds += proceeds
            tot_commission += commission
            tot_spread += spread

            lines.append(
                f"  {t.ccy:<5} {dir_val:<5} {n_foreign:>14,.0f} "
                f"{'D' + str(t.day):>4} "
                f"{t.tenor:<6} {'D' + str(t.settle_day):>4} "
                f"{fx_rate:>10.6f}"
                f"  {proceeds:>{col},.2f}"
                f"  {commission:>{col},.2f}"
                f"  {spread:>{col},.2f}"
                f"  {commission + spread:>{col},.2f}"
            )

        tot_trade_cost = tot_commission + tot_spread
        lines.append(f"  {'-' * (w - 4)}")
        pad = 5 + 5 + 14 + 4 + 6 + 4 + 10 + 6
        lines.append(
            f"  {'TOTAL':>{pad}}"
            f"  {tot_proceeds:>{col},.2f}"
            f"  {tot_commission:>{col},.2f}"
            f"  {tot_spread:>{col},.2f}"
            f"  {tot_trade_cost:>{col},.2f}"
        )

        # ── Balance-dependent costs ──
        credit_carry, debit_carry, fx_exposure, terminal_unwind = (
            self._compute_balance_costs(cfg))
        balance_cost = credit_carry + debit_carry + fx_exposure + terminal_unwind

        lines.append("")
        lines.append(f"  {'─' * (w - 4)}")
        lines.append("  BALANCE-DEPENDENT COSTS (not attributable to one trade)")
        lines.append(f"  {'─' * (w - 4)}")
        lines.append(f"    Credit carry differential (all ccys)  : {credit_carry:>{col},.4f}")
        lines.append(f"    Debit carry (all ccys incl. base)     : {debit_carry:>{col},.4f}")
        lines.append(f"    FX exposure penalty                   : {fx_exposure:>{col},.4f}")
        if cfg.value_trade_rates:
            lines.append(f"    Terminal unwind of residual position  : {terminal_unwind:>{col},.4f}")
        lines.append(f"    {'─' * 52}")
        lines.append(f"    Balance-dependent total               : {balance_cost:>{col},.4f}")

        lines.append("")
        lines.append(f"  {'─' * (w - 4)}")
        lines.append("  RECONCILIATION")
        lines.append(f"  {'─' * (w - 4)}")
        lines.append(f"    Trade costs (commission + rate vs ref) : {tot_trade_cost:>{col},.4f}")
        lines.append(f"    Balance-dependent costs                : {balance_cost:>{col},.4f}")
        lines.append(f"    {'─' * 53}")
        lines.append(f"    Sum of the parts                       : {tot_trade_cost + balance_cost:>{col},.4f}")
        if self.total_cost is not None:
            residual = self.total_cost - (tot_trade_cost + balance_cost)
            lines.append(f"    Optimizer objective                    : {self.total_cost:>{col},.4f}")
            lines.append(f"    Unexplained residual                   : {residual:>{col},.4f}")

        if self.net_terminal_wealth is not None:
            lines.append("")
            lines.append(f"  {'─' * (w - 4)}")
            lines.append("  WHAT THE PLAN DELIVERS")
            lines.append(f"  {'─' * (w - 4)}")
            lines.append(f"    Book at reference rates, frictionless  : {self.reference_value:>{col},.4f}")
            lines.append(f"    Less total cost                        : {self.total_cost:>{col},.4f}")
            lines.append(f"    {'─' * 53}")
            lines.append(f"    Net base currency at end of horizon    : {self.net_terminal_wealth:>{col},.4f}")

        lines.append(f"\n{'=' * w}\n")
        return "\n".join(lines)

    def print_cost(self, cfg: Config) -> None:
        """Print a per-trade cost attribution breakdown."""
        print(self.format_cost(cfg))

