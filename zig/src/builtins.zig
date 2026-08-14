//! The standard library.
//!
//! Every function here is a `NativeFn`. The ones that call back into
//! JavaScript -- the array iteration methods, `sort`, the promise combinators,
//! `JSON.stringify` with a replacer -- re-enter the dispatch loop and can
//! therefore hit a collection safe point, so they park anything they are
//! holding in `vm.temps`. That rule is the one thing in this file that is not
//! checked by the compiler.

const std = @import("std");
const val = @import("value.zig");
const vmod = @import("vm.zig");
const regex = @import("regex.zig");

const Vm = vmod.Vm;
const Value = val.Value;
const Obj = val.Obj;
const Error = vmod.Error;

inline fn V(ctx: *anyopaque) *Vm {
    return @ptrCast(@alignCast(ctx));
}

inline fn arg(args: []const Value, i: usize) Value {
    return if (i < args.len) args[i] else .undefined;
}

/// Park a value as a collection root for the lifetime of the caller's scope.
const Root = struct {
    vm: *Vm,
    n: usize,

    fn open(vm: *Vm) Root {
        return .{ .vm = vm, .n = vm.temps.items.len };
    }
    fn add(self: *Root, v: Value) !void {
        try self.vm.temps.append(self.vm.gpa, v);
    }
    fn close(self: *Root) void {
        self.vm.temps.items.len = self.n;
    }
};

pub fn install(vm: *Vm) !void {
    const h = &vm.heap;
    h.enabled = false; // no collection until the roots exist
    defer h.enabled = true;

    vm.object_proto = try h.newObj(.plain, null);
    vm.function_proto = try h.newObj(.plain, vm.object_proto);
    vm.array_proto = try h.newObj(.plain, vm.object_proto);
    vm.string_proto = try h.newObj(.plain, vm.object_proto);
    vm.number_proto = try h.newObj(.plain, vm.object_proto);
    vm.boolean_proto = try h.newObj(.plain, vm.object_proto);
    vm.error_proto = try h.newObj(.plain, vm.object_proto);
    vm.regexp_proto = try h.newObj(.plain, vm.object_proto);
    vm.date_proto = try h.newObj(.plain, vm.object_proto);
    vm.map_proto = try h.newObj(.plain, vm.object_proto);
    vm.set_proto = try h.newObj(.plain, vm.object_proto);
    vm.promise_proto = try h.newObj(.plain, vm.object_proto);
    vm.globals = try h.newObj(.global, vm.object_proto);

    try installObject(vm);
    try installFunction(vm);
    try installArray(vm);
    try installString(vm);
    try installNumber(vm);
    try installBoolean(vm);
    try installMath(vm);
    try installJson(vm);
    try installError(vm);
    try installMapSet(vm);
    try installDate(vm);
    try installRegExp(vm);
    try installPromise(vm);
    try installGlobals(vm);
}

// ==========================================================================
// console
// ==========================================================================

fn consoleLog(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    var out = std.ArrayListUnmanaged(u8){};
    defer out.deinit(vm.gpa);
    for (args, 0..) |a, i| {
        if (i > 0) try out.append(vm.gpa, ' ');
        try inspect(vm, &out, a, 0);
    }
    const line = try out.toOwnedSlice(vm.gpa);
    try vm.logs.append(vm.gpa, line);
    return .undefined;
}

/// How a value looks in the page's log. Strings print bare at the top level,
/// quoted inside a structure, which is what a browser console does and what
/// makes `console.log("hello", 2)` read as `hello 2`.
fn inspect(vm: *Vm, out: *std.ArrayListUnmanaged(u8), v: Value, depth: u32) anyerror!void {
    const a = vm.gpa;
    if (depth > 4) return out.appendSlice(a, "...");
    switch (v) {
        .string => |s| {
            if (depth == 0) return out.appendSlice(a, s.bytes);
            try out.append(a, '"');
            try out.appendSlice(a, s.bytes);
            try out.append(a, '"');
        },
        .object => |o| switch (o.class) {
            .array => {
                try out.append(a, '[');
                for (o.data.elements.items, 0..) |e, i| {
                    if (i > 0) try out.appendSlice(a, ", ");
                    if (i >= 100) {
                        try out.appendSlice(a, "...");
                        break;
                    }
                    try inspect(vm, out, e, depth + 1);
                }
                try out.append(a, ']');
            },
            .function => {
                const name = try vm.getProp(v, "name");
                try out.appendSlice(a, "function ");
                if (name == .string) try out.appendSlice(a, name.string.bytes);
                try out.appendSlice(a, "()");
            },
            .err => {
                const s = try errorToString(vm, o);
                try out.appendSlice(a, s);
                vm.gpa.free(s);
            },
            .plain, .global => {
                try out.appendSlice(a, "{ ");
                var first = true;
                for (o.props.entries.items) |p| {
                    if (p.dead or !p.enumerable) continue;
                    if (!first) try out.appendSlice(a, ", ");
                    first = false;
                    try out.appendSlice(a, p.key);
                    try out.appendSlice(a, ": ");
                    try inspect(vm, out, p.value, depth + 1);
                }
                try out.appendSlice(a, if (first) "{}" else " }");
                if (first) out.items.len -= 4; // undo the "{ " we opened with
            },
            else => {
                const s = try vm.toString(v);
                try out.appendSlice(a, s.string.bytes);
            },
        },
        else => {
            const s = try vm.toString(v);
            try out.appendSlice(a, s.string.bytes);
        },
    }
}

// ==========================================================================
// Object
// ==========================================================================

fn objectCtor(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const a = arg(args, 0);
    if (a == .object) return a;
    return .{ .object = try vm.newObject() };
}

fn ownKeys(vm: *Vm, v: Value, want: enum { keys, values, entries }) anyerror!Value {
    var items = std.ArrayListUnmanaged(Value){};
    defer items.deinit(vm.gpa);
    var root = Root.open(vm);
    defer root.close();
    switch (v) {
        .object => |o| {
            if (o.class == .array) {
                for (o.data.elements.items, 0..) |e, i| {
                    var buf: [24]u8 = undefined;
                    const k = try std.fmt.bufPrint(&buf, "{d}", .{i});
                    const kv = try vm.str(k);
                    try root.add(kv);
                    try items.append(vm.gpa, switch (want) {
                        .keys => kv,
                        .values => e,
                        .entries => .{ .object = try vm.newArray(&.{ kv, e }) },
                    });
                    try root.add(items.items[items.items.len - 1]);
                }
            }
            for (o.props.entries.items) |p| {
                if (p.dead or !p.enumerable) continue;
                if (p.key.len > 0 and p.key[0] == '#') continue;
                const kv = try vm.str(p.key);
                try root.add(kv);
                const pv = if (p.is_accessor) try vm.getProp(v, p.key) else p.value;
                try items.append(vm.gpa, switch (want) {
                    .keys => kv,
                    .values => pv,
                    .entries => .{ .object = try vm.newArray(&.{ kv, pv }) },
                });
                try root.add(items.items[items.items.len - 1]);
            }
        },
        .string => |s| {
            var i: usize = 0;
            const n = vmod.utf16Length(s.bytes);
            while (i < n) : (i += 1) {
                var buf: [24]u8 = undefined;
                const k = try std.fmt.bufPrint(&buf, "{d}", .{i});
                const kv = try vm.str(k);
                try root.add(kv);
                const ch = try vm.getIndex(v, .{ .number = @floatFromInt(i) });
                try items.append(vm.gpa, switch (want) {
                    .keys => kv,
                    .values => ch,
                    .entries => .{ .object = try vm.newArray(&.{ kv, ch }) },
                });
                try root.add(items.items[items.items.len - 1]);
            }
        },
        else => {},
    }
    return .{ .object = try vm.newArray(items.items) };
}

fn objectKeys(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return ownKeys(V(ctx), arg(args, 0), .keys);
}

fn objectValues(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return ownKeys(V(ctx), arg(args, 0), .values);
}

fn objectEntries(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return ownKeys(V(ctx), arg(args, 0), .entries);
}

fn objectAssign(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const target = arg(args, 0);
    if (target != .object) return target;
    for (args[@min(1, args.len)..]) |src| {
        if (src != .object) continue;
        const keys = try ownKeys(vm, src, .keys);
        var root = Root.open(vm);
        defer root.close();
        try root.add(keys);
        for (keys.object.data.elements.items) |k| {
            const v = try vm.getProp(src, k.string.bytes);
            try vm.setProp(target, k.string.bytes, v);
        }
    }
    return target;
}

fn objectFreeze(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = this;
    const t = arg(args, 0);
    if (t == .object) t.object.extensible = false;
    return t;
}

fn objectIsFrozen(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = this;
    const t = arg(args, 0);
    return .{ .boolean = t != .object or !t.object.extensible };
}

fn objectCreate(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const p = arg(args, 0);
    const o = try vm.heap.newObj(.plain, if (p == .object) p.object else null);
    if (arg(args, 1) == .object) {
        const descs = args[1].object;
        for (descs.props.entries.items) |d| {
            if (d.dead) continue;
            try applyDescriptor(vm, o, d.key, d.value);
        }
    }
    return .{ .object = o };
}

fn applyDescriptor(vm: *Vm, o: *Obj, key: []const u8, desc: Value) anyerror!void {
    if (desc != .object) return;
    const get = try vm.getProp(desc, "get");
    const set = try vm.getProp(desc, "set");
    if (get.isCallable() or set.isCallable()) {
        try o.props.putProp(vm.heap.alloc, key, .{
            .key = undefined,
            .is_accessor = true,
            .getter = if (get == .object) get.object else null,
            .setter = if (set == .object) set.object else null,
            .enumerable = vm.truthy(try vm.getProp(desc, "enumerable")),
        });
        return;
    }
    const v = try vm.getProp(desc, "value");
    const enumerable = if (try vm.hasProperty(desc, "enumerable"))
        vm.truthy(try vm.getProp(desc, "enumerable"))
    else
        false;
    try o.props.putProp(vm.heap.alloc, key, .{ .key = undefined, .value = v, .enumerable = enumerable });
}

fn objectDefineProperty(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const t = arg(args, 0);
    if (t != .object) return t;
    const k = try vm.toString(arg(args, 1));
    var root = Root.open(vm);
    defer root.close();
    try root.add(k);
    try applyDescriptor(vm, t.object, k.string.bytes, arg(args, 2));
    return t;
}

fn objectGetPrototypeOf(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = this;
    const t = arg(args, 0);
    if (t != .object) return .null;
    if (t.object.proto) |p| return .{ .object = p };
    return .null;
}

fn objectSetPrototypeOf(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = this;
    const t = arg(args, 0);
    const p = arg(args, 1);
    if (t == .object) t.object.proto = if (p == .object) p.object else null;
    return t;
}

fn objectFromEntries(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const o = try vm.newObject();
    var root = Root.open(vm);
    defer root.close();
    try root.add(.{ .object = o });
    const it = try vm.makeIterator(arg(args, 0), false);
    try root.add(.{ .object = it });
    for (it.data.elements.items) |pair| {
        const k = try vm.toString(try vm.getIndex(pair, .{ .number = 0 }));
        try root.add(k);
        const v = try vm.getIndex(pair, .{ .number = 1 });
        try o.props.put(vm.heap.alloc, k.string.bytes, v);
    }
    return .{ .object = o };
}

fn hasOwnProperty(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const k = try vm.toString(arg(args, 0));
    if (this != .object) return .{ .boolean = false };
    const o = this.object;
    if (o.class == .array) {
        if (vmod.indexOfKey(k.string.bytes)) |i| {
            return .{ .boolean = i < o.data.elements.items.len };
        }
    }
    if (o.class == .host) return .{ .boolean = try vm.hasProperty(this, k.string.bytes) };
    return .{ .boolean = o.props.find(k.string.bytes) != null };
}

fn objectToString(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const vm = V(ctx);
    return switch (this) {
        .undefined => vm.str("[object Undefined]"),
        .null => vm.str("[object Null]"),
        .object => |o| switch (o.class) {
            .array => vm.str("[object Array]"),
            .function => vm.str("[object Function]"),
            .err => vm.str("[object Error]"),
            .date => vm.str("[object Date]"),
            else => vm.str("[object Object]"),
        },
        else => vm.str("[object Object]"),
    };
}

fn identityValueOf(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = args;
    if (this == .object and this.object.class == .boxed) return this.object.data.boxed;
    return this;
}

fn installObject(vm: *Vm) !void {
    const p = vm.object_proto;
    try vm.defineFn(p, "hasOwnProperty", 1, hasOwnProperty);
    try vm.defineFn(p, "toString", 0, objectToString);
    try vm.defineFn(p, "toLocaleString", 0, objectToString);
    try vm.defineFn(p, "valueOf", 0, identityValueOf);
    try vm.defineFn(p, "isPrototypeOf", 1, isPrototypeOf);
    try vm.defineFn(p, "propertyIsEnumerable", 1, hasOwnProperty);

    const ctor = try vm.newNative("Object", 1, objectCtor);
    try vm.define(ctor, "prototype", .{ .object = p });
    try vm.define(p, "constructor", .{ .object = ctor });
    try vm.defineFn(ctor, "keys", 1, objectKeys);
    try vm.defineFn(ctor, "values", 1, objectValues);
    try vm.defineFn(ctor, "entries", 1, objectEntries);
    try vm.defineFn(ctor, "assign", 2, objectAssign);
    try vm.defineFn(ctor, "freeze", 1, objectFreeze);
    try vm.defineFn(ctor, "isFrozen", 1, objectIsFrozen);
    try vm.defineFn(ctor, "create", 2, objectCreate);
    try vm.defineFn(ctor, "defineProperty", 3, objectDefineProperty);
    try vm.defineFn(ctor, "getPrototypeOf", 1, objectGetPrototypeOf);
    try vm.defineFn(ctor, "setPrototypeOf", 2, objectSetPrototypeOf);
    try vm.defineFn(ctor, "fromEntries", 1, objectFromEntries);
    try vm.defineFn(ctor, "getOwnPropertyNames", 1, objectKeys);
    try vm.globals.props.put(vm.heap.alloc, "Object", .{ .object = ctor });
}

fn isPrototypeOf(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    const t = arg(args, 0);
    if (this != .object or t != .object) return .{ .boolean = false };
    var cur = t.object.proto;
    while (cur) |c| {
        if (c == this.object) return .{ .boolean = true };
        cur = c.proto;
    }
    return .{ .boolean = false };
}

// ==========================================================================
// Function.prototype
// ==========================================================================

fn fnCall(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const recv = arg(args, 0);
    const rest = if (args.len > 1) args[1..] else &[_]Value{};
    return vm.callValue(this, recv, rest);
}

fn fnApply(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const recv = arg(args, 0);
    const list = arg(args, 1);
    if (list.isNullish()) return vm.callValue(this, recv, &.{});
    const it = try vm.makeIterator(list, false);
    var root = Root.open(vm);
    defer root.close();
    try root.add(.{ .object = it });
    return vm.callValue(this, recv, it.data.elements.items);
}

fn fnBind(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    if (this != .object or !this.object.callable()) {
        return vm.throwType("bind called on a non-function", .{});
    }
    const o = try vm.heap.newObj(.function, vm.function_proto);
    const extra = if (args.len > 1) args[1..] else &[_]Value{};
    o.data = .{ .func = .{
        .bound_target = this.object,
        .bound_this = arg(args, 0),
        .bound_args = try vm.heap.alloc.dupe(Value, extra),
    } };
    const name = try vm.getProp(this, "name");
    try vm.define(o, "name", name);
    return .{ .object = o };
}

fn fnToString(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const vm = V(ctx);
    const name = try vm.getProp(this, "name");
    const n = if (name == .string) name.string.bytes else "";
    return vm.adopt(try std.fmt.allocPrint(vm.heap.alloc, "function {s}() {{ [native code] }}", .{n}));
}

/// `new Function(body)` compiles a string, which is what we do not do -- the
/// engine has no `eval`. The constructor still has to exist: feature tests
/// reach for `Function.prototype` constantly, and a missing global turns a
/// working page into a `ReferenceError` before it gets anywhere near needing
/// dynamic code.
fn functionCtor(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    _ = args;
    return V(ctx).throwError("EvalError", "the Function constructor is not supported", .{});
}

fn installFunction(vm: *Vm) !void {
    const p = vm.function_proto;
    try vm.defineFn(p, "call", 1, fnCall);
    try vm.defineFn(p, "apply", 2, fnApply);
    try vm.defineFn(p, "bind", 1, fnBind);
    try vm.defineFn(p, "toString", 0, fnToString);

    const ctor = try vm.newNative("Function", 1, functionCtor);
    try vm.define(ctor, "prototype", .{ .object = p });
    try vm.define(p, "constructor", .{ .object = ctor });
    try vm.globals.props.put(vm.heap.alloc, "Function", .{ .object = ctor });
}

// ==========================================================================
// Array
// ==========================================================================

/// The elements of `this` -- or, for an array-like object, a snapshot of
/// them.
///
/// `Array.prototype.slice.call(arguments)` and `[].forEach.call(nodeList, f)`
/// are how a decade of library code turns something array-shaped into an
/// array, and jQuery does not get past its own bootstrap without them.
/// Refusing anything that is not literally an array costs far more than the
/// copy does. The snapshot is rooted in `temps`, which the VM unwinds when
/// the native returns; a method that writes therefore writes to the copy,
/// which `docs/limitations.md` records.
fn thisElements(vm: *Vm, this: Value) Error!*std.ArrayListUnmanaged(Value) {
    if (this == .object) {
        const o = this.object;
        if (o.class == .array or o.class == .arguments) return &o.data.elements;
        const len = try vm.getProp(this, "length");
        if (len != .undefined and len != .null) {
            const n_f = try vm.toNumber(len);
            const n: usize = if (n_f > 0) @intFromFloat(@min(n_f, 1e7)) else 0;
            const copy = try vm.newArray(&.{});
            try vm.temps.append(vm.gpa, .{ .object = copy });
            var i: usize = 0;
            while (i < n) : (i += 1) {
                const e = try vm.getIndex(this, .{ .number = @floatFromInt(i) });
                try copy.data.elements.append(vm.heap.alloc, e);
            }
            return &copy.data.elements;
        }
    }
    return vm.throwType("not an array", .{});
}

fn arrayCtor(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    if (args.len == 1 and args[0] == .number) {
        const n: usize = if (args[0].number > 0) @intFromFloat(@min(args[0].number, 1e7)) else 0;
        const a = try vm.newArray(&.{});
        try a.data.elements.appendNTimes(vm.heap.alloc, .undefined, n);
        return .{ .object = a };
    }
    return .{ .object = try vm.newArray(args) };
}

fn arrayIsArray(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = this;
    const a = arg(args, 0);
    return .{ .boolean = a == .object and a.object.class == .array };
}

fn arrayFrom(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const src = arg(args, 0);
    var root = Root.open(vm);
    defer root.close();
    var it: *Obj = undefined;
    if (src == .object and src.object.class != .array and
        src.object.class != .map and src.object.class != .set and src.object.class != .arguments)
    {
        // Array-like: honour `length` even when it is not iterable.
        const len = try vm.getProp(src, "length");
        if (len != .undefined) {
            const n: usize = blk: {
                const d = try vm.toNumber(len);
                break :blk if (d > 0) @intFromFloat(@min(d, 1e7)) else 0;
            };
            it = try vm.newArray(&.{});
            try root.add(.{ .object = it });
            for (0..n) |i| {
                try it.data.elements.append(vm.heap.alloc, try vm.getIndex(src, .{ .number = @floatFromInt(i) }));
            }
        } else {
            it = try vm.makeIterator(src, false);
            try root.add(.{ .object = it });
        }
    } else {
        it = try vm.makeIterator(src, false);
        try root.add(.{ .object = it });
    }
    const f = arg(args, 1);
    const out = try vm.newArray(it.data.elements.items);
    try root.add(.{ .object = out });
    if (f.isCallable()) {
        for (out.data.elements.items, 0..) |e, i| {
            out.data.elements.items[i] = try vm.callValue(f, .undefined, &.{ e, .{ .number = @floatFromInt(i) } });
        }
    }
    return .{ .object = out };
}

fn arrayOf(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return .{ .object = try V(ctx).newArray(args) };
}

fn arrayPush(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    try els.appendSlice(vm.heap.alloc, args);
    return .{ .number = @floatFromInt(els.items.len) };
}

fn arrayPop(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const els = try thisElements(V(ctx), this);
    if (els.items.len == 0) return .undefined;
    const v = els.items[els.items.len - 1];
    els.items.len -= 1;
    return v;
}

fn arrayShift(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const els = try thisElements(V(ctx), this);
    if (els.items.len == 0) return .undefined;
    return els.orderedRemove(0);
}

fn arrayUnshift(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    var i = args.len;
    while (i > 0) {
        i -= 1;
        try els.insert(vm.heap.alloc, 0, args[i]);
    }
    return .{ .number = @floatFromInt(els.items.len) };
}

fn clampIndex(raw: f64, len: usize) usize {
    if (std.math.isNan(raw)) return 0;
    const l: f64 = @floatFromInt(len);
    var i = @trunc(raw);
    if (i < 0) i += l;
    if (i < 0) return 0;
    if (i > l) return len;
    return @intFromFloat(i);
}

fn arraySlice(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    const len = els.items.len;
    const start = if (arg(args, 0) == .undefined) 0 else clampIndex(try vm.toNumber(args[0]), len);
    const end = if (arg(args, 1) == .undefined) len else clampIndex(try vm.toNumber(args[1]), len);
    if (start >= end) return .{ .object = try vm.newArray(&.{}) };
    return .{ .object = try vm.newArray(els.items[start..end]) };
}

fn arraySplice(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    const len = els.items.len;
    const start = if (args.len == 0) 0 else clampIndex(try vm.toNumber(args[0]), len);
    var count: usize = len - start;
    if (args.len > 1) {
        const d = try vm.toNumber(args[1]);
        count = if (std.math.isNan(d) or d < 0) 0 else @min(@as(usize, @intFromFloat(@min(d, 1e7))), len - start);
    }
    const removed = try vm.newArray(els.items[start .. start + count]);
    var root = Root.open(vm);
    defer root.close();
    try root.add(.{ .object = removed });
    const insert = if (args.len > 2) args[2..] else &[_]Value{};
    try els.replaceRange(vm.heap.alloc, start, count, insert);
    return .{ .object = removed };
}

fn arrayConcat(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    const out = try vm.newArray(els.items);
    var root = Root.open(vm);
    defer root.close();
    try root.add(.{ .object = out });
    for (args) |a| {
        if (a == .object and a.object.class == .array) {
            try out.data.elements.appendSlice(vm.heap.alloc, a.object.data.elements.items);
        } else {
            try out.data.elements.append(vm.heap.alloc, a);
        }
    }
    return .{ .object = out };
}

fn arrayJoin(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    var sep: []const u8 = ",";
    var root = Root.open(vm);
    defer root.close();
    if (arg(args, 0) != .undefined) {
        const s = try vm.toString(args[0]);
        try root.add(s);
        sep = s.string.bytes;
    }
    var out = std.ArrayListUnmanaged(u8){};
    defer out.deinit(vm.gpa);
    for (els.items, 0..) |e, i| {
        if (i > 0) try out.appendSlice(vm.gpa, sep);
        if (e.isNullish()) continue;
        const s = try vm.toString(e);
        try root.add(s);
        try out.appendSlice(vm.gpa, s.string.bytes);
    }
    return vm.str(out.items);
}

fn arrayToString(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    return arrayJoin(ctx, callee, this, args[0..0]);
}

fn arrayIndexOf(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    const target = arg(args, 0);
    for (els.items, 0..) |e, i| {
        if (vm.strictEquals(e, target)) return .{ .number = @floatFromInt(i) };
    }
    return .{ .number = -1 };
}

fn arrayLastIndexOf(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    const target = arg(args, 0);
    var i = els.items.len;
    while (i > 0) {
        i -= 1;
        if (vm.strictEquals(els.items[i], target)) return .{ .number = @floatFromInt(i) };
    }
    return .{ .number = -1 };
}

fn arrayIncludes(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    const target = arg(args, 0);
    for (els.items) |e| {
        if (vm.strictEquals(e, target)) return .{ .boolean = true };
        if (e == .number and target == .number and
            std.math.isNan(e.number) and std.math.isNan(target.number)) return .{ .boolean = true };
    }
    return .{ .boolean = false };
}

const IterKind = enum { map, filter, for_each, find, find_index, some, every };

fn arrayIterate(vm: *Vm, this: Value, args: []const Value, kind: IterKind) anyerror!Value {
    const els = try thisElements(vm, this);
    const f = arg(args, 0);
    if (!f.isCallable()) return vm.throwType("callback is not a function", .{});
    const recv = arg(args, 1);
    var root = Root.open(vm);
    defer root.close();
    var out: ?*Obj = null;
    if (kind == .map or kind == .filter) {
        out = try vm.newArray(&.{});
        try root.add(.{ .object = out.? });
    }
    var i: usize = 0;
    while (i < els.items.len) : (i += 1) {
        const e = els.items[i];
        const r = try vm.callValue(f, recv, &.{ e, .{ .number = @floatFromInt(i) }, this });
        switch (kind) {
            .map => try out.?.data.elements.append(vm.heap.alloc, r),
            .filter => if (vm.truthy(r)) try out.?.data.elements.append(vm.heap.alloc, e),
            .for_each => {},
            .find => if (vm.truthy(r)) return e,
            .find_index => if (vm.truthy(r)) return .{ .number = @floatFromInt(i) },
            .some => if (vm.truthy(r)) return .{ .boolean = true },
            .every => if (!vm.truthy(r)) return .{ .boolean = false },
        }
    }
    return switch (kind) {
        .map, .filter => .{ .object = out.? },
        .for_each => .undefined,
        .find => .undefined,
        .find_index => .{ .number = -1 },
        .some => .{ .boolean = false },
        .every => .{ .boolean = true },
    };
}

fn arrayMap(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    return arrayIterate(V(ctx), this, args, .map);
}
fn arrayFilter(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    return arrayIterate(V(ctx), this, args, .filter);
}
fn arrayForEach(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    return arrayIterate(V(ctx), this, args, .for_each);
}
fn arrayFind(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    return arrayIterate(V(ctx), this, args, .find);
}
fn arrayFindIndex(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    return arrayIterate(V(ctx), this, args, .find_index);
}
fn arraySome(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    return arrayIterate(V(ctx), this, args, .some);
}
fn arrayEvery(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    return arrayIterate(V(ctx), this, args, .every);
}

fn arrayReduce(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    const f = arg(args, 0);
    if (!f.isCallable()) return vm.throwType("reduce callback is not a function", .{});
    var acc: Value = .undefined;
    var i: usize = 0;
    if (args.len > 1) {
        acc = args[1];
    } else {
        if (els.items.len == 0) return vm.throwType("reduce of empty array with no initial value", .{});
        acc = els.items[0];
        i = 1;
    }
    var root = Root.open(vm);
    defer root.close();
    try root.add(acc);
    while (i < els.items.len) : (i += 1) {
        acc = try vm.callValue(f, .undefined, &.{ acc, els.items[i], .{ .number = @floatFromInt(i) }, this });
        vm.temps.items[root.n] = acc;
    }
    return acc;
}

fn arrayReduceRight(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    const f = arg(args, 0);
    if (!f.isCallable()) return vm.throwType("reduceRight callback is not a function", .{});
    var i = els.items.len;
    var acc: Value = .undefined;
    if (args.len > 1) {
        acc = args[1];
    } else {
        if (i == 0) return vm.throwType("reduce of empty array with no initial value", .{});
        i -= 1;
        acc = els.items[i];
    }
    var root = Root.open(vm);
    defer root.close();
    try root.add(acc);
    while (i > 0) {
        i -= 1;
        acc = try vm.callValue(f, .undefined, &.{ acc, els.items[i], .{ .number = @floatFromInt(i) }, this });
        vm.temps.items[root.n] = acc;
    }
    return acc;
}

fn arrayReverse(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const els = try thisElements(V(ctx), this);
    std.mem.reverse(Value, els.items);
    return this;
}

fn arrayFill(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    const len = els.items.len;
    const v = arg(args, 0);
    const start = if (arg(args, 1) == .undefined) 0 else clampIndex(try vm.toNumber(args[1]), len);
    const end = if (arg(args, 2) == .undefined) len else clampIndex(try vm.toNumber(args[2]), len);
    var i = start;
    while (i < end) : (i += 1) els.items[i] = v;
    return this;
}

fn arrayFlat(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    const depth: u32 = if (arg(args, 0) == .undefined) 1 else @intFromFloat(@max(0, @min(16, try vm.toNumber(args[0]))));
    const out = try vm.newArray(&.{});
    var root = Root.open(vm);
    defer root.close();
    try root.add(.{ .object = out });
    try flattenInto(vm, out, els.items, depth);
    return .{ .object = out };
}

fn flattenInto(vm: *Vm, out: *Obj, items: []const Value, depth: u32) anyerror!void {
    for (items) |e| {
        if (depth > 0 and e == .object and e.object.class == .array) {
            try flattenInto(vm, out, e.object.data.elements.items, depth - 1);
        } else {
            try out.data.elements.append(vm.heap.alloc, e);
        }
    }
}

/// Merge sort, because a comparator can throw and can allocate, and because
/// quadratic behaviour on a page's data table is a hang the user sees.
fn arraySort(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const els = try thisElements(vm, this);
    const cmp = arg(args, 0);
    const n = els.items.len;
    if (n < 2) return this;
    const scratch = try vm.gpa.alloc(Value, n);
    defer vm.gpa.free(scratch);
    try mergeSort(vm, els.items, scratch, cmp);
    return this;
}

fn mergeSort(vm: *Vm, items: []Value, scratch: []Value, cmp: Value) anyerror!void {
    if (items.len < 2) return;
    const mid = items.len / 2;
    try mergeSort(vm, items[0..mid], scratch[0..mid], cmp);
    try mergeSort(vm, items[mid..], scratch[mid..], cmp);
    @memcpy(scratch[0..items.len], items);
    var i: usize = 0;
    var j: usize = mid;
    var k: usize = 0;
    while (k < items.len) : (k += 1) {
        if (i >= mid) {
            items[k] = scratch[j];
            j += 1;
        } else if (j >= items.len) {
            items[k] = scratch[i];
            i += 1;
        } else if (try sortLess(vm, scratch[j], scratch[i], cmp)) {
            items[k] = scratch[j];
            j += 1;
        } else {
            items[k] = scratch[i];
            i += 1;
        }
    }
}

fn sortLess(vm: *Vm, a: Value, b: Value, cmp: Value) anyerror!bool {
    // `undefined` sorts last regardless of the comparator, per the spec.
    if (a == .undefined) return false;
    if (b == .undefined) return true;
    if (cmp.isCallable()) {
        const r = try vm.callValue(cmp, .undefined, &.{ a, b });
        const d = try vm.toNumber(r);
        return d < 0;
    }
    var root = Root.open(vm);
    defer root.close();
    const sa = try vm.toString(a);
    try root.add(sa);
    const sb = try vm.toString(b);
    return std.mem.order(u8, sa.string.bytes, sb.string.bytes) == .lt;
}

fn installArray(vm: *Vm) !void {
    const p = vm.array_proto;
    try vm.defineFn(p, "push", 1, arrayPush);
    try vm.defineFn(p, "pop", 0, arrayPop);
    try vm.defineFn(p, "shift", 0, arrayShift);
    try vm.defineFn(p, "unshift", 1, arrayUnshift);
    try vm.defineFn(p, "slice", 2, arraySlice);
    try vm.defineFn(p, "splice", 2, arraySplice);
    try vm.defineFn(p, "concat", 1, arrayConcat);
    try vm.defineFn(p, "join", 1, arrayJoin);
    try vm.defineFn(p, "toString", 0, arrayToString);
    try vm.defineFn(p, "indexOf", 1, arrayIndexOf);
    try vm.defineFn(p, "lastIndexOf", 1, arrayLastIndexOf);
    try vm.defineFn(p, "includes", 1, arrayIncludes);
    try vm.defineFn(p, "map", 1, arrayMap);
    try vm.defineFn(p, "filter", 1, arrayFilter);
    try vm.defineFn(p, "forEach", 1, arrayForEach);
    try vm.defineFn(p, "find", 1, arrayFind);
    try vm.defineFn(p, "findIndex", 1, arrayFindIndex);
    try vm.defineFn(p, "some", 1, arraySome);
    try vm.defineFn(p, "every", 1, arrayEvery);
    try vm.defineFn(p, "reduce", 1, arrayReduce);
    try vm.defineFn(p, "reduceRight", 1, arrayReduceRight);
    try vm.defineFn(p, "reverse", 0, arrayReverse);
    try vm.defineFn(p, "fill", 1, arrayFill);
    try vm.defineFn(p, "flat", 0, arrayFlat);
    try vm.defineFn(p, "sort", 1, arraySort);

    const ctor = try vm.newNative("Array", 1, arrayCtor);
    try vm.define(ctor, "prototype", .{ .object = p });
    try vm.define(p, "constructor", .{ .object = ctor });
    try vm.defineFn(ctor, "isArray", 1, arrayIsArray);
    try vm.defineFn(ctor, "from", 1, arrayFrom);
    try vm.defineFn(ctor, "of", 0, arrayOf);
    try vm.globals.props.put(vm.heap.alloc, "Array", .{ .object = ctor });
}

// ==========================================================================
// String
// ==========================================================================

/// Byte offset of code unit `i`, clamped to the end. Indices are UTF-16 code
/// units so that `.length` and `charAt` agree with a browser on ASCII, which
/// is what every page script we care about actually indexes.
fn cuToByte(bytes: []const u8, i: usize) usize {
    var seen: usize = 0;
    var p: usize = 0;
    while (p < bytes.len) {
        if (seen >= i) return p;
        const w = std.unicode.utf8ByteSequenceLength(bytes[p]) catch 1;
        seen += if (w == 4) @as(usize, 2) else 1;
        p += w;
    }
    return bytes.len;
}

fn thisString(vm: *Vm, this: Value) Error!Value {
    if (this == .object and this.object.class == .boxed) return vm.toString(this.object.data.boxed);
    return vm.toString(this);
}

fn stringCtor(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const s = if (args.len == 0) try vm.str("") else try vm.toString(args[0]);
    // `new String(x)` boxes; `String(x)` does not.
    if (this == .object and this.object.class == .plain and this.object.proto == vm.object_proto) {
        const box = try vm.heap.newObj(.boxed, vm.string_proto);
        box.data = .{ .boxed = s };
        return .{ .object = box };
    }
    return s;
}

fn strLength(vm: *Vm, s: Value) usize {
    _ = vm;
    return vmod.utf16Length(s.string.bytes);
}

fn strCharAt(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const s = try thisString(vm, this);
    const i = try vm.toNumber(arg(args, 0));
    if (i < 0) return vm.str("");
    return vm.getIndex(s, .{ .number = @trunc(i) });
}

fn strCharCodeAt(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const s = try thisString(vm, this);
    const n = try vm.toNumber(arg(args, 0));
    if (n < 0) return .{ .number = std.math.nan(f64) };
    const idx: usize = @intFromFloat(@trunc(n));
    const bytes = s.string.bytes;
    const off = cuToByte(bytes, idx);
    if (off >= bytes.len) return .{ .number = std.math.nan(f64) };
    const w = std.unicode.utf8ByteSequenceLength(bytes[off]) catch 1;
    const cp = std.unicode.utf8Decode(bytes[off..@min(off + w, bytes.len)]) catch bytes[off];
    if (cp < 0x10000) return .{ .number = @floatFromInt(cp) };
    // A supplementary character occupies two code units.
    const v = cp - 0x10000;
    const lead: u32 = 0xD800 + (v >> 10);
    const trail: u32 = 0xDC00 + (v & 0x3FF);
    const at_lead = cuToByte(bytes, idx) == off and idx == cuToByte2(bytes, off);
    return .{ .number = @floatFromInt(if (at_lead) lead else trail) };
}

fn cuToByte2(bytes: []const u8, off: usize) usize {
    return vmod.utf16Length(bytes[0..off]);
}

fn strCodePointAt(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const s = try thisString(vm, this);
    const idx: usize = @intFromFloat(@max(0, @trunc(try vm.toNumber(arg(args, 0)))));
    const bytes = s.string.bytes;
    const off = cuToByte(bytes, idx);
    if (off >= bytes.len) return .undefined;
    const w = std.unicode.utf8ByteSequenceLength(bytes[off]) catch 1;
    const cp = std.unicode.utf8Decode(bytes[off..@min(off + w, bytes.len)]) catch bytes[off];
    return .{ .number = @floatFromInt(cp) };
}

fn strFromCharCode(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    var out = std.ArrayListUnmanaged(u8){};
    defer out.deinit(vm.gpa);
    for (args) |a| {
        const n = try vm.toNumber(a);
        const cp: u21 = @intCast(@as(u32, @intFromFloat(@max(0, @min(0x10FFFF, @trunc(n))))));
        var buf: [4]u8 = undefined;
        const len = std.unicode.utf8Encode(cp, &buf) catch continue;
        try out.appendSlice(vm.gpa, buf[0..len]);
    }
    return vm.str(out.items);
}

fn strIndexOf(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    const needle = try vm.toString(arg(args, 0));
    try root.add(needle);
    var from: usize = 0;
    if (args.len > 1) from = cuToByte(s.string.bytes, @intFromFloat(@max(0, @trunc(try vm.toNumber(args[1])))));
    if (from > s.string.bytes.len) return .{ .number = -1 };
    const at = std.mem.indexOfPos(u8, s.string.bytes, from, needle.string.bytes) orelse return .{ .number = -1 };
    return .{ .number = @floatFromInt(vmod.utf16Length(s.string.bytes[0..at])) };
}

fn strLastIndexOf(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    const needle = try vm.toString(arg(args, 0));
    const at = std.mem.lastIndexOf(u8, s.string.bytes, needle.string.bytes) orelse return .{ .number = -1 };
    return .{ .number = @floatFromInt(vmod.utf16Length(s.string.bytes[0..at])) };
}

fn strIncludes(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    const needle = try vm.toString(arg(args, 0));
    return .{ .boolean = std.mem.indexOf(u8, s.string.bytes, needle.string.bytes) != null };
}

fn strStartsWith(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    const needle = try vm.toString(arg(args, 0));
    try root.add(needle);
    var from: usize = 0;
    if (args.len > 1) from = cuToByte(s.string.bytes, @intFromFloat(@max(0, @trunc(try vm.toNumber(args[1])))));
    if (from > s.string.bytes.len) return .{ .boolean = false };
    return .{ .boolean = std.mem.startsWith(u8, s.string.bytes[from..], needle.string.bytes) };
}

fn strEndsWith(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    const needle = try vm.toString(arg(args, 0));
    try root.add(needle);
    var end = s.string.bytes.len;
    if (args.len > 1 and args[1] != .undefined) {
        end = cuToByte(s.string.bytes, @intFromFloat(@max(0, @trunc(try vm.toNumber(args[1])))));
    }
    return .{ .boolean = std.mem.endsWith(u8, s.string.bytes[0..end], needle.string.bytes) };
}

fn strSlice(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    const bytes = s.string.bytes;
    const len = vmod.utf16Length(bytes);
    const start = if (arg(args, 0) == .undefined) 0 else clampIndex(try vm.toNumber(args[0]), len);
    const end = if (arg(args, 1) == .undefined) len else clampIndex(try vm.toNumber(args[1]), len);
    if (start >= end) return vm.str("");
    return vm.str(bytes[cuToByte(bytes, start)..cuToByte(bytes, end)]);
}

fn strSubstring(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    const bytes = s.string.bytes;
    const len = vmod.utf16Length(bytes);
    var a = if (arg(args, 0) == .undefined) 0 else clampNonNeg(try vm.toNumber(args[0]), len);
    var b = if (arg(args, 1) == .undefined) len else clampNonNeg(try vm.toNumber(args[1]), len);
    if (a > b) std.mem.swap(usize, &a, &b);
    return vm.str(bytes[cuToByte(bytes, a)..cuToByte(bytes, b)]);
}

fn clampNonNeg(raw: f64, len: usize) usize {
    if (std.math.isNan(raw) or raw < 0) return 0;
    const t = @trunc(raw);
    if (t > @as(f64, @floatFromInt(len))) return len;
    return @intFromFloat(t);
}

fn strSubstr(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    const bytes = s.string.bytes;
    const len = vmod.utf16Length(bytes);
    const start = if (arg(args, 0) == .undefined) 0 else clampIndex(try vm.toNumber(args[0]), len);
    var count = len - start;
    if (arg(args, 1) != .undefined) {
        const d = try vm.toNumber(args[1]);
        count = if (d < 0 or std.math.isNan(d)) 0 else @min(@as(usize, @intFromFloat(@min(d, 1e7))), len - start);
    }
    return vm.str(bytes[cuToByte(bytes, start)..cuToByte(bytes, start + count)]);
}

fn strAt(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const s = try thisString(vm, this);
    const len = vmod.utf16Length(s.string.bytes);
    var i = @trunc(try vm.toNumber(arg(args, 0)));
    if (i < 0) i += @floatFromInt(len);
    if (i < 0 or i >= @as(f64, @floatFromInt(len))) return .undefined;
    return vm.getIndex(s, .{ .number = i });
}

fn strToUpper(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const vm = V(ctx);
    const s = try thisString(vm, this);
    const out = try vm.heap.alloc.dupe(u8, s.string.bytes);
    for (out) |*c| c.* = std.ascii.toUpper(c.*);
    return vm.adopt(out);
}

fn strToLower(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const vm = V(ctx);
    const s = try thisString(vm, this);
    const out = try vm.heap.alloc.dupe(u8, s.string.bytes);
    for (out) |*c| c.* = std.ascii.toLower(c.*);
    return vm.adopt(out);
}

const ws = " \t\n\r\x0b\x0c";

fn strTrim(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const vm = V(ctx);
    const s = try thisString(vm, this);
    return vm.str(std.mem.trim(u8, s.string.bytes, ws));
}

fn strTrimStart(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const vm = V(ctx);
    const s = try thisString(vm, this);
    return vm.str(std.mem.trimLeft(u8, s.string.bytes, ws));
}

fn strTrimEnd(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const vm = V(ctx);
    const s = try thisString(vm, this);
    return vm.str(std.mem.trimRight(u8, s.string.bytes, ws));
}

fn strRepeat(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    const n = try vm.toNumber(arg(args, 0));
    if (n < 0 or std.math.isNan(n)) return vm.throwError("RangeError", "invalid count value", .{});
    const count: usize = @intFromFloat(@min(@trunc(n), 1e6));
    var out = std.ArrayListUnmanaged(u8){};
    defer out.deinit(vm.gpa);
    for (0..count) |_| try out.appendSlice(vm.gpa, s.string.bytes);
    return vm.str(out.items);
}

fn strPad(vm: *Vm, this: Value, args: []const Value, at_start: bool) anyerror!Value {
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    const target: usize = @intFromFloat(@max(0, @min(1e6, @trunc(try vm.toNumber(arg(args, 0))))));
    var pad: []const u8 = " ";
    if (arg(args, 1) != .undefined) {
        const p = try vm.toString(args[1]);
        try root.add(p);
        pad = p.string.bytes;
    }
    const have = vmod.utf16Length(s.string.bytes);
    if (have >= target or pad.len == 0) return s;
    var out = std.ArrayListUnmanaged(u8){};
    defer out.deinit(vm.gpa);
    var filled: usize = 0;
    while (filled < target - have) : (filled += 1) {
        try out.append(vm.gpa, pad[filled % pad.len]);
    }
    if (at_start) {
        try out.appendSlice(vm.gpa, s.string.bytes);
    } else {
        var head = std.ArrayListUnmanaged(u8){};
        defer head.deinit(vm.gpa);
        try head.appendSlice(vm.gpa, s.string.bytes);
        try head.appendSlice(vm.gpa, out.items);
        return vm.str(head.items);
    }
    return vm.str(out.items);
}

fn strPadStart(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    return strPad(V(ctx), this, args, true);
}

fn strPadEnd(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    return strPad(V(ctx), this, args, false);
}

fn strConcat(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    var out = std.ArrayListUnmanaged(u8){};
    defer out.deinit(vm.gpa);
    try out.appendSlice(vm.gpa, s.string.bytes);
    for (args) |a| {
        const x = try vm.toString(a);
        try root.add(x);
        try out.appendSlice(vm.gpa, x.string.bytes);
    }
    return vm.str(out.items);
}

fn strSplit(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    const out = try vm.newArray(&.{});
    try root.add(.{ .object = out });
    const sep = arg(args, 0);
    if (sep == .undefined) {
        try out.data.elements.append(vm.heap.alloc, s);
        return .{ .object = out };
    }
    if (sep == .object and sep.object.class == .regexp) {
        const rd = sep.object.data.regex;
        const re: *regex.Regex = @ptrCast(@alignCast(rd.prog));
        var caps_buf: [32]?regex.Span = undefined;
        const caps = caps_buf[0..@min(caps_buf.len, re.group_count + 1)];
        var pos: u32 = 0;
        var last: u32 = 0;
        while (pos <= s.string.bytes.len) {
            if (!re.exec(s.string.bytes, pos, caps)) break;
            const m = caps[0].?;
            if (m.end == m.start) {
                pos = m.start + 1;
                if (pos > s.string.bytes.len) break;
                continue;
            }
            try out.data.elements.append(vm.heap.alloc, try vm.str(s.string.bytes[last..m.start]));
            last = m.end;
            pos = m.end;
        }
        try out.data.elements.append(vm.heap.alloc, try vm.str(s.string.bytes[last..]));
        return .{ .object = out };
    }
    const sp = try vm.toString(sep);
    try root.add(sp);
    if (sp.string.bytes.len == 0) {
        var p: usize = 0;
        while (p < s.string.bytes.len) {
            const w = std.unicode.utf8ByteSequenceLength(s.string.bytes[p]) catch 1;
            try out.data.elements.append(vm.heap.alloc, try vm.str(s.string.bytes[p..@min(p + w, s.string.bytes.len)]));
            p += w;
        }
        return .{ .object = out };
    }
    var it = std.mem.splitSequence(u8, s.string.bytes, sp.string.bytes);
    while (it.next()) |part| {
        try out.data.elements.append(vm.heap.alloc, try vm.str(part));
    }
    return .{ .object = out };
}

fn installString(vm: *Vm) !void {
    const p = vm.string_proto;
    try vm.defineFn(p, "charAt", 1, strCharAt);
    try vm.defineFn(p, "charCodeAt", 1, strCharCodeAt);
    try vm.defineFn(p, "codePointAt", 1, strCodePointAt);
    try vm.defineFn(p, "indexOf", 1, strIndexOf);
    try vm.defineFn(p, "lastIndexOf", 1, strLastIndexOf);
    try vm.defineFn(p, "includes", 1, strIncludes);
    try vm.defineFn(p, "startsWith", 1, strStartsWith);
    try vm.defineFn(p, "endsWith", 1, strEndsWith);
    try vm.defineFn(p, "slice", 2, strSlice);
    try vm.defineFn(p, "substring", 2, strSubstring);
    try vm.defineFn(p, "substr", 2, strSubstr);
    try vm.defineFn(p, "at", 1, strAt);
    try vm.defineFn(p, "toUpperCase", 0, strToUpper);
    try vm.defineFn(p, "toLowerCase", 0, strToLower);
    try vm.defineFn(p, "toLocaleUpperCase", 0, strToUpper);
    try vm.defineFn(p, "toLocaleLowerCase", 0, strToLower);
    try vm.defineFn(p, "trim", 0, strTrim);
    try vm.defineFn(p, "trimStart", 0, strTrimStart);
    try vm.defineFn(p, "trimEnd", 0, strTrimEnd);
    try vm.defineFn(p, "repeat", 1, strRepeat);
    try vm.defineFn(p, "padStart", 2, strPadStart);
    try vm.defineFn(p, "padEnd", 2, strPadEnd);
    try vm.defineFn(p, "concat", 1, strConcat);
    try vm.defineFn(p, "split", 2, strSplit);
    try vm.defineFn(p, "toString", 0, identityValueOf);
    try vm.defineFn(p, "valueOf", 0, identityValueOf);
    try vm.defineFn(p, "replace", 2, strReplace);
    try vm.defineFn(p, "replaceAll", 2, strReplaceAll);
    try vm.defineFn(p, "match", 1, strMatch);
    try vm.defineFn(p, "search", 1, strSearch);

    const ctor = try vm.newNative("String", 1, stringCtor);
    try vm.define(ctor, "prototype", .{ .object = p });
    try vm.define(p, "constructor", .{ .object = ctor });
    try vm.defineFn(ctor, "fromCharCode", 1, strFromCharCode);
    try vm.globals.props.put(vm.heap.alloc, "String", .{ .object = ctor });
}

// ==========================================================================
// Number, Boolean
// ==========================================================================

fn numberCtor(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    if (args.len == 0) return .{ .number = 0 };
    return .{ .number = try vm.toNumber(args[0]) };
}

fn thisNumber(vm: *Vm, this: Value) Error!f64 {
    if (this == .object and this.object.class == .boxed) return vm.toNumber(this.object.data.boxed);
    return vm.toNumber(this);
}

fn numToFixed(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const n = try thisNumber(vm, this);
    const d: usize = @intFromFloat(@max(0, @min(100, @trunc(try vm.toNumber(arg(args, 0))))));
    if (std.math.isNan(n)) return vm.str("NaN");
    if (std.math.isInf(n)) return vm.str(if (n > 0) "Infinity" else "-Infinity");
    const s = try std.fmt.allocPrint(vm.heap.alloc, "{d:.[1]}", .{ n, d });
    return vm.adopt(s);
}

fn numToString(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const n = try thisNumber(vm, this);
    if (arg(args, 0) != .undefined) {
        const radix: u8 = @intFromFloat(@max(2, @min(36, try vm.toNumber(args[0]))));
        if (radix != 10) {
            if (std.math.isNan(n) or std.math.isInf(n)) return vm.str(if (std.math.isNan(n)) "NaN" else "Infinity");
            var buf: [80]u8 = undefined;
            const neg = n < 0;
            var iv: u64 = @intFromFloat(@abs(@trunc(n)));
            var i: usize = buf.len;
            if (iv == 0) {
                i -= 1;
                buf[i] = '0';
            }
            while (iv > 0) {
                i -= 1;
                buf[i] = "0123456789abcdefghijklmnopqrstuvwxyz"[iv % radix];
                iv /= radix;
            }
            if (neg) {
                i -= 1;
                buf[i] = '-';
            }
            return vm.str(buf[i..]);
        }
    }
    return vm.adopt(try vmod.numberToString(vm.heap.alloc, n));
}

fn numValueOf(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    return .{ .number = try thisNumber(V(ctx), this) };
}

fn numIsInteger(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = this;
    const a = arg(args, 0);
    if (a != .number) return .{ .boolean = false };
    return .{ .boolean = std.math.isFinite(a.number) and a.number == @trunc(a.number) };
}

fn numIsFinite(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = this;
    const a = arg(args, 0);
    return .{ .boolean = a == .number and std.math.isFinite(a.number) };
}

fn numIsNaN(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = this;
    const a = arg(args, 0);
    return .{ .boolean = a == .number and std.math.isNan(a.number) };
}

fn globalParseInt(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try vm.toString(arg(args, 0));
    try root.add(s);
    var t = std.mem.trimLeft(u8, s.string.bytes, ws);
    var radix: u8 = 0;
    if (arg(args, 1) != .undefined) {
        const r = try vm.toNumber(args[1]);
        if (!std.math.isNan(r) and r != 0) radix = @intFromFloat(@max(2, @min(36, @trunc(r))));
    }
    var neg = false;
    if (t.len > 0 and (t[0] == '+' or t[0] == '-')) {
        neg = t[0] == '-';
        t = t[1..];
    }
    if ((radix == 0 or radix == 16) and t.len > 2 and t[0] == '0' and (t[1] == 'x' or t[1] == 'X')) {
        t = t[2..];
        radix = 16;
    }
    if (radix == 0) radix = 10;
    var end: usize = 0;
    while (end < t.len and digitValue(t[end]) != null and digitValue(t[end]).? < radix) end += 1;
    if (end == 0) return .{ .number = std.math.nan(f64) };
    var acc: f64 = 0;
    for (t[0..end]) |c| acc = acc * @as(f64, @floatFromInt(radix)) + @as(f64, @floatFromInt(digitValue(c).?));
    return .{ .number = if (neg) -acc else acc };
}

fn digitValue(c: u8) ?u8 {
    return switch (c) {
        '0'...'9' => c - '0',
        'a'...'z' => c - 'a' + 10,
        'A'...'Z' => c - 'A' + 10,
        else => null,
    };
}

fn globalParseFloat(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const s = try vm.toString(arg(args, 0));
    const t = std.mem.trimLeft(u8, s.string.bytes, ws);
    if (std.mem.startsWith(u8, t, "Infinity") or std.mem.startsWith(u8, t, "+Infinity")) {
        return .{ .number = std.math.inf(f64) };
    }
    if (std.mem.startsWith(u8, t, "-Infinity")) return .{ .number = -std.math.inf(f64) };
    var end: usize = 0;
    var seen_dot = false;
    var seen_e = false;
    while (end < t.len) : (end += 1) {
        const c = t[end];
        if (c >= '0' and c <= '9') continue;
        if ((c == '+' or c == '-') and (end == 0 or t[end - 1] == 'e' or t[end - 1] == 'E')) continue;
        if (c == '.' and !seen_dot and !seen_e) {
            seen_dot = true;
            continue;
        }
        if ((c == 'e' or c == 'E') and !seen_e and end > 0) {
            seen_e = true;
            continue;
        }
        break;
    }
    while (end > 0) {
        const d = std.fmt.parseFloat(f64, t[0..end]) catch {
            end -= 1;
            continue;
        };
        return .{ .number = d };
    }
    return .{ .number = std.math.nan(f64) };
}

fn globalIsNaN(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return .{ .boolean = std.math.isNan(try V(ctx).toNumber(arg(args, 0))) };
}

fn globalIsFinite(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return .{ .boolean = std.math.isFinite(try V(ctx).toNumber(arg(args, 0))) };
}

fn installNumber(vm: *Vm) !void {
    const p = vm.number_proto;
    try vm.defineFn(p, "toFixed", 1, numToFixed);
    try vm.defineFn(p, "toString", 1, numToString);
    try vm.defineFn(p, "toLocaleString", 0, numToString);
    try vm.defineFn(p, "toPrecision", 1, numToString);
    try vm.defineFn(p, "valueOf", 0, numValueOf);

    const ctor = try vm.newNative("Number", 1, numberCtor);
    try vm.define(ctor, "prototype", .{ .object = p });
    try vm.define(p, "constructor", .{ .object = ctor });
    try vm.defineFn(ctor, "isInteger", 1, numIsInteger);
    try vm.defineFn(ctor, "isSafeInteger", 1, numIsInteger);
    try vm.defineFn(ctor, "isFinite", 1, numIsFinite);
    try vm.defineFn(ctor, "isNaN", 1, numIsNaN);
    try vm.defineFn(ctor, "parseInt", 2, globalParseInt);
    try vm.defineFn(ctor, "parseFloat", 1, globalParseFloat);
    try vm.define(ctor, "MAX_SAFE_INTEGER", .{ .number = 9007199254740991 });
    try vm.define(ctor, "MIN_SAFE_INTEGER", .{ .number = -9007199254740991 });
    try vm.define(ctor, "MAX_VALUE", .{ .number = std.math.floatMax(f64) });
    try vm.define(ctor, "MIN_VALUE", .{ .number = std.math.floatTrueMin(f64) });
    try vm.define(ctor, "EPSILON", .{ .number = std.math.floatEps(f64) });
    try vm.define(ctor, "POSITIVE_INFINITY", .{ .number = std.math.inf(f64) });
    try vm.define(ctor, "NEGATIVE_INFINITY", .{ .number = -std.math.inf(f64) });
    try vm.define(ctor, "NaN", .{ .number = std.math.nan(f64) });
    try vm.globals.props.put(vm.heap.alloc, "Number", .{ .object = ctor });
}

fn booleanCtor(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return .{ .boolean = V(ctx).truthy(arg(args, 0)) };
}

fn boolToString(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const vm = V(ctx);
    const b = if (this == .object and this.object.class == .boxed) this.object.data.boxed else this;
    return vm.str(if (vm.truthy(b)) "true" else "false");
}

fn installBoolean(vm: *Vm) !void {
    const p = vm.boolean_proto;
    try vm.defineFn(p, "toString", 0, boolToString);
    try vm.defineFn(p, "valueOf", 0, identityValueOf);
    const ctor = try vm.newNative("Boolean", 1, booleanCtor);
    try vm.define(ctor, "prototype", .{ .object = p });
    try vm.define(p, "constructor", .{ .object = ctor });
    try vm.globals.props.put(vm.heap.alloc, "Boolean", .{ .object = ctor });
}

// ==========================================================================
// Math
// ==========================================================================

fn mathUnary(comptime f: fn (f64) f64) val.NativeFn {
    return struct {
        fn call(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
            _ = callee;
            _ = this;
            return .{ .number = f(try V(ctx).toNumber(arg(args, 0))) };
        }
    }.call;
}

fn mFloor(x: f64) f64 {
    return @floor(x);
}
fn mCeil(x: f64) f64 {
    return @ceil(x);
}
fn mAbs(x: f64) f64 {
    return @abs(x);
}
fn mSqrt(x: f64) f64 {
    return @sqrt(x);
}
fn mTrunc(x: f64) f64 {
    return @trunc(x);
}
fn mSign(x: f64) f64 {
    if (std.math.isNan(x)) return x;
    if (x > 0) return 1;
    if (x < 0) return -1;
    return x;
}
fn mRound(x: f64) f64 {
    if (std.math.isNan(x) or std.math.isInf(x)) return x;
    return @floor(x + 0.5);
}
fn mLog(x: f64) f64 {
    return @log(x);
}
fn mLog2(x: f64) f64 {
    return @log2(x);
}
fn mLog10(x: f64) f64 {
    return @log10(x);
}
fn mExp(x: f64) f64 {
    return @exp(x);
}
fn mSin(x: f64) f64 {
    return @sin(x);
}
fn mCos(x: f64) f64 {
    return @cos(x);
}
fn mTan(x: f64) f64 {
    return @tan(x);
}
fn mAsin(x: f64) f64 {
    return std.math.asin(x);
}
fn mAcos(x: f64) f64 {
    return std.math.acos(x);
}
fn mAtan(x: f64) f64 {
    return std.math.atan(x);
}
fn mCbrt(x: f64) f64 {
    return std.math.cbrt(x);
}

fn mathMax(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    var best: f64 = -std.math.inf(f64);
    for (args) |a| {
        const n = try vm.toNumber(a);
        if (std.math.isNan(n)) return .{ .number = n };
        if (n > best) best = n;
    }
    return .{ .number = best };
}

fn mathMin(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    var best: f64 = std.math.inf(f64);
    for (args) |a| {
        const n = try vm.toNumber(a);
        if (std.math.isNan(n)) return .{ .number = n };
        if (n < best) best = n;
    }
    return .{ .number = best };
}

fn mathPow(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    return .{ .number = std.math.pow(f64, try vm.toNumber(arg(args, 0)), try vm.toNumber(arg(args, 1))) };
}

fn mathAtan2(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    return .{ .number = std.math.atan2(try vm.toNumber(arg(args, 0)), try vm.toNumber(arg(args, 1))) };
}

fn mathHypot(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    var sum: f64 = 0;
    for (args) |a| {
        const n = try vm.toNumber(a);
        sum += n * n;
    }
    return .{ .number = @sqrt(sum) };
}

var rng_state: u64 = 0x2545F4914F6CDD1D;

fn mathRandom(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = this;
    _ = args;
    // xorshift64*, seeded once from the clock. Nothing here needs to be
    // unpredictable to an adversary; it needs to be cheap and not repeat.
    rng_state ^= rng_state >> 12;
    rng_state ^= rng_state << 25;
    rng_state ^= rng_state >> 27;
    const bits = (rng_state *% 0x2545F4914F6CDD1D) >> 11;
    return .{ .number = @as(f64, @floatFromInt(bits)) / 9007199254740992.0 };
}

fn installMath(vm: *Vm) !void {
    rng_state ^= @bitCast(std.time.milliTimestamp());
    const m = try vm.heap.newObj(.plain, vm.object_proto);
    try vm.defineFn(m, "floor", 1, mathUnary(mFloor));
    try vm.defineFn(m, "ceil", 1, mathUnary(mCeil));
    try vm.defineFn(m, "abs", 1, mathUnary(mAbs));
    try vm.defineFn(m, "sqrt", 1, mathUnary(mSqrt));
    try vm.defineFn(m, "trunc", 1, mathUnary(mTrunc));
    try vm.defineFn(m, "sign", 1, mathUnary(mSign));
    try vm.defineFn(m, "round", 1, mathUnary(mRound));
    try vm.defineFn(m, "log", 1, mathUnary(mLog));
    try vm.defineFn(m, "log2", 1, mathUnary(mLog2));
    try vm.defineFn(m, "log10", 1, mathUnary(mLog10));
    try vm.defineFn(m, "exp", 1, mathUnary(mExp));
    try vm.defineFn(m, "sin", 1, mathUnary(mSin));
    try vm.defineFn(m, "cos", 1, mathUnary(mCos));
    try vm.defineFn(m, "tan", 1, mathUnary(mTan));
    try vm.defineFn(m, "asin", 1, mathUnary(mAsin));
    try vm.defineFn(m, "acos", 1, mathUnary(mAcos));
    try vm.defineFn(m, "atan", 1, mathUnary(mAtan));
    try vm.defineFn(m, "cbrt", 1, mathUnary(mCbrt));
    try vm.defineFn(m, "max", 2, mathMax);
    try vm.defineFn(m, "min", 2, mathMin);
    try vm.defineFn(m, "pow", 2, mathPow);
    try vm.defineFn(m, "atan2", 2, mathAtan2);
    try vm.defineFn(m, "hypot", 2, mathHypot);
    try vm.defineFn(m, "random", 0, mathRandom);
    try vm.define(m, "PI", .{ .number = std.math.pi });
    try vm.define(m, "E", .{ .number = std.math.e });
    try vm.define(m, "LN2", .{ .number = @log(2.0) });
    try vm.define(m, "LN10", .{ .number = @log(10.0) });
    try vm.define(m, "SQRT2", .{ .number = @sqrt(2.0) });
    try vm.globals.props.put(vm.heap.alloc, "Math", .{ .object = m });
}

// ==========================================================================
// JSON
// ==========================================================================

fn jsonStringify(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    var indent: []const u8 = "";
    var ind_buf: [10]u8 = undefined;
    const sp = arg(args, 2);
    if (sp == .number) {
        const n: usize = @intFromFloat(@max(0, @min(10, @trunc(sp.number))));
        @memset(ind_buf[0..n], ' ');
        indent = ind_buf[0..n];
    } else if (sp == .string) {
        indent = sp.string.bytes[0..@min(10, sp.string.bytes.len)];
    }
    var out = std.ArrayListUnmanaged(u8){};
    defer out.deinit(vm.gpa);
    const wrote = try jsonWrite(vm, &out, arg(args, 0), indent, 0);
    if (!wrote) return .undefined;
    return vm.str(out.items);
}

fn jsonWrite(
    vm: *Vm,
    out: *std.ArrayListUnmanaged(u8),
    v: Value,
    indent: []const u8,
    depth: u32,
) anyerror!bool {
    const a = vm.gpa;
    if (depth > 100) return vm.throwType("converting circular structure to JSON", .{});
    var cur = v;
    if (cur == .object) {
        const tj = try vm.getProp(cur, "toJSON");
        if (tj.isCallable()) cur = try vm.callValue(tj, cur, &.{});
    }
    switch (cur) {
        .undefined => return false,
        .null => try out.appendSlice(a, "null"),
        .boolean => |b| try out.appendSlice(a, if (b) "true" else "false"),
        .number => |n| {
            if (!std.math.isFinite(n)) {
                try out.appendSlice(a, "null");
            } else {
                const s = try vmod.numberToString(a, n);
                defer a.free(s);
                try out.appendSlice(a, s);
            }
        },
        .string => |s| try jsonQuote(a, out, s.bytes),
        .object => |o| {
            if (o.callable()) return false;
            var root = Root.open(vm);
            defer root.close();
            try root.add(cur);
            if (o.class == .array) {
                try out.append(a, '[');
                for (o.data.elements.items, 0..) |e, i| {
                    if (i > 0) try out.append(a, ',');
                    try jsonNewline(a, out, indent, depth + 1);
                    if (!try jsonWrite(vm, out, e, indent, depth + 1)) try out.appendSlice(a, "null");
                }
                if (o.data.elements.items.len > 0) try jsonNewline(a, out, indent, depth);
                try out.append(a, ']');
                return true;
            }
            const keys = try ownKeys(vm, cur, .keys);
            try root.add(keys);
            try out.append(a, '{');
            var first = true;
            for (keys.object.data.elements.items) |k| {
                const pv = try vm.getProp(cur, k.string.bytes);
                const mark = out.items.len;
                if (!first) try out.append(a, ',');
                try jsonNewline(a, out, indent, depth + 1);
                try jsonQuote(a, out, k.string.bytes);
                try out.append(a, ':');
                if (indent.len > 0) try out.append(a, ' ');
                if (!try jsonWrite(vm, out, pv, indent, depth + 1)) {
                    out.items.len = mark; // an undefined member is omitted entirely
                    continue;
                }
                first = false;
            }
            if (!first) try jsonNewline(a, out, indent, depth);
            try out.append(a, '}');
        },
    }
    return true;
}

fn jsonNewline(a: std.mem.Allocator, out: *std.ArrayListUnmanaged(u8), indent: []const u8, depth: u32) !void {
    if (indent.len == 0) return;
    try out.append(a, '\n');
    for (0..depth) |_| try out.appendSlice(a, indent);
}

fn jsonQuote(a: std.mem.Allocator, out: *std.ArrayListUnmanaged(u8), s: []const u8) !void {
    try out.append(a, '"');
    for (s) |c| switch (c) {
        '"' => try out.appendSlice(a, "\\\""),
        '\\' => try out.appendSlice(a, "\\\\"),
        '\n' => try out.appendSlice(a, "\\n"),
        '\r' => try out.appendSlice(a, "\\r"),
        '\t' => try out.appendSlice(a, "\\t"),
        0x08 => try out.appendSlice(a, "\\b"),
        0x0c => try out.appendSlice(a, "\\f"),
        0...7, 0x0b, 0x0e...0x1f => {
            var buf: [6]u8 = undefined;
            try out.appendSlice(a, try std.fmt.bufPrint(&buf, "\\u{x:0>4}", .{c}));
        },
        else => try out.append(a, c),
    };
    try out.append(a, '"');
}

const JsonParser = struct {
    vm: *Vm,
    src: []const u8,
    i: usize = 0,

    fn ws(self: *JsonParser) void {
        while (self.i < self.src.len and (self.src[self.i] == ' ' or self.src[self.i] == '\t' or
            self.src[self.i] == '\n' or self.src[self.i] == '\r')) self.i += 1;
    }

    fn fail(self: *JsonParser) Error {
        return self.vm.throwError("SyntaxError", "Unexpected token in JSON at position {d}", .{self.i});
    }

    fn value(self: *JsonParser, depth: u32) anyerror!Value {
        if (depth > 200) return self.fail();
        self.ws();
        if (self.i >= self.src.len) return self.fail();
        const c = self.src[self.i];
        switch (c) {
            '{' => {
                self.i += 1;
                const o = try self.vm.newObject();
                var root = Root.open(self.vm);
                defer root.close();
                try root.add(.{ .object = o });
                self.ws();
                if (self.i < self.src.len and self.src[self.i] == '}') {
                    self.i += 1;
                    return .{ .object = o };
                }
                while (true) {
                    self.ws();
                    const k = try self.string();
                    try root.add(k);
                    self.ws();
                    if (self.i >= self.src.len or self.src[self.i] != ':') return self.fail();
                    self.i += 1;
                    const v = try self.value(depth + 1);
                    try o.props.put(self.vm.heap.alloc, k.string.bytes, v);
                    self.ws();
                    if (self.i < self.src.len and self.src[self.i] == ',') {
                        self.i += 1;
                        continue;
                    }
                    if (self.i < self.src.len and self.src[self.i] == '}') {
                        self.i += 1;
                        return .{ .object = o };
                    }
                    return self.fail();
                }
            },
            '[' => {
                self.i += 1;
                const arr = try self.vm.newArray(&.{});
                var root = Root.open(self.vm);
                defer root.close();
                try root.add(.{ .object = arr });
                self.ws();
                if (self.i < self.src.len and self.src[self.i] == ']') {
                    self.i += 1;
                    return .{ .object = arr };
                }
                while (true) {
                    const v = try self.value(depth + 1);
                    try arr.data.elements.append(self.vm.heap.alloc, v);
                    self.ws();
                    if (self.i < self.src.len and self.src[self.i] == ',') {
                        self.i += 1;
                        continue;
                    }
                    if (self.i < self.src.len and self.src[self.i] == ']') {
                        self.i += 1;
                        return .{ .object = arr };
                    }
                    return self.fail();
                }
            },
            '"' => return self.string(),
            't' => {
                if (!std.mem.startsWith(u8, self.src[self.i..], "true")) return self.fail();
                self.i += 4;
                return .{ .boolean = true };
            },
            'f' => {
                if (!std.mem.startsWith(u8, self.src[self.i..], "false")) return self.fail();
                self.i += 5;
                return .{ .boolean = false };
            },
            'n' => {
                if (!std.mem.startsWith(u8, self.src[self.i..], "null")) return self.fail();
                self.i += 4;
                return .null;
            },
            else => {
                const start = self.i;
                if (self.i < self.src.len and (self.src[self.i] == '-' or self.src[self.i] == '+')) self.i += 1;
                while (self.i < self.src.len) : (self.i += 1) {
                    const d = self.src[self.i];
                    if ((d >= '0' and d <= '9') or d == '.' or d == 'e' or d == 'E' or d == '+' or d == '-') continue;
                    break;
                }
                if (self.i == start) return self.fail();
                const n = std.fmt.parseFloat(f64, self.src[start..self.i]) catch return self.fail();
                return .{ .number = n };
            },
        }
    }

    fn string(self: *JsonParser) anyerror!Value {
        if (self.i >= self.src.len or self.src[self.i] != '"') return self.fail();
        self.i += 1;
        var out = std.ArrayListUnmanaged(u8){};
        defer out.deinit(self.vm.gpa);
        const a = self.vm.gpa;
        while (self.i < self.src.len) {
            const c = self.src[self.i];
            if (c == '"') {
                self.i += 1;
                return self.vm.str(out.items);
            }
            if (c == '\\') {
                self.i += 1;
                if (self.i >= self.src.len) return self.fail();
                const e = self.src[self.i];
                self.i += 1;
                switch (e) {
                    'n' => try out.append(a, '\n'),
                    't' => try out.append(a, '\t'),
                    'r' => try out.append(a, '\r'),
                    'b' => try out.append(a, 0x08),
                    'f' => try out.append(a, 0x0c),
                    '/' => try out.append(a, '/'),
                    '"' => try out.append(a, '"'),
                    '\\' => try out.append(a, '\\'),
                    'u' => {
                        if (self.i + 4 > self.src.len) return self.fail();
                        var cp: u32 = std.fmt.parseInt(u32, self.src[self.i..][0..4], 16) catch return self.fail();
                        self.i += 4;
                        if (cp >= 0xD800 and cp < 0xDC00 and self.i + 6 <= self.src.len and
                            self.src[self.i] == '\\' and self.src[self.i + 1] == 'u')
                        {
                            const lo = std.fmt.parseInt(u32, self.src[self.i + 2 ..][0..4], 16) catch 0;
                            if (lo >= 0xDC00 and lo < 0xE000) {
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                                self.i += 6;
                            }
                        }
                        if (cp >= 0xD800 and cp < 0xE000) cp = 0xFFFD;
                        var buf: [4]u8 = undefined;
                        const n = std.unicode.utf8Encode(@intCast(cp), &buf) catch continue;
                        try out.appendSlice(a, buf[0..n]);
                    },
                    else => return self.fail(),
                }
                continue;
            }
            try out.append(a, c);
            self.i += 1;
        }
        return self.fail();
    }
};

fn jsonParse(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try vm.toString(arg(args, 0));
    try root.add(s);
    var p = JsonParser{ .vm = vm, .src = s.string.bytes };
    const v = try p.value(0);
    p.ws();
    if (p.i != p.src.len) return p.fail();
    return v;
}

fn installJson(vm: *Vm) !void {
    const j = try vm.heap.newObj(.plain, vm.object_proto);
    try vm.defineFn(j, "stringify", 3, jsonStringify);
    try vm.defineFn(j, "parse", 2, jsonParse);
    try vm.globals.props.put(vm.heap.alloc, "JSON", .{ .object = j });
}

// ==========================================================================
// Error
// ==========================================================================

fn errorToString(vm: *Vm, o: *Obj) ![]u8 {
    const name = try vm.getProp(.{ .object = o }, "name");
    const msg = try vm.getProp(.{ .object = o }, "message");
    const n = if (name == .string) name.string.bytes else "Error";
    const m = if (msg == .string) msg.string.bytes else "";
    if (m.len == 0) return vm.gpa.dupe(u8, n);
    return std.fmt.allocPrint(vm.gpa, "{s}: {s}", .{ n, m });
}

fn errToString(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const vm = V(ctx);
    if (this != .object) return vm.str("Error");
    const s = try errorToString(vm, this.object);
    defer vm.gpa.free(s);
    return vm.str(s);
}

fn makeErrorCtor(vm: *Vm, name: []const u8, proto: *Obj) !*Obj {
    const f = struct {
        fn call(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
            const v = V(ctx);
            const pv = try v.getProp(.{ .object = callee }, "prototype");
            const o = try v.heap.newObj(.err, if (pv == .object) pv.object else v.error_proto);
            var root = Root.open(v);
            defer root.close();
            try root.add(.{ .object = o });
            if (arg(args, 0) != .undefined) {
                const m = try v.toString(args[0]);
                try o.props.putProp(v.heap.alloc, "message", .{ .key = undefined, .value = m, .enumerable = false });
            }
            const s = try errorToString(v, o);
            defer v.gpa.free(s);
            try o.props.putProp(v.heap.alloc, "stack", .{
                .key = undefined,
                .value = try v.str(s),
                .enumerable = false,
            });
            _ = this;
            return .{ .object = o };
        }
    }.call;
    const ctor = try vm.newNative(name, 1, f);
    try vm.define(ctor, "prototype", .{ .object = proto });
    try vm.define(proto, "constructor", .{ .object = ctor });
    try vm.define(proto, "name", try vm.str(name));
    try vm.define(proto, "message", try vm.str(""));
    try vm.globals.props.put(vm.heap.alloc, name, .{ .object = ctor });
    return ctor;
}

fn installError(vm: *Vm) !void {
    try vm.defineFn(vm.error_proto, "toString", 0, errToString);
    _ = try makeErrorCtor(vm, "Error", vm.error_proto);
    for ([_][]const u8{ "TypeError", "RangeError", "SyntaxError", "ReferenceError", "EvalError", "URIError" }) |n| {
        const p = try vm.heap.newObj(.plain, vm.error_proto);
        _ = try makeErrorCtor(vm, n, p);
    }
}

// ==========================================================================
// Map and Set
// ==========================================================================

fn entryIndex(vm: *Vm, o: *Obj, key: Value) ?usize {
    for (o.data.entries.items, 0..) |e, i| {
        if (e.dead) continue;
        if (vm.strictEquals(e.key, key)) return i;
        if (e.key == .number and key == .number and
            std.math.isNan(e.key.number) and std.math.isNan(key.number)) return i;
    }
    return null;
}

fn liveCount(o: *Obj) f64 {
    var n: f64 = 0;
    for (o.data.entries.items) |e| {
        if (!e.dead) n += 1;
    }
    return n;
}

fn mapCtor(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const o = try vm.heap.newObj(.map, vm.map_proto);
    o.data = .{ .entries = .{} };
    var root = Root.open(vm);
    defer root.close();
    try root.add(.{ .object = o });
    if (!arg(args, 0).isNullish()) {
        const it = try vm.makeIterator(args[0], false);
        try root.add(.{ .object = it });
        for (it.data.elements.items) |pair| {
            const k = try vm.getIndex(pair, .{ .number = 0 });
            const v = try vm.getIndex(pair, .{ .number = 1 });
            try o.data.entries.append(vm.heap.alloc, .{ .key = k, .value = v });
        }
    }
    return .{ .object = o };
}

fn setCtor(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const o = try vm.heap.newObj(.set, vm.set_proto);
    o.data = .{ .entries = .{} };
    var root = Root.open(vm);
    defer root.close();
    try root.add(.{ .object = o });
    if (!arg(args, 0).isNullish()) {
        const it = try vm.makeIterator(args[0], false);
        try root.add(.{ .object = it });
        for (it.data.elements.items) |v| {
            if (entryIndex(vm, o, v) == null) {
                try o.data.entries.append(vm.heap.alloc, .{ .key = v, .value = v });
            }
        }
    }
    return .{ .object = o };
}

fn requireEntries(vm: *Vm, this: Value) Error!*Obj {
    if (this != .object or (this.object.class != .map and this.object.class != .set)) {
        return vm.throwType("not a Map or Set", .{});
    }
    return this.object;
}

fn mapGet(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const o = try requireEntries(vm, this);
    const i = entryIndex(vm, o, arg(args, 0)) orelse return .undefined;
    return o.data.entries.items[i].value;
}

fn mapSet(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const o = try requireEntries(vm, this);
    const k = arg(args, 0);
    const v = arg(args, 1);
    if (entryIndex(vm, o, k)) |i| {
        o.data.entries.items[i].value = v;
    } else {
        try o.data.entries.append(vm.heap.alloc, .{ .key = k, .value = v });
    }
    return this;
}

fn setAdd(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const o = try requireEntries(vm, this);
    const k = arg(args, 0);
    if (entryIndex(vm, o, k) == null) {
        try o.data.entries.append(vm.heap.alloc, .{ .key = k, .value = k });
    }
    return this;
}

fn mapHas(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const o = try requireEntries(vm, this);
    return .{ .boolean = entryIndex(vm, o, arg(args, 0)) != null };
}

fn mapDelete(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const o = try requireEntries(vm, this);
    const i = entryIndex(vm, o, arg(args, 0)) orelse return .{ .boolean = false };
    _ = o.data.entries.orderedRemove(i);
    return .{ .boolean = true };
}

fn mapClear(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const o = try requireEntries(V(ctx), this);
    o.data.entries.clearRetainingCapacity();
    return .undefined;
}

fn mapSize(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const o = try requireEntries(V(ctx), this);
    return .{ .number = liveCount(o) };
}

fn mapForEach(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const o = try requireEntries(vm, this);
    const f = arg(args, 0);
    if (!f.isCallable()) return vm.throwType("callback is not a function", .{});
    const snapshot = try vm.gpa.dupe(val.MapEntry, o.data.entries.items);
    defer vm.gpa.free(snapshot);
    var root = Root.open(vm);
    defer root.close();
    for (snapshot) |e| {
        if (e.dead) continue;
        _ = try vm.callValue(f, arg(args, 1), &.{ e.value, e.key, this });
    }
    return .undefined;
}

fn mapKeysOrValues(vm: *Vm, this: Value, want: enum { keys, values, entries }) anyerror!Value {
    const o = try requireEntries(vm, this);
    var items = std.ArrayListUnmanaged(Value){};
    defer items.deinit(vm.gpa);
    var root = Root.open(vm);
    defer root.close();
    for (o.data.entries.items) |e| {
        if (e.dead) continue;
        const v: Value = switch (want) {
            .keys => e.key,
            .values => e.value,
            .entries => .{ .object = try vm.newArray(&.{ e.key, e.value }) },
        };
        try items.append(vm.gpa, v);
        try root.add(v);
    }
    return .{ .object = try vm.newArray(items.items) };
}

fn mapKeys(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    return mapKeysOrValues(V(ctx), this, .keys);
}
fn mapValues(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    return mapKeysOrValues(V(ctx), this, .values);
}
fn mapEntries(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    return mapKeysOrValues(V(ctx), this, .entries);
}

fn installMapSet(vm: *Vm) !void {
    const mp = vm.map_proto;
    try vm.defineFn(mp, "get", 1, mapGet);
    try vm.defineFn(mp, "set", 2, mapSet);
    try vm.defineFn(mp, "has", 1, mapHas);
    try vm.defineFn(mp, "delete", 1, mapDelete);
    try vm.defineFn(mp, "clear", 0, mapClear);
    try vm.defineFn(mp, "forEach", 1, mapForEach);
    try vm.defineFn(mp, "keys", 0, mapKeys);
    try vm.defineFn(mp, "values", 0, mapValues);
    try vm.defineFn(mp, "entries", 0, mapEntries);
    const size_fn = try vm.newNative("size", 0, mapSize);
    try mp.props.putProp(vm.heap.alloc, "size", .{
        .key = undefined,
        .is_accessor = true,
        .getter = size_fn,
        .enumerable = false,
    });
    const mctor = try vm.newNative("Map", 0, mapCtor);
    try vm.define(mctor, "prototype", .{ .object = mp });
    try vm.define(mp, "constructor", .{ .object = mctor });
    try vm.globals.props.put(vm.heap.alloc, "Map", .{ .object = mctor });
    try vm.globals.props.put(vm.heap.alloc, "WeakMap", .{ .object = mctor });

    const sp = vm.set_proto;
    try vm.defineFn(sp, "add", 1, setAdd);
    try vm.defineFn(sp, "has", 1, mapHas);
    try vm.defineFn(sp, "delete", 1, mapDelete);
    try vm.defineFn(sp, "clear", 0, mapClear);
    try vm.defineFn(sp, "forEach", 1, mapForEach);
    try vm.defineFn(sp, "values", 0, mapValues);
    try vm.defineFn(sp, "keys", 0, mapKeys);
    try sp.props.putProp(vm.heap.alloc, "size", .{
        .key = undefined,
        .is_accessor = true,
        .getter = size_fn,
        .enumerable = false,
    });
    const sctor = try vm.newNative("Set", 0, setCtor);
    try vm.define(sctor, "prototype", .{ .object = sp });
    try vm.define(sp, "constructor", .{ .object = sctor });
    try vm.globals.props.put(vm.heap.alloc, "Set", .{ .object = sctor });
    try vm.globals.props.put(vm.heap.alloc, "WeakSet", .{ .object = sctor });
}

// ==========================================================================
// Date
// ==========================================================================
//
// Civil-from-days after Howard Hinnant's algorithm. There is no timezone
// database: local time is UTC and `getTimezoneOffset` returns zero. A browser
// that renders a page's timestamps an hour out is a nuisance; one that ships
// a copy of tzdata is a different project.

const Civil = struct { y: i64, m: u32, d: u32 };

fn civilFromDays(z_in: i64) Civil {
    var z = z_in + 719468;
    const era = @divFloor(if (z >= 0) z else z - 146096, 146097);
    const doe: u64 = @intCast(z - era * 146097);
    const yoe: u64 = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const y: i64 = @as(i64, @intCast(yoe)) + era * 400;
    const doy: u64 = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const mp: u64 = (5 * doy + 2) / 153;
    const d: u64 = doy - (153 * mp + 2) / 5 + 1;
    const m: u64 = if (mp < 10) mp + 3 else mp - 9;
    z = 0;
    return .{ .y = if (m <= 2) y + 1 else y, .m = @intCast(m), .d = @intCast(d) };
}

fn daysFromCivil(y_in: i64, m: i64, d: i64) i64 {
    const y = if (m <= 2) y_in - 1 else y_in;
    const era = @divFloor(if (y >= 0) y else y - 399, 400);
    const yoe: i64 = y - era * 400;
    const mp: i64 = if (m > 2) m - 3 else m + 9;
    const doy: i64 = @divTrunc(153 * mp + 2, 5) + d - 1;
    const doe: i64 = yoe * 365 + @divFloor(yoe, 4) - @divFloor(yoe, 100) + doy;
    return era * 146097 + doe - 719468;
}

fn dateMs(vm: *Vm, this: Value) Error!f64 {
    if (this != .object or this.object.class != .date) return vm.throwType("not a Date", .{});
    return this.object.data.date;
}

fn dateParts(ms: f64) struct { civil: Civil, h: u32, mi: u32, s: u32, milli: u32, dow: u32 } {
    const total: i64 = @intFromFloat(@floor(ms));
    const days = @divFloor(total, 86_400_000);
    const rem: u64 = @intCast(total - days * 86_400_000);
    const dow: i64 = @mod(days + 4, 7);
    return .{
        .civil = civilFromDays(days),
        .h = @intCast(rem / 3_600_000),
        .mi = @intCast((rem / 60_000) % 60),
        .s = @intCast((rem / 1000) % 60),
        .milli = @intCast(rem % 1000),
        .dow = @intCast(dow),
    };
}

fn dateCtor(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const o = try vm.heap.newObj(.date, vm.date_proto);
    var ms: f64 = @floatFromInt(std.time.milliTimestamp());
    if (args.len == 1) {
        if (args[0] == .string) {
            ms = parseDate(args[0].string.bytes);
        } else {
            ms = try vm.toNumber(args[0]);
        }
    } else if (args.len >= 2) {
        const y: i64 = @intFromFloat(try vm.toNumber(args[0]));
        const mo: i64 = @intFromFloat(try vm.toNumber(args[1]));
        const d: i64 = if (args.len > 2) @intFromFloat(try vm.toNumber(args[2])) else 1;
        const h: i64 = if (args.len > 3) @intFromFloat(try vm.toNumber(args[3])) else 0;
        const mi: i64 = if (args.len > 4) @intFromFloat(try vm.toNumber(args[4])) else 0;
        const s: i64 = if (args.len > 5) @intFromFloat(try vm.toNumber(args[5])) else 0;
        const days = daysFromCivil(y + @divFloor(mo, 12), @mod(mo, 12) + 1, d);
        ms = @floatFromInt(days * 86_400_000 + h * 3_600_000 + mi * 60_000 + s * 1000);
    }
    o.data = .{ .date = ms };
    return .{ .object = o };
}

fn parseDate(s: []const u8) f64 {
    // ISO 8601 only: "YYYY-MM-DD" with an optional "THH:MM:SS(.mmm)(Z)".
    if (s.len < 10) return std.math.nan(f64);
    const y = std.fmt.parseInt(i64, s[0..4], 10) catch return std.math.nan(f64);
    if (s[4] != '-') return std.math.nan(f64);
    const mo = std.fmt.parseInt(i64, s[5..7], 10) catch return std.math.nan(f64);
    const d = std.fmt.parseInt(i64, s[8..10], 10) catch return std.math.nan(f64);
    var ms: i64 = daysFromCivil(y, mo, d) * 86_400_000;
    if (s.len >= 16 and (s[10] == 'T' or s[10] == ' ')) {
        const h = std.fmt.parseInt(i64, s[11..13], 10) catch 0;
        const mi = std.fmt.parseInt(i64, s[14..16], 10) catch 0;
        ms += h * 3_600_000 + mi * 60_000;
        if (s.len >= 19) ms += (std.fmt.parseInt(i64, s[17..19], 10) catch 0) * 1000;
    }
    return @floatFromInt(ms);
}

fn dateNow(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = this;
    _ = args;
    return .{ .number = @floatFromInt(std.time.milliTimestamp()) };
}

fn dateGetter(comptime pick: fn (f64) f64) val.NativeFn {
    return struct {
        fn call(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
            _ = callee;
            _ = args;
            const ms = try dateMs(V(ctx), this);
            if (std.math.isNan(ms)) return .{ .number = ms };
            return .{ .number = pick(ms) };
        }
    }.call;
}

fn gTime(ms: f64) f64 {
    return ms;
}
fn gYear(ms: f64) f64 {
    return @floatFromInt(dateParts(ms).civil.y);
}
fn gMonth(ms: f64) f64 {
    return @floatFromInt(dateParts(ms).civil.m - 1);
}
fn gDate(ms: f64) f64 {
    return @floatFromInt(dateParts(ms).civil.d);
}
fn gDay(ms: f64) f64 {
    return @floatFromInt(dateParts(ms).dow);
}
fn gHours(ms: f64) f64 {
    return @floatFromInt(dateParts(ms).h);
}
fn gMinutes(ms: f64) f64 {
    return @floatFromInt(dateParts(ms).mi);
}
fn gSeconds(ms: f64) f64 {
    return @floatFromInt(dateParts(ms).s);
}
fn gMillis(ms: f64) f64 {
    return @floatFromInt(dateParts(ms).milli);
}
fn gZero(ms: f64) f64 {
    _ = ms;
    return 0;
}

fn dateToISO(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const vm = V(ctx);
    const ms = try dateMs(vm, this);
    if (std.math.isNan(ms)) return vm.throwError("RangeError", "invalid time value", .{});
    const p = dateParts(ms);
    const s = try std.fmt.allocPrint(vm.heap.alloc, "{d:0>4}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}.{d:0>3}Z", .{
        p.civil.y, p.civil.m, p.civil.d, p.h, p.mi, p.s, p.milli,
    });
    return vm.adopt(s);
}

const day_names = [_][]const u8{ "Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat" };
const month_names = [_][]const u8{ "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec" };

fn dateToString(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const vm = V(ctx);
    const ms = try dateMs(vm, this);
    if (std.math.isNan(ms)) return vm.str("Invalid Date");
    const p = dateParts(ms);
    const s = try std.fmt.allocPrint(
        vm.heap.alloc,
        "{s} {s} {d:0>2} {d} {d:0>2}:{d:0>2}:{d:0>2} GMT+0000",
        .{ day_names[p.dow % 7], month_names[(p.civil.m - 1) % 12], p.civil.d, p.civil.y, p.h, p.mi, p.s },
    );
    return vm.adopt(s);
}

fn installDate(vm: *Vm) !void {
    const p = vm.date_proto;
    try vm.defineFn(p, "getTime", 0, dateGetter(gTime));
    try vm.defineFn(p, "valueOf", 0, dateGetter(gTime));
    try vm.defineFn(p, "getFullYear", 0, dateGetter(gYear));
    try vm.defineFn(p, "getUTCFullYear", 0, dateGetter(gYear));
    try vm.defineFn(p, "getMonth", 0, dateGetter(gMonth));
    try vm.defineFn(p, "getUTCMonth", 0, dateGetter(gMonth));
    try vm.defineFn(p, "getDate", 0, dateGetter(gDate));
    try vm.defineFn(p, "getUTCDate", 0, dateGetter(gDate));
    try vm.defineFn(p, "getDay", 0, dateGetter(gDay));
    try vm.defineFn(p, "getHours", 0, dateGetter(gHours));
    try vm.defineFn(p, "getUTCHours", 0, dateGetter(gHours));
    try vm.defineFn(p, "getMinutes", 0, dateGetter(gMinutes));
    try vm.defineFn(p, "getSeconds", 0, dateGetter(gSeconds));
    try vm.defineFn(p, "getMilliseconds", 0, dateGetter(gMillis));
    try vm.defineFn(p, "getTimezoneOffset", 0, dateGetter(gZero));
    try vm.defineFn(p, "toISOString", 0, dateToISO);
    try vm.defineFn(p, "toJSON", 0, dateToISO);
    try vm.defineFn(p, "toString", 0, dateToString);
    try vm.defineFn(p, "toUTCString", 0, dateToString);
    try vm.defineFn(p, "toLocaleString", 0, dateToString);
    try vm.defineFn(p, "toLocaleDateString", 0, dateToString);
    try vm.defineFn(p, "toLocaleTimeString", 0, dateToString);

    const ctor = try vm.newNative("Date", 7, dateCtor);
    try vm.define(ctor, "prototype", .{ .object = p });
    try vm.define(p, "constructor", .{ .object = ctor });
    try vm.defineFn(ctor, "now", 0, dateNow);
    try vm.defineFn(ctor, "parse", 1, dateParseFn);
    try vm.globals.props.put(vm.heap.alloc, "Date", .{ .object = ctor });
}

fn dateParseFn(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const s = try vm.toString(arg(args, 0));
    return .{ .number = parseDate(s.string.bytes) };
}

// ==========================================================================
// RegExp
// ==========================================================================

fn makeRegex(vm: *Vm, pattern: []const u8, flags: []const u8) Error!*Obj {
    const re = try vm.heap.alloc.create(regex.Regex);
    errdefer vm.heap.alloc.destroy(re);
    re.* = regex.Regex.compile(vm.heap.alloc, pattern, flags) catch |e| {
        vm.heap.alloc.destroy(re);
        if (e == error.OutOfMemory) return error.OutOfMemory;
        return vm.throwError("SyntaxError", "invalid regular expression: /{s}/{s}", .{ pattern, flags });
    };
    const o = try vm.heap.newObj(.regexp, vm.regexp_proto);
    const rd = try vm.heap.alloc.create(val.RegexData);
    rd.* = .{
        .prog = re,
        .source = try vm.heap.alloc.dupe(u8, pattern),
        .flags = try vm.heap.alloc.dupe(u8, flags),
    };
    o.data = .{ .regex = rd };
    try vm.define(o, "source", try vm.str(pattern));
    try vm.define(o, "flags", try vm.str(flags));
    try vm.define(o, "global", .{ .boolean = re.flags.global });
    try vm.define(o, "ignoreCase", .{ .boolean = re.flags.ignore_case });
    try vm.define(o, "multiline", .{ .boolean = re.flags.multiline });
    try o.props.putProp(vm.heap.alloc, "lastIndex", .{
        .key = undefined,
        .value = .{ .number = 0 },
        .enumerable = false,
    });
    return o;
}

/// Used by the compiler's constant pool for a `/re/` literal.
pub fn newRegExp(vm: *Vm, pattern: []const u8, flags: []const u8) Error!Value {
    return .{ .object = try makeRegex(vm, pattern, flags) };
}

fn regexpCtor(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const a = arg(args, 0);
    if (a == .object and a.object.class == .regexp and arg(args, 1) == .undefined) return a;
    var root = Root.open(vm);
    defer root.close();
    const pat = if (a == .object and a.object.class == .regexp)
        try vm.str(a.object.data.regex.source)
    else
        try vm.toString(a);
    try root.add(pat);
    var flags: []const u8 = "";
    if (arg(args, 1) != .undefined) {
        const f = try vm.toString(args[1]);
        try root.add(f);
        flags = f.string.bytes;
    }
    return .{ .object = try makeRegex(vm, pat.string.bytes, flags) };
}

fn thisRegex(vm: *Vm, this: Value) Error!*Obj {
    if (this != .object or this.object.class != .regexp) return vm.throwType("not a RegExp", .{});
    return this.object;
}

fn buildMatch(vm: *Vm, re: *const regex.Regex, input: []const u8, caps: []const ?regex.Span) anyerror!Value {
    const arr = try vm.newArray(&.{});
    var root = Root.open(vm);
    defer root.close();
    try root.add(.{ .object = arr });
    for (caps) |c| {
        if (c) |sp| {
            try arr.data.elements.append(vm.heap.alloc, try vm.str(input[sp.start..sp.end]));
        } else {
            try arr.data.elements.append(vm.heap.alloc, .undefined);
        }
    }
    try vm.define(arr, "index", .{ .number = @floatFromInt(caps[0].?.start) });
    try vm.define(arr, "input", try vm.str(input));
    var named: ?*Obj = null;
    for (re.group_names, 0..) |n, i| {
        if (i == 0 or n.len == 0) continue;
        if (named == null) named = try vm.newObject();
        const v: Value = if (i < caps.len and caps[i] != null)
            try vm.str(input[caps[i].?.start..caps[i].?.end])
        else
            .undefined;
        try named.?.props.put(vm.heap.alloc, n, v);
    }
    try vm.define(arr, "groups", if (named) |n| .{ .object = n } else .undefined);
    return .{ .object = arr };
}

fn regexpExec(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    const o = try thisRegex(vm, this);
    const rd = o.data.regex;
    const re: *const regex.Regex = @ptrCast(@alignCast(rd.prog));
    var root = Root.open(vm);
    defer root.close();
    const s = try vm.toString(arg(args, 0));
    try root.add(s);
    var start: u32 = 0;
    if (re.flags.global or re.flags.sticky) {
        const li = try vm.getProp(this, "lastIndex");
        const n = try vm.toNumber(li);
        if (n > 0) start = @intFromFloat(@min(n, @as(f64, @floatFromInt(s.string.bytes.len + 1))));
    }
    var caps_buf: [64]?regex.Span = undefined;
    if (re.group_count + 1 > caps_buf.len) return .null;
    const caps = caps_buf[0 .. re.group_count + 1];
    if (start > s.string.bytes.len or !re.exec(s.string.bytes, start, caps)) {
        try vm.setProp(this, "lastIndex", .{ .number = 0 });
        return .null;
    }
    if (re.flags.global or re.flags.sticky) {
        const end = caps[0].?.end;
        const next: u32 = if (end == caps[0].?.start) end + 1 else end;
        try vm.setProp(this, "lastIndex", .{ .number = @floatFromInt(next) });
    }
    return buildMatch(vm, re, s.string.bytes, caps);
}

fn regexpTest(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    const r = try regexpExec(ctx, callee, this, args);
    return .{ .boolean = r != .null };
}

fn regexpToString(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = args;
    const vm = V(ctx);
    const o = try thisRegex(vm, this);
    const rd = o.data.regex;
    return vm.adopt(try std.fmt.allocPrint(vm.heap.alloc, "/{s}/{s}", .{ rd.source, rd.flags }));
}

fn installRegExp(vm: *Vm) !void {
    const p = vm.regexp_proto;
    try vm.defineFn(p, "exec", 1, regexpExec);
    try vm.defineFn(p, "test", 1, regexpTest);
    try vm.defineFn(p, "toString", 0, regexpToString);
    const ctor = try vm.newNative("RegExp", 2, regexpCtor);
    try vm.define(ctor, "prototype", .{ .object = p });
    try vm.define(p, "constructor", .{ .object = ctor });
    try vm.globals.props.put(vm.heap.alloc, "RegExp", .{ .object = ctor });
}

// -- String methods that need the matcher -----------------------------------

fn expandReplacement(
    vm: *Vm,
    out: *std.ArrayListUnmanaged(u8),
    tmpl: []const u8,
    input: []const u8,
    caps: []const ?regex.Span,
) !void {
    const a = vm.gpa;
    var i: usize = 0;
    while (i < tmpl.len) {
        if (tmpl[i] == '$' and i + 1 < tmpl.len) {
            const c = tmpl[i + 1];
            if (c == '$') {
                try out.append(a, '$');
                i += 2;
                continue;
            }
            if (c == '&') {
                try out.appendSlice(a, input[caps[0].?.start..caps[0].?.end]);
                i += 2;
                continue;
            }
            if (c >= '0' and c <= '9') {
                var n: usize = c - '0';
                var used: usize = 2;
                if (i + 2 < tmpl.len and tmpl[i + 2] >= '0' and tmpl[i + 2] <= '9') {
                    const two = n * 10 + (tmpl[i + 2] - '0');
                    if (two < caps.len) {
                        n = two;
                        used = 3;
                    }
                }
                if (n > 0 and n < caps.len) {
                    if (caps[n]) |sp| try out.appendSlice(a, input[sp.start..sp.end]);
                    i += used;
                    continue;
                }
            }
        }
        try out.append(a, tmpl[i]);
        i += 1;
    }
}

fn replaceImpl(vm: *Vm, this: Value, args: []const Value, force_all: bool) anyerror!Value {
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    const input = s.string.bytes;
    const pat = arg(args, 0);
    const rep = arg(args, 1);
    var out = std.ArrayListUnmanaged(u8){};
    defer out.deinit(vm.gpa);

    if (pat == .object and pat.object.class == .regexp) {
        const rd = pat.object.data.regex;
        const re: *const regex.Regex = @ptrCast(@alignCast(rd.prog));
        const all = force_all or re.flags.global;
        var caps_buf: [64]?regex.Span = undefined;
        if (re.group_count + 1 > caps_buf.len) return s;
        const caps = caps_buf[0 .. re.group_count + 1];
        var pos: u32 = 0;
        var last: u32 = 0;
        while (pos <= input.len) {
            if (!re.exec(input, pos, caps)) break;
            const m = caps[0].?;
            try out.appendSlice(vm.gpa, input[last..m.start]);
            if (rep.isCallable()) {
                var call_args = std.ArrayListUnmanaged(Value){};
                defer call_args.deinit(vm.gpa);
                for (caps) |c| {
                    const cv: Value = if (c) |sp| try vm.str(input[sp.start..sp.end]) else .undefined;
                    try call_args.append(vm.gpa, cv);
                    try root.add(cv);
                }
                try call_args.append(vm.gpa, .{ .number = @floatFromInt(m.start) });
                try call_args.append(vm.gpa, s);
                const r = try vm.callValue(rep, .undefined, call_args.items);
                const rs = try vm.toString(r);
                try root.add(rs);
                try out.appendSlice(vm.gpa, rs.string.bytes);
            } else {
                const rs = try vm.toString(rep);
                try root.add(rs);
                try expandReplacement(vm, &out, rs.string.bytes, input, caps);
            }
            last = m.end;
            pos = if (m.end == m.start) m.end + 1 else m.end;
            if (!all) break;
        }
        if (last <= input.len) try out.appendSlice(vm.gpa, input[last..]);
        return vm.str(out.items);
    }

    const needle = try vm.toString(pat);
    try root.add(needle);
    const nb = needle.string.bytes;
    var from: usize = 0;
    while (true) {
        const at = if (nb.len == 0)
            (if (from == 0) @as(?usize, 0) else null)
        else
            std.mem.indexOfPos(u8, input, from, nb);
        if (at == null) break;
        try out.appendSlice(vm.gpa, input[from..at.?]);
        if (rep.isCallable()) {
            const r = try vm.callValue(rep, .undefined, &.{
                needle, .{ .number = @floatFromInt(at.?) }, s,
            });
            const rs = try vm.toString(r);
            try root.add(rs);
            try out.appendSlice(vm.gpa, rs.string.bytes);
        } else {
            const rs = try vm.toString(rep);
            try root.add(rs);
            var one = [_]?regex.Span{.{ .start = @intCast(at.?), .end = @intCast(at.? + nb.len) }};
            try expandReplacement(vm, &out, rs.string.bytes, input, &one);
        }
        from = at.? + @max(1, nb.len);
        if (!force_all) break;
        if (from > input.len) break;
    }
    if (from <= input.len) try out.appendSlice(vm.gpa, input[from..]);
    return vm.str(out.items);
}

fn strReplace(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    return replaceImpl(V(ctx), this, args, false);
}

fn strReplaceAll(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    return replaceImpl(V(ctx), this, args, true);
}

fn strMatch(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    var pat = arg(args, 0);
    if (pat != .object or pat.object.class != .regexp) {
        const p = try vm.toString(pat);
        try root.add(p);
        pat = .{ .object = try makeRegex(vm, p.string.bytes, "") };
        try root.add(pat);
    }
    const re: *const regex.Regex = @ptrCast(@alignCast(pat.object.data.regex.prog));
    if (!re.flags.global) return regexpExec(ctx, undefined, pat, &.{s});
    var caps_buf: [64]?regex.Span = undefined;
    if (re.group_count + 1 > caps_buf.len) return .null;
    const caps = caps_buf[0 .. re.group_count + 1];
    const out = try vm.newArray(&.{});
    try root.add(.{ .object = out });
    var pos: u32 = 0;
    const input = s.string.bytes;
    while (pos <= input.len and re.exec(input, pos, caps)) {
        const m = caps[0].?;
        try out.data.elements.append(vm.heap.alloc, try vm.str(input[m.start..m.end]));
        pos = if (m.end == m.start) m.end + 1 else m.end;
    }
    if (out.data.elements.items.len == 0) return .null;
    return .{ .object = out };
}

fn strSearch(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try thisString(vm, this);
    try root.add(s);
    var pat = arg(args, 0);
    if (pat != .object or pat.object.class != .regexp) {
        const p = try vm.toString(pat);
        try root.add(p);
        pat = .{ .object = try makeRegex(vm, p.string.bytes, "") };
        try root.add(pat);
    }
    const re: *const regex.Regex = @ptrCast(@alignCast(pat.object.data.regex.prog));
    var caps_buf: [64]?regex.Span = undefined;
    if (re.group_count + 1 > caps_buf.len) return .{ .number = -1 };
    const caps = caps_buf[0 .. re.group_count + 1];
    if (!re.exec(s.string.bytes, 0, caps)) return .{ .number = -1 };
    return .{ .number = @floatFromInt(caps[0].?.start) };
}

// ==========================================================================
// Promise
// ==========================================================================

fn promiseCtor(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const p = try vm.newPromise();
    var root = Root.open(vm);
    defer root.close();
    try root.add(.{ .object = p });
    const executor = arg(args, 0);
    if (executor.isCallable()) {
        const res = try vm.newNative("resolve", 1, settleBound);
        try vm.define(res, "#p", .{ .object = p });
        try vm.define(res, "#ok", .{ .boolean = true });
        const rej = try vm.newNative("reject", 1, settleBound);
        try vm.define(rej, "#p", .{ .object = p });
        try vm.define(rej, "#ok", .{ .boolean = false });
        _ = vm.callValue(executor, .undefined, &.{ .{ .object = res }, .{ .object = rej } }) catch |e| {
            if (e != error.JsThrow) return e;
            const exc = vm.exception;
            vm.exception = .undefined;
            try vm.rejectPromise(p, exc);
        };
    }
    return .{ .object = p };
}

fn settleBound(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = this;
    const vm = V(ctx);
    const p = (callee.props.find("#p") orelse return .undefined).value;
    const ok = (callee.props.find("#ok") orelse return .undefined).value;
    const v = arg(args, 0);
    if (ok == .boolean and ok.boolean) {
        try vm.resolvePromise(p.object, v);
    } else {
        try vm.rejectPromise(p.object, v);
    }
    return .undefined;
}

fn promiseThen(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    if (this != .object or this.object.class != .promise) return vm.throwType("not a promise", .{});
    const next = try vm.newPromise();
    const on_ok = arg(args, 0);
    const on_err = arg(args, 1);
    try vm.addReaction(
        this.object,
        if (on_ok.isCallable()) on_ok.object else null,
        if (on_err.isCallable()) on_err.object else null,
        next,
    );
    return .{ .object = next };
}

fn promiseCatch(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    return promiseThen(ctx, callee, this, &.{ .undefined, arg(args, 0) });
}

fn promiseFinally(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    const f = arg(args, 0);
    return promiseThen(ctx, callee, this, &.{ f, f });
}

fn promiseResolveStatic(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    return .{ .object = try vm.toPromise(arg(args, 0)) };
}

fn promiseRejectStatic(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const p = try vm.newPromise();
    try vm.rejectPromise(p, arg(args, 0));
    return .{ .object = p };
}

/// `Promise.all` and friends. The combinator holds its state in properties of
/// the per-element callback rather than in Zig locals, so a collection during
/// any element's settlement cannot lose it.
fn promiseCombinator(vm: *Vm, args: []const Value, mode: enum { all, race, all_settled, any }) anyerror!Value {
    var root = Root.open(vm);
    defer root.close();
    const result = try vm.newPromise();
    try root.add(.{ .object = result });
    const it = try vm.makeIterator(arg(args, 0), false);
    try root.add(.{ .object = it });
    const items = it.data.elements.items;
    const acc = try vm.newArray(&.{});
    try root.add(.{ .object = acc });
    try acc.data.elements.appendNTimes(vm.heap.alloc, .undefined, items.len);
    const state = try vm.newObject();
    try root.add(.{ .object = state });
    try vm.define(state, "left", .{ .number = @floatFromInt(items.len) });
    try vm.define(state, "acc", .{ .object = acc });
    try vm.define(state, "target", .{ .object = result });
    try vm.define(state, "mode", .{ .number = @floatFromInt(@intFromEnum(mode)) });

    if (items.len == 0) {
        switch (mode) {
            .all, .all_settled => try vm.resolvePromise(result, .{ .object = acc }),
            .any => try vm.rejectPromise(result, .{ .object = try vm.newError("AggregateError", "all promises were rejected") }),
            .race => {},
        }
        return .{ .object = result };
    }

    for (items, 0..) |item, i| {
        const p = try vm.toPromise(item);
        try root.add(.{ .object = p });
        const ok = try vm.newNative("", 1, combinatorStep);
        try vm.define(ok, "#state", .{ .object = state });
        try vm.define(ok, "#i", .{ .number = @floatFromInt(i) });
        try vm.define(ok, "#ok", .{ .boolean = true });
        const err = try vm.newNative("", 1, combinatorStep);
        try vm.define(err, "#state", .{ .object = state });
        try vm.define(err, "#i", .{ .number = @floatFromInt(i) });
        try vm.define(err, "#ok", .{ .boolean = false });
        try vm.addReaction(p, ok, err, try vm.newPromise());
    }
    return .{ .object = result };
}

fn combinatorStep(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = this;
    const vm = V(ctx);
    const state = (callee.props.find("#state") orelse return .undefined).value.object;
    const i: usize = @intFromFloat((callee.props.find("#i") orelse return .undefined).value.number);
    const ok = (callee.props.find("#ok") orelse return .undefined).value.boolean;
    const target = (state.props.find("target") orelse return .undefined).value.object;
    const acc = (state.props.find("acc") orelse return .undefined).value.object;
    const mode: u8 = @intFromFloat((state.props.find("mode") orelse return .undefined).value.number);
    const v = arg(args, 0);

    switch (mode) {
        0 => { // all
            if (!ok) {
                try vm.rejectPromise(target, v);
                return .undefined;
            }
            acc.data.elements.items[i] = v;
        },
        1 => { // race
            if (ok) {
                try vm.resolvePromise(target, v);
            } else {
                try vm.rejectPromise(target, v);
            }
            return .undefined;
        },
        2 => { // allSettled
            const rec = try vm.newObject();
            var root = Root.open(vm);
            defer root.close();
            try root.add(.{ .object = rec });
            try rec.props.put(vm.heap.alloc, "status", try vm.str(if (ok) "fulfilled" else "rejected"));
            try rec.props.put(vm.heap.alloc, if (ok) "value" else "reason", v);
            acc.data.elements.items[i] = .{ .object = rec };
        },
        else => { // any
            if (ok) {
                try vm.resolvePromise(target, v);
                return .undefined;
            }
            acc.data.elements.items[i] = v;
        },
    }
    const left_prop = state.props.find("left").?;
    left_prop.value = .{ .number = left_prop.value.number - 1 };
    if (left_prop.value.number <= 0) {
        if (mode == 3) {
            try vm.rejectPromise(target, .{ .object = try vm.newError("AggregateError", "all promises were rejected") });
            return .undefined;
        }
        try vm.resolvePromise(target, .{ .object = acc });
    }
    return .undefined;
}

fn promiseAll(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return promiseCombinator(V(ctx), args, .all);
}
fn promiseRace(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return promiseCombinator(V(ctx), args, .race);
}
fn promiseAllSettled(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return promiseCombinator(V(ctx), args, .all_settled);
}
fn promiseAny(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return promiseCombinator(V(ctx), args, .any);
}

fn installPromise(vm: *Vm) !void {
    const p = vm.promise_proto;
    try vm.defineFn(p, "then", 2, promiseThen);
    try vm.defineFn(p, "catch", 1, promiseCatch);
    try vm.defineFn(p, "finally", 1, promiseFinally);
    const ctor = try vm.newNative("Promise", 1, promiseCtor);
    try vm.define(ctor, "prototype", .{ .object = p });
    try vm.define(p, "constructor", .{ .object = ctor });
    try vm.defineFn(ctor, "resolve", 1, promiseResolveStatic);
    try vm.defineFn(ctor, "reject", 1, promiseRejectStatic);
    try vm.defineFn(ctor, "all", 1, promiseAll);
    try vm.defineFn(ctor, "race", 1, promiseRace);
    try vm.defineFn(ctor, "allSettled", 1, promiseAllSettled);
    try vm.defineFn(ctor, "any", 1, promiseAny);
    try vm.globals.props.put(vm.heap.alloc, "Promise", .{ .object = ctor });
}

// ==========================================================================
// Timers and the rest of the global object
// ==========================================================================

fn addTimer(vm: *Vm, args: []const Value, repeating: bool) anyerror!Value {
    const f = arg(args, 0);
    if (!f.isCallable() and f != .string) return .{ .number = 0 };
    var delay = try vm.toNumber(arg(args, 1));
    if (std.math.isNan(delay) or delay < 0) delay = 0;
    const extra = if (args.len > 2) args[2..] else &[_]Value{};
    const id = vm.next_timer_id;
    vm.next_timer_id += 1;
    try vm.timers.append(vm.gpa, .{
        .id = id,
        .at = vm.clock + delay,
        .interval = @max(delay, 1),
        .fn_val = f,
        .args = try vm.gpa.dupe(Value, extra),
        .repeating = repeating,
    });
    return .{ .number = @floatFromInt(id) };
}

fn setTimeout(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return addTimer(V(ctx), args, false);
}

fn setInterval(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return addTimer(V(ctx), args, true);
}

fn clearTimer(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const id: u32 = @intFromFloat(@max(0, try vm.toNumber(arg(args, 0))));
    for (vm.timers.items) |*t| {
        if (t.id == id) t.cancelled = true;
    }
    return .undefined;
}

fn queueMicrotask(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    const f = arg(args, 0);
    if (!f.isCallable()) return .undefined;
    const p = try vm.newPromise();
    try vm.resolvePromise(p, .undefined);
    try vm.addReaction(p, f.object, null, try vm.newPromise());
    return .undefined;
}

const uri_unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()";

fn encodeUri(vm: *Vm, args: []const Value, keep: []const u8) anyerror!Value {
    var root = Root.open(vm);
    defer root.close();
    const s = try vm.toString(arg(args, 0));
    try root.add(s);
    var out = std.ArrayListUnmanaged(u8){};
    defer out.deinit(vm.gpa);
    for (s.string.bytes) |c| {
        if (std.mem.indexOfScalar(u8, uri_unreserved, c) != null or std.mem.indexOfScalar(u8, keep, c) != null) {
            try out.append(vm.gpa, c);
        } else {
            var buf: [3]u8 = undefined;
            try out.appendSlice(vm.gpa, try std.fmt.bufPrint(&buf, "%{X:0>2}", .{c}));
        }
    }
    return vm.str(out.items);
}

fn encodeURIComponent(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return encodeUri(V(ctx), args, "");
}

fn encodeURI(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    return encodeUri(V(ctx), args, ";/?:@&=+$,#");
}

fn decodeUri(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    const vm = V(ctx);
    var root = Root.open(vm);
    defer root.close();
    const s = try vm.toString(arg(args, 0));
    try root.add(s);
    var out = std.ArrayListUnmanaged(u8){};
    defer out.deinit(vm.gpa);
    var i: usize = 0;
    const b = s.string.bytes;
    while (i < b.len) {
        if (b[i] == '%' and i + 2 < b.len) {
            const hi = digitValue(b[i + 1]) orelse return vm.throwError("URIError", "URI malformed", .{});
            const lo = digitValue(b[i + 2]) orelse return vm.throwError("URIError", "URI malformed", .{});
            try out.append(vm.gpa, hi * 16 + lo);
            i += 3;
            continue;
        }
        try out.append(vm.gpa, b[i]);
        i += 1;
    }
    return vm.str(out.items);
}

fn globalThisGetter(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    _ = this;
    _ = args;
    return .{ .object = V(ctx).globals };
}

fn noop(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = this;
    _ = args;
    return .undefined;
}

fn returnsFalse(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = this;
    _ = args;
    return .{ .boolean = false };
}

/// What the network layer sends, so a page that sniffs and a server that
/// sniffs reach the same conclusion.
const user_agent = "FeetBrowser/0.1.1 (https://github.com/JuiceyDew/FeetBrowser)";

// -- Storage ---------------------------------------------------------------
//
// `localStorage` keeps its items as ordinary enumerable properties of itself,
// which makes `storage.foo` and `storage.getItem("foo")` the same thing and
// `key(i)` a matter of insertion order -- which is what the specification
// asks for anyway. Nothing is written to disk: a page gets a store that lives
// as long as its interpreter does, and a browser that quietly persisted every
// site's data would owe the user a way to see and clear it.

fn storageGet(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    if (this != .object) return .null;
    const k = try vm.toString(arg(args, 0));
    const p = this.object.props.find(k.string.bytes) orelse return .null;
    return p.value;
}

fn storageSet(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    if (this != .object) return .undefined;
    const k = try vm.toString(arg(args, 0));
    const v = try vm.toString(arg(args, 1));
    try vm.setProp(this, k.string.bytes, v);
    return .undefined;
}

fn storageRemove(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    if (this != .object) return .undefined;
    const k = try vm.toString(arg(args, 0));
    _ = this.object.props.remove(k.string.bytes);
    return .undefined;
}

fn storageClear(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = args;
    if (this != .object) return .undefined;
    for (this.object.props.entries.items) |*p| {
        if (p.enumerable) p.dead = true;
    }
    return .undefined;
}

fn storageKey(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = callee;
    const vm = V(ctx);
    if (this != .object) return .null;
    const want = try vm.toNumber(arg(args, 0));
    if (want < 0) return .null;
    var seen: f64 = 0;
    for (this.object.props.entries.items) |p| {
        if (p.dead or !p.enumerable) continue;
        if (seen == want) return vm.str(p.key);
        seen += 1;
    }
    return .null;
}

fn storageLength(ctx: *anyopaque, callee: *Obj, this: Value, args: []const Value) anyerror!Value {
    _ = ctx;
    _ = callee;
    _ = args;
    if (this != .object) return .{ .number = 0 };
    var n: f64 = 0;
    for (this.object.props.entries.items) |p| {
        if (!p.dead and p.enumerable) n += 1;
    }
    return .{ .number = n };
}

fn newStorage(vm: *Vm) !*Obj {
    const o = try vm.heap.newObj(.plain, vm.object_proto);
    try vm.defineFn(o, "getItem", 1, storageGet);
    try vm.defineFn(o, "setItem", 2, storageSet);
    try vm.defineFn(o, "removeItem", 1, storageRemove);
    try vm.defineFn(o, "clear", 0, storageClear);
    try vm.defineFn(o, "key", 1, storageKey);
    const len = try vm.newNative("length", 0, storageLength);
    try o.props.putProp(vm.heap.alloc, "length", .{
        .key = undefined,
        .getter = len,
        .is_accessor = true,
        .enumerable = false,
    });
    return o;
}

fn installGlobals(vm: *Vm) !void {
    const g = vm.globals;
    const a = vm.heap.alloc;

    const console = try vm.heap.newObj(.plain, vm.object_proto);
    try vm.defineFn(console, "log", 1, consoleLog);
    try vm.defineFn(console, "info", 1, consoleLog);
    try vm.defineFn(console, "warn", 1, consoleLog);
    try vm.defineFn(console, "error", 1, consoleLog);
    try vm.defineFn(console, "debug", 1, consoleLog);
    try vm.defineFn(console, "trace", 1, consoleLog);
    try vm.defineFn(console, "group", 1, consoleLog);
    try vm.defineFn(console, "groupEnd", 0, noop);
    try vm.defineFn(console, "table", 1, consoleLog);
    try vm.defineFn(console, "time", 1, noop);
    try vm.defineFn(console, "timeEnd", 1, noop);
    try vm.defineFn(console, "assert", 2, noop);
    try g.props.put(a, "console", .{ .object = console });

    try g.props.put(a, "NaN", .{ .number = std.math.nan(f64) });
    try g.props.put(a, "Infinity", .{ .number = std.math.inf(f64) });
    try g.props.put(a, "undefined", .undefined);

    const pi = try vm.newNative("parseInt", 2, globalParseInt);
    try g.props.put(a, "parseInt", .{ .object = pi });
    const pf = try vm.newNative("parseFloat", 1, globalParseFloat);
    try g.props.put(a, "parseFloat", .{ .object = pf });
    const inan = try vm.newNative("isNaN", 1, globalIsNaN);
    try g.props.put(a, "isNaN", .{ .object = inan });
    const ifin = try vm.newNative("isFinite", 1, globalIsFinite);
    try g.props.put(a, "isFinite", .{ .object = ifin });

    for ([_]struct { []const u8, u32, val.NativeFn }{
        .{ "setTimeout", 2, setTimeout },
        .{ "setInterval", 2, setInterval },
        .{ "clearTimeout", 1, clearTimer },
        .{ "clearInterval", 1, clearTimer },
        .{ "queueMicrotask", 1, queueMicrotask },
        .{ "requestAnimationFrame", 1, setTimeout },
        .{ "cancelAnimationFrame", 1, clearTimer },
        .{ "encodeURIComponent", 1, encodeURIComponent },
        .{ "decodeURIComponent", 1, decodeUri },
        .{ "encodeURI", 1, encodeURI },
        .{ "decodeURI", 1, decodeUri },
    }) |e| {
        const f = try vm.newNative(e[0], e[1], e[2]);
        try g.props.put(a, e[0], .{ .object = f });
    }

    try g.props.put(a, "localStorage", .{ .object = try newStorage(vm) });
    try g.props.put(a, "sessionStorage", .{ .object = try newStorage(vm) });

    // Enough of `navigator` to get past the feature sniffing at the top of
    // every library. It says what we are rather than impersonating a browser
    // whose quirks we do not have.
    const nav = try vm.heap.newObj(.plain, vm.object_proto);
    try vm.define(nav, "userAgent", try vm.str(user_agent));
    try vm.define(nav, "appVersion", try vm.str(user_agent));
    try vm.define(nav, "appName", try vm.str("FeetBrowser"));
    try vm.define(nav, "appCodeName", try vm.str("Mozilla"));
    try vm.define(nav, "product", try vm.str("Gecko"));
    try vm.define(nav, "platform", try vm.str(""));
    try vm.define(nav, "vendor", try vm.str(""));
    try vm.define(nav, "language", try vm.str("en-US"));
    try vm.define(nav, "languages", .{ .object = try vm.newArray(&.{try vm.str("en-US")}) });
    try vm.define(nav, "onLine", .{ .boolean = true });
    try vm.define(nav, "cookieEnabled", .{ .boolean = false });
    try vm.define(nav, "doNotTrack", .null);
    try vm.define(nav, "maxTouchPoints", .{ .number = 0 });
    try vm.defineFn(nav, "javaEnabled", 0, returnsFalse);
    try g.props.put(a, "navigator", .{ .object = nav });

    // `window`, `globalThis` and `self` are the global object itself, so
    // `window.x = 1` and `x` are the same binding and a handle round-trips.
    try g.props.put(a, "window", .{ .object = g });
    try g.props.put(a, "globalThis", .{ .object = g });
    try g.props.put(a, "self", .{ .object = g });
    _ = globalThisGetter;
}
