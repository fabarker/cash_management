from nicegui import ui, app
from pathlib import Path

from pmg_core.apps.pmg_web_tools.components.header_common import build_clock, build_user_name, build_tools_menu, load_wordmark_svg

WORDMARK_SVG = (Path(__file__).resolve().parent.parent / 'assets' / 'goldman_wordmark.svg').read_text(encoding='utf-8')

def build_header_landing() -> None:
    display_name = app.storage.user.get('display_name', '')


    ui.add_head_html("""
    <style>
    .focus-in-expand {
        -webkit-animation: focus-in-expand 1s cubic-bezier(0.470, 0.000, 0.745, 0.715) both;
                animation: focus-in-expand 1s cubic-bezier(0.470, 0.000, 0.745, 0.715) both;
    }

    @-webkit-keyframes focus-in-expand {
      0% {
        letter-spacing: -0.5em;
        -webkit-filter: blur(12px);
                filter: blur(12px);
        opacity: 0;
      }
      100% {
        -webkit-filter: blur(0);
                filter: blur(0);
        opacity: 1;
      }
    }

    @keyframes focus-in-expand {
      0% {
        letter-spacing: -0.5em;
        filter: blur(12px);
        opacity: 0;
      }
      100% {
        filter: blur(0);
        opacity: 1;
      }
    }
    </style>
    """, shared=False)

    wordmark_svg = load_wordmark_svg()

    with ui.header(fixed=True).style(
            'height: 72px; '
            'min-height: 72px; '
            'padding: 0 40px 0 16px; '
            'align-items: center; '
            'background-color: #000000;'
    ):
        # Single bar row \=\> no wrapping to a new header line
        with ui.row().classes('w-full items-center flex-nowrap no-wrap gap-4'):
            result = ui.label().classes('text-white')

            with ui.button(icon='menu').props('flat').style(
                    'background-color: #000000; color: #FFFFFF!important'
            ).classes('blink-burger'):
                build_tools_menu(result)

            ui.html(wordmark_svg, sanitize=False).classes('header-logo')

            ui.space()
            with ui.row().classes('items-center gap-4'):
                build_user_name(display_name)
                ui.separator().props('vertical').classes('bg-white').style('height: 20px; opacity: 0.8;')
                build_clock()