# Cash and FX optimiser

A proof-of-concept multi-currency cash manager. Given the opening balance in each
currency and the payments and receipts expected over a short, day-indexed horizon, it
solves a mixed-integer program for the FX trades that meet every obligation at the
lowest total cost.

## The problem

A treasury book holds cash in several currencies and owes money in several more. Over
the coming week it must:

- cover every debit, foreign and domestic, on the day it falls due;
- sweep foreign balances back to base rather than leave positions open;
- do both as cheaply as possible after dealing spreads, commission, and the interest
  earned on credit balances and charged on overdrafts.

That last clause is the whole problem. Funding a payment early means holding a currency
and earning or paying its differential against base. Funding it late means dealing at a
different point on the forward curve. Netting a receipt against a payment avoids two
commissions but only works if the timing lines up. The optimiser prices all of it and
picks the cheapest path.

Minimising that cost is the same thing as **maximising the closing base balance**, and
the model is built that way: `Result.reference_value - total_cost` is exactly the base
currency the plan delivers.

## Quick start

Everything runs from the repository root. Modules use absolute imports rooted at
`scripts.cash_optimizer_poc`, so `PYTHONPATH` must include it.

```python
from scripts.cash_optimizer_poc.cash_manager import CashManager
from scripts.cash_optimizer_poc.models import Config, CashFlowSet

cfg = Config()
cf = CashFlowSet(horizon_days=cfg.horizon_days)
cf.add("USD", 1, -100_000)          # a dollar payable on day 1

mgr = CashManager(cfg, cf, opening_balances={"GBP": 500_000, "USD": 50_000})
mgr.print_cash_ladder()

result = mgr.solve_optimal()
result.print_summary()
result.print_cost(cfg)              # per-trade cost attribution
```

Or step through the scenario library:

```python
from scripts.cash_optimizer_poc.scenarios import load_library, build_cash_manager

for sc in load_library():
    build_cash_manager(sc).solve_optimal().print_summary()
```

### Tests

```bash
python3 -m unittest discover -s tests -v      # 145 cases, ~15 seconds
```

### Web page

```bash
PYTHONPATH=. python3 -m scripts.web_tools.app     # http://localhost:8080
```

A NiceGUI page that steps through the scenario library one at a time, lets you edit the
projected balances in place, re-runs the optimiser, and shows the plan with a cost
breakdown that carries the arithmetic behind every line. Hovering a rate shows what it
is worth in basis points per day.

The **What-If** section prices a route you enter by hand against the same ladder and
the same cost model, with no solver run, and reads it line by line against the
optimiser's plan. Because both plans are priced by the same code, a difference in the
cost is a difference in the plan rather than in how it was measured, and the per-line
difference tells you *where* the money went.

It leads with the rule rather than the number. `execute_trades` prices whatever it is
handed and checks only currency, tenor, horizon and size, so a hand plan can take
routes the optimiser was forbidden to consider and appear to beat it — and since the
constraints exist to forbid profitable speculation, that is the usual way it wins. A
saving obtained by breaking a rule is shown as **void**, with the rule named. A route
that is cheaper with nothing broken gets a third verdict, because against a true
optimum that should be impossible and the baseline is then the thing to doubt.

The builder opens from a button that appears once you have run the optimiser, and the
route you enter is priced against whatever plan came back. That includes a scenario the
solver calls `Infeasible` — S16 — where there is no baseline to compare against but
pricing a hand plan is still the only way to see what the binding constraint costs.

## Requirements

`pulp` is the only hard dependency and it bundles the CBC solver. **Install `highspy`
as well** — CBC 2.10.3 returns provably sub-optimal plans on this model and labels them
`Optimal`. The solver prefers HiGHS when it is available and falls back to CBC, where a
verification pass usually recovers the better plan and sets `Result.optimality_unproven`.

`nicegui` is needed only for the web page.

## How it works

### FX conventions

Quotes are **base currency per one unit of foreign**, bid and ask, per currency per
tenor.

| You do | You deal at | Base moves | Foreign moves |
|---|---|---|---|
| **Buy** foreign | the **ask** | −(ask × amount) | +amount |
| **Sell** foreign | the **bid** | +(bid × amount) | −amount |

Carry is valued at the rate a position would actually be closed at: a long at the spot
bid, a short at the spot ask. Interest accrues on a day count that varies by currency —
sterling and yen on 365, the dollar and euro on 360 — so two rates a quarter of a point
apart are not a quarter of a point apart per day.

Days are integers from 0 to `horizon_days - 1`, not calendar dates. Weekends, holidays
and settlement calendars are outside the model. Tenors map a name to a settlement lag,
so a trade dealt on day 3 at `T2` settles on day 5.

### Constraints

Four are switchable, on `Config.constraints`:

- **`holding_ceiling`** is the anti-speculation policy, and the one that does most of
  the work. Two bounds per currency per day keep the balance inside the corridor the
  cash flows themselves define: a ceiling at the funding hole a trade dealt today could
  still settle against, plus whatever part of banked receipts a remaining outflow will
  consume; and a floor at the deepest overdraft the cash flows actually dig. The reach
  window on the ceiling is what makes it anti-speculation rather than a size limit —
  owning currency before the obligation is a position, not funding, so the ceiling is
  zero until the obligation comes within range.
- **`settle_on_need_only`** sets how wide that window is, and ships **on**. With it on
  the window closes to the single day: the balance may only be positive where the
  ladder is itself overdrawn, so a purchase settles on the very day the money leaves
  and the currency is never held overnight. Turn it off and the window opens to the
  full settlement lag, which restores a free choice of tenor and collects the carry a
  short holding earns. Ten of the twenty-four solved scenarios change plan between the
  two, each paying back exactly the carry the wider window collects. It is inert while
  `holding_ceiling` is off, and it narrows only the acquisition term — money the
  account was *given* is untouched, or a receipt would have to be sold and rebought.
- **`sweep_opening_surplus`** forces foreign credit that is on the books today and spoken
  for by nothing to be *dealt* today, at any tenor. Off by default. The ceiling says when a
  position must be **gone**, not when the decision must be **made**, and its deadline is
  measured from today — so a plan can say "sell in two days" every morning and never sell.
  This constrains the trade instead of the balance, which breaks that loop. Only day 0 is
  constrained: a receipt landing later is swept by the ceiling on arrival, or becomes
  *today's* opening balance at the next re-plan.
- **`terminal_sweep`** forces every foreign balance to zero on the last day.
- **`no_loop`** forbids buying and selling one currency for the same settlement day. It
  looks inert — the objective rejects a wash trade at any spread — but it binds where
  one is *forced*: give the model a need below the minimum ticket and it will otherwise
  deal two legal tickets netting to an illegal amount, which the corridor cannot see
  because the position nets to zero every day.

`docs/holding-corridor.html` explains the ceiling, the floor and the grace period in plain
English, with the arithmetic worked through on real scenarios — worth reading before
changing any of them. `docs/sweeping-the-surplus.html` is the analysis behind
`sweep_opening_surplus` — why unearmarked foreign credit has to be *dealt* on day 0 rather than
merely gone by some later day, and why constraining day 0 alone is sufficient. Both are single self-contained files; open them in a
browser.

Balance evolution, balance decomposition, activation linking and commission-tier linking
are structural and always applied.

Because base overdrafts are permitted, a shortage of cash surfaces as a negative closing
balance rather than as infeasibility. `Infeasible` therefore always means a constraint
conflict; toggling one flag off and re-solving identifies which.

### Scenario library

`scenarios/cash_scenarios.json` holds 25 scenarios varying base currency across GBP,
USD, EUR, JPY and CHF, which foreign currencies are in play, carry rates, dealing
spreads and commission schedules. Each carries a narrative, an expectation of what a
correct plan looks like, and the cost reasoning for why that plan is the cheapest.

The FX book is **derived, not invented**: `scenarios/generate.py` crosses every pair off
a single USD table and computes each tenor by covered interest parity, so the book is
arbitrage-free and forward points carry the right sign against each rate differential.
Edit the generator and re-run it; the JSON is output, not source.

```bash
python3 scenarios/generate.py
```

Five are deliberately not plain successes. **S16** is `Infeasible` because the need sits
below the minimum ticket and **S25** sits exactly on it from the other side; **S20**
returns a plan with `insufficient_funds` set; **S07** is the carry test, where switching
the holding ceiling off makes the plan *cheaper* and that fall is the speculation being
taken; **S21** puts a currency in the cash flows that the config never declares.

## Layout

| Path | Role |
|---|---|
| `scripts/cash_optimizer_poc/models.py` | Config, dataclasses, enums |
| `scripts/cash_optimizer_poc/optimizer.py` | Builds and solves the LP/MIP |
| `scripts/cash_optimizer_poc/result.py` | Formatting and cost attribution |
| `scripts/cash_optimizer_poc/cash_manager.py` | Stateful workbench; manual trades and comparison |
| `scripts/cash_optimizer_poc/workings.py` | Derives the arithmetic behind each cost line |
| `scripts/cash_optimizer_poc/scenarios.py` | Loads the JSON library into solvable managers |
| `scripts/cash_optimizer_poc/utils.py` | Pivoted-table formatters; configures logging on import |
| `scripts/web_tools/` | NiceGUI page |
| `tests/` | 116 cases |

## Known limitations

- **The cost model is written out five times** — the LP objective, `CashManager._compute_cost`,
  two places in `Result`, and `workings.py`. A change to the economics has to land in all
  of them or the comparison tables stop reconciling. This is the single biggest hazard in
  the codebase. `workings.py` at least checks itself against the model and reports a
  mismatch rather than printing a derivation that does not reconcile.
- **The forward curve does not roll.** One quote table serves every day, so the T+0 rate
  is the same number whether it is dealt on day 0 or day 4. In reality the spot rate on
  day 2 would be today's T+2 forward if nothing else moved. The model discounts forwards
  for the passage of time while holding spot fixed through the same passage, which
  biases it toward dealing as early as the ceiling allows at the longest tenor. It does
  not affect position sizing, which the corridor governs — read the *timing* and *tenor*
  choices with that in mind.
- **Round trips are not forbidden.** Buying on Monday and selling on Wednesday is
  unconstrained; `no_loop` only blocks a buy and a sell landing on the same day. None
  appear in practice because the spread and two commissions make them lossy, but that is
  the objective protecting you rather than a rule, and it holds only while the carry
  available stays below the round-trip cost.
- **The web page's cost table is sign-flipped.** It reads as value impact — a cost
  negative, a benefit positive — while the model, `Result` and `print_cost()` keep the
  cost convention where a cost is positive. The same number appears with opposite signs
  in the two places.
- There is a large commented-out integration layer at the bottom of `cash_manager.py`
  that maps live projection, FX and interest-rate feeds into `Config` and `CashFlowSet`.
  Its dependencies are not vendored here, so it does not run; it is kept as the
  reference for how real data is meant to arrive.
