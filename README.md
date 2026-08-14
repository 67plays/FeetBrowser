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
./run.sh                 # opens the welcome page
./run.sh https://example.com
```

No GUI toolkit to install — just Python 3 and a system font. The one thing
that does get built is the JavaScript engine: `run.sh` compiles the Rust
extension (`feetbrowser_engine`) into a local `.venv` when it isn't
importable, so a first run needs the Rust toolchain (the script installs
`maturin` into the venv for you). Once the extension is built and installed
for the interpreter you're invoking, `python3 -m feetbrowser <url>` works
directly.

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
