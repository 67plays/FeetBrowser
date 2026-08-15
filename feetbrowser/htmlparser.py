"""The DOM node types, and the entry point to the HTML parser.

The parser itself is `rust/src/html/`: a WHATWG-conformant tokenizer and tree
builder that scores 99.6% on the html5lib tree-construction suite. It parses
into a Rust arena and then materialises that arena into the `Element` and
`Text` objects defined here, which is what every consumer in this package
reads. See `rust/src/materialize.rs` for why the document is handed to Python
rather than kept in the arena and proxied.

The hand-rolled Python parser that used to live here was replaced wholesale.
It was a character loop with a stack of open tags and a table of implied end
tags, and it got the easy majority of real markup right; the cases it could
not reach are not "more tags" but three algorithms that move nodes already in
the tree -- foster parenting, formatting reconstruction, and the adoption
agency. `HTMLParser` below is kept as a thin compatibility shim over the new
entry point so existing callers keep working.
"""

from feetbrowser_engine import parse_html as _parse_html
from feetbrowser_engine import parse_fragment_html as _parse_fragment


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


def parse(body, scripting=False):
    """Parse a document and return its `<html>` Element.

    `scripting` is the spec's scripting flag, which only changes how
    `<noscript>` is treated. It is off by default because this browser runs
    scripts *after* the parse rather than during it, so a `<noscript>` block's
    contents are still the markup the page expects to be laid out.
    """
    return _parse_html(body, scripting)


def parse_fragment(body, context="body"):
    """Parse `body` as the contents of a `context` element.

    Returns a list of top-level nodes. This is what `innerHTML` assignment
    uses; parsing in context is what lets `<td>` survive an assignment to a
    `<tr>`.
    """
    return _parse_fragment(body, context)


class HTMLParser:
    """Compatibility shim: `HTMLParser(body).parse()` still works.

    Kept so the test suite and `tests/smoke.py` did not all have to change in
    the same commit that swapped the parser out. New code should call
    `parse()` directly.
    """

    def __init__(self, body):
        self.body = body

    def parse(self):
        return parse(self.body)
