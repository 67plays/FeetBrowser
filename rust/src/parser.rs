//! Recursive-descent parser ported from `jsengine.py::_Parser`.

use crate::ast::*;
use crate::ast::Node::*;
use crate::token::*;
use crate::value::JsError;
use std::collections::BTreeMap;
use std::rc::Rc;

pub struct Parser {
    source: String,
    tokens: Vec<Token>,
    pos: usize,
    async_depth: usize,
    generator_depth: usize,
}

type PResult = Result<Rc<Node>, JsError>;

/// A parsed parameter list.
///
/// `function f({a, b: [c]}, d)` binds three names from a slot that has no name
/// at all, and the interpreter's parameter binder only knows how to put one
/// value under one name. Rather than teach it patterns -- which would mean
/// threading `DeclTarget` through every function-shaped node, the call path
/// and the `arguments` object -- the parser gives each pattern a parameter
/// name of its own and records the unpacking to be done once the call has
/// bound it. `let {a, b: [c]} = <slot>;` is then the first statement of the
/// body, and every part of destructuring the engine already supports for
/// `let {a} = x` works here for free, defaults and nesting and rest included.
struct Params {
    names: Vec<String>,
    defaults: BTreeMap<String, Rc<Node>>,
    rest: Option<String>,
    /// `(pattern, name of the synthetic parameter holding the value)`.
    unpack: Vec<(DeclTarget, String)>,
}

impl Params {
    /// The parameter list of `x => ...`, which needs no brackets and so never
    /// goes through `param_list`.
    fn one(name: String) -> Params {
        Params {
            names: vec![name],
            defaults: BTreeMap::new(),
            rest: None,
            unpack: Vec::new(),
        }
    }
}

/// The name given to the nth pattern parameter of a function. The leading
/// space is the point: the tokenizer cannot produce an identifier containing
/// one, so no name a page can write will ever collide with it, and a stray
/// reference to one shows up in a trace as obviously ours.
fn synthetic_param(n: usize) -> String {
    format!(" arg{n}")
}

impl Parser {
    pub fn new(source: &str) -> Result<Parser, JsError> {
        let tokens = tokenize(source)?;
        Ok(Parser {
            source: source.to_string(),
            tokens,
            pos: 0,
            async_depth: 0,
            generator_depth: 0,
        })
    }

    fn peek(&self) -> Option<&Token> {
        self.tokens.get(self.pos)
    }

    fn peek2(&self) -> Option<&Token> {
        self.tokens.get(self.pos + 1)
    }

    fn peek_n(&self, n: usize) -> Option<&Token> {
        self.tokens.get(self.pos + n)
    }

    fn peek2_is_punct(&self, text: &str) -> bool {
        self.peek2().map_or(false, |t| t.kind == TokKind::Punct && t.text == text)
    }

    /// Whether the third token from here is `=>`, which is what tells
    /// `async n => n` apart from a variable that happens to be called `async`.
    ///
    /// `peek2` is the second token, so the third is `peek_n(2)`; this asked for
    /// `peek_n(3)` and so tested the token *after* the arrow. The effect was
    /// not a syntax error but something worse: `async` came back as a plain
    /// identifier, automatic semicolon insertion split `var f = async n => n`
    /// into two statements that both parse, and `f` was quietly left
    /// undefined. It only became visible where ASI could not paper over it,
    /// as in `p.then(async n => ...)`, which is how vimeo.com writes it.
    fn peek3_is_arrow(&self) -> bool {
        self.peek_n(2).map_or(false, |t| t.kind == TokKind::Punct && t.text == "=>")
    }

    /// Whether the token after next could begin a property name. This is how
    /// `{ get v() {} }` is told apart from `{ get: 1 }` and `{ get }`: in the
    /// accessor form a name follows `get`, in the others a punctuator does.
    fn peek2_is_property_name(&self) -> bool {
        self.peek2().map_or(false, |t| {
            matches!(
                t.kind,
                TokKind::Ident | TokKind::Kw | TokKind::Str | TokKind::Number
            )
        })
    }

    fn peek_is_punct(&self, text: &str) -> bool {
        self.peek().map_or(false, |t| t.kind == TokKind::Punct && t.text == text)
    }

    /// The body of the template at the cursor, if there is one there. The
    /// token is left where it is; the caller decides whether to take it.
    fn peek_template(&self) -> Option<String> {
        let t = self.peek()?;
        match (&t.kind, &t.payload) {
            (TokKind::Template, TokPayload::Str(s)) => Some(s.clone()),
            _ => None,
        }
    }

    fn match_punct(&mut self, text: &str) -> bool {
        if self.peek_is_punct(text) {
            self.pos += 1;
            true
        } else {
            false
        }
    }

    fn match_kw(&mut self, text: &str) -> bool {
        if self.peek().map_or(false, |t| t.kind == TokKind::Kw && t.text == text) {
            self.pos += 1;
            true
        } else {
            false
        }
    }

    fn expect_punct(&mut self, text: &str) -> PResult {
        if !self.match_punct(text) {
            self.syntax(&format!("expected '{text}'"))
        } else {
            Ok(rc(Literal(LiteralVal::Undefined)))
        }
    }

    fn match_ident(&mut self) -> Option<String> {
        if let Some(t) = self.peek() {
            if t.kind == TokKind::Ident {
                let text = t.text.clone();
                self.pos += 1;
                return Some(text);
            }
        }
        None
    }

    fn expect_ident(&mut self) -> Result<String, JsError> {
        match self.match_ident() {
            Some(n) => Ok(n),
            None => self.syntax("expected identifier"),
        }
    }

    fn expect_property_name(&mut self) -> Result<String, JsError> {
        if let Some(t) = self.peek() {
            if t.kind == TokKind::Ident || t.kind == TokKind::Kw {
                let text = t.text.clone();
                self.pos += 1;
                return Ok(text);
            }
        }
        self.syntax("expected property name")
    }

    /// The name of a class member, which is a property name, a string or
    /// number literal, or `[expr]` -- and the last of those is not optional
    /// decoration: `[Symbol.iterator]() {}` and `[Symbol.toPrimitive](hint)
    /// {}` are how a class joins the protocols the language spells with
    /// symbols, and both turn up in shipped bundles.
    fn member_name(&mut self) -> Result<(String, Option<Rc<Node>>), JsError> {
        if self.match_punct("[") {
            let expr = self.assign()?;
            self.expect_punct("]")?;
            return Ok((String::new(), Some(expr)));
        }
        if let Some(t) = self.peek() {
            // A quoted or numeric member name is the string it denotes, the
            // same as it would be in an object literal.
            if matches!(t.kind, TokKind::Str | TokKind::Number) {
                let text = match &t.payload {
                    TokPayload::Str(s) => s.clone(),
                    _ => t.text.clone(),
                };
                self.pos += 1;
                return Ok((text, None));
            }
        }
        Ok((self.expect_property_name()?, None))
    }

    /// Whether the next token could begin an expression. Only `yield` asks,
    /// and only to tell `yield x` from the bare `yield` that a `)`, `]`, `}`,
    /// `,` or `;` closes.
    fn starts_expression(&self) -> bool {
        match self.peek() {
            None => false,
            Some(t) => {
                t.kind != TokKind::Punct
                    || !matches!(t.text.as_str(), ")" | "]" | "}" | "," | ";" | ":")
            }
        }
    }

    fn next_is_kw(&self, text: &str) -> bool {
        self.peek2().map_or(false, |t| t.kind == TokKind::Kw && t.text == text)
    }

    fn syntax<T>(&self, msg: &str) -> Result<T, JsError> {
        let offset = self.peek().map_or(self.source.len(), |t| t.offset);
        let line = self.source[..offset.min(self.source.len())].matches('\n').count() + 1;
        Err(JsError::js(format!(
            "SyntaxError on line {line}: {msg}{}",
            near(&self.source, offset)
        )))
    }

    // -- grammar ------------------------------------------------------------

    pub fn parse_program(&mut self) -> PResult {
        let stmts = self.parse_stmts_until(None)?;
        Ok(rc(Program(stmts)))
    }

    pub fn parse_expression(&mut self) -> PResult {
        self.expression()
    }

    fn statement(&mut self) -> PResult {
        if let Some(t) = self.peek() {
            if t.kind == TokKind::Ident && self.peek2_is_punct(":") {
                let name = t.text.clone();
                self.pos += 2;
                let body = self.statement()?;
                // A loop needs its own name to hand back to `continue name`;
                // everything else only ever sees a `break`, which the
                // Labelled node catches for itself.
                let body = label_loop(body, &name);
                return Ok(rc(Labelled { name, body }));
            }
        }
        if self.peek_is_punct(";") {
            // The empty statement. `if (a) ; else b()` is real code, and
            // `for (;;) ;` is how you spell a body-less loop.
            self.pos += 1;
            return Ok(rc(Block(Vec::new())));
        }
        if self.peek_is_punct("{") {
            return Ok(rc(Block(self.parse_stmts_until(Some("}"))?)));
        }
        if let Some(t) = self.peek() {
            if t.kind == TokKind::Kw {
                let text = t.text.clone();
                if let Some(node) = self.stmt_for_keyword(&text)? {
                    return Ok(node);
                }
            } else if t.kind == TokKind::Ident && t.text == "async" && self.next_is_kw("function")
            {
                self.pos += 2; // past `async` and `function`
                let f = self.function_declaration(true)?;
                return Ok(rc(FunctionDecl(f)));
            }
        }
        let expr = self.sequence()?;
        Ok(rc(ExprStmt(expr)))
    }

    fn stmt_for_keyword(&mut self, text: &str) -> Result<Option<Rc<Node>>, JsError> {
        let node = match text {
            "var" | "let" | "const" => {
                self.pos += 1;
                let decls = self.declaration_list()?;
                Some(rc(VarDecl { kind: text.to_string(), decls }))
            }
            "function" => {
                self.pos += 1;
                let f = self.function_declaration(false)?;
                Some(rc(FunctionDecl(f)))
            }
            "class" => {
                self.pos += 1;
                let c = self.class_declaration()?;
                Some(rc(ClassDecl(c)))
            }
            "return" => {
                self.pos += 1;
                Some(rc(Return(self.return_value()?)))
            }
            "if" => {
                self.pos += 1;
                Some(self.if_statement()?)
            }
            "while" => {
                self.pos += 1;
                Some(self.while_statement()?)
            }
            "do" => {
                self.pos += 1;
                Some(self.do_while_statement()?)
            }
            "switch" => {
                self.pos += 1;
                Some(self.switch_statement()?)
            }
            "for" => {
                self.pos += 1;
                Some(self.for_statement()?)
            }
            "break" => {
                self.pos += 1;
                Some(rc(Break(self.optional_label())))
            }
            "continue" => {
                self.pos += 1;
                Some(rc(Continue(self.optional_label())))
            }
            "throw" => {
                self.pos += 1;
                let expr = self.sequence()?;
                Some(rc(Throw(expr)))
            }
            "try" => {
                self.pos += 1;
                Some(self.try_statement()?)
            }
            _ => None,
        };
        Ok(node)
    }

    fn return_value(&mut self) -> Result<Option<Rc<Node>>, JsError> {
        if let Some(t) = self.peek() {
            if !(t.kind == TokKind::Punct && (t.text == ";" || t.text == "}")) {
                return Ok(Some(self.sequence()?));
            }
        }
        Ok(None)
    }

    /// The name after `break`/`continue`, when there is one on the line.
    ///
    /// A newline ends the statement instead (automatic semicolon insertion),
    /// so `break\nfoo()` breaks and then calls -- it does not break to a
    /// label named foo.
    fn optional_label(&mut self) -> Option<String> {
        let (name, offset) = match self.peek() {
            Some(t) if t.kind == TokKind::Ident => (t.text.clone(), t.offset),
            _ => return None,
        };
        let prev_end = if self.pos > 0 {
            let prev = &self.tokens[self.pos - 1];
            prev.offset + prev.text.len()
        } else {
            0
        };
        if self.source[prev_end.min(offset)..offset].contains('\n') {
            return None;
        }
        self.pos += 1;
        Some(name)
    }

    fn parse_stmts_until(&mut self, closing: Option<&str>) -> Result<Vec<Rc<Node>>, JsError> {
        if let Some(_c) = closing {
            self.expect_punct("{")?;
        }
        let mut stmts = Vec::new();
        loop {
            match self.peek() {
                None => {
                    if closing.is_some() {
                        return self.syntax(&format!("expected '{}'", closing.unwrap()));
                    }
                    break;
                }
                Some(t) if t.kind == TokKind::Punct && Some(t.text.as_str()) == closing => {
                    self.pos += 1;
                    break;
                }
                _ => {}
            }
            if self.match_punct(";") {
                continue;
            }
            stmts.push(self.statement()?);
            self.match_punct(";");
        }
        Ok(stmts)
    }

    fn declaration_list(&mut self) -> Result<Vec<(DeclTarget, Option<Rc<Node>>)>, JsError> {
        let mut decls = Vec::new();
        loop {
            let target = self.declaration_target()?;
            let value = if self.match_punct("=") {
                Some(self.expression()?)
            } else {
                None
            };
            decls.push((target, value));
            if !self.match_punct(",") {
                break;
            }
        }
        Ok(decls)
    }

    fn declaration_target(&mut self) -> Result<DeclTarget, JsError> {
        if let Some(t) = self.peek() {
            if t.kind == TokKind::Ident {
                let name = t.text.clone();
                self.pos += 1;
                return Ok(DeclTarget::Name(name));
            }
            if t.kind == TokKind::Punct && (t.text == "[" || t.text == "{") {
                return self.pattern();
            }
        }
        self.syntax("expected identifier")
    }

    fn pattern(&mut self) -> Result<DeclTarget, JsError> {
        if self.peek_is_punct("[") {
            self.pos += 1;
            let mut parts = Vec::new();
            let mut rest = None;
            loop {
                if self.match_punct("]") {
                    break;
                }
                if self.match_punct(",") {
                    continue;
                }
                if self.match_punct("...") {
                    rest = Some(self.pattern_target()?);
                    self.match_punct(",");
                    self.expect_punct("]")?;
                    break;
                }
                let target = self.pattern_target()?;
                let default = if self.match_punct("=") {
                    Some(self.assign()?)
                } else {
                    None
                };
                parts.push(PatternPart::Array { target, default });
                if !self.match_punct(",") {
                    self.expect_punct("]")?;
                    break;
                }
            }
            return Ok(DeclTarget::Pattern(Rc::new(PatternNode {
                kind: "array".to_string(),
                parts,
                rest,
            })));
        }
        self.expect_punct("{")?;
        let mut parts = Vec::new();
        let mut rest = None;
        loop {
            if self.match_punct("}") {
                break;
            }
            if self.match_punct(",") {
                continue;
            }
            if self.match_punct("...") {
                rest = Some(self.pattern_target()?);
                self.match_punct(",");
                self.expect_punct("}")?;
                break;
            }
            let (key, key_is_ident) = match self.peek() {
                Some(t)
                    if t.kind == TokKind::Ident
                        || t.kind == TokKind::Str
                        || t.kind == TokKind::Kw =>
                {
                    let k = t.text.clone();
                    let is_ident = t.kind == TokKind::Ident;
                    self.pos += 1;
                    (k, is_ident)
                }
                _ => return self.syntax("expected property name"),
            };
            let (target, default);
            if self.match_punct(":") {
                target = self.pattern_target()?;
            } else {
                if !key_is_ident {
                    return self.syntax("expected ':' in destructuring");
                }
                target = DeclTarget::Name(key.clone());
            }
            default = if self.match_punct("=") {
                Some(self.assign()?)
            } else {
                None
            };
            parts.push(PatternPart::Object { key, target, default });
            if !self.match_punct(",") {
                self.expect_punct("}")?;
                break;
            }
        }
        Ok(DeclTarget::Pattern(Rc::new(PatternNode {
            kind: "object".to_string(),
            parts,
            rest,
        })))
    }

    fn pattern_target(&mut self) -> Result<DeclTarget, JsError> {
        if let Some(t) = self.peek() {
            if t.kind == TokKind::Ident {
                let name = t.text.clone();
                self.pos += 1;
                return Ok(DeclTarget::Name(name));
            }
            if t.kind == TokKind::Punct && (t.text == "[" || t.text == "{") {
                return self.pattern();
            }
        }
        self.syntax("expected identifier in destructuring")
    }

    fn function_declaration(&mut self, async_: bool) -> Result<FuncNode, JsError> {
        // `function* g() {}`: the star sits between the keyword and the name.
        let is_generator = self.match_punct("*");
        let name = self.expect_ident()?;
        let f = self.function_rest_gen(async_, is_generator)?;
        let mut f = f;
        f.name = name;
        Ok(f)
    }

    fn function_rest_gen(&mut self, async_: bool, is_generator: bool) -> Result<FuncNode, JsError> {
        let p = self.param_list()?;
        // A body sets its own async and generator status rather than
        // inheriting the enclosing one, so a plain function nested in an async
        // function cannot use `await`, and one nested in a generator cannot
        // use `yield`. Both are ordinary identifiers there.
        let outer_async = self.async_depth;
        let outer_gen = self.generator_depth;
        self.async_depth = if async_ { outer_async + 1 } else { 0 };
        self.generator_depth = if is_generator { outer_gen + 1 } else { 0 };
        let body = self.parse_stmts_until(Some("}"));
        self.generator_depth = outer_gen;
        self.async_depth = outer_async;
        let mut stmts = Self::unpack_prelude(p.unpack);
        stmts.extend(body?);
        Ok(FuncNode {
            name: String::new(),
            params: p.names,
            defaults: p.defaults,
            rest: p.rest,
            body: stmts,
            body_expr: None,
            async_,
            arrow: false,
            is_generator,
        })
    }

    /// The `(params) { body }` half of a method in an object literal, once its
    /// name has already been read. The name goes on the function so that
    /// `({ f() {} }).f.name` and a stack trace both say `f`.
    fn method_rest(&mut self, name: &str) -> Result<FuncNode, JsError> {
        self.method_rest_async(name, false)
    }

    fn method_rest_async(&mut self, name: &str, async_: bool) -> Result<FuncNode, JsError> {
        self.method_rest_gen(name, async_, false)
    }

    fn method_rest_gen(
        &mut self,
        name: &str,
        async_: bool,
        is_generator: bool,
    ) -> Result<FuncNode, JsError> {
        if !self.peek_is_punct("(") {
            return self.syntax("expected '(' in method definition");
        }
        let mut f = self.function_rest_gen(async_, is_generator)?;
        f.name = name.to_string();
        Ok(f)
    }

    fn param_list(&mut self) -> Result<Params, JsError> {
        let mut names: Vec<String> = Vec::new();
        let mut defaults = BTreeMap::new();
        let mut unpack: Vec<(DeclTarget, String)> = Vec::new();
        let mut rest = None;
        self.list("(", ")", |p| {
            let is_rest = p.match_punct("...");
            // A parameter is either a name or a destructuring pattern, and a
            // pattern is handled by giving the slot a name of our own and
            // unpacking it into the real bindings on entry -- see `Params`.
            let is_pattern = p.peek_is_punct("[") || p.peek_is_punct("{");
            let name = if is_pattern {
                let slot = synthetic_param(names.len() + unpack.len());
                let target = p.pattern()?;
                unpack.push((target, slot.clone()));
                slot
            } else {
                p.expect_ident()?
            };
            if is_rest {
                rest = Some(name);
                return Ok(());
            }
            names.push(name.clone());
            if p.match_punct("=") {
                let d = p.assign()?;
                defaults.insert(name, d);
            }
            Ok(())
        })?;
        Ok(Params { names, defaults, rest, unpack })
    }

    /// The `let {a, b} = <slot>;` statements that turn the synthetic
    /// parameters `param_list` invented back into the bindings the source
    /// asked for, ready to be spliced in front of a function body.
    fn unpack_prelude(unpack: Vec<(DeclTarget, String)>) -> Vec<Rc<Node>> {
        unpack
            .into_iter()
            .map(|(target, slot)| {
                rc(VarDecl {
                    kind: "let".to_string(),
                    decls: vec![(target, Some(rc(Identifier(slot))))],
                })
            })
            .collect()
    }

    fn arrow_rest(&mut self, p: Params, async_: bool) -> PResult {
        self.expect_punct("=>")?;
        let prelude = Self::unpack_prelude(p.unpack);
        let mut f = FuncNode {
            name: String::new(),
            params: p.names,
            defaults: p.defaults,
            rest: p.rest,
            body: Vec::new(),
            body_expr: None,
            async_,
            arrow: true,
            is_generator: false,
        };
        // `await` is only spellable inside an async body, and an arrow with a
        // braced body is still an async body. Only the expression form used to
        // say so, so `async (a, b) => { await f() }` -- the ordinary way to
        // write an async arrow, and the only way once it needs a statement --
        // was rejected as "await is only valid in async functions".
        //
        // A plain arrow resets to zero for the same reason a plain function
        // does: `async function f() { const g = () => await x }` is a syntax
        // error, not an await.
        let outer_async = self.async_depth;
        self.async_depth = if async_ { outer_async + 1 } else { 0 };
        let braced = self.peek_is_punct("{");
        let result = if braced {
            self.parse_stmts_until(Some("}"))
        } else {
            // `x => expr` with a pattern parameter needs somewhere to put the
            // unpacking, so it becomes `x => { let {..} = slot; return expr }`.
            // Without a pattern the expression body is left as it is, since
            // that is the shape the interpreter has a fast path for.
            self.assign().map(|expr| vec![rc(Return(Some(expr)))])
        };
        self.async_depth = outer_async;
        let body = result?;
        if prelude.is_empty() && !braced {
            f.body_expr = match &*body[0] {
                Return(Some(e)) => Some(e.clone()),
                _ => unreachable!("expression body is always a Return"),
            };
        } else {
            f.body = prelude;
            f.body.extend(body);
        }
        Ok(rc(ArrowFunc(f)))
    }

    fn paren_followed_by_arrow(&self) -> bool {
        let mut depth = 0i64;
        let mut i = self.pos;
        while i < self.tokens.len() {
            let t = &self.tokens[i];
            if t.kind == TokKind::Punct {
                if t.text == "(" || t.text == "[" || t.text == "{" {
                    depth += 1;
                } else if t.text == ")" || t.text == "]" || t.text == "}" {
                    depth -= 1;
                    if depth == 0 && t.text == ")" {
                        return self
                            .tokens
                            .get(i + 1)
                            .map_or(false, |n| n.kind == TokKind::Punct && n.text == "=>");
                    }
                }
            }
            i += 1;
        }
        false
    }

    fn list(
        &mut self,
        opener: &str,
        closer: &str,
        mut item: impl FnMut(&mut Parser) -> Result<(), JsError>,
    ) -> Result<(), JsError> {
        if !opener.is_empty() {
            self.expect_punct(opener)?;
        }
        loop {
            if self.match_punct(closer) {
                break;
            }
            item(self)?;
            if self.match_punct(closer) {
                break;
            }
            self.expect_punct(",")?;
        }
        Ok(())
    }

    fn if_statement(&mut self) -> PResult {
        let (cond, then) = self.cond_body()?;
        // The semicolon belongs to the statement it ends, and only a
        // statement list bothers to eat one. Left here it hides the `else`
        // in `if (a) b(); else c();` -- which is every minified if/else.
        self.match_punct(";");
        let else_ = if self.match_kw("else") {
            Some(self.statement()?)
        } else {
            None
        };
        Ok(rc(If { cond, then, else_ }))
    }

    fn while_statement(&mut self) -> PResult {
        let (cond, body) = self.cond_body()?;
        Ok(rc(While { cond, body, label: None }))
    }

    fn do_while_statement(&mut self) -> PResult {
        let body = self.statement()?;
        self.match_punct(";");
        self.match_kw("while");
        self.expect_punct("(")?;
        let cond = self.sequence()?;
        self.expect_punct(")")?;
        self.match_punct(";");
        Ok(rc(DoWhile { body, cond, label: None }))
    }

    fn switch_statement(&mut self) -> PResult {
        self.expect_punct("(")?;
        let expr = self.sequence()?;
        self.expect_punct(")")?;
        self.expect_punct("{")?;
        let mut cases = Vec::new();
        loop {
            match self.peek() {
                None => return self.syntax("expected '}'"),
                Some(t) if t.kind == TokKind::Punct && t.text == "}" => {
                    self.pos += 1;
                    break;
                }
                _ => {}
            }
            if self.match_kw("case") {
                let test = self.expression()?;
                self.expect_punct(":")?;
                let body = self.case_body()?;
                cases.push(("case".to_string(), Some(test), body));
            } else if self.match_kw("default") {
                self.expect_punct(":")?;
                let body = self.case_body()?;
                cases.push(("default".to_string(), None, body));
            } else {
                return self.syntax("expected 'case' or 'default'");
            }
        }
        Ok(rc(Switch { expr, cases }))
    }

    fn case_body(&mut self) -> Result<Vec<Rc<Node>>, JsError> {
        let mut stmts = Vec::new();
        loop {
            match self.peek() {
                None => return self.syntax("expected '}'"),
                Some(t) if t.kind == TokKind::Punct && t.text == "}" => break,
                Some(t) if t.kind == TokKind::Kw && (t.text == "case" || t.text == "default") => {
                    break;
                }
                _ => {}
            }
            if self.match_punct(";") {
                continue;
            }
            stmts.push(self.statement()?);
        }
        Ok(stmts)
    }

    fn cond_body(&mut self) -> Result<(Rc<Node>, Rc<Node>), JsError> {
        self.expect_punct("(")?;
        let cond = self.sequence()?;
        self.expect_punct(")")?;
        let body = self.statement()?;
        Ok((cond, body))
    }

    fn for_statement(&mut self) -> PResult {
        self.expect_punct("(")?;
        let head_kw = match self.peek() {
            Some(t)
                if t.kind == TokKind::Kw
                    && (t.text == "var" || t.text == "let" || t.text == "const") =>
            {
                Some(t.text.clone())
            }
            _ => None,
        };
        // Both heads -- `for (let x of ...)` and `for (x of ...)` -- are read
        // the same way and rewound the same way. Nothing here can tell a
        // for-of from a plain three-clause `for` until the `of` or `in` shows
        // up after the target, and `for (let i = 0; ...)` starts identically,
        // so the attempt has to be speculative.
        let save = self.pos;
        if head_kw.is_some() {
            self.pos += 1;
        }
        if let Ok(target) = self.declaration_target() {
            if let Some(t2) = self.peek() {
                if (t2.kind == TokKind::Kw && t2.text == "in")
                    || (t2.kind == TokKind::Ident && t2.text == "of")
                {
                    let op = t2.text.clone();
                    self.pos += 1;
                    let iterable = self.sequence()?;
                    self.expect_punct(")")?;
                    let body = self.statement()?;
                    return Ok(rc(if op == "in" {
                        ForIn {
                            var_kind: head_kw.clone(),
                            target,
                            iterable,
                            body,
                            label: None,
                        }
                    } else {
                        ForOf {
                            var_kind: head_kw.clone(),
                            target,
                            iterable,
                            body,
                            label: None,
                        }
                    }));
                }
            }
        }
        self.pos = save;
        let init = if self.peek_is_punct(";") {
            None
        } else {
            if let Some(t) = self.peek() {
                if t.kind == TokKind::Kw && (t.text == "var" || t.text == "let" || t.text == "const")
                {
                    let kind = t.text.clone();
                    self.pos += 1;
                    let decls = self.declaration_list()?;
                    Some(rc(VarDecl { kind, decls }))
                } else {
                    let expr = self.sequence()?;
                    Some(rc(ExprStmt(expr)))
                }
            } else {
                None
            }
        };
        self.expect_punct(";")?;
        let cond = if self.peek_is_punct(";") {
            None
        } else {
            Some(self.sequence()?)
        };
        self.expect_punct(";")?;
        let update = if self.peek_is_punct(")") {
            None
        } else {
            Some(self.sequence()?)
        };
        self.expect_punct(")")?;
        let body = self.statement()?;
        Ok(rc(For { init, cond, update, body, label: None }))
    }

    fn try_statement(&mut self) -> PResult {
        let try_block = rc(Block(self.parse_stmts_until(Some("}"))?));
        let mut catch_param = None;
        let mut catch_block = None;
        if self.match_kw("catch") {
            // The binding is optional: `catch {}` is the form you write when
            // the failure itself is the news and the error object is not.
            let mut prelude = Vec::new();
            if self.match_punct("(") {
                // `catch ({message})` destructures, and takes the same route a
                // destructuring parameter takes: a slot of our own to catch
                // into, unpacked by the first statement of the block.
                if self.peek_is_punct("{") || self.peek_is_punct("[") {
                    let slot = synthetic_param(0);
                    let target = self.pattern()?;
                    prelude = Self::unpack_prelude(vec![(target, slot.clone())]);
                    catch_param = Some(slot);
                } else {
                    catch_param = Some(self.expect_ident()?);
                }
                self.expect_punct(")")?;
            }
            let mut stmts = prelude;
            stmts.extend(self.parse_stmts_until(Some("}"))?);
            catch_block = Some(rc(Block(stmts)));
        }
        let finally_block = if self.match_kw("finally") {
            Some(rc(Block(self.parse_stmts_until(Some("}"))?)))
        } else {
            None
        };
        Ok(rc(TryCatch {
            try_block,
            catch_param,
            catch_block,
            finally_block,
        }))
    }

    // -- expressions --------------------------------------------------------

    /// One expression, stopping at a comma.
    ///
    /// This is the form that goes in an argument list, an array element or an
    /// object value -- everywhere a comma separates things rather than
    /// joining them. `sequence` is the other one.
    fn expression(&mut self) -> PResult {
        self.assign()
    }

    /// An expression where a comma joins rather than separates.
    fn sequence(&mut self) -> PResult {
        let node = self.assign()?;
        if !self.peek_is_punct(",") {
            return Ok(node);
        }
        let mut items = vec![node];
        while self.match_punct(",") {
            items.push(self.assign()?);
        }
        Ok(rc(Sequence(items)))
    }

    fn assign(&mut self) -> PResult {
        // `yield` binds looser than everything except the comma, which is why
        // it is read here and not in `unary`: `yield a + b` yields the sum.
        if self.generator_depth > 0 {
            if let Some(t) = self.peek() {
                if t.kind == TokKind::Ident && t.text == "yield" {
                    self.pos += 1;
                    let delegate = self.match_punct("*");
                    // `yield` on its own is legal and yields undefined, so an
                    // operand is only there if something that can start an
                    // expression follows it.
                    let arg = if self.starts_expression() {
                        Some(self.assign()?)
                    } else {
                        None
                    };
                    return Ok(rc(Yield { arg, delegate }));
                }
            }
        }
        let left = self.conditional()?;
        if let Some(t) = self.peek() {
            if t.kind == TokKind::Punct
                && matches!(
                    t.text.as_str(),
                    "=" | "+=" | "-=" | "*=" | "/=" | "%=" | "**=" | "&=" | "|=" | "^="
                        | "<<=" | ">>=" | ">>>=" | "&&=" | "||=" | "??="
                )
            {
                let op = t.text.clone();
                self.pos += 1;
                let right = self.assign()?;
                if !matches!(*left, Node::Identifier(_) | Node::Member { .. } | Node::Index { .. }) {
                    // `[a, b] = pair` and `({x, y} = point)` were read as an
                    // array or object literal, because up to the `=` that is
                    // exactly what they look like. Re-reading the literal as a
                    // pattern is how every JS parser handles this; the
                    // alternative is unbounded lookahead over a whole literal.
                    if op == "=" {
                        if let Some(target) = expr_to_pattern(&left) {
                            return Ok(rc(AssignPattern { target, value: right }));
                        }
                    }
                    return self.syntax("invalid assignment target");
                }
                return Ok(rc(Assign { op, target: left, value: right }));
            }
        }
        Ok(left)
    }

    fn conditional(&mut self) -> PResult {
        let cond = self.or()?;
        if self.match_punct("?") {
            let then_expr = self.assign()?;
            self.expect_punct(":")?;
            let else_expr = self.assign()?;
            return Ok(rc(Conditional { cond, then_expr, else_expr }));
        }
        Ok(cond)
    }

    fn or(&mut self) -> PResult {
        let mut node = self.and()?;
        loop {
            if self.match_punct("||") {
                let right = self.and()?;
                node = rc(Logical("||".to_string(), node, right));
            } else if self.match_punct("??") {
                let right = self.and()?;
                node = rc(Logical("??".to_string(), node, right));
            } else {
                break;
            }
        }
        Ok(node)
    }

    fn and(&mut self) -> PResult {
        self.logical_chain("&&", &mut |p| p.bitwise_or())
    }

    fn logical_chain(&mut self, op: &str, sub: &mut dyn FnMut(&mut Parser) -> PResult) -> PResult {
        let mut node = sub(self)?;
        while self.match_punct(op) {
            let right = sub(self)?;
            node = rc(Logical(op.to_string(), node, right));
        }
        Ok(node)
    }

    fn bitwise_or(&mut self) -> PResult {
        self.binop("|", &mut |p| p.bitwise_xor())
    }

    fn bitwise_xor(&mut self) -> PResult {
        self.binop("^", &mut |p| p.bitwise_and())
    }

    fn bitwise_and(&mut self) -> PResult {
        self.binop("&", &mut |p| p.equality())
    }

    fn equality(&mut self) -> PResult {
        self.binop_multi(&["==", "!=", "===", "!=="], &mut |p| p.relational())
    }

    fn relational(&mut self) -> PResult {
        self.binop_multi(&["<", "<=", ">", ">=", "in", "instanceof"], &mut |p| p.shift())
    }

    fn shift(&mut self) -> PResult {
        self.binop_multi(&["<<", ">>", ">>>"], &mut |p| p.additive())
    }

    fn additive(&mut self) -> PResult {
        self.binop_multi(&["+", "-"], &mut |p| p.multiplicative())
    }

    fn multiplicative(&mut self) -> PResult {
        self.binop_multi(&["*", "/", "%"], &mut |p| p.exponent())
    }

    fn exponent(&mut self) -> PResult {
        let node = self.unary()?;
        if self.match_punct("**") {
            let right = self.exponent()?;
            return Ok(rc(Binary("**".to_string(), node, right)));
        }
        Ok(node)
    }

    fn binop(&mut self, op: &str, sub: &mut dyn FnMut(&mut Parser) -> PResult) -> PResult {
        let mut node = sub(self)?;
        while self.match_punct(op) {
            let right = sub(self)?;
            node = rc(Binary(op.to_string(), node, right));
        }
        Ok(node)
    }

    fn binop_multi(
        &mut self,
        ops: &[&str],
        sub: &mut dyn FnMut(&mut Parser) -> PResult,
    ) -> PResult {
        let mut node = sub(self)?;
        loop {
            let value = match self.peek() {
                Some(t) if t.kind == TokKind::Punct || t.kind == TokKind::Kw => {
                    if ops.contains(&t.text.as_str()) {
                        let v = t.text.clone();
                        self.pos += 1;
                        Some(v)
                    } else {
                        None
                    }
                }
                _ => None,
            };
            let Some(v) = value else { break };
            let right = sub(self)?;
            node = rc(Binary(v, node, right));
        }
        Ok(node)
    }

    fn unary(&mut self) -> PResult {
        if let Some(t) = self.peek() {
            if t.kind == TokKind::Punct && matches!(t.text.as_str(), "!" | "-" | "+" | "~" | "++" | "--")
            {
                let op = t.text.clone();
                self.pos += 1;
                if op == "++" || op == "--" {
                    let operand = self.unary()?;
                    return Ok(rc(Update { op, operand, prefix: true }));
                }
                let operand = self.unary()?;
                return Ok(rc(Unary(op, operand)));
            }
            if t.kind == TokKind::Kw && matches!(t.text.as_str(), "typeof" | "delete" | "void") {
                let op = t.text.clone();
                self.pos += 1;
                let operand = self.unary()?;
                return Ok(rc(Unary(op, operand)));
            }
            if t.kind == TokKind::Kw && t.text == "await" {
                if self.async_depth == 0 {
                    return self.syntax("await is only valid in async functions");
                }
                self.pos += 1;
                let operand = self.unary()?;
                return Ok(rc(Await(operand)));
            }
        }
        self.call()
    }

    fn call(&mut self) -> PResult {
        let mut node = self.primary()?;
        loop {
            if self.match_punct("(") {
                let args = self.args()?;
                node = rc(Call { callee: node, args, optional: false });
            } else if self.match_punct(".") {
                let name = self.expect_property_name()?;
                node = rc(Member { obj: node, name, optional: false });
            } else if self.match_punct("?.") {
                if self.match_punct("(") {
                    // `args` starts *after* the paren, the way the plain call
                    // branch above enters it. Peeking instead of consuming
                    // left the `(` for the argument parser to trip over, so
                    // `f?.()` -- the whole point of an optional call -- was a
                    // syntax error.
                    let args = self.args()?;
                    node = rc(Call { callee: node, args, optional: true });
                } else if self.peek_is_punct("[") {
                    self.pos += 1;
                    let index = self.expression()?;
                    self.expect_punct("]")?;
                    node = rc(Index { obj: node, index, optional: true });
                } else {
                    let name = self.expect_property_name()?;
                    node = rc(Member { obj: node, name, optional: true });
                }
            } else if self.match_punct("[") {
                let index = self.expression()?;
                self.expect_punct("]")?;
                node = rc(Index { obj: node, index, optional: false });
            } else if let Some(raw) = self.peek_template() {
                // A template hard up against something callable is a tagged
                // template, and the tag binds as tightly as a call does:
                // ``a.b`x` `` tags `a.b`, and ``f`x``y` `` tags the result of
                // the first tag with the second. Sitting in the call loop is
                // what gets both of those right for free.
                self.pos += 1;
                let (quasis, raws, exprs) = self.template_parts(&raw)?;
                node = rc(TaggedTemplate { tag: node, quasis, raws, exprs });
            } else if self.match_punct("++") {
                node = rc(Update { op: "++".to_string(), operand: node, prefix: false });
            } else if self.match_punct("--") {
                node = rc(Update { op: "--".to_string(), operand: node, prefix: false });
            } else {
                break;
            }
        }
        Ok(node)
    }

    fn args(&mut self) -> Result<Vec<Rc<Node>>, JsError> {
        let mut out = Vec::new();
        loop {
            if self.match_punct(")") {
                break;
            }
            if self.match_punct("...") {
                let expr = self.expression()?;
                out.push(rc(Spread(expr)));
            } else {
                let expr = self.expression()?;
                out.push(expr);
            }
            if self.match_punct(")") {
                break;
            }
            self.expect_punct(",")?;
        }
        Ok(out)
    }

    fn array_item(&mut self) -> PResult {
        if self.match_punct("...") {
            let expr = self.expression()?;
            return Ok(rc(Spread(expr)));
        }
        self.expression()
    }

    fn new_expression(&mut self) -> PResult {
        let mut callee = self.primary()?;
        // The thing being constructed reaches as far as the dots and brackets
        // go, and stops dead at the first `(` -- which belongs to `new` as its
        // argument list, not to the callee as a call. Reading only the primary
        // made `new a.B()` construct `a` and then, if that somehow survived,
        // look up `.B` on the result: a namespaced constructor, which is how
        // every bundle spells one, could not be constructed at all.
        loop {
            if self.match_punct(".") {
                let name = self.expect_property_name()?;
                callee = rc(Member { obj: callee, name, optional: false });
            } else if self.match_punct("[") {
                let index = self.expression()?;
                self.expect_punct("]")?;
                callee = rc(Index { obj: callee, index, optional: false });
            } else {
                break;
            }
        }
        let args = if self.match_punct("(") { self.args()? } else { Vec::new() };
        Ok(rc(New { callee, args }))
    }

    fn primary(&mut self) -> PResult {
        if let Some(t) = self.peek() {
            match t.kind {
                TokKind::Number => {
                    let v = match &t.payload {
                        TokPayload::Number(n) => *n,
                        _ => f64::NAN,
                    };
                    self.pos += 1;
                    return Ok(rc(Literal(LiteralVal::Number(v))));
                }
                TokKind::Str => {
                    let s = match &t.payload {
                        TokPayload::Str(s) => s.clone(),
                        _ => String::new(),
                    };
                    self.pos += 1;
                    return Ok(rc(Literal(LiteralVal::Str(s.into()))));
                }
                TokKind::Regex => {
                    let (src, flags) = match &t.payload {
                        TokPayload::Regex(a, b) => (a.clone(), b.clone()),
                        _ => (String::new(), String::new()),
                    };
                    self.pos += 1;
                    return Ok(rc(Regex { source: src, flags }));
                }
                TokKind::Template => {
                    let raw = match &t.payload {
                        TokPayload::Str(s) => s.clone(),
                        _ => String::new(),
                    };
                    self.pos += 1;
                    return self.template_literal(&raw);
                }
                TokKind::Kw => {
                    let v = t.text.clone();
                    self.pos += 1;
                    match v.as_str() {
                        "true" => return Ok(rc(Literal(LiteralVal::Bool(true)))),
                        "false" => return Ok(rc(Literal(LiteralVal::Bool(false)))),
                        "null" => return Ok(rc(Literal(LiteralVal::Null))),
                        "undefined" => return Ok(rc(Literal(LiteralVal::Undefined))),
                        "function" => {
                            let f = self.function_expression(false)?;
                            return Ok(rc(FunctionExpr(f)));
                        }
                        "this" => return Ok(rc(This)),
                        "new" => return self.new_expression(),
                        "class" => {
                            let c = self.class_expression()?;
                            return Ok(rc(ClassExpr(c)));
                        }
                        "super" => return Ok(rc(Super)),
                        _ => return self.syntax(&format!("unexpected keyword '{v}'")),
                    }
                }
                TokKind::Ident => {
                    let v = t.text.clone();
                    if v == "async" && self.next_is_kw("function") {
                        self.pos += 1;
                        self.pos += 1;
                        let f = self.function_expression(true)?;
                        return Ok(rc(FunctionExpr(f)));
                    }
                    if v == "async" {
                        if self.peek2().map_or(false, |t2| t2.kind == TokKind::Ident)
                            && self.peek3_is_arrow()
                        {
                            self.pos += 1;
                            let name = self.match_ident().unwrap();
                            return self.arrow_rest(Params::one(name), true);
                        }
                        if self.peek2_is_punct("(") && self.paren_followed_by_arrow() {
                            self.pos += 1;
                            let p = self.param_list()?;
                            return self.arrow_rest(p, true);
                        }
                    }
                    if self.peek2_is_punct("=>") {
                        self.pos += 1;
                        return self.arrow_rest(Params::one(v), false);
                    }
                    self.pos += 1;
                    return Ok(rc(Identifier(v)));
                }
                TokKind::Punct => {
                    let v = t.text.clone();
                    match v.as_str() {
                        "(" => {
                            if self.paren_followed_by_arrow() {
                                let p = self.param_list()?;
                                return self.arrow_rest(p, false);
                            }
                            self.pos += 1;
                            let node = self.sequence()?;
                            self.expect_punct(")")?;
                            return Ok(node);
                        }
                        "[" => {
                            self.pos += 1;
                            let items = self.array_items()?;
                            return Ok(rc(ArrayLit(items)));
                        }
                        "{" => {
                            self.pos += 1;
                            let pairs = self.object_pairs()?;
                            return Ok(rc(ObjectLit(pairs)));
                        }
                        _ => {}
                    }
                }
            }
        }
        self.syntax("unexpected token")
    }

    fn array_items(&mut self) -> Result<Vec<Rc<Node>>, JsError> {
        let mut out = Vec::new();
        loop {
            if self.match_punct("]") {
                break;
            }
            if self.match_punct(",") {
                continue;
            }
            out.push(self.array_item()?);
            if self.match_punct("]") {
                break;
            }
            self.expect_punct(",")?;
        }
        Ok(out)
    }

    fn object_pairs(&mut self) -> Result<Vec<ObjectPair>, JsError> {
        let mut out = Vec::new();
        loop {
            if self.match_punct("}") {
                break;
            }
            if self.match_punct(",") {
                continue;
            }
            if self.match_punct("...") {
                out.push(ObjectPair::Spread(self.expression()?));
                if self.match_punct("}") {
                    break;
                }
                self.expect_punct(",")?;
                continue;
            }
            // `{ async name() {} }`. Only a marker when a property name
            // follows it, because `{ async: 1 }` and `{ async }` are both
            // ordinary properties -- `async` is never a reserved word. Without
            // this the whole object literal came apart one token later, which
            // is what `new ReadableStream({ async pull(c) {...} })` on
            // vimeo.com reported as "expected ','".
            let is_async = match self.peek() {
                Some(t)
                    if t.kind == TokKind::Ident
                        && t.text == "async"
                        && (self.peek2_is_property_name() || self.peek2_is_punct("[")) =>
                {
                    self.pos += 1;
                    true
                }
                _ => false,
            };
            // `{ *entries() {...} }`. A star before the name is the only mark
            // a generator method carries, and object literals full of them are
            // how a bundle hands back a Map-like: `keys`, `values` and then
            // `*entries()` in the same braces.
            let is_generator = self.match_punct("*");
            // A computed key -- `{ [expr]: value }`. The brackets are the only
            // thing that distinguishes it, and what is inside is an ordinary
            // expression, so it cannot be folded into the name case below.
            if self.match_punct("[") {
                let key_expr = self.expression()?;
                self.expect_punct("]")?;
                // `{ [Symbol.iterator]() {} }` -- a computed key can name a
                // method as well as a value, and a transpiler emits far more
                // of the former than a human writes of either.
                let val = if self.peek_is_punct("(") {
                    let f = self.method_rest_gen("", is_async, is_generator)?;
                    rc(FunctionExpr(f))
                } else {
                    self.expect_punct(":")?;
                    self.expression()?
                };
                out.push(ObjectPair::Computed(key_expr, val));
                if self.match_punct("}") {
                    break;
                }
                self.expect_punct(",")?;
                continue;
            }
            // `get` and `set` are only accessor markers when a property name
            // *or* a computed key follows them. On their own they are perfectly
            // good property names -- `{ get: 1 }` and `{ set }` both appear in
            // real code -- so peek past them before committing.
            let accessor = match self.peek() {
                Some(t)
                    if !is_async
                        && t.kind == TokKind::Ident
                        && (t.text == "get" || t.text == "set")
                        && (self.peek2_is_property_name() || self.peek2_is_punct("[")) =>
                {
                    let kind = t.text.clone();
                    self.pos += 1;
                    Some(kind)
                }
                _ => None,
            };
            // A computed key -- `{ [expr]: value }`, `{ [expr]() {...} }`,
            // `{ get [expr]() {...} }`. The brackets are the only thing that
            // distinguishes it, and what is inside is an ordinary expression,
            // so it cannot be folded into the name case below.
            let computed = if self.match_punct("[") {
                let key_expr = self.expression()?;
                self.expect_punct("]")?;
                Some(key_expr)
            } else {
                None
            };
            let key = match &computed {
                Some(_) => String::new(),
                None => match self.peek() {
                    Some(t)
                        if t.kind == TokKind::Ident || t.kind == TokKind::Str
                            || t.kind == TokKind::Kw
                            || t.kind == TokKind::Number =>
                    {
                        let k = t.text.clone();
                        self.pos += 1;
                        k
                    }
                    _ => return self.syntax("expected property name"),
                },
            };
            if let Some(kind) = accessor {
                let func = self.method_rest(&key)?;
                match computed {
                    Some(key_expr) => out.push(ObjectPair::ComputedAccessor {
                        key_expr,
                        kind,
                        func,
                    }),
                    None => out.push(ObjectPair::Accessor { key, kind, func }),
                }
            } else if let Some(key_expr) = computed {
                if self.peek_is_punct("(") {
                    // Computed method shorthand: `{ [key]() {...} }`.
                    let func = self.method_rest(&key)?;
                    out.push(ObjectPair::Computed(key_expr, rc(FunctionExpr(func))));
                } else if self.match_punct(":") {
                    let val = self.expression()?;
                    out.push(ObjectPair::Computed(key_expr, val));
                } else {
                    return self.syntax("expected ':' or '(' after computed key");
                }
            } else if self.peek_is_punct("(") {
                // Method shorthand: `{ name() {...} }` is `{ name: function
                // name() {...} }` in every way that matters here.
                let func = self.method_rest_gen(&key, is_async, is_generator)?;
                out.push(ObjectPair::Key(key, rc(FunctionExpr(func))));
            } else if self.match_punct(":") {
                let val = self.expression()?;
                out.push(ObjectPair::Key(key, val));
            } else {
                // Property shorthand { name } === { name: name }.
                out.push(ObjectPair::Key(key.clone(), rc(Identifier(key))));
            }
            if self.match_punct("}") {
                break;
            }
            self.expect_punct(",")?;
        }
        Ok(out)
    }

    fn function_expression(&mut self, async_: bool) -> Result<FuncNode, JsError> {
        let is_generator = self.match_punct("*");
        let mut name = String::new();
        if let Some(t) = self.peek() {
            if t.kind == TokKind::Ident {
                name = t.text.clone();
                self.pos += 1;
            }
        }
        let mut f = self.function_rest_gen(async_, is_generator)?;
        f.name = name;
        Ok(f)
    }

    fn template_literal(&mut self, raw: &str) -> PResult {
        let (quasis, _, exprs) = self.template_parts(raw)?;
        Ok(rc(TemplateLiteral { quasis, exprs }))
    }

    #[allow(clippy::type_complexity)]
    fn template_parts(
        &mut self,
        raw: &str,
    ) -> Result<(Vec<String>, Vec<String>, Vec<Rc<Node>>), JsError> {
        let mut quasis = Vec::new();
        let mut raws = Vec::new();
        let mut exprs = Vec::new();
        for (quasi, raw_text, expr_src) in split_template(raw) {
            quasis.push(quasi);
            raws.push(raw_text);
            if let Some(src) = expr_src {
                let mut sub = Parser::new(&src)?;
                exprs.push(sub.parse_expression()?);
            }
        }
        Ok((quasis, raws, exprs))
    }

    fn class_declaration(&mut self) -> Result<ClassNode, JsError> {
        let name = self.expect_ident()?;
        let mut c = ClassNode {
            name,
            superclass: None,
            methods: Vec::new(),
            fields: Vec::new(),
        };
        if self.match_kw("extends") {
            c.superclass = Some(self.expression()?);
        }
        let (methods, fields) = self.class_body()?;
        c.methods = methods;
        c.fields = fields;
        Ok(c)
    }

    fn class_expression(&mut self) -> Result<ClassNode, JsError> {
        let mut c = ClassNode {
            name: String::new(),
            superclass: None,
            methods: Vec::new(),
            fields: Vec::new(),
        };
        if let Some(t) = self.peek() {
            if t.kind == TokKind::Ident {
                c.name = t.text.clone();
                self.pos += 1;
            }
        }
        if self.match_kw("extends") {
            c.superclass = Some(self.expression()?);
        }
        let (methods, fields) = self.class_body()?;
        c.methods = methods;
        c.fields = fields;
        Ok(c)
    }

    fn class_body(&mut self) -> Result<(Vec<ClassMethodNode>, Vec<ClassFieldNode>), JsError> {
        self.expect_punct("{")?;
        let mut methods = Vec::new();
        let mut fields = Vec::new();
        loop {
            if self.match_punct("}") {
                break;
            }
            if self.match_punct(";") {
                continue;
            }
            let mut is_static = false;
            let mut accessor = None;
            if let Some(t) = self.peek() {
                if t.kind == TokKind::Ident && t.text == "static" {
                    if !self.peek2_is_punct("(") {
                        self.pos += 1;
                        is_static = true;
                    }
                }
            }
            let mut is_async = false;
            if let Some(t) = self.peek() {
                // `async f() {}`, but not a method that happens to be *called*
                // `async` -- which is legal, since `async` is never a keyword.
                if t.kind == TokKind::Ident
                    && t.text == "async"
                    && self.peek2_is_property_name()
                {
                    self.pos += 1;
                    is_async = true;
                }
            }
            if let Some(t) = self.peek() {
                if t.kind == TokKind::Ident && (t.text == "get" || t.text == "set") {
                    if !self.peek2_is_punct("(") && !self.peek2_is_punct("=")
                        && !self.peek2_is_punct(";") && !self.peek2_is_punct("}")
                        && !self.peek2_is_punct(",")
                    {
                        accessor = Some(t.text.clone());
                        self.pos += 1;
                    }
                }
            }
            // Accessors cannot be async: `async get x() {}` is a syntax error,
            // while `async get() {}` is an async method named `get` (the accessor
            // check above never fires because `(` follows `get`).
            if is_async && accessor.is_some() {
                return self.syntax("async accessors are not supported");
            }
            // `*next() {}` and `static *[Symbol.iterator]() {}`: the star binds
            // to the member, not to the name.
            let is_generator = self.match_punct("*");
            let (name, key) = self.member_name()?;
            if !self.peek_is_punct("(") {
                // Not a method: a field. `x = 1`, `x;`, `static x = 1`, and the
                // bare `x` that only declares one. `get`/`set`/`async`/`static`
                // are all legal field names too, and the lookahead above has
                // already put those back when what followed was not a name.
                if accessor.is_some() || is_async || is_generator {
                    return self.syntax("expected '(' in class method");
                }
                let value = if self.match_punct("=") {
                    Some(self.assign()?)
                } else {
                    None
                };
                self.match_punct(";");
                fields.push(ClassFieldNode {
                    name,
                    key,
                    value,
                    is_static,
                });
                continue;
            }
            let p = self.param_list()?;
            // `await` is only a keyword inside an async body, and a class body
            // parses its methods here rather than through `function_rest`, so
            // the depth has to be carried over the body by hand. A non-async
            // method still resets the outer depth, so it cannot `await`, and a
            // non-generator method resets it for `yield` for the same reason.
            let outer_async = self.async_depth;
            let outer_gen = self.generator_depth;
            self.async_depth = if is_async { outer_async + 1 } else { 0 };
            self.generator_depth = if is_generator { outer_gen + 1 } else { 0 };
            let body = self.parse_stmts_until(Some("}"));
            self.generator_depth = outer_gen;
            self.async_depth = outer_async;
            let mut stmts = Self::unpack_prelude(p.unpack);
            stmts.extend(body?);
            methods.push(ClassMethodNode {
                name,
                key,
                params: p.names,
                defaults: p.defaults,
                rest: p.rest,
                body: stmts,
                is_static,
                accessor,
                is_async,
                is_generator,
            });
        }
        Ok((methods, fields))
    }
}

fn rc(n: Node) -> Rc<Node> {
    Rc::new(n)
}

/// Re-read an array or object literal as the binding pattern it turns out to
/// have been, or `None` if it cannot be one.
///
/// `None` is the honest answer for the shapes this cannot express rather than
/// a shape it gets wrong: `[a.b] = pair` assigns through a member expression,
/// which is legal JavaScript and not something a `DeclTarget` can hold, so it
/// stays the syntax error it was instead of silently binding a local called
/// `b`.
fn expr_to_pattern(node: &Rc<Node>) -> Option<DeclTarget> {
    match &**node {
        Identifier(name) => Some(DeclTarget::Name(name.clone())),
        // `[a.b, c[0]] = pair` assigns to two properties and declares nothing.
        // It reads as an array literal until the `=` arrives, at which point
        // the member expressions inside it have to survive the rewrite intact.
        Member { .. } | Index { .. } => Some(DeclTarget::Member(node.clone())),
        ArrayLit(items) => {
            let mut parts = Vec::new();
            let mut rest = None;
            for (i, item) in items.iter().enumerate() {
                if let Spread(inner) = &**item {
                    if i + 1 != items.len() {
                        return None;
                    }
                    rest = Some(expr_to_pattern(inner)?);
                    continue;
                }
                let (target, default) = split_default(item)?;
                parts.push(PatternPart::Array { target, default });
            }
            Some(DeclTarget::Pattern(Rc::new(PatternNode {
                kind: "array".to_string(),
                parts,
                rest,
            })))
        }
        ObjectLit(pairs) => {
            let mut parts = Vec::new();
            let mut rest = None;
            for pair in pairs {
                match pair {
                    ObjectPair::Key(key, value) => {
                        let (target, default) = split_default(value)?;
                        parts.push(PatternPart::Object {
                            key: key.clone(),
                            target,
                            default,
                        });
                    }
                    ObjectPair::Spread(inner) => rest = Some(expr_to_pattern(inner)?),
                    _ => return None,
                }
            }
            Some(DeclTarget::Pattern(Rc::new(PatternNode {
                kind: "object".to_string(),
                parts,
                rest,
            })))
        }
        _ => None,
    }
}

/// `a = 1` inside a literal-turned-pattern is a default, not an assignment.
fn split_default(node: &Rc<Node>) -> Option<(DeclTarget, Option<Rc<Node>>)> {
    if let Assign { op, target, value } = &**node {
        if op == "=" {
            return Some((expr_to_pattern(target)?, Some(value.clone())));
        }
    }
    if let AssignPattern { target, value } = &**node {
        return Some((target.clone(), Some(value.clone())));
    }
    Some((expr_to_pattern(node)?, None))
}

/// Hand a loop the name it was labelled with, so a `continue name` aimed at
/// it can be resumed there rather than escaping. Anything that is not a loop
/// is returned untouched: only `break` can name it, and the enclosing
/// `Labelled` node catches that.
fn label_loop(node: Rc<Node>, name: &str) -> Rc<Node> {
    let labelled = match &*node {
        While { cond, body, .. } => While {
            cond: cond.clone(),
            body: body.clone(),
            label: Some(name.to_string()),
        },
        DoWhile { body, cond, .. } => DoWhile {
            body: body.clone(),
            cond: cond.clone(),
            label: Some(name.to_string()),
        },
        For { init, cond, update, body, .. } => For {
            init: init.clone(),
            cond: cond.clone(),
            update: update.clone(),
            body: body.clone(),
            label: Some(name.to_string()),
        },
        ForIn { var_kind, target, iterable, body, .. } => ForIn {
            var_kind: var_kind.clone(),
            target: target.clone(),
            iterable: iterable.clone(),
            body: body.clone(),
            label: Some(name.to_string()),
        },
        ForOf { var_kind, target, iterable, body, .. } => ForOf {
            var_kind: var_kind.clone(),
            target: target.clone(),
            iterable: iterable.clone(),
            body: body.clone(),
            label: Some(name.to_string()),
        },
        _ => return node,
    };
    rc(labelled)
}


/// Split template raw source into [(quasi, expr_source|None), ...].
/// A template's inner text cut into alternating literal chunks and `${}`
/// expression sources.
///
/// The literal chunks come back cooked -- escapes decoded -- which they did
/// not used to: a backslash and the character after it were copied through
/// unchanged, so `` `line\nline` `` produced a string with a literal backslash
/// and the letter `n` in it rather than a newline. Nothing complained, because
/// the result is still a perfectly good string; it is just the wrong one, and
/// what it usually breaks is markup that a page then inserts into the DOM.
/// Split a template body into its literal pieces and its substitutions.
///
/// Each piece comes back twice: once cooked, with `\n` turned into a newline,
/// and once exactly as it was written. An ordinary template only ever uses the
/// first, but a tag function is handed both, and `String.raw` -- the tag every
/// path-building and regex-building helper on the web reaches for -- exists
/// solely to return the second.
fn split_template(raw: &str) -> Vec<(String, String, Option<String>)> {
    let mut parts = Vec::new();
    let mut buf = String::new();
    let b = raw.as_bytes();
    let n = b.len();
    let mut i = 0usize;
    let mut raw_start = 0usize;
    while i < n {
        if b[i] == b'\\' && i + 1 < n {
            let (decoded, next) = read_escape(raw, i + 1);
            if let Some(c) = decoded {
                buf.push(c);
            }
            i = next;
            continue;
        }
        if b[i] == b'$' && i + 1 < n && b[i + 1] == b'{' {
            match find_subst_end(raw, i) {
                Some(close) => {
                    parts.push((
                        std::mem::take(&mut buf),
                        raw[raw_start..i].to_string(),
                        Some(raw[i + 2..close].to_string()),
                    ));
                    i = close + 1;
                    raw_start = i;
                    continue;
                }
                // An unclosed `${` is not a substitution, just two characters.
                None => {}
            }
        }
        let c = char_at(raw, i);
        buf.push(c);
        i += c.len_utf8();
    }
    parts.push((buf, raw[raw_start..].to_string(), None));
    parts
}

/// Convenience entry point used by the interpreter to parse a program.
pub fn parse_program(source: &str) -> Result<Rc<Node>, JsError> {
    let mut p = Parser::new(source)?;
    p.parse_program()
}