#!/usr/bin/env python3
"""Generate `rust/src/html/entities.rs` from `rust/data/entities.json`.

The named character reference table in WHATWG HTML §13.5 has 2231 entries.
Hand-typing it would be 2231 opportunities to fat-finger a codepoint, and no
review would catch it, so the table is generated instead and the generator is
checked in next to the data it reads.

Usage (from the repo root):

    python3 rust/tools/gen_entities.py

Re-run it if `rust/data/entities.json` is ever refreshed from the spec.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUST = os.path.dirname(HERE)
SRC = os.path.join(RUST, "data", "entities.json")
DST = os.path.join(RUST, "src", "html", "entities.rs")

SOURCE_URL = "https://html.spec.whatwg.org/entities.json"


def rust_str(s: str) -> str:
    """Escape a Python str as a Rust string literal."""
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif " " <= ch <= "~":
            out.append(ch)
        else:
            out.append("\\u{%x}" % ord(ch))
    out.append('"')
    return "".join(out)


def main() -> int:
    with open(SRC, encoding="utf-8") as fh:
        data = json.load(fh)

    # Keys arrive as "&name" or "&name;". The tokenizer matches against the
    # text *after* the ampersand, so strip it here and keep the trailing
    # semicolon, which is significant: `&not` and `&not;` are both real and
    # expand differently from `&notin;`.
    rows = []
    for key, entry in data.items():
        assert key.startswith("&"), key
        rows.append((key[1:], entry["characters"]))

    # Sorted by byte order so the tokenizer can binary-search.
    rows.sort(key=lambda r: r[0].encode("utf-8"))

    longest = max(len(name) for name, _ in rows)
    with_semi = sum(1 for name, _ in rows if name.endswith(";"))

    lines = [
        "//! The named character reference table from WHATWG HTML §13.5.",
        "//!",
        "//! GENERATED FILE - DO NOT EDIT BY HAND.",
        "//!",
        "//! Source data: %s" % SOURCE_URL,
        "//!   vendored at `rust/data/entities.json`",
        "//! Generator:   `rust/tools/gen_entities.py`",
        "//!",
        "//! Regenerate with `python3 rust/tools/gen_entities.py` from the repo",
        "//! root after refreshing the vendored JSON.",
        "//!",
        "//! %d entries, %d of which require the trailing semicolon. The other" % (len(rows), with_semi),
        "//! %d are the historical semicolon-less forms (`&amp`, `&not`, ...)," % (len(rows) - with_semi),
        "//! which the spec still requires a conforming tokenizer to accept.",
        "",
        "/// The longest name in the table, including its trailing semicolon.",
        "///",
        "/// The tokenizer never needs to look further ahead than this to decide",
        "/// whether a named reference matches.",
        "pub const LONGEST_NAME: usize = %d;" % longest,
        "",
        "/// `(name, replacement)` sorted by `name`'s byte order, where `name` is",
        "/// everything after the `&` and includes the `;` when the entity has one.",
        "pub static NAMED_REFERENCES: &[(&str, &str)] = &[",
    ]
    for name, chars in rows:
        lines.append("    (%s, %s)," % (rust_str(name), rust_str(chars)))
    lines.append("];")
    lines.append("")

    os.makedirs(os.path.dirname(DST), exist_ok=True)
    with open(DST, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print("wrote %s (%d entries, longest %d)" % (DST, len(rows), longest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
