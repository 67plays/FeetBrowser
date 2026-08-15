# 🦶 Toes: the extension framework

FeetBrowser ships with **no toes by default**. The framework is built in;
the toes are opt-in.

To install toes, open the browser and visit **`toe://hub`** (or `toehub://`).
The ToeHub pulls a catalog from the official toe repository
([xplosivex/feetbrowser-toes](https://github.com/xplosivex/feetbrowser-toes))
and lets you **install, uninstall, enable, and disable** toes from inside the
browser, no restart needed.

## Anatomy of a toe

A toe is a plain Python module in its own folder:

```
toes/name-of-your-toe/
    toe.json     # { "name", "version", "description", "entry" }
    toe.py       # the code, exposing activate(ctx)
```

`toe.json` is read at discovery; `toe.py` is imported and its `activate(ctx)`
is called once with a `Context`. Register whatever hooks you care about; the
rest are optional. A toe that raises while loading is skipped with a warning;
one bad toe never bricks the browser.

## Hooks

| Hook | Called when | Return |
|------|-------------|--------|
| `on_load(url, body)` | before the HTML is parsed | rewritten `body`, or `None` |
| `extra_css(url)` | gathering stylesheets (after the UA sheet) | CSS text, or `None` |
| `handle(url, tab)` | a navigation starts (before fetching) | `(headers, body, content_type)` to take over, or `None` |
| `on_draw(canvas, offset)` | each repaint, after the page | None |
| `buttons()` | building the toolbar | list of `toes.ButtonDef(id, glyph)` |
| `on_click(button_id)` | a toe toolbar button is clicked | None |
| `on_keypress(event)` | a key is pressed (no address-bar focus) | `True` to swallow |
| `on_motion(x, y)` | the mouse moved over the page | None |
| `on_new_tab()` | a new tab is created | None |
| `chrome_bands()` | declare chrome bands above the tabs | `[(id, height), ...]` |
| `on_chrome_draw(canvas, bands)` | paint the toe's chrome bands | None |
| `on_chrome_click(x, y, bands)` | a click in the band region | `True` to consume |

Helpers on the context: `ctx.current_tab()`, `ctx.tabs()`, `ctx.set_status(msg)`,
`ctx.open(url)`, `ctx.popup(url, width, height)`, and
`ctx.settings` / `ctx.save_settings()` (per-toe persisted settings).

## Managing toes

**From the browser**: `toe://hub`:
- install / uninstall any toe from the catalog
- enable / disable installed toes (disabled toes stay installed but no hooks fire)

**From the CLI:**
```bash
python3 -m feetbrowser --toes                 # list installed toes + status
python3 -m feetbrowser --toe-search <term>    # search the catalog
python3 -m feetbrowser --toe-install <name>   # install a toe
python3 -m feetbrowser --toe-uninstall <name> # uninstall a toe
python3 -m feetbrowser --toe-enable <name>    # enable a disabled toe
python3 -m feetbrowser --toe-disable <name>   # disable an installed toe
python3 -m feetbrowser --new-toe <name>       # scaffold a new toe
python3 -m feetbrowser --toe-docs             # generate a markdown reference
```

Install state (enabled/disabled) lives in `toes/config.json`, which is
gitignored. Installed toes themselves live under `toes/` and are never
committed.

## The catalog

The ToeHub reads `index.json` from the configured toe repository (default:
`https://raw.githubusercontent.com/xplosivex/feetbrowser-toes/main/index.json`).
Add your own toes by forking that repo and following its README.
