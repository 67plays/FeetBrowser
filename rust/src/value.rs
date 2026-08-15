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

/// A JS array: the elements, plus the handful of named properties an array is
/// still allowed to carry. Almost nothing puts anything in `props` -- but a
/// regexp match result is an array with `index`, `input` and `groups` hanging
/// off it, and that is not a shape the element vector alone can express.
///
/// `borrow`/`borrow_mut` reach the elements, because that is what nearly every
/// caller wants and reading `a.borrow()` as "the array's contents" keeps the
/// call sites honest about which half they mean.
pub struct JsArray {
    pub items: RefCell<Vec<JsValue>>,
    pub props: RefCell<BTreeMap<String, JsValue>>,
}

impl JsArray {
    pub fn new(items: Vec<JsValue>) -> JsArray {
        JsArray {
            items: RefCell::new(items),
            props: RefCell::new(BTreeMap::new()),
        }
    }

    pub fn borrow(&self) -> std::cell::Ref<'_, Vec<JsValue>> {
        self.items.borrow()
    }

    pub fn borrow_mut(&self) -> std::cell::RefMut<'_, Vec<JsValue>> {
        self.items.borrow_mut()
    }
}

/// A native host function/constructor object.
pub struct Native {
    pub name: Rc<str>,
    pub call: Option<NativeFn>,
    pub ctor: Option<NativeFn>,
    pub get: Option<NativeGet>,
    pub set: Option<NativeSet>,
    /// Set when this value is a method that was read off a receiver rather
    /// than a free function: `"abc".slice` remembers `"abc"`.
    ///
    /// A method here is built as a closure that has already captured the value
    /// it was found on, which is exactly right until someone takes it off one
    /// value and runs it on another -- `Function.prototype.call.apply(fn,
    /// args)`, the shape every polyfill bundle is made of. Remembering where
    /// the method came from is what lets `.call`, `.apply` and `.bind` put a
    /// different receiver underneath it.
    ///
    /// The three parts are the receiver, what to run for it, and whether that
    /// second thing takes the receiver as its first argument. Most methods are
    /// bound closures and are re-bound by looking the name up again on the new
    /// receiver -- `"abc".slice` on another string finds that string's own
    /// `slice`. The ones flagged generic cannot be: `({}).toString` applied to
    /// an array has to stay `Object.prototype.toString` and answer `[object
    /// Array]`, not turn into the array's own `toString` and answer `1,2`, so
    /// those keep one receiver-first implementation and only swap the value in
    /// front of it.
    pub method_of: Option<(JsValue, JsValue, bool)>,
}

impl Native {
    pub fn new(name: &str) -> Native {
        Native {
            name: Rc::from(name),
            call: None,
            ctor: None,
            get: None,
            set: None,
            method_of: None,
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
    Array(Rc<JsArray>),
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
    /// Not a value a program can ever hold: it is what sits in a property slot
    /// that was defined with `get`/`set`, and `js_get`/`js_set` unwrap it by
    /// running the accessor. Storing it inline like this is what lets objects,
    /// class prototypes, statics and instance property bags all keep being
    /// plain `name -> value` maps.
    Accessor(Rc<JsAccessor>),
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
            JsValue::Accessor(_) => write!(f, "accessor"),
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
        JsValue::Array(Rc::new(JsArray::new(vals)))
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

/// Coerce to uint32 (ToUint32), used by Array.prototype.split limits.
pub fn to_uint32(v: &JsValue) -> u32 {
    let n = to_number(v) as u64 & 0xFFFF_FFFF;
    n as u32
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
        JsValue::Accessor(a) => format!("obj:{:p}", Rc::as_ptr(a)),
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
        | JsValue::Accessor(_)
        | JsValue::Super(_)
        | JsValue::Host(_) => "object",
        // A Native is whatever it was built to be. `Math`, `JSON`, `console`
        // and `window` are all Natives and none of them is callable, so
        // answering "function" for the lot of them told a feature test the
        // opposite of the truth -- and `typeof window == "function"` is the
        // sort of answer that sends a bundle down a code path written for
        // some other host entirely.
        JsValue::Native(n) => {
            if n.call.is_some() || n.ctor.is_some() {
                "function"
            } else {
                "object"
            }
        }
        JsValue::Callback(_) => "function",
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

    /// `assign`, in the shape the loop and destructuring binders want: they
    /// hold a `fn` pointer that also stands for `set_var`/`set_let`/`set_const`
    /// and so cannot report anything. Assigning to a `const` is the only thing
    /// `assign` complains about, and a loop head that does it is a program that
    /// was already wrong before it got here.
    pub fn assign_loop_var(&self, name: &str, value: JsValue) {
        let _ = self.assign(name, value);
    }

    pub fn assign(&self, name: &str, value: JsValue) -> Result<(), JsError> {
        let mut env: Env = Rc::new(self.clone());
        loop {
            if env.lets.borrow().contains_key(name) {
                env.lets.borrow_mut().insert(name.to_string(), value);
                return Ok(());
            }
            if env.consts.borrow().contains_key(name) {
                return Err(JsError::js(format!(
                    "Assignment to constant variable '{name}'."
                )));
            }
            if env.vars.borrow().contains_key(name) {
                env.vars.borrow_mut().insert(name.to_string(), value);
                return Ok(());
            }
            let parent = env.parent.clone();
            match parent {
                Some(p) => env = p,
                None => break,
            }
        }
        // Nothing up the chain owns the name, so this is sloppy mode's implicit
        // global: `x = 1` with no declaration anywhere makes a property of the
        // global object, however deep inside functions and blocks it happens.
        // The binding used to land in the innermost scope instead, which reads
        // as correct right up until the next statement -- the assignment
        // appears to work, and then the value vanishes the moment the block
        // ends. Bundled code leans on this more than you would hope: an IIFE
        // that publishes its namespace with a bare `MyLib = {...}` and a later
        // script that reads it are a pairing older minifiers emit freely, and
        // one that used to leave the second script staring at undefined.
        env.vars.borrow_mut().insert(name.to_string(), value);
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
    /// `function*`. Calling one runs the body to the end and collects what it
    /// yielded, rather than suspending it -- see `run_generator`.
    pub generator: bool,
    pub super_info: Option<(JsValue, JsValue)>,
    pub prototype: RefCell<Option<Rc<RefCell<BTreeMap<String, JsValue>>>>>,
    /// Properties hung on the function itself. A function is an object, and
    /// the pre-class way to write a static member -- `Foo.create = ...`,
    /// `Foo.VERSION = 3`, a memo cache on `f.cache` -- is to hang it here.
    /// Writes used to be dropped on the floor unless the name was `prototype`,
    /// so the value read back as undefined and the library that wrote it
    /// looked broken for reasons nowhere near where it had gone wrong.
    pub props: RefCell<BTreeMap<String, JsValue>>,
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
        match value {
            JsValue::Object(map) => *self.prototype.borrow_mut() = Some(map),
            // `C.prototype = Object.create(P.prototype)` and its older sibling
            // `C.prototype = new P()` are how inheritance was spelled for the
            // fifteen years of JavaScript that most shipped bundles were
            // written in, and both hand over an object that already has a
            // prototype of its own. Taking only the literal-object form meant
            // the assignment did nothing at all: the subclass kept its own
            // empty prototype, and every inherited method was undefined.
            //
            // The instance's own property map becomes the prototype -- shared,
            // not copied, so `C.prototype.m = ...` afterwards is visible
            // through both -- with a `__proto__` link added so the lookup
            // carries on up to the parent.
            JsValue::Instance(inst) => {
                let (own, parent) = {
                    let i = inst.borrow();
                    (i.props.clone(), i.proto.clone())
                };
                own.borrow_mut()
                    .insert("__proto__".to_string(), JsValue::Object(parent));
                *self.prototype.borrow_mut() = Some(own);
            }
            _ => {}
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
    /// Instance field initialisers, already reduced to a name and the
    /// expression that produces the value, in source order. They cannot be
    /// evaluated when the class is defined the way a static field can: each
    /// one belongs to a different object and may read `this`, so the
    /// expression has to be kept and re-run for every instance made. The
    /// environment it closes over is kept beside it because the class body's
    /// scope is gone by the time anything calls `new`.
    pub fields: Vec<(String, Option<Rc<crate::ast::Node>>)>,
    pub field_env: Option<Env>,
}

#[derive(Debug)]
pub struct JsClassInstance {
    pub proto: Rc<RefCell<BTreeMap<String, JsValue>>>,
    pub props: Rc<RefCell<BTreeMap<String, JsValue>>>,
}

/// The two halves of an accessor property. Either may be missing: a getter
/// with no setter silently swallows writes, and a setter with no getter reads
/// back as `undefined`, which is exactly what the language says should happen.
#[derive(Debug, Default)]
pub struct JsAccessor {
    pub get: RefCell<Option<JsValue>>,
    pub set: RefCell<Option<JsValue>>,
}

#[derive(Debug)]
pub struct JsSuper {
    pub this: JsValue,
    pub parent_proto: JsValue,
    pub parent_ctor: JsValue,
}

/// A Map keyed by `map_key`'s string form of the key, holding the key itself
/// alongside the value.
///
/// The string is only an identity: `map_key` gives objects their address and
/// primitives their spelling, which is what makes `m.get(k)` a lookup rather
/// than a scan. It cannot be handed back, though -- `m.keys()`, `m.forEach`
/// and `for (const [k, v] of m)` all want the original value, and a Map whose
/// keys are objects (the case Map exists for at all) has nothing to
/// reconstruct them from. So the key rides along with the value.
#[derive(Debug)]
pub struct JsMap {
    pub store: RefCell<BTreeMap<String, (JsValue, JsValue)>>,
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

/// A date is a single number: milliseconds since the epoch. Every field a
/// script can ask for is derived from it on demand by `date_parts`, so there
/// is nothing here that can drift out of step with `ms` when it is written to.
pub struct JsDate {
    pub ms: f64,
}

pub struct JsRegex {
    pub source: String,
    pub flags: String,
    pub global_: bool,
    pub ignore_case: bool,
    pub multiline: bool,
    pub last_index: Cell<f64>,
    pub re: crate::regexp::Regex,
}