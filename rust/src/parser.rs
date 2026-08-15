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
}

type PResult = Result<Rc<Node>, JsError>;

impl Parser {
    pub fn new(source: &str) -> Result<Parser, JsError> {
        let tokens = tokenize(source)?;
        Ok(Parser {
            source: source.to_string(),
            tokens,
            pos: 0,
            async_depth: 0,
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

    fn peek3_is_arrow(&self) -> bool {
        self.peek_n(3).map_or(false, |t| t.kind == TokKind::Punct && t.text == "=>")
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

    fn next_is_kw(&self, text: &str) -> bool {
        self.peek2().map_or(false, |t| t.kind == TokKind::Kw && t.text == text)
    }

    fn syntax<T>(&self, msg: &str) -> Result<T, JsError> {
        let offset = self.peek().map_or(self.source.len(), |t| t.offset);
        let line = self.source[..offset].matches('\n').count() + 1;
        Err(JsError::js(format!("SyntaxError on line {line}: {msg}")))
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
        let name = self.expect_ident()?;
        let f = self.function_rest(async_)?;
        let mut f = f;
        f.name = name;
        Ok(f)
    }

    fn function_rest(&mut self, async_: bool) -> Result<FuncNode, JsError> {
        let (params, defaults, rest) = self.param_list()?;
        if async_ {
            self.async_depth += 1;
        }
        let body = self.parse_stmts_until(Some("}"));
        if async_ {
            self.async_depth -= 1;
        }
        let body = body?;
        Ok(FuncNode {
            name: String::new(),
            params,
            defaults,
            rest,
            body,
            body_expr: None,
            async_,
            arrow: false,
        })
    }

    /// The `(params) { body }` half of a method in an object literal, once its
    /// name has already been read. The name goes on the function so that
    /// `({ f() {} }).f.name` and a stack trace both say `f`.
    fn method_rest(&mut self, name: &str) -> Result<FuncNode, JsError> {
        if !self.peek_is_punct("(") {
            return self.syntax("expected '(' in method definition");
        }
        let mut f = self.function_rest(false)?;
        f.name = name.to_string();
        Ok(f)
    }

    fn param_list(&mut self) -> Result<(Vec<String>, BTreeMap<String, Rc<Node>>, Option<String>), JsError> {
        let mut names = Vec::new();
        let mut defaults = BTreeMap::new();
        let mut rest = None;
        self.list("(", ")", |p| {
            let is_rest = p.match_punct("...");
            let name = p.expect_ident()?;
            if is_rest {
                rest = Some(name);
                return Ok(());
            }
            names.push(name);
            if p.match_punct("=") {
                let d = p.assign()?;
                defaults.insert(names.last().unwrap().clone(), d);
            }
            Ok(())
        })?;
        Ok((names, defaults, rest))
    }

    fn arrow_rest(
        &mut self,
        params: Vec<String>,
        defaults: BTreeMap<String, Rc<Node>>,
        rest: Option<String>,
        async_: bool,
    ) -> PResult {
        self.expect_punct("=>")?;
        let mut f = FuncNode {
            name: String::new(),
            params,
            defaults,
            rest,
            body: Vec::new(),
            body_expr: None,
            async_,
            arrow: true,
        };
        if self.peek_is_punct("{") {
            f.body = self.parse_stmts_until(Some("}"))?;
            return Ok(rc(ArrowFunc(f)));
        }
        if async_ {
            self.async_depth += 1;
        }
        let expr = self.assign();
        if async_ {
            self.async_depth -= 1;
        }
        f.body_expr = Some(expr?);
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
        if let Some(kind) = head_kw {
            let save = self.pos;
            self.pos += 1;
            if let Some(name) = self.match_ident() {
                if let Some(t2) = self.peek() {
                    if t2.kind == TokKind::Kw && (t2.text == "in" || t2.text == "of") {
                        let op = t2.text.clone();
                        self.pos += 1;
                        let iterable = self.sequence()?;
                        self.expect_punct(")")?;
                        let body = self.statement()?;
                        return Ok(rc(if op == "in" {
                            ForIn {
                                var_kind: Some(kind.clone()),
                                name,
                                iterable,
                                body,
                                label: None,
                            }
                        } else {
                            ForOf {
                                var_kind: Some(kind.clone()),
                                name,
                                iterable,
                                body,
                                label: None,
                            }
                        }));
                    }
                }
            }
            self.pos = save;
        } else {
            let save = self.pos;
            if let Some(name) = self.match_ident() {
                if let Some(t2) = self.peek() {
                    if t2.kind == TokKind::Kw && (t2.text == "in" || t2.text == "of") {
                        let op = t2.text.clone();
                        self.pos += 1;
                        let iterable = self.sequence()?;
                        self.expect_punct(")")?;
                        let body = self.statement()?;
                        return Ok(rc(if op == "in" {
                            ForIn {
                                var_kind: None,
                                name,
                                iterable,
                                body,
                                label: None,
                            }
                        } else {
                            ForOf {
                                var_kind: None,
                                name,
                                iterable,
                                body,
                                label: None,
                            }
                        }));
                    }
                }
                self.pos = save;
            }
        }
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
            if self.match_punct("(") {
                catch_param = Some(self.expect_ident()?);
                self.expect_punct(")")?;
            }
            catch_block = Some(rc(Block(self.parse_stmts_until(Some("}"))?)));
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
                if self.peek_is_punct("(") {
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
        let callee = self.primary()?;
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
                            return self.arrow_rest(vec![name], BTreeMap::new(), None, true);
                        }
                        if self.peek2_is_punct("(") && self.paren_followed_by_arrow() {
                            self.pos += 1;
                            let (params, defaults, rest) = self.param_list()?;
                            return self.arrow_rest(params, defaults, rest, true);
                        }
                    }
                    if self.peek2_is_punct("=>") {
                        self.pos += 1;
                        return self.arrow_rest(vec![v], BTreeMap::new(), None, false);
                    }
                    self.pos += 1;
                    return Ok(rc(Identifier(v)));
                }
                TokKind::Punct => {
                    let v = t.text.clone();
                    match v.as_str() {
                        "(" => {
                            if self.paren_followed_by_arrow() {
                                let (params, defaults, rest) = self.param_list()?;
                                return self.arrow_rest(params, defaults, rest, false);
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
            // A computed key -- `{ [expr]: value }`. The brackets are the only
            // thing that distinguishes it, and what is inside is an ordinary
            // expression, so it cannot be folded into the name case below.
            if self.match_punct("[") {
                let key_expr = self.expression()?;
                self.expect_punct("]")?;
                self.expect_punct(":")?;
                let val = self.expression()?;
                out.push(ObjectPair::Computed(key_expr, val));
                if self.match_punct("}") {
                    break;
                }
                self.expect_punct(",")?;
                continue;
            }
            // `get` and `set` are only accessor markers when a property name
            // follows them. On their own they are perfectly good property
            // names -- `{ get: 1 }` and `{ set }` both appear in real code --
            // so peek past them before committing.
            let accessor = match self.peek() {
                Some(t)
                    if t.kind == TokKind::Ident
                        && (t.text == "get" || t.text == "set")
                        && self.peek2_is_property_name() =>
                {
                    let kind = t.text.clone();
                    self.pos += 1;
                    Some(kind)
                }
                _ => None,
            };
            let key = match self.peek() {
                Some(t)
                    if t.kind == TokKind::Ident || t.kind == TokKind::Str || t.kind == TokKind::Kw
                        || t.kind == TokKind::Number =>
                {
                    let k = t.text.clone();
                    self.pos += 1;
                    k
                }
                _ => return self.syntax("expected property name"),
            };
            if let Some(kind) = accessor {
                let func = self.method_rest(&key)?;
                out.push(ObjectPair::Accessor { key, kind, func });
            } else if self.peek_is_punct("(") {
                // Method shorthand: `{ name() {...} }` is `{ name: function
                // name() {...} }` in every way that matters here.
                let func = self.method_rest(&key)?;
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
        let mut name = String::new();
        if let Some(t) = self.peek() {
            if t.kind == TokKind::Ident {
                name = t.text.clone();
                self.pos += 1;
            }
        }
        let mut f = self.function_rest(async_)?;
        f.name = name;
        Ok(f)
    }

    fn template_literal(&mut self, raw: &str) -> PResult {
        let mut quasis = Vec::new();
        let mut exprs = Vec::new();
        for (quasi, expr_src) in split_template(raw) {
            quasis.push(quasi);
            if let Some(src) = expr_src {
                let mut sub = Parser::new(&src)?;
                exprs.push(sub.parse_expression()?);
            }
        }
        Ok(rc(TemplateLiteral { quasis, exprs }))
    }

    fn class_declaration(&mut self) -> Result<ClassNode, JsError> {
        let name = self.expect_ident()?;
        let mut c = ClassNode {
            name,
            superclass: None,
            methods: Vec::new(),
        };
        if self.match_kw("extends") {
            c.superclass = Some(self.expression()?);
        }
        c.methods = self.class_body()?;
        Ok(c)
    }

    fn class_expression(&mut self) -> Result<ClassNode, JsError> {
        let mut c = ClassNode {
            name: String::new(),
            superclass: None,
            methods: Vec::new(),
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
        c.methods = self.class_body()?;
        Ok(c)
    }

    fn class_body(&mut self) -> Result<Vec<ClassMethodNode>, JsError> {
        self.expect_punct("{")?;
        let mut methods = Vec::new();
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
                if t.kind == TokKind::Kw && t.text == "static" {
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
            let name = self.expect_property_name()?;
            if !self.peek_is_punct("(") {
                return self.syntax("expected '(' in class method");
            }
            let (params, defaults, rest) = self.param_list()?;
            // `await` is only a keyword inside an async body, and a class body
            // parses its methods here rather than through `function_rest`, so
            // the depth has to be carried over the body by hand.
            if is_async {
                self.async_depth += 1;
            }
            let body = self.parse_stmts_until(Some("}"));
            if is_async {
                self.async_depth -= 1;
            }
            let body = body?;
            methods.push(ClassMethodNode {
                name,
                params,
                defaults,
                rest,
                body,
                is_static,
                accessor,
                is_async,
            });
        }
        Ok(methods)
    }
}

fn rc(n: Node) -> Rc<Node> {
    Rc::new(n)
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
        ForIn { var_kind, name: var, iterable, body, .. } => ForIn {
            var_kind: var_kind.clone(),
            name: var.clone(),
            iterable: iterable.clone(),
            body: body.clone(),
            label: Some(name.to_string()),
        },
        ForOf { var_kind, name: var, iterable, body, .. } => ForOf {
            var_kind: var_kind.clone(),
            name: var.clone(),
            iterable: iterable.clone(),
            body: body.clone(),
            label: Some(name.to_string()),
        },
        _ => return node,
    };
    rc(labelled)
}


/// Split template raw source into [(quasi, expr_source|None), ...].
fn split_template(raw: &str) -> Vec<(String, Option<String>)> {
    let mut parts = Vec::new();
    let mut buf = String::new();
    let chars: Vec<char> = raw.chars().collect();
    let mut i = 0usize;
    let n = chars.len();
    while i < n {
        let ch = chars[i];
        if ch == '\\' {
            if i + 1 < n {
                buf.push(ch);
                buf.push(chars[i + 1]);
                i += 2;
                continue;
            }
        }
        if ch == '$' && i + 1 < n && chars[i + 1] == '{' {
            let mut j = i + 2;
            let mut d = 1i64;
            let mut q: Option<char> = None;
            while j < n {
                let c = chars[j];
                if let Some(quote) = q {
                    if c == '\\' {
                        j += 2;
                        continue;
                    }
                    if c == quote {
                        q = None;
                    }
                } else if c == '\'' || c == '"' || c == '`' {
                    q = Some(c);
                } else if c == '{' {
                    d += 1;
                } else if c == '}' {
                    d -= 1;
                    if d == 0 {
                        break;
                    }
                }
                j += 1;
            }
            if j >= n {
                buf.push(ch);
                i += 1;
                continue;
            }
            let expr: String = chars[i + 2..j].iter().collect();
            parts.push((buf.clone(), Some(expr)));
            buf.clear();
            i = j + 1;
        } else {
            buf.push(ch);
            i += 1;
        }
    }
    parts.push((buf, None));
    parts
}

/// Convenience entry point used by the interpreter to parse a program.
pub fn parse_program(source: &str) -> Result<Rc<Node>, JsError> {
    let mut p = Parser::new(source)?;
    p.parse_program()
}