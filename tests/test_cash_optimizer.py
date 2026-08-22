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
    CommissionTier,
    Config,
    ConstraintFlags,
    Direction,
    FXTenorQuote,
    PhasingPlan,
)
from scripts.cash_optimizer_poc.optimizer import CashOptimizer
from scripts.cash_optimizer_poc.cash_manager import CashManager, ManualTrade

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

    def test_known_cbc_failure_is_detected_and_corrected(self):
        r = solve([("USD", 2, -40_000_000)], {"GBP": 250_000_000},
                  solver="PULP_CBC_CMD")
        self.assertEqual(r.status, "Optimal")
        self.assertTrue(
            r.optimality_unproven,
            "CBC's sub-optimal plan should have been flagged")
        self.assertIsNotNone(r.improved_from)
        self.assertLess(
            r.total_cost, r.improved_from,
            "the adopted plan should be cheaper than the solver's first answer")

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

    def test_reported_cost_survives_the_verification_pass(self):
        # Building a constraint from the objective mutates it in PuLP; if that
        # leaks, the reported cost collapses to zero.
        r = solve([("USD", 2, -40_000_000)], {"GBP": 250_000_000},
                  solver="PULP_CBC_CMD")
        self.assertGreater(r.total_cost, 1.0)

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
        # Guards the guard: confirms the test above is actually exercising
        # the new term rather than passing for unrelated reasons.
        r = solve([], {"USD": 1_000_000}, value_trade_rates=False)
        self.assertEqual(r.trades[0].tenor, "T1")

    def test_liquidates_even_with_the_terminal_sweep_off(self):
        flags = ConstraintFlags(terminal_sweep=False, no_carry_trade=False)
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

class TestAntiSpeculativeCap(CostAssertions):
    """The cap used to net inflows against outflows across the whole horizon,
    so a real two-day shortfall could not be funded at all."""

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

    def test_speculation_stays_blocked_at_any_carry_differential(self):
        flags = ConstraintFlags(no_carry_trade=False, terminal_sweep=False,
                                t0_debit_only=False, no_loop=False)
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
            for flag in ["anti_speculative", "no_loop", "reserve"]:
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

class TestKnownOpenFindings(CostAssertions):
    """These assert the *current* broken behaviour on purpose.  When a fix
    lands the test fails, which is the signal to update it."""

    def test_f5_day_zero_payment_is_still_infeasible(self):
        r = solve([("USD", 0, -100_000)], {"GBP": 5_000_000})
        self.assertEqual(r.status, "Infeasible",
                         "F5 fixed? update this test")

    def test_f7_minimum_reserve_still_cannot_be_enabled(self):
        r = solve([("USD", 2, -100_000)], {"GBP": 5_000_000}, min_reserve=1.0)
        self.assertEqual(r.status, "Infeasible", "F7 fixed? update this test")

    def test_f12_phasing_on_an_unheld_currency_still_raises(self):
        cfg = Config()
        cf = CashFlowSet(horizon_days=5)
        cf.add("USD", 2, -100_000)
        plan = PhasingPlan(currency="EUR", tranche_schedule={3: 50_000})
        opt = CashOptimizer(cfg, cf, opening_balances={"GBP": 5_000_000},
                            phasing=[plan])
        with self.assertRaises(TypeError):
            opt.solve()

    def test_f17_nan_still_silently_drops_a_currency(self):
        cfg = Config()
        opt = CashOptimizer(cfg, CashFlowSet(horizon_days=5),
                            opening_balances={"GBP": 5e6, "USD": float("nan")})
        self.assertEqual(opt.active_foreign_ccys, [],
                         "F17 fixed? update this test")


if __name__ == "__main__":
    unittest.main(verbosity=2)
