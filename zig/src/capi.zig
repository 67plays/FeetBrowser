//! The C ABI the browser loads with ctypes.
//!
//! Everything crosses as a `CValue`: a tag, a double, and a pointer/length
//! pair. Ownership is deliberately lopsided. Strings handed *out* live in the
//! VM's single scratch buffer and are only valid until the next call, so the
//! caller copies them at once; strings handed *in* are borrowed for the
//! duration of the call and copied by us. Anything that is not a primitive
//! comes out as a handle, which roots the value until `js_release`.
//!
//! The embedder registers five callbacks -- get, set, call, construct,
//! release -- and that is the whole of the DOM bridge. A JavaScript object
//! that is really a Python node is a `.host` object holding an integer the
//! Python side hands back to itself.

const std = @import("std");
const vmod = @import("vm.zig");
const val = @import("value.zig");

const Vm = vmod.Vm;
const Value = val.Value;
const CValue = vmod.CValue;

var gpa_state = std.heap.GeneralPurposeAllocator(.{}){};

fn alloc() std.mem.Allocator {
    return gpa_state.allocator();
}

// -- lifecycle -------------------------------------------------------------

export fn js_new() callconv(.c) ?*Vm {
    return Vm.create(alloc()) catch null;
}

export fn js_free(vm: *Vm) callconv(.c) void {
    vm.destroy();
}

export fn js_set_host(
    vm: *Vm,
    ctx: ?*anyopaque,
    get: ?*const fn (?*anyopaque, u64, [*]const u8, u32, *CValue) callconv(.c) void,
    set: ?*const fn (?*anyopaque, u64, [*]const u8, u32, *const CValue) callconv(.c) void,
    call: ?*const fn (?*anyopaque, u64, *const CValue, [*]const CValue, u32, *CValue) callconv(.c) void,
    construct: ?*const fn (?*anyopaque, u64, [*]const CValue, u32, *CValue) callconv(.c) void,
    release: ?*const fn (?*anyopaque, u64) callconv(.c) void,
) callconv(.c) void {
    vm.host = .{
        .ctx = ctx,
        .get = get,
        .set = set,
        .call = call,
        .construct = construct,
        .release = release,
    };
}

// -- helpers ---------------------------------------------------------------

/// Copy a string result into the scratch buffer so that a collection between
/// here and the caller's copy cannot pull it out from under them.
fn stabilize(vm: *Vm, c: CValue) CValue {
    if (c.tag != CValue.string) return c;
    const bytes = @as([*]const u8, @ptrFromInt(c.ptr))[0..c.len];
    vm.out_buf.clearRetainingCapacity();
    vm.out_buf.appendSlice(vm.gpa, bytes) catch return .{ .tag = CValue.undef };
    return .{
        .tag = CValue.string,
        .ptr = @intFromPtr(vm.out_buf.items.ptr),
        .len = @intCast(vm.out_buf.items.len),
    };
}

fn out(vm: *Vm, v: Value, dst: *CValue) void {
    const c = vm.toC(v) catch CValue{ .tag = CValue.undef };
    dst.* = stabilize(vm, c);
}

/// Turn a pending exception into a string the caller can raise. The engine
/// never hands a live error object out; a message is all Python needs.
fn outError(vm: *Vm, dst: *CValue) void {
    const exc = vm.exception;
    vm.exception = .undefined;
    var msg: []const u8 = "error";
    if (exc == .object and exc.object.class == .err) {
        const name = vm.getProp(exc, "name") catch Value.undefined;
        const m = vm.getProp(exc, "message") catch Value.undefined;
        vm.out_buf.clearRetainingCapacity();
        if (name == .string) vm.out_buf.appendSlice(vm.gpa, name.string.bytes) catch {};
        if (m == .string and m.string.bytes.len > 0) {
            vm.out_buf.appendSlice(vm.gpa, ": ") catch {};
            vm.out_buf.appendSlice(vm.gpa, m.string.bytes) catch {};
        }
        // A runtime error records where it happened; a parse error already
        // says so in its message, and one built by a script says nothing.
        const line = vm.getProp(exc, "__line__") catch Value.undefined;
        if (line == .number) {
            var buf: [32]u8 = undefined;
            const s = std.fmt.bufPrint(&buf, " (line {d})", .{@as(u32, @intFromFloat(line.number))}) catch "";
            vm.out_buf.appendSlice(vm.gpa, s) catch {};
        }
        dst.* = .{
            .tag = CValue.throw,
            .ptr = @intFromPtr(vm.out_buf.items.ptr),
            .len = @intCast(vm.out_buf.items.len),
        };
        return;
    }
    const s = vm.toString(exc) catch Value.undefined;
    if (s == .string) msg = s.string.bytes;
    vm.out_buf.clearRetainingCapacity();
    vm.out_buf.appendSlice(vm.gpa, msg) catch {};
    dst.* = .{
        .tag = CValue.throw,
        .ptr = @intFromPtr(vm.out_buf.items.ptr),
        .len = @intCast(vm.out_buf.items.len),
    };
}

fn slice(ptr: [*]const u8, len: u32) []const u8 {
    return ptr[0..len];
}

// -- running ---------------------------------------------------------------

/// Returns 0 on success, 1 if the script threw. `dst` carries the completion
/// value on success and the message on failure.
export fn js_run(
    vm: *Vm,
    src: [*]const u8,
    src_len: u32,
    name: [*]const u8,
    name_len: u32,
    dst: *CValue,
) callconv(.c) i32 {
    const r = vm.evaluate(slice(src, src_len), slice(name, name_len)) catch |e| {
        if (e == error.JsThrow) {
            outError(vm, dst);
        } else {
            dst.* = .{ .tag = CValue.throw };
        }
        vm.sp = 0;
        vm.fp = 0;
        return 1;
    };
    out(vm, r, dst);
    return 0;
}

export fn js_drain(vm: *Vm) callconv(.c) void {
    vm.drainJobs();
}

export fn js_advance(vm: *Vm, ms: f64) callconv(.c) void {
    vm.advance(ms);
}

export fn js_collect(vm: *Vm) callconv(.c) void {
    vm.collect();
}

export fn js_heap_bytes(vm: *Vm) callconv(.c) u64 {
    return vm.heap.bytes;
}

// -- logs ------------------------------------------------------------------

export fn js_log_count(vm: *Vm) callconv(.c) u32 {
    return @intCast(vm.logs.items.len);
}

export fn js_log_at(vm: *Vm, i: u32, len: *u32) callconv(.c) [*]const u8 {
    if (i >= vm.logs.items.len) {
        len.* = 0;
        return "";
    }
    len.* = @intCast(vm.logs.items[i].len);
    return vm.logs.items[i].ptr;
}

export fn js_logs_clear(vm: *Vm) callconv(.c) void {
    for (vm.logs.items) |l| vm.gpa.free(l);
    vm.logs.clearRetainingCapacity();
}

// -- globals ---------------------------------------------------------------

export fn js_global_get(vm: *Vm, name: [*]const u8, len: u32, dst: *CValue) callconv(.c) i32 {
    const v = vm.getProp(.{ .object = vm.globals }, slice(name, len)) catch {
        outError(vm, dst);
        return 1;
    };
    out(vm, v, dst);
    return 0;
}

export fn js_global_set(vm: *Vm, name: [*]const u8, len: u32, src: *const CValue) callconv(.c) i32 {
    const v = vm.fromC(src.*) catch {
        vm.exception = .undefined;
        return 1;
    };
    vm.setProp(.{ .object = vm.globals }, slice(name, len), v) catch {
        vm.exception = .undefined;
        return 1;
    };
    return 0;
}

export fn js_global_del(vm: *Vm, name: [*]const u8, len: u32) callconv(.c) void {
    _ = vm.globals.props.remove(slice(name, len));
}

export fn js_global_has(vm: *Vm, name: [*]const u8, len: u32) callconv(.c) i32 {
    return if (vm.globals.props.find(slice(name, len)) != null) 1 else 0;
}

export fn js_global_count(vm: *Vm) callconv(.c) u32 {
    return @intCast(vm.globals.props.count());
}

export fn js_global_key_at(vm: *Vm, i: u32, len: *u32) callconv(.c) [*]const u8 {
    var seen: u32 = 0;
    for (vm.globals.props.entries.items) |p| {
        if (p.dead) continue;
        if (seen == i) {
            len.* = @intCast(p.key.len);
            return p.key.ptr;
        }
        seen += 1;
    }
    len.* = 0;
    return "";
}

// -- values ----------------------------------------------------------------

export fn js_release(vm: *Vm, handle: u64) callconv(.c) void {
    vm.releaseHandle(@intCast(handle));
}

/// 1 array, 2 plain object, 3 function, 4 promise, 0 anything else. The
/// embedder uses this to decide whether to copy the value into a Python list
/// or dict, the way the previous engine did.
export fn js_class(vm: *Vm, handle: u64) callconv(.c) i32 {
    const v = vm.handleValue(@intCast(handle));
    if (v != .object) return 0;
    return switch (v.object.class) {
        .array => 1,
        .plain => 2,
        .function => 3,
        .promise => 4,
        else => 0,
    };
}

export fn js_get(vm: *Vm, handle: u64, name: [*]const u8, len: u32, dst: *CValue) callconv(.c) i32 {
    const base = vm.handleValue(@intCast(handle));
    const v = vm.getProp(base, slice(name, len)) catch {
        outError(vm, dst);
        return 1;
    };
    out(vm, v, dst);
    return 0;
}

export fn js_set(vm: *Vm, handle: u64, name: [*]const u8, len: u32, src: *const CValue) callconv(.c) i32 {
    const base = vm.handleValue(@intCast(handle));
    const v = vm.fromC(src.*) catch {
        vm.exception = .undefined;
        return 1;
    };
    vm.setProp(base, slice(name, len), v) catch {
        vm.exception = .undefined;
        return 1;
    };
    return 0;
}

export fn js_length(vm: *Vm, handle: u64) callconv(.c) u32 {
    const v = vm.handleValue(@intCast(handle));
    if (v == .object and v.object.class == .array) return @intCast(v.object.data.elements.items.len);
    return 0;
}

export fn js_index(vm: *Vm, handle: u64, i: u32, dst: *CValue) callconv(.c) i32 {
    const v = vm.handleValue(@intCast(handle));
    const e = vm.getIndex(v, .{ .number = @floatFromInt(i) }) catch {
        outError(vm, dst);
        return 1;
    };
    out(vm, e, dst);
    return 0;
}

export fn js_key_count(vm: *Vm, handle: u64) callconv(.c) u32 {
    const v = vm.handleValue(@intCast(handle));
    if (v != .object) return 0;
    var n: u32 = 0;
    for (v.object.props.entries.items) |p| {
        if (!p.dead and p.enumerable) n += 1;
    }
    return n;
}

export fn js_key_at(vm: *Vm, handle: u64, i: u32, len: *u32) callconv(.c) [*]const u8 {
    const v = vm.handleValue(@intCast(handle));
    if (v == .object) {
        var seen: u32 = 0;
        for (v.object.props.entries.items) |p| {
            if (p.dead or !p.enumerable) continue;
            if (seen == i) {
                len.* = @intCast(p.key.len);
                return p.key.ptr;
            }
            seen += 1;
        }
    }
    len.* = 0;
    return "";
}

export fn js_call(
    vm: *Vm,
    handle: u64,
    this: *const CValue,
    argv: [*]const CValue,
    argc: u32,
    dst: *CValue,
) callconv(.c) i32 {
    const f = vm.handleValue(@intCast(handle));
    const recv = vm.fromC(this.*) catch {
        vm.exception = .undefined;
        return 1;
    };
    return callInto(vm, f, recv, argv[0..argc], dst, false);
}

export fn js_construct(
    vm: *Vm,
    handle: u64,
    argv: [*]const CValue,
    argc: u32,
    dst: *CValue,
) callconv(.c) i32 {
    const f = vm.handleValue(@intCast(handle));
    return callInto(vm, f, .undefined, argv[0..argc], dst, true);
}

fn callInto(vm: *Vm, f: Value, this: Value, argv: []const CValue, dst: *CValue, is_new: bool) i32 {
    const mark = vm.temps.items.len;
    defer vm.temps.items.len = mark;
    vm.temps.append(vm.gpa, f) catch return 1;
    vm.temps.append(vm.gpa, this) catch return 1;
    for (argv) |c| {
        const v = vm.fromC(c) catch {
            outError(vm, dst);
            return 1;
        };
        vm.temps.append(vm.gpa, v) catch return 1;
    }
    const args = vm.temps.items[mark + 2 ..];
    const r = (if (is_new) vm.construct(f, args) else vm.callValue(f, this, args)) catch |e| {
        if (e == error.JsThrow) {
            outError(vm, dst);
        } else {
            dst.* = .{ .tag = CValue.throw };
        }
        return 1;
    };
    out(vm, r, dst);
    return 0;
}

/// Build a JavaScript array from values the embedder already holds. There is
/// no single `CValue` shape for a list, so a Python list crosses as its
/// elements and is reassembled here.
export fn js_new_array(vm: *Vm, argv: [*]const CValue, argc: u32, dst: *CValue) callconv(.c) i32 {
    const mark = vm.temps.items.len;
    defer vm.temps.items.len = mark;
    for (argv[0..argc]) |c| {
        const v = vm.fromC(c) catch {
            vm.exception = .undefined;
            return 1;
        };
        vm.temps.append(vm.gpa, v) catch return 1;
    }
    const a = vm.newArray(vm.temps.items[mark..]) catch return 1;
    out(vm, .{ .object = a }, dst);
    return 0;
}

/// An empty plain object; the embedder fills it in with `js_set`.
export fn js_new_object(vm: *Vm, dst: *CValue) callconv(.c) i32 {
    const o = vm.newObject() catch return 1;
    out(vm, .{ .object = o }, dst);
    return 0;
}

export fn js_repr(vm: *Vm, handle: u64, len: *u32) callconv(.c) [*]const u8 {
    const v = vm.handleValue(@intCast(handle));
    const s = vm.toString(v) catch Value.undefined;
    vm.out_buf.clearRetainingCapacity();
    if (s == .string) vm.out_buf.appendSlice(vm.gpa, s.string.bytes) catch {};
    len.* = @intCast(vm.out_buf.items.len);
    return if (vm.out_buf.items.len > 0) vm.out_buf.items.ptr else "";
}

// -- promises the embedder settles ----------------------------------------

export fn js_promise_new(vm: *Vm, dst: *CValue) callconv(.c) i32 {
    const p = vm.newPromise() catch return 1;
    out(vm, .{ .object = p }, dst);
    return 0;
}

export fn js_promise_settle(vm: *Vm, handle: u64, src: *const CValue, ok: i32) callconv(.c) i32 {
    const v = vm.handleValue(@intCast(handle));
    if (v != .object or v.object.class != .promise) return 1;
    const arg = vm.fromC(src.*) catch {
        vm.exception = .undefined;
        return 1;
    };
    if (ok != 0) {
        vm.resolvePromise(v.object, arg) catch {
            vm.exception = .undefined;
            return 1;
        };
    } else {
        vm.rejectPromise(v.object, arg) catch {
            vm.exception = .undefined;
            return 1;
        };
    }
    return 0;
}

/// Wrap an embedder object so the engine hands back the same JavaScript
/// object for it every time -- `window.foo === window.foo`.
export fn js_host_value(vm: *Vm, handle: u64, callable: i32, dst: *CValue) callconv(.c) i32 {
    const v = vm.hostWrap(handle, callable != 0) catch return 1;
    out(vm, v, dst);
    return 0;
}
