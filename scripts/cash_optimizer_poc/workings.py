"""Show the arithmetic behind each line of the cost breakdown.

One line is easy to misread and is named carefully because of it.  "Rate vs
spot-mid reference" is not the dealing spread: the model prices every trade
against the mid at the *spot* tenor, because that is what
``Result.reference_value`` values the whole book at.  So the figure bundles
the half-spread with the forward points from the trade tenor back to spot,
and it can be **negative** — dealing T+0 in a currency yielding well below
base earns a better rate than the spot-mid reference assumes, and the line
becomes a credit.  Three trades in the shipped scenario library do exactly
that.  It is not double-counted against carry: under covered interest
parity the points and the carry differential are the same quantity seen
from two sides, and the model charges the rate difference here and the
carry there, which nets correctly.


The cost model is already written out in four places, and this is a fifth.
That is the single biggest hazard in the codebase, so this module does not
get to be trusted: it recomputes every component independently and then
checks itself against ``CashManager._compute_cost``.  Where the two
disagree the line says so rather than printing a derivation that does not
reconcile — a plausible-looking formula that is quietly wrong is worse than
no formula at all.

    from scripts.cash_optimizer_poc.workings import cost_workings
    for component in cost_workings(mgr, result):
        print(component.label, component.value, component.lines)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .models import Direction

#: Below this a component is treated as zero and shown without workings.
_EPS = 5e-5


@dataclass
class Component:
    """One line of the breakdown, with the arithmetic that produced it."""

    key: str
    label: str
    value: float
    lines: List[str] = field(default_factory=list)
    reconciles: bool = True

    @property
    def workings(self) -> str:
        return "  +  ".join(self.lines) if self.lines else ""


def _fmt(x: float, dp: int = 2) -> str:
    return f"{x:,.{dp}f}"


def _rate(x: float) -> str:
    """Rates run from 0.005 (yen) to 190 (yen base), so pick the precision."""
    return f"{x:,.6f}" if abs(x) < 1 else f"{x:,.4f}"


def cost_workings(manager: Any, result: Any,
                  value_impact: bool = False) -> List[Component]:
    """Return the cost components, each with its derivation.

    ``manager`` is a ``CashManager``; ``result`` the ``Result`` it produced.

    With ``value_impact=False`` (the default) figures follow the model's own
    convention: a cost is positive, because the objective is a cost and is
    minimised.  With ``value_impact=True`` every sign is flipped, so a cost
    reads negative and a benefit positive, which is how money moving in and
    out of an account normally reads.

    The flip is not applied to the number alone.  Each derivation is written
    so its own arithmetic produces the sign shown -- a row whose figure says
    -148.13 while its formula works out to +148.13 is worse than no formula.
    The self-check below always compares against the model in the model's
    convention, whichever way the display is set.
    """
    sgn = -1.0 if value_impact else 1.0
    cfg = manager.config
    base = cfg.base_ccy
    base_label = f"{base} (Base)"
    foreign = [c for c in cfg.currencies if c != base]

    bal = {(b.ccy, b.day): b for b in result.balances}
    base_credit_bps = cfg.credit_carry_bps_per_day[base]
    base_debit_bps = cfg.debit_carry_bps_per_day[base]
    carry_start = cfg.credit_carry_start_day

    credit = Component("credit_carry", "Credit carry (differential)", 0.0)
    debit = Component("debit_carry", "Debit carry (overdraft)", 0.0)
    commission = Component("commission", "Commission", 0.0)
    spread = Component("spread", "Rate vs spot-mid reference", 0.0)
    unwind = Component("terminal_unwind", "Terminal unwind", 0.0)

    # ── Base overdraft ────────────────────────────────────────
    base_debit_days = sum(
        bal[(base_label, d)].debit
        for d in range(cfg.horizon_days) if (base_label, d) in bal
    )
    if base_debit_days > _EPS:
        amount = sgn * base_debit_bps / 1e4 * base_debit_days
        debit.value += amount
        debit.lines.append(
            f"{base} {'-' if value_impact else ''}{_fmt(base_debit_bps, 4)}bps/day "
            f"x {_fmt(base_debit_days, 0)} overdrawn balance-days = {_fmt(amount)}"
        )

    # ── Per foreign currency ──────────────────────────────────
    for ccy in foreign:
        net_bps = base_credit_bps - cfg.credit_carry_bps_per_day[ccy]
        debit_bps = cfg.debit_carry_bps_per_day[ccy]
        s_bid, s_ask = cfg.fx_spot_bid(ccy), cfg.fx_spot_ask(ccy)
        s_mid = cfg.fx_spot_mid(ccy)

        credit_days = sum(
            bal[(ccy, d)].credit for d in range(carry_start, cfg.horizon_days)
            if (ccy, d) in bal
        )
        debit_days = sum(
            bal[(ccy, d)].debit for d in range(cfg.horizon_days)
            if (ccy, d) in bal
        )

        if abs(credit_days) > _EPS and abs(net_bps) > 1e-9:
            amount = sgn * net_bps / 1e4 * s_bid * credit_days
            credit.value += amount
            a, b = base_credit_bps, cfg.credit_carry_bps_per_day[ccy]
            if value_impact:
                a, b = b, a          # foreign earned less base foregone
            credit.lines.append(
                f"{ccy} ({_fmt(a, 4)} - {_fmt(b, 4)})bps/day x "
                f"{_rate(s_bid)} bid x {_fmt(credit_days, 0)} balance-days "
                f"= {_fmt(amount)}"
            )
        if debit_days > _EPS:
            amount = sgn * debit_bps / 1e4 * s_ask * debit_days
            debit.value += amount
            debit.lines.append(
                f"{ccy} {'-' if value_impact else ''}{_fmt(debit_bps, 4)}bps/day "
                f"x {_rate(s_ask)} ask x {_fmt(debit_days, 0)} overdrawn "
                f"balance-days = {_fmt(amount)}"
            )

    # ── Trades ────────────────────────────────────────────────
    grouped: Dict[Any, float] = {}
    for t in result.trades:
        key = (t.ccy, t.day, t.tenor, t.direction)
        grouped[key] = grouped.get(key, 0.0) + t.amount

    for (ccy, day, tenor, direction), amount in sorted(
        grouped.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        buying = direction == Direction.BUY
        rate = cfg.fx_ask(ccy, tenor) if buying else cfg.fx_bid(ccy, tenor)
        s_mid = cfg.fx_spot_mid(ccy)

        fee_foreign, bands = _tiered(cfg, amount)
        amount_base = sgn * rate * fee_foreign
        commission.value += amount_base
        commission.lines.append(
            f"{ccy} day {day} {'buy' if buying else 'sell'} "
            f"{_fmt(amount, 0)} {'-' if value_impact else ''}({bands}) x "
            f"{_rate(rate)} {tenor} {'ask' if buying else 'bid'} "
            f"= {_fmt(amount_base)}"
        )

        if cfg.value_trade_rates:
            half = (rate - s_mid) if buying else (s_mid - rate)
            amount_base = sgn * half * amount
            spread.value += amount_base
            dealt = f"{_rate(rate)} {tenor} {'ask' if buying else 'bid'}"
            ref = f"{_rate(s_mid)} spot mid"
            first, second = (ref, dealt) if value_impact else (dealt, ref)
            spread.lines.append(
                f"{ccy} day {day} ({first} - {second}) x "
                f"{_fmt(amount, 0)} = {_fmt(amount_base)}"
            )

    # ── Anything still held at the close ──────────────────────
    if cfg.value_trade_rates:
        last = cfg.horizon_days - 1
        unwind_bps = cfg.commission_tiers[0].rate_bps / 1e4
        for ccy in foreign:
            snap = bal.get((ccy, last))
            if snap is None:
                continue
            s_bid, s_ask = cfg.fx_spot_bid(ccy), cfg.fx_spot_ask(ccy)
            s_mid = cfg.fx_spot_mid(ccy)
            if snap.credit > _EPS:
                amount = sgn * ((s_mid - s_bid) + unwind_bps * s_bid) * snap.credit
                unwind.value += amount
                unwind.lines.append(
                    f"{ccy} {_fmt(snap.credit, 0)} still held on day {last}: "
                    f"{'-' if value_impact else ''}(half-spread + "
                    f"{_fmt(cfg.commission_tiers[0].rate_bps, 1)}bps) = {_fmt(amount)}"
                )
            if snap.debit > _EPS:
                amount = sgn * ((s_ask - s_mid) + unwind_bps * s_ask) * snap.debit
                unwind.value += amount
                unwind.lines.append(
                    f"{ccy} {_fmt(snap.debit, 0)} still overdrawn on day "
                    f"{last}: {'-' if value_impact else ''}(half-spread + "
                    f"{_fmt(cfg.commission_tiers[0].rate_bps, 1)}bps) = {_fmt(amount)}"
                )

    components = [credit, debit, commission, spread, unwind]

    # ── Check this module against the model it describes ──────
    truth = manager._compute_optimal_cost_breakdown(result)
    expected = {
        "credit_carry": truth.credit_carry_cost,
        "debit_carry": truth.debit_carry_cost,
        "commission": truth.commission_cost,
        "spread": truth.spread_cost,
        "terminal_unwind": truth.terminal_unwind_cost,
    }
    for c in components:
        c.reconciles = abs(sgn * c.value - expected[c.key]) < 5e-4
        if not c.reconciles:
            # Trust the model, not this module, and say the workings are off.
            c.lines = [
                f"workings do not reconcile: derived {_fmt(sgn * c.value, 4)} "
                f"against {_fmt(expected[c.key], 4)} from the cost model"
            ]
            c.value = sgn * expected[c.key]

    return components


def _tiered(cfg: Any, amount_foreign: float) -> tuple:
    """Commission in foreign units, plus a readable band-by-band breakdown."""
    remaining = abs(amount_foreign)
    total = 0.0
    prev = 0.0
    parts: List[str] = []
    for tier in cfg.commission_tiers:
        band = tier.threshold - prev
        fill = min(remaining, band)
        if fill > 0:
            total += fill * (tier.rate_bps / 1e4)
            parts.append(f"{_fmt(tier.rate_bps, 1)}bps on {_fmt(fill, 0)}")
        remaining -= fill
        prev = tier.threshold
        if remaining <= 0:
            break
    return total, " + ".join(parts) if parts else "no commission"
