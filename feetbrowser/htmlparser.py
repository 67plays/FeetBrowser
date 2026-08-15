"""A from-scratch HTML parser producing a DOM tree.

Handles: tags/attributes, comments, doctype, character entities, raw-text
elements (script/style), void elements, and a pragmatic version of implicit
tag insertion (html/head/body) so real-world pages parse into something sane.
"""

import html as _htmllib


class Node:
    def __init__(self, parent):
        self.parent = parent
        self.children = []
        # Filled in by the styling pass.
        self.style = {}


class Text(Node):
    def __init__(self, text, parent):
        super().__init__(parent)
        self.text = text

    def __repr__(self):
        return repr(self.text)


class Element(Node):
    def __init__(self, tag, attributes, parent):
        super().__init__(parent)
        self.tag = tag
        self.attributes = attributes

    def __repr__(self):
        attrs = "".join(f' {k}="{v}"' for k, v in self.attributes.items())
        return f"<{self.tag}{attrs}>"


# Elements that never have children / close themselves.
VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Elements whose text content is not parsed as HTML.
RAW_TEXT_ELEMENTS = {"script", "style"}

# Tags that belong in <head>.
HEAD_TAGS = {
    "base", "basefont", "bgsound", "noscript", "link",
    "meta", "title", "style", "script",
}

# Simple implied-end-tag rules: opening a key tag implicitly closes any
# currently-open tag in the value set. The table row/header/footer tags all
# close each other.
_TABLE_CLOSERS = {"tr", "td", "th", "tbody", "thead", "tfoot"}
IMPLICIT_CLOSE = {
    "li": {"li"},
    "p": {"p"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "tr": {"tr", "td", "th"},
    "thead": _TABLE_CLOSERS,
    "tbody": _TABLE_CLOSERS,
    "tfoot": _TABLE_CLOSERS,
    "dd": {"dd", "dt"},
    "dt": {"dd", "dt"},
    "option": {"option"},
}

# Elements that close an open <p> (the HTML "p implies end" rule). Does not
# include <br>, <li>, <td>, <tr>: those may validly appear inside a <p>.
P_CLOSING_ELEMENTS = {
    "address", "article", "aside", "blockquote", "details", "div", "dl",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav", "ol",
    "p", "pre", "search", "section", "table", "ul",
}


class HTMLParser:
    def __init__(self, body):
        self.body = body
        self.unfinished = []  # stack of open Elements

    def parse(self):
        text = ""
        i = 0
        n = len(self.body)
        in_tag = False
        in_comment = False
        tag_quote = None  # ' or " when inside a quoted attribute value
        raw_text_tag = None  # e.g. "script" while inside <script>...</script>

        while i < n:
            c = self.body[i]

            # Inside a raw-text element, only its matching close tag ends it.
            if raw_text_tag is not None:
                close = "</" + raw_text_tag
                after = self.body[i + len(close):i + len(close) + 1]
                if self.body[i:i + len(close)].lower() == close \
                        and (after == "" or after == ">" or after.isspace()):
                    text = self._flush(text)
                    end = self.body.find(">", i)
                    if end == -1:
                        end = n
                    self.close_tag(raw_text_tag)
                    raw_text_tag = None
                    i = end + 1
                    continue
                text += c
                i += 1
                continue

            # Comment handling.
            if in_comment:
                if self.body[i:i + 3] == "-->":
                    in_comment = False
                    i += 3
                    continue
                i += 1
                continue

            if c == "<":
                if self.body[i:i + 4] == "<!--":
                    text = self._flush(text)
                    in_comment = True
                    i += 4
                    continue
                in_tag = True
                tag_quote = None
                text = self._flush(text)
                i += 1
            elif c == ">" and in_tag:
                if tag_quote is not None:
                    # A '>' inside a quoted attribute value is just data.
                    text += c
                    i += 1
                    continue
                in_tag = False
                opened = self.add_tag(text)
                if opened in RAW_TEXT_ELEMENTS:
                    raw_text_tag = opened
                text = ""
                i += 1
            else:
                if in_tag and c in "\"'" and (tag_quote is None or c == tag_quote):
                    tag_quote = c if tag_quote is None else None
                text += c
                i += 1

        # Flush whatever is left: unterminated raw-text content and tags cut
        # off by EOF are treated as character data rather than silently lost.
        text = self._flush(text)
        return self.finish()

    # -- helpers ---------------------------------------------------------

    def _flush(self, text):
        if text:
            self.add_text(text)
        return ""

    def add_text(self, text):
        decoded = _htmllib.unescape(text)
        # Drop whitespace-only text when no element is open (between root tags).
        if decoded.strip() == "" and not self.unfinished:
            return
        self.implicit_tags(None)
        if not self.unfinished:
            return
        parent = self.unfinished[-1]
        parent.children.append(Text(decoded, parent))

    def add_tag(self, text):
        tag, attributes = self.get_attributes(text.strip())
        if not tag or tag.startswith("!"):
            return None  # doctype / declaration
        if tag.startswith("/"):
            self.close_tag(tag[1:].strip())
            return None

        self.implicit_close(tag)
        self.implicit_tags(tag)

        parent = self.unfinished[-1] if self.unfinished else None
        node = Element(tag, attributes, parent)
        if tag in VOID_ELEMENTS:
            if parent is not None:
                parent.children.append(node)
            return None
        self.unfinished.append(node)
        return tag

    def close_tag(self, tag):
        tag = tag.lower()
        # Find a matching open tag; close everything up to and including it.
        for idx in range(len(self.unfinished) - 1, -1, -1):
            if self.unfinished[idx].tag == tag:
                while len(self.unfinished) > idx:
                    self._reparent(self.unfinished.pop())
                return
        # No matching open tag -> ignore stray close.

    def _reparent(self, node):
        # Attach a just-popped node to its new parent (or store as the root if
        # the whole document was explicitly closed).
        if self.unfinished:
            self.unfinished[-1].children.append(node)
        else:
            self._root = node

    def implicit_close(self, tag):
        if not self.unfinished:
            return
        open_tag = self.unfinished[-1].tag
        if tag in IMPLICIT_CLOSE and open_tag in IMPLICIT_CLOSE[tag]:
            self.close_tag(open_tag)
        # A block element closes an open <p> (spec: p implies end).
        elif open_tag == "p" and tag in P_CLOSING_ELEMENTS:
            self.close_tag("p")

    def implicit_tags(self, tag):
        # Insert <html>, <head>, <body> as needed. Checks the stack by
        # length/name directly instead of materializing an open-tag list on
        # every call (this runs for every tag and every text run, so the list
        # allocation dominated parse time on text-heavy documents).
        stack = self.unfinished
        while True:
            if not stack:
                if tag == "html":
                    break
                stack.append(Element("html", {}, None))
            elif len(stack) == 1 and stack[0].tag == "html" \
                    and tag not in ("head", "body", "/html"):
                stack.append(Element(
                    "head" if tag in HEAD_TAGS else "body", {}, stack[0]))
            elif len(stack) == 2 and stack[0].tag == "html" \
                    and stack[1].tag == "head" and tag != "/head" \
                    and tag not in HEAD_TAGS:
                self.close_tag("head")
            else:
                break

    def _skip_ws(self, text, i):
        while i < len(text) and text[i].isspace():
            i += 1
        return i

    def get_attributes(self, text):
        if not text:
            return "", {}
        # Split tag name from the rest.
        i = 0
        while i < len(text) and not text[i].isspace():
            i += 1
        tag = text[:i].lower().rstrip("/")
        rest = text[i:]

        attributes = {}
        # Manual attribute scanner handling quotes.
        j = 0
        m = len(rest)
        while j < m:
            j = self._skip_ws(rest, j)
            if j >= m:
                break
            start = j
            while j < m and rest[j] not in "= \t\r\n/":
                j += 1
            name = rest[start:j].lower()
            # Skip whitespace before '='
            k = self._skip_ws(rest, j)
            if k < m and rest[k] == "=":
                k += 1
                k = self._skip_ws(rest, k)
                if k < m and rest[k] in "\"'":
                    quote = rest[k]
                    k += 1
                    vstart = k
                    while k < m and rest[k] != quote:
                        k += 1
                    value = rest[vstart:k]
                    k += 1
                else:
                    vstart = k
                    while k < m and not rest[k].isspace() and rest[k] != "/":
                        k += 1
                    value = rest[vstart:k]
                if name:
                    attributes[name] = _htmllib.unescape(value)
                j = k
            else:
                if name:
                    attributes[name] = ""
                j = j if j > start else j + 1

        return tag, attributes

    def finish(self):
        # If the document explicitly closed its root, the finished tree is in
        # _root; don't synthesize a new (empty) one.
        if not self.unfinished:
            return getattr(self, "_root", Element("html", {}, None))
        while len(self.unfinished) > 1:
            self._reparent(self.unfinished.pop())
        return self.unfinished.pop()
