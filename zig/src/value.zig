//! Values, objects and the collected heap.
//!
//! A value is a 16-byte tagged union. That is twice what a NaN-boxed value
//! costs, and the trade is deliberate: NaN boxing wins cache density that only
//! starts to matter at heap sizes we will never reach, and it buys that with
//! pointer punning that Zig cannot check. A tagged union is checked in debug
//! builds, so a type confusion is a panic at the line that caused it rather
//! than a corrupted pointer three frames later. Numbers are plain f64 stored
//! inline, which is where V8's small-integer tagging was heading anyway -- the
//! whole point of an SMI is to keep an integer out of a heap box, and a union
//! that already carries eight bytes of payload never boxes one.
//!
//! Everything reachable from a value -- strings, objects, environments -- is
//! allocated by `Heap` and freed by mark and sweep. See docs/jszig.md for why
//! that and not reference counting.

const std = @import("std");

pub const GcKind = enum(u8) { string, object, env };

pub const Gc = struct {
    next: ?*Gc = null,
    kind: GcKind,
    marked: bool = false,
};

// -- strings ---------------------------------------------------------------

/// An immutable UTF-8 string. Concatenation allocates; there is no rope, so a
/// script that builds a megabyte one character at a time is quadratic. Real
/// page scripts do not, and a rope would need its own flattening rules in
/// every place that wants bytes.
pub const Str = struct {
    gc: Gc,
    bytes: []u8,

    pub fn slice(self: *const Str) []const u8 {
        return self.bytes;
    }
};

// -- values ----------------------------------------------------------------

pub const Value = union(enum) {
    undefined,
    null,
    boolean: bool,
    number: f64,
    string: *Str,
    object: *Obj,

    pub fn isNullish(self: Value) bool {
        return self == .undefined or self == .null;
    }

    pub fn isCallable(self: Value) bool {
        return switch (self) {
            .object => |o| o.callable(),
            else => false,
        };
    }

    /// Reference identity for objects and strings-by-value everywhere else.
    /// This is `===` minus the number rules, which the VM handles.
    pub fn sameRef(a: Value, b: Value) bool {
        return switch (a) {
            .object => |x| switch (b) {
                .object => |y| x == y,
                else => false,
            },
            .string => |x| switch (b) {
                .string => |y| std.mem.eql(u8, x.bytes, y.bytes),
                else => false,
            },
            else => false,
        };
    }
};

// -- properties ------------------------------------------------------------

/// A property slot. Keys are owned copies rather than collected strings: a
/// property name is not a value, nothing else can reach it, and duplicating a
/// few bytes per property keeps the collector from having to trace the shape
/// of every object it walks.
pub const Prop = struct {
    key: []u8,
    value: Value = .undefined,
    getter: ?*Obj = null,
    setter: ?*Obj = null,
    enumerable: bool = true,
    is_accessor: bool = false,
    /// A deleted slot. Kept so insertion order of the survivors is stable.
    dead: bool = false,
};

/// Insertion-ordered property storage. Order is observable -- `Object.keys`,
/// `for...in` and `JSON.stringify` all promise it -- so the array is the
/// authority and the hash map is only an index into it.
pub const PropMap = struct {
    entries: std.ArrayListUnmanaged(Prop) = .{},
    index: std.StringHashMapUnmanaged(u32) = .{},
    dead_count: u32 = 0,

    pub fn deinit(self: *PropMap, alloc: std.mem.Allocator) void {
        for (self.entries.items) |*p| alloc.free(p.key);
        self.entries.deinit(alloc);
        self.index.deinit(alloc);
    }

    pub fn find(self: *const PropMap, key: []const u8) ?*Prop {
        const i = self.index.get(key) orelse return null;
        const p = &self.entries.items[i];
        if (p.dead) return null;
        return p;
    }

    pub fn put(self: *PropMap, alloc: std.mem.Allocator, key: []const u8, value: Value) !void {
        if (self.index.get(key)) |i| {
            const p = &self.entries.items[i];
            if (p.dead) {
                p.dead = false;
                p.enumerable = true;
                p.is_accessor = false;
                p.getter = null;
                p.setter = null;
                self.dead_count -= 1;
            }
            p.value = value;
            return;
        }
        try self.putNew(alloc, key, .{ .key = undefined, .value = value });
    }

    pub fn putProp(self: *PropMap, alloc: std.mem.Allocator, key: []const u8, prop: Prop) !void {
        if (self.index.get(key)) |i| {
            const p = &self.entries.items[i];
            if (p.dead) self.dead_count -= 1;
            const owned = p.key;
            p.* = prop;
            p.key = owned;
            return;
        }
        try self.putNew(alloc, key, prop);
    }

    fn putNew(self: *PropMap, alloc: std.mem.Allocator, key: []const u8, prop: Prop) !void {
        const owned = try alloc.dupe(u8, key);
        errdefer alloc.free(owned);
        var p = prop;
        p.key = owned;
        p.dead = false;
        try self.entries.append(alloc, p);
        errdefer _ = self.entries.pop();
        try self.index.put(alloc, owned, @intCast(self.entries.items.len - 1));
    }

    pub fn remove(self: *PropMap, key: []const u8) bool {
        const i = self.index.get(key) orelse return false;
        const p = &self.entries.items[i];
        if (p.dead) return false;
        p.dead = true;
        p.value = .undefined;
        p.getter = null;
        p.setter = null;
        self.dead_count += 1;
        return true;
    }

    pub fn count(self: *const PropMap) usize {
        return self.entries.items.len - self.dead_count;
    }
};

// -- objects ---------------------------------------------------------------

pub const Class = enum(u8) {
    plain,
    global,
    array,
    function,
    arguments,
    err,
    date,
    regexp,
    map,
    set,
    promise,
    /// A Python object reached over the C ABI. Property access on one of these
    /// is a call back out to the host.
    host,
    boxed,
};

/// A builtin. `callee` is the function object itself, which is how a builtin
/// that needs private state -- a bound promise, a suspended coroutine -- finds
/// it without the VM having to carry a side channel.
pub const NativeFn = *const fn (vm: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value;

pub const FuncKind = enum(u8) { normal, arrow, method, getter, setter, ctor, derived_ctor };

/// The compiled body of a function. Owned by the Script it was compiled from
/// and never collected: bytecode is bounded by the size of the source, which
/// a page hands us once.
pub const Proto = struct {
    name: []u8,
    code: []u8,
    consts: []Value,
    /// Nested function bodies, indexed by the closure instruction's operand.
    protos: []*Proto,
    n_params: u32,
    n_slots: u32,
    /// Parameter defaults and destructuring run as a prelude; the compiler
    /// records how many arguments may simply be copied into slots.
    simple_params: bool,
    is_async: bool,
    is_generator: bool,
    kind: FuncKind,
    handlers: []Handler,
    /// Byte offset -> source line, for error messages. Sorted by offset.
    lines: []LineEntry,
    /// Names of the slots, for `for...in`-free debugging and for `arguments`.
    slot_names: [][]const u8,
    source_name: []const u8,
};

pub const LineEntry = struct { offset: u32, line: u32 };

pub const Handler = struct {
    start: u32,
    end: u32,
    target: u32,
    /// Stack depth to unwind to before jumping.
    depth: u32,
    kind: enum(u8) { catch_block, finally_block },
};

pub const FuncData = struct {
    proto: ?*Proto = null,
    env: ?*Env = null,
    native: ?NativeFn = null,
    /// `this` for arrows and bound functions; null means "use the caller's".
    bound_this: ?Value = null,
    bound_args: []Value = &.{},
    bound_target: ?*Obj = null,
    /// The object a method was defined on, so `super.m()` can find its proto.
    home: ?*Obj = null,
    /// Set on class constructors so `new` knows to run field initialisers.
    fields: ?*Obj = null,
    n_args: u32 = 0,
    /// A callable belonging to the embedder: `host` is its handle.
    is_host: bool = false,
    host: u64 = 0,
    /// A suspended async activation, for the two natives that resume one.
    coro: ?*anyopaque = null,
};

pub const MapEntry = struct { key: Value, value: Value, dead: bool = false };

pub const PromiseState = enum(u8) { pending, fulfilled, rejected };

pub const Reaction = struct {
    on_ok: ?*Obj,
    on_err: ?*Obj,
    next: *Obj, // the derived promise
};

pub const PromiseData = struct {
    state: PromiseState = .pending,
    value: Value = .undefined,
    reactions: std.ArrayListUnmanaged(Reaction) = .{},
    handled: bool = false,
};

pub const RegexData = struct {
    /// Opaque to this module; vm.zig owns the compiled program.
    prog: *anyopaque,
    source: []u8,
    flags: []u8,
    last_index: u32 = 0,
};

pub const Data = union(enum) {
    none,
    func: FuncData,
    /// Dense element storage. Holes are `undefined`, which is observably the
    /// same thing for everything we implement.
    elements: std.ArrayListUnmanaged(Value),
    entries: std.ArrayListUnmanaged(MapEntry),
    promise: *PromiseData,
    regex: *RegexData,
    /// A host handle owned by the embedder.
    host: u64,
    boxed: Value,
    date: f64,
};

pub const Obj = struct {
    gc: Gc,
    class: Class,
    proto: ?*Obj,
    props: PropMap = .{},
    data: Data = .none,
    /// Set on `Object.freeze`d and on host objects, which have no own props.
    extensible: bool = true,

    pub fn callable(self: *const Obj) bool {
        if (self.class != .function) return false;
        const f = self.data.func;
        return f.proto != null or f.native != null or f.bound_target != null or f.is_host;
    }

    pub fn elements(self: *Obj) *std.ArrayListUnmanaged(Value) {
        return &self.data.elements;
    }
};

// -- environments ----------------------------------------------------------

/// One lexical scope's storage. Every declared binding lives in one of these
/// rather than in a stack frame, which costs an allocation per scope and buys
/// closures that are simply a pointer to the scope chain. Escape analysis
/// would let most scopes stay on the stack; it would also mean a bug in the
/// analysis shows up as a variable that silently stops updating.
pub const Env = struct {
    gc: Gc,
    parent: ?*Env,
    slots: []Value,
    /// Set once the binding has been initialised, so `let` before its
    /// declaration throws instead of reading undefined.
    ready: []bool,
};

// -- the heap --------------------------------------------------------------

/// Mark and sweep over one intrusive list of everything allocated.
///
/// Collection only ever happens at a bytecode instruction boundary, and that
/// single rule is what makes precise rooting possible: at an instruction
/// boundary every live value is either in the VM's value stack, in a frame, in
/// the globals, in a queued job, or in the embedder's handle table. Nothing
/// is stranded in a Zig local. Native builtins that call back into JavaScript
/// re-enter the interpreter and so can hit a safe point, which is why they --
/// and only they -- have to park their temporaries in `Vm.temps`.
pub const Heap = struct {
    alloc: std.mem.Allocator,
    all: ?*Gc = null,
    bytes: usize = 0,
    threshold: usize = 1 << 20,
    /// Raised by the allocator, lowered by the VM when it collects.
    pending: bool = false,
    gray: std.ArrayListUnmanaged(*Gc) = .{},
    enabled: bool = true,
    /// Set by the VM so a swept RegExp can free its compiled program without
    /// this module having to know what one looks like.
    regex_free: ?*const fn (std.mem.Allocator, *anyopaque) void = null,

    pub fn init(alloc: std.mem.Allocator) Heap {
        return .{ .alloc = alloc };
    }

    fn note(self: *Heap, n: usize) void {
        self.bytes += n;
        if (self.bytes > self.threshold) self.pending = true;
    }

    fn link(self: *Heap, gc: *Gc) void {
        gc.next = self.all;
        self.all = gc;
    }

    pub fn newStr(self: *Heap, bytes: []const u8) !*Str {
        const s = try self.alloc.create(Str);
        s.* = .{ .gc = .{ .kind = .string }, .bytes = try self.alloc.dupe(u8, bytes) };
        self.link(&s.gc);
        self.note(@sizeOf(Str) + bytes.len);
        return s;
    }

    /// Takes ownership of `bytes`, which must come from this heap's allocator.
    pub fn adoptStr(self: *Heap, bytes: []u8) !*Str {
        const s = try self.alloc.create(Str);
        s.* = .{ .gc = .{ .kind = .string }, .bytes = bytes };
        self.link(&s.gc);
        self.note(@sizeOf(Str) + bytes.len);
        return s;
    }

    pub fn newObj(self: *Heap, class: Class, proto: ?*Obj) !*Obj {
        const o = try self.alloc.create(Obj);
        o.* = .{ .gc = .{ .kind = .object }, .class = class, .proto = proto };
        self.link(&o.gc);
        self.note(@sizeOf(Obj));
        return o;
    }

    pub fn newEnv(self: *Heap, parent: ?*Env, n: usize) !*Env {
        const e = try self.alloc.create(Env);
        const slots = try self.alloc.alloc(Value, n);
        @memset(slots, Value.undefined);
        const ready = try self.alloc.alloc(bool, n);
        @memset(ready, false);
        e.* = .{ .gc = .{ .kind = .env }, .parent = parent, .slots = slots, .ready = ready };
        self.link(&e.gc);
        self.note(@sizeOf(Env) + n * (@sizeOf(Value) + 1));
        return e;
    }

    // -- marking -----------------------------------------------------------

    pub fn markValue(self: *Heap, v: Value) void {
        switch (v) {
            .string => |s| self.markGc(&s.gc),
            .object => |o| self.markGc(&o.gc),
            else => {},
        }
    }

    pub fn markObj(self: *Heap, o: ?*Obj) void {
        if (o) |p| self.markGc(&p.gc);
    }

    pub fn markEnv(self: *Heap, e: ?*Env) void {
        if (e) |p| self.markGc(&p.gc);
    }

    fn markGc(self: *Heap, gc: *Gc) void {
        if (gc.marked) return;
        gc.marked = true;
        if (gc.kind == .string) return; // no outgoing edges
        // Strings are leaves; anything else goes on the worklist. An explicit
        // worklist and not recursion, because a long linked list built in JS
        // would otherwise recurse as deep as the list is long.
        self.gray.append(self.alloc, gc) catch {
            // Out of memory during marking is not recoverable in a way that
            // helps, so fall back to tracing this one edge recursively.
            self.traceOne(gc);
        };
    }

    fn drainGray(self: *Heap) void {
        while (self.gray.items.len > 0) {
            const gc = self.gray.items[self.gray.items.len - 1];
            self.gray.items.len -= 1;
            self.traceOne(gc);
        }
    }

    fn traceOne(self: *Heap, gc: *Gc) void {
        switch (gc.kind) {
            .string => {},
            .object => {
                const o: *Obj = @fieldParentPtr("gc", gc);
                self.markObj(o.proto);
                for (o.props.entries.items) |*p| {
                    if (p.dead) continue;
                    self.markValue(p.value);
                    self.markObj(p.getter);
                    self.markObj(p.setter);
                }
                switch (o.data) {
                    .func => |f| {
                        self.markEnv(f.env);
                        if (f.bound_this) |t| self.markValue(t);
                        for (f.bound_args) |a| self.markValue(a);
                        self.markObj(f.bound_target);
                        self.markObj(f.home);
                        self.markObj(f.fields);
                        // `f.proto` is not traced here: compiled bodies are
                        // permanent and the VM roots every one of them once,
                        // which is cheaper than re-walking the constant pool
                        // for every closure the heap contains.
                    },
                    .elements => |els| for (els.items) |v| self.markValue(v),
                    .entries => |ents| for (ents.items) |e| {
                        self.markValue(e.key);
                        self.markValue(e.value);
                    },
                    .promise => |p| {
                        self.markValue(p.value);
                        for (p.reactions.items) |r| {
                            self.markObj(r.on_ok);
                            self.markObj(r.on_err);
                            self.markObj(r.next);
                        }
                    },
                    .boxed => |v| self.markValue(v),
                    else => {},
                }
            },
            .env => {
                const e: *Env = @fieldParentPtr("gc", gc);
                self.markEnv(e.parent);
                for (e.slots) |v| self.markValue(v);
            },
        }
    }

    /// Sweep, calling `release` for every host object that did not survive so
    /// the embedder can drop its reference.
    pub fn sweep(self: *Heap, ctx: ?*anyopaque, release: ?*const fn (?*anyopaque, u64) void) void {
        self.drainGray();
        var live: usize = 0;
        var cur = self.all;
        var prev: ?*Gc = null;
        while (cur) |gc| {
            const next = gc.next;
            if (gc.marked) {
                gc.marked = false;
                prev = gc;
                live += self.sizeOf(gc);
            } else {
                if (prev) |p| p.next = next else self.all = next;
                self.free(gc, ctx, release);
            }
            cur = next;
        }
        self.bytes = live;
        self.threshold = @max(@as(usize, 1) << 20, live * 2);
        self.pending = false;
    }

    fn sizeOf(self: *Heap, gc: *Gc) usize {
        _ = self;
        return switch (gc.kind) {
            .string => blk: {
                const s: *Str = @fieldParentPtr("gc", gc);
                break :blk @sizeOf(Str) + s.bytes.len;
            },
            .object => blk: {
                const o: *Obj = @fieldParentPtr("gc", gc);
                var n: usize = @sizeOf(Obj) + o.props.entries.items.len * @sizeOf(Prop);
                switch (o.data) {
                    .elements => |e| n += e.items.len * @sizeOf(Value),
                    .entries => |e| n += e.items.len * @sizeOf(MapEntry),
                    else => {},
                }
                break :blk n;
            },
            .env => blk: {
                const e: *Env = @fieldParentPtr("gc", gc);
                break :blk @sizeOf(Env) + e.slots.len * (@sizeOf(Value) + 1);
            },
        };
    }

    fn free(self: *Heap, gc: *Gc, ctx: ?*anyopaque, release: ?*const fn (?*anyopaque, u64) void) void {
        switch (gc.kind) {
            .string => {
                const s: *Str = @fieldParentPtr("gc", gc);
                self.alloc.free(s.bytes);
                self.alloc.destroy(s);
            },
            .object => {
                const o: *Obj = @fieldParentPtr("gc", gc);
                o.props.deinit(self.alloc);
                switch (o.data) {
                    .elements => |*e| @constCast(e).deinit(self.alloc),
                    .entries => |*e| @constCast(e).deinit(self.alloc),
                    .promise => |p| {
                        p.reactions.deinit(self.alloc);
                        self.alloc.destroy(p);
                    },
                    .regex => |r| {
                        self.alloc.free(r.source);
                        self.alloc.free(r.flags);
                        if (self.regex_free) |rf| rf(self.alloc, r.prog);
                        self.alloc.destroy(r);
                    },
                    .func => |f| {
                        if (f.bound_args.len > 0) self.alloc.free(f.bound_args);
                        if (f.is_host) {
                            if (release) |r| r(ctx, f.host);
                        }
                    },
                    .host => |h| if (release) |r| r(ctx, h),
                    else => {},
                }
                self.alloc.destroy(o);
            },
            .env => {
                const e: *Env = @fieldParentPtr("gc", gc);
                self.alloc.free(e.slots);
                self.alloc.free(e.ready);
                self.alloc.destroy(e);
            },
        }
    }

    /// Tear the whole heap down. Nothing is marked, so everything goes.
    pub fn deinit(self: *Heap, ctx: ?*anyopaque, release: ?*const fn (?*anyopaque, u64) void) void {
        var cur = self.all;
        while (cur) |gc| {
            const next = gc.next;
            self.free(gc, ctx, release);
            cur = next;
        }
        self.all = null;
        self.gray.deinit(self.alloc);
    }
};
