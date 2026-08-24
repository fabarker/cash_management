"""Cash Management page state.

Holds all mutable, per-page, per-session data.  No NiceGUI dependencies.
Validation predicates and data-preparation helpers live here so the
Controller stays thin.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple, TYPE_CHECKING

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .types import CashManagementRefs


@dataclass
class CashManagementState:
    """Ephemeral session state for the Cash Management page.

    Attributes
    ----------
    is_loading : bool
        True while an async load/solve operation is in-flight.
        Used to prevent duplicate submissions.
    cash_manager : object or None
        The ``CashManager`` built from the loaded scenario.  Stored so the
        controller can call ``solve_optimal()`` without rebuilding it.
    optimal_result : object or None
        The ``Result`` returned by ``cash_manager.solve_optimal()``.
    manual_trades : list
        The what-if route the user has entered by hand, as ``ManualTrade``
        objects.  Held unpriced until ``evaluate_manual()`` runs.
    manual_result : object or None
        The ``ManualResult`` from pricing ``manual_trades``.  Cleared
        whenever the ladder underneath it changes, because a plan priced
        against a ladder that has since moved is worse than no plan.
    manual_violations : list of str
        Which of the optimizer's rules the priced route breaks.  Empty is
        not the same as unchecked -- see ``manual_check_error``.
    manual_check_error : str
        Set when the violation check itself failed to run.  Kept apart from
        ``manual_violations`` so an empty violation list never has to mean
        two different things.
    group_number : str
        The most recently loaded group number (for display / logging).
    error_message : str
        Latest error message to display (empty when no error).
    """

    is_loading: bool = False
    cash_manager: Any = None          # scripts.cash_optimizer_poc.cash_manager.CashManager
    optimal_result: Any = None        # scripts.cash_optimizer_poc.result.Result
    group_number: str = ''
    error_message: str = ''
    _fx_service: Any = None           # RefinitivFXService — kept alive across loads
    is_demo: bool = False             # True when the scenario was generated locally
    scenario: Any = None              # the loaded scenario dict, if any
    scenario_index: int = -1          # 0-based position in the library; -1 = none yet

    # ── What-if (hand-entered) plan ───────────────────────────
    manual_trades: List[Any] = field(default_factory=list)   # models.ManualTrade
    manual_result: Any = None         # cash_manager.ManualResult
    manual_violations: List[str] = field(default_factory=list)
    manual_check_error: str = ''

    # ── Validation predicates ─────────────────────────────────

    def can_load(self, refs: CashManagementRefs) -> bool:
        """Return True whenever no operation is in flight.

        The button steps through the scenario library, so it does not need
        anything typed.  The input is an optional jump-to.
        """
        return not self.is_loading

    def can_generate_trades(self) -> bool:
        """Return True when a CashManager is loaded and not busy."""
        return self.cash_manager is not None and not self.is_loading

    # ── Data helpers (called by the controller) ───────────────

    def _get_or_create_fx_service(self) -> Any:
        """Return a persistent ``RefinitivFXService``, or ``None``.

        The LSEG/Refinitiv ``rd`` library uses a global session that cannot
        be cleanly re-opened after ``rd.close_session()``.
        ``CashManager._fetch_fx_rates`` disconnects the service in its
        ``finally`` block when it owns one, so a single instance is created
        here and passed in; ``_fetch_fx_rates`` then sees
        ``owns_service = False`` and leaves the session open.

        The service lives in ``pmg_core``, which is not vendored in this
        repository.  When it cannot be imported this returns ``None`` and
        the caller falls back to a local scenario, rather than failing with
        a ``NameError`` on a variable that was never assigned.
        """
        if self._fx_service is not None:
            return self._fx_service

        try:
            from pmg_core.market_data.refinitiv import RefinitivFXService
        except ImportError:
            log.info(
                'RefinitivFXService unavailable (pmg_core not installed); '
                'live FX will not be fetched.'
            )
            return None

        # Don't connect eagerly — _ensure_legs defers the connection
        # until after checking in-memory and daily disk caches.
        svc = RefinitivFXService()
        self._fx_service = svc
        return svc

    def load_scenario(self, selector: str = '') -> None:
        """Load the next scenario from the library, or the one *selector* names.

        Blocking (the solve is not, but building the CashManager validates
        the whole config) — the controller runs it via ``run.io_bound``.

        With *selector* empty the library advances one place and wraps at the
        end, so repeatedly pressing the button walks all twenty in order.
        A selector matching a scenario id (``S07``) or a 1-based position
        (``7``) jumps straight there instead.

        Parameters
        ----------
        selector : str
            Scenario id, 1-based index, or empty to advance.
        """
        from scripts.cash_optimizer_poc.scenarios import (
            build_cash_manager, load_library, summarise,
        )

        library = load_library()
        selector = (selector or '').strip()

        if not selector:
            index = (self.scenario_index + 1) % len(library)
        else:
            index = self._resolve_selector(selector, library)

        sc = library[index]
        log.info('Loading scenario %d/%d: %s', index + 1, len(library), summarise(sc))

        self.cash_manager = build_cash_manager(sc)
        self.scenario = sc
        self.scenario_index = index
        self.group_number = f"{sc['id']} ({index + 1}/{len(library)})"
        self.is_demo = True
        self.optimal_result = None  # Clear stale results on reload
        # A different scenario means different currencies, tenors and
        # horizon, so the entered route goes too -- not just its price.
        self.clear_manual(keep_trades=False)

    @staticmethod
    def _resolve_selector(selector: str, library: List[Dict[str, Any]]) -> int:
        """Map a typed selector to a library position, or raise."""
        wanted = selector.upper()
        for i, sc in enumerate(library):
            if sc['id'].upper() == wanted:
                return i
        if selector.isdigit():
            n = int(selector)
            if 1 <= n <= len(library):
                return n - 1
            raise ValueError(
                f'Scenario {n} is out of range — the library holds '
                f'{len(library)}.'
            )
        raise ValueError(
            f'No scenario matches {selector!r}. Use an id such as '
            f'{library[0]["id"]}, a position from 1 to {len(library)}, or '
            f'leave it blank to load the next one.'
        )

    @property
    def scenario_count(self) -> int:
        from scripts.cash_optimizer_poc.scenarios import load_library
        return len(load_library())

    def apply_edits_and_optimize(self, edits: Dict[str, Any]) -> None:
        """Apply user table edits to the CashManager, then run the optimizer.

        Blocking call — must be run via ``run.io_bound``.

        Steps:
        1. Rebuild ``opening_balances`` and ``CashFlowSet`` from the
           edited closing balances.
        2. For any newly added foreign currencies, fetch FX quotes
           from the persistent Refinitiv service and add default
           credit/debit carry rates.
        3. Run ``solve_optimal()``.

        Parameters
        ----------
        edits : dict
            ``{'days': [0, 1, ...], 'rows': [{'ccy': 'EUR', 'closing': [100, 200, ...]}, ...]}``
            Extracted from the QTable by the controller on the main thread.
        """
        if self.cash_manager is None:
            raise RuntimeError('No CashManager loaded.')

        mgr = self.cash_manager
        days = edits['days']
        rows = edits['rows']

        # ── Step 1: rebuild opening balances + cashflows ──
        from scripts.cash_optimizer_poc.models import CashFlowSet

        new_opening: Dict[str, float] = {}
        new_cashflows = CashFlowSet()
        new_cashflows.horizon_days = mgr.config.horizon_days

        all_ccys: List[str] = []

        for row in rows:
            ccy = row['ccy']
            all_ccys.append(ccy)
            closing = row['closing']  # list of floats, one per day

            if closing:
                new_opening[ccy] = closing[0]
                # Day 0: no cashflow (opening = closing)
                new_cashflows.add(ccy, days[0], 0.0)
                for i in range(1, len(closing)):
                    daily_change = closing[i] - closing[i - 1]
                    new_cashflows.add(ccy, days[i], daily_change)

        mgr._opening = new_opening
        mgr._cashflows = new_cashflows

        # ── Step 2: handle newly added currencies ──
        existing_ccys = set(mgr.config.currencies)
        new_foreign: List[str] = []

        for ccy in all_ccys:
            if ccy not in existing_ccys:
                mgr.config.currencies.append(ccy)
                if ccy != mgr.config.base_ccy:
                    mgr._foreign_ccys.append(ccy)
                    new_foreign.append(ccy)

        # Fetch FX quotes for any new foreign currencies
        if new_foreign:
            import logging
            log = logging.getLogger(__name__)
            log.info('Fetching FX quotes for new currencies: %s', new_foreign)

            from scripts.cash_optimizer_poc.cash_manager import CashManager

            fx_service = self._get_or_create_fx_service()
            if hasattr(CashManager, '_fetch_fx_rates') and fx_service is not None:
                new_fx = CashManager._fetch_fx_rates(
                    base_ccy=mgr.config.base_ccy,
                    foreign_ccys=new_foreign,
                    fx_service=fx_service,
                )
                # Merge into existing fx_quotes
                for ccy, tenor_quotes in new_fx.items():
                    mgr.config.fx_quotes[ccy] = tenor_quotes
            else:
                log.warning(
                    'No FX service available; %s cannot be priced and the '
                    'check below will reject them.', new_foreign,
                )

            # Add default credit/debit rates for new currencies (0%)
            for ccy in new_foreign:
                if ccy not in mgr.config.credit_carry_pa:
                    mgr.config.credit_carry_pa[ccy] = 0.0
                if ccy not in mgr.config.debit_carry_pa:
                    mgr.config.debit_carry_pa[ccy] = 0.0

            # Warn about currencies we couldn't get FX for
            missing_fx = [c for c in new_foreign if c not in mgr.config.fx_quotes]
            if missing_fx:
                raise ValueError(
                    f'Cannot optimize: no FX quotes available for {missing_fx}. '
                    f'These currencies are not in the supported FX universe.'
                )

        # ── Step 3: run the optimizer ──
        # The ladder has just been rebuilt from the user's edits, so any
        # priced what-if plan describes a book that no longer exists.  The
        # trades themselves stay -- they are still legal entries against
        # this scenario, and re-pricing them is one click.
        self.clear_manual()
        self.optimal_result = None
        result = mgr.solve_optimal()
        self.optimal_result = result

    def run_optimizer(self) -> None:
        """Run the optimizer on the currently loaded CashManager.

        Blocking call — must be run via ``asyncio.to_thread``.

        Stores the result on ``self.optimal_result``.

        Raises
        ------
        RuntimeError
            If no CashManager has been loaded yet.
        Exception
            Propagated from the LP/MIP solver on failure.
        """
        if self.cash_manager is None:
            raise RuntimeError('No scenario loaded — call load_scenario() first.')

        result = self.cash_manager.solve_optimal()
        self.optimal_result = result

    # ── What-if: entering, pricing and comparing a hand plan ──

    def clear_manual(self, keep_trades: bool = True) -> None:
        """Discard the priced what-if plan.

        Call this whenever the ladder, the constraints or the scenario move
        underneath it.  *keep_trades* is the default because losing a typed
        route on every ledger tweak is a worse failure than re-pricing it:
        the entered trades stay legal, it is only their **price** that went
        stale.  A scenario load passes ``keep_trades=False``, since the
        currencies and horizon themselves change there.
        """
        self.manual_result = None
        self.manual_violations = []
        self.manual_check_error = ''
        if not keep_trades:
            self.manual_trades = []

    def add_manual_trade(self, ccy: str, day: int, tenor: str,
                         direction: str, amount: float) -> None:
        """Append one hand-entered trade, rejecting it if it is not dealable.

        Validation is the manager's, not a second copy of it: the candidate
        is appended and the whole list re-checked, so the index in any error
        message is the row the user is looking at.  A rejected trade is
        removed again before the error propagates.
        """
        from scripts.cash_optimizer_poc.models import Direction, ManualTrade

        if self.cash_manager is None:
            raise RuntimeError('Load a scenario before entering trades.')

        trade = ManualTrade(
            ccy=ccy,
            day=int(day),
            tenor=tenor,
            direction=Direction(direction),
            amount=float(amount),
        )
        self.manual_trades.append(trade)
        try:
            self.cash_manager._validate_manual_trades(self.manual_trades)
        except Exception:
            self.manual_trades.pop()
            raise
        self.clear_manual()

    def remove_manual_trade(self, index: int) -> None:
        """Drop the trade at *index* and invalidate the priced plan."""
        if 0 <= index < len(self.manual_trades):
            self.manual_trades.pop(index)
            self.clear_manual()

    def copy_optimal_to_manual(self) -> int:
        """Seed the what-if list from the optimizer's plan.

        Returns the number of trades copied.  The point is to change one
        thing -- a tenor, a day, a size -- and see what it costs, rather
        than retyping a plan the solver already found.
        """
        from scripts.cash_optimizer_poc.models import ManualTrade

        result = self.optimal_result
        if result is None or not getattr(result, 'trades', None):
            return 0
        self.manual_trades = [
            ManualTrade(ccy=t.ccy, day=t.day, tenor=t.tenor,
                        direction=t.direction, amount=t.amount)
            for t in result.trades
        ]
        self.clear_manual()
        return len(self.manual_trades)

    def evaluate_manual(self) -> None:
        """Price the entered route and check it against the active rules.

        No solver runs and the ``CashManager`` is not rebuilt, so the
        optimizer's plan stays valid beside this one.  That is the whole
        guarantee the comparison rests on: both plans reach
        ``CashManager._compute_cost`` over the same ladder, so a difference
        in the reported cost is a difference in the plan and never a
        difference in how it was measured.

        Blocking -- the controller runs it via ``run.io_bound``, because
        the violation check builds a ``CashOptimizer`` to read the holding
        corridor off it (it does not solve).
        """
        if self.cash_manager is None:
            raise RuntimeError('No scenario loaded — call load_scenario() first.')

        mgr = self.cash_manager
        self.manual_check_error = ''
        # An empty list is a legal plan -- it prices doing nothing -- and
        # the manager already names that case better than a fixed label
        # would, so let it.
        self.manual_result = mgr.execute_trades(
            list(self.manual_trades),
            label='What-if' if self.manual_trades else None,
        )
        try:
            self.manual_violations = mgr.constraint_violations(self.manual_result)
        except Exception as exc:            # pragma: no cover - defensive
            # An empty violation list must never stand in for "not checked".
            log.exception('constraint_violations failed on the what-if plan')
            self.manual_violations = []
            self.manual_check_error = str(exc)

    @property
    def has_baseline(self) -> bool:
        """True when there is an optimizer plan worth comparing against.

        An ``Infeasible`` result is not one: it carries ``total_cost=None``
        and no balances, so every delta computed from it would be a
        comparison against nothing.  Pricing a hand plan is still useful
        there -- it shows what the binding constraint costs -- which is why
        this gates the *delta*, not the feature.
        """
        result = self.optimal_result
        return result is not None and getattr(result, 'total_cost', None) is not None

    def get_manual_trades_table_data(self) -> List[Dict[str, Any]]:
        """Return the entered (not yet priced) trades as row dicts.

        Settlement day is derived here rather than read off the trade,
        because a ``ManualTrade`` does not carry one until the manager
        resolves it.
        """
        mgr = self.cash_manager
        if mgr is None:
            return []
        tenors = mgr.config.tenors
        return [
            {
                'index': i,
                'ccy': t.ccy,
                'day': t.day,
                'tenor': t.tenor,
                'direction': t.direction.value,
                'amount': t.amount,
                'settle_day': t.day + tenors.get(t.tenor, 0),
            }
            for i, t in enumerate(self.manual_trades)
        ]

    def get_manual_verdict(self) -> Dict[str, Any]:
        """Summarise the what-if plan against the baseline, in one dict.

        ``kind`` is what the banner is written from:

        ``no_baseline``    priced, nothing to compare against.
        ``dearer``         legal and costs more — an ordinary answer.
        ``void``           cheaper, but only by breaking a rule.  The
                           saving is not available and is labelled so.
        ``illegal``        dearer *and* illegal; the delta is beside the point.
        ``suspect``        cheaper with nothing broken.  That should not
                           happen against a proven optimum, so the finger
                           points at the baseline, not the route — see
                           the CBC note in CLAUDE.md.
        ``level``          the same money either way.

        Signs are the page's, not the model's: a cost is negative, so a
        positive ``delta`` means the hand plan keeps more money.
        """
        if self.manual_result is None:
            return {}

        manual_impact = -self.manual_result.total_cost
        opt = self.optimal_result
        baseline = self.has_baseline
        optimal_impact = -opt.total_cost if baseline else None
        delta = (manual_impact - optimal_impact) if baseline else None

        violations = list(self.manual_violations)
        unproven = bool(getattr(opt, 'optimality_unproven', False)) if opt else False

        if not baseline:
            kind = 'no_baseline'
        elif delta > 0.0005:
            kind = 'void' if violations else 'suspect'
        elif delta < -0.0005:
            kind = 'illegal' if violations else 'dearer'
        else:
            kind = 'level'

        return {
            'kind': kind,
            'manual_impact': manual_impact,
            'optimal_impact': optimal_impact,
            'delta': delta,
            'violations': violations,
            'check_error': self.manual_check_error,
            'optimality_unproven': unproven,
            'solver_used': getattr(opt, 'solver_used', '') if opt else '',
            'solver_status': getattr(opt, 'status', '') if opt else '',
            'n_trades': len(self.manual_result.trades),
        }

    def get_manual_comparison_rows(self) -> List[Dict[str, Any]]:
        """Return the cost breakdown of both plans, line by line, with deltas.

        Both sides come from ``cost_workings``, which emits the same keys in
        the same order for any plan, so the rows line up and a per-line
        difference means something: you can see *where* the money went, not
        only that it went.  With no baseline the optimal columns are None
        and the table degrades to the hand plan's own breakdown.
        """
        manual = self.get_cost_components(self.manual_result)
        optimal = self.get_cost_components(self.optimal_result) if self.has_baseline else []
        opt_by_key = {c['key']: c for c in optimal}

        rows: List[Dict[str, Any]] = []
        for c in manual:
            o = opt_by_key.pop(c['key'], None)
            rows.append({
                'key': c['key'],
                'label': c['label'],
                'optimal': o['value'] if o else None,
                'manual': c['value'],
                'delta': (c['value'] - o['value']) if o else None,
                'optimal_workings': o['workings'] if o else '',
                'manual_workings': c['workings'],
                'reconciles': c['reconciles'] and (o['reconciles'] if o else True),
            })
        # A key the optimal plan has and the manual one does not would
        # otherwise vanish from a table that is meant to reconcile.
        for key, o in opt_by_key.items():
            rows.append({
                'key': key,
                'label': o['label'],
                'optimal': o['value'],
                'manual': 0.0,
                'delta': -o['value'],
                'optimal_workings': o['workings'],
                'manual_workings': '',
                'reconciles': o['reconciles'],
            })
        return rows

    def get_manual_ladder_comparison(self) -> Dict[str, Any]:
        """Return after-trade balances for both plans, as ccy x day rows.

        Shape: ``{'days': [...], 'rows': [{'ccy', 'optimal', 'manual',
        'delta'}]}`` with one list per plan, indexed by day.  Empty when
        there is no baseline -- the caller shows the single-plan ladder
        instead.
        """
        if self.manual_result is None or not self.has_baseline:
            return {'days': [], 'rows': []}

        def by_ccy(balances):
            out: Dict[str, Dict[int, float]] = {}
            for b in balances:
                out.setdefault(b.ccy, {})[b.day] = b.balance
            return out

        opt = by_ccy(self.optimal_result.balances)
        man = by_ccy(self.manual_result.balances)

        days = sorted({d for m in list(opt.values()) + list(man.values()) for d in m})
        rows = []
        for ccy in opt.keys() | man.keys():
            o = [opt.get(ccy, {}).get(d, 0.0) for d in days]
            m = [man.get(ccy, {}).get(d, 0.0) for d in days]
            rows.append({
                'ccy': ccy,
                'optimal': o,
                'manual': m,
                'delta': [mi - oi for oi, mi in zip(o, m)],
            })
        rows.sort(key=lambda r: r['ccy'])
        return {'days': days, 'rows': rows}

    # ── Data extraction for UI tables ─────────────────────��───

    def get_info_summary(self) -> Dict[str, Any]:
        """Return a dict of display-ready summary values for the info panel.

        Keys:
            base_ccy, funding_id, horizon, currencies,
            fx_quotes, credit_carry_pa, debit_carry_pa,
            commission_tiers
        """
        mgr = self.cash_manager
        if mgr is None:
            return {}

        cfg = mgr.config
        return {
            'base_ccy': cfg.base_ccy,
            # Repurposed: this row identifies the loaded scenario, since the
            # page steps through a library rather than fetching a funding id.
            'funding_id': (
                f"{self.scenario['id']} — {self.scenario['name']}"
                if self.scenario else self.group_number or '—'
            ),
            'scenario_narrative': (self.scenario or {}).get('narrative', ''),
            'scenario_expectation': (self.scenario or {}).get('expectation', ''),
            'scenario_cost_reasoning': (self.scenario or {}).get('cost_reasoning', ''),
            'horizon': cfg.horizon_days,
            'currencies': cfg.currencies,
            'fx_quotes': cfg.fx_quotes,       # {ccy: {tenor: FXTenorQuote}}
            'credit_carry_pa': cfg.credit_carry_pa,
            'debit_carry_pa': cfg.debit_carry_pa,
            # Daily equivalents and the day count that produced them, so the
            # page can show where an annual rate turns into a per-day one.
            'credit_bps_per_day': dict(cfg.credit_carry_bps_per_day),
            'debit_bps_per_day': dict(cfg.debit_carry_bps_per_day),
            'day_count_basis': {c: cfg.day_count(c) for c in cfg.currencies},
            'tenors': dict(cfg.tenors),
            'spot_tenor': cfg.spot_tenor,
            'commission_tiers': cfg.commission_tiers,
        }

    def get_ledger_table_data(self) -> Dict[str, Any]:
        """Return data for the pre-trade cash ledger table.

        Returns a dict with keys:
            days  — list[int]
            rows  — list[dict] each with 'ccy' and per-day 'closing' values
        """
        mgr = self.cash_manager
        if mgr is None:
            return {'days': [], 'rows': []}

        ladder = mgr.cash_ladder  # List[CashLadderEntry]
        return self._pivot_ladder(ladder)

    def get_trades_table_data(self, result: Any = None) -> List[Dict[str, Any]]:
        """Return a plan's trades as a list of row dicts.

        Each dict has: ccy, day, tenor, direction, amount, settle_day.

        *result* defaults to the optimizer's plan.  Pass a ``ManualResult``
        to render a hand-entered one instead: the two carry the same
        ``trades`` list, so this reads either without special-casing.
        """
        result = result if result is not None else self.optimal_result
        if result is None:
            return []
        return [
            {
                'ccy': t.ccy,
                'day': t.day,
                'tenor': t.tenor,
                'direction': t.direction.value,
                'amount': t.amount,
                'settle_day': t.settle_day,
            }
            for t in result.trades
        ]

    def get_after_trade_table_data(self, result: Any = None) -> Dict[str, Any]:
        """Return after-trade balance data (pivoted like the ledger).

        *result* defaults to the optimizer's plan; pass a ``ManualResult``
        for a hand-entered one.
        """
        result = result if result is not None else self.optimal_result
        if result is None:
            return {'days': [], 'rows': []}

        return self._pivot_balances(result.balances)

    def get_cost_breakdown(self, result: Any = None) -> Dict[str, float]:
        """Return cost breakdown as a dict.

        Keys: credit_carry, debit_carry, commission,
        spread, terminal_unwind, total.

        Uses the CashManager's _compute_cost helper for the optimizer
        result so we get an itemised breakdown (the Result object only
        stores the aggregate total_cost).

        All six components must be listed.  ``CostBreakdown`` gained
        ``spread_cost`` and ``terminal_unwind_cost`` when trades started
        being valued at the rate they are dealt at rather than at spot mid,
        and a table that omits them does not add up to its own total -- it
        understates the plan by the half-spread on every trade, which is
        precisely the defect that made a duplicate CostBreakdown worth
        deleting in the first place.
        """
        mgr = self.cash_manager
        result = result if result is not None else self.optimal_result
        if mgr is None or result is None:
            return {}

        # Use the CashManager's deterministic cost calculator for
        # itemised breakdown of the result.  It reads only ``balances``
        # and ``trades``, which a ManualResult carries too, so the same
        # call prices both sides of a what-if comparison.
        cb = mgr._compute_optimal_cost_breakdown(result)
        return {
            'credit_carry': cb.credit_carry_cost,
            'debit_carry': cb.debit_carry_cost,
            'commission': cb.commission_cost,
            'spread': cb.spread_cost,
            'terminal_unwind': cb.terminal_unwind_cost,
            'total': cb.total_cost,
        }

    def get_cost_components(self, result: Any = None) -> List[Dict[str, Any]]:
        """Return each cost line with the arithmetic that produced it.

        Returned in **value-impact** convention: a cost is negative and a
        benefit positive, which is how money leaving and entering an account
        normally reads.  The model itself works the other way round, because
        its objective is a cost and is minimised -- so anything comparing
        these figures against ``result.total_cost`` has to flip the sign.

        The workings come from ``cash_optimizer_poc.workings``, which derives
        them independently and then checks itself against the cost model.  A
        line whose ``reconciles`` flag is False is telling you the derivation
        disagrees with the model -- believe the value, not the formula, and
        treat it as a defect.
        """
        from scripts.cash_optimizer_poc.workings import cost_workings

        result = result if result is not None else self.optimal_result
        if self.cash_manager is None or result is None:
            return []
        return [
            {
                'key': c.key,
                'label': c.label,
                'value': c.value,
                'workings': c.workings,
                'reconciles': c.reconciles,
            }
            for c in cost_workings(self.cash_manager, result,
                                   value_impact=True)
        ]

    # ── Private helpers ───────────────────────────────────────

    @staticmethod
    def _pivot_ladder(
        entries: list,
    ) -> Dict[str, Any]:
        """Pivot CashLadderEntry list into {days, rows} for table rendering."""
        if not entries:
            return {'days': [], 'rows': []}

        days: List[int] = []
        ccys: List[str] = []
        grid: Dict[Tuple[str, int], float] = {}
        for e in entries:
            if e.day not in days:
                days.append(e.day)
            if e.ccy not in ccys:
                ccys.append(e.ccy)
            grid[(e.ccy, e.day)] = e.closing

        rows = []
        for ccy in ccys:
            row: Dict[str, Any] = {'ccy': ccy}
            for d in days:
                row[f'd{d}'] = grid.get((ccy, d), 0.0)
            rows.append(row)

        return {'days': days, 'rows': rows}

    @staticmethod
    def _pivot_balances(
        balances: list,
    ) -> Dict[str, Any]:
        """Pivot BalanceSnapshot list into {days, rows} for table rendering."""
        if not balances:
            return {'days': [], 'rows': []}

        days: List[int] = []
        ccys: List[str] = []
        grid: Dict[Tuple[str, int], float] = {}
        for b in balances:
            if b.day not in days:
                days.append(b.day)
            if b.ccy not in ccys:
                ccys.append(b.ccy)
            grid[(b.ccy, b.day)] = b.balance

        rows = []
        for ccy in ccys:
            row: Dict[str, Any] = {'ccy': ccy}
            for d in days:
                row[f'd{d}'] = grid.get((ccy, d), 0.0)
            rows.append(row)

        return {'days': days, 'rows': rows}

