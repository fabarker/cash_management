"""Cash Management page state.

Holds all mutable, per-page, per-session data.  No NiceGUI dependencies.
Validation predicates and data-preparation helpers live here so the
Controller stays thin.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
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

    def get_trades_table_data(self) -> List[Dict[str, Any]]:
        """Return the recommended trades as a list of row dicts.

        Each dict has: ccy, day, tenor, direction, amount, settle_day.
        """
        result = self.optimal_result
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

    def get_after_trade_table_data(self) -> Dict[str, Any]:
        """Return after-trade balance data (pivoted like the ledger)."""
        result = self.optimal_result
        if result is None:
            return {'days': [], 'rows': []}

        return self._pivot_balances(result.balances)

    def get_cost_breakdown(self) -> Dict[str, float]:
        """Return cost breakdown as a dict.

        Keys: credit_carry, debit_carry, fx_exposure, commission,
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
        result = self.optimal_result
        if mgr is None or result is None:
            return {}

        # Use the CashManager's deterministic cost calculator for
        # itemised breakdown of the optimal result.
        cb = mgr._compute_optimal_cost_breakdown(result)
        return {
            'credit_carry': cb.credit_carry_cost,
            'debit_carry': cb.debit_carry_cost,
            'fx_exposure': cb.fx_exposure_cost,
            'commission': cb.commission_cost,
            'spread': cb.spread_cost,
            'terminal_unwind': cb.terminal_unwind_cost,
            'total': cb.total_cost,
        }

    def get_cost_components(self) -> List[Dict[str, Any]]:
        """Return each cost line with the arithmetic that produced it.

        The workings come from ``cash_optimizer_poc.workings``, which derives
        them independently and then checks itself against the cost model.  A
        line whose ``reconciles`` flag is False is telling you the derivation
        disagrees with the model -- believe the value, not the formula, and
        treat it as a defect.
        """
        from scripts.cash_optimizer_poc.workings import cost_workings

        if self.cash_manager is None or self.optimal_result is None:
            return []
        return [
            {
                'key': c.key,
                'label': c.label,
                'value': c.value,
                'workings': c.workings,
                'reconciles': c.reconciles,
            }
            for c in cost_workings(self.cash_manager, self.optimal_result)
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

