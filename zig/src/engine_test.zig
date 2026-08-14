//! End-to-end tests: source in, global out. These are the engine's own
//! smoke tests; the browser's `tests/test_js.py` is the real gate.
const std = @import("std");
const Vm = @import("vm.zig").Vm;

fn expectNumber(src: []const u8, name: []const u8, want: f64) !void {
    const vm = try Vm.create(std.testing.allocator);
    defer vm.destroy();
    _ = vm.evaluate(src, "test") catch {
        std.debug.print("threw: {s}\n", .{src});
        return error.Threw;
    };
    vm.drainJobs();
    const v = try vm.getProp(.{ .object = vm.globals }, name);
    if (v != .number or v.number != want) {
        std.debug.print("{s}: got {any}, want {d}\n", .{ name, v, want });
        return error.Mismatch;
    }
}

fn expectString(src: []const u8, name: []const u8, want: []const u8) !void {
    const vm = try Vm.create(std.testing.allocator);
    defer vm.destroy();
    _ = vm.evaluate(src, "test") catch return error.Threw;
    vm.drainJobs();
    const v = try vm.getProp(.{ .object = vm.globals }, name);
    if (v != .string) return error.NotAString;
    try std.testing.expectEqualStrings(want, v.string.bytes);
}

test "arithmetic and precedence" {
    try expectNumber("var a = 1 + 2 * 3;", "a", 7);
}

test "closures keep outer state" {
    try expectNumber(
        \\function mk() { var n = 0; return function () { n += 1; return n; }; }
        \\var c = mk(); c(); var inc = c();
    , "inc", 2);
}

test "string concatenation" {
    try expectString("var s = \"foo\" + \"bar\";", "s", "foobar");
}

test "array coercion" {
    try expectString("var d = [] + []; var e = [] + 5;", "e", "5");
}

test "for loop with continue" {
    try expectNumber(
        \\var total = 0;
        \\for (var i = 0; i < 5; i++) { if (i === 2) { continue; } total += i; }
    , "total", 8);
}

test "classes and super" {
    try expectNumber(
        \\function Base(n) { this.n = n; }
        \\Base.prototype.double = function () { return this.n * 2; };
        \\var K = class extends Base { constructor(n) { super(n); }
        \\  quad() { return this.double() * 2; } };
        \\var q = new K(21).quad();
    , "q", 84);
}

test "try catch finally" {
    try expectString(
        \\var out; try { throw "err"; } catch (e) { out = e; }
    , "out", "err");
}

test "promises and microtasks" {
    try expectNumber(
        \\var out = 0;
        \\Promise.resolve(2).then(function (v) { return v * 3; })
        \\  .then(function (v) { out = v; });
    , "out", 6);
}

test "async await" {
    try expectNumber(
        \\var out = 0;
        \\(async function () { var x = await Promise.resolve(6);
        \\  var y = await Promise.resolve(7); out = x * y; })();
    , "out", 42);
}

test "labelled continue" {
    try expectNumber(
        \\var n = 0;
        \\outer: for (var i = 0; i < 3; i++) {
        \\  for (var j = 0; j < 3; j++) { if (j == 1) continue outer; n += 1; }
        \\}
    , "n", 3);
}

test "regex and string methods" {
    try expectString("var r = \"abc\".replace(/b/, \"X\");", "r", "aXc");
}

test "json round trip" {
    try expectString("var s = JSON.stringify({a:1});", "s", "{\"a\":1}");
}

test "destructuring and spread" {
    try expectNumber("var [a, ...rest] = [1,2,3]; var n = rest.length;", "n", 2);
}

test "switch with break" {
    try expectString(
        \\var sw = "none"; var c = 2;
        \\switch (c) { case 1: sw = "one"; break; case 2: sw = "two"; break; }
    , "sw", "two");
}

test "template literals nest" {
    try expectString("var t = `a${`b${3}c`}d`;", "t", "ab3cd");
}

test "optional chaining and nullish" {
    try expectNumber("var ch = ({a:{b:5}}).a?.b;", "ch", 5);
}

test "timers fire in order" {
    const vm = try Vm.create(std.testing.allocator);
    defer vm.destroy();
    _ = try vm.evaluate(
        \\var out = 0;
        \\setTimeout(function(){ out = 1; }, 50);
        \\setTimeout(function(){ out = 2; }, 10);
    , "test");
    vm.advance(20);
    vm.drainJobs();
    var v = try vm.getProp(.{ .object = vm.globals }, "out");
    try std.testing.expectEqual(@as(f64, 2), v.number);
    vm.advance(100);
    vm.drainJobs();
    v = try vm.getProp(.{ .object = vm.globals }, "out");
    try std.testing.expectEqual(@as(f64, 1), v.number);
}

test "collection survives a live cycle" {
    const vm = try Vm.create(std.testing.allocator);
    defer vm.destroy();
    _ = try vm.evaluate(
        \\var keep = [];
        \\for (var i = 0; i < 2000; i++) {
        \\  var a = {}; var b = { other: a }; a.other = b;
        \\  if (i % 500 === 0) keep.push(a);
        \\}
        \\var n = keep.length;
    , "test");
    vm.collect();
    const v = try vm.getProp(.{ .object = vm.globals }, "n");
    try std.testing.expectEqual(@as(f64, 4), v.number);
}
