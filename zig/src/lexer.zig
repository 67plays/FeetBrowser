//! Hand-written JavaScript lexer.
//!
//! Produces one token at a time.  Two things make a JS lexer awkward and both
//! are handled here:
//!
//!   * `/` is either division or the start of a regex literal.  We decide with
//!     the previous-significant-token rule that small engines use, refined for
//!     `)` (we remember whether the matching `(` belonged to an `if`/`while`/
//!     `for`/`with` head) and for `}` (always treated as statement-ish, so a
//!     regex may follow).
//!   * Template literals nest arbitrarily.  A backtick is scanned in one go
//!     into a `TemplateData` (cooked chunks plus source spans for the `${}`
//!     holes); the parser re-seeks the lexer into those spans.  Scanning the
//!     holes reuses the lexer itself, so nested templates, regexes and object
//!     literals inside `${}` all work.
//!
//! The lexer never panics.  Anything malformed comes back as a `.invalid`
//! token whose `str` is the message.

const std = @import("std");

pub const T = enum {
    eof,
    invalid,
    ident,
    num,
    str,
    tmpl,
    regex,

    lbrace,
    rbrace,
    lparen,
    rparen,
    lbracket,
    rbracket,
    semi,
    comma,
    dot,
    ellipsis,
    colon,
    question,
    opt_chain, // ?.
    arrow, // =>

    plus,
    minus,
    star,
    slash,
    percent,
    starstar,
    plusplus,
    minusminus,

    shl,
    shr,
    ushr,

    lt,
    gt,
    le,
    ge,
    eq,
    ne,
    seq,
    sne,

    amp,
    pipe,
    caret,
    bang,
    tilde,
    andand,
    oror,
    question2,

    assign,
    plus_a,
    minus_a,
    star_a,
    slash_a,
    percent_a,
    starstar_a,
    shl_a,
    shr_a,
    ushr_a,
    amp_a,
    pipe_a,
    caret_a,
    andand_a,
    oror_a,
    question2_a,
};

pub const K = enum {
    none,
    kvar,
    klet,
    kconst,
    kfunction,
    kreturn,
    kif,
    kelse,
    kfor,
    kwhile,
    kdo,
    kswitch,
    kcase,
    kdefault,
    kbreak,
    kcontinue,
    kthrow,
    ktry,
    kcatch,
    kfinally,
    knew,
    kdelete,
    ktypeof,
    kinstanceof,
    kin,
    kof,
    kvoid,
    kthis,
    ksuper,
    kclass,
    kextends,
    knull,
    ktrue,
    kfalse,
    kasync,
    kawait,
    kyield,
    kstatic,
    kget,
    kset,
    kwith,
    kdebugger,
};

const keywords = std.StaticStringMap(K).initComptime(.{
    .{ "var", K.kvar },
    .{ "let", K.klet },
    .{ "const", K.kconst },
    .{ "function", K.kfunction },
    .{ "return", K.kreturn },
    .{ "if", K.kif },
    .{ "else", K.kelse },
    .{ "for", K.kfor },
    .{ "while", K.kwhile },
    .{ "do", K.kdo },
    .{ "switch", K.kswitch },
    .{ "case", K.kcase },
    .{ "default", K.kdefault },
    .{ "break", K.kbreak },
    .{ "continue", K.kcontinue },
    .{ "throw", K.kthrow },
    .{ "try", K.ktry },
    .{ "catch", K.kcatch },
    .{ "finally", K.kfinally },
    .{ "new", K.knew },
    .{ "delete", K.kdelete },
    .{ "typeof", K.ktypeof },
    .{ "instanceof", K.kinstanceof },
    .{ "in", K.kin },
    .{ "of", K.kof },
    .{ "void", K.kvoid },
    .{ "this", K.kthis },
    .{ "super", K.ksuper },
    .{ "class", K.kclass },
    .{ "extends", K.kextends },
    .{ "null", K.knull },
    .{ "true", K.ktrue },
    .{ "false", K.kfalse },
    .{ "async", K.kasync },
    .{ "await", K.kawait },
    .{ "yield", K.kyield },
    .{ "static", K.kstatic },
    .{ "get", K.kget },
    .{ "set", K.kset },
    .{ "with", K.kwith },
    .{ "debugger", K.kdebugger },
});

/// Reserved words that may not be used as a plain variable name.  Contextual
/// keywords (`let`, `of`, `get`, `set`, `static`, `async`, `await`, `yield`)
/// are deliberately absent.
pub fn isReserved(k: K) bool {
    return switch (k) {
        .none, .klet, .kof, .kget, .kset, .kstatic, .kasync, .kawait, .kyield => false,
        else => true,
    };
}

pub const Span = struct {
    start: usize,
    end: usize,
    line: u32,
    col: u32,
};

pub const TemplateData = struct {
    /// n+1 cooked chunks.
    quasis: [][]const u8,
    /// n source spans, one per `${}` hole.
    spans: []Span,
};

pub const Token = struct {
    type: T = .eof,
    kw: K = .none,
    start: usize = 0,
    end: usize = 0,
    line: u32 = 1,
    col: u32 = 1,
    /// A line terminator (or a block comment containing one) preceded this
    /// token.  Drives automatic semicolon insertion.
    nl_before: bool = false,
    num: f64 = 0,
    /// identifier name / cooked string / regex pattern / error message
    str: []const u8 = "",
    /// regex flags
    flags: []const u8 = "",
    /// index into `Lexer.templates`
    tmpl: u32 = 0,
};

const MAX_PAREN = 256;

pub const LexError = error{ OutOfMemory, LexFailed };

pub const Lexer = struct {
    source: []const u8,
    arena: std.mem.Allocator,
    templates: *std.ArrayList(TemplateData),

    pos: usize = 0,
    line: u32 = 1,
    col: u32 = 1,

    prev: T = .eof,
    prev_kw: K = .none,

    // Whether each open paren belongs to an `if (`/`while (`/`for (`/`with (`.
    paren_ctrl: [MAX_PAREN]bool = [_]bool{false} ** MAX_PAREN,
    paren_depth: u32 = 0,
    last_paren_ctrl: bool = false,

    err_msg: ?[]const u8 = null,
    err_line: u32 = 1,
    err_col: u32 = 1,

    // guards against runaway recursion when scanning nested templates
    tmpl_depth: u32 = 0,

    pub fn init(arena: std.mem.Allocator, source: []const u8, templates: *std.ArrayList(TemplateData)) Lexer {
        return .{ .source = source, .arena = arena, .templates = templates };
    }

    pub fn next(self: *Lexer) Token {
        const before_line = self.line;
        const before_pos = self.pos;
        return self.nextInner() catch {
            return .{
                .type = .invalid,
                .start = before_pos,
                .end = self.pos,
                .line = self.err_line,
                .col = self.err_col,
                .str = self.err_msg orelse "out of memory",
                .nl_before = before_line != self.line,
            };
        };
    }

    fn lexErr(self: *Lexer, msg: []const u8) LexError {
        if (self.err_msg == null) {
            self.err_msg = msg;
            self.err_line = self.line;
            self.err_col = self.col;
        }
        return error.LexFailed;
    }

    fn adv(self: *Lexer, n: usize) void {
        self.pos += n;
        self.col += @intCast(n);
    }

    fn at(self: *const Lexer, off: usize) u8 {
        const i = self.pos + off;
        if (i >= self.source.len) return 0;
        return self.source[i];
    }

    fn eol(self: *const Lexer, off: usize) bool {
        const i = self.pos + off;
        if (i >= self.source.len) return false;
        const c = self.source[i];
        if (c == '\n' or c == '\r') return true;
        // U+2028 / U+2029
        if (c == 0xE2 and i + 2 < self.source.len and self.source[i + 1] == 0x80 and
            (self.source[i + 2] == 0xA8 or self.source[i + 2] == 0xA9)) return true;
        return false;
    }

    /// Consumes one line terminator (handles CRLF and the 3-byte separators).
    fn eatNewline(self: *Lexer) void {
        const c = self.at(0);
        if (c == '\r') {
            self.pos += 1;
            if (self.at(0) == '\n') self.pos += 1;
        } else if (c == 0xE2) {
            self.pos += 3;
        } else {
            self.pos += 1;
        }
        self.line += 1;
        self.col = 1;
    }

    fn skipTrivia(self: *Lexer) LexError!bool {
        var nl = false;
        while (self.pos < self.source.len) {
            const c = self.source[self.pos];
            if (c == ' ' or c == '\t' or c == 0x0B or c == 0x0C) {
                self.adv(1);
                continue;
            }
            if (c == 0xEF and self.at(1) == 0xBB and self.at(2) == 0xBF) { // BOM
                self.adv(3);
                continue;
            }
            if (c == 0xC2 and self.at(1) == 0xA0) { // NBSP
                self.adv(2);
                continue;
            }
            if (self.eol(0)) {
                nl = true;
                self.eatNewline();
                continue;
            }
            if (c == '/' and self.at(1) == '/') {
                self.adv(2);
                while (self.pos < self.source.len and !self.eol(0)) self.adv(1);
                continue;
            }
            if (c == '/' and self.at(1) == '*') {
                self.adv(2);
                var closed = false;
                while (self.pos < self.source.len) {
                    if (self.at(0) == '*' and self.at(1) == '/') {
                        self.adv(2);
                        closed = true;
                        break;
                    }
                    if (self.eol(0)) {
                        nl = true;
                        self.eatNewline();
                    } else self.adv(1);
                }
                if (!closed) return self.lexErr("unterminated comment");
                continue;
            }
            // `<!--` html-comment-as-line-comment, seen in ancient inline scripts
            if (c == '<' and self.at(1) == '!' and self.at(2) == '-' and self.at(3) == '-') {
                self.adv(4);
                while (self.pos < self.source.len and !self.eol(0)) self.adv(1);
                continue;
            }
            break;
        }
        return nl;
    }

    pub fn regexAllowed(self: *const Lexer) bool {
        return switch (self.prev) {
            .num, .str, .tmpl, .regex, .rbracket, .plusplus, .minusminus => false,
            .rparen => self.last_paren_ctrl,
            .rbrace => true,
            .ident => switch (self.prev_kw) {
                .none, .kthis, .ksuper, .ktrue, .kfalse, .knull, .klet, .kof, .kget, .kset, .kstatic => false,
                else => true,
            },
            else => true,
        };
    }

    fn nextInner(self: *Lexer) LexError!Token {
        const nl = try self.skipTrivia();
        var tok = Token{
            .start = self.pos,
            .end = self.pos,
            .line = self.line,
            .col = self.col,
            .nl_before = nl,
        };
        if (self.pos >= self.source.len) {
            tok.type = .eof;
            self.prev = .eof;
            self.prev_kw = .none;
            return tok;
        }
        const c = self.source[self.pos];

        if (isIdentStart(c)) {
            const s = self.pos;
            while (self.pos < self.source.len and isIdentPart(self.source[self.pos])) self.adv(1);
            tok.type = .ident;
            tok.str = self.source[s..self.pos];
            tok.kw = keywords.get(tok.str) orelse .none;
            tok.end = self.pos;
            self.prev = tok.type;
            self.prev_kw = tok.kw;
            return tok;
        }

        if (c >= '0' and c <= '9') {
            try self.scanNumber(&tok);
            self.prev = tok.type;
            self.prev_kw = .none;
            return tok;
        }
        if (c == '.' and self.at(1) >= '0' and self.at(1) <= '9') {
            try self.scanNumber(&tok);
            self.prev = tok.type;
            self.prev_kw = .none;
            return tok;
        }

        if (c == '"' or c == '\'') {
            try self.scanString(&tok, c);
            self.prev = tok.type;
            self.prev_kw = .none;
            return tok;
        }

        if (c == '`') {
            self.adv(1);
            try self.scanTemplate(&tok);
            self.prev = tok.type;
            self.prev_kw = .none;
            return tok;
        }

        if (c == '/' and self.regexAllowed()) {
            try self.scanRegex(&tok);
            self.prev = tok.type;
            self.prev_kw = .none;
            return tok;
        }

        try self.scanPunct(&tok);
        self.prev = tok.type;
        self.prev_kw = .none;
        return tok;
    }

    fn scanPunct(self: *Lexer, tok: *Token) LexError!void {
        const c = self.at(0);
        const c1 = self.at(1);
        const c2 = self.at(2);
        var ty: T = .invalid;
        var n: usize = 1;
        switch (c) {
            '{' => ty = .lbrace,
            '}' => ty = .rbrace,
            '(' => ty = .lparen,
            ')' => ty = .rparen,
            '[' => ty = .lbracket,
            ']' => ty = .rbracket,
            ';' => ty = .semi,
            ',' => ty = .comma,
            ':' => ty = .colon,
            '~' => ty = .tilde,
            '.' => {
                if (c1 == '.' and c2 == '.') {
                    ty = .ellipsis;
                    n = 3;
                } else ty = .dot;
            },
            '?' => {
                if (c1 == '.' and !(c2 >= '0' and c2 <= '9')) {
                    ty = .opt_chain;
                    n = 2;
                } else if (c1 == '?' and c2 == '=') {
                    ty = .question2_a;
                    n = 3;
                } else if (c1 == '?') {
                    ty = .question2;
                    n = 2;
                } else ty = .question;
            },
            '+' => {
                if (c1 == '+') {
                    ty = .plusplus;
                    n = 2;
                } else if (c1 == '=') {
                    ty = .plus_a;
                    n = 2;
                } else ty = .plus;
            },
            '-' => {
                if (c1 == '-') {
                    ty = .minusminus;
                    n = 2;
                } else if (c1 == '=') {
                    ty = .minus_a;
                    n = 2;
                } else ty = .minus;
            },
            '*' => {
                if (c1 == '*' and c2 == '=') {
                    ty = .starstar_a;
                    n = 3;
                } else if (c1 == '*') {
                    ty = .starstar;
                    n = 2;
                } else if (c1 == '=') {
                    ty = .star_a;
                    n = 2;
                } else ty = .star;
            },
            '/' => {
                if (c1 == '=') {
                    ty = .slash_a;
                    n = 2;
                } else ty = .slash;
            },
            '%' => {
                if (c1 == '=') {
                    ty = .percent_a;
                    n = 2;
                } else ty = .percent;
            },
            '=' => {
                if (c1 == '=' and c2 == '=') {
                    ty = .seq;
                    n = 3;
                } else if (c1 == '=') {
                    ty = .eq;
                    n = 2;
                } else if (c1 == '>') {
                    ty = .arrow;
                    n = 2;
                } else ty = .assign;
            },
            '!' => {
                if (c1 == '=' and c2 == '=') {
                    ty = .sne;
                    n = 3;
                } else if (c1 == '=') {
                    ty = .ne;
                    n = 2;
                } else ty = .bang;
            },
            '<' => {
                if (c1 == '<' and c2 == '=') {
                    ty = .shl_a;
                    n = 3;
                } else if (c1 == '<') {
                    ty = .shl;
                    n = 2;
                } else if (c1 == '=') {
                    ty = .le;
                    n = 2;
                } else ty = .lt;
            },
            '>' => {
                if (c1 == '>' and c2 == '>' and self.at(3) == '=') {
                    ty = .ushr_a;
                    n = 4;
                } else if (c1 == '>' and c2 == '>') {
                    ty = .ushr;
                    n = 3;
                } else if (c1 == '>' and c2 == '=') {
                    ty = .shr_a;
                    n = 3;
                } else if (c1 == '>') {
                    ty = .shr;
                    n = 2;
                } else if (c1 == '=') {
                    ty = .ge;
                    n = 2;
                } else ty = .gt;
            },
            '&' => {
                if (c1 == '&' and c2 == '=') {
                    ty = .andand_a;
                    n = 3;
                } else if (c1 == '&') {
                    ty = .andand;
                    n = 2;
                } else if (c1 == '=') {
                    ty = .amp_a;
                    n = 2;
                } else ty = .amp;
            },
            '|' => {
                if (c1 == '|' and c2 == '=') {
                    ty = .oror_a;
                    n = 3;
                } else if (c1 == '|') {
                    ty = .oror;
                    n = 2;
                } else if (c1 == '=') {
                    ty = .pipe_a;
                    n = 2;
                } else ty = .pipe;
            },
            '^' => {
                if (c1 == '=') {
                    ty = .caret_a;
                    n = 2;
                } else ty = .caret;
            },
            else => return self.lexErr("unexpected character"),
        }

        if (ty == .lparen) {
            const ctrl = self.prev == .ident and switch (self.prev_kw) {
                .kif, .kwhile, .kfor, .kwith => true,
                else => false,
            };
            if (self.paren_depth < MAX_PAREN) self.paren_ctrl[self.paren_depth] = ctrl;
            self.paren_depth += 1;
        } else if (ty == .rparen) {
            if (self.paren_depth > 0) self.paren_depth -= 1;
            self.last_paren_ctrl = if (self.paren_depth < MAX_PAREN) self.paren_ctrl[self.paren_depth] else false;
        }

        self.adv(n);
        tok.type = ty;
        tok.end = self.pos;
    }

    fn scanNumber(self: *Lexer, tok: *Token) LexError!void {
        const start = self.pos;
        tok.type = .num;
        var value: f64 = 0;

        if (self.at(0) == '0') {
            const r: ?u8 = switch (self.at(1)) {
                'x', 'X' => 16,
                'o', 'O' => 8,
                'b', 'B' => 2,
                else => null,
            };
            if (r) |radix| {
                self.adv(2);
                var any = false;
                while (self.pos < self.source.len) {
                    const ch = self.source[self.pos];
                    if (ch == '_') {
                        self.adv(1);
                        continue;
                    }
                    const d = digitVal(ch) orelse break;
                    if (d >= radix) break;
                    value = value * @as(f64, @floatFromInt(radix)) + @as(f64, @floatFromInt(d));
                    any = true;
                    self.adv(1);
                }
                if (!any) return self.lexErr("invalid numeric literal");
                if (self.at(0) == 'n') self.adv(1);
                tok.num = value;
                tok.end = self.pos;
                return;
            }
        }

        var buf = std.ArrayList(u8).init(self.arena);
        const leading_zero = self.at(0) == '0';
        var saw_dot = false;
        var saw_exp = false;
        var saw_89 = false;
        while (self.pos < self.source.len) {
            const ch = self.source[self.pos];
            if (ch == '_') {
                self.adv(1);
                continue;
            }
            if (ch >= '0' and ch <= '9') {
                if (ch == '8' or ch == '9') saw_89 = true;
                try buf.append(ch);
                self.adv(1);
                continue;
            }
            if (ch == '.' and !saw_dot and !saw_exp) {
                saw_dot = true;
                try buf.append(ch);
                self.adv(1);
                continue;
            }
            if ((ch == 'e' or ch == 'E') and !saw_exp) {
                const n1 = self.at(1);
                const n2 = self.at(2);
                const ok = (n1 >= '0' and n1 <= '9') or ((n1 == '+' or n1 == '-') and n2 >= '0' and n2 <= '9');
                if (!ok) break;
                saw_exp = true;
                try buf.append(ch);
                self.adv(1);
                if (self.at(0) == '+' or self.at(0) == '-') {
                    try buf.append(self.at(0));
                    self.adv(1);
                }
                continue;
            }
            break;
        }
        if (self.at(0) == 'n') self.adv(1);

        if (buf.items.len == 0) return self.lexErr("invalid numeric literal");

        if (leading_zero and buf.items.len > 1 and !saw_dot and !saw_exp and !saw_89) {
            // legacy octal, e.g. 0755
            value = 0;
            for (buf.items[1..]) |ch| value = value * 8 + @as(f64, @floatFromInt(ch - '0'));
        } else {
            var cleaned = std.ArrayList(u8).init(self.arena);
            if (buf.items[0] == '.') try cleaned.append('0');
            try cleaned.appendSlice(buf.items);
            if (cleaned.items[cleaned.items.len - 1] == '.') try cleaned.append('0');
            value = std.fmt.parseFloat(f64, cleaned.items) catch
                return self.lexErr("invalid numeric literal");
        }
        tok.num = value;
        tok.start = start;
        tok.end = self.pos;
    }

    fn scanString(self: *Lexer, tok: *Token, quote: u8) LexError!void {
        const start = self.pos;
        self.adv(1);
        var buf = std.ArrayList(u8).init(self.arena);
        var simple_start = self.pos;
        var escaped = false;
        while (true) {
            if (self.pos >= self.source.len) return self.lexErr("unterminated string literal");
            const ch = self.source[self.pos];
            if (ch == quote) {
                if (!escaped) {
                    tok.str = self.source[simple_start..self.pos];
                } else {
                    try buf.appendSlice(self.source[simple_start..self.pos]);
                    tok.str = try buf.toOwnedSlice();
                }
                self.adv(1);
                break;
            }
            if (self.eol(0)) return self.lexErr("unterminated string literal");
            if (ch == '\\') {
                try buf.appendSlice(self.source[simple_start..self.pos]);
                escaped = true;
                try self.readEscape(&buf);
                simple_start = self.pos;
                continue;
            }
            self.adv(1);
        }
        tok.type = .str;
        tok.start = start;
        tok.end = self.pos;
    }

    /// Consumes a backslash escape and appends the cooked bytes.
    fn readEscape(self: *Lexer, buf: *std.ArrayList(u8)) LexError!void {
        self.adv(1); // the backslash
        if (self.pos >= self.source.len) return self.lexErr("unterminated escape sequence");
        if (self.eol(0)) { // line continuation
            self.eatNewline();
            return;
        }
        const ch = self.source[self.pos];
        switch (ch) {
            'n' => {
                try buf.append('\n');
                self.adv(1);
            },
            't' => {
                try buf.append('\t');
                self.adv(1);
            },
            'r' => {
                try buf.append('\r');
                self.adv(1);
            },
            'b' => {
                try buf.append(0x08);
                self.adv(1);
            },
            'f' => {
                try buf.append(0x0C);
                self.adv(1);
            },
            'v' => {
                try buf.append(0x0B);
                self.adv(1);
            },
            '0'...'7' => {
                // \0 is NUL; legacy octal escapes otherwise
                var val: u32 = 0;
                var count: u32 = 0;
                const max: u32 = if (ch <= '3') 3 else 2;
                while (count < max and self.pos < self.source.len) {
                    const d = self.source[self.pos];
                    if (d < '0' or d > '7') break;
                    val = val * 8 + (d - '0');
                    self.adv(1);
                    count += 1;
                }
                try encodeCp(buf, @intCast(val & 0x10FFFF));
            },
            'x' => {
                self.adv(1);
                var val: u32 = 0;
                var i: u32 = 0;
                while (i < 2) : (i += 1) {
                    if (self.pos >= self.source.len) return self.lexErr("invalid \\x escape");
                    const d = digitVal(self.source[self.pos]) orelse return self.lexErr("invalid \\x escape");
                    if (d >= 16) return self.lexErr("invalid \\x escape");
                    val = val * 16 + d;
                    self.adv(1);
                }
                try encodeCp(buf, @intCast(val));
            },
            'u' => {
                self.adv(1);
                if (self.at(0) == '{') {
                    self.adv(1);
                    var val: u32 = 0;
                    var any = false;
                    while (self.pos < self.source.len and self.source[self.pos] != '}') {
                        const d = digitVal(self.source[self.pos]) orelse return self.lexErr("invalid unicode escape");
                        if (d >= 16) return self.lexErr("invalid unicode escape");
                        val = val * 16 + d;
                        if (val > 0x10FFFF) return self.lexErr("unicode escape out of range");
                        any = true;
                        self.adv(1);
                    }
                    if (!any or self.at(0) != '}') return self.lexErr("invalid unicode escape");
                    self.adv(1);
                    try encodeCp(buf, @intCast(val));
                } else {
                    const hi = try self.read4Hex();
                    if (hi >= 0xD800 and hi <= 0xDBFF and self.at(0) == '\\' and self.at(1) == 'u' and self.at(2) != '{') {
                        const save_pos = self.pos;
                        const save_col = self.col;
                        self.adv(2);
                        const lo = self.read4Hex() catch {
                            self.pos = save_pos;
                            self.col = save_col;
                            self.err_msg = null;
                            try encodeCp(buf, @intCast(hi));
                            return;
                        };
                        if (lo >= 0xDC00 and lo <= 0xDFFF) {
                            const cp = 0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00);
                            try encodeCp(buf, @intCast(cp));
                        } else {
                            try encodeCp(buf, @intCast(hi));
                            try encodeCp(buf, @intCast(lo));
                        }
                    } else {
                        try encodeCp(buf, @intCast(hi));
                    }
                }
            },
            else => {
                // unknown escape: the character itself (copy whole utf8 unit)
                var n: usize = 1;
                if (ch >= 0xF0) n = 4 else if (ch >= 0xE0) n = 3 else if (ch >= 0xC0) n = 2;
                if (self.pos + n > self.source.len) n = 1;
                try buf.appendSlice(self.source[self.pos .. self.pos + n]);
                self.adv(n);
            },
        }
    }

    fn read4Hex(self: *Lexer) LexError!u32 {
        var val: u32 = 0;
        var i: u32 = 0;
        while (i < 4) : (i += 1) {
            if (self.pos >= self.source.len) return self.lexErr("invalid unicode escape");
            const d = digitVal(self.source[self.pos]) orelse return self.lexErr("invalid unicode escape");
            if (d >= 16) return self.lexErr("invalid unicode escape");
            val = val * 16 + d;
            self.adv(1);
        }
        return val;
    }

    fn scanRegex(self: *Lexer, tok: *Token) LexError!void {
        const start = self.pos;
        self.adv(1); // '/'
        const body_start = self.pos;
        var in_class = false;
        while (true) {
            if (self.pos >= self.source.len or self.eol(0)) return self.lexErr("unterminated regular expression");
            const ch = self.source[self.pos];
            if (ch == '\\') {
                self.adv(1);
                if (self.pos >= self.source.len or self.eol(0)) return self.lexErr("unterminated regular expression");
                self.adv(1);
                continue;
            }
            if (ch == '[') {
                in_class = true;
                self.adv(1);
                continue;
            }
            if (ch == ']') {
                in_class = false;
                self.adv(1);
                continue;
            }
            if (ch == '/' and !in_class) break;
            self.adv(1);
        }
        tok.str = self.source[body_start..self.pos];
        self.adv(1); // closing '/'
        const flag_start = self.pos;
        while (self.pos < self.source.len and isIdentPart(self.source[self.pos])) self.adv(1);
        tok.flags = self.source[flag_start..self.pos];
        tok.type = .regex;
        tok.start = start;
        tok.end = self.pos;
    }

    /// `self.pos` is just past the opening backtick.
    fn scanTemplate(self: *Lexer, tok: *Token) LexError!void {
        if (self.tmpl_depth > 64) return self.lexErr("template literals nested too deeply");
        var quasis = std.ArrayList([]const u8).init(self.arena);
        var spans = std.ArrayList(Span).init(self.arena);
        var buf = std.ArrayList(u8).init(self.arena);

        while (true) {
            if (self.pos >= self.source.len) return self.lexErr("unterminated template literal");
            const ch = self.source[self.pos];
            if (ch == '`') {
                self.adv(1);
                break;
            }
            if (ch == '\\') {
                try self.readEscape(&buf);
                continue;
            }
            if (ch == '$' and self.at(1) == '{') {
                try quasis.append(try buf.toOwnedSlice());
                self.adv(2);
                const s_start = self.pos;
                const s_line = self.line;
                const s_col = self.col;

                var sub = self.*;
                sub.tmpl_depth = self.tmpl_depth + 1;
                sub.prev = .eof;
                sub.prev_kw = .none;
                sub.paren_depth = 0;
                var depth: i32 = 0;
                var end_pos: usize = 0;
                var guard: u32 = 0;
                while (true) {
                    guard += 1;
                    if (guard > 2_000_000) return self.lexErr("unterminated template literal");
                    const t = sub.next();
                    if (t.type == .eof) return self.lexErr("unterminated template literal");
                    if (t.type == .invalid) {
                        self.err_msg = t.str;
                        self.err_line = t.line;
                        self.err_col = t.col;
                        return error.LexFailed;
                    }
                    switch (t.type) {
                        .lbrace => depth += 1,
                        .rbrace => {
                            if (depth == 0) {
                                end_pos = t.start;
                                break;
                            }
                            depth -= 1;
                        },
                        else => {},
                    }
                }
                try spans.append(.{ .start = s_start, .end = end_pos, .line = s_line, .col = s_col });
                self.pos = sub.pos;
                self.line = sub.line;
                self.col = sub.col;
                continue;
            }
            if (self.eol(0)) {
                // template line terminators are normalised to \n
                self.eatNewline();
                try buf.append('\n');
                continue;
            }
            try buf.append(ch);
            self.adv(1);
        }
        try quasis.append(try buf.toOwnedSlice());

        const idx: u32 = @intCast(self.templates.items.len);
        try self.templates.append(.{
            .quasis = try quasis.toOwnedSlice(),
            .spans = try spans.toOwnedSlice(),
        });
        tok.type = .tmpl;
        tok.tmpl = idx;
        tok.end = self.pos;
    }
};

pub fn isIdentStart(c: u8) bool {
    return (c >= 'a' and c <= 'z') or (c >= 'A' and c <= 'Z') or c == '_' or c == '$' or c == '#' or c >= 0x80;
}

pub fn isIdentPart(c: u8) bool {
    return isIdentStart(c) or (c >= '0' and c <= '9');
}

fn digitVal(c: u8) ?u32 {
    if (c >= '0' and c <= '9') return c - '0';
    if (c >= 'a' and c <= 'f') return c - 'a' + 10;
    if (c >= 'A' and c <= 'F') return c - 'A' + 10;
    return null;
}

/// UTF-8 encoder that tolerates lone surrogates (WTF-8), which JS strings can
/// legitimately contain.
fn encodeCp(buf: *std.ArrayList(u8), cp: u21) !void {
    const v: u32 = cp;
    if (v < 0x80) {
        try buf.append(@intCast(v));
    } else if (v < 0x800) {
        try buf.append(@intCast(0xC0 | (v >> 6)));
        try buf.append(@intCast(0x80 | (v & 0x3F)));
    } else if (v < 0x10000) {
        try buf.append(@intCast(0xE0 | (v >> 12)));
        try buf.append(@intCast(0x80 | ((v >> 6) & 0x3F)));
        try buf.append(@intCast(0x80 | (v & 0x3F)));
    } else {
        try buf.append(@intCast(0xF0 | (v >> 18)));
        try buf.append(@intCast(0x80 | ((v >> 12) & 0x3F)));
        try buf.append(@intCast(0x80 | ((v >> 6) & 0x3F)));
        try buf.append(@intCast(0x80 | (v & 0x3F)));
    }
}
