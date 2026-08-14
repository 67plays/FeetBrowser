"""A from-scratch layout engine.

Produces a layout tree from a styled DOM and a display list of paint
commands. Implements block-and-inline flow: block boxes stack vertically,
inline content flows into lines with word wrapping. Supports font size /
weight / style, colors, backgrounds, list bullets, and horizontal rules.

Coordinates are in CSS px == canvas px. Fonts are Tk fonts, cached.
"""

import re
import tkinter.font

from .htmlparser import Text, Element

# Tags whose default flow is inline.
INLINE_ELEMENTS = {
    "a", "b", "i", "em", "strong", "span", "small", "big", "sub", "sup",
    "code", "tt", "kbd", "samp", "u", "abbr", "cite", "q", "s", "strike",
    "font", "label", "br", "img", "input", "button", "mark", "time", "var",
    "select", "textarea", "option",
}

_FONT_CACHE = {}

# Measuring text and reading metrics round-trips into the Tcl interpreter,
# which costs on the order of a millisecond per call. Repeatedly measuring the
# same word with the same font dominates layout time on text-heavy pages, so
# both are memoized keyed by (font key, arg). Bounded so a wild page full of
# unique strings cannot grow the cache without limit.
_MEASURE_CACHE = {}
_METRICS_CACHE = {}
_MEASURE_CACHE_MAX = 100_000
# Tk's font measure applies no kerning or ligatures here, so
# measure("abc") == measure("a") + measure("b") + measure("c") exactly. Only
# each unique (font, char) therefore needs a Tcl round-trip; word widths are a
# Python sum. Bounded because a page full of exotic codepoints must not grow
# the table without limit.
_CHAR_CACHE = {}
_CHAR_CACHE_MAX = 50_000


def get_font(size, weight, style, family=""):
    key = (size, weight, style, family)
    if key not in _FONT_CACHE:
        fam = family if family else "Times"
        font = tkinter.font.Font(size=size, weight=weight, slant=style, family=fam)
        font._ftbs_key = key  # stable cache identity for memo tables
        _FONT_CACHE[key] = font
    return _FONT_CACHE[key]


def _measure(font, text):
    """Memoized font.measure(text). Because Tk applies no kerning, this is the
    sum of per-character widths, so only unique characters need Tcl calls."""
    if not text:
        return 0.0
    key = (font._ftbs_key, text)
    try:
        return _MEASURE_CACHE[key]
    except KeyError:
        pass
    width = 0.0
    for ch in text:
        ckey = (font._ftbs_key, ch)
        try:
            width += _CHAR_CACHE[ckey]
        except KeyError:
            cw = font.measure(ch)
            if len(_CHAR_CACHE) < _CHAR_CACHE_MAX:
                _CHAR_CACHE[ckey] = cw
            width += cw
    if len(_MEASURE_CACHE) < _MEASURE_CACHE_MAX:
        _MEASURE_CACHE[key] = width
    return width


def _metrics(font, name):
    """Memoized font.metrics(name): ascent/descent/linespace are constant per
    font, and flush() queries them for every line item."""
    key = (font._ftbs_key, name)
    try:
        return _METRICS_CACHE[key]
    except KeyError:
        pass
    value = font.metrics(name)
    _METRICS_CACHE[key] = value
    return value


def _linespace(font):
    return _metrics(font, "linespace")


def _measure_batch(font, chars):
    """Measure a list of distinct characters with `font` in a single Tcl
    round-trip, filling the shared per-character width cache. Falls back to
    per-char calls if no Tk root is around (it always is during layout)."""
    if not chars:
        return
    key = font._ftbs_key
    todo = [c for c in chars if (key, c) not in _CHAR_CACHE]
    if not todo:
        return
    tk = getattr(font, "_tk", None)
    if tk is None:
        for c in todo:
            if len(_CHAR_CACHE) < _CHAR_CACHE_MAX:
                _CHAR_CACHE[(key, c)] = font.measure(c)
            else:
                font.measure(c)
        return
    # Pass the chars as a proper Tcl list (tkinter marshals each element
    # safely), measure them all in one `eval`, then pull the widths back.
    tk.call("set", "::fb_chars", todo)
    tk.call("set", "::fb_font", font.name)
    tk.call("eval", "set ::fb_out {}; foreach c $::fb_chars "
                    "{lappend ::fb_out [font measure $::fb_font $c]}")
    out = tk.splitlist(tk.call("set", "::fb_out"))
    for c, value in zip(todo, out):
        if len(_CHAR_CACHE) < _CHAR_CACHE_MAX:
            _CHAR_CACHE[(key, c)] = float(value)


def _prewarm(root_node):
    """Measure every distinct character the text layout will need up front, in
    a handful of batched Tcl calls. Because Tk applies no kerning, a word's
    width is the sum of its character widths, so prewarming characters makes
    the per-word _measure() calls pure Python."""
    pending = {}  # font._ftbs_key -> (font, set of chars)
    stack = [root_node]
    while stack:
        node = stack.pop()
        if isinstance(node, Text):
            font = _node_font(node)
            key = font._ftbs_key
            if key not in pending:
                pending[key] = (font, set())
            pending[key][1].add(" ")
            pending[key][1].update(node.text)
        else:
            stack.extend(node.children)
    for font, chars in pending.values():
        _measure_batch(font, chars)


def _node_font(node):
    style = getattr(node, "style", {}) or {}
    size = int(round(parse_px(style.get("font-size", "16px"), 16)))
    size = max(6, min(size, 80))
    weight = "bold" if style.get("font-weight") in ("bold", "bolder", "600",
                                                     "700", "800", "900") else "normal"
    slant = "italic" if style.get("font-style") in ("italic", "oblique") else "roman"
    fam = style.get("font-family", "")
    if fam:
        fam = fam.split(",")[0].strip().strip("'\"")
        if fam.lower() in ("monospace", "courier"):
            fam = "Courier"
        elif fam.lower() in ("serif", "times"):
            fam = "Times"
        elif fam.lower() in ("sans-serif", "arial", "helvetica"):
            fam = "Helvetica"
    return get_font(size, weight, slant, fam)


class DrawText:
    def __init__(self, x1, y1, text, font, color, node=None):
        self.top = y1
        self.left = x1
        self.text = text
        self.font = font
        self.color = color
        self.node = node  # source DOM node, for hit-testing links
        self.right = x1 + _measure(font, text)
        self.bottom = y1 + _metrics(font, "linespace")

    def hit(self, x, y):
        return self.left <= x < self.right and self.top <= y < self.bottom

    def execute(self, scroll, canvas):
        try:
            canvas.create_text(
                self.left, self.top - scroll, text=self.text,
                font=self.font, fill=self.color or "black", anchor="nw")
        except tkinter.TclError:
            canvas.create_text(
                self.left, self.top - scroll, text=self.text,
                font=self.font, fill="black", anchor="nw")


class _DrawShape:
    """Shared rectangle geometry for the fill/line/outline commands."""

    def __init__(self, x1, y1, x2, y2, color, thickness=0):
        self.top, self.left, self.bottom, self.right = y1, x1, y2, x2
        self.color = color
        self.thickness = thickness


class DrawRect(_DrawShape):
    def execute(self, scroll, canvas):
        try:
            canvas.create_rectangle(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                width=0, fill=self.color)
        except tkinter.TclError:
            canvas.create_rectangle(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                width=0, fill="black")


class DrawLine(_DrawShape):
    def execute(self, scroll, canvas):
        try:
            canvas.create_line(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                fill=self.color, width=self.thickness)
        except tkinter.TclError:
            canvas.create_line(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                fill="black", width=self.thickness)


class DrawOutline(_DrawShape):
    def execute(self, scroll, canvas):
        try:
            canvas.create_rectangle(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                width=self.thickness, outline=self.color)
        except tkinter.TclError:
            canvas.create_rectangle(
                self.left, self.top - scroll, self.right, self.bottom - scroll,
                width=self.thickness, outline="black")


class DrawImage:
    """Draws a decoded Tk PhotoImage at the given rectangle."""

    def __init__(self, x1, y1, x2, y2, photo, node=None):
        self.top, self.left, self.bottom, self.right = y1, x1, y2, x2
        self.photo = photo
        self.node = node  # source <img>, for hit-testing links

    def execute(self, scroll, canvas):
        canvas.create_image(
            self.left, self.top - scroll, anchor="nw", image=self.photo)


def parse_px(value, default=0.0):
    try:
        if value.endswith("px"):
            return float(value[:-2])
        if value.endswith("rem"):
            return float(value[:-3]) * 16.0
        if value.endswith("%"):
            return default
        return float(value)
    except (ValueError, AttributeError):
        return default


def _padding_box(style):
    """Expand the `padding` shorthand (1-4 values) into the per-side longhands
    that layout reads, falling back to whatever explicit sides are set."""
    top = parse_px(style.get("padding-top", "0"))
    right = parse_px(style.get("padding-right", "0"))
    bottom = parse_px(style.get("padding-bottom", "0"))
    left = parse_px(style.get("padding-left", "0"))
    shorthand = style.get("padding")
    if shorthand:
        parts = shorthand.split()
        if len(parts) == 1:
            v = parse_px(parts[0]); top = right = bottom = left = v
        elif len(parts) == 2:
            v = parse_px(parts[0]); h = parse_px(parts[1])
            top = bottom = v; right = left = h
        elif len(parts) == 3:
            top = parse_px(parts[0])
            h = parse_px(parts[1]); right = left = h
            bottom = parse_px(parts[2])
        elif len(parts) == 4:
            top = parse_px(parts[0]); right = parse_px(parts[1])
            bottom = parse_px(parts[2]); left = parse_px(parts[3])
    return top, right, bottom, left


_COLOR_FUNC_RE = re.compile(r"^(rgba?|hsla?)\((.*)\)$", re.DOTALL)


def _color_channels(name):
    """Split the inside of rgb()/rgba()/hsl()/hsla() into channel strings,
    accepting both comma and modern space/slash syntax."""
    m = _COLOR_FUNC_RE.match(name)
    if not m:
        return None
    inner = re.sub(r"[,/]", " ", m.group(2)).strip()
    parts = [p for p in inner.split() if p]
    if len(parts) not in (3, 4):
        return None
    return m.group(1), parts


def _color_channel(v):
    """Convert a CSS channel value (0-255 or percentage) to an int 0-255."""
    v = v.strip()
    try:
        if v.endswith("%"):
            val = float(v[:-1]) / 100.0 * 255.0
        else:
            val = float(v)
    except ValueError:
        return 0
    return max(0, min(255, int(round(val))))


def _color_alpha(v):
    """Convert a CSS alpha value (0-1 or percentage) to a float."""
    if v is None:
        return 1.0
    v = v.strip()
    try:
        if v.endswith("%"):
            return max(0.0, min(1.0, float(v[:-1]) / 100.0))
        return max(0.0, min(1.0, float(v)))
    except ValueError:
        return 1.0


def _hsl_to_rgb(h, s, l):
    h = (h % 360) / 360.0
    s = max(0.0, min(1.0, s))
    l = max(0.0, min(1.0, l))
    if s == 0:
        return l, l, l
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    def hue(t):
        if t < 0:
            t += 1
        if t > 1:
            t -= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p
    return hue(h + 1 / 3), hue(h), hue(h - 1 / 3)


def _parse_hue(v):
    v = v.strip().lower()
    if v.endswith("%"):
        return float(v[:-1]) / 100.0 * 360.0
    for unit, factor in (("rad", 180 / 3.141592653589793),
                         ("grad", 0.9), ("turn", 360.0), ("deg", 1.0)):
        if v.endswith(unit):
            return float(v[:-len(unit)]) * factor
    return float(v)


def resolve_color(name):
    if not name:
        return None
    name = name.strip().lower()
    if name in ("transparent", "none", "currentcolor", "inherit", "initial"):
        return None
    parsed = _color_channels(name)
    if parsed:
        kind, parts = parsed
        a = parts[3] if len(parts) == 4 else None
        if _color_alpha(a) <= 0:
            return None
        if kind.startswith("rgb"):
            r, g, b = parts[:3]
            return "#%02x%02x%02x" % (
                _color_channel(r), _color_channel(g), _color_channel(b))
        h, s, l = parts[:3]
        sval = float(s.rstrip("%")) / 100.0 if s.endswith("%") else float(s)
        lval = float(l.rstrip("%")) / 100.0 if l.endswith("%") else float(l)
        r, g, b = _hsl_to_rgb(_parse_hue(h), sval, lval)
        return "#%02x%02x%02x" % (
            int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))
    # 3/4/6/8-digit hex: Tk only accepts #rgb and #rrggbb reliably, so expand.
    if name.startswith("#") and len(name) in (4, 5):
        n = "".join(c * 2 for c in name[1:])
        if len(n) == 8 and n[6:] == "00":
            return None  # alpha 0
        return "#" + n[:6]
    if len(name) == 9 and name.startswith("#"):
        if name[7:] == "00":
            return None  # #rrggbbaa with alpha 0
        return name[:7]
    return name  # Tk understands names and #rrggbb


def _block_padding(node):
    """Vertical padding (top + bottom) of a block's own style."""
    return parse_px(node.style.get("padding-top", "0")) \
        + parse_px(node.style.get("padding-bottom", "0"))


def _dispatch_layout(box):
    """Route a laid-out box to its display-type layout algorithm."""
    node = box.node
    disp = node.style.get("display", "") if isinstance(node, Element) else ""
    if disp == "flex":
        box._layout_flex()
    elif disp == "grid":
        box._layout_grid()
    elif disp in ("table", "inline-table") or \
            (isinstance(node, Element) and node.tag == "table"):
        box._layout_table()
    elif box.layout_mode() == "block":
        box._layout_block()
    else:
        box._layout_inline()


def _paint_bg(box, cmds, require_size=True):
    """Emit a DrawRect for `box`'s resolved background color, if any."""
    node = box.node
    if isinstance(node, Element):
        bg = resolve_color(node.style.get("background-color")) or \
            resolve_color(node.style.get("background"))
        if bg and (not require_size or box.width > 0 and box.height > 0):
            cmds.append(DrawRect(box.x, box.y, box.x + box.width,
                                 box.y + box.height, bg))


class LayoutBox:
    """Base class carrying geometry."""

    def __init__(self, node, parent, previous):
        self.node = node
        self.parent = parent
        self.previous = previous
        self.children = []
        self.x = self.y = self.width = self.height = 0
        # (left, top, right, bottom, node) rects for form controls, used for
        # hit-testing clicks on inputs and submit buttons.
        self.input_boxes = []
        # Floats placed by a containing block; entries are dicts with edge
        # coordinates and the side they occupy. Consulted by inline layout to
        # wrap text and by `clear` to push content below floats.
        self.float_regions = []
        # Vertical cursor for each float side within this block so floats
        # stack without overlapping (keyed "left" / "right").
        self._float_cursor = {"left": 0.0, "right": 0.0}


class _LineItem:
    """A word or inline image pending on the current output line."""

    __slots__ = ("kind", "x", "text", "font", "color", "node", "w", "h", "photo",
                 "bg", "pl", "pr", "pt", "pb")

    def __init__(self, kind, x, text, font, color, node, w, h, photo=None,
                 bg=None, pl=0, pr=0, pt=0, pb=0):
        self.kind = kind  # "text", "img" or "pill"
        self.x = x
        self.text = text
        self.font = font
        self.color = color
        self.node = node
        self.w = w
        self.h = h
        self.photo = photo
        self.bg = bg
        self.pl = pl
        self.pr = pr
        self.pt = pt
        self.pb = pb

    @property
    def ascent(self):
        if self.kind == "img":
            return int(self.h * 0.82)
        return _metrics(self.font, "ascent")

    @property
    def descent(self):
        if self.kind == "img":
            return int(self.h * 0.18)
        return _metrics(self.font, "descent")


class BlockLayout(LayoutBox):
    # Horizontal padding drawn on either side of table cell content.
    CELL_PAD = 4

    def layout(self):
        node = self.node
        self.x = self.parent.x + parse_px(node.style.get("margin-left", "0"))
        self.width = self.parent.width - parse_px(node.style.get("margin-left", "0")) \
            - parse_px(node.style.get("margin-right", "0"))
        if self.previous:
            # margin-bottom is "0" for text nodes, so this is safe for them too.
            self.y = self.previous.y + self.previous.height \
                + parse_px(self.previous.node.style.get("margin-bottom", "0"))
        else:
            self.y = self.parent.y + parse_px(
                self.parent.node.style.get("padding-top", "0"))
        self.y += parse_px(node.style.get("margin-top", "0"))
        # `clear` pushes a block (or a float/line box) below its side's floats.
        if getattr(self, "_y_floor", None) is not None:
            self.y = max(self.y, self._y_floor)

        if getattr(self, "_float_pos", None) is not None:
            # A float shrinks to fit its content and is pinned to a given
            # x/y; its children are laid out within that box.
            self.x, self.y, self.width = self._float_pos
        if getattr(self, "_absolute_pos", None) is not None:
            # An absolutely positioned box is out of flow and pinned to the
            # containing block; it never pushes siblings or grows the parent.
            self.x, self.y, self.width = self._absolute_pos
        _dispatch_layout(self)

    def layout_mode(self):
        node = self.node
        if isinstance(node, Text):
            return "inline"
        for child in node.children:
            if isinstance(child, Text):
                if child.text.strip():
                    return "inline"
            elif child.style.get("display") not in ("none",) and \
                    (child.tag in INLINE_ELEMENTS and child.style.get("display") is None
                     or child.style.get("display") in ("inline", "inline-block")):
                continue
            elif isinstance(child, Element) and child.style.get("display") == "none":
                continue
            else:
                return "block"
        # No block children -> inline (even if empty).
        return "inline"

    def _layout_block(self):
        previous = None
        float_boxes = []
        for child in self.node.children:
            if isinstance(child, Element) and child.style.get("display") == "none":
                continue
            if isinstance(child, Text) and not child.text.strip():
                continue
            if isinstance(child, Element) and \
                    child.style.get("float") in ("left", "right"):
                fb = self._layout_float(child)
                float_boxes.append(fb)
                continue
            if isinstance(child, Element) and \
                    child.style.get("position") in ("absolute", "fixed"):
                # position:absolute/fixed boxes are out of flow: they don't
                # push siblings or stretch the parent (e.g. hidden dropdowns
                # and overlays that must not take up layout space).
                self.children.append(self._layout_absolute(child))
                continue
            box = BlockLayout(child, self, previous)
            clear = child.style.get("clear") if isinstance(child, Element) else ""
            if clear:
                box._y_floor = self._cleared(clear, getattr(box, "_y_floor", 0.0))
            self.children.append(box)
            previous = box
        content_h = 0
        last_flow = None
        for box in self.children:
            box.layout()
            if getattr(box, "_absolute_pos", None) is None:
                last_flow = box
        if last_flow is not None:
            content_h = (last_flow.y + last_flow.height
                         + parse_px(last_flow.node.style.get("margin-bottom", "0"))
                         - self.y)
        for f in self.float_regions:
            content_h = max(content_h, f["bottom"] - self.y)
        self.children.extend(float_boxes)
        self.height = content_h + _block_padding(self.node)

    # -- floats ----------------------------------------------------------

    def _layout_absolute(self, el):
        """Lay out a `position:absolute/fixed` element out of the flow, pinned
        to this box's origin plus any top/left offsets. Real sites use these
        for dropdowns, modals and tooltips so they never stretch the parent;
        hidden ones are skipped by painting."""
        box = BlockLayout(el, self, None)
        x = self.x + parse_px(el.style.get("left", ""), 0)
        y = self.y + parse_px(el.style.get("top", ""), 0)
        right = parse_px(el.style.get("right", ""), 0)
        w = max(0.0, self.width - right) if right else self.width
        box._absolute_pos = (x, y, w)
        return box

    def _layout_float(self, el):
        """Position a `float: left/right` box out of flow, shrink-to-fit its
        width, and record its region so inline content wraps around it."""
        side = el.style.get("float")
        ml = parse_px(el.style.get("margin-left", "0"))
        mr = parse_px(el.style.get("margin-right", "0"))
        mb = parse_px(el.style.get("margin-bottom", "0"))
        mt = parse_px(el.style.get("margin-top", "0"))
        avail = max(0.0, self.width - ml - mr)
        mi, ma = self._measure_width(el)
        w = max(1.0, min(avail, max(mi, ma)))
        css_w = el.style.get("width")
        if css_w and css_w.strip() not in (
                "auto", "fit-content", "min-content", "max-content"):
            w = max(1.0, min(avail, parse_px(css_w, avail)))

        clear = el.style.get("clear")
        top = self._cleared(clear, self._float_cursor[side])
        y = self.y + mt + top
        x = self.x + ml if side == "left" else self.x + self.width - w - mr

        box = BlockLayout(el, self, None)
        box._float_pos = (x, y, w)
        box.layout()
        h = box.height
        css_h = el.style.get("height")
        if css_h:
            h = max(h, parse_px(css_h, h))
            box.height = h
        self._float_cursor[side] = top + h + mb
        self.float_regions.append({
            "side": side, "top": y, "bottom": y + h + mb,
            "left": x, "right": x + w, "box": box})
        return box

    def _clear_bottom(self, side):
        bottom = 0.0
        for f in self.float_regions:
            if f["side"] == side:
                bottom = max(bottom, f["bottom"])
        return bottom

    def _cleared(self, clear, base):
        """Raise `base` to clear the floats `clear` targets (left/right/both)."""
        if clear:
            for side in ("left", "right"):
                if clear == "both" or clear == side:
                    base = max(base, self._clear_bottom(side))
        return base

    def _all_float_regions(self):
        """Float regions visible from this box: its own plus every ancestor's,
        all expressed in absolute page coordinates."""
        regions = []
        box = self
        while box is not None:
            regions.extend(box.float_regions)
            box = box.parent
        return regions

    def _line_bounds(self):
        """Horizontal span available to the current line, clipped by any
        floats whose vertical span covers the line's top."""
        x0 = self.x
        x1 = self.x + self.width
        y = self.cursor_y
        for f in self._all_float_regions():
            if f["top"] <= y < f["bottom"]:
                if f["side"] == "left":
                    x0 = max(x0, f["right"])
                else:
                    x1 = min(x1, f["left"])
        return x0, max(x0, x1)

    # -- tables ----------------------------------------------------------

    def _layout_table(self):
        node = self.node
        self.children = []

        rows = []
        for child in node.children:
            if not isinstance(child, Element):
                continue
            if child.tag == "tr":
                rows.append(child)
            elif child.tag in ("thead", "tbody", "tfoot", "caption"):
                for g in child.children:
                    if isinstance(g, Element) and g.tag == "tr":
                        rows.append(g)
        if not rows:
            self.height = 0
            return

        # Put every cell on a (row, column) grid, skipping columns blocked by
        # an upward rowspan.
        grid, num_cols, occupied = [], 0, {}
        for tr in rows:
            row_cells, c = [], 0
            for child in tr.children:
                if not isinstance(child, Element) or child.tag not in ("td", "th"):
                    continue
                while occupied.get(c, 0) > 0:
                    c += 1
                try:
                    cs = max(1, int(child.attributes.get("colspan", "1") or 1))
                    rs = max(1, int(child.attributes.get("rowspan", "1") or 1))
                except ValueError:
                    cs = rs = 1
                row_cells.append((c, cs, rs, child))
                if rs > 1:
                    occupied[c] = max(occupied.get(c, 0), rs - 1)
                num_cols = max(num_cols, c + cs)
                c += cs
            grid.append(row_cells)

        # Column min/max content widths (spanning cells share their width
        # across the columns they cover so a single full-width cell still
        # gives the table usable columns).
        col_min = [0.0] * num_cols
        col_max = [0.0] * num_cols
        for row_cells in grid:
            for c, cs, rs, el in row_cells:
                mi, ma = self._measure_width(el)
                # The content box is the column minus CELL_PAD on each side,
                # so the measured single-line width only fits if the padding
                # is added back to the column's min/max widths.
                mi += 2 * self.CELL_PAD
                ma += 2 * self.CELL_PAD
                share = max(1, cs)
                for k in range(c, c + cs):
                    col_min[k] = max(col_min[k], mi / share)
                    col_max[k] = max(col_max[k], ma / share)

        # Compute final column widths up front so cell content is measured at
        # its real width instead of wrapping onto a line per word. Auto tables
        # shrink to fit their content (width: fit-content); an explicit width
        # (px or %) stretches the table to that width when content is short.
        avail = self.width
        explicit = None
        css_w = node.style.get("width")
        if css_w:
            cw = css_w.strip()
            if cw.endswith("%"):
                try:
                    explicit = avail * min(100.0, max(0.0, float(cw[:-1]))) / 100.0
                except ValueError:
                    pass
            elif cw == "fit-content":
                explicit = None
            else:
                explicit = parse_px(cw, avail)
        self._widths = self._distribute_column_widths(avail, col_min, col_max)
        if explicit is not None:
            used = sum(self._widths)
            if explicit > used:
                grow = [max(0.0, m - n0) for m, n0 in zip(self._widths, col_min)]
                gsum = sum(grow) or 1.0
                extra = explicit - used
                self._widths = [mi + extra * (g / gsum)
                                for mi, g in zip(self._widths, grow)]
        # Auto tables shrink to their used column widths; the table box must
        # match the cells so borders/backgrounds don't extend past the content.
        if sum(self._widths) > 0:
            self.width = min(self.width, sum(self._widths))

        cells = []  # (ri, col, cs, rs, el, content_block, content_h, col_w)
        for ri, row_cells in enumerate(grid):
            for c, cs, rs, el in row_cells:
                col_w = sum(self._widths[c:c + cs])
                content_w = max(1, col_w - 2 * self.CELL_PAD)
                cb = BlockLayout(el, self, None)
                cb.x = 0
                cb.y = 0
                cb.width = content_w
                if cb.layout_mode() == "block":
                    cb._layout_block()
                else:
                    cb._layout_inline()
                cells.append((ri, c, cs, rs, el, cb, cb.height, col_w))

        # Row heights from non-spanning cells, then let rowspans stretch rows.
        row_h = [0.0] * len(grid)
        for ri, c, cs, rs, el, cb, content_h, col_w in cells:
            if rs == 1:
                row_h[ri] = max(row_h[ri], content_h + 2 * self.CELL_PAD)
        for ri, c, cs, rs, el, cb, content_h, col_w in cells:
            if rs <= 1:
                continue
            span_sum = sum(row_h[ri:ri + rs])
            overflow = (content_h + 2 * self.CELL_PAD) - span_sum
            if overflow > 0:
                row_h[ri] += overflow

        # Build row / cell boxes.
        y_cursor = self.y
        row_boxes = []
        for ri in range(len(grid)):
            row = RowLayout(rows[ri], self, row_boxes[-1] if row_boxes else None)
            row.x = self.x
            row.y = y_cursor
            row.width = self.width
            row.height = row_h[ri]
            self.children.append(row)
            row_boxes.append(row)
            y_cursor += row_h[ri]

        for ri, c, cs, rs, el, cb, content_h, col_w in cells:
            row = row_boxes[ri]
            cell = CellLayout(el, row, None)
            cell.x = self.x + sum(self._widths[:c])
            cell.y = row.y
            cell.width = sum(self._widths[c:c + cs])
            cell.height = sum(row_h[ri:ri + rs]) if rs > 1 else row_h[ri]
            cell.content = self._render_cell(cb, cell, content_h)
            row.children.append(cell)

        self.display_list = [
            DrawOutline(self.x, self.y, self.x + self.width, y_cursor, "#bbbbbb", 1)]
        self.height = y_cursor - self.y

    def _measure_width(self, el):
        """Approximate a cell's min (longest word) and preferred single-line
        content widths so the auto table layout can size its columns."""
        font = _node_font(el)
        cache = self._image_cache()
        total, longest = 0.0, 0.0
        stack = [el]
        while stack:
            n = stack.pop()
            if isinstance(n, Text):
                for word in n.text.split():
                    w = _measure(font, word)
                    total += w + _measure(font, " ")
                    longest = max(longest, w)
            elif isinstance(n, Element):
                if n.tag == "img":
                    # Size against the real pixels when the image has been
                    # decoded, not the "[img]" placeholder, or the column is
                    # drawn far too narrow and the image overlaps its
                    # neighbours. Matches _inline_img's advance (w * 1.25).
                    photo = None
                    src = n.attributes.get("src")
                    if src and cache:
                        photo = cache.get(src)
                    if photo is not None:
                        v = float(photo.width()) * 1.25
                    else:
                        v = _measure(font, "[img]") + 8
                    total += v
                    longest = max(longest, v)
                elif n.tag in ("input", "textarea", "button", "select"):
                    v = 110.0
                    total += v
                    longest = max(longest, v)
                for ch in reversed(n.children):
                    stack.append(ch)
        return max(1.0, longest), max(1.0, total)

    @staticmethod
    def _distribute_column_widths(avail, col_min, col_max):
        """Auto table layout: fit columns into a given width, honouring each
        column's content-based min and preferred widths."""
        n = len(col_min)
        if n == 0:
            return []
        total_min = sum(col_min)
        total_max = sum(col_max)
        if avail <= total_min or total_max <= total_min:
            return list(col_min)
        if avail >= total_max:
            return list(col_max)
        grow = [max(0.0, m - n0) for m, n0 in zip(col_max, col_min)]
        gsum = sum(grow) or 1.0
        extra = avail - total_min
        return [mi + extra * (g / gsum) for mi, g in zip(col_min, grow)]

    def _render_cell(self, cb, cell_box, content_h):
        """Flatten a cell's laid-out subtree into absolute paint coordinates,
        applying padding and the cell's vertical alignment."""
        pad = self.CELL_PAD
        cell_node = cb.node
        valign = cell_node.style.get("vertical-align",
                                     "middle" if cell_node.tag in ("td", "th")
                                     else "top") if isinstance(cell_node, Element) \
            else "top"
        cap = max(0, cell_box.height - 2 * pad - content_h)
        dy = pad
        if valign == "middle":
            dy += cap / 2
        elif valign == "bottom":
            dy += cap
        out = []
        self._flatten_paint(cb, out, cell_box.x + pad, cell_box.y + dy)
        return out

    def _shift_cmd(self, cmd, dx, dy):
        if hasattr(cmd, "left"):
            cmd.left += dx
        if hasattr(cmd, "top"):
            cmd.top += dy
        if hasattr(cmd, "right"):
            cmd.right += dx
        if hasattr(cmd, "bottom"):
            cmd.bottom += dy

    def _flatten_paint(self, box, out, dx, dy):
        for cmd in box.paint():
            self._shift_cmd(cmd, dx, dy)
            out.append(cmd)
        for child in box.children:
            self._flatten_paint(child, out, dx, dy)
        for lx, ty, rx, by, node in getattr(box, "input_boxes", ()):
            self.input_boxes.append((lx + dx, ty + dy, rx + dx, by + dy, node))

    def _translate(self, box, dx, dy):
        """Shift `box` subtree geometry, paint commands, and input hit-boxes
        by (dx, dy). Used when a laid-out subtree (flex/grid item) is
        repositioned after measuring."""
        box.x += dx
        box.y += dy
        for cmd in getattr(box, "display_list", ()):
            self._shift_cmd(cmd, dx, dy)
        # Table cells keep their flattened paint commands in `content`
        # rather than display_list, so those must be translated too, or the
        # cell's text/images stay pinned at the pre-move position and overlap
        # the surrounding content.
        for cmd in getattr(box, "content", ()):
            self._shift_cmd(cmd, dx, dy)
        for child in box.children:
            self._translate(child, dx, dy)
        if getattr(box, "input_boxes", None):
            box.input_boxes = [(lx + dx, ty + dy, rx + dx, by + dy, n)
                               for lx, ty, rx, by, n in box.input_boxes]

    def _gaps(self, node):
        """Resolve gap/row-gap/column-gap shorthand into explicit gaps."""
        gap = parse_px(node.style.get("gap", ""))
        row_gap = parse_px(node.style.get("row-gap", ""))
        column_gap = parse_px(node.style.get("column-gap", ""))
        if gap and not row_gap:
            row_gap = gap
        if gap and not column_gap:
            column_gap = gap
        return row_gap, column_gap

    def _flex_items(self):
        """Child elements (and non-empty text) of a flex/grid container."""
        items = []
        for child in self.node.children:
            if isinstance(child, Text):
                if child.text.strip():
                    items.append(child)
                continue
            if child.style.get("display") == "none":
                continue
            items.append(child)
        return items

    def _layout_item(self, el, w):
        """Lay a flex/grid item in a scratch box at x=0/y=0 of width `w`,
        applying any explicit CSS height; returns (box, box.height)."""
        box = BlockLayout(el, self, None)
        box.x = 0
        box.y = 0
        box.width = w
        _dispatch_layout(box)
        css_h = parse_px(el.style.get("height", "")) if isinstance(el, Element) else 0.0
        box.height = max(box.height, css_h)
        return box, box.height

    def _layout_flex(self):
        """Subset flexbox: `flex-direction: row/column`, `gap`, flex item
        `flex-grow` (and `flex-basis` in px), `justify-content` and
        `align-items`. No wrapping (nowrap only)."""
        node = self.node
        direction = (node.style.get("flex-direction", "row")
                     if isinstance(node, Element) else "row")
        if direction not in ("row", "column"):
            direction = "row"
        row_gap, column_gap = self._gaps(node)
        justify = (node.style.get("justify-content", "flex-start")
                   if isinstance(node, Element) else "flex-start")
        align = (node.style.get("align-items", "stretch")
                 if isinstance(node, Element) else "stretch")

        items = self._flex_items()
        if not items:
            self.height = parse_px(node.style.get("height", ""), 0)
            return

        def margins(el):
            if not isinstance(el, Element):
                return 0.0, 0.0, 0.0, 0.0
            return (parse_px(el.style.get("margin-left", "0")),
                    parse_px(el.style.get("margin-right", "0")),
                    parse_px(el.style.get("margin-top", "0")),
                    parse_px(el.style.get("margin-bottom", "0")))

        def grows(el):
            if isinstance(el, Element):
                v = el.style.get("flex-grow")
                if v is None:
                    fl = el.style.get("flex")
                    if fl:
                        v = fl.split()[0]
                try:
                    return max(0.0, float(v))
                except (TypeError, ValueError):
                    pass
            return 0.0

        def basis(el):
            if not isinstance(el, Element):
                return None
            b = el.style.get("flex-basis")
            if b and b.endswith("px"):
                return parse_px(b)
            fl = el.style.get("flex")
            if fl:
                for tok in fl.split():
                    if tok.endswith("px"):
                        return parse_px(tok)
            css_w = el.style.get("width")
            if css_w and css_w.endswith("px"):
                return parse_px(css_w)
            return None

        def natural_width(el):
            mi, ma = self._measure_width(el)
            b = basis(el)
            if b is not None:
                return b
            return max(mi, min(ma, self.width))

        def distra_leftover(extra, widths):
            """Grow flex items proportionally to their `flex-grow`."""
            gs = [grows(el) for el in items]
            gsum = sum(gs)
            if gsum > 0 and extra > 0:
                out = list(widths)
                for i, g in enumerate(gs):
                    out[i] += extra * (g / gsum)
                return out, 0.0
            return widths, extra

        def justify_start(start, leftover, end_alias):
            """First-item cursor for justify-content plus the per-gap amount
            to add between space-between items."""
            cursor = start
            gap = 0.0
            if justify in ("center", "middle"):
                cursor = start + leftover / 2
            elif justify in ("flex-end", "end", end_alias):
                cursor = start + leftover
            elif justify == "space-between" and len(items) > 1:
                cursor = start
                gap = leftover / (len(items) - 1)
            elif justify in ("space-around", "space-evenly"):
                parts = leftover / len(items) if justify == "space-around" \
                    else leftover / (len(items) + 1)
                cursor = start + parts
            return cursor, gap

        if direction == "row":
            widths = [natural_width(el) for el in items]
            margin_w = sum(ml + mr for ml, mr, _, _ in (margins(el) for el in items))
            gap_total = column_gap * (len(items) - 1)
            avail = self.width
            total = sum(widths) + margin_w + gap_total
            if total > avail:
                leftover = 0.0
                available = max(0.0, avail - gap_total - margin_w)
                if sum(widths) > 0:
                    factor = available / sum(widths)
                    widths = [w * factor for w in widths]
                else:
                    widths = [0.0] * len(widths)
            else:
                widths, leftover = distra_leftover(avail - total, widths)
            total = sum(widths) + margin_w + gap_total + leftover

            # justify-content places the leftover space.
            cursor, extra = justify_start(self.x, leftover, "right")

            placement = []
            for i, el in enumerate(items):
                ml, mr, mt, mb = margins(el)
                w = widths[i]
                extra_gap = (extra if (justify == "space-between" and i > 0) else 0.0) \
                    if justify == "space-between" else 0.0
                x = cursor + ml
                box, ch = self._layout_item(el, w)
                placement.append((box, ch, mt, mb, x))
                cursor += ml + w + mr + column_gap + extra_gap

            max_h = max(ch + mt + mb for _, ch, mt, mb, _ in placement) or 0.0
            stretch_h = parse_px(node.style.get("height", ""), 0)
            if stretch_h:
                self.height = stretch_h
                max_h = max(max_h, stretch_h)
            else:
                self.height = max_h
            for box, ch, mt, mb, x in placement:
                if align == "stretch":
                    box.height = max_h - mt - mb
                    y = self.y + mt
                elif align in ("flex-end", "end"):
                    box.height = ch
                    y = self.y + self.height - mb - ch
                elif align in ("center", "middle"):
                    box.height = ch
                    y = self.y + mt + (self.height - mt - ch - mb) / 2
                else:
                    box.height = ch
                    y = self.y + mt
                self._translate(box, x, y)
                self.children.append(box)

        else:  # column
            avails = []
            for el in items:
                b = basis(el)
                if b is not None:
                    avails.append(min(self.width, b))
                else:
                    mi, ma = self._measure_width(el)
                    if align == "stretch":
                        avails.append(self.width)
                    else:
                        avails.append(max(mi, min(ma, self.width)))

            placement = []
            for el, w in zip(items, avails):
                ml, mr, mt, mb = margins(el)
                box, ch = self._layout_item(el, w)
                placement.append((el, box, ch, ml, mr, mt, mb))

            gap_total = row_gap * (len(items) - 1)
            margin_h = sum(mt + mb for _, _, _, _, _, mt, mb in placement)
            content_h = sum(ch for _, _, ch, _, _, _, _ in placement)
            total = content_h + gap_total + margin_h
            css_h = parse_px(node.style.get("height", ""), 0)
            extra = max(0.0, css_h - total) if css_h else 0.0

            heights = [ch for _, _, ch, _, _, _, _ in placement]
            if extra > 0:
                heights, extra = distra_leftover(extra, heights)

            cursor, extra_gap = justify_start(self.y, extra, "bottom")

            for i, (el, box, ch, ml, mr, mt, mb) in enumerate(placement):
                h = heights[i]
                if align == "stretch" and not (
                        isinstance(el, Element) and el.style.get("width")):
                    x = self.x
                elif align in ("flex-end", "end", "right"):
                    x = self.x + (self.width - ml - mr - box.width)
                elif align in ("center", "middle"):
                    x = self.x + ml + ((self.width - ml - mr - box.width) / 2)
                else:
                    x = self.x + ml
                y = cursor + mt
                self._translate(box, x, y)
                cursor += mt + h + mb + row_gap + (
                    extra_gap if justify == "space-between" and i > 0 else 0.0)
                self.children.append(box)

            self.height = max(css_h, cursor - self.y) if css_h else cursor - self.y

        self.height += _block_padding(node)

    def _parse_grid_areas(self, value):
        """Parse `grid-template-areas` (whitespace-separated quoted strings,
        one row per string, '.' is an empty cell) into a map of area name ->
        (row, rowspan, col, colspan)."""
        if not value:
            return {}
        rows = []
        for open_q, close_q in re.findall(r"'([^']*)'|\"([^\"]*)\"", value):
            rows.append((open_q if open_q else close_q).split())
        areas = {}
        for r, row in enumerate(rows):
            for c, name in enumerate(row):
                if name == "." or name in areas:
                    continue
                cspan = 1
                while c + cspan < len(row) and row[c + cspan] == name:
                    cspan += 1
                rspan = 1
                while r + rspan < len(rows):
                    nxt = rows[r + rspan]
                    if c + cspan <= len(nxt) and \
                            all(x == name for x in nxt[c:c + cspan]):
                        rspan += 1
                    else:
                        break
                areas[name] = (r, rspan, c, cspan)
        return areas

    def _layout_grid(self):
        """Subset CSS grid: `grid-template-columns` (px/%/fr/auto), row
        auto-placement with `grid-column`/`grid-row` (start, span, or
        start/end), `gap`, and auto row heights from content."""
        node = self.node
        self.children = []

        def parse_tracks(value):
            if not value:
                return []
            out = []
            for tok in value.split():
                tok = tok.strip()
                if not tok:
                    continue
                # minmax(<min>, <max>): a track between two bounds. Use the
                # definite bound (max first, then min), which is right for
                # the common `minmax(0, 1fr)` and `minmax(15.5rem, auto)`.
                if tok.startswith("minmax(") and tok.endswith(")"):
                    parts = [p.strip() for p in tok[len("minmax("):-1].split(",")]
                    chosen = None
                    for p in reversed(parts):
                        if p.lower() not in ("auto", "min-content", "max-content",
                                             "fit-content"):
                            chosen = p
                            break
                    if chosen is None:
                        out.append(("auto", 0.0))
                        continue
                    tok = chosen
                for kind, suffix, cut in (("fr", "fr", -2), ("pct", "%", -1),
                                          ("px", "px", -2), ("rem", "rem", -3)):
                    if tok.endswith(suffix):
                        try:
                            v = float(tok[:cut])
                            if kind == "rem":
                                kind, v = "px", v * 16.0
                            out.append((kind, v))
                        except ValueError:
                            out.append(("auto", 0.0))
                        break
                else:
                    out.append(("auto", 0.0))
            return out

        col_def = parse_tracks(node.style.get("grid-template-columns", ""))
        row_def = parse_tracks(node.style.get("grid-template-rows", ""))
        # The `grid-template` shorthand (`<rows> / <columns>`) is common on
        # real sites (Wikipedia's header) and was silently dropped, leaving
        # the grid at a single auto column that collapses wide content.
        template = node.style.get("grid-template", "")
        if "/" in template:
            t_rows, t_cols = template.split("/", 1)
            if not row_def:
                row_def = parse_tracks(t_rows)
            if not col_def:
                col_def = parse_tracks(t_cols)
        row_gap, col_gap = self._gaps(node)

        # `grid-template-areas` maps named cells to a row/column span:
        # "'siteNotice siteNotice' 'columnStart pageContent' 'footer footer'"
        # -> siteNotice spans both columns of row 0, etc. Items reference a
        # cell by name via `grid-area`, and are placed there (row-major when
        # the name is duplicated, as real browsers do).
        areas = self._parse_grid_areas(node.style.get("grid-template-areas", ""))
        # The number of columns/rows implied by the areas template must be
        # created even if the item tracks weren't declared.
        if areas:
            rows = max(r + rspan for r, rspan, _, _ in areas.values())
            cols = max(c + cspan for _, _, c, cspan in areas.values())
            if not col_def and cols:
                col_def = [("auto", 0.0)] * cols
            if not row_def and rows:
                row_def = [("auto", 0.0)] * rows

        items = self._flex_items()
        if not items:
            self.height = parse_px(node.style.get("height", ""), 0)
            return
        if not col_def:
            col_def = [("auto", 0.0)]

        def parse_num(tok):
            try:
                return int(tok)
            except (TypeError, ValueError):
                return None

        def placement_of(el, areas):
            """Return (col_start, col_span, row_start, row_span), 0-based
            starts; None means auto. Understands 'start/end', 'start/span N',
            'span N', or bare numbers, plus a `grid-area` name."""
            def sides(prop):
                v = el.style.get(prop)
                start = span = None
                if v:
                    parts = [p.strip() for p in v.split("/")]
                    if len(parts) == 2:
                        a, b = parts
                        if a.startswith("span"):
                            span = parse_num(a.split()[1]) or 1
                            end = parse_num(b)
                            if end is not None:
                                start = end - span
                        elif b.startswith("span"):
                            start = parse_num(a)
                            span = parse_num(b.split()[1]) or 1
                        else:
                            start = parse_num(a)
                            end = parse_num(b)
                            if start is not None and end is not None:
                                span = end - start
                    else:
                        v = parts[0]
                        if v.startswith("span"):
                            span = parse_num(v.split()[1]) or 1
                        else:
                            start = parse_num(v)
                return start, span or 1
            cs, cspan = sides("grid-column")
            rs, rspan = sides("grid-row")
            area = el.style.get("grid-area")
            if area and "/" not in area and area in areas:
                ars, arspan, acs, acspan = areas[area]
                if cs is None:
                    cs, cspan = acs, acspan
                if rs is None:
                    rs, rspan = ars, arspan
            return cs, cspan, rs, rspan

        # Auto-place into rows of `col_def` columns, extending tracks when an
        # explicit column goes past the template. Row-major cursor so items
        # fill left-to-right, top-to-bottom by default.
        placements = []  # (row, col, cspan, rspan, el)
        occupied = {}
        cur_r, cur_c = 0, 0
        ncols_so_far = len(col_def)
        for el in items:
            cs, cspan, rs, rspan = placement_of(el, areas)
            ncols_so_far = max(ncols_so_far, cspan)
            if rs is not None:
                row = max(0, rs)
            else:
                row = cur_r
            if cs is not None:
                col = max(0, cs)
            else:
                col = None
            # Wrap the sparse cursor to the next row once it passes the last
            # column known so far (matches row auto-placement).
            while row == cur_r and cur_c + cspan > ncols_so_far:
                cur_r += 1
                cur_c = 0

            if col is None:
                # Scan row-major from the current cursor for a free slot.
                r = max(row, cur_r)
                start_c = cur_c if r == cur_r else 0
                while True:
                    c = start_c if r == row else 0
                    while c < 4096:
                        if all((r + rr, c + cc) not in occupied
                               for rr in range(rspan) for cc in range(cspan)):
                            col = c
                            break
                        c += 1
                    if col is not None:
                        break
                    r += 1
                    if r > 4096:
                        raise RuntimeError("grid auto-placement runaway")
                row = r
            ncols_so_far = max(ncols_so_far, col + cspan)
            for rr in range(rspan):
                for cc in range(cspan):
                    occupied[(row + rr, col + cc)] = True
            placements.append((row, col, cspan, rspan, el))
            cur_r, cur_c = row, col + cspan

        # Determine the number of columns actually used (template may be
        # widened by explicit placement).
        ncols = len(col_def)
        for row, col, cspan, rspan, el in placements:
            ncols = max(ncols, col + cspan)
        col_def += [("auto", 0.0)] * (ncols - len(col_def))
        nrows = max((row + rspan for row, _, _, rspan, _ in placements), default=0)

        # Column widths. Auto tracks size to the widest min-content item.
        col_min = [0.0] * ncols
        for row, col, cspan, rspan, el in placements:
            if cspan == 1:
                mi, _ = self._measure_width(el)
                col_min[col] = max(col_min[col], mi)
        col_w = [0.0] * ncols
        avail = self.width
        fr_sum = sum(v for k, v in col_def if k == "fr")
        used = 0.0
        for i, (k, v) in enumerate(col_def):
            if k == "px":
                col_w[i] = v
            elif k == "pct":
                col_w[i] = avail * v / 100.0
            elif k == "auto":
                col_w[i] = col_min[i]
            used += col_w[i]
        remaining = max(0.0, avail - used - col_gap * (ncols - 1))
        for i, (k, v) in enumerate(col_def):
            if k == "fr" and fr_sum:
                col_w[i] = remaining * v / fr_sum
        # Recompute fr widths from what's left after fixed columns filled the
        # container (rare over-constraint -> shrink fr tracks proportionally).
        total = sum(col_w) + col_gap * (ncols - 1)
        if total > avail:
            scale = max(0.0, (avail - col_gap * (ncols - 1)) / (sum(col_w) or 1))
            col_w = [w * scale for w in col_w]

        # Lay out each item in a scratch box to learn its content height.
        placed = []
        for row, col, cspan, rspan, el in placements:
            w = sum(col_w[col:col + cspan]) + col_gap * (cspan - 1)
            box, _ = self._layout_item(el, w)
            placed.append((row, col, cspan, rspan, el, box, w))

        # Row heights: auto rows grow to their tallest item; explicit rows
        # keep their track size (items overflow rather than stretch).
        row_h = [0.0] * nrows
        for row, col, cspan, rspan, el, box, w in placed:
            if rspan == 1:
                row_h[row] = max(row_h[row], box.height)
        for i, (k, v) in enumerate(row_def):
            if i < nrows and k == "px":
                row_h[i] = v
        # Rowspans: let a spanning item push its top row down.
        for row, col, cspan, rspan, el, box, w in placed:
            if rspan <= 1:
                continue
            span_h = sum(row_h[row:row + rspan]) + row_gap * (rspan - 1)
            if box.height > span_h:
                row_h[row] += box.height - span_h

        # Position via translate so item content moves with its box.
        x_cursor = self.x
        for i in range(ncols):
            if i:
                x_cursor += col_gap
            w = col_w[i]
            for row, col, cspan, rspan, el, box, _w in placed:
                if col == i:
                    y_cursor = self.y
                    for r in range(row):
                        y_cursor += row_h[r] + row_gap
                    self._translate(box, x_cursor, y_cursor)
                    self.children.append(box)
            x_cursor += w

        self.height = sum(row_h) + row_gap * (nrows - 1) + _block_padding(node)

    def _layout_inline(self):
        self.display_list = []
        clear = self.node.style.get("clear") if isinstance(self.node, Element) else ""
        self.cursor_y = self._cleared(clear, self.y)
        self.cursor_x = self._line_bounds()[0]
        self.line = []  # pending words on the current line

        # List item bullet.
        if isinstance(self.node, Element) and \
                self.node.style.get("display") == "list-item":
            self._draw_bullet()

        self.recurse(self.node)
        self.flush()
        self.height = self.cursor_y - self.y \
            + parse_px(self.node.style.get("padding-bottom", "0"))

    # -- inline layout ---------------------------------------------------

    def recurse(self, node):
        if isinstance(node, Text):
            return self.text(node)
        if node.style.get("display") == "none":
            return
        if node.tag == "br":
            return self.flush(force=True)
        if node.tag == "img":
            return self._inline_img(node)
        if node.tag in ("input", "textarea", "button"):
            return self._inline_button(node) if node.tag == "button" \
                else self._inline_input(node)
        if node.tag == "hr":
            self.flush()
            return self._draw_hr(node)
        # <select> is rendered as a read-only field for now.
        if node.tag == "select":
            return self._inline_select(node)
        # display:inline-block with a background (e.g. Google's "Sign in"
        # pill) paints as one box instead of split words. Padding alone does
        # not trigger this: inline-blocks without a background keep their
        # normal flow so their inline children stay together.
        style = node.style
        if style.get("display") == "inline-block" and isinstance(node, Element):
            bg = resolve_color(style.get("background-color")) or \
                resolve_color(style.get("background"))
            if bg:
                pt, pr, pb, pl = _padding_box(style)
                self._inline_pill(node, bg, pl, pr, pt, pb)
                return
        for child in node.children:
            self.recurse(child)

    def text(self, node):
        font = _node_font(node)
        color = resolve_color(node.style.get("color", "black")) or "black"
        white_space = node.style.get("white-space", "normal")
        content = node.text
        if white_space == "pre":
            for k, line in enumerate(content.replace("\t", "    ").split("\n")):
                if k > 0:
                    self.flush(force=True)
                self._place_word(line, font, color, node, measure=False, nowrap=True)
            return
        nowrap = white_space == "nowrap"
        for word in content.split():
            self._place_word(word, font, color, node, nowrap=nowrap)

    def _place_word(self, word, font, color, node, measure=True, nowrap=False):
        if not word:
            return
        w = _measure(font, word)
        x0, x1 = self._line_bounds()
        if not nowrap and x0 >= x1:
            # A float covers the whole line (e.g. a full-width floated table):
            # don't draw the word on top of the float, drop below it first.
            bottom = self.cursor_y
            for f in self._all_float_regions():
                if f["top"] <= self.cursor_y < f["bottom"]:
                    bottom = max(bottom, f["bottom"])
            if bottom > self.cursor_y:
                self.cursor_y = bottom
            self.cursor_x = self._line_bounds()[0]
            x0, x1 = self._line_bounds()
        if not nowrap and self.cursor_x + w > x1:
            if self.line:
                self.flush()
            self.cursor_x = self._line_bounds()[0]
        self.line.append(_LineItem("text", self.cursor_x, word, font, color, node, w, 0))
        self.cursor_x += w + (_measure(font, " ") if measure else 0)

    def flush(self, force=False):
        if not self.line:
            if not force:
                return
            # A bare <br> (or <br><br>) still has to advance the line; advance
            # by one line box using the current font metrics.
            font = _node_font(self.node)
            self.cursor_y += 1.25 * (_metrics(font, "ascent") + _metrics(font, "descent"))
            self.cursor_x = self._line_bounds()[0]
            return

        max_ascent = max(item.ascent for item in self.line)
        max_descent = max(item.descent for item in self.line)
        baseline = self.cursor_y + 1.25 * max_ascent
        align = self.node.style.get("text-align", "left")
        line_width = (self.line[-1].x + self.line[-1].w) - self.x
        offset = 0
        if align == "center":
            offset = max(0, (self.width - line_width) / 2)
        elif align == "right":
            offset = max(0, self.width - line_width)

        for item in self.line:
            if item.kind == "img":
                y = baseline - item.ascent
                if item.photo:
                    self.display_list.append(DrawImage(
                        item.x + offset, y,
                        item.x + offset + item.w, y + item.h,
                        item.photo, item.node))
                    continue
                self.display_list.append(DrawOutline(
                    item.x + offset, y,
                    item.x + offset + item.w, y + item.h, "#aaaaaa"))
                xoff, ty, color = 4, y + 2, "#888888"
            elif item.kind == "pill":
                y = baseline - item.h
                if item.bg:
                    self.display_list.append(DrawRect(
                        item.x + offset, y,
                        item.x + offset + item.w, y + item.h, item.bg))
                ty = y + item.pt + max(
                    0.0, (item.h - item.pt - item.pb - _linespace(item.font)) / 2)
                xoff, color = item.pl, item.color
            else:
                y = baseline - _metrics(item.font, "ascent")
                xoff, ty, color = 0, y, item.color
            self.display_list.append(DrawText(
                item.x + offset + xoff, ty, item.text, item.font, color, item.node))
            if item.kind == "text":
                self._maybe_underline(
                    item.x + offset, y, item.text, item.font, item.color, item.node)
        self.cursor_y = baseline + 1.25 * max_descent
        self.cursor_x = self._line_bounds()[0]
        self.line = []

    def _maybe_underline(self, x, y, word, font, color, node):
        # Walk up to see if any ancestor requests underline (links, <u>).
        n = node
        while n is not None:
            if isinstance(n, Element) and (n.tag in ("a", "u")
                    or n.style.get("text-decoration") == "underline"):
                yb = y + _metrics(font, "ascent") + 1
                self.display_list.append(
                    DrawLine(x, yb, x + _measure(font, word), yb, color, 1))
                return
            n = n.parent

    def _draw_bullet(self):
        size = int(round(parse_px(self.node.style.get("font-size", "16px"), 16)))
        color = resolve_color(self.node.style.get("color", "black")) or "black"
        by = self.cursor_y + size * 0.5
        bx = self.x - 14
        self.display_list.append(DrawRect(bx, by, bx + 5, by + 5, color))

    def _draw_hr(self, node):
        y = self.cursor_y + 4
        self.display_list.append(
            DrawLine(self.x, y, self.x + self.width, y, "#888888", 1))
        self.cursor_y = y + 6

    def _inline_img(self, node):
        alt = node.attributes.get("alt", "") if isinstance(node, Element) else ""
        src = node.attributes.get("src", "") if isinstance(node, Element) else ""
        photo = None
        cache = self._image_cache()
        if src and cache:
            photo = cache.get(src)
        if photo is None:
            # Placeholder box (image pending / failed to decode).
            label = f"[img: {alt}]" if alt else "[img]"
            font = get_font(12, "normal", "roman")
            w = _measure(font, label) + 8
            h = _linespace(font)
        else:
            label, font = "", None
            w, h = photo.width(), photo.height()
        w = self._fit_control(w, min_w=w)
        self.line.append(_LineItem("img", self.cursor_x, label, font, None,
                                   node, w, h, photo))
        self.cursor_x += w + (_measure(font, " ") if photo is None else w * 0.25)

    def _image_cache(self):
        """Walk up the layout tree to find the tab's image cache (a dict of
        absolute URL -> tkinter.PhotoImage), if any was attached."""
        box = self
        while box is not None:
            cache = getattr(box, "image_cache", None)
            if cache is not None:
                return cache
            box = box.parent
        return None

    def _fit_control(self, w, min_w=20):
        """Flush if the control would overflow the line and clamp it to the
        space remaining; returns the fitted width."""
        if self.cursor_x + w > self._line_bounds()[1] and self.line:
            self.flush()
        if self.width and w > self.width - (self.cursor_x - self.x):
            w = max(min_w, self.width - (self.cursor_x - self.x))
        return w

    def _box_control(self, x, y, w, h, font, node, rect=None, outline=None,
                     thickness=1, texts=()):
        """Paint a control box (optional fill and border) plus its label
        text(s), record its hit box, and advance the cursor past it."""
        if rect:
            self.display_list.append(DrawRect(x, y, x + w, y + h, rect))
        if outline:
            self.display_list.append(DrawOutline(x, y, x + w, y + h, outline,
                                                 thickness))
        for tx, ty, text, tfont, color in texts:
            self.display_list.append(DrawText(tx, ty, text, tfont, color, node))
        self.input_boxes.append((x, y, x + w, y + h, node))
        self.cursor_x = x + w + _measure(font, " ")

    def _paint_control(self, node, label, wpad, hpad, rect, outline,
                       dx, dy, tcolor, dropdown=False):
        """Paint a button/select-shaped control from a resolved label."""
        font = get_font(13, "normal", "roman")
        w = self._fit_control(_measure(font, label) + wpad)
        h = _linespace(font) + hpad
        y = self.cursor_y
        texts = [(self.cursor_x + dx, y + dy, label, font, tcolor)]
        if dropdown:
            texts.append((self.cursor_x + w - 14, y + 4, "▾", font, "#555555"))
        self._box_control(self.cursor_x, y, w, h, font, node,
                          rect=rect, outline=outline, texts=texts)

    def _inline_input(self, node):
        itype = node.attributes.get("type", "text").lower()
        if itype == "hidden":
            return
        if itype == "submit" or itype == "image":
            return self._inline_button(node)
        font = get_font(13, "normal", "roman")
        bull = _linespace(font)
        value = node.attributes.get("value", "")
        placeholder = node.attributes.get("placeholder", "")
        label = value
        if node.tag == "textarea":
            label = value.split("\n", 1)[0]
        if not label:
            label = placeholder if placeholder else ("" if value else " ")
        if itype == "password" and value:
            label = "•" * len(value)
        show_placeholder = not value and bool(placeholder)
        if itype in ("checkbox", "radio"):
            w = 18
            y = self.cursor_y
            h = bull + 2
            if self.cursor_x + w > self._line_bounds()[1] and self.line:
                self.flush()
            checked = value == "on"
            self.display_list.append(DrawOutline(
                self.cursor_x, y, self.cursor_x + w - 4, y + h, "#666666", 1))
            if checked:
                self.display_list.append(DrawText(
                    self.cursor_x + 1, y - 1, "✓", get_font(12, "bold", "roman"),
                    "#1a73e8", node))
            self.input_boxes.append(
                (self.cursor_x, y, self.cursor_x + w, y + h, node))
            self.cursor_x += w
            return
        w = 160
        if "size" in node.attributes:
            try:
                w = max(24, int(node.attributes["size"]) * 9)
            except ValueError:
                pass
        w = self._fit_control(w)
        h = bull + 8
        y = self.cursor_y
        focused = "data-focused" in node.attributes
        color = "#3b82f6" if focused else "#999999"
        texts = []
        if label.strip():
            lw = _measure(font, label)
            if lw > w - 8:
                ratio = max(1.0, (w - 12) / (_measure(font, "m") or 1))
                label = label[:int(ratio)] + "…"
            texts = [(self.cursor_x + 4, y + 4, label, font,
                      "#8a8a8a" if show_placeholder else "#111111")]
        self._box_control(self.cursor_x, y, w, h, font, node,
                          outline=color, thickness=2 if focused else 1,
                          texts=texts)

    def _inline_button(self, node):
        if node.tag == "input":
            label = node.attributes.get("value", "") or "Submit"
        else:
            label = "".join(c.text for c in node.children
                            if isinstance(c, Text)).strip() or "Button"
        if not isinstance(label, str):
            label = str(label)
        pressed = "data-focused" in node.attributes
        self._paint_control(node, label, 16, 10,
                            "#dcdcdc" if not pressed else "#b9c9e8",
                            "#777777", 8, 5, "#222222")

    def _inline_pill(self, node, bg, pl, pr, pt, pb):
        """Paint a display:inline-block element (background + padding) as a
        single rounded-ish box with its text laid out inside, e.g. a button
        link. Falls back to normal inline flow if text is empty."""
        parts = []
        for child in node.children:
            if isinstance(child, Text):
                parts.append(child.text)
            elif isinstance(child, Element):
                parts.append("".join(
                    c.text for c in child.children if isinstance(c, Text)))
        label = "".join(parts).strip()
        if not label:
            return
        font = _node_font(node)
        color = resolve_color(node.style.get("color", "black")) or "black"
        w = _measure(font, label)
        total_w = self._fit_control(w + pl + pr, min_w=w)
        lh = parse_px(node.style.get("line-height", "0"))
        h = max(_linespace(font), lh if lh else 0) + pt + pb
        self.line.append(_LineItem("pill", self.cursor_x, label, font, color,
                                   node, total_w, h, bg=bg, pl=pl, pr=pr,
                                   pt=pt, pb=pb))
        self.cursor_x += total_w + _measure(font, " ")

    def _inline_select(self, node):
        opts = [c for c in node.children
                if isinstance(c, Element) and c.tag == "option"]
        chosen = [o for o in opts if "selected" in o.attributes] or opts[:1]
        label = chosen[0].attributes.get("value", "") if chosen else ""
        if not label and chosen:
            label = "".join(c.text for c in chosen[0].children
                            if isinstance(c, Text))
        self._paint_control(node, label or "▾", 20, 8,
                            "#f2f2f2", "#999999", 4, 4, "#111111",
                            dropdown=True)

    # -- painting --------------------------------------------------------

    def paint(self):
        cmds = []
        _paint_bg(self, cmds)
        if hasattr(self, "display_list"):
            cmds.extend(self.display_list)
        return cmds


class RowLayout(LayoutBox):
    """A single <tr>: a block box that stacks table cells horizontally via
    explicit coordinates assigned by _layout_table."""

    def paint(self):
        cmds = []
        _paint_bg(self, cmds)
        return cmds


class CellLayout(LayoutBox):
    """A <td>/<th>: owns its own pre-flattened display list plus the source
    node used for hit-testing links inside the cell."""

    def __init__(self, node, parent, previous):
        super().__init__(node, parent, previous)
        self.content = []

    def paint(self):
        cmds = []
        _paint_bg(self, cmds, require_size=False)
        node = self.node
        if isinstance(node, Element):
            border = node.style.get("border") or \
                node.style.get("border-top-width") or ""
            if border:
                cmds.append(DrawOutline(self.x, self.y,
                                        self.x + self.width, self.y + self.height,
                                        "#666666", 1))
        cmds.extend(self.content)
        return cmds


class DocumentLayout(LayoutBox):
    """Root of the layout tree; establishes the viewport width."""

    def __init__(self, node, width):
        super().__init__(node, None, None)
        self.viewport_width = width

    def layout(self):
        self.width = self.viewport_width - 16  # left/right gutter
        self.x = 8
        self.y = 8
        _prewarm(self.node)
        child = BlockLayout(self.node, self, None)
        self.children = [child]
        child.layout()
        self.height = child.height + 16

    def collect_inputs(self, out):
        """Gather hit-test rectangles for every form control in the tree."""
        stack = list(self.children)
        while stack:
            box = stack.pop()
            out.extend(getattr(box, "input_boxes", ()))
            stack.extend(box.children)
        return out

    def paint(self):
        return []


def paint_tree(layout_box, display_list, hidden=False):
    """Flatten a box tree into paint commands, honouring `visibility`: a box
    with `visibility:hidden` (or one nested under a hidden box, unless it
    explicitly opts back in with `visibility:visible`) is not painted."""
    node = getattr(layout_box, "node", None)
    if isinstance(node, Element):
        vis = node.style.get("visibility")
        if vis == "hidden":
            hidden = True
        elif vis == "visible":
            hidden = False
    if not hidden:
        display_list.extend(layout_box.paint())
    for child in layout_box.children:
        paint_tree(child, display_list, hidden)
