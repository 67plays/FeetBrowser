//! Fixture tests for the layout engine.
//!
//! # Where the expected numbers come from
//!
//! Every expectation in this file is a value Chrome produced via
//! `getBoundingClientRect` on the same markup at the same viewport, recorded in
//! `scratchpad/layout-diagnosis.md` before any of this code existed. Nothing
//! here asserts what the engine happens to compute: where a Chrome number was
//! not recorded for a fixture, the fixture has no test and is counted as
//! untested rather than given an invented expectation.
//!
//! # The font problem, stated plainly
//!
//! Chrome measured these pages in a real face; this crate has no font loader
//! reachable from pure Rust (`crate::font::Font` only has a `#[new]`
//! constructor taking a Python buffer). So [`StubFont`] provides deterministic
//! metrics, and where an expectation depends on a real advance, that advance is
//! *injected* from Chrome's own measurement via `with_advance` and stated at
//! the call site. Those tests check the box arithmetic around the text, not the
//! text measurement — which is the part this engine is responsible for.
//!
//! Fixtures whose expectations are font-independent (every explicit length, and
//! every `line-height` given as a number) are unqualified tests.

use std::collections::HashMap;
use std::rc::Rc;

use crate::domtree::{Dom, NodeData, NodeId};
use crate::html;

use super::style::{compute, ComputedStyle, Display, StyleContext};
use super::text::{FontSource, StubFont};
use super::{BoxTreeBuilder, Fragment, FragmentKind};

// ---------------------------------------------------------------------------
// A minimal CSS cascade, for tests only
// ---------------------------------------------------------------------------
//
// `crate::css` cannot be used here: its `style()` entry point is a
// `#[pyfunction]` that imports `feetbrowser.cssparser` and mutates Python
// `Element` objects, so it needs an interpreter. This is the smallest parser
// that can drive the fixtures — tag, class, id, `*`, descendant and child
// combinators, and CSS identifier escapes. It is deliberately not exported.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Combinator {
    Descendant,
    Child,
}

#[derive(Debug, Clone, Default)]
struct Compound {
    tag: Option<String>,
    id: Option<String>,
    classes: Vec<String>,
}

#[derive(Debug, Clone)]
struct Selector {
    /// Rightmost compound last; each carries the combinator joining it to the
    /// one on its left.
    parts: Vec<(Option<Combinator>, Compound)>,
}

impl Selector {
    /// (ids, classes, type selectors) — CSS 2.1 §6.4.3.
    fn specificity(&self) -> (u32, u32, u32) {
        let mut s = (0, 0, 0);
        for (_, c) in &self.parts {
            if c.id.is_some() {
                s.0 += 1;
            }
            s.1 += c.classes.len() as u32;
            if c.tag.is_some() {
                s.2 += 1;
            }
        }
        s
    }
}

#[derive(Debug, Clone)]
struct Rule {
    selector: Selector,
    decls: Vec<(String, String)>,
}

/// Read one CSS identifier, honouring backslash escapes.
///
/// This is the whole of diagnosis cause #10: `cssparser.py` matches a class with
/// `\.[-_A-Za-z0-9]+`, which stops dead at the backslash in `.md\:flex` and
/// silently drops the rule. Two thirds of Vimeo's stylesheet goes that way.
fn scan_ident(src: &[char], i: &mut usize) -> String {
    let mut out = String::new();
    while *i < src.len() {
        let c = src[*i];
        if c == '\\' {
            *i += 1;
            if *i < src.len() {
                out.push(src[*i]);
                *i += 1;
            }
            continue;
        }
        if c.is_alphanumeric() || c == '-' || c == '_' || (c as u32) > 127 {
            out.push(c);
            *i += 1;
            continue;
        }
        break;
    }
    out
}

fn parse_compound(src: &[char], i: &mut usize) -> Option<Compound> {
    let mut c = Compound::default();
    let mut any = false;
    while *i < src.len() {
        match src[*i] {
            '*' => {
                *i += 1;
                any = true;
            }
            '.' => {
                *i += 1;
                let n = scan_ident(src, i);
                if n.is_empty() {
                    return None;
                }
                c.classes.push(n);
                any = true;
            }
            '#' => {
                *i += 1;
                let n = scan_ident(src, i);
                if n.is_empty() {
                    return None;
                }
                c.id = Some(n);
                any = true;
            }
            ch if ch.is_alphanumeric() || ch == '\\' || ch == '-' || ch == '_' => {
                let n = scan_ident(src, i);
                if n.is_empty() {
                    return None;
                }
                c.tag = Some(n.to_ascii_lowercase());
                any = true;
            }
            _ => break,
        }
    }
    if any {
        Some(c)
    } else {
        None
    }
}

fn parse_selector(text: &str) -> Option<Selector> {
    let src: Vec<char> = text.trim().chars().collect();
    let mut i = 0;
    let mut parts: Vec<(Option<Combinator>, Compound)> = Vec::new();
    let mut pending: Option<Combinator> = None;
    while i < src.len() {
        while i < src.len() && src[i].is_whitespace() {
            i += 1;
            if !parts.is_empty() && pending.is_none() {
                pending = Some(Combinator::Descendant);
            }
        }
        if i >= src.len() {
            break;
        }
        if src[i] == '>' {
            i += 1;
            pending = Some(Combinator::Child);
            continue;
        }
        let before = i;
        let c = parse_compound(&src, &mut i)?;
        if i == before {
            return None; // unsupported syntax: pseudo-class, attribute selector
        }
        parts.push((pending.take(), c));
    }
    if parts.is_empty() {
        None
    } else {
        Some(Selector { parts })
    }
}

fn parse_declarations(text: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for chunk in text.split(';') {
        let chunk = chunk.trim();
        if chunk.is_empty() {
            continue;
        }
        if let Some(colon) = chunk.find(':') {
            let name = chunk[..colon].trim().to_ascii_lowercase();
            let value = chunk[colon + 1..].trim().to_string();
            if !name.is_empty() && !value.is_empty() {
                out.push((name, value));
            }
        }
    }
    out
}

fn parse_stylesheet(text: &str) -> Vec<Rule> {
    let mut rules = Vec::new();
    let text = strip_comments(text);
    let mut rest = text.as_str();
    while let Some(open) = rest.find('{') {
        let prelude = &rest[..open];
        let after = &rest[open + 1..];
        let close = match after.find('}') {
            Some(c) => c,
            None => break,
        };
        let body = &after[..close];
        if !prelude.trim_start().starts_with('@') {
            let decls = parse_declarations(body);
            for sel in split_selector_list(prelude) {
                if let Some(s) = parse_selector(&sel) {
                    rules.push(Rule { selector: s, decls: decls.clone() });
                }
            }
        }
        rest = &after[close + 1..];
    }
    rules
}

/// Split on commas that are not inside an escape.
fn split_selector_list(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut chars = text.chars();
    while let Some(c) = chars.next() {
        match c {
            '\\' => {
                cur.push(c);
                if let Some(n) = chars.next() {
                    cur.push(n);
                }
            }
            ',' => out.push(std::mem::take(&mut cur)),
            _ => cur.push(c),
        }
    }
    out.push(cur);
    out.into_iter().filter(|s| !s.trim().is_empty()).collect()
}

fn strip_comments(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find("/*") {
        out.push_str(&rest[..start]);
        match rest[start + 2..].find("*/") {
            Some(end) => rest = &rest[start + 2 + end + 2..],
            None => return out,
        }
    }
    out.push_str(rest);
    out
}

// ---------------------------------------------------------------------------
// Matching
// ---------------------------------------------------------------------------

fn classes_of(dom: &Dom, id: NodeId) -> Vec<String> {
    dom.get_attribute(id, "class")
        .ok()
        .flatten()
        .map(|v| v.split_whitespace().map(|s| s.to_string()).collect())
        .unwrap_or_default()
}

fn matches_compound(dom: &Dom, id: NodeId, c: &Compound) -> bool {
    if let Some(tag) = &c.tag {
        match dom.tag_name(id) {
            Some(t) if t.eq_ignore_ascii_case(tag) => {}
            _ => return false,
        }
    }
    if let Some(want) = &c.id {
        match dom.get_attribute(id, "id").ok().flatten() {
            Some(got) if got == want.as_str() => {}
            _ => return false,
        }
    }
    if !c.classes.is_empty() {
        let have = classes_of(dom, id);
        if !c.classes.iter().all(|w| have.iter().any(|h| h == w)) {
            return false;
        }
    }
    true
}

fn matches(dom: &Dom, id: NodeId, sel: &Selector) -> bool {
    let last = sel.parts.len() - 1;
    if !matches_compound(dom, id, &sel.parts[last].1) {
        return false;
    }
    let mut cur = id;
    let mut i = last;
    while i > 0 {
        let comb = sel.parts[i].0.unwrap_or(Combinator::Descendant);
        let want = &sel.parts[i - 1].1;
        match comb {
            Combinator::Child => {
                let p = match dom.parent(cur) {
                    Some(p) => p,
                    None => return false,
                };
                if !matches_compound(dom, p, want) {
                    return false;
                }
                cur = p;
            }
            Combinator::Descendant => {
                let mut anc = dom.parent(cur);
                loop {
                    match anc {
                        None => return false,
                        Some(a) => {
                            if dom.tag_name(a).is_some() && matches_compound(dom, a, want) {
                                cur = a;
                                break;
                            }
                            anc = dom.parent(a);
                        }
                    }
                }
            }
        }
        i -= 1;
    }
    true
}

// ---------------------------------------------------------------------------
// The UA sheet
// ---------------------------------------------------------------------------
//
// Only what the fixtures exercise. Values are the HTML5 suggested rendering
// defaults, which is what Chrome's numbers were produced against.

const UA_SHEET: &str = "
html, body, div, p, h1, h2, h3, h4, h5, h6, ul, ol, li, section, article,
header, footer, nav, main, aside, blockquote, figure, form, table { display: block }
head, style, script, meta, title, link, base { display: none }
span, a, b, strong, i, em, small, code, label, br { display: inline }
body { margin: 8px }
p { margin-top: 1em; margin-bottom: 1em }
h1 { font-size: 2em; font-weight: bold; margin-top: 0.67em; margin-bottom: 0.67em }
h2 { font-size: 1.5em; font-weight: bold; margin-top: 0.83em; margin-bottom: 0.83em }
b, strong { font-weight: bold }
i, em { font-style: italic }
";

// ---------------------------------------------------------------------------
// The harness
// ---------------------------------------------------------------------------

struct Page {
    dom: Dom,
    root: Fragment,
}

fn collect_style_text(dom: &Dom, doc: NodeId) -> String {
    let mut out = String::new();
    for node in dom.descendants(doc) {
        if dom.is_html_element(node, "style") {
            out.push('\n');
            out.push_str(&dom.text_content(node));
        }
    }
    out
}

fn html_element(dom: &Dom, doc: NodeId) -> Option<NodeId> {
    dom.descendants(doc).find(|&n| dom.is_html_element(n, "html"))
}

fn cascade(
    dom: &Dom,
    root: NodeId,
    rules: &[Rule],
    ua_len: usize,
    vw: f32,
    vh: f32,
) -> HashMap<NodeId, Rc<ComputedStyle>> {
    let mut out: HashMap<NodeId, Rc<ComputedStyle>> = HashMap::new();
    let root_style = ComputedStyle::default();
    let mut stack: Vec<(NodeId, Rc<ComputedStyle>)> = vec![(root, Rc::new(root_style))];
    // The root font size is fixed before anything else so `rem` has a base.
    let root_font_size = 16.0;
    while let Some((node, parent_style)) = stack.pop() {
        // Gather matching declarations, ordered by (origin, specificity, source
        // order) — the last write for a property wins.
        let mut hits: Vec<(usize, (u32, u32, u32), usize)> = Vec::new();
        for (i, r) in rules.iter().enumerate() {
            if matches(dom, node, &r.selector) {
                let origin = if i < ua_len { 0 } else { 1 };
                hits.push((origin, r.selector.specificity(), i));
            }
        }
        hits.sort();
        let mut decls: HashMap<String, String> = HashMap::new();
        for (_, _, i) in hits {
            for (k, v) in &rules[i].decls {
                decls.insert(k.clone(), v.clone());
            }
        }
        if let Some(inline) = dom.get_attribute(node, "style").ok().flatten() {
            for (k, v) in parse_declarations(inline) {
                decls.insert(k, v);
            }
        }
        let cx = StyleContext {
            font_size: parent_style.font_size,
            root_font_size,
            viewport_width: vw,
            viewport_height: vh,
        };
        let style = Rc::new(compute(&decls, &parent_style, &cx));
        out.insert(node, style.clone());
        if style.display != Display::None {
            for child in dom.children(node) {
                match dom.data(child) {
                    Ok(NodeData::Element(_)) => stack.push((child, style.clone())),
                    Ok(NodeData::Text(_)) => {
                        out.insert(child, style.clone());
                    }
                    _ => {}
                }
            }
        }
    }
    out
}

fn lay_out(source: &str, fonts: &dyn FontSource, vw: f32, vh: f32) -> Page {
    let (dom, doc) = html::parse(source);
    let root = html_element(&dom, doc).expect("no <html> element");

    let mut rules = parse_stylesheet(UA_SHEET);
    let ua_len = rules.len();
    rules.extend(parse_stylesheet(&collect_style_text(&dom, doc)));

    let styles = cascade(&dom, root, &rules, ua_len, vw, vh);
    let lookup = |n: NodeId| styles.get(&n).cloned();
    let tree = BoxTreeBuilder::new(&dom, &lookup).build(root).expect("no box tree");
    let frag = super::layout_document(&tree, fonts, vw, vh);
    Page { dom, root: frag }
}

/// Chrome measured the corpus in a 1200-wide window; the diagnosis records the
/// content height it reported as 751.
const VW: f32 = 1200.0;
const VH: f32 = 751.0;

fn fixture(name: &str) -> String {
    let path = format!("{}/src/layout/fixtures/{}", env!("CARGO_MANIFEST_DIR"), name);
    std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {}", path, e))
}

impl Page {
    fn all(&self) -> Vec<&Fragment> {
        let mut v = Vec::new();
        self.root.walk(&mut v);
        v
    }

    fn by_class(&self, class: &str) -> Vec<&Fragment> {
        self.all()
            .into_iter()
            .filter(|f| {
                f.kind == FragmentKind::Box
                    && f.dom.is_some_and(|d| classes_of(&self.dom, d).iter().any(|c| c == class))
            })
            .collect()
    }

    fn one(&self, class: &str) -> &Fragment {
        let v = self.by_class(class);
        assert_eq!(v.len(), 1, "expected exactly one .{}, found {}", class, v.len());
        v[0]
    }

    fn nth(&self, class: &str, n: usize) -> &Fragment {
        let v = self.by_class(class);
        assert!(v.len() > n, "expected at least {} .{}, found {}", n + 1, class, v.len());
        v[n]
    }

    fn by_tag(&self, tag: &str) -> Vec<&Fragment> {
        self.all()
            .into_iter()
            .filter(|f| f.kind == FragmentKind::Box && f.label == tag)
            .collect()
    }

    /// The first text fragment whose run is exactly `text`.
    fn text(&self, text: &str) -> &Fragment {
        self.all()
            .into_iter()
            .find(|f| matches!(&f.kind, FragmentKind::Text(t) if t == text))
            .unwrap_or_else(|| panic!("no text fragment {:?}", text))
    }

    fn lines(&self) -> Vec<&Fragment> {
        self.all().into_iter().filter(|f| f.kind == FragmentKind::Line).collect()
    }
}

#[track_caller]
fn close(actual: f32, expected: f32, what: &str) {
    assert!(
        (actual - expected).abs() < 0.06,
        "{}: expected {}, got {}",
        what,
        expected,
        actual
    );
}

// ---------------------------------------------------------------------------
// 01 — margin collapsing between siblings (§8.3.1)
// ---------------------------------------------------------------------------

#[test]
fn repro_01_sibling_margins_collapse() {
    let page = lay_out(&fixture("01-margin-collapse.html"), &StubFont::default(), VW, VH);
    // Chrome: .b at y=60 — 20 + max(40, 20), not 20 + 40 + 20.
    close(page.one("a").border_box.y, 0.0, ".a y");
    close(page.one("b").border_box.y, 60.0, ".b y");
    // Chrome: document height 80.
    close(page.by_tag("body")[0].border_box.height, 80.0, "body height");
}

// ---------------------------------------------------------------------------
// 02 — parent/first-child margin collapsing (§8.3.1)
// ---------------------------------------------------------------------------

#[test]
fn repro_02_parent_child_margins_collapse() {
    let page = lay_out(&fixture("02-parent-child-margin.html"), &StubFont::default(), VW, VH);
    // Chrome: .outer at y=50 with height 20 — the child's margin escapes and
    // moves the parent rather than being trapped inside it.
    let outer = page.one("outer");
    close(outer.border_box.y, 50.0, ".outer y");
    close(outer.border_box.height, 20.0, ".outer height");
    close(page.one("inner").border_box.y, 50.0, ".inner y");
}

// ---------------------------------------------------------------------------
// 03 — percentage heights down a chain of definite containing blocks (§10.5)
// ---------------------------------------------------------------------------

#[test]
fn repro_03_percent_height_chain() {
    let page = lay_out(&fixture("03-percent-height.html"), &StubFont::default(), VW, VH);
    // Chrome at viewport height 751: .shell 751, .half 375.5.
    close(page.one("shell").border_box.height, 751.0, ".shell height");
    close(page.one("half").border_box.height, 375.5, ".half height");
}

/// The other half of §10.5: a percentage height whose containing block is
/// `auto` computes to `auto`, not to zero. This is the same code path as the
/// test above with the definite base removed, and it is the specific line the
/// brief pointed at (`layout.py:1559`, base written as the literal `0`).
#[test]
fn percent_height_against_auto_is_auto_not_zero() {
    let src = "<!doctype html><style>body{margin:0}\
               .outer{background:#eee}\
               .inner{height:50%}\
               .kid{height:30px}</style>\
               <div class=outer><div class=inner><div class=kid></div></div></div>";
    let page = lay_out(src, &StubFont::default(), VW, VH);
    // With `height: 50%` computing to `auto`, `.inner` is as tall as its
    // content. Resolving the percentage against zero would give 0.
    close(page.one("inner").border_box.height, 30.0, ".inner height");
}

// ---------------------------------------------------------------------------
// 04 — the absolute containing block (§10.1 point 3, §10.3.7, §10.6.4)
// ---------------------------------------------------------------------------

#[test]
fn repro_04_absolute_containing_block() {
    let page = lay_out(&fixture("04-abs-containing-block.html"), &StubFont::default(), VW, VH);
    // Chrome: all three resolve against `.rel`, skipping the unpositioned
    // `.mid` — and each keeps its own specified width.
    let abs = page.one("abs");
    close(abs.border_box.x, 100.0, ".abs x");
    close(abs.border_box.y, 0.0, ".abs y");
    close(abs.border_box.width, 50.0, ".abs width");
    close(abs.border_box.height, 50.0, ".abs height");

    let br = page.one("br");
    close(br.border_box.x, 450.0, ".br x");
    close(br.border_box.y, 150.0, ".br y");
    close(br.border_box.width, 50.0, ".br width");

    let pct = page.one("pct");
    close(pct.border_box.x, 300.0, ".pct x");
    close(pct.border_box.y, 100.0, ".pct y");
    close(pct.border_box.width, 20.0, ".pct width");
}

// ---------------------------------------------------------------------------
// 05 — percentage padding resolves against the containing block's width (§8.4)
// ---------------------------------------------------------------------------

#[test]
fn repro_05_percent_padding() {
    let page = lay_out(&fixture("05-percent-padding.html"), &StubFont::default(), VW, VH);
    // Chrome: 10% of the 1200px containing block = 120px on all four sides,
    // giving 640 x 250 and a child at (120, 120).
    let bx = page.one("box");
    close(bx.border_box.width, 640.0, ".box width");
    close(bx.border_box.height, 250.0, ".box height");
    let kid = page.one("kid");
    close(kid.border_box.x, 120.0, ".kid x");
    close(kid.border_box.y, 120.0, ".kid y");
}

// ---------------------------------------------------------------------------
// 06 — `em` resolves against the element's own font-size
// ---------------------------------------------------------------------------

#[test]
fn repro_06_em_padding_uses_own_font_size() {
    let page = lay_out(&fixture("06-em-padding.html"), &StubFont::default(), VW, VH);
    // Chrome: .kid at (32, 32) — 1em of the element's own 32px, not the
    // parent's 16px.
    let kid = page.one("kid");
    close(kid.border_box.x, 32.0, ".kid x");
    close(kid.border_box.y, 32.0, ".kid y");
}

// ---------------------------------------------------------------------------
// 07 — `box-sizing: content-box` is the initial value
// ---------------------------------------------------------------------------

#[test]
fn repro_07_content_box_is_the_default() {
    let page = lay_out(&fixture("07-box-sizing.html"), &StubFont::default(), VW, VH);
    // Chrome, content-box as authored: border box 240, child at 20 spanning 200.
    close(page.one("cb").border_box.width, 240.0, ".cb width");
    let kid = page.one("kid");
    close(kid.border_box.x, 20.0, ".kid x");
    close(kid.border_box.width, 200.0, ".kid width");
}

#[test]
fn box_sizing_border_box_subtracts_padding() {
    // Same fixture with the border-box reset the diagnosis measured separately:
    // Chrome gives border box 200, child at 20 spanning 160.
    let src = "<!doctype html><style>*{box-sizing:border-box}body{margin:0}\
               .cb{width:200px;padding:20px}.kid{height:10px}</style>\
               <div class=cb><div class=kid></div></div>";
    let page = lay_out(src, &StubFont::default(), VW, VH);
    close(page.one("cb").border_box.width, 200.0, ".cb width");
    let kid = page.one("kid");
    close(kid.border_box.x, 20.0, ".kid x");
    close(kid.border_box.width, 160.0, ".kid width");
}

// ---------------------------------------------------------------------------
// 08 — line-height and half-leading (§10.8, §10.8.1)
// ---------------------------------------------------------------------------

#[test]
fn repro_08_line_height() {
    let page = lay_out(&fixture("08-line-height.html"), &StubFont::default(), VW, VH);
    // Chrome: .tight 48 (3 x 16), .loose 144 (3 x 48). Both are pure multiples
    // of the declared line-height, so they hold for any face.
    close(page.one("tight").border_box.height, 48.0, ".tight height");
    close(page.one("loose").border_box.height, 144.0, ".loose height");
    let lines = page.lines();
    assert_eq!(lines.len(), 6, "expected 6 line boxes, got {}", lines.len());
    for l in &lines[..3] {
        close(l.border_box.height, 16.0, "tight line height");
    }
    for l in &lines[3..] {
        close(l.border_box.height, 48.0, "loose line height");
    }
}

/// Half-leading is distributed symmetrically, so the first baseline sits
/// `(line-height − (ascent + descent)) / 2 + ascent` below the top of the line.
/// With the stub face (ascent 0.8em, descent 0.2em) and `line-height: 3` on a
/// 16px font that is (48 − 16)/2 + 12.8 = 28.8.
#[test]
fn half_leading_is_split_evenly() {
    let page = lay_out(&fixture("08-line-height.html"), &StubFont::default(), VW, VH);
    let lines = page.lines();
    close(lines[3].baseline.unwrap(), 28.8, "loose first baseline");
    close(lines[0].baseline.unwrap(), 12.8, "tight first baseline");
}

// ---------------------------------------------------------------------------
// 09 — the white-space model invents no spaces (CSS Text §4.1.1)
// ---------------------------------------------------------------------------

#[test]
fn repro_09_no_invented_space_at_element_boundaries() {
    // Chrome measured `italic` starting at x = 30.24 in
    // `<p><b>bold</b><i>italic</i></p>` — exactly the advance of the bold
    // "bold", because the markup has no white space at that boundary.
    // That advance is injected here; the assertion is that nothing is added to
    // it. `layout.py` puts the run at 34.24, a full space too far right.
    let font = StubFont::default().with_advance("bold", 16.0, 30.24);
    let page = lay_out(&fixture("09-inline-whitespace.html"), &font, VW, VH);
    close(page.text("italic").border_box.x, 30.24, "`italic` x");
}

#[test]
fn white_space_collapsing_keeps_boundary_information() {
    // Phase I of CSS Text §4.1.1, checked directly: a run of spaces becomes
    // one, a boundary with no space stays without one.
    let mut last = true;
    let a = super::inline::process_whitespace(
        "  A  ",
        super::style::WhiteSpace::Normal,
        &mut last,
    );
    assert_eq!(a, "A ");
    let b = super::inline::process_whitespace(
        "web",
        super::style::WhiteSpace::Normal,
        &mut last,
    );
    assert_eq!(b, "web");
    let c = super::inline::process_whitespace(
        ", often",
        super::style::WhiteSpace::Normal,
        &mut last,
    );
    assert_eq!(c, ", often");
}

// ---------------------------------------------------------------------------
// 10 — border participates in the box model
// ---------------------------------------------------------------------------

#[test]
fn repro_10_border_width() {
    let page = lay_out(&fixture("10-border-width.html"), &StubFont::default(), VW, VH);
    // Chrome: .b border box 440 wide, child inset by the border to (20, 20)
    // spanning 400 — not painted over the border.
    close(page.one("b").border_box.width, 440.0, ".b width");
    let kid = page.one("kid");
    close(kid.border_box.x, 20.0, ".kid x");
    close(kid.border_box.width, 400.0, ".kid width");
    close(kid.border_box.y, 20.0, ".kid y");
}

// ---------------------------------------------------------------------------
// 11 — float containment is a property of BFC roots only (§10.6.3)
// ---------------------------------------------------------------------------

#[test]
fn repro_11_plain_block_does_not_contain_its_floats() {
    let page = lay_out(&fixture("11-float-bfc.html"), &StubFont::default(), VW, VH);
    // Chrome: .plain has height 0 and .after starts at y=0, sliding under the
    // overhanging float. `layout.py` grows every block to contain its floats.
    close(page.one("plain").border_box.height, 0.0, ".plain height");
    close(page.one("after").border_box.y, 0.0, ".after y");
    let f = page.one("f");
    close(f.border_box.y, 0.0, ".f y");
    close(f.border_box.height, 100.0, ".f height");
}

#[test]
fn a_bfc_root_does_contain_its_floats() {
    // The other side of §10.6.3, from the same fixture with `overflow: hidden`
    // added: the containing block now establishes a BFC, so its auto height
    // grows to the float's bottom. This is standard CSS 2.1, not a measured
    // Chrome value, and it is the behaviour `layout.py` applies unconditionally.
    let src = "<!doctype html><style>body{margin:0}\
               .plain{overflow:hidden}.f{float:left;width:100px;height:100px}\
               .after{height:20px}</style>\
               <div class=plain><div class=f></div></div><div class=after></div>";
    let page = lay_out(src, &StubFont::default(), VW, VH);
    close(page.one("plain").border_box.height, 100.0, ".plain height");
    close(page.one("after").border_box.y, 100.0, ".after y");
}

// ---------------------------------------------------------------------------
// 12 — escaped identifiers in selectors
// ---------------------------------------------------------------------------

#[test]
fn repro_12_escaped_class_selectors() {
    // Not a layout defect at all: `cssparser.py:391` drops `.md\:flex` and
    // `.w-\[200px\]` before layout ever sees them, which is 67.5% of Vimeo's
    // stylesheet. Included so the corpus records where the failure actually
    // lives — the expectation is what the declarations plainly say.
    let page = lay_out(&fixture("12-escaped-selector.html"), &StubFont::default(), VW, VH);
    let esc = page.by_class("md:flex");
    assert_eq!(esc.len(), 1, "escaped class did not match");
    close(esc[0].border_box.height, 40.0, "escaped height");
    close(esc[0].border_box.width, 200.0, "escaped width");
    close(page.one("plain").border_box.height, 40.0, ".plain height");
}

// ---------------------------------------------------------------------------
// 13 — `position: fixed` resolves against the viewport (§10.1 point 4)
// ---------------------------------------------------------------------------

#[test]
fn repro_13_fixed_against_viewport() {
    let page = lay_out(&fixture("13-fixed-position.html"), &StubFont::default(), VW, VH);
    let bar = page.one("bar");
    close(bar.border_box.x, 0.0, ".bar x");
    close(bar.border_box.y, 0.0, ".bar y");
    // `width: 100%` against the viewport, not against the 2000px-tall spacer's
    // content box (which happens to be the same width here) — the test that
    // discriminates is the height and origin above plus the width below.
    close(bar.border_box.width, VW, ".bar width");
    close(bar.border_box.height, 50.0, ".bar height");
}

// ---------------------------------------------------------------------------
// 14 / 16 — `text-align: center` measures against the content box (§16.2)
// ---------------------------------------------------------------------------
//
// These two are a matched pair. 14 is the diagnosis's deliberate false
// negative: `layout.py` centres against the border box *and* treats the box as
// border-box, and the two errors cancel exactly. 16 adds
// `*{box-sizing:border-box}` so they no longer cancel, and the error shows up
// as a clean +100 = padding-right / 2.

/// Chrome measured the centred text at x = 68.89 in the border-box variant,
/// which pins the advance of "CENTER" at 16px to 200 − 2 x 68.89 = 62.22.
const CENTER_ADVANCE: f32 = 62.22;

#[test]
fn repro_14_center_content_box() {
    let font = StubFont::default().with_advance("CENTER", 16.0, CENTER_ADVANCE);
    let page = lay_out(&fixture("14-text-align-center.html"), &font, VW, VH);
    // Content box is 400 wide (content-box sizing), so (400 − 62.22)/2.
    close(page.text("CENTER").border_box.x, 168.89, "centred text x");
}

#[test]
fn repro_16_center_border_box() {
    let font = StubFont::default().with_advance("CENTER", 16.0, CENTER_ADVANCE);
    let page = lay_out(&fixture("16-center-borderbox.html"), &font, VW, VH);
    // Content box is 400 − 200 = 200 wide, so (200 − 62.22)/2 = 68.89.
    close(page.text("CENTER").border_box.x, 68.89, "centred text x");
}

// ---------------------------------------------------------------------------
// 15 — nested percentage width (the axis the brief says already works)
// ---------------------------------------------------------------------------

#[test]
fn repro_15_nested_percent_width() {
    let page = lay_out(&fixture("15-pct-width-nested.html"), &StubFont::default(), VW, VH);
    // Chrome: .b is 50% of .a's 600px content box, at x = 50 (inside .a's
    // padding). A guard that the inline axis stays correct, not a fix.
    let a = page.one("a");
    close(a.border_box.width, 700.0, ".a width");
    let b = page.one("b");
    close(b.border_box.x, 50.0, ".b x");
    close(b.border_box.width, 300.0, ".b width");
}

// ---------------------------------------------------------------------------
// 18 — inline-block sizing and baseline (§10.8.1)
// ---------------------------------------------------------------------------

#[test]
fn repro_18_inline_block_size_and_baseline() {
    let page = lay_out(&fixture("18-inline-block-size.html"), &StubFont::default(), VW, VH);
    // Chrome: wrapper 60 tall, chips at (0,0) and (150,0), each 150 x 60.
    // `layout.py` makes the wrapper 21.25 and paints the labels at y = −21.5,
    // above the top of the document.
    let a = page.nth("chip", 0);
    let b = page.nth("chip", 1);
    close(a.border_box.x, 0.0, "chip A x");
    close(a.border_box.y, 0.0, "chip A y");
    close(a.border_box.width, 150.0, "chip A width");
    close(a.border_box.height, 60.0, "chip A height");
    close(b.border_box.x, 150.0, "chip B x");
    close(b.border_box.y, 0.0, "chip B y");
    let wrapper = page.by_tag("div")[0];
    close(wrapper.border_box.height, 60.0, "wrapper height");
}

// ---------------------------------------------------------------------------
// Intrinsic sizing (§ CSS Sizing 3) — the input shrink-to-fit needs
// ---------------------------------------------------------------------------
//
// Fixture 20 is the recorded case: Chrome makes each `.cell` 73.10 wide, being
// 23.1 of text plus 40 of padding plus 10 of border, and `layout.py` makes it
// 27.10 — the text advance alone. The flex formatting context that would place
// those cells does not exist yet, so the number the flex pass *consumes* is
// tested directly instead, through a float, which is shrink-to-fit sized by the
// same §10.3.5 rule.

#[test]
fn intrinsic_width_includes_padding_and_border() {
    // Same box as `20-flex-center-natural.html`'s `.cell`, floated so that
    // §10.3.5 applies. Chrome's 73.10 = 23.10 + 2x20 padding + 2x5 border; the
    // 23.10 is injected, the other 50 is what this engine must contribute.
    let font = StubFont::default().with_advance("one", 16.0, 23.10);
    let src = "<!doctype html><style>body{margin:0}\
               .cell{float:left;padding:20px;border:5px solid #000}</style>\
               <div class=cell>one</div>";
    let page = lay_out(src, &font, VW, VH);
    close(page.one("cell").border_box.width, 73.10, ".cell border-box width");
}

#[test]
fn intrinsic_width_recurses_through_descendants() {
    // The recursive half of the same defect: a float's shrink-to-fit width has
    // to see the padding of a *descendant*, not just its own. 23.10 injected,
    // 60 of padding across two levels contributed by the engine.
    let font = StubFont::default().with_advance("one", 16.0, 23.10);
    let src = "<!doctype html><style>body{margin:0}\
               .outer{float:left;padding:10px}\
               .inner{padding:20px}</style>\
               <div class=outer><div class=inner>one</div></div>";
    let page = lay_out(src, &font, VW, VH);
    close(page.one("outer").border_box.width, 83.10, ".outer width");
    close(page.one("inner").border_box.width, 63.10, ".inner width");
}

#[test]
fn shrink_to_fit_clamps_to_the_available_width() {
    // §10.3.5: min(max(min-content, available), max-content). A float wider
    // than its containing block is capped at the containing block, and one
    // narrower than it keeps its max-content width.
    use super::block::shrink_to_fit_width;
    close(shrink_to_fit_width(30.0, 500.0, 200.0, 0.0), 200.0, "capped");
    close(shrink_to_fit_width(30.0, 100.0, 200.0, 0.0), 100.0, "max-content");
    close(shrink_to_fit_width(300.0, 500.0, 200.0, 0.0), 300.0, "min-content floor");
}

// ---------------------------------------------------------------------------
// Margin collapsing, the cases the fixtures do not cover
// ---------------------------------------------------------------------------

#[test]
fn margins_do_not_collapse_across_a_bfc_boundary() {
    // §8.3.1: "Margins of elements that establish new block formatting contexts
    // do not collapse with their in-flow children." This is the whole mechanism
    // behind the `overflow: hidden` folk remedy.
    let src = "<!doctype html><style>body{margin:0}\
               .outer{overflow:hidden}.inner{margin-top:50px;height:20px}</style>\
               <div class=outer><div class=inner></div></div>";
    let page = lay_out(src, &StubFont::default(), VW, VH);
    let outer = page.one("outer");
    close(outer.border_box.y, 0.0, ".outer y");
    close(outer.border_box.height, 70.0, ".outer height");
    close(page.one("inner").border_box.y, 50.0, ".inner y");
}

#[test]
fn a_border_stops_the_parent_child_collapse() {
    // §8.3.1 again: a top border separates the two margins, so the child's
    // margin stays inside. Fixture 02 is the same markup without the border.
    let src = "<!doctype html><style>body{margin:0}\
               .outer{border-top:1px solid #000}\
               .inner{margin-top:50px;height:20px}</style>\
               <div class=outer><div class=inner></div></div>";
    let page = lay_out(src, &StubFont::default(), VW, VH);
    let outer = page.one("outer");
    close(outer.border_box.y, 0.0, ".outer y");
    close(outer.border_box.height, 71.0, ".outer height");
    close(page.one("inner").border_box.y, 51.0, ".inner y");
}

#[test]
fn negative_margins_take_the_minimum_of_the_negatives() {
    // §8.3.1: "the maximum of the absolute values of the negative adjoining
    // margins is deducted from the maximum of the positive adjoining margins."
    // 20 + max(40, ...) + min(..., −15) = 20 + 40 − 15 = 45.
    let src = "<!doctype html><style>body{margin:0}\
               .a{height:20px;margin-bottom:40px}\
               .b{height:20px;margin-top:-15px}</style>\
               <div class=a></div><div class=b></div>";
    let page = lay_out(src, &StubFont::default(), VW, VH);
    close(page.one("b").border_box.y, 45.0, ".b y");
}

// ---------------------------------------------------------------------------
// The inline axis the brief says already works — guards, not fixes
// ---------------------------------------------------------------------------

#[test]
fn auto_margins_centre_a_block() {
    // §10.3.3: with `width` definite and both margins `auto`, the remainder is
    // split equally. The brief is explicit that this already works in
    // `layout.py`; the guard is here so the rewrite does not lose it.
    let src = "<!doctype html><style>body{margin:0}\
               .c{width:400px;margin-left:auto;margin-right:auto;height:10px}</style>\
               <div class=c></div>";
    let page = lay_out(src, &StubFont::default(), VW, VH);
    close(page.one("c").border_box.x, 400.0, ".c x");
}

#[test]
fn max_width_re_solves_the_auto_margins() {
    // §10.4: when the used width violates `max-width`, the whole inline
    // equation is solved again — so a clamped centred box stays centred.
    let src = "<!doctype html><style>body{margin:0}\
               .c{max-width:400px;margin-left:auto;margin-right:auto;height:10px}</style>\
               <div class=c></div>";
    let page = lay_out(src, &StubFont::default(), VW, VH);
    let c = page.one("c");
    close(c.border_box.width, 400.0, ".c width");
    close(c.border_box.x, 400.0, ".c x");
}

// ---------------------------------------------------------------------------
// Containing block plumbing
// ---------------------------------------------------------------------------

#[test]
fn containing_block_chain_distinguishes_the_three_schemes() {
    use super::style::Position;
    use super::{ContainingBlock, ContainingBlockChain};
    let viewport = ContainingBlock::viewport(1200.0, 751.0);
    let chain = ContainingBlockChain::root(viewport);
    let content = ContainingBlock::at(10.0, 20.0, 300.0, Some(100.0));
    let padding = ContainingBlock::at(5.0, 15.0, 310.0, Some(110.0));
    let mut positioned = ComputedStyle::default();
    positioned.position = Position::Relative;
    let inner = chain.descend(&positioned, content, padding);
    assert_eq!(inner.for_position(Position::Static).width, 300.0);
    // §10.1 point 3: the *padding* box of the positioned ancestor.
    assert_eq!(inner.for_position(Position::Absolute).width, 310.0);
    assert_eq!(inner.for_position(Position::Fixed).width, 1200.0);

    // An unpositioned box is transparent to absolutely positioned descendants.
    let plain = ComputedStyle::default();
    let deeper = inner.descend(&plain, content, padding);
    assert_eq!(deeper.for_position(Position::Absolute).width, 310.0);
}

#[test]
fn a_percentage_height_needs_a_definite_base() {
    use super::style::{LengthPercentage, Size};
    // The typed statement of §10.5: `Option<f32>` means "no base", and a
    // percentage against it is `None` — auto — rather than zero.
    let pct = Size::LengthPercentage(LengthPercentage::Percent(50.0));
    assert_eq!(pct.resolve(Some(200.0)), Some(100.0));
    assert_eq!(pct.resolve(None), None);
    // A real zero-height containing block is a different thing entirely.
    assert_eq!(pct.resolve(Some(0.0)), Some(0.0));
}

// ---------------------------------------------------------------------------
// 17, 19, 20 — the three fixtures this engine cannot yet answer
// ---------------------------------------------------------------------------
//
// Flex and grid formatting contexts are step 6 of the priority list and are not
// implemented: `layout_children` currently routes them to the block arm. These
// three tests carry the recorded Chrome geometry so the corpus has a real
// denominator and the next person does not have to re-measure. They are
// `#[ignore]`d, not deleted, and not weakened to match what the engine
// produces — `cargo test --lib -- --ignored` is the progress report.

#[test]
#[ignore = "grid formatting context not implemented"]
fn repro_17_grid_repeat() {
    let page = lay_out(&fixture("17-grid-repeat.html"), &StubFont::default(), VW, VH);
    // Chrome: every container resolves to equal 1fr tracks. `.i`, the literal
    // `1fr 1fr 1fr` control, is the one `layout.py` already gets right — the
    // defect there is the `repeat()`/`minmax()` parser, not the track algorithm.
    for (class, count, width) in [("g", 3usize, 200.0f32), ("h", 2, 300.0), ("i", 3, 200.0)] {
        let kids: Vec<&Fragment> = page
            .by_tag("div")
            .into_iter()
            .filter(|f| {
                f.dom.is_some_and(|d| {
                    page.dom
                        .parent(d)
                        .is_some_and(|p| classes_of(&page.dom, p).iter().any(|c| c == class))
                })
            })
            .collect();
        assert_eq!(kids.len(), count, ".{} track count", class);
        for k in kids {
            close(k.border_box.width, width, "track width");
        }
    }
}

#[test]
#[ignore = "flex formatting context not implemented"]
fn repro_19_inline_flex_is_inline_level() {
    // Chrome: the badge sits on the heading's own line at x = 84.41, is 117.56
    // wide, and leaves the `h1` 39px tall. `layout.py` drops it onto its own
    // line at x = 0, 1184 wide, inflating the heading to 71.
    let page = lay_out(&fixture("19-inline-flex.html"), &StubFont::default(), VW, VH);
    let btn = page.one("btn");
    close(btn.border_box.x, 84.41, ".btn x");
    close(btn.border_box.y, 0.0, ".btn y");
    close(btn.border_box.width, 117.56, ".btn width");
    close(page.by_tag("h1")[0].border_box.height, 39.0, "h1 height");
}

#[test]
#[ignore = "flex formatting context not implemented"]
fn repro_20_flex_centres_naturally_sized_items() {
    // Chrome: cells 73.10 and 74.00 wide, centred in the 600px row, so the
    // first starts at 226.45 and the second at 299.55. The advances are
    // injected; the padding and border inside those widths are the engine's
    // job, and `intrinsic_width_includes_padding_and_border` covers that half
    // already.
    let font = StubFont::default()
        .with_advance("one", 16.0, 23.10)
        .with_advance("two", 16.0, 24.00);
    let page = lay_out(&fixture("20-flex-center-natural.html"), &font, VW, VH);
    let a = page.nth("cell", 0);
    let b = page.nth("cell", 1);
    close(a.border_box.x, 226.45, "cell 1 x");
    close(a.border_box.width, 73.10, "cell 1 width");
    close(b.border_box.x, 299.55, "cell 2 x");
}

#[test]
fn anonymous_block_boxes_wrap_inline_runs() {
    // §9.2.1.1: a block container with a block-level child must have only
    // block-level children, so the stray text is wrapped.
    let src = "<!doctype html><style>body{margin:0}</style>\
               <div>text<div class=blk>block</div>more</div>";
    let page = lay_out(src, &StubFont::default(), VW, VH);
    // Two anonymous blocks, one either side of the real one.
    let anon: Vec<_> = page.all().into_iter().filter(|f| f.label == "#anonymous").collect();
    assert_eq!(anon.len(), 2, "expected 2 anonymous blocks, got {}", anon.len());
    assert!(anon[0].border_box.y < page.one("blk").border_box.y);
    assert!(anon[1].border_box.y >= page.one("blk").border_box.bottom() - 0.01);
}
