# 🦶 FeetBrowser
*See the web from a new ankle*

A web browser written **from scratch**. No Chromium, no WebKit, no borrowed
libraries — it does its own networking, HTML parsing, CSS, layout, JavaScript,
and drawing. Tk is only the surface it paints on. The JavaScript engine and
DOM bridge are compiled to a native Rust extension; the rest (networking,
parsing, layout, rendering, chrome) is Python.

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

`run.sh` builds the Rust JS engine (`feetbrowser_engine`) into a local `.venv`
if it isn't importable — so a first run needs the Rust toolchain and
`maturin` (the script installs maturin into the venv for you). Need Tk? On
Debian/Ubuntu: `sudo apt install python3-tk` (other distros:
`python3-tkinter` on Fedora, `tk` on Arch).

If the extension is already built and installed for the interpreter you're
invoking, `python3 -m feetbrowser <url>` works directly.

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
- [Extensions (Toes & ToeHub)](docs/toes.md)
- [What it does and doesn't do](docs/limitations.md)
- [Running the tests](docs/testing.md)
