# The rendering engine

FeetBrowser used to end at a Tk canvas: layout produced a display list, and Tk
turned it into pixels and answered the one question layout could not answer for
itself — how wide is this string in this font. That was the last piece of the
browser somebody else owned.

It is ours now. There is no GUI toolkit in the import graph: no Tk, no Qt, no
GTK, no SDL, no Cairo, no FreeType, no Pillow. The stack below turns glyph
outlines and paint commands into a framebuffer using nothing but our own code,
and the only thing it asks of the operating system is a font file to read.

```
layout.py            display list (paint commands, CSS px)
     |
canvas.py            retained scene graph, composited on demand
     |                 Font, PhotoImage, Canvas, create_*/delete/itemconfig
     |
raster.py            Surface: spans, scanline AA, glyph cache, PNG out
     |     \            -> rust/src/raster.rs
     |      \
fontengine.py         imagecodec.py
  discovery, index      PNG / GIF / JPEG / PNM -> RGBA
  -> rust/src/font.rs   -> rust/src/image.rs
```

The three layers under the scene graph are the ones that touch every pixel of
every frame, and they are compiled: they live in the same `feetbrowser_engine`
extension as the JS engine, in `rust/src/raster.rs`, `rust/src/font.rs` and
`rust/src/image.rs`. The Python modules that name them are shims — same
functions, same arguments, same exceptions — because everything above them was
written against that API and none of it had to change.

The rule at that boundary is that a page must never be able to make Rust
panic: a panic crosses into Python as `PanicException` and takes the whole
page load with it, which is a worse failure than the `IndexError` it replaced.
So every read of page-supplied data goes through a bounds-checked accessor and
every coordinate is saturating. `rust/src/pyutil.rs` holds the conversions
that keep the Python semantics the rest of the browser relies on: `int()`
truncating towards zero rather than flooring, bytes borrowed rather than
copied, and strings read as code points so a lone surrogate from a numeric
character reference measures as zero width instead of raising.

One thing above the scene graph is compiled for the same reason: the CSS
cascade, in `rust/src/css.rs`. It is not a rendering layer, but it runs once
per node per candidate rule, and on a long article that came to more time than
everything on this page put together. The split is the same as elsewhere —
`cssparser.py` still parses the stylesheet, and its selector objects are plain
Python data that the matcher compiles on first use. The rules list is compiled
once and cached against its identity, so a script that mutates the DOM
re-cascades without re-reading the stylesheet.

`window.py` sits beside all of it and supplies everything that is not
drawing: a binding table keyed by event sequence names, `after()` timers, and
a main loop. `gui.py` is what is left of the seam — it decides which *native*
window the frames are put in, and nothing else.

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

The parser is `rust/src/font.rs`; discovery stays in `fontengine.py`, because
walking directories and deciding what counts as a font directory on this
platform is policy, it happens once, and it is easier to read in Python. A
font file is the least trustworthy input in the browser after the network —
every offset in it is a number some other program wrote — so the whole parser
treats a table that runs off the end as a table that is absent. Where the
Python this replaced leaned on a slice quietly clamping, the Rust clamps on
purpose and says so in a comment, because the two are only the same by
accident: a `name` record whose length overruns its table still names the
font, and dropping it would have changed which family a page resolves to.

## raster.py — turning outlines into pixels

A `Surface` is a run of packed RGB bytes and a clip rectangle. Nothing more.
The buffer belongs to Rust, and `surface.pixels` is a read-only memoryview
onto it, so presenting a frame costs no copy: the window backend hands that
same memory to `CGImageCreate`.

Every drawing method starts by clipping its rectangle into byte offsets and
gives up if nothing survives, which is what makes the loops underneath safe to
write as plain indexing on a page's coordinates. Beyond that the shapes are
what they sound like:

- **Opaque spans** are a fill or a copy per scanline.
- **Translucent spans** blend per channel, `(dst * (255 - a) + src * a) // 255`
  — the same integer arithmetic the Python did, because it decides the exact
  byte in every antialiased edge on the page.
- **Opaque images** take the strided-copy path: `blit_rgba` is told when every
  alpha byte is 255, and then the inner loop does no arithmetic at all. That
  is the difference between a photo costing microseconds and milliseconds.

The translucent fill used to have two Python implementations racing under it:
a 256-entry translate table per channel, and, on Linux/x86-64, a call into the
hand-written span kernels in `asmblend.py` once per row. Both were answers to
the same question — how do you blend a run of bytes without a Python loop —
and the Rust fill answers it by crossing the boundary once for the whole
rectangle rather than once per row, so neither is on this path any more. The
kernels are still there and still tested (`tests/test_asmblend.py` checks them
against their Python references); nothing in the browser calls them now. One
detail worth carrying forward: the assembly rounded by `>> 8` where the tables
and the Rust round by `// 255`, so a translucent fill could land one level
darker at the top of the range on Linux/x86-64 and nowhere else.

Antialiasing is a scanline sampler: 4× vertical subsampling with analytic
horizontal coverage, accumulated with the nonzero winding rule — which is what
keeps the counter of an `o` open instead of filling it in. Coverage comes out
as an 8-bit mask, and blitting a mask is one blend per covered pixel.

Glyph rasterisation is cached on `(size, glyph id)`, held by the face itself,
because a page of text is a few dozen distinct glyphs drawn hundreds of times
each. Keying a shared cache on the font's address instead would let a
collected face hand its address — and its glyph shapes — to the next face
allocated there. A warm cache draws a 40×135 character screenful in 0.97 ms,
against 8.4 ms when the same loop was Python.

`to_png()` prefixes each row with a zero filter byte and compresses through
stdlib `zlib` at level 6. That last step deliberately stayed in Python: the
compressor is C already, and using the same one keeps a `--screenshot` PNG
byte-identical to the ones the test corpus was captured with.

## imagecodec.py — decoding what pages send

Everything returns `(width, height, rgba)`.

- **PNG**: all five colour types, bit depths 1/2/4/8/16, all five scanline
  filters including Paeth, `tRNS` transparency for palette / grey / truecolour,
  and Adam7 interlacing. An inflate with a ceiling on it does the
  decompression; the rest is ours.
- **GIF**: a hand-written variable-width LZW decoder, global and local colour
  tables, transparency index, and interlacing. First frame only: an animated
  GIF shows its first frame and does not move.
- **JPEG**: Huffman-coded 8-bit frames, baseline (SOF0), extended sequential
  (SOF1) and progressive (SOF2), one component or three, any sampling factors
  the file declares, and restart intervals. The inverse transform is the AAN
  one libjpeg calls `jidctflt`, with the scale factors folded into the
  dequantisation table so the per-block cost is the transform and nothing
  else; halved chroma is reconstructed with libjpeg's triangle filter rather
  than by repeating samples, which on our own fixtures is the difference
  between agreeing with libjpeg to within a level or two and being 87 levels
  out along a hard edge.
- **PNM**: P1–P6, because it is four lines of code and makes tests easy.

What JPEG does not do it says so about, and the image draws as its alt text:
arithmetic coding, CMYK and YCCK, 12-bit samples, lossless and hierarchical
frames, and any component count other than one or three. Those are the modes a
decoder cannot approximate — guessing at them produces a picture that looks
like a picture and is wrong, which is worse than no picture at all. A JPEG cut
short mid-scan is not in that list: it keeps the blocks that arrived, the same
way a truncated PNG keeps its rows.

The decoder was written against libjpeg's output rather than against its own.
Over 77 JPEGs pulled off the web, decoded both here and through libjpeg, the
largest per-channel difference is 3 and the largest mean difference is 0.040 —
the margin two conforming inverse transforms are entitled to disagree by. That
comparison is not in the suite, because keeping it there would mean keeping
libjpeg installed to run the tests, which is the thing this decoder exists to
stop. What the suite carries instead is the result of it: `test_units.py` pins
four exact pixel values it produced, and requires the progressive and
restart-marker fixtures to decode byte-identically to the baseline one, since
all three are the same photograph coded three ways. An 800x600 photograph
decodes in about 6.5 ms.

Scaling is nearest-neighbour, matching the `subsample`/`zoom` semantics the
browser already relied on.

This is the code in the browser most likely to be handed something written
specifically to break it, so two limits are hard-coded and enforced before any
allocation: `MAX_PIXELS` (20 million) rejects a header claiming a billion-pixel
image, and `MAX_INFLATED` bounds what a compressed stream is allowed to expand
into, so a few kilobytes of IDAT cannot ask for a gigabyte of memory. Short
input is not an error, though: a connection dropping mid-image is ordinary, so
a truncated stream keeps whatever inflated and the rows that never arrived
stay background, exactly as the Python decompressobj behaved.

The decoders are exercised deliberately badly. `tests/test_render.py` feeds
them truncated chunks, bad CRCs, headers no decoder can honour, absurd
dimensions, compressed data that expands without end, LZW code sizes that
cannot exist, Huffman tables naming symbols no 8-bit frame can use, and a few
thousand rounds of randomly corrupted real images, and asserts `ImageError`
rather than a crash — a distinction that matters more
now, because the layer
that used to raise `IndexError` in Python would panic here, and a panic
crossing the extension boundary would kill the page load rather than the
image.

## canvas.py — the scene graph

This is the widest surface area, because it is the part everything above it
already talks to. The contract it keeps is:

- Items carry an integer id and a tag set, and paint in creation order.
- `delete(tag)` wipes a layer. The browser's layered repaint — page, chrome,
  selection, toe overlays, each under its own tag — works unchanged.
- `find_all()` returns ids in creation order, which is how the browser diffs
  before and after a toe's `on_draw` to tag whatever the toe created.
- Colours accept `#rgb`, `#rrggbb`, `#rrrrggggbbbb` and the 148 CSS names, and
  raise `CanvasError` on nonsense — every `execute` in the display list
  catches that and paints in black rather than dropping the box.

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
reported and swallowed rather than propagated, so one broken toe cannot take
down the browser.

Platform windows subclass it and add two things: a source of real input
(`poll_events()`) and somewhere to push the surface (`present()`). Everything
above only ever sees `Window`'s own API, so adding a platform is additive.

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
become `<Button-1>`, `<Control-l>`, `<MouseWheel>`. Two binding rules matter
here, because the browser depends on both. A binding fires when its modifiers
are a *subset* of the ones held, which is what lets `<Control-ISO_Left_Tab>`
catch Control-Shift-Tab; and only the most specific binding fires, so a window
that bound `<Up>` and `<Key>` sees one keypress once. Command arrives as
`Control`, because that is where a Mac user's muscle memory puts it.

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

Translation is *lighter* here than on macOS, and the reason is historical:
the event vocabulary in `window.py` was inherited from a toolkit that was
itself an X11 program, so `event.state` is literally X's modifier mask and the
keysym names — `Return`, `Left`, `ISO_Left_Tab` — are X's keysym names. So
`XLookupString` (which applies the user's keyboard layout, and is the reason
a shifted `a` arrives as the keysym `A`) plus `XKeysymToString` is nearly the
whole job. Two conventions have to be added back on top: a printable key is
named by its character, and a modifier pressed on its own is not a keypress,
which X thinks it is. The binding rules themselves — which sequences a keypress tries, most
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
instead of asked. `XSetWMProtocols` opts in, and the message runs the
window's `protocol()` handler. Xlib's default error handler calls `exit()`,
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

`gui.py` picks all of this up, and this is now all it does. `window.Tk()` is
always the headless root, so tests and `--screenshot` never open anything, and
only `gui.new_window()` asks for a real one. Backends declare themselves in
one table and answer `available()` for themselves, so Cocoa is tried, then
Win32, then X11, and the first that can run wins — the order only matters
between Cocoa and X11, since macOS is the one system that can offer both and
XQuartz there is a deliberate choice rather than a default.
`FEETBROWSER_DISPLAY=x11`, `=cocoa` or `=win32` demands one by name and fails
loudly rather than falling back to a headless root that renders a black
screenshot; `FEETBROWSER_DISPLAY=none` forces headless even where a window is
possible. With no display — no `$DISPLAY`, an X server that will not answer,
or a platform with no backend at all — the browser says which of those it was
and renders headless instead of raising.

## Testing it

`tests/test_render.py` covers the layers directly and offline: font metrics and
the additive-measure invariant, span fills and clipping, antialiased partial
coverage, nonzero winding, the glyph cache, PNG round-tripping, every PNG
filter type, Adam7, hand-built GIF LZW, JPEG against corrupted photographs,
scene-graph ordering and tag deletion, and the timer and binding model.

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

It is also the check that made moving this layer into Rust safe to do. A
rewrite of a rasteriser is only correct if it produces the same bytes, so the
corpus was captured before the port and compared byte for byte after each
step, and `tests/bench_render.py` prints the timings the two versions are
argued about with.

## Known gaps

- No CFF/Type 2 rasterisation, so `.otf`-only faces can be measured but not
  drawn; a face with no `glyf` is skipped when resolving.
- No hinting and no subpixel positioning. Glyphs are cached per integer size
  and positioned on whole pixels horizontally.
- No kerning or ligatures — deliberately, see the invariant above.
- No right-to-left or complex-script shaping. Characters advance
  left-to-right, one glyph each.
- Animated GIFs show their first frame.
- No SVG, and no WebP, BMP, ICO or TIFF. Those draw as their alt text.
- The JPEG modes listed above — arithmetic coding, CMYK, 12-bit, lossless,
  hierarchical — are refused rather than approximated.
- No native Wayland backend. Wayland desktops get the X11 window through
  XWayland, which is how nearly all of them run X clients today.
- X11 needs a TrueColor visual, which is every server since about 2005;
  PseudoColor and its colormaps are not implemented.
