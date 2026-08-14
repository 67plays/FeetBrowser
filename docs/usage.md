# Usage

## Running

```bash
./run.sh                 # opens the welcome page
./run.sh https://example.com
./run.sh view-source:https://example.com
```

On Windows, `run.cmd` is the same script for `cmd.exe`:

```bat
run.cmd
run.cmd https://example.com
```

Either script builds the Rust JS engine (`feetbrowser_engine`) into a local
`.venv` with maturin if it isn't importable yet, then runs the browser from
that venv (so a first run needs the Rust toolchain; maturin is installed into
the venv automatically). Once the extension is built and installed for your
interpreter, `python3 -m feetbrowser <url>` works directly.

There is nothing else to install. The renderer is ours (see [the rendering
engine](rendering.md)), so no GUI toolkit is needed — only Python 3, a Rust
toolchain for that one extension, and at least one system font. The window is
ours as well: AppKit on macOS, Xlib on Linux and on Wayland desktops through
XWayland, and user32/gdi32 on Windows, all reached by ctypes with no bindings
package in between.

`FEETBROWSER_DISPLAY` decides which one, and normally wants leaving alone:

| value | effect |
| --- | --- |
| unset | whichever backend this machine has |
| `x11` | demand the X11 window; fail loudly if there is none |
| `cocoa` | demand the macOS window; fail loudly if there is none |
| `win32` | demand the Windows window; fail loudly if there is none |
| `none` | stay headless even where a window is possible |

With no display at all — no `$DISPLAY`, no server answering, or a platform
with no backend — the browser says which of those it was and carries on
headless, where `--screenshot` still works.

On Windows the Rust toolchain wants the MSVC build tools, which is what
rustup installs by default (`stable-x86_64-pc-windows-msvc`). If `python`
isn't on your `PATH` — a fresh install from the Microsoft Store often leaves
only the launcher — use `py -3` in place of `python` in the two commands
`run.cmd` runs.

To render a page without opening a window:

```bash
./run.sh --screenshot https://example.com page.png
```

This runs the whole browser — chrome, tabs, toolbar, page, scrollbar — waits
for images to load, and writes a PNG.

## Keyboard shortcuts

| Key | Action | Key | Action |
|-----|--------|-----|--------|
| `Ctrl-L` | focus address bar | `Ctrl-T` | new tab |
| `Ctrl-W` | close tab | `Ctrl-R` | reload |
| `Ctrl-D` | toggle bookmark | `about:bookmarks` | open bookmarks page |
| `Ctrl-H` | open `about:history` | `Ctrl-Tab` / `Ctrl-Shift-Tab` | next / previous tab |
| `PgUp` / `PgDn` / `Home` / `End` | page scroll controls | `Alt-←` / `Alt-→` | back / forward |
| `↑` / `↓` / wheel | scroll | `Esc` | blur address / input |
| middle / `Ctrl`-click | open link in new tab | `Ctrl-PgUp/Dn` | cycle tabs |

Type a URL in the address bar and press Enter, or type words to search
(DuckDuckGo HTML). Bare hosts without a scheme (`example.com:8080`,
`localhost:8000`) are assumed to be `https://`.

## Forms

Basic form support is wired up: `input[type=text/password]` fields are
focusable and typeable, checkboxes toggle, and submitting a form (clicking a
submit button or pressing Enter in a field) sends `GET` or `POST` to the form
`action`, which is resolved against the document's `<base href>` when one is
present.

## CLI reference

```bash
python3 -m feetbrowser --help             # full CLI reference
python3 -m feetbrowser --version          # print the version
python3 -m feetbrowser --toes                 # installed toes + status
python3 -m feetbrowser --toe-search <term>    # search the catalog
python3 -m feetbrowser --toe-install <name>   # install a toe
python3 -m feetbrowser --toe-uninstall <name> # uninstall a toe
python3 -m feetbrowser --toe-enable <name>    # enable a disabled toe
python3 -m feetbrowser --toe-disable <name>   # disable an installed toe
```
