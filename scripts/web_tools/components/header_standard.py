from __future__ import annotations

from nicegui import ui, app

from .header_common import build_clock, build_user_name, load_wordmark_svg, _logout


def build_header(height=72, text='PMG Web Tools') -> None:
    wordmark_svg = load_wordmark_svg()
    display_name = app.storage.user.get('display_name', '')

    with ui.header(fixed=True).style(
            f'height: {height}px; '
            f'height: {height}px; '
            'padding: 0 40px 0 16px; '
            'align-items: center; '
            'background-color: #000000;'
    ):
        with ui.row().classes('w-full items-center flex-nowrap no-wrap gap-4'):
            result = ui.label().classes('text-white')

            with ui.button(icon='menu').props('flat').style(
                    'background-color: #000000; color: #FFFFFF!important'
            ).classes('blink-burger'):
                with ui.menu() as menu:
                    ui.menu_item('Bulk Note Tool', on_click=lambda: ui.navigate.to('/bulk-notes-tools'))
                    ui.menu_item('Product Searcher', on_click=lambda: ui.navigate.to('/product-search'))
                    ui.menu_item('Model Trades', on_click=lambda: ui.navigate.to('/portfolio-trading'))
                    ui.menu_item('Trade Check', on_click=lambda: ui.navigate.to('/trade-check'))
                    ui.menu_item('Cash Management', on_click=lambda: ui.navigate.to('/cash-management'))
                    ui.separator()
                    ui.menu_item('Logout', on_click=lambda: _logout())

            with ui.row().classes('items-center gap-4'):
                with ui.link(target='/').classes('cursor-pointer no-underline'):
                    ui.html(wordmark_svg, sanitize=False).classes('header-logo')

            with ui.row().classes('gap-4'):
                ui.label('PMG Web Tools').style('font-size: 24px!important;')

            ui.space()

            with ui.row().classes("ml-auto items-center gap-4"):
                build_user_name(display_name)
                ui.separator().props('vertical').classes('bg-white').style('height: 20px; opacity: 0.8;')
                build_clock()
