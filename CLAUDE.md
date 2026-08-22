# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A proof-of-concept multi-currency cash/FX optimizer. Given opening balances and projected
cash flows per currency over a short day-indexed horizon, it solves an LP/MIP (PuLP + CBC)
for the cheapest set of FX trades that clears every debit, then reports the plan as
formatted terminal tables.

There is no README, no lint config and no packaging metadata. The only third-party
dependency is `pulp` (which bundles CBC); `highspy` is optional but strongly
preferred — see the solver note under Gotchas.

## Tests

```bash
python -m unittest discover -s tests -v    # from the repository root
```

37 cases, ~6 seconds. Each class maps to a finding from the model audit.
`TestKnownOpenFindings` asserts behaviour that is still *broken on purpose*
(F5, F7, F12, F17) so that fixing one fails loudly and prompts an update.
`TestOptimizationInvariants` encodes properties any correct optimiser must
satisfy — relaxing a constraint cannot raise the optimum, the objective cannot
fall below the LP bound — which is how the original solver defect was caught.

## Running

There is no entry point script. Everything is driven from a REPL or ad-hoc script, and
**the working directory must be the repo root** — modules use absolute imports rooted at
`scripts.cash_optimizer_poc.*`, and there is no `scripts/__init__.py` (it works as an
implicit namespace package, so `PYTHONPATH` must include the repo root).

The project interpreter (per `.idea/`) is `~/virtual-environments/.venv/bin/python`.

```python
from scripts.cash_optimizer_poc.cash_manager import CashManager, ManualTrade
from scripts.cash_optimizer_poc.models import Config, CashFlowSet, Direction

cfg = Config()                                # defaults now work
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
- **Insufficient funds is measured after solving, not before.**
  `_terminal_base_equivalent()` takes the plan's closing balances, converts any
  remaining foreign holding back to base at the rate it would be dealt at (credit at
  bid, debit at ask), and adds it to the closing base balance. Negative means the
  account genuinely cannot cover its obligations however the trades are arranged, and
  `Result.insufficient_funds` is set — *with a plan still returned*, so the user can
  see how much is needed and when. There was a pre-solve `_check_solvency` gate that
  refused to build the model on an aggregate deficit; it has been removed, because the
  model permits base overdrafts and an aggregate deficit is a financing question.
- **An infeasible model is never a funding shortfall.** Because base overdrafts are
  permitted, a lack of cash surfaces as a negative closing balance, never as
  infeasibility. `Infeasible` therefore always means a constraint conflict, and is
  reported as "NO FEASIBLE PLAN". Toggling one `ConstraintFlags` entry off and
  re-solving identifies which constraint is binding — that sweep takes well under a
  second and diagnosed every infeasibility found in the audit.

### Constraint toggles

`ConstraintFlags` (on `Config.constraints`, or passed to `CashManager`, which mutates the
shared `Config`) switches off `terminal_sweep`, `no_loop`, `anti_speculative`,
`no_carry_trade`, `phasing`. Balance evolution, balance decomposition, activation
linking and commission-tier linking are structural and always applied.

`_add_no_carry_trade_constraint` carries a long docstring explaining its business rules
(sweep deadline / monotonic drawdown / buy blocking). Read it before touching that
constraint — it encodes specific fixes for infeasibility traps a naive reformulation
will reintroduce.

There was also a reserve subsystem — `Config.min_reserve`, a `reserve` flag, and
per-currency `reserve` / `res_outflows` / `res_batched` variables. **It has been deleted.**
It could never be enabled (any positive `min_reserve` collided with the terminal sweep,
which forces the last day's balance to zero), it compared a base-currency floor against a
foreign-denominated balance, and with the floor at zero the variables were driven to zero
by a tiny tie-break penalty — so the "RESERVE ATTRIBUTION" block printed zeros in every
report it ever produced. If a minimum balance is wanted, add it as a direct floor on
`bal_pos` and fold it into `_shortfall_profile`, the way the phasing ring-fence already
is; a floor alone is infeasible, because to the anti-speculative cap a balance you must
hold is currency you have no cash-flow need for.

There was also a `t0_debit_only` gate, which permitted same-day dealing only in a
currency already overdrawn at the start of the day. **It has been removed.** It made a
payment due today infeasible, dominated solve time (four currencies over 20 days went
from 19s to 0.4s without it; six over 30 days from a 120s timeout to 1.0s), and could be
gamed by selling a sliver of currency purely to manufacture the overdraft it looked for.
Removing it changed no other plan by a penny and did not reintroduce speculation — the
anti-speculative and no-carry rules carry that. If same-day dealing needs restricting
again, do it as a direct bound on the T0 trade variables rather than a gate keyed on the
previous day's sign binary.

## Gotchas

- **Solver choice matters more than it should.** CBC 2.10.3 (bundled with PuLP)
  returns provably sub-optimal plans on this model and labels them `Optimal` — it
  has been observed finding the correct answer, discarding it, and reporting a
  worse one while its own log still showed the better bound. No CBC option
  recovers it. `solve()` prefers HiGHS when available and falls back to CBC, so
  `pip install highspy` is worth doing. When only CBC is present the verification
  pass usually recovers the better plan and sets `Result.optimality_unproven`.
- **`max_trade` is a trade cap; `max_balance` is the balance ceiling.** These were
  once the same constant, which made the default configuration unable to solve
  anything. `Config.big_m` is now the trade-activation constant only, sized to
  `min(max_trade, commission capacity)`. The per-currency balance ceiling is
  derived from the scenario's own cash (`balance_headroom`, default 1.25×) unless
  `max_balance` is set explicitly. A scenario whose own ladder breaches it raises
  a `ValueError` naming the currency and day, rather than returning `Infeasible`.
- **The objective is a cost, and `net_terminal_wealth` is the money.** The
  objective measures total value lost to spreads, commission, carry, exposure and
  any residual position. `Result.reference_value - total_cost` is exactly the base
  currency the plan delivers. Keep new objective terms at spread scale: notional-
  scale terms would swamp the relative threshold the optimality check relies on.
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
- **The cost model is still written out four times** — the LP objective,
  `CashManager._compute_cost()`, `Result._compute_balance_costs()` and
  `Result._tenor_decomposition_row()`. Any change to the economics has to land in
  all of them or the comparison tables stop reconciling. This remains the single
  biggest hazard in the codebase.
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