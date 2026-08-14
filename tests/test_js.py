"""Offline tests for the JS interpreter (jsengine) and its browser
integration (script execution, console, click handlers).
"""
import http.server
import sys, os, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import gui

from feetbrowser.net import URL
from feetbrowser.browser import Tab
from feetbrowser.layout import DrawText
from feetbrowser.jsengine import Interpreter, UNDEFINED


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


def test_js_promise_then_all():
    interp = Interpreter()
    interp.run("""
        var out = 0;
        Promise.resolve(42).then(function(v){ out = v; });
        var all;
        Promise.all([Promise.resolve(1), Promise.resolve(2)]).then(
            function(v){ all = v; });
        var chained;
        Promise.resolve(2).then(function(v){ return v * 3; }).then(
            function(v){ chained = v; });
        console.log("ok");
    """)
    eq(interp.globals["out"], 0, "then is async before drain")
    interp.drain()
    eq(interp.globals["out"], 42, "then runs on drain")
    eq(interp.globals["all"], [1, 2], "Promise.all resolves in order")
    eq(interp.globals["chained"], 6, "chained then carries values")


def test_js_promise_reject_and_new():
    interp = Interpreter()
    interp.run("""
        var caught;
        Promise.reject("boom").catch(function(e){ caught = e; });
        var fromExecutor;
        new Promise(function(resolve, reject){ resolve(7); }).then(
            function(v){ fromExecutor = v; });
        console.log("ok");
    """)
    interp.drain()
    eq(interp.globals["caught"], "boom", "reject() + catch()")
    eq(interp.globals["fromExecutor"], 7, "new Promise(executor)")


def test_js_async_await():
    interp = Interpreter()
    interp.run("""
        var out;
        (async function () {
            var x = await Promise.resolve(6);
            var y = await Promise.resolve(7);
            out = x * y;
        })();
    """)
    interp.drain()
    eq(interp.globals["out"], 42, "await resolves values")


def test_js_async_await_rejection():
    interp = Interpreter()
    interp.run("""
        var out;
        (async function () {
            try { await Promise.reject("nope"); }
            catch (e) { out = "caught:" + e; }
        })();
    """)
    interp.drain()
    eq(interp.globals["out"], "caught:nope", "await rejection is catchable")


def test_js_timers():
    interp = Interpreter()
    interp.run("""
        var out = 0;
        setTimeout(function(){ out = 1; }, 50);
        setTimeout(function(){ out = 2; }, 10);
    """)
    interp.advance(20); interp.drain()
    eq(interp.globals["out"], 2, "earlier timeout fires first")
    interp.advance(100); interp.drain()
    eq(interp.globals["out"], 1, "later timeout fires after advance")


def test_js_queue_microtask():
    interp = Interpreter()
    interp.run("""
        var out = 0;
        queueMicrotask(function(){ out = 5; });
    """)
    eq(interp.globals["out"], 0, "microtask deferred")
    interp.drain()
    eq(interp.globals["out"], 5, "microtask runs on drain")


def test_js_try_catch_throw():
    interp = Interpreter()
    interp.run("""
        var out;
        try { throw "err"; } catch (e) { out = e; }
        var fin;
        function f(){ try { return 1; } finally { fin = "done"; } }
        var r = f();
        var emsg;
        try { throw new Error("x"); } catch (e) { emsg = e.message; }
        console.log("ok");
    """)
    eq(interp.globals["out"], "err", "throw + catch binds the value")
    eq(interp.globals["r"], 1, "return value survives finally")
    eq(interp.globals["fin"], "done", "finally runs on return")
    eq(interp.globals["emsg"], "x", "new Error().message")


def test_js_fetch_updates_page():
    served = {"body": "fetched"}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = served["body"].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        tab = _make_tab(
            f'<p id="x">old</p><script>'
            f'fetch("http://127.0.0.1:{port}/data").then(function(r)'
            f'{{ return r.text(); }}).then(function(t)'
            f'{{ document.getElementById("x").textContent = t; }});'
            f'</script>')
        deadline = time.time() + 5
        texts = _texts(tab)
        while "fetched" not in texts and time.time() < deadline:
            tab._drain_js()
            time.sleep(0.02)
            texts = _texts(tab)
        assert "fetched" in texts, f"fetch result not rendered: {texts}"
    finally:
        srv.shutdown()


def test_js_xhr_basic_get():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"xhr-body"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        tab = _make_tab(
            f'<p id="x">old</p><script>'
            f'var x = new XMLHttpRequest();'
            f'x.onload = function()'
            f'{{ document.getElementById("x").textContent = x.responseText; }};'
            f'x.open("GET", "http://127.0.0.1:{port}/x");'
            f'x.send();'
            f'</script>')
        deadline = time.time() + 5
        texts = _texts(tab)
        while "xhr-body" not in texts and time.time() < deadline:
            tab._drain_js()
            time.sleep(0.02)
            texts = _texts(tab)
        assert "xhr-body" in texts, f"XHR result not rendered: {texts}"
    finally:
        srv.shutdown()


def test_js_logical_nullish_and_optional_chaining():
    interp = Interpreter()
    interp.run("""
        var orv = window.nope || "fallback";
        var andv = 0 && 99;
        var andt = 1 && 42;
        var qq = null ?? 7;
        var qq2 = 0 ?? 7;
        var ch = ({a:{b:5}}).a?.b;
        var noch = ({a:null}).a?.b;
    """)
    g = interp.globals
    eq(g["orv"], "fallback", "|| falls through on undefined")
    eq(g["andv"], 0, "&& returns the falsy operand")
    eq(g["andt"], 42, "&& returns the last truthy operand")
    eq(g["qq"], 7, "?? uses the fallback for null")
    eq(g["qq2"], 0, "?? keeps a non-nullish 0")
    eq(g["ch"], 5, "?. short-circuits member access")
    assert g["noch"] is UNDEFINED, "?. yields undefined on nullish base"


def test_js_template_literals():
    interp = Interpreter()
    interp.run(r"""
        var t1 = `x${1+1}y`;
        var t2 = `a${`b${3}c`}d`;
        var t3 = `sum:${1+2+3}`;
    """)
    g = interp.globals
    eq(g["t1"], "x2y", "interpolates expressions")
    eq(g["t2"], "ab3cd", "supports nested templates")
    eq(g["t3"], "sum:6", "templates join literal text and values")


def test_js_arrow_functions_lexical_this():
    interp = Interpreter()
    interp.run("""
        var v = 5;
        var obj = { v: 10, getV: function(){ return (() => this.v)(); } };
        var lex = obj.getV();
        var sq = ((x) => x * x)(7);
    """)
    g = interp.globals
    eq(g["lex"], 10, "arrow captures the enclosing this")
    eq(g["sq"], 49, "expression-body arrow returns its expression")


def test_js_classes_extends_and_super():
    interp = Interpreter()
    interp.run("""
        function Base(n) { this.n = n; }
        Base.prototype.double = function () { return this.n * 2; };
        var Klass = class extends Base {
            constructor(n) { super(n); }
            quad() { return this.double() * 2; }
        };
        var inst = new Klass(21);
        var dbl = inst.double();
        var quad = inst.quad();
        var isKlass = inst instanceof Klass;
        var isBase = inst instanceof Base;
        var isArr = [] instanceof Array;
        var isRe = /a/ instanceof RegExp;
        var isObj = {} instanceof Object;
    """)
    g = interp.globals
    eq(g["dbl"], 42, "inherited prototype method runs")
    eq(g["quad"], 84, "subclass method calls inherited method")
    assert g["isKlass"] is True, "instanceof own class"
    assert g["isBase"] is True, "instanceof function-based parent"
    assert g["isArr"] is True, "instanceof Array"
    assert g["isRe"] is True, "instanceof RegExp"
    assert g["isObj"] is True, "instanceof Object"


def test_js_spread_rest_and_bitwise():
    interp = Interpreter()
    interp.run("""
        var sp = [...[1,2],3,4];
        var rest = (function(a, ...b){ return b; })(1,2,3,4);
        var pow = 2 ** 8;
        var ur = -1 >>> 1;
        var shl = 3 << 2;
        var shr = -1 >> 1;
        var not = ~5;
        var band = 6 & 3;
        var bor = 4 | 1;
        var bxor = 5 ^ 3;
        var acc = 5; acc += 3; acc *= 2;
    """)
    g = interp.globals
    eq(g["sp"], [1, 2, 3, 4], "spread expands array literals")
    eq(g["rest"], [2, 3, 4], "rest collects extra arguments")
    eq(g["pow"], 256, "exponentiation operator")
    eq(g["ur"], 2147483647, "unsigned right shift")
    eq(g["shl"], 12, "left shift")
    eq(g["shr"], -1, "arithmetic right shift keeps sign")
    eq(g["not"], -6, "bitwise not")
    eq(g["band"], 2, "bitwise and")
    eq(g["bor"], 5, "bitwise or")
    eq(g["bxor"], 6, "bitwise xor")
    eq(g["acc"], 16, "compound assignment chain")


def test_js_regex_and_string_methods():
    interp = Interpreter()
    interp.run(r"""
        var rx = /a+/;
        var rxt = rx.test("caaa");
        var rex = rx.exec("caaa")[0];
        var rxr = "abc".replace(/b/, "X");
        var rxm = "a1b2".match(/\d/g).length;
        var spl = "a,b,c".split(",").length;
        var idx = "hello world".indexOf("world");
    """)
    g = interp.globals
    assert g["rxt"] is True, "regex test matches"
    eq(g["rex"], "aaa", "regex exec returns the match")
    eq(g["rxr"], "aXc", "string replace with regex")
    eq(g["rxm"], 2, "global match counts occurrences")
    eq(g["spl"], 3, "string split")
    eq(g["idx"], 6, "string indexOf")


def test_js_builtin_globals_map_set_json_math():
    interp = Interpreter()
    interp.run(r"""
        var m = [1,2,3].map(function(x){ return x * 10; });
        var f = [4,9,16].find(function(x){ return x > 5; });
        var flt = [1,2,3,4].filter(function(x){ return x % 2 === 0; });
        var js = JSON.stringify({a:1});
        var po = JSON.parse('{"x":9}').x;
        var mx = Math.max(1, 7, 3);
        var rnd = Math.floor(3.9);
        var mp = new Map([["a",1],["b",2]]);
        var mpg = mp.get("b");
        var st = new Set([1,2,2,3]);
        var sts = st.size;
        var sth = st.has(3);
        var ok = Object.keys({p:1,q:2}).length;
    """)
    g = interp.globals
    eq(g["m"], [10, 20, 30], "array map")
    eq(g["f"], 9, "array find")
    eq(g["flt"], [2, 4], "array filter")
    eq(g["js"], '{"a":1}', "JSON stringify")
    eq(g["po"], 9, "JSON parse")
    eq(g["mx"], 7, "Math.max")
    eq(g["rnd"], 3, "Math.floor")
    eq(g["mpg"], 2, "Map get after seeded constructor")
    eq(g["sts"], 3, "Set dedupes values")
    assert g["sth"] is True, "Set.has"
    eq(g["ok"], 2, "Object.keys length")


def test_js_for_of_and_for_in():
    interp = Interpreter()
    interp.run("""
        var sos = 0;
        for (var z of [1,2,3]) { sos += z; }
        var foic = 0;
        for (var k in {p:1, q:2}) { foic++; }
        var sos2 = 0;
        for (var w of "ab") { sos2++; }
    """)
    g = interp.globals
    eq(g["sos"], 6, "for...of iterates array elements")
    eq(g["foic"], 2, "for...in counts own keys")
    eq(g["sos2"], 2, "for...of iterates a string")
def test_js_modern_syntax():
    interp = Interpreter()
    interp.run("""
        var arrow = (x, y = 2) => x * y;
        var a = arrow(3);
        var t = `sum: ${1 + 1}`;
        var re = /^ab+c$/;
        var rmatch = re.test("abbbc");
        class Animal {
          constructor(name) { this.name = name; }
          speak() { return this.name + "!"; }
          static kind() { return "animal"; }
        }
        var dog = new Animal("Rex");
        var s = dog.speak();
        var k = Animal.kind();
        var name = "Rex";
        var o = { name, count: 2 };
        var n = o.name;
        var [first, ...rest] = [1, 2, 3];
        var { count: c } = o;
        var sw = "none";
        switch (c) { case 1: sw = "one"; break; case 2: sw = "two"; break; }
        var b1 = (5 & 3), b2 = (1 << 3), b3 = (-8 >>> 1);
        var nx = null ?? "fallback";
        var oc = null ?. missing;
        var del = delete o.count;
        console.log("ok");
    """)
    g = interp.globals
    eq(g["a"], 6, "arrow with default param")
    eq(g["t"], "sum: 2", "template literal interpolation")
    eq(g["rmatch"], True, "regex literal test")
    eq(g["s"], "Rex!", "class method + constructor")
    eq(g["k"], "animal", "static class method")
    eq(g["n"], "Rex", "object shorthand + value")
    eq(g["first"], 1, "array destructure")
    eq(g["rest"], [2, 3], "array rest destructure")
    eq(g["c"], 2, "object destructure rename")
    eq(g["sw"], "two", "switch statement")
    eq(g["b1"], 1, "bitwise and")
    eq(g["b2"], 8, "left shift")
    eq(g["b3"], 2147483644, "unsigned right shift")
    eq(g["nx"], "fallback", "nullish coalescing")
    assert g["oc"] is None or repr(g["oc"]) == "undefined", "optional chaining"
    eq(g["del"], True, "delete returns true")


def test_js_builtins_and_dom():
    interp = Interpreter()
    interp.run("""
        var up = "abc".toUpperCase() + "xyz".slice(0, 2);
        var arr = [1, 2, 3].map(function (x) { return x * 2; });
        var sum = [1, 2, 3, 4].reduce(function (a, b) { return a + b; }, 0);
        var keys = Object.keys({ a: 1, b: 2 }).length;
        var merged = Object.assign({}, { a: 1 }, { b: 2 });
        var j = JSON.parse('{"x": [1, 2]}');
        var str = JSON.stringify({ p: 1 });
        var m = Math.max(1, 9) + Math.floor(2.9);
        var d = Date.now() > 0;
        var bound = (function (a) { return this.x + a; }).bind({ x: 10 }, 5);
        var bind = bound();
        var f = function (a, b) { return a + b; }.apply(null, [3, 4]);
        var own = Object.prototype.hasOwnProperty.call({ k: 1 }, "k");
        var isa = [] instanceof Array;
        console.log("ok");
    """)
    g = interp.globals
    eq(g["up"], "ABCxy", "string methods")
    eq(g["arr"], [2, 4, 6], "Array.map")
    eq(g["sum"], 10, "Array.reduce")
    eq(g["keys"], 2, "Object.keys")
    eq(g["merged"], {"a": 1, "b": 2}, "Object.assign")
    eq(g["j"]["x"][1], 2, "JSON.parse")
    eq(g["str"], '{"p":1}', "JSON.stringify")
    eq(g["m"], 11, "Math methods")
    eq(g["d"], True, "Date.now")
    eq(g["bind"], 15, "Function.bind + this")
    eq(g["f"], 7, "Function.apply")
    eq(g["own"], True, "Object.prototype.hasOwnProperty.call")
    eq(g["isa"], True, "instanceof Array")


def test_js_dom_query_and_classlist():
    tab = _make_tab(
        '<div id="a" class="x y"><p class="q">hi</p></div>'
        '<script>'
        'var el = document.getElementById("a");'
        'el.classList.add("z");'
        'var has = el.classList.contains("z");'
        'var els = document.querySelectorAll("p");'
        'var n = els.length;'
        'el.dataset.foo = "1";'
        'var d = el.dataset.foo;'
        'el.setAttribute("role", "main");'
        'var r = el.getAttribute("role");'
        'el.removeAttribute("role");'
        'var r2 = el.hasAttribute("role");'
        '</script>')
    texts = _texts(tab)
    assert "hi" in texts, f"page rendered: {texts}"
    # The script mutated the DOM; classList/dataset attributes applied.
    root = tab.nodes
    from feetbrowser.htmlparser import Element
    div = next((n for n in _walk_all(root) if isinstance(n, Element)
                and n.attributes.get("id") == "a"), None)
    assert div is not None, "div present"
    assert "z" in div.attributes.get("class", "").split(), \
        f"classList.add applied: {div.attributes.get('class')}"
    assert div.attributes.get("role") is None, "removeAttribute applied"


def _walk_all(node):
    yield node
    for child in node.children:
        yield from _walk_all(child)


def _make_tab(body, url="https://example.com/page"):
    tab = Tab(700)
    u = URL(url)
    tab.url = u
    tab._build(u, body, "text/html")
    return tab


def main():
    root = gui.Tk(); root.withdraw()
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