from __future__ import annotations

from nicegui import ui

from .types import CashManagementRefs


def build_view() -> CashManagementRefs:
    """Construct the Cash Management UI and return refs to all interactive elements.

    This is a pure factory function — it builds the element tree,
    captures handles to every interactive element, and returns them
    as a :class:`CashManagementRefs` dataclass.  No event handlers
    are attached here; that is the controller's job.
    """

    # ── Page-specific CSS ──────────────────────────────────────
    ui.add_head_html("""
        <style>

        /* Page container — centred column */
        .cash-mgmt-container {
            padding: 35px 40px 40px 40px;
            width: 100%;
            max-width: 1200px;
            margin: 0 auto;
        }

        /* ─── Single master card ─── */
        .master-card {
            width: 100% !important;
            padding: 0 !important;
            margin-bottom: 0px;
            background-color: #ffffff !important;
            border: 1px solid #d0d4d8 !important;
            box-shadow: 0 1px 4px rgba(0,0,0,0.06) !important;
            border-radius: 6px;
            overflow: hidden;
        }

        .info-header-bar {
            width: 100%;
            padding: 28px 32px;
            background: linear-gradient(135deg, #1c2b36 0%, #263a47 100%);
        }

        .info-body {
            width: 100%;
            padding: 28px 32px;
            background-color: #fafbfc;
        }

        .card-section {
            width: 100%;
            padding: 24px 32px;
        }

        .stat-label {
            font-size: 11px;
            color: rgba(255,255,255,0.55);
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 1.2px;
        }

        .stat-value {
            font-size: 21px;
            color: #ffffff;
            font-weight: 600;
            margin-top: 4px;
            letter-spacing: 0.2px;
        }

        .info-section-title {
            font-size: 14px;
            font-weight: 700;
            color: #6c757d;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
        }

        .info-label { font-size: 16px; color: #495057; }


        /* ─── Info list items (FX quotes, rates — same style as commission tiers) ─── */
        .rates-table {
            border-collapse: collapse;
            font-family: 'Goldman Sans', Arial, sans-serif;
        }

        .rates-th {
            font-size: 14px;
            font-weight: 700;
            color: #6c757d;
            text-transform: uppercase;
            letter-spacing: 1px;
            padding: 0 24px 10px 0;
            text-align: left;
            white-space: nowrap;
        }

        .rates-th-label {
            /* first column header — empty, just needs spacing */
            min-width: 44px;
        }

        .rates-td {
            font-size: 16px;
            font-weight: 600;
            color: #1c2b36;
            font-variant-numeric: tabular-nums;
            padding: 4px 24px 4px 0;
            text-align: left;
            white-space: nowrap;
        }

        .rates-label {
            font-weight: 700;
            color: #1c2b36;
            min-width: 44px;
        }

        .rates-muted {
            color: #adb5bd;
            font-weight: 400;
        }

        .info-list-item {
            display: flex;
            justify-content: space-between;
            padding: 4px 0;
            font-size: 16px;
            gap: 20px;
        }

        .info-list-label {
            font-weight: 700;
            color: #1c2b36;
            min-width: 44px;
            font-size: 16px;
        }

        .info-list-value {
            font-variant-numeric: tabular-nums;
            font-weight: 600;
            color: #1c2b36;
            font-size: 16px;
        }

        .info-list-value.muted {
            color: #adb5bd;
            font-weight: 400;
        }

        /* ─── Commission info items ─── */
        .commission-item {
            display: flex;
            justify-content: space-between;
            padding: 4px 0;
            font-size: 16px;
            gap: 20px;
        }

        /* ─── Section title inside card ─── */
        .card-section-title {
            font-size: 17px;
            font-weight: 700;
            color: #1c2b36;
            margin-bottom: 14px;
            margin-top: 0;
            letter-spacing: 0.2px;
        }

        /* ─── Cash balance tables ─── */
        .cash-table {
            width: 30%;
            border-collapse: separate;
            border-spacing: 0;
            font-family: 'Goldman Sans', Arial, sans-serif;
            table-layout: auto;
            border: 1px solid #c8ced3;
            border-radius: 6px;
            overflow: hidden;
        }

        .cash-table .table-header-row {
            background: linear-gradient(180deg, #3a4a56 0%, #2d3c46 100%);
        }

        .cash-table .table-header-cell {
            padding: 10px 18px;
            text-align: right;
            font-weight: 600;
            color: rgba(255,255,255,0.92);
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.6px;
            white-space: nowrap;
            border-right: 1px solid rgba(255,255,255,0.10);
            border-bottom: 2px solid #1c2b36;
        }

        .cash-table .table-header-cell:first-child { text-align: left; }
        .cash-table .table-header-cell:last-child  { border-right: none; }

        .cash-table .table-data-row {
            background-color: #ffffff;
            transition: background-color 0.12s ease;
        }

        .cash-table .table-data-row:nth-child(even) {
            background-color: #f8f9fa;
        }

        .cash-table .table-data-row:hover {
            background-color: #e8edf2 !important;
        }

        .cash-table .table-data-cell {
            padding: 9px 18px;
            color: #212529;
            font-size: 14px;
            font-weight: 400;
            white-space: nowrap;
            font-variant-numeric: tabular-nums;
            text-align: right;
            border-right: 1px solid #eef0f2;
            border-bottom: 1px solid #e4e7ea;
        }

        .cash-table .table-data-cell:first-child {
            text-align: left;
            font-weight: 700;
            color: #1c2b36;
            background-color: rgba(245,247,248,0.6);
            border-right: 1px solid #dde0e3;
        }

        .cash-table .table-data-cell:last-child  { border-right: none; }

        .cash-table .table-data-row:last-child .table-data-cell {
            border-bottom: none;
        }

        .cash-table .negative { color: #c62828; font-weight: 600; }
        .cash-table .positive { color: #1b5e20; font-weight: 500; }

        /* Trades table (green accent) */
        .trades-table .table-header-row {
            background: linear-gradient(180deg, #2e7d32 0%, #256428 100%);
        }
        .trades-table .table-header-cell {
            border-right-color: rgba(255,255,255,0.12);
            border-bottom-color: #1b5e20;
        }

        /* After-trade table (slate accent) */
        .after-trade-table .table-header-row {
            background: linear-gradient(180deg, #455a64 0%, #37474f 100%);
        }
        .after-trade-table .table-header-cell {
            border-right-color: rgba(255,255,255,0.12);
            border-bottom-color: #263238;
        }

        /* ─── Cost breakdown ─── */
        .cost-card {
            width: 100% !important;
            padding: 24px !important;
            background-color: #ffffff !important;
            border: 1px solid #d0d4d8 !important;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
            border-radius: 6px;
        }

        .cost-row {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            padding: 6px 0;
            font-size: 15px;
            gap: 40px;
        }

        .cost-row.total {
            border-top: 2px solid #1c2b36;
            margin-top: 10px;
            padding-top: 12px;
            font-weight: 700;
        }

        /* Generate trades button area */
        .generate-btn-area {
            margin-top: 20px;
            margin-bottom: 4px;
        }

        /* ─── Editable projections ─── */
        .projections-clickable td {
            cursor: pointer;
        }
        .projections-clickable td:hover {
            background-color: #dde4ea !important;
        }

        </style>
    """, shared=False)

    # ── Page layout ────────────────────────────────────────────
    with ui.column().classes('cash-mgmt-container'):

        # Page title
        ui.label('Cash Management').classes(
            'text-4xl sm:text-3xl md:text-4xl text-primary font-gs-condensed'
        ).style(
            "font-family: gs-sans-condensed, 'Helvetica Neue', Arial, sans-serif; "
            "font-weight: 450 !important;"
        )

        # ── Search bar ─────────────────────────────────────────
        with ui.row().classes('items-center gap-4').style('margin-top: 16px; margin-bottom: 20px;'):
            group_number_input = (
                ui.input(label='Group Number', placeholder='e.g. C88A37694')
                .props('outlined dense clearable')
                .style('width: 280px;')
            )
            load_btn = (
                ui.button('Load Cash Projections', icon='search')
                .props('unelevated color=primary')
                .style('height: 40px;')
            )
            load_btn.disable()

        # ── Error label (hidden by default) ────────────────────
        error_label = (
            ui.label('')
            .classes('text-negative text-base w-full hidden')
            .style('margin-bottom: 8px;')
        )

        # ── Progress / spinner dialog ──────────────────────────
        with ui.dialog().props('persistent') as progress_dialog:
            with ui.card().classes('items-center p-6'):
                progress_title = ui.label('Loading cash data...').classes('text-lg mb-4')
                ui.spinner(size='lg')

        # ── Edit cell dialog (pre-built, reused for every cell) ──
        with ui.dialog() as edit_dialog:
            with ui.card().style('min-width: 300px; padding: 24px;'):
                edit_dialog_title = ui.label('Edit value').classes('text-lg font-bold mb-4')
                edit_dialog_input = ui.number(
                    label='Amount', format='%.2f',
                ).props('outlined dense autofocus').style('width: 100%;')
                with ui.row().classes('w-full justify-end gap-2 mt-4'):
                    ui.button('Cancel', on_click=edit_dialog.close).props(
                        'flat color=grey'
                    )
                    edit_dialog_save_btn = ui.button('Save').props(
                        'unelevated color=primary'
                    )

        # ── Add-cashflow dialog (pre-built, reused) ──
        with ui.dialog() as add_cashflow_dialog:
            with ui.card().style('min-width: 340px; padding: 24px;'):
                ui.label('Add Cashflow').classes('text-lg font-bold mb-4')
                add_cashflow_ccy_select = ui.select(
                    options=[], label='Currency',
                ).props('outlined dense').style('width: 100%;')
                add_cashflow_amount_input = ui.number(
                    label='Amount (positive = inflow, negative = outflow)',
                    format='%.2f',
                ).props('outlined dense').style('width: 100%; margin-top: 12px;')
                add_cashflow_day_select = ui.select(
                    options=[], label='Day',
                ).props('outlined dense').style('width: 100%; margin-top: 12px;')
                with ui.row().classes('w-full justify-end gap-2 mt-4'):
                    ui.button('Cancel', on_click=add_cashflow_dialog.close).props(
                        'flat color=grey'
                    )
                    add_cashflow_save_btn = ui.button('Add').props(
                        'unelevated color=primary'
                    )

        # ══════════════════════════════════════════════════════
        # SINGLE MASTER CARD — everything inside one card
        # ══════════════════════════════════════════════════════
        with ui.card().classes('master-card hidden') as info_panel:

            # ── Dark header bar with key stats ─────────────────
            with ui.element('div').classes('info-header-bar'):
                with ui.row().classes('w-full items-start gap-x-24 gap-y-4 flex-wrap'):
                    with ui.column().classes('gap-1'):
                        ui.label('Base Currency').classes('stat-label')
                        base_ccy_label = ui.label('—').classes('stat-value')

                    with ui.column().classes('gap-1'):
                        ui.label('Group Number').classes('stat-label')
                        funding_id_label = ui.label('—').classes('stat-value')

                    with ui.column().classes('gap-1'):
                        ui.label('Horizon').classes('stat-label')
                        horizon_label = ui.label('—').classes('stat-value')

                    with ui.column().classes('gap-1'):
                        ui.label('Currencies').classes('stat-label')
                        currencies_label = ui.label('—').classes('stat-value')

            # ── FX quotes, rates, commission ───────────────────
            with ui.element('div').classes('info-body'):
                with ui.row().classes('w-full gap-20 flex-wrap items-start'):

                    with ui.row().classes('gap-10 flex-wrap items-start') as fx_quotes_container:
                        pass  # Populated by controller

                    with ui.column().classes('gap-2') as commission_container:
                        ui.label('COMMISSION TIERS').classes('info-section-title')

            # ── Constraint toggles (hidden until load) ──────────
            with ui.element('div').classes('card-section hidden').style(
                'padding-top: 0; padding-bottom: 0;'
            ) as constraints_section:
                ui.separator().style('margin: 0 0 20px 0;')
                ui.label('CONSTRAINTS').classes('info-section-title')
                with ui.row().classes('items-center gap-3'):
                    t0_debit_only_switch = ui.switch('T0 trade debits only', value=True)
                    ui.label(
                        'When ON, T+0 trades are only allowed to cover a same-day debit.'
                    ).style('font-size: 13px; color: #6c757d;')

            # ── Cash Projections — Pre-Trade (hidden until load) ──
            with ui.element('div').classes('card-section hidden') as ledger_section:
                ui.separator().style('margin: 0 0 24px 0;')
                ui.label('Cash Projections — Pre-Trade (Assumes account is ready to trade)').classes('card-section-title')
                ui.label(
                    'Click any cell to edit. Changes will be used by the optimizer.'
                ).style('font-size: 13px; color: #6c757d; margin-bottom: 12px;')

                # HTML container — the controller renders the table here
                ledger_table_container = ui.element('div').classes('w-full overflow-x-auto')

                # Generate Trades + Add Cashflow buttons
                with ui.row().classes('generate-btn-area items-center gap-3'):
                    generate_trades_btn = (
                        ui.button('Get Suggested Trades', icon='auto_fix_high')
                        .props('unelevated color=primary')
                        .style('height: 42px;')
                    )
                    add_cashflow_btn = (
                        ui.button('Add Cashflow', icon='add')
                        .props('outline color=primary')
                        .style('height: 42px;')
                    )

            # ── Results (hidden until optimization runs) ───────
            with ui.element('div').classes('card-section hidden') as results_section:

                # Recommended trades
                ui.separator().style('margin: 0 0 24px 0;')
                ui.label('Recommended Trades').classes('card-section-title')
                with ui.element('div').classes('w-full overflow-x-auto') as trades_table_wrapper:
                    trades_table_rows = ui.element('div')

                # Cash Projections — After Trades
                ui.separator().style('margin: 24px 0;')
                with ui.element('div').classes('w-full') as after_trade_section:
                    ui.label('Cash Projections — After Trades').classes('card-section-title')
                    with ui.element('div').classes('w-full overflow-x-auto') as after_trade_table_wrapper:
                        after_trade_table_rows = ui.element('div')

                # Cost breakdown
                ui.separator().style('margin: 24px 0;')
                ui.label('Cost Breakdown').classes('card-section-title')
                with ui.column().classes('w-full') as cost_breakdown_container:
                    pass  # Populated by controller

    return CashManagementRefs(
        group_number_input=group_number_input,
        load_btn=load_btn,
        progress_dialog=progress_dialog,
        progress_title=progress_title,
        info_panel=info_panel,
        base_ccy_label=base_ccy_label,
        funding_id_label=funding_id_label,
        horizon_label=horizon_label,
        currencies_label=currencies_label,
        fx_quotes_container=fx_quotes_container,
        commission_container=commission_container,
        constraints_section=constraints_section,
        t0_debit_only_switch=t0_debit_only_switch,
        ledger_section=ledger_section,
        ledger_table_container=ledger_table_container,
        edit_dialog=edit_dialog,
        edit_dialog_title=edit_dialog_title,
        edit_dialog_input=edit_dialog_input,
        edit_dialog_save_btn=edit_dialog_save_btn,
        generate_trades_btn=generate_trades_btn,
        add_cashflow_btn=add_cashflow_btn,
        add_cashflow_dialog=add_cashflow_dialog,
        add_cashflow_ccy_select=add_cashflow_ccy_select,
        add_cashflow_amount_input=add_cashflow_amount_input,
        add_cashflow_day_select=add_cashflow_day_select,
        add_cashflow_save_btn=add_cashflow_save_btn,
        results_section=results_section,
        trades_table_wrapper=trades_table_wrapper,
        trades_table_rows=trades_table_rows,
        after_trade_section=after_trade_section,
        after_trade_table_wrapper=after_trade_table_wrapper,
        after_trade_table_rows=after_trade_table_rows,
        cost_breakdown_container=cost_breakdown_container,
        error_label=error_label,
    )
