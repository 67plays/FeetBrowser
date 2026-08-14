"""ToeHub — FeetBrowser's toe marketplace.

The ToeHub is built into the browser core so that extensions are always
installed *on demand* rather than bundled. It pulls a catalog
(`index.json`) from a configurable toe repository over the browser's own
HTTP stack, then installs / uninstalls / toggles toes in the local `toes/`
folder with live re-discovery — no restart needed.

Pages served (via the built-in handle hook):
    toehub://                  the hub: available + installed toes
    toehub://install/<name>    fetch a toe from the catalog and install it
    toehub://uninstall/<name>  remove an installed toe
    toehub://enable/<name>     enable a disabled toe
    toehub://disable/<name>    disable an enabled toe (kept installed)
    toehub://manual/<name>     show an installed toe's manual page
    toehub://config/<name>     configure an installed toe's options
    toehub://config/<name>/set/<key>/<value>   set one option
    toehub://refresh           re-fetch the catalog
    toe://hub                  alias for toehub://

The catalog repo URL is configurable and defaults to the official catalog.
"""

import json
import os
import shutil
import sys
import urllib.parse

from . import toes as toes
from .net import URL

DEFAULT_CATALOG = ("https://raw.githubusercontent.com/xplosivex/"
                   "feetbrowser-toes/main/index.json")

HUB_STYLE = """
  body { font-family: Courier; margin: 40px; background: #fdf6e3; color: #222; }
  h1 { color: #8b0000; letter-spacing: 2px; }
  h2 { color: #444; margin-top: 24px; }
  .k { color: #8b0000; }
  .v { color: #1a73e8; }
  .dim { color: #999; }
  .box { border: 1px solid #bbb; background: #fff; padding: 6px 10px; margin: 4px 0; }
  .installed { border-left: 4px solid #1a73e8; }
  .disabled { border-left: 4px solid #bbb; color: #888; }
  a { color: #8b0000; }
"""


def catalog_url():
    """The toe catalog URL, from the config if present."""
    cfg = toes._load_config()
    return cfg.get("catalog", DEFAULT_CATALOG)


def set_catalog_url(url):
    """Override the toe catalog URL (persisted in the shared config)."""
    cfg = toes._load_config()
    cfg["catalog"] = url
    toes._save_config(cfg)


def fetch_catalog():
    """Fetch and parse the toe catalog. Returns (toe dicts, repo name)."""
    url = URL(catalog_url())
    try:
        _h, body, _c = url.request()
        data = json.loads(body)
        return data.get("toes", []), data.get("repo", "")
    except Exception as e:  # noqa: BLE001 - a bad catalog should not crash
        sys.stderr.write(f"toehub: catalog fetch failed: {e}\n")
        return [], ""


def installed_toes():
    """Names of toes currently installed locally."""
    return [t.name for t in toes.discover_toes()]


def disabled_toes():
    return toes.disabled_toes()


def install_toe(name, catalog_toes, browser=None):
    """Install a toe by name from the catalog. Returns an HTML body."""
    match = next((t for t in catalog_toes if t.get("name") == name), None)
    if match is None:
        return _msg(f"<b>{name}</b> is not in the catalog.")
    base = catalog_url().rsplit("/index.json", 1)[0] + "/" + name + "/"
    folder = os.path.join(toes.repo_root(), toes.TOES_DIR, name)
    os.makedirs(folder, exist_ok=True)
    files = match.get("files") or ["toe.json", "toe.py"]
    for fname in files:
        # manual.md is optional; a missing one must not fail the install.
        optional = fname in ("manual.md", "README.md")
        url = URL(base + fname)
        try:
            _h, data, _c = url.request()
            with open(os.path.join(folder, fname), "w") as f:
                f.write(data)
        except Exception as e:  # noqa: BLE001 - bad fetch leaves a note
            if optional:
                continue
            return _msg(f"Could not fetch <b>{fname}</b>: {e}")
    toes.set_toe_enabled(name, True)
    if browser is not None:
        browser.reload_toes()
    return _msg(f"Installed <b>{name}</b> v{match.get('version', '?')}. "
                f"It is now gripping the browser.")


def uninstall_toe(name, browser=None):
    """Remove an installed toe folder. Returns an HTML body."""
    folder = os.path.join(toes.repo_root(), toes.TOES_DIR, name)
    if not os.path.isdir(folder):
        return _msg(f"<b>{name}</b> is not installed.")
    shutil.rmtree(folder, ignore_errors=True)
    toes.set_toe_enabled(name, True)
    if browser is not None:
        browser.reload_toes()
    return _msg(f"Uninstalled <b>{name}</b>. The browser breathes easier.")


def toggle_toe(name, enable, browser=None):
    """Enable or disable an installed toe. Returns an HTML body."""
    if name not in installed_toes():
        return _msg(f"<b>{name}</b> is not installed.")
    toes.set_toe_enabled(name, enable)
    if browser is not None:
        browser.reload_toes()
    state = "enabled" if enable else "disabled"
    return _msg(f"<b>{name}</b> is now <b>{state}</b>.")


def manual_toe(name):
    """Render an installed toe's manual.md as an HTML page. Returns an
    HTML body, or an error page if the toe or manual is missing."""
    from .toes import repo_root, TOES_DIR
    folder = os.path.join(repo_root(), TOES_DIR, name)
    manual = os.path.join(folder, "manual.md")
    if not os.path.isdir(folder):
        return _msg(f"<b>{name}</b> is not installed.")
    if not os.path.isfile(manual):
        # Fall back to the manifest description.
        try:
            with open(os.path.join(folder, "toe.json")) as f:
                manifest = json.load(f)
            desc = manifest.get("description", "No description.")
        except (OSError, ValueError):
            desc = "No description."
        return _manual_page(name, f"{desc}\n\nThis toe ships without a "
                                   "manual.md.")
    try:
        with open(manual) as f:
            md = f.read()
    except OSError as e:
        return _manual_page(name, f"Could not read manual: {e}")
    return _manual_page(name, md)


def _manual_page(name, md):
    """Render a markdown manual as a simple HTML page."""
    body = _md_to_html(md)
    return f"""<!doctype html>
<html><head><title>{_esc(name)} manual</title><style>{HUB_STYLE}</style>
</head>
<body>
<h1>{_esc(name)}</h1>
<p class="dim">MANUAL · HOW THIS TOE WORKS</p>
{body}
<p class="dim"><a href="toehub://">back to the hub</a></p>
</body></html>
"""


def _md_to_html(md):
    """A tiny markdown -> HTML renderer (headings, code fences, bullets,
    tables-as-text, paragraphs). Good enough for a manual."""
    out = []
    in_code = False
    for line in md.splitlines():
        if line.strip().startswith("```"):
            if in_code:
                out.append("</pre>")
                in_code = False
            else:
                out.append("<pre>")
                in_code = True
            continue
        if in_code:
            out.append(_esc(line))
            continue
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if stripped.startswith("# "):
            out.append(f"<h1>{_esc(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{_esc(stripped[3:])}</h2>")
        elif stripped.startswith("### "):
            out.append(f"<h3>{_esc(stripped[4:])}</h3>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            out.append(f'<div class="box">• {_esc(stripped[2:])}</div>')
        elif stripped.startswith("|"):
            out.append(f"<p>{_esc(stripped)}</p>")
        else:
            out.append(f"<p>{_esc(stripped)}</p>")
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _msg(body):
    return ("<!doctype html><html><head><title>ToeHub</title>"
            f"<style>{HUB_STYLE}</style></head><body>"
            f"<h1>🦶 TOEHUB</h1>{body}"
            '<p class="dim"><a href="toehub://">back to the hub</a></p>'
            "</body></html>")


def handle(url, tab):
    """The built-in hub pages. Returns a response tuple or None.

    Serves toehub:// (the marketplace) and the framework-level toe://
    pages (hub, gallery, hello) so the browser always has a front door
    even when no toes are installed. Any other host falls through.
    """
    browser = tab.browser if tab is not None else None
    if url.scheme == "toehub":
        return _handle_hub(url, browser)
    if url.scheme == "toe":
        if url.host in ("hub", ""):
            return {}, _hub_page(), "text/html"
        if url.host == "gallery":
            return {}, _gallery_page(), "text/html"
        if url.host == "hello":
            return {}, _hello_page(), "text/html"
    return None


def _handle_hub(url, browser):
    # toehub://action/name  ->  host = "action", path = "/name"
    action = url.host or ""
    name = (url.path or "/").lstrip("/") or ""
    if action == "install" and name:
        catalog, _repo = fetch_catalog()
        return {}, install_toe(name, catalog, browser), "text/html"
    if action == "uninstall" and name:
        return {}, uninstall_toe(name, browser), "text/html"
    if action == "enable" and name:
        return {}, toggle_toe(name, True, browser), "text/html"
    if action == "disable" and name:
        return {}, toggle_toe(name, False, browser), "text/html"
    if action == "manual" and name:
        return {}, manual_toe(name), "text/html"
    if action == "config" and name:
        return _config_page(url, name, browser)
    if action == "refresh" or action in ("hub", ""):
        return {}, _hub_page(), "text/html"
    return {}, _hub_page(), "text/html"


def _config_page(url, name, browser):
    """Render the config page for a toe, or apply a set action."""
    # toehub://config/<name>[/set/<key>?value=<v>]
    parts = [urllib.parse.unquote(p) for p in (url.path or "").split("/")
             if p]
    if not parts:
        return {}, _msg("Missing toe name."), "text/html"
    toe_name = parts[0]
    ctx = _find_context(browser, toe_name)
    if ctx is None:
        return {}, _msg(f"<b>{toe_name}</b> is not installed."), "text/html"
    if len(parts) >= 3 and parts[1] == "set":
        key, _, query = parts[2].partition("?")
        key = urllib.parse.unquote(key)
        params = urllib.parse.parse_qs(query)
        if "value" in params:
            ctx.set_config(key, params["value"][0])
        elif len(parts) >= 4:
            # Backward-compatible path style: set/<key>/<value>
            ctx.set_config(key, "/".join(parts[3:]))
    options = ctx.config_options()
    if not options:
        return {}, _config_page_html(toe_name, "<div class='box'>This toe "
            "has no configurable options.</div>"), "text/html"
    rows = []
    for key, opt in options:
        value = ctx.config_value(key)
        rows.append(_config_option_html(toe_name, key, opt, value))
    return {}, _config_page_html(toe_name, "\n".join(rows)), "text/html"


def _config_page_html(name, body):
    return f"""<!doctype html>
<html><head><title>{_esc(name)} config</title><style>{HUB_STYLE}</style>
</head>
<body>
<h1>{_esc(name)}</h1>
<p class="dim">CONFIGURATION · CUSTOMIZE THIS TOE</p>
{body}
<p class="dim"><a href="toehub://manual/{name}">manual</a> ·
<a href="toehub://">back to the hub</a></p>
</body></html>
"""


def _config_option_html(name, key, opt, value):
    value_html = _esc(str(value))
    key_esc = _esc(key)
    if opt.kind == "bool":
        toggle = "0" if value else "1"
        label = "ON" if value else "OFF"
        control = (f'<a href="toehub://config/{name}/set/{key_esc}'
                   f'?value={toggle}">toggle to {label}</a>')
    elif opt.kind == "choice":
        choices = "".join(
            f'<a href="toehub://config/{name}/set/{key_esc}'
            f'?value={_url_escape(str(v))}">{_esc(l)}</a> '
            for v, l in opt.options)
        control = choices
    else:
        # str / int: a real text input + submit button, prefilled with the
        # current value. GET submits value as a query param.
        control = (
            f'<form action="toehub://config/{name}/set/{key_esc}" '
            f'method="get">'
            f'<input type="text" name="value" value="{value_html}" '
            f'size="24">'
            f' <input type="submit" value="save"></form>')
    help_html = f'<div class="dim">{_esc(opt.help)}</div>' if opt.help else ""
    return (f'<div class="box"><b>{_esc(opt.label)}</b> '
            f'<span class="v">= {value_html}</span><br>{control}'
            f'<br>{help_html}</div>')


def _find_context(browser, name):
    if browser is None:
        return None
    for ctx in browser.toe_contexts:
        if ctx.toe_name() == name:
            return ctx
    return None


def _url_escape(s):
    return (s.replace("%", "%25").replace("/", "%2F")
            .replace("&", "%26").replace("?", "%3F"))


def _hub_page():
    catalog, repo = fetch_catalog()
    installed = set(installed_toes())
    disabled = disabled_toes()
    rows = []
    if not catalog:
        rows.append("<div class='box'>Could not reach the toe catalog. "
                    f"Checked <span class='v'>{catalog_url()}</span>."
                    "<br>Check your network or the toe repo.</div>")
    for toe in catalog:
        name = toe.get("name", "?")
        if name in installed:
            if name in disabled:
                action = (f'<a href="toehub://enable/{name}">enable</a> '
                          f'<a href="toehub://manual/{name}">manual</a> '
                          f'<a href="toehub://uninstall/{name}">uninstall</a> '
                          f'<span class="dim">disabled</span>')
                cls = "disabled"
            else:
                action = (f'<a href="toehub://disable/{name}">disable</a> '
                          f'<a href="toehub://manual/{name}">manual</a> '
                          f'<a href="toehub://uninstall/{name}">uninstall</a> '
                          f'<span class="dim">enabled</span>')
                cls = "installed"
        else:
            action = f'<a href="toehub://install/{name}">install</a>'
            cls = ""
        rows.append(
            f'<div class="box {cls}"><b>{name}</b> '
            f'<span class="dim">v{toe.get("version", "?")}</span> — '
            f'{toe.get("description", "")}<br>{action}</div>')
    if not installed:
        installed_html = ("<div class='box'><span class='k'>No toes "
                          "installed.</span> Pick one above and give the "
                          "browser some feet.</div>")
    else:
        installed_html = "".join(
            f'<div class="box {"disabled" if n in disabled else "installed"}">'
            f'<b>{n}</b>'
            + (f' — <a href="toehub://enable/{n}">enable</a>'
               if n in disabled else
               f' — <a href="toehub://disable/{n}">disable</a>')
            + f' · <a href="toehub://config/{n}">config</a>'
            + f' · <a href="toehub://manual/{n}">manual</a>'
            + f' · <a href="toehub://uninstall/{n}">uninstall</a></div>'
            for n in sorted(installed))
    return f"""<!doctype html>
<html><head><title>ToeHub</title><style>{HUB_STYLE}</style></head>
<body>
<h1>🦶 TOEHUB</h1>
<p class="dim">THE OFFICIAL TOE MARKETPLACE · CATALOG: {repo or 'unknown'}</p>
<h2>INSTALL</h2>
{"".join(rows)}
<h2>INSTALLED</h2>
{installed_html}
<p class="dim"><a href="toehub://refresh">refresh catalog</a> ·
<a href="toe://hub">toe://hub</a></p>
</body></html>
"""


def _gallery_page():
    installed = installed_toes()
    disabled = disabled_toes()
    if not installed:
        rows = ("<div class='box'><span class='k'>No toes installed.</span> "
                "Visit <a href='toe://hub'>the hub</a> to grow some "
                "feet.</div>")
    else:
        rows = "".join(
            f'<div class="box {"disabled" if n in disabled else "installed"}">'
            f'<b>{n}</b>'
            + (f' — <a href="toehub://enable/{n}">enable</a>'
               if n in disabled else
               f' — <a href="toehub://disable/{n}">disable</a>')
            + f' · <a href="toehub://config/{n}">config</a>'
            + f' · <a href="toehub://manual/{n}">manual</a>'
            + f' · <a href="toehub://uninstall/{n}">uninstall</a></div>'
            for n in sorted(installed))
    return f"""<!doctype html>
<html><head><title>The toe gallery</title><style>{HUB_STYLE}</style></head>
<body>
<h1>🦶 THE TOE GALLERY</h1>
<p class="dim">EVERY TOE CURRENTLY GRIPPING YOUR BROWSER</p>
{rows}
<p class="dim"><a href="toe://hub">back to the hub</a></p>
</body></html>
"""


def _hello_page():
    return """<!doctype html>
<html><head><title>toe://hello</title><style>%s</style></head>
<body>
<h1>toe://hello</h1>
<div class="box">The framework is here and it's gripping. But no toes are
installed yet — the browser is barefoot.</div>
<p class="dim">Open <a href="toe://hub">the hub</a> to install toes, or
<a href="toe://gallery">the gallery</a> to see what's already gripping.</p>
</body></html>
""" % HUB_STYLE
