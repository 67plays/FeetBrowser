"""A drop-in replacement for the Tk widgets the browser actually used.

The browser only ever asked Tk for four things: a font that can measure text,
an image that can report its size, a canvas that retains drawing items under
tags, and an exception type to catch when a colour is bad. This module
provides all four on top of our own font engine and rasteriser, so the paint
path -- the display list's `execute` methods, the chrome drawing, the toe
plugin `on_draw` contract -- keeps working verbatim.

The canvas is retained, exactly like Tk's: items keep their identity and tags,
`delete(tag)` removes a layer, and stacking order is creation order. The
difference is that compositing to pixels happens in `render()`, when a frame
is actually wanted, rather than inside every `create_*` call.
"""
import base64
import math

from . import fontengine, imagecodec, raster


class TclError(Exception):
    """Stands in for tkinter.TclError so existing except-clauses still fire."""


# -- colours ---------------------------------------------------------------

# CSS named colours. Pages reach for these constantly; the browser's own
# chrome uses hex, so this table exists purely for author stylesheets.
_NAMED = {
    "aliceblue": "#f0f8ff", "antiquewhite": "#faebd7", "aqua": "#00ffff",
    "aquamarine": "#7fffd4", "azure": "#f0ffff", "beige": "#f5f5dc",
    "bisque": "#ffe4c4", "black": "#000000", "blanchedalmond": "#ffebcd",
    "blue": "#0000ff", "blueviolet": "#8a2be2", "brown": "#a52a2a",
    "burlywood": "#deb887", "cadetblue": "#5f9ea0", "chartreuse": "#7fff00",
    "chocolate": "#d2691e", "coral": "#ff7f50", "cornflowerblue": "#6495ed",
    "cornsilk": "#fff8dc", "crimson": "#dc143c", "cyan": "#00ffff",
    "darkblue": "#00008b", "darkcyan": "#008b8b", "darkgoldenrod": "#b8860b",
    "darkgray": "#a9a9a9", "darkgrey": "#a9a9a9", "darkgreen": "#006400",
    "darkkhaki": "#bdb76b", "darkmagenta": "#8b008b",
    "darkolivegreen": "#556b2f", "darkorange": "#ff8c00",
    "darkorchid": "#9932cc", "darkred": "#8b0000", "darksalmon": "#e9967a",
    "darkseagreen": "#8fbc8f", "darkslateblue": "#483d8b",
    "darkslategray": "#2f4f4f", "darkslategrey": "#2f4f4f",
    "darkturquoise": "#00ced1", "darkviolet": "#9400d3", "deeppink": "#ff1493",
    "deepskyblue": "#00bfff", "dimgray": "#696969", "dimgrey": "#696969",
    "dodgerblue": "#1e90ff", "firebrick": "#b22222", "floralwhite": "#fffaf0",
    "forestgreen": "#228b22", "fuchsia": "#ff00ff", "gainsboro": "#dcdcdc",
    "ghostwhite": "#f8f8ff", "gold": "#ffd700", "goldenrod": "#daa520",
    "gray": "#808080", "grey": "#808080", "green": "#008000",
    "greenyellow": "#adff2f", "honeydew": "#f0fff0", "hotpink": "#ff69b4",
    "indianred": "#cd5c5c", "indigo": "#4b0082", "ivory": "#fffff0",
    "khaki": "#f0e68c", "lavender": "#e6e6fa", "lavenderblush": "#fff0f5",
    "lawngreen": "#7cfc00", "lemonchiffon": "#fffacd", "lightblue": "#add8e6",
    "lightcoral": "#f08080", "lightcyan": "#e0ffff",
    "lightgoldenrodyellow": "#fafad2", "lightgray": "#d3d3d3",
    "lightgrey": "#d3d3d3", "lightgreen": "#90ee90", "lightpink": "#ffb6c1",
    "lightsalmon": "#ffa07a", "lightseagreen": "#20b2aa",
    "lightskyblue": "#87cefa", "lightslategray": "#778899",
    "lightslategrey": "#778899", "lightsteelblue": "#b0c4de",
    "lightyellow": "#ffffe0", "lime": "#00ff00", "limegreen": "#32cd32",
    "linen": "#faf0e6", "magenta": "#ff00ff", "maroon": "#800000",
    "mediumaquamarine": "#66cdaa", "mediumblue": "#0000cd",
    "mediumorchid": "#ba55d3", "mediumpurple": "#9370db",
    "mediumseagreen": "#3cb371", "mediumslateblue": "#7b68ee",
    "mediumspringgreen": "#00fa9a", "mediumturquoise": "#48d1cc",
    "mediumvioletred": "#c71585", "midnightblue": "#191970",
    "mintcream": "#f5fffa", "mistyrose": "#ffe4e1", "moccasin": "#ffe4b5",
    "navajowhite": "#ffdead", "navy": "#000080", "oldlace": "#fdf5e6",
    "olive": "#808000", "olivedrab": "#6b8e23", "orange": "#ffa500",
    "orangered": "#ff4500", "orchid": "#da70d6", "palegoldenrod": "#eee8aa",
    "palegreen": "#98fb98", "paleturquoise": "#afeeee",
    "palevioletred": "#db7093", "papayawhip": "#ffefd5",
    "peachpuff": "#ffdab9", "peru": "#cd853f", "pink": "#ffc0cb",
    "plum": "#dda0dd", "powderblue": "#b0e0e6", "purple": "#800080",
    "rebeccapurple": "#663399", "red": "#ff0000", "rosybrown": "#bc8f8f",
    "royalblue": "#4169e1", "saddlebrown": "#8b4513", "salmon": "#fa8072",
    "sandybrown": "#f4a460", "seagreen": "#2e8b57", "seashell": "#fff5ee",
    "sienna": "#a0522d", "silver": "#c0c0c0", "skyblue": "#87ceeb",
    "slateblue": "#6a5acd", "slategray": "#708090", "slategrey": "#708090",
    "snow": "#fffafa", "springgreen": "#00ff7f", "steelblue": "#4682b4",
    "tan": "#d2b48c", "teal": "#008080", "thistle": "#d8bfd8",
    "tomato": "#ff6347", "turquoise": "#40e0d0", "violet": "#ee82ee",
    "wheat": "#f5deb3", "white": "#ffffff", "whitesmoke": "#f5f5f5",
    "yellow": "#ffff00", "yellowgreen": "#9acd32",
}

_COLOR_MEMO = {}


def color(value):
    """Parse a Tk/CSS colour into an ``(r, g, b)`` triple.

    Returns None for "no colour" -- an empty string, which Tk treats as
    transparent for a fill or outline. Raises TclError on genuine nonsense,
    matching Tk, because the display list relies on that to fall back to
    black rather than paint an invisible box.
    """
    if value is None:
        return None
    if isinstance(value, tuple):
        return value
    try:
        return _COLOR_MEMO[value]
    except KeyError:
        pass
    text = value.strip().lower()
    if not text:
        return None
    hexed = _NAMED.get(text, text)
    result = None
    if hexed.startswith("#"):
        digits = hexed[1:]
        if len(digits) == 3:
            result = tuple(int(c * 2, 16) for c in digits)
        elif len(digits) == 6:
            result = tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
        elif len(digits) == 12:  # Tk's 16-bit-per-channel form
            result = tuple(int(digits[i:i + 4], 16) >> 8 for i in (0, 4, 8))
    if result is None:
        raise TclError("unknown color name %r" % value)
    if len(_COLOR_MEMO) < 20000:
        _COLOR_MEMO[value] = result
    return result


# -- fonts -----------------------------------------------------------------

_FONT_SEQ = [0]

# What a family name should resolve to when the requested face is missing.
# Ordered by preference; the first family present on the system wins.
_FALLBACKS = {
    "times": ("times new roman", "times", "georgia", "dejavu serif",
              "liberation serif", "noto serif"),
    "helvetica": ("helvetica", "helvetica neue", "arial", "dejavu sans",
                  "liberation sans", "noto sans"),
    "courier": ("courier new", "courier", "menlo", "dejavu sans mono",
                "liberation mono", "noto sans mono"),
}


class Font:
    """The subset of tkinter.font.Font the layout engine uses.

    ``measure`` is exact and additive: a string's width is the sum of its
    characters' advances, with no kerning. That is not a shortcut, it is the
    invariant the layout engine's per-character width cache is built on, so
    the rasteriser advances the pen by the same per-character amounts and
    painted text lands exactly where layout measured it would.
    """

    def __init__(self, family="", size=12, weight="normal", slant="roman",
                 **_ignored):
        self.family = family or "Times"
        self.size = abs(int(size)) if size else 12
        self.weight = weight
        self.slant = slant
        self.bold = weight in ("bold", "heavy")
        self.italic = slant in ("italic", "oblique")
        _FONT_SEQ[0] += 1
        self.name = "ftbsfont%d" % _FONT_SEQ[0]
        self._tk = None  # no Tcl interpreter: layout's batch path stays off
        self.face = _resolve_face(self.family, self.bold, self.italic)
        if self.face is None:
            raise TclError("no usable font for %r" % self.family)
        self._scale = self.face.scale(self.size)
        self.ascent = int(round(self.face.ascent * self._scale))
        self.descent = int(round(-self.face.descent * self._scale))
        self.linespace = int(round(self.face.linespace() * self._scale))
        self._widths = {}
        self._faces = {}

    def face_for(self, ch):
        """The face that will actually draw `ch`, and its scale.

        A single face never covers everything a page throws at it -- arrows,
        stars, box-drawing, CJK -- and drawing .notdef boxes for those is a
        visible bug, so a character the primary face lacks is looked up in a
        fallback chain. Measuring and painting both come through here, which
        is what keeps them in agreement.
        """
        try:
            return self._faces[ch]
        except KeyError:
            pass
        face, gid = self.face, self.face.glyph_id(ch)
        if gid == 0 and not ch.isspace() and ch != "\ufffd":
            alt = _fallback_face(ch, self.bold, self.italic)
            if alt is not None:
                face, gid = alt, alt.glyph_id(ch)
        entry = (face, gid, face.scale(self.size))
        self._faces[ch] = entry
        return entry

    def measure(self, text):
        widths = self._widths
        total = 0.0
        for ch in text:
            try:
                total += widths[ch]
            except KeyError:
                face, gid, scale = self.face_for(ch)
                w = face.advance(gid) * scale
                widths[ch] = w
                total += w
        return total

    def draw(self, surface, text, x, baseline, fill):
        """Paint `text`, advancing exactly as `measure` says it will."""
        pen = float(x)
        widths = self._widths
        for ch in text:
            face, gid, scale = self.face_for(ch)
            try:
                advance = widths[ch]
            except KeyError:
                advance = face.advance(gid) * scale
                widths[ch] = advance
            if not ch.isspace():
                cov, w, h, left, top = raster.glyph_bitmap(face, self.size,
                                                           gid)
                if w:
                    surface.blit_coverage(cov, w, h, int(pen) + left,
                                          int(baseline) + top, fill)
            pen += advance
        return pen - x

    def metrics(self, name=None):
        table = {"ascent": self.ascent, "descent": self.descent,
                 "linespace": self.linespace, "fixed": 0}
        return table[name] if name else table

    def cget(self, option):
        return getattr(self, option, "")

    def actual(self, option=None):
        table = {"family": self.family, "size": self.size,
                 "weight": self.weight, "slant": self.slant}
        return table[option] if option else table

    def __repr__(self):
        return "<Font %s %dpx%s%s>" % (self.family, self.size,
                                       " bold" if self.bold else "",
                                       " italic" if self.italic else "")


_FACE_MEMO = {}
_CHAR_FACE_MEMO = {}

# Tried in order when the requested face has no glyph for a character. These
# are the families that actually carry the symbol, arrow and CJK ranges on
# the platforms we run on; anything still missing falls through to a scan of
# every installed family.
_FALLBACK_FAMILIES = (
    "apple symbols", "arial unicode ms", "segoe ui symbol",
    "noto sans symbols 2", "noto sans symbols", "symbola", "dejavu sans",
    "menlo", "consolas", "arial", "helvetica", "times new roman",
    "hiragino sans", "noto sans cjk jp", "microsoft yahei", "pingfang sc",
    "noto color emoji", "apple color emoji",
)


def _fallback_face(ch, bold, italic):
    """Find any installed face containing `ch`.

    The full scan is the slow path, but it runs at most once per distinct
    missing character for the life of the process, and only for characters
    the preferred families already failed to supply.
    """
    key = (ch, bold, italic)
    if key in _CHAR_FACE_MEMO:
        return _CHAR_FACE_MEMO[key]
    found = None
    for family in _FALLBACK_FAMILIES:
        face = fontengine.find(family, bold, italic)
        if face is not None and face.has_char(ch):
            found = face
            break
    if found is None:
        for family in sorted(fontengine.index()):
            face = fontengine.find(family, bold, italic)
            if face is not None and face.has_char(ch):
                found = face
                break
    _CHAR_FACE_MEMO[key] = found
    return found


def _resolve_face(family, bold, italic):
    """Find the best installed face, walking the fallback chain for the
    three generics the layout engine normalises font stacks down to."""
    key = (family.lower(), bold, italic)
    if key in _FACE_MEMO:
        return _FACE_MEMO[key]
    candidates = _FALLBACKS.get(family.lower(), (family.lower(),))
    face = None
    for name in candidates:
        face = fontengine.find(name, bold, italic)
        if face is not None:
            break
    if face is None:
        # Last resort: anything at all, so a page never fails to paint.
        for chain in _FALLBACKS.values():
            for name in chain:
                face = fontengine.find(name, bold, italic)
                if face is not None:
                    break
            if face is not None:
                break
    if face is None:
        available = fontengine.index()
        if available:
            first = sorted(available)[0]
            face = fontengine.find(first, bold, italic)
    _FACE_MEMO[key] = face
    return face


# -- images ----------------------------------------------------------------

class PhotoImage:
    """Holds decoded RGBA pixels; the API surface is width()/height()."""

    def __init__(self, data=None, file=None, width=None, height=None,
                 **_ignored):
        if file is not None:
            with open(file, "rb") as handle:
                data = handle.read()
        if data is None:
            if width is None or height is None:
                raise TclError("PhotoImage needs data, a file, or a size")
            self._width, self._height = max(1, int(width)), max(1, int(height))
            self.rgba = bytearray(self._width * self._height * 4)
            self.opaque = False
            return
        if isinstance(data, str):
            # Tk accepts base64 text for its supported formats.
            try:
                data = base64.b64decode(data)
            except Exception:
                data = data.encode("latin-1", "replace")
        try:
            self._width, self._height, self.rgba = imagecodec.decode(data)
        except imagecodec.ImageError as exc:
            raise TclError(str(exc))
        # Checked once here so every blit can take the fast row-copy path;
        # most photos on the web have no transparency at all.
        self.opaque = self.rgba[3::4].count(255) == self._width * self._height

    def width(self):
        return self._width

    def height(self):
        return self._height

    def subsample(self, x, y=None):
        return self._scaled(self._width // max(1, x),
                            self._height // max(1, y or x))

    def zoom(self, x, y=None):
        return self._scaled(self._width * max(1, x),
                            self._height * max(1, y or x))

    def _scaled(self, width, height):
        clone = PhotoImage.__new__(PhotoImage)
        clone.rgba = imagecodec.resize(self.rgba, self._width, self._height,
                                       width, height)
        clone._width, clone._height = max(1, width), max(1, height)
        clone.opaque = self.opaque
        return clone


# -- canvas ----------------------------------------------------------------

class _Item:
    __slots__ = ("id", "kind", "coords", "opts", "tags")

    def __init__(self, item_id, kind, coords, opts, tags):
        self.id = item_id
        self.kind = kind
        self.coords = coords
        self.opts = opts
        self.tags = tags


class Canvas:
    """Retained display list, composited on demand.

    Item ids and tags behave as Tk's do because the browser leans on both:
    it diffs `find_all()` before and after a plugin draws to tag that
    plugin's items, and deletes whole layers by tag on every frame.
    """

    def __init__(self, master=None, width=800, height=600, bg="white",
                 background=None, **_ignored):
        self.master = master
        self._width = int(width)
        self._height = int(height)
        self.background = color(background or bg) or (255, 255, 255)
        self.cursor = ""
        self._items = []
        self._by_tag = {}
        self._next_id = 1
        self.surface = raster.Surface(self._width, self._height,
                                      self.background)

    # -- geometry ----------------------------------------------------------

    def winfo_width(self):
        return self._width

    def winfo_height(self):
        return self._height

    def winfo_reqwidth(self):
        return self._width

    def winfo_reqheight(self):
        return self._height

    def resize(self, width, height):
        width, height = max(1, int(width)), max(1, int(height))
        if (width, height) == (self._width, self._height):
            return
        self._width, self._height = width, height
        self.surface = raster.Surface(width, height, self.background)

    def pack(self, **_ignored):
        # Geometry management is a single canvas filling its window, so
        # "packing" just means becoming the window's presented surface.
        if self.master is not None and hasattr(self.master, "canvas"):
            self.master.canvas = self
            self.resize(self.master.width, self.master.height)
        return self

    def place(self, **_ignored):
        return self

    def grid(self, **_ignored):
        return self

    def config(self, **kwargs):
        if "cursor" in kwargs:
            self.cursor = kwargs["cursor"]
        if "bg" in kwargs or "background" in kwargs:
            self.background = color(kwargs.get("bg")
                                    or kwargs.get("background")) or self.background
        if "width" in kwargs or "height" in kwargs:
            self.resize(kwargs.get("width", self._width),
                        kwargs.get("height", self._height))
        return self

    configure = config

    # -- item creation -----------------------------------------------------

    def _add(self, kind, coords, opts):
        tags = opts.pop("tags", ())
        if isinstance(tags, str):
            tags = (tags,)
        tags = tuple(tags)
        item = _Item(self._next_id, kind, coords, opts, tags)
        self._next_id += 1
        self._items.append(item)
        for tag in tags:
            self._by_tag.setdefault(tag, []).append(item)
        return item.id

    def create_rectangle(self, x1, y1, x2, y2, **opts):
        # Validate colours now, as Tk does, so callers' except-TclError
        # fallbacks still trigger on a bad colour rather than at paint time.
        color(opts.get("fill"))
        color(opts.get("outline"))
        return self._add("rectangle", (x1, y1, x2, y2), opts)

    def create_line(self, *coords, **opts):
        color(opts.get("fill"))
        return self._add("line", tuple(coords), opts)

    def create_text(self, x, y, **opts):
        color(opts.get("fill"))
        return self._add("text", (x, y), opts)

    def create_image(self, x, y, **opts):
        return self._add("image", (x, y), opts)

    def create_oval(self, x1, y1, x2, y2, **opts):
        color(opts.get("fill"))
        color(opts.get("outline"))
        return self._add("oval", (x1, y1, x2, y2), opts)

    def create_arc(self, x1, y1, x2, y2, **opts):
        color(opts.get("outline"))
        return self._add("arc", (x1, y1, x2, y2), opts)

    def create_polygon(self, *coords, **opts):
        color(opts.get("fill"))
        return self._add("polygon", tuple(coords), opts)

    # -- item management ---------------------------------------------------

    def find_all(self):
        return [item.id for item in self._items]

    def find_withtag(self, tag):
        return [item.id for item in self._resolve(tag)]

    def delete(self, *tags):
        for tag in tags:
            if tag == "all":
                self._items = []
                self._by_tag = {}
                continue
            doomed = set(id(item) for item in self._resolve(tag))
            if not doomed:
                continue
            self._items = [i for i in self._items if id(i) not in doomed]
            for key, items in list(self._by_tag.items()):
                kept = [i for i in items if id(i) not in doomed]
                if kept:
                    self._by_tag[key] = kept
                else:
                    del self._by_tag[key]

    def addtag_withtag(self, newtag, tag):
        for item in self._resolve(tag):
            if newtag not in item.tags:
                item.tags += (newtag,)
                self._by_tag.setdefault(newtag, []).append(item)

    def itemconfig(self, tag, **opts):
        for item in self._resolve(tag):
            item.opts.update(opts)

    itemconfigure = itemconfig

    def coords(self, tag, *values):
        items = self._resolve(tag)
        if not values:
            return list(items[0].coords) if items else []
        for item in items:
            item.coords = tuple(values)

    def bbox(self, tag=None):
        items = self._items if tag in (None, "all") else self._resolve(tag)
        boxes = [self._bounds(i) for i in items]
        boxes = [b for b in boxes if b]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def _resolve(self, tag):
        if tag == "all":
            return list(self._items)
        if isinstance(tag, int):
            return [i for i in self._items if i.id == tag]
        if isinstance(tag, str) and tag.isdigit():
            wanted = int(tag)
            return [i for i in self._items if i.id == wanted]
        return list(self._by_tag.get(tag, ()))

    def _bounds(self, item):
        c = item.coords
        if item.kind == "text":
            font = item.opts.get("font")
            text = str(item.opts.get("text", ""))
            if font is None:
                return None
            w = font.measure(text)
            h = font.linespace
            x, y = c[0], c[1]
            if item.opts.get("anchor", "nw") == "w":
                y -= h / 2
            return (x, y, x + w, y + h)
        if item.kind == "image":
            photo = item.opts.get("image")
            if photo is None:
                return None
            return (c[0], c[1], c[0] + photo.width(), c[1] + photo.height())
        if len(c) < 4:
            return None
        xs, ys = c[0::2], c[1::2]
        return (min(xs), min(ys), max(xs), max(ys))

    # -- painting ----------------------------------------------------------

    def render(self, region=None):
        """Composite every item into the surface and return it.

        Painting the whole retained list each frame is what keeps the model
        honest: overlapping items, deletions and re-inserts all resolve
        correctly without tracking damaged pixels. `region` clips the work
        to a rectangle when only part of the frame changed.
        """
        surface = self.surface
        if region:
            x0, y0, x1, y1 = region
            saved = surface.set_clip(x0, y0, x1, y1)
            surface.fill_rect(x0, y0, x1, y1, self.background)
        else:
            saved = None
            surface.fill_all(self.background)
        for item in self._items:
            self._paint(surface, item)
        if saved is not None:
            surface.reset_clip(saved)
        return surface

    def _paint(self, surface, item):
        kind = item.kind
        opts = item.opts
        c = item.coords
        if kind == "text":
            self._paint_text(surface, c, opts)
        elif kind == "rectangle":
            x0, y0, x1, y1 = _ordered(c)
            alpha = 128 if opts.get("stipple") else 255
            fill = color(opts.get("fill"))
            if fill:
                surface.fill_rect(x0, y0, x1, y1, fill, alpha)
            outline = color(opts.get("outline"))
            width = int(opts.get("width", 1 if outline else 0))
            if outline and width:
                surface.outline_rect(x0, y0, x1, y1, outline, width, alpha)
        elif kind == "line":
            stroke = color(opts.get("fill")) or (0, 0, 0)
            width = int(opts.get("width", 1))
            for i in range(0, len(c) - 3, 2):
                surface.draw_line(c[i], c[i + 1], c[i + 2], c[i + 3],
                                  stroke, width)
        elif kind == "image":
            photo = opts.get("image")
            if photo is not None:
                x, y = c[0], c[1]
                if opts.get("anchor", "nw") == "center":
                    x -= photo.width() / 2
                    y -= photo.height() / 2
                surface.blit_rgba(photo.rgba, photo.width(), photo.height(),
                                  x, y, getattr(photo, "opaque", False))
        elif kind in ("oval", "arc"):
            self._paint_arc(surface, item)
        elif kind == "polygon":
            fill = color(opts.get("fill"))
            if fill and len(c) >= 6:
                points = [(c[i], c[i + 1]) for i in range(0, len(c) - 1, 2)]
                _fill_polygon(surface, points, fill)

    def _paint_text(self, surface, coords, opts):
        font = opts.get("font")
        text = opts.get("text", "")
        if font is None or not text:
            return
        fill = color(opts.get("fill")) or (0, 0, 0)
        x, y = coords[0], coords[1]
        anchor = opts.get("anchor", "center")
        if anchor == "w":
            top = y - font.linespace / 2
        elif anchor in ("nw", "n", "ne"):
            top = y
        else:
            top = y - font.linespace / 2
        if anchor in ("center", "n", "s"):
            x -= font.measure(text) / 2
        elif anchor in ("e", "ne", "se"):
            x -= font.measure(text)
        baseline = top + font.ascent
        for line in str(text).split("\n"):
            font.draw(surface, line, x, baseline, fill)
            baseline += font.linespace

    def _paint_arc(self, surface, item):
        """Stroke an elliptical arc. Tk measures angles counter-clockwise
        from 3 o'clock in degrees, and the browser uses this for one thing:
        the loading spinner."""
        x0, y0, x1, y1 = _ordered(item.coords)
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        rx, ry = (x1 - x0) / 2.0, (y1 - y0) / 2.0
        opts = item.opts
        stroke = color(opts.get("outline")) or color(opts.get("fill"))
        if not stroke or rx <= 0 or ry <= 0:
            return
        width = int(opts.get("width", 1))
        start = float(opts.get("start", 0.0))
        extent = float(opts.get("extent", 360.0))
        steps = max(8, int(abs(extent) / 4))
        prev = None
        for i in range(steps + 1):
            angle = math.radians(start + extent * i / steps)
            px = cx + rx * math.cos(angle)
            py = cy - ry * math.sin(angle)  # screen y grows downward
            if prev:
                surface.draw_line(prev[0], prev[1], px, py, stroke, width)
            prev = (px, py)

    # -- output ------------------------------------------------------------

    def to_png(self):
        return self.render().to_png()

    def save_png(self, path):
        self.render().save_png(path)


def _ordered(coords):
    x0, y0, x1, y1 = coords[:4]
    if x0 > x1:
        x0, x1 = x1, x0
    if y0 > y1:
        y0, y1 = y1, y0
    return x0, y0, x1, y1


def _fill_polygon(surface, points, fill):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    left, top = int(min(xs)) - 1, int(min(ys)) - 1
    width = int(max(xs)) - left + 2
    height = int(max(ys)) - top + 2
    if width <= 0 or height <= 0 or width > 8192 or height > 8192:
        return
    cov = raster.rasterize([points], width, height, -left, -top)
    surface.blit_coverage(cov, width, height, left, top, fill)
