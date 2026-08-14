# What it does and doesn't do

**Does:** fetch and render real websites over HTTPS, apply their CSS
(text styling, colors, backgrounds, layout), follow links, keep per-tab
history, submit forms (GET/POST), show page source, open links in new tabs,
run JavaScript (scripts on load, DOM reads/writes, click handlers, `Promise`
with microtasks, `async`/`await`, timers, `fetch`/`XMLHttpRequest`, and
`throw`/`try`/`catch`, with `console.log` surfaced in the page's log buffer),
manage extensions ("toes") from the built-in ToeHub — install, uninstall,
enable, and disable them without a restart — restyle the whole browser
with **Shoes** color themes (`about:shoes`, or `Ctrl+Shift+S`), and download
files to disk with a manager that shows progress and can cancel (`Ctrl-J`).

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
first `run.cmd` stops at ``error: linker `link.exe` not found`` until the
Visual Studio build tools are installed. See [usage.md](usage.md) for the
exact download and the one workload to tick.

The GNU toolchain is a real alternative rather than a dead end, which is worth
saying because the opposite is usually assumed: official CPython is built with
MSVC, but `pyo3-ffi` does not read MSVC's import libraries, it generates its
own with `dlltool`, so a `stable-x86_64-pc-windows-gnu` build produces an
extension that imports and passes the whole suite. It is not the cheaper road,
though. It needs MinGW-w64's binutils on `PATH`; without them the build gets
past the linker and stops at `error calling dlltool 'dlltool.exe': program not
found` instead. The `dlltool.exe` rustup itself installs beside the toolchain
does not stand in for them — it shells out to an assembler that ships in the
same package and fails without it. So the GNU route swaps one download for
another rather than removing one, and MSVC is what this project builds against
in CI. The GNU path was checked by experiment on a Windows runner, not here.

**Windows, specifically:** the process asks for per-monitor-v2 DPI awareness,
so Windows hands over the real pixels rather than scaling a 96-DPI frame up.
Nothing scales the *page*, though, so one CSS pixel is one device pixel and a
site on a 200% display renders correspondingly small. A window dragged
between monitors of different scale is resized to the rectangle
`WM_DPICHANGED` supplies, so the frame and the client area stay in agreement,
but that path has never run anywhere except by reading: CI's runners are
headless and 96 DPI. There is no IME support for composed input, no
drag-and-drop, no printing, and no jump list or taskbar integration. `cargo`
builds under a deep checkout can run into the 260-character path limit; keep
the repo somewhere short, or turn long paths on.

**Downloads, specifically:** files go to the platform's download directory
under the name the server suggests, made safe first; there is no save-as
dialog to put one somewhere else, because there is no file picker in a browser
that draws its own widgets. A transfer can be cancelled but not paused by
hand, though one the network interrupts is resumed with a `Range` request
where the server allows it. Nothing is opened after it is saved, and there is
no history of past downloads across runs: the list is what this session did.

**Doesn't (yet):** flexbox wrapping, `<textarea>`/`<select>` selection (beyond
read-only), or the full ECMAScript feature set (see below). Shoes themes are
preset solid-color palettes only — there's no custom color editor, and page
colors aren't themed (only the browser chrome and the built-in pages). These
are natural next milestones — the architecture has clean seams for each.

## Images

Four formats decode: PNG, GIF, JPEG and Netpbm. Nothing else does, and an
image we cannot read draws as its `alt` text rather than as an error or a
blank space. The decoders are ours, so this list does not change with what is
installed on the machine — it is the same on a fresh checkout as on a
workstation with every graphics library on it.

WebP is the loss that shows. It used to decode when Pillow happened to be
present, and Google in particular serves a great deal of it, so pages that
looked complete on some machines now show alt text on all of them. That is
deliberate: the alternative was a browser whose rendering depended on
somebody else's `pip install`. BMP, ICO and TIFF go the same way and are
rarely missed.

SVG does not render, and there is no plan for it to. A partial SVG renderer
draws wrong pictures rather than no picture, and a wrong picture is worse
than a placeholder because it looks like it worked;
`docs/dependencies.md` sets out the full argument and the line count.

Within JPEG the modes that are refused rather than approximated are
arithmetic coding, CMYK and YCCK, 12-bit samples, and the lossless and
hierarchical frame types. Progressive JPEG does decode. EXIF orientation is
ignored, so a photograph that relies on it appears rotated. Animated GIFs
show their first frame and do not move.

## The JavaScript engine

There are two, and `FEETBROWSER_JS` picks between them: `rust` (the default)
and `zig`. They share the same Python-facing API and the same test suite.
That variable and the rest of the environment are documented in
[usage](usage.md#environment-variables). What follows is what the Zig engine
leaves out; its design is written up in `docs/jszig.md`.

**Syntax it will not parse.** ES modules — `import`/`export` are reported as
"ES modules are not supported" rather than as a mystery syntax error, so a
page whose scripts are `type="module"` runs none of them. Also `with`,
generators (`function*`, `yield`), class static blocks, and `new.target`.

**Semantics that are missing rather than wrong.** No `Symbol`, and therefore
no `Symbol.iterator` protocol: `for...of` and spread work on arrays, strings,
`Map`, `Set` and `arguments` because the engine knows about those types, not
because an object can declare itself iterable. No `Proxy` and no `Reflect`.
No `eval`, and `new Function(body)` throws — the `Function` global exists so
that `instanceof` and prototype lookups work, but compiling text that arrives
as page data is a bigger security question than a browser at this stage
should be answering. `String.raw` and
tagged-template raw strings are cooked-only.

**Close but not exact.** `Date` is UTC throughout, so `getHours()` and
`getUTCHours()` agree and `getTimezoneOffset()` is always 0. Regular
expressions are a backtracking matcher over bytes: case-insensitive matching
folds ASCII only, and there are no lookbehind, named groups, or unicode
property escapes. `toUpperCase` and `toLowerCase` map per character across
ASCII, Latin-1, Latin Extended-A, Greek and Cyrillic, and leave other scripts
alone; the mappings that change a string's length (`ß` to `SS`) and the ones
that depend on position (Greek final sigma) are not done.
`Number.prototype.toFixed` rounds the double it is given
rather than the decimal a reader imagines, which is what most engines do but
not all of them. Sorting is stable.

**The DOM is smaller than the language.** The bridge exposes elements,
attributes, `classList`, inline styles, `querySelector`/`querySelectorAll`
(tag, class and id selectors only — no combinators), `matches`, `closest`,
`getElementsBy*`, `innerHTML`, `outerHTML`, `textContent`, document
fragments, node insertion and removal, events, timers, `fetch`,
`XMLHttpRequest`, `location`, `getComputedStyle`, and `localStorage`.
`createTextNode` returns a text-node wrapper, but the tree walks
(`childNodes`, `firstChild`) still see elements only; there are no
`Element`/`Node` constructor objects to hang polyfills on, and no CSSOM.
jQuery 1.8.2 parses, compiles and runs to completion against this — the whole
library, its feature detection included — and `jQuery("#id")`, `.text()` and
the traversal it drives all work. Its Sizzle half does not: the feature
detection that decides whether `querySelectorAll` is usable runs against a
detached element and fails here, so `jQuery(".class")` and `jQuery("li")`
select nothing even though `document.querySelectorAll` answers both correctly.
Modernizr and anything that measures a laid-out box do not run at all.

**Cycles across the boundary are not collected.** The engine's collector is a
precise mark-and-sweep over its own heap, and Python's is a reference count
plus its own cycle detector. A JS object that reaches a Python object that
reaches back is kept alive by both until the interpreter is dropped. Within
one page load that is a bounded amount of memory; it is why the interpreter
is discarded per navigation rather than reused.
