
from __future__ import annotations

from nicegui import ui

def build_title(
        card_width: str = '600px',
        title: str = 'Bulk Notes Submitter',
        subtitle: str = 'Apply the same note to multiple C8 accounts in one go',
) -> None:
    with ui.row().classes('full-width justify-center'):
        with ui.column().style(f'width: {card_width};'):
            ui.label(title) \
                .classes('font-goldman-condensed text-lg font-bold') \
                .style('margin-top: 24px; font-size: 45px;')

            ui.label(subtitle) \
                .classes('text-lg font-goldman-condensed text-default-heading text-neutral-bol') \
                .style('margin-top: 5px; margin-bottom: 5px; font-size: 25px;')