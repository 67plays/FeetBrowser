//! JS value model, environment, functions, promises, and shared helpers.
//!
//! This mirrors `jsengine.py`'s value model. Host objects (Python DOM nodes,
//! native callables) are held as `Py<PyAny>` so the interpreter can call back
//! into Python through the PyO3 bridge.

use crate::interp::{EvResult, Interpreter};
use pyo3::prelude::*;
use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::rc::Rc;

pub const MAX_STEPS: u64 = 8_000_000;
pub const MAX_ARRAY_LEN: usize = 1_000_000;
pub const MAX_STRING_OUT: usize = 32_000_000;
pub const MAX_TIMERS: usize = 10_000;
pub const MAX_DRAIN: usize = 1_000_000;

#[derive(Debug, Clone)]
pub enum JsError {
    /// An ordinary JS error (JSException-like).
    Js(String),
    /// A thrown JS value (the `throw` statement).
    Thrown(JsValue),
    /// Control-flow signals, each optionally aimed at a named statement.
    Break(Option<String>),
    Continue(Option<String>),
    Return(JsValue),
    Budget(String),
}

impl JsError {
    pub fn js(msg: impl Into<String>) -> Self {
        JsError::Js(msg.into())
    }
}

pub type NativeFn = fn(&Rc<Interpreter>, &JsValue, Vec<JsValue>) -> EvResult;
pub type NativeGet = fn(&Rc<Interpreter>, &JsValue, &str) -> Result<JsValue, JsError>;
pub type NativeSet = fn(&Rc<Interpreter>, &JsValue, &str, &JsValue) -> Result<(), JsError>;

/// A native host function/constructor object.
pub struct Native {
    pub name: Rc<str>,
    pub call: Option<NativeFn>,
    pub ctor: Option<NativeFn>,
    pub get: Option<NativeGet>,
    pub set: Option<NativeSet>,
}

impl Native {
    pub fn new(name: &str) -> Native {
        Native {
            name: Rc::from(name),
            call: None,
            ctor: None,
            get: None,
            set: None,
        }
    }
}

/// A Rust closure exposed to JS as a callable value (e.g. promise handlers).
pub trait JsCallback: 'static {
    fn call(&self, interp: &Rc<Interpreter>, args: Vec<JsValue>) -> EvResult;
}

impl<F> JsCallback for F
where
    F: Fn(&Rc<Interpreter>, Vec<JsValue>) -> EvResult + 'static,
{
    fn call(&self, interp: &Rc<Interpreter>, args: Vec<JsValue>) -> EvResult {
        self(interp, args)
    }
}

#[derive(Clone)]
pub enum JsValue {
    Undefined,
    Null,
    Bool(bool),
    Number(f64),
    Str(Rc<str>),
    Array(Rc<RefCell<Vec<JsValue>>>),
    Object(Rc<RefCell<BTreeMap<String, JsValue>>>),
    Function(Rc<JSFunction>),
    Promise(Rc<RefCell<JsPromise>>),
    Class(Rc<RefCell<JsClass>>),
    Instance(Rc<RefCell<JsClassInstance>>),
    Map(Rc<RefCell<JsMap>>),
    Set(Rc<RefCell<JsSet>>),
    Date(Rc<RefCell<JsDate>>),
    Regex(Rc<RefCell<JsRegex>>),
    Error(Rc<RefCell<JsHostError>>),
    Super(Rc<JsSuper>),
    Native(Rc<Native>),
    Callback(Rc<dyn JsCallback>),
    Host(Py<PyAny>),
}

impl std::fmt::Debug for JsValue {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            JsValue::Undefined => write!(f, "undefined"),
            JsValue::Null => write!(f, "null"),
            JsValue::Bool(b) => write!(f, "{b}"),
            JsValue::Number(n) => write!(f, "{n}"),
            JsValue::Str(s) => write!(f, "{:?}", s.as_ref()),
            JsValue::Array(_) => write!(f, "Array"),
            JsValue::Object(_) => write!(f, "[object Object]"),
            JsValue::Function(fun) => write!(f, "function {}", fun.name),
            JsValue::Promise(_) => write!(f, "Promise"),
            JsValue::Class(c) => write!(f, "class {}", c.borrow().name),
            JsValue::Instance(_) => write!(f, "Instance"),
            JsValue::Map(_) => write!(f, "Map"),
            JsValue::Set(_) => write!(f, "Set"),
            JsValue::Date(_) => write!(f, "Date"),
            JsValue::Regex(_) => write!(f, "RegExp"),
            JsValue::Error(_) => write!(f, "Error"),
            JsValue::Super(_) => write!(f, "super"),
            JsValue::Native(n) => write!(f, "function {}", n.name),
            JsValue::Callback(_) => write!(f, "function"),
            JsValue::Host(_) => write!(f, "host"),
        }
    }
}

impl JsValue {
    pub fn undefined() -> JsValue {
        JsValue::Undefined
    }

    pub fn object() -> JsValue {
        JsValue::Object(Rc::new(RefCell::new(BTreeMap::new())))
    }

    pub fn array(vals: Vec<JsValue>) -> JsValue {
        JsValue::Array(Rc::new(RefCell::new(vals)))
    }

    pub fn str(text: impl Into<String>) -> JsValue {
        JsValue::Str(Rc::from(text.into()))
    }

    pub fn number(n: f64) -> JsValue {
        JsValue::Number(n)
    }
}

pub fn nullish(v: &JsValue) -> bool {
    matches!(v, JsValue::Undefined | JsValue::Null)
}

pub fn is_objectish(v: &JsValue) -> bool {
    !matches!(
        v,
        JsValue::Undefined
            | JsValue::Null
            | JsValue::Number(_)
            | JsValue::Str(_)
            | JsValue::Bool(_)
    )
}

pub fn truthy(v: &JsValue) -> bool {
    match v {
        JsValue::Undefined | JsValue::Null | JsValue::Bool(false) => false,
        JsValue::Number(n) => *n != 0.0 && n == n,
        JsValue::Str(s) => !s.is_empty(),
        _ => true,
    }
}

pub fn is_numberish(v: &JsValue) -> bool {
    matches!(v, JsValue::Number(_) | JsValue::Str(_))
}

/// ToNumber for loose equality and arithmetic coercion.
pub fn to_number(v: &JsValue) -> f64 {
    match v {
        JsValue::Undefined => f64::NAN,
        JsValue::Null => 0.0,
        JsValue::Bool(b) => {
            if *b {
                1.0
            } else {
                0.0
            }
        }
        JsValue::Number(n) => *n,
        JsValue::Str(s) => {
            let text = s.trim();
            if text.is_empty() {
                0.0
            } else {
                parse_number(text)
            }
        }
        _ => f64::NAN,
    }
}

/// Coerce to int32 (ToInt32) used by bitwise ops and array indexes.
pub fn to_int32(v: &JsValue) -> i32 {
    let n = to_number(v) as i64 & 0xFFFF_FFFF;
    if n & (1 << 31) != 0 {
        (n - (1 << 32)) as i32
    } else {
        n as i32
    }
}

pub fn parse_number(text: &str) -> f64 {
    let text = if let Some(t) = text.strip_prefix('.') {
        format!("0{t}")
    } else if let Some(t) = text.strip_suffix('.') {
        t.to_string()
    } else {
        text.to_string()
    };
    if all_digits(&text) {
        text.parse::<i64>().map(|i| i as f64).unwrap_or_else(|_| {
            text.parse::<f64>().unwrap_or(f64::NAN)
        })
    } else {
        text.parse::<f64>().unwrap_or(f64::NAN)
    }
}

pub fn all_digits(text: &str) -> bool {
    !text.is_empty() && text.chars().all(|c| c.is_ascii_digit())
}

/// An integer array index if `name` is a canonical decimal integer string.
pub fn int_index(name: &str) -> Option<i64> {
    let index: i64 = name.parse().ok()?;
    if name == index.to_string() {
        Some(index)
    } else {
        None
    }
}

pub fn divide(a: f64, b: f64) -> f64 {
    if b == 0.0 {
        if a == 0.0 {
            return f64::NAN;
        }
        return if a > 0.0 { f64::INFINITY } else { f64::NEG_INFINITY };
    }
    a / b
}

pub fn modulo(a: f64, b: f64) -> f64 {
    if b == 0.0 {
        return f64::NAN;
    }
    a % b
}

/// A hashable key for Map/Set that treats primitives by value and objects by
/// identity, mirroring `_map_key`.
pub fn map_key(v: &JsValue) -> String {
    match v {
        JsValue::Undefined => "u".to_string(),
        JsValue::Null => "n".to_string(),
        JsValue::Bool(b) => format!("b:{b}"),
        JsValue::Number(n) => {
            if n.is_nan() {
                "num:nan".to_string()
            } else {
                format!("num:{n:?}")
            }
        }
        JsValue::Str(s) => format!("s:{}", s.as_ref()),
        JsValue::Object(o) => format!("obj:{:p}", Rc::as_ptr(o)),
        JsValue::Array(a) => format!("obj:{:p}", Rc::as_ptr(a)),
        JsValue::Function(f) => format!("obj:{:p}", Rc::as_ptr(f)),
        JsValue::Promise(p) => format!("obj:{:p}", Rc::as_ptr(p)),
        JsValue::Class(c) => format!("obj:{:p}", Rc::as_ptr(c)),
        JsValue::Instance(i) => format!("obj:{:p}", Rc::as_ptr(i)),
        JsValue::Map(m) => format!("obj:{:p}", Rc::as_ptr(m)),
        JsValue::Set(s) => format!("obj:{:p}", Rc::as_ptr(s)),
        JsValue::Date(d) => format!("obj:{:p}", Rc::as_ptr(d)),
        JsValue::Regex(r) => format!("obj:{:p}", Rc::as_ptr(r)),
        JsValue::Error(e) => format!("obj:{:p}", Rc::as_ptr(e)),
        JsValue::Super(s) => format!("obj:{:p}", Rc::as_ptr(s)),
        JsValue::Native(n) => format!("obj:{:p}", Rc::as_ptr(n)),
        JsValue::Callback(_) => format!("obj:{:p}", std::ptr::addr_of!(*v)),
        JsValue::Host(h) => format!("obj:{:p}", h.as_ptr()),
    }
}

pub fn safe_char(text: &str, i: i64) -> String {
    if i < 0 {
        return String::new();
    }
    text.chars().nth(i as usize).map(String::from).unwrap_or_default()
}

pub fn safe_code(text: &str, i: i64) -> f64 {
    if i < 0 {
        return f64::NAN;
    }
    text.chars().nth(i as usize).map(|c| c as u32 as f64).unwrap_or(f64::NAN)
}

pub fn js_pad(text: &str, length: i64, fill: &str, left: bool) -> Result<String, JsError> {
    if fill.is_empty() {
        return Ok(text.to_string());
    }
    let need = length - text.chars().count() as i64;
    if need <= 0 {
        return Ok(text.to_string());
    }
    if need as usize > MAX_STRING_OUT {
        return Err(JsError::js("String padding result is too large"));
    }
    let fill_len = fill.chars().count();
    let reps = (need as usize / fill_len) + 1;
    let mut padded = fill.repeat(reps);
    padded.truncate(need as usize);
    if left {
        Ok(padded + text)
    } else {
        Ok(text.to_string() + &padded)
    }
}

pub fn is_js_function(v: &JsValue) -> bool {
    matches!(
        v,
        JsValue::Function(_) | JsValue::Native(_) | JsValue::Callback(_) | JsValue::Host(_)
    )
}

/// Reference identity for object-like values, mirroring Python `is`.
pub fn same_ref(a: &JsValue, b: &JsValue) -> bool {
    use JsValue::*;
    match (a, b) {
        (Object(x), Object(y)) => Rc::ptr_eq(x, y),
        (Array(x), Array(y)) => Rc::ptr_eq(x, y),
        (Function(x), Function(y)) => Rc::ptr_eq(x, y),
        (Promise(x), Promise(y)) => Rc::ptr_eq(x, y),
        (Class(x), Class(y)) => Rc::ptr_eq(x, y),
        (Instance(x), Instance(y)) => Rc::ptr_eq(x, y),
        (Map(x), Map(y)) => Rc::ptr_eq(x, y),
        (Set(x), Set(y)) => Rc::ptr_eq(x, y),
        (Date(x), Date(y)) => Rc::ptr_eq(x, y),
        (Regex(x), Regex(y)) => Rc::ptr_eq(x, y),
        (Error(x), Error(y)) => Rc::ptr_eq(x, y),
        (Super(x), Super(y)) => Rc::ptr_eq(x, y),
        (Native(x), Native(y)) => Rc::ptr_eq(x, y),
        (Host(x), Host(y)) => x.is(y),
        _ => false,
    }
}

pub fn loose_eq(a: &JsValue, b: &JsValue) -> bool {
    let na = nullish(a);
    let nb = nullish(b);
    if na || nb {
        return na && nb;
    }
    if let (JsValue::Str(x), JsValue::Str(y)) = (a, b) {
        return x == y;
    }
    if is_numberish(a) || is_numberish(b) {
        let ca = to_number(a);
        let cb = to_number(b);
        if ca != ca || cb != cb {
            return false;
        }
        return ca == cb;
    }
    if is_objectish(a) && is_objectish(b) {
        return same_ref(a, b);
    }
    false
}

pub fn strict_eq(a: &JsValue, b: &JsValue) -> bool {
    let ta = js_typeof(a);
    let tb = js_typeof(b);
    if ta != tb {
        return false;
    }
    if ta == "object" || ta == "function" {
        return same_ref(a, b);
    }
    match (a, b) {
        (JsValue::Number(x), JsValue::Number(y)) => {
            if x.is_nan() || y.is_nan() {
                false
            } else {
                x == y
            }
        }
        (JsValue::Str(x), JsValue::Str(y)) => x == y,
        (JsValue::Bool(x), JsValue::Bool(y)) => x == y,
        (JsValue::Undefined, JsValue::Undefined) => true,
        (JsValue::Null, JsValue::Null) => true,
        _ => same_ref(a, b),
    }
}

pub fn js_typeof(v: &JsValue) -> &'static str {
    match v {
        JsValue::Undefined => "undefined",
        JsValue::Null => "object",
        JsValue::Bool(_) => "boolean",
        JsValue::Str(_) => "string",
        JsValue::Number(_) => "number",
        JsValue::Function(_) => "function",
        JsValue::Class(_)
        | JsValue::Instance(_)
        | JsValue::Array(_)
        | JsValue::Object(_)
        | JsValue::Promise(_)
        | JsValue::Map(_)
        | JsValue::Set(_)
        | JsValue::Date(_)
        | JsValue::Regex(_)
        | JsValue::Error(_)
        | JsValue::Super(_)
        | JsValue::Host(_) => "object",
        JsValue::Native(_) | JsValue::Callback(_) => "function",
    }
}

/// The JS `typeof` result, which treats host callables/newables as functions.
pub fn typeof_value(py: Python<'_>, v: &JsValue) -> String {
    match v {
        JsValue::Host(obj) => {
            if is_py_function(py, obj) {
                "function".to_string()
            } else {
                "object".to_string()
            }
        }
        _ => js_typeof(v).to_string(),
    }
}

pub fn is_py_function(py: Python<'_>, obj: &Py<PyAny>) -> bool {
    let obj = obj.bind(py);
    if obj.is_callable() {
        return true;
    }
    if obj.getattr("js_call").is_ok() || obj.getattr("js_new").is_ok() {
        return true;
    }
    false
}

/// A binding environment (lexical scope chain).
#[derive(Clone)]
pub struct Environment {
    pub parent: Option<Env>,
    pub vars: Rc<RefCell<BTreeMap<String, JsValue>>>,
    pub lets: Rc<RefCell<BTreeMap<String, JsValue>>>,
    pub consts: Rc<RefCell<BTreeMap<String, JsValue>>>,
    pub function_scope: RefCell<Option<Env>>,
}

impl std::fmt::Debug for Environment {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Environment")
            .field("vars", &self.vars)
            .field("lets", &self.lets)
            .field("consts", &self.consts)
            .finish()
    }
}

pub type Env = Rc<Environment>;

impl Environment {
    pub fn new(parent: Option<Env>) -> Env {
        let fs = match &parent {
            Some(p) => p.function_scope.borrow().clone(),
            None => None,
        };
        Rc::new(Environment {
            parent,
            vars: Rc::new(RefCell::new(BTreeMap::new())),
            lets: Rc::new(RefCell::new(BTreeMap::new())),
            consts: Rc::new(RefCell::new(BTreeMap::new())),
            function_scope: RefCell::new(fs),
        })
    }

    pub fn from_vars(vars: Rc<RefCell<BTreeMap<String, JsValue>>>) -> Env {
        Rc::new(Environment {
            parent: None,
            vars,
            lets: Rc::new(RefCell::new(BTreeMap::new())),
            consts: Rc::new(RefCell::new(BTreeMap::new())),
            function_scope: RefCell::new(None),
        })
    }

    /// A fresh function-invocation scope: its own var scope.
    pub fn function(parent: Option<Env>) -> Env {
        let env = Environment::new(parent);
        env.function_scope.borrow_mut().replace(env.clone());
        env
    }

    pub fn set_var(&self, name: &str, value: JsValue) {
        let scope = self
            .function_scope
            .borrow()
            .clone()
            .unwrap_or_else(|| Rc::new(self.clone()));
        scope.vars.borrow_mut().insert(name.to_string(), value);
    }

    pub fn set_let(&self, name: &str, value: JsValue) {
        self.lets.borrow_mut().insert(name.to_string(), value);
    }

    pub fn set_const(&self, name: &str, value: JsValue) {
        self.consts.borrow_mut().insert(name.to_string(), value);
    }

    pub fn get(&self, name: &str) -> JsValue {
        let mut env: Option<Env> = Some(Rc::new(self.clone()));
        while let Some(e) = env {
            if let Some(v) = e.lets.borrow().get(name) {
                return v.clone();
            }
            if let Some(v) = e.consts.borrow().get(name) {
                return v.clone();
            }
            if let Some(v) = e.vars.borrow().get(name) {
                return v.clone();
            }
            env = e.parent.clone();
        }
        JsValue::Undefined
    }

    pub fn assign(&self, name: &str, value: JsValue) -> Result<(), JsError> {
        let mut env: Option<Env> = Some(Rc::new(self.clone()));
        while let Some(e) = env {
            if e.lets.borrow().contains_key(name) {
                e.lets.borrow_mut().insert(name.to_string(), value);
                return Ok(());
            }
            if e.consts.borrow().contains_key(name) {
                return Err(JsError::js(format!(
                    "Assignment to constant variable '{name}'."
                )));
            }
            if e.vars.borrow().contains_key(name) {
                e.vars.borrow_mut().insert(name.to_string(), value);
                return Ok(());
            }
            env = e.parent.clone();
        }
        self.vars.borrow_mut().insert(name.to_string(), value);
        Ok(())
    }
}

/// A JS function value.
#[derive(Debug)]
pub struct JSFunction {
    pub name: String,
    pub params: Vec<String>,
    pub defaults: BTreeMap<String, Rc<crate::ast::Node>>,
    pub rest: Option<String>,
    pub body: Vec<Rc<crate::ast::Node>>,
    pub body_expr: Option<Rc<crate::ast::Node>>,
    pub env: Env,
    pub async_: bool,
    pub arrow: bool,
    pub super_info: Option<(JsValue, JsValue)>,
    pub prototype: RefCell<Option<Rc<RefCell<BTreeMap<String, JsValue>>>>>,
}

impl JSFunction {
    pub fn prototype_obj(this: &Rc<JSFunction>) -> Rc<RefCell<BTreeMap<String, JsValue>>> {
        if let Some(p) = &*this.prototype.borrow() {
            return p.clone();
        }
        let p = Rc::new(RefCell::new(BTreeMap::new()));
        p.borrow_mut()
            .insert("constructor".to_string(), JsValue::Function(this.clone()));
        *this.prototype.borrow_mut() = Some(p.clone());
        p
    }

    pub fn set_prototype(&self, value: JsValue) {
        if let JsValue::Object(map) = value {
            *self.prototype.borrow_mut() = Some(map);
        }
    }
}

/// A JS Promise.
pub struct JsPromise {
    pub state: RefCell<PromiseState>,
    pub observers: RefCell<Vec<Rc<dyn Fn(JsValue, bool)>>>,
}

pub enum PromiseState {
    Pending,
    Resolved(JsValue),
    Rejected(JsValue),
}

impl JsPromise {
    pub fn new() -> Rc<RefCell<JsPromise>> {
        Rc::new(RefCell::new(JsPromise {
            state: RefCell::new(PromiseState::Pending),
            observers: RefCell::new(Vec::new()),
        }))
    }

    pub fn is_pending(&self) -> bool {
        matches!(*self.state.borrow(), PromiseState::Pending)
    }

    pub fn value(&self) -> JsValue {
        match &*self.state.borrow() {
            PromiseState::Resolved(v) | PromiseState::Rejected(v) => v.clone(),
            PromiseState::Pending => JsValue::Undefined,
        }
    }

    pub fn rejected(&self) -> bool {
        matches!(*self.state.borrow(), PromiseState::Rejected(_))
    }

    pub fn resolved(&self) -> bool {
        matches!(*self.state.borrow(), PromiseState::Resolved(_))
    }
}

#[derive(Debug)]
pub struct JsClass {
    pub name: String,
    pub prototype: Rc<RefCell<BTreeMap<String, JsValue>>>,
    pub ctor: Option<Rc<JSFunction>>,
    pub parent: Option<JsValue>,
    pub statics: Rc<RefCell<BTreeMap<String, JsValue>>>,
}

#[derive(Debug)]
pub struct JsClassInstance {
    pub proto: Rc<RefCell<BTreeMap<String, JsValue>>>,
    pub props: Rc<RefCell<BTreeMap<String, JsValue>>>,
}

#[derive(Debug)]
pub struct JsSuper {
    pub this: JsValue,
    pub parent_proto: JsValue,
    pub parent_ctor: JsValue,
}

#[derive(Debug)]
pub struct JsMap {
    pub store: RefCell<BTreeMap<String, JsValue>>,
}

#[derive(Debug)]
pub struct JsSet {
    pub store: RefCell<BTreeMap<String, JsValue>>,
}

#[derive(Debug)]
pub struct JsHostError {
    pub message: String,
    pub name: String,
}

pub struct JsDate {
    pub ms: f64,
    pub local: Option<chrono::NaiveDateTime>,
    pub utc: Option<chrono::DateTime<chrono::Utc>>,
}

pub struct JsRegex {
    pub source: String,
    pub flags: String,
    pub global_: bool,
    pub ignore_case: bool,
    pub multiline: bool,
    pub last_index: Cell<f64>,
    pub re: regex::Regex,
}