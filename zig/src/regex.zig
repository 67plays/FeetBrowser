//! A JavaScript-flavoured regular-expression engine.
//!
//! The shape is deliberately boring: a recursive-descent parser turns the
//! pattern into a small tree of `Node`s held in flat arrays, and a
//! backtracking matcher walks that tree with an explicit continuation chain
//! threaded through the Zig call stack. Backreferences and lookaround rule
//! out a Thompson/Pike NFA, so backtracking it is -- and backtracking means
//! the engine has to be able to give up: every step through `run`/`runCont`
//! burns one unit of a fixed budget, and blowing the budget fails the whole
//! `exec` rather than hanging the browser on `/(a+)+b/`.
//!
//! Matching is over UTF-8 *code points*, not bytes: `.`, character classes
//! and literals decode one code point at a time, so a match can never end
//! halfway through a multi-byte sequence. Bytes that are not valid UTF-8 are
//! treated as one-byte Latin-1 code points, which keeps the decoder total.
//!
//! `exec` allocates nothing. All the memory lives in the compiled `Regex`.

const std = @import("std");

pub const Span = struct { start: u32, end: u32 };

pub const Flags = struct {
    global: bool = false,
    ignore_case: bool = false,
    multiline: bool = false,
    dot_all: bool = false,
    sticky: bool = false,
    unicode: bool = false,
};

pub const Error = error{ BadPattern, OutOfMemory };

/// How many matcher steps a single `exec` call may take before it gives up
/// and reports "no match". One step is roughly one node visit.
pub const max_steps: u64 = 1_000_000;

/// How deep the mutual recursion between `run` and `runCont` may go. Simple
/// quantifiers (`a*`, `.+`, `[a-z]{2,}`) run iteratively and do not count
/// against this, so only nested-group repetition can reach it.
pub const max_depth: u32 = 4000;

// -- compiled form ---------------------------------------------------------

const Tag = enum(u8) {
    empty,
    lit,
    any,
    class,
    seq,
    alt,
    repeat,
    group,
    look,
    backref,
    bol,
    eol,
    wordb,
    nwordb,
};

const Node = struct {
    tag: Tag,
    /// `lit`: the code point.
    ch: u21 = 0,
    /// `class`: index into `classes`.
    cls: u32 = 0,
    /// `seq`/`alt`: window into `kids`.
    kid_start: u32 = 0,
    kid_len: u32 = 0,
    /// `repeat`/`group`/`look`: the single child node.
    child: u32 = 0,
    /// `repeat`: bounds and laziness.
    min: u32 = 0,
    max: u32 = 0,
    greedy: bool = true,
    /// `group`: 1-based capture index, 0 for a non-capturing group.
    cap: u32 = 0,
    /// `look`: direction, polarity, and (behind only) fixed width in code points.
    ahead: bool = true,
    negate: bool = false,
    width: u32 = 0,
    /// `repeat`/`look`: inclusive range of capture indices underneath, for the
    /// per-iteration capture reset. `cap_lo > cap_hi` means "no groups".
    cap_lo: u32 = 1,
    cap_hi: u32 = 0,
    /// `backref`: the group it refers to.
    ref: u32 = 0,
};

const Range = struct { lo: u21, hi: u21 };

const Class = struct {
    /// Membership for code points 0..127, the hot path.
    bitmap: [2]u64,
    /// Sorted, disjoint ranges for code points >= 128, in `ranges`.
    r_start: u32,
    r_len: u32,
};

pub const Regex = struct {
    /// Number of capturing groups, NOT counting group 0.
    group_count: u32,
    flags: Flags,
    /// Names of named groups, parallel to group index (1-based); empty string
    /// for unnamed groups. Slice length is group_count + 1, index 0 unused.
    group_names: [][]const u8,

    // internals
    nodes: []Node,
    kids: []u32,
    ranges: []Range,
    classes: []Class,
    root: u32,

    /// `flags` is the raw flag string, e.g. "gi". Unknown flag letters are an
    /// error.BadPattern.
    pub fn compile(alloc: std.mem.Allocator, pattern: []const u8, flags: []const u8) Error!Regex {
        const f = try parseFlags(flags);

        var names = std.ArrayList([]const u8).init(alloc);
        errdefer {
            for (names.items) |n| if (n.len > 0) alloc.free(n);
            names.deinit();
        }
        try names.append("");
        try prescanGroups(alloc, pattern, &names);

        var nodes = std.ArrayList(Node).init(alloc);
        errdefer nodes.deinit();
        var kids = std.ArrayList(u32).init(alloc);
        errdefer kids.deinit();
        var ranges = std.ArrayList(Range).init(alloc);
        errdefer ranges.deinit();
        var classes = std.ArrayList(Class).init(alloc);
        errdefer classes.deinit();

        var p = P{
            .pat = pattern,
            .i = 0,
            .alloc = alloc,
            .nodes = &nodes,
            .kids = &kids,
            .ranges = &ranges,
            .classes = &classes,
            .flags = f,
            .group_count = @intCast(names.items.len - 1),
            .names = names.items,
            .next_group = 0,
            .depth = 0,
        };

        const root = try parseAlternation(&p);
        if (p.i != pattern.len) return error.BadPattern; // stray ')'

        return Regex{
            .group_count = p.group_count,
            .flags = f,
            .group_names = try names.toOwnedSlice(),
            .nodes = try nodes.toOwnedSlice(),
            .kids = try kids.toOwnedSlice(),
            .ranges = try ranges.toOwnedSlice(),
            .classes = try classes.toOwnedSlice(),
            .root = root,
        };
    }

    pub fn deinit(self: *Regex, alloc: std.mem.Allocator) void {
        for (self.group_names) |n| if (n.len > 0) alloc.free(n);
        alloc.free(self.group_names);
        alloc.free(self.nodes);
        alloc.free(self.kids);
        alloc.free(self.ranges);
        alloc.free(self.classes);
        self.* = undefined;
    }

    /// Index of the named group, or null. Handy for the `groups` object.
    pub fn groupIndex(self: *const Regex, name: []const u8) ?u32 {
        for (self.group_names, 0..) |n, i| {
            if (i == 0) continue;
            if (std.mem.eql(u8, n, name)) return @intCast(i);
        }
        return null;
    }

    /// Try to match starting at or after byte offset `start`. `caps` must have
    /// room for `group_count + 1` entries; on success entry 0 is the whole
    /// match and entry i is group i (or null if that group did not
    /// participate). Returns true on a match.
    /// If the sticky flag is set, only offset `start` itself is tried.
    pub fn exec(self: *const Regex, input: []const u8, start: u32, caps: []?Span) bool {
        if (caps.len < self.group_count + 1) return false;
        if (start > input.len) return false;

        var st = St{
            .re = self,
            .input = input,
            .caps = caps[0 .. self.group_count + 1],
            .steps = 0,
            .depth = 0,
            .aborted = false,
            .match_start = start,
        };

        var at: u32 = start;
        while (true) {
            for (st.caps) |*c| c.* = null;
            st.depth = 0;
            st.match_start = at;
            if (run(&st, self.root, at, null)) return true;
            if (st.aborted) return false;
            if (self.flags.sticky) return false;
            if (at >= input.len) return false;
            at += decode(input, at).len;
        }
    }
};

// -- flags -----------------------------------------------------------------

fn parseFlags(s: []const u8) Error!Flags {
    var f = Flags{};
    for (s) |c| switch (c) {
        'g' => {
            if (f.global) return error.BadPattern;
            f.global = true;
        },
        'i' => {
            if (f.ignore_case) return error.BadPattern;
            f.ignore_case = true;
        },
        'm' => {
            if (f.multiline) return error.BadPattern;
            f.multiline = true;
        },
        's' => {
            if (f.dot_all) return error.BadPattern;
            f.dot_all = true;
        },
        'y' => {
            if (f.sticky) return error.BadPattern;
            f.sticky = true;
        },
        'u' => {
            if (f.unicode) return error.BadPattern;
            f.unicode = true;
        },
        else => return error.BadPattern,
    };
    return f;
}

// -- UTF-8 -----------------------------------------------------------------

const Dec = struct { cp: u21, len: u32 };

fn isCont(b: u8) bool {
    return b & 0xC0 == 0x80;
}

/// Total decoder: anything that is not a well-formed sequence comes back as a
/// single Latin-1 byte, so the matcher can never trip over bad input.
fn decode(s: []const u8, i: u32) Dec {
    const b = s[i];
    if (b < 0x80) return .{ .cp = b, .len = 1 };
    const rest = s.len - i;
    if (b >= 0xC2 and b <= 0xDF and rest >= 2 and isCont(s[i + 1])) {
        return .{ .cp = (@as(u21, b & 0x1F) << 6) | (s[i + 1] & 0x3F), .len = 2 };
    }
    if (b >= 0xE0 and b <= 0xEF and rest >= 3 and isCont(s[i + 1]) and isCont(s[i + 2])) {
        return .{
            .cp = (@as(u21, b & 0x0F) << 12) | (@as(u21, s[i + 1] & 0x3F) << 6) | (s[i + 2] & 0x3F),
            .len = 3,
        };
    }
    if (b >= 0xF0 and b <= 0xF4 and rest >= 4 and isCont(s[i + 1]) and isCont(s[i + 2]) and isCont(s[i + 3])) {
        return .{
            .cp = (@as(u21, b & 0x07) << 18) | (@as(u21, s[i + 1] & 0x3F) << 12) |
                (@as(u21, s[i + 2] & 0x3F) << 6) | (s[i + 3] & 0x3F),
            .len = 4,
        };
    }
    return .{ .cp = b, .len = 1 };
}

/// Start offset of the code point that ends at `p`. Exactly inverts `decode`.
fn prevStart(s: []const u8, p: u32) u32 {
    var j = p - 1;
    while (j > 0 and p - j < 4 and isCont(s[j])) j -= 1;
    if (decode(s, j).len == p - j) return j;
    return p - 1;
}

fn stepBack(s: []const u8, pos: u32, count: u32) ?u32 {
    var p = pos;
    var i: u32 = 0;
    while (i < count) : (i += 1) {
        if (p == 0) return null;
        p = prevStart(s, p);
    }
    return p;
}

fn foldAscii(c: u21) u21 {
    return if (c >= 'A' and c <= 'Z') c + 32 else c;
}

fn isLineTerm(cp: u21) bool {
    return cp == '\n' or cp == '\r' or cp == 0x2028 or cp == 0x2029;
}

fn isWordByte(b: u8) bool {
    return (b >= '0' and b <= '9') or (b >= 'A' and b <= 'Z') or (b >= 'a' and b <= 'z') or b == '_';
}

// -- predefined sets -------------------------------------------------------
// Each list is sorted and disjoint; `appendComplement` relies on that.

const digit_set = [_]Range{.{ .lo = '0', .hi = '9' }};

const word_set = [_]Range{
    .{ .lo = '0', .hi = '9' },
    .{ .lo = 'A', .hi = 'Z' },
    .{ .lo = '_', .hi = '_' },
    .{ .lo = 'a', .hi = 'z' },
};

const space_set = [_]Range{
    .{ .lo = 0x09, .hi = 0x0D },
    .{ .lo = 0x20, .hi = 0x20 },
    .{ .lo = 0xA0, .hi = 0xA0 },
    .{ .lo = 0x1680, .hi = 0x1680 },
    .{ .lo = 0x2000, .hi = 0x200A },
    .{ .lo = 0x2028, .hi = 0x2029 },
    .{ .lo = 0x202F, .hi = 0x202F },
    .{ .lo = 0x205F, .hi = 0x205F },
    .{ .lo = 0x3000, .hi = 0x3000 },
    .{ .lo = 0xFEFF, .hi = 0xFEFF },
};

fn appendComplement(list: *std.ArrayList(Range), src: []const Range) Error!void {
    var next: u32 = 0;
    for (src) |r| {
        if (r.lo > next) try list.append(.{ .lo = @intCast(next), .hi = @intCast(r.lo - 1) });
        if (@as(u32, r.hi) + 1 > next) next = @as(u32, r.hi) + 1;
    }
    if (next <= 0x10FFFF) try list.append(.{ .lo = @intCast(next), .hi = 0x10FFFF });
}

fn rangeLess(_: void, a: Range, b: Range) bool {
    return a.lo < b.lo;
}

fn sortMerge(list: *std.ArrayList(Range)) void {
    if (list.items.len == 0) return;
    std.sort.pdq(Range, list.items, {}, rangeLess);
    var w: usize = 0;
    var i: usize = 1;
    while (i < list.items.len) : (i += 1) {
        const r = list.items[i];
        if (@as(u32, list.items[w].hi) + 1 >= r.lo) {
            if (r.hi > list.items[w].hi) list.items[w].hi = r.hi;
        } else {
            w += 1;
            list.items[w] = r;
        }
    }
    list.shrinkRetainingCapacity(w + 1);
}

// -- parser ----------------------------------------------------------------

const P = struct {
    pat: []const u8,
    i: usize,
    alloc: std.mem.Allocator,
    nodes: *std.ArrayList(Node),
    kids: *std.ArrayList(u32),
    ranges: *std.ArrayList(Range),
    classes: *std.ArrayList(Class),
    flags: Flags,
    group_count: u32,
    names: [][]const u8,
    next_group: u32,
    /// Group nesting, so a hostile page cannot overflow the parser's stack
    /// with `((((((...))))))`.
    depth: u32,

    fn at(self: *const P) ?u8 {
        return if (self.i < self.pat.len) self.pat[self.i] else null;
    }
};

fn add(p: *P, n: Node) Error!u32 {
    try p.nodes.append(n);
    return @intCast(p.nodes.items.len - 1);
}

/// Walk the pattern once up front so backreferences can point forwards and
/// `\k<name>` can be resolved before the group is parsed.
fn prescanGroups(alloc: std.mem.Allocator, pat: []const u8, names: *std.ArrayList([]const u8)) Error!void {
    var i: usize = 0;
    var in_class = false;
    while (i < pat.len) {
        const c = pat[i];
        if (c == '\\') {
            i += 2;
            continue;
        }
        if (in_class) {
            if (c == ']') in_class = false;
            i += 1;
            continue;
        }
        if (c == '[') {
            in_class = true;
            i += 1;
            continue;
        }
        if (c == '(') {
            if (i + 1 < pat.len and pat[i + 1] == '?') {
                if (i + 3 < pat.len and pat[i + 2] == '<' and pat[i + 3] != '=' and pat[i + 3] != '!') {
                    var j = i + 3;
                    while (j < pat.len and pat[j] != '>') j += 1;
                    if (j >= pat.len or j == i + 3) return error.BadPattern;
                    try names.append(try alloc.dupe(u8, pat[i + 3 .. j]));
                    i = j + 1;
                    continue;
                }
            } else {
                try names.append("");
            }
        }
        i += 1;
    }
}

fn parseAlternation(p: *P) Error!u32 {
    var branches = std.ArrayList(u32).init(p.alloc);
    defer branches.deinit();

    try branches.append(try parseSequence(p));
    while (p.at() == @as(u8, '|')) {
        p.i += 1;
        try branches.append(try parseSequence(p));
    }
    if (branches.items.len == 1) return branches.items[0];

    const start: u32 = @intCast(p.kids.items.len);
    try p.kids.appendSlice(branches.items);
    return add(p, .{
        .tag = .alt,
        .kid_start = start,
        .kid_len = @intCast(branches.items.len),
    });
}

fn parseSequence(p: *P) Error!u32 {
    var items = std.ArrayList(u32).init(p.alloc);
    defer items.deinit();

    while (p.i < p.pat.len and p.pat[p.i] != '|' and p.pat[p.i] != ')') {
        try items.append(try parseTerm(p));
    }
    if (items.items.len == 0) return add(p, .{ .tag = .empty });
    if (items.items.len == 1) return items.items[0];

    const start: u32 = @intCast(p.kids.items.len);
    try p.kids.appendSlice(items.items);
    return add(p, .{
        .tag = .seq,
        .kid_start = start,
        .kid_len = @intCast(items.items.len),
    });
}

const Quant = struct { min: u32, max: u32, greedy: bool };

fn parseQuantifier(p: *P) Error!?Quant {
    const c = p.at() orelse return null;
    var min: u32 = 0;
    var max: u32 = 0;
    switch (c) {
        '*' => {
            p.i += 1;
            min = 0;
            max = std.math.maxInt(u32);
        },
        '+' => {
            p.i += 1;
            min = 1;
            max = std.math.maxInt(u32);
        },
        '?' => {
            p.i += 1;
            min = 0;
            max = 1;
        },
        '{' => {
            const save = p.i;
            p.i += 1;
            const lo = readInt(p) orelse {
                p.i = save;
                return null;
            };
            min = lo;
            max = lo;
            if (p.at() == @as(u8, ',')) {
                p.i += 1;
                if (p.at() == @as(u8, '}')) {
                    max = std.math.maxInt(u32);
                } else {
                    max = readInt(p) orelse {
                        p.i = save;
                        return null;
                    };
                }
            }
            if (p.at() != @as(u8, '}')) {
                p.i = save;
                return null;
            }
            p.i += 1;
            // A well-formed but backwards range is a syntax error, not a literal.
            if (min > max) return error.BadPattern;
        },
        else => return null,
    }
    var greedy = true;
    if (p.at() == @as(u8, '?')) {
        p.i += 1;
        greedy = false;
    }
    return Quant{ .min = min, .max = max, .greedy = greedy };
}

fn readInt(p: *P) ?u32 {
    const start = p.i;
    var v: u64 = 0;
    while (p.i < p.pat.len and p.pat[p.i] >= '0' and p.pat[p.i] <= '9') : (p.i += 1) {
        v = v * 10 + (p.pat[p.i] - '0');
        if (v > 1_000_000) v = 1_000_000; // saturate; nobody means it
    }
    if (p.i == start) return null;
    return @intCast(v);
}

fn parseTerm(p: *P) Error!u32 {
    const before = p.next_group;
    const atom = try parseAtom(p);
    const q = (try parseQuantifier(p)) orelse return atom;

    // `a**` and friends: one quantifier per atom.
    if (p.at()) |c| {
        if (c == '*' or c == '+' or c == '?') return error.BadPattern;
    }

    return add(p, .{
        .tag = .repeat,
        .child = atom,
        .min = q.min,
        .max = q.max,
        .greedy = q.greedy,
        .cap_lo = before + 1,
        .cap_hi = p.next_group,
    });
}

fn parseAtom(p: *P) Error!u32 {
    const c = p.at() orelse return error.BadPattern;
    switch (c) {
        '^' => {
            p.i += 1;
            return add(p, .{ .tag = .bol });
        },
        '$' => {
            p.i += 1;
            return add(p, .{ .tag = .eol });
        },
        '.' => {
            p.i += 1;
            return add(p, .{ .tag = .any });
        },
        '(' => return parseGroup(p),
        '[' => {
            const ci = try parseClass(p);
            return add(p, .{ .tag = .class, .cls = ci });
        },
        '\\' => return parseEscape(p),
        '*', '+', '?' => return error.BadPattern, // nothing to repeat
        '{' => {
            // A `{` that parses as a quantifier here has nothing to quantify.
            const save = p.i;
            if (try parseQuantifier(p)) |_| return error.BadPattern;
            p.i = save + 1;
            return add(p, .{ .tag = .lit, .ch = '{' });
        },
        else => {
            const d = decode(p.pat, @intCast(p.i));
            p.i += d.len;
            return add(p, .{ .tag = .lit, .ch = d.cp });
        },
    }
}

/// How deep groups may nest before we call the pattern unreasonable.
const max_nesting: u32 = 200;

fn parseGroup(p: *P) Error!u32 {
    p.depth += 1;
    defer p.depth -= 1;
    if (p.depth > max_nesting) return error.BadPattern;

    const before = p.next_group;
    p.i += 1; // '('

    var kind: enum { cap, ncap, ahead, nahead, behind, nbehind } = .cap;
    if (p.at() == @as(u8, '?')) {
        p.i += 1;
        const c = p.at() orelse return error.BadPattern;
        switch (c) {
            ':' => {
                p.i += 1;
                kind = .ncap;
            },
            '=' => {
                p.i += 1;
                kind = .ahead;
            },
            '!' => {
                p.i += 1;
                kind = .nahead;
            },
            '<' => {
                p.i += 1;
                const d = p.at() orelse return error.BadPattern;
                if (d == '=') {
                    p.i += 1;
                    kind = .behind;
                } else if (d == '!') {
                    p.i += 1;
                    kind = .nbehind;
                } else {
                    // (?<name>...) -- the name was already recorded by the prescan.
                    while (p.i < p.pat.len and p.pat[p.i] != '>') p.i += 1;
                    if (p.i >= p.pat.len) return error.BadPattern;
                    p.i += 1;
                    kind = .cap;
                }
            },
            else => return error.BadPattern,
        }
    }

    var cap_idx: u32 = 0;
    if (kind == .cap) {
        p.next_group += 1;
        cap_idx = p.next_group;
        if (cap_idx > p.group_count) return error.BadPattern;
    }

    const child = try parseAlternation(p);
    if (p.at() != @as(u8, ')')) return error.BadPattern;
    p.i += 1;

    return switch (kind) {
        .cap, .ncap => add(p, .{ .tag = .group, .child = child, .cap = cap_idx }),
        .ahead, .nahead => add(p, .{
            .tag = .look,
            .child = child,
            .ahead = true,
            .negate = kind == .nahead,
            .cap_lo = before + 1,
            .cap_hi = p.next_group,
        }),
        .behind, .nbehind => blk: {
            const w = fixedWidth(p.nodes.items, p.kids.items, child) orelse return error.BadPattern;
            break :blk add(p, .{
                .tag = .look,
                .child = child,
                .ahead = false,
                .negate = kind == .nbehind,
                .width = w,
                .cap_lo = before + 1,
                .cap_hi = p.next_group,
            });
        },
    };
}

/// Width of a subpattern in code points, or null if it is not fixed. Only
/// lookbehind needs this -- we match lookbehind bodies left-to-right from a
/// computed start offset instead of implementing the spec's right-to-left
/// matcher, so variable-width lookbehind is rejected at compile time.
fn fixedWidth(nodes: []const Node, kids: []const u32, idx: u32) ?u32 {
    const n = nodes[idx];
    return switch (n.tag) {
        .empty, .bol, .eol, .wordb, .nwordb, .look => 0,
        .lit, .any, .class => 1,
        .group => fixedWidth(nodes, kids, n.child),
        .seq => blk: {
            var total: u32 = 0;
            var i: u32 = 0;
            while (i < n.kid_len) : (i += 1) {
                total += fixedWidth(nodes, kids, kids[n.kid_start + i]) orelse return null;
            }
            break :blk total;
        },
        .alt => blk: {
            var first: ?u32 = null;
            var i: u32 = 0;
            while (i < n.kid_len) : (i += 1) {
                const w = fixedWidth(nodes, kids, kids[n.kid_start + i]) orelse return null;
                if (first) |f| {
                    if (f != w) return null;
                } else first = w;
            }
            break :blk first orelse 0;
        },
        .repeat => blk: {
            if (n.min != n.max) return null;
            const w = fixedWidth(nodes, kids, n.child) orelse return null;
            break :blk n.min * w;
        },
        .backref => null,
    };
}

fn parseEscape(p: *P) Error!u32 {
    p.i += 1; // '\'
    const e = p.at() orelse return error.BadPattern; // trailing backslash
    switch (e) {
        'd', 'D', 'w', 'W', 's', 'S' => {
            p.i += 1;
            var list = std.ArrayList(Range).init(p.alloc);
            defer list.deinit();
            const src: []const Range = switch (e) {
                'd', 'D' => &digit_set,
                'w', 'W' => &word_set,
                else => &space_set,
            };
            try list.appendSlice(src);
            const neg = (e == 'D' or e == 'W' or e == 'S');
            const ci = try buildClass(p, &list, neg);
            return add(p, .{ .tag = .class, .cls = ci });
        },
        'b' => {
            p.i += 1;
            return add(p, .{ .tag = .wordb });
        },
        'B' => {
            p.i += 1;
            return add(p, .{ .tag = .nwordb });
        },
        '1'...'9' => {
            const save = p.i;
            const n = readInt(p).?;
            if (n >= 1 and n <= p.group_count) {
                return add(p, .{ .tag = .backref, .ref = n });
            }
            // No such group: Annex B rereads it as a legacy octal escape
            // (`\8` and `\9` as plain identity escapes). V8 does this too, and
            // a page whose regex is subtly wrong should still load.
            p.i = save;
            return add(p, .{ .tag = .lit, .ch = readOctal(p) });
        },
        'k' => {
            p.i += 1;
            if (p.at() != @as(u8, '<')) return error.BadPattern;
            p.i += 1;
            const s = p.i;
            while (p.i < p.pat.len and p.pat[p.i] != '>') p.i += 1;
            if (p.i >= p.pat.len) return error.BadPattern;
            const name = p.pat[s..p.i];
            p.i += 1;
            for (p.names, 0..) |nm, gi| {
                if (gi == 0) continue;
                if (nm.len > 0 and std.mem.eql(u8, nm, name)) {
                    return add(p, .{ .tag = .backref, .ref = @intCast(gi) });
                }
            }
            return error.BadPattern;
        },
        else => {
            const cp = try escapeChar(p);
            return add(p, .{ .tag = .lit, .ch = cp });
        },
    }
}

/// Decodes an escape that stands for a single code point. `p.i` points at the
/// character after the backslash.
fn escapeChar(p: *P) Error!u21 {
    const e = p.pat[p.i];
    switch (e) {
        'n' => {
            p.i += 1;
            return '\n';
        },
        'r' => {
            p.i += 1;
            return '\r';
        },
        't' => {
            p.i += 1;
            return '\t';
        },
        'f' => {
            p.i += 1;
            return 0x0C;
        },
        'v' => {
            p.i += 1;
            return 0x0B;
        },
        '0'...'7' => return readOctal(p),
        'x' => {
            p.i += 1;
            // `\x` without two hex digits is an identity escape for 'x'.
            return readHex(p, 2) orelse 'x';
        },
        'u' => {
            p.i += 1;
            if (p.at() == @as(u8, '{')) {
                const save = p.i;
                p.i += 1;
                var v: u32 = 0;
                var count: u32 = 0;
                while (p.i < p.pat.len and p.pat[p.i] != '}') : (p.i += 1) {
                    const h = hexVal(p.pat[p.i]) orelse {
                        v = 0x110000;
                        break;
                    };
                    v = v * 16 + h;
                    count += 1;
                    if (v > 0x10FFFF) break;
                }
                if (count > 0 and v <= 0x10FFFF and p.i < p.pat.len and p.pat[p.i] == '}') {
                    p.i += 1; // '}'
                    return @intCast(v);
                }
                p.i = save; // malformed: identity escape for 'u'
                return 'u';
            }
            return readHex(p, 4) orelse 'u';
        },
        'c' => {
            if (p.i + 1 < p.pat.len) {
                const l = p.pat[p.i + 1];
                if ((l >= 'a' and l <= 'z') or (l >= 'A' and l <= 'Z')) {
                    p.i += 2;
                    return l % 32;
                }
            }
            p.i += 1;
            return 'c';
        },
        else => {
            // Identity escape: `\.` `\/` `\\` `\$` and, permissively, anything
            // else we do not recognise. Real pages lean on this.
            const d = decode(p.pat, @intCast(p.i));
            p.i += d.len;
            return d.cp;
        },
    }
}

fn hexVal(c: u8) ?u32 {
    return switch (c) {
        '0'...'9' => c - '0',
        'a'...'f' => c - 'a' + 10,
        'A'...'F' => c - 'A' + 10,
        else => null,
    };
}

/// Exactly `n` hex digits, or null with `p.i` left untouched.
fn readHex(p: *P, n: u32) ?u21 {
    const save = p.i;
    var v: u32 = 0;
    var i: u32 = 0;
    while (i < n) : (i += 1) {
        if (p.i >= p.pat.len) {
            p.i = save;
            return null;
        }
        const h = hexVal(p.pat[p.i]) orelse {
            p.i = save;
            return null;
        };
        v = v * 16 + h;
        p.i += 1;
    }
    return @intCast(v);
}

/// Annex B legacy octal: up to three octal digits, capped at 255. `\8` and
/// `\9` are not octal, so they come back as themselves.
fn readOctal(p: *P) u21 {
    const first = p.pat[p.i];
    if (first == '8' or first == '9') {
        p.i += 1;
        return first;
    }
    var v: u32 = 0;
    var n: u32 = 0;
    while (n < 3 and p.i < p.pat.len and p.pat[p.i] >= '0' and p.pat[p.i] <= '7') {
        const next = v * 8 + (p.pat[p.i] - '0');
        if (next > 255) break;
        v = next;
        p.i += 1;
        n += 1;
    }
    return @intCast(v);
}

fn parseClass(p: *P) Error!u32 {
    p.i += 1; // '['
    var neg = false;
    if (p.at() == @as(u8, '^')) {
        p.i += 1;
        neg = true;
    }

    var list = std.ArrayList(Range).init(p.alloc);
    defer list.deinit();

    var closed = false;
    while (p.i < p.pat.len) {
        if (p.pat[p.i] == ']') {
            p.i += 1;
            closed = true;
            break;
        }
        const a = try classAtom(p, &list);
        if (a) |lo| {
            // `a-z` is a range; a trailing `-` before `]` is a literal.
            if (p.i + 1 < p.pat.len and p.pat[p.i] == '-' and p.pat[p.i + 1] != ']') {
                p.i += 1;
                const b = try classAtom(p, &list);
                if (b) |hi| {
                    if (lo > hi) return error.BadPattern;
                    try list.append(.{ .lo = lo, .hi = hi });
                } else {
                    // `[\d-a]`: the set already went in; keep `-` and `a` literal.
                    try list.append(.{ .lo = lo, .hi = lo });
                    try list.append(.{ .lo = '-', .hi = '-' });
                }
            } else {
                try list.append(.{ .lo = lo, .hi = lo });
            }
        }
    }
    if (!closed) return error.BadPattern;

    return buildClass(p, &list, neg);
}

/// Returns the code point, or null if a predefined set was appended instead.
fn classAtom(p: *P, list: *std.ArrayList(Range)) Error!?u21 {
    if (p.pat[p.i] != '\\') {
        const d = decode(p.pat, @intCast(p.i));
        p.i += d.len;
        return d.cp;
    }
    p.i += 1;
    if (p.i >= p.pat.len) return error.BadPattern;
    switch (p.pat[p.i]) {
        'd' => {
            p.i += 1;
            try list.appendSlice(&digit_set);
            return null;
        },
        'D' => {
            p.i += 1;
            try appendComplement(list, &digit_set);
            return null;
        },
        'w' => {
            p.i += 1;
            try list.appendSlice(&word_set);
            return null;
        },
        'W' => {
            p.i += 1;
            try appendComplement(list, &word_set);
            return null;
        },
        's' => {
            p.i += 1;
            try list.appendSlice(&space_set);
            return null;
        },
        'S' => {
            p.i += 1;
            try appendComplement(list, &space_set);
            return null;
        },
        // Inside a class `\b` is a backspace, not a boundary.
        'b' => {
            p.i += 1;
            return 0x08;
        },
        else => return try escapeChar(p),
    }
}

/// Canonicalise (case-fold when `i` is set) *then* complement, which is what
/// makes `[^a]/i` correctly reject `A`.
fn buildClass(p: *P, list: *std.ArrayList(Range), neg: bool) Error!u32 {
    if (p.flags.ignore_case) {
        const n0 = list.items.len;
        var i: usize = 0;
        while (i < n0) : (i += 1) {
            const r = list.items[i];
            if (r.hi >= 'A' and r.lo <= 'Z') {
                try list.append(.{ .lo = @max(r.lo, 'A') + 32, .hi = @min(r.hi, 'Z') + 32 });
            }
            if (r.hi >= 'a' and r.lo <= 'z') {
                try list.append(.{ .lo = @max(r.lo, 'a') - 32, .hi = @min(r.hi, 'z') - 32 });
            }
        }
    }
    sortMerge(list);

    if (neg) {
        var out = std.ArrayList(Range).init(p.alloc);
        defer out.deinit();
        try appendComplement(&out, list.items);
        list.clearRetainingCapacity();
        try list.appendSlice(out.items);
    }

    var bm = [2]u64{ 0, 0 };
    const r_start: u32 = @intCast(p.ranges.items.len);
    var r_len: u32 = 0;
    for (list.items) |r| {
        var lo = r.lo;
        if (lo < 128) {
            const hi = @min(r.hi, 127);
            var c: u21 = lo;
            while (true) : (c += 1) {
                bm[c >> 6] |= @as(u64, 1) << @intCast(c & 63);
                if (c == hi) break;
            }
            if (r.hi < 128) continue;
            lo = 128;
        }
        try p.ranges.append(.{ .lo = lo, .hi = r.hi });
        r_len += 1;
    }
    try p.classes.append(.{ .bitmap = bm, .r_start = r_start, .r_len = r_len });
    return @intCast(p.classes.items.len - 1);
}

// -- matcher ---------------------------------------------------------------

const CTag = enum {
    /// Finish the remaining items of a `seq`.
    seq,
    /// Come back around a `repeat`.
    repeat,
    /// Close a capturing group.
    group_end,
    /// Terminal inside a lookaround: succeed, without unwinding captures.
    assert_ok,
    /// Terminal inside a lookbehind: succeed only if we landed exactly here.
    behind_end,
};

const Cont = struct {
    tag: CTag,
    node: u32 = 0,
    idx: u32 = 0,
    pos: u32 = 0,
    next: ?*const Cont = null,
};

const St = struct {
    re: *const Regex,
    input: []const u8,
    caps: []?Span,
    steps: u64,
    depth: u32,
    aborted: bool,
    match_start: u32,

    fn tick(self: *St) bool {
        self.steps += 1;
        if (self.steps > max_steps) {
            self.aborted = true;
            return false;
        }
        return true;
    }
};

fn classHas(re: *const Regex, ci: u32, cp: u21) bool {
    const c = re.classes[ci];
    if (cp < 128) return (c.bitmap[cp >> 6] >> @intCast(cp & 63)) & 1 != 0;
    var i = c.r_start;
    const end = c.r_start + c.r_len;
    while (i < end) : (i += 1) {
        const r = re.ranges[i];
        if (cp < r.lo) return false;
        if (cp <= r.hi) return true;
    }
    return false;
}

/// Single-code-point atoms: the only nodes the iterative quantifier can drive.
fn isSimple(t: Tag) bool {
    return t == .lit or t == .any or t == .class;
}

fn matchAtom(st: *const St, n: Node, pos: u32) ?u32 {
    if (pos >= st.input.len) return null;
    const d = decode(st.input, pos);
    switch (n.tag) {
        .lit => {
            if (d.cp == n.ch) return d.len;
            if (st.re.flags.ignore_case and foldAscii(d.cp) == foldAscii(n.ch)) return d.len;
            return null;
        },
        .any => {
            if (!st.re.flags.dot_all and isLineTerm(d.cp)) return null;
            return d.len;
        },
        .class => {
            if (classHas(st.re, n.cls, d.cp)) return d.len;
            return null;
        },
        else => unreachable,
    }
}

fn atLineStart(st: *const St, pos: u32) bool {
    if (pos == 0) return true;
    if (!st.re.flags.multiline) return false;
    const j = prevStart(st.input, pos);
    return isLineTerm(decode(st.input, j).cp);
}

fn atLineEnd(st: *const St, pos: u32) bool {
    if (pos == st.input.len) return true;
    if (!st.re.flags.multiline) return false;
    return isLineTerm(decode(st.input, pos).cp);
}

fn isWordAt(st: *const St, pos: u32) bool {
    if (pos >= st.input.len) return false;
    return isWordByte(st.input[pos]);
}

fn isWordBefore(st: *const St, pos: u32) bool {
    if (pos == 0) return false;
    return isWordByte(st.input[pos - 1]);
}

fn run(st: *St, ni: u32, pos: u32, k: ?*const Cont) bool {
    if (!st.tick()) return false;
    st.depth += 1;
    defer st.depth -= 1;
    if (st.depth > max_depth) return false;

    const n = st.re.nodes[ni];
    switch (n.tag) {
        .empty => return runCont(st, k, pos),

        .lit, .any, .class => {
            const len = matchAtom(st, n, pos) orelse return false;
            return runCont(st, k, pos + len);
        },

        .bol => return if (atLineStart(st, pos)) runCont(st, k, pos) else false,
        .eol => return if (atLineEnd(st, pos)) runCont(st, k, pos) else false,

        .wordb, .nwordb => {
            const b = isWordBefore(st, pos) != isWordAt(st, pos);
            const want = n.tag == .wordb;
            return if (b == want) runCont(st, k, pos) else false;
        },

        .seq => {
            if (n.kid_len == 0) return runCont(st, k, pos);
            const f = Cont{ .tag = .seq, .node = ni, .idx = 1, .next = k };
            return run(st, st.re.kids[n.kid_start], pos, &f);
        },

        .alt => {
            var i: u32 = 0;
            while (i < n.kid_len) : (i += 1) {
                if (run(st, st.re.kids[n.kid_start + i], pos, k)) return true;
                if (st.aborted) return false;
            }
            return false;
        },

        .group => {
            if (n.cap == 0) return run(st, n.child, pos, k);
            const f = Cont{ .tag = .group_end, .node = ni, .pos = pos, .next = k };
            return run(st, n.child, pos, &f);
        },

        .repeat => {
            if (isSimple(st.re.nodes[n.child].tag)) return runSimpleRepeat(st, n, pos, k);
            return runRepeat(st, ni, 0, pos, k);
        },

        .look => return runLook(st, n, pos, k),

        .backref => {
            const sp = st.caps[n.ref] orelse return runCont(st, k, pos);
            const len = sp.end - sp.start;
            if (pos + len > st.input.len) return false;
            const a = st.input[sp.start..sp.end];
            const b = st.input[pos .. pos + len];
            if (st.re.flags.ignore_case) {
                for (a, b) |x, y| {
                    if (foldAscii(x) != foldAscii(y)) return false;
                }
            } else if (!std.mem.eql(u8, a, b)) return false;
            return runCont(st, k, pos + len);
        },
    }
}

fn runCont(st: *St, k: ?*const Cont, pos: u32) bool {
    if (!st.tick()) return false;
    const f = k orelse {
        st.caps[0] = .{ .start = st.match_start, .end = pos };
        return true;
    };
    st.depth += 1;
    defer st.depth -= 1;
    if (st.depth > max_depth) return false;

    switch (f.tag) {
        .assert_ok => return true,
        .behind_end => return pos == f.pos,

        .seq => {
            const n = st.re.nodes[f.node];
            if (f.idx >= n.kid_len) return runCont(st, f.next, pos);
            const g = Cont{ .tag = .seq, .node = f.node, .idx = f.idx + 1, .next = f.next };
            return run(st, st.re.kids[n.kid_start + f.idx], pos, &g);
        },

        .group_end => {
            const n = st.re.nodes[f.node];
            const old = st.caps[n.cap];
            st.caps[n.cap] = .{ .start = f.pos, .end = pos };
            if (runCont(st, f.next, pos)) return true;
            st.caps[n.cap] = old;
            return false;
        },

        .repeat => {
            const n = st.re.nodes[f.node];
            if (pos == f.pos) {
                // The body matched empty. Iterating again would do the same
                // forever, so jump straight to the minimum and get out.
                if (f.idx >= n.min) return runCont(st, f.next, pos);
                return runRepeat(st, f.node, n.min, pos, f.next);
            }
            return runRepeat(st, f.node, f.idx, pos, f.next);
        },
    }
}

/// Per ECMAScript, every iteration of a quantifier clears the captures inside
/// it, so `/(?:(a)|b)+/` on "ab" leaves group 1 unset. We only bother when the
/// group range is small enough to save on the stack.
const max_reset = 8;

fn tryBody(st: *St, ni: u32, pos: u32, f: *const Cont) bool {
    const n = st.re.nodes[ni];
    if (n.cap_lo <= n.cap_hi and n.cap_hi - n.cap_lo < max_reset) {
        var saved: [max_reset]?Span = undefined;
        var i = n.cap_lo;
        while (i <= n.cap_hi) : (i += 1) {
            saved[i - n.cap_lo] = st.caps[i];
            st.caps[i] = null;
        }
        if (run(st, n.child, pos, f)) return true;
        i = n.cap_lo;
        while (i <= n.cap_hi) : (i += 1) st.caps[i] = saved[i - n.cap_lo];
        return false;
    }
    return run(st, n.child, pos, f);
}

fn runRepeat(st: *St, ni: u32, count: u32, pos: u32, k: ?*const Cont) bool {
    if (!st.tick()) return false;
    st.depth += 1;
    defer st.depth -= 1;
    if (st.depth > max_depth) return false;

    const n = st.re.nodes[ni];
    const more = count < n.max;
    const exit_ok = count >= n.min;

    if (n.greedy) {
        if (more) {
            const f = Cont{ .tag = .repeat, .node = ni, .idx = count + 1, .pos = pos, .next = k };
            if (tryBody(st, ni, pos, &f)) return true;
            if (st.aborted) return false;
        }
        if (exit_ok) return runCont(st, k, pos);
        return false;
    }

    if (exit_ok) {
        if (runCont(st, k, pos)) return true;
        if (st.aborted) return false;
    }
    if (more) {
        const f = Cont{ .tag = .repeat, .node = ni, .idx = count + 1, .pos = pos, .next = k };
        return tryBody(st, ni, pos, &f);
    }
    return false;
}

/// `a*`, `.+?`, `[a-z]{2,5}` -- a quantifier over a single-code-point atom.
/// Driven with a loop rather than recursion so that matching a megabyte of
/// text does not put a megabyte of frames on the stack.
fn runSimpleRepeat(st: *St, n: Node, pos: u32, k: ?*const Cont) bool {
    const child = st.re.nodes[n.child];

    if (n.greedy) {
        var p = pos;
        var cnt: u32 = 0;
        while (cnt < n.max) {
            if (!st.tick()) return false;
            const len = matchAtom(st, child, p) orelse break;
            p += len;
            cnt += 1;
        }
        while (cnt >= n.min) {
            if (runCont(st, k, p)) return true;
            if (st.aborted) return false;
            if (cnt == 0) break;
            p = prevStart(st.input, p);
            cnt -= 1;
        }
        return false;
    }

    var p = pos;
    var cnt: u32 = 0;
    while (cnt < n.min) : (cnt += 1) {
        if (!st.tick()) return false;
        const len = matchAtom(st, child, p) orelse return false;
        p += len;
    }
    while (true) {
        if (runCont(st, k, p)) return true;
        if (st.aborted) return false;
        if (cnt >= n.max) return false;
        if (!st.tick()) return false;
        const len = matchAtom(st, child, p) orelse return false;
        p += len;
        cnt += 1;
    }
}

fn runLook(st: *St, n: Node, pos: u32, k: ?*const Cont) bool {
    const ok = Cont{ .tag = .assert_ok };

    var hit: bool = undefined;
    var start: u32 = pos;
    var end_frame: Cont = ok;
    if (!n.ahead) {
        start = stepBack(st.input, pos, n.width) orelse {
            // Not enough text behind us: the assertion simply fails.
            if (n.negate) return runCont(st, k, pos);
            return false;
        };
        end_frame = Cont{ .tag = .behind_end, .pos = pos };
    }

    if (n.negate) {
        // Undo anything the (successful, and therefore discarded) attempt set.
        if (n.cap_lo <= n.cap_hi and n.cap_hi - n.cap_lo < max_reset) {
            var saved: [max_reset]?Span = undefined;
            var i = n.cap_lo;
            while (i <= n.cap_hi) : (i += 1) saved[i - n.cap_lo] = st.caps[i];
            hit = run(st, n.child, start, &end_frame);
            i = n.cap_lo;
            while (i <= n.cap_hi) : (i += 1) st.caps[i] = saved[i - n.cap_lo];
        } else {
            hit = run(st, n.child, start, &end_frame);
        }
        if (st.aborted) return false;
        if (hit) return false;
        return runCont(st, k, pos);
    }

    // A positive lookaround is atomic: the body matches once, with a
    // continuation that always succeeds, and we never backtrack into it.
    // Because the frames unwind on `true` they leave their captures in place.
    if (!run(st, n.child, start, &end_frame)) return false;
    return runCont(st, k, pos);
}
