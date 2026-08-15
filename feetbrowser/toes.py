"""Toes — FeetBrowser's extension hooking.

Every foot deserves toes. A toe is a plain Python module, living in its own
folder under `toes/`, that gets invited to dinner at a few well-placed points
in the load pipeline. No new dependencies, no sandboxing theater: a toe is
trusted local code, exactly like the browser's own modules, and it can do
anything the browser itself can do.

A toe folder looks like:

    toes/name-of-toe/
        toe.json     # { "name", "version", "description", "entry" }
        toe.py       # the code, exposing activate(ctx)

`toe.json` is read at startup; `toe.py` is imported and its `activate(ctx)`
is called once with a Context that wires up the toe's hooks. A toe that
raises while loading is skipped with a warning to stderr — one bad toe never
bricks the browser.
"""

import importlib.util
import json
import os
import sys

from .net import URL

# Folder, relative to the repo root, where toes live.
TOES_DIR = "toes"


def _config_path():
    """Path to the shared ToeHub/toes config file."""
    return os.path.join(repo_root(), TOES_DIR, "config.json")


def _load_config():
    try:
        with open(_config_path(), encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_config(cfg):
    try:
        os.makedirs(os.path.dirname(_config_path()), exist_ok=True)
        with open(_config_path(), "w", encoding="utf8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def disabled_toes():
    """Set of toe names currently disabled (persisted in config.json)."""
    return set(_load_config().get("disabled", []))


def set_toe_enabled(name, enabled):
    """Enable or disable a toe, persisted in the shared config."""
    cfg = _load_config()
    disabled = set(cfg.get("disabled", []))
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    cfg["disabled"] = sorted(disabled)
    _save_config(cfg)


class ButtonDef:
    """A toolbar button the chrome should draw for a toe.

    `glyph` is a short label drawn on the hand-rolled toolbar; `id` is
    passed back to the toe's `on_click` handler.
    """

    def __init__(self, id, glyph, label=None):
        self.id = id
        self.glyph = glyph
        self.label = label or glyph

    def __repr__(self):
        return f"ButtonDef({self.id!r}, {self.glyph!r})"


class ConfigOption:
    """A configurable option a toe exposes to the ToeHub.

    `kind` is one of "bool", "int", "str", or "choice". Choices carry an
    `options` list of (value, label) pairs. `default` is used when the
    setting is unset. `help` is shown in the config page.
    """

    def __init__(self, key, label, kind="str", default=None, options=None,
                 help=""):
        self.key = key
        self.label = label
        self.kind = kind
        self.default = default
        self.options = options or []
        self.help = help

    def coerce(self, value):
        """Coerce a raw string (from the URL) to this option's type."""
        if self.kind == "bool":
            return str(value).lower() in ("1", "true", "yes", "on")
        if self.kind == "int":
            try:
                return int(value)
            except (TypeError, ValueError):
                return self.default
        return str(value)

    def render(self, value):
        """Render the current value for the config page."""
        return f"{value}"


class Context:
    """The only thing a toe gets to hold. It wraps the browser and the
    active tab and dispatches calls out to the toe's hook handlers.

    Every hook is optional; a toe simply defines the ones it cares about
    as plain methods on the object returned from `activate(ctx)`.

    Supported hooks:

        on_load(url, body)          -> body or None
            Rewrite the raw HTML before it is parsed. Return the new body,
            or None to leave it alone.

        extra_css(url)              -> css string or None
            Inject an author stylesheet for this page, applied after the
            user-agent sheet and before any <style>/<link> sheets.

        handle(url, tab)            -> (headers, body, content_type) or None
            First crack at a navigation. Return a response tuple to render
            the page yourself (this is how toe:// and friends work); return
            None to fall through to normal fetching.

        on_draw(canvas, offset)     -> None
            Paint directly onto the canvas (after the page, before the
            chrome). `offset` is how much the page is shifted by the chrome.
            The canvas is the retained display list from canvas.py, whose
            method names a toe from the published catalog already knows; see
            docs/toes.md for where that compatibility stops.

        buttons()                   -> [ButtonDef]
            Extra toolbar buttons, drawn on the hand-rolled toolbar.

        on_click(button_id)         -> None
            A toe toolbar button was clicked.

        on_keypress(event)          -> bool
            A key was pressed while no address bar had focus. Return True to
            swallow the key, False to let the browser handle it.

        on_motion(x, y)             -> None
            The mouse moved over the page (document coordinates, below the
            chrome). Fired on every motion event, so keep it cheap.

        on_new_tab()                -> None
            A new tab was created.

        chrome_bands()              -> [(id, height), ...]
            Declare horizontal bands this toe wants drawn in the browser
            chrome, stacked above the tabs. Each band is a (unique id,
            height-in-px) tuple. The browser grows its chrome to fit every
            toe's bands.

        on_chrome_draw(canvas, bands) -> None
            Paint the toe's chrome bands. `bands` is the list of
            (id, height, y_offset) tuples for the current frame, in draw
            order. Called after the chrome background, before the tabs.

        on_chrome_click(x, y, bands) -> bool
            A click landed inside the chrome band region. `bands` is the
            same list as on_chrome_draw. Return True if the click was
            consumed, False to let the normal chrome handle it.

    Configurable options (for the ToeHub's config page):

        ctx.define_config(ConfigOption(...), ...)
            Declare configurable options. Values live in the toe's
            persisted settings and are editable from `toehub://config/<name>`.
    """

    def __init__(self, browser, toe):
        self.browser = browser
        self.toe = toe
        self._callbacks = {}
        self._settings = None
        self._config = {}
        self.enabled = self.toe_name() not in disabled_toes()
        if hasattr(toe, "activate"):
            toe.activate(self)

    # -- configurable options ---------------------------------------------

    def define_config(self, *options):
        """Declare this toe's configurable options. Each arg is a
        ConfigOption. Values are stored in settings (persisted) and seeded
        with each option's default when unset."""
        for opt in options:
            self._config[opt.key] = opt
            if opt.key not in self.settings:
                self.settings[opt.key] = opt.default

    def config_options(self):
        """List of (key, ConfigOption) declared by this toe, sorted by key."""
        return sorted(self._config.items())

    def config_value(self, key):
        """Current value of a declared config option (coerced)."""
        opt = self._config.get(key)
        if opt is None:
            return self.settings.get(key)
        return opt.coerce(self.settings.get(key, opt.default))

    def set_config(self, key, value):
        """Coerce and store a config option value, then persist."""
        opt = self._config.get(key)
        if opt is not None:
            value = opt.coerce(value)
        self.settings[key] = value
        self.save_settings()

    # -- helpers the toe can call -----------------------------------------

    def current_tab(self):
        return self.browser.active_tab

    def tabs(self):
        return list(self.browser.tabs)

    def set_status(self, msg):
        tab = self.browser.active_tab
        if tab:
            tab.status = msg

    def open(self, url):
        """Open a URL in the active tab through the full pipeline."""
        tab = self.browser.active_tab
        if tab:
            tab.load(URL(str(url)) if isinstance(url, str) else url)

    def popup(self, url, width=320, height=240):
        """Open a real popup window rendering `url` through the pipeline.

        Popups are separate windows (not redirects) with their own
        canvas, a hand-drawn title bar, scrolling, and a scrollbar. They
        share the browser's toe contexts, so toe:// pages, the detective's
        paper trail, and link navigation all work inside them.
        """
        from .browser import PopupWindow
        return PopupWindow(self.browser, url, width, height)

    # -- per-toe settings -------------------------------------------------

    @property
    def settings(self):
        """A dict of this toe's persisted settings (loaded lazily)."""
        if self._settings is None:
            self._settings = self._load_settings()
        return self._settings

    def save_settings(self):
        """Persist the current settings dict to toes/<name>/settings.json."""
        path = self._settings_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf8") as f:
                json.dump(self._settings, f, indent=2)
        except OSError as e:
            sys.stderr.write(
                f"toe {self.toe_name()}: could not save settings: {e}\n")

    def _settings_path(self):
        folder = getattr(self.toe, "folder", None)
        if not folder:
            return os.path.join(repo_root(), TOES_DIR, "settings.json")
        return os.path.join(folder, "settings.json")

    def _load_settings(self):
        path = self._settings_path()
        try:
            with open(path, encoding="utf8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    # -- hook registration ------------------------------------------------

    def on(self, event, callback):
        self._callbacks[event] = callback

    # -- dispatch ---------------------------------------------------------

    def call(self, event, *args, **kwargs):
        if not self.enabled:
            return None
        cb = self._callbacks.get(event)
        if cb is None:
            return None
        try:
            return cb(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - a toe failure is not fatal
            sys.stderr.write(
                f"toe {self.toe_name()}: hook {event} raised "
                f"{type(e).__name__}: {e}\n")
            return None

    def toe_name(self):
        manifest = getattr(self.toe, "manifest", None)
        if manifest and manifest.get("name"):
            return manifest["name"]
        return getattr(self.toe, "__name__", "?")


class Toe:
    """A loaded toe: its manifest plus the module exposing activate()."""

    def __init__(self, name, version, description, folder, module):
        self.name = name
        self.version = version
        self.description = description
        self.folder = folder
        self.module = module


def repo_root():
    """Absolute path of the repo root (where toes/ sits)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def discover_toes(toes_dir=None):
    """Scan toes/ for toe.json manifests and return a list of Toe objects.

    A manifest missing required fields, or whose entry module cannot be
    imported, is skipped with a warning — a broken toe must not stop the
    browser from starting.
    """
    root = os.path.join(repo_root(), toes_dir or TOES_DIR)
    found = []
    if not os.path.isdir(root):
        return found
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        manifest_path = os.path.join(folder, "toe.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, encoding="utf8") as f:
                manifest = json.load(f)
            entry = manifest["entry"]
            module_path = os.path.join(folder, entry)
            spec = importlib.util.spec_from_file_location(
                f"toe_{name.replace('-', '_')}", module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:  # noqa: BLE001 - skip broken toes
            sys.stderr.write(
                f"toes: skipping {name}: {type(e).__name__}: {e}\n")
            continue
        module.manifest = manifest
        found.append(Toe(
            name=manifest.get("name", name),
            version=manifest.get("version", "0"),
            description=manifest.get("description", ""),
            folder=folder,
            module=module,
        ))
    return found


def dispatch(ctxs, event, *args, **kwargs):
    """Call `event` on every toe context; return the list of non-None results."""
    results = []
    for c in ctxs:
        r = c.call(event, *args, **kwargs)
        if r is not None:
            results.append(r)
    return results


def first(ctxs, event, *args, **kwargs):
    """Like dispatch but stops at the first non-None result."""
    for c in ctxs:
        r = c.call(event, *args, **kwargs)
        if r is not None:
            return r
    return None


def rewrite(ctxs, url, body):
    """Chain on_load: each toe may rewrite the body; last write wins."""
    for c in ctxs:
        r = c.call("on_load", url, body)
        if r is not None:
            body = r
    return body


def extra_css(ctxs, url):
    """Collect injected stylesheets from every toe, concatenated in order."""
    sheets = []
    for c in ctxs:
        r = c.call("extra_css", url)
        if r:
            sheets.append(r)
    return "\n".join(sheets) if sheets else None


def compute_bands(ctxs):
    """Collect every toe's chrome bands as [(id, height, y_offset), ...].

    Bands are stacked above the tabs in declaration order; each entry's
    y_offset is the top of its strip in canvas coordinates.
    """
    bands = []
    y = 0
    for c in ctxs:
        declared = c.call("chrome_bands") or []
        for band_id, height in declared:
            bands.append((band_id, height, y))
            y += height
    return bands


def band_height(bands):
    """Total height consumed by chrome bands."""
    return sum(h for _id, h, _y in bands)


# -- CLI helpers ----------------------------------------------------------


def list_toes():
    """Print a table of installed toes and their status."""
    found = discover_toes()
    if not found:
        print("No toes installed. Open toe://hub in the browser to install "
              "some, or use --toe-install <name>.")
        return
    disabled = disabled_toes()
    width = max(len(t.name) for t in found)
    for t in found:
        state = "disabled" if t.name in disabled else "enabled "
        print(f"{t.name:<{width}}  v{t.version}  [{state}]  {t.description}")


def search_toes(term):
    """Search the toe catalog by name/description."""
    from .toehub import fetch_catalog
    catalog, repo = fetch_catalog()
    term = term.lower()
    hits = [t for t in catalog
            if term in t.get("name", "").lower()
            or term in t.get("description", "").lower()]
    if not hits:
        print(f"No toes in the catalog match '{term}'.")
        return
    for t in hits:
        print(f"{t.get('name'):<20}  v{t.get('version')}  "
              f"{t.get('description')}")


def install_toe(name):
    """Install a toe by name from the catalog."""
    from .toehub import fetch_catalog, install_toe as _install
    catalog, _repo = fetch_catalog()
    msg = _install(name, catalog)
    _print_html_msg(msg)


def uninstall_toe(name):
    """Uninstall an installed toe."""
    from .toehub import uninstall_toe as _uninstall
    _print_html_msg(_uninstall(name))


def set_enabled(name, enable):
    """Enable or disable an installed toe."""
    found = discover_toes()
    if name not in {t.name for t in found}:
        print(f"error: {name} is not installed.")
        return 1
    set_toe_enabled(name, enable)
    print(f"{name} is now {'enabled' if enable else 'disabled'}.")
    return 0


def _print_html_msg(html):
    import re
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<h1[^>]*>.*?</h1>", " ", text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<a[^>]*>.*?</a>", " ", text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s([.,;:])", r"\1", text).strip()
    print(text)


def new_toe(name):
    """Scaffold a new toe folder with a manifest and an entry stub."""
    import re
    safe = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
    if not safe:
        print("error: toe name must contain letters or digits")
        return 1
    folder = os.path.join(repo_root(), TOES_DIR, safe)
    if os.path.exists(folder):
        print(f"error: {folder} already exists")
        return 1
    os.makedirs(folder)
    manifest = {
        "name": safe,
        "version": "0.1.0",
        "description": "A brand new toe.",
        "entry": "toe.py",
    }
    with open(os.path.join(folder, "toe.json"), "w", encoding="utf8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    # newline="\n" so the scaffold is the same file on every platform rather
    # than gaining CRLFs on Windows.
    with open(os.path.join(folder, "toe.py"), "w", encoding="utf8",
              newline="\n") as f:
        f.write('"""%s toe."""\n\n\n'
                'def activate(ctx):\n'
                '    # ctx.on("on_load", on_load)\n'
                '    pass\n' % safe)
    print(f"created {folder}")
    return 0


def toe_docs():
    """Print a markdown reference generated from every toe's manifest."""
    found = discover_toes()
    if not found:
        print("No toes installed.")
        return
    print("# Toe reference\n")
    for t in found:
        print(f"## {t.name} v{t.version}\n")
        print(f"{t.description or 'No description.'}\n")
        doc = getattr(t.module, "__doc__", "") or ""
        if doc.strip():
            print("```\n" + doc.strip() + "\n```\n")
