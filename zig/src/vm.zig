//! The interpreter: conversions, property access, calls and the dispatch loop.
//!
//! One rule governs memory here. A collection may only start at the top of the
//! dispatch loop, and at that point every live value is reachable from the
//! value stack, a frame, the globals, a queued job, a suspended coroutine or
//! the embedder's handle table. Nothing is stranded in a Zig local. The one
//! exception is a native builtin that calls back into JavaScript -- `map`,
//! `reduce`, a promise reaction -- because re-entering the loop re-enters a
//! safe point. Those, and only those, park their temporaries in `Vm.temps`.

const std = @import("std");
const val = @import("value.zig");
const bc = @import("bytecode.zig");
const ast = @import("ast.zig");
const Parser = @import("parser.zig").Parser;
const compiler = @import("compiler.zig");
const regex = @import("regex.zig");

pub const Value = val.Value;
pub const Obj = val.Obj;
pub const Str = val.Str;
pub const Env = val.Env;
const Op = bc.Op;
const Proto = val.Proto;

pub const Error = error{ JsThrow, OutOfMemory, StackOverflow };

const stack_limit = 1 << 16;
const frame_limit = 900;

pub const Frame = struct {
    func: *Obj,
    proto: *Proto,
    pc: usize,
    /// Where the callee, `this` and the arguments start on the value stack.
    base: usize,
    args_start: usize,
    argc: u32,
    bp: usize,
    env: *Env,
    this: Value,
    args_obj: ?*Obj = null,
    result_promise: ?*Obj = null,
    home: ?*Obj = null,
    new_target: ?*Obj = null,
};

/// A suspended async function: everything needed to put its frame back.
pub const Coro = struct {
    func: *Obj,
    proto: *Proto,
    pc: usize,
    env: *Env,
    this: Value,
    home: ?*Obj,
    saved: []Value,
    args: []Value,
    result_promise: *Obj,
    done: bool = false,
};

pub const Timer = struct {
    id: u32,
    at: f64,
    interval: f64,
    fn_val: Value,
    args: []Value,
    repeating: bool,
    cancelled: bool = false,
};

pub const Job = struct { fn_val: Value, arg: Value, kind: enum { call, resolve_reaction } };

/// Callbacks into the embedder. All of them are synchronous and must not
/// re-enter the engine except through the documented entry points.
pub const HostVTable = struct {
    ctx: ?*anyopaque = null,
    get: ?*const fn (?*anyopaque, u64, [*]const u8, u32, *CValue) callconv(.c) void = null,
    set: ?*const fn (?*anyopaque, u64, [*]const u8, u32, *const CValue) callconv(.c) void = null,
    call: ?*const fn (?*anyopaque, u64, *const CValue, [*]const CValue, u32, *CValue) callconv(.c) void = null,
    construct: ?*const fn (?*anyopaque, u64, [*]const CValue, u32, *CValue) callconv(.c) void = null,
    release: ?*const fn (?*anyopaque, u64) callconv(.c) void = null,
};

/// The wire format for a value crossing the C ABI. Strings are borrowed for
/// the duration of the call and copied by whichever side receives them.
pub const CValue = extern struct {
    tag: i32 = 0,
    len: u32 = 0,
    num: f64 = 0,
    ptr: u64 = 0,

    pub const undef: i32 = 0;
    pub const nul: i32 = 1;
    pub const boolean: i32 = 2;
    pub const number: i32 = 3;
    pub const string: i32 = 4;
    /// A handle owned by the embedder.
    pub const host: i32 = 5;
    /// A handle into the engine's own table.
    pub const js: i32 = 6;
    /// The embedder raised an exception; `ptr`/`len` carry the message.
    pub const throw: i32 = 7;
};

pub const Vm = struct {
    gpa: std.mem.Allocator,
    heap: val.Heap,
    stack: []Value,
    sp: usize = 0,
    frames: []Frame,
    fp: usize = 0,
    temps: std.ArrayListUnmanaged(Value) = .{},

    globals: *Obj = undefined,
    exception: Value = .undefined,
    completion: Value = .undefined,

    // Prototypes every builtin hangs off.
    object_proto: *Obj = undefined,
    function_proto: *Obj = undefined,
    array_proto: *Obj = undefined,
    string_proto: *Obj = undefined,
    number_proto: *Obj = undefined,
    boolean_proto: *Obj = undefined,
    error_proto: *Obj = undefined,
    regexp_proto: *Obj = undefined,
    date_proto: *Obj = undefined,
    map_proto: *Obj = undefined,
    set_proto: *Obj = undefined,
    promise_proto: *Obj = undefined,

    scripts: std.ArrayListUnmanaged(*compiler.Script) = .{},
    coros: std.ArrayListUnmanaged(*Coro) = .{},
    pending_reactions: std.ArrayListUnmanaged(PendingReaction) = .{},
    timers: std.ArrayListUnmanaged(Timer) = .{},
    next_timer_id: u32 = 1,
    clock: f64 = 0,
    logs: std.ArrayListUnmanaged([]u8) = .{},

    /// Values handed to the embedder, kept alive until it releases them.
    handles: std.ArrayListUnmanaged(Value) = .{},
    free_handles: std.ArrayListUnmanaged(u32) = .{},
    /// Wrappers for embedder objects, so the same host object is the same
    /// JavaScript object every time it comes across.
    host_objs: std.AutoHashMapUnmanaged(u64, *Obj) = .{},
    host: HostVTable = .{},

    /// Scratch used to hand strings back over the C ABI.
    out_buf: std.ArrayListUnmanaged(u8) = .{},
    /// Guards against the embedder re-entering while we are mid-collection.
    in_gc: bool = false,

    pub fn create(gpa: std.mem.Allocator) !*Vm {
        const vm = try gpa.create(Vm);
        vm.* = .{
            .gpa = gpa,
            .heap = val.Heap.init(gpa),
            .stack = try gpa.alloc(Value, stack_limit),
            .frames = try gpa.alloc(Frame, frame_limit),
        };
        vm.heap.regex_free = regexFree;
        try @import("builtins.zig").install(vm);
        return vm;
    }

    fn regexFree(alloc: std.mem.Allocator, prog: *anyopaque) void {
        const re: *regex.Regex = @ptrCast(@alignCast(prog));
        re.deinit(alloc);
        alloc.destroy(re);
    }

    pub fn destroy(self: *Vm) void {
        const gpa = self.gpa;
        self.heap.deinit(self, releaseHostThunk);
        for (self.scripts.items) |s| {
            s.deinit();
            gpa.destroy(s);
        }
        self.scripts.deinit(gpa);
        for (self.coros.items) |c| {
            gpa.free(c.saved);
            gpa.free(c.args);
            gpa.destroy(c);
        }
        self.coros.deinit(gpa);
        self.pending_reactions.deinit(gpa);
        for (self.timers.items) |t| gpa.free(t.args);
        self.timers.deinit(gpa);
        for (self.logs.items) |l| gpa.free(l);
        self.logs.deinit(gpa);
        self.handles.deinit(gpa);
        self.free_handles.deinit(gpa);
        self.host_objs.deinit(gpa);
        self.temps.deinit(gpa);
        self.out_buf.deinit(gpa);
        gpa.free(self.stack);
        gpa.free(self.frames);
        gpa.destroy(self);
    }

    fn releaseHostThunk(ctx: ?*anyopaque, handle: u64) void {
        const self: *Vm = @ptrCast(@alignCast(ctx.?));
        _ = self.host_objs.remove(handle);
        if (self.host.release) |r| r(self.host.ctx, handle);
    }

    // -- stack -------------------------------------------------------------

    pub fn push(self: *Vm, v: Value) !void {
        if (self.sp >= stack_limit) return error.StackOverflow;
        self.stack[self.sp] = v;
        self.sp += 1;
    }

    pub fn pop(self: *Vm) Value {
        if (self.sp == 0) return .undefined;
        self.sp -= 1;
        return self.stack[self.sp];
    }

    pub fn peek(self: *Vm, n: usize) Value {
        if (self.sp <= n) return .undefined;
        return self.stack[self.sp - 1 - n];
    }

    // -- allocation helpers ------------------------------------------------

    pub fn str(self: *Vm, bytes: []const u8) !Value {
        return .{ .string = try self.heap.newStr(bytes) };
    }

    pub fn adopt(self: *Vm, bytes: []u8) !Value {
        return .{ .string = try self.heap.adoptStr(bytes) };
    }

    pub fn newObject(self: *Vm) !*Obj {
        return self.heap.newObj(.plain, self.object_proto);
    }

    pub fn newArray(self: *Vm, items: []const Value) !*Obj {
        const o = try self.heap.newObj(.array, self.array_proto);
        o.data = .{ .elements = .{} };
        if (items.len > 0) try o.data.elements.appendSlice(self.heap.alloc, items);
        return o;
    }

    /// The source line the innermost running frame is on, if there is one.
    /// Every opcode that can throw writes its `pc` back to the frame first --
    /// the handler search in `unwind` needs that anyway -- so the frame's `pc`
    /// is trustworthy at exactly the moments an error gets built.
    pub fn currentLine(self: *Vm) ?u32 {
        if (self.fp == 0) return null;
        const fr = &self.frames[self.fp - 1];
        var line: ?u32 = null;
        for (fr.proto.lines) |e| {
            if (e.offset >= fr.pc) break;
            line = e.line;
        }
        return line;
    }

    pub fn newError(self: *Vm, name: []const u8, message: []const u8) !*Obj {
        const o = try self.heap.newObj(.err, self.error_proto);
        try o.props.put(self.heap.alloc, "name", try self.str(name));
        try o.props.put(self.heap.alloc, "message", try self.str(message));
        // `message` stays exactly what was asked for, because scripts compare
        // it. The line goes somewhere only the host and a human reading a
        // stack will look.
        const line = self.currentLine();
        const both = if (line) |n|
            try std.fmt.allocPrint(self.heap.alloc, "{s}: {s} (line {d})", .{ name, message, n })
        else
            try std.fmt.allocPrint(self.heap.alloc, "{s}: {s}", .{ name, message });
        try o.props.put(self.heap.alloc, "stack", try self.adopt(both));
        if (line) |n| try o.props.putProp(self.heap.alloc, "__line__", .{
            .key = undefined,
            .value = .{ .number = @floatFromInt(n) },
            .enumerable = false,
        });
        return o;
    }

    pub fn throwError(self: *Vm, name: []const u8, comptime fmt: []const u8, args: anytype) Error {
        const msg = std.fmt.allocPrint(self.gpa, fmt, args) catch "error";
        defer if (msg.len > 0 and !std.mem.eql(u8, msg, "error")) self.gpa.free(msg);
        const o = try self.newError(name, msg);
        self.exception = .{ .object = o };
        return error.JsThrow;
    }

    pub fn throwType(self: *Vm, comptime fmt: []const u8, args: anytype) Error {
        return self.throwError("TypeError", fmt, args);
    }

    pub fn throwValue(self: *Vm, v: Value) Error {
        self.exception = v;
        return error.JsThrow;
    }

    pub fn newNative(self: *Vm, name: []const u8, n: u32, f: val.NativeFn) !*Obj {
        const o = try self.heap.newObj(.function, self.function_proto);
        o.data = .{ .func = .{ .native = f, .n_args = n } };
        try o.props.putProp(self.heap.alloc, "name", .{
            .key = undefined,
            .value = try self.str(name),
            .enumerable = false,
        });
        try o.props.putProp(self.heap.alloc, "length", .{
            .key = undefined,
            .value = .{ .number = @floatFromInt(n) },
            .enumerable = false,
        });
        return o;
    }

    pub fn define(self: *Vm, o: *Obj, name: []const u8, v: Value) !void {
        try o.props.putProp(self.heap.alloc, name, .{ .key = undefined, .value = v, .enumerable = false });
    }

    pub fn defineFn(self: *Vm, o: *Obj, name: []const u8, n: u32, f: val.NativeFn) !void {
        const nf = try self.newNative(name, n, f);
        try self.define(o, name, .{ .object = nf });
    }

    // -- conversions -------------------------------------------------------

    pub fn truthy(self: *Vm, v: Value) bool {
        _ = self;
        return switch (v) {
            .undefined, .null => false,
            .boolean => |b| b,
            .number => |n| n != 0 and !std.math.isNan(n),
            .string => |s| s.bytes.len > 0,
            .object => true,
        };
    }

    pub fn toNumber(self: *Vm, v: Value) Error!f64 {
        return switch (v) {
            .undefined => std.math.nan(f64),
            .null => 0,
            .boolean => |b| if (b) 1 else 0,
            .number => |n| n,
            .string => |s| parseNumber(s.bytes),
            .object => blk: {
                const p = try self.toPrimitive(v, .number);
                if (p == .object) break :blk std.math.nan(f64);
                break :blk try self.toNumber(p);
            },
        };
    }

    pub fn toInt32(self: *Vm, v: Value) Error!i32 {
        return doubleToI32(try self.toNumber(v));
    }

    pub fn toUint32(self: *Vm, v: Value) Error!u32 {
        return @bitCast(doubleToI32(try self.toNumber(v)));
    }

    pub const Hint = enum { number, string, default };

    pub fn toPrimitive(self: *Vm, v: Value, hint: Hint) Error!Value {
        const o = switch (v) {
            .object => |x| x,
            else => return v,
        };
        if (o.class == .host) {
            // Host objects stringify through their own bridge if they offer
            // it, and otherwise become "[object Object]" like anything else.
            const s = try self.hostGet(o, "toString");
            if (s.isCallable()) {
                const r = try self.callValue(s, v, &.{});
                if (r != .object) return r;
            }
            return self.str("[object Object]");
        }
        const order: [2][]const u8 = if (hint == .string)
            .{ "toString", "valueOf" }
        else
            .{ "valueOf", "toString" };
        for (order) |name| {
            const m = try self.getProp(v, name);
            if (m.isCallable()) {
                const r = try self.callValue(m, v, &.{});
                if (r != .object) return r;
            }
        }
        return self.throwType("cannot convert object to primitive value", .{});
    }

    pub fn toString(self: *Vm, v: Value) Error!Value {
        return switch (v) {
            .string => v,
            .undefined => self.str("undefined"),
            .null => self.str("null"),
            .boolean => |b| self.str(if (b) "true" else "false"),
            .number => |n| {
                const bytes = numberToString(self.heap.alloc, n) catch return error.OutOfMemory;
                return self.adopt(bytes);
            },
            .object => blk: {
                const p = try self.toPrimitive(v, .string);
                break :blk try self.toString(p);
            },
        };
    }

    /// The bytes of `toString(v)`, valid until the next allocation that could
    /// collect. Callers that hold it across a call must root the string.
    pub fn toSlice(self: *Vm, v: Value) Error![]const u8 {
        const s = try self.toString(v);
        return s.string.bytes;
    }

    pub fn typeOf(self: *Vm, v: Value) []const u8 {
        _ = self;
        return switch (v) {
            .undefined => "undefined",
            .null => "object",
            .boolean => "boolean",
            .number => "number",
            .string => "string",
            .object => |o| if (o.callable()) "function" else "object",
        };
    }

    // -- equality ----------------------------------------------------------

    pub fn strictEquals(self: *Vm, a: Value, b: Value) bool {
        _ = self;
        return switch (a) {
            .undefined => b == .undefined,
            .null => b == .null,
            .boolean => |x| b == .boolean and b.boolean == x,
            .number => |x| b == .number and x == b.number,
            .string => |x| b == .string and std.mem.eql(u8, x.bytes, b.string.bytes),
            .object => |x| b == .object and b.object == x,
        };
    }

    pub fn looseEquals(self: *Vm, a: Value, b: Value) Error!bool {
        if (std.meta.activeTag(a) == std.meta.activeTag(b)) return self.strictEquals(a, b);
        if (a.isNullish() and b.isNullish()) return true;
        if (a.isNullish() or b.isNullish()) return false;
        if (a == .object) return self.looseEquals(try self.toPrimitive(a, .default), b);
        if (b == .object) return self.looseEquals(a, try self.toPrimitive(b, .default));
        const x = try self.toNumber(a);
        const y = try self.toNumber(b);
        return x == y;
    }

    // -- property access ---------------------------------------------------

    pub fn getProp(self: *Vm, base: Value, name: []const u8) Error!Value {
        switch (base) {
            .undefined, .null => return self.throwType(
                "cannot read properties of {s} (reading '{s}')",
                .{ if (base == .null) "null" else "undefined", name },
            ),
            .string => |s| {
                if (std.mem.eql(u8, name, "length")) {
                    return .{ .number = @floatFromInt(utf16Length(s.bytes)) };
                }
                if (indexOfKey(name)) |i| {
                    return self.stringCharAt(s.bytes, i);
                }
                return self.lookup(self.string_proto, name, base);
            },
            .number => return self.lookup(self.number_proto, name, base),
            .boolean => return self.lookup(self.boolean_proto, name, base),
            .object => |o| return self.getObjProp(o, name, base),
        }
    }

    fn getObjProp(self: *Vm, o: *Obj, name: []const u8, this: Value) Error!Value {
        if (o.class == .host) return self.hostGet(o, name);
        if (o.class == .array) {
            if (std.mem.eql(u8, name, "length")) {
                return .{ .number = @floatFromInt(o.data.elements.items.len) };
            }
            if (indexOfKey(name)) |i| {
                const els = o.data.elements.items;
                if (i < els.len) return els[i];
                return .undefined;
            }
        }
        if (o.class == .function and o.data.func.is_host) {
            const v = try self.hostGet(o, name);
            if (v != .undefined) return v;
        }
        return self.lookup(o, name, this);
    }

    fn lookup(self: *Vm, start: ?*Obj, name: []const u8, this: Value) Error!Value {
        var cur = start;
        while (cur) |o| {
            if (o.props.find(name)) |p| {
                if (p.is_accessor) {
                    if (p.getter) |g| return self.callValue(.{ .object = g }, this, &.{});
                    return .undefined;
                }
                return p.value;
            }
            if (o.class == .host) return self.hostGet(o, name);
            cur = o.proto;
        }
        return .undefined;
    }

    pub fn setProp(self: *Vm, base: Value, name: []const u8, v: Value) Error!void {
        const o = switch (base) {
            .object => |x| x,
            .undefined, .null => return self.throwType(
                "cannot set properties of {s}",
                .{if (base == .null) "null" else "undefined"},
            ),
            else => return, // primitives silently swallow writes
        };
        if (o.class == .host or (o.class == .function and o.data.func.is_host)) {
            return self.hostSet(o, name, v);
        }
        if (o.class == .array) {
            if (std.mem.eql(u8, name, "length")) {
                const n = try self.toNumber(v);
                const want: usize = if (n < 0 or std.math.isNan(n)) 0 else @intFromFloat(@min(n, 1e7));
                const els = &o.data.elements;
                if (want < els.items.len) {
                    els.items.len = want;
                } else {
                    while (els.items.len < want) try els.append(self.heap.alloc, .undefined);
                }
                return;
            }
            if (indexOfKey(name)) |i| {
                const els = &o.data.elements;
                if (i >= 1 << 24) return; // absurd index: keep it as a property
                while (els.items.len <= i) try els.append(self.heap.alloc, .undefined);
                els.items[i] = v;
                return;
            }
        }
        // An accessor anywhere on the prototype chain wins over a plain write.
        var cur: ?*Obj = o;
        while (cur) |c| {
            if (c.props.find(name)) |p| {
                if (p.is_accessor) {
                    if (p.setter) |s| _ = try self.callValue(.{ .object = s }, base, &.{v});
                    return;
                }
                break;
            }
            cur = c.proto;
        }
        if (!o.extensible and o.props.find(name) == null) return;
        try o.props.put(self.heap.alloc, name, v);
    }

    pub fn getIndex(self: *Vm, base: Value, key: Value) Error!Value {
        if (base == .object and base.object.class == .array) {
            if (key == .number) {
                const n = key.number;
                if (n >= 0 and n == @trunc(n)) {
                    const i: usize = @intFromFloat(n);
                    const els = base.object.data.elements.items;
                    if (i < els.len) return els[i];
                }
            }
        }
        if (base == .string and key == .number) {
            const n = key.number;
            if (n >= 0 and n == @trunc(n)) return self.stringCharAt(base.string.bytes, @intFromFloat(n));
        }
        const k = try self.toString(key);
        return self.getProp(base, k.string.bytes);
    }

    pub fn setIndex(self: *Vm, base: Value, key: Value, v: Value) Error!void {
        if (base == .object and base.object.class == .array and key == .number) {
            const n = key.number;
            if (n >= 0 and n == @trunc(n) and n < (1 << 24)) {
                const i: usize = @intFromFloat(n);
                const els = &base.object.data.elements;
                while (els.items.len <= i) try els.append(self.heap.alloc, .undefined);
                els.items[i] = v;
                return;
            }
        }
        const k = try self.toString(key);
        return self.setProp(base, k.string.bytes, v);
    }

    fn stringCharAt(self: *Vm, bytes: []const u8, i: usize) Error!Value {
        var seen: usize = 0;
        var p: usize = 0;
        while (p < bytes.len) {
            const n = std.unicode.utf8ByteSequenceLength(bytes[p]) catch 1;
            const width: usize = if (n == 4) 2 else 1;
            if (seen == i or (width == 2 and seen + 1 == i)) {
                return self.str(bytes[p..@min(p + n, bytes.len)]);
            }
            seen += width;
            p += n;
        }
        return .undefined;
    }

    // -- host bridge -------------------------------------------------------

    pub fn hostWrap(self: *Vm, handle: u64, callable: bool) !Value {
        if (self.host_objs.get(handle)) |o| return .{ .object = o };
        const o = if (callable) blk: {
            const f = try self.heap.newObj(.function, self.function_proto);
            f.data = .{ .func = .{ .is_host = true, .host = handle } };
            break :blk f;
        } else blk: {
            const h = try self.heap.newObj(.host, null);
            h.data = .{ .host = handle };
            break :blk h;
        };
        try self.host_objs.put(self.gpa, handle, o);
        return .{ .object = o };
    }

    fn hostHandle(o: *Obj) u64 {
        return switch (o.data) {
            .host => |h| h,
            .func => |f| f.host,
            else => 0,
        };
    }

    fn hostGet(self: *Vm, o: *Obj, name: []const u8) Error!Value {
        const get = self.host.get orelse return .undefined;
        var out = CValue{};
        get(self.host.ctx, hostHandle(o), name.ptr, @intCast(name.len), &out);
        return self.fromC(out);
    }

    fn hostSet(self: *Vm, o: *Obj, name: []const u8, v: Value) Error!void {
        const set = self.host.set orelse return;
        var cv = try self.toC(v);
        set(self.host.ctx, hostHandle(o), name.ptr, @intCast(name.len), &cv);
    }

    pub fn hostCall(self: *Vm, o: *Obj, this: Value, args: []const Value) Error!Value {
        const call = self.host.call orelse return .undefined;
        var buf: [16]CValue = undefined;
        const cargs = if (args.len <= buf.len)
            buf[0..args.len]
        else
            try self.gpa.alloc(CValue, args.len);
        defer if (args.len > buf.len) self.gpa.free(cargs);
        for (args, 0..) |a, i| cargs[i] = try self.toC(a);
        var cthis = try self.toC(this);
        var out = CValue{};
        call(self.host.ctx, hostHandle(o), &cthis, cargs.ptr, @intCast(args.len), &out);
        return self.fromC(out);
    }

    fn hostConstruct(self: *Vm, o: *Obj, args: []const Value) Error!Value {
        const ctor = self.host.construct orelse return self.hostCall(o, .undefined, args);
        var buf: [16]CValue = undefined;
        const cargs = if (args.len <= buf.len)
            buf[0..args.len]
        else
            try self.gpa.alloc(CValue, args.len);
        defer if (args.len > buf.len) self.gpa.free(cargs);
        for (args, 0..) |a, i| cargs[i] = try self.toC(a);
        var out = CValue{};
        ctor(self.host.ctx, hostHandle(o), cargs.ptr, @intCast(args.len), &out);
        return self.fromC(out);
    }

    pub fn toC(self: *Vm, v: Value) !CValue {
        return switch (v) {
            .undefined => CValue{ .tag = CValue.undef },
            .null => CValue{ .tag = CValue.nul },
            .boolean => |b| CValue{ .tag = CValue.boolean, .num = if (b) 1 else 0 },
            .number => |n| CValue{ .tag = CValue.number, .num = n },
            .string => |s| CValue{
                .tag = CValue.string,
                .ptr = @intFromPtr(s.bytes.ptr),
                .len = @intCast(s.bytes.len),
            },
            .object => |o| blk: {
                if (o.class == .host) break :blk CValue{ .tag = CValue.host, .ptr = o.data.host };
                if (o.class == .function and o.data.func.is_host) {
                    break :blk CValue{ .tag = CValue.host, .ptr = o.data.func.host };
                }
                break :blk CValue{ .tag = CValue.js, .ptr = try self.newHandle(v) };
            },
        };
    }

    pub fn fromC(self: *Vm, c: CValue) Error!Value {
        return switch (c.tag) {
            CValue.undef => .undefined,
            CValue.nul => .null,
            CValue.boolean => .{ .boolean = c.num != 0 },
            CValue.number => .{ .number = c.num },
            CValue.string => try self.str(@as([*]const u8, @ptrFromInt(c.ptr))[0..c.len]),
            CValue.host => try self.hostWrap(c.ptr, c.num != 0),
            CValue.js => self.handleValue(@intCast(c.ptr)),
            CValue.throw => blk: {
                const msg = if (c.len > 0) @as([*]const u8, @ptrFromInt(c.ptr))[0..c.len] else "host error";
                const e = try self.newError("Error", msg);
                self.exception = .{ .object = e };
                break :blk error.JsThrow;
            },
            else => .undefined,
        };
    }

    pub fn newHandle(self: *Vm, v: Value) !u64 {
        if (self.free_handles.items.len > 0) {
            const i = self.free_handles.items[self.free_handles.items.len - 1];
            self.free_handles.items.len -= 1;
            self.handles.items[i] = v;
            return i;
        }
        try self.handles.append(self.gpa, v);
        return self.handles.items.len - 1;
    }

    pub fn handleValue(self: *Vm, i: usize) Value {
        if (i >= self.handles.items.len) return .undefined;
        return self.handles.items[i];
    }

    pub fn releaseHandle(self: *Vm, i: usize) void {
        if (i >= self.handles.items.len) return;
        self.handles.items[i] = .undefined;
        self.free_handles.append(self.gpa, @intCast(i)) catch {};
    }

    // -- calling -----------------------------------------------------------

    pub fn callValue(self: *Vm, f: Value, this: Value, args: []const Value) Error!Value {
        const o = switch (f) {
            .object => |x| x,
            else => return self.throwType("{s} is not a function", .{self.typeOf(f)}),
        };
        if (!o.callable()) return self.throwType("value is not a function", .{});
        const base = self.sp;
        try self.push(f);
        try self.push(this);
        for (args) |a| try self.push(a);
        try self.invoke(o, @intCast(args.len), base, false, null);
        if (self.fp == 0 or self.frames[self.fp - 1].base != base) {
            // A native ran to completion and left its result on the stack.
            return self.pop();
        }
        try self.runFrames(self.fp - 1);
        return self.pop();
    }

    /// Set up the call whose callee sits at `base` on the stack. Either pushes
    /// a frame (JavaScript) or leaves the result on the stack (native).
    fn invoke(self: *Vm, o: *Obj, argc: u32, base: usize, is_new: bool, new_this: ?Value) Error!void {
        const f = o.data.func;
        if (f.bound_target) |target| {
            // Rebuild the call with the bound receiver and leading arguments.
            const args = self.stack[base + 2 ..][0..argc];
            var all = std.ArrayListUnmanaged(Value){};
            defer all.deinit(self.gpa);
            try all.appendSlice(self.gpa, f.bound_args);
            try all.appendSlice(self.gpa, args);
            const this = if (is_new) (new_this orelse .undefined) else (f.bound_this orelse .undefined);
            self.sp = base;
            try self.push(.{ .object = target });
            try self.push(this);
            for (all.items) |a| try self.push(a);
            return self.invoke(target, @intCast(all.items.len), base, is_new, new_this);
        }
        if (f.is_host) {
            const args = self.stack[base + 2 ..][0..argc];
            const this = self.stack[base + 1];
            const r = if (is_new)
                try self.hostConstruct(o, args)
            else
                try self.hostCall(o, this, args);
            self.sp = base;
            try self.push(r);
            return;
        }
        if (f.native) |nf| {
            const args = self.stack[base + 2 ..][0..argc];
            const this = self.stack[base + 1];
            // `temps` is scratch rooting for the duration of one native, and
            // unwinding it here is what makes that true even for a native
            // that throws part-way through.
            const tmark = self.temps.items.len;
            defer self.temps.items.len = tmark;
            const r = nf(self, o, this, args) catch |e| switch (e) {
                error.JsThrow => return error.JsThrow,
                error.OutOfMemory => return error.OutOfMemory,
                error.StackOverflow => return error.StackOverflow,
                else => return self.throwError("Error", "internal error", .{}),
            };
            self.sp = base;
            try self.push(r);
            return;
        }
        const proto = f.proto orelse return self.throwType("value is not a function", .{});
        if (self.fp >= frame_limit) return self.throwError("RangeError", "maximum call stack size exceeded", .{});

        const env = try self.heap.newEnv(f.env, proto.n_slots);
        if (proto.simple_params) {
            const args = self.stack[base + 2 ..][0..argc];
            var i: usize = 0;
            while (i < proto.n_params and i < env.slots.len) : (i += 1) {
                if (i < args.len) env.slots[i] = args[i];
                env.ready[i] = true;
            }
        }
        for (env.ready, 0..) |_, i| {
            if (i >= proto.n_params) env.ready[i] = true;
        }
        var this = self.stack[base + 1];
        if (proto.kind == .arrow) {
            this = f.bound_this orelse .undefined;
        } else if (is_new) {
            this = new_this.?;
        }
        self.frames[self.fp] = .{
            .func = o,
            .proto = proto,
            .pc = 0,
            .base = base,
            .args_start = base + 2,
            .argc = argc,
            .bp = base + 2 + argc,
            .env = env,
            .this = this,
            .home = if (proto.kind == .arrow) f.home else f.home,
            .new_target = if (is_new) o else null,
        };
        if (proto.is_async) {
            const p = try self.newPromise();
            self.frames[self.fp].result_promise = p;
        }
        self.fp += 1;
        self.sp = base + 2 + argc;
    }

    pub fn construct(self: *Vm, ctor: Value, args: []const Value) Error!Value {
        const o = switch (ctor) {
            .object => |x| x,
            else => return self.throwType("{s} is not a constructor", .{self.typeOf(ctor)}),
        };
        if (!o.callable()) return self.throwType("value is not a constructor", .{});
        if (o.class == .function and o.data.func.is_host) {
            return self.hostConstruct(o, args);
        }
        const base = self.sp;
        try self.push(ctor);
        try self.push(.undefined);
        for (args) |a| try self.push(a);
        try self.constructAt(o, @intCast(args.len), base);
        if (self.fp == 0 or self.frames[self.fp - 1].base != base) return self.pop();
        try self.runFrames(self.fp - 1);
        return self.pop();
    }

    fn constructAt(self: *Vm, o: *Obj, argc: u32, base: usize) Error!void {
        if (o.data.func.native != null or o.data.func.is_host or o.data.func.bound_target != null) {
            // Natives build and return their own instance.
            if (o.data.func.native) |nf| {
                const args = self.stack[base + 2 ..][0..argc];
                const marker = try self.heap.newObj(.plain, self.object_proto);
                const tmark = self.temps.items.len;
                defer self.temps.items.len = tmark;
                const r = nf(self, o, .{ .object = marker }, args) catch |e| switch (e) {
                    error.JsThrow => return error.JsThrow,
                    error.OutOfMemory => return error.OutOfMemory,
                    else => return self.throwError("Error", "internal error", .{}),
                };
                self.sp = base;
                try self.push(r);
                return;
            }
            return self.invoke(o, argc, base, true, null);
        }
        const proto_val = try self.getProp(.{ .object = o }, "prototype");
        const instance = try self.heap.newObj(
            .plain,
            if (proto_val == .object) proto_val.object else self.object_proto,
        );
        try self.applyFields(o, instance);
        try self.invoke(o, argc, base, true, .{ .object = instance });
        if (self.fp > 0 and self.frames[self.fp - 1].base == base) {
            self.frames[self.fp - 1].new_target = o;
        }
    }

    fn applyFields(self: *Vm, ctor: *Obj, instance: *Obj) Error!void {
        if (ctor.proto) |parent| {
            if (parent.class == .function) try self.applyFields(parent, instance);
        }
        const fields = ctor.data.func.fields orelse return;
        for (fields.props.entries.items) |p| {
            if (p.dead) continue;
            try instance.props.put(self.heap.alloc, p.key, p.value);
        }
    }

    // -- promises ----------------------------------------------------------
    //
    // A reaction is not run where the promise settles; it is moved onto
    // `pending_reactions` and run by `drainJobs`. That is what makes the
    // ordering guarantee -- every `then` callback runs after the current
    // script finishes, in registration order -- fall out of the design rather
    // than having to be arranged for at each call site.

    pub fn newPromise(self: *Vm) !*Obj {
        const o = try self.heap.newObj(.promise, self.promise_proto);
        const d = try self.heap.alloc.create(val.PromiseData);
        d.* = .{};
        o.data = .{ .promise = d };
        return o;
    }

    pub fn resolvePromise(self: *Vm, p: *Obj, v: Value) Error!void {
        const d = p.data.promise;
        if (d.state != .pending) return;
        if (v == .object and v.object == p) {
            return self.rejectPromise(p, .{ .object = try self.newError(
                "TypeError",
                "chaining cycle detected for promise",
            ) });
        }
        // Resolving with a promise adopts its state, which is what makes
        // `return somePromise` inside `then` collapse instead of nesting.
        if (v == .object and v.object.class == .promise) {
            const inner = v.object.data.promise;
            switch (inner.state) {
                .pending => {
                    try inner.reactions.append(self.heap.alloc, .{
                        .on_ok = null,
                        .on_err = null,
                        .next = p,
                    });
                    inner.handled = true;
                    return;
                },
                .fulfilled => return self.resolvePromise(p, inner.value),
                .rejected => return self.rejectPromise(p, inner.value),
            }
        }
        if (v == .object) {
            const then = try self.getProp(v, "then");
            if (then.isCallable()) {
                const res = try self.newNative("", 1, thenableSettle);
                try self.define(res, "#p", .{ .object = p });
                try self.define(res, "#ok", .{ .boolean = true });
                const rej = try self.newNative("", 1, thenableSettle);
                try self.define(rej, "#p", .{ .object = p });
                try self.define(rej, "#ok", .{ .boolean = false });
                _ = try self.callValue(then, v, &.{ .{ .object = res }, .{ .object = rej } });
                return;
            }
        }
        d.state = .fulfilled;
        d.value = v;
        try self.settle(p);
    }

    pub fn rejectPromise(self: *Vm, p: *Obj, v: Value) Error!void {
        const d = p.data.promise;
        if (d.state != .pending) return;
        d.state = .rejected;
        d.value = v;
        try self.settle(p);
    }

    fn thenableSettle(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
        const self: *Vm = @ptrCast(@alignCast(ctx));
        _ = this;
        const p = (callee.props.find("#p") orelse return .undefined).value;
        const ok = (callee.props.find("#ok") orelse return .undefined).value;
        const v = if (args.len > 0) args[0] else Value.undefined;
        if (ok == .boolean and ok.boolean) {
            try self.resolvePromise(p.object, v);
        } else {
            try self.rejectPromise(p.object, v);
        }
        return .undefined;
    }

    /// Hand every registered reaction to the job queue.
    fn settle(self: *Vm, p: *Obj) Error!void {
        const d = p.data.promise;
        if (d.state == .pending or d.reactions.items.len == 0) return;
        for (d.reactions.items) |r| {
            try self.pending_reactions.append(self.gpa, .{
                .state = d.state,
                .value = d.value,
                .on_ok = r.on_ok,
                .on_err = r.on_err,
                .next = r.next,
            });
        }
        d.reactions.clearRetainingCapacity();
    }

    pub fn addReaction(self: *Vm, p: *Obj, on_ok: ?*Obj, on_err: ?*Obj, next: *Obj) Error!void {
        const d = p.data.promise;
        d.handled = true;
        try d.reactions.append(self.heap.alloc, .{ .on_ok = on_ok, .on_err = on_err, .next = next });
        if (d.state != .pending) try self.settle(p);
    }

    fn runReaction(self: *Vm, r: PendingReaction) Error!void {
        const handler = if (r.state == .fulfilled) r.on_ok else r.on_err;
        if (handler == null) {
            // A pass-through link: `then(null, f)` on success, or the
            // forwarding reaction that promise adoption installs.
            if (r.state == .fulfilled) {
                return self.resolvePromise(r.next, r.value);
            }
            return self.rejectPromise(r.next, r.value);
        }
        const result = self.callValue(.{ .object = handler.? }, .undefined, &.{r.value}) catch |e| {
            if (e != error.JsThrow) return e;
            const exc = self.exception;
            self.exception = .undefined;
            return self.rejectPromise(r.next, exc);
        };
        return self.resolvePromise(r.next, result);
    }

    /// Run every microtask, then every microtask they queued, until quiet.
    pub fn drainJobs(self: *Vm) void {
        var guard: usize = 0;
        while (self.pending_reactions.items.len > 0) {
            guard += 1;
            if (guard > 200_000) break; // a promise loop that never settles
            const r = self.pending_reactions.orderedRemove(0);
            self.runReaction(r) catch |e| {
                if (e == error.JsThrow) {
                    self.reportUncaught(self.exception);
                    self.exception = .undefined;
                }
            };
        }
    }

    /// Compile and run a script at the top level. The compiled `Script` is
    /// kept for the lifetime of the VM because its arena owns every `Proto`,
    /// and a closure made during the run can outlive the run itself.
    pub fn evaluate(self: *Vm, source: []const u8, source_name: []const u8) Error!Value {
        var arena = std.heap.ArenaAllocator.init(self.gpa);
        defer arena.deinit();
        var p = Parser.init(arena.allocator(), source);
        const program = p.parseProgram() catch |e| {
            if (e == error.OutOfMemory) return error.OutOfMemory;
            const pe = p.err orelse ast.ParseError{ .message = "syntax error", .line = 0, .column = 0 };
            return self.throwError("SyntaxError", "{s} (line {d})", .{ pe.message, pe.line });
        };
        const script = compiler.Compiler.compile(self.gpa, &self.heap, program, source_name, &p.lines) catch |e| {
            if (e == error.OutOfMemory) return error.OutOfMemory;
            return self.throwError("SyntaxError", "compile error", .{});
        };
        // Once the list has it the VM owns it, and it must stay owned even if
        // the run throws: a closure the script made can outlive the run, and
        // its code lives in the script's arena.
        self.scripts.append(self.gpa, script) catch {
            script.deinit();
            self.gpa.destroy(script);
            return error.OutOfMemory;
        };

        const fn_obj = try self.heap.newObj(.function, self.function_proto);
        fn_obj.data = .{ .func = .{ .proto = script.root, .env = null } };
        const base = self.sp;
        try self.push(.{ .object = fn_obj });
        try self.push(.{ .object = self.globals });
        try self.invoke(fn_obj, 0, base, false, null);
        try self.runFrames(self.fp - 1);
        return self.pop();
    }

    /// Move the clock forward and fire whatever timers that reaches. Timers
    /// are a browser-level concern, so the embedder decides when time passes;
    /// nothing here reads the wall clock.
    pub fn advance(self: *Vm, ms: f64) void {
        self.clock += ms;
        var guard: usize = 0;
        while (guard < 10_000) : (guard += 1) {
            var best: ?usize = null;
            for (self.timers.items, 0..) |t, i| {
                if (t.cancelled or t.at > self.clock) continue;
                if (best == null or t.at < self.timers.items[best.?].at) best = i;
            }
            const i = best orelse break;
            const t = self.timers.items[i];
            // Root the callback and its arguments before the timer entry --
            // which is what was keeping them alive -- goes away.
            const mark = self.temps.items.len;
            self.temps.append(self.gpa, t.fn_val) catch return;
            self.temps.appendSlice(self.gpa, t.args) catch return;
            const args = self.temps.items[mark + 1 ..];
            if (t.repeating) {
                self.timers.items[i].at += @max(t.interval, 1);
            } else {
                self.gpa.free(self.timers.orderedRemove(i).args);
            }
            _ = self.callValue(t.fn_val, .undefined, args) catch |e| {
                if (e == error.JsThrow) {
                    self.reportUncaught(self.exception);
                    self.exception = .undefined;
                }
            };
            self.temps.items.len = mark;
            self.drainJobs();
        }
        self.reapCancelled();
    }

    fn reapCancelled(self: *Vm) void {
        var i: usize = 0;
        while (i < self.timers.items.len) {
            if (self.timers.items[i].cancelled) {
                self.gpa.free(self.timers.orderedRemove(i).args);
            } else {
                i += 1;
            }
        }
    }

    /// An unhandled rejection or a throw from a job: the page keeps going, so
    /// the only thing to do is put it where `console.log` output goes.
    pub fn reportUncaught(self: *Vm, v: Value) void {
        const s = self.toString(v) catch return;
        const line = std.fmt.allocPrint(self.gpa, "Uncaught {s}", .{s.string.bytes}) catch return;
        self.logs.append(self.gpa, line) catch self.gpa.free(line);
    }

    // -- the dispatch loop -------------------------------------------------

    pub fn runFrames(self: *Vm, base_fp: usize) Error!void {
        while (self.fp > base_fp) {
            self.step(base_fp) catch |e| switch (e) {
                error.JsThrow => try self.unwind(base_fp),
                else => return e,
            };
        }
    }

    fn unwind(self: *Vm, base_fp: usize) Error!void {
        while (self.fp > base_fp) {
            const fr = &self.frames[self.fp - 1];
            const pc = fr.pc;
            for (fr.proto.handlers) |h| {
                if (pc > h.start and pc <= h.end) {
                    self.sp = fr.bp + h.depth;
                    try self.push(self.exception);
                    fr.pc = h.target;
                    self.exception = .undefined;
                    return;
                }
            }
            if (fr.result_promise) |p| {
                const exc = self.exception;
                self.exception = .undefined;
                const base = fr.base;
                self.fp -= 1;
                self.sp = base;
                try self.rejectPromise(p, exc);
                try self.push(.{ .object = p });
                return;
            }
            self.fp -= 1;
            self.sp = fr.base;
        }
        return error.JsThrow;
    }

    fn step(self: *Vm, base_fp: usize) Error!void {
        const fr = &self.frames[self.fp - 1];
        const code = fr.proto.code;
        const consts = fr.proto.consts;
        var pc = fr.pc;

        if (self.heap.pending and self.heap.enabled) {
            fr.pc = pc;
            self.collect();
        }

        // Run a slice of instructions before checking for a collection again.
        var budget: u32 = 4096;
        while (budget > 0) : (budget -= 1) {
            const op: Op = @enumFromInt(code[pc]);
            pc += 1;
            switch (op) {
                .push_const => {
                    const i = bc.readU32(code, pc);
                    pc += 4;
                    try self.push(consts[i]);
                },
                .push_undef => try self.push(.undefined),
                .push_null => try self.push(.null),
                .push_true => try self.push(.{ .boolean = true }),
                .push_false => try self.push(.{ .boolean = false }),
                .pop => _ = self.pop(),
                .dup => try self.push(self.peek(0)),
                .dup2 => {
                    const a = self.peek(1);
                    const b = self.peek(0);
                    try self.push(a);
                    try self.push(b);
                },
                .swap => {
                    const a = self.stack[self.sp - 1];
                    self.stack[self.sp - 1] = self.stack[self.sp - 2];
                    self.stack[self.sp - 2] = a;
                },
                .pick => {
                    const n = code[pc];
                    pc += 1;
                    try self.push(self.peek(n));
                },
                .drop_under => {
                    const n = code[pc];
                    pc += 1;
                    const top = self.stack[self.sp - 1];
                    self.sp -= n;
                    self.stack[self.sp - 1] = top;
                },
                .nop => {},

                .get_local => {
                    const d = bc.readU16(code, pc);
                    const s = bc.readU16(code, pc + 2);
                    pc += 4;
                    var e = fr.env;
                    var k: u16 = 0;
                    while (k < d) : (k += 1) e = e.parent.?;
                    try self.push(e.slots[s]);
                },
                .set_local, .init_local => {
                    const d = bc.readU16(code, pc);
                    const s = bc.readU16(code, pc + 2);
                    pc += 4;
                    var e = fr.env;
                    var k: u16 = 0;
                    while (k < d) : (k += 1) e = e.parent.?;
                    if (op == .init_local) {
                        e.slots[s] = self.pop();
                        e.ready[s] = true;
                    } else {
                        e.slots[s] = self.peek(0);
                    }
                },
                .get_global => {
                    const i = bc.readU32(code, pc);
                    pc += 4;
                    const name = consts[i].string.bytes;
                    if (self.globals.props.find(name)) |p| {
                        try self.push(p.value);
                    } else {
                        fr.pc = pc;
                        return self.throwError("ReferenceError", "{s} is not defined", .{name});
                    }
                },
                .typeof_global => {
                    const i = bc.readU32(code, pc);
                    pc += 4;
                    const name = consts[i].string.bytes;
                    const v: Value = if (self.globals.props.find(name)) |p| p.value else .undefined;
                    try self.push(try self.str(self.typeOf(v)));
                },
                .set_global => {
                    const i = bc.readU32(code, pc);
                    pc += 4;
                    try self.globals.props.put(self.heap.alloc, consts[i].string.bytes, self.peek(0));
                },
                .declare_var => {
                    const i = bc.readU32(code, pc);
                    pc += 4;
                    const name = consts[i].string.bytes;
                    if (self.globals.props.find(name) == null) {
                        try self.globals.props.put(self.heap.alloc, name, .undefined);
                    }
                },
                .delete_global => {
                    const i = bc.readU32(code, pc);
                    pc += 4;
                    _ = self.globals.props.remove(consts[i].string.bytes);
                    try self.push(.{ .boolean = true });
                },

                .get_prop, .get_prop_this => {
                    const i = bc.readU32(code, pc);
                    pc += 4;
                    fr.pc = pc;
                    const base = if (op == .get_prop) self.pop() else self.peek(0);
                    const v = try self.getProp(base, consts[i].string.bytes);
                    try self.push(v);
                },
                .set_prop => {
                    const i = bc.readU32(code, pc);
                    pc += 4;
                    fr.pc = pc;
                    const v = self.pop();
                    const base = self.pop();
                    try self.setProp(base, consts[i].string.bytes, v);
                    try self.push(v);
                },
                .get_index, .get_index_this => {
                    fr.pc = pc;
                    const key = self.pop();
                    const base = if (op == .get_index) self.pop() else self.peek(0);
                    try self.push(try self.getIndex(base, key));
                },
                .set_index => {
                    fr.pc = pc;
                    const v = self.pop();
                    const key = self.pop();
                    const base = self.pop();
                    try self.setIndex(base, key, v);
                    try self.push(v);
                },
                .del_prop => {
                    const i = bc.readU32(code, pc);
                    pc += 4;
                    const base = self.pop();
                    try self.push(.{ .boolean = self.deleteKey(base, consts[i].string.bytes) });
                },
                .del_index => {
                    fr.pc = pc;
                    const key = self.pop();
                    const base = self.pop();
                    const k = try self.toString(key);
                    try self.push(.{ .boolean = self.deleteKey(base, k.string.bytes) });
                },
                .get_super, .get_super_index => {
                    fr.pc = pc;
                    var name: []const u8 = undefined;
                    if (op == .get_super) {
                        const i = bc.readU32(code, pc);
                        pc += 4;
                        name = consts[i].string.bytes;
                    } else {
                        const k = try self.toString(self.pop());
                        name = k.string.bytes;
                    }
                    const this = self.peek(0);
                    const home = fr.home orelse self.object_proto;
                    const start = home.proto orelse self.object_proto;
                    try self.push(try self.lookup(start, name, this));
                },

                .add => {
                    fr.pc = pc;
                    const b = self.pop();
                    const a = self.pop();
                    try self.push(try self.addValues(a, b));
                },
                .sub, .mul, .div, .mod, .pow => {
                    fr.pc = pc;
                    const b = try self.toNumber(self.pop());
                    const a = try self.toNumber(self.pop());
                    try self.push(.{ .number = switch (op) {
                        .sub => a - b,
                        .mul => a * b,
                        .div => a / b,
                        .mod => jsMod(a, b),
                        else => std.math.pow(f64, a, b),
                    } });
                },
                .neg => {
                    fr.pc = pc;
                    try self.push(.{ .number = -(try self.toNumber(self.pop())) });
                },
                .unary_plus => {
                    fr.pc = pc;
                    try self.push(.{ .number = try self.toNumber(self.pop()) });
                },
                .not => try self.push(.{ .boolean = !self.truthy(self.pop()) }),
                .bit_not => {
                    fr.pc = pc;
                    try self.push(.{ .number = @floatFromInt(~(try self.toInt32(self.pop()))) });
                },
                .shl, .shr, .ushr => {
                    fr.pc = pc;
                    const shift: u5 = @truncate(try self.toUint32(self.pop()));
                    const a = self.pop();
                    switch (op) {
                        .shl => try self.push(.{ .number = @floatFromInt((try self.toInt32(a)) << shift) }),
                        .shr => try self.push(.{ .number = @floatFromInt((try self.toInt32(a)) >> shift) }),
                        else => try self.push(.{ .number = @floatFromInt((try self.toUint32(a)) >> shift) }),
                    }
                },
                .bit_and, .bit_or, .bit_xor => {
                    fr.pc = pc;
                    const b = try self.toInt32(self.pop());
                    const a = try self.toInt32(self.pop());
                    try self.push(.{ .number = @floatFromInt(switch (op) {
                        .bit_and => a & b,
                        .bit_or => a | b,
                        else => a ^ b,
                    }) });
                },
                .eq, .neq => {
                    fr.pc = pc;
                    const b = self.pop();
                    const a = self.pop();
                    const r = try self.looseEquals(a, b);
                    try self.push(.{ .boolean = if (op == .eq) r else !r });
                },
                .strict_eq, .strict_neq => {
                    const b = self.pop();
                    const a = self.pop();
                    const r = self.strictEquals(a, b);
                    try self.push(.{ .boolean = if (op == .strict_eq) r else !r });
                },
                .lt, .gt, .le, .ge => {
                    fr.pc = pc;
                    const b = self.pop();
                    const a = self.pop();
                    try self.push(try self.compare(op, a, b));
                },
                .instance_of => {
                    fr.pc = pc;
                    const ctor = self.pop();
                    const obj = self.pop();
                    try self.push(.{ .boolean = try self.instanceOf(obj, ctor) });
                },
                .in_op => {
                    fr.pc = pc;
                    const obj = self.pop();
                    const key = self.pop();
                    const k = try self.toString(key);
                    try self.push(.{ .boolean = try self.hasProperty(obj, k.string.bytes) });
                },
                .typeof_op => try self.push(try self.str(self.typeOf(self.pop()))),
                .void_op => {
                    _ = self.pop();
                    try self.push(.undefined);
                },
                .to_number => {
                    fr.pc = pc;
                    try self.push(.{ .number = try self.toNumber(self.pop()) });
                },
                .inc, .dec => {
                    fr.pc = pc;
                    const n = try self.toNumber(self.pop());
                    try self.push(.{ .number = if (op == .inc) n + 1 else n - 1 });
                },

                .jump => {
                    const off = bc.readI32(code, pc);
                    pc = @intCast(@as(i64, @intCast(pc + 4)) + off);
                },
                .jump_if_false, .jump_if_true => {
                    const off = bc.readI32(code, pc);
                    pc += 4;
                    const t = self.truthy(self.pop());
                    if (t == (op == .jump_if_true)) pc = @intCast(@as(i64, @intCast(pc)) + off);
                },
                .jump_if_false_keep, .jump_if_true_keep => {
                    const off = bc.readI32(code, pc);
                    pc += 4;
                    const t = self.truthy(self.peek(0));
                    if (t == (op == .jump_if_true_keep)) pc = @intCast(@as(i64, @intCast(pc)) + off);
                },
                .jump_if_not_nullish_keep => {
                    const off = bc.readI32(code, pc);
                    pc += 4;
                    if (!self.peek(0).isNullish()) pc = @intCast(@as(i64, @intCast(pc)) + off);
                },
                .jump_if_nullish => {
                    const off = bc.readI32(code, pc);
                    pc += 4;
                    if (self.pop().isNullish()) pc = @intCast(@as(i64, @intCast(pc)) + off);
                },

                .call, .construct, .call_spread, .construct_spread => {
                    var argc: u32 = 0;
                    if (op == .call or op == .construct) {
                        argc = bc.readU32(code, pc);
                        pc += 4;
                    }
                    fr.pc = pc;
                    const is_new = op == .construct or op == .construct_spread;
                    if (op == .call_spread or op == .construct_spread) {
                        const arr = self.pop();
                        const items = if (arr == .object and arr.object.class == .array)
                            arr.object.data.elements.items
                        else
                            &[_]Value{};
                        argc = @intCast(items.len);
                        for (items) |it| try self.push(it);
                    }
                    const base = self.sp - argc - (if (is_new) @as(usize, 1) else 2);
                    if (is_new) {
                        // Constructors have no `this` slot on the stack yet.
                        var i = self.sp;
                        try self.push(.undefined);
                        while (i > base + 1) : (i -= 1) self.stack[i] = self.stack[i - 1];
                        self.stack[base + 1] = .undefined;
                    }
                    const callee = self.stack[base];
                    if (callee != .object or !callee.object.callable()) {
                        return self.throwType("{s} is not a function", .{try self.describe(callee)});
                    }
                    if (is_new) {
                        try self.constructAt(callee.object, argc, base);
                    } else {
                        try self.invoke(callee.object, argc, base, false, null);
                    }
                    if (self.fp > 0 and self.frames[self.fp - 1].base == base) return; // entered a new frame
                },
                .super_call, .super_call_spread => {
                    var argc: u32 = 0;
                    if (op == .super_call) {
                        argc = bc.readU32(code, pc);
                        pc += 4;
                    }
                    fr.pc = pc;
                    var args_buf = std.ArrayListUnmanaged(Value){};
                    defer args_buf.deinit(self.gpa);
                    if (op == .super_call_spread) {
                        const arr = self.pop();
                        if (arr == .object and arr.object.class == .array) {
                            try args_buf.appendSlice(self.gpa, arr.object.data.elements.items);
                        }
                    } else {
                        try args_buf.appendSlice(self.gpa, self.stack[self.sp - argc ..][0..argc]);
                        self.sp -= argc;
                    }
                    const parent = fr.func.proto orelse self.object_proto;
                    if (parent.class == .function and parent.callable()) {
                        _ = try self.callValue(.{ .object = parent }, fr.this, args_buf.items);
                    }
                },
                .ret, .ret_undef => {
                    var v = if (op == .ret) self.pop() else Value.undefined;
                    // A constructor that returns a primitive still yields the
                    // instance -- `new Foo()` is never `undefined`.
                    if (fr.new_target != null and v != .object) v = fr.this;
                    const base = fr.base;
                    const promise = fr.result_promise;
                    self.fp -= 1;
                    self.sp = base;
                    if (promise) |p| {
                        try self.resolvePromise(p, v);
                        try self.push(.{ .object = p });
                    } else {
                        try self.push(v);
                    }
                    return;
                },

                .new_object => try self.push(.{ .object = try self.newObject() }),
                .new_array => {
                    const n = bc.readU32(code, pc);
                    pc += 4;
                    const items = self.stack[self.sp - n ..][0..n];
                    const a = try self.newArray(items);
                    self.sp -= n;
                    try self.push(.{ .object = a });
                },
                .array_push => {
                    const v = self.pop();
                    const a = self.peek(0);
                    try a.object.data.elements.append(self.heap.alloc, v);
                },
                .array_push_spread => {
                    fr.pc = pc;
                    const src = self.pop();
                    const a = self.peek(0);
                    try self.spreadInto(a.object, src);
                },
                .define_prop => {
                    const i = bc.readU32(code, pc);
                    pc += 4;
                    const v = self.pop();
                    const o = self.peek(0);
                    try o.object.props.put(self.heap.alloc, consts[i].string.bytes, v);
                },
                .define_prop_computed => {
                    fr.pc = pc;
                    const v = self.pop();
                    const k = try self.toString(self.pop());
                    const o = self.peek(0);
                    try self.setProp(o, k.string.bytes, v);
                },
                .define_spread => {
                    fr.pc = pc;
                    const src = self.pop();
                    const o = self.peek(0);
                    try self.copyOwnInto(o.object, src);
                },
                .define_accessor, .define_accessor_computed => {
                    fr.pc = pc;
                    var name: []const u8 = undefined;
                    var kind: u8 = 0;
                    const f = self.pop();
                    if (op == .define_accessor) {
                        const i = bc.readU32(code, pc);
                        pc += 4;
                        kind = code[pc];
                        pc += 1;
                        name = consts[i].string.bytes;
                    } else {
                        kind = code[pc];
                        pc += 1;
                        const k = try self.toString(self.pop());
                        name = k.string.bytes;
                    }
                    const o = self.peek(0).object;
                    try self.defineAccessor(o, name, f, kind == 0);
                },

                .closure => {
                    const i = bc.readU32(code, pc);
                    pc += 4;
                    try self.push(.{ .object = try self.makeClosure(fr, fr.proto.protos[i]) });
                },
                .class_def => {
                    const i = bc.readU32(code, pc);
                    pc += 4;
                    fr.pc = pc;
                    const parent = self.pop();
                    try self.push(.{ .object = try self.makeClass(fr, fr.proto.protos[i], parent) });
                },
                .class_method, .class_method_computed, .class_accessor, .class_field => {
                    fr.pc = pc;
                    const f = self.pop();
                    var name: []const u8 = undefined;
                    if (op == .class_method_computed) {
                        const k = try self.toString(self.pop());
                        name = k.string.bytes;
                    } else {
                        const i = bc.readU32(code, pc);
                        pc += 4;
                        name = consts[i].string.bytes;
                    }
                    const is_static = code[pc] == 1;
                    pc += 1;
                    var kind: u8 = 0;
                    if (op == .class_accessor) {
                        kind = code[pc];
                        pc += 1;
                    }
                    const cls = self.peek(0).object;
                    const target = if (is_static) cls else blk: {
                        const pv = try self.getProp(.{ .object = cls }, "prototype");
                        break :blk pv.object;
                    };
                    if (op == .class_field) {
                        if (is_static) {
                            try cls.props.put(self.heap.alloc, name, f);
                        } else {
                            if (cls.data.func.fields == null) {
                                cls.data.func.fields = try self.heap.newObj(.plain, null);
                            }
                            try cls.data.func.fields.?.props.put(self.heap.alloc, name, f);
                        }
                    } else if (op == .class_accessor) {
                        try self.defineAccessor(target, name, f, kind == 0);
                    } else {
                        if (f == .object and f.object.class == .function) {
                            f.object.data.func.home = target;
                        }
                        try target.props.putProp(self.heap.alloc, name, .{
                            .key = undefined,
                            .value = f,
                            .enumerable = false,
                        });
                    }
                },

                .push_scope => {
                    const n = bc.readU16(code, pc);
                    pc += 2;
                    fr.env = try self.heap.newEnv(fr.env, n);
                },
                .pop_scope => fr.env = fr.env.parent.?,
                .copy_scope => {
                    const old = fr.env;
                    const fresh = try self.heap.newEnv(old.parent, old.slots.len);
                    @memcpy(fresh.slots, old.slots);
                    @memcpy(fresh.ready, old.ready);
                    fr.env = fresh;
                },

                .for_in_start, .for_of_start => {
                    fr.pc = pc;
                    const src = self.pop();
                    try self.push(.{ .object = try self.makeIterator(src, op == .for_in_start) });
                },
                .iter_next => {
                    const it = self.peek(0).object;
                    const i_prop = it.props.find("#i").?;
                    const i: usize = @intFromFloat(i_prop.value.number);
                    const items = it.data.elements.items;
                    if (i < items.len) {
                        i_prop.value = .{ .number = @floatFromInt(i + 1) };
                        try self.push(items[i]);
                        try self.push(.{ .boolean = false });
                    } else {
                        try self.push(.undefined);
                        try self.push(.{ .boolean = true });
                    }
                },

                .iter_rest => {
                    const it = self.peek(0).object;
                    const i_prop = it.props.find("#i").?;
                    const i: usize = @intFromFloat(i_prop.value.number);
                    const items = it.data.elements.items;
                    const rest = try self.newArray(if (i < items.len) items[i..] else &.{});
                    i_prop.value = .{ .number = @floatFromInt(items.len) };
                    try self.push(.{ .object = rest });
                },

                .throw_op, .rethrow => {
                    fr.pc = pc;
                    self.exception = self.pop();
                    return error.JsThrow;
                },

                .push_this => try self.push(fr.this),
                .push_callee => try self.push(.{ .object = fr.func }),
                .push_arguments => {
                    if (fr.args_obj == null) {
                        fr.args_obj = try self.newArray(self.stack[fr.args_start..][0..fr.argc]);
                    }
                    try self.push(.{ .object = fr.args_obj.? });
                },
                .save_completion => self.completion = self.pop(),
                .await_op => {
                    fr.pc = pc;
                    try self.suspendFrame();
                    return;
                },
            }
            fr.pc = pc;
            if (self.fp == 0 or self.fp - 1 < base_fp) return;
        }
        fr.pc = pc;
    }

    fn describe(self: *Vm, v: Value) Error![]const u8 {
        return switch (v) {
            .undefined => "undefined",
            .null => "null",
            else => self.typeOf(v),
        };
    }

    fn deleteKey(self: *Vm, base: Value, name: []const u8) bool {
        if (base != .object) return true;
        const o = base.object;
        if (o.class == .array) {
            if (indexOfKey(name)) |i| {
                if (i < o.data.elements.items.len) {
                    o.data.elements.items[i] = .undefined;
                    return true;
                }
            }
        }
        _ = self;
        return o.props.remove(name);
    }

    pub fn hasProperty(self: *Vm, base: Value, name: []const u8) Error!bool {
        const o = switch (base) {
            .object => |x| x,
            else => return false,
        };
        if (o.class == .array) {
            if (indexOfKey(name)) |i| return i < o.data.elements.items.len;
            if (std.mem.eql(u8, name, "length")) return true;
        }
        if (o.class == .host) {
            const v = try self.hostGet(o, name);
            return v != .undefined;
        }
        var cur: ?*Obj = o;
        while (cur) |c| {
            if (c.props.find(name) != null) return true;
            cur = c.proto;
        }
        return false;
    }

    pub fn instanceOf(self: *Vm, obj: Value, ctor: Value) Error!bool {
        if (ctor != .object or !ctor.object.callable()) {
            return self.throwType("right-hand side of 'instanceof' is not callable", .{});
        }
        if (obj != .object) return false;
        const pv = try self.getProp(ctor, "prototype");
        if (pv != .object) return false;
        var cur = obj.object.proto;
        while (cur) |c| {
            if (c == pv.object) return true;
            cur = c.proto;
        }
        return false;
    }

    fn compare(self: *Vm, op: Op, a: Value, b: Value) Error!Value {
        const pa = try self.toPrimitive(a, .number);
        const pb = try self.toPrimitive(b, .number);
        if (pa == .string and pb == .string) {
            const order = std.mem.order(u8, pa.string.bytes, pb.string.bytes);
            return .{ .boolean = switch (op) {
                .lt => order == .lt,
                .gt => order == .gt,
                .le => order != .gt,
                else => order != .lt,
            } };
        }
        const x = try self.toNumber(pa);
        const y = try self.toNumber(pb);
        if (std.math.isNan(x) or std.math.isNan(y)) return .{ .boolean = false };
        return .{ .boolean = switch (op) {
            .lt => x < y,
            .gt => x > y,
            .le => x <= y,
            else => x >= y,
        } };
    }

    pub fn addValues(self: *Vm, a: Value, b: Value) Error!Value {
        const pa = try self.toPrimitive(a, .default);
        const pb = try self.toPrimitive(b, .default);
        if (pa == .string or pb == .string) {
            const sa = try self.toString(pa);
            try self.temps.append(self.gpa, sa);
            defer self.temps.items.len -= 1;
            const sb = try self.toString(pb);
            const joined = try std.mem.concat(self.heap.alloc, u8, &.{ sa.string.bytes, sb.string.bytes });
            return self.adopt(joined);
        }
        return .{ .number = (try self.toNumber(pa)) + (try self.toNumber(pb)) };
    }

    fn defineAccessor(self: *Vm, o: *Obj, name: []const u8, f: Value, is_get: bool) Error!void {
        const existing = o.props.find(name);
        var prop = val.Prop{ .key = undefined, .enumerable = false, .is_accessor = true };
        if (existing) |e| {
            if (e.is_accessor) {
                prop.getter = e.getter;
                prop.setter = e.setter;
            }
        }
        if (is_get) prop.getter = f.object else prop.setter = f.object;
        try o.props.putProp(self.heap.alloc, name, prop);
    }

    fn makeClosure(self: *Vm, fr: *Frame, proto: *Proto) Error!*Obj {
        const o = try self.heap.newObj(.function, self.function_proto);
        o.data = .{ .func = .{
            .proto = proto,
            .env = fr.env,
            .bound_this = if (proto.kind == .arrow) fr.this else null,
            .home = fr.home,
            .n_args = proto.n_params,
        } };
        try self.define(o, "name", try self.str(proto.name));
        try self.define(o, "length", .{ .number = @floatFromInt(proto.n_params) });
        if (proto.kind != .arrow) {
            const p = try self.heap.newObj(.plain, self.object_proto);
            try self.define(p, "constructor", .{ .object = o });
            try self.define(o, "prototype", .{ .object = p });
        }
        return o;
    }

    fn makeClass(self: *Vm, fr: *Frame, proto: *Proto, parent: Value) Error!*Obj {
        const ctor = try self.makeClosure(fr, proto);
        const pv = try self.getProp(.{ .object = ctor }, "prototype");
        const cls_proto = pv.object;
        if (parent == .object and parent.object.callable()) {
            ctor.proto = parent.object;
            const parent_proto = try self.getProp(parent, "prototype");
            if (parent_proto == .object) cls_proto.proto = parent_proto.object;
        }
        ctor.data.func.home = cls_proto;
        return ctor;
    }

    fn spreadInto(self: *Vm, arr: *Obj, src: Value) Error!void {
        const it = try self.makeIterator(src, false);
        try arr.data.elements.appendSlice(self.heap.alloc, it.data.elements.items);
    }

    fn copyOwnInto(self: *Vm, dst: *Obj, src: Value) Error!void {
        switch (src) {
            .object => |o| {
                if (o.class == .array) {
                    for (o.data.elements.items, 0..) |v, i| {
                        var buf: [24]u8 = undefined;
                        const k = std.fmt.bufPrint(&buf, "{d}", .{i}) catch continue;
                        try dst.props.put(self.heap.alloc, k, v);
                    }
                    return;
                }
                for (o.props.entries.items) |p| {
                    if (p.dead or !p.enumerable) continue;
                    const v = if (p.is_accessor) try self.lookup(o, p.key, src) else p.value;
                    try dst.props.put(self.heap.alloc, p.key, v);
                }
            },
            else => {},
        }
    }

    /// Every `for...of` and every spread materialises its source up front.
    /// Lazy iterators would let a generator drive a loop; we have no
    /// generators, and eager materialisation keeps the loop body from having
    /// to be re-entrant with respect to the iterator's own frame.
    pub fn makeIterator(self: *Vm, src: Value, keys_only: bool) Error!*Obj {
        const it = try self.heap.newObj(.arguments, null);
        it.data = .{ .elements = .{} };
        try it.props.put(self.heap.alloc, "#i", .{ .number = 0 });
        const els = &it.data.elements;
        const a = self.heap.alloc;
        if (keys_only) {
            switch (src) {
                .object => |o| {
                    if (o.class == .array) {
                        for (0..o.data.elements.items.len) |i| {
                            var buf: [24]u8 = undefined;
                            const k = std.fmt.bufPrint(&buf, "{d}", .{i}) catch continue;
                            try els.append(a, try self.str(k));
                        }
                    }
                    var cur: ?*Obj = o;
                    while (cur) |c| {
                        for (c.props.entries.items) |p| {
                            if (p.dead or !p.enumerable) continue;
                            if (p.key.len > 0 and p.key[0] == '#') continue;
                            try els.append(a, try self.str(p.key));
                        }
                        cur = c.proto;
                        if (cur == self.object_proto or cur == self.array_proto) break;
                    }
                },
                else => {},
            }
            return it;
        }
        switch (src) {
            .string => |s| {
                var p: usize = 0;
                while (p < s.bytes.len) {
                    const n = std.unicode.utf8ByteSequenceLength(s.bytes[p]) catch 1;
                    try els.append(a, try self.str(s.bytes[p..@min(p + n, s.bytes.len)]));
                    p += n;
                }
            },
            .object => |o| switch (o.class) {
                .array, .arguments => try els.appendSlice(a, o.data.elements.items),
                .set => for (o.data.entries.items) |e| {
                    if (!e.dead) try els.append(a, e.key);
                },
                .map => for (o.data.entries.items) |e| {
                    if (e.dead) continue;
                    const pair = try self.newArray(&.{ e.key, e.value });
                    try els.append(a, .{ .object = pair });
                },
                else => {
                    const len = try self.getProp(src, "length");
                    if (len == .number) {
                        const n: usize = if (len.number > 0) @intFromFloat(@min(len.number, 1e7)) else 0;
                        for (0..n) |i| {
                            try els.append(a, try self.getIndex(src, .{ .number = @floatFromInt(i) }));
                        }
                    } else {
                        return self.throwType("value is not iterable", .{});
                    }
                },
            },
            else => return self.throwType("{s} is not iterable", .{self.typeOf(src)}),
        }
        return it;
    }

    // -- suspension --------------------------------------------------------

    fn suspendFrame(self: *Vm) Error!void {
        const fr = &self.frames[self.fp - 1];
        const awaited = self.pop();
        const promise = try self.toPromise(awaited);
        const coro = try self.gpa.create(Coro);
        const saved = try self.gpa.dupe(Value, self.stack[fr.bp..self.sp]);
        const args = try self.gpa.dupe(Value, self.stack[fr.args_start..][0..fr.argc]);
        coro.* = .{
            .func = fr.func,
            .proto = fr.proto,
            .pc = fr.pc,
            .env = fr.env,
            .this = fr.this,
            .home = fr.home,
            .saved = saved,
            .args = args,
            .result_promise = fr.result_promise.?,
        };
        try self.coros.append(self.gpa, coro);

        const on_ok = try self.newNative("", 1, resumeOk);
        on_ok.data.func.coro = coro;
        const on_err = try self.newNative("", 1, resumeErr);
        on_err.data.func.coro = coro;
        try self.addReaction(promise, on_ok, on_err, try self.newPromise());

        const base = fr.base;
        const rp = fr.result_promise.?;
        self.fp -= 1;
        self.sp = base;
        try self.push(.{ .object = rp });
    }

    fn resumeOk(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
        const self: *Vm = @ptrCast(@alignCast(ctx));
        _ = this;
        const coro: ?*Coro = @ptrCast(@alignCast(callee.data.func.coro));
        return self.resumeCoro(coro, if (args.len > 0) args[0] else .undefined, false);
    }

    fn resumeErr(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
        const self: *Vm = @ptrCast(@alignCast(ctx));
        _ = this;
        const coro: ?*Coro = @ptrCast(@alignCast(callee.data.func.coro));
        return self.resumeCoro(coro, if (args.len > 0) args[0] else .undefined, true);
    }

    /// Put a suspended frame back and run it to its next suspension or its end.
    pub fn resumeCoro(self: *Vm, maybe: ?*Coro, v: Value, is_error: bool) Error!Value {
        const coro = maybe orelse return .undefined;
        if (coro.done) return .undefined;
        coro.done = true; // this activation is consumed exactly once
        if (self.fp >= frame_limit) {
            return self.throwError("RangeError", "maximum call stack size exceeded", .{});
        }
        const base = self.sp;
        try self.push(.{ .object = coro.func });
        try self.push(coro.this);
        for (coro.args) |a| try self.push(a);
        const args_start = base + 2;
        const bp = args_start + coro.args.len;
        for (coro.saved) |s| try self.push(s);
        self.frames[self.fp] = .{
            .func = coro.func,
            .proto = coro.proto,
            .pc = coro.pc,
            .base = base,
            .args_start = args_start,
            .argc = @intCast(coro.args.len),
            .bp = bp,
            .env = coro.env,
            .this = coro.this,
            .home = coro.home,
            .result_promise = coro.result_promise,
        };
        self.fp += 1;
        const fp_base = self.fp - 1;
        if (is_error) {
            self.exception = v;
            self.unwind(fp_base) catch |e| return e;
        } else {
            try self.push(v);
        }
        try self.runFrames(fp_base);
        self.retireCoro(coro);
        return self.pop();
    }

    fn retireCoro(self: *Vm, coro: *Coro) void {
        for (self.coros.items, 0..) |c, i| {
            if (c == coro) {
                _ = self.coros.swapRemove(i);
                self.gpa.free(c.saved);
                self.gpa.free(c.args);
                self.gpa.destroy(c);
                return;
            }
        }
    }

    pub fn toPromise(self: *Vm, v: Value) Error!*Obj {
        if (v == .object and v.object.class == .promise) return v.object;
        const p = try self.newPromise();
        try self.resolvePromise(p, v);
        return p;
    }

    pub const PendingReaction = struct {
        state: val.PromiseState,
        value: Value,
        on_ok: ?*Obj,
        on_err: ?*Obj,
        next: *Obj,
    };

    // -- collection --------------------------------------------------------

    pub fn collect(self: *Vm) void {
        if (self.in_gc) return;
        self.in_gc = true;
        defer self.in_gc = false;
        const h = &self.heap;
        h.markObj(self.globals);
        h.markObj(self.object_proto);
        h.markObj(self.function_proto);
        h.markObj(self.array_proto);
        h.markObj(self.string_proto);
        h.markObj(self.number_proto);
        h.markObj(self.boolean_proto);
        h.markObj(self.error_proto);
        h.markObj(self.regexp_proto);
        h.markObj(self.date_proto);
        h.markObj(self.map_proto);
        h.markObj(self.set_proto);
        h.markObj(self.promise_proto);
        h.markValue(self.exception);
        h.markValue(self.completion);
        for (self.stack[0..self.sp]) |v| h.markValue(v);
        for (self.frames[0..self.fp]) |fr| {
            h.markObj(fr.func);
            h.markEnv(fr.env);
            h.markValue(fr.this);
            h.markObj(fr.args_obj);
            h.markObj(fr.result_promise);
            h.markObj(fr.home);
            h.markObj(fr.new_target);
        }
        for (self.temps.items) |v| h.markValue(v);
        for (self.handles.items) |v| h.markValue(v);
        for (self.pending_reactions.items) |r| {
            h.markValue(r.value);
            h.markObj(r.on_ok);
            h.markObj(r.on_err);
            h.markObj(r.next);
        }
        for (self.timers.items) |t| {
            h.markValue(t.fn_val);
            for (t.args) |a| h.markValue(a);
        }
        for (self.coros.items) |c| {
            h.markObj(c.func);
            h.markEnv(c.env);
            h.markValue(c.this);
            h.markObj(c.home);
            h.markObj(c.result_promise);
            for (c.saved) |v| h.markValue(v);
            for (c.args) |v| h.markValue(v);
        }
        for (self.scripts.items) |s| markProto(h, s.root);
        h.sweep(self, releaseHostThunk);
    }

    fn markProto(h: *val.Heap, p: *Proto) void {
        for (p.consts) |c| h.markValue(c);
        for (p.protos) |sub| markProto(h, sub);
    }
};

// -- numeric helpers -------------------------------------------------------

pub fn doubleToI32(d: f64) i32 {
    if (std.math.isNan(d) or std.math.isInf(d)) return 0;
    const t = @trunc(d);
    if (@abs(t) >= 9.2233720368547758e18) return 0;
    const as_i64: i64 = @intFromFloat(t);
    return @truncate(@as(i64, @bitCast(@as(u64, @bitCast(as_i64)) & 0xFFFFFFFF)) << 32 >> 32);
}

pub fn jsMod(a: f64, b: f64) f64 {
    if (std.math.isNan(a) or std.math.isNan(b) or std.math.isInf(a) or b == 0) return std.math.nan(f64);
    if (std.math.isInf(b)) return a;
    return @rem(a, b);
}

pub fn indexOfKey(name: []const u8) ?usize {
    if (name.len == 0 or name.len > 10) return null;
    if (name.len > 1 and name[0] == '0') return null;
    var n: usize = 0;
    for (name) |c| {
        if (c < '0' or c > '9') return null;
        n = n * 10 + (c - '0');
    }
    return n;
}

pub fn utf16Length(bytes: []const u8) usize {
    var n: usize = 0;
    var p: usize = 0;
    while (p < bytes.len) {
        const w = std.unicode.utf8ByteSequenceLength(bytes[p]) catch 1;
        n += if (w == 4) @as(usize, 2) else 1;
        p += w;
    }
    return n;
}

pub fn parseNumber(bytes: []const u8) f64 {
    const s = std.mem.trim(u8, bytes, " \t\n\r\x0b\x0c\u{feff}");
    if (s.len == 0) return 0;
    if (std.mem.eql(u8, s, "Infinity") or std.mem.eql(u8, s, "+Infinity")) return std.math.inf(f64);
    if (std.mem.eql(u8, s, "-Infinity")) return -std.math.inf(f64);
    if (s.len > 2 and s[0] == '0') {
        const radix: ?u8 = switch (s[1]) {
            'x', 'X' => 16,
            'o', 'O' => 8,
            'b', 'B' => 2,
            else => null,
        };
        if (radix) |r| {
            const n = std.fmt.parseInt(u64, s[2..], r) catch return std.math.nan(f64);
            return @floatFromInt(n);
        }
    }
    return std.fmt.parseFloat(f64, s) catch std.math.nan(f64);
}

/// JavaScript's number formatting: integers print without a point, and
/// everything else takes the shortest representation that round-trips.
pub fn numberToString(alloc: std.mem.Allocator, n: f64) ![]u8 {
    if (std.math.isNan(n)) return alloc.dupe(u8, "NaN");
    if (std.math.isInf(n)) return alloc.dupe(u8, if (n > 0) "Infinity" else "-Infinity");
    if (n == 0) return alloc.dupe(u8, "0");
    if (n == @trunc(n) and @abs(n) < 1e21) {
        return std.fmt.allocPrint(alloc, "{d}", .{@as(i64, @intFromFloat(n))});
    }
    var buf: [64]u8 = undefined;
    const s = try std.fmt.bufPrint(&buf, "{d}", .{n});
    return alloc.dupe(u8, s);
}
