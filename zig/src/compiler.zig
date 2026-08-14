//! Lowers the syntax tree to bytecode.
//!
//! Scope resolution happens here and nowhere else: every identifier that is
//! not a global comes out of the compiler as a (depth, slot) pair, so the
//! interpreter never does a name lookup for a local. Bindings all live in heap
//! environments rather than in stack frames. Escape analysis would keep most
//! of them on the stack, and it would also mean that a bug in the analysis
//! shows up as a closure that silently stops seeing updates -- the worst
//! possible failure mode. One allocation per scope that declares something is
//! a price worth paying for a closure being nothing but a pointer.

const std = @import("std");
const ast = @import("ast.zig");
const bc = @import("bytecode.zig");
const val = @import("value.zig");

const Op = bc.Op;
const Value = val.Value;
const Proto = val.Proto;

pub const Error = error{ OutOfMemory, CompileFailed };

pub const Script = struct {
    arena: std.heap.ArenaAllocator,
    root: *Proto,
    message: []const u8 = "",

    pub fn deinit(self: *Script) void {
        self.arena.deinit();
    }
};

const Binding = struct {
    name: []const u8,
    slot: u16,
    kind: ast.DeclKind,
};

const Scope = struct {
    parent: ?*Scope,
    bindings: std.ArrayListUnmanaged(Binding) = .{},
    /// The scope a `var` declaration lands in.
    is_function: bool,
    /// A scope that exists only at compile time (the global object's).
    is_global: bool,
    fn_ctx: *FnCtx,
};

const LoopKind = enum { loop, switch_block, labeled_block };

const Loop = struct {
    kind: LoopKind,
    label: ?[]const u8,
    /// Jumps to patch when the loop's end is known.
    breaks: std.ArrayListUnmanaged(u32) = .{},
    continues: std.ArrayListUnmanaged(u32) = .{},
    /// How many enclosing finalizers a break/continue out of here must run.
    finally_depth: u32,
    /// How many scopes to pop on the way out.
    scope_depth: u32,
    /// Operand-stack entries that outlive a statement boundary at the point
    /// the loop was entered -- a switch discriminant, a for-of iterator.
    persist_depth: u32,
};

const FnCtx = struct {
    w: bc.Writer,
    consts: std.ArrayListUnmanaged(Value) = .{},
    protos: std.ArrayListUnmanaged(*Proto) = .{},
    handlers: std.ArrayListUnmanaged(val.Handler) = .{},
    lines: std.ArrayListUnmanaged(val.LineEntry) = .{},
    loops: std.ArrayListUnmanaged(Loop) = .{},
    /// Finalizer bodies whose code must be replayed on the way out.
    finallys: std.ArrayListUnmanaged(*ast.Node) = .{},
    scope: *Scope,
    /// Number of runtime scopes currently open inside this function.
    open_scopes: u32 = 0,
    /// Operand-stack entries live across statement boundaries right now.
    /// Unwinding to a catch handler resets the stack to `bp + persist_depth`,
    /// so a `try` inside a `switch` does not lose the discriminant.
    persist_depth: u32 = 0,
    is_async: bool = false,
    is_generator: bool = false,
    kind: val.FuncKind = .normal,
    parent: ?*FnCtx = null,
};

pub const Compiler = struct {
    alloc: std.mem.Allocator, // the script arena
    heap: *val.Heap,
    fn_ctx: *FnCtx = undefined,
    message: []const u8 = "",
    source_name: []const u8 = "script",
    depth: u32 = 0,
    /// Borrowed from the parser, and only for the length of the compile:
    /// it lives in the parse arena, which goes away with the tree.
    src_lines: *const ast.LineMap = &empty_lines,

    const empty_lines: ast.LineMap = .{};

    const max_depth = 400;

    pub fn compile(
        gpa: std.mem.Allocator,
        heap: *val.Heap,
        program: *ast.Node,
        source_name: []const u8,
        src_lines: *const ast.LineMap,
    ) Error!*Script {
        const script = try gpa.create(Script);
        script.* = .{ .arena = std.heap.ArenaAllocator.init(gpa), .root = undefined };
        const a = script.arena.allocator();

        var c = Compiler{ .alloc = a, .heap = heap, .source_name = source_name, .src_lines = src_lines };
        var global_scope = Scope{
            .parent = null,
            .is_function = true,
            .is_global = true,
            .fn_ctx = undefined,
        };
        var ctx = FnCtx{ .w = bc.Writer.init(a), .scope = &global_scope };
        global_scope.fn_ctx = &ctx;
        c.fn_ctx = &ctx;

        c.hoistInto(program.program, true) catch |e| return c.fail(script, e);
        c.block(program.program) catch |e| return c.fail(script, e);
        ctx.w.op(.ret_undef) catch |e| return c.fail(script, e);

        script.root = c.finishProto(&ctx, "", 0, 0, true) catch |e| return c.fail(script, e);
        return script;
    }

    fn fail(self: *Compiler, script: *Script, e: anyerror) Error {
        script.message = self.message;
        if (e == error.OutOfMemory) return error.OutOfMemory;
        return error.CompileFailed;
    }

    fn err(self: *Compiler, msg: []const u8) Error {
        if (self.message.len == 0) self.message = msg;
        return error.CompileFailed;
    }

    // -- emitting ----------------------------------------------------------

    fn w(self: *Compiler) *bc.Writer {
        return &self.fn_ctx.w;
    }

    fn emit(self: *Compiler, op: Op) !void {
        try self.w().op(op);
    }

    fn constIndex(self: *Compiler, v: Value) !u32 {
        const list = &self.fn_ctx.consts;
        for (list.items, 0..) |c, i| {
            if (std.meta.activeTag(c) != std.meta.activeTag(v)) continue;
            switch (c) {
                .number => |n| if (n == v.number and !std.math.isNan(n)) return @intCast(i),
                .string => |s| if (std.mem.eql(u8, s.bytes, v.string.bytes)) return @intCast(i),
                else => {},
            }
        }
        try list.append(self.alloc, v);
        return @intCast(list.items.len - 1);
    }

    fn strConst(self: *Compiler, s: []const u8) !u32 {
        const str = try self.heap.newStr(s);
        return self.constIndex(.{ .string = str });
    }

    fn emitStr(self: *Compiler, op: Op, s: []const u8) !void {
        const i = try self.strConst(s);
        try self.emit(op);
        try self.w().u32v(i);
    }

    fn emitNumber(self: *Compiler, n: f64) !void {
        const i = try self.constIndex(.{ .number = n });
        try self.emit(.push_const);
        try self.w().u32v(i);
    }

    // -- scopes ------------------------------------------------------------

    fn pushScope(self: *Compiler, is_function: bool) !*Scope {
        const s = try self.alloc.create(Scope);
        s.* = .{
            .parent = self.fn_ctx.scope,
            .is_function = is_function,
            .is_global = false,
            .fn_ctx = self.fn_ctx,
        };
        self.fn_ctx.scope = s;
        return s;
    }

    fn popScope(self: *Compiler) void {
        self.fn_ctx.scope = self.fn_ctx.scope.parent.?;
    }

    fn declare(self: *Compiler, scope: *Scope, name: []const u8, kind: ast.DeclKind) !u16 {
        for (scope.bindings.items) |b| {
            if (std.mem.eql(u8, b.name, name)) return b.slot;
        }
        const slot: u16 = @intCast(scope.bindings.items.len);
        try scope.bindings.append(self.alloc, .{ .name = name, .slot = slot, .kind = kind });
        return slot;
    }

    const Resolved = struct { depth: u16, slot: u16 };

    fn resolve(self: *Compiler, name: []const u8) ?Resolved {
        var depth: u16 = 0;
        var scope: ?*Scope = self.fn_ctx.scope;
        while (scope) |s| {
            if (s.is_global) return null;
            for (s.bindings.items) |b| {
                if (std.mem.eql(u8, b.name, name)) return .{ .depth = depth, .slot = b.slot };
            }
            depth += 1;
            scope = s.parent;
        }
        return null;
    }

    /// The function-level scope a `var` belongs to.
    fn varScope(self: *Compiler) *Scope {
        var scope: *Scope = self.fn_ctx.scope;
        while (!scope.is_function) scope = scope.parent.?;
        return scope;
    }

    // -- hoisting ----------------------------------------------------------

    /// Walk a statement list without descending into nested functions and
    /// declare every `var` and every function declaration it contains.
    fn hoistInto(self: *Compiler, stmts: []*ast.Node, global: bool) Error!void {
        for (stmts) |s| try self.hoistStmt(s, global);
    }

    fn hoistStmt(self: *Compiler, n: *ast.Node, global: bool) Error!void {
        switch (n.*) {
            .var_decl => |d| {
                if (d.kind != .@"var") return;
                for (d.decls) |dec| try self.hoistPattern(dec.target, global);
            },
            .func_decl => |f| {
                if (global) {
                    // Nothing to do: the global object gets the binding when
                    // the declaration executes.
                } else {
                    _ = try self.declare(self.varScope(), f.name, .@"var");
                }
            },
            .block => |b| try self.hoistInto(b, global),
            .if_stmt => |s| {
                try self.hoistStmt(s.then_body, global);
                if (s.else_body) |e| try self.hoistStmt(e, global);
            },
            .for_stmt => |s| {
                if (s.init) |i| try self.hoistStmt(i, global);
                try self.hoistStmt(s.body, global);
            },
            .for_in => |s| {
                if (s.decl == .@"var") try self.hoistPattern(s.left, global);
                try self.hoistStmt(s.body, global);
            },
            .while_stmt => |s| try self.hoistStmt(s.body, global),
            .do_while => |s| try self.hoistStmt(s.body, global),
            .switch_stmt => |s| for (s.cases) |c| try self.hoistInto(c.body, global),
            .try_stmt => |s| {
                try self.hoistStmt(s.block, global);
                if (s.handler) |h| try self.hoistStmt(h, global);
                if (s.finalizer) |f| try self.hoistStmt(f, global);
            },
            .labeled => |s| try self.hoistStmt(s.body, global),
            else => {},
        }
    }

    fn hoistPattern(self: *Compiler, n: *ast.Node, global: bool) Error!void {
        switch (n.*) {
            .identifier => |name| {
                if (global) return;
                _ = try self.declare(self.varScope(), name, .@"var");
            },
            .array_pattern => |els| for (els) |e| {
                if (e) |x| try self.hoistPattern(x, global);
            },
            .object_pattern => |props| for (props) |p| try self.hoistPattern(p.value, global),
            .assign_pattern => |p| try self.hoistPattern(p.target, global),
            .rest_element => |r| try self.hoistPattern(r, global),
            else => {},
        }
    }

    /// Lexical declarations in a statement list, which need a runtime scope.
    fn lexicalCount(stmts: []*ast.Node) usize {
        var n: usize = 0;
        for (stmts) |s| {
            switch (s.*) {
                .var_decl => |d| if (d.kind == .let or d.kind == .@"const") {
                    n += d.decls.len;
                },
                .class_decl => n += 1,
                .func_decl => n += 1,
                else => {},
            }
        }
        return n;
    }

    fn declareLexical(self: *Compiler, scope: *Scope, stmts: []*ast.Node) Error!void {
        for (stmts) |s| {
            switch (s.*) {
                .var_decl => |d| {
                    if (d.kind != .let and d.kind != .@"const") continue;
                    for (d.decls) |dec| try self.declarePattern(scope, dec.target, d.kind);
                },
                .class_decl => |cl| _ = try self.declare(scope, cl.name, .let),
                .func_decl => |f| _ = try self.declare(scope, f.name, .@"var"),
                else => {},
            }
        }
    }

    fn declarePattern(self: *Compiler, scope: *Scope, n: *ast.Node, kind: ast.DeclKind) Error!void {
        switch (n.*) {
            .identifier => |name| _ = try self.declare(scope, name, kind),
            .array_pattern => |els| for (els) |e| {
                if (e) |x| try self.declarePattern(scope, x, kind);
            },
            .object_pattern => |props| for (props) |p| try self.declarePattern(scope, p.value, kind),
            .assign_pattern => |p| try self.declarePattern(scope, p.target, kind),
            .rest_element => |r| try self.declarePattern(scope, r, kind),
            else => {},
        }
    }

    // -- statements --------------------------------------------------------

    /// Note where in the source the code about to be emitted came from. Runs
    /// of instructions on one line collapse to a single entry, and a
    /// statement on a line already covered adds nothing.
    fn markLine(self: *Compiler, n: *ast.Node) Error!void {
        const line = self.src_lines.get(n) orelse return;
        const ctx = self.fn_ctx;
        const at = ctx.w.here();
        if (ctx.lines.items.len > 0) {
            const last = &ctx.lines.items[ctx.lines.items.len - 1];
            if (last.line == line) return;
            // Nothing was emitted since the last note, so replace it.
            if (last.offset == at) {
                last.line = line;
                return;
            }
        }
        try ctx.lines.append(self.alloc, .{ .offset = at, .line = line });
    }

    fn block(self: *Compiler, stmts: []*ast.Node) Error!void {
        // Function declarations come into existence before anything runs.
        for (stmts) |s| {
            if (s.* == .func_decl) try self.stmt(s);
        }
        for (stmts) |s| {
            if (s.* == .func_decl) continue;
            try self.stmt(s);
        }
    }

    fn scopedBlock(self: *Compiler, stmts: []*ast.Node) Error!void {
        const lex = lexicalCount(stmts);
        if (lex == 0) {
            try self.block(stmts);
            return;
        }
        const scope = try self.pushScope(false);
        try self.declareLexical(scope, stmts);
        try self.emit(.push_scope);
        const at = self.w().here();
        try self.w().u16v(0);
        self.fn_ctx.open_scopes += 1;
        try self.block(stmts);
        self.fn_ctx.open_scopes -= 1;
        try self.emit(.pop_scope);
        std.mem.writeInt(u16, self.w().code.items[at..][0..2], @intCast(scope.bindings.items.len), .little);
        self.popScope();
    }

    fn stmt(self: *Compiler, n: *ast.Node) Error!void {
        self.depth += 1;
        defer self.depth -= 1;
        if (self.depth > max_depth) return self.err("too much nesting");
        try self.markLine(n);
        switch (n.*) {
            .empty_stmt => {},
            .expr_stmt => |e| {
                try self.expr(e);
                try self.emit(.save_completion);
            },
            .block => |b| try self.scopedBlock(b),
            .var_decl => |d| try self.varDecl(d.kind, d.decls),
            .func_decl => |f| {
                try self.functionValue(f);
                try self.storeName(f.name, true);
                try self.emit(.pop);
            },
            .class_decl => |cl| {
                try self.classValue(cl);
                try self.storeName(cl.name, true);
                try self.emit(.pop);
            },
            .if_stmt => |s| try self.ifStmt(s.cond, s.then_body, s.else_body),
            .while_stmt => |s| try self.whileStmt(s.cond, s.body, null),
            .do_while => |s| try self.doWhile(s.body, s.cond, null),
            .for_stmt => |s| try self.forStmt(s.init, s.cond, s.update, s.body, null),
            .for_in => |s| try self.forIn(s.left, s.right, s.body, s.of, s.decl, null),
            .switch_stmt => |s| try self.switchStmt(s.disc, s.cases, null),
            .return_stmt => |r| {
                if (r) |e| try self.expr(e) else try self.emit(.push_undef);
                try self.replayFinallys(0);
                try self.emit(.ret);
            },
            .break_stmt => |label| try self.breakOrContinue(label, true),
            .continue_stmt => |label| try self.breakOrContinue(label, false),
            .throw_stmt => |e| {
                try self.expr(e);
                try self.emit(.throw_op);
            },
            .try_stmt => try self.tryStmt(n),
            .labeled => |s| try self.labeled(s.label, s.body),
            .program => |p| try self.block(p),
            else => {
                try self.expr(n);
                try self.emit(.save_completion);
            },
        }
    }

    fn varDecl(self: *Compiler, kind: ast.DeclKind, decls: []ast.Declarator) Error!void {
        for (decls) |d| {
            if (d.init) |init_expr| {
                try self.namedExpr(init_expr, d.target);
                try self.bindPattern(d.target, kind);
            } else if (kind == .@"var") {
                // `var x;` must create the binding without clobbering a value
                // an earlier assignment already put there.
                if (d.target.* == .identifier) {
                    const name = d.target.identifier;
                    if (self.resolve(name) == null) try self.emitStr(.declare_var, name);
                }
            } else {
                try self.emit(.push_undef);
                try self.bindPattern(d.target, kind);
            }
        }
    }

    /// `var f = function () {}` gives the function the variable's name.
    fn namedExpr(self: *Compiler, e: *ast.Node, target: *ast.Node) Error!void {
        if (target.* == .identifier) {
            switch (e.*) {
                .function => |f| if (f.name.len == 0) {
                    f.name = target.identifier;
                },
                .class_expr => |cl| if (cl.name.len == 0) {
                    cl.name = target.identifier;
                },
                else => {},
            }
        }
        try self.expr(e);
    }

    /// Consumes the value on the stack and binds it to `target`.
    fn bindPattern(self: *Compiler, target: *ast.Node, kind: ast.DeclKind) Error!void {
        switch (target.*) {
            .identifier => |name| {
                if (kind == .none) {
                    try self.storeName(name, false);
                    try self.emit(.pop);
                } else if (self.resolve(name)) |r| {
                    try self.emit(.init_local);
                    try self.w().u16v(r.depth);
                    try self.w().u16v(r.slot);
                } else {
                    try self.emitStr(.set_global, name);
                    try self.emit(.pop);
                }
            },
            .member => try self.assignToMember(target),
            .array_pattern => |els| try self.destructureArray(els, kind),
            .object_pattern => |props| try self.destructureObject(props, kind),
            .assign_pattern => |p| {
                // A default at the top level of a binding: `= undefined` means
                // take the default.
                try self.emit(.dup);
                try self.emit(.push_undef);
                try self.emit(.strict_eq);
                const skip = try self.w().jump(.jump_if_false);
                try self.emit(.pop);
                try self.expr(p.default);
                self.w().patch(skip);
                try self.bindPattern(p.target, kind);
            },
            else => return self.err("cannot assign to this"),
        }
    }

    /// The value to store is already on the stack and is consumed.
    fn assignToMember(self: *Compiler, target: *ast.Node) Error!void {
        const m = target.member;
        try self.expr(m.object); // [value, obj]
        if (m.computed) {
            try self.expr(m.property); // [value, obj, key]
            try self.pick(2); // [value, obj, key, value]
            try self.emit(.set_index); // [value, stored]
        } else {
            try self.pick(1); // [value, obj, value]
            try self.emitStr(.set_prop, try self.propName(m.property));
        }
        try self.emit(.pop);
        try self.emit(.pop);
    }

    fn pick(self: *Compiler, n: u8) Error!void {
        if (n == 0) return self.emit(.dup);
        try self.emit(.pick);
        try self.w().u8v(n);
    }

    fn dropUnder(self: *Compiler, n: u8) Error!void {
        if (n == 0) return;
        try self.emit(.drop_under);
        try self.w().u8v(n);
    }

    fn destructureArray(self: *Compiler, els: []?*ast.Node, kind: ast.DeclKind) Error!void {
        // [iterable] -> materialise into an array we can index.
        try self.emit(.for_of_start);
        for (els, 0..) |maybe, i| {
            const el = maybe orelse {
                try self.emit(.dup);
                try self.emit(.iter_next);
                try self.emit(.pop);
                try self.emit(.pop);
                try self.emit(.pop);
                continue;
            };
            if (el.* == .rest_element) {
                try self.emit(.iter_rest); // [iter, rest]
                try self.bindPattern(el.rest_element, kind);
                _ = i;
                break;
            }
            try self.emit(.dup);
            try self.emit(.iter_next); // [iter, iter, value, done]
            try self.emit(.pop); // drop done
            try self.emit(.swap); // [iter, value, iter]
            try self.emit(.pop); // [iter, value]
            try self.bindPattern(el, kind);
        }
        try self.emit(.pop); // the iterator
    }

    fn destructureObject(self: *Compiler, props: []ast.PatternProp, kind: ast.DeclKind) Error!void {
        for (props) |p| {
            if (p.rest) {
                // Not worth a real implementation: copy every own key that has
                // not already been named.
                try self.emit(.dup);
                try self.bindPattern(p.value, kind);
                continue;
            }
            try self.emit(.dup);
            if (p.computed) {
                try self.expr(p.key);
                try self.emit(.get_index);
            } else {
                const name = switch (p.key.*) {
                    .identifier => |s| s,
                    .string => |s| s,
                    .number => |n| try std.fmt.allocPrint(self.alloc, "{d}", .{n}),
                    else => return self.err("bad destructuring key"),
                };
                try self.emitStr(.get_prop, name);
            }
            try self.bindPattern(p.value, kind);
        }
        try self.emit(.pop);
    }

    /// Store the value on the top of the stack, leaving it there.
    fn storeName(self: *Compiler, name: []const u8, declare_it: bool) Error!void {
        if (self.resolve(name)) |r| {
            if (declare_it) try self.emit(.dup);
            try self.emit(if (declare_it) .init_local else .set_local);
            try self.w().u16v(r.depth);
            try self.w().u16v(r.slot);
        } else {
            try self.emitStr(.set_global, name);
        }
    }

    fn ifStmt(self: *Compiler, cond: *ast.Node, then_body: *ast.Node, else_body: ?*ast.Node) Error!void {
        try self.expr(cond);
        const to_else = try self.w().jump(.jump_if_false);
        try self.stmt(then_body);
        if (else_body) |e| {
            const to_end = try self.w().jump(.jump);
            self.w().patch(to_else);
            try self.stmt(e);
            self.w().patch(to_end);
        } else {
            self.w().patch(to_else);
        }
    }

    fn pushLoop(self: *Compiler, kind: LoopKind, label: ?[]const u8) Error!void {
        try self.fn_ctx.loops.append(self.alloc, .{
            .kind = kind,
            .label = label,
            .finally_depth = @intCast(self.fn_ctx.finallys.items.len),
            .scope_depth = self.fn_ctx.open_scopes,
            .persist_depth = self.fn_ctx.persist_depth,
        });
    }

    fn popLoop(self: *Compiler, break_target: u32, continue_target: ?u32) void {
        const l = &self.fn_ctx.loops.items[self.fn_ctx.loops.items.len - 1];
        for (l.breaks.items) |at| self.w().patchTo(at, break_target);
        if (continue_target) |t| {
            for (l.continues.items) |at| self.w().patchTo(at, t);
        }
        l.breaks.deinit(self.alloc);
        l.continues.deinit(self.alloc);
        self.fn_ctx.loops.items.len -= 1;
    }

    fn whileStmt(self: *Compiler, cond: *ast.Node, body: *ast.Node, label: ?[]const u8) Error!void {
        const top = self.w().here();
        try self.expr(cond);
        const out = try self.w().jump(.jump_if_false);
        try self.pushLoop(.loop, label);
        try self.stmt(body);
        const back = try self.w().jump(.jump);
        self.w().patchTo(back, top);
        self.w().patch(out);
        self.popLoop(self.w().here(), top);
    }

    fn doWhile(self: *Compiler, body: *ast.Node, cond: *ast.Node, label: ?[]const u8) Error!void {
        const top = self.w().here();
        try self.pushLoop(.loop, label);
        try self.stmt(body);
        const cont = self.w().here();
        try self.expr(cond);
        const back = try self.w().jump(.jump_if_true);
        self.w().patchTo(back, top);
        self.popLoop(self.w().here(), cont);
    }

    fn forStmt(
        self: *Compiler,
        init_node: ?*ast.Node,
        cond: ?*ast.Node,
        update_expr: ?*ast.Node,
        body: *ast.Node,
        label: ?[]const u8,
    ) Error!void {
        var opened = false;
        var scope: ?*Scope = null;
        if (init_node) |i| {
            if (i.* == .var_decl and (i.var_decl.kind == .let or i.var_decl.kind == .@"const")) {
                const s = try self.pushScope(false);
                for (i.var_decl.decls) |d| try self.declarePattern(s, d.target, i.var_decl.kind);
                try self.emit(.push_scope);
                try self.w().u16v(@intCast(s.bindings.items.len));
                self.fn_ctx.open_scopes += 1;
                opened = true;
                scope = s;
            }
            try self.stmt(i);
        }
        const top = self.w().here();
        var out: ?u32 = null;
        if (cond) |c| {
            try self.expr(c);
            out = try self.w().jump(.jump_if_false);
        }
        try self.pushLoop(.loop, label);
        try self.stmt(body);
        const cont = self.w().here();
        if (opened) try self.emit(.copy_scope);
        if (update_expr) |u| {
            try self.expr(u);
            try self.emit(.pop);
        }
        const back = try self.w().jump(.jump);
        self.w().patchTo(back, top);
        if (out) |o| self.w().patch(o);
        self.popLoop(self.w().here(), cont);
        if (opened) {
            try self.emit(.pop_scope);
            self.fn_ctx.open_scopes -= 1;
            self.popScope();
        }
    }

    fn forIn(
        self: *Compiler,
        left: *ast.Node,
        right: *ast.Node,
        body: *ast.Node,
        of: bool,
        decl: ast.DeclKind,
        label: ?[]const u8,
    ) Error!void {
        try self.expr(right);
        try self.emit(if (of) .for_of_start else .for_in_start);
        self.fn_ctx.persist_depth += 1;

        var scope: ?*Scope = null;
        if (decl == .let or decl == .@"const") {
            const s = try self.pushScope(false);
            try self.declarePattern(s, left, decl);
            scope = s;
        }

        const top = self.w().here();
        try self.emit(.dup);
        try self.emit(.iter_next); // [iter, iter, value, done]
        const out = try self.w().jump(.jump_if_true);
        try self.emit(.swap); // [iter, value, iter]
        try self.emit(.pop); // [iter, value]

        if (scope) |s| {
            try self.emit(.push_scope);
            try self.w().u16v(@intCast(s.bindings.items.len));
            self.fn_ctx.open_scopes += 1;
        }
        try self.bindPattern(left, decl);
        try self.pushLoop(.loop, label);
        try self.stmt(body);
        const cont = self.w().here();
        if (scope != null) {
            try self.emit(.pop_scope);
            self.fn_ctx.open_scopes -= 1;
        }
        const back = try self.w().jump(.jump);
        self.w().patchTo(back, top);

        self.w().patch(out);
        // The failing iter_next left [iter, iter, value]; drop the extras.
        try self.emit(.pop);
        try self.emit(.pop);
        self.popLoop(self.w().here(), cont);
        try self.emit(.pop); // the iterator
        self.fn_ctx.persist_depth -= 1;
        if (scope != null) self.popScope();
    }

    fn switchStmt(self: *Compiler, disc: *ast.Node, cases: []ast.SwitchCase, label: ?[]const u8) Error!void {
        try self.expr(disc);
        self.fn_ctx.persist_depth += 1;
        try self.pushLoop(.switch_block, label);

        var targets = std.ArrayListUnmanaged(u32){};
        defer targets.deinit(self.alloc);
        var default_jump: ?u32 = null;
        for (cases) |c| {
            if (c.taste) |t| {
                try self.emit(.dup);
                try self.expr(t);
                try self.emit(.strict_eq);
                const hit = try self.w().jump(.jump_if_true);
                try targets.append(self.alloc, hit);
            } else {
                try targets.append(self.alloc, 0); // filled in below
            }
        }
        default_jump = try self.w().jump(.jump);

        var bodies = std.ArrayListUnmanaged(u32){};
        defer bodies.deinit(self.alloc);
        var default_at: ?u32 = null;
        for (cases, 0..) |c, i| {
            const at = self.w().here();
            try bodies.append(self.alloc, at);
            if (c.taste == null) default_at = at;
            _ = i;
            try self.block(c.body);
        }
        const end = self.w().here();
        for (cases, 0..) |c, i| {
            if (c.taste != null) self.w().patchTo(targets.items[i], bodies.items[i]);
        }
        self.w().patchTo(default_jump.?, default_at orelse end);
        self.popLoop(end, null);
        try self.emit(.pop); // the discriminant
        self.fn_ctx.persist_depth -= 1;
    }

    fn labeled(self: *Compiler, label: []const u8, body: *ast.Node) Error!void {
        switch (body.*) {
            .while_stmt => |s| return self.whileStmt(s.cond, s.body, label),
            .do_while => |s| return self.doWhile(s.body, s.cond, label),
            .for_stmt => |s| return self.forStmt(s.init, s.cond, s.update, s.body, label),
            .for_in => |s| return self.forIn(s.left, s.right, s.body, s.of, s.decl, label),
            .switch_stmt => |s| return self.switchStmt(s.disc, s.cases, label),
            else => {
                try self.pushLoop(.labeled_block, label);
                try self.stmt(body);
                self.popLoop(self.w().here(), null);
            },
        }
    }

    fn breakOrContinue(self: *Compiler, label: ?[]const u8, is_break: bool) Error!void {
        const loops = self.fn_ctx.loops.items;
        var i = loops.len;
        while (i > 0) {
            i -= 1;
            const l = &loops[i];
            const matches = if (label) |lb|
                (l.label != null and std.mem.eql(u8, l.label.?, lb))
            else
                (is_break or l.kind == .loop) and l.kind != .labeled_block;
            if (!matches) continue;
            if (!is_break and l.kind != .loop) continue;
            // Run any finalizers we are jumping out of, and close any scopes.
            try self.replayFinallys(l.finally_depth);
            var n = self.fn_ctx.open_scopes;
            while (n > l.scope_depth) : (n -= 1) try self.emit(.pop_scope);
            var d = self.fn_ctx.persist_depth;
            while (d > l.persist_depth) : (d -= 1) try self.emit(.pop);
            const at = try self.w().jump(.jump);
            if (is_break) {
                try l.breaks.append(self.alloc, at);
            } else {
                try l.continues.append(self.alloc, at);
            }
            return;
        }
        return self.err(if (is_break) "illegal break" else "illegal continue");
    }

    /// Emit the bodies of the finalizers between the current depth and `down`,
    /// innermost first. Finally blocks are duplicated at every exit rather
    /// than given a return-address mechanism in the interpreter: the code
    /// growth is bounded by nesting depth, and the alternative puts a second
    /// kind of control flow into the dispatch loop.
    fn replayFinallys(self: *Compiler, down: u32) Error!void {
        var i = self.fn_ctx.finallys.items.len;
        while (i > down) {
            i -= 1;
            const node = self.fn_ctx.finallys.items[i];
            const saved = self.fn_ctx.finallys.items.len;
            self.fn_ctx.finallys.items.len = i;
            try self.stmt(node);
            self.fn_ctx.finallys.items.len = saved;
        }
    }

    fn tryStmt(self: *Compiler, n: *ast.Node) Error!void {
        const t = n.try_stmt;
        const has_finally = t.finalizer != null;

        const try_start = self.w().here();
        if (has_finally) try self.fn_ctx.finallys.append(self.alloc, t.finalizer.?);
        try self.stmt(t.block);
        if (has_finally) self.fn_ctx.finallys.items.len -= 1;
        const try_end = self.w().here();

        var to_end = std.ArrayListUnmanaged(u32){};
        defer to_end.deinit(self.alloc);

        if (t.handler) |h| {
            try to_end.append(self.alloc, try self.w().jump(.jump));
            const catch_pc = self.w().here();
            try self.fn_ctx.handlers.append(self.alloc, .{
                .start = try_start,
                .end = try_end,
                .target = catch_pc,
                .depth = self.fn_ctx.persist_depth,
                .kind = .catch_block,
            });
            // The thrown value arrives on the stack.
            if (has_finally) try self.fn_ctx.finallys.append(self.alloc, t.finalizer.?);
            if (t.param) |p| {
                const scope = try self.pushScope(false);
                try self.declarePattern(scope, p, .let);
                try self.emit(.push_scope);
                try self.w().u16v(@intCast(scope.bindings.items.len));
                self.fn_ctx.open_scopes += 1;
                try self.bindPattern(p, .let);
                try self.stmt(h);
                try self.emit(.pop_scope);
                self.fn_ctx.open_scopes -= 1;
                self.popScope();
            } else {
                try self.emit(.pop);
                try self.stmt(h);
            }
            if (has_finally) self.fn_ctx.finallys.items.len -= 1;
        }

        if (has_finally) {
            const catch_end = self.w().here();
            try to_end.append(self.alloc, try self.w().jump(.jump));
            const fin_pc = self.w().here();
            try self.fn_ctx.handlers.append(self.alloc, .{
                .start = try_start,
                .end = catch_end,
                .target = fin_pc,
                .depth = self.fn_ctx.persist_depth,
                .kind = .finally_block,
            });
            // The exception is on the stack; run the finalizer under it.
            try self.stmt(t.finalizer.?);
            try self.emit(.rethrow);
        }

        for (to_end.items) |at| self.w().patch(at);
        if (has_finally) try self.stmt(t.finalizer.?);
    }

    // -- expressions -------------------------------------------------------

    fn expr(self: *Compiler, n: *ast.Node) Error!void {
        self.depth += 1;
        defer self.depth -= 1;
        if (self.depth > max_depth) return self.err("too much nesting");
        switch (n.*) {
            .number => |v| try self.emitNumber(v),
            .string => |s| try self.emitStr(.push_const, s),
            .boolean => |b| try self.emit(if (b) .push_true else .push_false),
            .null_lit => try self.emit(.push_null),
            .undefined_lit => try self.emit(.push_undef),
            .this_expr => try self.emit(.push_this),
            .identifier => |name| try self.loadName(name),
            .regex => |r| try self.regexLiteral(r.pattern, r.flags),
            .template => |t| try self.template(t),
            .tagged_template => |t| try self.taggedTemplate(t),
            .array_lit => |els| try self.arrayLit(els),
            .object_lit => |props| try self.objectLit(props),
            .function => |f| try self.functionValue(f),
            .class_expr => |cl| try self.classValue(cl),
            .unary => |u| try self.unary(u.op, u.operand),
            .update => |u| try self.update(u.op, u.prefix, u.target),
            .binary => |b| {
                try self.expr(b.left);
                try self.expr(b.right);
                try self.emit(binOp(b.op));
            },
            .logical => |l| try self.logical(l.op, l.left, l.right),
            .assign => |a| try self.assign(a.op, a.target, a.value),
            .conditional => |c| {
                try self.expr(c.cond);
                const to_else = try self.w().jump(.jump_if_false);
                try self.expr(c.then_expr);
                const to_end = try self.w().jump(.jump);
                self.w().patch(to_else);
                try self.expr(c.else_expr);
                self.w().patch(to_end);
            },
            .sequence => |items| {
                for (items, 0..) |e, i| {
                    try self.expr(e);
                    if (i + 1 < items.len) try self.emit(.pop);
                }
            },
            .member => try self.memberLoad(n, false),
            .call => try self.callExpr(n),
            .new_expr => |ne| try self.newExpr(ne.callee, ne.args),
            .await_expr => |e| {
                try self.expr(e);
                try self.emit(.await_op);
            },
            .spread => |e| try self.expr(e),
            .super_expr => try self.emit(.push_this),
            .yield_expr => return self.err("generators are not supported"),
            else => return self.err("unsupported expression"),
        }
    }

    fn loadName(self: *Compiler, name: []const u8) Error!void {
        if (std.mem.eql(u8, name, "undefined")) return self.emit(.push_undef);
        if (std.mem.eql(u8, name, "arguments")) {
            if (self.resolve(name) == null and !self.fn_ctx.scope.is_global) {
                return self.emit(.push_arguments);
            }
        }
        if (self.resolve(name)) |r| {
            try self.emit(.get_local);
            try self.w().u16v(r.depth);
            try self.w().u16v(r.slot);
        } else {
            try self.emitStr(.get_global, name);
        }
    }

    fn regexLiteral(self: *Compiler, pattern: []const u8, flags: []const u8) Error!void {
        try self.emitStr(.get_global, "RegExp");
        try self.emitStr(.push_const, pattern);
        try self.emitStr(.push_const, flags);
        try self.emit(.construct);
        try self.w().u32v(2);
    }

    fn template(self: *Compiler, t: ast.Template) Error!void {
        try self.emitStr(.push_const, t.quasis[0]);
        for (t.exprs, 0..) |e, i| {
            try self.expr(e);
            try self.emit(.add);
            if (i + 1 < t.quasis.len) {
                try self.emitStr(.push_const, t.quasis[i + 1]);
                try self.emit(.add);
            }
        }
    }

    fn taggedTemplate(self: *Compiler, t: anytype) Error!void {
        // tag(strings, ...values) with `strings` a plain array. `raw` is the
        // same array, which is wrong for escapes and right for everything a
        // page actually does with it.
        try self.expr(t.tag);
        try self.emit(.push_undef);
        try self.emit(.new_array);
        try self.w().u32v(0);
        for (t.quasis) |q| {
            try self.emitStr(.push_const, q);
            try self.emit(.array_push);
        }
        try self.emit(.dup);
        try self.emit(.dup);
        try self.emitStr(.set_prop, "raw");
        try self.emit(.pop);
        for (t.exprs) |e| try self.expr(e);
        try self.emit(.call);
        try self.w().u32v(@intCast(1 + t.exprs.len));
    }

    fn arrayLit(self: *Compiler, els: []const ?*ast.Node) Error!void {
        var simple = true;
        for (els) |e| {
            if (e == null or e.?.* == .spread) simple = false;
        }
        if (simple) {
            for (els) |e| try self.expr(e.?);
            try self.emit(.new_array);
            try self.w().u32v(@intCast(els.len));
            return;
        }
        try self.emit(.new_array);
        try self.w().u32v(0);
        for (els) |maybe| {
            if (maybe) |e| {
                if (e.* == .spread) {
                    try self.expr(e.spread);
                    try self.emit(.array_push_spread);
                } else {
                    try self.expr(e);
                    try self.emit(.array_push);
                }
            } else {
                try self.emit(.push_undef);
                try self.emit(.array_push);
            }
        }
    }

    fn objectLit(self: *Compiler, props: []ast.Property) Error!void {
        try self.emit(.new_object);
        for (props) |p| {
            switch (p.kind) {
                .spread => {
                    try self.expr(p.value);
                    try self.emit(.define_spread);
                },
                .get, .set => {
                    const is_get = p.kind == .get;
                    if (p.computed) {
                        try self.expr(p.key);
                        try self.expr(p.value);
                        try self.emit(.define_accessor_computed);
                        try self.w().u8v(if (is_get) 0 else 1);
                    } else {
                        const name = try self.propName(p.key);
                        try self.expr(p.value);
                        try self.emitStr(.define_accessor, name);
                        try self.w().u8v(if (is_get) 0 else 1);
                    }
                },
                .init => {
                    if (p.computed) {
                        try self.expr(p.key);
                        try self.expr(p.value);
                        try self.emit(.define_prop_computed);
                    } else {
                        const name = try self.propName(p.key);
                        if (p.value.* == .function and p.value.function.name.len == 0) {
                            p.value.function.name = name;
                        }
                        try self.expr(p.value);
                        try self.emitStr(.define_prop, name);
                    }
                },
            }
        }
    }

    fn propName(self: *Compiler, key: *ast.Node) Error![]const u8 {
        return switch (key.*) {
            .identifier => |s| s,
            .string => |s| s,
            .number => |n| numToKey(self.alloc, n),
            else => self.err("bad property key"),
        };
    }

    fn numToKey(alloc: std.mem.Allocator, n: f64) Error![]const u8 {
        if (n == @trunc(n) and @abs(n) < 1e21) {
            return std.fmt.allocPrint(alloc, "{d}", .{@as(i64, @intFromFloat(n))});
        }
        return std.fmt.allocPrint(alloc, "{d}", .{n});
    }

    fn unary(self: *Compiler, op: ast.UnaryOp, operand: *ast.Node) Error!void {
        switch (op) {
            .delete_op => {
                if (operand.* == .member) {
                    const m = operand.member;
                    try self.expr(m.object);
                    if (m.computed) {
                        try self.expr(m.property);
                        try self.emit(.del_index);
                    } else {
                        try self.emitStr(.del_prop, try self.propName(m.property));
                    }
                } else if (operand.* == .identifier) {
                    try self.emitStr(.delete_global, operand.identifier);
                } else {
                    try self.expr(operand);
                    try self.emit(.pop);
                    try self.emit(.push_true);
                }
                return;
            },
            .typeof_op => {
                if (operand.* == .identifier and self.resolve(operand.identifier) == null) {
                    try self.emitStr(.typeof_global, operand.identifier);
                    return;
                }
                try self.expr(operand);
                try self.emit(.typeof_op);
                return;
            },
            else => {},
        }
        try self.expr(operand);
        try self.emit(switch (op) {
            .neg => .neg,
            .plus => .unary_plus,
            .not => .not,
            .bitnot => .bit_not,
            .void_op => .void_op,
            else => unreachable,
        });
    }

    fn update(self: *Compiler, op: ast.UpdateOp, prefix: bool, target: *ast.Node) Error!void {
        const step: Op = if (op == .inc) .inc else .dec;
        switch (target.*) {
            .identifier => |name| {
                try self.loadName(name);
                try self.emit(.to_number);
                if (!prefix) try self.emit(.dup);
                try self.emit(step);
                if (self.resolve(name)) |r| {
                    try self.emit(.set_local);
                    try self.w().u16v(r.depth);
                    try self.w().u16v(r.slot);
                } else {
                    try self.emitStr(.set_global, name);
                }
                if (!prefix) try self.emit(.pop);
            },
            .member => {
                const m = target.member;
                try self.expr(m.object);
                if (m.computed) {
                    try self.expr(m.property); // [obj, key]
                    try self.emit(.dup2); // [obj, key, obj, key]
                    try self.emit(.get_index); // [obj, key, raw]
                    try self.emit(.to_number); // [obj, key, old]
                    try self.pick(2); // [obj, key, old, obj]
                    try self.pick(2); // [obj, key, old, obj, key]
                    try self.pick(2); // [obj, key, old, obj, key, old]
                    try self.emit(step); // ... new
                    try self.emit(.set_index); // [obj, key, old, new]
                    if (prefix) {
                        try self.dropUnder(3); // [new]
                    } else {
                        try self.emit(.pop); // [obj, key, old]
                        try self.dropUnder(2); // [old]
                    }
                } else {
                    const name = try self.propName(m.property);
                    try self.emit(.dup); // [obj, obj]
                    try self.emitStr(.get_prop, name); // [obj, raw]
                    try self.emit(.to_number); // [obj, old]
                    try self.pick(1); // [obj, old, obj]
                    try self.pick(1); // [obj, old, obj, old]
                    try self.emit(step); // [obj, old, obj, new]
                    try self.emitStr(.set_prop, name); // [obj, old, new]
                    if (prefix) {
                        try self.dropUnder(2); // [new]
                    } else {
                        try self.emit(.pop); // [obj, old]
                        try self.dropUnder(1); // [old]
                    }
                }
            },
            else => return self.err("bad increment target"),
        }
    }

    fn logical(self: *Compiler, op: ast.LogicalOp, left: *ast.Node, right: *ast.Node) Error!void {
        try self.expr(left);
        const at = switch (op) {
            .logical_and => try self.w().jump(.jump_if_false_keep),
            .logical_or => try self.w().jump(.jump_if_true_keep),
            .nullish => try self.w().jump(.jump_if_not_nullish_keep),
        };
        try self.emit(.pop);
        try self.expr(right);
        self.w().patch(at);
    }

    fn assign(self: *Compiler, op: ast.AssignOp, target: *ast.Node, value: *ast.Node) Error!void {
        if (op == .assign) {
            switch (target.*) {
                .identifier => |name| {
                    try self.namedExpr(value, target);
                    if (self.resolve(name)) |r| {
                        try self.emit(.set_local);
                        try self.w().u16v(r.depth);
                        try self.w().u16v(r.slot);
                    } else {
                        try self.emitStr(.set_global, name);
                    }
                },
                .member => |m| {
                    try self.expr(m.object);
                    if (m.computed) {
                        try self.expr(m.property);
                        try self.expr(value);
                        try self.emit(.set_index);
                    } else {
                        try self.expr(value);
                        try self.emitStr(.set_prop, try self.propName(m.property));
                    }
                },
                .array_pattern, .object_pattern => {
                    try self.expr(value);
                    try self.emit(.dup);
                    try self.bindPattern(target, .none);
                },
                else => return self.err("bad assignment target"),
            }
            return;
        }

        // Logical assignment short-circuits and skips the store entirely.
        switch (op) {
            .logical_and, .logical_or, .nullish => {
                try self.expr(target);
                const at = switch (op) {
                    .logical_and => try self.w().jump(.jump_if_false_keep),
                    .logical_or => try self.w().jump(.jump_if_true_keep),
                    else => try self.w().jump(.jump_if_not_nullish_keep),
                };
                try self.emit(.pop);
                try self.assign(.assign, target, value);
                self.w().patch(at);
                return;
            },
            else => {},
        }

        const bop: Op = switch (op) {
            .add => .add,
            .sub => .sub,
            .mul => .mul,
            .div => .div,
            .mod => .mod,
            .pow => .pow,
            .shl => .shl,
            .shr => .shr,
            .ushr => .ushr,
            .bitand => .bit_and,
            .bitor => .bit_or,
            .bitxor => .bit_xor,
            else => unreachable,
        };
        switch (target.*) {
            .identifier => |name| {
                try self.loadName(name);
                try self.expr(value);
                try self.emit(bop);
                if (self.resolve(name)) |r| {
                    try self.emit(.set_local);
                    try self.w().u16v(r.depth);
                    try self.w().u16v(r.slot);
                } else {
                    try self.emitStr(.set_global, name);
                }
            },
            .member => |m| {
                try self.expr(m.object);
                if (m.computed) {
                    try self.expr(m.property);
                    try self.emit(.dup2);
                    try self.emit(.get_index);
                    try self.expr(value);
                    try self.emit(bop);
                    try self.emit(.set_index);
                } else {
                    const name = try self.propName(m.property);
                    try self.emit(.dup);
                    try self.emitStr(.get_prop, name);
                    try self.expr(value);
                    try self.emit(bop);
                    try self.emitStr(.set_prop, name);
                }
            },
            else => return self.err("bad assignment target"),
        }
    }

    fn memberLoad(self: *Compiler, n: *ast.Node, keep_this: bool) Error!void {
        const m = n.member;
        if (m.object.* == .super_expr) {
            try self.emit(.push_this);
            if (m.computed) {
                try self.expr(m.property);
                try self.emit(.get_super_index);
            } else {
                try self.emitStr(.get_super, try self.propName(m.property));
            }
            if (!keep_this) {
                try self.emit(.swap);
                try self.emit(.pop);
            }
            return;
        }
        try self.expr(m.object);
        var short: ?u32 = null;
        if (m.optional) {
            try self.emit(.dup);
            short = try self.w().jump(.jump_if_nullish);
        }
        if (m.computed) {
            try self.expr(m.property);
            try self.emit(if (keep_this) .get_index_this else .get_index);
        } else {
            try self.emitStr(if (keep_this) .get_prop_this else .get_prop, try self.propName(m.property));
        }
        if (short) |s| {
            const done = try self.w().jump(.jump);
            self.w().patch(s);
            // The nullish base is still on the stack; replace it.
            try self.emit(.pop);
            try self.emit(.push_undef);
            if (keep_this) try self.emit(.push_undef);
            self.w().patch(done);
        }
    }

    fn hasSpread(args: []ast.Arg) bool {
        for (args) |a| {
            if (a.spread) return true;
        }
        return false;
    }

    fn pushArgs(self: *Compiler, args: []ast.Arg) Error!void {
        if (!hasSpread(args)) {
            for (args) |a| try self.expr(a.value);
            return;
        }
        try self.emit(.new_array);
        try self.w().u32v(0);
        for (args) |a| {
            try self.expr(a.value);
            try self.emit(if (a.spread) .array_push_spread else .array_push);
        }
    }

    fn callExpr(self: *Compiler, n: *ast.Node) Error!void {
        const c = n.call;
        const spread = hasSpread(c.args);

        if (c.callee.* == .super_expr) {
            try self.pushArgs(c.args);
            try self.emit(if (spread) .super_call_spread else .super_call);
            if (!spread) try self.w().u32v(@intCast(c.args.len));
            try self.emit(.push_undef);
            return;
        }

        var short: ?u32 = null;
        if (c.callee.* == .member) {
            try self.memberLoad(c.callee, true);
            // [this, func] -> [func, this]
            try self.emit(.swap);
        } else {
            try self.expr(c.callee);
            try self.emit(.push_undef);
        }
        if (c.optional) {
            // [func, this]; test the function.
            try self.emit(.swap);
            try self.emit(.dup);
            short = try self.w().jump(.jump_if_nullish);
            try self.emit(.swap);
        }
        try self.pushArgs(c.args);
        try self.emit(if (spread) .call_spread else .call);
        if (!spread) try self.w().u32v(@intCast(c.args.len));
        if (short) |s| {
            const done = try self.w().jump(.jump);
            self.w().patch(s);
            try self.emit(.pop); // this
            try self.emit(.pop); // func
            try self.emit(.push_undef);
            self.w().patch(done);
        }
    }

    fn newExpr(self: *Compiler, callee: *ast.Node, args: []ast.Arg) Error!void {
        try self.expr(callee);
        const spread = hasSpread(args);
        try self.pushArgs(args);
        try self.emit(if (spread) .construct_spread else .construct);
        if (!spread) try self.w().u32v(@intCast(args.len));
    }

    // -- functions ---------------------------------------------------------

    fn functionValue(self: *Compiler, f: *ast.Function) Error!void {
        const proto = try self.compileFunction(f);
        try self.fn_ctx.protos.append(self.alloc, proto);
        try self.emit(.closure);
        try self.w().u32v(@intCast(self.fn_ctx.protos.items.len - 1));
    }

    fn compileFunction(self: *Compiler, f: *ast.Function) Error!*Proto {
        const parent = self.fn_ctx;
        var scope = Scope{
            .parent = parent.scope,
            .is_function = true,
            .is_global = false,
            .fn_ctx = undefined,
        };
        var ctx = FnCtx{
            .w = bc.Writer.init(self.alloc),
            .scope = &scope,
            .is_async = f.is_async,
            .is_generator = f.is_generator,
            .kind = switch (f.kind) {
                .arrow => .arrow,
                .method => .method,
                .getter => .getter,
                .setter => .setter,
                .constructor => .ctor,
                else => .normal,
            },
            .parent = parent,
        };
        scope.fn_ctx = &ctx;
        self.fn_ctx = &ctx;
        defer self.fn_ctx = parent;

        // Parameters occupy the first slots, in order.
        var simple = true;
        for (f.params) |p| {
            switch (p.*) {
                .identifier => |name| _ = try self.declare(&scope, name, .let),
                else => simple = false,
            }
        }
        if (!simple) {
            // Non-trivial parameters get hidden slots and a prelude.
            scope.bindings.clearRetainingCapacity();
            for (f.params, 0..) |p, i| {
                _ = i;
                try self.declarePattern(&scope, p, .let);
            }
        }
        if (f.written_name and f.name.len > 0 and f.kind == .normal) {
            _ = try self.declare(&scope, f.name, .@"var");
        }

        if (!f.expression_body) try self.hoistInto(f.body.block, false);

        if (!simple) try self.paramPrelude(f.params);
        if (f.written_name and f.name.len > 0 and f.kind == .normal) {
            // A named function expression can call itself through a name its
            // caller never sees, so bind the name to the running function.
            if (self.resolve(f.name)) |r| {
                try self.emit(.push_callee);
                try self.emit(.init_local);
                try self.w().u16v(r.depth);
                try self.w().u16v(r.slot);
            }
        }

        if (f.expression_body) {
            try self.expr(f.body);
            try self.emit(.ret);
        } else {
            try self.block(f.body.block);
            try self.emit(.ret_undef);
        }

        return self.finishProto(&ctx, f.name, @intCast(f.params.len), @intCast(scope.bindings.items.len), simple);
    }

    fn paramPrelude(self: *Compiler, params: []*ast.Node) Error!void {
        for (params, 0..) |p, i| {
            if (p.* == .rest_element) {
                try self.emit(.push_arguments);
                try self.emitStr(.get_prop, "slice");
                try self.emit(.push_arguments);
                try self.emitNumber(@floatFromInt(i));
                try self.emit(.call);
                try self.w().u32v(1);
                try self.bindPattern(p.rest_element, .let);
                continue;
            }
            try self.emit(.push_arguments);
            try self.emitNumber(@floatFromInt(i));
            try self.emit(.get_index);
            try self.bindPattern(p, .let);
        }
    }

    fn finishProto(
        self: *Compiler,
        ctx: *FnCtx,
        name: []const u8,
        n_params: u32,
        n_slots: u32,
        simple: bool,
    ) Error!*Proto {
        const p = try self.alloc.create(Proto);
        p.* = .{
            .name = try self.alloc.dupe(u8, name),
            .code = try self.alloc.dupe(u8, ctx.w.code.items),
            .consts = try self.alloc.dupe(Value, ctx.consts.items),
            .protos = try self.alloc.dupe(*Proto, ctx.protos.items),
            .n_params = n_params,
            .n_slots = n_slots,
            .simple_params = simple,
            .is_async = ctx.is_async,
            .is_generator = ctx.is_generator,
            .kind = ctx.kind,
            .handlers = try self.alloc.dupe(val.Handler, ctx.handlers.items),
            .lines = try self.alloc.dupe(val.LineEntry, ctx.lines.items),
            .slot_names = &.{},
            .source_name = self.source_name,
        };
        return p;
    }

    // -- classes -----------------------------------------------------------

    fn classValue(self: *Compiler, cl: *ast.Class) Error!void {
        if (cl.superclass) |s| try self.expr(s) else try self.emit(.push_undef);

        // Find the constructor, or synthesise one.
        var ctor_fn: ?*ast.Function = null;
        for (cl.members) |m| {
            if (m.kind == .constructor and m.value != null and m.value.?.* == .function) {
                ctor_fn = m.value.?.function;
            }
        }
        const proto = if (ctor_fn) |cf| blk: {
            cf.name = cl.name;
            cf.kind = .constructor;
            break :blk try self.compileFunction(cf);
        } else try self.synthCtor(cl);
        try self.fn_ctx.protos.append(self.alloc, proto);
        try self.emit(.class_def);
        try self.w().u32v(@intCast(self.fn_ctx.protos.items.len - 1));

        for (cl.members) |m| {
            if (m.kind == .constructor) continue;
            if (m.is_field) {
                const name = try self.propName(m.key);
                if (m.value) |v| try self.expr(v) else try self.emit(.push_undef);
                try self.emitStr(.class_field, name);
                try self.w().u8v(if (m.is_static) 1 else 0);
                continue;
            }
            const f = m.value.?;
            if (m.computed) {
                try self.expr(m.key);
                try self.expr(f);
                try self.emit(.class_method_computed);
                try self.w().u8v(if (m.is_static) 1 else 0);
                continue;
            }
            const name = try self.propName(m.key);
            if (f.* == .function and f.function.name.len == 0) f.function.name = name;
            try self.expr(f);
            switch (m.kind) {
                .getter, .setter => {
                    try self.emitStr(.class_accessor, name);
                    try self.w().u8v(if (m.is_static) 1 else 0);
                    try self.w().u8v(if (m.kind == .getter) 0 else 1);
                },
                else => {
                    try self.emitStr(.class_method, name);
                    try self.w().u8v(if (m.is_static) 1 else 0);
                },
            }
        }
    }

    /// `class X extends Y {}` with no constructor forwards its arguments.
    fn synthCtor(self: *Compiler, cl: *ast.Class) Error!*Proto {
        const parent = self.fn_ctx;
        var scope = Scope{ .parent = parent.scope, .is_function = true, .is_global = false, .fn_ctx = undefined };
        var ctx = FnCtx{
            .w = bc.Writer.init(self.alloc),
            .scope = &scope,
            .kind = .ctor,
            .parent = parent,
        };
        scope.fn_ctx = &ctx;
        self.fn_ctx = &ctx;
        defer self.fn_ctx = parent;
        if (cl.superclass != null) {
            try self.emit(.push_arguments);
            try self.emit(.super_call_spread);
        }
        try self.emit(.ret_undef);
        return self.finishProto(&ctx, cl.name, 0, 0, true);
    }
};

fn binOp(op: ast.BinaryOp) Op {
    return switch (op) {
        .add => .add,
        .sub => .sub,
        .mul => .mul,
        .div => .div,
        .mod => .mod,
        .pow => .pow,
        .eq => .eq,
        .neq => .neq,
        .strict_eq => .strict_eq,
        .strict_neq => .strict_neq,
        .lt => .lt,
        .gt => .gt,
        .le => .le,
        .ge => .ge,
        .shl => .shl,
        .shr => .shr,
        .ushr => .ushr,
        .bitand => .bit_and,
        .bitor => .bit_or,
        .bitxor => .bit_xor,
        .instanceof => .instance_of,
        .in_op => .in_op,
    };
}
