"""Public, content-free handoff to Hook. No store or credentials are involved."""
import base64
import hashlib
import re

from mailbox.link_icon import ICON_PNG_B64


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


# Fixed script: the only varying value is the already-validated link in the page.
# It picks the platform (Android, iPhone or iPad, anything else), points the
# primary button at that platform's link, and tries the redirect as before:
# Android opens the intent link, every other browser opens the iPhone link
# (`hook://`, which the Mac app also handles). iPadOS reports a Mac user agent, so
# a touch screen on "Macintosh" counts as iPad. On a Mac or other desktop the
# button points at `relay://`.
SCRIPT = ('var d=document,u=navigator.userAgent,'
          'p=u.includes("Android")?"android":'
          '(/iPhone|iPad|iPod/.test(u)||(u.includes("Macintosh")&&navigator.maxTouchPoints>1))'
          '?"iphone":"mac";'
          'd.getElementById("open").href=d.getElementById(p).href;'
          'if (p==="android") '
          'window.location.replace(d.getElementById("android").href); '
          'else window.location.replace(d.getElementById("iphone").href);')

# Fixed stylesheet, allowed by its hash only. System fonts, no external assets.
STYLE = (
    ":root{color-scheme:light dark;--bg:#F6F2FA;--card:#FFFFFF;--ink:#1E1527;"
    "--muted:#625A6E;--line:#E6DEEF;--acc:#7412BE;--on-acc:#FFFFFF;--ring:#7412BE}"
    "@media (prefers-color-scheme:dark){:root{--bg:#140C1B;--card:#1E1527;"
    "--ink:#F3EEF8;--muted:#B3A9C0;--line:#33263F;--acc:#B46CF0;--on-acc:#140C1B;"
    "--ring:#D8B4FA}}"
    "*{box-sizing:border-box}"
    "html,body{height:100%}"
    "body{margin:0;background:var(--bg);color:var(--ink);"
    "font:15px/1.5 -apple-system,BlinkMacSystemFont,\"Segoe UI\",system-ui,sans-serif;"
    "display:grid;place-items:center;padding:28px 16px}"
    "main{background:var(--card);border:1px solid var(--line);border-radius:16px;"
    "padding:26px 24px;max-width:340px;width:100%;display:grid;gap:14px;"
    "justify-items:center;text-align:center;box-shadow:0 10px 30px rgba(60,20,90,.08)}"
    ".icon{display:block;width:72px;height:72px;border-radius:18px}"
    "h1{font-size:20px;font-weight:650;margin:0}"
    "p{margin:0}"
    ".s{color:var(--muted);font-size:13.5px}"
    ".spin{width:16px;height:16px;border-radius:50%;border:2px solid currentColor;"
    "border-right-color:transparent;display:inline-block;vertical-align:-3px;"
    "margin-right:8px;animation:r 1s linear infinite}"
    "@keyframes r{to{transform:rotate(360deg)}}"
    "@media (prefers-reduced-motion:reduce){.spin{animation:none}}"
    "a{color:var(--acc);border-radius:6px}"
    "a:focus-visible{outline:2px solid var(--ring);outline-offset:3px}"
    ".btn{display:block;width:100%;padding:11px 14px;border-radius:10px;"
    "background:var(--acc);color:var(--on-acc);font-weight:600;font-size:14.5px;"
    "text-decoration:none}"
    ".others{display:flex;gap:18px;justify-content:center;font-size:13px}"
    ".others a{text-decoration:none;padding:2px 4px}"
    ".others a:hover{text-decoration:underline}"
    "footer{color:var(--muted);font-size:12px;border-top:1px solid var(--line);"
    "padding-top:12px;width:100%}"
)

# The Hook app icon as a fixed data: URI (see link_icon.py). The title says "Hook",
# so the image is decorative and has an empty alt.
ICON = ('<img class="icon" src="data:image/png;base64,' + ICON_PNG_B64
        + '" width="72" height="72" alt="">')

SCRIPT_HASH = _sha256(SCRIPT)
STYLE_HASH = _sha256(STYLE)
CSP = ("default-src 'none'; script-src 'sha256-" + SCRIPT_HASH
       + "'; style-src 'sha256-" + STYLE_HASH
       + "'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
ANDROID_PACKAGE = "io.github.xooxoxxo.hook.android"
_ROUTE = re.compile(r"/i/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


def render(path: str) -> bytes | None:
    """Only an exact UUID route; reject queries, fragments and encoded input.

    Unknown and known asks have exactly the same page. Never look up an ask.
    """
    match = _ROUTE.fullmatch(path)
    if match is None:
        return None
    item_id = match[1].lower()
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="color-scheme" content="light dark">'
            f'<title>Open in Hook</title><style>{STYLE}</style></head><body><main>'
            f'{ICON}'
            f'<h1>Opening Hook…</h1>'
            f'<p class="s" role="status"><span class="spin" aria-hidden="true"></span>'
            f'Handing this ask to the Hook app.</p>'
            f'<a id="open" class="btn" href="hook://i/{item_id}">Open in Hook</a>'
            f'<nav class="others" aria-label="Open on another device">'
            f'<a id="iphone" href="hook://i/{item_id}">iPhone</a>'
            f'<a id="android" href="intent://i/{item_id}#Intent;scheme=hook;'
            f'package={ANDROID_PACKAGE};end">Android</a>'
            f'<a id="mac" href="relay://i/{item_id}">Mac</a></nav>'
            f'<footer>Needs Hook installed and connected to your Pond.</footer>'
            f'</main><script>{SCRIPT}</script></body></html>').encode("utf-8")
