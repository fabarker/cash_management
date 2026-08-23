"""PMG Web Tools NiceGUI entrypoint.

Importing page modules registers their routes via @ui.page decorators.
"""

import logging
import sys

from nicegui import ui, app
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Suppress harmless Windows asyncio noise ────────────────────
# The ProactorEventLoop on Windows logs ERROR-level tracebacks when
# WebSocket connections close abruptly (browser refresh / tab close).
# These are cosmetic and can be safely filtered out.
if sys.platform == 'win32':
    class _ProactorFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return '_ProactorBasePipeTransport' not in record.getMessage()

    logging.getLogger('asyncio').addFilter(_ProactorFilter())

# Suppress Chrome DevTools .well-known probe (cosmetic noise)
class _DevToolsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return '.well-known/appspecific' not in record.getMessage()

logging.getLogger('nicegui').addFilter(_DevToolsFilter())

# ── Prevent NiceGUI from force-reloading pages ────────────────
# NiceGUI's Outbox.try_rewind sends window.location.reload() when
# it can't replay messages after a brief WebSocket reconnect.  This
# causes the page to visibly refresh and lose all UI state.
#
# Fix: increase reconnect_timeout and message history so the rewind
# succeeds.  The default reconnect_timeout of 3s is too tight when
# long-running io_bound operations are in-flight.
app.config.message_history_length = 10_000  # default is 1000


app.add_static_files('/assets', Path(__file__).resolve().parent / 'assets')
from scripts.web_tools.pages.cash_management import page as cash_management_page

def main() -> None:
    ui.run(
        reload=False,
        reconnect_timeout=60.0,       # default 3s is too tight; prevents spurious reloads
        title="PMG Web Tools"
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()