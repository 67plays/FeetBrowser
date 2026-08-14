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

`FEETBROWSER_DISPLAY` decides which one, and normally wants leaving alone; it
and the rest of the environment are described under [environment
variables](#environment-variables) below.

With no display at all — no `$DISPLAY`, no server answering, or a platform
with no backend — the browser says which of those it was and carries on
headless, where `--screenshot` still works.

On Windows, "a Rust toolchain" is two installs rather than one, and the
second is the one that catches people out. rustup selects the MSVC toolchain
by default (`stable-x86_64-pc-windows-msvc`), but selecting it is not the
same as having it: `rustc` compiles the code and then hands it to a C++
linker, and Windows does not ship one. Without it the first `run.cmd` gets a
long way — venv made, maturin installed, crates downloaded — and then stops
with

```
error: linker `link.exe` not found
```

Install **Build Tools for Visual Studio** from
<https://visualstudio.microsoft.com/downloads/> and tick the **Desktop
development with C++** workload in its installer. Ticking that workload is
the step that gets missed, and the Build Tools installed without it give the
identical error, so it is worth checking rather than assuming. A full Visual
Studio installation with the same workload does just as well. Open a new
command prompt afterwards so the linker is on `PATH`, then run `run.cmd`
again. `run.cmd` and `test.cmd` both say all of this themselves if the build
fails, so nobody has to find this page first.

If `python` isn't on your `PATH` — a fresh install from the Microsoft Store
often leaves only the launcher — use `py -3` in place of `python` in the two
commands `run.cmd` runs.

To render a page without opening a window:

```bash
./run.sh --screenshot https://example.com page.png
```

This runs the whole browser — chrome, tabs, toolbar, page, scrollbar — waits
for images to load, and writes a PNG.

## Environment variables

The browser reads four variables of its own, and none of them has to be set
for it to work: every one has a default that is the right answer on a normal
machine. They exist because the browser has two of several things — two
drawing backends, two window backends, two JavaScript engines — and a choice
that is only ever made at build time cannot be tested both ways. A fifth, the
standard `DISPLAY`, is not ours but decides whether the X11 window can open,
so it is described here too.

The four are read as text, stripped of surrounding whitespace and lowercased,
so `Zig` and ` zig ` are the same as `zig`. Each is read once and the choice
is then fixed for the life of the process; changing one from inside a running
browser does nothing.

### `FEETBROWSER_BACKEND`

Which drawing backend renders the browser, from `feetbrowser/gui.py`.

| value | effect |
| --- | --- |
| `raster` | our own font engine, rasteriser and event loop (the default) |
| `tk` | the original tkinter widgets |
| `auto` | `raster`, falling back to `tk` if the raster backend cannot start |

`auto` is for machines where the raster backend may have nothing to draw
with: it tries raster first and falls back if that raises, which in practice
means the font engine found no usable fonts. With `raster` the same machine
gets an error saying so, which is the more useful answer when you did not ask
for a fallback.

An unrecognised value is treated as `raster` rather than rejected. Only `tk`
and `auto` are tested for by name, and everything else takes the default
path, so a typo like `rastor` silently gets you the default backend.

`--screenshot` needs the raster backend and refuses to run under `tk`, saying
so, because only our own canvas can hand its pixels back.

The `tk` backend is on its way out — separate work removes it, after which
this variable will accept only the raster path. What is written here is what
the code in this branch does today.

### `FEETBROWSER_DISPLAY`

Which native window backend opens a window, also from `feetbrowser/gui.py`.
This is a separate question from `FEETBROWSER_BACKEND`: that one picks what
draws, this one picks what the drawing is shown in, and neither implies the
other.

| value | effect |
| --- | --- |
| unset or empty | try Cocoa, then Win32, then X11, and take the first that works (the default) |
| `cocoa`, `macos`, `darwin` | demand the macOS window; fail loudly if there is none |
| `win32`, `windows` | demand the Windows window; fail loudly if there is none |
| `x11`, `linux`, `xorg` | demand the X11 window; fail loudly if there is none |
| `none` | stay headless even where a window is possible |

The order of the first row costs nothing to get right and would be confusing
to get wrong: no machine offers Win32 alongside either of the others, so its
position only matters on paper. Cocoa is ahead of X11 for a real reason,
which is that macOS with XQuartz installed has both and the Mac window is the
one you meant.

Naming a backend that cannot run here is an error rather than a quiet
fallback, and it is reported as a sentence rather than a traceback. Silently
handing back a headless root is how you end up with an empty screenshot and
no idea why.

An unrecognised value behaves differently from an unrecognised backend name,
and less helpfully: it matches no backend, so every backend is skipped and
the browser runs headless without complaining. `FEETBROWSER_DISPLAY=wayland`
therefore opens no window and says nothing about it.

### `FEETBROWSER_JS`

Which JavaScript engine runs the page's scripts, from
`feetbrowser/jsengine.py`.

| value | effect |
| --- | --- |
| `rust` | the `feetbrowser_engine` extension module (the default) |
| `zig` | our own engine, a dynamic library loaded with `ctypes` |

Only `zig` is tested for by name; every other value, recognised or not, gets
the Rust engine. The two are held to the same test suite rather than being a
primary and a fallback — see [the JavaScript engine](limitations.md#the-javascript-engine)
for what the Zig one leaves out.

The choice is resolved the first time something asks for an interpreter, not
at import, so importing the browser neither builds nor loads an engine.

`run.sh` reads this variable too, and it changes what the script has to
build: the default path builds the Rust extension into a local `.venv` and
runs the browser from there, while `FEETBROWSER_JS=zig` runs `zig build` and
then the system `python3`, needing no venv and no extension module.

`run.cmd` does not read it. It builds the Rust extension and starts the
browser from the venv, which is the default either way. Nothing here has
ever built the Zig engine on Windows — CI builds it on the Linux jobs only —
so rather than have the Windows script offer a path nobody has walked, it
offers the one that is tested.

### `FEETBROWSER_JS_LIB`

Where the Zig engine's shared library is, from `feetbrowser/jszig.py`. It is
only consulted when that engine is the one selected.

Unset or empty, the library is looked for next to the sources it is built
from — `zig/zig-out/lib/libfeetjs.so`, or `libfeetjs.dylib` on macOS and
`feetjs.dll` on Windows — which is exactly where `zig build` puts it. Set, it
is used as the path verbatim, for running against a library built somewhere
else.

A path that does not exist is an error either way, and the message names the
missing file and suggests running `zig build` or falling back to
`FEETBROWSER_JS=rust`. There is no search of the system library path.

### `DISPLAY`

Not ours, but read: the X11 backend needs the standard X11 variable to find a
server. With it unset the backend reports that there is no server to draw on,
which is the reason `FEETBROWSER_DISPLAY=x11` fails on a machine with no X
session. Under the default `FEETBROWSER_DISPLAY` this is simply one of the
ways the browser ends up headless.

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
