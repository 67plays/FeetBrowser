//! The instruction set.
//!
//! Stack machine, not a register machine. V8's Ignition uses an accumulator
//! plus a register file because a register encoding is smaller and because
//! every later tier reads the bytecode as its input; we have no later tier,
//! and the property that actually earns its keep here is that the operand
//! stack *is* the root set. At an instruction boundary nothing live is hiding
//! in a Zig local, so the collector can be precise without any shadow-stack
//! bookkeeping in the interpreter. The same property makes suspension cheap:
//! `await` just copies the frame's slice of the stack somewhere and leaves.
//!
//! Encoding is one opcode byte followed by fixed-width little-endian operands.
//! Compact encodings would save memory we are not short of and cost decoding
//! we would notice.

const std = @import("std");

pub const Op = enum(u8) {
    // -- constants and stack ----------------------------------------------
    push_const, // u32 const index
    push_undef,
    push_null,
    push_true,
    push_false,
    pop,
    dup,
    dup2, // duplicate the top two, in order
    swap,
    /// Copy the value `n` slots below the top to the top. n:u8 (0 is `dup`).
    pick,
    /// Remove `n` values from just below the top. n:u8
    drop_under,

    // -- variables ---------------------------------------------------------
    get_local, // u16 depth, u16 slot
    set_local, // u16 depth, u16 slot -- leaves the value
    init_local, // u16 depth, u16 slot -- consumes, marks initialised
    get_global, // u32 name const
    set_global, // u32 name const -- leaves the value
    /// `typeof x` where x may not exist: undefined instead of a throw.
    typeof_global, // u32 name const
    delete_global, // u32 name const

    // -- properties --------------------------------------------------------
    get_prop, // u32 name const   [obj] -> [val]
    set_prop, // u32 name const   [obj, val] -> [val]
    get_index, //                 [obj, key] -> [val]
    set_index, //                 [obj, key, val] -> [val]
    del_prop, // u32 name const   [obj] -> [bool]
    del_index, //                 [obj, key] -> [bool]
    /// Property read that keeps the object underneath, for method calls.
    get_prop_this, // u32 name const  [obj] -> [obj, val]
    get_index_this, //                [obj, key] -> [obj, val]
    /// `super.x` and `super[x]`: look up on the home object's prototype but
    /// call with the current `this`.
    get_super, // u32 name const
    get_super_index,

    // -- operators ---------------------------------------------------------
    add,
    sub,
    mul,
    div,
    mod,
    pow,
    neg,
    unary_plus,
    not,
    bit_not,
    shl,
    shr,
    ushr,
    bit_and,
    bit_or,
    bit_xor,
    eq,
    neq,
    strict_eq,
    strict_neq,
    lt,
    gt,
    le,
    ge,
    instance_of,
    in_op,
    typeof_op,
    void_op,
    to_number, // for ++/-- on a member expression
    inc,
    dec,

    // -- control -----------------------------------------------------------
    jump, // i32 relative to the byte after the operand
    jump_if_false, // i32, pops
    jump_if_true, // i32, pops
    /// `&&` and `||`: jump keeping the tested value, else pop and continue.
    jump_if_false_keep, // i32
    jump_if_true_keep, // i32
    /// `??` and `?.`
    jump_if_not_nullish_keep, // i32
    jump_if_nullish, // i32, pops -- optional chaining short circuit

    // -- calls -------------------------------------------------------------
    /// [func, this, arg0..argN-1] -> [result]. u32 argc
    call,
    /// [func, this, argArray] -> [result]
    call_spread,
    /// [ctor, arg0..argN-1] -> [instance]. u32 argc
    construct,
    construct_spread,
    /// `super(...)`: [arg0..argN-1] -> []
    super_call, // u32 argc
    super_call_spread,
    ret,
    /// A function body that fell off the end.
    ret_undef,

    // -- object and array construction -------------------------------------
    new_object,
    new_array, // u32 count of stack entries to take
    /// [array, value] -> [array]
    array_push,
    /// [array, iterable] -> [array]
    array_push_spread,
    /// [obj, value] -> [obj], name from a const. u32 name const
    define_prop,
    /// [obj, key, value] -> [obj]
    define_prop_computed,
    /// [obj, source] -> [obj] -- object spread
    define_spread,
    /// [obj, fn] -> [obj]. u32 name const, then u8 0=get 1=set
    define_accessor,
    define_accessor_computed,

    // -- functions and classes ---------------------------------------------
    /// u32 proto index in the enclosing proto's `protos`
    closure,
    /// u32 proto index -- a class constructor; [superclass|undefined] on stack
    class_def,
    /// [class, fn] -> [class]; u32 name const, u8 static
    class_method,
    class_method_computed,
    class_accessor, // u32 name const, u8 static, u8 kind
    class_field, // u32 name const, u8 static -- [class, initFn] -> [class]

    // -- scopes ------------------------------------------------------------
    push_scope, // u16 slot count
    pop_scope,
    /// Replace the top scope with a fresh copy of it, for per-iteration `let`.
    copy_scope,

    // -- iteration ---------------------------------------------------------
    /// [obj] -> [keys array, 0]
    for_in_start,
    /// [iterable] -> [iterator]
    for_of_start,
    /// [iterator] -> [iterator, value, done]
    iter_next,
    /// Everything the iterator has not yielded yet, as a fresh array.
    iter_rest,

    // -- exceptions --------------------------------------------------------
    throw_op,
    /// Rethrow the value on the top of the stack from a finally handler.
    rethrow,

    // -- misc --------------------------------------------------------------
    push_this,
    /// The function currently running, so a named function expression can
    /// call itself through a name its caller cannot see.
    push_callee,
    /// The `arguments` object of the current call.
    push_arguments,
    /// `await` -- suspends the frame.
    await_op,
    /// A statement's value, remembered so `run()` can report it.
    save_completion,
    /// Bring a hoisted `var` into existence in the function scope.
    declare_var, // u32 name const
    /// Assert the operand is an object, for `for (... of ...)` errors.
    nop,
};

pub const Writer = struct {
    code: std.ArrayListUnmanaged(u8) = .{},
    alloc: std.mem.Allocator,

    pub fn init(alloc: std.mem.Allocator) Writer {
        return .{ .alloc = alloc };
    }

    pub fn op(self: *Writer, o: Op) !void {
        try self.code.append(self.alloc, @intFromEnum(o));
    }

    pub fn u8v(self: *Writer, v: u8) !void {
        try self.code.append(self.alloc, v);
    }

    pub fn u16v(self: *Writer, v: u16) !void {
        try self.code.appendSlice(self.alloc, std.mem.asBytes(&v));
    }

    pub fn u32v(self: *Writer, v: u32) !void {
        try self.code.appendSlice(self.alloc, std.mem.asBytes(&v));
    }

    pub fn i32v(self: *Writer, v: i32) !void {
        try self.code.appendSlice(self.alloc, std.mem.asBytes(&v));
    }

    pub fn here(self: *const Writer) u32 {
        return @intCast(self.code.items.len);
    }

    /// Emit a jump with a placeholder target; returns the operand offset.
    pub fn jump(self: *Writer, o: Op) !u32 {
        try self.op(o);
        const at = self.here();
        try self.i32v(0);
        return at;
    }

    pub fn patch(self: *Writer, at: u32) void {
        const target: i32 = @intCast(self.code.items.len);
        const base: i32 = @intCast(at + 4);
        const rel = target - base;
        std.mem.writeInt(i32, self.code.items[at..][0..4], rel, .little);
    }

    pub fn patchTo(self: *Writer, at: u32, target: u32) void {
        const t: i32 = @intCast(target);
        const base: i32 = @intCast(at + 4);
        std.mem.writeInt(i32, self.code.items[at..][0..4], t - base, .little);
    }
};

pub inline fn readU8(code: []const u8, pc: usize) u8 {
    return code[pc];
}

pub inline fn readU16(code: []const u8, pc: usize) u16 {
    return std.mem.readInt(u16, code[pc..][0..2], .little);
}

pub inline fn readU32(code: []const u8, pc: usize) u32 {
    return std.mem.readInt(u32, code[pc..][0..4], .little);
}

pub inline fn readI32(code: []const u8, pc: usize) i32 {
    return std.mem.readInt(i32, code[pc..][0..4], .little);
}
