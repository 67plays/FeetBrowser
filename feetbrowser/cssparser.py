"""A from-scratch CSS parser + cascade.

Supports: tag / class / id / universal selectors, descendant combinators,
grouped selectors (a, b), a property/value declaration parser, specificity,
inheritance of inherited properties, and inline style="" attributes.

The parser is here; the cascade is not. `style()` comes from the Rust
extension, because it is the one part of this module that runs per node per
rule -- on a big article it was half the time between asking for a page and
seeing it. The selector classes below are the parser's output and the Rust
matcher's input: each carries a `kind` the compiler over there reads, and the
matching itself lives in `rust/src/css.rs`. What stays in Python is
everything that is a table rather than a loop -- what inherits, which
shorthands expand, what a media query can ask about -- so the rules of the
cascade remain readable in one place.
"""

import copy
import re

import feetbrowser_engine

# Re-exported under this module's name, because that is where the rest of the
# browser has always asked for it.
style = feetbrowser_engine.style

# The media-query viewport. Real browsers only apply the rules inside an
# @media block when its query matches the current window size; the default
# 1000x720 matches the browser's default WIDTH/HEIGHT and is updated on resize.
_VIEWPORT = (1000.0, 720.0)


def set_viewport(width, height):
    global _VIEWPORT
    _VIEWPORT = (float(width), float(height))


def get_viewport():
    """Current (width, height) viewport. A module-level accessor (rather than
    importing the `_VIEWPORT` tuple) so callers always see `set_viewport`
    updates instead of a stale copy."""
    return _VIEWPORT


# What this browser is, in the vocabulary media queries ask questions in.
# Anything missing from here still matches, so an unfamiliar query never
# silently drops a rule -- but the ones we can answer have to be answered.
# `prefers-color-scheme` especially: assuming it matched meant every site
# with a dark theme got the dark theme, and a site whose dark rules are all
# custom properties we do not resolve came out as a black rectangle.
_MEDIA_FEATURES = {
    "prefers-color-scheme": "light",
    "prefers-reduced-motion": "no-preference",
    "prefers-reduced-transparency": "no-preference",
    "prefers-contrast": "no-preference",
    "forced-colors": "none",
    "inverted-colors": "none",
    "hover": "hover",
    "any-hover": "hover",
    "pointer": "fine",
    "any-pointer": "fine",
    "scripting": "enabled",
    "display-mode": "browser",
}


def media_matches(prelude, width, height):
    """Evaluate a media-query prelude against a viewport. Handles `and`,
    comma-OR lists, media types (all/screen/print), the common
    (min/max-width/height) features and the preference features this browser
    can answer for itself; anything else is assumed to match so rules aren't
    silently dropped."""
    if not prelude or not prelude.strip():
        return True
    for alt in re.split(r"\s*,\s*", prelude):
        if not alt:
            continue
        negated = bool(re.search(r"(?:^|\s)not\s", alt))
        cond = True
        for feat in re.findall(r"\(([^()]*)\)", alt):
            if ":" not in feat:
                continue
            prop, val = feat.split(":", 1)
            prop = prop.strip().lower()
            val = val.strip().lower()
            if prop in ("min-width", "max-width", "min-height", "max-height"):
                m = re.match(r"([-.\d]+)\s*(px|em|rem)?", val)
                if not m:
                    continue
                n = float(m.group(1))
                if m.group(2) in ("em", "rem"):
                    # A 16px root size, matching layout.py's parse_px for rem.
                    n *= 16.0
                if prop == "min-width" and width < n:
                    cond = False
                elif prop == "max-width" and width > n:
                    cond = False
                elif prop == "min-height" and height < n:
                    cond = False
                elif prop == "max-height" and height > n:
                    cond = False
            elif prop == "orientation":
                if val != ("portrait" if height >= width else "landscape"):
                    cond = False
            elif prop in _MEDIA_FEATURES and val != _MEDIA_FEATURES[prop]:
                cond = False
        if re.search(r"(?:^|\s)print(?:\s|$)", alt):
            cond = False
        if negated:
            cond = not cond
        if cond:
            return True
    return False

INHERITED_PROPERTIES = {
    "font-size": "16px",
    "font-style": "normal",
    "font-weight": "normal",
    "font-family": "",
    "color": "black",
    "line-height": "normal",
    "text-align": "left",
    "white-space": "normal",
    "list-style-type": "disc",
}


def _combine_priorities(a, b):
    """Element-wise sum of two specificity tuples."""
    return tuple(x + y for x, y in zip(a, b))


class TagSelector:
    kind = "tag"

    def __init__(self, tag):
        self.tag = tag
        self.priority = (0, 0, 1) if tag != "*" else (0, 0, 0)


class ClassSelector:
    kind = "class"

    def __init__(self, cls):
        self.cls = cls
        self.priority = (0, 1, 0)


class IdSelector:
    kind = "id"

    def __init__(self, id_):
        self.id = id_
        self.priority = (1, 0, 0)


class CompoundSelector:
    """One or more simple selectors on the same element, e.g. div.note#x"""

    kind = "compound"

    def __init__(self, parts):
        self.parts = parts
        self.priority = tuple(sum(p) for p in zip(*[s.priority for s in parts]))


class DescendantSelector:
    kind = "descendant"

    def __init__(self, ancestor, descendant):
        self.ancestor = ancestor
        self.descendant = descendant
        self.priority = _combine_priorities(ancestor.priority,
                                          descendant.priority)


class ChildSelector:
    """`a > b`: b's own parent must be an a, not merely some ancestor.

    Treating this as a descendant relationship is how one `.menu > li` rule
    reaches every list item on the page, submenus included.
    """

    kind = "child"

    def __init__(self, parent, child):
        self.parent = parent
        self.child = child
        self.priority = _combine_priorities(parent.priority, child.priority)


class SiblingSelector:
    """`a + b` (adjacent) and `a ~ b` (any earlier sibling)."""

    kind = "sibling"

    def __init__(self, before, after, adjacent):
        self.before = before
        self.after = after
        self.adjacent = adjacent
        self.priority = _combine_priorities(before.priority, after.priority)


class RootSelector:
    """Matches the document root element (`:root`), i.e. the node with no
    parent. Typical target for custom-property (`--x`) declarations."""

    kind = "root"

    def __init__(self):
        self.priority = (0, 0, 1)


class AttrSelector:
    """Attribute selector: [attr], [attr=value], [attr~=v], [attr|=v],
    [attr^=v], [attr$=v], [attr*=v]. `op` is None for mere presence."""

    kind = "attr"

    def __init__(self, attr, op=None, value=None):
        self.attr = attr
        self.op = op
        self.value = value
        self.priority = (0, 1, 0)


# Pseudo-classes whose state the engine cannot track (interaction, browsing
# history, etc.). Their rule's base selector still applies (as if the
# pseudo-class were stripped) so styling is not silently lost.
_DYNAMIC_PSEUDOS = frozenset({
    "hover", "active", "focus", "focus-within", "focus-visible", "visited",
    "target", "any-link", "current", "past", "future", "playing", "paused",
    "autofill", "default", "defined", "fullscreen", "indeterminate", "open",
    "optional", "read-only", "read-write", "user-invalid", "valid", "invalid",
    "in-range", "out-of-range", "placeholder-shown", "scope", "blank",
    "popover-open", "lang",
})

class PseudoSelector:
    """A pseudo-class on an element, e.g. :first-child, :not(.x), :nth-of-type.
    `arg` holds the matched argument text, or (for :not/:is/:where/:has) a
    list of parsed sub-selectors."""

    kind = "pseudo"

    def __init__(self, name, arg=None):
        self.name = name
        self.arg = arg
        self.priority = self._priority()

    def _priority(self):
        if self.name == "where":
            return (0, 0, 0)
        if self.name in ("not", "is", "has") and self.arg:
            return max(s.priority for s in self.arg)
        return (0, 1, 0)


_PSEUDO_ELEMENTS = {
    "before", "after", "first-line", "first-letter", "selection",
    "placeholder", "marker", "backdrop", "file-selector-button", "cue",
    "cue-region", "grammar-error", "spelling-error", "highlight", "target-text",
}

_VENDOR_PREFIXES = ("-webkit-", "-moz-", "-ms-", "-o-", "-khtml-")

# Sentinel: a dynamic pseudo-class (:hover, ...) was parsed; the base selector
# still applies, so simple_selector() emits nothing for this token.
_SKIP_PSEUDO = object()

def _strip_comments(value):
    """Drop /* ... */ from a declaration value, leaving quoted text alone.

    `content: "a/*b*/c"` is a string that happens to contain the characters,
    not a comment, and it is the one place they can legally appear.
    """
    out = []
    i, n, quote = 0, len(value), None
    while i < n:
        ch = value[i]
        if quote:
            if ch == quote:
                quote = None
            out.append(ch)
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif value[i:i + 2] == "/*":
            end = value.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# -- escaped identifiers (CSS Syntax sec. 4.3.7) --------------------------
#
# `.md\:flex`, `.w-1\/2`, `.p-\[10px\]`: a utility-class stylesheet is mostly
# identifiers containing characters that are punctuation to the selector
# grammar, written with a backslash to say "this is a name character, not
# syntax". Matching a name with a plain `[-_A-Za-z0-9]+` does not merely fail
# on those -- it matches the prefix, so `.hover\:bg-red` silently becomes
# `.hover`, a selector that is wrong rather than absent and matches whatever
# else on the page happens to be called that. Everything below consumes
# escapes instead, so the name that comes out is the name the author wrote.

_HEX = "0123456789abcdefABCDEF"
_WS = " \t\r\n\f"
_MAX_CODEPOINT = 0x10FFFF


def _consume_escape(text, i):
    """Consume the escape sequence at `text[i] == '\\\\'`.

    Returns `(character, next_index)`. A backslash at end-of-input, and a hex
    escape naming U+0000, a surrogate or a code point past the Unicode
    maximum, all yield U+FFFD -- the spec's answer, and the one that keeps a
    malformed sheet parsing.
    """
    n = len(text)
    j = i + 1
    if j >= n:
        return "\ufffd", j
    if text[j] not in _HEX:
        return text[j], j + 1
    start = j
    while j < n and j - start < 6 and text[j] in _HEX:
        j += 1
    code = int(text[start:j], 16)
    # One whitespace character after the digits is a delimiter, not content:
    # it is how `\2c ` says "the escape ended here". A CRLF counts as one.
    if j < n and text[j] in _WS:
        if text[j] == "\r" and j + 1 < n and text[j + 1] == "\n":
            j += 1
        j += 1
    if code == 0 or 0xD800 <= code <= 0xDFFF or code > _MAX_CODEPOINT:
        return "\ufffd", j
    return chr(code), j


def _is_name_char(ch):
    """A CSS name character: ASCII alphanumerics, `-`, `_`, and anything
    non-ASCII (the spec's rule, which is what lets a class be named in a
    language that is not English)."""
    return ch == "-" or ch == "_" or ch.isalnum() or ord(ch) >= 0x80


def _consume_name(text, i):
    """Consume an identifier at `i`, unescaping as it goes.

    Returns `(name, next_index)`, or `(None, i)` when there is no name here.
    """
    out = []
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            char, i = _consume_escape(text, i)
            out.append(char)
        elif _is_name_char(ch):
            out.append(ch)
            i += 1
        else:
            break
    if not out:
        return None, i
    return "".join(out), i


def _skip_sel_ws(text, i):
    while i < len(text) and text[i] in _WS:
        i += 1
    return i


def _split_top_level(text, sep=","):
    """Split a selector list on `sep`, leaving escapes alone.

    `\\2c ` inside an arbitrary-value class name is a comma the author spelled
    as a name character; splitting on it cuts a selector in half.
    """
    parts, buf = [], []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            _char, end = _consume_escape(text, i)
            buf.append(text[i:end])
            i = end
        elif ch == sep:
            parts.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    parts.append("".join(buf))
    return parts


_ATTR_OPS = "~|^$*"


def _attr_selector(text, i):
    """Parse `[attr]`, `[attr=value]` and the `~= |= ^= $= *=` forms at `i`.

    Returns `(AttrSelector, end_index)`, or `(None, i)` when this is not one.
    Both the name and an unquoted value are identifiers as far as escaping
    goes -- `[data-x=a\\/b]` is one value, not a value and then a stray slash.
    """
    n = len(text)
    j = _skip_sel_ws(text, i + 1)
    name, j = _consume_name(text, j)
    if name is None:
        return None, i
    j = _skip_sel_ws(text, j)
    op = value = None
    if j < n and text[j] != "]":
        if text[j] == "=":
            op, j = "=", j + 1
        elif text[j] in _ATTR_OPS and j + 1 < n and text[j + 1] == "=":
            op, j = text[j:j + 2], j + 2
        else:
            return None, i
        j = _skip_sel_ws(text, j)
        if j < n and text[j] in "\"'":
            quote, j = text[j], j + 1
            out = []
            while j < n and text[j] != quote:
                if text[j] == "\\":
                    char, j = _consume_escape(text, j)
                    out.append(char)
                else:
                    out.append(text[j])
                    j += 1
            if j >= n:
                return None, i
            value, j = "".join(out), j + 1
        else:
            out = []
            while j < n and text[j] != "]" and text[j] not in _WS:
                if text[j] == "\\":
                    char, j = _consume_escape(text, j)
                    out.append(char)
                else:
                    out.append(text[j])
                    j += 1
            if not out:
                return None, i
            value = "".join(out)
        j = _skip_sel_ws(text, j)
    if j >= n or text[j] != "]":
        return None, i
    return AttrSelector(name.lower(), op, value), j + 1


# `!important`, in the spellings a stylesheet actually arrives in: the case
# is free, and the space between the bang and the word is legal (and minifiers
# that leave it alone do exist). Anchored at the end because that is the only
# place the flag may appear in a declaration value.
_IMPORTANT_RE = re.compile(r"!\s*important\s*$", re.I)

# How far an `!important` declaration is lifted above the ordinary cascade.
#
# CSS does not order declarations on specificity alone: importance comes
# first, and only within one importance band does specificity decide (CSS
# Cascade 4 sec. 6.1). The matcher in rust/src/css.rs sorts rules on the
# selector's `priority` triple and nothing else, so the band is expressed
# where that sort can see it -- as a constant added to the leading component,
# far larger than any specificity a real selector can reach (that would take
# a million ids). An `#a#b#c` rule is (3, 0, 0); an important `p` rule is
# (1048576, 0, 1), and every important declaration therefore outranks every
# normal one whatever it was selected by, which is the whole point of the
# flag.
#
# Two deviations remain, both because they need the matcher to know about
# origins rather than the sheet: an important rule in the UA sheet does not
# beat an important author rule (CSS says it should), and the inline
# `style=""` attribute -- applied last of all, unconditionally -- still beats
# an important author rule (CSS says it should not).
IMPORTANT_BAND = 1 << 20


def _important_selector(sel):
    """A copy of `sel` whose priority sits in the important band.

    Copied rather than mutated because the same selector object is what the
    normal-band rule was emitted with, and the two have to sort apart. The
    copy is shallow: the matcher reads the structure below the top level and
    never its `priority`, so the sub-selectors can stay shared.
    """
    lifted = copy.copy(sel)
    a, b, c = sel.priority
    lifted.priority = (a + IMPORTANT_BAND, b, c)
    return lifted


class CSSParser:
    def __init__(self, s):
        self.s = s
        self.i = 0
        # Set by body(): the `!important` subset of the block it last read.
        self.important = {}

    def skip_ws(self):
        while self.i < len(self.s):
            if self.s[self.i] in " \t\r\n\f":
                self.i += 1
            elif self.s[self.i:self.i + 2] == "/*":
                end = self.s.find("*/", self.i)
                self.i = len(self.s) if end == -1 else end + 2
            else:
                break

    def literal(self, ch):
        if self.i < len(self.s) and self.s[self.i] == ch:
            self.i += 1
            return True
        return False

    def pair(self):
        # property : value
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in ":;}{":
            self.i += 1
        prop = self.s[start:self.i].strip().lower()
        if not self.literal(":"):
            return None
        # Values may contain ; } inside url(...), data: URIs or quoted strings,
        # so track brace/bracket/paren depth.
        vstart = self.i
        depth = 0
        in_quote = None
        while self.i < len(self.s):
            c = self.s[self.i]
            if in_quote:
                if c == in_quote:
                    in_quote = None
            elif c in "\"'":
                in_quote = c
            elif c in "([":
                depth += 1
            elif c in ")]":
                depth = max(0, depth - 1)
            elif depth == 0 and c in ";}{":
                # `{` ends a value the same way `;` does, because reaching one
                # means this was never a declaration: `&:hover { ... }` is a
                # nested rule whose prelude happens to contain a colon.
                # Stopping here is what lets body() see the `{` afterwards
                # and treat the whole thing as a nested rule; a value that
                # swallowed the block would be a silently wrong declaration.
                break
            self.i += 1
        value = self.s[vstart:self.i]
        if "/*" in value:
            # A comment is not part of the value, and inside a calc() it looks
            # like a division: `calc(10px + /* logo */ 18px)` is how a page
            # explains where its numbers came from.
            value = _strip_comments(value)
        value = value.strip()
        if not value or not prop:
            return None
        stripped = _IMPORTANT_RE.sub("", value).rstrip()
        important = stripped != value
        if important:
            # `!important` on its own is not a value, and a declaration
            # without one sets nothing in either band.
            if not stripped:
                return None
            value = stripped
        return (prop, value, important)

    def body(self):
        """Parse a declaration block { ... } already positioned after '{'.

        Returns the block's declarations. The `!important` ones among them
        are left in here too -- same property, same value, so a caller that
        ignores the distinction reads exactly what it read before -- and are
        *also* put aside on `self.important`, for `parse()` to re-emit as a
        rule of its own in the important band.
        """
        pairs = {}
        important = {}
        while self.i < len(self.s) and self.s[self.i] != "}":
            self.skip_ws()
            if self.i >= len(self.s) or self.s[self.i] == "}":
                break
            here = self.i
            p = self.pair()
            if self.i < len(self.s) and self.s[self.i] == "{":
                # CSS nesting: a rule inside a rule, as in
                #   .box { color: red; & + div pre { border: none } }
                # which docs.python.org's theme and most Tailwind output are
                # written in. A declaration ends at ';' or '}', so a '{' here
                # means what pair() just read was a selector, not a property,
                # whether or not it found a ':' in it (`&:hover {` has one).
                #
                # Nesting is not implemented -- flattening it needs the
                # parent selector threaded through, which is a feature, not a
                # parse fix -- so the nested rule is skipped like a @container
                # block and the declarations around it are kept. What matters
                # here is that it is *consumed*: leaving the scanner sitting
                # on the '{' is what used to spin this loop forever, because
                # pair() reads nothing when there is no ':' before the brace
                # and neither did the ';' below.
                self.i += 1
                self._read_block()
            elif p:
                pairs[p[0]] = p[1]
                if p[2]:
                    important[p[0]] = p[1]
            self.skip_ws()
            self.literal(";")
            if self.i == here:
                # Belt and braces: nothing above consumed a character, so the
                # next pass would see the same input and do the same nothing.
                # One character of progress turns a hang into a dropped
                # declaration.
                self.i += 1
        self.important = important
        return pairs

    def simple_selector(self, text):
        # e.g. div.note#id, .cls, #id, *, :root,
        #      [data-x="1"].cls:first-child, a[href^="https"]:not(.no-link)
        text = text.strip()
        if not text:
            return None
        if text == ":root":
            return RootSelector()
        parts = []
        i = 0
        n = len(text)
        while i < n:
            c = text[i]
            if c == ".":
                name, i = _consume_name(text, i + 1)
                if name is None:
                    return None
                parts.append(ClassSelector(name))
            elif c == "#":
                name, i = _consume_name(text, i + 1)
                if name is None:
                    return None
                parts.append(IdSelector(name))
            elif c == "[":
                sel, end = _attr_selector(text, i)
                if sel is None:
                    return None
                parts.append(sel)
                i = end
            elif c == ":":
                sel, end = self._pseudo_selector(text, i)
                if sel is None:
                    return None
                if sel is not _SKIP_PSEUDO:
                    parts.append(sel)
                i = end
            elif c == "*":
                parts.append(TagSelector("*"))
                i += 1
            elif c == "\\" or c.isalpha() or ord(c) >= 0x80:
                name, i = _consume_name(text, i)
                if name is None:
                    return None
                parts.append(TagSelector(name.lower()))
            else:
                return None
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return CompoundSelector(parts)

    def _pseudo_selector(self, text, i):
        """Parse a `:name(arg)` token starting at `i`. Returns `(selector,
        end_index)`; the selector is a real selector, the `_SKIP_PSEUDO`
        sentinel (dynamic pseudo-class: apply the base selector, emit nothing)
        or None (pseudo-element / bad syntax: drop the whole rule)."""
        n = len(text)
        j = i + 1
        if j < n and text[j] == ":":
            return None, j  # pseudo-element (::before, ...): drop the rule
        while j < n and re.match(r"[A-Za-z0-9_-]", text[j]):
            j += 1
        name = text[i + 1:j].lower()
        for pref in _VENDOR_PREFIXES:
            if name.startswith(pref):
                name = name[len(pref):]
                break
        if name in _PSEUDO_ELEMENTS:
            return None, j
        arg = None
        if j < n and text[j] == "(":
            depth = 1
            j += 1
            start = j
            while j < n and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            if depth != 0:
                return None, j
            arg = text[start:j - 1]
        if name == "root":
            sel = RootSelector()
        elif name in ("not", "is", "where", "has"):
            arg_sels = self._pseudo_arg(arg)
            if arg_sels is None:
                return None, j
            sel = PseudoSelector(name, arg_sels)
        elif name in _DYNAMIC_PSEUDOS:
            sel = _SKIP_PSEUDO
        else:
            sel = PseudoSelector(name, arg)
        return sel, j

    def _pseudo_arg(self, arg):
        """Parse the selector argument of :not/:is/:where/:has."""
        if arg is None or not arg.strip():
            return None
        sels = []
        for part in _split_top_level(arg):
            sel = self.selector(part.strip())
            if sel is None:
                return None
            sels.append(sel)
        return sels

    def selector(self, text):
        # Split into compounds and the combinators between them. Whitespace
        # inside parentheses is left alone, so `:has(> img)` survives; a
        # combinator needs no space around it, because minified CSS writes
        # `.menu>li+li` and that is three compounds, not one.
        tokens = []
        buf = []
        depth = 0
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == "\\":
                # An escaped character is part of a name, whatever it is: the
                # `>` in `.\[\&\>li\]\:mt-2` is three glyphs of a class name,
                # not a child combinator, and the `[` beside it is not a
                # bracket to count either. Carried through raw so the escape
                # is still there for _consume_name to read.
                _char, end = _consume_escape(text, i)
                buf.append(text[i:end])
                i = end
                continue
            i += 1
            if ch in "([":
                depth += 1
                buf.append(ch)
            elif ch in ")]":
                depth = max(0, depth - 1)
                buf.append(ch)
            elif depth == 0 and (ch in " \t\r\n\f" or ch in ">+~"):
                if buf:
                    tokens.append("".join(buf))
                    buf = []
                if ch in ">+~":
                    # A combinator replaces any descendant space beside it.
                    if tokens and tokens[-1] in (">", "+", "~"):
                        tokens[-1] = ch
                    else:
                        tokens.append(ch)
            else:
                buf.append(ch)
        if buf:
            tokens.append("".join(buf))
        while tokens and tokens[0] in (">", "+", "~"):
            # A leading combinator means a relative selector, and the only
            # place we accept one is inside `:has()` -- `:has(> img)`. What it
            # is relative to is the element being tested, which _has_match
            # already supplies by walking descendants, so the combinator has
            # nothing left to say here.
            tokens.pop(0)
        if not tokens:
            return None
        result = self.simple_selector(tokens[0])
        if result is None:
            return None
        combinator = " "
        for tok in tokens[1:]:
            if tok in (">", "+", "~"):
                combinator = tok
                continue
            simple = self.simple_selector(tok)
            if simple is None:
                return None
            if combinator == ">":
                result = ChildSelector(result, simple)
            elif combinator in ("+", "~"):
                result = SiblingSelector(result, simple, combinator == "+")
            else:
                result = DescendantSelector(result, simple)
            combinator = " "
        return result

    def parse(self):
        """Return a list of (selector, declarations) rules."""
        rules = []
        while self.i < len(self.s):
            here = self.i
            self.skip_ws()
            if self.i >= len(self.s):
                break
            # @-rules: skip @media { ... } but keep inner rules for @media all/screen.
            if self.s[self.i] == "@":
                self._handle_at_rule(rules)
                if self.i == here:
                    self.i += 1  # see the guard at the foot of this loop
                continue
            # Read selector text up to '{'. A backslash-escaped brace is a
            # name character in a selector, not the start of a block: taking
            # it for one loses the rest of the sheet, not just this rule.
            start = self.i
            while self.i < len(self.s) and self.s[self.i] not in "{}":
                if self.s[self.i] == "\\":
                    _char, self.i = _consume_escape(self.s, self.i)
                    continue
                self.i += 1
            if self.i >= len(self.s) or self.s[self.i] == "}":
                self.i += 1
                continue
            sel_text = self.s[start:self.i].strip()
            self.literal("{")
            decls = self.body()
            important = self.important
            self.literal("}")
            for one in _split_top_level(sel_text):
                sel = self.selector(one.strip())
                if sel is not None:
                    rules.append((sel, decls))
                    if important:
                        # The same selector twice, once per importance band.
                        # Emitting the important declarations as a rule of
                        # their own is what lets one flat specificity sort
                        # express a cascade that has two levels in it.
                        rules.append((_important_selector(sel), important))
            if self.i == here:
                # A stylesheet is arbitrary bytes off the network, and every
                # loop that walks one has to end. Each branch above already
                # consumes at least the character it looked at; this is the
                # guarantee, so that a future branch that forgets costs a
                # dropped rule instead of a browser that never comes back.
                self.i += 1
        return rules

    def _handle_at_rule(self, rules):
        # Find the at-rule keyword.
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in "{;":
            self.i += 1
        prelude = self.s[start:self.i]
        words = prelude.split()
        keyword = words[0].lower() if words else ""
        if self.i < len(self.s) and self.s[self.i] == ";":
            self.i += 1  # @import/@charset etc.
            return
        # It's a block at-rule.
        self.literal("{")
        if keyword == "@media":
            width, height = _VIEWPORT
            if media_matches(prelude, width, height):
                inner = CSSParser(self._read_block())
                rules.extend(inner.parse())
            else:
                self._read_block()
        elif keyword == "@container":
            # A container query is the one grouping at-rule whose contents are
            # written *expecting* to be off most of the time -- it is how a
            # card says "and when my column is wide, stack me differently".
            # Flattening it makes that variant unconditional, and being later
            # in the sheet it wins over the plain rule it was meant to
            # override. Until container sizes are tracked, off is the honest
            # answer.
            self._read_block()
        elif keyword in ("@supports", "@layer", "@scope"):
            # Grouping at-rules whose condition we cannot evaluate, but whose
            # contents are ordinary rules. Including them naively is much
            # closer to right than dropping them: a modern site puts its
            # entire stylesheet inside `@layer`, and skipping the block left
            # such a page with nothing but the UA sheet. Layer ordering is
            # not modelled -- everything lands in one flat cascade.
            inner = CSSParser(self._read_block())
            rules.extend(inner.parse())
        else:
            self._read_block()  # skip @font-face, @keyframes, etc.

    def _read_block(self):
        """Consume up to the `}` that closes the block already opened.

        Only the braces matter, so this hops between them with `find` rather
        than looking at every character in turn. That is the same scan, but the
        stepping happens in C: a sheet written the modern way wraps everything
        in `@layer`, and each nesting level re-reads the whole text below it,
        so the bytes of a big sheet get walked several times over. Stepping
        them one Python iteration at a time -- with a `len()` per character --
        was most of the cost of parsing CSS at all.
        """
        depth = 1
        s = self.s
        n = len(s)
        start = i = self.i
        while i < n and depth > 0:
            opened = s.find("{", i)
            closed = s.find("}", i)
            if closed < 0:
                # Unbalanced: the block runs to the end of the text. Taking
                # what there is beats discarding the rest of the sheet.
                i = n
                break
            if 0 <= opened < closed:
                depth += 1
                i = opened + 1
            else:
                depth -= 1
                if depth == 0:
                    i = closed
                    break
                i = closed + 1
        self.i = i
        block = s[start:self.i]
        self.literal("}")
        return block


def parse_inline(style_text):
    cached = _INLINE_CACHE.get(style_text)
    if cached is not None:
        return cached
    parser = CSSParser("{" + style_text + "}")
    parser.literal("{")
    pairs = parser.body()
    if len(_INLINE_CACHE) >= _INLINE_CACHE_MAX:
        _INLINE_CACHE.clear()
    _INLINE_CACHE[style_text] = pairs
    return pairs


# Inline `style=""` attributes repeat across a document (same class, same
# styling). The parser round-trip is cheap but style() runs it for every
# element on every cascade, and JS-driven re-cascades replay the whole tree.
_INLINE_CACHE = {}
_INLINE_CACHE_MAX = 2048


_LIST_STYLE_POSITIONS = ("inside", "outside")

# A value is a run of non-space characters, except that a function call keeps
# its parentheses -- and the spaces inside them -- together, so
# `inset: 0 calc(1rem + 2px)` is two values rather than four. Layout has the
# same regex for the same reason; it cannot be shared, because layout imports
# this module and not the other way round.
_COMPONENT_RE = re.compile(r"[^\s(]+\([^)]*\)|\S+")

# The four sides a 1-to-4 value box maps onto, in CSS's clock order: one
# value is all of them, two are (block, inline), three are (top, inline,
# bottom), four are top/right/bottom/left.
_CLOCK = {
    1: (0, 0, 0, 0),
    2: (0, 1, 0, 1),
    3: (0, 1, 2, 1),
    4: (0, 1, 2, 3),
}

# The physical sides the logical ones mean, assuming `writing-mode:
# horizontal-tb` and `direction: ltr`. Both of those are properties this
# engine does not read, so assuming them is not a shortcut so much as a
# statement of what the rest of the layout already believes; an author who
# writes `padding-inline-start` on an English page means padding-left, and
# that is the overwhelming majority of what is out there. A right-to-left
# document will get these mirrored, which is exactly as wrong as everything
# else about it here.
_LOGICAL_SIDES = {
    "block": ("top", "bottom"),
    "inline": ("left", "right"),
    "block-start": ("top",),
    "block-end": ("bottom",),
    "inline-start": ("left",),
    "inline-end": ("right",),
}

_SIDES = ("top", "right", "bottom", "left")

# `inset` and its logical pieces, which say in one declaration what
# top/right/bottom/left say in four. Layout reads the four.
_INSET_SHORTHANDS = frozenset(
    ["inset"] + ["inset-" + k for k in _LOGICAL_SIDES])

# The box properties with a logical spelling. `margin`/`padding` themselves
# are left alone: layout already resolves those shorthands where it uses
# them, and expanding them here would only duplicate that.
_LOGICAL_BOXES = frozenset(
    "%s-%s" % (box, k) for box in ("margin", "padding")
    for k in _LOGICAL_SIDES)

# `grid-gap` and friends: the pre-standard spelling of `gap`, still emitted
# by every grid framework of a certain age and by autoprefixer to this day.
_GAP_ALIASES = {
    "grid-gap": "gap",
    "grid-row-gap": "row-gap",
    "grid-column-gap": "column-gap",
}
_GAP_SHORTHANDS = frozenset(set(_GAP_ALIASES) | {"gap"})

# The shorthands `_expand` has something to say about. The cascade in Rust
# reads this to know when it has to ask, so a declaration whose property is
# not in here is applied as it stands.
EXPANDING_SHORTHANDS = frozenset(
    {"list-style"} | _GAP_SHORTHANDS | _INSET_SHORTHANDS | _LOGICAL_BOXES)


def _components(value):
    """Split a value into its space-separated components."""
    return _COMPONENT_RE.findall(value or "")


def _box_sides(value):
    """Map a 1-to-4 value box onto (top, right, bottom, left), or None when
    the value is not a box at all (`inset: auto`, five values, nothing)."""
    parts = _components(value)
    order = _CLOCK.get(len(parts))
    if order is None:
        return None
    return tuple(parts[i] for i in order)


def _expand(prop, value):
    """Yield the properties a declaration really sets.

    Almost every shorthand in this engine is left un-expanded and read
    where it is used, which works because layout can go looking for it.
    The ones handled here are the ones where that does not work: either
    because the shorthand's parts do not all behave alike (`list-style`), or
    because the property is a *different name* for one layout already reads
    and would otherwise be cascaded onto the node and never looked at again
    (`inset`, `grid-gap`, and the logical margins and paddings). Those three
    account for the largest block of dropped declarations on the sites
    surveyed -- 1222 `inset`, 1315 `grid-*-gap` -- and every one of them is
    a box that ends up in the wrong place.

    Expansion happens in declaration order, exactly as writing the longhand
    would have been, and the shorthand itself is yielded first so anything
    still reading it directly is unaffected.
    """
    yield prop, value
    if prop == "list-style":
        for token in value.split():
            lowered = token.lower()
            if lowered in _LIST_STYLE_POSITIONS:
                yield "list-style-position", lowered
            elif lowered.startswith("url("):
                yield "list-style-image", token
            else:
                yield "list-style-type", lowered
        return
    prop = _GAP_ALIASES.get(prop, prop)
    if prop == "row-gap" or prop == "column-gap":
        # Only reachable as the standard spelling of a `grid-*-gap` alias;
        # the longhands themselves are read where they are used.
        yield prop, value
        return
    if prop == "gap":
        # `gap: 10px 20px` is a row gap and then a column gap. Layout reads
        # the shorthand with parse_px, which takes the first number and uses
        # it for both, so the two-value form silently lost its column gap.
        parts = _components(value)
        if parts:
            yield "row-gap", parts[0]
            yield "column-gap", parts[1] if len(parts) > 1 else parts[0]
        return
    if prop in _INSET_SHORTHANDS or prop in _LOGICAL_BOXES:
        box, _dash, logical = prop.partition("-")
        if prop == "inset":
            box, logical = "", ""
        prefix = (box + "-") if box in ("margin", "padding") else ""
        if not logical:
            sides = _box_sides(value)
            if sides is None:
                return
            for side, part in zip(_SIDES, sides):
                yield prefix + side, part
            return
        physical = _LOGICAL_SIDES[logical]
        parts = _components(value)
        if not parts:
            return
        if len(physical) == 1:
            yield prefix + physical[0], parts[0]
            return
        # `margin-inline: a b` is start then end; one value is both.
        for side, part in zip(physical, parts if len(parts) > 1
                              else parts * 2):
            yield prefix + side, part


# `var(--name, fallback)` substitution used to live here, called back into
# from the cascade once per var()-bearing declaration per node. On a page
# built out of custom properties -- the `light-dark()` polyfill emits a
# `:root *` rule setting thirty of them on every element -- that was a
# quarter of a million calls across one style pass and 94% of its time. It is
# a loop, not a table, so it moved to `find_vars`/`resolve_vars` in
# rust/src/css.rs with the rest of the per-node work.
