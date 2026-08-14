# What it does and doesn't do

**Does:** fetch and render real websites over HTTPS, apply their CSS
(text styling, colors, backgrounds, layout), follow links, keep per-tab
history, submit forms (GET/POST), show page source, open links in new tabs,
run JavaScript (scripts on load, DOM reads/writes, click handlers, `Promise`
with microtasks, `async`/`await`, timers, `fetch`/`XMLHttpRequest`, and
`throw`/`try`/`catch`, with `console.log` surfaced in the page's log buffer),
manage extensions ("toes") from the built-in ToeHub — install, uninstall,
enable, and disable them without a restart — and restyle the whole browser
with **Shoes** color themes (`about:shoes`, or `Ctrl+Shift+S`).

**Runs on:** macOS and Windows, with a real window of their own on each
(`cocoa.py` and `win32.py`, both ctypes and no toolkit), and anywhere at all
headless — `--screenshot` and the whole test suite need no display. On Linux
and the BSDs there is no window yet: `gui.platform_root()` finds nothing and
you get the headless root, so the browser runs but does not open. An X11 or
Wayland backend is the same shape of work as the two that exist.

**Needs building:** the JavaScript engine is a Rust extension
(`feetbrowser_engine`), and there is no Python fallback, so a Rust toolchain
is a hard prerequisite on every platform. `run.sh` and `run.cmd` build it
into a local `.venv` on first run. On Windows that means the MSVC build tools
(the `stable-x86_64-pc-windows-msvc` toolchain rustup installs by default);
the GNU toolchain works too if that is what you already have, but do not mix
the two in one `.venv`.

**Windows, specifically:** the window is DPI-aware, which means it is sharp —
but nothing scales the *page*, so one CSS pixel is one device pixel and a
site on a 200% display renders correspondingly small. There is no
`WM_DPICHANGED` handling for dragging a window between monitors of different
scale, no IME support for composed input, no drag-and-drop, no printing, and
no jump list or taskbar integration. `cargo` builds under a deep checkout can
run into the 260-character path limit; keep the repo somewhere short, or turn
long paths on.

**Doesn't (yet):** flexbox wrapping, `<textarea>`/`<select>` selection (beyond
read-only), or the full ECMAScript feature set (no getters/setters, no
generators, no ES modules, no `Proxy`). Shoes themes are preset solid-color
palettes only — there's no custom color editor, and page colors aren't themed
(only the browser chrome and the built-in pages). These are natural next
milestones — the architecture has clean seams for each.
