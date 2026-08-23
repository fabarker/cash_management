"""Show the arithmetic behind each line of the cost breakdown.

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


def cost_workings(manager: Any, result: Any) -> List[Component]:
    """Return the six cost components, each with its derivation.

    ``manager`` is a ``CashManager``; ``result`` the ``Result`` it produced.
    """
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
    spread = Component("spread", "FX spread paid", 0.0)
    unwind = Component("terminal_unwind", "Terminal unwind", 0.0)

    # ── Base overdraft ────────────────────────────────────────
    base_debit_days = sum(
        bal[(base_label, d)].debit
        for d in range(cfg.horizon_days) if (base_label, d) in bal
    )
    if base_debit_days > _EPS:
        amount = base_debit_bps / 1e4 * base_debit_days
        debit.value += amount
        debit.lines.append(
            f"{base} {_fmt(base_debit_bps, 4)}bps/day x "
            f"{_fmt(base_debit_days, 0)} overdrawn balance-days = {_fmt(amount)}"
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
            amount = net_bps / 1e4 * s_bid * credit_days
            credit.value += amount
            credit.lines.append(
                f"{ccy} ({_fmt(base_credit_bps, 4)} - "
                f"{_fmt(cfg.credit_carry_bps_per_day[ccy], 4)})bps/day x "
                f"{_rate(s_bid)} bid x {_fmt(credit_days, 0)} balance-days "
                f"= {_fmt(amount)}"
            )
        if debit_days > _EPS:
            amount = debit_bps / 1e4 * s_ask * debit_days
            debit.value += amount
            debit.lines.append(
                f"{ccy} {_fmt(debit_bps, 4)}bps/day x {_rate(s_ask)} ask x "
                f"{_fmt(debit_days, 0)} overdrawn balance-days = {_fmt(amount)}"
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
        amount_base = rate * fee_foreign
        commission.value += amount_base
        commission.lines.append(
            f"{ccy} day {day} {'buy' if buying else 'sell'} "
            f"{_fmt(amount, 0)} ({bands}) x {_rate(rate)} {tenor} "
            f"{'ask' if buying else 'bid'} = {_fmt(amount_base)}"
        )

        if cfg.value_trade_rates:
            half = (rate - s_mid) if buying else (s_mid - rate)
            amount_base = half * amount
            spread.value += amount_base
            spread.lines.append(
                f"{ccy} day {day} ({_rate(rate)} {tenor} "
                f"{'ask' if buying else 'bid'} - {_rate(s_mid)} mid) x "
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
                amount = ((s_mid - s_bid) + unwind_bps * s_bid) * snap.credit
                unwind.value += amount
                unwind.lines.append(
                    f"{ccy} {_fmt(snap.credit, 0)} still held on day {last}: "
                    f"(half-spread + {_fmt(cfg.commission_tiers[0].rate_bps, 1)}bps) "
                    f"= {_fmt(amount)}"
                )
            if snap.debit > _EPS:
                amount = ((s_ask - s_mid) + unwind_bps * s_ask) * snap.debit
                unwind.value += amount
                unwind.lines.append(
                    f"{ccy} {_fmt(snap.debit, 0)} still overdrawn on day "
                    f"{last}: (half-spread + "
                    f"{_fmt(cfg.commission_tiers[0].rate_bps, 1)}bps) "
                    f"= {_fmt(amount)}"
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
        c.reconciles = abs(c.value - expected[c.key]) < 5e-4
        if not c.reconciles:
            # Trust the model, not this module, and say the workings are off.
            c.lines = [
                f"workings do not reconcile: derived {_fmt(c.value, 4)} "
                f"against {_fmt(expected[c.key], 4)} from the cost model"
            ]
            c.value = expected[c.key]

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
