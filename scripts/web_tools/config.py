"""Shared, app-wide configuration.

Keep only cross-page constants here (e.g. theme defaults). Page specific
constants now live under each page's package"""

from dataclasses import dataclass

PRIMARY_COLOR = '#538ce0'
CARD_WIDTH = '600px'


@dataclass(frozen=True)
class Theme:
    primary: str = PRIMARY_COLOR
    card_width: str = CARD_WIDTH
    background: str = "rgb(247, 247, 250)"
    warn_red: str = "#d32f2f"
