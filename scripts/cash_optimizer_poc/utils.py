"""
Formatting utilities and shared constants for the cash optimizer.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from scripts.cash_optimizer_poc.models import (
    BalanceSnapshot,
    CashLadderEntry,
)

COL_W = 14  # width of each day column in pivoted tables

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("cash_optimizer_poc")


# ────────────────────────────────────────────
# Pivoted table formatters
# ────────────────────────────────────────────

def format_ladder_pivoted(
    entries: List[CashLadderEntry],
    indent: str = "  ",
) -> List[str]:
    """
    Format a pre-trade cash ladder as a pivoted table:
    currencies on rows, days on columns, showing end-of-day (closing) balance.
    """
    if not entries:
        return [f"{indent}(no data)"]

    days: List[int] = []
    ccys: List[str] = []
    grid: Dict[Tuple[str, int], float] = {}
    for e in entries:
        if e.day not in days:
            days.append(e.day)
        if e.ccy not in ccys:
            ccys.append(e.ccy)
        grid[(e.ccy, e.day)] = e.closing

    cw = COL_W
    lines: List[str] = []
    day_headers = "".join(f"{'D' + str(d):>{cw}}" for d in days)
    lines.append(f"{indent}{'CCY':<14}{day_headers}")
    lines.append(f"{indent}{'-' * (14 + cw * len(days))}")

    for ccy in ccys:
        vals = "".join(f"{grid.get((ccy, d), 0.0):>{cw},.2f}" for d in days)
        lines.append(f"{indent}{ccy:<14}{vals}")

    return lines


def format_balances_pivoted(
    balances: List[BalanceSnapshot],
    indent: str = "  ",
) -> List[str]:
    """
    Format post-trade balance snapshots as a pivoted table:
    currencies on rows, days on columns, showing end-of-day net balance
    (positive = credit, negative = debit).
    """
    if not balances:
        return [f"{indent}(no data)"]

    days: List[int] = []
    ccys: List[str] = []
    grid: Dict[Tuple[str, int], float] = {}
    for b in balances:
        if b.day not in days:
            days.append(b.day)
        if b.ccy not in ccys:
            ccys.append(b.ccy)
        grid[(b.ccy, b.day)] = b.balance

    cw = COL_W
    lines: List[str] = []
    day_headers = "".join(f"{'D' + str(d):>{cw}}" for d in days)
    lines.append(f"{indent}{'CCY':<14}{day_headers}")
    lines.append(f"{indent}{'-' * (14 + cw * len(days))}")

    for ccy in ccys:
        vals = "".join(f"{grid.get((ccy, d), 0.0):>{cw},.2f}" for d in days)
        lines.append(f"{indent}{ccy:<14}{vals}")

    return lines


# Backward compatibility aliases
_format_ladder_pivoted = format_ladder_pivoted
_format_balances_pivoted = format_balances_pivoted

