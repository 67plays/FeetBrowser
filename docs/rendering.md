# The rendering engine

FeetBrowser used to end at a Tk canvas: layout produced a display list, and Tk
turned it into pixels and answered the one question layout could not answer for
itself: how wide is this string in this font. That was the last piece of the
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
`rust/src/image.rs`. The Python modules that name them are shims (same
functions, same arguments, same exceptions), because everything above them was
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
everything on this page put together. The split is the same as elsewhere:
`cssparser.py` still parses the stylesheet, and its selector objects are plain
Python data that the matcher compiles on first use. The rules list is compiled
once and cached against its identity, so a script that mutates the DOM
re-cascades without re-reading the stylesheet.

`doormat` sits beside all of it and supplies everything that is not drawing:
a binding table keyed by event sequence names, `after()` timers, a main loop,
and the native windows under those. It is a package of its own, it is ctypes
and nothing else, and it produces no pixels; what it asks of this stack is
[below](#the-window-what-doormat-asks-of-this-stack). `gui.py` is our side of
that seam: it decides which *native* window the frames are put in, and
nothing else.

## fontengine.py: reading the font

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
rasteriser. CFF/`OTTO` fonts are indexed and measured but not rasterised:
their outlines are cubic and in a different container, so a face made only of
CFF outlines is skipped when picking a face to draw with.

`index()` scans the platform font directories once per process and returns
`{family: {(bold, italic): (path, face)}}`. That single dict is what lets
`Font(family="Helvetica")` and glyph-level fallback both work.

The parser is `rust/src/font.rs`; discovery stays in `fontengine.py`, because
walking directories and deciding what counts as a font directory on this
platform is policy, it happens once, and it is easier to read in Python. A
font file is the least trustworthy input in the browser after the network (
every offset in it is a number some other program wrote), so the whole parser
treats a table that runs off the end as a table that is absent. Where the
Python this replaced leaned on a slice quietly clamping, the Rust clamps on
purpose and says so in a comment, because the two are only the same by
accident: a `name` record whose length overruns its table still names the
font, and dropping it would have changed which family a page resolves to.

## raster.py: turning outlines into pixels

A `Surface` is a run of packed RGB bytes and a clip rectangle. Nothing more.
The buffer belongs to Rust, and `surface.pixels` is a read-only memoryview
onto it, so presenting a frame costs no copy: doormat's Cocoa backend hands
that same memory to `CGImageCreate`.

Every drawing method starts by clipping its rectangle into byte offsets and
gives up if nothing survives, which is what makes the loops underneath safe to
write as plain indexing on a page's coordinates. Beyond that the shapes are
what they sound like:

- **Opaque spans** are a fill or a copy per scanline.
- **Translucent spans** blend per channel, `(dst * (255 - a) + src * a) // 255`
  (the same integer arithmetic the Python did, because it decides the exact
  byte in every antialiased edge on the page.
- **Opaque images** take the strided-copy path: `blit_rgba` is told when every
  alpha byte is 255, and then the inner loop does no arithmetic at all. That
  is the difference between a photo costing microseconds and milliseconds.

The translucent fill used to have two Python implementations racing under it:
a 256-entry translate table per channel, and, on Linux/x86-64, a call into a
hand-written span kernel once per row. Both were answers to the same question
(how do you blend a run of bytes without a Python loop), and the Rust fill
answers it by crossing the boundary once for the whole rectangle rather than
once per row, so neither is on this path any more.

Antialiasing is a scanline sampler: 4× vertical subsampling with analytic
horizontal coverage, accumulated with the nonzero winding rule, which is what
keeps the counter of an `o` open instead of filling it in. Coverage comes out
as an 8-bit mask, and blitting a mask is one blend per covered pixel.

Glyph rasterisation is cached on `(size, glyph id)`, held by the face itself,
because a page of text is a few dozen distinct glyphs drawn hundreds of times
each. Keying a shared cache on the font's address instead would let a
collected face hand its address (and its glyph shapes) to the next face
allocated there. A warm cache draws a 40×135 character screenful in 0.97 ms,
against 8.4 ms when the same loop was Python.

`to_png()` prefixes each row with a zero filter byte and compresses through
stdlib `zlib` at level 6. That last step deliberately stayed in Python: the
compressor is C already, and using the same one keeps a `--screenshot` PNG
byte-identical to the ones the test corpus was captured with.

## imagecodec.py: decoding what pages send

Everything returns `(width, height, rgba)`.

- **PNG**: all five colour types, bit depths 1/2/4/8/16, all five scanline
  filters including Paeth, `tRNS` transparency for palette / grey / truecolour,
  and Adam7 interlacing. An inflate with a ceiling on it does the
  decompression; the rest is ours.
- **GIF**: a hand-written variable-width LZW decoder, global and local colour
  tables, transparency index, and interlacing. Animation as well: every frame
  is composited onto the logical screen with the disposal method the file
  asked for (leave it, clear it back to transparent, put back what was
  underneath), and the NETSCAPE2.0 loop count is read. `decode` still hands
  back one frame, because most callers want a picture; `decode_gif_frames`
  hands back all of them with their delays, and `canvas.PhotoImage` is what
  turns that into an animation. See
  [Animation](#animation-lives-in-photoimage-not-in-layout) below.
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
decoder cannot approximate: guessing at them produces a picture that looks
like a picture and is wrong, which is worse than no picture at all. A JPEG cut
short mid-scan is not in that list: it keeps the blocks that arrived, the same
way a truncated PNG keeps its rows.

The decoder was written against libjpeg's output rather than against its own.
Over 77 JPEGs pulled off the web, decoded both here and through libjpeg, the
largest per-channel difference is 3 and the largest mean difference is 0.040,
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

### Animation lives in PhotoImage, not in layout

`docs/media.md` proposed running animated GIF through the video player, as a
codec adapter plus a layout rule. It is not built that way, because the draw
path made a much smaller change possible: `DrawImage` blits whatever
`photo.rgba` currently is, so replacing those bytes is already enough to
change what the next repaint shows. Nothing in layout, in the display list or
in the element tree needs to know that an image moves. A GIF is also an
`<img>` rather than a `<video>` -- it has no sound, no playhead and no
controls -- and giving it a `VideoPlayer` would have meant giving it all
three.

So `canvas.PhotoImage` holds the frames and their delays, and `advance(now)`
moves to whichever frame is due at that instant, taking `now` rather than
reading a clock so the behaviour is testable and so every image on a page
moves against one timestamp. `Tab.tick_images()` calls it for everything in
the image cache, off `Browser._video_tick`, which was already running for
video; a tick that changes any image on the active tab asks for a repaint.
Two consequences worth stating:

- **An animation is never "busy".** `Browser.busy()` does not count animated
  images, exactly as it does not count playing video. A GIF that loops for
  ever is the ordinary case, and counting it would mean `settle()` and
  `--screenshot` never returned on most of the web.
- **A late tick does not stretch the animation.** The frame deadline advances
  by the delay, not to the moment the tick arrived, so a stalled tab catches
  up rather than replaying every frame it missed -- and `advance` walks at
  most one pass round the frames per call, so an hour of missed ticks costs
  one tick's work.

Two policies sit on the Python side of that line rather than in the decoder.
The decoder reports the delay the file asked for, including the zero that
means "as fast as you can"; `PhotoImage` rounds anything under 20 ms up to
100 ms, which is what Chrome, Firefox and Safari all do and what those files
-- almost always written that way by accident -- have looked like since the
1990s. And the NETSCAPE2.0 loop count is the extension's own arithmetic: 0
means for ever, and `n` means `n` repeats *after* the first pass, so a file
written with ImageMagick's `-loop 3` stores 2 and plays three times. When the
last pass ends the animation stops on its final frame and stops costing
repaints.

The vectors are seven animations from two encoders, checked frame for frame
against ImageMagick 7's `-coalesce`, which performs exactly this operation.
FFmpeg is deliberately not the reference: its GIF decoder resamples variable
delays onto a constant frame rate, so it does not even agree about how many
frames a file has, and it clears a disposed region to the header's background
colour where every browser clears it to transparent. It contributes a
bitstream instead. `tests/fixtures/gif/make_gif_vectors.sh` rebuilds them all
and records the argument.

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
rather than a crash, a distinction that matters more
now, because the layer
that used to raise `IndexError` in Python would panic here, and a panic
crossing the extension boundary would kill the page load rather than the
image.

## canvas.py: the scene graph

This is the widest surface area, because it is the part everything above it
already talks to. The contract it keeps is:

- Items carry an integer id and a tag set, and paint in creation order.
- `delete(tag)` wipes a layer. The browser's layered repaint (page, chrome,
  selection, toe overlays, each under its own tag) works unchanged.
- `find_all()` returns ids in creation order, which is how the browser diffs
  before and after a toe's `on_draw` to tag whatever the toe created.
- Colours accept `#rgb`, `#rrggbb`, `#rrrrggggbbbb` and the 148 CSS names, and
  raise `CanvasError` on nonsense; every `execute` in the display list
  catches that and paints in black rather than dropping the box.

`Font` is where the two halves have to agree. `measure()` and `draw()` both
resolve each character through the same `face_for()`, which consults the
primary face, and on a miss walks a preferred fallback list and then the whole
font index looking for someone who has the glyph, memoising the answer per
character. Because both walk the identical per-character advance path, and
because we apply no kerning, `measure("abc") == measure("a") + measure("b") +
measure("c")` holds exactly, and painted text lands exactly where layout
measured it. That invariant is load-bearing: layout caches per-character
widths and sums them.

Glyph fallback is not a nicety. No single text face has the toolbar's `⟳`,
`⌂` and `☆`, and without fallback the chrome renders a row of `.notdef` boxes.

## The window: what doormat asks of this stack

The window is `doormat`: a package of ours with the Cocoa, X11 and Win32
backends in it, the input translation behind them and the event loop above
them. It is ctypes throughout, it has no dependencies of its own, and it
draws nothing at all, so the only part of it that belongs in a document about
pixels is the seam -- which is duck-typed, and therefore invisible from both
sides until the day it breaks.

A window is handed a *canvas* and reads three names off it. `canvas.dirty` is
set by every mutation and cleared by `render`, so a frame nothing changed in
is never uploaded. `canvas.render(region=None)` composites the retained items
and returns the surface, `region` being in CSS pixels, which is how a
text-selection drag repaints a strip instead of a page. `canvas.cursor` is
the pointer shape to ask the window system for.

`render` hands back a surface, and the window reads four attributes off that:
`.pixels` -- packed RGB, three bytes to a pixel, and a read-only memoryview
straight onto Rust's buffer -- plus `.width`, `.height` and `.stride`. That
is the entire contract in the drawing direction, and it is what lets a frame
reach the screen without a copy where the platform's own format happens to
be ours: AppKit takes that memoryview through `CGImageCreate`, and an X
server whose visual is 24bpp in RGB order gets it handed to `XPutImage`
untouched. Where a platform disagrees -- GDI wants BGRX and its rows
bottom-up -- converting is the window's problem, and nothing on this side of
the seam knows it happened.

Geometry travels the other way, because the window system is the authority on
it and we are not. `device_size()` reports the framebuffer in device pixels;
`resize(width, height, device=None)` and `set_scale(scale, device=None)` are
called when the window changes size or moves to a display of a different
ratio. Both take the device size separately rather than deriving it, because
where the two disagree -- a HiDPI display, a fractional ratio -- the window
system's number is the exact one and ours is a rounded one, and a
framebuffer a pixel narrower than the window it is blitted into shears.

There is no base class to inherit and nothing to import: `canvas.Canvas` over
`raster.Surface` satisfies this by having the right names, and it had them
before doormat existed, which is the only reason the split was cheap. It is
also why nothing enforces it. A rename on either side type-checks nowhere and
fails at the join, which is what `tests/test_x11.py`, `tests/test_cocoa.py`
and `tests/test_win32.py` are for: a real browser in a real window on each
platform, clicked and typed at through the real event queue.

`gui.py` is the whole of our side of it, and it is 127 lines.
`gui.headless_root()` is always the headless root, so tests and
`--screenshot` never open anything, and only `gui.new_window()` asks for a
real window; nothing gets one by accident. `FEETBROWSER_DISPLAY=x11`,
`=cocoa` or `=win32` demands a backend by name and fails loudly rather than
falling back to a headless root that renders a black screenshot;
`FEETBROWSER_DISPLAY=none` forces headless even where a window is possible.
That value is passed to doormat on each call rather than read once at import,
so a test can set it between windows and nothing has to export doormat's own
`$DOORMAT_DISPLAY`. With no display (no `$DISPLAY`, an X server that will not
answer, or a platform with no backend at all), `display_problem()` says which
of those it was and the browser renders headless instead of raising.

The icon is the other thing that stays on this side. `gui.icon()` decodes
`feetbrowser/icon.png` through our own `imagecodec` and hands the window a
`(width, height, rgba)` triple, because a browser that puts a picture on its
window already has a PNG decoder and a brand, and a windowing library has
neither. It is decoded once per process and a failure to read it is no icon
rather than no browser.

## Testing it

`tests/test_render.py` covers the layers directly and offline: font metrics and
the additive-measure invariant, span fills and clipping, antialiased partial
coverage, nonzero winding, the glyph cache, PNG round-tripping, every PNG
filter type, Adam7, hand-built GIF LZW, JPEG against corrupted photographs,
scene-graph ordering and tag deletion, and the timer and binding model.

The seam gets its own suites, each of which opens a real window on its own
operating system and skips with a message everywhere else:
`tests/test_x11.py` on Linux, `tests/test_cocoa.py` on macOS and
`tests/test_win32.py` on Windows. Each runs a whole `Browser` in that window
and drives it with real platform events -- a click on the new-tab button,
`Ctrl-L` into the address bar, a drag on the scrollbar, a page reaching the
glass -- because "a window works" and "the browser has hold of one" are
different claims and doormat's own suite can only make the first. CI runs all
three jobs.

The end-to-end check is a screenshot: `python3 -m feetbrowser --screenshot
<url> out.png` runs the real browser (chrome, tabs, toolbar, page, scrollbar),
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
- No kerning or ligatures; deliberately, see the invariant above.
- No right-to-left or complex-script shaping. Characters advance
  left-to-right, one glyph each.
- No SVG, and no WebP, BMP, ICO or TIFF. Those draw as their alt text.
- `subsample`/`zoom` of an animated GIF give a still: they resample the frame
  that is current and the copy has no frames of its own. Nothing on the
  `<img>` path calls them today -- an image draws at the size the file says --
  so this is a limit of the scaling API rather than one a page can see.
- The JPEG modes listed above (arithmetic coding, CMYK, 12-bit, lossless,
  hierarchical) are refused rather than approximated.
- No native Wayland backend. Wayland desktops get the X11 window through
  XWayland, which is how nearly all of them run X clients today.
- X11 needs a TrueColor visual, which is every server since about 2005;
  PseudoColor and its colormaps are not implemented.
