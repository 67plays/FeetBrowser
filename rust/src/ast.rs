//! AST node types, ported from `jsengine.py`.

use std::collections::BTreeMap;
use std::rc::Rc;

#[derive(Debug, Clone, PartialEq)]
pub enum LiteralVal {
    Number(f64),
    Str(Rc<str>),
    Bool(bool),
    Null,
    Undefined,
}

#[derive(Debug, Clone)]
pub enum Node {
    // Expressions
    Literal(LiteralVal),
    Identifier(String),
    This,
    ArrayLit(Vec<Rc<Node>>),
    ObjectLit(Vec<ObjectPair>),
    Unary(String, Rc<Node>),
    /// `a, b, c` -- evaluate each in turn, and the value is the last one.
    /// Minifiers lean on it: it is how `return f(x), y` and
    /// `for (i = 0, n = 5;;)` get written once every statement boundary has
    /// been squeezed out.
    Sequence(Vec<Rc<Node>>),
    Update { op: String, operand: Rc<Node>, prefix: bool },
    Binary(String, Rc<Node>, Rc<Node>),
    Logical(String, Rc<Node>, Rc<Node>),
    Conditional { cond: Rc<Node>, then_expr: Rc<Node>, else_expr: Rc<Node> },
    Assign { op: String, target: Rc<Node>, value: Rc<Node> },
    Call { callee: Rc<Node>, args: Vec<Rc<Node>>, optional: bool },
    New { callee: Rc<Node>, args: Vec<Rc<Node>> },
    Member { obj: Rc<Node>, name: String, optional: bool },
    Index { obj: Rc<Node>, index: Rc<Node>, optional: bool },
    FunctionExpr(FuncNode),
    Spread(Rc<Node>),
    Pattern(PatternNode),
    TemplateLiteral { quasis: Vec<String>, exprs: Vec<Rc<Node>> },
    ArrowFunc(FuncNode),
    ClassExpr(ClassNode),
    ClassMethod(ClassMethodNode),
    Super,
    Await(Rc<Node>),
    Regex { source: String, flags: String },

    // Statements
    Program(Vec<Rc<Node>>),
    Block(Vec<Rc<Node>>),
    VarDecl { kind: String, decls: Vec<(DeclTarget, Option<Rc<Node>>)> },
    FunctionDecl(FuncNode),
    ClassDecl(ClassNode),
    ExprStmt(Rc<Node>),
    If { cond: Rc<Node>, then: Rc<Node>, else_: Option<Rc<Node>> },
    While { cond: Rc<Node>, body: Rc<Node>, label: Option<String> },
    DoWhile { body: Rc<Node>, cond: Rc<Node>, label: Option<String> },
    Switch { expr: Rc<Node>, cases: Vec<(String, Option<Rc<Node>>, Vec<Rc<Node>>)> },
    For {
        init: Option<Rc<Node>>,
        cond: Option<Rc<Node>>,
        update: Option<Rc<Node>>,
        body: Rc<Node>,
        label: Option<String>,
    },
    ForIn {
        var_kind: Option<String>,
        name: String,
        iterable: Rc<Node>,
        body: Rc<Node>,
        label: Option<String>,
    },
    ForOf {
        var_kind: Option<String>,
        name: String,
        iterable: Rc<Node>,
        body: Rc<Node>,
        label: Option<String>,
    },
    /// `name: statement`. Only two things ever look at the name: a `break` or
    /// `continue` that spells it out, and the `a: { ... break a }` a minifier
    /// writes where an early return would cost more bytes.
    Labelled { name: String, body: Rc<Node> },
    Return(Option<Rc<Node>>),
    Break(Option<String>),
    Continue(Option<String>),
    Throw(Rc<Node>),
    TryCatch {
        try_block: Rc<Node>,
        catch_param: Option<String>,
        catch_block: Option<Rc<Node>>,
        finally_block: Option<Rc<Node>>,
    },
}

#[derive(Debug, Clone)]
pub enum ObjectPair {
    Key(String, Rc<Node>),
    Spread(Rc<Node>),
    /// `{ [expr]: value }`. The key is not known until the object is built, so
    /// unlike `Key` it carries an expression to evaluate rather than a name.
    Computed(Rc<Node>, Rc<Node>),
    /// `{ get v() {...} }` / `{ set v(n) {...} }`. `kind` is `"get"` or
    /// `"set"`; two pairs with the same name and different kinds describe one
    /// property with both halves, which is why they are not merged here.
    Accessor { key: String, kind: String, func: FuncNode },
}

#[derive(Debug, Clone)]
pub struct FuncNode {    pub name: String,
    pub params: Vec<String>,
    pub defaults: BTreeMap<String, Rc<Node>>,
    pub rest: Option<String>,
    pub body: Vec<Rc<Node>>,
    pub body_expr: Option<Rc<Node>>,
    pub async_: bool,
    pub arrow: bool,
}

#[derive(Debug, Clone)]
pub struct ClassNode {
    pub name: String,
    pub superclass: Option<Rc<Node>>,
    pub methods: Vec<ClassMethodNode>,
}

#[derive(Debug, Clone)]
pub struct ClassMethodNode {
    pub name: String,
    pub params: Vec<String>,
    pub defaults: BTreeMap<String, Rc<Node>>,
    pub rest: Option<String>,
    pub body: Vec<Rc<Node>>,
    pub is_static: bool,
    pub accessor: Option<String>,
    /// `async f() {}`. Carried separately from `accessor` because the two are
    /// mutually exclusive in the grammar -- there is no such thing as an
    /// `async get` -- and because only this one changes how the body runs.
    pub is_async: bool,
}

#[derive(Debug, Clone)]
pub struct PatternNode {
    pub kind: String, // "array" or "object"
    pub parts: Vec<PatternPart>,
    pub rest: Option<DeclTarget>,
}

#[derive(Debug, Clone)]
pub enum PatternPart {
    Array { target: DeclTarget, default: Option<Rc<Node>> },
    Object { key: String, target: DeclTarget, default: Option<Rc<Node>> },
}

#[derive(Debug, Clone)]
pub enum DeclTarget {
    Name(String),
    Pattern(Rc<PatternNode>),
}