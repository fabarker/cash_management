"""Load the JSON scenario library into ready-to-solve CashManagers.

The library lives at ``scenarios/cash_scenarios.json`` and is produced by
``scenarios/generate.py``.  Keeping the loader here rather than in the web
app means the same scenarios drive a REPL session, a test, and the page.

    from scripts.cash_optimizer_poc.scenarios import load_library, build_cash_manager

    lib = load_library()
    mgr = build_cash_manager(lib[0])
    mgr.solve_optimal().print_summary()
"""
from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Dict, List, Optional

from .cash_manager import CashManager
from .models import CashFlowSet, CommissionTier, Config, FXTenorQuote

log = logging.getLogger(__name__)

#: Repository-root-relative location of the library.
DEFAULT_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "scenarios" / "cash_scenarios.json"
)


def load_library(path: Optional[pathlib.Path] = None) -> List[Dict[str, Any]]:
    """Return the scenario list, in file order.

    Order is the contract: the page steps through them first to last, so a
    scenario's position is how a user refers to it.
    """
    p = pathlib.Path(path) if path else DEFAULT_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    scenarios = data.get("scenarios", [])
    if not scenarios:
        raise ValueError(f"No scenarios found in {p}")
    return scenarios


def build_config(scenario: Dict[str, Any]) -> Config:
    """Turn one scenario dict into a validated ``Config``."""
    quotes = {
        ccy: {
            tenor: FXTenorQuote(bid=float(leg["bid"]), ask=float(leg["ask"]))
            for tenor, leg in legs.items()
        }
        for ccy, legs in scenario["fx_quotes"].items()
    }

    credit = dict(scenario["credit_carry_pa"])
    # A scenario may deliberately depart from the shared rate table -- the
    # carry cases need a foreign currency yielding more than base, which the
    # default book does not provide for a sterling account.
    credit.update(scenario.get("credit_carry_pa_override", {}))

    debit = dict(scenario["debit_carry_pa"])
    debit.update(scenario.get("debit_carry_pa_override", {}))

    return Config(
        base_ccy=scenario["base_ccy"],
        currencies=list(scenario["currencies"]),
        horizon_days=int(scenario["horizon_days"]),
        tenors=dict(scenario["tenors"]),
        fx_quotes=quotes,
        commission_tiers=[
            CommissionTier(threshold=float(t["threshold"]),
                           rate_bps=float(t["rate_bps"]))
            for t in scenario["commission_tiers"]
        ],
        credit_carry_pa=credit,
        debit_carry_pa=debit,
        day_count_basis=dict(scenario["day_count_basis"]),
        min_trade=float(scenario.get("min_trade", 0.0)),
        max_trade=float(scenario["max_trade"]),
    )


def build_cash_flows(scenario: Dict[str, Any]) -> CashFlowSet:
    cf = CashFlowSet(horizon_days=int(scenario["horizon_days"]))
    for entry in scenario["cash_flows"]:
        cf.add(entry["ccy"], int(entry["day"]), float(entry["amount"]))
    return cf


def build_cash_manager(scenario: Dict[str, Any]) -> CashManager:
    """Build a ready-to-solve ``CashManager`` for one scenario."""
    return CashManager(
        build_config(scenario),
        build_cash_flows(scenario),
        opening_balances=dict(scenario["opening_balances"]),
    )


def summarise(scenario: Dict[str, Any]) -> str:
    """One-line description, for logs and page captions."""
    foreign = [c for c in scenario["currencies"] if c != scenario["base_ccy"]]
    return (
        f"{scenario['id']} {scenario['name']} — base {scenario['base_ccy']}, "
        f"foreign {'/'.join(foreign)}, {scenario['horizon_days']}d, "
        f"{scenario['commission_profile']} commission"
    )
