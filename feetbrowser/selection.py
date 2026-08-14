"""Selecting rendered page text with the mouse.

The display list is a flat list of paint commands with no notion of "the
text", so everything a selection needs -- which run is under a pixel, which
character inside it, what comes after it in the document, what the whole
thing spells -- has to be reconstructed from it. That is what `Index` is:
one pass over a display list producing the text runs in *document* order,
with the geometry beside them.

**The unit of position.** A `Point` is a DOM text node plus a character
offset into that node's *rendered* text -- the concatenation of the runs
layout produced for it, which is the node's text with each run of whitespace
collapsed away. Not a pixel, and not an index into the display list:

  * Pixels move when the page scrolls. `repaint()` rebuilds the display list
    on every scroll tick (sticky and fixed boxes depend on the offset), so a
    selection remembering a `DrawText` -- or a y coordinate -- is pointing at
    something that no longer exists one wheel click later.
  * Display-list indices move when the window is resized, because rewrapping
    changes which line a word lands on.

A node offset survives both. Rewrapping a paragraph changes where its words
are drawn but never their order or their spelling, so the same offsets still
name the same characters and the highlight follows the text. Only a document
that is genuinely different -- a navigation, a script rewriting the DOM --
invalidates a position, which is exactly when the highlight *should* go
away; `Selection.revalidate` is where that is decided.

Ordering two points is (document order of the node, offset). The document
order comes from a pre-order walk of the DOM rather than from the display
list, because paint order is not document order: a `z-index` lifts a box's
paint above its neighbours' without moving its text in the document.

Out of scope, deliberately: selection inside editable fields (the address
bar has its own, and page fields are drawn as controls rather than as text
runs), IME composition, and bidirectional or right-to-left text -- a
selection there is a set of visual ranges rather than one, and nothing else
in the renderer models RTL either.
"""

from .htmlparser import Text
from .layout import DrawText, _measure, _metrics

__all__ = ["Index", "Point", "Run", "Selection", "contrasting_text_color"]

# A word for double-click purposes: letters, digits, underscore, and the
# apostrophes that sit inside words ("don't", "l’an"). Everything else is
# punctuation, and a double-click on punctuation selects the punctuation run,
# which is what every browser does.
_WORD_EXTRA = "_'’"


def _is_word_char(ch):
    return ch.isalnum() or ch in _WORD_EXTRA


class Run:
    """One `DrawText` of selectable page text, placed in the document.

    `start` is where this run's text begins inside its node's rendered text,
    so `start` .. `start + len(text)` is the range of node offsets it covers
    and consecutive runs of a node tile that range without gaps.
    """

    __slots__ = ("cmd", "node", "start", "order", "line")

    def __init__(self, cmd, node, start, order, line):
        self.cmd = cmd
        self.node = node
        self.start = start
        self.order = order      # document order of `node`
        self.line = line        # index into Index.lines

    @property
    def text(self):
        return self.cmd.text

    @property
    def font(self):
        return self.cmd.font

    @property
    def left(self):
        return self.cmd.left

    @property
    def right(self):
        return self.cmd.right

    @property
    def top(self):
        return self.cmd.top

    @property
    def bottom(self):
        return self.cmd.bottom

    @property
    def end(self):
        return self.start + len(self.cmd.text)

    def x_at(self, offset):
        """Pixel x of the boundary before character `offset` of this run."""
        return self.left + _measure(self.font, self.text[:offset])

    def offset_at(self, x):
        """The character boundary nearest to pixel `x`, as a run-local index.

        Advances come one character at a time from the font engine (through
        layout's per-character memo, so this is the same arithmetic that
        decided where the glyphs went) -- nothing here assumes the face is
        monospaced, and a proportional face lands on the boundary a user
        aimed at. Nearest boundary rather than the one before, so clicking
        the right half of a character puts the caret after it.
        """
        text, font = self.text, self.font
        pen = self.left
        best, best_d = 0, abs(x - pen)
        for i, ch in enumerate(text):
            pen += _measure(font, ch)
            distance = abs(x - pen)
            if distance < best_d:
                best, best_d = i + 1, distance
        return best

    def word_at(self, offset):
        """The (start, end) run-local range of the word around `offset`."""
        text = self.text
        if not text:
            return 0, 0
        i = min(max(offset, 0), len(text) - 1)
        # A boundary click (offset == len) belongs to the character before it.
        if offset >= len(text):
            i = len(text) - 1
        wordish = _is_word_char(text[i])
        start = i
        while start > 0 and _is_word_char(text[start - 1]) == wordish:
            start -= 1
        end = i + 1
        while end < len(text) and _is_word_char(text[end]) == wordish:
            end += 1
        return start, end


class Point:
    """A position in the document: a text node and an offset into it."""

    __slots__ = ("node", "offset")

    def __init__(self, node, offset):
        self.node = node
        self.offset = int(offset)

    def __eq__(self, other):
        return isinstance(other, Point) and other.node is self.node \
            and other.offset == self.offset

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((id(self.node), self.offset))

    def __repr__(self):
        return "<Point %s+%d>" % (type(self.node).__name__, self.offset)


class Selection:
    """An anchor and a focus, in that order of creation.

    The anchor is where the drag started and the focus is where the pointer
    is now, so a backwards drag is a selection whose focus precedes its
    anchor. Nothing downstream has to care: `Index.spans` normalises the pair
    into document order before doing anything with it, which is the one place
    the direction of the drag stops mattering.
    """

    __slots__ = ("anchor", "focus", "granularity", "anchor_start",
                 "anchor_end")

    def __init__(self, anchor, focus=None, granularity="char"):
        self.anchor = anchor
        self.focus = focus if focus is not None else anchor
        # "char", "word" or "line": what a drag extends by, so dragging after
        # a double-click keeps snapping to whole words the way it does
        # everywhere else.
        self.granularity = granularity
        # The two ends of the unit the multi-click started on. A drag that
        # crosses back over the anchor has to pivot to the far end of that
        # unit, or double-click-then-drag-left eats the word it started on.
        self.anchor_start = self.anchor
        self.anchor_end = self.focus

    def collapsed(self):
        return self.anchor == self.focus

    def __bool__(self):
        return not self.collapsed()

    def __repr__(self):
        return "<Selection %r -> %r>" % (self.anchor, self.focus)


def contrasting_text_color(fill):
    """Black or white, whichever stays legible on `fill`.

    The highlight colour is the shoe's accent, and the shoes range from a
    pale ocean blue to a near-black, so a hardcoded white would vanish on
    half of them.
    """
    try:
        from .canvas import color
        r, g, b = color(fill)[:3]
    except Exception:  # noqa: BLE001 - an unparseable colour is not fatal
        return "#ffffff"
    # Rec. 709 luma, which is close enough to perceived brightness for a
    # two-way choice.
    luma = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    return "#000000" if luma > 0.6 else "#ffffff"


class Index:
    """The selectable text of one display list, in document order.

    Built fresh from a display list, which means it is rebuilt on every
    scroll tick. That is affordable -- it is a linear pass over the paint
    commands with no measuring -- and it is what keeps the geometry honest
    when sticky boxes move.
    """

    def __init__(self, display_list):
        self.runs = []
        self.lines = []          # [(top, bottom, [run, ...])], top to bottom
        self._order = {}         # id(node) -> document order
        self._by_node = {}       # id(node) -> [run, ...]
        self._build(display_list or [])

    # -- construction ----------------------------------------------------

    def _build(self, display_list):
        commands = [cmd for cmd in display_list
                    if isinstance(cmd, DrawText) and cmd.text
                    and isinstance(cmd.node, Text)]
        if not commands:
            return
        self._order = _document_order(commands[0].node)
        # Runs of one node keep the order the display list has them in, which
        # is the order layout emitted them: line by line, left to right. Only
        # *between* nodes does paint order stop meaning document order.
        per_node = {}
        for cmd in commands:
            per_node.setdefault(id(cmd.node), []).append(cmd)
        runs = []
        for key, cmds in per_node.items():
            node = cmds[0].node
            order = self._order.get(key, len(self._order))
            offset = 0
            node_runs = []
            for cmd in cmds:
                run = Run(cmd, node, offset, order, -1)
                offset += len(cmd.text)
                node_runs.append(run)
                runs.append(run)
            self._by_node[key] = node_runs
        runs.sort(key=lambda r: (r.order, r.start))
        self.runs = runs
        self._build_lines()

    def _build_lines(self):
        """Group runs into visual lines by the baseline they were placed on.

        Layout puts every item of one line on a single baseline and then
        positions each by its own ascent, so runs of different sizes on the
        same line share no top edge and no centre -- but they do share
        `top + ascent` exactly. That is the only grouping that survives a
        heading and its footnote marker sitting side by side.
        """
        groups = {}
        for run in self.runs:
            baseline = round(run.top + _metrics(run.font, "ascent"), 3)
            groups.setdefault(baseline, []).append(run)
        for baseline in sorted(groups):
            members = sorted(groups[baseline], key=lambda r: r.left)
            top = min(r.top for r in members)
            bottom = max(r.bottom for r in members)
            for run in members:
                run.line = len(self.lines)
            self.lines.append((top, bottom, members))

    # -- ordering --------------------------------------------------------

    def key(self, point):
        """A sortable key for `point`, or None when it is not in this page."""
        order = self._order.get(id(point.node))
        if order is None:
            return None
        return (order, point.offset)

    def contains(self, point):
        return point is not None and id(point.node) in self._by_node

    def ordered(self, selection):
        """(start, end) points of `selection` in document order, or None."""
        if selection is None:
            return None
        a, b = self.key(selection.anchor), self.key(selection.focus)
        if a is None or b is None or a == b:
            return None
        return (selection.anchor, selection.focus) if a < b \
            else (selection.focus, selection.anchor)

    # -- hit testing -----------------------------------------------------

    def point_at(self, x, y):
        """The document position nearest to (x, y) in document coordinates.

        Never fails while the page has any text: a click in the margin, below
        the last paragraph or past the end of a line all resolve to the
        nearest edge, because a drag spends most of its time outside the
        glyphs it is selecting.
        """
        run, offset = self.hit(x, y)
        if run is None:
            return None
        return Point(run.node, run.start + offset)

    def hit(self, x, y):
        """(run, run-local offset) nearest to (x, y), or (None, None)."""
        line = self._line_at(x, y)
        if line is None:
            return None, None
        members = line[2]
        first, last = members[0], members[-1]
        if x <= first.left:
            return first, 0
        if x >= last.right:
            return last, len(last.text)
        previous = None
        for run in members:
            if x < run.left:
                # In the gap between two runs -- the space layout advanced by
                # but never drew. Whichever side is closer gets the caret.
                if previous is None:
                    return run, 0
                if x - previous.right <= run.left - x:
                    return previous, len(previous.text)
                return run, 0
            if x < run.right:
                return run, run.offset_at(x)
            previous = run
        return last, len(last.text)

    def _line_at(self, x, y):
        """The visual line (x, y) falls on, or the nearest one to it."""
        if not self.lines:
            return None
        best, best_key = None, None
        for line in self.lines:
            top, bottom, members = line
            if top <= y < bottom:
                dy = 0.0
            else:
                dy = top - y if y < top else y - bottom + 1
            # Several lines can share a band of y: a float beside a
            # paragraph, or a tall inline-block. x settles those.
            if x < members[0].left:
                dx = members[0].left - x
            elif x > members[-1].right:
                dx = x - members[-1].right
            else:
                dx = 0.0
            key = (dy, dx)
            if best_key is None or key < best_key:
                best, best_key = line, key
        return best

    # -- ranges ----------------------------------------------------------

    def spans(self, selection):
        """`(run, start, end)` slices covered by `selection`, document order.

        A run the selection only partly covers comes back with the character
        range that is actually inside it, so the painter highlights the two
        letters a drag stopped on rather than the whole word.
        """
        bounds = self.ordered(selection)
        if bounds is None:
            return []
        start, end = bounds
        start_key, end_key = self.key(start), self.key(end)
        out = []
        for run in self.runs:
            run_start = (run.order, run.start)
            run_end = (run.order, run.end)
            if run_end <= start_key or run_start >= end_key:
                continue
            s = start.offset - run.start if run.node is start.node else 0
            e = end.offset - run.start if run.node is end.node else len(run.text)
            s = max(0, min(s, len(run.text)))
            e = max(0, min(e, len(run.text)))
            if s < e:
                out.append((run, s, e))
        return out

    def text(self, selection):
        """What the selection spells -- what a copy puts on the clipboard.

        Assembled from the runs as drawn rather than from the source text,
        because those are what the user can see: collapsed whitespace is
        gone, a word broken across two elements reads as one, and a line
        break in the markup that the layout did not honour is not a line
        break here either. Runs separated by a gap on screen are joined by a
        space, and a change of visual line is a newline.
        """
        parts, previous = [], None
        for run, s, e in self.spans(selection):
            if previous is not None:
                if run.line != previous.line:
                    parts.append("\n")
                elif run.left - previous.right >= _measure(run.font, " ") / 2:
                    parts.append(" ")
            parts.append(run.text[s:e])
            previous = run
        return "".join(parts)

    # -- granularity -----------------------------------------------------

    def word_around(self, x, y):
        """A `Selection` covering the word under (x, y), or None."""
        run, offset = self.hit(x, y)
        if run is None:
            return None
        s, e = run.word_at(offset)
        if s >= e:
            return None
        return Selection(Point(run.node, run.start + s),
                         Point(run.node, run.start + e), "word")

    def line_around(self, x, y):
        """A `Selection` covering the whole visual line under (x, y).

        Triple-click. macOS -- and Chrome, Safari and Firefox on it -- takes a
        triple-click as "the paragraph"; a laid-out line here *is* the
        paragraph's line, and selecting the line is what a user pointing at
        wrapped body text is asking for. Selecting the whole block instead
        would sweep in text several screens away on a long paragraph.
        """
        line = self._line_at(x, y)
        if line is None:
            return None
        members = line[2]
        first, last = members[0], members[-1]
        return Selection(Point(first.node, first.start),
                         Point(last.node, last.end), "line")

    def extend(self, selection, x, y):
        """Move `selection`'s focus to (x, y), respecting its granularity.

        Word- and line-granularity drags grow by whole words or lines and
        never shrink below the unit the multi-click started with, which is
        the behaviour every browser has and the reason a double-click-drag
        does not fall back to characters the moment the pointer moves.
        """
        point = self.point_at(x, y)
        if point is None:
            return selection
        if selection.granularity == "char":
            selection.focus = point
            return selection
        maker = self.word_around if selection.granularity == "word" \
            else self.line_around
        unit = maker(x, y)
        if unit is None:
            selection.focus = point
            return selection
        start_key = self.key(selection.anchor_start)
        if start_key is not None and self.key(unit.anchor) is not None \
                and self.key(unit.anchor) < start_key:
            # The pointer has crossed above/left of the unit it started on:
            # pivot so that unit stays wholly inside the selection.
            selection.anchor = selection.anchor_end
            selection.focus = unit.anchor
        else:
            selection.anchor = selection.anchor_start
            selection.focus = unit.focus
        return selection

    # -- lifetime --------------------------------------------------------

    def revalidate(self, selection):
        """`selection` if it still names live text here, else None.

        Called after a relayout. A resize that only rewraps keeps both nodes
        and both offsets meaningful, so the highlight stays on the words it
        was on; a navigation or a script that replaced the paragraph does
        not, and the highlight goes rather than pointing at whatever moved
        into those coordinates.
        """
        if selection is None:
            return None
        for point in (selection.anchor, selection.focus,
                      selection.anchor_start, selection.anchor_end):
            runs = self._by_node.get(id(point.node))
            if not runs:
                return None
            if not 0 <= point.offset <= runs[-1].end:
                return None
        return selection


def _document_order(node):
    """Pre-order index of every node in `node`'s document, by identity.

    Walked from the DOM root rather than read off the display list: paint
    order is stacking order, and a `z-index` on one paragraph would otherwise
    reorder the document under it.
    """
    root = node
    while getattr(root, "parent", None) is not None:
        root = root.parent
    order, index, stack = {}, 0, [root]
    while stack:
        current = stack.pop()
        order[id(current)] = index
        index += 1
        children = getattr(current, "children", None)
        if children:
            stack.extend(reversed(children))
    return order
