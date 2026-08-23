"""Regression tests for the cash optimizer.

Run from the repository root::

    python -m unittest discover -s tests -v

Each test class corresponds to a finding from the model audit.  The numbers
asserted here were established by running the model, not derived on paper, so
a failure means behaviour has changed — not that the arithmetic was wrong.

Solvers report objective values to limited precision (differences of ~3e-5
relative were observed between CBC and HiGHS on identical plans), so cost
comparisons use relative tolerances rather than exact equality.
"""
import logging
import unittest

logging.disable(logging.INFO)

from scripts.cash_optimizer_poc.models import (
    CashFlowSet,
    CostBreakdown,
    CommissionTier,
    Config,
    ConstraintFlags,
    Direction,
    FXTenorQuote,
)
from scripts.cash_optimizer_poc.optimizer import (
    CashOptimizer,
    SolverFailure,
)
from scripts.cash_optimizer_poc.cash_manager import CashManager, ManualTrade


# A desk without same-day settlement: nothing settles on the day it is
# dealt. Since monotonic drawdown was corrected to apply to the holding
# rather than the signed balance, a payment due today under this setup is
# feasible again — it is funded at T+1 and the overdraft repaid — so this
# is no longer an infeasibility case, only a restricted one.
NO_SAME_DAY = dict(
    tenors={"T1": 1, "T2": 2},
    spot_tenor="T2",
    fx_quotes={c: {k: v for k, v in q.items() if k in ("T1", "T2")}
               for c, q in Config().fx_quotes.items()},
)

REL = 1e-4  # cost comparisons: relative, to survive cross-solver precision


def solve(flows=(), opening=None, horizon=5, solver=None, **cfg_kw):
    """Build and solve a scenario. ``flows`` is a list of (ccy, day, amount)."""
    cfg = Config(horizon_days=horizon, **cfg_kw)
    cf = CashFlowSet(horizon_days=horizon)
    for ccy, day, amount in flows:
        cf.add(ccy, day, amount)
    optimizer = CashOptimizer(cfg, cf, opening_balances=dict(opening or {}))
    return optimizer.solve(solver_name=solver)


def plan_of(result):
    """Normalised trade list, for comparing plans rather than costs."""
    return sorted(
        (t.ccy, t.day, t.tenor, t.direction.value, round(t.amount, 2))
        for t in result.trades
    )


class CostAssertions(unittest.TestCase):
    def assertCostClose(self, actual, expected, msg=None):
        tol = max(abs(expected) * REL, 0.005)
        self.assertAlmostEqual(actual, expected, delta=tol, msg=msg)


# ─────────────────────────────────────────────────────────────
# F1 / F20 — configuration must not silently produce nonsense
# ─────────────────────────────────────────────────────────────

class TestDefaultConfiguration(CostAssertions):
    """The shipped defaults used to make every model infeasible."""

    def test_default_config_solves_an_ordinary_funding_problem(self):
        r = solve([("USD", 1, -100_000)], {"GBP": 500_000, "USD": 50_000})
        self.assertEqual(r.status, "Optimal")
        self.assertTrue(r.trades, "expected a funding trade, got none")

    def test_max_trade_above_commission_capacity_is_rejected(self):
        # Trades are bounded by the commission schedule, so a max_trade above
        # it is unreachable — and used to surface as a bare "Infeasible".
        with self.assertRaises(ValueError) as ctx:
            Config(max_trade=5e11)
        self.assertIn("max_trade", str(ctx.exception))

    def test_balance_ceiling_below_the_ladder_is_rejected_with_detail(self):
        cfg = Config(max_balance=1_000_000.0)
        cf = CashFlowSet(horizon_days=5)
        cf.add("USD", 2, -100_000)
        with self.assertRaises(ValueError) as ctx:
            CashOptimizer(cfg, cf, opening_balances={"GBP": 50_000_000})
        message = str(ctx.exception)
        self.assertIn("GBP", message)
        self.assertIn("max_balance", message)

    def test_big_m_is_a_trade_constant_not_a_balance_ceiling(self):
        # big_m must track the commission schedule, not the raw max_trade.
        cfg = Config(max_trade=1_000_000.0)
        self.assertLessEqual(cfg.big_m, 1_000_001.0)


# ─────────────────────────────────────────────────────────────
# F2 — the same problem must solve at any size
# ─────────────────────────────────────────────────────────────

class TestScaleInvariance(CostAssertions):
    """Identical structure, six orders of magnitude of balance."""

    SCALES = [1e0, 1e2, 1e3, 1e5, 1e6, 1e7]

    def test_same_problem_solves_at_every_magnitude(self):
        for scale in self.SCALES:
            with self.subTest(scale=scale):
                r = solve([("USD", 2, -40.0 * scale)], {"GBP": 250.0 * scale})
                self.assertEqual(r.status, "Optimal")
                self.assertTrue(r.trades)

    def test_cost_scales_linearly_with_size(self):
        small = solve([("USD", 2, -40_000)], {"GBP": 250_000})
        large = solve([("USD", 2, -400_000)], {"GBP": 2_500_000})
        self.assertCostClose(large.total_cost, small.total_cost * 10)


# ─────────────────────────────────────────────────────────────
# F3 — a plan the solver calls optimal may not be
# ─────────────────────────────────────────────────────────────

class TestSolverReliability(CostAssertions):
    """CBC returns provably sub-optimal plans on this model and labels them
    Optimal.  The verification pass has to catch and correct that."""

    # The two scenarios that used to expose CBC returning a sub-optimal plan
    # as "Optimal" no longer do: removing the same-day gate took out the
    # sign-pin machinery that made the model hard to search.  That is not a
    # fix for CBC — the defect is in the solver — so the verification stays
    # as a safety net.  These tests assert it is wired and currently silent.

    def test_historical_cbc_failures_now_solve_correctly(self):
        # Was 34,733.58 against a true optimum of 32,003.10.
        full = solve([("USD", 2, -40_000_000)], {"GBP": 250_000_000},
                     solver="PULP_CBC_CMD")
        quotes = {c: {k: v for k, v in q.items() if k in ("T0", "T2")}
                  for c, q in Config().fx_quotes.items()}
        restricted = solve([("USD", 2, -40_000_000)], {"GBP": 250_000_000},
                           solver="PULP_CBC_CMD",
                           tenors={"T0": 0, "T2": 2}, spot_tenor="T2",
                           fx_quotes=quotes)
        self.assertLessEqual(
            full.total_cost, restricted.total_cost * (1 + REL) + 0.01,
            "removing an option must not improve the reported optimum")

    def test_verification_is_wired_and_reports_a_bound(self):
        r = solve([("USD", 2, -100_000)], {"GBP": 5_000_000})
        self.assertIsNotNone(r.lp_bound)
        self.assertIsNotNone(r.optimality_gap)
        self.assertIsNotNone(r.solver_used)

    def test_reported_cost_survives_the_verification_pass(self):
        # Building a constraint from the objective mutates it in PuLP; if that
        # leaks, the reported cost collapses to zero.
        r = solve([("USD", 2, -40_000_000)], {"GBP": 250_000_000},
                  solver="PULP_CBC_CMD")
        self.assertGreater(r.total_cost, 1.0)

    def test_verification_does_not_cry_wolf_on_ordinary_solves(self):
        # The check must only fire when a cheaper plan was actually produced.
        for flows, opening in [
            ([("USD", 1, -100_000)], {"GBP": 500_000, "USD": 50_000}),
            ([("USD", 2, -100_000)], {"GBP": 5_000_000}),
            ([], {"USD": 1_000_000}),
            ([("USD", 1, -100_000), ("USD", 3, 100_000)], {"GBP": 5_000_000}),
        ]:
            with self.subTest(flows=flows):
                r = solve(flows, opening)
                self.assertFalse(
                    r.optimality_unproven,
                    "no cheaper plan exists here, so nothing should be flagged")

    def test_disabling_verification_is_honoured(self):
        r = solve([("USD", 2, -100_000)], {"GBP": 5_000_000},
                  verify_optimality=False)
        self.assertEqual(r.status, "Optimal")
        self.assertIsNone(r.improved_from)


# ─────────────────────────────────────────────────────────────
# F4 — the objective must see the rate it deals at
# ─────────────────────────────────────────────────────────────

class TestTradeRateValuation(CostAssertions):
    """Without this the model settles too early on every trade, and refuses
    to liquidate at all when the terminal sweep is off."""

    def test_picks_the_tenor_that_actually_pays_more(self):
        r = solve([], {"USD": 1_000_000})
        self.assertEqual(len(r.trades), 1)
        self.assertEqual(r.trades[0].tenor, "T2",
                         "T2 fetches the best rate net of carry")
        self.assertEqual(r.trades[0].direction, Direction.SELL)

    def test_without_the_fix_it_settles_too_early(self):
        # Guards the guard: confirms the test above is actually exercising the
        # new term rather than passing for unrelated reasons.  Blind to the
        # rate, the objective takes the tenor with the least carry, which is
        # same-day — the worst outcome on offer, and worth 2.07 bps.
        r = solve([], {"USD": 1_000_000}, value_trade_rates=False)
        self.assertEqual(r.trades[0].tenor, "T0")

    def test_liquidates_even_with_the_terminal_sweep_off(self):
        flags = ConstraintFlags(terminal_sweep=False, holding_ceiling=False)
        r = solve([], {"USD": 1_000_000}, constraints=flags)
        terminal = [b.balance for b in r.balances
                    if b.ccy == "USD" and b.day == 4][0]
        self.assertAlmostEqual(terminal, 0.0, delta=1.0,
                               msg="should convert rather than sit on the position")

    def test_net_terminal_wealth_reconciles_with_the_ladder(self):
        r = solve([], {"USD": 1_000_000})
        gbp = [b.balance for b in r.balances
               if b.ccy.startswith("GBP") and b.day == 4][0]
        # The ladder already carries the spread; commission and carry do not
        # touch a balance anywhere, so they are the difference.
        breakdown = CashManager(
            Config(), CashFlowSet(horizon_days=5),
            opening_balances={"USD": 1_000_000},
        )._compute_cost(r.balances, r.trades)
        non_ladder = (breakdown.commission_cost + breakdown.credit_carry_cost
                      + breakdown.debit_carry_cost + breakdown.fx_exposure_cost)
        self.assertCostClose(r.net_terminal_wealth, gbp - non_ladder)

    def test_reference_value_is_the_frictionless_book(self):
        r = solve([], {"USD": 1_000_000})
        self.assertCostClose(r.reference_value,
                             Config().fx_spot_mid("USD") * 1_000_000)
        self.assertCostClose(r.net_terminal_wealth,
                             r.reference_value - r.total_cost)


# ─────────────────────────────────────────────────────────────
# F9 — the purchase cap must respect timing
# ─────────────────────────────────────────────────────────────

class TestFundingNeedIsMeasuredByTiming(CostAssertions):
    """A genuine timing gap must be fundable, and a receipt that already
    covers a later payment must not require buying anything.

    Netting every inflow against every outflow across the horizon gets
    both of these wrong in opposite directions, which is why the need is
    measured by walking the ladder day by day."""

    CHEAP = dict(
        commission_tiers=[CommissionTier(500_000_000, 0.1)],
        debit_carry_pa={"GBP": 5.0, "USD": 40.0, "EUR": 4.5, "JPY": 3.0},
    )

    def test_funds_a_genuine_timing_gap(self):
        r = solve([("USD", 1, -1_000_000), ("USD", 3, 1_000_000)],
                  {"GBP": 20_000_000}, **self.CHEAP)
        self.assertEqual(r.status, "Optimal")
        self.assertTrue(r.trades,
                        "the currency is genuinely short for two days")

    def test_declines_when_the_commission_exceeds_the_interest_saved(self):
        r = solve([("USD", 1, -100_000), ("USD", 3, 100_000)],
                  {"GBP": 5_000_000})
        self.assertEqual(r.trades, [],
                         "overdraft is cheaper here than two commissions")

    def test_shortfall_profile_tracks_the_running_position(self):
        cfg = Config()
        cf = CashFlowSet(horizon_days=5)
        cf.add("USD", 1, -1_000_000)
        cf.add("USD", 3, 1_000_000)
        opt = CashOptimizer(cfg, cf, opening_balances={"GBP": 20_000_000})
        profile = opt._shortfall_profile("USD")
        self.assertEqual([round(v) for v in profile],
                         [0, 1_000_000, 1_000_000, 0, 0])

    def test_shortfall_profile_nets_the_opening_balance(self):
        cfg = Config()
        cf = CashFlowSet(horizon_days=5)
        cf.add("USD", 2, -100_000)
        opt = CashOptimizer(cfg, cf,
                            opening_balances={"GBP": 5e6, "USD": 30_000})
        self.assertEqual([round(v) for v in opt._shortfall_profile("USD")],
                         [0, 0, 70_000, 70_000, 70_000])

    def test_no_shortfall_means_no_purchases_permitted(self):
        # Receipt arrives before the payment: nothing needs buying.
        r = solve([("USD", 1, 1_000_000), ("USD", 3, -1_000_000)],
                  {"GBP": 20_000_000}, **self.CHEAP)
        buys = [t for t in r.trades if t.direction == Direction.BUY]
        self.assertEqual(buys, [])


# ─────────────────────────────────────────────────────────────
# F21 — the printed breakdown must add up
# ─────────────────────────────────────────────────────────────

class TestCostAttribution(CostAssertions):

    SCENARIOS = [
        ([], {"USD": 1_000_000}),
        ([("USD", 2, -100_000)], {"GBP": 5_000_000}),
        ([("USD", 2, -100_000), ("EUR", 1, -250_000)], {"GBP": 5_000_000}),
        ([("USD", 2, -40_000_000)], {"GBP": 250_000_000}),
    ]

    def test_attribution_table_reconciles_against_the_objective(self):
        import re

        for flows, opening in self.SCENARIOS:
            with self.subTest(flows=flows):
                r = solve(flows, opening)
                text = r.format_cost(Config())

                def figure(label):
                    for line in text.splitlines():
                        if label in line:
                            found = re.findall(r"-?[\d,]+\.\d+", line)
                            if found:
                                return float(found[-1].replace(",", ""))
                    return None

                residual = figure("Unexplained residual")
                self.assertIsNotNone(residual)
                self.assertAlmostEqual(
                    residual, 0.0, delta=0.01,
                    msg="printed parts must sum to the objective")


# ─────────────────────────────────────────────────────────────
# Guard rails that must keep holding
# ─────────────────────────────────────────────────────────────

class TestGuardRails(CostAssertions):

    def test_phasing_is_gone(self):
        # F12-F14: it crashed on a currency not already held, conflicted with
        # the no-carry rule, and applied its tolerance in the wrong direction.
        self.assertFalse(hasattr(ConstraintFlags(), "phasing"))
        import scripts.cash_optimizer_poc.models as models
        self.assertFalse(hasattr(models, "PhasingPlan"))
        import inspect
        from scripts.cash_optimizer_poc.optimizer import CashOptimizer as CO
        self.assertNotIn("phasing", inspect.signature(CO.__init__).parameters)

    def test_reserve_subsystem_is_gone(self):
        # F7/F8: it could never be enabled and reported only zeros.
        self.assertFalse(hasattr(Config(), "min_reserve"))
        self.assertFalse(hasattr(Config(), "reserve_tiebreak_penalty"))
        self.assertFalse(hasattr(ConstraintFlags(), "reserve"))
        r = solve([("USD", 2, -100_000)], {"GBP": 5_000_000})
        self.assertFalse(hasattr(r, "reserves"))
        self.assertFalse(hasattr(r, "reserve_attribution"))
        self.assertNotIn("RESERVE", r.format_summary().upper())

    def test_speculation_stays_blocked_at_any_carry_differential(self):
        flags = ConstraintFlags(terminal_sweep=False)
        for usd_rate in [5.0, 50.0, 200.0]:
            with self.subTest(usd_rate=usd_rate):
                r = solve([], {"GBP": 50_000_000, "USD": 1.0},
                          constraints=flags,
                          commission_tiers=[CommissionTier(500_000_000, 0.0)],
                          fx_exposure_bps_per_day=0.0,
                          credit_carry_pa={"GBP": 3.65, "USD": usd_rate,
                                           "EUR": 0.2, "JPY": 0.01})
                peak = max((b.balance for b in r.balances if b.ccy == "USD"),
                           default=0.0)
                self.assertLess(peak, 1_000.0,
                                "no position should be built without a need")

    def test_inverted_quotes_are_rejected(self):
        quotes = dict(Config().fx_quotes)
        quotes["USD"] = {
            "T0": FXTenorQuote(bid=0.99, ask=0.79),
            "T1": FXTenorQuote(bid=0.7897, ask=0.7903),
            "T2": FXTenorQuote(bid=0.7898, ask=0.7902),
        }
        with self.assertRaises(ValueError):
            Config(fx_quotes=quotes)

    def test_cash_flow_outside_the_horizon_is_rejected(self):
        cf = CashFlowSet(horizon_days=5)
        with self.assertRaises(ValueError):
            cf.add("USD", 99, -50_000_000)

    def test_terminal_sweep_clears_foreign_balances(self):
        r = solve([("USD", 2, -100_000)], {"GBP": 5_000_000, "USD": 500_000})
        for b in r.balances:
            if b.ccy == "USD" and b.day == 4:
                self.assertAlmostEqual(b.balance, 0.0, delta=1.0)


# ─────────────────────────────────────────────────────────────
# Optimisation invariants — these catch F3-class regressions
# ─────────────────────────────────────────────────────────────

class TestOptimizationInvariants(CostAssertions):
    """Properties that must hold for any correct optimiser.  A failure here
    means the search is returning something it should not, which is exactly
    how the original sub-optimality was found."""

    SCENARIOS = [
        ([("USD", 2, -100_000)], {"GBP": 5_000_000}),
        ([], {"USD": 1_000_000}),
        ([("USD", 2, -100_000), ("EUR", 1, -250_000)], {"GBP": 5_000_000}),
    ]

    def test_relaxing_a_constraint_cannot_raise_the_optimum(self):
        for flows, opening in self.SCENARIOS:
            for flag in ["holding_ceiling", "terminal_sweep"]:
                with self.subTest(flows=flows, flag=flag):
                    constrained = solve(flows, opening)
                    relaxed = solve(flows, opening,
                                    constraints=ConstraintFlags(**{flag: False}))
                    if constrained.total_cost is None or relaxed.total_cost is None:
                        self.skipTest("scenario infeasible under one variant")
                    self.assertLessEqual(
                        relaxed.total_cost,
                        constrained.total_cost * (1 + REL) + 0.01,
                        f"removing {flag} made the reported optimum worse")

    def test_objective_is_never_below_a_valid_lower_bound(self):
        for flows, opening in self.SCENARIOS:
            with self.subTest(flows=flows):
                r = solve(flows, opening)
                if r.lp_bound is None or r.total_cost is None:
                    continue
                self.assertGreaterEqual(
                    r.total_cost, r.lp_bound - max(abs(r.lp_bound) * REL, 0.01),
                    "a plan cannot cost less than the relaxation allows")

    def test_solving_twice_gives_the_same_plan(self):
        first = solve([("USD", 2, -100_000)], {"GBP": 5_000_000})
        second = solve([("USD", 2, -100_000)], {"GBP": 5_000_000})
        self.assertEqual(plan_of(first), plan_of(second))

    def test_solve_cannot_be_called_twice_on_one_instance(self):
        cfg = Config()
        cf = CashFlowSet(horizon_days=5)
        cf.add("USD", 2, -100_000)
        opt = CashOptimizer(cfg, cf, opening_balances={"GBP": 5_000_000})
        opt.solve()
        with self.assertRaises(RuntimeError):
            opt.solve()


# ─────────────────────────────────────────────────────────────
# Known-good plans — pin the behaviour, not just the cost
# ─────────────────────────────────────────────────────────────

class TestKnownPlans(CostAssertions):

    def test_pre_funds_a_mid_horizon_payment(self):
        r = solve([("USD", 2, -100_000)], {"GBP": 5_000_000})
        self.assertEqual(
            plan_of(r), [("USD", 0, "T2", "BUY", 100_000.0)],
            "should buy once, settling exactly on the payment date")

    def test_covers_an_overnight_shortfall(self):
        r = solve([("USD", 1, -100_000)], {"GBP": 500_000, "USD": 50_000})
        self.assertEqual(plan_of(r), [("USD", 0, "T1", "BUY", 50_000.0)])

    def test_does_nothing_when_there_is_nothing_to_do(self):
        r = solve([], {"GBP": 1_000_000})
        self.assertEqual(r.trades, [])

    def test_manual_evaluation_reproduces_the_optimizer_cost(self):
        cfg = Config()
        cf = CashFlowSet(horizon_days=5)
        cf.add("USD", 2, -100_000)
        manager = CashManager(cfg, cf, opening_balances={"GBP": 5_000_000})
        optimal = manager.solve_optimal()
        replay = manager.execute_trades(
            [ManualTrade(t.ccy, t.day, t.tenor, t.direction, t.amount)
             for t in optimal.trades],
            label="replay")
        self.assertCostClose(replay.total_cost, optimal.total_cost)


# ─────────────────────────────────────────────────────────────
# Known open findings — documented, not yet fixed
# ─────────────────────────────────────────────────────────────

class TestSameDaySettlement(CostAssertions):
    """The same-day gate was removed: it made a payment due today infeasible,
    dominated solve time, and could be gamed by selling a sliver of currency
    to manufacture the overdraft it looked for."""

    def test_payment_due_today_can_be_funded(self):
        r = solve([("USD", 0, -100_000)], {"GBP": 5_000_000})
        self.assertEqual(r.status, "Optimal", "F5: a payment due today")
        # Funded on day 0. The tenor is the model's choice: since monotonic
        # drawdown was corrected, settling at T+1 and repaying one day of
        # overdraft is open to it, and at the default curve that is cheaper
        # than same-day by about 9 in base currency — a better rate is worth
        # more than the day of interest.
        self.assertEqual(len(r.trades), 1)
        trade = r.trades[0]
        self.assertEqual((trade.ccy, trade.day, trade.direction.value,
                          round(trade.amount, 2)),
                         ("USD", 0, "BUY", 100_000.0))

    def test_single_day_horizon_is_solvable(self):
        r = solve([], {"GBP": 1_000_000, "USD": 100_000}, horizon=1)
        self.assertEqual(r.status, "Optimal")
        self.assertEqual(plan_of(r), [("USD", 0, "T0", "SELL", 100_000.0)])

    def test_removal_did_not_change_other_plans(self):
        # Every case that solved before must produce the identical plan.
        for flows, opening, expected in [
            ([("USD", 2, -100_000)], {"GBP": 5_000_000},
             [("USD", 0, "T2", "BUY", 100_000.0)]),
            ([("USD", 1, -100_000)], {"GBP": 500_000, "USD": 50_000},
             [("USD", 0, "T1", "BUY", 50_000.0)]),
            ([], {"USD": 1_000_000},
             [("USD", 0, "T2", "SELL", 1_000_000.0)]),
        ]:
            with self.subTest(flows=flows):
                self.assertEqual(plan_of(solve(flows, opening)), expected)

    def test_large_multi_currency_models_solve_quickly(self):
        # This shape used to exceed the 120s limit and return nothing.
        import time
        ccys = ["GBP", "USD", "EUR", "JPY", "CHF"]
        quotes = {c: {t: FXTenorQuote(bid=0.7895 + 0.0001 * i,
                                      ask=0.7905 + 0.0001 * i)
                      for i, t in enumerate(["T0", "T1", "T2"])}
                  for c in ccys[1:]}
        flows = [(c, 2 + i, -100_000 * (i + 1)) for i, c in enumerate(ccys[1:])]
        started = time.time()
        r = solve(flows, {"GBP": 50_000_000}, horizon=20, currencies=ccys,
                  fx_quotes=quotes,
                  credit_carry_pa={c: 1.0 for c in ccys},
                  debit_carry_pa={c: 5.0 for c in ccys})
        elapsed = time.time() - started
        self.assertEqual(r.status, "Optimal")
        self.assertLess(elapsed, 30.0,
                        "four currencies over 20 days should not take minutes")


class TestInsufficientFunds(CostAssertions):
    """Insufficient funds means one thing only: with every foreign balance
    converted back to base, the account still closes in debit.  An infeasible
    model is a constraint conflict, never a funding shortfall — base
    overdrafts are permitted, so a lack of cash shows up as a negative
    closing balance instead."""

    SHORT = ([("USD", 2, -100_000)], {"GBP": 0.0})

    def test_a_real_shortfall_is_flagged(self):
        r = solve(*self.SHORT)
        self.assertTrue(r.insufficient_funds)
        self.assertLess(r.terminal_base_equivalent, 0.0)
        self.assertCostClose(r.shortfall, -r.terminal_base_equivalent)

    def test_a_shortfall_still_returns_a_plan(self):
        # The old gate refused before the model was built, so the user was
        # told to raise cash without being told how much or when.
        r = solve(*self.SHORT)
        self.assertEqual(r.status, "Optimal")
        self.assertTrue(r.trades, "a fundable plan should still be produced")

    def test_ample_cash_is_not_flagged(self):
        r = solve([("USD", 2, -100_000)], {"GBP": 5_000_000})
        self.assertFalse(r.insufficient_funds)
        self.assertGreater(r.terminal_base_equivalent, 0.0)

    def test_constraint_conflict_is_not_called_a_funding_shortfall(self):
        # Ample cash, but the funding required is smaller than the smallest
        # dealable ticket and the anti-speculative cap will not permit
        # buying a whole one, so no legal plan exists.
        r = solve([("USD", 2, -1_000)], {"GBP": 5_000_000}, min_trade=50_000.0)
        self.assertEqual(r.status, "Infeasible")
        self.assertFalse(r.insufficient_funds,
                         "ample cash: this is a constraint conflict")
        self.assertNotIn("INSUFFICIENT FUNDS", r.format_summary())
        self.assertIn("NO FEASIBLE PLAN", r.format_summary())

    def test_foreign_credit_offsets_a_base_debit(self):
        # A base overdraft covered by a foreign holding is not a shortfall:
        # the holding converts back and covers it.
        r = solve([("GBP", 1, -500_000)], {"GBP": 100_000, "USD": 1_000_000})
        self.assertFalse(r.insufficient_funds)
        self.assertGreater(r.terminal_base_equivalent, 0.0)

    def test_the_threshold_sits_where_the_balance_crosses_zero(self):
        for opening, expected in [(80_000.0, False), (79_100.0, False),
                                  (79_000.0, True), (78_000.0, True)]:
            with self.subTest(opening=opening):
                r = solve([("USD", 2, -100_000)], {"GBP": opening})
                self.assertEqual(r.insufficient_funds, expected)

    def test_closing_balance_accounts_for_every_currency(self):
        r = solve([("USD", 2, -100_000)], {"GBP": 200_000, "EUR": 50_000})
        total = sum(r.shortfall_detail.values())
        self.assertCostClose(r.terminal_base_equivalent, total)


class TestNonOptimalStatus(CostAssertions):
    """A status that is neither Optimal nor Infeasible used to return an
    empty Result — no trades, no cost, no exception — which a caller reading
    result.trades could not tell apart from "nothing worth doing" (F16)."""

    def test_non_optimal_status_raises(self):
        cfg = Config()
        cf = CashFlowSet(horizon_days=5)
        cf.add("USD", 2, -100_000)
        opt = CashOptimizer(cfg, cf, opening_balances={"GBP": 5_000_000})
        opt.solve()
        for status in ("Not Solved", "Undefined", "Unbounded"):
            with self.subTest(status=status):
                with self.assertRaises(SolverFailure):
                    opt._extract_results(status)

    def test_the_failure_carries_diagnostics(self):
        cfg = Config()
        cf = CashFlowSet(horizon_days=5)
        cf.add("USD", 2, -100_000)
        opt = CashOptimizer(cfg, cf, opening_balances={"GBP": 5_000_000})
        opt.solve()
        with self.assertRaises(SolverFailure) as ctx:
            opt._extract_results("Not Solved", solver_used="PULP_CBC_CMD")
        self.assertEqual(ctx.exception.status, "Not Solved")
        self.assertEqual(ctx.exception.solver, "PULP_CBC_CMD")
        self.assertIn("not solved to optimality", str(ctx.exception))

    def test_it_is_catchable_as_a_runtime_error(self):
        # Subclassing RuntimeError keeps existing handlers working.
        self.assertTrue(issubclass(SolverFailure, RuntimeError))

    def test_a_real_timeout_raises_rather_than_returning_nothing(self):
        # Large enough not to finish inside the limit.
        ccys = ["GBP", "USD", "EUR", "JPY", "CHF", "AUD", "CAD"]
        quotes = {c: {t: FXTenorQuote(bid=0.7895 + 0.0001 * i,
                                      ask=0.7905 + 0.0001 * i)
                      for i, t in enumerate(["T0", "T1", "T2"])}
                  for c in ccys[1:]}
        cfg = Config(horizon_days=200, currencies=ccys, fx_quotes=quotes,
                     verify_optimality=False,
                     credit_carry_pa={c: 1.0 for c in ccys},
                     debit_carry_pa={c: 5.0 for c in ccys})
        cf = CashFlowSet(horizon_days=200)
        for i, c in enumerate(ccys[1:]):
            for day in range(2 + i, 190, 7):
                cf.add(c, day, -100_000 * (i + 1))
        opt = CashOptimizer(cfg, cf, opening_balances={"GBP": 500_000_000})
        try:
            result = opt.solve(time_limit=2)
        except SolverFailure as exc:
            self.assertEqual(exc.status, "Not Solved")
            self.assertEqual(exc.time_limit, 2)
            return
        # If the solver got there in time, the point still stands: what must
        # never happen is an empty result presented as a plan.
        self.assertEqual(result.status, "Optimal")

    def test_infeasible_still_returns_a_result(self):
        # Infeasibility is an informative outcome, not a solver failure.
        r = solve([("USD", 2, -1_000)], {"GBP": 5_000_000}, min_trade=50_000.0)
        self.assertEqual(r.status, "Infeasible")
        self.assertEqual(r.trades, [])


class TestNaNRejection(CostAssertions):
    """A non-finite figure must never reach the model.  NaN is the dangerous
    one: every comparison against it is false, so "I could not read this"
    answers the activity test the same way "there is nothing here" does, and
    the currency is dropped without a word (F17, F18)."""

    NAN = float("nan")

    def test_nan_cash_flow_is_rejected_at_entry(self):
        cf = CashFlowSet(horizon_days=5)
        with self.assertRaises(ValueError) as ctx:
            cf.add("USD", 2, self.NAN)
        self.assertIn("USD", str(ctx.exception))
        self.assertIn("day 2", str(ctx.exception))

    def test_nan_opening_balance_is_rejected(self):
        cfg = Config()
        cf = CashFlowSet(horizon_days=5)
        with self.assertRaises(ValueError) as ctx:
            CashOptimizer(cfg, cf,
                          opening_balances={"GBP": 5e6, "USD": self.NAN})
        self.assertIn("USD", str(ctx.exception))

    def test_nan_fx_quote_is_rejected_at_config_time(self):
        quotes = dict(Config().fx_quotes)
        quotes["USD"] = {
            "T0": FXTenorQuote(bid=self.NAN, ask=0.7905),
            "T1": FXTenorQuote(bid=0.7897, ask=0.7903),
            "T2": FXTenorQuote(bid=0.7898, ask=0.7902),
        }
        with self.assertRaises(ValueError) as ctx:
            Config(fx_quotes=quotes)
        self.assertIn("T0", str(ctx.exception))

    def test_nan_carry_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(credit_carry_pa={"GBP": 3.65, "USD": self.NAN,
                                    "EUR": 0.2, "JPY": 0.01})

    def test_infinity_is_rejected_too(self):
        cf = CashFlowSet(horizon_days=5)
        with self.assertRaises(ValueError):
            cf.add("USD", 2, float("inf"))

    def test_good_data_still_passes(self):
        r = solve([("USD", 2, -100_000)], {"GBP": 5_000_000})
        self.assertEqual(r.status, "Optimal")


class TestCurrencyDiscovery(CostAssertions):
    """A cash flow may arrive in a currency nobody declared.  It must be
    modelled, not silently ignored (F19) — but only if its market data is
    available, because a rate cannot be inferred from anything."""

    def test_an_undeclared_currency_is_picked_up_from_the_cash_flows(self):
        quotes = dict(Config().fx_quotes)
        quotes["CHF"] = {t: FXTenorQuote(bid=0.88, ask=0.89)
                         for t in Config().tenors}
        cfg = Config(fx_quotes=quotes,
                     credit_carry_pa={"GBP": 3.65, "USD": 0.5, "EUR": 0.2,
                                      "JPY": 0.01, "CHF": 0.1},
                     debit_carry_pa={"GBP": 5.0, "USD": 5.0, "EUR": 4.5,
                                     "JPY": 3.0, "CHF": 4.0})
        cf = CashFlowSet(horizon_days=5)
        cf.add("CHF", 2, -100_000)
        opt = CashOptimizer(cfg, cf, opening_balances={"GBP": 5_000_000})

        self.assertNotIn("CHF", cfg.currencies, "not declared")
        self.assertIn("CHF", opt.foreign_ccys, "but discovered")
        r = opt.solve()
        self.assertEqual(r.status, "Optimal")
        self.assertTrue([t for t in r.trades if t.ccy == "CHF"],
                        "the obligation should be funded, not ignored")
        self.assertTrue([b for b in r.balances if b.ccy == "CHF"],
                        "and should appear in the reported balances")

    def test_the_callers_config_is_not_mutated(self):
        quotes = dict(Config().fx_quotes)
        quotes["CHF"] = {t: FXTenorQuote(bid=0.88, ask=0.89)
                         for t in Config().tenors}
        cfg = Config(fx_quotes=quotes,
                     credit_carry_pa={"GBP": 3.65, "USD": 0.5, "EUR": 0.2,
                                      "JPY": 0.01, "CHF": 0.1},
                     debit_carry_pa={"GBP": 5.0, "USD": 5.0, "EUR": 4.5,
                                     "JPY": 3.0, "CHF": 4.0})
        before = list(cfg.currencies)
        cf = CashFlowSet(horizon_days=5)
        cf.add("CHF", 2, -100_000)
        CashOptimizer(cfg, cf, opening_balances={"GBP": 5_000_000})
        self.assertEqual(cfg.currencies, before)

    def test_a_currency_without_market_data_fails_loudly(self):
        cf = CashFlowSet(horizon_days=5)
        cf.add("CHF", 2, -5_000_000)
        with self.assertRaises(ValueError) as ctx:
            CashOptimizer(Config(), cf, opening_balances={"GBP": 5_000_000})
        message = str(ctx.exception)
        self.assertIn("CHF", message)
        self.assertIn("fx_quotes", message)
        self.assertIn("day 2", message, "should say where it came from")

    def test_an_undeclared_opening_balance_is_picked_up_too(self):
        with self.assertRaises(ValueError) as ctx:
            CashOptimizer(Config(), CashFlowSet(horizon_days=5),
                          opening_balances={"GBP": 5e6, "CHF": 250_000})
        self.assertIn("opening balance", str(ctx.exception))

    def test_a_missing_carry_rate_is_named(self):
        quotes = dict(Config().fx_quotes)
        quotes["CHF"] = {t: FXTenorQuote(bid=0.88, ask=0.89)
                         for t in Config().tenors}
        cf = CashFlowSet(horizon_days=5)
        cf.add("CHF", 2, -100_000)
        with self.assertRaises(ValueError) as ctx:
            CashOptimizer(Config(fx_quotes=quotes), cf,
                          opening_balances={"GBP": 5_000_000})
        self.assertIn("credit_carry_pa", str(ctx.exception))


class TestMonotonicDrawdown(CostAssertions):
    """A holding winds down and is not built back up.

    There is no longer a rule that says so.  The dedicated monotonic
    drawdown constraint was removed after it was shown never to change an
    answer: the sweep deadline forces the balance to zero shortly after
    the last cashflow anyway, and the objective has no reason to build a
    position it must liquidate a day later.

    These tests assert the property rather than the mechanism, which is
    why they still hold.  If a future change makes any of them fail, the
    property has genuinely been lost and needs a rule again -- do not
    relax the assertion.
    """

    def test_an_overdraft_after_the_last_activity_can_be_repaid(self):
        # No same-day settlement, so a payment due today leaves the currency
        # overdrawn past its last cash flow. Under the signed-balance form
        # that overdraft could never be cleared and the model was infeasible.
        r = solve([("USD", 0, -100_000)], {"GBP": 5_000_000}, **NO_SAME_DAY)
        self.assertEqual(r.status, "Optimal")
        self.assertTrue(r.trades)

    def test_a_holding_still_may_not_grow_once_idle(self):
        r = solve([], {"USD": 1_000_000})
        held = [b.balance for b in r.balances if b.ccy == "USD"]
        for earlier, later in zip(held, held[1:]):
            self.assertLessEqual(later, earlier + 0.01,
                                 "an idle holding must only wind down")

    def test_the_anti_carry_intent_survives(self):
        r = solve([], {"GBP": 50_000_000, "USD": 1.0},
                  credit_carry_pa={"GBP": 3.65, "USD": 200.0,
                                   "EUR": 0.2, "JPY": 0.01})
        peak = max((b.balance for b in r.balances if b.ccy == "USD"),
                   default=0.0)
        self.assertLess(peak, 1_000.0,
                        "no position should be built for yield")


class TestManualPlanViolations(CostAssertions):
    """execute_trades deliberately prices whatever it is given, so a manual
    plan can use trades the optimizer was forbidden to consider and appear
    to beat it.  compare() called that impossible; it is routine, and the
    useful thing is to name the rule that was broken (F23)."""

    def _manager(self, **cfg_kw):
        cfg = Config(**cfg_kw)
        cf = CashFlowSet(horizon_days=5)
        cf.add("USD", 2, -100_000)
        return CashManager(cfg, cf, opening_balances={"GBP": 20_000_000})

    def test_a_legal_plan_reports_nothing(self):
        manager = self._manager()
        legal = manager.execute_trades(
            [ManualTrade("USD", 0, "T2", Direction.BUY, 100_000.0)])
        self.assertEqual(manager.constraint_violations(legal), [])

    def test_a_wash_trade_is_caught(self):
        manager = self._manager()
        looped = manager.execute_trades([
            ManualTrade("USD", 0, "T2", Direction.BUY, 900_000.0),
            ManualTrade("USD", 1, "T1", Direction.SELL, 800_000.0),
        ])
        self.assertTrue(any("no_loop" in v
                            for v in manager.constraint_violations(looped)))

    def test_buying_beyond_the_funding_need_is_caught(self):
        manager = self._manager()
        oversized = manager.execute_trades(
            [ManualTrade("USD", 0, "T2", Direction.BUY, 900_000.0)])
        self.assertTrue(any("holding_ceiling" in v
                            for v in manager.constraint_violations(oversized)))

    def test_leaving_a_position_open_is_caught(self):
        manager = self._manager()
        left_open = manager.execute_trades(
            [ManualTrade("USD", 0, "T2", Direction.BUY, 900_000.0)])
        self.assertTrue(any("terminal_sweep" in v
                            for v in manager.constraint_violations(left_open)))

    def test_a_ticket_below_the_minimum_is_caught(self):
        manager = self._manager(min_trade=50_000.0)
        tiny = manager.execute_trades(
            [ManualTrade("USD", 0, "T2", Direction.BUY, 100.0)])
        self.assertTrue(any("min_trade" in v
                            for v in manager.constraint_violations(tiny)))

    def test_the_impossible_claim_is_gone(self):
        import inspect
        import scripts.cash_optimizer_poc.cash_manager as cash_manager
        self.assertNotIn("This should not happen",
                         inspect.getsource(cash_manager))


class TestMinimumTradeSize(CostAssertions):
    """Nothing stopped the optimizer emitting a ticket nobody could deal.
    The activation binaries existed for exactly this and carried no floor
    and no cost (F15)."""

    def test_off_by_default(self):
        self.assertEqual(Config().min_trade, 0.0)
        r = solve([("USD", 2, -100_000)], {"GBP": 5_000_000})
        self.assertEqual(plan_of(r), [("USD", 0, "T2", "BUY", 100_000.0)])

    def test_a_ticket_above_the_minimum_is_unaffected(self):
        r = solve([("USD", 2, -100_000)], {"GBP": 5_000_000},
                  min_trade=50_000.0)
        self.assertEqual(r.status, "Optimal")
        self.assertEqual(plan_of(r), [("USD", 0, "T2", "BUY", 100_000.0)])

    def test_every_reported_trade_clears_the_minimum(self):
        r = solve([("USD", 2, -100_000)], {"GBP": 5_000_000},
                  min_trade=50_000.0)
        cfg = Config(min_trade=50_000.0)
        for t in r.trades:
            with self.subTest(trade=t):
                self.assertGreaterEqual(t.amount + 1e-6,
                                        cfg.min_trade_in(t.ccy))

    def test_a_need_below_the_minimum_has_no_legal_ticket(self):
        # A true answer about a real dealing constraint, not a defect: the
        # amount is under the minimum and the anti-speculative cap will not
        # permit buying a whole ticket's worth.
        r = solve([("USD", 2, -1_000)], {"GBP": 5_000_000}, min_trade=50_000.0)
        self.assertEqual(r.status, "Infeasible")
        self.assertFalse(r.insufficient_funds, "cash is not the problem")

    def test_the_minimum_is_worth_the_same_in_every_currency(self):
        cfg = Config(min_trade=10_000.0)
        for ccy in ("USD", "EUR"):
            with self.subTest(ccy=ccy):
                self.assertCostClose(
                    cfg.min_trade_in(ccy) * cfg.fx_spot_mid(ccy), 10_000.0)

    def test_a_negative_minimum_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(min_trade=-1.0)


class TestTradeReportFloor(CostAssertions):
    """Trades below a floor are treated as rounding and left out of the
    reported plan.  The floor used to be a flat 0.01 for every currency,
    which means very different things across them (F28)."""

    def _cfg(self):
        quotes = dict(Config().fx_quotes)
        quotes["JPY"] = {t: FXTenorQuote(bid=0.0051, ask=0.0053)
                         for t in Config().tenors}
        return Config(currencies=["GBP", "USD", "EUR", "JPY"],
                      fx_quotes=quotes)

    def test_the_floor_is_worth_the_same_in_every_currency(self):
        cfg = self._cfg()
        for ccy in ("USD", "EUR", "JPY"):
            with self.subTest(ccy=ccy):
                in_ccy = cfg.trade_report_floor_in(ccy)
                self.assertCostClose(in_ccy * cfg.fx_spot_mid(ccy),
                                     cfg.trade_report_floor)

    def test_yen_needs_a_much_larger_number_of_units(self):
        # The case a flat 0.01 got wrong: 0.01 of a yen is far below
        # quotable precision, so any tiny JPY trade was reported.
        cfg = self._cfg()
        self.assertGreater(cfg.trade_report_floor_in("JPY"), 1.0)

    def test_the_base_currency_uses_the_floor_directly(self):
        self.assertCostClose(Config().trade_report_floor_in("GBP"), 0.01)

    def test_a_negative_floor_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(trade_report_floor=-1.0)

    def test_ordinary_plans_are_unaffected(self):
        r = solve([("USD", 2, -100_000)], {"GBP": 5_000_000})
        self.assertEqual(plan_of(r), [("USD", 0, "T2", "BUY", 100_000.0)])


class TestManualAndOptimizerAgree(CostAssertions):
    """The manual evaluator converted foreign settlements at spot mid while
    the optimizer used the tenor's bid or ask, so the same trade produced
    different base balances depending which path priced it — by the
    half-spread, always in the manual plan's favour (F22)."""

    def _replay(self, flows, opening):
        cfg = Config()
        cf = CashFlowSet(horizon_days=5)
        for ccy, day, amount in flows:
            cf.add(ccy, day, amount)
        manager = CashManager(cfg, cf, opening_balances=dict(opening))
        optimal = manager.solve_optimal()
        replay = manager.execute_trades(
            [ManualTrade(t.ccy, t.day, t.tenor, t.direction, t.amount)
             for t in optimal.trades])
        return optimal, replay

    def test_base_balances_match_on_identical_trades(self):
        for flows, opening in [([("USD", 2, -100_000)], {"GBP": 5_000_000}),
                               ([], {"USD": 1_000_000})]:
            with self.subTest(flows=flows):
                optimal, replay = self._replay(flows, opening)
                opt_bal = {b.day: b.balance for b in optimal.balances
                           if b.ccy.startswith("GBP")}
                man_bal = {b.day: b.balance for b in replay.balances
                           if b.ccy.startswith("GBP")}
                for day, value in opt_bal.items():
                    self.assertCostClose(man_bal[day], value)

    def test_a_buy_is_priced_at_the_ask(self):
        cfg = Config()
        cf = CashFlowSet(horizon_days=5)
        cf.add("USD", 2, -100_000)
        manager = CashManager(cfg, cf, opening_balances={"GBP": 5_000_000})
        replay = manager.execute_trades(
            [ManualTrade("USD", 0, "T2", Direction.BUY, 100_000.0)])
        spent = 5_000_000 - [b.balance for b in replay.balances
                             if b.ccy.startswith("GBP") and b.day == 2][0]
        self.assertCostClose(spent, cfg.fx_ask("USD", "T2") * 100_000)

    def test_a_sell_is_priced_at_the_bid(self):
        cfg = Config()
        manager = CashManager(cfg, CashFlowSet(horizon_days=5),
                              opening_balances={"USD": 1_000_000})
        replay = manager.execute_trades(
            [ManualTrade("USD", 0, "T2", Direction.SELL, 1_000_000.0)])
        received = [b.balance for b in replay.balances
                    if b.ccy.startswith("GBP") and b.day == 2][0]
        self.assertCostClose(received, cfg.fx_bid("USD", "T2") * 1_000_000)


class TestDerivedConfigValues(CostAssertions):
    """big_m and the FX exposure start day used to be filled in by
    __post_init__ and never refreshed, so changing max_trade, the commission
    schedule or the tenors afterwards left them describing the configuration
    as it was at construction (F27).  They are computed on access now."""

    def test_big_m_tracks_max_trade(self):
        cfg = Config()
        self.assertCostClose(cfg.big_m, 500_000_001.0)
        cfg.max_trade = 1_000_000.0
        self.assertCostClose(cfg.big_m, 1_000_001.0)

    def test_big_m_tracks_the_commission_schedule(self):
        cfg = Config()
        cfg.commission_tiers = [CommissionTier(250_000, 20.0)]
        self.assertCostClose(cfg.big_m, 250_001.0)

    def test_the_exposure_start_day_tracks_the_tenors(self):
        cfg = Config()
        self.assertEqual(cfg.fx_exposure_from_day, 3)
        cfg.tenors = {"T0": 0, "T1": 1, "T2": 2, "T5": 5}
        self.assertEqual(cfg.fx_exposure_from_day, 6)

    def test_an_explicit_exposure_start_day_is_honoured(self):
        self.assertEqual(Config(fx_exposure_start_day=1).fx_exposure_from_day, 1)

    def test_nothing_derived_is_stored(self):
        import dataclasses
        names = {f.name for f in dataclasses.fields(Config)}
        self.assertNotIn("big_m", names, "a stored copy could go stale")
        self.assertIsInstance(Config.big_m, property)
        self.assertIsInstance(Config.fx_exposure_from_day, property)

    def test_replace_carries_the_derived_values_correctly(self):
        from dataclasses import replace
        cfg = replace(Config(), max_trade=2_000_000.0)
        self.assertCostClose(cfg.big_m, 2_000_001.0)


class TestSingleClassDefinitions(CostAssertions):
    """ManualTrade and CostBreakdown were declared in models and again in
    cash_manager, the second shadowing the first.  The two CostBreakdowns
    then drifted apart: the local copy gained the rate and unwind terms
    while the other kept summing four components, so anything importing
    from models understated every total by the spread (F24)."""

    def test_there_is_one_definition_of_each(self):
        import scripts.cash_optimizer_poc.cash_manager as cash_manager
        import scripts.cash_optimizer_poc.models as models
        for name in ("ManualTrade", "CostBreakdown"):
            with self.subTest(name=name):
                self.assertIs(getattr(cash_manager, name),
                              getattr(models, name),
                              "importing from either module must give the "
                              "same class")

    def test_the_total_includes_every_component(self):
        breakdown = CostBreakdown(
            credit_carry_cost=10.0, debit_carry_cost=5.0,
            fx_exposure_cost=2.0, commission_cost=100.0,
            spread_cost=20.0, terminal_unwind_cost=3.0,
        )
        self.assertCostClose(breakdown.total_cost, 140.0)

    def test_the_rate_term_reaches_the_total(self):
        # The exact regression the duplication caused.
        without = CostBreakdown(commission_cost=158.04)
        with_rate = CostBreakdown(commission_cost=158.04, spread_cost=20.0)
        self.assertCostClose(with_rate.total_cost - without.total_cost, 20.0)

    def test_the_manual_evaluator_matches_the_optimizer(self):
        cfg = Config()
        cf = CashFlowSet(horizon_days=5)
        cf.add("USD", 2, -100_000)
        manager = CashManager(cfg, cf, opening_balances={"GBP": 5_000_000})
        optimal = manager.solve_optimal()
        replay = manager.execute_trades(
            [ManualTrade(t.ccy, t.day, t.tenor, t.direction, t.amount)
             for t in optimal.trades])
        self.assertCostClose(replay.total_cost, optimal.total_cost)
        self.assertGreater(replay.cost_breakdown.spread_cost, 0.0,
                           "the rate term should be populated, not dropped")


class TestConfigIsolation(CostAssertions):
    """CashManager used to write its constraint override straight through to
    the caller's Config, so two managers built from one Config were not
    independent and the order they were constructed in decided which flags
    each ended up with (F26)."""

    def _flows(self):
        cf = CashFlowSet(horizon_days=5)
        cf.add("USD", 2, -100_000)
        return cf

    def test_the_callers_config_is_not_modified(self):
        cfg = Config()
        before = cfg.constraints.summary()
        CashManager(cfg, self._flows(), opening_balances={"GBP": 5_000_000},
                    constraints=ConstraintFlags(terminal_sweep=False))
        self.assertEqual(cfg.constraints.summary(), before)
        self.assertTrue(cfg.constraints.terminal_sweep)

    def test_two_managers_from_one_config_stay_independent(self):
        cfg = Config()
        baseline = CashManager(cfg, self._flows(),
                               opening_balances={"GBP": 5_000_000})
        variant = CashManager(cfg, self._flows(),
                              opening_balances={"GBP": 5_000_000},
                              constraints=ConstraintFlags(terminal_sweep=False))
        self.assertTrue(baseline.config.constraints.terminal_sweep)
        self.assertFalse(variant.config.constraints.terminal_sweep)
        self.assertIsNot(baseline.config, variant.config)

    def test_construction_order_does_not_matter(self):
        cfg = Config()
        CashManager(cfg, self._flows(), opening_balances={"GBP": 5_000_000},
                    constraints=ConstraintFlags(terminal_sweep=False))
        plain = CashManager(cfg, self._flows(),
                            opening_balances={"GBP": 5_000_000})
        self.assertTrue(plain.config.constraints.terminal_sweep,
                        "a manager with no override must get the original flags")

    def test_the_copy_keeps_everything_else(self):
        cfg = Config()
        variant = CashManager(cfg, self._flows(),
                              opening_balances={"GBP": 5_000_000},
                              constraints=ConstraintFlags(terminal_sweep=False))
        self.assertEqual(variant.config.big_m, cfg.big_m)
        self.assertEqual(variant.config.fx_exposure_from_day,
                         cfg.fx_exposure_from_day)
        self.assertEqual(variant.config.day_count("USD"), cfg.day_count("USD"))
        self.assertEqual(variant.solve_optimal().status, "Optimal")

    def test_without_an_override_the_config_is_shared_unchanged(self):
        cfg = Config()
        manager = CashManager(cfg, self._flows(),
                              opening_balances={"GBP": 5_000_000})
        self.assertIs(manager.config, cfg)


class TestDayCount(CostAssertions):
    """Interest accrues on a day count that varies by currency.  One divisor
    for everything mis-states any currency that does not match it by
    365/360 - 1 = 1.39%, charged in full on an overdraft (F25)."""

    def test_each_currency_uses_its_own_basis(self):
        cfg = Config()
        self.assertEqual(cfg.day_count("GBP"), 365)
        self.assertEqual(cfg.day_count("USD"), 360)
        self.assertEqual(cfg.day_count("EUR"), 360)

    def test_yen_is_act_365_not_360(self):
        # The correction that prompted the map: it is not "sterling is 365
        # and everything else is 360".
        cfg = Config()
        for ccy in ("JPY", "CAD", "AUD", "NZD", "HKD", "SGD"):
            with self.subTest(ccy=ccy):
                self.assertEqual(cfg.day_count(ccy), 365)

    def test_the_daily_rate_follows_the_basis(self):
        cfg = Config()
        self.assertCostClose(cfg.credit_carry_bps_per_day["USD"],
                             cfg.credit_carry_pa["USD"] * 100.0 / 360.0)
        self.assertCostClose(cfg.credit_carry_bps_per_day["GBP"],
                             cfg.credit_carry_pa["GBP"] * 100.0 / 365.0)

    def test_overdraft_interest_is_no_longer_understated(self):
        cfg = Config()
        now = cfg.debit_carry_bps_per_day["USD"]
        before = cfg.debit_carry_pa["USD"] * 100.0 / 365.0
        self.assertGreater(now, before)
        self.assertCostClose(now / before, 365.0 / 360.0)

    def test_the_map_can_be_overridden(self):
        cfg = Config(day_count_basis={**Config().day_count_basis, "USD": 365})
        self.assertEqual(cfg.day_count("USD"), 365)

    def test_an_unlisted_currency_falls_back_and_is_reported(self):
        cfg = Config()
        self.assertEqual(cfg.day_count("XYZ"), cfg.default_day_count)
        listed = Config(credit_carry_pa={"GBP": 3.65, "USD": 0.5, "EUR": 0.2,
                                         "JPY": 0.01, "BRL": 10.0},
                        debit_carry_pa={"GBP": 5.0, "USD": 5.0, "EUR": 4.5,
                                        "JPY": 3.0, "BRL": 14.0})
        self.assertIn("BRL", listed.currencies_on_default_day_count())
        self.assertNotIn("USD", listed.currencies_on_default_day_count())

    def test_a_non_positive_basis_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(day_count_basis={"GBP": 0})
        with self.assertRaises(ValueError):
            Config(default_day_count=0)


class TestHoldingCorridor(CostAssertions):
    """The balance must stay inside the corridor the cash flows define:
    a ceiling on what may be held, a floor on how deep an overdraft may go.

    One rule replaced a cumulative purchase cap and a three-part no-carry
    rule.  These tests cover what each of them used to guarantee, plus the
    two cases the old rules got wrong."""

    HIGH_USD = dict(credit_carry_pa={"GBP": 0.5, "USD": 18.0,
                                     "EUR": 1.0, "JPY": 0.01},
                    debit_carry_pa={"GBP": 6.0, "USD": 9.0,
                                    "EUR": 9.0, "JPY": 9.0})

    def _ceiling(self, flows, opening=None, horizon=8):
        cfg = Config(horizon_days=horizon)
        cf = CashFlowSet(horizon_days=horizon)
        for ccy, day, amount in flows:
            cf.add(ccy, day, amount)
        opt = CashOptimizer(cfg, cf, opening_balances=dict(opening or {}))
        return [round(v) for v in opt._holding_ceiling("USD")]

    # ── what the old rules got wrong ──

    def test_an_opening_overdraft_can_be_cured(self):
        # The rule this replaced read the cashflow file alone, so an opening
        # overdraft granted no permission to buy while the sweep deadline
        # still demanded the balance reach zero.  Ordinary overdrawn account,
        # returned Infeasible.
        r = solve([], {"GBP": 6_000_000, "USD": -200_000})
        self.assertEqual(r.status, "Optimal")
        self.assertTrue(r.trades, "the overdraft has to be bought back")

    def test_one_dollar_does_not_unlock_the_currency(self):
        # It used to: an outflow of any size flipped a currency from "may
        # not trade at all" to "may buy without limit".
        without = solve([], {"GBP": 6_000_000, "USD": -200_000})
        with_a_dollar = solve([("USD", 3, -1.0)],
                              {"GBP": 6_000_000, "USD": -200_000})
        self.assertEqual(without.status, "Optimal")
        self.assertEqual(with_a_dollar.status, "Optimal")
        self.assertAlmostEqual(
            with_a_dollar.total_cost, without.total_cost, delta=2.0,
            msg="a one-dollar outflow must not change what may be traded")

    def test_a_receipt_covering_a_later_payment_may_be_held(self):
        # The hole is measured on the do-nothing ladder, which already
        # contains the receipt -- so a receipt that covers a later payment
        # digs no hole.  A ceiling built from the hole alone forbade holding
        # money the account was always going to spend, and the only way out
        # was to sell it and buy it back for two spreads and two commissions.
        ceiling = self._ceiling([("USD", 1, 600_000), ("USD", 5, -400_000)])
        self.assertGreaterEqual(
            ceiling[3], 400_000,
            "the earmarked part of a receipt must be holdable")
        self.assertEqual(ceiling[7], 0,
                         "and nothing may remain once the payment is made")

    def test_an_unearmarked_receipt_may_not_be_held(self):
        ceiling = self._ceiling([("USD", 2, 900_000)])
        self.assertEqual(ceiling[4:], [0, 0, 0, 0],
                         "a receipt with nothing to fund must be swept")

    # ── what the old rules got right, still guaranteed ──

    def test_nothing_is_held_before_the_reach_window(self):
        ceiling = self._ceiling([("USD", 6, -250_000)])
        self.assertEqual(ceiling[:4], [0, 0, 0, 0],
                         "a day-6 payment is out of reach until day 4")
        self.assertEqual(ceiling[4], 250_000)

        r = solve([("USD", 6, -250_000)], {"GBP": 8_000_000},
                  horizon=8, **self.HIGH_USD)
        early = [b.balance for b in r.balances
                 if b.ccy == "USD" and b.day < 4]
        self.assertTrue(all(abs(v) < 1.0 for v in early),
                        "no position may exist before the payment is reachable")

    def test_no_position_is_built_for_yield(self):
        for rate in (18.0, 50.0, 200.0):
            with self.subTest(usd_rate=rate):
                r = solve([], {"GBP": 50_000_000, "USD": 1.0},
                          commission_tiers=[CommissionTier(500_000_000, 0.0)],
                          fx_exposure_bps_per_day=0.0,
                          credit_carry_pa={"GBP": 0.5, "USD": rate,
                                           "EUR": 1.0, "JPY": 0.01},
                          debit_carry_pa={"GBP": 6.0, "USD": 9.0,
                                          "EUR": 9.0, "JPY": 9.0})
                peak = max((b.balance for b in r.balances
                            if b.ccy == "USD"), default=0.0)
                self.assertLess(peak, 1_000.0)

    def test_a_tiny_need_draws_only_a_tiny_trade(self):
        r = solve([("USD", 6, -1_000)], {"GBP": 20_000_000},
                  horizon=8, **self.HIGH_USD)
        bought = sum(t.amount for t in r.trades
                     if t.ccy == "USD" and t.direction == Direction.BUY)
        self.assertLess(bought, 1_100.0,
                        "18% on a large position must not look worth funding")

    # ── the floor ──

    def test_a_short_position_cannot_be_manufactured(self):
        # Selling a currency you do not own is the same speculation in
        # reverse.  Doing nothing lands exactly on the floor, so this
        # forbids only overdrafts the model would have to manufacture.
        r = solve([("USD", 6, -250_000)], {"GBP": 8_000_000}, horizon=8,
                  credit_carry_pa={"GBP": 0.5, "USD": 0.01,
                                   "EUR": 1.0, "JPY": 0.01},
                  debit_carry_pa={"GBP": 6.0, "USD": 9.0,
                                  "EUR": 9.0, "JPY": 9.0})
        worst = min((b.balance for b in r.balances if b.ccy == "USD"),
                    default=0.0)
        self.assertGreaterEqual(
            worst, -250_000.01,
            "the overdraft may not run deeper than the cash flows dig")

    def test_an_overdraft_the_cashflows_dig_is_still_allowed(self):
        # The floor must not forbid a genuine overdraft -- running one is a
        # priced choice, not a violation.
        r = solve([("USD", 1, -100_000), ("USD", 3, 100_000)],
                  {"GBP": 5_000_000})
        self.assertEqual(r.status, "Optimal")
        self.assertEqual(r.trades, [],
                         "overdraft is cheaper here than two commissions")

    # ── the minimum-ticket corner ──

    def test_a_wash_trade_cannot_manufacture_a_legal_ticket(self):
        # A need below the minimum ticket has no legal plan.  Without
        # no_loop the model deals two legal tickets netting to an illegal
        # amount, and the corridor cannot see it: the position nets to zero
        # on every day.  This is the case that showed no_loop is not the
        # dead weight a cumulative purchase cap made it look.
        blocked = solve([("USD", 2, -1_000)], {"GBP": 5_000_000},
                        min_trade=50_000.0)
        self.assertEqual(blocked.status, "Infeasible")

        allowed = solve([("USD", 2, -1_000)], {"GBP": 5_000_000},
                        min_trade=50_000.0,
                        constraints=ConstraintFlags(no_loop=False))
        self.assertEqual(allowed.status, "Optimal",
                         "if this ever goes Infeasible the corridor has "
                         "started covering the wash route and no_loop can "
                         "be reconsidered")


class TestKnownOpenFindings(CostAssertions):
    """These assert the *current* broken behaviour on purpose.  When a fix
    lands the test fails, which is the signal to update it."""


if __name__ == "__main__":
    unittest.main(verbosity=2)
