"""A from-scratch CSS parser + cascade.

Supports: tag / class / id / universal selectors, descendant combinators,
grouped selectors (a, b), a property/value declaration parser, specificity,
inheritance of inherited properties, and inline style="" attributes.
"""

import re

from .htmlparser import Element

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


class TagSelector:
    def __init__(self, tag):
        self.tag = tag
        self.priority = (0, 0, 1) if tag != "*" else (0, 0, 0)

    def matches(self, node):
        return isinstance(node, Element) and (self.tag == "*" or node.tag == self.tag)


class ClassSelector:
    def __init__(self, cls):
        self.cls = cls
        self.priority = (0, 1, 0)

    def matches(self, node):
        if not isinstance(node, Element):
            return False
        return self.cls in node.attributes.get("class", "").split()


class IdSelector:
    def __init__(self, id_):
        self.id = id_
        self.priority = (1, 0, 0)

    def matches(self, node):
        return isinstance(node, Element) and node.attributes.get("id") == self.id


class CompoundSelector:
    """One or more simple selectors on the same element, e.g. div.note#x"""

    def __init__(self, parts):
        self.parts = parts
        self.priority = tuple(sum(p) for p in zip(*[s.priority for s in parts]))

    def matches(self, node):
        return all(p.matches(node) for p in self.parts)


class DescendantSelector:
    def __init__(self, ancestor, descendant):
        self.ancestor = ancestor
        self.descendant = descendant
        self.priority = tuple(
            a + b for a, b in zip(ancestor.priority, descendant.priority))

    def matches(self, node):
        if not self.descendant.matches(node):
            return False
        # Fast path: when the style pass has primed the node's ancestor
        # features (it walks the tree top-down), a tag/class/id ancestor can
        # be decided with a set membership test instead of walking up the
        # parent chain again for every rule.
        a = self.ancestor
        anc_tags = getattr(node, "_anc_tags", None)
        if anc_tags is not None:
            if isinstance(a, TagSelector) and a.tag != "*":
                return a.tag in anc_tags
            if isinstance(a, ClassSelector):
                return a.cls in getattr(node, "_anc_classes", ())
            if isinstance(a, IdSelector):
                return a.id in getattr(node, "_anc_ids", ())
            if not _ancestor_possible(a, node):
                return False
        parent = node.parent
        while parent:
            if self.ancestor.matches(parent):
                return True
            parent = parent.parent
        return False


class ChildSelector:
    """`a > b`: b's own parent must be an a, not merely some ancestor.

    Treating this as a descendant relationship is how one `.menu > li` rule
    reaches every list item on the page, submenus included.
    """

    def __init__(self, parent, child):
        self.parent = parent
        self.child = child
        self.priority = tuple(
            a + b for a, b in zip(parent.priority, child.priority))

    def matches(self, node):
        return self.child.matches(node) and node.parent is not None \
            and isinstance(node.parent, Element) \
            and self.parent.matches(node.parent)


class SiblingSelector:
    """`a + b` (adjacent) and `a ~ b` (any earlier sibling)."""

    def __init__(self, before, after, adjacent):
        self.before = before
        self.after = after
        self.adjacent = adjacent
        self.priority = tuple(
            a + b for a, b in zip(before.priority, after.priority))

    def matches(self, node):
        if not self.after.matches(node):
            return False
        parent = node.parent
        if not isinstance(parent, Element):
            return False
        earlier = []
        for child in parent.children:
            if child is node:
                break
            if isinstance(child, Element):
                earlier.append(child)
        if self.adjacent:
            return bool(earlier) and self.before.matches(earlier[-1])
        return any(self.before.matches(sib) for sib in earlier)


class RootSelector:
    """Matches the document root element (`:root`), i.e. the node with no
    parent. Typical target for custom-property (`--x`) declarations."""

    def __init__(self):
        self.priority = (0, 0, 1)

    def matches(self, node):
        return isinstance(node, Element) and node.parent is None


class AttrSelector:
    """Attribute selector: [attr], [attr=value], [attr~=v], [attr|=v],
    [attr^=v], [attr$=v], [attr*=v]. `op` is None for mere presence."""

    def __init__(self, attr, op=None, value=None):
        self.attr = attr
        self.op = op
        self.value = value
        self.priority = (0, 1, 0)

    def matches(self, node):
        if not isinstance(node, Element):
            return False
        val = node.attributes.get(self.attr)
        if val is None:
            return False
        if self.op is None:
            return True
        val = str(val)
        if self.op == "=":
            return val == self.value
        if self.op == "~=":
            return self.value in val.split()
        if self.op == "|=":
            return val == self.value or val.startswith(self.value + "-")
        if self.op == "^=":
            return val.startswith(self.value)
        if self.op == "$=":
            return val.endswith(self.value)
        if self.op == "*=":
            return self.value in val
        return False


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

_FORM_TAGS = frozenset({
    "input", "button", "select", "textarea", "option", "fieldset",
})


def _match_nth(expr, index):
    """Evaluate an :nth-child() expression against a 1-based element index."""
    if expr is None:
        return False
    expr = expr.strip().lower()
    if expr == "odd":
        return index % 2 == 1
    if expr == "even":
        return index % 2 == 0
    if "n" in expr:
        m = re.match(r"^([+-]?\d*)n\s*(?:([+-])\s*(\d+))?$", expr)
        if not m:
            return False
        a_str = m.group(1)
        a = int(a_str) if a_str not in ("", "+", "-") \
            else (1 if a_str in ("", "+") else -1)
        if m.group(2) is None:
            b = 0
        else:
            b = int(m.group(3)) * (1 if m.group(2) == "+" else -1)
        diff = index - b
        if diff % a != 0:
            return False
        k = diff // a
        return k >= 1 and index >= 1
    try:
        return index == int(expr)
    except ValueError:
        return False


class PseudoSelector:
    """A pseudo-class on an element, e.g. :first-child, :not(.x), :nth-of-type.
    `arg` holds the matched argument text, or (for :not/:is/:where/:has) a
    list of parsed sub-selectors."""

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

    def matches(self, node):
        name = self.name
        if name in ("not", "is", "where"):
            if name == "not":
                return not any(s.matches(node) for s in self.arg or ())
            return any(s.matches(node) for s in self.arg or ())
        if name == "has":
            return _has_match(node, self.arg or ())
        if name in ("first-child", "last-child", "only-child", "nth-child",
                    "nth-last-child", "first-of-type", "last-of-type",
                    "only-of-type", "nth-of-type", "nth-last-of-type"):
            return self._structural(node)
        if name == "empty":
            return isinstance(node, Element) and not node.children
        if name == "link":
            return isinstance(node, Element) and node.tag == "a" \
                and "href" in node.attributes
        if name == "checked":
            return isinstance(node, Element) and node.tag in ("input", "option") \
                and "checked" in node.attributes
        if name in ("disabled", "enabled", "required"):
            if not isinstance(node, Element) or node.tag not in _FORM_TAGS:
                return False
            if name == "disabled":
                return "disabled" in node.attributes
            if name == "enabled":
                return "disabled" not in node.attributes
            return "required" in node.attributes
        return False

    def _structural(self, node):
        if not isinstance(node, Element) or node.parent is None:
            return False
        sibs = [c for c in node.parent.children if isinstance(c, Element)]
        try:
            idx = sibs.index(node)
        except ValueError:
            return False
        count = len(sibs)
        name = self.name
        if "of-type" in name:
            tsibs = [c for c in sibs if c.tag == node.tag]
            tidx = tsibs.index(node)
            tcount = len(tsibs)
            if name == "first-of-type":
                return tidx == 0
            if name == "last-of-type":
                return tidx == tcount - 1
            if name == "only-of-type":
                return tcount == 1
            if name == "nth-of-type":
                return _match_nth(self.arg, tidx + 1)
            return _match_nth(self.arg, tcount - tidx)
        if name == "first-child":
            return idx == 0
        if name == "last-child":
            return idx == count - 1
        if name == "only-child":
            return count == 1
        if name == "nth-child":
            return _match_nth(self.arg, idx + 1)
        return _match_nth(self.arg, count - idx)


def _has_match(node, selectors):
    """:has(sel) — true when any descendant matches `sel`. Child (>) and
    sibling (+/~) combinators inside the argument are treated as the
    descendant approximation already used elsewhere in the parser."""
    if not isinstance(node, Element):
        return False
    for child in node.children:
        if any(s.matches(child) for s in selectors):
            return True
        if isinstance(child, Element) and _has_match(child, selectors):
            return True
    return False


def _ancestor_possible(selector, node):
    """Cheap necessary condition for `selector` to match some ancestor of
    `node`, using the ancestor-feature sets primed by style(). Only used as a
    quick-reject; a True result still requires the real ancestor walk."""
    tags = node._anc_tags
    classes = node._anc_classes
    ids = node._anc_ids
    if isinstance(selector, TagSelector):
        return selector.tag == "*" or selector.tag in tags
    if isinstance(selector, ClassSelector):
        return selector.cls in classes
    if isinstance(selector, IdSelector):
        return selector.id in ids
    if isinstance(selector, CompoundSelector):
        # Must hold for every part, though not necessarily the same ancestor.
        return all(_ancestor_possible(p, node) for p in selector.parts)
    if isinstance(selector, DescendantSelector):
        # The chain is A (X) (descendant of) B...: B must be reachable.
        return _ancestor_possible(selector.descendant, node)
    return True


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


_ATTR_SEL_RE = re.compile(
    r"\[\s*([-_A-Za-z0-9]+)\s*"
    r"(?:([~|^$*]?=)\s*(?:\"([^\"]*)\"|'([^']*)'|([^\]\s]+))\s*)?\]")


class CSSParser:
    def __init__(self, s):
        self.s = s
        self.i = 0

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
            elif depth == 0 and c in ";}":
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
        if value.endswith("!important"):
            value = value[:-len("!important")].rstrip()
        return (prop, value)

    def body(self):
        """Parse a declaration block { ... } already positioned after '{'."""
        pairs = {}
        while self.i < len(self.s) and self.s[self.i] != "}":
            self.skip_ws()
            if self.i >= len(self.s) or self.s[self.i] == "}":
                break
            p = self.pair()
            if p:
                pairs[p[0]] = p[1]
            self.skip_ws()
            self.literal(";")
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
                m = re.match(r"\.[-_A-Za-z0-9]+", text[i:])
                if not m:
                    return None
                parts.append(ClassSelector(m.group(0)[1:]))
                i += m.end()
            elif c == "#":
                m = re.match(r"#[-_A-Za-z0-9]+", text[i:])
                if not m:
                    return None
                parts.append(IdSelector(m.group(0)[1:]))
                i += m.end()
            elif c == "[":
                m = _ATTR_SEL_RE.match(text, i)
                if not m:
                    return None
                value = m.group(3)
                if value is None:
                    value = m.group(4)
                if value is None:
                    value = m.group(5)
                parts.append(AttrSelector(m.group(1).lower(), m.group(2),
                                          value))
                i = m.end()
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
            elif re.match(r"[A-Za-z]", c):
                m = re.match(r"[-_A-Za-z0-9]+", text[i:])
                parts.append(TagSelector(m.group(0).lower()))
                i += m.end()
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
        for part in arg.split(","):
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
        for ch in text:
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
            self.skip_ws()
            if self.i >= len(self.s):
                break
            # @-rules: skip @media { ... } but keep inner rules for @media all/screen.
            if self.s[self.i] == "@":
                self._handle_at_rule(rules)
                continue
            # Read selector text up to '{'.
            start = self.i
            while self.i < len(self.s) and self.s[self.i] not in "{}":
                self.i += 1
            if self.i >= len(self.s) or self.s[self.i] == "}":
                self.i += 1
                continue
            sel_text = self.s[start:self.i].strip()
            self.literal("{")
            decls = self.body()
            self.literal("}")
            for one in sel_text.split(","):
                sel = self.selector(one.strip())
                if sel is not None:
                    rules.append((sel, decls))
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
        depth = 1
        start = self.i
        while self.i < len(self.s) and depth > 0:
            if self.s[self.i] == "{":
                depth += 1
            elif self.s[self.i] == "}":
                depth -= 1
                if depth == 0:
                    break
            self.i += 1
        block = self.s[start:self.i]
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


def cascade_priority(rule):
    return rule[0].priority


def _selector_hint(selector):
    """Terminal-part hint used to bucket rules. Returns a tuple that must match
    the node (tag name, class, id) for the rule to have any chance of matching,
    so style() only walks candidates instead of every rule."""
    if isinstance(selector, TagSelector):
        return ("tag", selector.tag) if selector.tag != "*" else ("any", None)
    if isinstance(selector, ClassSelector):
        return ("class", selector.cls)
    if isinstance(selector, IdSelector):
        return ("id", selector.id)
    if isinstance(selector, RootSelector):
        return ("root", None)
    if isinstance(selector, CompoundSelector):
        # Walk parts from the end so a trailing :nth-child/[attr] pseudo
        # doesn't hide a bucketing tag/class/id that precedes it.
        for part in reversed(selector.parts):
            hint = _selector_hint(part)
            if hint[0] != "any":
                return hint
        return ("any", None)
    if isinstance(selector, DescendantSelector):
        return _selector_hint(selector.descendant)
    return ("any", None)


_RULE_KEY = lambda item: (item[0], item[1])

# Rule-index cache keyed by the rules list's identity. A Tab re-cascades the
# *same* `self._last_rules` list for every JS-driven DOM mutation, and the
# index construction (sorting every bucket) is the most expensive part of a
# restyle; keeping it around turns those re-cascades into a dict lookup. The
# cache holds the list too so an id() can never be reused by a newer list.
_RULE_INDEX_CACHE = {}
_RULE_INDEX_CACHE_MAX = 32


def _build_rule_index(rules):
    key = id(rules)
    cached = _RULE_INDEX_CACHE.get(key)
    if cached is not None:
        return cached[1]
    index = {}
    for i, (selector, body) in enumerate(rules):
        hint = _selector_hint(selector)
        index.setdefault(hint, []).append((selector.priority, i, selector, body))
    for bucket in index.values():
        bucket.sort(key=_RULE_KEY)
    if len(_RULE_INDEX_CACHE) >= _RULE_INDEX_CACHE_MAX:
        _RULE_INDEX_CACHE.clear()
    _RULE_INDEX_CACHE[key] = (rules, index)
    return index


_LIST_STYLE_POSITIONS = ("inside", "outside")


def _expand(prop, value):
    """Yield the properties a declaration really sets.

    Almost every shorthand in this engine is left un-expanded and read
    where it is used, which works because layout can go looking for it.
    `list-style` cannot be handled that way: only its type component
    inherits, and the whole point of `list-style: none` on a <ul> is that
    the <li>s inside it lose their markers. So it is expanded here, in
    declaration order, exactly as writing the longhand would have been.
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


def style(node, rules):
    """Compute the `.style` dict for `node` and its subtree.

    Rules are bucketed by selector hint so each node only considers rules that
    could possibly match it instead of scanning the whole rule list (a
    text-heavy page has thousands of rules but a node only ever matches a
    handful). The tree walk is iterative so deeply nested documents cannot
    blow the recursion limit.
    """
    index = _build_rule_index(rules)

    stack = [(node, None)]
    while stack:
        node, parent = stack.pop()

        node.style = {}

        # Prime ancestor feature sets for descendant-selector fast paths. The
        # child's ancestors = parent's ancestors + the parent itself.
        if parent is None:
            node._anc_tags = node._anc_classes = node._anc_ids = frozenset()
        else:
            node._anc_tags = getattr(parent, "_anc_tags", frozenset()) | (
                {parent.tag} if isinstance(parent, Element) else frozenset())
            node._anc_classes = getattr(parent, "_anc_classes", frozenset()) | (
                frozenset(parent.attributes.get("class", "").split())
                if isinstance(parent, Element) else frozenset())
            node._anc_ids = getattr(parent, "_anc_ids", frozenset()) | (
                {parent.attributes.get("id")}
                if isinstance(parent, Element)
                and parent.attributes.get("id") else frozenset())

        # 1. Inherited properties from parent (or defaults at root).
        parent_style = parent.style if parent else {}
        node.style.update(
            {p: parent_style.get(p, d) for p, d in INHERITED_PROPERTIES.items()})

        # 2. Author + UA rules, in cascade order. Only rules whose terminal
        #    selector could match this node's tag / classes / id are walked.
        hints = [("any", None)]
        if isinstance(node, Element):
            hints.append(("tag", node.tag))
            hints.append(("id", node.attributes.get("id")))
            if node.parent is None:
                hints.append(("root", None))
            for cls in node.attributes.get("class", "").split():
                hints.append(("class", cls))
        candidates = [r for hint in hints for r in index.get(hint, ())]
        candidates.sort(key=_RULE_KEY)
        for _prio, _i, selector, body in candidates:
            if not selector.matches(node):
                continue
            for prop, value in body.items():
                for expanded, v in _expand(prop, value):
                    node.style[expanded] = v

        # 3. Inline style attribute (highest, aside from !important we ignore).
        if isinstance(node, Element) and "style" in node.attributes:
            for prop, value in parse_inline(node.attributes["style"]).items():
                for expanded, v in _expand(prop, value):
                    node.style[expanded] = v

        # 3b. Resolve var(--custom, fallback) references. Custom properties
        # inherit, so lookups walk up the parent chain.
        for prop, value in list(node.style.items()):
            if "var(" in value:
                node.style[prop] = _resolve_var(value, node)

        # 4. Resolve relative font sizes (percent / em) against the parent.
        _resolve_font_size(node)

        for child in reversed(node.children):
            stack.append((child, node))


_VAR_RE = re.compile(
    r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*([^()]*))?\)")


def _resolve_var(value, node):
    """Substitute every `var(--name, fallback)` in `value`, walking the
    ancestor chain for the custom property. Runs to a fixed point so nested
    fallbacks (e.g. `var(--a, var(--b, #fff))`) also resolve."""
    for _ in range(10):
        resolved = value
        for match in _VAR_RE.finditer(value):
            custom_name = match.group(1)
            fallback = match.group(2)
            current = node
            replacement = None
            while current is not None:
                if isinstance(current, Element) \
                        and custom_name in current.style:
                    replacement = current.style[custom_name]
                    break
                current = current.parent
            if replacement is None:
                replacement = fallback.strip() if fallback is not None else ""
            resolved = resolved.replace(match.group(0), replacement, 1)
        if resolved == value:
            break
        value = resolved
    return value


def _resolve_font_size(node):
    if "font-size" not in node.style:
        return
    value = node.style["font-size"]
    parent_size = 16.0
    if node.parent and "font-size" in node.parent.style:
        ps = node.parent.style["font-size"]
        if ps.endswith("px"):
            try:
                parent_size = float(ps[:-2])
            except ValueError:
                pass
    if value.endswith("%"):
        def factor(v):
            return parent_size * float(v[:-1]) / 100
    elif value.endswith("em"):
        def factor(v):
            return parent_size * float(v[:-2])
    elif value in ("smaller", "larger"):
        def factor(v):
            return parent_size * (0.8 if v == "smaller" else 1.2)
    else:
        return
    try:
        node.style["font-size"] = f"{factor(value):.1f}px"
    except ValueError:
        node.style["font-size"] = f"{parent_size}px"
