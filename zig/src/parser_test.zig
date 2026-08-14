//! Standalone test runner for the JS lexer + parser.
//!
//!   zig run zig/src/parser_test.zig
//!
//! Prints a pass/fail count and exits nonzero if anything failed.

const std = @import("std");
const ast = @import("ast.zig");
const parser = @import("parser.zig");

var passed: usize = 0;
var failed: usize = 0;

fn report(comptime fmt: []const u8, args: anytype) void {
    const w = std.io.getStdOut().writer();
    w.print(fmt, args) catch {};
}

const Outcome = struct {
    text: ?[]const u8,
    err: ?ast.ParseError,
};

fn run(alloc: std.mem.Allocator, src: []const u8) Outcome {
    var p = parser.Parser.init(alloc, src);
    const tree = p.parseProgram() catch {
        return .{ .text = null, .err = p.err };
    };
    var buf = std.ArrayList(u8).init(alloc);
    parser.dump(tree, buf.writer()) catch return .{ .text = null, .err = null };
    return .{ .text = buf.items, .err = null };
}

/// Parses `src` and compares the dumped tree with `expect`.
fn check(src: []const u8, expect: []const u8) void {
    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    const o = run(arena.allocator(), src);
    if (o.text) |t| {
        if (std.mem.eql(u8, t, expect)) {
            passed += 1;
            return;
        }
        failed += 1;
        report("FAIL  {s}\n  expected: {s}\n  actual:   {s}\n", .{ src, expect, t });
    } else {
        failed += 1;
        if (o.err) |e| {
            report("FAIL  {s}\n  parse error at {d}:{d}: {s}\n", .{ src, e.line, e.column, e.message });
        } else {
            report("FAIL  {s}\n  parse failed with no error set\n", .{src});
        }
    }
}

/// Asserts that `src` parses at all.
fn ok(src: []const u8) void {
    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    const o = run(arena.allocator(), src);
    if (o.text != null) {
        passed += 1;
        return;
    }
    failed += 1;
    if (o.err) |e| {
        report("FAIL (should parse)  {s}\n  error at {d}:{d}: {s}\n", .{ src, e.line, e.column, e.message });
    } else {
        report("FAIL (should parse)  {s}\n  no error set\n", .{src});
    }
}

/// Asserts that `src` is rejected, cleanly, with an error message.
fn bad(src: []const u8) void {
    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    const o = run(arena.allocator(), src);
    if (o.text != null) {
        failed += 1;
        report("FAIL (should be rejected)  {s}\n  got: {s}\n", .{ src, o.text.? });
        return;
    }
    if (o.err == null or o.err.?.message.len == 0) {
        failed += 1;
        report("FAIL (rejected without a message)  {s}\n", .{src});
        return;
    }
    passed += 1;
}

fn badLong(alloc: std.mem.Allocator, src: []const u8, what: []const u8) void {
    var arena = std.heap.ArenaAllocator.init(alloc);
    defer arena.deinit();
    const o = run(arena.allocator(), src);
    if (o.text != null) {
        failed += 1;
        report("FAIL (should be rejected)  <{s}>\n", .{what});
        return;
    }
    if (o.err == null) {
        failed += 1;
        report("FAIL (rejected without a message)  <{s}>\n", .{what});
        return;
    }
    passed += 1;
}

fn okLong(alloc: std.mem.Allocator, src: []const u8, what: []const u8) void {
    var arena = std.heap.ArenaAllocator.init(alloc);
    defer arena.deinit();
    const o = run(arena.allocator(), src);
    if (o.text != null) {
        passed += 1;
        return;
    }
    failed += 1;
    if (o.err) |e| {
        report("FAIL (should parse) <{s}>\n  error at {d}:{d}: {s}\n", .{ what, e.line, e.column, e.message });
    } else {
        report("FAIL (should parse) <{s}>\n", .{what});
    }
}

// ---------------------------------------------------------------------------

fn testLiterals() void {
    check("1;", "(program (expr 1))");
    check("1.5;", "(program (expr 1.5))");
    check(".5;", "(program (expr 0.5))");
    check("5.;", "(program (expr 5))");
    check("0x1F;", "(program (expr 31))");
    check("0o17;", "(program (expr 15))");
    check("0b1011;", "(program (expr 11))");
    check("1e3;", "(program (expr 1000))");
    check("1.5e-2;", "(program (expr 0.015))");
    check("1_000_000;", "(program (expr 1000000))");
    check("0755;", "(program (expr 493))");
    check("089;", "(program (expr 89))");
    check("'abc';", "(program (expr \"abc\"))");
    check("\"a\\tb\";", "(program (expr \"a\tb\"))");
    check("'\\x41';", "(program (expr \"A\"))");
    check("'\\u0041';", "(program (expr \"A\"))");
    check("'\\u{1F600}';", "(program (expr \"\u{1F600}\"))");
    check("'a\\\nb';", "(program (expr \"ab\"))");
    check("'\\q';", "(program (expr \"q\"))");
    check("true; false; null;", "(program (expr true) (expr false) (expr null))");
    check("this;", "(program (expr this))");
    check("[1,,2];", "(program (expr (array 1 hole 2)))");
    check("[1,2,];", "(program (expr (array 1 2)))");
    check("$_a1;", "(program (expr $_a1))");
    ok("var \u{00e9}t\u{00e9} = 1;");
    ok("'\\0';");
    ok("1n; 0x10n;");
}

fn testOperators() void {
    check("1 + 2 * 3;", "(program (expr (+ 1 (* 2 3))))");
    check("1 * 2 + 3;", "(program (expr (+ (* 1 2) 3)))");
    check("2 ** 3 ** 2;", "(program (expr (** 2 (** 3 2))))");
    check("a = b = c;", "(program (expr (= a (= b c))))");
    check("a - b - c;", "(program (expr (- (- a b) c)))");
    check("a || b && c;", "(program (expr (|| a (&& b c))))");
    check("a | b ^ c & d;", "(program (expr (| a (^ b (& c d)))))");
    check("a == b < c;", "(program (expr (== a (< b c))))");
    check("a << b + c;", "(program (expr (<< a (+ b c))))");
    check("a ?? b;", "(program (expr (?? a b)))");
    check("a ? b : c ? d : e;", "(program (expr (?: a b (?: c d e))))");
    check("a, b, c;", "(program (expr (seq a b c)))");
    check("!a;", "(program (expr (! a)))");
    check("typeof a;", "(program (expr (typeof a)))");
    check("void 0;", "(program (expr (void 0)))");
    check("delete a.b;", "(program (expr (delete (. a b))))");
    check("-a ** 2;", "(program (expr (** (- a) 2)))");
    check("++a;", "(program (expr (pre++ a)))");
    check("a++;", "(program (expr (post++ a)))");
    check("a--;", "(program (expr (post-- a)))");
    check("a instanceof b;", "(program (expr (instanceof a b)))");
    check("a in b;", "(program (expr (in a b)))");
    check("a >>> b;", "(program (expr (>>> a b)))");
    check("a &&= b;", "(program (expr (&&= a b)))");
    check("a ??= b;", "(program (expr (??= a b)))");
    check("a **= b;", "(program (expr (**= a b)))");
    check("a >>>= b;", "(program (expr (>>>= a b)))");
    check("(1 + 2) * 3;", "(program (expr (* (+ 1 2) 3)))");
}

fn testMemberCall() void {
    check("a.b.c;", "(program (expr (. (. a b) c)))");
    check("a[b];", "(program (expr (. a [b])))");
    check("f(1, 2);", "(program (expr (call f 1 2)))");
    check("f(...a);", "(program (expr (call f ...a)))");
    check("new A;", "(program (expr (new A)))");
    check("new A();", "(program (expr (new A)))");
    check("new a.b.C(1);", "(program (expr (new (. (. a b) C) 1)))");
    check("new new A()();", "(program (expr (new (new A))))");
    check("a?.b;", "(program (expr (?. a b)))");
    check("a?.[b];", "(program (expr (?. a [b])))");
    check("a?.(b);", "(program (expr (?call a b)))");
    check("a?.b?.[c]?.(d);", "(program (expr (?call (?. (?. a b) [c]) d)))");
    check("a.if;", "(program (expr (. a if)))");
    ok("new.target;");
    ok("f()()();");
    ok("a.b(c).d[e](f);");
}

fn testStatements() void {
    check(";", "(program (empty))");
    check(";;;", "(program (empty) (empty) (empty))");
    check("if (0) ; else x();", "(program (if 0 (empty) (expr (call x))))");
    check("for (;;) ;", "(program (for _ _ _ (empty)))");
    check("if (a) b(); else c();", "(program (if a (expr (call b)) (expr (call c))))");
    check("{ a; b; }", "(program (block (expr a) (expr b)))");
    check("var a = 1, b;", "(program (var (a 1) (b)))");
    check("let a = 1;", "(program (let (a 1)))");
    check("const a = 1;", "(program (const (a 1)))");
    check("while (a) b();", "(program (while a (expr (call b))))");
    check("do a(); while (b);", "(program (do (expr (call a)) b))");
    check("do a(); while (b)", "(program (do (expr (call a)) b))");
    check("for (var i = 0; i < 10; i++) f(i);", "(program (for (var (i 0)) (< i 10) (post++ i) (expr (call f i))))");
    check("for (i = 0, j = 1; i < j; i++, j--) ;", "(program (for (expr (seq (= i 0) (= j 1))) (< i j) (seq (post++ i) (post-- j)) (empty)))");
    check("for (var k in o) f(k);", "(program (for-in var k o (expr (call f k))))");
    check("for (const v of a) f(v);", "(program (for-of const v a (expr (call f v))))");
    check("for (x of y) ;", "(program (for-of - x y (empty)))");
    check("for (const [k, v] of m) ;", "(program (for-of const (array-pat k v) m (empty)))");
    check("switch (a) { case 1: b(); break; default: c(); case 2: }", "(program (switch a (case 1 (expr (call b)) (break)) (case default (expr (call c))) (case 2)))");
    check("return;", "(program (return))");
    check("return 1;", "(program (return 1))");
    check("return a, b;", "(program (return (seq a b)))");
    check("break;", "(program (break))");
    check("continue;", "(program (continue))");
    check("throw e;", "(program (throw e))");
    check("try { a(); } catch (e) { b(); }", "(program (try (block (expr (call a))) (catch e (block (expr (call b))))))");
    check("try { a(); } catch { b(); }", "(program (try (block (expr (call a))) (catch (block (expr (call b))))))");
    check("try { a(); } finally { b(); }", "(program (try (block (expr (call a))) (finally (block (expr (call b))))))");
    check("try {} catch ({message}) {} finally {}", "(program (try (block) (catch (object-pat (message message)) (block)) (finally (block))))");
    check("done: { break done; }", "(program (label done (block (break done))))");
    check("outer: for (;;) { continue outer; }", "(program (label outer (for _ _ _ (block (continue outer)))))");
    check("lbl: x = 1;", "(program (label lbl (expr (= x 1))))");
    check("debugger;", "(program (empty))");
    ok("for (;;) break;");
    ok("for (let i = 0, n = 10; i < n; ++i) {}");
    ok("l1: l2: while (1) break l1;");
}

fn testAsi() void {
    check("function f() { return\nx }", "(program (fn-decl f (params) (block (return) (expr x))))");
    check("a\nb", "(program (expr a) (expr b))");
    check("a = 1\nb = 2", "(program (expr (= a 1)) (expr (= b 2)))");
    check("a\n++b", "(program (expr a) (expr (pre++ b)))");
    check("{ a }", "(program (block (expr a)))");
    check("a", "(program (expr a))");
    check("var x = 1 /* comment\nwith newline */\nvar y = 2", "(program (var (x 1)) (var (y 2)))");
    check("x = 1 // trailing\ny = 2", "(program (expr (= x 1)) (expr (= y 2)))");
    check("function f(){ break\n}", "(program (fn-decl f (params) (block (break))))");
    ok("do ; while (0) x = 1");
    bad("throw\nx");
    bad("var x = 1 var y = 2");
}

fn testRegex() void {
    check("a / b / c;", "(program (expr (/ (/ a b) c)))");
    check("x = /re/g;", "(program (expr (= x (regex /re/g))))");
    check("if (a) /re/.test(b);", "(program (if a (expr (call (. (regex /re/) test) b))))");
    check("function f(){}\n/re/.test(x);", "(program (fn-decl f (params) (block)) (expr (call (. (regex /re/) test) x)))");
    check("var r = /[/]/;", "(program (var (r (regex /[/]/))))");
    check("var r = /a\\/b/i;", "(program (var (r (regex /a\\/b/i))))");
    check("(1) / 2;", "(program (expr (/ 1 2)))");
    check("a[0] / 2;", "(program (expr (/ (. a [0]) 2)))");
    check("a++ / 2;", "(program (expr (/ (post++ a) 2)))");
    check("return /re/;", "(program (return (regex /re/)))");
    check("typeof /re/;", "(program (expr (typeof (regex /re/))))");
    check("x = a ? /y/ : /z/;", "(program (expr (= x (?: a (regex /y/) (regex /z/)))))");
    check("x /= 2;", "(program (expr (/= x 2)))");
    ok("var re = /\\d+(\\.\\d+)?/g, s = 'a/b';");
    ok("[/a/, /b/];");
    bad("x = /abc");
}

fn testTemplates() void {
    check("`abc`;", "(program (expr (template \"abc\")))");
    check("`a${b}c`;", "(program (expr (template \"a\" b \"c\")))");
    check("`${a}${b}`;", "(program (expr (template \"\" a \"\" b \"\")))");
    check("`a${`x${y}z`}b`;", "(program (expr (template \"a\" (template \"x\" y \"z\") \"b\")))");
    check("`${ {a: 1} }`;", "(program (expr (template \"\" (object (a 1)) \"\")))");
    check("tag`a${b}c`;", "(program (expr (tagged tag \"a\" b \"c\")))");
    check("`a\\nb`;", "(program (expr (template \"a\nb\")))");
    check("`${a, b}`;", "(program (expr (template \"\" (seq a b) \"\")))");
    check("`${f(`${g}`)}`;", "(program (expr (template \"\" (call f (template \"\" g \"\")) \"\")))");
    ok("`${ x ? `y${z}` : `w` }`;");
    ok("String.raw`a\\nb`;");
    bad("`abc");
    bad("`a${b`");
}

fn testFunctions() void {
    check("function f() {}", "(program (fn-decl f (params) (block)))");
    check("function f(a, b) { return a; }", "(program (fn-decl f (params a b) (block (return a))))");
    check("var g = function () {};", "(program (var (g (fn (params) (block)))))");
    check("var g = function h() {};", "(program (var (g (fn h (params) (block)))))");
    check("function f(a = 1, ...rest) {}", "(program (fn-decl f (params (default a 1) (rest rest)) (block)))");
    check("function f({a, b: c}, [d]) {}", "(program (fn-decl f (params (object-pat (a a) (b c)) (array-pat d)) (block)))");
    check("async function f() { await g(); }", "(program (fn-decl async f (params) (block (expr (await (call g))))))");
    check("function* f() { yield 1; yield* g(); }", "(program (fn-decl gen f (params) (block (expr (yield 1)) (expr (yield* (call g))))))");
    check("function* f() { yield; }", "(program (fn-decl gen f (params) (block (expr (yield)))))");
    check("x => x;", "(program (expr (fn arrow (params x) x)))");
    check("(a, b) => {};", "(program (expr (fn arrow (params a b) (block))))");
    check("() => 1;", "(program (expr (fn arrow (params) 1)))");
    check("(a, b);", "(program (expr (seq a b)))");
    check("async x => x;", "(program (expr (fn async arrow (params x) x)))");
    check("async (a) => a;", "(program (expr (fn async arrow (params a) a)))");
    check("async(a);", "(program (expr (call async a)))");
    check("async;", "(program (expr async))");
    check("(a = 1, ...r) => a;", "(program (expr (fn arrow (params (default a 1) (rest r)) a)))");
    check("({a}) => a;", "(program (expr (fn arrow (params (object-pat (a a))) a)))");
    check("a => b => a + b;", "(program (expr (fn arrow (params a) (fn arrow (params b) (+ a b)))))");
    check("(a) => ({b: 1});", "(program (expr (fn arrow (params a) (object (b 1)))))");
    check("f(x => x, 1);", "(program (expr (call f (fn arrow (params x) x) 1)))");
    check("((a)) + 1;", "(program (expr (+ a 1)))");
    ok("(function () {})();");
    ok("!function(){}();");
    ok("(() => {})();");
    ok("var f = async () => { await x; };");
    ok("((a, b) => a)((c) => c);");
}

fn testObjects() void {
    check("({});", "(program (expr (object)))");
    check("({a: 1, b: 2});", "(program (expr (object (a 1) (b 2))))");
    check("({a});", "(program (expr (object (a a))))");
    check("({[k]: v});", "(program (expr (object ([k] v))))");
    check("({m() {}});", "(program (expr (object (m (fn (params) (block))))))");
    check("({*g() {}});", "(program (expr (object (g (fn gen (params) (block))))))");
    check("({async m() {}});", "(program (expr (object (m (fn async (params) (block))))))");
    check("({get x() {}, set x(v) {}});", "(program (expr (object (get x (fn (params) (block))) (set x (fn (params v) (block))))))");
    check("({get: 1, set: 2});", "(program (expr (object (get 1) (set 2))))");
    check("({...a, b: 1});", "(program (expr (object (... a) (b 1))))");
    check("({'a-b': 1, 2: 3});", "(program (expr (object (\"a-b\" 1) (2 3))))");
    check("[...a, b];", "(program (expr (array (... a) b)))");
    check("({default: 1, class: 2, if: 3});", "(program (expr (object (default 1) (class 2) (if 3))))");
    ok("({a: 1, });");
    ok("({[`k${i}`]: v});");
}

fn testClasses() void {
    check("class A {}", "(program (class A))");
    check("class A extends B {}", "(program (class A (extends B)))");
    check("var K = class extends Base {};", "(program (var (K (class-expr (extends Base)))))");
    check("class A { constructor(x) { super(x); } }", "(program (class A (ctor constructor (fn (params x) (block (expr (call super x)))))))");
    check("class A { m() { super.m(); } }", "(program (class A (m (fn (params) (block (expr (call (. super m))))))))");
    check("class A { static m() {} }", "(program (class A (static m (fn (params) (block)))))");
    check("class A { get x() {} set x(v) {} }", "(program (class A (get x (fn (params) (block))) (set x (fn (params v) (block)))))");
    check("class A { [k]() {} }", "(program (class A ([k] (fn (params) (block)))))");
    check("class A { x = 1; }", "(program (class A (field x 1)))");
    check("class A { static x = 1; }", "(program (class A (static field x 1)))");
    check("class A { x; y = 2 }", "(program (class A (field x) (field y 2)))");
    check("class A { *g() {} async m() {} }", "(program (class A (g (fn gen (params) (block))) (m (fn async (params) (block)))))");
    check("class A { static() {} }", "(program (class A (static (fn (params) (block)))))");
    ok("class A { #p = 1; m() { return this.#p; } }");
    ok("class A extends (B || C) {}");
    bad("class A { static { x(); } }");
}

fn testDestructuring() void {
    check("var [a, ...rest] = arr;", "(program (var ((array-pat a (rest rest)) arr)))");
    check("var {x: y = 1, ...others} = o;", "(program (var ((object-pat (x (default y 1)) (rest others)) o)))");
    check("var [a, [b, c]] = d;", "(program (var ((array-pat a (array-pat b c)) d)))");
    check("var {a: {b}} = c;", "(program (var ((object-pat (a (object-pat (b b)))) c)))");
    check("var [, a] = b;", "(program (var ((array-pat hole a) b)))");
    check("var {a = 1} = b;", "(program (var ((object-pat (a (default a 1))) b)))");
    check("var {[k]: v} = o;", "(program (var ((object-pat ([k] v)) o)))");
    check("[a, b] = [b, a];", "(program (expr (= (array-pat a b) (array b a))))");
    check("({a} = o);", "(program (expr (= (object-pat (a a)) o)))");
    check("({a: b.c} = o);", "(program (expr (= (object-pat (a (. b c))) o)))");
    check("[a, ...b] = c;", "(program (expr (= (array-pat a (rest b)) c)))");
    check("[a[0], b.c] = d;", "(program (expr (= (array-pat (. a [0]) (. b c)) d)))");
    check("({...r} = o);", "(program (expr (= (object-pat (rest r)) o)))");
    ok("let {a, b: [c, {d}]} = e;");
    ok("for (const {a, b} of list) {}");
}

fn testMinified() void {
    ok("!function(a,b){var c=a||{},d=[1,2,3].map(function(e){return e*2}),f=c.x?c.y?1:2:3;for(var g=0,h=d.length;g<h;g++)c[d[g]]=g;return b&&b(c,f)}(window,function(o,n){return o&&n});");
    ok("(function(e,t){\"object\"==typeof module?module.exports=t():e.lib=t()})(this,function(){var e={};return e.f=function(t){return t?/^[a-z]+$/i.test(t)?1:0:-1},e.g=(a,b)=>a>b?a:b,e});");
    ok("for(var i=0,l=a.length;i<l;i++)if(a[i])b[i]=a[i];else delete b[i];");
    ok("var x=(a,b)=>({y:a,z:b}),y=x(1,2),z=`${y.y}-${y.z}`,w=[...Object.keys(y)].filter(k=>k!=\"y\").map(k=>`${k}=${y[k]}`).join(\"&\");");
    ok("try{JSON.parse(s)}catch(e){}finally{n++}");
    ok("a?b:c?d:e?f:g;");
    ok("void 0===a&&(a={}),a.b=a.b||[],a.b.push(function(){return!0});");
    ok("do{i++}while(i<10);");
    ok("switch(t){case 1:case 2:x=1;break;default:x=0}");
    ok("new (a.b||c)(d,e);");
    ok("(0,a.b)(c);");
    ok("x=y=>({...y,z:1});");
    ok("if(a)b();else if(c)d();else e();");
    ok("label:for(var k in o){if(!o[k])continue label;f(k)}");
    ok("var s='it\\'s',t=\"say \\\"hi\\\"\",u='\\u00e9\\x41';");
    ok("a=b/c/d,e=/x/g.test(f);");
}

fn testMisc() void {
    check("0.1.toFixed(2);", "(program (expr (call (. 0.1 toFixed) 2)))");
    check("new Foo().bar;", "(program (expr (. (new Foo) bar)))");
    check("new Foo.bar();", "(program (expr (new (. Foo bar))))");
    check("f((a,b) => c);", "(program (expr (call f (fn arrow (params a b) c))))");
    check("x = a?.5:b;", "(program (expr (= x (?: a 0.5 b))))");
    check("a.b`x${y}`;", "(program (expr (tagged (. a b) \"x\" y \"\")))");
    check("x = {get, set};", "(program (expr (= x (object (get get) (set set)))))");
    check("class A { get = 1; static get x() {} }", "(program (class A (field get 1) (static get x (fn (params) (block)))))");
    check("a ** -b;", "(program (expr (** a (- b))))");
    check("for (var i = ('a' in o) ? 0 : 1;;) ;", "(program (for (var (i (?: (in \"a\" o) 0 1))) _ _ (empty)))");
    check("var o = {a: 1 in b};", "(program (var (o (object (a (in 1 b))))))");
    check("x = (a, b) => (c, d) => e;", "(program (expr (= x (fn arrow (params a b) (fn arrow (params c d) e)))))");
    check("(a) => a, (b) => b;", "(program (expr (seq (fn arrow (params a) a) (fn arrow (params b) b))))");
    check("x?.[0]?.y?.();", "(program (expr (?call (?. (?. x [0]) y))))");
    check("!--x;", "(program (expr (! (pre-- x))))");
    check("a = b ? c = 1 : d = 2;", "(program (expr (= a (?: b (= c 1) (= d 2)))))");
    check("label: ;", "(program (label label (empty)))");
    check("({ async *g(){ yield 1 } });", "(program (expr (object (g (fn async gen (params) (block (expr (yield 1))))))))");
    check("class A { static async *m() {} }", "(program (class A (static m (fn async gen (params) (block)))))");
    check("var {a: [b = 1] = []} = c;", "(program (var ((object-pat (a (default (array-pat (default b 1)) (array)))) c)))");
    check("f(a,);", "(program (expr (call f a)))");
    check("(a,) => a;", "(program (expr (fn arrow (params a) a)))");
    check("f(x)/2;", "(program (expr (/ (call f x) 2)))");
    check("while (a) /re/.test(b);", "(program (while a (expr (call (. (regex /re/) test) b))))");
    check("`${\"}\"}`;", "(program (expr (template \"\" \"}\" \"\")))");
    check("`${ {m(){return 1}} }`;", "(program (expr (template \"\" (object (m (fn (params) (block (return 1))))) \"\")))");
    check("x = a++ + ++b;", "(program (expr (= x (+ (post++ a) (pre++ b)))))");
    check("{a: 1}", "(program (block (label a (expr 1))))");
    check("a\n.b\n.c;", "(program (expr (. (. a b) c)))");
    check("w = a || b ?? c;", "(program (expr (= w (?? (|| a b) c))))");
    check("var yield = 1;", "(program (var (yield 1)))");
    check("for await (const x of y) {}", "(program (for-of const x y (block)))");
    check("class A { \"str\"() {} 1() {} }", "(program (class A (\"str\" (fn (params) (block))) (1 (fn (params) (block)))))");
    check("<!-- comment\nx = 1;", "(program (expr (= x 1)))");
    check("new new.target;", "(program (expr (new new.target)))");
    ok("(async () => { for await (const c of s) {} })();");
    ok("(({a: {b: [c]}}) => c)();");
    ok("x = () => () => () => 1;");
    ok("'\\ud83d\\ude00';");
    bad("'\\u{110000}';");
    bad("a ?. 5 : b;");
    bad("else c");
}

fn testErrors() void {
    bad("var = = =");
    bad("\"abc");
    bad("/* foo");
    bad("}");
    bad("function {}");
    bad("a +");
    bad("{");
    bad("[1,2");
    bad("if (");
    bad("var 1 = 2;");
    bad("a ? b;");
    bad("class {");
    bad("for (;;");
    bad("try {}");
    bad("({a:});");
    bad("f(,);");
    bad("a b c =");
    bad("*");
    bad("var x = ;");
    bad("switch (a) { b(); }");
}

fn testDepth(alloc: std.mem.Allocator) void {
    // 10000 nested parens must be rejected, not crash the process.
    {
        var buf = std.ArrayList(u8).init(alloc);
        defer buf.deinit();
        buf.appendNTimes('(', 10000) catch return;
        buf.append('1') catch return;
        buf.appendNTimes(')', 10000) catch return;
        buf.append(';') catch return;
        badLong(alloc, buf.items, "10000 nested parens");
    }
    // nested blocks
    {
        var buf = std.ArrayList(u8).init(alloc);
        defer buf.deinit();
        buf.appendNTimes('{', 10000) catch return;
        buf.appendNTimes('}', 10000) catch return;
        badLong(alloc, buf.items, "10000 nested blocks");
    }
    // nested arrays
    {
        var buf = std.ArrayList(u8).init(alloc);
        defer buf.deinit();
        buf.appendNTimes('[', 5000) catch return;
        buf.appendNTimes(']', 5000) catch return;
        buf.append(';') catch return;
        badLong(alloc, buf.items, "5000 nested arrays");
    }
    // unterminated: 10000 open parens and nothing else
    {
        var buf = std.ArrayList(u8).init(alloc);
        defer buf.deinit();
        buf.appendNTimes('(', 10000) catch return;
        badLong(alloc, buf.items, "10000 unclosed parens");
    }
    // a long but shallow program must still parse
    {
        var buf = std.ArrayList(u8).init(alloc);
        defer buf.deinit();
        var i: usize = 0;
        while (i < 5000) : (i += 1) {
            buf.appendSlice("a=a+1;") catch return;
        }
        okLong(alloc, buf.items, "5000 shallow statements");
    }
    // long chained call/member, shallow in the parser but long in the lexer
    {
        var buf = std.ArrayList(u8).init(alloc);
        defer buf.deinit();
        buf.appendSlice("x") catch return;
        var i: usize = 0;
        while (i < 3000) : (i += 1) {
            buf.appendSlice(".a(1)") catch return;
        }
        buf.append(';') catch return;
        okLong(alloc, buf.items, "3000 chained calls");
    }
}

fn testErrorPositions() void {
    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    const src = "var a = 1;\nvar b = ;\n";
    const o = run(arena.allocator(), src);
    if (o.text != null) {
        failed += 1;
        report("FAIL error-position test: unexpectedly parsed\n", .{});
        return;
    }
    if (o.err) |e| {
        if (e.line == 2 and e.column == 9) {
            passed += 1;
        } else {
            failed += 1;
            report("FAIL error-position test: got {d}:{d} ({s})\n", .{ e.line, e.column, e.message });
        }
    } else {
        failed += 1;
        report("FAIL error-position test: no error set\n", .{});
    }
}

/// `zig run parser_test.zig -- "<source>"` dumps the tree for one snippet
/// instead of running the suite.  Handy while iterating.
fn dumpOne(src: []const u8) void {
    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    const o = run(arena.allocator(), src);
    if (o.text) |t| {
        report("{s}\n", .{t});
    } else if (o.err) |e| {
        report("ERROR {d}:{d}: {s}\n", .{ e.line, e.column, e.message });
    } else {
        report("ERROR (no message)\n", .{});
    }
}

pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const alloc = gpa.allocator();

    {
        var args = try std.process.argsWithAllocator(alloc);
        defer args.deinit();
        _ = args.skip();
        if (args.next()) |snippet| {
            dumpOne(snippet);
            return;
        }
    }

    testLiterals();
    testOperators();
    testMemberCall();
    testStatements();
    testAsi();
    testRegex();
    testTemplates();
    testFunctions();
    testObjects();
    testClasses();
    testDestructuring();
    testMinified();
    testMisc();
    testErrors();
    testErrorPositions();
    testDepth(alloc);

    report("\n{d} passed, {d} failed ({d} total)\n", .{ passed, failed, passed + failed });
    if (failed > 0) std.process.exit(1);
}
