"""Offline tests for the JS interpreter (jsengine) and its browser
integration (script execution, console, click handlers).
"""
import sys, os, tkinter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser.net import URL
from feetbrowser.browser import Tab
from feetbrowser.layout import DrawText
from feetbrowser.jsengine import Interpreter


def eq(a, b, msg=""):
    assert a == b, f"{msg}: {a!r} != {b!r}"


def _drawtexts(tab):
    return [c for c in tab.display_list if isinstance(c, DrawText)]


def _texts(tab):
    return [c.text for c in _drawtexts(tab)]


def test_js_engine_arithmetic_and_functions():
    interp = Interpreter()
    interp.run("""
        var a = 1 + 2 * 3;
        var s = "foo" + "bar";
        function makeCounter() {
            var n = 0;
            return function () { n += 1; return n; };
        }
        var counter = makeCounter();
        counter();
        var inc = counter();
        var arr = [1, 2, 3];
        arr.push(4);
        var len = arr.length;
        var obj = { key: "value" };
        var k = obj.key;
        var total = 0;
        for (var i = 0; i < 5; i++) {
            if (i === 2) { continue; }
            total += i;
        }
        var t = 3 > 2 ? "yes" : "no";
        var loose = (1 == "1");
        var strict = (1 === "1");
        var tarr = typeof arr;
        var tfun = typeof counter;
        var tobj = typeof obj;
        var tstr = typeof s;
        console.log("ok");
    """)
    g = interp.globals
    eq(g["a"], 7, "two-level expression precedence")
    eq(g["s"], "foobar", "string concatenation")
    eq(g["inc"], 2, "closure keeps outer state across calls")
    eq(g["len"], 4, "array push updates length")
    eq(g["k"], "value", "object property read")
    eq(g["total"], 8, "for loop with continue skips i==2")
    eq(g["t"], "yes", "ternary picks the true branch")
    assert g["loose"] is True, "1 == '1' loose equality"
    assert g["strict"] is False, "1 === '1' strict equality"
    eq(g["tarr"], "object", "typeof array")
    eq(g["tfun"], "function", "typeof closure")
    eq(g["tobj"], "object", "typeof object")
    eq(g["tstr"], "string", "typeof string")


def test_js_builtin_conversions():
    interp = Interpreter()
    interp.run("""
        var a = String(1 + 2);
        var b = String("x" + 4);
        var c = Number("12.5");
        var d = Number(true);
        var e = Boolean(0);
        var f = Boolean("hi");
        var g = parseInt("42px");
        var h = parseFloat("3.14abc");
        var i = parseInt("0x1F");
        console.log("ok");
    """)
    g = interp.globals
    eq(g["a"], "3", "String numbers the result")
    eq(g["b"], "x4", "String concatenates number")
    eq(g["c"], 12.5, "Number parses decimal")
    eq(g["d"], 1, "Number(true) is 1")
    assert g["e"] is False, "Boolean(0) is false"
    assert g["f"] is True, "Boolean('hi') is true"
    eq(g["g"], 42, "parseInt stops at non-digit")
    eq(g["h"], 3.14, "parseFloat reads numeric prefix")
    eq(g["i"], 31, "parseInt handles 0x hex")
    eq(interp.logs, ["ok"], "console.log captured as one entry")


def test_js_script_modifies_page():
    tab = _make_tab(
        '<p id="x">old</p>'
        '<script>document.getElementById("x").textContent = "new";'
        '</script>')
    texts = _texts(tab)
    assert "new" in texts, f"script-set text not rendered: {texts}"
    assert "old" not in texts, f"original text lingered: {texts}"


def test_js_console_logged():
    tab = _make_tab('<script>console.log("hello", 1+1);</script>')
    eq(tab.js_logs, ["hello 2"], "console.log args joined with a space")


def test_js_click_handler_changes_page():
    tab = _make_tab(
        '<p id="c">before</p>'
        '<script>document.getElementById("c").addEventListener("click", '
        'function(){ document.getElementById("c").textContent = "after"; });'
        '</script>')
    before = [c for c in _drawtexts(tab) if c.text == "before"]
    assert before, "no 'before' text node rendered"
    c = before[0]
    tab.click(c.left + 2, c.top + 2)
    texts = _texts(tab)
    assert "after" in texts, f"click handler did not update the page: {texts}"


def test_js_bad_script_does_not_crash():
    tab = _make_tab(
        '<script>var = = =</script>'
        '<p>still here</p>')
    texts = _texts(tab)
    assert "still" in texts and "here" in texts, \
        f"page must still render around a bad script: {texts}"
    assert tab.js_logs, "a failed script must append an error entry"
    assert any("JS error" in s for s in tab.js_logs), \
        f"no 'JS error' in logs: {tab.js_logs!r}"


def test_js_inner_html_set():
    tab = _make_tab(
        '<div id="d"></div>'
        '<script>document.getElementById("d").innerHTML = "<span>hi</span>";'
        '</script>')
    texts = _texts(tab)
    assert "hi" in texts, f"innerHTML markup not rendered: {texts}"


def test_js_style_mutation():
    tab = _make_tab(
        '<p style="color: black">st</p>'
        '<script>document.querySelector("p").style.color = "red";'
        '</script>')
    st = [c for c in _drawtexts(tab) if c.text == "st"]
    assert st, "no 'st' text node rendered"
    assert st[0].color == "red", \
        f"style.color not applied: {st[0].color!r}"


def test_js_nan_infinity_globals():
    interp = Interpreter()
    interp.run("""
        var x = 0/0;
        var y = typeof NaN;
        var z = typeof Infinity;
        var n = NaN;
        var r;
        if (x) { r = "truthy"; } else { r = "falsy"; }
        var e = (NaN === NaN);
        console.log("ok");
    """)
    g = interp.globals
    eq(g["y"], "number", "typeof NaN is number")
    eq(g["z"], "number", "typeof Infinity is number")
    eq(g["r"], "falsy", "NaN is falsy in a condition")
    assert g["e"] is False, "NaN never strictly equals itself"
    assert g["n"] != g["n"], "NaN stored is actually NaN"


def test_js_null_and_array_coercion():
    interp = Interpreter()
    interp.run("""
        var a = null + 1;
        var b = Number(null);
        var c = undefined + 1;
        var d = [] + [];
        var e = [] + 5;
        var f = [1,2] + "x";
        console.log("ok");
    """)
    g = interp.globals
    eq(g["a"], 1, "null coerces to 0 in addition")
    eq(g["b"], 0, "Number(null) is 0")
    assert g["c"] != g["c"], "undefined + 1 is NaN"
    eq(g["d"], "", "[] + [] is the empty string")
    eq(g["e"], "5", "[] + 5 string-concatenates")
    eq(g["f"], "1,2x", "[1,2] + 'x' joins then concatenates")


def test_js_array_growth_and_length_truncate():
    interp = Interpreter()
    interp.run("""
        var a = [];
        a[3] = "x";
        var hole = a[1];
        var len = a.length;
        a.length = 2;
        var len2 = a.length;
        console.log("ok");
    """)
    g = interp.globals
    eq(g["len"], 4, "assigning past the end grows the array")
    assert g["hole"] is None or str(g["hole"]) == "undefined", \
        "holes read as undefined"
    eq(g["len2"], 2, "setting length truncates the array")


def _make_tab(body, url="https://example.com/page"):
    tab = Tab(700)
    u = URL(url)
    tab.url = u
    tab._build(u, body, "text/html")
    return tab


def main():
    root = tkinter.Tk(); root.withdraw()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback; traceback.print_exc()
            print(f" FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(tests)} JS TESTS PASSED")


if __name__ == "__main__":
    main()