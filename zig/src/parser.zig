//! Recursive-descent JavaScript parser producing the tree in `ast.zig`.
//!
//! Everything is allocated from the arena handed to `init`; nothing is ever
//! freed individually.  The parser is total: no panics, no `unreachable`, no
//! asserts.  Malformed input yields `error.ParseFailed` with `self.err` set.
//!
//! Two constructs need lookahead rather than a cover grammar:
//!
//!   * arrow functions -- at a `(` we snapshot the lexer, scan to the matching
//!     `)` counting nesting, and check for `=>`.  If it is an arrow we rewind
//!     and parse the contents as a binding list; otherwise as an expression.
//!   * template substitutions -- the lexer already located each `${}` hole, so
//!     we rewind into the hole, parse an expression, and restore.

const std = @import("std");
const ast = @import("ast.zig");
const lex = @import("lexer.zig");

const T = lex.T;
const K = lex.K;
const Token = lex.Token;

pub const Error = error{ ParseFailed, OutOfMemory };

/// Recursion guard.  Deeply nested input is a syntax error, not a crash.
pub const MAX_DEPTH: u32 = 500;

const KeyC = struct { key: *ast.Node, computed: bool };

pub const Parser = struct {
    arena: std.mem.Allocator,
    source: []const u8,
    lx: lex.Lexer = undefined,
    cur: Token = .{},
    err: ?ast.ParseError = null,
    /// Statement node -> source line, for runtime error messages.
    lines: ast.LineMap = .{},

    depth: u32 = 0,
    no_in: bool = false,
    in_gen: bool = false,
    in_async: bool = false,
    func_depth: u32 = 0,

    pub fn init(arena: std.mem.Allocator, source: []const u8) Parser {
        return .{ .arena = arena, .source = source };
    }

    // -- plumbing ----------------------------------------------------------

    fn node(self: *Parser, v: ast.Node) Error!*ast.Node {
        const n = try self.arena.create(ast.Node);
        n.* = v;
        return n;
    }

    fn fail(self: *Parser, comptime fmt: []const u8, args: anytype) Error {
        if (self.err == null) {
            const msg = std.fmt.allocPrint(self.arena, fmt, args) catch "syntax error";
            self.err = .{ .message = msg, .line = self.cur.line, .column = self.cur.col };
        }
        return error.ParseFailed;
    }

    fn tokenText(self: *Parser) []const u8 {
        if (self.cur.type == .eof) return "end of input";
        if (self.cur.start >= self.cur.end or self.cur.end > self.source.len) return "token";
        return self.source[self.cur.start..self.cur.end];
    }

    fn advance(self: *Parser) Error!void {
        self.cur = self.lx.next();
        if (self.cur.type == .invalid) {
            self.err = .{ .message = self.cur.str, .line = self.cur.line, .column = self.cur.col };
            return error.ParseFailed;
        }
    }

    fn peek(self: *Parser) Token {
        var lx2 = self.lx;
        return lx2.next();
    }

    fn is(self: *const Parser, t: T) bool {
        return self.cur.type == t;
    }

    fn isKw(self: *const Parser, k: K) bool {
        return self.cur.type == .ident and self.cur.kw == k;
    }

    fn eat(self: *Parser, t: T) Error!bool {
        if (self.cur.type == t) {
            try self.advance();
            return true;
        }
        return false;
    }

    fn eatKw(self: *Parser, k: K) Error!bool {
        if (self.isKw(k)) {
            try self.advance();
            return true;
        }
        return false;
    }

    fn expect(self: *Parser, t: T, what: []const u8) Error!void {
        if (self.cur.type != t) return self.fail("expected '{s}' but found '{s}'", .{ what, self.tokenText() });
        try self.advance();
    }

    fn enter(self: *Parser) Error!void {
        self.depth += 1;
        if (self.depth > MAX_DEPTH) return self.fail("expression nested too deeply", .{});
    }

    fn leave(self: *Parser) void {
        if (self.depth > 0) self.depth -= 1;
    }

    /// Automatic semicolon insertion.
    fn semicolon(self: *Parser) Error!void {
        if (self.is(.semi)) {
            try self.advance();
            return;
        }
        if (self.is(.rbrace) or self.is(.eof) or self.cur.nl_before) return;
        return self.fail("unexpected token '{s}'", .{self.tokenText()});
    }

    // -- entry point -------------------------------------------------------

    pub fn parseProgram(self: *Parser) Error!*ast.Node {
        const tl = try self.arena.create(std.ArrayList(lex.TemplateData));
        tl.* = std.ArrayList(lex.TemplateData).init(self.arena);
        self.lx = lex.Lexer.init(self.arena, self.source, tl);
        try self.advance();

        var body = std.ArrayList(*ast.Node).init(self.arena);
        while (!self.is(.eof)) {
            try body.append(try self.parseStatement());
        }
        return self.node(.{ .program = try body.toOwnedSlice() });
    }

    // -- statements --------------------------------------------------------

    /// Every statement remembers the line it started on. That is granular
    /// enough for "which line threw" and costs one map entry per statement.
    fn parseStatement(self: *Parser) Error!*ast.Node {
        const ln = self.cur.line;
        const n = try self.parseStatementInner();
        try self.lines.put(self.arena, n, ln);
        return n;
    }

    fn parseStatementInner(self: *Parser) Error!*ast.Node {
        try self.enter();
        defer self.leave();

        switch (self.cur.type) {
            .semi => {
                try self.advance();
                return self.node(.empty_stmt);
            },
            .lbrace => return self.parseBlock(),
            .ident => switch (self.cur.kw) {
                .kvar, .kconst => return self.parseVarStatement(),
                .klet => {
                    if (self.letStartsDecl()) return self.parseVarStatement();
                },
                .kfunction => return self.parseFunctionDecl(false),
                .kasync => {
                    const p = self.peek();
                    if (p.type == .ident and p.kw == .kfunction and !p.nl_before) {
                        try self.advance();
                        return self.parseFunctionDecl(true);
                    }
                },
                .kclass => return self.parseClassDecl(),
                .kif => return self.parseIf(),
                .kfor => return self.parseFor(),
                .kwhile => return self.parseWhile(),
                .kdo => return self.parseDoWhile(),
                .kswitch => return self.parseSwitch(),
                .kreturn => return self.parseReturn(),
                .kbreak, .kcontinue => return self.parseBreakContinue(),
                .kthrow => return self.parseThrow(),
                .ktry => return self.parseTry(),
                .kdebugger => {
                    try self.advance();
                    try self.semicolon();
                    return self.node(.empty_stmt);
                },
                .kwith => return self.fail("'with' statements are not supported", .{}),
                else => {
                    // A module says so on its first line, and saying "ES
                    // modules are not supported" beats a syntax error about
                    // whatever token happened to follow `export`.
                    if (std.mem.eql(u8, self.cur.str, "export")) {
                        return self.fail("ES modules are not supported", .{});
                    }
                    if (std.mem.eql(u8, self.cur.str, "import")) {
                        const p = self.peek();
                        // `import(...)` and `import.meta` are expressions and
                        // belong to the expression parser, not here.
                        if (p.type != .lparen and p.type != .dot) {
                            return self.fail("ES modules are not supported", .{});
                        }
                    }
                },
            },
            else => {},
        }

        // labelled statement?
        if (self.cur.type == .ident and !lex.isReserved(self.cur.kw)) {
            const p = self.peek();
            if (p.type == .colon) {
                const label = self.cur.str;
                try self.advance();
                try self.advance();
                const body = try self.parseStatement();
                return self.node(.{ .labeled = .{ .label = label, .body = body } });
            }
        }

        const e = try self.parseExpression();
        try self.semicolon();
        return self.node(.{ .expr_stmt = e });
    }

    /// `let` is only a declaration when a binding target follows.
    fn letStartsDecl(self: *Parser) bool {
        const p = self.peek();
        return switch (p.type) {
            .lbracket, .lbrace => true,
            .ident => !lex.isReserved(p.kw),
            else => false,
        };
    }

    fn parseBlock(self: *Parser) Error!*ast.Node {
        try self.expect(.lbrace, "{");
        var body = std.ArrayList(*ast.Node).init(self.arena);
        while (!self.is(.rbrace)) {
            if (self.is(.eof)) return self.fail("unexpected end of input, expected '}}'", .{});
            try body.append(try self.parseStatement());
        }
        try self.advance(); // }
        return self.node(.{ .block = try body.toOwnedSlice() });
    }

    fn declKind(k: K) ast.DeclKind {
        return switch (k) {
            .kvar => .@"var",
            .kconst => .@"const",
            else => .let,
        };
    }

    fn parseVarStatement(self: *Parser) Error!*ast.Node {
        const kind = declKind(self.cur.kw);
        try self.advance();
        const n = try self.parseDeclaratorList(kind);
        try self.semicolon();
        return n;
    }

    fn parseDeclaratorList(self: *Parser, kind: ast.DeclKind) Error!*ast.Node {
        var decls = std.ArrayList(ast.Declarator).init(self.arena);
        while (true) {
            const target = try self.parseBindingTarget();
            var init_expr: ?*ast.Node = null;
            if (try self.eat(.assign)) init_expr = try self.parseAssign();
            try decls.append(.{ .target = target, .init = init_expr });
            if (!try self.eat(.comma)) break;
        }
        return self.node(.{ .var_decl = .{ .kind = kind, .decls = try decls.toOwnedSlice() } });
    }

    fn parseIf(self: *Parser) Error!*ast.Node {
        try self.advance();
        try self.expect(.lparen, "(");
        const cond = try self.parseExpressionReset();
        try self.expect(.rparen, ")");
        const then_body = try self.parseStatement();
        var else_body: ?*ast.Node = null;
        if (self.isKw(.kelse)) {
            try self.advance();
            else_body = try self.parseStatement();
        }
        return self.node(.{ .if_stmt = .{ .cond = cond, .then_body = then_body, .else_body = else_body } });
    }

    fn parseWhile(self: *Parser) Error!*ast.Node {
        try self.advance();
        try self.expect(.lparen, "(");
        const cond = try self.parseExpressionReset();
        try self.expect(.rparen, ")");
        const body = try self.parseStatement();
        return self.node(.{ .while_stmt = .{ .cond = cond, .body = body } });
    }

    fn parseDoWhile(self: *Parser) Error!*ast.Node {
        try self.advance();
        const body = try self.parseStatement();
        if (!self.isKw(.kwhile)) return self.fail("expected 'while' after do-block", .{});
        try self.advance();
        try self.expect(.lparen, "(");
        const cond = try self.parseExpressionReset();
        try self.expect(.rparen, ")");
        _ = try self.eat(.semi);
        return self.node(.{ .do_while = .{ .body = body, .cond = cond } });
    }

    fn parseFor(self: *Parser) Error!*ast.Node {
        try self.advance(); // for
        _ = try self.eatKw(.kawait);
        try self.expect(.lparen, "(");

        var init_node: ?*ast.Node = null;

        if (!self.is(.semi)) {
            const is_decl = self.isKw(.kvar) or self.isKw(.kconst) or (self.isKw(.klet) and self.letStartsDecl());
            if (is_decl) {
                const kind = declKind(self.cur.kw);
                try self.advance();
                const first = try self.parseBindingTarget();
                if (self.isKw(.kin) or self.isKw(.kof)) {
                    const of = self.isKw(.kof);
                    try self.advance();
                    const right = if (of) try self.parseAssignReset() else try self.parseExpressionReset();
                    try self.expect(.rparen, ")");
                    const body = try self.parseStatement();
                    return self.node(.{ .for_in = .{
                        .left = first,
                        .right = right,
                        .body = body,
                        .of = of,
                        .decl = kind,
                    } });
                }
                var decls = std.ArrayList(ast.Declarator).init(self.arena);
                var init0: ?*ast.Node = null;
                if (try self.eat(.assign)) init0 = try self.parseAssignNoIn();
                try decls.append(.{ .target = first, .init = init0 });
                while (try self.eat(.comma)) {
                    const t2 = try self.parseBindingTarget();
                    var init2: ?*ast.Node = null;
                    if (try self.eat(.assign)) init2 = try self.parseAssignNoIn();
                    try decls.append(.{ .target = t2, .init = init2 });
                }
                init_node = try self.node(.{ .var_decl = .{ .kind = kind, .decls = try decls.toOwnedSlice() } });
            } else {
                const saved = self.no_in;
                self.no_in = true;
                const e = self.parseExpression() catch |err| {
                    self.no_in = saved;
                    return err;
                };
                self.no_in = saved;
                if (self.isKw(.kin) or self.isKw(.kof)) {
                    const of = self.isKw(.kof);
                    try self.advance();
                    const left = try self.toPattern(e);
                    const right = if (of) try self.parseAssignReset() else try self.parseExpressionReset();
                    try self.expect(.rparen, ")");
                    const body = try self.parseStatement();
                    return self.node(.{ .for_in = .{
                        .left = left,
                        .right = right,
                        .body = body,
                        .of = of,
                        .decl = .none,
                    } });
                }
                init_node = try self.node(.{ .expr_stmt = e });
            }
        }

        try self.expect(.semi, ";");
        var cond: ?*ast.Node = null;
        if (!self.is(.semi)) cond = try self.parseExpressionReset();
        try self.expect(.semi, ";");
        var update: ?*ast.Node = null;
        if (!self.is(.rparen)) update = try self.parseExpressionReset();
        try self.expect(.rparen, ")");
        const body = try self.parseStatement();
        return self.node(.{ .for_stmt = .{ .init = init_node, .cond = cond, .update = update, .body = body } });
    }

    fn parseSwitch(self: *Parser) Error!*ast.Node {
        try self.advance();
        try self.expect(.lparen, "(");
        const disc = try self.parseExpressionReset();
        try self.expect(.rparen, ")");
        try self.expect(.lbrace, "{");
        var cases = std.ArrayList(ast.SwitchCase).init(self.arena);
        while (!self.is(.rbrace)) {
            if (self.is(.eof)) return self.fail("unterminated switch statement", .{});
            var taste: ?*ast.Node = null;
            if (self.isKw(.kcase)) {
                try self.advance();
                taste = try self.parseExpressionReset();
            } else if (self.isKw(.kdefault)) {
                try self.advance();
            } else {
                return self.fail("expected 'case' or 'default'", .{});
            }
            try self.expect(.colon, ":");
            var body = std.ArrayList(*ast.Node).init(self.arena);
            while (!self.is(.rbrace) and !self.isKw(.kcase) and !self.isKw(.kdefault)) {
                if (self.is(.eof)) return self.fail("unterminated switch statement", .{});
                try body.append(try self.parseStatement());
            }
            try cases.append(.{ .taste = taste, .body = try body.toOwnedSlice() });
        }
        try self.advance(); // }
        return self.node(.{ .switch_stmt = .{ .disc = disc, .cases = try cases.toOwnedSlice() } });
    }

    fn parseReturn(self: *Parser) Error!*ast.Node {
        try self.advance();
        var arg: ?*ast.Node = null;
        if (!self.is(.semi) and !self.is(.rbrace) and !self.is(.eof) and !self.cur.nl_before) {
            arg = try self.parseExpression();
        }
        try self.semicolon();
        return self.node(.{ .return_stmt = arg });
    }

    fn parseBreakContinue(self: *Parser) Error!*ast.Node {
        const is_break = self.cur.kw == .kbreak;
        try self.advance();
        var label: ?[]const u8 = null;
        if (self.cur.type == .ident and !lex.isReserved(self.cur.kw) and !self.cur.nl_before) {
            label = self.cur.str;
            try self.advance();
        }
        try self.semicolon();
        if (is_break) return self.node(.{ .break_stmt = label });
        return self.node(.{ .continue_stmt = label });
    }

    fn parseThrow(self: *Parser) Error!*ast.Node {
        try self.advance();
        if (self.cur.nl_before) return self.fail("newline not allowed after 'throw'", .{});
        const arg = try self.parseExpression();
        try self.semicolon();
        return self.node(.{ .throw_stmt = arg });
    }

    fn parseTry(self: *Parser) Error!*ast.Node {
        try self.advance();
        const block = try self.parseBlock();
        var param: ?*ast.Node = null;
        var handler: ?*ast.Node = null;
        var finalizer: ?*ast.Node = null;
        if (self.isKw(.kcatch)) {
            try self.advance();
            if (try self.eat(.lparen)) {
                param = try self.parseBindingElement();
                try self.expect(.rparen, ")");
            }
            handler = try self.parseBlock();
        }
        if (self.isKw(.kfinally)) {
            try self.advance();
            finalizer = try self.parseBlock();
        }
        if (handler == null and finalizer == null) return self.fail("missing catch or finally after try", .{});
        return self.node(.{ .try_stmt = .{
            .block = block,
            .param = param,
            .handler = handler,
            .finalizer = finalizer,
        } });
    }

    // -- binding patterns --------------------------------------------------

    fn parseBindingTarget(self: *Parser) Error!*ast.Node {
        try self.enter();
        defer self.leave();
        if (self.is(.lbracket)) return self.parseArrayPattern();
        if (self.is(.lbrace)) return self.parseObjectPattern();
        if (self.cur.type == .ident and !lex.isReserved(self.cur.kw)) {
            const name = self.cur.str;
            try self.advance();
            return self.node(.{ .identifier = name });
        }
        return self.fail("expected a binding name but found '{s}'", .{self.tokenText()});
    }

    fn parseBindingElement(self: *Parser) Error!*ast.Node {
        if (self.is(.ellipsis)) {
            try self.advance();
            const t = try self.parseBindingTarget();
            return self.node(.{ .rest_element = t });
        }
        const target = try self.parseBindingTarget();
        if (try self.eat(.assign)) {
            const def = try self.parseAssign();
            return self.node(.{ .assign_pattern = .{ .target = target, .default = def } });
        }
        return target;
    }

    fn parseArrayPattern(self: *Parser) Error!*ast.Node {
        try self.advance(); // [
        const saved = self.no_in;
        self.no_in = false;
        defer self.no_in = saved;
        var els = std.ArrayList(?*ast.Node).init(self.arena);
        while (!self.is(.rbracket)) {
            if (self.is(.eof)) return self.fail("unterminated array pattern", .{});
            if (self.is(.comma)) {
                try els.append(null);
                try self.advance();
                continue;
            }
            try els.append(try self.parseBindingElement());
            if (!try self.eat(.comma)) break;
        }
        try self.expect(.rbracket, "]");
        return self.node(.{ .array_pattern = try els.toOwnedSlice() });
    }

    fn parseObjectPattern(self: *Parser) Error!*ast.Node {
        try self.advance(); // {
        const saved = self.no_in;
        self.no_in = false;
        defer self.no_in = saved;
        var props = std.ArrayList(ast.PatternProp).init(self.arena);
        while (!self.is(.rbrace)) {
            if (self.is(.eof)) return self.fail("unterminated object pattern", .{});
            if (self.is(.ellipsis)) {
                try self.advance();
                const t = try self.parseBindingTarget();
                try props.append(.{ .key = t, .value = t, .computed = false, .rest = true });
                if (!try self.eat(.comma)) break;
                continue;
            }
            const kc = try self.parsePropertyKey();
            var value: *ast.Node = undefined;
            if (try self.eat(.colon)) {
                value = try self.parseBindingElement();
            } else {
                if (kc.computed or kc.key.* != .identifier)
                    return self.fail("invalid shorthand property in pattern", .{});
                value = try self.node(.{ .identifier = kc.key.identifier });
                if (try self.eat(.assign)) {
                    const def = try self.parseAssign();
                    value = try self.node(.{ .assign_pattern = .{ .target = value, .default = def } });
                }
            }
            try props.append(.{ .key = kc.key, .value = value, .computed = kc.computed, .rest = false });
            if (!try self.eat(.comma)) break;
        }
        try self.expect(.rbrace, "}");
        return self.node(.{ .object_pattern = try props.toOwnedSlice() });
    }

    /// Reinterprets an already-parsed expression as an assignment target.
    fn toPattern(self: *Parser, n: *ast.Node) Error!*ast.Node {
        switch (n.*) {
            .array_lit => |els| {
                const out = try self.arena.alloc(?*ast.Node, els.len);
                for (els, 0..) |e, i| {
                    if (e) |x| {
                        out[i] = try self.toPattern(x);
                    } else out[i] = null;
                }
                return self.node(.{ .array_pattern = out });
            },
            .object_lit => |props| {
                const out = try self.arena.alloc(ast.PatternProp, props.len);
                for (props, 0..) |p, i| {
                    if (p.kind == .spread) {
                        const t = try self.toPattern(p.value);
                        out[i] = .{ .key = t, .value = t, .computed = false, .rest = true };
                    } else {
                        out[i] = .{
                            .key = p.key,
                            .value = try self.toPattern(p.value),
                            .computed = p.computed,
                            .rest = false,
                        };
                    }
                }
                return self.node(.{ .object_pattern = out });
            },
            .assign => |a| {
                if (a.op == .assign) {
                    return self.node(.{ .assign_pattern = .{
                        .target = try self.toPattern(a.target),
                        .default = a.value,
                    } });
                }
                return n;
            },
            .spread => |x| return self.node(.{ .rest_element = try self.toPattern(x) }),
            else => return n,
        }
    }

    // -- functions and classes --------------------------------------------

    fn parseParams(self: *Parser) Error![]*ast.Node {
        try self.expect(.lparen, "(");
        const saved = self.no_in;
        self.no_in = false;
        defer self.no_in = saved;
        var params = std.ArrayList(*ast.Node).init(self.arena);
        while (!self.is(.rparen)) {
            if (self.is(.eof)) return self.fail("unterminated parameter list", .{});
            try params.append(try self.parseBindingElement());
            if (!try self.eat(.comma)) break;
        }
        try self.expect(.rparen, ")");
        return params.toOwnedSlice();
    }

    fn parseFuncRest(
        self: *Parser,
        name: []const u8,
        kind: ast.FuncKind,
        is_async: bool,
        is_gen: bool,
    ) Error!*ast.Function {
        const params = try self.parseParams();
        const sg = self.in_gen;
        const sa = self.in_async;
        const sn = self.no_in;
        self.in_gen = is_gen;
        self.in_async = is_async;
        self.no_in = false;
        self.func_depth += 1;
        const body = self.parseBlock() catch |e| {
            self.in_gen = sg;
            self.in_async = sa;
            self.no_in = sn;
            self.func_depth -= 1;
            return e;
        };
        self.in_gen = sg;
        self.in_async = sa;
        self.no_in = sn;
        self.func_depth -= 1;

        const f = try self.arena.create(ast.Function);
        f.* = .{
            .name = name,
            .written_name = name.len > 0,
            .params = params,
            .body = body,
            .expression_body = false,
            .is_async = is_async,
            .is_generator = is_gen,
            .kind = kind,
        };
        return f;
    }

    /// `self.cur` is `function`.
    fn parseFunctionCommon(self: *Parser, is_async: bool, want_name: bool) Error!*ast.Function {
        try self.advance(); // function
        const is_gen = try self.eat(.star);
        var name: []const u8 = "";
        if (self.cur.type == .ident and !lex.isReserved(self.cur.kw)) {
            name = self.cur.str;
            try self.advance();
        } else if (want_name) {
            return self.fail("function declarations need a name", .{});
        }
        return self.parseFuncRest(name, .normal, is_async, is_gen);
    }

    fn parseFunctionDecl(self: *Parser, is_async: bool) Error!*ast.Node {
        const f = try self.parseFunctionCommon(is_async, true);
        return self.node(.{ .func_decl = f });
    }

    fn parseClassDecl(self: *Parser) Error!*ast.Node {
        const c = try self.parseClassCommon(true);
        return self.node(.{ .class_decl = c });
    }

    fn parseClassCommon(self: *Parser, want_name: bool) Error!*ast.Class {
        try self.advance(); // class
        var name: []const u8 = "";
        if (self.cur.type == .ident and !lex.isReserved(self.cur.kw) and self.cur.kw != .kextends) {
            name = self.cur.str;
            try self.advance();
        } else if (want_name and !self.isKw(.kextends) and !self.is(.lbrace)) {
            return self.fail("expected class name", .{});
        }
        var superclass: ?*ast.Node = null;
        if (try self.eatKw(.kextends)) {
            superclass = try self.parseUnaryTail();
        }
        const members = try self.parseClassBody();
        const c = try self.arena.create(ast.Class);
        c.* = .{ .name = name, .superclass = superclass, .members = members };
        return c;
    }

    fn parseClassBody(self: *Parser) Error![]ast.ClassMember {
        try self.expect(.lbrace, "{");
        const saved = self.no_in;
        self.no_in = false;
        defer self.no_in = saved;

        var members = std.ArrayList(ast.ClassMember).init(self.arena);
        while (!self.is(.rbrace)) {
            if (self.is(.eof)) return self.fail("unterminated class body", .{});
            if (try self.eat(.semi)) continue;

            var is_static = false;
            if (self.isKw(.kstatic) and !modifierStops(self.peek())) {
                is_static = true;
                try self.advance();
            }
            if (is_static and self.is(.lbrace)) {
                return self.fail("class static initialisation blocks are not supported", .{});
            }

            var is_async = false;
            var is_gen = false;
            var kind: ast.FuncKind = .method;

            if (self.isKw(.kasync)) {
                const p = self.peek();
                if (!modifierStops(p) and !p.nl_before) {
                    is_async = true;
                    try self.advance();
                }
            }
            if (self.is(.star)) {
                is_gen = true;
                try self.advance();
            }
            if ((self.isKw(.kget) or self.isKw(.kset)) and !modifierStops(self.peek())) {
                kind = if (self.cur.kw == .kget) .getter else .setter;
                try self.advance();
            }

            const kc = try self.parsePropertyKey();
            if (self.is(.lparen)) {
                var k = kind;
                if (k == .method and !kc.computed and !is_static and kc.key.* == .identifier and
                    std.mem.eql(u8, kc.key.identifier, "constructor")) k = .constructor;
                const f = try self.parseFuncRest("", k, is_async, is_gen);
                try members.append(.{
                    .key = kc.key,
                    .value = try self.node(.{ .function = f }),
                    .kind = k,
                    .is_static = is_static,
                    .computed = kc.computed,
                    .is_field = false,
                });
            } else {
                if (kind != .method or is_async or is_gen)
                    return self.fail("expected method body", .{});
                var value: ?*ast.Node = null;
                if (try self.eat(.assign)) value = try self.parseAssign();
                try self.semicolon();
                try members.append(.{
                    .key = kc.key,
                    .value = value,
                    .kind = .method,
                    .is_static = is_static,
                    .computed = kc.computed,
                    .is_field = true,
                });
            }
        }
        try self.advance(); // }
        return members.toOwnedSlice();
    }

    /// True when the token after a possible modifier means the modifier was
    /// really the member name (`static() {}`, `get = 1`, `async;`).
    fn modifierStops(p: Token) bool {
        return switch (p.type) {
            .lparen, .assign, .semi, .rbrace, .colon, .comma, .eof => true,
            else => false,
        };
    }

    fn parsePropertyKey(self: *Parser) Error!KeyC {
        switch (self.cur.type) {
            .lbracket => {
                try self.advance();
                const saved = self.no_in;
                self.no_in = false;
                const e = self.parseAssign() catch |err| {
                    self.no_in = saved;
                    return err;
                };
                self.no_in = saved;
                try self.expect(.rbracket, "]");
                return .{ .key = e, .computed = true };
            },
            .ident => {
                const nm = self.cur.str;
                try self.advance();
                return .{ .key = try self.node(.{ .identifier = nm }), .computed = false };
            },
            .str => {
                const s = self.cur.str;
                try self.advance();
                return .{ .key = try self.node(.{ .string = s }), .computed = false };
            },
            .num => {
                const v = self.cur.num;
                try self.advance();
                return .{ .key = try self.node(.{ .number = v }), .computed = false };
            },
            else => return self.fail("expected a property name but found '{s}'", .{self.tokenText()}),
        }
    }

    // -- expressions -------------------------------------------------------

    fn parseExpressionReset(self: *Parser) Error!*ast.Node {
        const saved = self.no_in;
        self.no_in = false;
        const e = self.parseExpression() catch |err| {
            self.no_in = saved;
            return err;
        };
        self.no_in = saved;
        return e;
    }

    fn parseAssignReset(self: *Parser) Error!*ast.Node {
        const saved = self.no_in;
        self.no_in = false;
        const e = self.parseAssign() catch |err| {
            self.no_in = saved;
            return err;
        };
        self.no_in = saved;
        return e;
    }

    fn parseAssignNoIn(self: *Parser) Error!*ast.Node {
        const saved = self.no_in;
        self.no_in = true;
        const e = self.parseAssign() catch |err| {
            self.no_in = saved;
            return err;
        };
        self.no_in = saved;
        return e;
    }

    pub fn parseExpression(self: *Parser) Error!*ast.Node {
        const first = try self.parseAssign();
        if (!self.is(.comma)) return first;
        var items = std.ArrayList(*ast.Node).init(self.arena);
        try items.append(first);
        while (try self.eat(.comma)) {
            try items.append(try self.parseAssign());
        }
        return self.node(.{ .sequence = try items.toOwnedSlice() });
    }

    fn assignOp(t: T) ?ast.AssignOp {
        return switch (t) {
            .assign => .assign,
            .plus_a => .add,
            .minus_a => .sub,
            .star_a => .mul,
            .slash_a => .div,
            .percent_a => .mod,
            .starstar_a => .pow,
            .shl_a => .shl,
            .shr_a => .shr,
            .ushr_a => .ushr,
            .amp_a => .bitand,
            .pipe_a => .bitor,
            .caret_a => .bitxor,
            .andand_a => .logical_and,
            .oror_a => .logical_or,
            .question2_a => .nullish,
            else => null,
        };
    }

    fn parseAssign(self: *Parser) Error!*ast.Node {
        try self.enter();
        defer self.leave();

        if (self.isKw(.kyield) and self.in_gen) return self.parseYield();

        // arrow-function lookahead
        if (self.cur.type == .ident and !lex.isReserved(self.cur.kw)) {
            const p = self.peek();
            if (p.type == .arrow and !p.nl_before) return self.parseArrowFromIdent(false);
        }
        if (self.isKw(.kasync)) {
            const p = self.peek();
            if (!p.nl_before) {
                if (p.type == .ident and !lex.isReserved(p.kw)) {
                    var lx2 = self.lx;
                    _ = lx2.next();
                    const p2 = lx2.next();
                    if (p2.type == .arrow and !p2.nl_before) {
                        try self.advance(); // async
                        return self.parseArrowFromIdent(true);
                    }
                } else if (p.type == .lparen) {
                    var lx2 = self.lx;
                    _ = lx2.next(); // the '('
                    if (arrowAfterParens(lx2)) {
                        try self.advance(); // async
                        return self.parseArrowParen(true);
                    }
                }
            }
        }
        if (self.is(.lparen) and arrowAfterParens(self.lx)) return self.parseArrowParen(false);

        const left = try self.parseConditional();
        if (assignOp(self.cur.type)) |op| {
            try self.advance();
            const target = if (op == .assign) try self.toPattern(left) else left;
            const value = try self.parseAssign();
            return self.node(.{ .assign = .{ .op = op, .target = target, .value = value } });
        }
        return left;
    }

    fn parseYield(self: *Parser) Error!*ast.Node {
        try self.advance(); // yield
        const delegate = try self.eat(.star);
        var arg: ?*ast.Node = null;
        if (!self.cur.nl_before and self.canStartExpression()) arg = try self.parseAssign();
        return self.node(.{ .yield_expr = .{ .arg = arg, .delegate = delegate } });
    }

    fn canStartExpression(self: *const Parser) bool {
        return switch (self.cur.type) {
            .eof, .rparen, .rbracket, .rbrace, .comma, .semi, .colon, .arrow => false,
            .ident => switch (self.cur.kw) {
                .kin, .kinstanceof, .kelse, .kcase, .kdefault, .kextends => false,
                else => true,
            },
            else => true,
        };
    }

    fn parseArrowFromIdent(self: *Parser, is_async: bool) Error!*ast.Node {
        const name = self.cur.str;
        try self.advance();
        var params = try self.arena.alloc(*ast.Node, 1);
        params[0] = try self.node(.{ .identifier = name });
        return self.finishArrow(params, is_async);
    }

    fn parseArrowParen(self: *Parser, is_async: bool) Error!*ast.Node {
        const params = try self.parseParams();
        return self.finishArrow(params, is_async);
    }

    fn finishArrow(self: *Parser, params: []*ast.Node, is_async: bool) Error!*ast.Node {
        if (!self.is(.arrow)) return self.fail("expected '=>'", .{});
        if (self.cur.nl_before) return self.fail("newline not allowed before '=>'", .{});
        try self.advance();

        const sg = self.in_gen;
        const sa = self.in_async;
        const sn = self.no_in;
        self.in_gen = false;
        self.in_async = is_async;
        self.no_in = false;
        self.func_depth += 1;
        defer {
            self.in_gen = sg;
            self.in_async = sa;
            self.no_in = sn;
            self.func_depth -= 1;
        }

        var expression_body = false;
        var body: *ast.Node = undefined;
        if (self.is(.lbrace)) {
            body = try self.parseBlock();
        } else {
            expression_body = true;
            body = try self.parseAssign();
        }
        const f = try self.arena.create(ast.Function);
        f.* = .{
            .name = "",
            .params = params,
            .body = body,
            .expression_body = expression_body,
            .is_async = is_async,
            .is_generator = false,
            .kind = .arrow,
        };
        return self.node(.{ .function = f });
    }

    fn parseConditional(self: *Parser) Error!*ast.Node {
        const cond = try self.parseBinary(1);
        if (!self.is(.question)) return cond;
        try self.advance();
        const then_expr = try self.parseAssignReset();
        try self.expect(.colon, ":");
        const else_expr = try self.parseAssign();
        return self.node(.{ .conditional = .{
            .cond = cond,
            .then_expr = then_expr,
            .else_expr = else_expr,
        } });
    }

    fn binPrec(self: *const Parser, tok: Token) u8 {
        return switch (tok.type) {
            .question2 => 1,
            .oror => 2,
            .andand => 3,
            .pipe => 4,
            .caret => 5,
            .amp => 6,
            .eq, .ne, .seq, .sne => 7,
            .lt, .gt, .le, .ge => 8,
            .shl, .shr, .ushr => 9,
            .plus, .minus => 10,
            .star, .slash, .percent => 11,
            .starstar => 12,
            .ident => switch (tok.kw) {
                .kinstanceof => 8,
                .kin => if (self.no_in) 0 else 8,
                else => 0,
            },
            else => 0,
        };
    }

    fn parseBinary(self: *Parser, min_prec: u8) Error!*ast.Node {
        try self.enter();
        defer self.leave();
        var left = try self.parseUnary();
        while (true) {
            const p = self.binPrec(self.cur);
            if (p == 0 or p < min_prec) break;
            const tok = self.cur;
            try self.advance();
            const next_min: u8 = if (tok.type == .starstar) p else p + 1;
            const right = try self.parseBinary(next_min);
            left = switch (tok.type) {
                .andand => try self.node(.{ .logical = .{ .op = .logical_and, .left = left, .right = right } }),
                .oror => try self.node(.{ .logical = .{ .op = .logical_or, .left = left, .right = right } }),
                .question2 => try self.node(.{ .logical = .{ .op = .nullish, .left = left, .right = right } }),
                else => try self.node(.{ .binary = .{ .op = binOp(tok), .left = left, .right = right } }),
            };
        }
        return left;
    }

    fn binOp(tok: Token) ast.BinaryOp {
        return switch (tok.type) {
            .plus => .add,
            .minus => .sub,
            .star => .mul,
            .slash => .div,
            .percent => .mod,
            .starstar => .pow,
            .eq => .eq,
            .ne => .neq,
            .seq => .strict_eq,
            .sne => .strict_neq,
            .lt => .lt,
            .gt => .gt,
            .le => .le,
            .ge => .ge,
            .shl => .shl,
            .shr => .shr,
            .ushr => .ushr,
            .amp => .bitand,
            .pipe => .bitor,
            .caret => .bitxor,
            .ident => if (tok.kw == .kinstanceof) .instanceof else .in_op,
            else => .add,
        };
    }

    fn parseUnary(self: *Parser) Error!*ast.Node {
        const op: ?ast.UnaryOp = switch (self.cur.type) {
            .bang => .not,
            .tilde => .bitnot,
            .plus => .plus,
            .minus => .neg,
            .ident => switch (self.cur.kw) {
                .ktypeof => ast.UnaryOp.typeof_op,
                .kvoid => ast.UnaryOp.void_op,
                .kdelete => ast.UnaryOp.delete_op,
                else => null,
            },
            else => null,
        };
        if (op) |o| {
            try self.advance();
            const operand = try self.parseUnary();
            return self.node(.{ .unary = .{ .op = o, .operand = operand } });
        }
        if (self.is(.plusplus) or self.is(.minusminus)) {
            const uop: ast.UpdateOp = if (self.is(.plusplus)) .inc else .dec;
            try self.advance();
            const target = try self.parseUnary();
            return self.node(.{ .update = .{ .op = uop, .prefix = true, .target = target } });
        }
        if (self.isKw(.kawait) and (self.in_async or self.func_depth == 0)) {
            const p = self.peek();
            if (p.type != .arrow and p.type != .assign) {
                try self.advance();
                const operand = try self.parseUnary();
                return self.node(.{ .await_expr = operand });
            }
        }
        return self.parsePostfix();
    }

    /// Used for `extends <expr>`: a left-hand-side expression, no operators.
    fn parseUnaryTail(self: *Parser) Error!*ast.Node {
        return self.parseCallMember(true);
    }

    fn parsePostfix(self: *Parser) Error!*ast.Node {
        const expr = try self.parseCallMember(true);
        if ((self.is(.plusplus) or self.is(.minusminus)) and !self.cur.nl_before) {
            const uop: ast.UpdateOp = if (self.is(.plusplus)) .inc else .dec;
            try self.advance();
            return self.node(.{ .update = .{ .op = uop, .prefix = false, .target = expr } });
        }
        return expr;
    }

    fn parseCallMember(self: *Parser, allow_call: bool) Error!*ast.Node {
        const base = if (self.isKw(.knew)) try self.parseNew() else try self.parsePrimary();
        return self.parseMemberTail(base, allow_call);
    }

    fn parseNew(self: *Parser) Error!*ast.Node {
        try self.advance(); // new
        if (self.is(.dot)) {
            try self.advance();
            if (self.cur.type != .ident) return self.fail("expected 'target' after 'new.'", .{});
            try self.advance();
            return self.node(.{ .identifier = "new.target" });
        }
        const inner = if (self.isKw(.knew)) try self.parseNew() else try self.parsePrimary();
        const callee = try self.parseMemberTail(inner, false);
        var args: []ast.Arg = &[_]ast.Arg{};
        if (self.is(.lparen)) args = try self.parseArgs();
        return self.node(.{ .new_expr = .{ .callee = callee, .args = args } });
    }

    fn parseMemberTail(self: *Parser, base_in: *ast.Node, allow_call: bool) Error!*ast.Node {
        var base = base_in;
        while (true) {
            switch (self.cur.type) {
                .dot => {
                    try self.advance();
                    if (self.cur.type != .ident) return self.fail("expected a property name after '.'", .{});
                    const nm = self.cur.str;
                    try self.advance();
                    base = try self.node(.{ .member = .{
                        .object = base,
                        .property = try self.node(.{ .identifier = nm }),
                        .computed = false,
                        .optional = false,
                    } });
                },
                .lbracket => {
                    try self.advance();
                    const idx = try self.parseExpressionReset();
                    try self.expect(.rbracket, "]");
                    base = try self.node(.{ .member = .{
                        .object = base,
                        .property = idx,
                        .computed = true,
                        .optional = false,
                    } });
                },
                .opt_chain => {
                    try self.advance();
                    if (self.is(.lparen)) {
                        if (!allow_call) return base;
                        const args = try self.parseArgs();
                        base = try self.node(.{ .call = .{ .callee = base, .args = args, .optional = true } });
                    } else if (self.is(.lbracket)) {
                        try self.advance();
                        const idx = try self.parseExpressionReset();
                        try self.expect(.rbracket, "]");
                        base = try self.node(.{ .member = .{
                            .object = base,
                            .property = idx,
                            .computed = true,
                            .optional = true,
                        } });
                    } else {
                        if (self.cur.type != .ident) return self.fail("expected a property name after '?.'", .{});
                        const nm = self.cur.str;
                        try self.advance();
                        base = try self.node(.{ .member = .{
                            .object = base,
                            .property = try self.node(.{ .identifier = nm }),
                            .computed = false,
                            .optional = true,
                        } });
                    }
                },
                .lparen => {
                    if (!allow_call) return base;
                    const args = try self.parseArgs();
                    base = try self.node(.{ .call = .{ .callee = base, .args = args, .optional = false } });
                },
                .tmpl => {
                    base = try self.parseTemplate(base);
                },
                else => return base,
            }
        }
    }

    fn parseArgs(self: *Parser) Error![]ast.Arg {
        try self.expect(.lparen, "(");
        const saved = self.no_in;
        self.no_in = false;
        defer self.no_in = saved;
        var args = std.ArrayList(ast.Arg).init(self.arena);
        while (!self.is(.rparen)) {
            if (self.is(.eof)) return self.fail("unterminated argument list", .{});
            var spread = false;
            if (self.is(.ellipsis)) {
                spread = true;
                try self.advance();
            }
            try args.append(.{ .value = try self.parseAssign(), .spread = spread });
            if (!try self.eat(.comma)) break;
        }
        try self.expect(.rparen, ")");
        return args.toOwnedSlice();
    }

    fn parsePrimary(self: *Parser) Error!*ast.Node {
        try self.enter();
        defer self.leave();

        switch (self.cur.type) {
            .num => {
                const v = self.cur.num;
                try self.advance();
                return self.node(.{ .number = v });
            },
            .str => {
                const s = self.cur.str;
                try self.advance();
                return self.node(.{ .string = s });
            },
            .regex => {
                const pat = self.cur.str;
                const fl = self.cur.flags;
                try self.advance();
                return self.node(.{ .regex = .{ .pattern = pat, .flags = fl } });
            },
            .tmpl => return self.parseTemplate(null),
            .lbracket => return self.parseArrayLiteral(),
            .lbrace => return self.parseObjectLiteral(),
            .lparen => {
                try self.advance();
                const saved = self.no_in;
                self.no_in = false;
                const e = self.parseExpression() catch |err| {
                    self.no_in = saved;
                    return err;
                };
                self.no_in = saved;
                try self.expect(.rparen, ")");
                return e;
            },
            .ident => switch (self.cur.kw) {
                .knull => {
                    try self.advance();
                    return self.node(.null_lit);
                },
                .ktrue => {
                    try self.advance();
                    return self.node(.{ .boolean = true });
                },
                .kfalse => {
                    try self.advance();
                    return self.node(.{ .boolean = false });
                },
                .kthis => {
                    try self.advance();
                    return self.node(.this_expr);
                },
                .ksuper => {
                    try self.advance();
                    return self.node(.super_expr);
                },
                .kfunction => {
                    const f = try self.parseFunctionCommon(false, false);
                    return self.node(.{ .function = f });
                },
                .kclass => {
                    const c = try self.parseClassCommon(false);
                    return self.node(.{ .class_expr = c });
                },
                .kasync => {
                    const p = self.peek();
                    if (p.type == .ident and p.kw == .kfunction and !p.nl_before) {
                        try self.advance();
                        const f = try self.parseFunctionCommon(true, false);
                        return self.node(.{ .function = f });
                    }
                    const nm = self.cur.str;
                    try self.advance();
                    return self.node(.{ .identifier = nm });
                },
                .knew => return self.parseNew(),
                else => {
                    if (lex.isReserved(self.cur.kw))
                        return self.fail("unexpected keyword '{s}'", .{self.tokenText()});
                    const nm = self.cur.str;
                    try self.advance();
                    return self.node(.{ .identifier = nm });
                },
            },
            else => return self.fail("unexpected token '{s}'", .{self.tokenText()}),
        }
    }

    fn parseArrayLiteral(self: *Parser) Error!*ast.Node {
        try self.advance(); // [
        const saved = self.no_in;
        self.no_in = false;
        defer self.no_in = saved;
        var els = std.ArrayList(?*ast.Node).init(self.arena);
        while (!self.is(.rbracket)) {
            if (self.is(.eof)) return self.fail("unterminated array literal", .{});
            if (self.is(.comma)) {
                try els.append(null);
                try self.advance();
                continue;
            }
            if (self.is(.ellipsis)) {
                try self.advance();
                const inner = try self.parseAssign();
                try els.append(try self.node(.{ .spread = inner }));
            } else {
                try els.append(try self.parseAssign());
            }
            if (!try self.eat(.comma)) break;
        }
        try self.expect(.rbracket, "]");
        const owned = try els.toOwnedSlice();
        return self.node(.{ .array_lit = owned });
    }

    fn parseObjectLiteral(self: *Parser) Error!*ast.Node {
        try self.advance(); // {
        const saved = self.no_in;
        self.no_in = false;
        defer self.no_in = saved;
        var props = std.ArrayList(ast.Property).init(self.arena);
        while (!self.is(.rbrace)) {
            if (self.is(.eof)) return self.fail("unterminated object literal", .{});

            if (self.is(.ellipsis)) {
                try self.advance();
                const v = try self.parseAssign();
                try props.append(.{
                    .key = v,
                    .value = v,
                    .computed = false,
                    .kind = .spread,
                    .method = false,
                });
                if (!try self.eat(.comma)) break;
                continue;
            }

            var is_async = false;
            var is_gen = false;
            var kind: ast.PropKind = .init;

            if (self.isKw(.kasync)) {
                const p = self.peek();
                if (!modifierStops(p) and !p.nl_before) {
                    is_async = true;
                    try self.advance();
                }
            }
            if (self.is(.star)) {
                is_gen = true;
                try self.advance();
            }
            if ((self.isKw(.kget) or self.isKw(.kset)) and !modifierStops(self.peek())) {
                kind = if (self.cur.kw == .kget) .get else .set;
                try self.advance();
            }

            const kc = try self.parsePropertyKey();

            if (self.is(.lparen)) {
                const fk: ast.FuncKind = switch (kind) {
                    .get => .getter,
                    .set => .setter,
                    else => .method,
                };
                const f = try self.parseFuncRest("", fk, is_async, is_gen);
                try props.append(.{
                    .key = kc.key,
                    .value = try self.node(.{ .function = f }),
                    .computed = kc.computed,
                    .kind = kind,
                    .method = true,
                });
            } else if (kind != .init or is_async or is_gen) {
                return self.fail("expected method body", .{});
            } else if (try self.eat(.colon)) {
                const v = try self.parseAssign();
                try props.append(.{
                    .key = kc.key,
                    .value = v,
                    .computed = kc.computed,
                    .kind = .init,
                    .method = false,
                });
            } else {
                if (kc.computed or kc.key.* != .identifier)
                    return self.fail("expected ':' after property name", .{});
                var v = try self.node(.{ .identifier = kc.key.identifier });
                if (try self.eat(.assign)) {
                    const def = try self.parseAssign();
                    v = try self.node(.{ .assign = .{ .op = .assign, .target = v, .value = def } });
                }
                try props.append(.{
                    .key = kc.key,
                    .value = v,
                    .computed = false,
                    .kind = .init,
                    .method = false,
                });
            }

            if (!try self.eat(.comma)) break;
        }
        try self.expect(.rbrace, "}");
        return self.node(.{ .object_lit = try props.toOwnedSlice() });
    }

    /// `self.cur` is the template token.  Rewinds the lexer into each `${}`
    /// hole, parses it, then restores and advances past the whole literal.
    fn parseTemplate(self: *Parser, tag: ?*ast.Node) Error!*ast.Node {
        const tok = self.cur;
        if (tok.tmpl >= self.lx.templates.items.len) return self.fail("internal template error", .{});
        const td = self.lx.templates.items[tok.tmpl];
        const saved_lx = self.lx;
        const saved_no_in = self.no_in;

        var exprs = try self.arena.alloc(*ast.Node, td.spans.len);
        for (td.spans, 0..) |sp, i| {
            self.lx.pos = sp.start;
            self.lx.line = sp.line;
            self.lx.col = sp.col;
            self.lx.prev = .eof;
            self.lx.prev_kw = .none;
            self.lx.paren_depth = 0;
            self.no_in = false;
            try self.advance();
            exprs[i] = try self.parseExpression();
            if (self.cur.start < sp.end) return self.fail("unexpected token in template substitution", .{});
        }
        self.no_in = saved_no_in;
        self.lx = saved_lx;
        try self.advance();

        if (tag) |tg| {
            return self.node(.{ .tagged_template = .{
                .tag = tg,
                .quasis = td.quasis,
                .exprs = exprs,
            } });
        }
        return self.node(.{ .template = .{ .quasis = td.quasis, .exprs = exprs } });
    }
};

/// `lx` must sit just after an open paren.  Scans to the matching close paren
/// and reports whether `=>` follows.
fn arrowAfterParens(lx_in: lex.Lexer) bool {
    var lx = lx_in;
    var depth: i32 = 1;
    var guard: u32 = 0;
    while (true) {
        guard += 1;
        if (guard > 20000) return false;
        const t = lx.next();
        if (t.type == .eof or t.type == .invalid) return false;
        switch (t.type) {
            .lparen, .lbracket, .lbrace => depth += 1,
            .rparen, .rbracket, .rbrace => {
                depth -= 1;
                if (depth <= 0) {
                    const n = lx.next();
                    return n.type == .arrow;
                }
            },
            else => {},
        }
    }
}

// -- AST dumper -----------------------------------------------------------

/// Recursion budget for the dumper.  A long method chain nests thousands of
/// nodes deep and the dumper is only a debugging aid, so it truncates rather
/// than overflowing the stack.
const DUMP_MAX_DEPTH: u32 = 300;
threadlocal var dump_depth: u32 = 0;

/// Compact s-expression rendering of a tree.  Handy in tests and for anyone
/// debugging the compiler that consumes this AST.
pub fn dump(n: *const ast.Node, writer: anytype) anyerror!void {
    if (dump_depth >= DUMP_MAX_DEPTH) {
        try writer.writeAll("...");
        return;
    }
    dump_depth += 1;
    defer dump_depth -= 1;

    switch (n.*) {
        .number => |v| {
            if (v == @floor(v) and @abs(v) < 1e15) {
                try writer.print("{d}", .{@as(i64, @intFromFloat(v))});
            } else {
                try writer.print("{d}", .{v});
            }
        },
        .string => |s| try writer.print("\"{s}\"", .{s}),
        .regex => |r| try writer.print("(regex /{s}/{s})", .{ r.pattern, r.flags }),
        .boolean => |b| try writer.print("{s}", .{if (b) "true" else "false"}),
        .null_lit => try writer.writeAll("null"),
        .undefined_lit => try writer.writeAll("undefined"),
        .identifier => |s| try writer.print("{s}", .{s}),
        .this_expr => try writer.writeAll("this"),
        .super_expr => try writer.writeAll("super"),

        .array_lit => |els| {
            try writer.writeAll("(array");
            for (els) |e| {
                try writer.writeAll(" ");
                if (e) |x| try dump(x, writer) else try writer.writeAll("hole");
            }
            try writer.writeAll(")");
        },
        .object_lit => |props| {
            try writer.writeAll("(object");
            for (props) |p| {
                try writer.writeAll(" ");
                try dumpProp(p, writer);
            }
            try writer.writeAll(")");
        },
        .template => |t| {
            try writer.writeAll("(template");
            for (t.quasis, 0..) |q, i| {
                try writer.print(" \"{s}\"", .{q});
                if (i < t.exprs.len) {
                    try writer.writeAll(" ");
                    try dump(t.exprs[i], writer);
                }
            }
            try writer.writeAll(")");
        },
        .tagged_template => |t| {
            try writer.writeAll("(tagged ");
            try dump(t.tag, writer);
            for (t.quasis, 0..) |q, i| {
                try writer.print(" \"{s}\"", .{q});
                if (i < t.exprs.len) {
                    try writer.writeAll(" ");
                    try dump(t.exprs[i], writer);
                }
            }
            try writer.writeAll(")");
        },

        .unary => |u| {
            try writer.print("({s} ", .{unaryName(u.op)});
            try dump(u.operand, writer);
            try writer.writeAll(")");
        },
        .update => |u| {
            try writer.print("({s}{s} ", .{
                if (u.prefix) "pre" else "post",
                if (u.op == .inc) "++" else "--",
            });
            try dump(u.target, writer);
            try writer.writeAll(")");
        },
        .binary => |b| {
            try writer.print("({s} ", .{binName(b.op)});
            try dump(b.left, writer);
            try writer.writeAll(" ");
            try dump(b.right, writer);
            try writer.writeAll(")");
        },
        .logical => |b| {
            try writer.print("({s} ", .{switch (b.op) {
                .logical_and => "&&",
                .logical_or => "||",
                .nullish => "??",
            }});
            try dump(b.left, writer);
            try writer.writeAll(" ");
            try dump(b.right, writer);
            try writer.writeAll(")");
        },
        .assign => |a| {
            try writer.print("({s} ", .{assignName(a.op)});
            try dump(a.target, writer);
            try writer.writeAll(" ");
            try dump(a.value, writer);
            try writer.writeAll(")");
        },
        .conditional => |c| {
            try writer.writeAll("(?: ");
            try dump(c.cond, writer);
            try writer.writeAll(" ");
            try dump(c.then_expr, writer);
            try writer.writeAll(" ");
            try dump(c.else_expr, writer);
            try writer.writeAll(")");
        },
        .sequence => |items| {
            try writer.writeAll("(seq");
            for (items) |e| {
                try writer.writeAll(" ");
                try dump(e, writer);
            }
            try writer.writeAll(")");
        },

        .member => |m| {
            try writer.writeAll(if (m.optional) "(?. " else "(. ");
            try dump(m.object, writer);
            try writer.writeAll(" ");
            if (m.computed) {
                try writer.writeAll("[");
                try dump(m.property, writer);
                try writer.writeAll("]");
            } else try dump(m.property, writer);
            try writer.writeAll(")");
        },
        .call => |c| {
            try writer.writeAll(if (c.optional) "(?call " else "(call ");
            try dump(c.callee, writer);
            for (c.args) |a| {
                try writer.writeAll(" ");
                if (a.spread) try writer.writeAll("...");
                try dump(a.value, writer);
            }
            try writer.writeAll(")");
        },
        .new_expr => |c| {
            try writer.writeAll("(new ");
            try dump(c.callee, writer);
            for (c.args) |a| {
                try writer.writeAll(" ");
                if (a.spread) try writer.writeAll("...");
                try dump(a.value, writer);
            }
            try writer.writeAll(")");
        },

        .function => |f| try dumpFunction(f, writer, "fn"),
        .class_expr => |c| try dumpClass(c, writer, "class-expr"),
        .spread => |x| {
            try writer.writeAll("(... ");
            try dump(x, writer);
            try writer.writeAll(")");
        },
        .yield_expr => |y| {
            try writer.writeAll(if (y.delegate) "(yield*" else "(yield");
            if (y.arg) |a| {
                try writer.writeAll(" ");
                try dump(a, writer);
            }
            try writer.writeAll(")");
        },
        .await_expr => |x| {
            try writer.writeAll("(await ");
            try dump(x, writer);
            try writer.writeAll(")");
        },

        .array_pattern => |els| {
            try writer.writeAll("(array-pat");
            for (els) |e| {
                try writer.writeAll(" ");
                if (e) |x| try dump(x, writer) else try writer.writeAll("hole");
            }
            try writer.writeAll(")");
        },
        .object_pattern => |props| {
            try writer.writeAll("(object-pat");
            for (props) |p| {
                try writer.writeAll(" ");
                if (p.rest) {
                    try writer.writeAll("(rest ");
                    try dump(p.value, writer);
                    try writer.writeAll(")");
                } else {
                    try writer.writeAll("(");
                    if (p.computed) {
                        try writer.writeAll("[");
                        try dump(p.key, writer);
                        try writer.writeAll("]");
                    } else try dump(p.key, writer);
                    try writer.writeAll(" ");
                    try dump(p.value, writer);
                    try writer.writeAll(")");
                }
            }
            try writer.writeAll(")");
        },
        .assign_pattern => |a| {
            try writer.writeAll("(default ");
            try dump(a.target, writer);
            try writer.writeAll(" ");
            try dump(a.default, writer);
            try writer.writeAll(")");
        },
        .rest_element => |x| {
            try writer.writeAll("(rest ");
            try dump(x, writer);
            try writer.writeAll(")");
        },

        .program => |body| {
            try writer.writeAll("(program");
            for (body) |s| {
                try writer.writeAll(" ");
                try dump(s, writer);
            }
            try writer.writeAll(")");
        },
        .var_decl => |v| {
            try writer.print("({s}", .{switch (v.kind) {
                .none => "decl",
                .@"var" => "var",
                .let => "let",
                .@"const" => "const",
            }});
            for (v.decls) |d| {
                try writer.writeAll(" (");
                try dump(d.target, writer);
                if (d.init) |i| {
                    try writer.writeAll(" ");
                    try dump(i, writer);
                }
                try writer.writeAll(")");
            }
            try writer.writeAll(")");
        },
        .expr_stmt => |e| {
            try writer.writeAll("(expr ");
            try dump(e, writer);
            try writer.writeAll(")");
        },
        .empty_stmt => try writer.writeAll("(empty)"),
        .block => |body| {
            try writer.writeAll("(block");
            for (body) |s| {
                try writer.writeAll(" ");
                try dump(s, writer);
            }
            try writer.writeAll(")");
        },
        .if_stmt => |i| {
            try writer.writeAll("(if ");
            try dump(i.cond, writer);
            try writer.writeAll(" ");
            try dump(i.then_body, writer);
            if (i.else_body) |e| {
                try writer.writeAll(" ");
                try dump(e, writer);
            }
            try writer.writeAll(")");
        },
        .for_stmt => |f| {
            try writer.writeAll("(for ");
            if (f.init) |x| try dump(x, writer) else try writer.writeAll("_");
            try writer.writeAll(" ");
            if (f.cond) |x| try dump(x, writer) else try writer.writeAll("_");
            try writer.writeAll(" ");
            if (f.update) |x| try dump(x, writer) else try writer.writeAll("_");
            try writer.writeAll(" ");
            try dump(f.body, writer);
            try writer.writeAll(")");
        },
        .for_in => |f| {
            try writer.print("(for-{s} {s} ", .{
                if (f.of) "of" else "in",
                switch (f.decl) {
                    .none => "-",
                    .@"var" => "var",
                    .let => "let",
                    .@"const" => "const",
                },
            });
            try dump(f.left, writer);
            try writer.writeAll(" ");
            try dump(f.right, writer);
            try writer.writeAll(" ");
            try dump(f.body, writer);
            try writer.writeAll(")");
        },
        .while_stmt => |w| {
            try writer.writeAll("(while ");
            try dump(w.cond, writer);
            try writer.writeAll(" ");
            try dump(w.body, writer);
            try writer.writeAll(")");
        },
        .do_while => |w| {
            try writer.writeAll("(do ");
            try dump(w.body, writer);
            try writer.writeAll(" ");
            try dump(w.cond, writer);
            try writer.writeAll(")");
        },
        .switch_stmt => |s| {
            try writer.writeAll("(switch ");
            try dump(s.disc, writer);
            for (s.cases) |c| {
                try writer.writeAll(" (case ");
                if (c.taste) |t| try dump(t, writer) else try writer.writeAll("default");
                for (c.body) |b| {
                    try writer.writeAll(" ");
                    try dump(b, writer);
                }
                try writer.writeAll(")");
            }
            try writer.writeAll(")");
        },
        .return_stmt => |r| {
            try writer.writeAll("(return");
            if (r) |x| {
                try writer.writeAll(" ");
                try dump(x, writer);
            }
            try writer.writeAll(")");
        },
        .break_stmt => |l| {
            try writer.writeAll("(break");
            if (l) |s| try writer.print(" {s}", .{s});
            try writer.writeAll(")");
        },
        .continue_stmt => |l| {
            try writer.writeAll("(continue");
            if (l) |s| try writer.print(" {s}", .{s});
            try writer.writeAll(")");
        },
        .throw_stmt => |x| {
            try writer.writeAll("(throw ");
            try dump(x, writer);
            try writer.writeAll(")");
        },
        .try_stmt => |t| {
            try writer.writeAll("(try ");
            try dump(t.block, writer);
            if (t.handler) |h| {
                try writer.writeAll(" (catch ");
                if (t.param) |p| {
                    try dump(p, writer);
                    try writer.writeAll(" ");
                }
                try dump(h, writer);
                try writer.writeAll(")");
            }
            if (t.finalizer) |f| {
                try writer.writeAll(" (finally ");
                try dump(f, writer);
                try writer.writeAll(")");
            }
            try writer.writeAll(")");
        },
        .labeled => |l| {
            try writer.print("(label {s} ", .{l.label});
            try dump(l.body, writer);
            try writer.writeAll(")");
        },
        .func_decl => |f| try dumpFunction(f, writer, "fn-decl"),
        .class_decl => |c| try dumpClass(c, writer, "class"),
    }
}

fn dumpProp(p: ast.Property, writer: anytype) anyerror!void {
    switch (p.kind) {
        .spread => {
            try writer.writeAll("(...");
            try writer.writeAll(" ");
            try dump(p.value, writer);
            try writer.writeAll(")");
        },
        else => {
            try writer.writeAll("(");
            try writer.writeAll(switch (p.kind) {
                .get => "get ",
                .set => "set ",
                else => "",
            });
            if (p.computed) {
                try writer.writeAll("[");
                try dump(p.key, writer);
                try writer.writeAll("]");
            } else try dump(p.key, writer);
            try writer.writeAll(" ");
            try dump(p.value, writer);
            try writer.writeAll(")");
        },
    }
}

fn dumpFunction(f: *const ast.Function, writer: anytype, tag: []const u8) anyerror!void {
    try writer.print("({s}", .{tag});
    if (f.is_async) try writer.writeAll(" async");
    if (f.is_generator) try writer.writeAll(" gen");
    if (f.kind == .arrow) try writer.writeAll(" arrow");
    if (f.name.len > 0) try writer.print(" {s}", .{f.name});
    try writer.writeAll(" (params");
    for (f.params) |p| {
        try writer.writeAll(" ");
        try dump(p, writer);
    }
    try writer.writeAll(") ");
    try dump(f.body, writer);
    try writer.writeAll(")");
}

fn dumpClass(c: *const ast.Class, writer: anytype, tag: []const u8) anyerror!void {
    try writer.print("({s}", .{tag});
    if (c.name.len > 0) try writer.print(" {s}", .{c.name});
    if (c.superclass) |s| {
        try writer.writeAll(" (extends ");
        try dump(s, writer);
        try writer.writeAll(")");
    }
    for (c.members) |m| {
        try writer.writeAll(" (");
        if (m.is_static) try writer.writeAll("static ");
        if (m.is_field) {
            try writer.writeAll("field ");
        } else switch (m.kind) {
            .getter => try writer.writeAll("get "),
            .setter => try writer.writeAll("set "),
            .constructor => try writer.writeAll("ctor "),
            else => {},
        }
        if (m.computed) {
            try writer.writeAll("[");
            try dump(m.key, writer);
            try writer.writeAll("]");
        } else try dump(m.key, writer);
        if (m.value) |v| {
            try writer.writeAll(" ");
            try dump(v, writer);
        }
        try writer.writeAll(")");
    }
    try writer.writeAll(")");
}

fn unaryName(op: ast.UnaryOp) []const u8 {
    return switch (op) {
        .neg => "-",
        .plus => "+",
        .not => "!",
        .bitnot => "~",
        .typeof_op => "typeof",
        .void_op => "void",
        .delete_op => "delete",
    };
}

fn binName(op: ast.BinaryOp) []const u8 {
    return switch (op) {
        .add => "+",
        .sub => "-",
        .mul => "*",
        .div => "/",
        .mod => "%",
        .pow => "**",
        .eq => "==",
        .neq => "!=",
        .strict_eq => "===",
        .strict_neq => "!==",
        .lt => "<",
        .gt => ">",
        .le => "<=",
        .ge => ">=",
        .shl => "<<",
        .shr => ">>",
        .ushr => ">>>",
        .bitand => "&",
        .bitor => "|",
        .bitxor => "^",
        .instanceof => "instanceof",
        .in_op => "in",
    };
}

fn assignName(op: ast.AssignOp) []const u8 {
    return switch (op) {
        .assign => "=",
        .add => "+=",
        .sub => "-=",
        .mul => "*=",
        .div => "/=",
        .mod => "%=",
        .pow => "**=",
        .shl => "<<=",
        .shr => ">>=",
        .ushr => ">>>=",
        .bitand => "&=",
        .bitor => "|=",
        .bitxor => "^=",
        .logical_and => "&&=",
        .logical_or => "||=",
        .nullish => "??=",
    };
}
