from nicegui import ui
from scripts.web_tools.config import Theme


def apply_styles(theme: Theme):
    ui.colors(primary=theme.primary)

    ui.add_head_html(

        f"""
        <style type="text/tailwindcss">
        @font-face {{
            font-family: 'gs-sans-regular';
            src: url('/assets/gs-sans-regular.woff2') format('woff2');
            font-weight: 400;
            font-style: normal;
            font-display: swap;
        }}
        @font-face {{
            font-family: 'gs-sans-variable';
            src: url('/assets/gs-sans-variable.woff2') format('woff2');
            font-weight: 300 900;
            font-style: normal;
            font-display: swap;
        }}
        @font-face {{
            font-family: 'gs-sans-condensed';
            src: url('/assets/gs-sans-condensed.woff2') format('woff2');
            font-weight: 300 900;
            font-style: normal;
            font-display: swap;
        }}
        @font-face {{
            font-family: 'gs-sans-italics';
            src: url('/assets/gs-sans-italics.woff2') format('woff2');
            font-weight: 400;
            font-style: normal;
            font-display: swap;
        }}

        html, body, .q-app {{
            font-family: 'gs-sans-condensed', 'gs-sans-regular', 'Helvetica Neue', Arial, sans-serif !important;

        }}

        .font-gs-regular {{
            font-family: 'gs-sans-regular', 'Helvtica Neue',  Ariel, sans-serif !important;
            font-weight: 400 !important;
        }}

        .font-gs-variable {{
            font-family: 'gs-sans-variable', 'Helvtica Neue',  Ariel,
            sans-serif !important;
        }}

        .font-gs-condensed {{
             font-family: 'gs-sans-condensed', 'Helvetica Neue', Arial, sans-serif !important;
        }}

        input, button, textarea, select,
        .q-field__native, .q-field__input, .q-btn, .q-item, .q-label {{
            font-family: inherit !important;
        }}
    </style>

    <style>

        .font-goldman {{
            font-family: 'gs-sans-regular', 'Helvtica Neue',  Ariel, sans-serif !important;
            font-weight: 400 !important;
        }}

        .font-goldman-condensed {{
            font-family: 'gs-sans-condensed', 'Helvtica Neue',  Ariel, sans-serif !important;
            font-weight: 400 !important;
        }}

        .text-neutral-bold {{
            color: #111827 !important; /* neutral-900 */
        }}

        .text-default-heading {{
            font-size: 32px;
            line-height: 1;
            font-weight: 400;
        }}

        @media (min-width: 640px) {{
            .text-default-heading {{
                font-size:36px;       
            }}
        }}

        html, body, .q-app {{
            background: rgb(247, 247, 250) !important;
        }}

        .q-btn-disabled .q-btn__content {{
            color: #9ca3af !important;
        }}

        .q-field__bottom {{
            background-color: #ffffff !important;
        }}

        .acct-label-warn .q-field__label {{
            color: #d32f2f !important; /* red */
        }}

        .acct-nums.q-textarea.q-field--dense.q-field--labeled .q-field__native{{
            padding-top: 10px !important;
            background-color: #fafafa!important;
        }}

        .acct-nums .q-field__label {{
            background-color: #fafafa !important;
            font-size: 18px;
        }}

        .failed-account-item {{
        width: 100%;
        padding: 2px 2px;
        text-align: center;
        border-bottom: 0px solid #e5e7eb;
        font-size: 18px;
        }}

       .failed-account-item:nth-child(odd) {{
        background-color: #ffffff;
       }}

       .failed-account-item:nth-child(even) {{
        background-color: #f3f4f6;
       }}

       .failed-account-item:last-child {{
        border-bottom: none;
       }}

    </style>

    """
    )


def apply_styles_old(theme: Theme):
    ui.colors(primary=theme.primary)

    ui.add_head_html("""
    <link href="https://fonts.cdnfonts.com/css/goldman-sans?styles=71669" rel="stylesheet">
    <style type="text/tailwindcss">
      @import url('https://fonts.cdnfonts.com/css/goldman-sans?styles=71669');
      html, body, .q-app {
          font-family: 'Goldman Sans' !important;
      }

      /* Make common Quasar controls inherit the global font */
      input, button, textarea, select,
      .q-field__native, .q-field__input, .q-btn, .q-item, .q-label {
        font-family: inherit !important;
      }
    </style>

    <style>

        html, body, .q-app {
            background: rgb(247, 247, 250) !important;
        }
        .note-textarea.q-textarea.q-field--dense .q-field__control {
          padding-right: 2px !important;
        }

        .q-btn--disabled {
          background-color: #e5e7eb !important;  /* grey-200 */
          color: #9ca3af !important;            /* grey-400 */
          opacity: 1 !important;                /* remove fade */
        }

        /* Also fix the inner label/icon color */
        .q-btn--disabled .q-btn__content {
          color: #9ca3af !important;
        }

        .q-field__bottom {
          background-color: #ffffff !important;
        }

        /* wrapper class you toggle from Python */
        .acct-label-warn .q-field__label {
        color: #d32f2f !important; /* red */
        }

        .acct-nums.q-textarea.q-field--dense.q-field--labeled .q-field__native{
            padding-top: 10px !important;
        }

    </style>

    """)
