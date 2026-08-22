# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A proof-of-concept multi-currency cash/FX optimizer. Given opening balances and projected
cash flows per currency over a short day-indexed horizon, it solves an LP/MIP (PuLP + CBC)
for the cheapest set of FX trades that clears every debit, then reports the plan as
formatted terminal tables.

There is no README, no test suite, no lint config, no packaging metadata, and no VCS
(`git init` has not been run). The only third-party dependency is `pulp` (which bundles CBC).

## Running

There is no entry point script. Everything is driven from a REPL or ad-hoc script, and
**the working directory must be the repo root** — modules use absolute imports rooted at
`scripts.cash_optimizer_poc.*`, and there is no `scripts/__init__.py` (it works as an
implicit namespace package, so `PYTHONPATH` must include the repo root).

The project interpreter (per `.idea/`) is `~/virtual-environments/.venv/bin/python`.

```python
from scripts.cash_optimizer_poc.cash_manager import CashManager, ManualTrade
from scripts.cash_optimizer_poc.models import Config, CashFlowSet, Direction

cfg = Config(max_trade=10_000_000.0)          # see Big-M note below
cf  = CashFlowSet(horizon_days=cfg.horizon_days)
cf.add("USD", 1, -100_000)

mgr = CashManager(cfg, cf, opening_balances={"GBP": 500_000, "USD": 50_000})
mgr.print_cash_ladder()

opt = mgr.solve_optimal()                     # LP/MIP
opt.print_summary()
opt.print_cost(cfg)                           # per-trade cost attribution

manual = mgr.execute_trades([                 # same cost model, no solver
    ManualTrade("USD", day=0, tenor="T1", direction=Direction.BUY, amount=50_000),
])
mgr.compare(manual)                           # head-to-head cost table
```

`cash_manager.py`'s `if __name__ == "__main__"` block calls `CashManager.from_group_number`,
which is **entirely commented out** — running the module directly raises `AttributeError`.

## Architecture

Strict one-way layering; each module imports only from the ones above it.

| Module | Role |
|---|---|
| `models.py` | All dataclasses/enums: `Config`, `ConstraintFlags`, `FXTenorQuote`, `CommissionTier`, `CashFlowSet`, `PhasingPlan`, `Trade`, `BalanceSnapshot`, … |
| `utils.py` | Pivoted-table formatters (ccy on rows, days on columns) + `logging.basicConfig` |
| `optimizer.py` | `CashOptimizer` — builds and solves the LP/MIP, extracts a `Result` |
| `result.py` | `Result` — formatting, cost attribution, after-cost ladder (display only) |
| `cash_manager.py` | `CashManager` — stateful workbench wrapping the optimizer; manual-trade evaluation and comparison |

### The cost model is duplicated in four places

This is the single most important thing to know before changing anything economic. The same
formulas (credit carry differential, debit carry, FX exposure penalty, tiered commission)
are re-implemented as:

1. `CashOptimizer._build_objective()` — as PuLP linear terms (the thing actually minimised)
2. `CashManager._compute_cost()` — plain arithmetic over `BalanceSnapshot`s, used for both
   manual trades and (via `_compute_optimal_cost_breakdown`) the optimizer's own result
3. `Result._compute_balance_costs()` — the global balance-dependent costs in `format_cost()`
4. `Result._tenor_decomposition_row()` — per-trade attribution, plus a fourth copy of the
   tiered-commission loop in `Result.compute_after_cost_balances()`

Change a rate convention or add a cost term in one and the comparison tables silently stop
reconciling. Update all of them together.

### FX rate conventions

Quotes are **base currency per 1 unit of foreign** (`FXTenorQuote(bid, ask)` per ccy per
tenor; `Config.validate()` enforces `bid < ask`).

- **Buy foreign** → pay base at the **ask**
- **Sell foreign** → receive base at the **bid**
- Carry and exposure are valued at the **spot tenor** (`Config.spot_tenor`, default `"T2"`),
  not the trade tenor: credit carry at spot bid, debit carry at spot ask, exposure at spot mid.

Days are 0-based integers, not dates. Tenors map name → settlement lag (`{"T0": 0, "T1": 1, "T2": 2}`).

### LP/MIP structure (`optimizer.py`)

All variables live in one dict `self._vars` keyed by tuples, read through `self._v(*key)`,
which returns `None` for absent keys — much of the code branches on that.

- Trade variables are **split into commission-tier segments**: `("buy_seg", ccy, d, tenor, k)`
  with `upBound` = that tier's band width. `_total_buy`/`_total_sell` sum the segments;
  `fill_buy`/`fill_sell` binaries force lower tiers to fill before higher ones (making the
  piecewise commission cost exact rather than just convex-relaxed).
- Trade variables are **only created when `d + lag < horizon_days`**. `_has_trade_vars()`
  guards every loop over tenors.
- Balances are decomposed `bal = bal_pos - bal_neg` with a `bal_sign` binary and Big-M
  complementarity, for all tracked currencies including base.
- `active_foreign_ccys` — foreign currencies with a zero opening balance *and* no cash flows
  are dropped from the model entirely, then re-inserted as all-zero rows during result
  extraction so output shape stays stable.
- `solve()` is single-shot: it raises on a second call, so build a fresh `CashOptimizer`
  (or call `mgr.solve_optimal()` again, which constructs one) to re-solve.
- A pre-solve aggregate solvency check (`_check_solvency`) short-circuits to an
  `Infeasible` `Result` with `insufficient_funds=True` and a per-currency shortfall
  breakdown before the model is ever built.

### Constraint toggles

`ConstraintFlags` (on `Config.constraints`, or passed to `CashManager`, which mutates the
shared `Config`) switches off `terminal_sweep`, `no_loop`, `anti_speculative`,
`no_carry_trade`, `phasing`, `reserve`, `t0_debit_only`. Balance evolution, balance
decomposition, activation linking, commission-tier linking and reserve attribution are
structural and always applied.

`_add_no_carry_trade_constraint` and `_add_t0_debit_only_constraint` carry long docstrings
explaining their business rules (sweep deadline / monotonic drawdown / buy blocking; and the
per-currency previous-day debit gate with its per-currency `eps` sign-pin). Read those before
touching either — both encode specific fixes for infeasibility traps that a naive
reformulation will reintroduce.

## Gotchas

- **Big-M / `max_trade`.** The default `Config.max_trade = 5e11` sets `big_m` to the same
  magnitude and makes CBC return `Infeasible` on scenarios that are trivially feasible.
  Verified: the example above solves as `Optimal` with `max_trade=10_000_000.0` and returns
  `Infeasible` with the default. Always size `max_trade` to the scenario (roughly 2× total
  absolute cash movement, with a floor) — the commented-out `from_cash_projections` does
  exactly this and notes the same reason.
- **`ManualTrade` and `CostBreakdown` are defined twice.** `cash_manager.py` imports both
  from `models.py` and then redefines them below the import, so the local definitions win.
  The `cash_manager` `CostBreakdown` carries extra `spread_cost` / `slippage_cost` fields
  that nothing ever populates (spread is embedded in bid/ask). Anything importing from
  `models.py` gets the other class — do not assume identity.
- **Base currency rows are labelled `"GBP (Base)"`**, not `"GBP"`, in `BalanceSnapshot`,
  `CashLadderEntry` and `CashFlowEntry`. Lookups recover the code with `.split(" ")[0]`.
  A dict keyed on `b.ccy` will not match `cfg.base_ccy`.
- **`utils.py` calls `logging.basicConfig` at import time**, so importing anything in the
  package configures root logging at INFO.
- **The after-cost ladder is display-only** — `Result.compute_after_cost_balances()` accrues
  per-currency interest and deducts commission on the settlement day, and does not feed back
  into the objective.
- **Large commented-out integration layer.** ~500 lines at the bottom of `cash_manager.py`
  (`from_cash_projections`, `from_group_number`, `_fetch_fx_rates`, `_fetch_credit_debit_rates`)
  depend on internal `pmg_core` packages — Maxis `CashProjectionsManager`, Refinitiv FX
  forwards, the Interest Engine — that are not vendored here. It is the reference for how live
  data is meant to be mapped into `Config` / `CashFlowSet` (opening balance = day-0 balance,
  cash flows = day-over-day balance deltas). `_FALLBACK_CREDIT_PA` and
  `_FALLBACK_DEBIT_MULTIPLIER` at the top of the module exist only for that code.