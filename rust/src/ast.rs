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
    /// `[a, b] = pair` and `({x, y} = point)`, which are assignments and not
    /// declarations: nothing new is bound, the names already exist. It is a
    /// node of its own because the left-hand side is a pattern rather than an
    /// expression, even though it was written as one.
    AssignPattern { target: DeclTarget, value: Rc<Node> },
    Call { callee: Rc<Node>, args: Vec<Rc<Node>>, optional: bool },
    New { callee: Rc<Node>, args: Vec<Rc<Node>> },
    Member { obj: Rc<Node>, name: String, optional: bool },
    Index { obj: Rc<Node>, index: Rc<Node>, optional: bool },
    FunctionExpr(FuncNode),
    Spread(Rc<Node>),
    Pattern(PatternNode),
    TemplateLiteral { quasis: Vec<String>, exprs: Vec<Rc<Node>> },
    /// ``tag`a${b}c` ``. The tag is handed the literal pieces as an array and
    /// the substitutions as the remaining arguments, so both halves have to
    /// survive parsing separately -- and the pieces are kept twice, cooked and
    /// raw, because `String.raw` is a tag whose whole job is to read the
    /// second copy.
    TaggedTemplate {
        tag: Rc<Node>,
        quasis: Vec<String>,
        raws: Vec<String>,
        exprs: Vec<Rc<Node>>,
    },
    ArrowFunc(FuncNode),
    ClassExpr(ClassNode),
    ClassMethod(ClassMethodNode),
    Super,
    Await(Rc<Node>),
    /// `yield v` and `yield* other`. Only ever inside a generator body: the
    /// parser reads `yield` as a plain identifier anywhere else, which is what
    /// it is in a language that never made it a reserved word.
    Yield { arg: Option<Rc<Node>>, delegate: bool },
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
    /// The loop variable is a full binding target rather than a name because
    /// `for (const [k, v] of map)` is how anyone walks a Map, and a minifier
    /// writes it for every `Object.entries` loop it can reach.
    ForIn {
        var_kind: Option<String>,
        target: DeclTarget,
        iterable: Rc<Node>,
        body: Rc<Node>,
        label: Option<String>,
    },
    ForOf {
        var_kind: Option<String>,
        target: DeclTarget,
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
    /// `{ get [expr]() {...} }` / `{ set [expr](n) {...} }`. Same as
    /// `Accessor`, but the key is only known once the object is built.
    ComputedAccessor { key_expr: Rc<Node>, kind: String, func: FuncNode },
}

#[derive(Debug, Clone)]
pub struct FuncNode {
    pub name: String,
    pub params: Vec<String>,
    pub defaults: BTreeMap<String, Rc<Node>>,
    pub rest: Option<String>,
    pub body: Vec<Rc<Node>>,
    pub body_expr: Option<Rc<Node>>,
    pub async_: bool,
    pub arrow: bool,
    /// `function* g() {}`. See `run_generator` in the interpreter for what
    /// this engine can and cannot do with one.
    pub is_generator: bool,
}

#[derive(Debug, Clone)]
pub struct ClassNode {
    pub name: String,
    pub superclass: Option<Rc<Node>>,
    pub methods: Vec<ClassMethodNode>,
    pub fields: Vec<ClassFieldNode>,
}

/// `x = 1` or `static styles = css` in a class body: a property rather than a
/// method. A static one is written onto the class as the class is defined; an
/// instance one is written onto every object the class makes.
#[derive(Debug, Clone)]
pub struct ClassFieldNode {
    pub name: String,
    /// `[expr] = v`, the same computed key a method can have.
    pub key: Option<Rc<Node>>,
    /// Absent for a bare `x;`, which declares the field as `undefined`.
    pub value: Option<Rc<Node>>,
    pub is_static: bool,
}

#[derive(Debug, Clone)]
pub struct ClassMethodNode {
    pub name: String,
    /// `[Symbol.iterator]() {}`. The name is not known until the class is
    /// defined, so it carries the expression instead; `name` is empty then.
    pub key: Option<Rc<Node>>,
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
    /// `*next() {}`, including the `*[Symbol.iterator]() {}` that makes a
    /// class walkable.
    pub is_generator: bool,
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
    /// A property, not a variable: `({a: obj.x} = src)`. Only ever produced
    /// when an ordinary expression is re-read as a destructuring assignment,
    /// which is the only place the language allows it -- a `let` or `const`
    /// declares names and nothing else.
    Member(Rc<Node>),
}