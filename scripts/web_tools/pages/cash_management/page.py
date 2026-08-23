
from __future__ import annotations

from nicegui import ui

from pmg_core.apps.pmg_web_tools.auth import require_auth
from pmg_core.apps.pmg_web_tools.config import Theme
from pmg_core.apps.pmg_web_tools.styles import apply_styles
from pmg_core.apps.pmg_web_tools.components.header_standard import build_header

from .controller import CashManagementController
from .state import CashManagementState
from .view import build_view


@ui.page('/cash-management')
def cash_management_page() -> None:
    """Cash Management page — route ``/cash-management``.

    Orchestration follows the 8-line MVCS formula:
    auth → styles → header → view → state → controller → wire.
    """

    # TODO: Re-enable auth before going to production
    # if not require_auth():
    #     return

    apply_styles(Theme())
    build_header(80)

    refs = build_view()
    state = CashManagementState()
    controller = CashManagementController(refs=refs, state=state)
    controller.wire()
