"""Cash Management page controller.

Extends ``BaseController[CashManagementRefs, CashManagementState]``.
Handles event wiring, async workflows, and all UI updates.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from nicegui import ui, run

from ..base_controller import BaseController
from .state import CashManagementState
from .types import CashManagementRefs

log = logging.getLogger(__name__)


class CashManagementController(BaseController[CashManagementRefs, CashManagementState]):
    """Controller for the Cash Management page.

    Responsibilities:
    - Wire UI events (``wire()``)
    - Orchestrate async data loading and optimizer runs
    - Populate tables and info panels from state data
    - Show/hide loading indicators and error messages
    """

    def __init__(
        self,
        *,
        refs: CashManagementRefs,
        state: CashManagementState,
    ) -> None:
        super().__init__(refs=refs, state=state)
        # Editable ledger data (Python-side source of truth)
        self._ledger_days: List[int] = []
        self._ledger_rows: List[Dict[str, Any]] = []
        # References to value labels keyed by (row_idx, field)
        # so we can update in-place without creating/destroying elements
        self._cell_labels: Dict[tuple, ui.label] = {}
        # Whether the grid has been built yet
        self._ledger_built: bool = False
        # Pending edit coordinates for the edit dialog
        self._edit_row_idx: int = -1
        self._edit_field: str = ''
        # Guard so programmatic set_value() on the constraint switch
        # (e.g. while syncing from config on load) does not re-enter the
        # change handler and re-trigger UI updates.
        self._suppress_switch_event: bool = False

    # ================================================================
    # UI update helpers
    # ================================================================

    def _update_load_btn_state(self, *_) -> None:
        """Enable/disable the Load button based on input validation."""
        if self.state.can_load(self.refs):
            self.refs.load_btn.enable()
        else:
            self.refs.load_btn.disable()

    def _update_generate_btn_state(self) -> None:
        """Enable/disable the Generate Trades button."""
        if self.state.can_generate_trades():
            self.refs.generate_trades_btn.enable()
            self.refs.add_cashflow_btn.enable()
        else:
            self.refs.generate_trades_btn.disable()
            self.refs.add_cashflow_btn.disable()

    def _show_error(self, message: str) -> None:
        """Display an error message below the search bar."""
        self.state.error_message = message
        self.refs.error_label.set_text(message)
        self.refs.error_label.classes(remove='hidden')

    def _hide_error(self) -> None:
        """Clear any visible error message."""
        self.state.error_message = ''
        self.refs.error_label.set_text('')
        self.refs.error_label.classes(add='hidden')

    def _open_progress(self, title: str) -> None:
        """Open the persistent progress dialog with a custom title."""
        self.refs.progress_title.set_text(title)
        self.refs.progress_dialog.open()

    def _close_progress(self) -> None:
        """Close the progress dialog."""
        self.refs.progress_dialog.close()

    def _show_info_panel(self) -> None:
        """Populate and reveal the info panel from state data."""
        info = self.state.get_info_summary()
        if not info:
            return

        # Dark header bar stats
        self.refs.base_ccy_label.set_text(info['base_ccy'])
        self.refs.funding_id_label.set_text(info.get('funding_id', '—'))

        # Narrative and what to look for, so the scenario explains itself.
        narrative = info.get('scenario_narrative', '')
        expectation = info.get('scenario_expectation', '')
        caption = narrative
        if expectation:
            caption = f'{narrative}  ·  Expect: {expectation}' if narrative else expectation
        self.refs.scenario_caption.set_text(caption)
        self.refs.horizon_label.set_text(f'{info["horizon"]} days')
        self.refs.currencies_label.set_text(', '.join(info['currencies']))

        # FX quotes + credit + debit rates: unified aligned table
        self._populate_rates_table(info)

        # Commission tiers
        self._populate_commission_section(
            self.refs.commission_container,
            info.get('commission_tiers', []),
        )

        # Sync constraint switches from the loaded config and reveal
        # the constraints section.
        mgr = self.state.cash_manager
        if mgr is not None:
            current = bool(getattr(mgr.config.constraints, 'holding_ceiling', True))
            # set_value triggers on_value_change; use a guarded write
            self._suppress_switch_event = True
            try:
                self.refs.holding_ceiling_switch.set_value(current)
            finally:
                self._suppress_switch_event = False
        self.refs.constraints_section.classes(remove='hidden')

        self.refs.info_panel.classes(remove='hidden')

    def _populate_rates_table(self, info: Dict[str, Any]) -> None:
        """Render FX quotes, credit and debit rates as styled lists.

        Each category (FX T0, FX T1, FX T2, Credit, Debit) is rendered
        as a titled list matching the commission tiers style.
        Base currency shows '–' for FX columns.
        FX mid rates are truncated to 4 decimal places (100 pips).
        """
        self.refs.fx_quotes_container.clear()

        base_ccy = info.get('base_ccy', '')
        fx_quotes = info.get('fx_quotes', {})
        credit = info.get('credit_carry_pa', {})
        debit = info.get('debit_carry_pa', {})

        # Build a unified currency list, base first
        all_ccys: list = []
        for ccy in info.get('currencies', []):
            if ccy not in all_ccys:
                all_ccys.append(ccy)
        for ccy in credit:
            if ccy not in all_ccys:
                all_ccys.append(ccy)
        for ccy in debit:
            if ccy not in all_ccys:
                all_ccys.append(ccy)
        if base_ccy in all_ccys:
            all_ccys.remove(base_ccy)
            all_ccys.insert(0, base_ccy)

        if not all_ccys:
            with self.refs.fx_quotes_container:
                ui.label('No rate data available.').classes('info-label')
            return

        # Collect tenors
        all_tenors: list = []
        for tenors in fx_quotes.values():
            for t in tenors:
                if t not in all_tenors:
                    all_tenors.append(t)
        all_tenors.sort()

        with self.refs.fx_quotes_container:

            # Build a single borderless HTML table for perfect row alignment
            # Columns: CCY | T0 | T1 | T2 | Credit | Debit
            tenor_ths = ''.join(
                f'<th class="rates-th">{t}</th>' for t in all_tenors
            )
            header = (
                f'<tr>'
                f'<th class="rates-th rates-th-label"></th>'
                f'{tenor_ths}'
                f'<th class="rates-th">Credit (p.a.)</th>'
                f'<th class="rates-th">Debit (p.a.)</th>'
                f'</tr>'
            )

            body = ''
            for ccy in all_ccys:
                is_base = ccy.upper() == base_ccy.upper()

                # FX mid cells
                fx_cells = ''
                for tenor in all_tenors:
                    if is_base:
                        fx_cells += '<td class="rates-td rates-muted">&ndash;</td>'
                    else:
                        ccy_tenors = fx_quotes.get(ccy, {})
                        quote = ccy_tenors.get(tenor)
                        if quote and quote.mid is not None:
                            truncated = int(quote.mid * 10000) / 10000
                            fx_cells += f'<td class="rates-td">{truncated:.4f}</td>'
                        else:
                            fx_cells += '<td class="rates-td rates-muted">&ndash;</td>'

                cr = credit.get(ccy)
                cr_cell = f'<td class="rates-td">{cr:.2f}%</td>' if cr is not None else '<td class="rates-td rates-muted">&ndash;</td>'
                dr = debit.get(ccy)
                dr_cell = f'<td class="rates-td">{dr:.2f}%</td>' if dr is not None else '<td class="rates-td rates-muted">&ndash;</td>'

                body += f'<tr><td class="rates-td rates-label">{ccy}</td>{fx_cells}{cr_cell}{dr_cell}</tr>'

            html = (
                f'<table class="rates-table">'
                f'<thead>{header}</thead>'
                f'<tbody>{body}</tbody>'
                f'</table>'
            )
            ui.html(html, sanitize=False)



    def _populate_commission_section(
        self, container: ui.column, tiers: list,
    ) -> None:
        """Render commission tiers as a single ui.html element.

        Replaces all children after the title label with one HTML
        block to avoid element create/destroy churn.
        """
        children = list(container)
        for child in children[1:]:
            child.delete()
        with container:
            if not tiers:
                ui.html('<div class="info-label">—</div>', sanitize=False)
                return
            items = ''
            for tier in tiers:
                items += (
                    f'<div class="commission-item">'
                    f'<div>≤ {tier.threshold:,.0f}</div>'
                    f'<div style="font-weight: 600; color: #1c2b36;">'
                    f'{tier.rate_bps:.1f} bps</div></div>'
                )
            ui.html(items, sanitize=False)

    # ── Editable projections (CSS grid + edit dialog) ────────────

    def _populate_editable_ledger(self) -> None:
        """Build (or rebuild) the editable cash projection grid.

        On the first call, creates the CSS-grid layout with header
        labels and per-cell ``ui.label`` elements (with click handlers).
        References to every value label are stored in
        ``self._cell_labels`` keyed by ``(row_idx, field)``.

        On subsequent calls (e.g. loading a different group number),
        the grid is torn down and rebuilt because the number of
        currencies/days may have changed.

        After a single-cell edit, call ``_update_cell_display()``
        instead — it mutates existing labels in-place without touching
        the element tree, which avoids the NiceGUI reconciliation
        that triggers ``window.location.reload()``.
        """
        ledger_data = self.state.get_ledger_table_data()
        self._ledger_days = ledger_data.get('days', [])
        rows = ledger_data.get('rows', [])

        # Build internal row list: [{ccy, d0, d1, ...}, ...]
        self._ledger_rows = []
        for row in rows:
            r: Dict[str, Any] = {'ccy': row['ccy']}
            for d in self._ledger_days:
                r[f'd{d}'] = round(row.get(f'd{d}', 0.0), 2)
            self._ledger_rows.append(r)

        # Tear down the old grid (only happens on re-load, not on cell edit)
        self.refs.ledger_table_container.clear()
        self._cell_labels.clear()
        self._ledger_built = False

        self._build_ledger_grid()

    def _build_ledger_grid(self) -> None:
        """Create the CSS-grid table elements once.  Called by _populate_editable_ledger."""
        if not self._ledger_rows:
            with self.refs.ledger_table_container:
                ui.label('No data available.').classes('text-base text-gray-500')
            return

        days = self._ledger_days
        day_fields = [f'd{d}' for d in days]

        with self.refs.ledger_table_container:
            grid_template = f'auto {"auto " * len(days)}'.strip()
            with ui.element('div').style(
                f'display: grid; grid-template-columns: {grid_template}; '
                f'border: 1px solid #c8ced3; border-radius: 6px; overflow: hidden; '
                f'width: fit-content; font-family: "Goldman Sans", Arial, sans-serif;'
            ):
                # ── Header row ──
                hdr_style = (
                    'padding: 10px 18px; font-weight: 600; '
                    'color: rgba(255,255,255,0.92); font-size: 12px; '
                    'text-transform: uppercase; letter-spacing: 0.6px; '
                    'white-space: nowrap; '
                    'background: linear-gradient(180deg, #3a4a56 0%, #2d3c46 100%); '
                    'border-bottom: 2px solid #1c2b36; '
                    'border-right: 1px solid rgba(255,255,255,0.10);'
                )
                ui.label('CCY').style(hdr_style + ' text-align: left;')
                for i, d in enumerate(days):
                    extra = '' if i < len(days) - 1 else ' border-right: none;'
                    ui.label(f'D{d}').style(hdr_style + f' text-align: right;{extra}')

                # ── Data rows ──
                for row_idx, row in enumerate(self._ledger_rows):
                    is_even = row_idx % 2 == 1
                    bg = '#f8f9fa' if is_even else '#ffffff'

                    # CCY cell (not editable)
                    ui.label(row['ccy']).style(
                        f'padding: 9px 18px; font-weight: 700; color: #1c2b36; '
                        f'background-color: {bg}; font-size: 14px; '
                        f'border-right: 1px solid #dde0e3; '
                        f'border-bottom: 1px solid #e4e7ea; white-space: nowrap;'
                    )

                    # Value cells (editable — click to open dialog)
                    for col_idx, field in enumerate(day_fields):
                        val = row.get(field, 0.0)
                        color, weight = self._cell_color_weight(val)
                        br = '' if col_idx < len(day_fields) - 1 else ' border-right: none;'
                        cell = ui.label(f'{val:,.2f}').style(
                            f'padding: 9px 18px; color: {color}; font-weight: {weight}; '
                            f'font-size: 14px; text-align: right; cursor: pointer; '
                            f'font-variant-numeric: tabular-nums; white-space: nowrap; '
                            f'background-color: {bg}; '
                            f'border-right: 1px solid #eef0f2; '
                            f'border-bottom: 1px solid #e4e7ea;{br}'
                        )
                        # Capture row_idx and field in the closure
                        cell.on('click', lambda _, r=row_idx, f=field: self._open_edit_dialog(r, f))
                        # Store the reference so we can update in-place later
                        self._cell_labels[(row_idx, field)] = cell

        self._ledger_built = True

    @staticmethod
    def _cell_color_weight(val: float) -> tuple:
        """Return (color, font-weight) CSS values for a cell value."""
        if val < -0.005:
            return '#c62828', '600'
        elif val > 0.005:
            return '#1b5e20', '500'
        return '#212529', '400'

    def _update_cell_display(self, row_idx: int, field: str) -> None:
        """Update a single cell label in-place (no element create/destroy).

        This is the key method that avoids NiceGUI's reconciliation
        and the resulting ``window.location.reload()``.
        """
        cell = self._cell_labels.get((row_idx, field))
        if cell is None:
            return
        val = self._ledger_rows[row_idx].get(field, 0.0)
        color, weight = self._cell_color_weight(val)
        cell.set_text(f'{val:,.2f}')
        # Use add= to override only color/weight without wiping other styles
        cell.style(add=f'color: {color}; font-weight: {weight};')

    def _open_edit_dialog(self, row_idx: int, field: str) -> None:
        """Open the edit dialog for a specific cell."""
        row = self._ledger_rows[row_idx]
        ccy = row.get('ccy', '?')
        current = row.get(field, 0.0)

        self._edit_row_idx = row_idx
        self._edit_field = field

        self.refs.edit_dialog_title.set_text(f'Edit {ccy} — {field.upper()}')
        self.refs.edit_dialog_input.set_value(current)
        self.refs.edit_dialog.open()

    def _on_edit_save(self, *_) -> None:
        """Save the edited value from the dialog back into the ledger data.

        Updates the Python-side data and the single affected cell label
        in-place — no elements are created or destroyed.
        """
        val = self.refs.edit_dialog_input.value
        try:
            val = round(float(val), 2)
        except (ValueError, TypeError):
            ui.notify('Invalid number', type='warning', position='top')
            return

        if 0 <= self._edit_row_idx < len(self._ledger_rows):
            self._ledger_rows[self._edit_row_idx][self._edit_field] = val
            # Update the single cell in-place (no DOM churn)
            self._update_cell_display(self._edit_row_idx, self._edit_field)

        self.refs.edit_dialog.close()

    def _extract_table_edits(self) -> Dict[str, Any]:
        """Read the current editable ledger data (Python-side source of truth).

        Returns a dict with 'days' and 'rows' that the state can
        apply to the CashManager in a background thread.
        """
        day_fields = [f'd{d}' for d in self._ledger_days]

        extracted_rows = []
        for row in self._ledger_rows:
            ccy = row['ccy'].replace(' (Base)', '')
            closing_values = [float(row.get(f, 0.0)) for f in day_fields]
            extracted_rows.append({'ccy': ccy, 'closing': closing_values})

        return {'days': list(self._ledger_days), 'rows': extracted_rows}

    # ── Table rendering helpers ────────────────────────────────

    def _render_pivoted_table(
        self,
        container: ui.element,
        data: Dict[str, Any],
        extra_css_class: str = '',
    ) -> None:
        """Render a pivoted (ccy × days) table as raw HTML inside *container*.

        Uses the portfolio_trading table styling with table-header-row /
        table-header-cell / table-data-row / table-data-cell classes.
        """
        container.clear()
        days = data.get('days', [])
        rows = data.get('rows', [])

        if not rows:
            with container:
                ui.label('No data available.').classes(
                    'text-base text-gray-500'
                ).style('padding: 20px 0;')
            return

        # Build HTML table with portfolio_trading-style classes
        header_cells = ''.join(
            f'<th class="table-header-cell">D{d}</th>' for d in days
        )
        header = (
            f'<tr class="table-header-row">'
            f'<th class="table-header-cell" style="text-align:left">CCY</th>'
            f'{header_cells}</tr>'
        )

        body_rows = ''
        for row in rows:
            cells = ''
            for d in days:
                val = row.get(f'd{d}', 0.0)
                css_class = 'table-data-cell'
                if val < -0.005:
                    css_class += ' negative'
                elif val > 0.005:
                    css_class += ' positive'
                cells += f'<td class="{css_class}">{val:,.2f}</td>'
            body_rows += (
                f'<tr class="table-data-row">'
                f'<td class="table-data-cell">{row["ccy"]}</td>'
                f'{cells}</tr>'
            )

        table_class = f'cash-table {extra_css_class}'.strip()
        html = (
            f'<table class="{table_class}">'
            f'<thead>{header}</thead>'
            f'<tbody>{body_rows}</tbody></table>'
        )

        with container:
            ui.html(html, sanitize=False)

    def _render_trades_table(
        self,
        container: ui.element,
        trades: List[Dict[str, Any]],
    ) -> None:
        """Render the recommended trades as a table."""
        container.clear()

        if not trades:
            with container:
                ui.label('No trades recommended.').classes(
                    'text-base text-gray-500'
                ).style('padding: 20px 0;')
            return

        header = (
            '<tr class="table-header-row">'
            '<th class="table-header-cell" style="text-align:left">CCY</th>'
            '<th class="table-header-cell" style="text-align:center">Day</th>'
            '<th class="table-header-cell" style="text-align:center">Tenor</th>'
            '<th class="table-header-cell" style="text-align:left">Direction</th>'
            '<th class="table-header-cell">Amount</th>'
            '<th class="table-header-cell" style="text-align:center">Settle Day</th>'
            '</tr>'
        )
        body = ''
        for t in trades:
            dir_class = 'table-data-cell positive' if t['direction'] == 'BUY' else 'table-data-cell negative'
            body += (
                f'<tr class="table-data-row">'
                f'<td class="table-data-cell">{t["ccy"]}</td>'
                f'<td class="table-data-cell" style="text-align:center">{t["day"]}</td>'
                f'<td class="table-data-cell" style="text-align:center">{t["tenor"]}</td>'
                f'<td class="{dir_class}" style="text-align:left">{t["direction"]}</td>'
                f'<td class="table-data-cell">{t["amount"]:,.2f}</td>'
                f'<td class="table-data-cell" style="text-align:center">{t["settle_day"]}</td>'
                f'</tr>'
            )
        html = (
            f'<table class="cash-table trades-table">'
            f'<thead>{header}</thead><tbody>{body}</tbody></table>'
        )
        with container:
            ui.html(html, sanitize=False)

    def _render_cost_breakdown(self, costs: Dict[str, float]) -> None:
        """Populate the cost breakdown container as a single HTML block."""
        self.refs.cost_breakdown_container.clear()

        if not costs:
            with self.refs.cost_breakdown_container:
                ui.html('<div class="text-base text-gray-500">No cost data available.</div>',
                        sanitize=False)
            return

        components = [
            ('Credit carry (differential)', costs.get('credit_carry', 0.0)),
            ('Debit carry (overdraft)', costs.get('debit_carry', 0.0)),
            ('FX exposure penalty', costs.get('fx_exposure', 0.0)),
            ('Commission', costs.get('commission', 0.0)),
            ('FX spread paid', costs.get('spread', 0.0)),
            ('Terminal unwind', costs.get('terminal_unwind', 0.0)),
        ]

        rows_html = ''
        for label, value in components:
            if value < -0.00005:
                css = 'color: #2e7d32;'
            elif value > 0.00005:
                css = 'color: #c62828;'
            else:
                css = 'color: #495057;'
            rows_html += (
                f'<div class="cost-row">'
                f'<div style="color: #495057; flex: 1; min-width: 250px;">{label}</div>'
                f'<div style="font-variant-numeric: tabular-nums; min-width: 120px; '
                f'text-align: right; {css}">{value:,.4f}</div>'
                f'</div>'
            )

        total = costs.get('total', 0.0)

        # The rows above must reconcile to the total.  They have not always:
        # two components were missing and the table silently understated
        # every plan by the spread.  Say so rather than print a sum that
        # does not add up.
        residual = total - sum(v for _, v in components)
        if abs(residual) > 0.0005:
            rows_html += (
                f'<div class="cost-row">'
                f'<div style="color: #b26a00; flex: 1; min-width: 250px;">'
                f'Unattributed &mdash; breakdown does not reconcile</div>'
                f'<div style="font-variant-numeric: tabular-nums; min-width: 120px; '
                f'text-align: right; color: #b26a00;">{residual:,.4f}</div>'
                f'</div>'
            )

        total_color = '#2e7d32' if total < -0.005 else '#c62828' if total > 0.005 else '#1F3864'
        rows_html += (
            f'<div class="cost-row total">'
            f'<div style="color: #1F3864; flex: 1; min-width: 250px;">TOTAL COST</div>'
            f'<div style="font-variant-numeric: tabular-nums; min-width: 120px; '
            f'text-align: right; color: {total_color};">{total:,.4f}</div>'
            f'</div>'
        )

        # Solver status
        if self.state.optimal_result is not None:
            status = getattr(self.state.optimal_result, 'status', '')
            rows_html += (
                f'<div style="font-size: 12px; color: #9e9e9e; margin-top: 8px;">'
                f'Solver status: {status}</div>'
            )

        with self.refs.cost_breakdown_container:
            with ui.card().classes('cost-card'):
                ui.html(rows_html, sanitize=False)

    # ================================================================
    # Event handlers
    # ================================================================

    async def _on_load(self, *_) -> None:
        """Load the next scenario, or the one named in the input.

        The page steps through a fixed library rather than fetching a group
        from Maxis, so the button needs nothing typed: each press advances
        one place and wraps at the end.  Typing an id (``S07``) or a
        position (``7``) jumps there instead.
        """
        if not self.state.can_load(self.refs):
            return

        selector = (self.refs.group_number_input.value or '').strip()

        self._hide_error()
        self.state.is_loading = True
        self._update_load_btn_state()
        self._open_progress('Loading scenario…')

        try:
            await run.io_bound(self.state.load_scenario, selector)

            # Populate the UI from the loaded state
            self._show_info_panel()

            # Populate the editable pre-trade cash projections
            self._populate_editable_ledger()
            self.refs.ledger_section.classes(remove='hidden')

            # Hide stale results from a previous run
            self.refs.results_section.classes(add='hidden')

            sc = self.state.scenario or {}
            ui.notify(
                f'Loaded {sc.get("id", "")} — {sc.get("name", "")}',
                type='positive',
                position='top',
            )

        except Exception as exc:
            log.exception('Failed to load scenario %r', selector)
            self._show_error(f'Failed to load scenario: {exc}')
            ui.notify(str(exc), type='negative', position='top')

        finally:
            self.state.is_loading = False
            self._close_progress()
            self._update_load_btn_state()
            self._update_generate_btn_state()

    async def _on_generate_trades(self, *_) -> None:
        """Handle the Generate Optimal Trades button click.

        Runs the LP/MIP solver in a background thread, then populates
        the results section (trades table, after-trade table, cost
        breakdown).
        """
        if not self.state.can_generate_trades():
            return

        self._hide_error()
        self.state.is_loading = True
        self._update_generate_btn_state()
        self._update_load_btn_state()
        self._open_progress('Running optimizer…')

        try:
            # Extract table edits from Python-side ledger data,
            # then apply + solve on background thread (blocking I/O)
            edits = self._extract_table_edits()

            await run.io_bound(self.state.apply_edits_and_optimize, edits)

            # Render trades table
            trades_data = self.state.get_trades_table_data()
            self._render_trades_table(self.refs.trades_table_rows, trades_data)

            # Render after-trade balance table
            after_data = self.state.get_after_trade_table_data()
            self._render_pivoted_table(
                self.refs.after_trade_table_rows,
                after_data,
                extra_css_class='after-trade-table',
            )

            # Render cost breakdown
            costs = self.state.get_cost_breakdown()
            self._render_cost_breakdown(costs)

            # Show the results section
            self.refs.results_section.classes(remove='hidden')

            # Notify
            n_trades = len(trades_data)
            total = costs.get('total', 0.0)
            ui.notify(
                f'Optimization complete — {n_trades} trade(s), '
                f'total cost {total:,.4f}',
                type='positive',
                position='top',
            )

        except Exception as exc:
            log.exception('Optimizer failed')
            self._show_error(f'Optimizer error: {exc}')
            ui.notify(str(exc), type='negative', position='top')

        finally:
            self.state.is_loading = False
            self._close_progress()
            self._update_generate_btn_state()
            self._update_load_btn_state()

    # ================================================================
    # Add-Cashflow dialog handlers
    # ================================================================

    def _open_add_cashflow_dialog(self, *_) -> None:
        """Populate currency / day selects from the current ledger and open the dialog."""
        if not self.state.can_generate_trades():
            return

        # Currencies: those currently shown in the ledger table
        ccys = [row['ccy'].replace(' (Base)', '') for row in self._ledger_rows]
        # Days: horizon days currently displayed
        days = list(self._ledger_days)

        if not ccys or not days:
            ui.notify('No cash projections loaded.', type='warning', position='top')
            return

        self.refs.add_cashflow_ccy_select.set_options(ccys, value=ccys[0])
        self.refs.add_cashflow_day_select.set_options(days, value=days[0])
        self.refs.add_cashflow_amount_input.set_value(0.0)
        self.refs.add_cashflow_dialog.open()

    def _on_add_cashflow_save(self, *_) -> None:
        """Validate dialog inputs and call ``CashManager.insert_cash_flow``."""
        ccy = self.refs.add_cashflow_ccy_select.value
        day = self.refs.add_cashflow_day_select.value
        raw_amount = self.refs.add_cashflow_amount_input.value

        if not ccy:
            ui.notify('Select a currency.', type='warning', position='top')
            return
        if day is None:
            ui.notify('Select a day.', type='warning', position='top')
            return
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            ui.notify('Invalid amount.', type='warning', position='top')
            return
        if amount == 0.0:
            ui.notify('Amount must be non-zero.', type='warning', position='top')
            return

        mgr = self.state.cash_manager
        if mgr is None:
            ui.notify('No CashManager loaded.', type='negative', position='top')
            return

        try:
            mgr.insert_cash_flow(ccy, int(day), amount)
        except Exception as exc:
            log.exception('insert_cash_flow failed')
            ui.notify(f'Failed to add cashflow: {exc}', type='negative', position='top')
            return

        # Stale optimal results are no longer valid
        self.state.optimal_result = None
        self.refs.results_section.classes(add='hidden')

        # Rebuild the ledger from the manager's updated cash_ladder.
        # NOTE: any unsaved cell edits are discarded — they are only
        # pushed into the optimizer when "Get Suggested Trades" is clicked.
        self._populate_editable_ledger()

        self.refs.add_cashflow_dialog.close()
        ui.notify(
            f'Added cashflow: {ccy} {amount:,.2f} on D{int(day)}',
            type='positive', position='top',
        )

    # ================================================================
    # Constraint toggle handlers
    # ================================================================

    def _on_holding_ceiling_change(self, e) -> None:
        """Flip the ``holding_ceiling`` constraint on the loaded CashManager.

        This switch used to target ``t0_debit_only``, a constraint that has
        since been removed from the model.  ``ConstraintFlags`` is not
        frozen, so setting it was accepted silently and did nothing at all
        — the switch moved, the plan never changed.

        ``holding_ceiling`` is the anti-speculation policy: it caps what may
        be held in each currency at the funding the near-term ladder can
        justify.  Turning it off lets the optimiser hold foreign currency
        purely for the interest differential, which is worth showing a user
        precisely because the cost usually *falls* when they do.

        Invalidates any cached optimal result and hides stale output so it
        is clear the plan needs re-running.
        """
        if self._suppress_switch_event:
            return

        mgr = self.state.cash_manager
        if mgr is None:
            return

        new_value = bool(e.value)
        mgr.config.constraints.holding_ceiling = new_value
        # Invalidate cached optimizer output
        if hasattr(mgr, '_optimal_result'):
            mgr._optimal_result = None
        self.state.optimal_result = None
        self.refs.results_section.classes(add='hidden')

        ui.notify(
            f'Speculative-holding limit {"ON" if new_value else "OFF"}. '
            f'Re-run "Get Suggested Trades" to apply.',
            type='info', position='top',
        )

    # ================================================================
    # Wiring
    # ================================================================

    def wire(self) -> None:
        """Attach all event handlers to UI elements.

        Called once at the end of ``page.py`` to bring the page to life.
        """
        # Search bar: enable/disable Load button based on input
        self.refs.group_number_input.on_value_change(self._update_load_btn_state)
        self.refs.group_number_input.on('clear', lambda: self._update_load_btn_state())
        self.refs.group_number_input.on('keydown.enter', self._on_load)

        # Load button
        self.refs.load_btn.on_click(self._on_load)

        # Generate Trades button
        self.refs.generate_trades_btn.on_click(self._on_generate_trades)

        # Add Cashflow button + dialog
        self.refs.add_cashflow_btn.on_click(self._open_add_cashflow_dialog)
        self.refs.add_cashflow_save_btn.on_click(self._on_add_cashflow_save)
        self.refs.add_cashflow_amount_input.on('keydown.enter', self._on_add_cashflow_save)

        # Constraint toggles
        self.refs.holding_ceiling_switch.on_value_change(self._on_holding_ceiling_change)

        # Edit dialog — save button and Enter key on input
        self.refs.edit_dialog_save_btn.on_click(self._on_edit_save)
        self.refs.edit_dialog_input.on('keydown.enter', self._on_edit_save)

        # Set initial button states
        self._update_load_btn_state()
        self._update_generate_btn_state()

