"""Shoes: the FeetBrowser theme manager.

Every theme is a "shoe": a palette of solid colors for the hand-drawn
browser chrome. Each shoe maps the same set of role keys (chrome background,
tabs, toolbar, address bar, status bar, scrollbar, accents, popup title bar)
to colors, so a dark shoe and a light shoe both render the same way.

The current shoe is persisted to `~/.feetbrowser_shoes.json` (mirroring the
bookmarks file) and loaded at startup. Missing keys fall back to the
"Classic Sneaker" palette so a partial shoe never leaves a chrome element
uncolored.
"""

import json
import os

SHOES_FILE = os.path.expanduser("~/.feetbrowser_shoes.json")

#: The default shoe; used when no theme is saved and as the fallback for
#: any role a shoe doesn't define.
DEFAULT_SHOE = "Classic Sneaker"

#: Every chrome element a shoe can color. Missing keys inherit the default.
#:
#:   chrome_bg            chrome area behind the address bar
#:   tab_bar              tab strip background
#:   tab_active           background of the selected tab
#:   tab_inactive         background of the other tabs
#:   tab_text             tab titles
#:   tab_close            the close (×) glyph
#:   plus_button          the new-tab (+) glyph
#:   button_bg            toolbar button fill
#:   button_border        toolbar button outline
#:   button_glyph         toolbar button glyph
#:   button_glyph_disabled
#:   addr_bg              address bar fill
#:   addr_border          address bar outline (unfocused)
#:   addr_focus_border    address bar outline when focused
#:   addr_text            URL text
#:   addr_placeholder     "Type a URL or search term…"
#:   caret                the caret / selection fill
#:   status_bg            status bar fill
#:   status_border        status bar top border
#:   status_text          status text
#:   scroll_thumb         scrollbar thumb
#:   accent               selection / spinner / focus highlight
#:   log_bg               load-error strip fill
#:   log_border           load-error strip top border
#:   log_text             load-error text
#:   toe_btn_bg           installed-toe toolbar button fill
#:   popup_titlebar       popup window title bar
#:   popup_border         popup title bar bottom border
#:   popup_text           popup title / close glyph
#:   menu_bg              context menu fill
#:   menu_border          context menu outline
#:   menu_text            context menu item text
#:   menu_hover           context menu hovered item fill
#:   page_bg              default page background (blank/new-tab pages)
#:   page_text            default page text (blank/new-tab pages)
#:   link_color           default link color on internal pages
SHOE_KEYS = (
    "chrome_bg", "tab_bar", "tab_active", "tab_inactive", "tab_text",
    "tab_close", "plus_button", "button_bg", "button_border", "button_glyph",
    "button_glyph_disabled", "addr_bg", "addr_border", "addr_focus_border",
    "addr_text", "addr_placeholder", "caret", "status_bg", "status_border",
    "status_text", "scroll_thumb", "accent", "log_bg", "log_border",
    "log_text", "toe_btn_bg", "popup_titlebar", "popup_border",
    "popup_text", "menu_bg", "menu_border", "menu_text", "menu_hover",
    "page_bg", "page_text", "link_color",
)

SHOES = {
    "Classic Sneaker": {
        "chrome_bg": "#e8e8e8", "tab_bar": "#d0d0d0",
        "tab_active": "#ffffff", "tab_inactive": "#c4c4c4",
        "tab_text": "#222222", "tab_close": "#666666",
        "plus_button": "#333333", "button_bg": "#f4f4f4",
        "button_border": "#999999", "button_glyph": "#333333",
        "button_glyph_disabled": "#bbbbbb", "addr_bg": "#ffffff",
        "addr_border": "#999999", "addr_focus_border": "#3b82f6",
        "addr_text": "#111111", "addr_placeholder": "#aaaaaa",
        "caret": "#111111", "status_bg": "#efefef",
        "status_border": "#cccccc", "status_text": "#444444",
        "scroll_thumb": "#9aa0a6", "accent": "#1a73e8",
        "log_bg": "#fff4e6", "log_border": "#e0cda8",
        "log_text": "#8a5a00", "toe_btn_bg": "#fdf6e3",
        "popup_titlebar": "#d0d0d0", "popup_border": "#999999",
        "popup_text": "#333333", "menu_bg": "#ffffff",
        "menu_border": "#666666", "menu_text": "#222222",
        "menu_hover": "#1a73e8", "menu_sep": "#dddddd",
        "menu_shadow": "#d0d0d0", "menu_disabled": "#aaaaaa",
        "page_bg": "#ffffff",
        "page_text": "#222222", "link_color": "#1a73e8",
    },
    "Midnight Boot": {
        "chrome_bg": "#202124", "tab_bar": "#2d2e31",
        "tab_active": "#3c4043", "tab_inactive": "#292a2d",
        "tab_text": "#e8eaed", "tab_close": "#bdc1c6",
        "plus_button": "#e8eaed", "button_bg": "#3c4043",
        "button_border": "#5f6368", "button_glyph": "#e8eaed",
        "button_glyph_disabled": "#5f6368", "addr_bg": "#3c4043",
        "addr_border": "#5f6368", "addr_focus_border": "#8ab4f8",
        "addr_text": "#e8eaed", "addr_placeholder": "#9aa0a6",
        "caret": "#e8eaed", "status_bg": "#202124",
        "status_border": "#3c4043", "status_text": "#9aa0a6",
        "scroll_thumb": "#5f6368", "accent": "#8ab4f8",
        "log_bg": "#3c2a1e", "log_border": "#6b4a2a",
        "log_text": "#ffd9a8", "toe_btn_bg": "#3c4043",
        "popup_titlebar": "#2d2e31", "popup_border": "#5f6368",
        "popup_text": "#e8eaed", "menu_bg": "#3c4043",
        "menu_border": "#5f6368", "menu_text": "#e8eaed",
        "menu_hover": "#1a73e8", "menu_sep": "#4a4d51",
        "menu_shadow": "#1a1b1d", "menu_disabled": "#5f6368",
        "page_bg": "#202124",
        "page_text": "#e8eaed", "link_color": "#8ab4f8",
    },
    "Ocean Slipper": {
        "chrome_bg": "#e8f1fb", "tab_bar": "#cfe2f3",
        "tab_active": "#ffffff", "tab_inactive": "#c0d6e8",
        "tab_text": "#123a5c", "tab_close": "#5a7a96",
        "plus_button": "#123a5c", "button_bg": "#f4f9ff",
        "button_border": "#7aa7cc", "button_glyph": "#0f4c81",
        "button_glyph_disabled": "#a9c6de", "addr_bg": "#ffffff",
        "addr_border": "#7aa7cc", "addr_focus_border": "#0f4c81",
        "addr_text": "#0b2438", "addr_placeholder": "#8aa8c2",
        "caret": "#0b2438", "status_bg": "#e8f1fb",
        "status_border": "#c0d6e8", "status_text": "#34597a",
        "scroll_thumb": "#5b83a5", "accent": "#0f4c81",
        "log_bg": "#e9f2fb", "log_border": "#b7d2ea",
        "log_text": "#1f4a73", "toe_btn_bg": "#d7e9f9",
        "popup_titlebar": "#cfe2f3", "popup_border": "#7aa7cc",
        "popup_text": "#123a5c", "menu_bg": "#ffffff",
        "menu_border": "#7aa7cc", "menu_text": "#123a5c",
        "menu_hover": "#0f4c81", "menu_sep": "#c0d6e8",
        "menu_shadow": "#a9c6de", "menu_disabled": "#a9c6de",
        "page_bg": "#ffffff",
        "page_text": "#0b2438", "link_color": "#0f4c81",
    },
    "Forest Moccasin": {
        "chrome_bg": "#e9f2ea", "tab_bar": "#cfe3d2",
        "tab_active": "#ffffff", "tab_inactive": "#bcd6c0",
        "tab_text": "#1e3d24", "tab_close": "#5d8064",
        "plus_button": "#1e3d24", "button_bg": "#f2faf3",
        "button_border": "#7fa887", "button_glyph": "#1b5e20",
        "button_glyph_disabled": "#a3c2a8", "addr_bg": "#ffffff",
        "addr_border": "#7fa887", "addr_focus_border": "#1b5e20",
        "addr_text": "#12331a", "addr_placeholder": "#8caf92",
        "caret": "#12331a", "status_bg": "#e9f2ea",
        "status_border": "#c0d8c4", "status_text": "#3c6b44",
        "scroll_thumb": "#5f8a67", "accent": "#1b5e20",
        "log_bg": "#f0f5e9", "log_border": "#ccd8a8",
        "log_text": "#4a5a1f", "toe_btn_bg": "#d6e8d9",
        "popup_titlebar": "#cfe3d2", "popup_border": "#7fa887",
        "popup_text": "#1e3d24", "menu_bg": "#ffffff",
        "menu_border": "#7fa887", "menu_text": "#1e3d24",
        "menu_hover": "#1b5e20", "menu_sep": "#c0d8c4",
        "menu_shadow": "#a3c2a8", "menu_disabled": "#a3c2a8",
        "page_bg": "#ffffff",
        "page_text": "#12331a", "link_color": "#1b5e20",
    },
    "Sunset Heel": {
        "chrome_bg": "#fdf1ea", "tab_bar": "#f5dcc9",
        "tab_active": "#ffffff", "tab_inactive": "#eccdb4",
        "tab_text": "#5c2a12", "tab_close": "#a06a47",
        "plus_button": "#5c2a12", "button_bg": "#fff6f0",
        "button_border": "#d89a6a", "button_glyph": "#d84315",
        "button_glyph_disabled": "#e0b99b", "addr_bg": "#ffffff",
        "addr_border": "#d89a6a", "addr_focus_border": "#d84315",
        "addr_text": "#4a2008", "addr_placeholder": "#c59a7c",
        "caret": "#4a2008", "status_bg": "#fdf1ea",
        "status_border": "#eccdb4", "status_text": "#8a4a26",
        "scroll_thumb": "#c98a5b", "accent": "#d84315",
        "log_bg": "#fdf0e0", "log_border": "#e3c296",
        "log_text": "#7a4a10", "toe_btn_bg": "#f7e2d0",
        "popup_titlebar": "#f5dcc9", "popup_border": "#d89a6a",
        "popup_text": "#5c2a12", "menu_bg": "#ffffff",
        "menu_border": "#d89a6a", "menu_text": "#5c2a12",
        "menu_hover": "#d84315", "menu_sep": "#eccdb4",
        "menu_shadow": "#e0b99b", "menu_disabled": "#e0b99b",
        "page_bg": "#ffffff",
        "page_text": "#4a2008", "link_color": "#d84315",
    },
    "Candy High-Top": {
        "chrome_bg": "#fdf0f4", "tab_bar": "#f3d3e2",
        "tab_active": "#ffffff", "tab_inactive": "#e8bdd1",
        "tab_text": "#5c1240", "tab_close": "#9e5b82",
        "plus_button": "#5c1240", "button_bg": "#fff7fa",
        "button_border": "#d98bb2", "button_glyph": "#c2185b",
        "button_glyph_disabled": "#dfa6c1", "addr_bg": "#ffffff",
        "addr_border": "#d98bb2", "addr_focus_border": "#c2185b",
        "addr_text": "#47122f", "addr_placeholder": "#c98aa8",
        "caret": "#47122f", "status_bg": "#fdf0f4",
        "status_border": "#e8bdd1", "status_text": "#8a2a60",
        "scroll_thumb": "#c8789f", "accent": "#c2185b",
        "log_bg": "#fdf0e6", "log_border": "#e6c9a8",
        "log_text": "#7a4a20", "toe_btn_bg": "#f7dbea",
        "popup_titlebar": "#f3d3e2", "popup_border": "#d98bb2",
        "popup_text": "#5c1240", "menu_bg": "#ffffff",
        "menu_border": "#d98bb2", "menu_text": "#5c1240",
        "menu_hover": "#c2185b", "menu_sep": "#e8bdd1",
        "menu_shadow": "#dfa6c1", "menu_disabled": "#dfa6c1",
        "page_bg": "#ffffff",
        "page_text": "#47122f", "link_color": "#c2185b",
    },
}


def shoe_names():
    """Return the names of all built-in shoes, in display order."""
    return list(SHOES)


def find(name):
    """Case-insensitively resolve a shoe name to its canonical key."""
    if name in SHOES:
        return name
    for key in SHOES:
        if key.lower() == str(name).lower():
            return key
    return None


def resolve(name):
    """Return the palette for `name`, falling back to the default shoe."""
    return SHOES.get(name, SHOES[DEFAULT_SHOE])


def load():
    """Return the name of the saved shoe (or the default)."""
    try:
        with open(SHOES_FILE, encoding="utf8") as f:
            data = json.load(f)
        name = data.get("shoe") if isinstance(data, dict) else None
    except (OSError, ValueError):
        name = None
    if name not in SHOES:
        return DEFAULT_SHOE
    return name


def save(name):
    """Persist `name` as the active shoe."""
    try:
        with open(SHOES_FILE, "w", encoding="utf8") as f:
            json.dump({"shoe": name}, f, indent=2)
    except OSError:
        pass


def merge(palette):
    """Fill missing keys in `palette` from the default shoe."""
    base = SHOES[DEFAULT_SHOE]
    out = dict(base)
    out.update(palette or {})
    return out
