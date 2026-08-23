"""Cash Management page state.

Holds all mutable, per-page, per-session data.  No NiceGUI dependencies.
Validation predicates and data-preparation helpers live here so the
Controller stays thin.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, TYPE_CHECKING

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
        The ``CashManager`` instance returned by
        ``CashManager.from_group_number()``.  Stored so the controller
        can call ``solve_optimal()`` without re-fetching projections.
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

    # ── Validation predicates ─────────────────────────────────

    def can_load(self, refs: CashManagementRefs) -> bool:
        """Return True when the group number input is non-empty and
        no operation is currently in-flight."""
        value = (refs.group_number_input.value or '').strip()
        return bool(value) and not self.is_loading

    def can_generate_trades(self) -> bool:
        """Return True when a CashManager is loaded and not busy."""
        return self.cash_manager is not None and not self.is_loading

    # ── Data helpers (called by the controller) ───────────────

    def _get_or_create_fx_service(self) -> Any:
        """Return a persistent RefinitivFXService instance.

        The LSEG/Refinitiv ``rd`` library uses a global session that
        cannot be cleanly re-opened after ``rd.close_session()`` is
        called.  ``CashManager._fetch_fx_rates`` disconnects the
        service (and closes the global session) in its ``finally``
        block when it owns the service.

        By creating a single ``RefinitivFXService`` here and passing
        it into ``CashManager.from_group_number(fx_service=...)``,
        ``_fetch_fx_rates`` sees ``owns_service = False`` and skips
        the disconnect.  The session stays open for subsequent loads.
        """
        if self._fx_service is not None:
            return self._fx_service

        from pmg_core.dataModel.vendor.refinitiv.FXForwardPrices import (
            RefinitivFXService,
        )
        # Don't connect eagerly — _ensure_legs defers the connection
        # until after checking in-memory and daily disk caches.
        svc = RefinitivFXService()
        self._fx_service = svc
        return svc

    def load_cash_manager(self, group_number: str) -> None:
        """Create a CashManager for *group_number* via the POC module.

        This is a blocking I/O call (network fetches for projections,
        FX, and interest rates).  The controller must call it inside
        ``asyncio.to_thread`` or ``run.io_bound``.

        The resulting CashManager is stored on ``self.cash_manager``.

        A persistent ``RefinitivFXService`` is reused across calls so
        the Refinitiv global session is not closed and re-opened
        (which the LSEG library does not support cleanly).

        Raises
        ------
        Exception
            Propagated from CashManager.from_group_number on any
            failure (invalid group, network error, missing data, etc.).
        """
        from scripts.cash_optimizer_poc.cash_manager import CashManager

        fx_svc = self._get_or_create_fx_service()

        mgr = CashManager.from_group_number(
            group_number,
            fx_service=fx_svc,
        )
        self.cash_manager = mgr
        self.group_number = group_number
        self.optimal_result = None  # Clear stale results on reload

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
            new_fx = CashManager._fetch_fx_rates(
                base_ccy=mgr.config.base_ccy,
                foreign_ccys=new_foreign,
                fx_service=fx_service,
            )

            # Merge into existing fx_quotes
            for ccy, tenor_quotes in new_fx.items():
                mgr.config.fx_quotes[ccy] = tenor_quotes

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
            raise RuntimeError('No CashManager loaded — call load_cash_manager() first.')

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
            # NOTE: The CashManager does not persist the funding_id after
            # construction.  It is used only internally during from_group_number().
            # If you need to display it, the CashManager class would need
            # a property added.  For now we show the group number instead.
            'funding_id': self.group_number,
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

        Keys: credit_carry, debit_carry, fx_exposure, commission, total.
        Uses the CashManager's _compute_cost helper for the optimizer
        result so we get an itemised breakdown (the Result object only
        stores the aggregate total_cost).
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
            'total': cb.total_cost,
        }

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

