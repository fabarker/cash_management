
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from nicegui import ui, app

from pmg_core.apps.pmg_web_tools.session_context import remove_session

_ASSETS_DIR = Path(__file__).resolve().parent.parent / 'assets'


def load_wordmark_svg() -> str:
    return (_ASSETS_DIR / 'goldman_wordmark.svg').read_text(encoding='utf-8')

def _logout() -> None:
    """Clear auth state, remove live session objects, and redirect to login."""
    remove_session()
    app.storage.user['authenticated'] = False
    app.storage.user.pop('username', None)
    app.storage.user.pop('display_name', None)
    ui.navigate.to('/login')

def build_tools_menu(results_label: ui.label) -> None:
    with ui.menu().classes('font-gs-regular'):
        ui.menu_item('Bulk Note Tool', on_click=lambda: ui.navigate.to('/bulk-notes-tools'))
        ui.menu_item('Product Searcher', on_click=lambda: ui.navigate.to('/product-search'))
        ui.menu_item('Model Trades', on_click=lambda: ui.navigate.to('/portfolio-trading'))
        ui.menu_item('Trade Check', on_click=lambda: ui.navigate.to('/trade-check'))
        ui.menu_item('Cash Management', on_click=lambda: ui.navigate.to('/cash-management'))
        ui.separator()
        ui.menu_item('Logout', on_click=lambda: _logout())

def build_clock() -> None:
    time_label = ui.label(datetime.now().strftime('%Y-%m-%d %H:%M:%S')).classes('header-clock text-white').style('font-size: large;')
    ui.timer(1.0, lambda: time_label.set_text(datetime.now().strftime('%Y-%m-%d %H:%M:%S')))


def build_user_name(display_name: str = '') -> None:
    """Display the authenticated user's name in the header."""
    if display_name:
        ui.icon('person', size='sm').classes('text-white')
        ui.label(display_name).classes('text-white').style('font-size: large;')