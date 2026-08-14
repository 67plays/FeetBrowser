//! The syntax tree the parser builds and the compiler consumes.
//!
//! Every node lives in a parse arena that is thrown away as soon as the
//! function it belongs to has been compiled to bytecode, so nothing here is
//! reference counted and nothing here is collected: the tree is write-once
//! and read-once. Slices point straight into arena memory; string payloads
//! are already unescaped and own their bytes.

const std = @import("std");

pub const Node = union(enum) {
    // -- expressions -------------------------------------------------------
    number: f64,
    string: []const u8,
    /// A regular-expression literal, split into its two halves.
    regex: struct { pattern: []const u8, flags: []const u8 },
    boolean: bool,
    null_lit,
    undefined_lit,
    identifier: []const u8,
    this_expr,
    super_expr,

    array_lit: []const ?*Node, // null entries are elisions: [1,,2]
    object_lit: []Property,
    template: Template,
    /// tag`cooked${expr}` -- the quasis are the literal chunks.
    tagged_template: struct { tag: *Node, quasis: [][]const u8, exprs: []*Node },

    unary: struct { op: UnaryOp, operand: *Node },
    update: struct { op: UpdateOp, prefix: bool, target: *Node },
    binary: struct { op: BinaryOp, left: *Node, right: *Node },
    logical: struct { op: LogicalOp, left: *Node, right: *Node },
    assign: struct { op: AssignOp, target: *Node, value: *Node },
    conditional: struct { cond: *Node, then_expr: *Node, else_expr: *Node },
    /// `a, b, c` -- every operand is evaluated, the last one is the value.
    sequence: []*Node,

    member: struct { object: *Node, property: *Node, computed: bool, optional: bool },
    call: struct { callee: *Node, args: []Arg, optional: bool },
    new_expr: struct { callee: *Node, args: []Arg },

    function: *Function,
    class_expr: *Class,
    /// `...x` in a position where the compiler handles it directly.
    spread: *Node,

    yield_expr: struct { arg: ?*Node, delegate: bool },
    await_expr: *Node,

    // -- patterns (destructuring targets) ----------------------------------
    array_pattern: []?*Node,
    object_pattern: []PatternProp,
    assign_pattern: struct { target: *Node, default: *Node },
    rest_element: *Node,

    // -- statements --------------------------------------------------------
    program: []*Node,
    var_decl: struct { kind: DeclKind, decls: []Declarator },
    expr_stmt: *Node,
    empty_stmt,
    block: []*Node,
    if_stmt: struct { cond: *Node, then_body: *Node, else_body: ?*Node },
    for_stmt: struct { init: ?*Node, cond: ?*Node, update: ?*Node, body: *Node },
    for_in: struct { left: *Node, right: *Node, body: *Node, of: bool, decl: DeclKind },
    while_stmt: struct { cond: *Node, body: *Node },
    do_while: struct { body: *Node, cond: *Node },
    switch_stmt: struct { disc: *Node, cases: []SwitchCase },
    return_stmt: ?*Node,
    break_stmt: ?[]const u8,
    continue_stmt: ?[]const u8,
    throw_stmt: *Node,
    try_stmt: struct {
        block: *Node,
        param: ?*Node, // catch binding, possibly a pattern; null for `catch {}`
        handler: ?*Node,
        finalizer: ?*Node,
    },
    labeled: struct { label: []const u8, body: *Node },
    func_decl: *Function,
    class_decl: *Class,
};

pub const DeclKind = enum { none, @"var", let, @"const" };

pub const Declarator = struct { target: *Node, init: ?*Node };

pub const Arg = struct { value: *Node, spread: bool };

pub const PropKind = enum { init, get, set, spread };

pub const Property = struct {
    key: *Node,
    value: *Node,
    computed: bool,
    kind: PropKind,
    /// `{ method() {} }` -- affects nothing but `super` handling.
    method: bool,
};

pub const PatternProp = struct {
    key: *Node,
    value: *Node, // the binding target, possibly an assign_pattern
    computed: bool,
    rest: bool,
};

pub const Template = struct {
    /// n+1 literal chunks interleaved with n expressions.
    quasis: [][]const u8,
    exprs: []*Node,
};

pub const SwitchCase = struct { taste: ?*Node, body: []*Node };

pub const FuncKind = enum { normal, arrow, method, getter, setter, constructor };

pub const Function = struct {
    name: []const u8, // "" when anonymous
    /// True when the name was written at the function, as in
    /// `function f() {}`. A name the compiler infers from an assignment or a
    /// property key is only ever a label: `{ f: function () {} }` must not
    /// put `f` in the function's own scope, or it shadows an outer `f`.
    written_name: bool = false,
    params: []*Node, // identifiers, patterns, assign_patterns, rest_elements
    body: *Node, // a block, or an expression for a concise arrow body
    expression_body: bool,
    is_async: bool,
    is_generator: bool,
    kind: FuncKind,
};

pub const ClassMember = struct {
    key: *Node,
    value: ?*Node, // a function node for methods, an initializer for fields
    kind: FuncKind,
    is_static: bool,
    computed: bool,
    is_field: bool,
};

pub const Class = struct {
    name: []const u8,
    superclass: ?*Node,
    members: []ClassMember,
};

pub const UnaryOp = enum { neg, plus, not, bitnot, typeof_op, void_op, delete_op };

pub const UpdateOp = enum { inc, dec };

pub const BinaryOp = enum {
    add, sub, mul, div, mod, pow,
    eq, neq, strict_eq, strict_neq,
    lt, gt, le, ge,
    shl, shr, ushr,
    bitand, bitor, bitxor,
    instanceof, in_op,
};

pub const LogicalOp = enum { logical_and, logical_or, nullish };

pub const AssignOp = enum {
    assign,
    add, sub, mul, div, mod, pow,
    shl, shr, ushr,
    bitand, bitor, bitxor,
    logical_and, logical_or, nullish,
};

/// Source line for statement nodes, kept beside the tree rather than inside
/// it: a line number on every node would cost a word on every expression to
/// answer a question only statements are ever asked.
pub const LineMap = std.AutoHashMapUnmanaged(*const Node, u32);

/// Where a parse gave up, for the message the browser shows in its log.
pub const ParseError = struct {
    message: []const u8,
    line: u32,
    column: u32,
};
