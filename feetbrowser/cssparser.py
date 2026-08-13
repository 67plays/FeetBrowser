"""A from-scratch CSS parser + cascade.

Supports: tag / class / id / universal selectors, descendant combinators,
grouped selectors (a, b), a property/value declaration parser, specificity,
inheritance of inherited properties, and inline style="" attributes.
"""

import re

from .htmlparser import Element

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


class RootSelector:
    """Matches the document root element (`:root`), i.e. the node with no
    parent. Typical target for custom-property (`--x`) declarations."""

    def __init__(self):
        self.priority = (0, 0, 1)

    def matches(self, node):
        return isinstance(node, Element) and node.parent is None


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


_IDENT_RE = re.compile(r"^[A-Za-z*][-_A-Za-z0-9]*$")


def _strip_pseudo(text):
    """Remove pseudo-classes/-elements (:hover, ::before, :not(.x), ...);
    `:root` is translated to a marker for the root-element selector."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != ":":
            out.append(text[i])
            i += 1
            continue
        start = i
        while i < n and text[i] not in " \t\r\n>+~":
            if text[i] == "(":
                depth = 1
                i += 1
                while i < n and depth:
                    if text[i] == "(":
                        depth += 1
                    elif text[i] == ")":
                        depth -= 1
                    i += 1
                continue
            i += 1
        if text[start:i] in (":root", "::root"):
            out.append(":root")
    return "".join(out)


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
        value = self.s[vstart:self.i].strip()
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
        # e.g. div.note#id  or  .cls  or  #id  or  *  or  :root
        text = _strip_pseudo(text).strip()
        if not text:
            return None
        if text == ":root":
            return RootSelector()
        parts = [p for p in re.split(r"(?=[.#])", text) if p]

        simples = []
        for part in parts:
            if part.startswith(("#", ".")):
                ident = part[1:]
                if not ident or not _IDENT_RE.match(ident):
                    return None
                simples.append(IdSelector(ident) if part[0] == "#"
                               else ClassSelector(ident))
            else:
                # Attribute selectors (input[type=x]) etc. are not supported;
                # skip the rule rather than match nothing forever.
                if not _IDENT_RE.match(part):
                    return None
                simples.append(TagSelector(part.lower()))
        if len(simples) == 1:
            return simples[0]
        return CompoundSelector(simples)

    def selector(self, text):
        # Handle descendant combinators (whitespace between simple selectors).
        # >, + and ~ are approximated as descendant relationships; unsupported
        # tokens are dropped rather than left to crash the rule.
        tokens = text.split()
        tokens = [t for t in tokens if t not in (">", "+", "~")]
        if not tokens:
            return None
        result = self.simple_selector(tokens[0])
        if result is None:
            return None
        for tok in tokens[1:]:
            simple = self.simple_selector(tok)
            if simple is None:
                return None
            result = DescendantSelector(result, simple)
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
        if keyword in ("@media", "@supports"):
            # Naively include the inner rules regardless of the query.
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
    parser = CSSParser("{" + style_text + "}")
    parser.literal("{")
    return parser.body()


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
        return _selector_hint(selector.parts[-1])
    if isinstance(selector, DescendantSelector):
        return _selector_hint(selector.descendant)
    return ("any", None)


_RULE_KEY = lambda item: (item[0], item[1])


def _build_rule_index(rules):
    """Bucket rules by terminal-selector hint. Buckets keep cascade order:
    within a bucket rules are stable-sorted by (priority, original index), and
    merging buckets on the same key never reorders equal priorities."""
    index = {}
    for i, (selector, body) in enumerate(rules):
        hint = _selector_hint(selector)
        index.setdefault(hint, []).append((selector.priority, i, selector, body))
    for bucket in index.values():
        bucket.sort(key=_RULE_KEY)
    return index


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
                node.style[prop] = value

        # 3. Inline style attribute (highest, aside from !important we ignore).
        if isinstance(node, Element) and "style" in node.attributes:
            for prop, value in parse_inline(node.attributes["style"]).items():
                node.style[prop] = value

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
