"""Tests for the what-if page state — pricing a hand-entered route.

Run from the repository root::

    python -m unittest discover -s tests -v

These exercise ``CashManagementState``, which is deliberately free of any
NiceGUI dependency, so the whole feature is testable without a browser or a
running server.  The controller above it is presentation; every decision
worth asserting lives here.

The figures are taken from solving the shipped scenarios, not derived on
paper, so a failure means behaviour has changed.
"""
import logging
import unittest

logging.disable(logging.INFO)

from scripts.web_tools.pages.cash_management.state import CashManagementState

REL = 1e-4


def loaded(scenario_id, solve=True):
    """A page state with *scenario_id* loaded, optionally solved."""
    state = CashManagementState()
    state.load_scenario(scenario_id)
    if solve:
        state.run_optimizer()
    return state


def ledger_edits(state):
    """The ledger in the shape ``apply_edits_and_optimize`` expects.

    The controller assembles this from its own Python-side copy of the
    editable grid (``_extract_table_edits``); this is the same shape, taken
    straight from the pivoted ladder so the round trip changes nothing.
    """
    data = state.get_ledger_table_data()
    days = data['days']
    return {
        'days': list(days),
        'rows': [
            {
                'ccy': row['ccy'].replace(' (Base)', ''),
                'closing': [float(row.get(f'd{d}', 0.0)) for d in days],
            }
            for row in data['rows']
        ],
    }


def priced(scenario_id, trades, solve=True):
    """A page state with a hand-entered route entered and priced."""
    state = loaded(scenario_id, solve=solve)
    for trade in trades:
        state.add_manual_trade(*trade)
    state.evaluate_manual()
    return state


class WhatIfAssertions(unittest.TestCase):
    def assertCostClose(self, actual, expected, msg=None):
        tol = max(abs(expected) * REL, 0.005)
        self.assertAlmostEqual(actual, expected, delta=tol, msg=msg)


# ─────────────────────────────────────────────────────────────
# The table getters take a result; they used to reach for one
# ─────────────────────────────────────────────────────────────

class TestGettersStillDefaultToOptimal(WhatIfAssertions):
    """Every table helper gained a *result* parameter so the same renderer
    can draw either plan.  Defaulting had to stay exactly as it was, or the
    existing page changes behaviour on a refactor that was meant to be
    invisible."""

    def test_each_getter_defaults_to_the_optimal_result(self):
        state = loaded('S01')
        opt = state.optimal_result
        self.assertEqual(state.get_trades_table_data(),
                         state.get_trades_table_data(opt))
        self.assertEqual(state.get_after_trade_table_data(),
                         state.get_after_trade_table_data(opt))
        self.assertEqual(state.get_cost_breakdown(),
                         state.get_cost_breakdown(opt))
        self.assertEqual(state.get_cost_components(),
                         state.get_cost_components(opt))

    def test_the_ledger_is_pre_trade_and_takes_no_result(self):
        # It reads the do-nothing ladder off the manager, so it is the one
        # table that is the same whichever plan is on screen.
        state = loaded('S01')
        before = state.get_ledger_table_data()
        state.add_manual_trade('USD', 3, 'T1', 'BUY', 750_000)
        state.evaluate_manual()
        self.assertEqual(state.get_ledger_table_data(), before)

    def test_the_getters_are_empty_rather_than_raising_with_no_result(self):
        state = loaded('S01', solve=False)
        self.assertEqual(state.get_trades_table_data(), [])
        self.assertEqual(state.get_after_trade_table_data(),
                         {'days': [], 'rows': []})
        self.assertEqual(state.get_cost_breakdown(), {})
        self.assertEqual(state.get_cost_components(), [])


# ─────────────────────────────────────────────────────────────
# Both plans are priced by the same code over the same ladder
# ─────────────────────────────────────────────────────────────

class TestSameCostModelBothSides(WhatIfAssertions):
    """The comparison is only worth anything because a difference in the
    reported cost is a difference in the plan and never a difference in how
    it was measured.  That is structural — both sides reach
    ``CashManager._compute_cost`` — so it is worth asserting rather than
    assuming."""

    def test_the_pages_breakdown_of_a_hand_plan_matches_the_manager(self):
        state = priced('S01', [('USD', 3, 'T1', 'BUY', 750_000)])
        page = state.get_cost_breakdown(state.manual_result)
        self.assertCostClose(page['total'], state.manual_result.total_cost)

    def test_the_comparison_rows_add_up_to_both_totals(self):
        # A breakdown that does not reconcile to its own total is the
        # defect that once understated every plan by the spread.
        state = priced('S07', [('USD', 5, 'T0', 'SELL', 2_450_000)])
        verdict = state.get_manual_verdict()
        rows = state.get_manual_comparison_rows()
        self.assertCostClose(sum(r['manual'] for r in rows),
                             verdict['manual_impact'])
        self.assertCostClose(sum(r['optimal'] for r in rows),
                             verdict['optimal_impact'])

    def test_every_derivation_reconciles_against_the_model(self):
        state = priced('S07', [('USD', 5, 'T0', 'SELL', 2_450_000)])
        for row in state.get_manual_comparison_rows():
            self.assertTrue(row['reconciles'],
                            f"{row['label']} disagrees with the cost model")

    def test_evaluating_does_not_disturb_the_baseline(self):
        # Rebuilding the CashManager between the two evaluations is exactly
        # what would break the guarantee above.
        state = loaded('S01')
        manager, plan, cost = (state.cash_manager, state.optimal_result,
                               state.optimal_result.total_cost)
        state.add_manual_trade('USD', 3, 'T1', 'BUY', 750_000)
        state.evaluate_manual()
        self.assertIs(state.cash_manager, manager)
        self.assertIs(state.optimal_result, plan)
        self.assertEqual(state.optimal_result.total_cost, cost)


# ─────────────────────────────────────────────────────────────
# The verdict — the one decision the feature lives or dies on
# ─────────────────────────────────────────────────────────────

class TestVerdict(WhatIfAssertions):
    """A hand plan that comes out cheaper has almost always broken a rule,
    because the constraints exist precisely to forbid profitable
    speculation.  Reporting that as a saving would be misleading in exactly
    the case a user is most likely to go looking for."""

    def test_a_legal_dearer_route_is_reported_as_dearer(self):
        # S01: the optimal plan deals day 2 at T2, settling on the day the
        # payment falls due; this waits a day and deals T1 into the same
        # settlement day.  Legal, and dearer by the day of dollar carry it
        # gives up plus the worse near-tenor rate.
        state = priced('S01', [('USD', 3, 'T1', 'BUY', 750_000)])
        verdict = state.get_manual_verdict()
        self.assertEqual(verdict['kind'], 'dearer')
        self.assertEqual(verdict['violations'], [])
        self.assertCostClose(verdict['delta'], -5.0244)

    def test_settling_before_the_need_day_is_now_a_violation(self):
        """The route that used to be optimal is now forbidden.

        ``settle_on_need_only`` ships on, so a purchase settling two days
        before the payment is a position rather than funding.  It is also
        *cheaper* — it collects two days of dollar carry — which is exactly
        the case the void verdict exists for: a real saving that is not
        available.
        """
        state = priced('S01', [('USD', 0, 'T2', 'BUY', 750_000)])
        verdict = state.get_manual_verdict()
        self.assertEqual(verdict['kind'], 'void')
        self.assertGreater(verdict['delta'], 0.0)
        self.assertTrue(any('holding_ceiling' in v
                            for v in verdict['violations']))

    def test_a_cheaper_route_that_breaks_a_rule_is_void_not_a_saving(self):
        # S07: hold the dollar credit to the last day instead of sweeping
        # on day 2.  It does not merely beat the optimiser, it turns a
        # profit — and it is not available.
        state = priced('S07', [('USD', 5, 'T0', 'SELL', 2_450_000)])
        verdict = state.get_manual_verdict()
        self.assertEqual(verdict['kind'], 'void')
        self.assertCostClose(verdict['delta'], 475.4320)
        self.assertGreater(verdict['manual_impact'], 0.0)   # it profits
        self.assertTrue(any('holding_ceiling' in v
                            for v in verdict['violations']))

    def test_the_whole_of_that_difference_is_carry(self):
        # Same trade, same commission, same rate: the only change is three
        # more days of position.  If this ever stops being true the
        # scenario has drifted away from what it is testing.
        state = priced('S07', [('USD', 5, 'T0', 'SELL', 2_450_000)])
        by_key = {r['key']: r for r in state.get_manual_comparison_rows()}
        carry = next(r for k, r in by_key.items() if 'credit' in k)
        self.assertCostClose(carry['delta'],
                             state.get_manual_verdict()['delta'])

    def test_a_dearer_route_that_also_breaks_a_rule_is_flagged_illegal(self):
        state = priced('S05', [])          # doing nothing leaves EUR unswept
        verdict = state.get_manual_verdict()
        self.assertEqual(verdict['kind'], 'illegal')
        self.assertLess(verdict['delta'], 0.0)
        self.assertTrue(any('terminal_sweep' in v
                            for v in verdict['violations']))

    def test_copying_the_optimal_plan_prices_level_with_it(self):
        # Whatever the shipped corridor policy is, copying its own answer
        # has to price identically to it.
        state = loaded('S01')
        self.assertEqual(state.copy_optimal_to_manual(),
                         len(state.optimal_result.trades))
        state.evaluate_manual()
        verdict = state.get_manual_verdict()
        self.assertEqual(verdict['kind'], 'level')
        self.assertCostClose(verdict['delta'], 0.0)

    def test_cheaper_with_nothing_broken_points_at_the_baseline(self):
        """The third verdict, and the one the design originally missed.

        Against a true optimum this cannot happen, so the finger points at
        the solver rather than the route — CBC returns provably sub-optimal
        plans on this model and labels them ``Optimal``.  Simulated here by
        moving the baseline, which is what that defect looks like from the
        page's side.
        """
        state = priced('S01', [('USD', 2, 'T2', 'BUY', 750_000)])
        self.assertEqual(state.get_manual_verdict()['kind'], 'level')
        state.optimal_result.total_cost += 500.0
        verdict = state.get_manual_verdict()
        self.assertEqual(verdict['kind'], 'suspect')
        self.assertEqual(verdict['violations'], [])
        self.assertCostClose(verdict['delta'], 500.0)

    def test_an_unpriced_route_has_no_verdict_at_all(self):
        state = loaded('S01')
        state.add_manual_trade('USD', 3, 'T1', 'BUY', 750_000)
        self.assertEqual(state.get_manual_verdict(), {})


# ─────────────────────────────────────────────────────────────
# A baseline is useful, never a precondition
# ─────────────────────────────────────────────────────────────

class TestPricingWithoutABaseline(WhatIfAssertions):
    """Pricing a hand plan with nothing to compare against is still worth
    doing, and on an infeasible scenario it is the only way to see what the
    binding constraint is costing."""

    def test_a_route_prices_before_the_optimizer_has_ever_run(self):
        state = priced('S01', [('USD', 0, 'T2', 'BUY', 750_000)], solve=False)
        verdict = state.get_manual_verdict()
        self.assertEqual(verdict['kind'], 'no_baseline')
        self.assertIsNone(verdict['delta'])
        self.assertCostClose(verdict['manual_impact'], -1126.0682)

    def test_an_infeasible_scenario_still_prices_a_hand_plan(self):
        # S16 is infeasible because the need is below the minimum ticket.
        state = priced('S16', [('USD', 0, 'T2', 'BUY', 100_000)])
        self.assertEqual(state.optimal_result.status, 'Infeasible')
        verdict = state.get_manual_verdict()
        self.assertEqual(verdict['kind'], 'no_baseline')
        self.assertTrue(any('min_trade' in v for v in verdict['violations']),
                        'the binding constraint should be named')

    def test_an_infeasible_result_is_not_treated_as_a_baseline(self):
        # It carries total_cost=None and no balances, so every delta drawn
        # from it would be a comparison against nothing.
        state = loaded('S16')
        self.assertIsNone(state.optimal_result.total_cost)
        self.assertFalse(state.has_baseline)

    def test_the_ladder_comparison_is_empty_without_a_baseline(self):
        state = priced('S01', [('USD', 0, 'T2', 'BUY', 750_000)], solve=False)
        self.assertEqual(state.get_manual_ladder_comparison(),
                         {'days': [], 'rows': []})

    def test_the_comparison_rows_degrade_to_one_column(self):
        state = priced('S01', [('USD', 0, 'T2', 'BUY', 750_000)], solve=False)
        rows = state.get_manual_comparison_rows()
        self.assertTrue(rows)
        self.assertTrue(all(r['optimal'] is None and r['delta'] is None
                            for r in rows))


# ─────────────────────────────────────────────────────────────
# Staleness — the easiest way for this feature to lie
# ─────────────────────────────────────────────────────────────

class TestStaleness(WhatIfAssertions):
    """A plan priced against a ladder that has since moved is worse than no
    plan.  The price goes; the route stays, because losing a typed route on
    every ledger tweak is the worse failure and re-pricing is one click."""

    def test_clearing_drops_the_price_and_keeps_the_route(self):
        state = priced('S01', [('USD', 3, 'T1', 'BUY', 750_000)])
        state.clear_manual()
        self.assertIsNone(state.manual_result)
        self.assertEqual(state.manual_violations, [])
        self.assertEqual(len(state.manual_trades), 1)

    def test_loading_a_scenario_drops_the_route_as_well(self):
        # Different currencies, tenors and horizon: the entries themselves
        # may no longer be dealable.
        state = priced('S01', [('USD', 3, 'T1', 'BUY', 750_000)])
        state.load_scenario('S02')
        self.assertEqual(state.manual_trades, [])
        self.assertIsNone(state.manual_result)

    def test_re_solving_after_edits_drops_the_price(self):
        state = priced('S01', [('USD', 3, 'T1', 'BUY', 750_000)])
        state.apply_edits_and_optimize(ledger_edits(state))
        self.assertIsNone(state.manual_result)
        self.assertEqual(len(state.manual_trades), 1)

    def test_editing_the_route_drops_the_price(self):
        state = priced('S01', [('USD', 3, 'T1', 'BUY', 750_000)])
        state.add_manual_trade('USD', 0, 'T2', 'BUY', 100_000)
        self.assertIsNone(state.manual_result)
        state.evaluate_manual()
        state.remove_manual_trade(0)
        self.assertIsNone(state.manual_result)

    def test_an_empty_violation_list_never_means_unchecked(self):
        # The two are separate fields precisely so that one value does not
        # have to carry two meanings.
        state = priced('S01', [('USD', 3, 'T1', 'BUY', 750_000)])
        self.assertEqual(state.manual_violations, [])
        self.assertEqual(state.manual_check_error, '')


# ─────────────────────────────────────────────────────────────
# Entering a route
# ─────────────────────────────────────────────────────────────

class TestTradeEntry(WhatIfAssertions):
    """Validation is the manager's, not a second copy of it."""

    def test_a_rejected_trade_does_not_linger_in_the_list(self):
        state = loaded('S01')
        bad = [
            ('USD', 5, 'T2', 'BUY', 100_000),    # settles past the horizon
            ('JPY', 0, 'T2', 'BUY', 100_000),    # not in this scenario
            ('USD', 0, 'T9', 'BUY', 100_000),    # no such tenor
            ('GBP', 0, 'T2', 'BUY', 100_000),    # the base currency
            ('USD', 0, 'T2', 'BUY', -5),         # not a positive amount
        ]
        for entry in bad:
            with self.assertRaises(Exception):
                state.add_manual_trade(*entry)
        self.assertEqual(state.manual_trades, [])

    def test_the_rejection_names_the_row_and_says_why(self):
        state = loaded('S01')
        state.add_manual_trade('USD', 0, 'T2', 'BUY', 100_000)
        with self.assertRaises(ValueError) as caught:
            state.add_manual_trade('USD', 5, 'T2', 'BUY', 100_000)
        message = str(caught.exception)
        self.assertIn('Trade 1', message)
        self.assertIn('settlement day 7', message)
        self.assertEqual(len(state.manual_trades), 1)

    def test_settlement_day_is_shown_before_the_route_is_priced(self):
        state = loaded('S01')
        state.add_manual_trade('USD', 3, 'T1', 'BUY', 750_000)
        row, = state.get_manual_trades_table_data()
        self.assertEqual(row['settle_day'], 4)
        self.assertEqual(row['index'], 0)

    def test_an_empty_route_prices_the_do_nothing_plan(self):
        state = priced('S05', [])
        self.assertEqual(state.manual_result.label, 'Do Nothing')
        self.assertEqual(state.manual_result.trades, [])

    def test_copying_an_empty_plan_copies_nothing(self):
        state = loaded('S01', solve=False)
        self.assertEqual(state.copy_optimal_to_manual(), 0)


if __name__ == '__main__':
    unittest.main()
