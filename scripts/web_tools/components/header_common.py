
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from nicegui import ui, app

_ASSETS_DIR = Path(__file__).resolve().parent.parent / 'assets'


#: Drawn only when the real wordmark is absent, so a missing brand asset
#: degrades to a plain caption instead of a 500 on every page.
_FALLBACK_WORDMARK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 132 24" height="24" '
    'role="img" aria-label="PMG Web Tools">'
    '<text x="0" y="17" fill="currentColor" font-size="15" letter-spacing="1.5" '
    'font-family="Helvetica, Arial, sans-serif">PMG</text></svg>'
)


def load_wordmark_svg() -> str:
    """Return the header wordmark, or a plain caption if it is not present.

    The brand assets are not in this repository.  Missing ones must not take
    the page down with them: the wordmark is decoration, and a 500 on every
    route because a logo is absent is a worse failure than an unbranded
    header.  Drop the real file in at ``web_tools/assets/`` and it is picked
    up with no code change.
    """
    path = _ASSETS_DIR / 'goldman_wordmark.svg'
    try:
        return path.read_text(encoding='utf-8')
    except OSError:
        return _FALLBACK_WORDMARK

def build_tools_menu(results_label: ui.label) -> None:
    with ui.menu().classes('font-gs-regular'):
        ui.menu_item('Bulk Note Tool', on_click=lambda: ui.navigate.to('/bulk-notes-tools'))
        ui.menu_item('Product Searcher', on_click=lambda: ui.navigate.to('/product-search'))
        ui.menu_item('Model Trades', on_click=lambda: ui.navigate.to('/portfolio-trading'))
        ui.menu_item('Trade Check', on_click=lambda: ui.navigate.to('/trade-check'))
        ui.menu_item('Cash Management', on_click=lambda: ui.navigate.to('/cash-management'))
        ui.separator()

def build_clock() -> None:
    time_label = ui.label(datetime.now().strftime('%Y-%m-%d %H:%M:%S')).classes('header-clock text-white').style('font-size: large;')
    ui.timer(1.0, lambda: time_label.set_text(datetime.now().strftime('%Y-%m-%d %H:%M:%S')))


def build_user_name(display_name: str = '') -> None:
    """Display the authenticated user's name in the header."""
    if display_name:
        ui.icon('person', size='sm').classes('text-white')
        ui.label(display_name).classes('text-white').style('font-size: large;')