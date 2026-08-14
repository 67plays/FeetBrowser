# 🦶 FeetBrowser
*See the web from a new ankle*

A web browser written **from scratch**. No Chromium, no WebKit, no borrowed
libraries — it does its own networking, HTML parsing, CSS, layout, JavaScript,
fonts, and pixels. No GUI toolkit either: the TrueType parser, the antialiased
rasteriser and the image decoders are all in this repo. The JavaScript engine
and DOM bridge are compiled to a native Rust extension; everything else
(networking, parsing, layout, rendering, chrome) is Python, standard library
only.

## STRIDE — how code is judged

Every change should be a **stride forward**: one deliberate step, then iterate.
Code in this repo is evaluated on six principles:

- **S**imple — KISS + DRY: no repetition, no cognitive load
- **T**rue to spec — correctness against the web specs (HTTP/1.1, HTML tree-building, CSS cascade)
- **R**eadable — Clean Code + SOLID: modular, explicit, maintainable
- **I**terative — Agile + DevOps: small steps, continuous feedback, shared ownership
- **D**on't Repeat Yourself — no duplication
- **E**fficient — Unix + minimalism: one thing well, fewer resources

## Run it

```bash
./run.sh                 # macOS, Linux: opens the welcome page
./run.sh https://example.com
```

```bat
run.cmd                  :: Windows: the same script for cmd.exe
run.cmd https://example.com
```

No GUI toolkit to install — the window is ours too, straight ctypes into
AppKit on macOS and into user32/gdi32 on Windows. What you do need is Python
3, a system font, and a Rust toolchain, because the JavaScript engine is a
compiled extension and there is no Python fallback for it: `run.sh` and
`run.cmd` build `feetbrowser_engine` into a local `.venv` when it isn't
importable (installing `maturin` into the venv for you). Once the extension
is built and installed for the interpreter you're invoking, `python3 -m
feetbrowser <url>` works directly.

A real window opens on **macOS** and **Windows**. Elsewhere the browser runs
headless — `--screenshot` and the tests work everywhere.

The window itself is ours too. macOS gets one through AppKit and Linux gets
one through Xlib — both by ctypes, so there is nothing to install for either,
and X11 covers Wayland desktops through XWayland. Anywhere else, and anywhere
with no display, the browser still renders: `--screenshot` writes the page to
a PNG without opening anything.

To render a page to a PNG without opening a window:

```bash
./run.sh --screenshot https://example.com page.png
```

## What you can do

- Open tabs, back/forward, reload, bookmarks, history, and page source
- Fill in forms, follow links, search from the address bar
- Add extensions ("toes") — open **`toe://hub`** in the browser
- Restyle the browser with **Shoes** themes — open **`about:shoes`**
  (`Ctrl+Shift+S`)
- Keyboard shortcuts: `Ctrl-T` new tab, `Ctrl-L` focus address bar,
  `Ctrl-W` close tab, and more

## Learn more

- [Usage & shortcuts](docs/usage.md)
- [Architecture — how the engine works](docs/architecture.md)
- [The rendering engine — fonts, rasteriser, pixels](docs/rendering.md)
- [Extensions (Toes & ToeHub)](docs/toes.md)
- [What it does and doesn't do](docs/limitations.md)
- [Running the tests](docs/testing.md)
