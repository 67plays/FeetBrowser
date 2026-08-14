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
    toehub://refresh           re-fetch the catalog
    toe://hub                  alias for toehub://

The catalog repo URL is configurable and defaults to the official catalog.
"""

import json
import os
import shutil
import sys

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
        url = URL(base + fname)
        try:
            _h, data, _c = url.request()
            with open(os.path.join(folder, fname), "w") as f:
                f.write(data)
        except Exception as e:  # noqa: BLE001 - bad fetch leaves a note
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
    if action == "refresh" or action in ("hub", ""):
        return {}, _hub_page(), "text/html"
    return {}, _hub_page(), "text/html"


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
                          f'<a href="toehub://uninstall/{name}">uninstall</a> '
                          f'<span class="dim">disabled</span>')
                cls = "disabled"
            else:
                action = (f'<a href="toehub://disable/{name}">disable</a> '
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
