//! FeetBrowser's HTML parser: a WHATWG-conformant tokenizer and tree builder.
//!
//! This replaces the ad-hoc, regex-and-a-stack parser in
//! `feetbrowser/htmlparser.py`, which got the easy 70% of real markup right
//! and then had nowhere to go: the remaining 30% is not "more tags", it is
//! three algorithms (foster parenting, formatting reconstruction, and the
//! adoption agency) that move nodes which are already in the tree.
//!
//! # Layout
//!
//! * [`tokenizer`] — §13.2.5, the ~80-state character-level machine.
//! * [`treebuilder`] — §13.2.6, the insertion modes.
//! * [`entities`] — the generated named character reference table.
//!
//! # Entry points
//!
//! ```ignore
//! let (dom, root) = html::parse(source);
//! let (dom, root) = html::parse_fragment_html(source, "td");
//! ```
//!
//! This is the browser's parser. `feetbrowser/htmlparser.py` calls
//! [`crate::materialize::parse_html`], which drives [`parse`] and hands the
//! resulting tree to Python as `Element`/`Text` objects.

// Same situation as `domtree`: this module is complete and tested but has no
// in-crate caller until Phase 3 rewires the browser off
// `feetbrowser/htmlparser.py`, so every entry point below reads as dead code.
#![allow(dead_code, unused_imports)]

pub mod entities;
pub mod tokenizer;
pub mod treebuilder;

use crate::domtree::{Attr, Dom, Namespace, NodeData, NodeId};

pub use treebuilder::{Mode, QuirksMode, TreeBuilder};

/// Parse a complete document. Returns the arena and its `Document` node.
pub fn parse(source: &str) -> (Dom, NodeId) {
    treebuilder::parse_document(source, false)
}

/// Parse a complete document with scripting enabled, which changes how
/// `<noscript>` is treated.
pub fn parse_scripted(source: &str) -> (Dom, NodeId) {
    treebuilder::parse_document(source, true)
}

/// Parse `source` as the contents of an HTML `context` element — the engine
/// behind `innerHTML`. Returns the arena and the fragment node.
pub fn parse_fragment_html(source: &str, context: &str) -> (Dom, NodeId) {
    treebuilder::parse_fragment(source, Namespace::Html, context, false)
}

/// As [`parse_fragment_html`], with an explicit namespace for the context.
pub fn parse_fragment(
    source: &str,
    context_ns: Namespace,
    context_name: &str,
    scripting: bool,
) -> (Dom, NodeId) {
    treebuilder::parse_fragment(source, context_ns, context_name, scripting)
}

// ---------------------------------------------------------------------------
// The html5lib-tests serialisation format
// ---------------------------------------------------------------------------

/// Render a tree in the format the html5lib-tests `#document` sections use.
///
/// This exists so the fixture suite can be compared as plain text, and it
/// doubles as a readable dump for tests in this crate. It is *not* an HTML
/// serialiser — it never round-trips back to markup.
///
/// ```text
/// | <html>
/// |   <head>
/// |   <body>
/// |     "hello"
/// ```
pub fn serialize_for_tests(dom: &Dom, root: NodeId) -> String {
    let mut out = String::new();
    for child in dom.children(root).collect::<Vec<_>>() {
        write_node(dom, child, 0, &mut out);
    }
    out
}

fn indent(depth: usize, out: &mut String) {
    out.push_str("| ");
    for _ in 0..depth {
        out.push_str("  ");
    }
}

fn write_node(dom: &Dom, id: NodeId, depth: usize, out: &mut String) {
    let Ok(data) = dom.data(id) else { return };
    match data {
        NodeData::Document => {
            indent(depth, out);
            out.push_str("#document\n");
        }
        NodeData::Fragment => {
            // A `<template>`'s contents; html5lib calls this line `content`.
            indent(depth, out);
            out.push_str("content\n");
            for child in dom.children(id).collect::<Vec<_>>() {
                write_node(dom, child, depth + 1, out);
            }
        }
        NodeData::Doctype(d) => {
            indent(depth, out);
            if d.name.is_empty() && d.public_id.is_empty() && d.system_id.is_empty() {
                out.push_str("<!DOCTYPE >\n");
            } else if d.public_id.is_empty() && d.system_id.is_empty() {
                out.push_str(&format!("<!DOCTYPE {}>\n", d.name));
            } else {
                out.push_str(&format!(
                    "<!DOCTYPE {} \"{}\" \"{}\">\n",
                    d.name, d.public_id, d.system_id
                ));
            }
        }
        NodeData::Text(t) => {
            indent(depth, out);
            out.push('"');
            out.push_str(t);
            out.push_str("\"\n");
        }
        NodeData::Comment(c) => {
            indent(depth, out);
            out.push_str(&format!("<!-- {} -->\n", c));
        }
        NodeData::ProcessingInstruction(p) => {
            indent(depth, out);
            out.push_str(&format!("<?{} {}?>\n", p.target, p.data));
        }
        NodeData::Element(e) => {
            indent(depth, out);
            let prefix = match e.namespace {
                Namespace::Svg => "svg ",
                Namespace::MathMl => "math ",
                _ => "",
            };
            out.push_str(&format!("<{}{}>\n", prefix, e.name));

            let mut attrs: Vec<(String, &Attr)> =
                e.attrs.iter().map(|a| (attr_key(a), a)).collect();
            attrs.sort_by(|a, b| a.0.cmp(&b.0));
            for (key, a) in attrs {
                indent(depth + 1, out);
                out.push_str(&format!("{}=\"{}\"\n", key, a.value));
            }

            for child in dom.children(id).collect::<Vec<_>>() {
                write_node(dom, child, depth + 1, out);
            }
        }
    }
}

/// html5lib prints namespaced attributes as `prefix local`, with a space
/// rather than a colon, so that `xlink:href` (no namespace) and `xlink href`
/// (properly adjusted) are distinguishable in a fixture.
fn attr_key(a: &Attr) -> String {
    match (&a.namespace, &a.prefix) {
        (Some(Namespace::XmlNs), None) => "xmlns xmlns".to_string(),
        (Some(_), Some(p)) => format!("{} {}", p, a.local),
        _ => a.local.clone(),
    }
}

#[cfg(test)]
mod tests;
