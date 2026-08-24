"""Cash Management page controller.

Extends ``BaseController[CashManagementRefs, CashManagementState]``.
Handles event wiring, async workflows, and all UI updates.
"""
from __future__ import annotations

import logging
from html import escape
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

        # Narrative, what to expect, and why that is the cheapest answer.
        self.refs.scenario_caption.set_content(self._scenario_caption_html(info))
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

    @staticmethod
    def _scenario_caption_html(info: Dict[str, Any]) -> str:
        """Narrative, expected outcome, and the cost logic behind it.

        The expectation says what a correct plan looks like; the reasoning
        says why that plan is the cheapest one, which is the part that makes
        the scenario worth running rather than just reading.
        """
        blocks = [
            (None, info.get('scenario_narrative', '')),
            ('Expect', info.get('scenario_expectation', '')),
            ('Why, on cost', info.get('scenario_cost_reasoning', '')),
        ]
        parts = []
        for heading, body in blocks:
            if not body:
                continue
            text = escape(body)
            if heading is None:
                parts.append(
                    f'<div style="margin-bottom:6px; color:#212529;">{text}</div>'
                )
            else:
                colour = '#1F3864' if heading == 'Expect' else '#0b6b5e'
                parts.append(
                    f'<div style="margin-bottom:4px;">'
                    f'<span style="color:{colour}; font-weight:700;">'
                    f'{heading}:</span> {text}</div>'
                )
        return ''.join(parts)

    @staticmethod
    def _rate_tip(kind: str, ccy: str, pa: float,
                  bps_per_day: Any, basis: Any) -> str:
        """Tooltip body turning an annual rate into a daily one.

        The day count is part of the answer, not a footnote: sterling
        accrues on 365 and the dollar on 360, so two rates a quarter of a
        point apart are not a quarter of a point apart per day.
        """
        if bps_per_day is None or basis is None:
            return ''
        return (
            f'<b>{escape(ccy)} {escape(kind.lower())} carry</b><br>'
            f'{pa:.2f}% p.a. &divide; {basis} (ACT/{basis}) &times; 10,000<br>'
            f'= <b>{bps_per_day:.4f} bps/day</b>'
        )

    @staticmethod
    def _tenor_tip(ccy: str, tenor: str, quotes: Dict[str, Any],
                   lags: Dict[str, int], base_ccy: str,
                   spot_tenor: str) -> str:
        """Tooltip body showing the carry a forward quote has priced in.

        Measured against the spot tenor, so spot is the reference and every
        other leg is quoted as points away from it.  Forward points are the
        interest differential wearing a different hat: a currency yielding
        more than the base trades at a discount, and the discount per day
        IS the differential per day.  Reporting it in bps/day puts it in
        the same unit as the credit column beside it, where the two can be
        read against one another.
        """
        lag, spot_lag = lags.get(tenor), lags.get(spot_tenor)
        own_q, spot_q = quotes.get(tenor), quotes.get(spot_tenor)
        if lag is None or spot_lag is None or own_q is None or spot_q is None:
            return ''
        head = f'<b>{escape(ccy)} {escape(tenor)}</b>'
        if lag == spot_lag:
            return (f'{head} &mdash; spot<br>'
                    f'The reference every other leg is<br>measured against.')
        days = lag - spot_lag
        pts = own_q.mid - spot_q.mid
        bpd = pts / spot_q.mid * 10_000 / days
        lean = 'discount' if bpd < 0 else 'premium'
        who = (f'{escape(ccy)} out-yields {escape(base_ccy)}' if bpd < 0
               else f'{escape(base_ccy)} out-yields {escape(ccy)}')
        return (
            f'{head} vs {escape(spot_tenor)} spot, {days:+d} day(s)<br>'
            f'{own_q.mid:.6f} &minus; {spot_q.mid:.6f} = {pts:+.6f}<br>'
            f'= <b>{bpd:+.4f} bps/day</b> &mdash; a forward {lean}<br>'
            f'{who}, so the rate offsets it.'
        )

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
        credit_bpd = info.get('credit_bps_per_day', {})
        debit_bpd = info.get('debit_bps_per_day', {})
        basis = info.get('day_count_basis', {})
        lags = info.get('tenors', {})
        spot_tenor = info.get('spot_tenor', '')

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

        def cell(tag: str, classes: str, text: str, tip: str = '') -> None:
            """One table cell, with an optional NiceGUI tooltip.

            This table used to be a single block of raw HTML, which gave
            perfect column alignment and left nothing for a tooltip to
            attach to.  It is built from elements now; the classes, and so
            the alignment, are unchanged.
            """
            with ui.element(tag).classes(classes):
                ui.html(text, sanitize=False, tag='span')
                if tip:
                    with ui.tooltip().classes('rate-tip-body'):
                        ui.html(tip, sanitize=False, tag='div')

        with self.refs.fx_quotes_container:
            with ui.element('table').classes('rates-table'):
                with ui.element('thead'):
                    with ui.element('tr'):
                        cell('th', 'rates-th rates-th-label', '')
                        for tenor in all_tenors:
                            cell('th', 'rates-th', escape(tenor))
                        cell('th', 'rates-th', 'Credit (p.a.)')
                        cell('th', 'rates-th', 'Debit (p.a.)')

                with ui.element('tbody'):
                    for ccy in all_ccys:
                        is_base = ccy.upper() == base_ccy.upper()
                        ccy_tenors = fx_quotes.get(ccy, {})
                        with ui.element('tr'):
                            cell('td', 'rates-td rates-label', escape(ccy))

                            for tenor in all_tenors:
                                quote = None if is_base else ccy_tenors.get(tenor)
                                if quote is None or quote.mid is None:
                                    cell('td', 'rates-td rates-muted', '&ndash;')
                                    continue
                                truncated = int(quote.mid * 10000) / 10000
                                cell('td', 'rates-td has-tip', f'{truncated:.4f}',
                                     self._tenor_tip(ccy, tenor, ccy_tenors, lags,
                                                     base_ccy, spot_tenor))

                            cr = credit.get(ccy)
                            if cr is None:
                                cell('td', 'rates-td rates-muted', '&ndash;')
                            else:
                                cell('td', 'rates-td has-tip', f'{cr:.2f}%',
                                     self._rate_tip('Credit', ccy, cr,
                                                    credit_bpd.get(ccy),
                                                    basis.get(ccy)))
                            dr = debit.get(ccy)
                            if dr is None:
                                cell('td', 'rates-td rates-muted', '&ndash;')
                            else:
                                cell('td', 'rates-td has-tip', f'{dr:.2f}%',
                                     self._rate_tip('Debit', ccy, dr,
                                                    debit_bpd.get(ccy),
                                                    basis.get(ccy)))


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
        """Populate the cost breakdown, each line with its own arithmetic.

        Shown in value-impact convention: a cost is negative, a benefit
        positive.  That is the opposite of the objective, which is a cost and
        is minimised, so the total here is the negative of
        ``result.total_cost``.  The workings flip with it -- each derivation
        is written so its own arithmetic produces the sign beside it.

        The figure on its own says how much; the workings say where it came
        from -- which tier of the commission schedule, how many balance-days
        at what rate, which side of the quote.  Those derivations are checked
        against the cost model before they are rendered, and a line that
        failed the check says so instead.
        """
        self.refs.cost_breakdown_container.clear()

        components = self.state.get_cost_components()
        if not components and not costs:
            with self.refs.cost_breakdown_container:
                ui.html('<div class="text-base text-gray-500">No cost data available.</div>',
                        sanitize=False)
            return

        # Value-impact colours: negative is money given up, positive is money
        # gained.  This is the reverse of the cost convention the model uses.
        def money(value: float) -> str:
            if value < -0.00005:
                return 'color: #c62828;'
            if value > 0.00005:
                return 'color: #2e7d32;'
            return 'color: #495057;'

        rows_html = (
            '<div style="font-size: 12px; color: #6c757d; margin-bottom: 10px;">'
            'Negative is value given up, positive is value gained. The total is '
            'what the plan costs against a frictionless book dealt at spot mid.'
            '</div>'
        )

        running = 0.0
        for comp in components:
            value = comp['value']
            running += value
            if comp['reconciles']:
                workings = escape(comp['workings'])
                tone = '#6c757d'
            else:
                workings = escape(comp['workings'])
                tone = '#b26a00'
            rows_html += (
                f'<div class="cost-row" style="align-items: baseline;">'
                f'<div style="color: #495057; flex: 0 0 220px;">'
                f'{escape(comp["label"])}</div>'
                f'<div style="font-variant-numeric: tabular-nums; '
                f'flex: 0 0 120px; text-align: right; {money(value)}">'
                f'{value:,.4f}</div>'
                f'<div style="flex: 1 1 auto; padding-left: 20px; '
                f'font-size: 11.5px; font-family: ui-monospace, Menlo, '
                f'monospace; color: {tone}; word-break: break-word;">'
                f'{workings}</div>'
                f'</div>'
            )

        # costs['total'] comes straight from the model, so it is a cost and
        # positive; flip it to match the rows above.
        total = -costs['total'] if 'total' in costs else running

        # The lines above must reconcile to the total.  They have not always:
        # two components were missing and the table silently understated
        # every plan by the spread.  Say so rather than print a sum that
        # does not add up.
        residual = total - running
        if abs(residual) > 0.0005:
            rows_html += (
                f'<div class="cost-row" style="align-items: baseline;">'
                f'<div style="color: #b26a00; flex: 0 0 220px;">Unattributed</div>'
                f'<div style="font-variant-numeric: tabular-nums; '
                f'flex: 0 0 120px; text-align: right; color: #b26a00;">'
                f'{residual:,.4f}</div>'
                f'<div style="flex: 1 1 auto; padding-left: 20px; '
                f'font-size: 11.5px; color: #b26a00;">'
                f'breakdown does not reconcile to the total</div>'
                f'</div>'
            )

        total_color = '#c62828' if total < -0.005 else '#2e7d32' if total > 0.005 else '#1F3864'
        rows_html += (
            f'<div class="cost-row total" style="align-items: baseline;">'
            f'<div style="color: #1F3864; flex: 0 0 220px;">NET VALUE IMPACT</div>'
            f'<div style="font-variant-numeric: tabular-nums; '
            f'flex: 0 0 120px; text-align: right; color: {total_color};">'
            f'{total:,.4f}</div>'
            f'<div style="flex: 1 1 auto;"></div>'
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

            # The what-if section is revealed here, not after a solve: the
            # optimiser's plan is a baseline when there is one, never a
            # precondition for pricing a route by hand.
            self._populate_whatif_inputs()
            self._refresh_whatif()
            self.refs.whatif_section.classes(remove='hidden')

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

            # apply_edits_and_optimize has already dropped the priced
            # what-if plan (its ladder was just rewritten); redraw so the
            # stale comparison is gone from the page as well as from state.
            self._refresh_whatif()

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
    # What-if — price a hand-entered route
    # ================================================================

    def _update_whatif_btn_state(self) -> None:
        """Enable only what the current state can actually do."""
        loaded = self.state.cash_manager is not None and not self.state.is_loading
        for btn in (self.refs.whatif_add_btn, self.refs.whatif_evaluate_btn,
                    self.refs.whatif_donothing_btn):
            btn.enable() if loaded else btn.disable()

        has_plan = bool(getattr(self.state.optimal_result, 'trades', None))
        self.refs.whatif_copy_btn.enable() if (loaded and has_plan) \
            else self.refs.whatif_copy_btn.disable()

        has_entries = bool(self.state.manual_trades)
        self.refs.whatif_clear_btn.enable() if (loaded and has_entries) \
            else self.refs.whatif_clear_btn.disable()

    def _populate_whatif_inputs(self) -> None:
        """Fill the entry dropdowns from the loaded scenario.

        Every option comes off the config rather than a hard-coded list:
        the library spans five base currencies and the dealable set,
        the horizon and the tenor names all change with the scenario.
        """
        mgr = self.state.cash_manager
        if mgr is None:
            return

        cfg = mgr.config
        ccys = list(mgr.foreign_currencies)
        days = list(range(cfg.horizon_days))
        tenors = list(cfg.tenors.keys())

        self.refs.whatif_ccy_select.set_options(
            ccys, value=ccys[0] if ccys else None)
        self.refs.whatif_day_select.set_options(
            days, value=days[0] if days else None)
        self.refs.whatif_tenor_select.set_options(
            tenors, value=tenors[0] if tenors else None)
        self.refs.whatif_direction_select.set_value('BUY')
        self.refs.whatif_amount_input.set_value(None)

    def _render_entered_trades(self) -> None:
        """Rebuild the entered-trades table from state.

        Built from elements rather than raw HTML because each row carries
        a remove button, and a button injected as a string is inert text.
        """
        container = self.refs.whatif_trades_container
        container.clear()

        rows = self.state.get_manual_trades_table_data()
        if not rows:
            with container:
                ui.label(
                    'No trades entered — evaluating now would price the '
                    'do-nothing plan.'
                ).classes('text-base text-gray-500').style('padding: 6px 0;')
            return

        headers = ('CCY', 'Day', 'Tenor', 'Direction', 'Amount', 'Settles', '')
        with container:
            with ui.element('table').classes('cash-table entered-table'):
                with ui.element('thead'):
                    with ui.element('tr').classes('table-header-row'):
                        for h in headers:
                            align = 'left' if h in ('CCY', 'Direction') else 'center'
                            if h == 'Amount':
                                align = 'right'
                            with ui.element('th').classes('table-header-cell').style(
                                    f'text-align: {align};'):
                                ui.html(h, sanitize=False, tag='span')
                with ui.element('tbody'):
                    for r in rows:
                        dir_cls = ('table-data-cell positive'
                                   if r['direction'] == 'BUY'
                                   else 'table-data-cell negative')
                        with ui.element('tr').classes('table-data-row'):
                            self._td(r['ccy'], 'table-data-cell', 'left')
                            self._td(str(r['day']), 'table-data-cell', 'center')
                            self._td(r['tenor'], 'table-data-cell', 'center')
                            self._td(r['direction'], dir_cls, 'left')
                            self._td(f'{r["amount"]:,.2f}', 'table-data-cell', 'right')
                            self._td(f'D{r["settle_day"]}', 'table-data-cell', 'center')
                            with ui.element('td').classes('table-data-cell').style(
                                    'text-align: center;'):
                                (ui.button(icon='close',
                                           on_click=lambda _, i=r['index']:
                                           self._on_whatif_remove(i))
                                 .props('flat dense round size=sm color=grey-7'))

    @staticmethod
    def _td(text: str, css: str, align: str = 'right') -> None:
        """One table cell built as an element (so siblings can hold widgets)."""
        with ui.element('td').classes(css).style(f'text-align: {align};'):
            ui.html(escape(text), sanitize=False, tag='span')

    # ── Verdict ─────────────────────────────────────────────────

    #: (banner class, chip text) per verdict kind.  The colour is the
    #: message here: a saving that is not available must not be green.
    _VERDICT_STYLE = {
        'no_baseline': ('info', 'no baseline'),
        'dearer':      ('info', 'no rules broken'),
        'level':       ('info', 'no rules broken'),
        'void':        ('void', 'comparison void'),
        'illegal':     ('void', 'rule broken'),
        'suspect':     ('warn', 'check the baseline'),
    }

    def _verdict_html(self, v: Dict[str, Any]) -> str:
        """Compose the banner: headline, one line of why, then the rules.

        The framing is the whole point of the feature.  ``execute_trades``
        prices whatever it is handed and checks only currency, tenor,
        horizon and size, so a hand plan can use routes the optimiser was
        forbidden to consider and then appear to beat it.  Breaking a rule
        is *usually* how a manual plan wins, because the constraints exist
        precisely to forbid profitable speculation -- so a bare "you saved
        475.43" would be actively misleading in the case a user is most
        likely to go looking for.
        """
        kind = v['kind']
        css, chip = self._VERDICT_STYLE[kind]
        delta = v['delta']
        mine = v['manual_impact']

        if kind == 'no_baseline':
            head = f'Your route is worth {mine:,.2f} to the account.'
            if v['solver_status'] == 'Infeasible':
                sub = ('The optimiser found no feasible plan here, so there is '
                       'nothing to compare against — but this figure is real, '
                       'and the rules below are what it could not get around.')
            else:
                sub = ('No optimiser plan to compare against yet. Press '
                       '"Get Suggested Trades" for a baseline.')
        elif kind == 'level':
            head = 'Your route costs the same as the optimal plan.'
            sub = 'Different trades, identical money.'
        elif kind == 'dearer':
            head = f'Your route costs {abs(delta):,.2f} more than optimal.'
            sub = ('It is a legal plan, just a dearer one. The difference '
                   'column below says which cost line it went to.')
        elif kind == 'illegal':
            head = (f'Your route costs {abs(delta):,.2f} more than optimal — '
                    f'and breaks a rule the optimiser must obey.')
            sub = 'Dearer either way, so the rule is not what is costing you here.'
        elif kind == 'void':
            head = (f'Your route looks {delta:,.2f} better — but it breaks a '
                    f'rule the optimiser must obey.')
            sub = ('The arithmetic is right and the saving is not available. '
                   'You have not found a better route; you have found the rule.')
        else:  # suspect
            head = (f'Your route is {delta:,.2f} better with no rule broken.')
            sub = ('That should not be possible against a true optimum, so the '
                   'baseline is the thing to doubt, not your route. '
                   + ('The solver already flagged this plan as unproven. '
                      if v['optimality_unproven'] else '')
                   + f'Solved by {escape(v["solver_used"] or "unknown")} — CBC is '
                     'known to return sub-optimal plans on this model and label '
                     'them Optimal; installing highspy is the fix.')

        html = (
            f'<div class="verdict {css}">'
            f'<div class="verdict-headline">{escape(head)}'
            f'<span class="verdict-chip">{escape(chip)}</span></div>'
            f'<div class="verdict-sub">{escape(sub)}</div>'
        )
        for rule in v['violations']:
            html += f'<div class="verdict-rule">{escape(rule)}</div>'
        if v['check_error']:
            html += (
                f'<div class="verdict-rule">The rule check could not be run, so '
                f'this plan is unchecked rather than clean — '
                f'{escape(v["check_error"])}</div>'
            )
        return html + '</div>'

    # ── Comparison tables ───────────────────────────────────────

    @staticmethod
    def _clean(value: float, eps: float = 0.005) -> float:
        """Flatten a value that rounds to nothing onto positive zero.

        Floating-point subtraction of two equal balances lands on -0.0 as
        readily as 0.0, and "-0" in a difference column reads as a real
        movement too small to see rather than as no movement at all.
        """
        return 0.0 if abs(value) < eps else value

    @classmethod
    def _delta_cell(cls, value: float) -> str:
        value = cls._clean(value)
        css = 'table-data-cell'
        if value < 0:
            css += ' delta-neg'
        elif value > 0:
            css += ' delta-pos'
        sign = f'{value:+,.2f}' if value else f'{value:,.2f}'
        return f'<td class="{css}">{sign}</td>'

    def _cost_comparison_html(self, v: Dict[str, Any],
                              rows: List[Dict[str, Any]]) -> str:
        """The two breakdowns side by side, with a per-line difference.

        Both columns come from ``cost_workings`` over the same manager, so
        they emit the same keys in the same order and the rows genuinely
        line up.  That is what makes the difference column mean something
        line by line -- you can see *where* the money went, not only that
        it went.
        """
        baseline = v['delta'] is not None
        head = (
            '<tr class="table-header-row">'
            '<th class="table-header-cell" style="text-align:left">Cost line</th>'
            + ('<th class="table-header-cell">Optimal</th>' if baseline else '')
            + '<th class="table-header-cell">Yours</th>'
            + ('<th class="table-header-cell">Difference</th>' if baseline else '')
            + '</tr>'
        )

        body = ''
        run_opt = 0.0
        run_man = 0.0
        for r in rows:
            run_man += r['manual']
            body += f'<tr class="table-data-row"><td class="table-data-cell">{escape(r["label"])}</td>'
            if baseline:
                o = r['optimal'] or 0.0
                run_opt += o
                body += f'<td class="table-data-cell">{self._clean(o):,.2f}</td>'
            body += (f'<td class="table-data-cell">'
                     f'{self._clean(r["manual"]):,.2f}</td>')
            if baseline:
                body += self._delta_cell(r['delta'] or 0.0)
            body += '</tr>'

        # The lines must add up to the totals they sit under.  They have
        # not always: two components were once missing and the table
        # silently understated every plan by the spread.
        for label, running, total in (
                ('optimal', run_opt, v['optimal_impact']),
                ('yours', run_man, v['manual_impact'])):
            if total is None:
                continue
            if abs(total - running) > 0.0005:
                body += (
                    f'<tr class="table-data-row">'
                    f'<td class="table-data-cell" style="color:#b26a00">'
                    f'Unattributed ({escape(label)})</td>'
                    + ('<td class="table-data-cell"></td>' if baseline else '')
                    + f'<td class="table-data-cell" style="color:#b26a00">'
                      f'{total - running:,.2f}</td>'
                    + ('<td class="table-data-cell"></td>' if baseline else '')
                    + '</tr>'
                )

        total_row = (
            '<tr class="table-data-row" style="font-weight:700">'
            '<td class="table-data-cell" style="color:#1F3864">NET VALUE IMPACT</td>'
        )
        if baseline:
            total_row += (f'<td class="table-data-cell">'
                          f'{self._clean(v["optimal_impact"]):,.2f}</td>')
        total_row += (f'<td class="table-data-cell">'
                      f'{self._clean(v["manual_impact"]):,.2f}</td>')
        if baseline:
            total_row += self._delta_cell(v['delta'])
        total_row += '</tr>'

        return (
            '<div style="font-size:12px; color:#6c757d; margin-bottom:10px;">'
            'Negative is value given up, positive is value gained — the page\'s '
            'convention, which is the reverse of the model\'s. A positive '
            'difference means your route keeps more money.'
            '</div>'
            f'<table class="cash-table compare-table"><thead>{head}</thead>'
            f'<tbody>{body}{total_row}</tbody></table>'
        )

    @classmethod
    def _ladder_comparison_html(cls, data: Dict[str, Any]) -> str:
        """Optimal and manual after-trade balances, and the gap between them.

        Three rows per currency rather than two grids: past two currencies
        the difference row is the only one anyone reads.
        """
        days = data.get('days', [])
        rows = data.get('rows', [])
        if not rows:
            return ''

        head = (
            '<tr class="table-header-row">'
            '<th class="table-header-cell" style="text-align:left">CCY</th>'
            '<th class="table-header-cell" style="text-align:left">Plan</th>'
            + ''.join(f'<th class="table-header-cell">D{d}</th>' for d in days)
            + '</tr>'
        )

        body = ''
        for r in rows:
            for n, (label, series) in enumerate(
                    (('Optimal', r['optimal']), ('Yours', r['manual']),
                     ('Difference', r['delta']))):
                is_delta = label == 'Difference'
                tr_cls = 'table-data-row row-delta' if is_delta else 'table-data-row'
                body += f'<tr class="{tr_cls}">'
                body += (f'<td class="table-data-cell">{escape(r["ccy"])}</td>'
                         if n == 0 else
                         '<td class="table-data-cell"></td>')
                body += f'<td class="table-data-cell plan-cell">{label}</td>'
                for val in series:
                    val = cls._clean(val, 0.5)   # rows are shown to the unit
                    css = 'table-data-cell'
                    if is_delta and val < 0:
                        css += ' delta-neg'
                    elif is_delta and val > 0:
                        css += ' delta-pos'
                    elif not is_delta and val < 0:
                        css += ' negative'
                    text = f'{val:+,.0f}' if (is_delta and val) else f'{val:,.0f}'
                    body += f'<td class="{css}">{text}</td>'
                body += '</tr>'

        return (
            f'<table class="cash-table compare-table"><thead>{head}</thead>'
            f'<tbody>{body}</tbody></table>'
        )

    def _render_whatif_results(self) -> None:
        """Render the verdict, the cost comparison and the two ladders.

        In descending order of what anyone actually looks at.  Nothing is
        rendered at all until a route has been priced.
        """
        container = self.refs.whatif_results_container
        container.clear()

        if self.state.manual_result is None:
            return

        verdict = self.state.get_manual_verdict()
        rows = self.state.get_manual_comparison_rows()

        with container:
            ui.html(self._verdict_html(verdict), sanitize=False)

            with ui.card().classes('cost-card'):
                ui.label('Cost breakdown').classes('card-section-title')
                ui.html(self._cost_comparison_html(verdict, rows), sanitize=False)

                # The derivations are kept but folded away: the headline is
                # the delta, and a wall of arithmetic above it buries that.
                with ui.expansion('Show the arithmetic behind each line').classes(
                        'w-full').style('margin-top: 14px;'):
                    for r in rows:
                        if not r['manual_workings'] and not r['optimal_workings']:
                            continue
                        ui.label(r['label']).style(
                            'font-size: 13px; font-weight: 700; color:#1c2b36; '
                            'margin-top: 8px;')
                        stale = '' if r['reconciles'] else ' stale'
                        if r['optimal_workings']:
                            ui.html(
                                f'<div class="workings-line{stale}">optimal &nbsp;'
                                f'{escape(r["optimal_workings"])}</div>',
                                sanitize=False)
                        if r['manual_workings']:
                            ui.html(
                                f'<div class="workings-line{stale}">yours &nbsp;&nbsp;&nbsp;'
                                f'{escape(r["manual_workings"])}</div>',
                                sanitize=False)
                        if not r['reconciles']:
                            ui.html(
                                '<div class="workings-line stale">this derivation '
                                'disagrees with the cost model — believe the '
                                'figure, not the formula</div>', sanitize=False)

            ladder = self.state.get_manual_ladder_comparison()
            if ladder['rows']:
                ui.label('Where the two ladders diverge').classes(
                    'card-section-title').style('margin-top: 22px;')
                ui.html(self._ladder_comparison_html(ladder), sanitize=False)
            else:
                ui.label('Cash projections — your route').classes(
                    'card-section-title').style('margin-top: 22px;')
                self._render_pivoted_table(
                    ui.element('div').classes('w-full overflow-x-auto'),
                    self.state.get_after_trade_table_data(self.state.manual_result),
                    extra_css_class='after-trade-table',
                )

    def _refresh_whatif(self) -> None:
        """Redraw the whole what-if section from state."""
        self._render_entered_trades()
        self._render_whatif_results()
        self._update_whatif_btn_state()

    def _invalidate_whatif(self) -> None:
        """Drop the priced plan because the ladder underneath it moved.

        The entered trades survive -- they are still legal entries against
        this scenario, and re-pricing them is one click.  What must not
        survive is the *price*, because a comparison against a book that
        has since changed is the easiest way for this feature to lie.
        """
        had_result = self.state.manual_result is not None
        self.state.clear_manual()
        self._refresh_whatif()
        return had_result

    # ── Handlers ────────────────────────────────────────────────

    def _on_whatif_add(self, *_) -> None:
        """Validate and append one trade to the route."""
        ccy = self.refs.whatif_ccy_select.value
        day = self.refs.whatif_day_select.value
        tenor = self.refs.whatif_tenor_select.value
        direction = self.refs.whatif_direction_select.value
        amount = self.refs.whatif_amount_input.value

        if not ccy or day is None or not tenor or not direction:
            ui.notify('Choose a currency, day, tenor and direction.',
                      type='warning', position='top')
            return
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            ui.notify('Enter an amount.', type='warning', position='top')
            return

        try:
            self.state.add_manual_trade(ccy, day, tenor, direction, amount)
        except Exception as exc:
            # The manager's messages name the offending row and say what is
            # wrong with it, which is more use than a generic rejection.
            ui.notify(str(exc), type='negative', position='top', multi_line=True,
                      classes='whitespace-pre-line')
            return

        self.refs.whatif_amount_input.set_value(None)
        self._refresh_whatif()

    def _on_whatif_remove(self, index: int) -> None:
        self.state.remove_manual_trade(index)
        self._refresh_whatif()

    def _on_whatif_clear(self, *_) -> None:
        self.state.clear_manual(keep_trades=False)
        self._refresh_whatif()
        ui.notify('What-if route cleared.', type='info', position='top')

    def _on_whatif_copy_optimal(self, *_) -> None:
        """Seed the route from the optimiser's plan, to vary one thing."""
        n = self.state.copy_optimal_to_manual()
        self._refresh_whatif()
        if n:
            ui.notify(f'Copied {n} trade(s) from the optimal plan — change one '
                      f'and evaluate.', type='positive', position='top')
        else:
            ui.notify('The optimal plan has no trades to copy.',
                      type='warning', position='top')

    async def _on_whatif_do_nothing(self, *_) -> None:
        """Price the empty plan.

        This empties the route first, on purpose: what is priced is always
        what is in the list, and a cost sitting above trades it was not
        computed from is exactly the kind of quiet lie this feature exists
        to avoid.
        """
        if self.state.manual_trades:
            ui.notify('Trade list cleared — pricing the do-nothing plan.',
                      type='info', position='top')
        self.state.clear_manual(keep_trades=False)
        self._render_entered_trades()
        await self._evaluate_whatif()

    async def _on_whatif_evaluate(self, *_) -> None:
        await self._evaluate_whatif()

    async def _evaluate_whatif(self) -> None:
        """Price the route and render the comparison.

        No solver runs and the ``CashManager`` is not rebuilt, so the
        optimiser's plan stays valid beside this one.  Rebuilding the
        manager between the two evaluations is precisely what would break
        the guarantee that a cost difference is a plan difference.
        """
        if self.state.cash_manager is None:
            return

        self._hide_error()
        self.state.is_loading = True
        self._update_whatif_btn_state()

        try:
            await run.io_bound(self.state.evaluate_manual)
            self._render_whatif_results()

            v = self.state.get_manual_verdict()
            n = len(self.state.manual_violations)
            ui.notify(
                f'Route priced — value impact {v["manual_impact"]:,.2f}'
                + (f', {n} rule(s) broken' if n else ''),
                type='warning' if n else 'positive', position='top',
            )
        except Exception as exc:
            log.exception('What-if evaluation failed')
            self.state.clear_manual()
            self._render_whatif_results()
            self._show_error(f'Could not price that route: {exc}')
            ui.notify(str(exc), type='negative', position='top', multi_line=True,
                      classes='whitespace-pre-line')
        finally:
            self.state.is_loading = False
            self._update_whatif_btn_state()
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
        # ...and neither is a what-if plan priced against the old ladder.
        if self._invalidate_whatif():
            ui.notify('The what-if price was cleared — the ladder changed '
                      'under it. Evaluate again.', type='info', position='top')

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
        # The rule set just changed, so the violation list attached to any
        # priced what-if plan is describing a policy that is no longer in
        # force.  Drop the price; keep the route.
        self._invalidate_whatif()

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

        # What-if section
        self.refs.whatif_add_btn.on_click(self._on_whatif_add)
        self.refs.whatif_amount_input.on('keydown.enter', self._on_whatif_add)
        self.refs.whatif_evaluate_btn.on_click(self._on_whatif_evaluate)
        self.refs.whatif_copy_btn.on_click(self._on_whatif_copy_optimal)
        self.refs.whatif_donothing_btn.on_click(self._on_whatif_do_nothing)
        self.refs.whatif_clear_btn.on_click(self._on_whatif_clear)

        # Set initial button states
        self._update_load_btn_state()
        self._update_generate_btn_state()
        self._update_whatif_btn_state()

