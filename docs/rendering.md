# The rendering engine

FeetBrowser used to end at a Tk canvas: layout produced a display list, and Tk
turned it into pixels and answered the one question layout could not answer for
itself — how wide is this string in this font. That was the last piece of the
browser somebody else owned.

It is ours now. There is no GUI toolkit in the import graph: no Tk, no Qt, no
GTK, no SDL, no Cairo, no FreeType, no Pillow. The stack below turns glyph
outlines and paint commands into a framebuffer using nothing but the standard
library, and the only thing it asks of the operating system is a font file to
read.

```
layout.py            display list (paint commands, CSS px)
     |
canvas.py            retained scene graph  --  Tk canvas semantics
     |                 Font, PhotoImage, Canvas, create_*/delete/itemconfig
     |
raster.py            Surface: spans, scanline AA, glyph cache, PNG out
     |     \
fontengine.py         imagecodec.py
  TrueType tables      PNG / GIF / PNM -> RGBA
  outlines, metrics
```

`window.py` sits beside all of it and supplies what Tk supplied besides
drawing: a binding table using Tk's own sequence names, `after()` timers, and
a main loop. `gui.py` is the seam — a facade that picks a backend, so the old
Tk path is still selectable with `FEETBROWSER_BACKEND=tk` for comparison.

## fontengine.py — reading the font

A `.ttf`/`.ttc` file is a table directory. We parse the tables a text renderer
actually needs:

| table | what we take from it |
| --- | --- |
| `head` | `unitsPerEm`, the scale denominator for everything else |
| `hhea`, `OS/2` | ascent, descent, line gap |
| `hmtx` | per-glyph advance widths |
| `cmap` | character -> glyph id (formats 0, 4, 6 and 12) |
| `loca`, `glyf` | the outlines |
| `maxp` | glyph count, for bounds checks |
| `name` | family, subfamily; used to build the index |

Outlines are quadratic B-splines with a wrinkle: TrueType omits on-curve
points that fall exactly between two off-curve points, so the parser has to
put the implied midpoints back before the curve means anything. Composite
glyphs (an `é` assembled from `e` and `´`) recurse, with a depth cap and
`F2Dot14` transforms applied to the component's points.

`flatten()` subdivides each Bézier into line segments and flips the y axis, so
its output is polygons in pixel space relative to the baseline, ready for the
rasteriser. CFF/`OTTO` fonts are indexed and measured but not rasterised —
their outlines are cubic and in a different container, so a face made only of
CFF outlines is skipped when picking a face to draw with.

`index()` scans the platform font directories once per process and returns
`{family: {(bold, italic): (path, face)}}`. That single dict is what lets
`Font(family="Helvetica")` and glyph-level fallback both work.

## raster.py — turning outlines into pixels

A `Surface` is a `bytearray` of packed RGB and a clip rectangle. Nothing more.

The interesting part is that pure Python cannot afford a per-pixel inner loop,
so the hot paths are all expressed as operations on `bytes` objects, where the
work happens in C:

- **Opaque spans** are `self.pixels[o:o + span] = row`, one slice assignment
  per scanline — a `memcpy`.
- **Translucent spans** use three cached 256-byte translate tables (one per
  channel) and three strided `bytes.translate()` calls per scanline. This is
  the single biggest win in the whole engine: one full-page translucent
  rectangle went from 76.8 ms in a Python loop to under a millisecond.
- **Opaque images** drop the alpha channel with `del line[3::4]` on a
  bytearray copy of the row, then blit it as one slice.

Antialiasing is a scanline sampler: 4× vertical subsampling with analytic
horizontal coverage, accumulated with the nonzero winding rule — which is what
keeps the counter of an `o` open instead of filling it in. Coverage comes out
as an 8-bit mask, and blitting a mask is the same strided-translate trick with
a table per distinct (colour, alpha).

Glyph rasterisation is cached on `(face, size, glyph id)`, because a page of
text is a few dozen distinct glyphs drawn hundreds of times each. A warm cache
draws a 40×135 character screenful in 8.4 ms; a full page composite is 9.9 ms,
comfortably inside a frame.

`to_png()` writes the surface out through stdlib `zlib`, which is how
`--screenshot` works and how the renderer is tested.

## imagecodec.py — decoding what pages send

Everything returns `(width, height, rgba)`.

- **PNG**: all five colour types, bit depths 1/2/4/8/16, all five scanline
  filters including Paeth, `tRNS` transparency for palette / grey / truecolour,
  and Adam7 interlacing. `zlib` does the inflate; the rest is ours.
- **GIF**: a hand-written variable-width LZW decoder, global and local colour
  tables, transparency index, and interlacing. First frame only, which is what
  Tk did too.
- **PNM**: P1–P6, because it is four lines of code and makes tests easy.

Scaling is nearest-neighbour, matching the `subsample`/`zoom` semantics the
browser already relied on.

## canvas.py — the scene graph

This is the widest surface area, because it is the part everything above it
already talks to. It reproduces the Tk canvas contract the browser depends on:

- Items carry an integer id and a tag set, and paint in creation order.
- `delete(tag)` wipes a layer. The browser's layered repaint — page, chrome,
  selection, toe overlays, each under its own tag — works unchanged.
- `find_all()` returns ids in creation order, which is how the browser diffs
  before and after a toe's `on_draw` to tag whatever the toe created.
- Colours accept `#rgb`, `#rrggbb`, `#rrrrggggbbbb` and the 148 CSS names, and
  raise `TclError` on nonsense — the display list already catches that and
  falls back, so those paths keep working.

`Font` is where the two halves have to agree. `measure()` and `draw()` both
resolve each character through the same `face_for()` — which consults the
primary face, and on a miss walks a preferred fallback list and then the whole
font index looking for someone who has the glyph, memoising the answer per
character. Because both walk the identical per-character advance path, and
because we apply no kerning, `measure("abc") == measure("a") + measure("b") +
measure("c")` holds exactly, and painted text lands exactly where layout
measured it. That invariant is load-bearing: layout caches per-character
widths and sums them.

Glyph fallback is not a nicety. No single text face has the toolbar's `⟳`,
`⌂` and `☆`, and without fallback the chrome renders a row of `.notdef` boxes.

## window.py — events without a toolkit

`Window` is headless by design: bindings, a timer heap keyed by absolute
deadline, and a loop that drains them. That is the entire event model, and it
runs with no display at all — which is what makes `tests/test_render.py` and
the rest of the suite possible on a machine with no GUI. Handler exceptions are
reported and swallowed, matching Tk's report-and-continue, so one broken toe
cannot take down the browser.

Platform windows subclass it and add two things: a source of real input
(`poll_events()`) and somewhere to push the surface (`present()`). Everything
above only ever sees the Tk-shaped API, so adding a platform is additive.

## cocoa.py — a real window, still with no toolkit

Objective-C is a C library with a message dispatcher, so ctypes is enough to
drive AppKit: `objc_getClass`, `sel_registerName`, `objc_msgSend`. No PyObjC,
no compiled shim. Two details are not optional. Every call needs its signature
declared, because ctypes defaults a return type to `c_int` and silently
truncates a 64-bit pointer to a wild one — that shipped once, as a segfault on
the first frame. And a struct larger than 16 bytes comes back through
`objc_msgSend_stret` on x86_64 but plain `objc_msgSend` on arm64, so `NSRect`
returns pick the entry point by CPU.

Presenting costs no conversion: our RGB framebuffer is already a valid 24-bit
bitmap, so it goes straight into `CGImageCreate` through a data provider and
on to an `NSImageView`. The last couple of frames stay referenced because
AppKit draws asynchronously, and a frame whose canvas is not dirty is not
uploaded at all.

Input is the mirror image of that: Cocoa event types and virtual key codes
become `<Button-1>`, `<Control-l>`, `<MouseWheel>`. Two pieces of Tk behaviour
are reproduced deliberately, because the browser depends on both. A binding
fires when its modifiers are a *subset* of the ones held, which is what lets
`<Control-ISO_Left_Tab>` catch Control-Shift-Tab; and only the most specific
binding fires, so a window that bound `<Up>` and `<Key>` sees one keypress
once. Command maps to Tk's Control, because that is where a Mac user's muscle
memory puts it.

There is one event queue per *application*, not per window, so a module-level
registry maps each `NSWindow` back to its Python window and the root's loop
feeds any popups. `[NSApp sendEvent:]` runs before translation — without it the
close button, titlebar drag and live resize do not work.

## x11.py — the same idea on Linux

Xlib is a C library too, so this is ctypes again: `libX11.so.6` by soname,
`XOpenDisplay`, `XCreateSimpleWindow`, `XSelectInput`, `XMapWindow`, and a
pump over `XPending`/`XNextEvent`. No python-xlib, no compiled shim, and no
XCB — the same rule the rest of the repo lives by. XWayland means this covers
most Wayland desktops as well; a native Wayland backend is not written.

Translation is *lighter* here than on macOS, because Tk is an X11 program and
it shows. Tk's `event.state` bits are literally X's modifier mask, and Tk's
keysym names — `Return`, `Left`, `ISO_Left_Tab` — are X's keysym names. So
`XLookupString` (which applies the user's keyboard layout, and is the reason
a shifted `a` arrives as the keysym `A`) plus `XKeysymToString` is nearly the
whole job. Two habits of Tk's are added back: a printable key is named by its
character, and a modifier pressed on its own is not a keypress, which X thinks
it is. The binding rules themselves — which sequences a keypress tries, most
specific first — live in `window.py` and are shared with `cocoa.py` rather
than written twice. The wheel is buttons 4 and 5 on X, translated to
`<MouseWheel>` with a delta small enough that `browser.py` reads it as pixels.

Presenting is where X asks for real work. The server names its pixel format
rather than agreeing to ours, so the visual's `red_mask`/`green_mask`/
`blue_mask` and `XImageByteOrder` are read once at startup and turned into
byte offsets: a mask of `0x00FF0000` is byte 2 on an LSBFirst server and byte
1 on an MSBFirst one, and getting that backwards swaps red and blue on
exactly the machines nobody tests on. 24- and 32-bit TrueColor in either byte
order convert with three strided slice assignments over the whole frame;
depth 15 and 16 fall to a slower per-pixel path with a scaling table per
channel, so white stays white on five bits of red. A server whose format is
byte-for-byte ours — 24bpp, RGB order — gets the framebuffer handed to
`XPutImage` with no copy at all. Rows are padded to the server's
`scanline_pad`, which is invisible at a round width and shears the picture at
an odd one.

`XShmPutImage` is deliberately not used. Shared memory is faster and does not
exist over a network socket, and a browser that only works when the server is
on the same machine is a worse browser than a slightly slower one.

There is no close event in X: the window manager asks through a
`WM_DELETE_WINDOW` client message, and a client that ignores it gets killed
instead of asked. `XSetWMProtocols` opts in, and the message runs the same
`protocol()` handler Tk would. Xlib's default error handler calls `exit()`,
which would take the browser down over a stale window id, so ours records the
error and returns. As on macOS there is one event queue per *connection*, so
a module-level registry maps each window id back to its Python window and the
root's loop feeds any popups.

## win32.py — the same window, a different operating system

Win32 is a plain C API, so ctypes is enough again: load `user32`, `gdi32` and
`kernel32`, declare the signatures, call the functions. No pywin32. The
signatures are as non-optional here as they are on macOS and for the same
reason — a missing `restype` truncates a 64-bit `HWND` to a handle that
belongs to nobody.

Presenting costs one conversion, which is the one thing this backend does
that Cocoa's does not. A device-independent bitmap is BGR rather than RGB,
and its rows run bottom-up unless the height in the header is negative, so
the framebuffer cannot go to GDI untouched. It is converted to 32-bit BGRX
and pushed with `StretchDIBits` under a negative `biHeight`. **32bpp rather
than 24bpp is deliberate:** DIB rows are padded to a four-byte boundary, so a
24-bit frame whose width is not a multiple of four needs per-row padding and
smears diagonally down the window if you forget, while at 32bpp the stride is
always `width * 4` and the frame is one buffer with no row loop at all. The
conversion is three strided slice assignments, which run in C.

Per-monitor-v2 DPI awareness is set before the first window opens, falling
back through `SetProcessDpiAwareness` and `SetProcessDPIAware` on older
systems. Without it Windows renders the whole browser at 96 DPI and has the
compositor scale the result, which is a blurry browser on any display made
this decade. What it does *not* do is scale the page: one CSS pixel is one
device pixel, so text on a 200% display is sharp and small.

Input is where the two backends differ most, because Windows splits a
keypress in two. `WM_KEYDOWN` carries a virtual key code and `WM_CHAR`
carries the character the user's keyboard layout produced, so named keys and
anything held under Control or Alt are resolved from the virtual key — under
Control the character message carries a control code, `0x0C` rather than
`l` — and everything else waits for `WM_CHAR`, which is the only thing that
knows about the layout. A character outside the basic plane arrives as two
`WM_CHAR`s, one surrogate each. Modifiers are read from `GetKeyState` rather
than tracked, which keeps them right when the window loses focus with a key
held. The wheel is the one mouse message carrying *screen* coordinates.

There is one message queue per *thread*, so as on macOS a module-level
registry maps each `HWND` back to its Python window and one shared window
procedure routes each message to whichever window it belongs to. The
procedure itself is stored on the module, not on a window: a ctypes callback
that gets collected leaves Windows calling into freed memory.

Everything above that is arithmetic or a lookup table — the stride, the
colour conversion, the wheel scaling, both keysym tables — is a plain
module-level function, so the part that can only run on Windows is as small
as it can be made. Those functions are tested from `tests/test_units.py` on
whatever platform the suite is running on.

`gui.py` picks all of this up: `gui.Tk()` is always the headless root, so tests
and `--screenshot` never open anything, and only `gui.new_window()` asks for a
real one. Backends declare themselves in one table and answer `available()`
for themselves, so Cocoa is tried, then Win32, then X11, and the first that can
run wins — the order only matters between Cocoa and X11, since macOS is the one
system that can offer both and XQuartz there is a deliberate choice rather than
a default. `FEETBROWSER_DISPLAY=x11`, `=cocoa` or `=win32` demands one by name
and fails loudly rather than falling back to a headless root that renders a
black screenshot; `FEETBROWSER_DISPLAY=none` forces headless even where a
window is possible. All of that selects among *our* windows, so it has nothing
to say to `FEETBROWSER_BACKEND=tk`, which opens whatever tkinter opens. With no
display — no `$DISPLAY`, an X server that will not answer, or a platform with
no backend at all — the browser says which of those it was and renders headless
instead of raising.

## Testing it

`tests/test_render.py` covers the layers directly and offline: font metrics and
the additive-measure invariant, span fills and clipping, antialiased partial
coverage, nonzero winding, the glyph cache, PNG round-tripping, every PNG
filter type, Adam7, hand-built GIF LZW, scene-graph ordering and tag deletion,
and the timer and binding model.

The platform windows get their own suites, each of which opens real windows on
its own operating system and skips with a message everywhere else:
`tests/test_cocoa.py` on macOS and `tests/test_win32.py` on Windows. The
Win32 one blits through real GDI into a memory bitmap and reads the pixels
back, because red and blue swapping places or the rows coming out upside down
are exactly the mistakes a DIB lets you make quietly. CI runs both jobs.

The end-to-end check is a screenshot: `python3 -m feetbrowser --screenshot
<url> out.png` runs the real browser — chrome, tabs, toolbar, page, scrollbar —
settles the image loads, and writes a PNG. CI does this on every push, on both
platforms it can run on.

## Known gaps

- No CFF/Type 2 rasterisation, so `.otf`-only faces can be measured but not
  drawn; a face with no `glyf` is skipped when resolving.
- No hinting and no subpixel positioning. Glyphs are cached per integer size
  and positioned on whole pixels horizontally.
- No kerning or ligatures — deliberately, see the invariant above.
- No right-to-left or complex-script shaping. Characters advance
  left-to-right, one glyph each.
- Animated GIFs show their first frame.
- No native Wayland backend. Wayland desktops get the X11 window through
  XWayland, which is how nearly all of them run X clients today.
- X11 needs a TrueColor visual, which is every server since about 2005;
  PseudoColor and its colormaps are not implemented.
