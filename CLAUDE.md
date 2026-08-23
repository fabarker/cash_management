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

Each audit finding was fixed in its own commit, and `REVERTING.md` documents how to
undo any one of them individually.

## Tests

```bash
python -m unittest discover -s tests -v    # from the repository root
```

118 cases, ~15 seconds. Most classes map to a finding from the model audit.
`TestKnownOpenFindings` is now empty — it held tests asserting behaviour that was
still broken on purpose, so that fixing one would fail loudly; every finding it
tracked has since been fixed. Keep the class: it is where the next known-broken
behaviour goes.

`TestOptimizationInvariants` encodes properties any correct optimiser must
satisfy — relaxing a constraint cannot raise the optimum, the objective cannot
fall below the LP bound — which is how the original solver defect was caught.
`TestHoldingCorridor` covers the anti-speculation policy, and its last test is a
deliberate tripwire: it asserts the minimum-ticket wash route is *open* with
`no_loop` off, so if the corridor ever grows to cover that route the suite says so
rather than leaving a now-redundant constraint in place unexamined.

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

## Scenario library

`scenarios/cash_scenarios.json` holds twenty economically-shaped scenarios — varying
base currency (GBP/USD/EUR/JPY/CHF), which foreign currencies are in play, carry rates,
dealing spreads and commission schedules. Each carries a `narrative` and an
`expectation` describing what a correct plan looks like.

FX forwards are **derived, not invented**: `scenarios/generate.py` crosses every pair
off a single USD table and computes each tenor by covered interest parity, so the book
is arbitrage-free and forward points carry the right sign against the rate
differential. Edit the generator and re-run it; do not hand-edit the JSON.

```bash
python3 scenarios/generate.py        # regenerate after editing
```

```python
from scripts.cash_optimizer_poc.scenarios import load_library, build_cash_manager
for sc in load_library():
    build_cash_manager(sc).solve_optimal().print_summary()
```

Each also carries `cost_reasoning` — why the expected plan is the *cheapest* one, in
terms of the actual figures. Commission dominates almost every scenario here (20bps on
the first 500k of a 0.79 rate is ~790 base), which is worth knowing before reading any
of them.

Three are deliberately not plain successes: **S16** is `Infeasible` because the need is
below the minimum ticket, **S20** returns a plan with `insufficient_funds` set, and
**S07** is the carry test — toggling `holding_ceiling` off makes it *cheaper*, and that
fall is the speculation being taken.

The web page steps through the library in order: the load button advances one place and
wraps at the end, and typing an id (`S07`) or a position (`7`) jumps there instead.

## Architecture

Strict one-way layering; each module imports only from the ones above it.

| Module | Role |
|---|---|
| `models.py` | All dataclasses/enums: `Config`, `ConstraintFlags`, `FXTenorQuote`, `CommissionTier`, `CashFlowSet`, `PhasingPlan`, `Trade`, `BalanceSnapshot`, … |
| `utils.py` | Pivoted-table formatters (ccy on rows, days on columns) + `logging.basicConfig` |
| `optimizer.py` | `CashOptimizer` — builds and solves the LP/MIP, extracts a `Result` |
| `result.py` | `Result` — formatting, cost attribution, after-cost ladder (display only) |
| `cash_manager.py` | `CashManager` — stateful workbench wrapping the optimizer; manual-trade evaluation and comparison |

### The cost model is duplicated in four places — and `workings.py` is a fifth

`workings.py` derives each line of the breakdown for display (which commission tier,
how many balance-days at what rate, which side of the spread). It is a fifth copy of
the same arithmetic, so it does not get to be trusted: it recomputes every component
independently and then checks itself against `CashManager._compute_cost`. Where the two
disagree, the line reports the mismatch and shows the model's figure rather than its
own. A plausible-looking formula that is quietly wrong is worse than no formula.

### The cost model is duplicated in four places

This is the single most important thing to know before changing anything economic. The same
formulas (credit carry differential, debit carry, tiered commission, spread, terminal unwind)
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

- **Buy foreign** → pay base at the **ask**; base falls by `ask x amount`, foreign rises
- **Sell foreign** → receive base at the **bid**; base rises by `bid x amount`, foreign falls

Verified end to end on solved plans, both legs, in both directions. Forwards carry the
right sign too: every currency yielding more than base trades at a forward discount.
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
- **Derived config values are computed on access, not stored.** `Config.big_m` and
  `Config.fx_exposure_from_day` are properties. They used to be filled in by
  `__post_init__` and never refreshed, so changing `max_trade`, the commission schedule
  or the tenors afterwards left them describing the config as it was at construction.
  `fx_exposure_start_day` remains the field you set; leave it `None` and
  `fx_exposure_from_day` resolves it to one day past the longest settlement lag.
- **Interest accrues on a day count that varies by currency.**
  `Config.day_count_basis` maps each currency to the number of days its market treats
  as a year; `Config.day_count(ccy)` reads it, falling back to `default_day_count`
  (360, the more common convention) with a logged warning. It is *not* "sterling is 365
  and everything else is 360" — JPY, CAD, AUD, NZD, HKD and SGD are also ACT/365. One
  divisor for everything understated non-matching currencies by 1.39%, charged in full
  on an overdraft. Check the map against whatever supplies your rates.
- **Non-finite numbers are rejected at the door.** NaN or infinity in a cash flow,
  opening balance, FX quote or carry rate raises immediately, naming the currency and
  day. NaN is the dangerous one: every comparison against it is false, so
  `abs(balance) > 1e-9` answers "nothing here" for a figure that means "unreadable",
  and the currency was silently dropped from the model.
- **The currency universe is discovered, not declared.** `Config.currencies` is a
  statement of intent; the model actually runs on that list plus any currency appearing
  in the opening balances or cash flows. A projection feed can deliver an obligation in
  a currency nobody pre-declared, and it used to be invisible everywhere. The caller's
  `Config` is never mutated. What a discovered currency must still bring is its market
  data — `_check_market_data()` raises if `fx_quotes` or the carry rates are missing,
  naming the currency, what is absent, and where it came from. Which currencies exist
  can be read off the data; an exchange rate cannot be inferred from anything.
- **A non-optimal solver status raises, it does not return an empty result.**
  Anything other than `Optimal` or `Infeasible` — a time limit, an unbounded model, a
  solver error — raises `SolverFailure` (a `RuntimeError` subclass carrying `.status`,
  `.solver` and `.time_limit`). It previously returned a `Result` with no trades, no
  cost and no exception, which a caller reading `result.trades` could not distinguish
  from "nothing worth doing". `Infeasible` still returns a `Result`, because that is an
  informative outcome rather than a failure to solve.
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

`ConstraintFlags` (on `Config.constraints`, or passed to `CashManager`, which takes a
copy) switches off `terminal_sweep`, `no_loop`, `holding_ceiling`. Balance evolution,
balance decomposition, activation linking and commission-tier linking are structural
and always applied.

**`holding_ceiling` is the whole anti-speculation policy.** Two bounds per currency
per day, keeping the balance inside the corridor the cash flows themselves define:

```
bal_pos[d] <= hole reachable from a trade dealt today
              + the part of the do-nothing holding an outflow will consume
bal_neg[d] <= the deepest overdraft the cash flows themselves dig
```

Everything is read off `_do_nothing_ladder(ccy)` — opening balance plus that
currency's own cash flows, no trades — so the rule sees an opening overdraft, which
is what a rule keyed on the cash flow file cannot.

The reach window (`d .. d + max_lag`) on the first term is what makes it
anti-speculation rather than a size limit: if a payment can always be funded by
dealing at the longest tenor, owning the currency earlier is a position, not funding,
so the ceiling is simply zero until the obligation comes within dealing range.

Four details are load-bearing, each of them a bug that testing caught:

- **The earmark term is not optional.** The hole is measured on the do-nothing
  ladder, which already contains the receipts, so a receipt covering a later payment
  digs no hole. A hole-only ceiling forbids holding money the account was always
  going to spend, and the model's only escape is to sell it and buy it back for two
  spreads and two commissions. This is the mirror of the netting defect the old
  cumulative cap's docstring warns about.
- **The carve-out window is `max_lag`, not `min_lag`.** Money already on the books
  before any trade could have been dealt against it needs the settlement window to be
  cleared. Narrowing it forces a T+0 exit on day nought, which gains nothing — the
  position is contracted away the moment the trade is dealt — but removes the choice
  of tenor and the better forward rate with it.
- **The floor matters as much as the ceiling.** A bound on `bal_pos` alone leaves the
  short side wide open, and selling currency you do not own is the same speculation
  in reverse. Doing nothing lands exactly on the floor and trading can only lift you
  off it, so it forbids nothing the account can genuinely experience.
- **`no_loop` is not dead weight.** It looks inert, and against a *cheap* wash trade
  it is — the objective rejects one at any spread, even at zero commission. It binds
  where a wash trade is **forced**: give the model a need below `min_trade` and it
  deals two legal tickets netting to an illegal amount, and the corridor cannot see
  it because the position nets to zero on every day.
  `TestHoldingCorridor.test_a_wash_trade_cannot_manufacture_a_legal_ticket` is a
  tripwire for this — it asserts the route is *open* with `no_loop` off, so if the
  corridor ever grows to cover it the test says so.

Removed, and worth knowing why so they are not reintroduced:

- **`anti_speculative`** capped *cumulative purchases* at the deepest reachable
  shortfall. The corridor caps the *position* instead, which also reaches currency
  that arrived as a receipt — something a purchase cap cannot see at all — and does
  not constrain the path to a position, only the position. That is why several
  scenarios got cheaper without holding a penny more.
- **`no_carry_trade`** was three rules under one flag. Only the sweep deadline ever
  changed an answer, and the ceiling reproduces it exactly by falling to zero once
  obligations have passed. The monotonic-drawdown rule never bound anywhere. The
  buy-blocking rule was strictly dominated by the cumulative cap *and* caused an
  infeasibility: it read the cash flow file alone, so an opening overdraft granted no
  permission to buy while the sweep deadline still demanded the balance reach zero.

`_shortfall_profile` survives as a one-line diagnostic over `_do_nothing_ladder`.

There was also **phasing** — `PhasingPlan`, a `phasing` flag, and a ring-fenced floor
on the balance of a currency being deployed in tranches. **It has been removed.** It
crashed on a currency not already held (the natural case, since you phase into
something you have yet to buy), could not be used alongside the no-carry rule, and
applied its tolerance in the wrong direction — a 2% tolerance forced 102% to be held
rather than permitting a 2% shortfall.

There was also a reserve subsystem — `Config.min_reserve`, a `reserve` flag, and
per-currency `reserve` / `res_outflows` / `res_batched` variables. **It has been deleted.**
It could never be enabled (any positive `min_reserve` collided with the terminal sweep,
which forces the last day's balance to zero), it compared a base-currency floor against a
foreign-denominated balance, and with the floor at zero the variables were driven to zero
by a tiny tie-break penalty. If a minimum balance is wanted, it belongs in
`_do_nothing_ladder` as the level the balance is measured against, so the ceiling and the
floor both see it — a floor imposed only as a constraint is infeasible, because to the
ceiling a balance you must hold is currency you have no cash-flow need for.

There was also a `t0_debit_only` gate, which permitted same-day dealing only in a
currency already overdrawn at the start of the day. **It has been removed.** It made a
payment due today infeasible, dominated solve time (four currencies over 20 days went
from 19s to 0.4s without it; six over 30 days from a 120s timeout to 1.0s), and could be
gamed by selling a sliver of currency purely to manufacture the overdraft it looked for.

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
- **There is no FX exposure penalty.** A 1bp/day charge on the absolute foreign
  position used to sit in the objective. It was a risk proxy rather than a cash
  cost — nothing debits the account for it — and it was removed because the
  objective is meant to measure money actually lost. It was worth 16–26% of total
  cost on scenarios that hold anything. Two consequences worth knowing: reported
  costs fell (S11 5,268 → 4,968, S13 1,551 → 1,467), and the largest model got
  slower, because charging the position was breaking ties for the solver. On a
  four-currency nine-day book that is roughly a 3× increase in solve time; the
  smaller scenarios are unchanged. If a position limit is wanted, it belongs in
  `_holding_ceiling`, where it constrains rather than prices.
- **The objective is a cost, and `net_terminal_wealth` is the money.** The
  objective measures total value lost to spreads, commission, carry and
  any residual position. `Result.reference_value - total_cost` is exactly the base
  currency the plan delivers. Keep new objective terms at spread scale: notional-
  scale terms would swamp the relative threshold the optimality check relies on.
- **Base currency rows are labelled `"GBP (Base)"`**, not `"GBP"`, in `BalanceSnapshot`,
  `CashLadderEntry` and `CashFlowEntry`. Lookups recover the code with `.split(" ")[0]`.
  A dict keyed on `b.ccy` will not match `cfg.base_ccy`.
- **`utils.py` calls `logging.basicConfig` at import time**, so importing anything in the
  package configures root logging at INFO.
- **`ManualTrade` and `CostBreakdown` have one definition each, in `models.py`.** They
  used to be declared there and again in `cash_manager.py`, where the second shadowed the
  first — and the two `CostBreakdown`s drifted, the local copy gaining the F4 rate and
  unwind terms while the other kept summing four components and understating every total
  by the spread. Import from either module now; it is the same class.
- **A constraint that never binds may still be load-bearing.** `no_loop` was
  removed on the evidence that nothing it forbade was ever chosen — tested at the
  narrowest legal spread and at zero commission. That evidence was gathered in the
  wrong direction. It had to be restored one commit later, because a wash trade can
  be *forced* by `min_trade` rather than chosen, and the cumulative purchase cap had
  been hiding that. Before deleting a constraint, ask what makes its target
  unattractive, and whether every other rule that also makes it unattractive is
  staying.
- **The cost model is still written out four times** — the LP objective,
  `CashManager._compute_cost()`, `Result._compute_balance_costs()` and
  `Result._tenor_decomposition_row()`. Any change to the economics has to land in
  all of them or the comparison tables stop reconciling. This remains the single
  biggest hazard in the codebase.
- **`spread_cost` is not the dealing spread.** It is the deal rate against the mid at
  the *spot* tenor, because that is what `reference_value` prices the whole book at.
  So it bundles the half-spread with the forward points back to spot, and it can be
  **negative** — dealing T+0 in a currency yielding well below base beats the spot-mid
  reference, and the line becomes a credit. Three trades in the shipped scenario
  library do this. It is not double-counted against carry: under covered interest
  parity the points and the carry differential are one quantity seen from two sides,
  charged once here and once there, and they net. `models.py` calls it "Rate vs
  reference" for this reason; `workings.py` now agrees.
- **The web page's cost table is sign-flipped; nothing else is.** The model is a
  cost and is minimised, so in `Result`, `CostBreakdown` and `print_cost()` a cost
  is **positive**. The Cash Management page shows the reverse — a cost negative, a
  benefit positive — because that is how money leaving an account reads. The flip
  lives in `workings.cost_workings(value_impact=True)`, which negates the figures
  *and* rewrites each derivation so its own arithmetic produces the sign shown; a
  row reading -148.13 beside a formula that works out to +148.13 would be worse
  than no formula. Its self-check always compares against the model in the model's
  convention. `Result.total_cost` and the page's NET VALUE IMPACT are therefore
  the same number with opposite signs — anything comparing the two must flip one.
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