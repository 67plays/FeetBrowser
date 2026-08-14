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

**Runs on:** macOS, Linux and Windows, with a real window of its own on each
(`cocoa.py`, `x11.py` and `win32.py`, all ctypes and no toolkit), and
anywhere at all headless — `--screenshot` and the whole test suite need no
display. X11 covers Wayland desktops through XWayland; a native Wayland
backend is not written. On a platform with none of the three,
`gui.platform_root()` finds nothing and you get the headless root, so the
browser runs but does not open.

**Needs building:** the JavaScript engine is a Rust extension
(`feetbrowser_engine`), and there is no Python fallback, so a Rust toolchain
is a hard prerequisite on every platform. `run.sh` and `run.cmd` build it
into a local `.venv` on first run. Windows needs a second install that the
other two platforms do not: rustup selects the MSVC toolchain but does not
provide the linker it calls, and Windows ships no linker of its own, so the
first `run.cmd` stops at `error: linker \`link.exe\` not found` until the
Visual Studio build tools are installed. See [usage.md](usage.md) for the
exact download and the one workload to tick. Whether the GNU toolchain can
stand in for it here is untested and not claimed: official CPython is built
with MSVC, and MinGW does not read the import-library format that produces,
so MSVC is the only toolchain this has been built with.

**Windows, specifically:** the process asks for per-monitor-v2 DPI awareness,
so Windows hands over the real pixels rather than scaling a 96-DPI frame up.
Nothing scales the *page*, though, so one CSS pixel is one device pixel and a
site on a 200% display renders correspondingly small. A window dragged
between monitors of different scale is resized to the rectangle
`WM_DPICHANGED` supplies, so the frame and the client area stay in agreement,
but that path has never run anywhere except by reading: CI's runners are
headless and 96 DPI. There is no IME support for composed input, no
drag-and-drop, no printing, and no jump list or taskbar integration. `cargo` builds under a deep checkout can
run into the 260-character path limit; keep the repo somewhere short, or turn
long paths on.

**Doesn't (yet):** flexbox wrapping, `<textarea>`/`<select>` selection (beyond
read-only), or the full ECMAScript feature set (no getters/setters, no
generators, no ES modules, no `Proxy`). Shoes themes are preset solid-color
palettes only — there's no custom color editor, and page colors aren't themed
(only the browser chrome and the built-in pages). These are natural next
milestones — the architecture has clean seams for each.
