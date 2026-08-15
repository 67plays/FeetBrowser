"""Offline tests for the JS interpreter (jsengine) and its browser
integration (script execution, console, click handlers).
"""
import http.server
import sys, os, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser.window import Tk

from feetbrowser.net import URL
from feetbrowser.browser import Tab, tree_to_list
from feetbrowser.htmlparser import Element
from feetbrowser.layout import DrawText, LISTBOX_ROW_H, LISTBOX_PAD
from feetbrowser.jsengine import Interpreter, JSException, UNDEFINED


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


def test_js_location_replace_redirect():
    # DuckDuckGo-style redirect: a page whose inline script does
    # `window.parent.location.replace(...)` must navigate to the target.
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            port = self.server.server_address[1]
            if self.path == "/l":
                body = ('<script>window.parent.location.replace("http://'
                        f'127.0.0.1:{port}/target");</script>').encode()
            else:
                body = b"<title>Target</title><p>arrived</p>"
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
        tab = Tab(700)
        tab.load(URL(f"http://127.0.0.1:{port}/l"))
        assert str(tab.url) == f"http://127.0.0.1:{port}/target", \
            f"location.replace did not navigate: {tab.url}"
        texts = _texts(tab)
        assert "arrived" in texts, f"redirect target not rendered: {texts}"
    finally:
        srv.shutdown()


def test_js_location_href_assignment():
    # Assigning `location.href` (and bare `location` on window) navigates too.
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            port = self.server.server_address[1]
            if self.path == "/l":
                body = (f'<script>location.href = "http://'
                        f'127.0.0.1:{port}/target";</script>').encode()
            else:
                body = b"<p>assigned</p>"
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
        tab = Tab(700)
        tab.load(URL(f"http://127.0.0.1:{port}/l"))
        assert str(tab.url) == f"http://127.0.0.1:{port}/target", \
            f"location.href assignment did not navigate: {tab.url}"
        texts = _texts(tab)
        assert "assigned" in texts, f"target not rendered: {texts}"
    finally:
        srv.shutdown()


def test_meta_refresh_redirect():
    # A zero-delay <meta http-equiv="refresh"> redirect navigates without JS.
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/l":
                body = (b'<meta http-equiv="refresh" '
                        b'content="0; url=/target">')
            else:
                body = b"<p>metaarrived</p>"
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
        tab = Tab(700)
        tab.load(URL(f"http://127.0.0.1:{port}/l"))
        assert str(tab.url) == f"http://127.0.0.1:{port}/target", \
            f"meta refresh did not navigate: {tab.url}"
        texts = _texts(tab)
        assert "metaarrived" in texts, f"target not rendered: {texts}"
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


def test_js_object_literal_accessors():
    interp = Interpreter()
    interp.run("""
        var seen = [];
        var person = {
            first: "Ada",
            get name() { return this.first + " Lovelace"; },
            set name(v) { seen.push(v); this.first = v.split(" ")[0]; }
        };
        var read = person.name;
        person.name = "Grace Hopper";
        var afterWrite = person.first;
        var readAgain = person.name;
        var readonly = { get v() { return 7; } };
        readonly.v = 99;
        var stillSeven = readonly.v;
        var writeonly = { set v(n) { this.kept = n; } };
        writeonly.v = 5;
        var noGetter = writeonly.v;
        var kept = writeonly.kept;
        var shorthand = { twice(n) { return n * 2; } }.twice(21);
    """)
    g = interp.globals
    eq(g["read"], "Ada Lovelace", "a getter runs on read with this bound")
    eq(g["seen"], ["Grace Hopper"], "a setter runs on write with the new value")
    eq(g["afterWrite"], "Grace", "the setter's own writes stick")
    eq(g["readAgain"], "Grace Lovelace", "the getter sees what the setter did")
    eq(g["stillSeven"], 7, "a write to a getter-only property is swallowed")
    assert g["noGetter"] is UNDEFINED, "a setter-only property reads as undefined"
    eq(g["kept"], 5, "but its setter still ran")
    eq(g["shorthand"], 42, "method shorthand in an object literal")


def test_js_computed_object_keys():
    interp = Interpreter()
    interp.run("""
        var k = "dyn";
        var i = 2;
        var o = { [k]: 1, ["a" + "b"]: 2, [i * 3]: "six", plain: "yes" };
        var a = o.dyn, b = o.ab, c = o[6], d = o.plain;
        var keyCount = Object.keys(o).length;
        var counter = 0;
        var once = { [(counter += 1, "k" + counter)]: true };
        var onceKey = Object.keys(once)[0];
    """)
    g = interp.globals
    eq(g["a"], 1, "a computed key from a variable")
    eq(g["b"], 2, "a computed key from an expression")
    eq(g["c"], "six", "a numeric computed key stringifies")
    eq(g["d"], "yes", "plain keys still work alongside computed ones")
    eq(g["keyCount"], 4, "every computed key lands as its own property")
    eq(g["onceKey"], "k1", "the key expression is evaluated exactly once")
    eq(g["counter"], 1, "and only once")


def test_js_optional_catch_binding():
    interp = Interpreter()
    interp.run("""
        var hits = 0;
        try { throw new Error("boom"); } catch { hits += 1; }
        var fin = 0;
        try { throw "x"; } catch { hits += 1; } finally { fin = 9; }
        var nested = "";
        try {
            try { throw "inner"; } catch { throw "rethrown"; }
        } catch (e) { nested = e; }
        var bound = "";
        try { throw "still works"; } catch (e) { bound = e; }
    """)
    g = interp.globals
    eq(g["hits"], 2, "catch with no binding still catches")
    eq(g["fin"], 9, "finally runs after an unbound catch")
    eq(g["nested"], "rethrown", "an unbound catch can throw onwards")
    eq(g["bound"], "still works", "the bound form is unaffected")


def test_js_array_from_and_of():
    interp = Interpreter()
    interp.run("""
        var copy = Array.from([1, 2, 3]);
        var mapped = Array.from([1, 2, 3], function (x) { return x * 3; });
        var indexes = Array.from([10, 20], function (x, i) { return i; });
        var chars = Array.from("hey");
        var deduped = Array.from(new Set([1, 1, 2]));
        var like = Array.from({ length: 3, 0: "a", 1: "b", 2: "c" });
        var empty = Array.from([]).length;
        var nothing = Array.from(undefined).length;
        var one = Array.of(7);
        var many = Array.of(1, 2, 3);
        var none = Array.of().length;
        var sized = Array(3).length;
    """)
    g = interp.globals
    eq(g["copy"], [1, 2, 3], "Array.from copies an array")
    eq(g["mapped"], [3, 6, 9], "Array.from applies its mapping function")
    eq(g["indexes"], [0, 1], "the mapping function is given the index too")
    eq(g["chars"], ["h", "e", "y"], "Array.from splits a string")
    eq(g["deduped"], [1, 2], "Array.from drains a Set")
    eq(g["like"], ["a", "b", "c"], "Array.from honours length on a plain object")
    eq(g["empty"], 0, "an empty array stays empty")
    eq(g["nothing"], 0, "undefined yields an empty array rather than throwing")
    eq(g["one"], [7], "Array.of(7) is the array [7], not seven empty slots")
    eq(g["sized"], 3, "whereas Array(3) is still three slots")
    eq(g["many"], [1, 2, 3], "Array.of takes every argument as a value")
    eq(g["none"], 0, "Array.of() is empty")


def test_js_date_fields():
    interp = Interpreter()
    interp.run("""
        var epoch = new Date(0);
        var y = epoch.getFullYear();
        var mo = epoch.getMonth();
        var d = epoch.getDate();
        var h = epoch.getHours();
        var dow = epoch.getDay();
        var t = epoch.getTime();
        var offset = epoch.getTimezoneOffset();
        var iso = epoch.toISOString();
        var parsed = new Date("2021-03-04T05:06:07Z");
        var py = parsed.getFullYear(), pmo = parsed.getMonth(), pd = parsed.getDate();
        var pmin = parsed.getMinutes(), psec = parsed.getSeconds();
        var utcMatches = parsed.getHours() === parsed.getUTCHours();
        var built = new Date(2021, 2, 4).getDate();
        var builtYear = new Date(2021, 2, 4).getFullYear();
        var stamp = Date.UTC(1970, 0, 2);
        var roundTrip = new Date(Date.UTC(2000, 11, 31)).toISOString();
        var ms = new Date(1234567890123).getTime();
    """)
    g = interp.globals
    eq(g["y"], 1970, "the epoch is in 1970, not 1969 in some other zone")
    eq(g["mo"], 0, "January is month zero")
    eq(g["d"], 1, "the epoch is the first of the month")
    eq(g["h"], 0, "and midnight, because local time is UTC here")
    eq(g["dow"], 4, "1 Jan 1970 was a Thursday")
    eq(g["t"], 0, "getTime returns the milliseconds it was built from")
    eq(g["offset"], 0, "there is no zone offset to report")
    eq(g["iso"], "1970-01-01T00:00:00.000Z", "toISOString")
    eq(g["py"], 2021, "a parsed ISO year")
    eq(g["pmo"], 2, "a parsed month is zero-based")
    eq(g["pd"], 4, "a parsed day")
    eq(g["pmin"], 6, "a parsed minute")
    eq(g["psec"], 7, "a parsed second")
    assert g["utcMatches"] is True, "the local and UTC getters agree"
    eq(g["built"], 4, "a date built from parts keeps its day")
    eq(g["builtYear"], 2021, "and its year")
    eq(g["stamp"], 86400000, "Date.UTC counts milliseconds from the epoch")
    eq(g["roundTrip"], "2000-12-31T00:00:00.000Z", "Date.UTC round-trips")
    eq(g["ms"], 1234567890123, "a date built from a timestamp keeps it exactly")


def test_js_regexp_named_groups():
    interp = Interpreter()
    interp.run(r"""
        var m = /(?<y>\d{4})-(?<mo>\d{2})-(?<d>\d{2})/.exec("on 2021-03-04 ok");
        var y = m.groups.y, mo = m.groups.mo, d = m.groups.d;
        var whole = m[0];
        var byNumber = m[1];
        var at = m.index;
        var plain = /\d+/.exec("x7");
        var noGroups = plain.groups;
        var backref = /(?<w>\w+) \k<w>/.test("go go");
        var backrefNo = /(?<w>\w+) \k<w>/.test("go stop");
        var swapped = "2021-03-04".replace(
            /(?<y>\d{4})-(?<mo>\d{2})-(?<d>\d{2})/, "$<d>/$<mo>/$<y>");
        var optional = /(?<a>x)|(?<b>y)/.exec("y");
        var missing = optional.groups.a;
        var present = optional.groups.b;
    """)
    g = interp.globals
    eq(g["y"], "2021", "a named group reads back off .groups")
    eq(g["mo"], "03", "second named group")
    eq(g["d"], "04", "third named group")
    eq(g["whole"], "2021-03-04", "the whole match is still index 0")
    eq(g["byNumber"], "2021", "named groups are numbered as well as named")
    eq(g["at"], 3, "the match reports where it started")
    assert g["noGroups"] is UNDEFINED, "no named groups means no .groups object"
    assert g["backref"] is True, r"\k<name> matches a repeat"
    assert g["backrefNo"] is False, r"and rejects a non-repeat"
    eq(g["swapped"], "04/03/2021", "$<name> in a replacement")
    assert g["missing"] is UNDEFINED, "an unmatched named group is undefined"
    eq(g["present"], "y", "while the one that matched holds its text")


def test_js_regexp_lookaround():
    interp = Interpreter()
    interp.run(r"""
        var pos = /foo(?=bar)/.test("foobar");
        var posNo = /foo(?=bar)/.test("foobaz");
        var neg = /foo(?!bar)/.test("foobaz");
        var negNo = /foo(?!bar)/.test("foobar");
        var kept = "foobar".replace(/foo(?=bar)/, "X");
        var width = /foo(?=bar)/.exec("foobar")[0];
        var grouped = "1234567".replace(/\B(?=(\d{3})+(?!\d))/g, ",");
        var behind = /(?<=\$)\d+/.exec("cost $42")[0];
        var negBehind = /(?<!\$)\b\d+/.exec("cost 42")[0];
        var chained = /\d+(?= dollars)(?!\d)/.exec("50 dollars")[0];
    """)
    g = interp.globals
    assert g["pos"] is True, "lookahead accepts when what follows matches"
    assert g["posNo"] is False, "and rejects when it does not"
    assert g["neg"] is True, "negative lookahead accepts when it does not match"
    assert g["negNo"] is False, "and rejects when it does"
    eq(g["kept"], "Xbar", "lookahead is zero width, so 'bar' survives")
    eq(g["width"], "foo", "the match itself excludes the lookahead")
    eq(g["grouped"], "1,234,567", "the thousands-separator idiom works")
    eq(g["behind"], "42", "lookbehind")
    eq(g["negBehind"], "42", "negative lookbehind")
    eq(g["chained"], "50", "two assertions in a row")


def test_js_async_functions_beyond_expressions():
    interp = Interpreter()
    interp.run("""
        var order = [];
        async function twice(n) { return n * 2; }
        async function work() {
            var a = await twice(4);
            var b = await Promise.resolve(a + 1);
            order.push(a, b);
            return "done";
        }
        var p = work();
        var isThenable = typeof p.then === "function";
        var settled = "";
        work().then(function (v) { settled = v; });
        var thrown = "";
        async function boom() { throw "kaboom"; }
        boom().catch(function (e) { thrown = e; });
        class Loader {
            async load() { return await Promise.resolve("payload"); }
        }
        var loaded = "";
        new Loader().load().then(function (v) { loaded = v; });
        var awaited = 0;
        async function loop() {
            var total = 0;
            for (var i = 0; i < 3; i++) { total += await Promise.resolve(i); }
            awaited = total;
        }
        loop();
    """)
    interp.drain()
    g = interp.globals
    assert g["isThenable"] is True, "an async declaration returns a promise"
    eq(g["order"], [8, 9, 8, 9], "await inside a declared async function")
    eq(g["settled"], "done", "its return value resolves the promise")
    eq(g["thrown"], "kaboom", "a throw inside it rejects the promise")
    eq(g["loaded"], "payload", "an async class method")
    eq(g["awaited"], 3, "await inside a loop")


def test_js_arguments_object():
    interp = Interpreter()
    interp.run("""
        function count() { return arguments.length; }
        var three = count(1, 2, 3);
        var zero = count();
        function second() { return arguments[1]; }
        var b = second("a", "b", "c");
        function total() {
            var t = 0;
            for (var i = 0; i < arguments.length; i++) { t += arguments[i]; }
            return t;
        }
        var summed = total(1, 2, 3, 4);
        function extras(named) { return named + ":" + arguments.length; }
        var beyond = extras("x", "unused", "also unused");
        var fromArrow = (function () {
            var inner = function () { return arguments.length; };
            return inner(1);
        })(9, 8, 7);
        var outerSeen = (function () {
            var inner = () => arguments.length;
            return inner();
        })(9, 8, 7);
        var asArray = (function () { return Array.from(arguments); })(1, 2);
    """)
    g = interp.globals
    eq(g["three"], 3, "arguments.length counts what was passed")
    eq(g["zero"], 0, "even when nothing was")
    eq(g["b"], "b", "arguments is indexable")
    eq(g["summed"], 10, "arguments can be walked")
    eq(g["beyond"], "x:3", "arguments sees more than the declared parameters")
    eq(g["fromArrow"], 1, "a nested function has its own arguments")
    eq(g["outerSeen"], 3, "but an arrow borrows the enclosing one")
    eq(g["asArray"], [1, 2], "arguments is array-like enough for Array.from")


def test_js_class_accessors():
    interp = Interpreter()
    interp.run("""
        class Temp {
            constructor(c) { this.c = c; }
            get f() { return this.c * 9 / 5 + 32; }
            set f(v) { this.c = (v - 32) * 5 / 9; }
            static get freezing() { return new Temp(0); }
        }
        var t = new Temp(100);
        var boiling = t.f;
        t.f = 32;
        var afterSet = t.c;
        var staticGet = Temp.freezing.f;
        class Counted extends Temp {
            get f() { return "overridden"; }
        }
        var overridden = new Counted(100).f;
        var inheritedSet = new Counted(0);
        inheritedSet.c = 37;
        var inheritedField = inheritedSet.c;
    """)
    g = interp.globals
    eq(g["boiling"], 212, "a class getter runs on read")
    eq(g["afterSet"], 0, "a class setter runs on write")
    eq(g["staticGet"], 32, "a static getter runs on the class itself")
    eq(g["overridden"], "overridden", "a subclass getter shadows the parent's")
    eq(g["inheritedField"], 37, "a plain field is untouched by all this")


def test_js_class_extends_error():
    interp = Interpreter()
    interp.run("""
        class AppError extends Error {
            constructor(msg, code) {
                super(msg);
                this.name = "AppError";
                this.code = code;
            }
            get label() { return this.name + "/" + this.code; }
        }
        var e = new AppError("bad input", 42);
        var msg = e.message;
        var nm = e.name;
        var code = e.code;
        var label = e.label;
        var isApp = e instanceof AppError;
        var isErr = e instanceof Error;
        var caught = "";
        try { throw new AppError("thrown", 7); }
        catch (err) { caught = err.name + ":" + err.message + ":" + err.code; }
        class Deeper extends AppError {
            constructor() { super("deep", 1); this.name = "Deeper"; }
        }
        var deep = new Deeper();
        var deepMsg = deep.message;
        var deepLabel = deep.label;
    """)
    g = interp.globals
    eq(g["msg"], "bad input", "super(msg) reaches the built-in Error")
    eq(g["nm"], "AppError", "the subclass can rename itself")
    eq(g["code"], 42, "and carry its own fields")
    eq(g["label"], "AppError/42", "a getter on an Error subclass")
    assert g["isApp"] is True, "instanceof the subclass"
    assert g["isErr"] is True, "instanceof the built-in Error it extends"
    eq(g["caught"], "AppError:thrown:7", "it survives being thrown and caught")
    eq(g["deepMsg"], "deep", "two levels of subclassing still reach Error")
    eq(g["deepLabel"], "Deeper/1", "and inherit the middle class's getter")


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


def test_js_comma_operator_runs_every_operand():
    """A minifier turns statements into commas wherever it can, so `return
    f(x), y` and `for (i = 0, n = 5;;)` are all over real scripts."""
    interp = Interpreter()
    interp.run("""
        var a;
        var paren = (a = 5, a + 1);
        function f() { return a += 1, "last"; }
        var ret = f();
        var seen = 0;
        for (var i = 0, j = 3; i < j; i++, seen++) ;
        var stmt1, stmt2;
        stmt1 = 1, stmt2 = 2;
    """)
    g = interp.globals
    eq(g["paren"], 6, "the value is the last operand")
    eq(g["a"], 6, "and the earlier ones still happened")
    eq(g["ret"], "last", "a comma in a return")
    eq(g["seen"], 3, "a comma in a for header")
    eq((g["stmt1"], g["stmt2"]), (1, 2), "a comma between statements")


def test_js_else_after_a_semicolon():
    """`if (a) b(); else c();` is how every minified if/else is written --
    the semicolon ends the consequent, and the else still belongs to the if.
    """
    interp = Interpreter()
    interp.run("""
        var taken;
        if (1) taken = "then"; else taken = "otherwise";
        var missed;
        if (0) missed = "then"; else missed = "otherwise";
        var empty = "untouched";
        if (0) ; else empty = "else ran";
        var spun = 0;
        for (var i = 0; i < 3; i++) ;
    """)
    g = interp.globals
    eq(g["taken"], "then")
    eq(g["missed"], "otherwise")
    eq(g["empty"], "else ran", "an empty statement is still a statement")


def test_js_labelled_break_and_continue():
    interp = Interpreter()
    interp.run("""
        var inner = [];
        outer: for (var i = 0; i < 3; i++) {
            for (var j = 0; j < 3; j++) {
                if (j == 1) continue outer;
                inner.push(i * 10 + j);
            }
        }
        var stopped = [];
        out2: for (var a = 0; a < 3; a++) {
            for (var b = 0; b < 3; b++) {
                if (a == 1) break out2;
                stopped.push(a * 10 + b);
            }
        }
        var block = [];
        done: { block.push(1); break done; block.push(2); }
        block.push(3);
        var plain = [];
        for (var c = 0; c < 3; c++) { if (c == 1) continue; plain.push(c); }
    """)
    g = interp.globals
    eq(g["inner"], [0, 10, 20], "continue skips to the outer loop's next turn")
    eq(g["stopped"], [0, 1, 2], "break leaves both loops at once")
    eq(g["block"], [1, 3], "a labelled block is breakable too")
    eq(g["plain"], [0, 2], "an unlabelled continue is unaffected")


def test_js_source_is_read_as_utf8_not_bytes():
    # The lexer used to read one byte and call it a character, which is a
    # Latin-1 misreading of UTF-8: a `×` scanned as `Ã` plus a control
    # character. That mangled every non-ASCII literal, and because `Ã` is
    # alphabetic the identifier scanner accepted it, stopped one byte in, and
    # sliced through the middle of the character -- a Rust panic, which
    # crosses the FFI boundary and kills the whole page load. python.org
    # ships a `×` in an inline script and rendered nothing at all.
    interp = Interpreter()
    interp.run("""
        var mul = "×";
        var mullen = mul.length;
        var jp = "日本語";
        var jplen = jp.length;
        var third = jp[2];
        var café = 5;
        var ident = café + 1;
        var escaped = "\\u00d7";
        var same = (escaped === mul);
        var hit = /é+/.test("xée");
        var up = "héllo".toUpperCase();
        /* × in a comment */
        var after = 1 + 1;
    """)
    g = interp.globals
    eq(g["mul"], "×", "a multi-byte literal survives intact")
    eq(g["mullen"], 1, "and counts as one character, not two bytes")
    eq(g["jp"], "日本語", "three-byte characters too")
    eq(g["jplen"], 3, "counted by character")
    eq(g["third"], "語", "and indexable by character")
    eq(g["ident"], 6, "a non-ASCII identifier is one name")
    assert g["same"] is True, "\\u00d7 and a literal x are the same string"
    assert g["hit"] is True, "a regex literal keeps its non-ASCII class"
    eq(g["up"], "HÉLLO", "case mapping is per character")
    eq(g["after"], 2, "and the scan carries on past a non-ASCII comment")

    # A stray non-ASCII character is a syntax error, and reports itself as the
    # character it is rather than the first byte of one.
    try:
        Interpreter().run("1 +× 2")
    except JSException as e:
        assert "'×'" in str(e), f"names the character it choked on: {e}"
    else:
        raise AssertionError("a stray character should not tokenize")
def test_js_dom_nodelist_and_traversal():
    """NodeList length/item/index/forEach, element traversal and geometry."""
    tab = _make_tab(
        '<div id="a" class="x"><span class="y">hi</span>'
        '<span class="y">there</span></div>'
        '<script>'
        'var a = document.getElementById("a");'
        'var ys = document.querySelectorAll(".y");'
        'var acc = [];'
        'ys.forEach(function(e, i) { acc.push(e.textContent + i); });'
        'var out = {'
        ' len: ys.length,'
        ' first: a.firstElementChild.tagName,'
        ' last: a.lastElementChild.textContent,'
        ' nxt: ys[0].nextElementSibling.textContent,'
        ' prv: ys[1].previousElementSibling.textContent,'
        ' item: ys.item(1).textContent,'
        ' idx: ys[0].textContent,'
        ' fe: acc.join("|"),'
        ' contains: a.contains(ys[0]),'
        ' matches: a.matches(".x"),'
        ' closest: ys[0].closest("#a").id,'
        ' ohtml: a.outerHTML.slice(0, 12),'
        ' cec: a.childElementCount,'
        ' ecount: document.getElementsByTagName("span").length,'
        ' ctn: document.createTextNode("zz").textContent,'
        '};'
        'window.__out = out;'
        '</script>')
    eq(tab.js_logs, [], "no js errors")
    g = tab._js_interp.globals["__out"]
    eq(g["len"], 2, "NodeList.length")
    eq(g["first"], "SPAN", "firstElementChild")
    eq(g["last"], "there", "lastElementChild")
    eq(g["nxt"], "there", "nextElementSibling")
    eq(g["prv"], "hi", "previousElementSibling")
    eq(g["item"], "there", "NodeList.item(1)")
    eq(g["idx"], "hi", "NodeList[0]")
    eq(g["fe"], "hi0|there1", "NodeList.forEach indexes")
    eq(g["contains"], True, "element.contains")
    eq(g["matches"], True, "element.matches")
    eq(g["closest"], "a", "element.closest")
    eq(g["ohtml"], '<div id="a" ', "element.outerHTML")
    eq(g["cec"], 2, "childElementCount")
    eq(g["ecount"], 2, "getElementsByTagName")
    eq(g["ctn"], "zz", "createTextNode")


def test_js_window_environment_and_mutation():
    """getComputedStyle (live), window/navigator globals, createElement +
    appendChild, and element.remove()."""
    tab = _make_tab(
        '<div id="a"><span class="y">hi</span></div>'
        '<script>'
        'var s = document.querySelector(".y").style;'
        's.color = "blue";'
        'var cs = getComputedStyle(document.querySelector(".y"));'
        'var e = document.createElement("em");'
        'e.textContent = "NEW";'
        'document.getElementById("a").appendChild(e);'
        'var span = document.querySelector(".y");'
        'span.remove();'
        'var out = {'
        ' color: cs.color,'
        ' prop: cs.getPropertyValue("font-size"),'
        ' app: document.getElementById("a").lastElementChild.textContent,'
        ' removed: document.querySelectorAll(".y").length,'
        ' ua: navigator.userAgent.indexOf("FeetBrowser") >= 0,'
        ' raf: typeof requestAnimationFrame,'
        ' caf: typeof cancelAnimationFrame,'
        ' me: typeof matchMedia,'
        ' wad: typeof addEventListener,'
        ' dpr: devicePixelRatio,'
        ' iw: innerWidth,'
        '};'
        'window.__out = out;'
        '</script>')
    eq(tab.js_logs, [], "no js errors")
    g = tab._js_interp.globals["__out"]
    eq(g["color"], "blue", "getComputedStyle reflects live style")
    eq(g["prop"], "16px", "getPropertyValue resolves font-size")
    eq(g["app"], "NEW", "createElement + appendChild")
    eq(g["removed"], 0, "element.remove removes from DOM")
    eq(g["ua"], True, "navigator.userAgent")
    eq(g["raf"], "function", "requestAnimationFrame global")
    eq(g["caf"], "function", "cancelAnimationFrame global")
    eq(g["me"], "function", "matchMedia global")
    eq(g["wad"], "function", "window.addEventListener global")
    eq(g["dpr"], 1, "devicePixelRatio")
    eq(g["iw"], 1000, "innerWidth matches browser WIDTH")


def test_js_img_src_and_anchor_href_are_stored_as_attributes():
    """`img.src = ...` and `a.href = ...` land in the attribute dictionary,
    where js_get reads them from and layout looks images up by.

    Script-created banners are built exactly this way -- createElement('img'),
    set .src, appendChild -- and a write that went nowhere left every one of
    them a bare <img> that rendered as "[img]". The fetch half of the fix (a
    JS-created image actually loading) is covered in test_render.
    """
    import base64
    import struct
    import zlib as _z

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", _z.crc32(tag + data))

    def png(w, h):
        rows = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
        raw = b"\x89PNG\r\n\x1a\n"
        raw += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        raw += chunk(b"IDAT", _z.compress(rows))
        raw += chunk(b"IEND", b"")
        return raw

    src = "data:image/png;base64," + base64.b64encode(png(2, 2)).decode()
    tab = _make_tab(
        '<div id="a"></div>'
        '<script>'
        'var img = document.createElement("img");'
        'img.src = "' + src + '";'
        'var a = document.createElement("a");'
        'a.href = "/banners/ipv6.gif";'
        'a.appendChild(img);'
        'document.getElementById("a").appendChild(a);'
        'window.__src = img.getAttribute("src");'
        'window.__href = a.getAttribute("href");'
        '</script>')
    eq(tab.js_logs, [], "no js errors")
    g = tab._js_interp.globals
    eq(g["__src"], src, "img.src stored as an attribute")
    eq(g["__href"], "/banners/ipv6.gif", "a.href stored as an attribute")
    imgs = [n for n in tree_to_list(tab.nodes, [])
            if isinstance(n, Element) and n.tag == "img"]
    eq(imgs[0].attributes.get("src"), src, "DOM node carries the img src")


def test_js_document_fragment():
    tab = _make_tab(
        '<div id="a"></div>'
        '<script>'
        'var frag = document.createDocumentFragment();'
        'var e1 = document.createElement("b"); e1.textContent = "ONE";'
        'var e2 = document.createElement("i"); e2.textContent = "TWO";'
        'frag.appendChild(e1);'
        'frag.appendChild(e2);'
        'document.getElementById("a").appendChild(frag);'
        'window.__n = document.getElementById("a").childElementCount;'
        'window.__t = document.getElementById("a").textContent;'
        '</script>')
    eq(tab.js_logs, [], "no js errors")
    eq(tab._js_interp.globals["__n"], 2, "fragment children land in body")
    eq(tab._js_interp.globals["__t"], "ONETWO", "fragment text lands")


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


# -- <select> and the DOM ---------------------------------------------------

_SELECT = (
    '<select id="s">'
    '<option value="a">Apple</option>'
    '<option value="b" selected>Banana</option>'
    '<option value="c">Cherry</option>'
    '</select>'
)


def _option(tab, value):
    return next(n for n in tree_to_list(tab.nodes, [])
                if isinstance(n, Element) and n.tag == "option"
                and n.attributes.get("value") == value)


def _select(tab):
    return next(n for n in tree_to_list(tab.nodes, [])
                if isinstance(n, Element) and n.tag == "select")


def test_select_value_reads_the_selected_option():
    tab = _make_tab(
        _SELECT +
        '<script>window.got = document.getElementById("s").value;</script>')
    eq(tab._js_interp.globals["got"], "b",
       "select.value must read the `selected` option")


def test_choosing_an_option_fires_change():
    tab = _make_tab(
        _SELECT +
        '<script>window.seen = "";'
        'document.getElementById("s").addEventListener("change", function(){'
        '  window.seen = document.getElementById("s").value; });</script>')
    tab.choose_option(_select(tab), _option(tab, "c"))
    eq(tab._js_interp.globals["seen"], "c",
       "a change listener must run, and see the new value")


def test_the_onchange_attribute_fires_too():
    tab = _make_tab(
        '<select id="s" onchange="window.hit = 1">'
        '<option value="a">Apple</option><option value="b">Banana</option>'
        '</select><script>window.hit = 0;</script>')
    tab.choose_option(_select(tab), _option(tab, "b"))
    eq(tab._js_interp.globals["hit"], 1, "the onchange attribute must run")


def test_change_does_not_fire_when_the_choice_did_not_move():
    tab = _make_tab(
        _SELECT +
        '<script>window.n = 0;'
        'document.getElementById("s").addEventListener("change", function(){'
        '  window.n = window.n + 1; });</script>')
    tab.choose_option(_select(tab), _option(tab, "b"))  # already selected
    eq(tab._js_interp.globals["n"], 0,
       "re-picking the current option is not a change")


def test_writing_select_value_moves_the_selection():
    tab = _make_tab(
        _SELECT +
        '<script>document.getElementById("s").value = "c";</script>')
    tab.render()
    assert "selected" in _option(tab, "c").attributes, \
        "a script writing .value must move the selection"
    assert "selected" not in _option(tab, "b").attributes, \
        "and take it off the option that had it"
    assert "Cherry" in _texts(tab), \
        f"the closed control must repaint with the new label: {_texts(tab)}"


def test_clicking_a_listbox_row_fires_change():
    tab = _make_tab(
        '<select id="s" size="3">'
        '<option value="a" selected>Apple</option>'
        '<option value="b">Banana</option>'
        '</select>'
        '<script>window.seen = "";'
        'document.getElementById("s").addEventListener("change", function(){'
        '  window.seen = document.getElementById("s").value; });</script>')
    node = _select(tab)
    lx, ty, _rx, _by = tab._control_rect(node)
    tab.click(lx + 6, ty + LISTBOX_PAD + 1.5 * LISTBOX_ROW_H - tab.scroll)
    eq(tab._js_interp.globals["seen"], "b",
       "a click inside an expanded select must reach a change listener")


def test_a_multiple_listbox_reads_its_first_chosen_value():
    tab = _make_tab(
        '<select id="s" multiple>'
        '<option value="a">Apple</option>'
        '<option value="b" selected>Banana</option>'
        '<option value="c" selected>Cherry</option>'
        '</select>'
        '<script>window.v = document.getElementById("s").value;</script>')
    eq(tab._js_interp.globals["v"], "b",
       ".value on a multi-choice select is its first chosen option")


# The tests below each hold one snippet reduced from a script a real site
# serves. The comment above each says where it came from, because the shape of
# minified code is the whole reason these cases exist: nobody writes a regex
# hard up against a closing brace by hand.


def test_js_regex_literal_after_a_closing_brace():
    # vimeo.com, fb6eed97-*.js: `...continue}}}/^.+[.-]min\.js$/.test(x)`.
    # Read as division, the slash swallowed the rest of the file and the
    # parser reported a stray backslash three hundred lines later.
    interp = Interpreter()
    interp.run(r"""
        var hits = 0;
        for (var i = 0; i < 3; i++) { if (i === 0) { continue } hits++ }
        /^.+[.-]min\.js$/.test("a.min.js") && (hits += 10);
        var divided = (function(){ var n = 8; return n }() / 2);
    """)
    g = interp.globals
    eq(g["hits"], 12, "a regex may start a statement right after a block")
    eq(g["divided"], 4, "and a real division still divides")


def test_js_tagged_templates():
    # developer.mozilla.org, index.*.js: lit-html spells every piece of markup
    # as (0,a.qy)`<span>${x}</span>`, so a tag call was most of the file.
    interp = Interpreter()
    interp.run(r"""
        function tag(strings) {
            var subs = Array.prototype.slice.call(arguments, 1);
            return strings.raw[0] + "|" + strings[0] + "|" + subs.join(",");
        }
        var t = tag`a\nb${1}c${2}`;
        var raw = String.raw`C:\new\table${1}`;
        var lib = { qy: function(s){ return "<" + s.join("_") + ">"; } };
        var member = (0, lib.qy)`x${9}y`;
    """)
    g = interp.globals
    eq(g["t"], "a\\nb|a\nb|1,2", "a tag sees both the raw and the cooked text")
    eq(g["raw"], r"C:\new\table1", "String.raw leaves the backslashes alone")
    eq(g["member"], "<x_y>", "a tag may be a member expression")


def test_js_class_fields_and_private_names():
    # developer.mozilla.org, 18376: a class whose whole state is `#e;#t;#o;`
    # declared above the constructor, plus `static styles=r.A` on the button.
    interp = Interpreter()
    interp.run("""
        class Counter {
            #n = 0;
            step = 2;
            static kind = "counter";
            #bump() { this.#n += this.step; return this.#n; }
            get value() { return this.#bump(); }
        }
        var c = new Counter();
        var first = c.value;
        var second = c.value;
        var kind = Counter.kind;
        var has = ("#n" in {}) === false;
    """)
    g = interp.globals
    eq(g["first"], 2, "an instance field initialises before the constructor")
    eq(g["second"], 4, "a private field is state like any other")
    eq(g["kind"], "counter", "a static field lives on the class")
    assert g["has"] is True, "and a private name is not a name anything else can spell"


def test_js_computed_class_and_object_members():
    # developer.mozilla.org, 5909: `[Symbol.toPrimitive](e){...}`, and
    # vimeo.com, fb6eed97: `{*entries(){...}, [Symbol.iterator]: () => i()}`.
    interp = Interpreter()
    interp.run("""
        var KEY = "shout";
        class Words {
            constructor(list) { this.list = list; }
            [KEY]() { return this.list.join("!"); }
            [Symbol.iterator]() { return this.pairs(); }
            *pairs() { yield this.list[0]; yield this.list[1]; }
        }
        var w = new Words(["a", "b"]);
        var shouted = w.shout();
        var spread = [...w].join("-");
        var lit = { *entries() { yield 1; yield 2; }, [KEY]: 7 };
        var fromLit = [...lit.entries()].join("+");
        var litKey = lit.shout;
    """)
    g = interp.globals
    eq(g["shouted"], "a!b", "a computed method name is the string it evaluates to")
    eq(g["spread"], "a-b", "a class may spell Symbol.iterator that way")
    eq(g["fromLit"], "1+2", "an object literal may hold a generator method")
    eq(g["litKey"], 7, "and a computed key beside it")


def test_js_generators():
    # vimeo.com, 9192: `for (let n of function*(e){ ... yield t ... }(x))`.
    interp = Interpreter()
    interp.run("""
        function* count(n) { for (var i = 0; i < n; i++) { yield i; } }
        var listed = [...count(4)].join(",");
        function* both() { yield "a"; yield* count(2); }
        var delegated = Array.from(both()).join(",");
        var stepped = count(2);
        var one = stepped.next().value;
        var two = stepped.next().value;
        var done = stepped.next().done;
        var inline = [...function*(){ yield 9; }()].join("");
    """)
    g = interp.globals
    eq(g["listed"], "0,1,2,3", "a generator is iterable")
    eq(g["delegated"], "a,0,1", "yield* hands on everything the inner one yields")
    eq(g["one"], 0, "next() walks it a step at a time")
    eq(g["two"], 1, "and keeps its place")
    assert g["done"] is True, "and says when it is finished"
    eq(g["inline"], "9", "a generator expression may be called where it stands")


def test_js_destructuring_assignment_onto_properties():
    # vimeo.com, fb6eed97: `({comparer: f.moduleSpecifierComparer} = eaL(_, u))`
    # -- a destructuring assignment whose targets are properties, not names.
    interp = Interpreter()
    interp.run("""
        var out = {};
        ({ a: out.first, b: out.second } = { a: 1, b: 2 });
        [out.third] = [3];
        var swap = { x: 1, y: 2 };
        [swap.x, swap.y] = [swap.y, swap.x];
    """)
    g = interp.globals
    eq(g["out"]["first"], 1, "an object pattern may target a property")
    eq(g["out"]["third"], 3, "and so may an array pattern")
    eq(g["swap"]["x"], 2, "which is enough to swap two of them")


def test_js_uncurried_prototype_methods():
    # vimeo.com, polyfills-*.js (core-js): every method it ships is built by
    #   var call = Function.prototype.call;
    #   var uncurryThis = function (fn) {
    #     return function () { return call.apply(fn, arguments); };
    #   };
    # so a method taken off one value has to run against another.
    interp = Interpreter()
    interp.run("""
        var g = Function.prototype, call = g.call;
        function uncurry(fn) {
            return function () { return call.apply(fn, arguments); };
        }
        var classOf = uncurry({}.toString);
        var slice = uncurry("".slice);
        var join = uncurry([].join);
        var tagArray = classOf([]);
        var tagNull = classOf(null);
        var cut = slice("hello", 1, 3);
        var joined = join([1, 2, 3], "-");
        var bound = g.bind.call(function () { return this.z; }, { z: 9 })();
    """)
    g = interp.globals
    eq(g["tagArray"], "[object Array]",
       "Object.prototype.toString stays itself when it changes hands")
    eq(g["tagNull"], "[object Null]", "including for the values that have no methods")
    eq(g["cut"], "el", "an uncurried string method runs on the string it is given")
    eq(g["joined"], "1-2-3", "and an array method on the array")
    eq(g["bound"], 9, "bind reaches through the same path")


def test_js_error_subclasses():
    # vimeo.com, polyfills-*.js: `jt = TypeError` ... `new jt(...)`, which is
    # how every guard in that bundle reports a bad argument.
    interp = Interpreter()
    interp.run("""
        var names = [], msg = "";
        [TypeError, RangeError, SyntaxError, ReferenceError].forEach(function (E) {
            try { throw new E("bad"); } catch (e) { names.push(e.name); msg = e.message; }
        });
        var joined = names.join(",");
    """)
    g = interp.globals
    eq(g["joined"], "TypeError,RangeError,SyntaxError,ReferenceError",
       "each error constructor names itself")
    eq(g["msg"], "bad", "and carries the message it was given")


def main():
    root = Tk(); root.withdraw()
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