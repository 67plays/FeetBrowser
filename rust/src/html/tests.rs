//! Tests for the HTML parser.
//!
//! Two layers:
//!
//! 1. Named unit tests for behaviours we specifically set out to fix, so that
//!    a regression names itself instead of showing up as "3 more fixture
//!    failures".
//! 2. [`html5lib_tree_construction`], which runs the vendored html5lib-tests
//!    fixtures and prints a per-file and total score.
//!
//! The fixtures live in `rust/tests/html5lib-tests/`. They are data, not code
//! — see the LICENSE file beside them.

use super::*;
use crate::domtree::Namespace;

fn dump(source: &str) -> String {
    let (dom, root) = parse(source);
    serialize_for_tests(&dom, root)
}

fn dump_fragment(source: &str, context: &str) -> String {
    let (dom, root) = parse_fragment_html(source, context);
    serialize_for_tests(&dom, root)
}

// ---------------------------------------------------------------------------
// The three cases the old Python parser could not express
// ---------------------------------------------------------------------------

/// A `<tr>` with no `<tbody>` around it still gets one. The old parser
/// appended `<tr>` straight to `<table>`, so every CSS selector and DOM walk
/// written against `table > tbody > tr` missed.
#[test]
fn implicit_tbody_is_inserted_between_table_and_row() {
    assert_eq!(
        dump("<table><tr><td>c"),
        "\
| <html>
|   <head>
|   <body>
|     <table>
|       <tbody>
|         <tr>
|           <td>
|             \"c\"
"
    );
}

/// Content that cannot live in a table is *foster parented*: it moves out of
/// the table and lands immediately before it. An append-only parser has no
/// way to express this, because the table is already in the tree.
#[test]
fn foster_parenting_moves_stray_content_out_of_a_table() {
    assert_eq!(
        dump("<table><span>x</span>"),
        "\
| <html>
|   <head>
|   <body>
|     <span>
|       \"x\"
|     <table>
"
    );
}

/// The adoption agency algorithm. `</b>` arrives while `<i>` is open, so the
/// `<i>` has to be split in two: the "3" belongs to a *second* `<i>` that is
/// a sibling of the `<b>`, and stays italic.
#[test]
fn adoption_agency_splits_misnested_formatting_elements() {
    assert_eq!(
        dump("<b>1<i>2</b>3</i>"),
        "\
| <html>
|   <head>
|   <body>
|     <b>
|       \"1\"
|       <i>
|         \"2\"
|     <i>
|       \"3\"
"
    );
}

// ---------------------------------------------------------------------------
// A few more that exercise the machinery the three above depend on
// ---------------------------------------------------------------------------

/// `</p>` pops the `<b>` off the stack but leaves it *active*, so the next
/// character gets a freshly created `<b>` around it. Bold survives the block
/// boundary, which is what every browser does and what a stack-only parser
/// cannot do.
#[test]
fn formatting_elements_are_reconstructed_across_a_block() {
    assert_eq!(
        dump("<p><b>a</p>b"),
        "\
| <html>
|   <head>
|   <body>
|     <p>
|       <b>
|         \"a\"
|     <b>
|       \"b\"
"
    );
}

#[test]
fn a_second_body_tag_merges_its_attributes_into_the_first() {
    assert_eq!(
        dump("<body id=a><body class=b>"),
        "\
| <html>
|   <head>
|   <body>
|     class=\"b\"
|     id=\"a\"
"
    );
}

#[test]
fn doctype_only_ever_parents_to_the_document() {
    // A doctype seen after the document element is dropped, not appended to
    // whatever happens to be open. Phase 1 left this to the tree builder.
    let out = dump("<!DOCTYPE html><body><!DOCTYPE html>x");
    assert_eq!(
        out,
        "\
| <!DOCTYPE html>
| <html>
|   <head>
|   <body>
|     \"x\"
"
    );
}

#[test]
fn fragment_parsing_uses_the_context_element() {
    // Bare cells only survive if the context says we are inside a row.
    assert_eq!(
        dump_fragment("<td>a</td>", "tr"),
        "\
| <td>
|   \"a\"
"
    );
}

#[test]
fn svg_keeps_its_camel_case_and_namespaced_attributes() {
    assert_eq!(
        dump("<svg><clipPath xlink:href=\"#a\"></clipPath></svg>"),
        "\
| <html>
|   <head>
|   <body>
|     <svg svg>
|       <svg clipPath>
|         xlink href=\"#a\"
"
    );
}

#[test]
fn template_contents_go_into_a_fragment() {
    assert_eq!(
        dump("<template><div>x</div></template>"),
        "\
| <html>
|   <head>
|     <template>
|       content
|         <div>
|           \"x\"
|   <body>
"
    );
}

/// `<select>` is an ordinary element: it has no insertion mode of its own, so
/// a `<div>` inside one is kept rather than dropped, and the formatting
/// elements around it reconstruct normally.
#[test]
fn select_is_parsed_as_an_ordinary_element() {
    assert_eq!(
        dump("<select><div><i></div><option>option"),
        "\
| <html>
|   <head>
|   <body>
|     <select>
|       <div>
|         <i>
|       <i>
|         <option>
|           \"option\"
"
    );
}

/// ...but it still cannot nest, and an `<hr>` inside one closes any open
/// option and optgroup, because it separates their groups.
#[test]
fn a_nested_select_closes_the_outer_one_and_hr_closes_option_groups() {
    assert_eq!(
        dump("<select><optgroup><option><hr><select>x"),
        "\
| <html>
|   <head>
|   <body>
|     <select>
|       <optgroup>
|         <option>
|       <hr>
|     \"x\"
"
    );
}

#[test]
fn noahs_ark_caps_identical_formatting_elements_at_three() {
    // Four identical <b>s go into the tree, but the list of active formatting
    // elements only keeps three of them, so `</p>` + "y" reconstructs three,
    // not four. Without the clause this grows without bound.
    let out = dump("<p><b><b><b><b>x</p>y");
    assert_eq!(out.matches("<b>").count(), 4 + 3, "got:\n{out}");
}

// ---------------------------------------------------------------------------
// The html5lib-tests tree-construction suite
// ---------------------------------------------------------------------------

/// One `#data` block from a `.dat` file.
struct Fixture {
    data: String,
    document: String,
    fragment_context: Option<String>,
    /// `None` means "run in both scripting modes", per the format's README.
    scripting: Option<bool>,
}

/// Split a `.dat` file into fixtures.
///
/// The format is line-oriented: headings are lines that consist of `#`
/// followed by a known section name, and everything else belongs to the
/// current section. A new `#data` heading starts a new fixture. See
/// `rust/tests/html5lib-tests/tree-construction/README.md`.
fn parse_dat(text: &str) -> Vec<Fixture> {
    const HEADINGS: &[&str] = &[
        "#data",
        "#errors",
        "#new-errors",
        "#document-fragment",
        "#document",
        "#script-off",
        "#script-on",
    ];

    let mut fixtures = Vec::new();
    let mut section = "";
    let mut buf: Vec<&str> = Vec::new();
    let mut cur: Option<(String, Option<String>, Option<bool>, String)> = None;

    // (data, fragment, scripting, document)
    fn flush(
        section: &str,
        buf: &mut Vec<&str>,
        cur: &mut Option<(String, Option<String>, Option<bool>, String)>,
    ) {
        let value = buf.join("\n");
        buf.clear();
        let Some(entry) = cur.as_mut() else { return };
        match section {
            "#data" => entry.0 = value,
            "#document-fragment" => entry.1 = Some(value.trim().to_string()),
            "#document" => {
                // The blank line before the next fixture becomes a trailing
                // newline once joined; the real content keeps exactly one.
                let mut v = value;
                while v.ends_with('\n') {
                    v.pop();
                }
                if !v.is_empty() {
                    v.push('\n');
                }
                entry.3 = v;
            }
            _ => {}
        }
    }

    for line in text.split('\n') {
        if HEADINGS.contains(&line) {
            flush(section, &mut buf, &mut cur);
            if line == "#data" {
                if let Some((data, frag, script, doc)) = cur.take() {
                    fixtures.push(Fixture {
                        data,
                        document: doc,
                        fragment_context: frag,
                        scripting: script,
                    });
                }
                cur = Some((String::new(), None, None, String::new()));
            } else if line == "#script-off" {
                if let Some(e) = cur.as_mut() {
                    e.2 = Some(false);
                }
            } else if line == "#script-on" {
                if let Some(e) = cur.as_mut() {
                    e.2 = Some(true);
                }
            }
            section = line;
        } else {
            buf.push(line);
        }
    }
    flush(section, &mut buf, &mut cur);
    if let Some((data, frag, script, doc)) = cur.take() {
        fixtures.push(Fixture {
            data,
            document: doc,
            fragment_context: frag,
            scripting: script,
        });
    }
    fixtures
}

fn split_context(ctx: &str) -> (Namespace, &str) {
    match ctx.split_once(' ') {
        Some(("svg", name)) => (Namespace::Svg, name),
        Some(("math", name)) => (Namespace::MathMl, name),
        _ => (Namespace::Html, ctx),
    }
}

fn run_fixture(f: &Fixture, scripting: bool) -> String {
    match &f.fragment_context {
        Some(ctx) => {
            let (ns, name) = split_context(ctx);
            let (dom, root) = parse_fragment(&f.data, ns, name, scripting);
            serialize_for_tests(&dom, root)
        }
        None => {
            let (dom, root) = treebuilder::parse_document(&f.data, scripting);
            serialize_for_tests(&dom, root)
        }
    }
}

/// Run the whole vendored tree-construction suite and print the score.
///
/// This deliberately does not assert 100%: the suite covers behaviour we have
/// no intention of implementing here (scripted `document.write`, for one), and
/// a hard failure would hide the number, which is the actually useful output.
/// It does assert a floor, so that a regression is a test failure rather than
/// a smaller number nobody reads.
#[test]
fn html5lib_tree_construction() {
    let dir = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/html5lib-tests/tree-construction"
    );
    let mut files: Vec<std::path::PathBuf> = std::fs::read_dir(dir)
        .unwrap_or_else(|e| panic!("cannot read fixture directory {dir}: {e}"))
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().map(|x| x == "dat").unwrap_or(false))
        .collect();
    files.sort();
    assert!(!files.is_empty(), "no .dat fixtures found in {dir}");

    let mut total_pass = 0usize;
    let mut total = 0usize;
    let mut report = String::from("\nhtml5lib-tests tree-construction\n");

    for path in &files {
        let text = std::fs::read_to_string(path).expect("fixture is valid UTF-8");
        let fixtures = parse_dat(&text);
        let mut pass = 0usize;
        let mut count = 0usize;
        let mut failures = String::new();
        // Set HTML5LIB_VERBOSE to a substring of a fixture file name to have
        // that file's failures printed in full.
        let verbose = std::env::var("HTML5LIB_VERBOSE")
            .ok()
            .filter(|v| path.to_string_lossy().contains(v.as_str()))
            .is_some();

        for f in &fixtures {
            let modes: Vec<bool> = match f.scripting {
                Some(s) => vec![s],
                None => vec![false, true],
            };
            for scripting in modes {
                count += 1;
                let got = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    run_fixture(f, scripting)
                }))
                .unwrap_or_else(|_| "<panicked>\n".to_string());
                if got == f.document {
                    pass += 1;
                } else if verbose {
                    failures.push_str(&format!(
                        "  input:    {:?} (scripting={})\n  expected:\n{}  got:\n{}\n",
                        f.data, scripting, f.document, got
                    ));
                }
            }
        }

        total_pass += pass;
        total += count;
        let name = path.file_name().unwrap().to_string_lossy();
        report.push_str(&format!("  {:>5}/{:<5}  {}\n", pass, count, name));
        report.push_str(&failures);
    }

    report.push_str(&format!(
        "  ----- total: {}/{} ({:.1}%)\n",
        total_pass,
        total,
        100.0 * total_pass as f64 / total as f64
    ));
    println!("{report}");

    // Regression floor, not a target. Raise it when the number goes up.
    assert!(
        total_pass >= FLOOR,
        "html5lib pass count regressed: {total_pass} < {FLOOR}{report}"
    );
}

/// The pass count at the time this was last measured. See the comment in
/// [`html5lib_tree_construction`].
const FLOOR: usize = 3814;
