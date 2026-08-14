//! Standalone test suite for regex.zig.
//!
//!     zig run zig/src/regex_test.zig
//!
//! Prints a pass/fail count and exits nonzero if anything failed.

const std = @import("std");
const re = @import("regex.zig");

var passed: u32 = 0;
var failed: u32 = 0;
var alloc: std.mem.Allocator = undefined;

fn ok(cond: bool, comptime fmt: []const u8, args: anytype) void {
    if (cond) {
        passed += 1;
    } else {
        failed += 1;
        std.debug.print("FAIL: " ++ fmt ++ "\n", args);
    }
}

fn showOpt(s: ?[]const u8) []const u8 {
    return s orelse "<null>";
}

// -- helpers ---------------------------------------------------------------

const Res = struct {
    buf: [40]?re.Span,
    n: u32,
    input: []const u8,
    matched: bool,

    fn group(self: *const Res, i: u32) ?[]const u8 {
        if (i > self.n) return null;
        const sp = self.buf[i] orelse return null;
        return self.input[sp.start..sp.end];
    }
};

fn execAt(pat: []const u8, flags: []const u8, input: []const u8, start: u32) ?Res {
    var r = re.Regex.compile(alloc, pat, flags) catch {
        failed += 1;
        std.debug.print("FAIL: /{s}/{s} did not compile\n", .{ pat, flags });
        return null;
    };
    defer r.deinit(alloc);

    var res = Res{ .buf = undefined, .n = r.group_count, .input = input, .matched = false };
    res.matched = r.exec(input, start, res.buf[0 .. r.group_count + 1]);
    return res;
}

/// Whole-match assertion. `want` of null means "must not match".
fn expectMatch(pat: []const u8, flags: []const u8, input: []const u8, want: ?[]const u8) void {
    const res = execAt(pat, flags, input, 0) orelse return;
    if (want) |w| {
        if (!res.matched) {
            failed += 1;
            std.debug.print("FAIL: /{s}/{s} on \"{s}\": no match, wanted \"{s}\"\n", .{ pat, flags, input, w });
            return;
        }
        const got = res.group(0).?;
        ok(std.mem.eql(u8, got, w), "/{s}/{s} on \"{s}\": got \"{s}\", wanted \"{s}\"", .{ pat, flags, input, got, w });
    } else {
        ok(!res.matched, "/{s}/{s} on \"{s}\": matched \"{s}\", wanted no match", .{ pat, flags, input, showOpt(res.group(0)) });
    }
}

fn expectGroup(pat: []const u8, flags: []const u8, input: []const u8, idx: u32, want: ?[]const u8) void {
    const res = execAt(pat, flags, input, 0) orelse return;
    if (!res.matched) {
        failed += 1;
        std.debug.print("FAIL: /{s}/{s} on \"{s}\": no match (wanted group {d})\n", .{ pat, flags, input, idx });
        return;
    }
    const got = res.group(idx);
    if (want == null and got == null) {
        passed += 1;
        return;
    }
    if (want == null or got == null or !std.mem.eql(u8, got.?, want.?)) {
        failed += 1;
        std.debug.print("FAIL: /{s}/{s} on \"{s}\": group {d} = \"{s}\", wanted \"{s}\"\n", .{ pat, flags, input, idx, showOpt(got), showOpt(want) });
        return;
    }
    passed += 1;
}

fn expectStart(pat: []const u8, flags: []const u8, input: []const u8, from: u32, want_start: ?u32) void {
    const res = execAt(pat, flags, input, from) orelse return;
    if (want_start) |ws| {
        if (!res.matched) {
            failed += 1;
            std.debug.print("FAIL: /{s}/{s} from {d} on \"{s}\": no match\n", .{ pat, flags, from, input });
            return;
        }
        const got = res.buf[0].?.start;
        ok(got == ws, "/{s}/{s} from {d} on \"{s}\": start {d}, wanted {d}", .{ pat, flags, from, input, got, ws });
    } else {
        ok(!res.matched, "/{s}/{s} from {d} on \"{s}\": matched, wanted no match", .{ pat, flags, from, input });
    }
}

fn expectBad(pat: []const u8, flags: []const u8) void {
    if (re.Regex.compile(alloc, pat, flags)) |r| {
        var m = r;
        m.deinit(alloc);
        failed += 1;
        std.debug.print("FAIL: /{s}/{s} compiled, wanted error.BadPattern\n", .{ pat, flags });
    } else |e| {
        ok(e == error.BadPattern, "/{s}/{s}: got {any}, wanted error.BadPattern", .{ pat, flags, e });
    }
}

/// How many matches a /g-style scan finds.
fn countMatches(pat: []const u8, flags: []const u8, input: []const u8) u32 {
    var r = re.Regex.compile(alloc, pat, flags) catch {
        failed += 1;
        std.debug.print("FAIL: /{s}/{s} did not compile\n", .{ pat, flags });
        return 0;
    };
    defer r.deinit(alloc);

    var caps: [40]?re.Span = undefined;
    var n: u32 = 0;
    var at: u32 = 0;
    while (at <= input.len) {
        if (!r.exec(input, at, caps[0 .. r.group_count + 1])) break;
        n += 1;
        const sp = caps[0].?;
        at = if (sp.end == sp.start) sp.end + 1 else sp.end;
        if (n > 1000) break;
    }
    return n;
}

fn expectCount(pat: []const u8, flags: []const u8, input: []const u8, want: u32) void {
    const got = countMatches(pat, flags, input);
    ok(got == want, "/{s}/{s} on \"{s}\": {d} matches, wanted {d}", .{ pat, flags, input, got, want });
}

/// String.prototype.replace with a literal replacement, first match only.
fn replaceFirst(pat: []const u8, flags: []const u8, input: []const u8, with: []const u8, out: []u8) ?[]const u8 {
    var r = re.Regex.compile(alloc, pat, flags) catch return null;
    defer r.deinit(alloc);
    var caps: [40]?re.Span = undefined;
    if (!r.exec(input, 0, caps[0 .. r.group_count + 1])) return input;
    const sp = caps[0].?;
    var i: usize = 0;
    @memcpy(out[i .. i + sp.start], input[0..sp.start]);
    i += sp.start;
    @memcpy(out[i .. i + with.len], with);
    i += with.len;
    @memcpy(out[i .. i + input.len - sp.end], input[sp.end..]);
    i += input.len - sp.end;
    return out[0..i];
}

// -- the suite -------------------------------------------------------------

fn testLiterals() void {
    expectMatch("abc", "", "abc", "abc");
    expectMatch("abc", "", "xxabcyy", "abc");
    expectMatch("abc", "", "abd", null);
    expectMatch("", "", "abc", "");
    expectMatch("a", "i", "A", "a"[0..0] ++ "A"); // case-insensitive literal
    expectMatch("ABC", "i", "xabcx", "abc");
    expectMatch("\u{00e9}", "", "caf\u{00e9}", "\u{00e9}");
}

fn testDot() void {
    expectMatch(".", "", "a", "a");
    expectMatch("a.c", "", "abc", "abc");
    expectMatch("a.c", "", "a\nc", null);
    expectMatch("a.c", "s", "a\nc", "a\nc");
    expectMatch("a.c", "", "a\rc", null);
    // `.` consumes a whole code point, never half of one.
    expectMatch("^.$", "", "\u{00e9}", "\u{00e9}");
    expectMatch("^..$", "", "\u{4f60}\u{597d}", "\u{4f60}\u{597d}");
}

fn testClasses() void {
    expectMatch("[abc]+", "", "xxcabzz", "cab");
    expectMatch("[a-z]+", "", "12abcDE", "abc");
    expectMatch("[^a-z]+", "", "abc123abc", "123");
    expectMatch("[-a]+", "", "x-a-y", "-a-");
    expectMatch("[a-]+", "", "x-a-y", "-a-");
    // `[]` is the empty class, so `[]]` is "match nothing, then ']'": never matches.
    expectMatch("[]]", "", "]", null);
    expectMatch("[\\]]", "", "]", "]");
    // `[` inside a class is an ordinary member.
    expectMatch("[[a]+", "", "x[a]", "[a");
    expectMatch("[\\n]", "", "\n", "\n");
    expectMatch("[\\x41]", "", "A", "A");
    expectMatch("[a\\-z]", "", "-", "-");
    expectMatch("[A-Z]", "i", "a", "a");
    // canonicalise-then-complement: [^a]/i must reject 'A'
    expectMatch("[^a]", "i", "aA", null);
    expectMatch("[^a]", "i", "aAb", "b");
    expectMatch("[\\d]+", "", "ab123", "123");
    expectMatch("[\\D]+", "", "12ab3", "ab");
    expectMatch("[\\w]+", "", " a_1 ", "a_1");
    expectMatch("[\\W]+", "", "ab  cd", "  ");
    expectMatch("[\\s]+", "", "a \t b", " \t ");
    expectMatch("[\\S]+", "", "  ab  ", "ab");
    expectMatch("[\\d\\s]+", "", "x1 2y", "1 2");
    expectMatch("[^]", "", "\n", "\n");
    expectMatch("[]", "", "a", null);
    expectMatch("[\\b]", "", "\x08", "\x08");
}

fn testPredefined() void {
    expectMatch("\\d+", "", "ab123cd", "123");
    expectMatch("\\D+", "", "12abc34", "abc");
    expectMatch("\\w+", "", " -foo_1- ", "foo_1");
    expectMatch("\\W+", "", "ab..cd", "..");
    expectMatch("\\s+", "", "a \t\nb", " \t\n");
    expectMatch("\\S+", "", "   xy  ", "xy");
    expectMatch("\\d", "", "abc", null);
    // non-ASCII is not a word char, is not a digit, is matched by \W and \D
    expectMatch("\\W", "", "a\u{00e9}b", "\u{00e9}");
    expectMatch("\\s", "", "a\u{00a0}b", "\u{00a0}");
}

fn testEscapes() void {
    expectMatch("a\\nb", "", "a\nb", "a\nb");
    expectMatch("a\\rb", "", "a\rb", "a\rb");
    expectMatch("a\\tb", "", "a\tb", "a\tb");
    expectMatch("\\f", "", "\x0C", "\x0C");
    expectMatch("\\v", "", "\x0B", "\x0B");
    expectMatch("\\0", "", "\x00", "\x00");
    expectMatch("\\x41\\x42", "", "AB", "AB");
    expectMatch("\\u0041", "", "A", "A");
    expectMatch("\\u{1F600}", "", "x\u{1F600}y", "\u{1F600}");
    expectMatch("\\u00e9", "", "\u{00e9}", "\u{00e9}");
    expectMatch("a\\.c", "", "abc", null);
    expectMatch("a\\.c", "", "a.c", "a.c");
    expectMatch("\\/", "", "/", "/");
    expectMatch("\\\\", "", "\\", "\\");
    expectMatch("\\$\\^\\+\\*\\?\\(\\)\\[\\]\\{\\}\\|", "", "$^+*?()[]{}|", "$^+*?()[]{}|");
    expectMatch("\\cA", "", "\x01", "\x01");
    // Malformed \x and \u degrade to identity escapes rather than throwing.
    expectMatch("\\x4", "", "x4", "x4");
    expectMatch("\\u12", "", "u12", "u12");
    expectMatch("\\u{}", "", "u{}", "u{}");
    expectMatch("\\u{zz}", "", "u{zz}", "u{zz}");
    // Out of range, so `\u` degrades to 'u' and `{110000}` becomes a
    // quantifier on it -- 110000 u's, which "u{110000}" is not. V8 agrees.
    expectMatch("\\u{110000}", "", "u{110000}", null);
}

fn testAnchors() void {
    expectMatch("^abc", "", "abc", "abc");
    expectMatch("^abc", "", "xabc", null);
    expectMatch("abc$", "", "abc", "abc");
    expectMatch("abc$", "", "abcx", null);
    expectMatch("^ab+c$", "", "abbbc", "abbbc");
    expectMatch("^ab+c$", "", "abbbcd", null);
    expectMatch("^$", "", "", "");
    // multiline
    expectMatch("^b", "", "a\nb", null);
    expectMatch("^b", "m", "a\nb", "b");
    expectMatch("a$", "", "a\nb", null);
    expectMatch("a$", "m", "a\nb", "a");
    expectMatch("^b$", "m", "a\nb\nc", "b");
    expectMatch("^", "m", "a\rb", "");
    expectCount("^", "m", "a\nb\nc", 3);
    expectCount("$", "m", "a\nb", 2);
}

fn testWordBoundary() void {
    expectMatch("\\bfoo\\b", "", "a foo b", "foo");
    expectMatch("\\bfoo\\b", "", "afoob", null);
    expectMatch("\\bfoo", "", "foo", "foo"); // boundary at string start
    expectMatch("foo\\b", "", "foo", "foo"); // boundary at string end
    expectMatch("\\b", "", "", null); // empty string has no boundary
    expectMatch("\\B", "", "", ""); // ... so \B holds there
    expectMatch("\\Bar", "", "bar", "ar");
    expectMatch("\\Bar", "", " ar", null);
    expectMatch("a\\Bb", "", "ab", "ab");
    expectCount("\\b", "", "ab cd", 4);
}

fn testQuantifiers() void {
    expectMatch("a*", "", "aaa", "aaa");
    expectMatch("a*", "", "bbb", "");
    expectMatch("a+", "", "caaa", "aaa");
    expectMatch("a+", "", "ccc", null);
    expectMatch("ab?c", "", "ac", "ac");
    expectMatch("ab?c", "", "abc", "abc");
    expectMatch("a{3}", "", "aaaaa", "aaa");
    expectMatch("a{3}", "", "aa", null);
    expectMatch("a{2,}", "", "aaaa", "aaaa");
    expectMatch("a{2,3}", "", "aaaaa", "aaa");
    expectMatch("a{0}b", "", "b", "b");
    // lazy
    expectMatch("a+?", "", "aaa", "a");
    expectMatch("a*?", "", "aaa", "");
    expectMatch("a{2,4}?", "", "aaaa", "aa");
    expectMatch("<.+>", "", "<a><b>", "<a><b>");
    expectMatch("<.+?>", "", "<a><b>", "<a>");
    expectMatch("ab??", "", "ab", "a");
    // `{` that is not a quantifier is a literal
    expectMatch("a{", "", "a{", "a{");
    expectMatch("a{,3}", "", "a{,3}", "a{,3}");
    expectMatch("a{2", "", "a{2", "a{2");
    expectMatch("{}", "", "{}", "{}");
    // nested quantifiers, empty-body loops must terminate
    expectMatch("(?:a*)*", "", "aaa", "aaa");
    expectMatch("(?:a*)*b", "", "aaab", "aaab");
    expectMatch("(?:)*", "", "abc", "");
    expectMatch("(?:a?)*", "", "b", "");
}

fn testGroups() void {
    expectGroup("(a)(b)", "", "ab", 1, "a");
    expectGroup("(a)(b)", "", "ab", 2, "b");
    expectGroup("(a)|(b)", "", "b", 1, null); // non-participating group
    expectGroup("(a)|(b)", "", "b", 2, "b");
    expectGroup("(a(b))", "", "ab", 2, "b");
    expectMatch("(?:ab)+", "", "ababc", "abab");
    expectGroup("(?:a)(b)", "", "ab", 1, "b");
    expectGroup("(a)?b", "", "b", 1, null);
    expectGroup("(a)?b", "", "ab", 1, "a");
    expectGroup("(a+)(a)", "", "aaa", 1, "aa");
    // named groups
    expectGroup("(?<x>a)(?<y>b)", "", "ab", 1, "a");
    expectGroup("(?<x>a)(?<y>b)", "", "ab", 2, "b");
    // per-iteration capture reset: /(?:(a)|b)+/ on "ab" leaves group 1 unset
    expectGroup("(?:(a)|b)+", "", "ab", 1, null);
    expectMatch("(?:(a)|b)+", "", "ab", "ab");

    var r = re.Regex.compile(alloc, "(?<year>\\d{4})-(?<month>\\d{2})", "") catch {
        failed += 1;
        return;
    };
    defer r.deinit(alloc);
    ok(r.group_count == 2, "group_count = {d}, wanted 2", .{r.group_count});
    ok(r.group_names.len == 3, "group_names.len = {d}, wanted 3", .{r.group_names.len});
    ok(std.mem.eql(u8, r.group_names[1], "year"), "group_names[1] = \"{s}\"", .{r.group_names[1]});
    ok(std.mem.eql(u8, r.group_names[2], "month"), "group_names[2] = \"{s}\"", .{r.group_names[2]});
    ok(r.groupIndex("month") == 2, "groupIndex(\"month\") wrong", .{});
    var caps: [8]?re.Span = undefined;
    ok(r.exec("on 2024-07-01", 0, caps[0..3]), "named-group exec failed", .{});
    ok(caps[1] != null and std.mem.eql(u8, "2024", "on 2024-07-01"[caps[1].?.start..caps[1].?.end]), "year capture wrong", .{});
}

fn testLookaround() void {
    expectMatch("a(?=b)", "", "ab", "a");
    expectMatch("a(?=b)", "", "ac", null);
    expectMatch("a(?!b)", "", "ac", "a");
    expectMatch("a(?!b)", "", "ab", null);
    expectMatch("\\d+(?= dollars)", "", "50 dollars", "50");
    expectMatch("(?<=\\$)\\d+", "", "$42", "42");
    expectMatch("(?<=\\$)\\d+", "", "#42", null);
    expectMatch("(?<!\\$)\\d+", "", "#42", "42");
    expectMatch("(?<!\\$)\\d+", "", "$42", "2"); // "42" is blocked, "2" is not
    expectMatch("(?<=ab)c", "", "abc", "c");
    expectMatch("(?<=ab)c", "", "xbc", null);
    expectMatch("(?<=^)a", "", "a", "a");
    expectMatch("(?<=a|b)c", "", "bc", "c"); // same-width alternation is fine
    // captures inside a positive lookahead survive
    expectGroup("(?=(b))b", "", "b", 1, "b");
    // negative lookbehind at string start has nothing behind it: assertion holds
    expectMatch("(?<!x)a", "", "a", "a");
    // variable-width lookbehind is rejected at compile time
    expectBad("(?<=a+)b", "");
    expectBad("(?<=ab|c)d", "");
}

fn testBackrefs() void {
    expectMatch("(a)\\1", "", "aa", "aa");
    expectMatch("(a)\\1", "", "ab", null);
    expectMatch("(\\w+) \\1", "", "hello hello world", "hello hello");
    expectMatch("(\\w+) \\1", "", "hello world", null);
    expectMatch("(a)(b)\\2\\1", "", "abba", "abba");
    expectMatch("(a)\\1", "i", "aA", "aA");
    expectMatch("(?<w>ab)-\\k<w>", "", "ab-ab", "ab-ab");
    expectMatch("(?<w>ab)-\\k<w>", "", "ab-cd", null);
    // a backref to a group that did not participate matches the empty string
    expectMatch("(?:(a)|b)\\1c", "", "bc", "bc");
    expectBad("\\k<nope>", "");
    // A backref with no such group falls back to Annex B legacy octal, as V8 does.
    expectMatch("\\1", "", "\x01", "\x01");
    expectMatch("(a)\\2", "", "a\x02", "a\x02");
    expectMatch("\\8", "", "8", "8");
    expectMatch("\\012", "", "\n", "\n");
}

fn testAlternation() void {
    // leftmost alternative wins
    expectMatch("a|ab", "", "ab", "a");
    expectMatch("ab|a", "", "ab", "ab");
    expectMatch("abc|abd", "", "abd", "abd");
    expectMatch("x|", "", "y", "");
    expectMatch("|x", "", "x", "");
    expectMatch("(?:|a)b", "", "ab", "ab"); // empty branch, then backtrack
    expectMatch("cat|dog", "", "hotdog", "dog");
    expectMatch("^(?:a|ab)c$", "", "abc", "abc"); // backtracking into alternation
}

fn testFlags() void {
    expectMatch("ABC", "i", "abc", "abc");
    expectMatch("[a-f]+", "i", "ABCDEF", "ABCDEF");
    expectMatch("\\w+", "iu", "abc", "abc"); // `u` is accepted
    expectStart("b", "y", "ab", 0, null); // sticky: only tries `start`
    expectStart("b", "y", "ab", 1, 1);
    expectStart("b", "", "ab", 0, 1); // non-sticky scans forward
    expectBad("a", "q");
    expectBad("a", "gg");
    expectMatch("a", "gimsyu", "a", "a");
}

fn testBrowserSuiteCases() void {
    // /a+/.test("caaa") is true and .exec("caaa")[0] is "aaa"
    expectMatch("a+", "", "caaa", "aaa");

    // "abc".replace(/b/, "X") === "aXc"
    var buf: [64]u8 = undefined;
    const got = replaceFirst("b", "", "abc", "X", &buf);
    ok(got != null and std.mem.eql(u8, got.?, "aXc"), "replace: got \"{s}\", wanted \"aXc\"", .{showOpt(got)});

    // "a1b2".match(/\d/g).length === 2
    expectCount("\\d", "g", "a1b2", 2);

    // /^ab+c$/.test("abbbc") is true
    expectMatch("^ab+c$", "", "abbbc", "abbbc");

    // /a/ compiles and reports zero groups (the shape RegExp objects need)
    var r = re.Regex.compile(alloc, "a", "") catch {
        failed += 1;
        return;
    };
    defer r.deinit(alloc);
    ok(r.group_count == 0, "/a/ group_count = {d}, wanted 0", .{r.group_count});
    ok(r.group_names.len == 1, "/a/ group_names.len = {d}, wanted 1", .{r.group_names.len});
    ok(!r.flags.global and !r.flags.ignore_case, "/a/ flags should all be off", .{});
}

fn testExecStart() void {
    expectStart("a", "", "bbba", 0, 3);
    expectStart("a", "", "abca", 1, 3);
    expectStart("a", "", "abc", 1, null);
    expectStart("^a", "", "aa", 1, null); // ^ still means string start
    expectStart("", "", "abc", 3, 3); // empty match at the very end
    // `start` past the end is not a crash
    const res = execAt("a", "", "abc", 99);
    ok(res != null and !res.?.matched, "start past end should just fail", .{});
}

fn testCaseInsensitive() void {
    expectMatch("hello world", "i", "Hello World", "Hello World");
    expectMatch("[^abc]+", "i", "ABCdef", "def");
    expectMatch("\\bFOO\\b", "i", "a foo b", "foo");
    expectMatch("(FOO)\\1", "i", "fooFOO", "fooFOO");
    // folding is ASCII-only, on purpose
    expectMatch("\u{00c9}", "i", "\u{00e9}", null);
}

fn testMalformed() void {
    expectBad("(", "");
    expectBad("[a", "");
    expectBad("a{2,1}", "");
    expectBad("\\", "");
    expectBad(")", "");
    expectBad("a)", "");
    expectBad("*", "");
    expectBad("+", "");
    expectBad("?", "");
    expectBad("a**", "");
    expectBad("{2}", "");
    expectBad("(?<a>x", "");
    expectBad("(?", "");
    expectBad("(?P<a>x)", "");
    expectBad("[z-a]", "");
    expectBad("(a", "");
    expectBad("a[b", "");
    expectBad("(?<>a)", "");
}

fn testNesting() void {
    // Reasonable nesting works; absurd nesting is rejected instead of
    // overflowing the parser's stack.
    var buf: [4096]u8 = undefined;
    for ([_]u32{ 50, 199, 201, 1000 }) |n| {
        var i: usize = 0;
        while (i < n) : (i += 1) buf[i] = '(';
        buf[n] = 'a';
        i = 0;
        while (i < n) : (i += 1) buf[n + 1 + i] = ')';
        const pat = buf[0 .. n * 2 + 1];
        if (re.Regex.compile(alloc, pat, "")) |r| {
            var m = r;
            defer m.deinit(alloc);
            var caps: [2048]?re.Span = undefined;
            const hit = m.exec("a", 0, caps[0 .. m.group_count + 1]);
            ok(n <= 200 and hit, "nesting {d}: expected a match", .{n});
        } else |e| {
            ok(n > 200 and e == error.BadPattern, "nesting {d}: got {any}", .{ n, e });
        }
    }
}

fn testNoPanicOnGarbage() void {
    // Every one of these must either compile or return BadPattern, never trap.
    const pats = [_][]const u8{
        "",              "a",              "((((((((((a))))))))))",
        "(?:(?:(?:a)))", "[^\\W\\S]",      "a{0,0}",
        "(a*)*",         "(a|)+",          "[\\s\\S]*",
        "\\u{10FFFF}",   "(?<=(?<=a)b)c",  "(?!)",
        "a$^b",          "[a-z0-9_.+-]+",  "\\p{L}",
        "[",             "]",              "}",
        "\\8",           "(?<n>a)(?<n>b)", "\u{4f60}+",
    };
    var bad: u32 = 0;
    for (pats) |p| {
        if (re.Regex.compile(alloc, p, "")) |r| {
            var m = r;
            var caps: [40]?re.Span = undefined;
            _ = m.exec("some \u{4f60} arbitrary \x00 haystack 123", 0, caps[0 .. m.group_count + 1]);
            m.deinit(alloc);
        } else |e| {
            if (e != error.BadPattern) bad += 1;
        }
    }
    ok(bad == 0, "garbage patterns produced {d} non-BadPattern errors", .{bad});
}

fn testUtf8Integrity() void {
    const s = "a\u{4f60}\u{597d}b";
    // A greedy `.` run must land on code-point boundaries when it backtracks.
    expectMatch(".+b", "", s, s);
    expectMatch(".+?b", "", s, s);
    expectMatch("a.+", "", s, s);
    // Character classes see whole code points, not bytes.
    expectMatch("[\u{4f60}]", "", s, "\u{4f60}");
    expectMatch("\u{4f60}\u{597d}", "", s, "\u{4f60}\u{597d}");
    expectCount(".", "g", "\u{4f60}\u{597d}", 2);
    // Verify each reported match slice is valid UTF-8.
    var r = re.Regex.compile(alloc, ".{1,3}", "") catch {
        failed += 1;
        return;
    };
    defer r.deinit(alloc);
    var caps: [4]?re.Span = undefined;
    var at: u32 = 0;
    var all_valid = true;
    while (at < s.len) {
        if (!r.exec(s, at, caps[0..1])) break;
        const sp = caps[0].?;
        if (!std.unicode.utf8ValidateSlice(s[sp.start..sp.end])) all_valid = false;
        at = if (sp.end == sp.start) sp.end + 1 else sp.end;
    }
    ok(all_valid, "a match slice was not valid UTF-8", .{});
}

fn testLongInput() void {
    // A long simple quantifier must not put a frame per character on the stack.
    var buf: [200000]u8 = undefined;
    @memset(&buf, 'a');
    buf[buf.len - 1] = 'b';
    expectMatch("a+b", "", &buf, &buf);
    expectMatch("^a*b$", "", &buf, &buf);
    expectMatch(".*b", "", &buf, &buf);
    expectMatch("a*?b", "", &buf, &buf);
}

fn testCatastrophic() !u64 {
    var a30: [31]u8 = undefined;
    @memset(a30[0..30], 'a');
    a30[30] = 'c'; // never a 'b', so /(a+)+b/ can never succeed

    var timer = try std.time.Timer.start();
    const res = execAt("(a+)+b", "", a30[0..31], 0);
    const ns = timer.read();

    ok(res != null and !res.?.matched, "/(a+)+b/ on 30 a's should not match", .{});
    ok(ns < 1_000_000_000, "/(a+)+b/ took {d} ns, wanted well under 1s", .{ns});

    // A few more classic bombs, all of which must return promptly.
    var t2 = try std.time.Timer.start();
    _ = execAt("(a|aa)+$", "", a30[0..31], 0);
    _ = execAt("(a*)*b", "", a30[0..31], 0);
    _ = execAt("(x+x+)+y", "", a30[0..31], 0);
    const ns2 = t2.read();
    ok(ns2 < 3_000_000_000, "extra bombs took {d} ns", .{ns2});

    return ns;
}

pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    alloc = gpa.allocator();

    testLiterals();
    testDot();
    testClasses();
    testPredefined();
    testEscapes();
    testAnchors();
    testWordBoundary();
    testQuantifiers();
    testGroups();
    testLookaround();
    testBackrefs();
    testAlternation();
    testFlags();
    testBrowserSuiteCases();
    testExecStart();
    testCaseInsensitive();
    testMalformed();
    testNesting();
    testNoPanicOnGarbage();
    testUtf8Integrity();
    testLongInput();
    const bomb_ns = try testCatastrophic();

    const leaked = gpa.deinit() == .leak;
    if (leaked) {
        failed += 1;
        std.debug.print("FAIL: allocator reported leaks\n", .{});
    }

    std.debug.print("\n{d} passed, {d} failed, {d} total\n", .{ passed, failed, passed + failed });
    std.debug.print("/(a+)+b/ vs 30 a's: {d} ns ({d:.3} ms)\n", .{ bomb_ns, @as(f64, @floatFromInt(bomb_ns)) / 1e6 });

    if (failed != 0) std.process.exit(1);
}
