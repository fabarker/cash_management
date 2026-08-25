"""
CashOptimizer — LP/MIP solver for optimal FX trade scheduling.

Extracted from the monolithic ``cash_optimizer_poc.py`` for clarity.
Contains only the ``CashOptimizer`` class.
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional, Tuple

import pulp

from scripts.cash_optimizer_poc.models import (
    BalanceSnapshot,
    CashFlowEntry,
    CashFlowSet,
    CashLadderEntry,
    Config,
    Direction,
    FXTenorQuote,
    Trade,
)
from scripts.cash_optimizer_poc.result import Result

log = logging.getLogger("cash_optimizer_poc")


class SolverFailure(RuntimeError):
    """The solver did not return a solved model.

    Raised for any terminal status that is neither ``Optimal`` nor
    ``Infeasible`` — a time limit, an unbounded model, or an internal solver
    error.  Previously these returned an empty ``Result``: no trades, no
    balances, no cost and no exception, which a caller reading
    ``result.trades`` could not tell apart from "there was nothing worth
    doing".  Failing loudly is the point.

    Subclasses ``RuntimeError`` so existing handlers still catch it.

    Attributes
    ----------
    status : str
        The solver's terminal status, e.g. ``"Not Solved"``.
    solver : str or None
        Which solver produced it.
    time_limit : int or None
        The limit in force, which is the usual cause.
    """

    def __init__(self, status, solver=None, time_limit=None):
        self.status = status
        self.solver = solver
        self.time_limit = time_limit
        if status == "Not Solved":
            limit = (f"The {time_limit}s time limit" if time_limit
                     else "A time limit")
            hint = (f"{limit} is the usual cause: raise time_limit, shorten "
                    f"the horizon, or install HiGHS (pip install highspy), "
                    f"which this model solves considerably faster.")
        else:
            hint = "Check the model configuration and the solver log."
        super().__init__(
            f"Solver returned {status!r} — the model was not solved to "
            f"optimality, so there is no plan to report. {hint}"
        )


class CashOptimizer:
    """
    Builds and solves the cash management LP/MIP.

    Decision variables per (foreign_ccy, day, tenor):
        buy_X   — amount of foreign ccy purchased (base -> foreign)
        sell_X  — amount of foreign ccy sold (foreign -> base)
        act_buy — binary: 1 if buy_X > 0
        act_sell— binary: 1 if sell_X > 0

    Per (currency, day) — for ALL active currencies including base:
        bal     — end-of-day balance
        bal_pos — positive part of balance (credit)
        bal_neg — negative part of balance (debit)

    """

    def __init__(
        self,
        cfg: Config,
        cashflows: CashFlowSet,
        opening_balances: Optional[Dict[str, float]] = None,
    ):
        self.cfg = cfg
        self.cf = cashflows
        self.opening = opening_balances or {}
        self._check_opening_balances()
        self.currencies = self._resolve_currencies()
        self.foreign_ccys = [c for c in self.currencies if c != cfg.base_ccy]
        self._check_market_data()

        fallback = [c for c in self.cfg.currencies_on_default_day_count()
                    if c in self.currencies]
        if fallback:
            log.warning(
                "No day-count basis given for %s; falling back to ACT/%d. A "
                "wrong basis mis-states that currency's interest by about "
                "1.4%%, charged in full on an overdraft. Add it to "
                "Config.day_count_basis.",
                ", ".join(fallback), self.cfg.default_day_count,
            )

        self.active_foreign_ccys = self._compute_active_foreign_ccys()
        self.balance_bounds = self._compute_balance_bounds()
        self._check_balance_bounds()

        self._prob: Optional[pulp.LpProblem] = None
        self._vars: dict = {}
        self._solved: bool = False

    # ── Input validation and currency discovery ──

    def _check_opening_balances(self) -> None:
        """Reject a balance that is not a finite number.

        NaN is the dangerous case.  Every comparison against it is false, so
        ``abs(balance) > 1e-9`` answers "nothing here" for a figure that
        actually means "I could not read this" — and the currency is dropped
        from the model without a word.  A feed that returns NaN for missing
        data would silently become a feed that reports no exposure.
        """
        for ccy, amount in self.opening.items():
            if not math.isfinite(amount):
                raise ValueError(
                    f"Opening balance for {ccy} is {amount!r}, which is not a "
                    f"finite number. A missing or unreadable balance must not "
                    f"be passed in as NaN: it would be read as an empty "
                    f"account and {ccy} would be dropped from the model with "
                    f"no warning. Supply the real balance, or omit {ccy}."
                )

    def _resolve_currencies(self) -> List[str]:
        """The currencies actually in play, discovered rather than declared.

        ``Config.currencies`` is a statement of intent — the currencies you
        want modelled even if nothing happens in them.  It is not a reliable
        record of what turned up in the data: a projection feed can perfectly
        well deliver an obligation in a currency nobody pre-declared, and
        before this that obligation was invisible everywhere.

        The universe is therefore the declared list plus anything appearing in
        the opening balances or the cash flows.  The caller's ``Config`` is
        left untouched; what a discovered currency still has to bring with it
        is its market data, which ``_check_market_data`` insists on.
        """
        universe = list(self.cfg.currencies)
        provenance: Dict[str, str] = {}

        def note(ccy: str, where: str) -> None:
            if ccy not in universe:
                universe.append(ccy)
                provenance.setdefault(ccy, where)

        for ccy in self.opening:
            note(ccy, "an opening balance")
        for ccy, day, _ in self.cf.all_entries():
            note(ccy, f"a cash flow on day {day}")

        if self.cfg.base_ccy not in universe:
            universe.append(self.cfg.base_ccy)

        self._discovered = provenance
        if provenance:
            log.info(
                "Currencies found in the data but not declared in "
                "Config.currencies: %s",
                ", ".join(f"{c} (from {w})" for c, w in provenance.items()),
            )
        return universe

    def _check_market_data(self) -> None:
        """Every foreign currency in play needs quotes and carry rates.

        This is where flexibility stops.  Which currencies exist can be read
        off the data; an exchange rate cannot be inferred from anything, and a
        model that silently invented one would be worse than a model that
        refuses to run.
        """
        problems: List[str] = []
        for ccy in self.foreign_ccys:
            missing = []
            quotes = self.cfg.fx_quotes.get(ccy)
            if not quotes:
                missing.append("fx_quotes")
            else:
                absent = sorted(set(self.cfg.tenors) - set(quotes))
                if absent:
                    missing.append(f"fx_quotes for tenor(s) {absent}")
            if ccy not in self.cfg.credit_carry_pa:
                missing.append("credit_carry_pa")
            if ccy not in self.cfg.debit_carry_pa:
                missing.append("debit_carry_pa")
            if missing:
                origin = self._discovered.get(ccy)
                where = (f" — it arrived via {origin}, and was not declared in "
                         f"Config.currencies" if origin else "")
                problems.append(f"  {ccy}: missing {', '.join(missing)}{where}")

        if problems:
            raise ValueError(
                "Market data is missing for currencies the model has to price:"
                "\n" + "\n".join(problems) + "\n"
                "A currency can be discovered from the cash flows, but its "
                "rates cannot be. Supply fx_quotes and carry rates for each of "
                "the above, or remove the exposure."
            )

    # ── Balance ceiling ────────────────────

    def _gross_cash_base(self) -> float:
        """Total absolute cash in the scenario, valued in base currency.

        Sums the opening balance and every cash flow, in absolute terms,
        across all currencies.  This is the most cash that could pass
        through the account over the horizon, so no balance arising from a
        genuine cash-management plan can exceed it.
        """
        base = self.cfg.base_ccy
        total = abs(self.opening.get(base, 0.0)) + sum(
            abs(self.cf.get(base, d)) for d in range(self.cfg.horizon_days)
        )
        for ccy in self.foreign_ccys:
            gross = abs(self.opening.get(ccy, 0.0)) + sum(
                abs(self.cf.get(ccy, d)) for d in range(self.cfg.horizon_days)
            )
            if gross:
                total += gross * self.cfg.fx_spot_mid(ccy)
        return total

    def _compute_balance_bounds(self) -> Dict[str, float]:
        """Per-currency ceiling on ``|balance|``, in each currency's units.

        The balance decomposition (``_add_balance_decomposition``) needs a
        constant that is larger than any balance the model may legitimately
        take.  That constant is a hard position limit, so it must be big
        enough not to exclude a sensible plan — and small enough that the
        solver's absolute feasibility tolerances still resolve the model.

        ``Config.max_balance`` sets it explicitly (in base ccy).  Otherwise
        it is derived as total gross cash x ``Config.balance_headroom``,
        floored at ``Config.min_balance_ceiling``, then converted into each
        currency's own units at spot mid.
        """
        base = self.cfg.base_ccy
        if self.cfg.max_balance is not None:
            ceiling_base = float(self.cfg.max_balance)
        else:
            ceiling_base = self._gross_cash_base() * self.cfg.balance_headroom
        ceiling_base = max(ceiling_base, self.cfg.min_balance_ceiling)

        bounds = {base: ceiling_base}
        for ccy in self.foreign_ccys:
            bounds[ccy] = ceiling_base / self.cfg.fx_spot_mid(ccy)

        log.debug(
            "Balance ceiling: %.2f %s (%s), per-ccy %s",
            ceiling_base, base,
            "explicit" if self.cfg.max_balance is not None else "derived",
            {c: round(v, 2) for c, v in bounds.items()},
        )
        return bounds

    def _check_balance_bounds(self) -> None:
        """Reject a scenario whose own cash ladder breaches the ceiling.

        Without this, an undersized ``Config.max_balance`` produces a model
        that is infeasible before a single trade is considered — and the
        solver reports that as a bare "Infeasible", which reads to the user
        as a funding shortfall rather than a configuration error.
        """
        for ccy in [self.cfg.base_ccy] + self.foreign_ccys:
            bound = self.balance_bounds[ccy]
            running = self.opening.get(ccy, 0.0)
            for d in range(self.cfg.horizon_days):
                running += self.cf.get(ccy, d)
                if abs(running) > bound:
                    raise ValueError(
                        f"Balance ceiling too low for {ccy}: the do-nothing "
                        f"balance on day {d} is {running:,.2f} but the ceiling "
                        f"is {bound:,.2f}. The model cannot represent this "
                        f"balance, so it would be infeasible before any trade "
                        f"is considered. Raise Config.max_balance (currently "
                        f"{self.cfg.max_balance!r}, in base ccy) or "
                        f"Config.balance_headroom (currently "
                        f"{self.cfg.balance_headroom})."
                    )

    def _compute_active_foreign_ccys(self) -> List[str]:
        """Return foreign currencies that have a non-zero opening balance
        or at least one non-zero cash flow within the horizon."""
        active = []
        for ccy in self.foreign_ccys:
            opening = self.opening.get(ccy, 0.0)
            if abs(opening) > 1e-9:
                active.append(ccy)
                continue
            has_cf = any(
                abs(self.cf.get(ccy, d)) > 1e-9
                for d in range(self.cfg.horizon_days)
            )
            if has_cf:
                active.append(ccy)
        return active

    # ── Variable creation ──────────────────

    def _create_variables(self) -> None:
        v = {}
        days = range(self.cfg.horizon_days)
        tenors = list(self.cfg.tenors.keys())
        base = self.cfg.base_ccy

        for d in days:
            v[("bal", base, d)] = pulp.LpVariable(f"bal_{base}_{d}", cat="Continuous")
            v[("bal_pos", base, d)] = pulp.LpVariable(f"balP_{base}_{d}", lowBound=0)
            v[("bal_neg", base, d)] = pulp.LpVariable(f"balN_{base}_{d}", lowBound=0)
            v[("bal_sign", base, d)] = pulp.LpVariable(f"balZ_{base}_{d}", cat="Binary")

        for ccy in self.active_foreign_ccys:
            for d in days:
                v[("bal", ccy, d)] = pulp.LpVariable(f"bal_{ccy}_{d}", cat="Continuous")
                v[("bal_pos", ccy, d)] = pulp.LpVariable(f"balP_{ccy}_{d}", lowBound=0)
                v[("bal_neg", ccy, d)] = pulp.LpVariable(f"balN_{ccy}_{d}", lowBound=0)
                v[("bal_sign", ccy, d)] = pulp.LpVariable(f"balZ_{ccy}_{d}", cat="Binary")

                for tenor in tenors:
                    settle_day = d + self.cfg.tenors[tenor]
                    if settle_day >= self.cfg.horizon_days:
                        continue

                    tiers = self.cfg.commission_tiers
                    for k, tier in enumerate(tiers):
                        capacity = tier.threshold if k == 0 else (
                            tier.threshold - tiers[k - 1].threshold
                        )
                        v[("buy_seg", ccy, d, tenor, k)] = pulp.LpVariable(
                            f"buyS{k}_{ccy}_{d}_{tenor}", lowBound=0, upBound=capacity,
                        )
                        v[("sell_seg", ccy, d, tenor, k)] = pulp.LpVariable(
                            f"sellS{k}_{ccy}_{d}_{tenor}", lowBound=0, upBound=capacity,
                        )
                        if k < len(tiers) - 1:
                            v[("fill_buy", ccy, d, tenor, k)] = pulp.LpVariable(
                                f"fillB{k}_{ccy}_{d}_{tenor}", cat="Binary",
                            )
                            v[("fill_sell", ccy, d, tenor, k)] = pulp.LpVariable(
                                f"fillS{k}_{ccy}_{d}_{tenor}", cat="Binary",
                            )

                    v[("act_buy", ccy, d, tenor)] = pulp.LpVariable(
                        f"actB_{ccy}_{d}_{tenor}", cat="Binary",
                    )
                    v[("act_sell", ccy, d, tenor)] = pulp.LpVariable(
                        f"actS_{ccy}_{d}_{tenor}", cat="Binary",
                    )


        self._vars = v

    # ── Helpers ────────────────────────────

    def _v(self, *key):
        return self._vars.get(key)

    def _total_buy(self, ccy: str, d: int, tenor: str):
        segs = [
            self._v("buy_seg", ccy, d, tenor, k)
            for k in range(len(self.cfg.commission_tiers))
        ]
        segs = [s for s in segs if s is not None]
        if not segs:
            return None
        return pulp.lpSum(segs)

    def _total_sell(self, ccy: str, d: int, tenor: str):
        segs = [
            self._v("sell_seg", ccy, d, tenor, k)
            for k in range(len(self.cfg.commission_tiers))
        ]
        segs = [s for s in segs if s is not None]
        if not segs:
            return None
        return pulp.lpSum(segs)

    def _has_trade_vars(self, ccy: str, d: int, tenor: str) -> bool:
        return self._v("buy_seg", ccy, d, tenor, 0) is not None

    def _net_settlement(self, ccy: str, day: int):
        """Net foreign-ccy settlement on *day* (exogenous + trade buys − sells)."""
        total = self.cf.get(ccy, day)
        tenors = self.cfg.tenors
        for tenor, lag in tenors.items():
            trade_day = day - lag
            if trade_day < 0 or trade_day >= self.cfg.horizon_days:
                continue
            buy_var = self._total_buy(ccy, trade_day, tenor)
            sell_var = self._total_sell(ccy, trade_day, tenor)
            if buy_var is not None:
                total += buy_var
            if sell_var is not None:
                total -= sell_var
        return total

    # ── Constraints ────────────────────────

    def _add_balance_evolution(self, prob: pulp.LpProblem) -> None:
        base = self.cfg.base_ccy
        for d in range(self.cfg.horizon_days):
            prev = (
                self.opening.get(base, 0.0) if d == 0
                else self._v("bal", base, d - 1)
            )
            net = self.cf.get(base, d)
            for ccy in self.active_foreign_ccys:
                for tenor, lag in self.cfg.tenors.items():
                    trade_day = d - lag
                    if trade_day < 0 or trade_day >= self.cfg.horizon_days:
                        continue
                    buy_var = self._total_buy(ccy, trade_day, tenor)
                    sell_var = self._total_sell(ccy, trade_day, tenor)
                    if buy_var is not None:
                        # Buying foreign costs base at the ask rate
                        net -= self.cfg.fx_ask(ccy, tenor) * buy_var
                    if sell_var is not None:
                        # Selling foreign generates base at the bid rate
                        net += self.cfg.fx_bid(ccy, tenor) * sell_var
            prob += (self._v("bal", base, d) == prev + net, f"bal_evol_{base}_{d}")

        for ccy in self.active_foreign_ccys:
            for d in range(self.cfg.horizon_days):
                prev = self.opening.get(ccy, 0.0) if d == 0 else self._v("bal", ccy, d - 1)
                net = self._net_settlement(ccy, d)
                prob += (self._v("bal", ccy, d) == prev + net, f"bal_evol_{ccy}_{d}")

    def _add_balance_decomposition(self, prob: pulp.LpProblem) -> None:
        all_tracked = [self.cfg.base_ccy] + self.active_foreign_ccys
        for ccy in all_tracked:
            # Per-currency ceiling in that currency's own units — NOT the
            # trade Big-M, which is orders of magnitude larger and made the
            # rows too badly scaled for the solver to resolve.
            M = self.balance_bounds[ccy]
            for d in range(self.cfg.horizon_days):
                bp = self._v("bal_pos", ccy, d)
                bn = self._v("bal_neg", ccy, d)
                z = self._v("bal_sign", ccy, d)
                prob += (self._v("bal", ccy, d) == bp - bn, f"bal_decomp_{ccy}_{d}")
                prob += (bp <= M * z, f"bal_comp_pos_{ccy}_{d}")
                prob += (bn <= M * (1 - z), f"bal_comp_neg_{ccy}_{d}")

    def _add_activation_linking(self, prob: pulp.LpProblem) -> None:
        for ccy in self.active_foreign_ccys:
            # Sized to the largest trade this scenario could justify, not to
            # the abstract config ceiling.  A trade has to be absorbed by a
            # balance (at most the ceiling) plus that day's cash flow (at
            # most total gross), so twice the ceiling is a safe bound — and
            # leaving this at max_trade instead keeps a constant orders of
            # magnitude above any real trade in the same rows as the T+0
            # sign variable, which is enough to stall the search.
            M = min(self.cfg.big_m, self.balance_bounds[ccy] * 2.0)
            floor = self.cfg.min_trade_in(ccy)
            for d in range(self.cfg.horizon_days):
                for tenor in self.cfg.tenors:
                    if not self._has_trade_vars(ccy, d, tenor):
                        continue
                    buy = self._total_buy(ccy, d, tenor)
                    sell = self._total_sell(ccy, d, tenor)
                    act_b = self._v("act_buy", ccy, d, tenor)
                    act_s = self._v("act_sell", ccy, d, tenor)
                    prob += (buy <= M * act_b, f"actLink_buy_{ccy}_{d}_{tenor}")
                    prob += (sell <= M * act_s, f"actLink_sell_{ccy}_{d}_{tenor}")

                    # Deal at least a whole ticket, or not at all.  The
                    # activation flags have always existed for this and
                    # never done it, which is why plans could contain a
                    # one-cent trade nobody could execute.
                    if floor > 0:
                        prob += (buy >= floor * act_b,
                                 f"minTrade_buy_{ccy}_{d}_{tenor}")
                        prob += (sell >= floor * act_s,
                                 f"minTrade_sell_{ccy}_{d}_{tenor}")

    def _add_commission_tier_linking(self, prob: pulp.LpProblem) -> None:
        tiers = self.cfg.commission_tiers
        if len(tiers) <= 1:
            return
        for ccy in self.active_foreign_ccys:
            for d in range(self.cfg.horizon_days):
                for tenor in self.cfg.tenors:
                    if not self._has_trade_vars(ccy, d, tenor):
                        continue
                    for k in range(len(tiers) - 1):
                        cap_k = tiers[k].threshold if k == 0 else (
                            tiers[k].threshold - tiers[k - 1].threshold
                        )
                        cap_next = tiers[k + 1].threshold - tiers[k].threshold
                        seg_b_k = self._v("buy_seg", ccy, d, tenor, k)
                        seg_b_next = self._v("buy_seg", ccy, d, tenor, k + 1)
                        fill_b = self._v("fill_buy", ccy, d, tenor, k)
                        if seg_b_k is not None and fill_b is not None:
                            prob += (seg_b_next <= cap_next * fill_b, f"tier_order_buy_{ccy}_{d}_{tenor}_{k}")
                            prob += (seg_b_k >= cap_k * fill_b, f"tier_full_buy_{ccy}_{d}_{tenor}_{k}")
                        seg_s_k = self._v("sell_seg", ccy, d, tenor, k)
                        seg_s_next = self._v("sell_seg", ccy, d, tenor, k + 1)
                        fill_s = self._v("fill_sell", ccy, d, tenor, k)
                        if seg_s_k is not None and fill_s is not None:
                            prob += (seg_s_next <= cap_next * fill_s, f"tier_order_sell_{ccy}_{d}_{tenor}_{k}")
                            prob += (seg_s_k >= cap_k * fill_s, f"tier_full_sell_{ccy}_{d}_{tenor}_{k}")

    def _add_no_loop_constraints(self, prob: pulp.LpProblem) -> None:
        """Forbid buying and selling one currency for the same settle day.

        Not an anti-carry rule.  A wash trade pays the spread twice and two
        commissions, so the objective rejects it wherever it is merely
        available -- at any spread, and even with commission set to zero.

        It binds where a wash trade is not chosen but *forced*.  Give the
        model a 1,000 need and a 50,000 minimum ticket and it will buy
        64,291 and sell 63,291 for the same value date: two legal tickets
        netting to an amount no dealer would accept as one.  That is a
        fiction which games the minimum, and the holding ceiling cannot see
        it, because the position nets to zero on every single day.

        The cumulative purchase cap this model used to carry hid that,
        since it bounded the gross buy rather than the net position.  The
        ceiling deliberately does not, so this rule now does real work.
        """
        for ccy in self.active_foreign_ccys:
            settle_groups: Dict[int, List[Tuple[int, str]]] = {}
            for d in range(self.cfg.horizon_days):
                for tenor, lag in self.cfg.tenors.items():
                    sd = d + lag
                    if sd >= self.cfg.horizon_days:
                        continue
                    settle_groups.setdefault(sd, []).append((d, tenor))
            for sd, pairs in settle_groups.items():
                buy_acts = []
                sell_acts = []
                for (td, tn) in pairs:
                    ab = self._v("act_buy", ccy, td, tn)
                    as_ = self._v("act_sell", ccy, td, tn)
                    if ab is not None:
                        buy_acts.append(ab)
                    if as_ is not None:
                        sell_acts.append(as_)
                if not buy_acts and not sell_acts:
                    continue
                z = pulp.LpVariable(f"noloop_z_{ccy}_sd{sd}", cat="Binary")
                n_buy = len(buy_acts)
                n_sell = len(sell_acts)
                prob += (pulp.lpSum(buy_acts) <= n_buy * z, f"noloop_buy_{ccy}_sd{sd}")
                prob += (pulp.lpSum(sell_acts) <= n_sell * (1 - z), f"noloop_sell_{ccy}_sd{sd}")

    def _add_terminal_sweep(self, prob: pulp.LpProblem) -> None:
        last = self.cfg.horizon_days - 1
        for ccy in self.active_foreign_ccys:
            prob += (self._v("bal", ccy, last) == 0, f"term_sweep_{ccy}")

    def _shortfall_profile(self, ccy: str) -> List[float]:
        """Funding shortfall per day if no trades were placed at all.

        Walks the do-nothing cash ladder and records, for each day, how far
        the balance falls below zero.  This is the honest measure of "how
        much of this currency do I actually need to buy", and unlike a
        horizon-wide net it does not let a receipt on Friday cancel a
        payment on Monday.

        If a required minimum balance is ever added, it belongs here as the
        level the balance is measured against — a floor imposed only as a
        constraint is infeasible, because to the anti-speculative cap a
        balance you must hold is currency you have no cash-flow need for.
        """
        return [max(0.0, -b) for b in self._do_nothing_ladder(ccy)]

    def _do_nothing_ladder(self, ccy: str) -> List[float]:
        """Closing balance per day if no trades were placed at all.

        Opening balance plus that currency's own cash flows, accumulated.
        Both the funding shortfall and the holding ceiling are read off
        this single walk, so the two can never disagree about what the
        account would look like left alone.  That disagreement is exactly
        what made a rule keyed on the cash flow file blind to an opening
        overdraft.
        """
        running = self.opening.get(ccy, 0.0)
        ladder: List[float] = []
        for d in range(self.cfg.horizon_days):
            running += self.cf.get(ccy, d)
            ladder.append(running)
        return ladder

    def _holding_ceiling(self, ccy: str) -> List[float]:
        """Most of ``ccy`` the plan may still be holding at end of day.

        The ceiling on day ``d`` is the deepest funding hole a trade dealt
        on day ``d`` could still settle against — that is, the worst point
        of the do-nothing ladder anywhere in ``d .. d + max_settlement_lag``.

        The reach window is what makes this an anti-speculation rule rather
        than a size limit.  If a payment can always be funded by dealing at
        the longest tenor, there is no honest reason to own the currency
        earlier than that; anything held sooner is a position, not funding,
        so the ceiling is simply zero until the obligation comes within
        dealing range.

        The hole alone is not the whole ceiling.  It is measured on the
        do-nothing ladder, which already contains the receipts — so a
        receipt that covers a later payment in the same currency digs no
        hole at all, and a ceiling built from the hole alone would forbid
        holding money the account was always going to spend.  The model's
        only way out is to sell the receipt and buy it back, paying two
        spreads and two commissions for nothing.  That is the mirror image
        of the netting defect ``_add_anti_speculative_constraint`` documents.

        So the ceiling has two parts, and they add:

        * the hole reachable from a trade dealt today — currency that must
          still be **acquired**, which is where the reach window belongs;
        * currency already **earmarked** — the part of what the account has
          been given that a remaining outflow will consume.

        The earmark is capped by receipts banked to date, which no amount of
        trading can inflate, so it cannot be used to manufacture a position:
        before the money arrives it contributes nothing.  It carries no
        reach window, deliberately.  Money already in hand against a known
        obligation is not a position, and forcing it to be sold and rebought
        is pure waste.

        One carve-out.  A known future receipt can be sold forward to the
        day it lands, so it is never actually held and the ceiling costs
        nothing.  Money already on the books before any trade could have
        been dealt against it is different — there was no earlier day to
        sell it on.  For the first ``max_settlement_lag`` days the ceiling
        therefore admits the do-nothing balance itself.

        That window is the settlement window, not the shortest tenor,
        deliberately.  Narrowing it to the shortest lag would force such a
        balance out at T+0 on day nought, which is not an anti-speculation
        gain — the position is contracted away the moment the trade is
        dealt — but does remove the choice of tenor, and with it any chance
        of taking the better forward rate.  It is also exactly the grace
        the sweep deadline this replaces allowed: for a currency with no
        cashflows at all its deadline was day ``max_settlement_lag``.

        The carve-out is capped by the do-nothing balance, which no amount
        of trading can inflate, so it cannot be used to build a position.

        ``ConstraintFlags.settle_on_need_only`` closes the reach window to
        the single day, so the balance may only be positive where the
        do-nothing ladder is itself overdrawn.  A purchase must then settle
        on the day the money leaves and the currency is never held
        overnight.  Only the acquisition term narrows: the earmark and the
        opening carve-out describe money the account was *given* rather
        than money it is acquiring, and taking the window off those would
        force a receipt to be sold and rebought for two commissions.
        """
        max_lag = max(self.cfg.tenors.values())
        reach = 0 if self.cfg.constraints.settle_on_need_only else max_lag
        horizon = self.cfg.horizon_days

        ladder = self._do_nothing_ladder(ccy)
        hole = [max(0.0, -b) for b in ladder]

        # Gross outflows still ahead of each day: what a holding could
        # legitimately still be spent on.
        still_to_pay = [0.0] * horizon
        running = 0.0
        for d in range(horizon - 1, -1, -1):
            flow = self.cf.get(ccy, d)
            if flow < 0.0:
                running += -flow
            still_to_pay[d] = running

        # Receipts banked by each day, including a positive opening balance.
        # This is what the account was *given*, and no amount of trading can
        # change it -- which is what stops the earmark below being used to
        # manufacture a position.  It deliberately does not net the outflows
        # off: once a payment has been funded by a purchase, the receipt that
        # follows is genuinely spare cash, and a measure that keeps
        # subtracting the already-settled debt reads it as zero and forces it
        # to be swept and rebought.
        received = [0.0] * horizon
        running = max(0.0, self.opening.get(ccy, 0.0))
        for d in range(horizon):
            flow = self.cf.get(ccy, d)
            if flow > 0.0:
                running += flow
            received[d] = running

        ceiling: List[float] = []
        for d in range(horizon):
            to_acquire = max(hole[d:min(d + reach + 1, horizon)])
            earmarked = min(still_to_pay[d], received[d])
            cap = to_acquire + earmarked
            if d < max_lag:
                cap = max(cap, ladder[d])
            ceiling.append(cap)
        return ceiling

    @staticmethod
    def _tighten(var, upper: float) -> None:
        """Lower a variable's upper bound, never raise it."""
        if var is None:
            return
        if var.upBound is None or upper < var.upBound:
            var.upBound = upper

    def _add_holding_ceiling(self, prob: pulp.LpProblem) -> None:
        """Hold the balance inside the corridor the cash flows define.

        Two bounds, and both are needed.

        The **ceiling** goes on ``bal_pos``, not on ``bal``: an overdraft is
        not a holding, and imposing this on the signed balance would say an
        account may never go overdrawn — a different rule, and a wrong one,
        since the model prices overdrafts deliberately.

        But bounding only the positive part leaves the short side wide
        open, and selling a currency you do not own is the same
        speculation in reverse: short a low-yielding currency and the
        differential accrues to you.  So the **floor** goes on ``bal_neg``,
        at the deepest overdraft the cash flows themselves dig.  Doing
        nothing at all lands exactly on that floor, and trading can only
        lift you off it, so this forbids nothing the account can genuinely
        experience — only overdrafts the model would have to manufacture
        by selling currency it never had.
        """
        for ccy in self.active_foreign_ccys:
            ceiling = self._holding_ceiling(ccy)
            floor = [max(0.0, -b) for b in self._do_nothing_ladder(ccy)]
            for d in range(self.cfg.horizon_days):
                cap, flr = ceiling[d], floor[d]
                # Slack only where the bound is a positive number the plan
                # may be driven exactly onto.  A bound of zero stays exact:
                # "almost nothing" would leave the day's binaries live for a
                # position that cannot be justified at all.
                if cap > 0.0:
                    cap += max(cap * self.cfg.holding_tolerance,
                               self.cfg.holding_min_slack)
                if flr > 0.0:
                    flr += max(flr * self.cfg.holding_tolerance,
                               self.cfg.holding_min_slack)
                # Applied as variable bounds, not as constraint rows.  A cap
                # on a single variable is a bound; writing it as a row adds a
                # near-binding near-duplicate for every day of the horizon,
                # and that much dual degeneracy is exactly what the cap this
                # replaced went to some trouble to eliminate.  Measured on a
                # three-currency twelve-day model, rows cost 3x the wall time
                # of bounds for an identical answer.
                self._tighten(self._v("bal_pos", ccy, d), cap)
                self._tighten(self._v("bal_neg", ccy, d), flr)

            log.info(
                "Holding corridor for %s: ceiling=%s floor=%s",
                ccy, [round(c, 2) for c in ceiling],
                [round(-f, 2) for f in floor],
            )

    # ── Objective ──────────────────────────

    def _build_objective(self) -> pulp.LpAffineExpression:
        obj = pulp.LpAffineExpression()
        base = self.cfg.base_ccy
        base_credit_rate = self.cfg.credit_carry_bps_per_day[base]
        base_debit_rate = self.cfg.debit_carry_bps_per_day[base]
        carry_start = self.cfg.credit_carry_start_day

        for d in range(self.cfg.horizon_days):
            bn_base = self._v("bal_neg", base, d)
            obj += base_debit_rate / 1e4 * bn_base

        for ccy in self.active_foreign_ccys:
            foreign_credit_rate = self.cfg.credit_carry_bps_per_day[ccy]
            net_carry_bps = base_credit_rate - foreign_credit_rate
            debit_rate_bps = self.cfg.debit_carry_bps_per_day[ccy]

            # Rates for carry/exposure valuation (spot tenor)
            spot_bid = self.cfg.fx_spot_bid(ccy)
            spot_ask = self.cfg.fx_spot_ask(ccy)
            spot_mid = self.cfg.fx_spot_mid(ccy)

            for d in range(self.cfg.horizon_days):
                bp = self._v("bal_pos", ccy, d)
                bn = self._v("bal_neg", ccy, d)

                # Credit carry: value positive balance at bid (exit rate)
                if d >= carry_start:
                    obj += net_carry_bps / 1e4 * spot_bid * bp

                # Debit carry: value negative balance at ask (entry rate)
                obj += debit_rate_bps / 1e4 * spot_ask * bn

                for tenor in self.cfg.tenors:
                    if not self._has_trade_vars(ccy, d, tenor):
                        continue

                    ask_rate = self.cfg.fx_ask(ccy, tenor)
                    bid_rate = self.cfg.fx_bid(ccy, tenor)

                    # Commission: buy notional at ask, sell notional at bid
                    for k, tier in enumerate(self.cfg.commission_tiers):
                        rate_k = tier.rate_bps / 1e4
                        buy_seg = self._v("buy_seg", ccy, d, tenor, k)
                        sell_seg = self._v("sell_seg", ccy, d, tenor, k)
                        if buy_seg is not None:
                            obj += rate_k * ask_rate * buy_seg
                        if sell_seg is not None:
                            obj += rate_k * bid_rate * sell_seg

                    # Rate paid away against the common reference (F4).
                    # This is what lets the objective tell tenors apart: the
                    # term differs across T0/T1/T2 by exactly the forward
                    # points, which the model was otherwise blind to.
                    if self.cfg.value_trade_rates:
                        buy_total = self._total_buy(ccy, d, tenor)
                        sell_total = self._total_sell(ccy, d, tenor)
                        if buy_total is not None:
                            obj += (ask_rate - spot_mid) * buy_total
                        if sell_total is not None:
                            obj += (spot_mid - bid_rate) * sell_total

            # Cost of unwinding whatever is still held at the horizon, so a
            # position left open is not scored as though it were free.  The
            # terminal sweep forces this to zero while it is on; with it off,
            # this is what stops the model sitting on currency rather than
            # converting it.
            if self.cfg.value_trade_rates:
                last = self.cfg.horizon_days - 1
                unwind_bps = self.cfg.commission_tiers[0].rate_bps / 1e4
                bp_last = self._v("bal_pos", ccy, last)
                bn_last = self._v("bal_neg", ccy, last)
                if bp_last is not None:
                    obj += ((spot_mid - spot_bid)
                            + unwind_bps * spot_bid) * bp_last
                if bn_last is not None:
                    obj += ((spot_ask - spot_mid)
                            + unwind_bps * spot_ask) * bn_last

        return obj

    def _reference_value(self) -> float:
        """The whole book valued at reference rates, with no frictions.

        Opening balances plus every exogenous cash flow, foreign amounts
        converted at spot mid.  This is what the account would be worth in
        base currency if currency could be moved costlessly.

        With trade rates valued (F4), the objective is exactly the shortfall
        against this number, so ``reference_value - total_cost`` is the net
        base currency the plan actually ends with.
        """
        base = self.cfg.base_ccy
        total = self.opening.get(base, 0.0) + sum(
            self.cf.get(base, d) for d in range(self.cfg.horizon_days))
        for ccy in self.foreign_ccys:
            amount = self.opening.get(ccy, 0.0) + sum(
                self.cf.get(ccy, d) for d in range(self.cfg.horizon_days))
            if amount:
                total += amount * self.cfg.fx_spot_mid(ccy)
        return total

    # ── Solver selection and optimality verification ──

    #: Preference order when Config.solver_name is None.  HiGHS first: CBC
    #: 2.10.3 returns provably suboptimal plans on this model and reports
    #: them as Optimal, and no CBC option recovers the correct answer.
    SOLVER_PREFERENCE = ("HiGHS", "PULP_CBC_CMD")

    def _resolve_solver_name(self, requested: Optional[str]) -> str:
        """Pick a solver, preferring one known to solve this model correctly."""
        if requested:
            return requested
        if self.cfg.solver_name:
            return self.cfg.solver_name
        try:
            available = set(pulp.listSolvers(onlyAvailable=True))
        except Exception:
            available = set()
        for name in self.SOLVER_PREFERENCE:
            if name in available:
                return name
        return "PULP_CBC_CMD"

    def _solve_lp_bound(
        self, prob: pulp.LpProblem, solver_name: str, time_limit: int,
    ) -> Optional[float]:
        """Solve the LP relaxation to obtain a valid lower bound.

        Dropping integrality can only enlarge the feasible set, so the
        resulting objective is a floor under the true optimum.  Comparing
        the returned plan against it is the only cheap, solver-independent
        evidence that the plan is actually good — and it is what catches a
        solver that reports a worse plan as "Optimal".

        Integrality is restored before returning, so the caller can go on
        to solve the real model with the same problem object.
        """
        integer_vars = [v for v in prob.variables() if v.cat == pulp.LpInteger]
        for v in integer_vars:
            v.cat = pulp.LpContinuous
        try:
            solver = pulp.getSolver(solver_name, timeLimit=time_limit, msg=False)
            prob.solve(solver)
            bound = (pulp.value(prob.objective)
                     if pulp.LpStatus[prob.status] == "Optimal" else None)
        except Exception as exc:
            log.warning("LP relaxation failed (%s); "
                        "optimality cannot be corroborated", exc)
            bound = None
        finally:
            for v in integer_vars:
                v.cat = pulp.LpInteger
        return bound

    # ── Solve ──────────────────────────────

    def _terminal_base_equivalent(self, balances) -> Tuple[float, Dict[str, float]]:
        """The whole book in base currency at the end of the horizon.

        Takes the plan's closing balances, converts anything still held in a
        foreign currency back to base at the rate it would actually be dealt
        at — credit at the bid, debit at the ask — and adds it to the closing
        base balance.  The terminal sweep leaves nothing to convert while it is
        on; with it off, this is what values whatever is left.

        A negative result is the one honest definition of insufficient funds:
        after everything is turned back into base currency, the account still
        owes money, so no arrangement of trades could ever have covered every
        debit and an external inflow is genuinely required.

        Returns the total and a per-currency breakdown of what fed into it.
        """
        base = self.cfg.base_ccy
        base_label = f"{base} (Base)"
        last = self.cfg.horizon_days - 1
        closing = {b.ccy: b for b in balances if b.day == last}

        contributions: Dict[str, float] = {}
        total = 0.0

        snap = closing.get(base_label)
        if snap is not None:
            contributions[base_label] = round(snap.balance, 2)
            total += snap.balance

        for ccy in self.foreign_ccys:
            snap = closing.get(ccy)
            if snap is None:
                continue
            converted = (snap.credit * self.cfg.fx_spot_bid(ccy)
                         - snap.debit * self.cfg.fx_spot_ask(ccy))
            if abs(converted) > 0.005:
                contributions[ccy] = round(converted, 2)
            total += converted

        return total, contributions

    def solve(self, solver_name: Optional[str] = None,
              time_limit: int = 120) -> Result:
        """Build model, solve, and return structured results."""
        if self._solved:
            raise RuntimeError(
                "solve() has already been called on this CashOptimizer instance. "
                "Create a new instance to re-solve."
            )

        log.info("Building model ...")

        prob = pulp.LpProblem("CashManagerPOC", pulp.LpMinimize)
        self._create_variables()

        flags = self.cfg.constraints
        log.info("Constraint flags: %s", flags.summary())

        self._add_balance_evolution(prob)
        self._add_balance_decomposition(prob)
        self._add_activation_linking(prob)
        self._add_commission_tier_linking(prob)

        if flags.no_loop:
            self._add_no_loop_constraints(prob)
        else:
            log.info("  SKIPPED: no-loop constraints")
        if flags.terminal_sweep:
            self._add_terminal_sweep(prob)
        else:
            log.info("  SKIPPED: terminal sweep constraints")
        if flags.holding_ceiling:
            self._add_holding_ceiling(prob)
        else:
            log.info("  SKIPPED: holding ceiling")
        prob += self._build_objective(), "TotalCost"
        self._prob = prob

        log.info("Model built: %d variables, %d constraints", prob.numVariables(), prob.numConstraints())

        solver_name = self._resolve_solver_name(solver_name)
        log.info("Solver: %s", solver_name)

        # Lower bound first, while the problem object is still untouched by a
        # MIP solve.  Relaxing integrality can only widen the feasible set, so
        # this is a floor under the true optimum — and the only cheap way to
        # tell a good plan from one the solver merely believes is good.
        lp_bound = None
        if self.cfg.verify_optimality:
            lp_bound = self._solve_lp_bound(prob, solver_name, time_limit)

        solver = pulp.getSolver(solver_name, timeLimit=time_limit, msg=False)
        prob.solve(solver)

        status = pulp.LpStatus[prob.status]
        log.info("Solver status: %s", status)
        self._solved = True

        if status == "Infeasible":
            log.error(
                "Solver returned Infeasible -- no plan satisfies the active "
                "constraints. This is a constraint conflict, not a funding "
                "shortfall: the model permits a base-currency overdraft, so a "
                "lack of cash shows up as a negative terminal balance rather "
                "than as infeasibility. Review the active constraint flags."
            )
            return Result(
                status=status, total_cost=None, trades=[], balances=[],
                pre_trade_ladder=self._build_pre_trade_ladder(),
                cash_flows=self._build_cash_flow_entries(),
                insufficient_funds=False,
            )
        elif status not in ("Optimal",):
            log.error(
                "Solver returned '%s' -- no plan can be reported. Returning an "
                "empty result here would be indistinguishable from 'nothing to "
                "do', so this raises instead.", status,
            )
            raise SolverFailure(status, solver=solver_name,
                                time_limit=time_limit)

        improved_from = None
        objective = None
        if status == "Optimal" and self.cfg.verify_optimality:
            incumbent = pulp.value(prob.objective)
            if incumbent is not None:
                improved_from, objective = self._improve_solution(
                    prob, solver_name, time_limit, incumbent)

        return self._extract_results(status, lp_bound=lp_bound,
                                     solver_used=solver_name,
                                     improved_from=improved_from,
                                     objective=objective)

    # ── Result extraction ──────────────────

    @staticmethod
    def _safe_value(var) -> float:
        if var is None:
            return 0.0
        val = pulp.value(var)
        if val is None:
            return 0.0
        return float(val)

    def _build_pre_trade_ladder(self) -> List[CashLadderEntry]:
        ladder: List[CashLadderEntry] = []
        base = self.cfg.base_ccy

        base_running = self.opening.get(base, 0.0)
        for d in range(self.cfg.horizon_days):
            base_cf = self.cf.get(base, d)
            opening = round(base_running, 2)
            closing = round(base_running + base_cf, 2)
            ladder.append(CashLadderEntry(
                ccy=f"{base} (Base)", day=d, opening=opening,
                cash_flow=round(base_cf, 2), closing=closing,
            ))
            base_running += base_cf

        for ccy in self.foreign_ccys:
            running = self.opening.get(ccy, 0.0)
            for d in range(self.cfg.horizon_days):
                cf = self.cf.get(ccy, d)
                opening = round(running, 2)
                closing = round(running + cf, 2)
                ladder.append(CashLadderEntry(
                    ccy=ccy, day=d, opening=opening,
                    cash_flow=round(cf, 2), closing=closing,
                ))
                running += cf
        return ladder

    def _build_cash_flow_entries(self) -> List[CashFlowEntry]:
        entries: List[CashFlowEntry] = []
        base = self.cfg.base_ccy
        for d in range(self.cfg.horizon_days):
            amt = self.cf.get(base, d)
            entries.append(CashFlowEntry(ccy=f"{base} (Base)", day=d, amount=round(amt, 2)))
        for ccy in self.foreign_ccys:
            for d in range(self.cfg.horizon_days):
                amt = self.cf.get(ccy, d)
                entries.append(CashFlowEntry(ccy=ccy, day=d, amount=round(amt, 2)))
        return entries

    #: A plan must beat the incumbent by at least this, relatively, before
    #: it counts as an improvement.  Solvers report objective values to
    #: limited precision (differences of ~3e-5 relative were observed
    #: between CBC and HiGHS on identical plans), so a smaller threshold
    #: manufactures improvements out of rounding noise.  Real failures on
    #: this model have been 8% and 99%.
    IMPROVEMENT_TOLERANCE = 1e-3

    def _improve_solution(self, prob, solver_name, time_limit, incumbent):
        """Test whether a materially cheaper plan exists below the one returned.

        Adds ``objective <= best - epsilon`` and solves again.  If the solver
        finds something better, its answer was provably not optimal and the
        better plan is kept.  If not, the answer stands corroborated.

        This is evidence rather than a heuristic: a plan is only declared
        unproven once a cheaper one has actually been produced.  Comparing
        against the LP relaxation bound cannot do that — on this model the
        bound sits 30-50% below the true optimum even for correct answers,
        so any threshold on that gap fires on ordinary, correct solves.
        """
        # PuLP mutates the expression it is handed when building a
        # constraint, so the objective must never be passed in directly —
        # doing so corrupts it and the reported cost collapses to zero.
        objective = pulp.LpAffineExpression(prob.objective)
        variables = prob.variables()

        def snapshot():
            return {v.name: v.varValue for v in variables}

        # Every round ends on a solve that found nothing, which leaves the
        # variables holding that failed attempt.  Keep the winning values
        # ourselves rather than trying to re-solve back to them — the solver
        # under suspicion here is exactly the wrong thing to depend on.
        best_values = snapshot()
        best_value = incumbent
        improved_from = None
        name = "optimality_cutoff"

        for _ in range(max(0, self.cfg.optimality_passes)):
            margin = max(abs(best_value) * self.IMPROVEMENT_TOLERANCE, 1e-2)
            prob.constraints.pop(name, None)
            prob += (pulp.LpAffineExpression(objective)
                     <= best_value - margin, name)
            try:
                prob.solve(pulp.getSolver(solver_name, timeLimit=time_limit,
                                          msg=False))
                status = pulp.LpStatus[prob.status]
                value = objective.value()
            except Exception as exc:
                log.warning("Optimality check failed (%s)", exc)
                break
            if (status != "Optimal" or value is None
                    or value > best_value - margin + 1e-9):
                break
            log.warning(
                "Solver's plan was not optimal: found %.4f against the "
                "reported %.4f, an improvement of %.4f.",
                value, best_value, best_value - value,
            )
            if improved_from is None:
                improved_from = best_value
            best_value = value
            best_values = snapshot()

        prob.constraints.pop(name, None)
        for v in variables:
            if v.name in best_values:
                v.varValue = best_values[v.name]
        return improved_from, objective

    def _extract_results(self, status: str, lp_bound=None, solver_used=None,
                         improved_from=None, objective=None) -> Result:
        if status not in ("Optimal",):
            # solve() handles Infeasible before reaching here and raises on
            # every other non-optimal status, so this is unreachable from the
            # public path.  Kept as a guard: there must be no route by which
            # an empty Result is passed off as a plan.
            raise SolverFailure(status, solver=solver_used)

        trades = []
        balances = []
        base = self.cfg.base_ccy

        for d in range(self.cfg.horizon_days):
            bal_val = self._safe_value(self._v("bal", base, d))
            bal_p = self._safe_value(self._v("bal_pos", base, d))
            bal_n = self._safe_value(self._v("bal_neg", base, d))
            balances.append(BalanceSnapshot(
                ccy=f"{base} (Base)", day=d,
                balance=round(bal_val, 2), credit=round(bal_p, 2), debit=round(bal_n, 2),
            ))

        for ccy in self.foreign_ccys:
            if ccy in self.active_foreign_ccys:
                for d in range(self.cfg.horizon_days):
                    bal_val = self._safe_value(self._v("bal", ccy, d))
                    bal_p = self._safe_value(self._v("bal_pos", ccy, d))
                    bal_n = self._safe_value(self._v("bal_neg", ccy, d))
                    balances.append(BalanceSnapshot(
                        ccy=ccy, day=d, balance=round(bal_val, 2),
                        credit=round(bal_p, 2), debit=round(bal_n, 2),
                    ))
                    floor = self.cfg.trade_report_floor_in(ccy)
                    for tenor in self.cfg.tenors:
                        if not self._has_trade_vars(ccy, d, tenor):
                            continue
                        bv = sum(
                            self._safe_value(self._v("buy_seg", ccy, d, tenor, k))
                            for k in range(len(self.cfg.commission_tiers))
                        )
                        sv = sum(
                            self._safe_value(self._v("sell_seg", ccy, d, tenor, k))
                            for k in range(len(self.cfg.commission_tiers))
                        )
                        settle = d + self.cfg.tenors[tenor]
                        if abs(bv) > floor:
                            trades.append(Trade(
                                ccy=ccy, day=d, tenor=tenor,
                                direction=Direction.BUY, amount=round(bv, 2),
                                settle_day=settle,
                            ))
                        if abs(sv) > floor:
                            trades.append(Trade(
                                ccy=ccy, day=d, tenor=tenor,
                                direction=Direction.SELL, amount=round(sv, 2),
                                settle_day=settle,
                            ))
            else:
                for d in range(self.cfg.horizon_days):
                    balances.append(BalanceSnapshot(
                        ccy=ccy, day=d, balance=0.0, credit=0.0, debit=0.0,
                    ))

        if objective is not None:
            total_cost = objective.value()
        else:
            total_cost = pulp.value(self._prob.objective) if self._prob else None

        terminal_base, terminal_detail = self._terminal_base_equivalent(balances)
        short = terminal_base < -0.01
        if short:
            log.error(
                "INSUFFICIENT FUNDS: with every foreign balance converted back "
                "to base, the account closes at %.2f — a shortfall of %.2f. No "
                "set of trades can cover every debit; an external inflow is "
                "required.", terminal_base, -terminal_base,
            )

        gap = None
        if lp_bound is not None and total_cost is not None:
            gap = (total_cost - lp_bound) / max(abs(total_cost), 1e-9)
            # The bound and the plan come from separate solves, and solution
            # files carry limited precision, so only a material shortfall is
            # evidence of anything.  Near zero the relative test is unstable,
            # so require an absolute shortfall as well.
            below_bound = lp_bound - total_cost
            if gap < -1e-4 and below_bound > max(abs(lp_bound) * 1e-4, 1e-3):
                log.warning(
                    "Returned objective %.4f is BELOW a valid lower bound "
                    "%.4f — the plan cannot be trusted.", total_cost, lp_bound)

        return Result(
            status=status,
            total_cost=total_cost,
            trades=trades, balances=balances,
            pre_trade_ladder=self._build_pre_trade_ladder(),
            cash_flows=self._build_cash_flow_entries(),
            reference_value=(self._reference_value()
                             if self.cfg.value_trade_rates else None),
            terminal_base_equivalent=terminal_base,
            insufficient_funds=short,
            shortfall=(-terminal_base if short else None),
            shortfall_detail=terminal_detail,
            lp_bound=lp_bound, optimality_gap=gap,
            optimality_unproven=(
                improved_from is not None
                or (gap is not None and lp_bound is not None
                    and gap < -1e-4
                    and (lp_bound - total_cost)
                    > max(abs(lp_bound) * 1e-4, 1e-3))),
            improved_from=improved_from, solver_used=solver_used,
        )

