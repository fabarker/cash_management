from __future__ import annotations

from dataclasses import dataclass

from nicegui import ui


@dataclass
class CashManagementRefs:
    """References to all interactive UI elements the controller needs.

    This is the typed contract between the View and the Controller.
    Every interactive element created in ``build_view()`` that the
    controller needs to read, write, enable, disable, show, hide, or
    populate is captured here.
    """

    # ── Search bar ─────────────────────────────────────────────
    group_number_input: ui.input
    scenario_caption: ui.html
    load_btn: ui.button

    # ── Loading / progress dialog ──────────────────────────────
    progress_dialog: ui.dialog
    progress_title: ui.label

    # ── Info panel (summary section above the cash ledger) ─────
    info_panel: ui.card
    base_ccy_label: ui.label
    funding_id_label: ui.label
    horizon_label: ui.label
    currencies_label: ui.label
    fx_quotes_container: ui.row
    commission_container: ui.column

    # ── Constraint toggles section ─────────────────────────────
    constraints_section: ui.element
    holding_ceiling_switch: ui.switch

    # ── Editable cash projections ──────────────────────────────
    ledger_section: ui.element
    ledger_table_container: ui.element    # holds the rendered HTML table
    edit_dialog: ui.dialog                # dialog for editing cell values
    edit_dialog_title: ui.label
    edit_dialog_input: ui.number
    edit_dialog_save_btn: ui.button

    # ── Generate Trades button ─────────────────────────────────
    generate_trades_btn: ui.button

    # ── Add Cashflow button + dialog ───────────────────────────
    add_cashflow_btn: ui.button
    add_cashflow_dialog: ui.dialog
    add_cashflow_ccy_select: ui.select
    add_cashflow_amount_input: ui.number
    add_cashflow_day_select: ui.select
    add_cashflow_save_btn: ui.button

    # ── Results section (appears after optimization) ───────────
    results_section: ui.element

    # Suggested trades table
    trades_table_wrapper: ui.element
    trades_table_rows: ui.element

    # After-trade cash table
    after_trade_section: ui.element
    after_trade_table_wrapper: ui.element
    after_trade_table_rows: ui.element

    # Cost breakdown
    cost_breakdown_container: ui.column

    # ── What-if section (own section; visible once a scenario loads,
    #    with or without an optimizer plan to compare against) ──
    whatif_section: ui.element
    whatif_ccy_select: ui.select
    whatif_day_select: ui.select
    whatif_tenor_select: ui.select
    whatif_direction_select: ui.select
    whatif_amount_input: ui.number
    whatif_add_btn: ui.button
    whatif_trades_container: ui.element
    whatif_evaluate_btn: ui.button
    whatif_copy_btn: ui.button
    whatif_donothing_btn: ui.button
    whatif_clear_btn: ui.button
    whatif_results_container: ui.column

    # ── Error display ──────────────────────────────────────────
    error_label: ui.label
