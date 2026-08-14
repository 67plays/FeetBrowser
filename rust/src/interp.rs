//! The interpreter core: execution loop, value operations, promise machinery,
//! timers/microtasks, and the PyO3 host bridge.
//!
//! Ported from `jsengine.py::Interpreter`. The evaluator is async; the only
//! true suspension points are `await` inside async functions, driven by a tiny
//! single-threaded task executor whose wakers re-queue suspended tasks.

use crate::ast::*;
use crate::parser;
use crate::value::*;
use pyo3::conversion::IntoPyObjectExt;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, HashMap, VecDeque};
use std::future::Future;
use std::pin::Pin;
use std::rc::Rc;
use std::task::{Context, Poll, RawWaker, RawWakerVTable, Waker};

pub type BoxFut<T> = Pin<Box<dyn Future<Output = T>>>;
pub type EvResult = BoxFut<Result<JsValue, JsError>>;
pub type StResult = BoxFut<Result<(), JsError>>;
pub type Task = BoxFut<()>;

#[derive(Clone)]
pub struct Timer {
    pub id: u64,
    pub due: f64,
    pub fn_: JsValue,
    pub args: Vec<JsValue>,
    pub interval: f64,
    pub repeat: bool,
}

pub struct Interpreter {
    pub globals: Rc<RefCell<BTreeMap<String, JsValue>>>,
    pub global_env: Env,
    pub logs: RefCell<Vec<String>>,
    pub microtasks: RefCell<VecDeque<Rc<dyn Fn()>>>,
    pub timers: RefCell<Vec<Timer>>,
    pub timer_seq: Cell<u64>,
    pub now: Cell<f64>,
    pub steps: Cell<u64>,
    pub tasks: RefCell<HashMap<usize, Task>>,
    pub ready: Rc<RefCell<VecDeque<usize>>>,
    pub next_id: Cell<usize>,
    pub undefined_ref: Py<PyAny>,
    pub js_exception: Py<PyAny>,
    pub local_storage: Rc<RefCell<BTreeMap<String, String>>>,
}

impl Interpreter {
    pub fn new(py: Python<'_>) -> PyResult<Rc<Interpreter>> {
        let globals = Rc::new(RefCell::new(BTreeMap::new()));
        let global_env = Environment::from_vars(globals.clone());
        let jse = py.import("feetbrowser.jsengine")?;
        let undefined_ref = jse.getattr("UNDEFINED")?.unbind();
        let js_exception = jse.getattr("JSException")?.unbind();
        let interp = Rc::new(Interpreter {
            globals,
            global_env,
            logs: RefCell::new(Vec::new()),
            microtasks: RefCell::new(VecDeque::new()),
            timers: RefCell::new(Vec::new()),
            timer_seq: Cell::new(0),
            now: Cell::new(0.0),
            steps: Cell::new(0),
            tasks: RefCell::new(HashMap::new()),
            ready: Rc::new(RefCell::new(VecDeque::new())),
            next_id: Cell::new(0),
            undefined_ref,
            js_exception,
            local_storage: Rc::new(RefCell::new(BTreeMap::new())),
        });
        crate::stdlib::init_globals(&interp)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyException, _>(js_error_message(&interp, &e)))?;
        Ok(interp)
    }
}

// -- tick / steps ----------------------------------------------------------

pub fn tick(this: &Interpreter) -> Result<(), JsError> {
    let s = this.steps.get() + 1;
    this.steps.set(s);
    if s > MAX_STEPS {
        return Err(JsError::Budget(
            "Script exceeded the execution budget (possible infinite loop)"
                .to_string(),
        ));
    }
    Ok(())
}

// -- task executor ---------------------------------------------------------

struct WakeState {
    id: usize,
    ready: Rc<RefCell<VecDeque<usize>>>,
}

static WAKER_VTABLE: RawWakerVTable =
    RawWakerVTable::new(waker_clone, waker_wake, waker_wake_ref, waker_drop);

fn make_waker(id: usize, ready: Rc<RefCell<VecDeque<usize>>>) -> Waker {
    let state = Rc::new(WakeState { id, ready });
    let ptr = Rc::into_raw(state) as *const ();
    unsafe { Waker::from_raw(RawWaker::new(ptr, &WAKER_VTABLE)) }
}

unsafe fn waker_clone(ptr: *const ()) -> RawWaker {
    let rc = std::mem::ManuallyDrop::new(Rc::from_raw(ptr as *const WakeState));
    let _ = rc.clone();
    RawWaker::new(ptr, &WAKER_VTABLE)
}

unsafe fn waker_wake(ptr: *const ()) {
    let rc = Rc::from_raw(ptr as *const WakeState);
    rc.ready.borrow_mut().push_back(rc.id);
}

unsafe fn waker_wake_ref(ptr: *const ()) {
    let rc = std::mem::ManuallyDrop::new(Rc::from_raw(ptr as *const WakeState));
    rc.ready.borrow_mut().push_back(rc.id);
}

unsafe fn waker_drop(ptr: *const ()) {
    drop(Rc::from_raw(ptr as *const WakeState));
}

impl Interpreter {
    fn spawn_task(&self, task: Task) {
        let id = self.next_id.get();
        self.next_id.set(id + 1);
        self.resume_task(id, task);
    }

    fn resume_task(&self, id: usize, mut task: Task) {
        loop {
            let waker = make_waker(id, self.ready.clone());
            let mut cx = Context::from_waker(&waker);
            match task.as_mut().poll(&mut cx) {
                Poll::Ready(()) => break,
                Poll::Pending => {
                    self.tasks.borrow_mut().insert(id, task);
                    break;
                }
            }
        }
    }

    fn pump_ready(&self) {
        while !self.ready.borrow().is_empty() {
            let id = self.ready.borrow_mut().pop_front().unwrap();
            if let Some(task) = self.tasks.borrow_mut().remove(&id) {
                self.resume_task(id, task);
            }
        }
    }
}

// -- public API ------------------------------------------------------------

pub fn drive_sync(this: &Rc<Interpreter>, fut: EvResult) -> Result<JsValue, JsError> {
    let mut fut = fut;
    let waker = Waker::noop();
    let mut cx = Context::from_waker(&waker);
    match fut.as_mut().poll(&mut cx) {
        Poll::Ready(r) => r,
        Poll::Pending => Err(JsError::js("await is only valid in async functions")),
    }
}

pub fn drive_sync_unit(this: &Rc<Interpreter>, fut: StResult) -> Result<(), JsError> {
    let mut fut = fut;
    let waker = Waker::noop();
    let mut cx = Context::from_waker(&waker);
    match fut.as_mut().poll(&mut cx) {
        Poll::Ready(r) => r,
        Poll::Pending => Err(JsError::js("await is only valid in async functions")),
    }
}

impl Interpreter {
    pub fn run(self: &Rc<Self>, source: &str) -> Result<(), JsError> {
        let program = parser::parse_program(source)?;
        self.steps.set(0);
        let stmts = match &*program {
            Node::Program(stmts) => stmts.clone(),
            _ => vec![program.clone()],
        };
        let fut = exec_block(self, &stmts, self.global_env.clone());
        match drive_sync_unit(self, fut) {
            Ok(()) => Ok(()),
            Err(JsError::Return(_)) | Err(JsError::Break(_)) | Err(JsError::Continue(_)) => {
                Err(JsError::js("Illegal statement outside its context."))
            }
            Err(JsError::Budget(b)) => Err(JsError::js(b)),
            Err(JsError::Thrown(t)) => Err(JsError::js(self.repr(&t))),
            Err(e) => Err(e),
        }
    }

    pub fn call(self: &Rc<Self>, fn_: &JsValue, args: Vec<JsValue>) -> Result<JsValue, JsError> {
        self.steps.set(0);
        match drive_sync(self, call_value(self, fn_, args, JsValue::Undefined)) {
            Err(JsError::Budget(b)) => Err(JsError::js(b)),
            Err(JsError::Thrown(t)) => Err(JsError::js(self.repr(&t))),
            r => r,
        }
    }

    pub fn create_promise(&self) -> JsValue {
        JsValue::Promise(JsPromise::new())
    }

    pub fn advance(&self, ms: f64) {
        self.now.set(self.now.get() + ms);
    }

    pub fn enqueue(&self, job: Rc<dyn Fn()>) {
        self.microtasks.borrow_mut().push_back(job);
    }

    pub fn drain(self: &Rc<Self>) {
        let mut processed = 0usize;
        loop {
            loop {
                let next = self.microtasks.borrow_mut().pop_front();
                let Some(job) = next else { break };
                if processed >= MAX_DRAIN {
                    self.logs.borrow_mut().push(
                        "JS error: too many queued microtasks".to_string(),
                    );
                    self.microtasks.borrow_mut().clear();
                    return;
                }
                self.steps.set(0);
                processed += 1;
                job();
            }
            let due = {
                let mut ts = self.timers.borrow_mut();
                let mut due: Vec<Timer> = Vec::new();
                ts.retain(|t| {
                    if t.due <= self.now.get() {
                        due.push(t.clone());
                        false
                    } else {
                        true
                    }
                });
                due
            };
            for t in due {
                if processed >= MAX_DRAIN {
                    self.logs
                        .borrow_mut()
                        .push("JS error: too many timer callbacks".to_string());
                    return;
                }
                self.steps.set(0);
                processed += 1;
                let r = drive_sync(self, call_value(self, &t.fn_, t.args.clone(), JsValue::Undefined));
                if let Err(e) = r {
                    self.logs.borrow_mut().push(self.error_text(&e));
                }
                if t.repeat {
                    self.timers.borrow_mut().push(Timer {
                        due: t.due + t.interval,
                        ..t
                    });
                }
            }
            if self.ready.borrow().is_empty() {
                break;
            }
            self.pump_ready();
        }
    }

    pub fn error_text(&self, e: &JsError) -> String {
        match e {
            JsError::Thrown(v) => format!("JS error: {}", self.repr(v)),
            _ => format!("JS error: {}", js_error_message(self, e)),
        }
    }

    fn note_unhandled_rejection(&self, reason: &JsValue) {
        self.logs
            .borrow_mut()
            .push(format!("Unhandled promise rejection: {}", self.repr(reason)));
    }

    pub(crate) fn schedule_timer(&self, fn_: JsValue, ms: f64, repeat: bool) -> Result<JsValue, JsError> {
        if self.timers.borrow().len() >= MAX_TIMERS {
            return Err(JsError::js("Too many timers"));
        }
        let seq = self.timer_seq.get() + 1;
        self.timer_seq.set(seq);
        let due = self.now.get() + ms.max(0.0);
        self.timers.borrow_mut().push(Timer {
            id: seq,
            due,
            fn_,
            args: Vec::new(),
            interval: ms.max(0.0),
            repeat,
        });
        Ok(JsValue::Number(seq as f64))
    }

    pub(crate) fn clear_timer(&self, timer_id: &JsValue) -> JsValue {
        let id = to_number(timer_id) as u64;
        self.timers.borrow_mut().retain(|t| t.id != id);
        JsValue::Undefined
    }

    pub fn repr(&self, value: &JsValue) -> String {
        match value {
            JsValue::Undefined => "undefined".to_string(),
            JsValue::Null => "null".to_string(),
            JsValue::Bool(b) => {
                if *b {
                    "true".to_string()
                } else {
                    "false".to_string()
                }
            }
            JsValue::Str(s) => s.to_string(),
            JsValue::Number(n) => number_to_string(*n),
            JsValue::Array(arr) => arr
                .borrow()
                .iter()
                .map(|v| self.repr(v))
                .collect::<Vec<_>>()
                .join(","),
            JsValue::Function(f) => format!("function {}", f.name),
            JsValue::Class(c) => format!("class {}", c.borrow().name),
            JsValue::Promise(_) => "[object Object]".to_string(),
            JsValue::Object(_) => "[object Object]".to_string(),
            JsValue::Instance(_) => "[object Object]".to_string(),
            JsValue::Map(_) => "[object Map]".to_string(),
            JsValue::Set(_) => "[object Set]".to_string(),
            JsValue::Date(d) => date_repr(&d.borrow()),
            JsValue::Regex(r) => {
                let r = r.borrow();
                format!("/{}/{}", r.source, r.flags)
            }
            JsValue::Error(e) => {
                let e = e.borrow();
                format!("{}: {}", e.name, e.message)
            }
            JsValue::Super(_) => "super".to_string(),
            JsValue::Native(n) => format!("function {}", n.name),
            JsValue::Callback(_) => "function".to_string(),
            JsValue::Host(h) => Python::attach(|py| {
                let obj = h.bind(py);
                if let Ok(js_repr) = obj.getattr("js_repr") {
                    if let Ok(r) = js_repr.call0() {
                        if let Ok(s) = r.extract::<String>() {
                            return s;
                        }
                    }
                }
                if let Ok(s) = obj.extract::<String>() {
                    return s;
                }
                "[object Object]".to_string()
            }),
        }
    }
}

// -- error text helpers ----------------------------------------------------

pub fn js_error_message(this: &Interpreter, e: &JsError) -> String {
    match e {
        JsError::Js(m) => m.clone(),
        JsError::Thrown(v) => this.repr(v),
        JsError::Return(v) => this.repr(v),
        JsError::Break(_) | JsError::Continue(_) => {
            "Break or continue outside of a loop.".to_string()
        }
        JsError::Budget(m) => m.clone(),
    }
}

pub fn repr_js(this: &Interpreter, v: &JsValue) -> String {
    this.repr(v)
}

// -- host bridge -----------------------------------------------------------

pub fn py_to_js(this: &Interpreter, py: Python<'_>, obj: &Bound<'_, PyAny>) -> JsValue {
    if obj.is_none() {
        return JsValue::Null;
    }
    if obj.is(&this.undefined_ref) {
        return JsValue::Undefined;
    }
    if let Ok(b) = obj.extract::<bool>() {
        return JsValue::Bool(b);
    }
    if let Ok(n) = obj.extract::<f64>() {
        return JsValue::Number(n);
    }
    if let Ok(s) = obj.extract::<String>() {
        return JsValue::Str(Rc::from(s));
    }
    if let Ok(list) = obj.cast::<PyList>() {
        let vals: Vec<JsValue> = list
            .iter()
            .map(|item| py_to_js(this, py, &item))
            .collect();
        return JsValue::array(vals);
    }
    if let Ok(dict) = obj.cast::<PyDict>() {
        let mut map = BTreeMap::new();
        for (k, v) in dict.iter() {
            let key = k
                .extract::<String>()
                .unwrap_or_else(|_| format!("{:?}", k));
            map.insert(key, py_to_js(this, py, &v));
        }
        return JsValue::Object(Rc::new(RefCell::new(map)));
    }
    if let Ok(pjv) = obj.extract::<crate::pybind::PyJsValue>() {
        return pjv.take();
    }
    JsValue::Host(obj.clone().unbind())
}

pub fn js_to_py(this: &Rc<Interpreter>, py: Python<'_>, value: &JsValue) -> PyResult<Py<PyAny>> {
    Ok(match value {
        JsValue::Undefined => this.undefined_ref.clone_ref(py),
        JsValue::Null => py.None(),
        JsValue::Bool(b) => (*b).into_py_any(py)?,
        JsValue::Number(n) => (*n).into_py_any(py)?,
        JsValue::Str(s) => s.to_string().into_py_any(py)?,
        JsValue::Array(arr) => {
            let list = PyList::empty(py);
            for v in arr.borrow().iter() {
                list.append(js_to_py(this, py, v)?)?;
            }
            list.into_any().unbind()
        }
        JsValue::Object(map) => {
            let dict = PyDict::new(py);
            for (k, v) in map.borrow().iter() {
                dict.set_item(k, js_to_py(this, py, v)?)?;
            }
            dict.into_any().unbind()
        }
        _ => crate::pybind::PyJsValue::new(this, value.clone())?.into_py_any(py)?,
    })
}

fn py_err_to_js(e: PyErr) -> JsError {
    JsError::js(py_err_message(e))
}

fn py_err_message(e: PyErr) -> String {
    Python::attach(|py| format!("{}", e.value(py)))
}

fn member_tail(this: &Interpreter, obj: &Py<PyAny>, name: &str) -> Result<JsValue, JsError> {
    Python::attach(|py| {
        let obj_ref = obj.bind(py);
        if let Ok(method) = obj_ref.getattr("js_get") {
            match method.call1((name,)) {
                Ok(result) => Ok(py_to_js(this, py, &result)),
                Err(e) => Err(py_err_to_js(e)),
            }
        } else if let Ok(gi) = obj_ref.getattr("__getitem__") {
            match gi.call1((name,)) {
                Ok(result) => Ok(py_to_js(this, py, &result)),
                Err(_) => Ok(JsValue::Undefined),
            }
        } else {
            Ok(JsValue::Undefined)
        }
    })
}

fn member_tail_set(
    this: &Rc<Interpreter>,
    obj: &Py<PyAny>,
    name: &str,
    value: &JsValue,
) -> Result<(), JsError> {
    Python::attach(|py| {
        let obj_ref = obj.bind(py);
        if let Ok(method) = obj_ref.getattr("js_set") {
            let py_value = js_to_py(this, py, value).map_err(py_err_to_js)?;
            match method.call1((name, py_value)) {
                Ok(_) => Ok(()),
                Err(e) => Err(py_err_to_js(e)),
            }
        } else {
            Ok(())
        }
    })
}

fn host_is_callable(h: &Py<PyAny>) -> bool {
    Python::attach(|py| {
        let obj = h.bind(py);
        obj.is_callable()
            || obj.getattr("js_call").is_ok()
            || obj.getattr("js_new").is_ok()
    })
}

fn host_js_call(this: &Rc<Interpreter>, h: &Py<PyAny>, args: &[JsValue]) -> Result<JsValue, JsError> {
    Python::attach(|py| {
        let obj = h.bind(py);
        if let Ok(js_call) = obj.getattr("js_call") {
            let py_args = py_args(this, py, args).map_err(py_err_to_js)?;
            match js_call.call(py_args, None) {
                Ok(result) => Ok(py_to_js(this, py, &result)),
                Err(e) => Err(py_err_to_js(e)),
            }
        } else if obj.is_callable() {
            let py_args = py_args(this, py, args).map_err(py_err_to_js)?;
            match obj.call(py_args, None) {
                Ok(result) => Ok(py_to_js(this, py, &result)),
                Err(e) => Err(py_err_to_js(e)),
            }
        } else {
            Err(JsError::js("host value is not a function"))
        }
    })
}

fn py_args(this: &Rc<Interpreter>, py: Python<'_>, args: &[JsValue]) -> PyResult<Py<PyTuple>> {
    let mut items = Vec::with_capacity(args.len());
    for a in args {
        items.push(js_to_py(this, py, a)?);
    }
    Ok(PyTuple::new(py, items)?.unbind())
}

// -- index / keys helpers ---------------------------------------------------

pub fn index_name(this: &Interpreter, value: &JsValue) -> String {
    match value {
        JsValue::Str(s) => s.to_string(),
        _ => this.repr(value),
    }
}

pub fn own_keys(this: &Interpreter, value: &JsValue) -> Vec<String> {
    match value {
        JsValue::Object(m) => m.borrow().keys().cloned().collect(),
        JsValue::Array(a) => (0..a.borrow().len()).map(|i| i.to_string()).collect(),
        _ => Vec::new(),
    }
}

pub fn number_to_string(n: f64) -> String {
    if n.is_nan() {
        return "NaN".to_string();
    }
    if n == f64::INFINITY {
        return "Infinity".to_string();
    }
    if n == f64::NEG_INFINITY {
        return "-Infinity".to_string();
    }
    if n.is_finite() && n.fract() == 0.0 && n.abs() < 9.0e15 {
        return format!("{}", n as i64);
    }
    format!("{}", n)
}

pub fn to_fixed(n: f64, digits: f64) -> String {
    if n.is_nan() {
        return "NaN".to_string();
    }
    if n == f64::INFINITY {
        return "Infinity".to_string();
    }
    if n == f64::NEG_INFINITY {
        return "-Infinity".to_string();
    }
    let d = digits.clamp(0.0, 100.0) as usize;
    format!("{:.*}", d, n)
}

// -- number/string/array member getters -------------------------------------

pub fn number_to_string_radix(this: &Interpreter, n: f64, radix: &JsValue) -> Result<String, JsError> {
    if n.is_nan() {
        return Ok("NaN".to_string());
    }
    if n == f64::INFINITY {
        return Ok("Infinity".to_string());
    }
    if n == f64::NEG_INFINITY {
        return Ok("-Infinity".to_string());
    }
    if !nullish(radix) {
        let base = to_int32(radix);
        if base < 2 || base > 36 {
            return Err(JsError::js("toString() radix must be between 2 and 36"));
        }
        let digits = "0123456789abcdefghijklmnopqrstuvwxyz".as_bytes();
        let neg = n < 0.0;
        let mut nn = n.abs() as u64;
        if nn == 0 {
            return Ok("0".to_string());
        }
        let mut out: Vec<u8> = Vec::new();
        while nn > 0 {
            out.push(digits[(nn % base as u64) as usize]);
            nn /= base as u64;
        }
        out.reverse();
        let mut s = String::from_utf8(out).unwrap();
        if neg {
            s = format!("-{s}");
        }
        return Ok(s);
    }
    Ok(this.repr(&JsValue::Number(n)))
}

pub fn string_split(
    this: &Interpreter,
    text: &str,
    sep: &JsValue,
    limit: &JsValue,
) -> Result<JsValue, JsError> {
    if nullish(sep) {
        return Ok(JsValue::array(vec![JsValue::str(text)]));
    }
    if let JsValue::Regex(r) = sep {
        let r = r.borrow();
        let maxsplit = if nullish(limit) {
            0usize
        } else {
            to_int32(limit).max(0) as usize
        };
        let parts: Vec<String> = if maxsplit > 0 {
            r.re.splitn(text, maxsplit + 1).map(|s| s.to_string()).collect()
        } else {
            r.re.split(text).map(|s| s.to_string()).collect()
        };
        return Ok(JsValue::array(parts.into_iter().map(JsValue::str).collect()));
    }
    let s = this.repr(sep);
    let out: Vec<String> = if s.is_empty() {
        text.chars().map(String::from).collect()
    } else if !nullish(limit) {
        text.splitn(to_int32(limit).max(0) as usize + 1, s.as_str())
            .map(String::from)
            .collect()
    } else {
        text.split(s.as_str()).map(String::from).collect()
    };
    Ok(JsValue::array(out.into_iter().map(JsValue::str).collect()))
}

pub fn string_match(
    this: &Interpreter,
    text: &str,
    regex: &JsValue,
) -> Result<JsValue, JsError> {
    if let JsValue::Regex(r) = regex {
        let r = r.borrow();
        if r.global_ {
            let found: Vec<JsValue> = r
                .re
                .find_iter(text)
                .map(|m| JsValue::str(m.as_str()))
                .collect();
            if found.is_empty() {
                return Ok(JsValue::Null);
            }
            return Ok(JsValue::array(found));
        }
        if let Some(m) = r.re.find(text) {
            let mut out = vec![JsValue::str(m.as_str())];
            if let Some(caps) = r.re.captures(text) {
                for i in 1..caps.len() {
                    out.push(match caps.get(i) {
                        Some(g) => JsValue::str(g.as_str()),
                        None => JsValue::Undefined,
                    });
                }
            }
            return Ok(JsValue::array(out));
        }
        return Ok(JsValue::Null);
    }
    Err(JsError::js("String.prototype.match: not a RegExp"))
}

pub fn string_replace(
    this: &Rc<Interpreter>,
    text: &str,
    pat: &JsValue,
    repl: &JsValue,
    all: bool,
) -> EvResult {
    let this = this.clone();
    let text = text.to_string();
    let pat = pat.clone();
    let repl = repl.clone();
    Box::pin(async move {
        let repl_text = match &repl {
            JsValue::Str(s) => s.to_string(),
            _ => this.repr(&repl),
        };
        if let JsValue::Regex(r) = &pat {
            let r = r.borrow();
            let count = if r.global_ || all { 0usize } else { 1usize };
            if is_js_function(&repl) {
                let mut out = String::new();
                let mut last = 0usize;
                let matches: Vec<regex::Match> = r.re.find_iter(&text).collect();
                let iter = matches.iter().take(if count == 0 { matches.len() } else { 1 });
                for m in iter {
                    out.push_str(&text[last..m.start()]);
                    let args = js_capture_args(&r, &text, *m)?;
                    let v = call_value(&this, &repl.clone(), args, JsValue::Undefined).await?;
                    out.push_str(&this.repr(&v));
                    last = m.end();
                }
                out.push_str(&text[last..]);
                return Ok(JsValue::str(out));
            }
            let replaced = if count == 0 {
                r.re.replace_all(&text, repl_text.as_str()).to_string()
            } else {
                r.re.replace(&text, repl_text.as_str()).to_string()
            };
            return Ok(JsValue::str(replaced));
        }
        let pat_str = match &pat {
            JsValue::Str(s) => s.to_string(),
            _ => this.repr(&pat),
        };
        if all {
            return Ok(JsValue::str(text.replace(pat_str.as_str(), repl_text.as_str())));
        }
        if pat_str.is_empty() {
            return Ok(JsValue::str(repl_text + &text));
        }
        if let Some(idx) = text.find(&pat_str) {
            let mut out = text.clone();
            out.replace_range(idx..idx + pat_str.len(), &repl_text);
            return Ok(JsValue::str(out));
        }
        Ok(JsValue::str(text))
    })
}

fn js_capture_args(
    r: &JsRegex,
    text: &str,
    m: regex::Match<'_>,
) -> Result<Vec<JsValue>, JsError> {
    let mut args = vec![JsValue::str(m.as_str())];
    if let Some(caps) = r.re.captures(text) {
        for i in 1..caps.len() {
            args.push(match caps.get(i) {
                Some(g) => JsValue::str(g.as_str()),
                None => JsValue::Undefined,
            });
        }
    }
    Ok(args)
}

pub fn compile_regex(source: &str, flags: &str) -> JsRegex {
    let global_ = flags.contains('g');
    let ignore_case = flags.contains('i');
    let multiline = flags.contains('m');
    let mut pat = source.to_string();
    if multiline {
        pat = format!("(?m:{pat})");
    }
    if ignore_case {
        pat = format!("(?i:{pat})");
    }
    let re = regex::Regex::new(&pat).unwrap_or_else(|_| regex::Regex::new(r"[^\s\S]").unwrap());
    JsRegex {
        source: source.to_string(),
        flags: flags.to_string(),
        global_,
        ignore_case,
        multiline,
        last_index: Cell::new(0.0),
        re,
    }
}
// -- js_get / js_set -------------------------------------------------------

pub fn js_get(this: &Rc<Interpreter>, obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    match obj {
        JsValue::Object(map) => Ok(map.borrow().get(name).cloned().unwrap_or(JsValue::Undefined)),
        JsValue::Array(arr) => Ok(list_get(this, arr, name)),
        JsValue::Str(s) => Ok(string_get(this, s, name)),
        JsValue::Number(n) => Ok(number_get(this, *n, name)),
        JsValue::Function(f) => function_get(this, f, name),
        JsValue::Promise(p) => promise_get(this, p, name),
        JsValue::Class(c) => class_get(this, c, name),
        JsValue::Instance(inst) => Ok(instance_get(inst, name)),
        JsValue::Map(m) => Ok(map_get(this, m, name)),
        JsValue::Set(s) => Ok(set_get(this, s, name)),
        JsValue::Date(d) => Ok(date_get(this, d, name)),
        JsValue::Regex(r) => Ok(regex_get(this, r, name)),
        JsValue::Error(e) => Ok(error_get(e, name)),
        JsValue::Super(s) => Ok(super_get(s, name)),
        JsValue::Native(n) => {
            if matches!(name, "call" | "apply" | "bind") {
                return Ok(make_method_wrapper(obj.clone(), name));
            }
            match n.get {
                Some(get) => get(this, obj, name),
                None => Ok(JsValue::Undefined),
            }
        }
        JsValue::Callback(_) => {
            if matches!(name, "call" | "apply" | "bind") {
                return Ok(make_method_wrapper(obj.clone(), name));
            }
            Ok(JsValue::Undefined)
        }
        JsValue::Host(h) => host_get(this, h, name),
        JsValue::Undefined | JsValue::Null | JsValue::Bool(_) => Ok(JsValue::Undefined),
    }
}

fn make_method_wrapper(f: JsValue, name: &str) -> JsValue {
    let f2 = f.clone();
    match name {
        "call" => JsValue::Callback(Rc::new(move |interp: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
            let this_arg = args.first().cloned().unwrap_or(JsValue::Undefined);
            let rest = args.into_iter().skip(1).collect();
            if matches!(f2, JsValue::Native(_) | JsValue::Callback(_)) {
                let mut all = vec![this_arg];
                all.extend(rest);
                call_value(interp, &f2, all, JsValue::Undefined)
            } else {
                call_value(interp, &f2, rest, this_arg)
            }
        })),
        "apply" => JsValue::Callback(Rc::new(move |interp: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
            let this_arg = args.first().cloned().unwrap_or(JsValue::Undefined);
            let rest = match args.get(1) {
                Some(JsValue::Array(a)) => a.borrow().clone(),
                _ => vec![],
            };
            if matches!(f2, JsValue::Native(_) | JsValue::Callback(_)) {
                let mut all = vec![this_arg];
                all.extend(rest);
                call_value(interp, &f2, all, JsValue::Undefined)
            } else {
                call_value(interp, &f2, rest, this_arg)
            }
        })),
        _ => JsValue::Callback(Rc::new(move |_interp: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
            let this_arg = args.first().cloned().unwrap_or(JsValue::Undefined);
            let pre = args.into_iter().skip(1).collect();
            let out = make_bound_js(f2.clone(), this_arg, pre);
            Box::pin(async move { Ok(out) })
        })),
    }
}

fn make_bound_js(f: JsValue, this_arg: JsValue, pre: Vec<JsValue>) -> JsValue {
    JsValue::Callback(Rc::new(move |interp: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
        let mut all = pre.clone();
        all.extend(args);
        call_value(interp, &f, all, this_arg.clone())
    }))
}

pub fn js_set(
    this: &Rc<Interpreter>,
    obj: &JsValue,
    name: &str,
    value: &JsValue,
) -> Result<(), JsError> {
    match obj {
        JsValue::Object(map) => {
            map.borrow_mut().insert(name.to_string(), value.clone());
            Ok(())
        }
        JsValue::Array(arr) => array_set(arr, name, value),
        JsValue::Function(f) => {
            if name == "prototype" {
                f.set_prototype(value.clone());
            }
            Ok(())
        }
        JsValue::Instance(inst) => {
            inst.borrow().props.borrow_mut().insert(name.to_string(), value.clone());
            Ok(())
        }
        JsValue::Class(c) => {
            let is_proto_member = c.borrow().prototype.borrow().contains_key(name);
            if is_proto_member {
                c.borrow().prototype.borrow_mut().insert(name.to_string(), value.clone());
            } else {
                c.borrow().statics.borrow_mut().insert(name.to_string(), value.clone());
            }
            Ok(())
        }
        JsValue::Regex(r) => {
            if name == "lastIndex" {
                r.borrow().last_index.set(if nullish(value) {
                    0.0
                } else {
                    to_number(value)
                });
            }
            Ok(())
        }
        JsValue::Native(n) => match n.set {
            Some(set) => set(this, obj, name, value),
            None => Ok(()),
        },
        JsValue::Host(h) => member_tail_set(this, h, name, value),
        _ => Ok(()),
    }
}

fn array_set(arr: &Rc<RefCell<Vec<JsValue>>>, name: &str, value: &JsValue) -> Result<(), JsError> {
    if name == "length" {
        let len = to_number(value).max(0.0) as usize;
        let mut a = arr.borrow_mut();
        a.truncate(len);
        while a.len() < len {
            a.push(JsValue::Undefined);
        }
        return Ok(());
    }
    if let Some(index) = int_index(name) {
        if index >= 0 {
            if index as usize >= MAX_ARRAY_LEN {
                return Err(JsError::js(format!(
                    "Array index {index} exceeds the allowed maximum"
                )));
            }
            let mut a = arr.borrow_mut();
            while a.len() <= index as usize {
                a.push(JsValue::Undefined);
            }
            a[index as usize] = value.clone();
        }
    }
    Ok(())
}

fn host_get(this: &Rc<Interpreter>, h: &Py<PyAny>, name: &str) -> Result<JsValue, JsError> {
    if host_is_callable(h) {
        if name == "call" || name == "apply" || name == "bind" {
            return Ok(make_host_method(h.clone(), name));
        }
    }
    member_tail(this.as_ref(), h, name)
}

fn make_host_method(h: Py<PyAny>, name: &str) -> JsValue {
    let h = Python::attach(|py| h.clone_ref(py));
    let name = name.to_string();
    JsValue::Callback(Rc::new(move |interp: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
        let h = Python::attach(|py| h.clone_ref(py));
        match name.as_str() {
            "call" => {
                let this_arg = args.first().cloned().unwrap_or(JsValue::Undefined);
                let rest = args.into_iter().skip(1).collect::<Vec<_>>();
                let mut all = vec![this_arg];
                all.extend(rest);
                call_value(interp, &JsValue::Host(h), all, JsValue::Undefined)
            }
            "apply" => {
                let this_arg = args.first().cloned().unwrap_or(JsValue::Undefined);
                let arg_list = args
                    .get(1)
                    .map(|a| match a {
                        JsValue::Array(arr) => arr.borrow().clone(),
                        _ => vec![],
                    })
                    .unwrap_or_default();
                let mut all = vec![this_arg];
                all.extend(arg_list);
                call_value(interp, &JsValue::Host(h), all, JsValue::Undefined)
            }
            _ => {
                let this_arg = args.first().cloned().unwrap_or(JsValue::Undefined);
                let pre = args.into_iter().skip(1).collect::<Vec<_>>();
                let i3 = interp.clone();
                let h3 = Python::attach(|py| h.clone_ref(py));
                let pre2 = pre.clone();
                let t = this_arg.clone();
                Box::pin(async move {
                    Ok(JsValue::Callback(Rc::new(move |interp: &Rc<Interpreter>, args2: Vec<JsValue>| -> EvResult {
                        let h4 = Python::attach(|py| h3.clone_ref(py));
                        let mut all = vec![t.clone()];
                        all.extend(pre2.clone());
                        all.extend(args2);
                        call_value(interp, &JsValue::Host(h4), all, JsValue::Undefined)
                    })))
                })
            }
        }
    }))
}
// -- char/string helpers ---------------------------------------------------

fn char_count(s: &str) -> i64 {
    s.chars().count() as i64
}

fn char_slice(s: &str, start: i64, end: i64) -> String {
    let chars: Vec<char> = s.chars().collect();
    let n = chars.len() as i64;
    let start = start.clamp(0, n) as usize;
    let end = end.clamp(0, n) as usize;
    chars[start..end].iter().collect()
}

fn byte_of_char(s: &str, char_idx: i64) -> usize {
    if char_idx <= 0 {
        return 0;
    }
    let mut ci = 0i64;
    for (i, _) in s.char_indices() {
        if ci == char_idx {
            return i;
        }
        ci += 1;
    }
    s.len()
}

fn char_of_byte(s: &str, byte_idx: usize) -> i64 {
    s[..byte_idx.min(s.len())].chars().count() as i64
}

// -- list_get --------------------------------------------------------------

pub fn list_get(
    this: &Rc<Interpreter>,
    arr: &Rc<RefCell<Vec<JsValue>>>,
    name: &str,
) -> JsValue {
    if name == "length" {
        return JsValue::Number(arr.borrow().len() as f64);
    }
    let a = arr.clone();
    match name {
        "push" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let mut a2 = a.borrow_mut();
                a2.extend(args);
                let n = a2.len() as f64;
                Box::pin(async move { Ok(JsValue::Number(n)) })
            }));
        }
        "pop" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let v = a.borrow_mut().pop().unwrap_or(JsValue::Undefined);
                Box::pin(async move { Ok(v) })
            }));
        }
        "join" => {
            let i0 = this.clone();
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i0.clone();
                let a2 = a.clone();
                Box::pin(async move {
                    let sep = match args.first() {
                        Some(JsValue::Str(s)) => s.to_string(),
                        _ => ",".to_string(),
                    };
                    let joined = a2
                        .borrow()
                        .iter()
                        .map(|v| i2.repr(v))
                        .collect::<Vec<_>>()
                        .join(&sep);
                    Ok(JsValue::str(joined))
                })
            }));
        }
        "indexOf" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let a2 = a.clone();
                Box::pin(async move {
                    let value = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let start = match args.get(1) {
                        Some(s) if !nullish(s) => to_int32(s),
                        _ => 0,
                    };
                    let arr = a2.borrow();
                    let n = arr.len() as i32;
                    let s = if start >= 0 { start } else { (n + start).max(0) };
                    for i in s as usize..arr.len() {
                        if strict_eq(&arr[i], &value) {
                            return Ok(JsValue::Number(i as f64));
                        }
                    }
                    Ok(JsValue::Number(-1.0))
                })
            }));
        }
        "lastIndexOf" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let a2 = a.clone();
                Box::pin(async move {
                    let value = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let arr = a2.borrow();
                    let n = arr.len() as i32;
                    let start = match args.get(1) {
                        Some(s) if !nullish(s) => {
                            let v = to_int32(s);
                            if v >= 0 { v.min(n - 1) } else { (n + v).max(0) }
                        }
                        _ => n - 1,
                    };
                    for i in (0..=start.max(0) as usize).rev() {
                        if strict_eq(&arr[i], &value) {
                            return Ok(JsValue::Number(i as f64));
                        }
                    }
                    Ok(JsValue::Number(-1.0))
                })
            }));
        }
        "includes" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let a2 = a.clone();
                Box::pin(async move {
                    let value = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let found = a2.borrow().iter().any(|v| strict_eq(v, &value));
                    Ok(JsValue::Bool(found))
                })
            }));
        }
        "concat" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let a2 = a.clone();
                Box::pin(async move {
                    let mut out = a2.borrow().clone();
                    for o in args {
                        match o {
                            JsValue::Array(other) => out.extend(other.borrow().clone()),
                            v => {
                                if !nullish(&v) {
                                    out.push(v);
                                }
                            }
                        }
                    }
                    Ok(JsValue::array(out))
                })
            }));
        }
        "reverse" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let a2 = a.clone();
                Box::pin(async move {
                    a2.borrow_mut().reverse();
                    Ok(JsValue::Array(a2.clone()))
                })
            }));
        }
        "shift" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let v = a.borrow_mut().drain(..1).next().unwrap_or(JsValue::Undefined);
                Box::pin(async move { Ok(v) })
            }));
        }
        "unshift" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let a2 = a.clone();
                Box::pin(async move {
                    let mut new_front = args;
                    let mut rest = a2.borrow().clone();
                    new_front.extend(rest.drain(..));
                    *a2.borrow_mut() = new_front;
                    Ok(JsValue::Number(a2.borrow().len() as f64))
                })
            }));
        }
        "slice" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let a2 = a.clone();
                Box::pin(async move {
                    let arr = a2.borrow();
                    let n = arr.len() as i64;
                    let s = match args.first() {
                        Some(v) if !nullish(v) => {
                            let v = to_int32(v) as i64;
                            if v >= 0 { v } else { n + v }
                        }
                        _ => 0,
                    }
                    .max(0);
                    let e = match args.get(1) {
                        Some(v) if !nullish(v) => {
                            let v = to_int32(v) as i64;
                            if v >= 0 { v } else { n + v }
                        }
                        _ => n,
                    }
                    .max(0)
                    .min(n);
                    let s = s.min(e);
                    Ok(JsValue::array(arr[s as usize..e as usize].to_vec()))
                })
            }));
        }
        "splice" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let a2 = a.clone();
                Box::pin(async move {
                    let mut arr = a2.borrow_mut();
                    let n = arr.len() as i64;
                    let s = match args.first() {
                        Some(v) if !nullish(v) => {
                            let v = to_int32(v) as i64;
                            if v >= 0 { v } else { n + v }
                        }
                        _ => 0,
                    }
                    .clamp(0, n) as usize;
                    let dc = match args.get(1) {
                        Some(v) if !nullish(v) => (to_int32(v) as i64).max(0).min(n - s as i64),
                        _ => n - s as i64,
                    } as usize;
                    let items = args.into_iter().skip(2).collect::<Vec<_>>();
                    let removed: Vec<JsValue> = arr.drain(s..s + dc).collect();
                    let mut tail = arr.split_off(s);
                    arr.extend(items);
                    arr.extend(tail);
                    Ok(JsValue::array(removed))
                })
            }));
        }
        "sort" => {
            let i0 = this.clone();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let i3 = i0.clone();
                let a2 = a.clone();
                Box::pin(async move {
                    let compare_fn = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let mut items = a2.borrow().clone();
                    if nullish(&compare_fn) {
                        items.sort_by(|x, y| i3.repr(x).cmp(&i3.repr(y)));
                    } else {
                        let mut idx: Vec<usize> = (0..items.len()).collect();
                        let mut keyed: Vec<f64> = Vec::with_capacity(items.len());
                        for _ in 0..items.len() {
                            keyed.push(0.0);
                        }
                        for j in 1..idx.len() {
                            let mut k = j;
                            while k > 0 {
                                let a_val = items[idx[k - 1]].clone();
                                let b_val = items[idx[k]].clone();
                                let cmp = to_number(&drive_sync(
                                    &i2,
                                    call_value(&i2, &compare_fn, vec![a_val, b_val], JsValue::Undefined),
                                )?);
                                if cmp > 0.0 {
                                    idx.swap(k - 1, k);
                                    k -= 1;
                                } else {
                                    break;
                                }
                            }
                        }
                        let _ = keyed;
                        items = idx.into_iter().map(|ix| items[ix].clone()).collect();
                    }
                    *a2.borrow_mut() = items;
                    Ok(JsValue::Array(a2.clone()))
                })
            }));
        }
        "toString" => {
            let i0 = this.clone();
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let i2 = i0.clone();
                let a2 = a.clone();
                Box::pin(async move {
                    let s = a2.borrow().iter().map(|v| i2.repr(v)).collect::<Vec<_>>().join(",");
                    Ok(JsValue::str(s))
                })
            }));
        }
        "map" | "filter" | "forEach" | "find" | "findIndex" | "some" | "every" => {
            let op = name.to_string();
            let i0 = this.clone();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let i3 = i0.clone();
                let a2 = a.clone();
                let op2 = op.clone();
                Box::pin(async move {
                    let fn_ = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let items = a2.borrow().clone();
                    match op2.as_str() {
                        "map" => {
                            let mut out = Vec::new();
                            for (i, item) in items.iter().enumerate() {
                                let v = call_value(&i2, &fn_, vec![item.clone(), JsValue::Number(i as f64), JsValue::Array(a2.clone())], JsValue::Undefined).await?;
                                out.push(v);
                            }
                            Ok(JsValue::array(out))
                        }
                        "filter" => {
                            let mut out = Vec::new();
                            for (i, item) in items.iter().enumerate() {
                                let v = call_value(&i2, &fn_, vec![item.clone(), JsValue::Number(i as f64), JsValue::Array(a2.clone())], JsValue::Undefined).await?;
                                if truthy(&v) {
                                    out.push(item.clone());
                                }
                            }
                            Ok(JsValue::array(out))
                        }
                        "forEach" => {
                            for (i, item) in items.iter().enumerate() {
                                call_value(&i2, &fn_, vec![item.clone(), JsValue::Number(i as f64), JsValue::Array(a2.clone())], JsValue::Undefined).await?;
                            }
                            Ok(JsValue::Undefined)
                        }
                        "find" => {
                            for (i, item) in items.iter().enumerate() {
                                let v = call_value(&i2, &fn_, vec![item.clone(), JsValue::Number(i as f64), JsValue::Array(a2.clone())], JsValue::Undefined).await?;
                                if truthy(&v) {
                                    return Ok(item.clone());
                                }
                            }
                            Ok(JsValue::Undefined)
                        }
                        "findIndex" => {
                            for (i, item) in items.iter().enumerate() {
                                let v = call_value(&i2, &fn_, vec![item.clone(), JsValue::Number(i as f64), JsValue::Array(a2.clone())], JsValue::Undefined).await?;
                                if truthy(&v) {
                                    return Ok(JsValue::Number(i as f64));
                                }
                            }
                            Ok(JsValue::Number(-1.0))
                        }
                        "some" => {
                            for (i, item) in items.iter().enumerate() {
                                let v = call_value(&i2, &fn_, vec![item.clone(), JsValue::Number(i as f64), JsValue::Array(a2.clone())], JsValue::Undefined).await?;
                                if truthy(&v) {
                                    return Ok(JsValue::Bool(true));
                                }
                            }
                            Ok(JsValue::Bool(false))
                        }
                        _ => {
                            for (i, item) in items.iter().enumerate() {
                                let v = call_value(&i2, &fn_, vec![item.clone(), JsValue::Number(i as f64), JsValue::Array(a2.clone())], JsValue::Undefined).await?;
                                if !truthy(&v) {
                                    return Ok(JsValue::Bool(false));
                                }
                            }
                            Ok(JsValue::Bool(true))
                        }
                    }
                })
            }));
        }
        "reduce" | "reduceRight" => {
            let op = name.to_string();
            let i0 = this.clone();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let i3 = i0.clone();
                let a2 = a.clone();
                let op2 = op.clone();
                Box::pin(async move {
                    let fn_ = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let initial = args.get(1).cloned().unwrap_or(JsValue::Undefined);
                    let items = a2.borrow().clone();
                    let mut acc = initial;
                    if op2 == "reduce" {
                        let mut start = 0usize;
                        if nullish(&acc) {
                            if items.is_empty() {
                                return Err(JsError::js("Reduce of empty array with no initial value"));
                            }
                            acc = items[0].clone();
                            start = 1;
                        }
                        for i in start..items.len() {
                            acc = call_value(&i2, &fn_, vec![acc, items[i].clone(), JsValue::Number(i as f64), JsValue::Array(a2.clone())], JsValue::Undefined).await?;
                        }
                    } else {
                        if items.is_empty() {
                            if nullish(&acc) {
                                return Err(JsError::js("Reduce of empty array with no initial value"));
                            }
                            return Ok(acc);
                        }
                        let mut i = items.len() as i64 - 1;
                        if nullish(&acc) {
                            acc = items[i as usize].clone();
                            i -= 1;
                        }
                        while i >= 0 {
                            let idx = i as usize;
                            acc = call_value(&i2, &fn_, vec![acc, items[idx].clone(), JsValue::Number(idx as f64), JsValue::Array(a2.clone())], JsValue::Undefined).await?;
                            i -= 1;
                        }
                    }
                    Ok(acc)
                })
            }));
        }
        "flat" => {
            let i0 = this.clone();
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i0.clone();
                let a2 = a.clone();
                Box::pin(async move {
                    let depth = match args.first() {
                        Some(v) if !nullish(v) => to_int32(v).max(0),
                        _ => 1,
                    };
                    let flattened = flatten_array(&i2, &a2.borrow().clone(), depth);
                    Ok(JsValue::array(flattened))
                })
            }));
        }
        "at" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let a2 = a.clone();
                Box::pin(async move {
                    let i = to_int32(&args.first().cloned().unwrap_or(JsValue::Undefined));
                    let arr = a2.borrow();
                    let n = arr.len() as i64;
                    let mut idx = i as i64;
                    if idx < 0 {
                        idx += n;
                    }
                    if idx >= 0 && idx < n {
                        Ok(arr[idx as usize].clone())
                    } else {
                        Ok(JsValue::Undefined)
                    }
                })
            }));
        }
        _ => {}
    }
    if let Some(index) = int_index(name) {
        let arr = arr.borrow();
        let n = arr.len() as i64;
        if -n <= index && index < n {
            let i = if index < 0 { n + index } else { index };
            return arr[i as usize].clone();
        }
    }
    JsValue::Undefined
}

fn flatten_array(_this: &Interpreter, values: &[JsValue], depth: i32) -> Vec<JsValue> {
    let mut out = Vec::new();
    for v in values {
        if depth > 0 {
            if let JsValue::Array(a) = v {
                let inner = flatten_array(_this, &a.borrow().clone(), depth - 1);
                out.extend(inner);
                continue;
            }
        }
        out.push(v.clone());
    }
    out
}
// -- string_get ------------------------------------------------------------

pub fn string_get(this: &Rc<Interpreter>, text: &Rc<str>, name: &str) -> JsValue {
    if name == "length" {
        return JsValue::Number(char_count(text) as f64);
    }
    let t = text.clone();
    let i0 = this.clone();
    match name {
        "charAt" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move {
                    let idx = match args.first() {
                        Some(v) if !nullish(v) => to_int32(v) as i64,
                        _ => 0,
                    };
                    Ok(JsValue::str(safe_char(&t2, idx)))
                })
            }));
        }
        "at" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move {
                    let i = to_int32(&args.first().cloned().unwrap_or(JsValue::Undefined));
                    let n = char_count(&t2);
                    let mut idx = i as i64;
                    if idx < 0 {
                        idx += n;
                    }
                    Ok(JsValue::str(safe_char(&t2, idx)))
                })
            }));
        }
        "charCodeAt" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move {
                    let idx = match args.first() {
                        Some(v) if !nullish(v) => to_int32(v) as i64,
                        _ => 0,
                    };
                    Ok(JsValue::Number(safe_code(&t2, idx)))
                })
            }));
        }
        "indexOf" => {
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let t2 = t.clone();
                Box::pin(async move {
                    let sub = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let start = match args.get(1) {
                        Some(v) if !nullish(v) => to_int32(v).max(0) as usize,
                        _ => 0,
                    };
                    let sub_str = i2.repr(&sub);
                    let byte_start = byte_of_char(&t2, start as i64);
                    let found = match t2[byte_start..].find(&sub_str) {
                        Some(bi) => {
                            let char_idx = char_of_byte(&t2, byte_start + bi);
                            Some(char_idx)
                        }
                        None => None,
                    };
                    Ok(match found {
                        Some(ci) => JsValue::Number(ci as f64),
                        None => JsValue::Number(-1.0),
                    })
                })
            }));
        }
        "lastIndexOf" => {
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let t2 = t.clone();
                Box::pin(async move {
                    let sub = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let n = char_count(&t2) - 1;
                    let start = match args.get(1) {
                        Some(v) if !nullish(v) => {
                            let v = to_int32(v) as i64;
                            if v >= 0 { v.min(n) } else { n + v }
                        }
                        _ => n,
                    }
                    .max(0) as usize;
                    let sub_str = i2.repr(&sub);
                    let byte_end = byte_of_char(&t2, start as i64 + 1);
                    let text2 = &t2[..byte_end.min(t2.len())];
                    let found = match text2.rfind(&sub_str) {
                        Some(bi) => Some(char_of_byte(text2, bi)),
                        None => None,
                    };
                    Ok(match found {
                        Some(ci) => JsValue::Number(ci as f64),
                        None => JsValue::Number(-1.0),
                    })
                })
            }));
        }
        "includes" | "startsWith" | "endsWith" => {
            let op = name.to_string();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let t2 = t.clone();
                let op2 = op.clone();
                Box::pin(async move {
                    let sub = i2.repr(&args.first().cloned().unwrap_or(JsValue::Undefined));
                    let res = match op2.as_str() {
                        "includes" => t2.contains(sub.as_str()),
                        "startsWith" => t2.starts_with(sub.as_str()),
                        _ => t2.ends_with(sub.as_str()),
                    };
                    Ok(JsValue::Bool(res))
                })
            }));
        }
        "toLowerCase" | "toLocaleLowerCase" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move { Ok(JsValue::str(t2.to_lowercase())) })
            }));
        }
        "toUpperCase" | "toLocaleUpperCase" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move { Ok(JsValue::str(t2.to_uppercase())) })
            }));
        }
        "trim" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move { Ok(JsValue::str(t2.trim().to_string())) })
            }));
        }
        "trimStart" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move { Ok(JsValue::str(t2.trim_start().to_string())) })
            }));
        }
        "trimEnd" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move { Ok(JsValue::str(t2.trim_end().to_string())) })
            }));
        }
        "slice" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move {
                    let n = char_count(&t2);
                    let s = match args.first() {
                        Some(v) if !nullish(v) => {
                            let v = to_int32(v) as i64;
                            if v >= 0 { v } else { n + v }
                        }
                        _ => 0,
                    }
                    .max(0);
                    let e = match args.get(1) {
                        Some(v) if !nullish(v) => {
                            let v = to_int32(v) as i64;
                            if v >= 0 { v } else { n + v }
                        }
                        _ => n,
                    }
                    .max(0)
                    .min(n);
                    Ok(JsValue::str(char_slice(&t2, s, e.max(s))))
                })
            }));
        }
        "substring" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move {
                    let n = char_count(&t2);
                    let s = match args.first() {
                        Some(v) if !nullish(v) => to_int32(v) as i64,
                        _ => 0,
                    }
                    .clamp(0, n);
                    let e = match args.get(1) {
                        Some(v) if !nullish(v) => to_int32(v) as i64,
                        _ => n,
                    }
                    .clamp(0, n);
                    let (s, e) = if s > e { (e, s) } else { (s, e) };
                    Ok(JsValue::str(char_slice(&t2, s, e)))
                })
            }));
        }
        "substr" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move {
                    let n = char_count(&t2);
                    let s = match args.first() {
                        Some(v) if !nullish(v) => {
                            let v = to_int32(v) as i64;
                            if v >= 0 { v } else { n + v }
                        }
                        _ => 0,
                    }
                    .max(0);
                    let ln = match args.get(1) {
                        Some(v) if !nullish(v) => to_int32(v).max(0) as i64,
                        _ => n - s,
                    };
                    Ok(JsValue::str(char_slice(&t2, s, s + ln)))
                })
            }));
        }
        "concat" => {
            let i0 = this.clone();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let i3 = i0.clone();
                let t2 = t.clone();
                Box::pin(async move {
                    let mut out = t2.to_string();
                    for o in args {
                        out.push_str(&i3.repr(&o));
                    }
                    Ok(JsValue::str(out))
                })
            }));
        }
        "repeat" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move {
                    let c = to_int32(&args.first().cloned().unwrap_or(JsValue::Undefined)).max(0) as usize;
                    if c > 0 && t2.chars().count() > MAX_STRING_OUT / c {
                        return Err(JsError::js("String.prototype.repeat result is too large"));
                    }
                    Ok(JsValue::str(t2.repeat(c)))
                })
            }));
        }
        "padStart" | "padEnd" => {
            let op = name.to_string();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let t2 = t.clone();
                let op2 = op.clone();
                Box::pin(async move {
                    let ln = to_int32(&args.first().cloned().unwrap_or(JsValue::Undefined)).max(0);
                    let fill = match args.get(1) {
                        Some(JsValue::Str(s)) => s.to_string(),
                        _ => " ".to_string(),
                    };
                    let left = op2 == "padStart";
                    Ok(JsValue::str(js_pad(&t2, ln as i64, &fill, left)?))
                })
            }));
        }
        "split" => {
            let i0 = this.clone();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let i3 = i0.clone();
                let t2 = t.clone();
                Box::pin(async move {
                    let sep = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let limit = args.get(1).cloned().unwrap_or(JsValue::Undefined);
                    string_split(&i3, &t2, &sep, &limit)
                })
            }));
        }
        "match" | "matchAll" => {
            let i0 = this.clone();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let i3 = i0.clone();
                let t2 = t.clone();
                Box::pin(async move {
                    let regex = args.first().cloned().unwrap_or(JsValue::Undefined);
                    string_match(&i3, &t2, &regex)
                })
            }));
        }
        "replace" | "replaceAll" => {
            let op = name.to_string();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let t2 = t.clone();
                let op2 = op.clone();
                Box::pin(async move {
                    let pat = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let repl = args.get(1).cloned().unwrap_or(JsValue::Undefined);
                    string_replace(&i2, &t2, &pat, &repl, op2 == "replaceAll").await
                })
            }));
        }
        "localeCompare" => {
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let t2 = t.clone();
                Box::pin(async move {
                    let other = i2.repr(&args.first().cloned().unwrap_or(JsValue::Undefined));
                    let res = if t2.as_ref() == other {
                        0.0
                    } else if t2.as_ref() < other.as_str() {
                        -1.0
                    } else {
                        1.0
                    };
                    Ok(JsValue::Number(res))
                })
            }));
        }
        "toString" | "valueOf" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let t2 = t.clone();
                Box::pin(async move { Ok(JsValue::Str(t2)) })
            }));
        }
        _ => {}
    }
    if let Some(index) = int_index(name) {
        let n = char_count(text);
        if -n <= index && index < n {
            let i = if index < 0 { n + index } else { index };
            return JsValue::str(safe_char(text, i));
        }
    }
    JsValue::Undefined
}

// -- number_get ------------------------------------------------------------

pub fn number_get(this: &Rc<Interpreter>, num: f64, name: &str) -> JsValue {
    match name {
        "toFixed" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let digits = match args.first() {
                    Some(v) if !nullish(v) => to_number(v),
                    _ => 0.0,
                };
                Box::pin(async move { Ok(JsValue::str(to_fixed(num, digits))) })
            }));
        }
        "toString" => {
            let i0 = this.clone();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let i3 = i0.clone();
                Box::pin(async move {
                    let radix = args.first().cloned().unwrap_or(JsValue::Undefined);
                    Ok(JsValue::str(number_to_string_radix(&i3, num, &radix)?))
                })
            }));
        }
        "toLocaleString" => {
            let i0 = this.clone();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let i3 = i0.clone();
                Box::pin(async move {
                    let radix = args.first().cloned().unwrap_or(JsValue::Undefined);
                    Ok(JsValue::str(number_to_string_radix(&i3, num, &radix)?))
                })
            }));
        }
        "valueOf" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                Box::pin(async move { Ok(JsValue::Number(num)) })
            }));
        }
        _ => JsValue::Undefined,
    }
}

// -- map/set ---------------------------------------------------------------

pub fn map_get(this: &Rc<Interpreter>, m: &Rc<RefCell<JsMap>>, name: &str) -> JsValue {
    let i0 = this.clone();
    match name {
        "set" => {
            let m2 = m.clone();
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let k = args.first().cloned().unwrap_or(JsValue::Undefined);
                let v = args.get(1).cloned().unwrap_or(JsValue::Undefined);
                let key = map_key(&k);
                m2.borrow().store.borrow_mut().insert(key, v);
                let out = JsValue::Map(m2.clone());
                Box::pin(async move { Ok(out) })
            }));
        }
        "get" => {
            let m2 = m.clone();
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let k = args.first().cloned().unwrap_or(JsValue::Undefined);
                let key = map_key(&k);
                let v = m2.borrow().store.borrow().get(&key).cloned().unwrap_or(JsValue::Undefined);
                Box::pin(async move { Ok(v) })
            }));
        }
        "has" => {
            let m2 = m.clone();
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let k = args.first().cloned().unwrap_or(JsValue::Undefined);
                let key = map_key(&k);
                let has = m2.borrow().store.borrow().contains_key(&key);
                Box::pin(async move { Ok(JsValue::Bool(has)) })
            }));
        }
        "delete" => {
            let m2 = m.clone();
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let k = args.first().cloned().unwrap_or(JsValue::Undefined);
                let key = map_key(&k);
                let removed = m2.borrow().store.borrow_mut().remove(&key).is_some();
                Box::pin(async move { Ok(JsValue::Bool(removed)) })
            }));
        }
        "clear" => {
            let m2 = m.clone();
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                m2.borrow().store.borrow_mut().clear();
                Box::pin(async move { Ok(JsValue::Undefined) })
            }));
        }
        "size" => {
            return JsValue::Number(m.borrow().store.borrow().len() as f64);
        }
        "forEach" => {
            let m3 = m.clone();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let i3 = i0.clone();
                let m2 = m3.clone();
                Box::pin(async move {
                    let fn_ = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let entries: Vec<(JsValue, JsValue)> = m2
                        .borrow()
                        .store
                        .borrow()
                        .iter()
                        .map(|(_, v)| (v.clone(), v.clone()))
                        .collect();
                    for (_k, v) in entries {
                        call_value(&i2, &fn_, vec![v.clone(), JsValue::Undefined, JsValue::Map(m2.clone())], JsValue::Undefined).await?;
                    }
                    Ok(JsValue::Undefined)
                })
            }));
        }
        _ => JsValue::Undefined,
    }
}

pub fn set_get(this: &Rc<Interpreter>, s: &Rc<RefCell<JsSet>>, name: &str) -> JsValue {
    match name {
        "add" => {
            let s2 = s.clone();
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let v = args.first().cloned().unwrap_or(JsValue::Undefined);
                let key = map_key(&v);
                s2.borrow().store.borrow_mut().insert(key, v);
                let out = JsValue::Set(s2.clone());
                Box::pin(async move { Ok(out) })
            }));
        }
        "has" => {
            let s2 = s.clone();
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let v = args.first().cloned().unwrap_or(JsValue::Undefined);
                let key = map_key(&v);
                let has = s2.borrow().store.borrow().contains_key(&key);
                Box::pin(async move { Ok(JsValue::Bool(has)) })
            }));
        }
        "delete" => {
            let s2 = s.clone();
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let v = args.first().cloned().unwrap_or(JsValue::Undefined);
                let key = map_key(&v);
                let removed = s2.borrow().store.borrow_mut().remove(&key).is_some();
                Box::pin(async move { Ok(JsValue::Bool(removed)) })
            }));
        }
        "clear" => {
            let s2 = s.clone();
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                s2.borrow().store.borrow_mut().clear();
                Box::pin(async move { Ok(JsValue::Undefined) })
            }));
        }
        "size" => {
            return JsValue::Number(s.borrow().store.borrow().len() as f64);
        }
        "forEach" => {
            let i0 = this.clone();
            let s3 = s.clone();
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let s2 = s3.clone();
                Box::pin(async move {
                    let fn_ = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let values: Vec<JsValue> = s2.borrow().store.borrow().values().cloned().collect();
                    for v in values {
                        call_value(&i2, &fn_, vec![v.clone(), v.clone(), JsValue::Set(s2.clone())], JsValue::Undefined).await?;
                    }
                    Ok(JsValue::Undefined)
                })
            }));
        }
        _ => JsValue::Undefined,
    }
}

// -- date ------------------------------------------------------------------

fn date_repr(d: &JsDate) -> String {
    match &d.local {
        Some(dt) => format!(
            "{} {:02} {:03} {} {}:{}:{} GMT+0000",
            weekday_name(dt.weekday().num_days_from_monday()),
            dt.day(),
            month_name(dt.month()),
            dt.year(),
            dt.hour(),
            dt.minute(),
            dt.second()
        ),
        None => "Invalid Date".to_string(),
    }
}

use chrono::{Datelike, Timelike};

fn weekday_name(d: u32) -> &'static str {
    const N: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    N[(d as usize) % 7]
}

fn month_name(m: u32) -> &'static str {
    const N: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    N[((m as usize + 11) % 12)]
}

pub fn date_get(this: &Rc<Interpreter>, d: &Rc<RefCell<JsDate>>, name: &str) -> JsValue {
    let d2 = d.clone();
    let make = move |f: fn(&JsDate) -> Option<f64>| -> JsValue {
        let d2 = d2.clone();
        JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
            let v = f(&d2.borrow()).unwrap_or(f64::NAN);
            Box::pin(async move { Ok(JsValue::Number(v)) })
        }))
    };
    match name {
        "getTime" | "valueOf" => return make(|d| Some(d.ms)),
        "getFullYear" => return make(|d| d.local.as_ref().map(|x| x.year() as f64)),
        "getMonth" => return make(|d| d.local.as_ref().map(|x| (x.month() - 1) as f64)),
        "getDate" => return make(|d| d.local.as_ref().map(|x| x.day() as f64)),
        "getDay" => return make(|d| d.local.as_ref().map(|x| x.weekday().num_days_from_monday() as f64)),
        "getHours" => return make(|d| d.local.as_ref().map(|x| x.hour() as f64)),
        "getMinutes" => return make(|d| d.local.as_ref().map(|x| x.minute() as f64)),
        "getSeconds" => return make(|d| d.local.as_ref().map(|x| x.second() as f64)),
        "getMilliseconds" => return make(|d| Some((d.ms as i64).rem_euclid(1000) as f64)),
        "getUTCFullYear" => return make(|d| d.utc.as_ref().map(|x| x.year() as f64)),
        "getUTCMonth" => return make(|d| d.utc.as_ref().map(|x| (x.month() - 1) as f64)),
        "getUTCDate" => return make(|d| d.utc.as_ref().map(|x| x.day() as f64)),
        "getUTCDay" => return make(|d| d.utc.as_ref().map(|x| x.weekday().num_days_from_monday() as f64)),
        "getUTCHours" => return make(|d| d.utc.as_ref().map(|x| x.hour() as f64)),
        "getUTCMinutes" => return make(|d| d.utc.as_ref().map(|x| x.minute() as f64)),
        "getUTCSeconds" => return make(|d| d.utc.as_ref().map(|x| x.second() as f64)),
        "getUTCMilliseconds" => return make(|d| Some((d.ms as i64).rem_euclid(1000) as f64)),
        _ => {}
    }
    let d2 = d.clone();
    match name {
        "toISOString" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let d = d2.borrow();
                let s = match &d.utc {
                    Some(u) => format!(
                        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
                        u.year(),
                        u.month(),
                        u.day(),
                        u.hour(),
                        u.minute(),
                        u.second(),
                        (d.ms as i64).rem_euclid(1000)
                    ),
                    None => "Invalid Date".to_string(),
                };
                Box::pin(async move { Ok(JsValue::str(s)) })
            }));
        }
        "toUTCString" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let d = d2.borrow();
                let s = match &d.utc {
                    Some(u) => format!(
                        "{} {:02} {} {} {}:{}:{} GMT",
                        weekday_name(u.weekday().num_days_from_monday()),
                        u.day(),
                        month_name(u.month()),
                        u.year(),
                        u.hour(),
                        u.minute(),
                        u.second()
                    ),
                    None => "Invalid Date".to_string(),
                };
                Box::pin(async move { Ok(JsValue::str(s)) })
            }));
        }
        "toLocaleString" | "toString" | "toDateString" | "toTimeString" => {
            return JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, _args: Vec<JsValue>| -> EvResult {
                let d = d2.borrow();
                let s = match &d.local {
                    Some(x) => format!(
                        "{} {:02} {} {} {}:{}:{} GMT+0000",
                        weekday_name(x.weekday().num_days_from_monday()),
                        x.day(),
                        month_name(x.month()),
                        x.year(),
                        x.hour(),
                        x.minute(),
                        x.second()
                    ),
                    None => "Invalid Date".to_string(),
                };
                Box::pin(async move { Ok(JsValue::str(s)) })
            }));
        }
        _ => JsValue::Undefined,
    }
}

// -- regex/error -----------------------------------------------------------

pub fn regex_get(this: &Rc<Interpreter>, r: &Rc<RefCell<JsRegex>>, name: &str) -> JsValue {
    let r2 = r.clone();
    match name {
        "source" => {
            return JsValue::str(r.borrow().source.clone());
        }
        "flags" => {
            return JsValue::str(r.borrow().flags.clone());
        }
        "global" => {
            return JsValue::Bool(r.borrow().global_);
        }
        "ignoreCase" => {
            return JsValue::Bool(r.borrow().ignore_case);
        }
        "multiline" => {
            return JsValue::Bool(r.borrow().multiline);
        }
        "lastIndex" => {
            return JsValue::Number(r.borrow().last_index.get());
        }
        "test" => {
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let r2 = r2.clone();
                Box::pin(async move {
                    let text = i2.repr(&args.first().cloned().unwrap_or(JsValue::Undefined));
                    let mut r = r2.borrow_mut();
                    let start = if r.global_ {
                        r.last_index.get() as usize
                    } else {
                        0
                    };
                    let m = r.re.find(&text[start.min(text.len())..]);
                    match m {
                        Some(m) => {
                            if r.global_ {
                                let byte_end = start + m.end();
                                r.last_index.set(char_of_byte(&text, byte_end) as f64);
                            }
                            Ok(JsValue::Bool(true))
                        }
                        None => {
                            if r.global_ {
                                r.last_index.set(0.0);
                            }
                            Ok(JsValue::Bool(false))
                        }
                    }
                })
            }));
        }
        "exec" => {
            return JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let r2 = r2.clone();
                Box::pin(async move {
                    let text = i2.repr(&args.first().cloned().unwrap_or(JsValue::Undefined));
                    let mut r = r2.borrow_mut();
                    let start = if r.global_ {
                        r.last_index.get() as usize
                    } else {
                        0
                    };
                    let m = r.re.find(&text[start.min(text.len())..]);
                    match m {
                        Some(m) => {
                            if r.global_ {
                                let byte_end = start + m.end();
                                r.last_index.set(char_of_byte(&text, byte_end) as f64);
                            }
                            let mut out = vec![JsValue::str(m.as_str())];
                            if let Some(caps) = r.re.captures(&text[start..]) {
                                for i in 1..caps.len() {
                                    out.push(match caps.get(i) {
                                        Some(g) => JsValue::str(g.as_str()),
                                        None => JsValue::Undefined,
                                    });
                                }
                            }
                            Ok(JsValue::array(out))
                        }
                        None => {
                            if r.global_ {
                                r.last_index.set(0.0);
                            }
                            Ok(JsValue::Null)
                        }
                    }
                })
            }));
        }
        _ => JsValue::Undefined,
    }
}

pub fn error_get(e: &Rc<RefCell<JsHostError>>, name: &str) -> JsValue {
    match name {
        "message" => JsValue::str(e.borrow().message.clone()),
        "name" => JsValue::str(e.borrow().name.clone()),
        "stack" => {
            let e = e.borrow();
            JsValue::str(format!("{}: {}", e.name, e.message))
        }
        _ => JsValue::Undefined,
    }
}

// -- super / class / instance ----------------------------------------------

pub fn super_get(s: &JsSuper, name: &str) -> JsValue {
    match &s.parent_proto {
        JsValue::Object(map) => map
            .borrow()
            .get(name)
            .cloned()
            .unwrap_or(JsValue::Undefined),
        _ => JsValue::Undefined,
    }
}

pub fn class_get(this: &Rc<Interpreter>, c: &Rc<RefCell<JsClass>>, name: &str) -> Result<JsValue, JsError> {
    let c = c.borrow();
    if name == "prototype" {
        return Ok(JsValue::Object(c.prototype.clone()));
    }
    if name == "name" {
        return Ok(JsValue::str(c.name.clone()));
    }
    if name == "length" {
        let n = c.ctor.as_ref().map(|f| f.params.len()).unwrap_or(0) as f64;
        return Ok(JsValue::Number(n));
    }
    if let Some(v) = c.statics.borrow().get(name) {
        return Ok(v.clone());
    }
    if let Some(v) = c.prototype.borrow().get(name) {
        return Ok(v.clone());
    }
    if let Some(parent) = &c.parent {
        return js_get(this, parent, name);
    }
    Ok(JsValue::Undefined)
}

pub fn class_set(c: &Rc<RefCell<JsClass>>, name: &str, value: &JsValue) -> Result<(), JsError> {
    let is_proto_member = c.borrow().prototype.borrow().contains_key(name);
    if is_proto_member {
        c.borrow().prototype.borrow_mut().insert(name.to_string(), value.clone());
    } else {
        c.borrow().statics.borrow_mut().insert(name.to_string(), value.clone());
    }
    Ok(())
}

pub fn instance_get(inst: &Rc<RefCell<JsClassInstance>>, name: &str) -> JsValue {
    let inst = inst.borrow();
    if let Some(v) = inst.props.borrow().get(name) {
        return v.clone();
    }
    let mut p = Some(inst.proto.clone());
    while let Some(pp) = p {
        if let Some(v) = pp.borrow().get(name) {
            return v.clone();
        }
        let next = pp.borrow().get("__proto__").cloned();
        p = match next {
            Some(JsValue::Object(m)) => Some(m),
            _ => None,
        };
    }
    JsValue::Undefined
}

// -- function_get ----------------------------------------------------------

pub fn function_get(
    this: &Rc<Interpreter>,
    f: &Rc<JSFunction>,
    name: &str,
) -> Result<JsValue, JsError> {
    match name {
        "length" => Ok(JsValue::Number(f.params.len() as f64)),
        "name" => Ok(JsValue::str(f.name.clone())),
        "prototype" => Ok(JsValue::Object(JSFunction::prototype_obj(f))),
        "call" => {
            let f2 = f.clone();
            Ok(JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let f2 = f2.clone();
                Box::pin(async move {
                    let this_arg = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let rest = args.into_iter().skip(1).collect::<Vec<_>>();
                    call_value(&i2, &JsValue::Function(f2.clone()), rest, this_arg).await
                })
            })))
        }
        "apply" => {
            let f2 = f.clone();
            Ok(JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let i2 = i.clone();
                let f2 = f2.clone();
                Box::pin(async move {
                    let this_arg = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let arg_list = match args.get(1) {
                        Some(JsValue::Array(a)) => a.borrow().clone(),
                        _ => vec![],
                    };
                    call_value(&i2, &JsValue::Function(f2.clone()), arg_list, this_arg).await
                })
            })))
        }
        "bind" => {
            let f2 = f.clone();
            Ok(JsValue::Callback(Rc::new(move |_i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                let f2 = f2.clone();
                Box::pin(async move {
                    let this_arg = args.first().cloned().unwrap_or(JsValue::Undefined);
                    let pre = args.into_iter().skip(1).collect::<Vec<_>>();
                    Ok(make_bound(f2, this_arg, pre))
                })
            })))
        }
        _ => Ok(JsValue::Undefined),
    }
}

fn make_bound(f: Rc<JSFunction>, this_arg: JsValue, pre: Vec<JsValue>) -> JsValue {
    JsValue::Callback(Rc::new(move |interp: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
        let mut all = pre.clone();
        all.extend(args);
        call_value(
            interp,
            &JsValue::Function(f.clone()),
            all,
            this_arg.clone(),
        )
    }))
}

// -- promise_get -----------------------------------------------------------

pub fn promise_get(
    this: &Rc<Interpreter>,
    p: &Rc<RefCell<JsPromise>>,
    name: &str,
) -> Result<JsValue, JsError> {
    let p2 = p.clone();
    match name {
        "then" => Ok(JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
            let i2 = i.clone();
            let p2 = p2.clone();
            Box::pin(async move {
                let on_ok = args.first().cloned().unwrap_or(JsValue::Undefined);
                let on_err = args.get(1).cloned().unwrap_or(JsValue::Undefined);
                let child = promise_then(&i2, &p2, &on_ok, &on_err);
                Ok(JsValue::Promise(child))
            })
        }))),
        "catch" => Ok(JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
            let i2 = i.clone();
            let p2 = p2.clone();
            Box::pin(async move {
                let on_err = args.first().cloned().unwrap_or(JsValue::Undefined);
                let child = promise_then(&i2, &p2, &JsValue::Undefined, &on_err);
                Ok(JsValue::Promise(child))
            })
        }))),
        "finally" => Ok(JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
            let i2 = i.clone();
            let p2 = p2.clone();
            Box::pin(async move {
                let cb = args.first().cloned().unwrap_or(JsValue::Undefined);
                let child = promise_finally(&i2, &p2, &cb);
                Ok(JsValue::Promise(child))
            })
        }))),
        _ => Ok(JsValue::Undefined),
    }
}
// -- promise machinery -----------------------------------------------------

pub fn promise_resolve(this: &Rc<Interpreter>, p: &Rc<RefCell<JsPromise>>, value: JsValue) {
    settle(this, p, true, value);
}

pub fn promise_reject(this: &Rc<Interpreter>, p: &Rc<RefCell<JsPromise>>, reason: JsValue) {
    settle(this, p, false, reason);
}

fn settle(this: &Rc<Interpreter>, p: &Rc<RefCell<JsPromise>>, ok: bool, value: JsValue) {
    if !p.borrow().is_pending() {
        return;
    }
    if ok {
        if let JsValue::Promise(other) = &value {
            if Rc::ptr_eq(other, p) {
                return settle(this, p, false, JsValue::str("Chaining cycle detected"));
            }
            if other.borrow().is_pending() {
                let this2 = this.clone();
                let p2 = p.clone();
                let other2 = other.clone();
                promise_on_settle(this, &other2, Rc::new(move |v, r| {
                    if r {
                        promise_reject(&this2, &p2, v);
                    } else {
                        promise_resolve(&this2, &p2, v);
                    }
                }));
                return;
            }
            if other.borrow().rejected() {
                let v = other.borrow().value();
                return settle(this, p, false, v);
            }
            let v = other.borrow().value();
            return settle(this, p, true, v);
        } else if let Some(then) = thenable_method(this, &value) {
            return assimilate(this, p, value, then);
        }
    }
    {
        let pstate = p.borrow();
        *pstate.state.borrow_mut() = if ok {
            PromiseState::Resolved(value.clone())
        } else {
            PromiseState::Rejected(value.clone())
        };
        let observers = std::mem::take(&mut *pstate.observers.borrow_mut());
        drop(pstate);
        let this2 = this.clone();
        for cb in observers {
            let v = value.clone();
            let this3 = this2.clone();
            this2.enqueue(Rc::new(move || cb(v.clone(), !ok)));
        }
        if !ok {
            this2.note_unhandled_rejection(&value);
        }
    }
}

pub fn promise_on_settle(
    this: &Rc<Interpreter>,
    p: &Rc<RefCell<JsPromise>>,
    cb: Rc<dyn Fn(JsValue, bool)>,
) {
    let (pending, state_value, rejected) = {
        let b = p.borrow();
        (
            b.is_pending(),
            b.value(),
            b.rejected(),
        )
    };
    if pending {
        p.borrow_mut().observers.borrow_mut().push(cb);
    } else {
        let this2 = this.clone();
        this.enqueue(Rc::new(move || {
            cb(state_value.clone(), rejected);
        }));
    }
}

fn assimilate(this: &Rc<Interpreter>, p: &Rc<RefCell<JsPromise>>, value: JsValue, then: JsValue) {
    let this2 = this.clone();
    let p2 = p.clone();
    let on_ok = JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
        let v = args.first().cloned().unwrap_or(JsValue::Undefined);
        promise_resolve(i, &p2, v);
        Box::pin(async { Ok(JsValue::Undefined) })
    }));
    let this3 = this.clone();
    let p3 = p.clone();
    let on_err = JsValue::Callback(Rc::new(move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
        let v = args.first().cloned().unwrap_or(JsValue::Undefined);
        promise_reject(i, &p3, v);
        Box::pin(async { Ok(JsValue::Undefined) })
    }));
    let r = drive_sync(
        &this2,
        call_value(&this2, &then, vec![on_ok, on_err], JsValue::Undefined),
    );
    let _ = value;
    if r.is_err() {
        promise_reject(&this3, p, JsValue::str("Error while assimilating thenable"));
    }
}

pub fn promise_then(
    this: &Rc<Interpreter>,
    p: &Rc<RefCell<JsPromise>>,
    on_ok: &JsValue,
    on_err: &JsValue,
) -> Rc<RefCell<JsPromise>> {
    let child = JsPromise::new();
    let this2 = this.clone();
    let child2 = child.clone();
    let on_ok = on_ok.clone();
    let on_err = on_err.clone();
    let cb: Rc<dyn Fn(JsValue, bool)> = Rc::new(move |value, rejected| {
        let handler = if rejected { &on_err } else { &on_ok };
        if nullish(handler) {
            if rejected {
                promise_reject(&this2, &child2, value);
            } else {
                promise_resolve(&this2, &child2, value);
            }
            return;
        }
        let r = drive_sync(
            &this2,
            call_value(&this2, handler, vec![value], JsValue::Undefined),
        );
        match r {
            Ok(v) => promise_resolve(&this2, &child2, v),
            Err(JsError::Thrown(t)) => promise_reject(&this2, &child2, t),
            Err(e) => promise_reject(&this2, &child2, JsValue::str(js_error_message(&this2, &e))),
        }
    });
    promise_on_settle(this, p, cb);
    child
}

pub fn promise_finally(
    this: &Rc<Interpreter>,
    p: &Rc<RefCell<JsPromise>>,
    cb: &JsValue,
) -> Rc<RefCell<JsPromise>> {
    let child = JsPromise::new();
    let this2 = this.clone();
    let child2 = child.clone();
    let cb = cb.clone();
    let run_settle: Rc<dyn Fn(JsValue, bool)> = Rc::new(move |value, rejected| {
        let result = if nullish(&cb) {
            Ok(JsValue::Undefined)
        } else {
            drive_sync(&this2, call_value(&this2, &cb, vec![], JsValue::Undefined))
        };
        match result {
            Ok(result) => {
                if let JsValue::Promise(rp) = result {
                    let child3 = child2.clone();
                    let inner = this2.clone();
                    let v = value.clone();
                    promise_on_settle(&this2, &rp, Rc::new(move |_v, r| {
                        if r {
                            promise_reject(&inner, &child3, _v);
                        } else if rejected {
                            promise_reject(&inner, &child3, v.clone());
                        } else {
                            promise_resolve(&inner, &child3, v.clone());
                        }
                    }));
                } else if rejected {
                    promise_reject(&this2, &child2, value);
                } else {
                    promise_resolve(&this2, &child2, value);
                }
            }
            Err(e) => {
                let msg = match &e {
                    JsError::Thrown(t) => t.clone(),
                    _ => JsValue::str(js_error_message(&this2, &e)),
                };
                promise_reject(&this2, &child2, msg);
            }
        }
    });
    promise_on_settle(this, p, run_settle);
    child
}

pub fn as_promise(this: &Rc<Interpreter>, value: &JsValue) -> Rc<RefCell<JsPromise>> {
    if let JsValue::Promise(p) = value {
        return p.clone();
    }
    if let Some(then) = thenable_method(this, value) {
        let p = JsPromise::new();
        assimilate(this, &p, value.clone(), then);
        return p;
    }
    let p = JsPromise::new();
    promise_resolve(this, &p, value.clone());
    p
}

pub fn thenable_method(this: &Rc<Interpreter>, value: &JsValue) -> Option<JsValue> {
    if !is_objectish(value) {
        return None;
    }
    let then = match js_get(this, value, "then") {
        Ok(t) => t,
        Err(_) => return None,
    };
    if is_js_function(&then) {
        Some(then)
    } else {
        None
    }
}

// -- call / construct ------------------------------------------------------

pub fn call_value(
    this: &Rc<Interpreter>,
    fn_: &JsValue,
    args: Vec<JsValue>,
    this_arg: JsValue,
) -> EvResult {
    let this = this.clone();
    let fn_ = fn_.clone();
    Box::pin(async move {
        match &fn_ {
            JsValue::Function(f) => {
                if f.async_ {
                    Ok(start_async_call(&this, f, args, this_arg))
                } else {
                    call_function(&this, f, args, this_arg).await
                }
            }
            JsValue::Native(n) => match n.call {
                Some(call) => call(&this, &fn_, args).await,
                None => Err(JsError::js(format!(
                    "{} is not a function.",
                    this.repr(&fn_)
                ))),
            },
            JsValue::Callback(cb) => cb.call(&this, args).await,
            JsValue::Host(h) => {
                let h = Python::attach(|py| h.clone_ref(py));
                host_js_call(&this, &h, &args)
            }
            _ => Err(JsError::js(format!(
                "{} is not a function.",
                this.repr(&fn_)
            ))),
        }
    })
}

fn bind_args(
    this: &Rc<Interpreter>,
    fn_: &Rc<JSFunction>,
    scope: Env,
    args: Vec<JsValue>,
) -> StResult {
    let this = this.clone();
    let fn_ = fn_.clone();
    Box::pin(async move {
        for (i, name) in fn_.params.iter().enumerate() {
            if i < args.len() {
                scope.set_var(name, args[i].clone());
            } else if let Some(default) = fn_.defaults.get(name) {
                let v = eval(&this, default, scope.clone()).await?;
                scope.set_var(name, v);
            } else {
                scope.set_var(name, JsValue::Undefined);
            }
        }
        if let Some(rest) = &fn_.rest {
            scope.set_var(rest, JsValue::array(args[fn_.params.len()..].to_vec()));
        }
        Ok(())
    })
}

fn set_this(scope: &Env, fn_: &JSFunction, this_arg: &JsValue) {
    if fn_.arrow {
        let t = fn_.env.get("this");
        scope.vars.borrow_mut().insert("this".to_string(), t);
    } else if !matches!(this_arg, JsValue::Undefined) {
        scope
            .vars
            .borrow_mut()
            .insert("this".to_string(), this_arg.clone());
    }
    if let Some((pp, pc)) = &fn_.super_info {
        scope.vars.borrow_mut().insert(
            "__super__".to_string(),
            JsValue::Super(Rc::new(JsSuper {
                this: this_arg.clone(),
                parent_proto: pp.clone(),
                parent_ctor: pc.clone(),
            })),
        );
    }
}

fn call_function(
    this: &Rc<Interpreter>,
    fn_: &Rc<JSFunction>,
    args: Vec<JsValue>,
    this_arg: JsValue,
) -> EvResult {
    let this = this.clone();
    let fn_ = fn_.clone();
    Box::pin(async move {
        let scope = Environment::function(Some(fn_.env.clone()));
        bind_args(&this, &fn_, scope.clone(), args).await?;
        set_this(&scope, &fn_, &this_arg);
        let result = if let Some(expr) = &fn_.body_expr {
            eval(&this, expr, scope.clone()).await
        } else {
            match exec_block(&this, &fn_.body, scope.clone()).await {
                Ok(()) => Ok(JsValue::Undefined),
                Err(JsError::Return(v)) => Ok(v),
                Err(JsError::Break(_)) | Err(JsError::Continue(_)) => {
                    Err(JsError::js("Break or continue outside of a loop."))
                }
                Err(e) => Err(e),
            }
        };
        result
    })
}

fn start_async_call(
    this: &Rc<Interpreter>,
    fn_: &Rc<JSFunction>,
    args: Vec<JsValue>,
    this_arg: JsValue,
) -> JsValue {
    let promise = JsPromise::new();
    let this2 = this.clone();
    let this3 = this2.clone();
    let fn2 = fn_.clone();
    let p2 = promise.clone();
    let task: Task = Box::pin(async move {
        let scope = Environment::function(Some(fn2.env.clone()));
        match bind_args(&this2, &fn2, scope.clone(), args).await {
            Ok(()) => {
                set_this(&scope, &fn2, &this_arg);
                let r = exec_block(&this2, &fn2.body, scope.clone()).await;
                finish_async(&this2, &p2, r);
            }
            Err(e) => {
                promise_reject(&this2, &p2, JsValue::str(js_error_message(&this2, &e)));
            }
        }
    });
    this3.spawn_task(task);
    JsValue::Promise(promise)
}

fn finish_async(this: &Rc<Interpreter>, p: &Rc<RefCell<JsPromise>>, r: Result<(), JsError>) {
    match r {
        Ok(()) => promise_resolve(this, p, JsValue::Undefined),
        Err(JsError::Return(v)) => promise_resolve(this, p, v),
        Err(JsError::Break(_)) | Err(JsError::Continue(_)) => {
            promise_reject(this, p, JsValue::str("Break or continue outside of a loop."))
        }
        Err(JsError::Thrown(v)) => promise_reject(this, p, v),
        Err(e) => promise_reject(this, p, JsValue::str(js_error_message(this, &e))),
    }
}

pub fn construct(this: &Rc<Interpreter>, callee: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    let callee = callee.clone();
    Box::pin(async move {
        match &callee {
            JsValue::Class(c) => {
                let obj = JsValue::Instance(Rc::new(RefCell::new(JsClassInstance {
                    proto: c.borrow().prototype.clone(),
                    props: Rc::new(RefCell::new(BTreeMap::new())),
                })));
                class_construct_on_obj(&this, &c, &obj, args).await?;
                Ok(obj)
            }
            JsValue::Function(f) => {
                let obj = JsValue::Instance(Rc::new(RefCell::new(JsClassInstance {
                    proto: JSFunction::prototype_obj(f),
                    props: Rc::new(RefCell::new(BTreeMap::new())),
                })));
                let result = call_function(&this, f, args, obj.clone()).await?;
                if is_objectish(&result) {
                    Ok(result)
                } else {
                    Ok(obj)
                }
            }
            JsValue::Native(n) => match n.ctor {
                Some(ctor) => ctor(&this, &callee, args).await,
                None => Err(JsError::js(format!(
                    "{} is not a constructor",
                    this.repr(&callee)
                ))),
            },
            JsValue::Host(h) => {
                Python::attach(|py| {
                    let h = h.bind(py);
                    match h.getattr("js_new") {
                        Ok(js_new) => {
                            let py_args = py_args(&this, py, &args).map_err(py_err_to_js)?;
                            match js_new.call(py_args, None) {
                                Ok(result) => Ok(py_to_js(&this, py, &result)),
                                Err(e) => Err(py_err_to_js(e)),
                            }
                        }
                        Err(_) => Err(JsError::js(format!(
                            "{} is not a constructor",
                            this.repr(&callee)
                        ))),
                    }
                })
            }
            _ => Err(JsError::js(format!(
                "{} is not a constructor",
                this.repr(&callee)
            ))),
        }
    })
}

fn class_construct_on_obj(
    this: &Rc<Interpreter>,
    c: &Rc<RefCell<JsClass>>,
    obj: &JsValue,
    args: Vec<JsValue>,
) -> StResult {
    let this = this.clone();
    let c = c.clone();
    let obj = obj.clone();
    Box::pin(async move {
        if let Some(ctor) = &c.borrow().ctor {
            construct_on(&this, &obj, ctor, args).await?;
        } else if let Some(parent) = &c.borrow().parent {
            match parent {
                JsValue::Class(pc) => class_construct_on_obj(&this, pc, &obj, args).await?,
                JsValue::Function(pf) => {
                    construct_on(&this, &obj, pf, args).await?;
                }
                _ => {}
            }
        }
        Ok(())
    })
}

async fn construct_on(
    this: &Rc<Interpreter>,
    obj: &JsValue,
    fn_: &Rc<JSFunction>,
    args: Vec<JsValue>,
) -> Result<JsValue, JsError> {
    let scope = Environment::function(Some(fn_.env.clone()));
    bind_args(this, fn_, scope.clone(), args).await?;
    set_this(&scope, fn_, obj);
    match exec_block(this, &fn_.body, scope.clone()).await {
        Ok(()) => Ok(obj.clone()),
        Err(JsError::Return(v)) => {
            if is_objectish(&v) {
                Ok(v)
            } else {
                Ok(obj.clone())
            }
        }
        Err(e) => Err(e),
    }
}

async fn super_call(
    this: &Rc<Interpreter>,
    sup: &JsSuper,
    args: Vec<JsValue>,
) -> Result<JsValue, JsError> {
    if !nullish(&sup.parent_ctor) {
        if let JsValue::Function(f) = &sup.parent_ctor {
            construct_on(this, &sup.this, f, args).await?;
        }
    }
    Ok(sup.this.clone())
}
// -- evaluator: expressions -------------------------------------------------

pub fn eval(this: &Rc<Interpreter>, node: &Rc<Node>, env: Env) -> EvResult {
    let this = this.clone();
    let node = node.clone();
    Box::pin(async move { eval_inner(&this, &node, env).await })
}

async fn eval_inner(
    this: &Rc<Interpreter>,
    node: &Rc<Node>,
    env: Env,
) -> Result<JsValue, JsError> {
    tick(this)?;
    match &**node {
        Node::Literal(lit) => Ok(match lit {
            LiteralVal::Number(n) => JsValue::Number(*n),
            LiteralVal::Str(s) => JsValue::Str(s.clone()),
            LiteralVal::Bool(b) => JsValue::Bool(*b),
            LiteralVal::Null => JsValue::Null,
            LiteralVal::Undefined => JsValue::Undefined,
        }),
        Node::Identifier(name) => {
            let value = env.get(name);
            if matches!(value, JsValue::Undefined) {
                if let Some(v) = this.globals.borrow().get(name) {
                    return Ok(v.clone());
                }
            }
            Ok(value)
        }
        Node::This => Ok(env.get("this")),
        Node::Super => {
            let sup = env.get("__super__");
            if let JsValue::Super(s) = sup {
                Ok(JsValue::Super(s))
            } else {
                Ok(JsValue::Undefined)
            }
        }
        Node::ArrayLit(items) => {
            let mut out = Vec::new();
            for item in items {
                if let Node::Spread(e) = &**item {
                    let v = eval(this, e, env.clone()).await?;
                    match &v {
                        JsValue::Array(a) => out.extend(a.borrow().iter().cloned()),
                        JsValue::Str(s) => {
                            for c in s.chars() {
                                out.push(JsValue::str(c.to_string()));
                            }
                        }
                        _ => out.push(v),
                    }
                } else {
                    out.push(eval(this, item, env.clone()).await?);
                }
            }
            Ok(JsValue::array(out))
        }
        Node::ObjectLit(pairs) => {
            let out = Rc::new(RefCell::new(BTreeMap::new()));
            for pair in pairs {
                match pair {
                    ObjectPair::Key(key, expr) => {
                        let v = eval(this, expr, env.clone()).await?;
                        out.borrow_mut().insert(key.clone(), v);
                    }
                    ObjectPair::Spread(expr) => {
                        let v = eval(this, expr, env.clone()).await?;
                        match &v {
                            JsValue::Object(map) => {
                                for (k, val) in map.borrow().iter() {
                                    out.borrow_mut().insert(k.clone(), val.clone());
                                }
                            }
                            JsValue::Instance(inst) => {
                                for (k, val) in inst.borrow().props.borrow().iter() {
                                    out.borrow_mut().insert(k.clone(), val.clone());
                                }
                            }
                            _ => {}
                        }
                    }
                }
            }
            Ok(JsValue::Object(out))
        }
        Node::FunctionExpr(f) | Node::ArrowFunc(f) => {
            Ok(JsValue::Function(js_function_from(f, env.clone())))
        }
        Node::ClassExpr(c) => eval_class(this, c, env.clone()).await,
        Node::TemplateLiteral { quasis, exprs } => {
            let mut out = quasis[0].clone();
            for (i, expr) in exprs.iter().enumerate() {
                let v = eval(this, expr, env.clone()).await?;
                out.push_str(&this.repr(&v));
                out.push_str(&quasis[i + 1]);
            }
            Ok(JsValue::str(out))
        }
        Node::Regex { source, flags } => Ok(JsValue::Regex(Rc::new(RefCell::new(
            compile_regex(source, flags),
        )))),
        Node::Unary(op, operand) => eval_unary(this, op, operand, env.clone()).await,
        Node::Sequence(items) => {
            let mut value = JsValue::Undefined;
            for item in items {
                value = eval(this, item, env.clone()).await?;
            }
            Ok(value)
        }
        Node::Update { op, operand, prefix } => {
            eval_update(this, op, operand, *prefix, env.clone()).await
        }
        Node::Binary(op, left, right) => {
            let l = eval(this, left, env.clone()).await?;
            let r = eval(this, right, env.clone()).await?;
            eval_binary(this, op, &l, &r)
        }
        Node::Logical(op, left, right) => {
            let l = eval(this, left, env.clone()).await?;
            if op == "??" {
                if nullish(&l) {
                    eval(this, right, env.clone()).await
                } else {
                    Ok(l)
                }
            } else if truthy(&l) == (op == "||") {
                Ok(l)
            } else {
                eval(this, right, env.clone()).await
            }
        }
        Node::Conditional {
            cond,
            then_expr,
            else_expr,
        } => {
            if truthy(&eval(this, cond, env.clone()).await?) {
                eval(this, then_expr, env.clone()).await
            } else {
                eval(this, else_expr, env.clone()).await
            }
        }
        Node::Assign { op, target, value } => eval_assign(this, op, target, value, env.clone()).await,
        Node::Call { callee, args, optional } => {
            eval_call(this, callee, args, *optional, env.clone()).await
        }
        Node::New { callee, args } => {
            let callee = eval(this, callee, env.clone()).await?;
            let args = eval_args(this, args, env.clone()).await?;
            construct(this, &callee, args).await
        }
        Node::Member { obj, name, optional } => {
            let o = eval(this, obj, env.clone()).await?;
            if *optional && nullish(&o) {
                return Ok(JsValue::Undefined);
            }
            js_get(this, &o, name)
        }
        Node::Index { obj, index, optional } => {
            let o = eval(this, obj, env.clone()).await?;
            if *optional && nullish(&o) {
                return Ok(JsValue::Undefined);
            }
            let name = index_name(this, &eval(this, index, env.clone()).await?);
            js_get(this, &o, &name)
        }
        Node::Await(expr) => {
            let value = eval(this, expr, env.clone()).await?;
            let promise = as_promise(this, &value);
            {
                let p = promise.borrow();
                let state = p.state.borrow();
                match &*state {
                    PromiseState::Rejected(v) => return Err(JsError::Thrown(v.clone())),
                    PromiseState::Resolved(v) => return Ok(v.clone()),
                    PromiseState::Pending => {}
                }
            }
            await_promise(this, &promise).await
        }
        _ => Err(JsError::js(format!(
            "Unknown expression {:?}.",
            std::mem::discriminant(&**node)
        ))),
    }
}

fn js_function_from(f: &FuncNode, env: Env) -> Rc<JSFunction> {
    Rc::new(JSFunction {
        name: f.name.clone(),
        params: f.params.clone(),
        defaults: f.defaults.clone(),
        rest: f.rest.clone(),
        body: f.body.clone(),
        body_expr: f.body_expr.clone(),
        env,
        async_: f.async_,
        arrow: f.arrow,
        super_info: None,
        prototype: RefCell::new(None),
    })
}

async fn await_promise(
    this: &Rc<Interpreter>,
    promise: &Rc<RefCell<JsPromise>>,
) -> Result<JsValue, JsError> {
    let _ = this;
    std::future::poll_fn(|cx| {
        let p = promise.borrow();
        let state = p.state.borrow();
        match &*state {
            PromiseState::Resolved(v) => Poll::Ready(Ok(v.clone())),
            PromiseState::Rejected(v) => Poll::Ready(Err(JsError::Thrown(v.clone()))),
            PromiseState::Pending => {
                let waker = cx.waker().clone();
                let p2 = promise.clone();
                p.observers.borrow_mut().push(Rc::new(move |_v, _r| {
                    waker.wake_by_ref();
                }));
                Poll::Pending
            }
        }
    })
    .await
}

async fn eval_args(
    this: &Rc<Interpreter>,
    args: &[Rc<Node>],
    env: Env,
) -> Result<Vec<JsValue>, JsError> {
    let mut out = Vec::new();
    for a in args {
        if let Node::Spread(e) = &**a {
            let v = eval(this, e, env.clone()).await?;
            match &v {
                JsValue::Array(arr) => out.extend(arr.borrow().iter().cloned()),
                JsValue::Str(s) => {
                    for c in s.chars() {
                        out.push(JsValue::str(c.to_string()));
                    }
                }
                _ => out.push(v),
            }
        } else {
            out.push(eval(this, a, env.clone()).await?);
        }
    }
    Ok(out)
}

async fn eval_call(
    this: &Rc<Interpreter>,
    callee: &Rc<Node>,
    args: &[Rc<Node>],
    optional: bool,
    env: Env,
) -> Result<JsValue, JsError> {
    if let Node::Super = &**callee {
        let sup = eval(this, callee, env.clone()).await?;
        let sup = match sup {
            JsValue::Super(s) => s,
            _ => return Err(JsError::js("'super' keyword unexpected here")),
        };
        let args = eval_args(this, args, env.clone()).await?;
        return super_call(this, &sup, args).await;
    }
    if let Node::Member { obj, name, optional: o } = &**callee {
        let obj_val = eval(this, obj, env.clone()).await?;
        if *o && nullish(&obj_val) {
            return Ok(JsValue::Undefined);
        }
        let f = js_get(this, &obj_val, name)?;
        let args = eval_args(this, args, env.clone()).await?;
        let this_arg = match &obj_val {
            JsValue::Super(s) => s.this.clone(),
            _ => obj_val.clone(),
        };
        return call_value(this, &f, args, this_arg).await;
    }
    if let Node::Index { obj, index, optional: o } = &**callee {
        let obj_val = eval(this, obj, env.clone()).await?;
        if *o && nullish(&obj_val) {
            return Ok(JsValue::Undefined);
        }
        let name = index_name(this, &eval(this, index, env.clone()).await?);
        let f = js_get(this, &obj_val, &name)?;
        let args = eval_args(this, args, env.clone()).await?;
        let this_arg = match &obj_val {
            JsValue::Super(s) => s.this.clone(),
            _ => obj_val.clone(),
        };
        return call_value(this, &f, args, this_arg).await;
    }
    let f = eval(this, callee, env.clone()).await?;
    if optional && nullish(&f) {
        return Ok(JsValue::Undefined);
    }
    let args = eval_args(this, args, env.clone()).await?;
    call_value(this, &f, args, JsValue::Undefined).await
}

async fn eval_class(
    this: &Rc<Interpreter>,
    node: &ClassNode,
    env: Env,
) -> Result<JsValue, JsError> {
    let mut parent: Option<JsValue> = None;
    if let Some(sc) = &node.superclass {
        let p = eval(this, sc, env.clone()).await?;
        if !matches!(p, JsValue::Class(_) | JsValue::Function(_)) {
            return Err(JsError::js("Class extends value is not a constructor"));
        }
        parent = Some(p);
    }
    let name = node.name.clone();
    let prototype = Rc::new(RefCell::new(BTreeMap::new()));
    let mut statics = BTreeMap::new();
    let mut parent_proto = JsValue::Undefined;
    let mut parent_ctor = JsValue::Undefined;
    if let Some(p) = &parent {
        let proto = match p {
            JsValue::Class(c) => c.borrow().prototype.clone(),
            JsValue::Function(f) => JSFunction::prototype_obj(f),
            _ => Rc::new(RefCell::new(BTreeMap::new())),
        };
        prototype
            .borrow_mut()
            .insert("__proto__".to_string(), JsValue::Object(proto.clone()));
        parent_proto = JsValue::Object(proto);
        parent_ctor = match p {
            JsValue::Class(c) => c
                .borrow()
                .ctor
                .clone()
                .map(|f| JsValue::Function(f))
                .unwrap_or(JsValue::Undefined),
            JsValue::Function(f) => JsValue::Function(f.clone()),
            _ => JsValue::Undefined,
        };
    }
    let super_info = if parent.is_some() {
        Some((parent_proto, parent_ctor))
    } else {
        None
    };
    let mut ctor_fn: Option<Rc<JSFunction>> = None;
    for m in &node.methods {
        let fn_ = Rc::new(JSFunction {
            name: name.clone(),
            params: m.params.clone(),
            defaults: m.defaults.clone(),
            rest: m.rest.clone(),
            body: m.body.clone(),
            body_expr: None,
            env: env.clone(),
            async_: false,
            arrow: false,
            super_info: super_info.clone(),
            prototype: RefCell::new(None),
        });
        if m.name == "constructor" && !m.is_static {
            ctor_fn = Some(fn_);
        } else if m.is_static {
            statics.insert(m.name.clone(), JsValue::Function(fn_));
        } else {
            prototype.borrow_mut().insert(m.name.clone(), JsValue::Function(fn_));
        }
    }
    let cls = Rc::new(RefCell::new(JsClass {
        name: name.clone(),
        prototype,
        ctor: ctor_fn,
        parent,
        statics: Rc::new(RefCell::new(statics)),
    }));
    Ok(JsValue::Class(cls))
}

async fn eval_unary(
    this: &Rc<Interpreter>,
    op: &str,
    operand: &Rc<Node>,
    env: Env,
) -> Result<JsValue, JsError> {
    let operand_val = eval(this, operand, env.clone()).await?;
    match op {
        "!" => Ok(JsValue::Bool(!truthy(&operand_val))),
        "-" => Ok(JsValue::Number(-to_number(&operand_val))),
        "+" => Ok(JsValue::Number(to_number(&operand_val))),
        "~" => Ok(JsValue::Number(!to_int32(&operand_val) as f64)),
        "typeof" => Ok(JsValue::str(Python::attach(|py| {
            typeof_value(py, &operand_val)
        }))),
        "void" => Ok(JsValue::Undefined),
        "delete" => {
            let (obj, name) = lvalue(this, operand, env.clone()).await?;
            match obj {
                None => Ok(JsValue::Bool(false)),
                Some(JsValue::Object(map)) => {
                    map.borrow_mut().remove(&name);
                    Ok(JsValue::Bool(true))
                }
                Some(JsValue::Instance(inst)) => {
                    inst.borrow().props.borrow_mut().remove(&name);
                    Ok(JsValue::Bool(true))
                }
                Some(JsValue::Array(arr)) => {
                    if let Some(idx) = int_index(&name) {
                        let mut a = arr.borrow_mut();
                        if idx >= 0 && (idx as usize) < a.len() {
                            a[idx as usize] = JsValue::Undefined;
                        }
                    }
                    Ok(JsValue::Bool(true))
                }
                Some(_) => Ok(JsValue::Bool(true)),
            }
        }
        _ => Err(JsError::js(format!("Unknown unary operator '{op}'."))),
    }
}

async fn eval_update(
    this: &Rc<Interpreter>,
    op: &str,
    operand: &Rc<Node>,
    prefix: bool,
    env: Env,
) -> Result<JsValue, JsError> {
    let current = read_lvalue(this, operand, env.clone()).await?;
    let value = to_number(&current) + if op == "++" { 1.0 } else { -1.0 };
    write_lvalue(this, operand, env.clone(), &JsValue::Number(value)).await?;
    if prefix {
        Ok(JsValue::Number(value))
    } else {
        Ok(current)
    }
}

async fn eval_assign(
    this: &Rc<Interpreter>,
    op: &str,
    target: &Rc<Node>,
    value_node: &Rc<Node>,
    env: Env,
) -> Result<JsValue, JsError> {
    let value = eval(this, value_node, env.clone()).await?;
    let (obj, name) = lvalue(this, target, env.clone()).await?;
    if op == "=" {
        match &obj {
            None => env.assign(&name, value.clone())?,
            Some(o) => js_set(this, o, &name, &value)?,
        }
        return Ok(value);
    }
    let current = match &obj {
        None => env.get(&name),
        Some(o) => js_get(this, o, &name)?,
    };
    let result = if op == "&&=" {
        if truthy(&current) {
            value.clone()
        } else {
            current.clone()
        }
    } else if op == "||=" {
        if truthy(&current) {
            current.clone()
        } else {
            value.clone()
        }
    } else if op == "??=" {
        if nullish(&current) {
            value.clone()
        } else {
            current.clone()
        }
    } else {
        binary_op(this, &op[..op.len() - 1], &current, &value)?
    };
    match &obj {
        None => env.assign(&name, result.clone())?,
        Some(o) => js_set(this, o, &name, &result)?,
    }
    Ok(result)
}

async fn lvalue(
    this: &Rc<Interpreter>,
    target: &Rc<Node>,
    env: Env,
) -> Result<(Option<JsValue>, String), JsError> {
    match &**target {
        Node::Identifier(name) => Ok((None, name.clone())),
        Node::Member { obj, name, .. } => {
            let o = eval(this, obj, env.clone()).await?;
            Ok((Some(o), name.clone()))
        }
        Node::Index { obj, index, .. } => {
            let o = eval(this, obj, env.clone()).await?;
            let name = index_name(this, &eval(this, index, env.clone()).await?);
            Ok((Some(o), name))
        }
        _ => Err(JsError::js("Invalid assignment target")),
    }
}

async fn read_lvalue(
    this: &Rc<Interpreter>,
    target: &Rc<Node>,
    env: Env,
) -> Result<JsValue, JsError> {
    let (obj, name) = lvalue(this, target, env.clone()).await?;
    match obj {
        None => Ok(env.get(&name)),
        Some(o) => js_get(this, &o, &name),
    }
}

async fn write_lvalue(
    this: &Rc<Interpreter>,
    target: &Rc<Node>,
    env: Env,
    value: &JsValue,
) -> Result<(), JsError> {
    let (obj, name) = lvalue(this, target, env.clone()).await?;
    match obj {
        None => env.assign(&name, value.clone())?,
        Some(o) => js_set(this, &o, &name, value)?,
    }
    Ok(())
}

fn eval_binary(
    this: &Interpreter,
    op: &str,
    left: &JsValue,
    right: &JsValue,
) -> Result<JsValue, JsError> {
    match op {
        "+" | "-" | "*" | "/" | "%" | "**" | "&" | "|" | "^" | "<<" | ">>" | ">>>" => {
            binary_op(this, op, left, right)
        }
        "in" => Ok(JsValue::Bool(eval_in(this, left, right)?)),
        "instanceof" => Ok(JsValue::Bool(eval_instanceof(this, left, right)?)),
        _ => compare(op, left, right),
    }
}

fn binary_op(
    this: &Interpreter,
    op: &str,
    left: &JsValue,
    right: &JsValue,
) -> Result<JsValue, JsError> {
    if op == "+" {
        if matches!(left, JsValue::Str(_) | JsValue::Array(_))
            || matches!(right, JsValue::Str(_) | JsValue::Array(_))
        {
            return Ok(JsValue::str(this.repr(left) + &this.repr(right)));
        }
        return Ok(JsValue::Number(to_number(left) + to_number(right)));
    }
    if op == "**" {
        let a = to_number(left);
        let b = to_number(right);
        let r = if a == 0.0 && b < 0.0 {
            f64::NAN
        } else {
            a.powf(b)
        };
        return Ok(JsValue::Number(r));
    }
    let (a, b) = (to_number(left), to_number(right));
    match op {
        "-" => Ok(JsValue::Number(a - b)),
        "*" => Ok(JsValue::Number(a * b)),
        "/" => Ok(JsValue::Number(divide(a, b))),
        "%" => Ok(JsValue::Number(modulo(a, b))),
        "&" => Ok(JsValue::Number((to_int32(left) & to_int32(right)) as f64)),
        "|" => Ok(JsValue::Number((to_int32(left) | to_int32(right)) as f64)),
        "^" => Ok(JsValue::Number((to_int32(left) ^ to_int32(right)) as f64)),
        "<<" => Ok(JsValue::Number(
            to_int32(left).wrapping_shl((to_int32(right) & 31) as u32) as f64,
        )),
        ">>" => Ok(JsValue::Number(
            to_int32(left).wrapping_shr((to_int32(right) & 31) as u32) as f64,
        )),
        ">>>" => Ok(JsValue::Number(
            ((to_int32(left) as u32) >> ((to_int32(right) as u32) & 31)) as f64,
        )),
        _ => Err(JsError::js(format!("Unknown binary operator '{op}'."))),
    }
}

fn compare(op: &str, left: &JsValue, right: &JsValue) -> Result<JsValue, JsError> {
    let result = match op {
        "==" => loose_eq(left, right),
        "!=" => !loose_eq(left, right),
        "===" => strict_eq(left, right),
        "!==" => !strict_eq(left, right),
        "<" => ordered(left, right),
        "<=" => !ordered(right, left),
        ">" => ordered(right, left),
        ">=" => !ordered(left, right),
        _ => return Err(JsError::js(format!("Unknown operator '{op}'."))),
    };
    Ok(JsValue::Bool(result))
}

fn ordered(left: &JsValue, right: &JsValue) -> bool {
    if let (JsValue::Str(x), JsValue::Str(y)) = (left, right) {
        return x.as_ref() < y.as_ref();
    }
    to_number(left) < to_number(right)
}

fn eval_in(this: &Interpreter, key: &JsValue, obj: &JsValue) -> Result<bool, JsError> {
    let name = match key {
        JsValue::Str(s) => s.to_string(),
        _ => this.repr(key),
    };
    match obj {
        JsValue::Object(map) => Ok(map.borrow().contains_key(&name)),
        JsValue::Instance(inst) => {
            let i = inst.borrow();
            if i.props.borrow().contains_key(&name) {
                return Ok(true);
            }
            let mut p = Some(i.proto.clone());
            while let Some(pp) = p {
                if pp.borrow().contains_key(&name) {
                    return Ok(true);
                }
                let next = pp.borrow().get("__proto__").cloned();
                p = match next {
                    Some(JsValue::Object(m)) => Some(m),
                    _ => None,
                };
            }
            Ok(false)
        }
        JsValue::Array(_) => Ok(int_index(&name).is_some()),
        _ => Ok(false),
    }
}

fn eval_instanceof(
    this: &Interpreter,
    obj: &JsValue,
    ctor: &JsValue,
) -> Result<bool, JsError> {
    let target: Rc<RefCell<BTreeMap<String, JsValue>>> = match ctor {
        JsValue::Class(c) => c.borrow().prototype.clone(),
        JsValue::Function(f) => JSFunction::prototype_obj(f),
        _ => {
            let globals = this.globals.borrow();
            let builtins: &[(&str, &[&str])] = &[
                ("Array", &["Array"]),
                ("Object", &["Object", "Array", "Instance"]),
                ("RegExp", &["Regex"]),
                ("Map", &["Map"]),
                ("Set", &["Set"]),
                ("Date", &["Date"]),
                ("String", &["Str"]),
                ("Number", &["Number"]),
            ];
            for (gname, variants) in builtins {
                if let Some(g) = globals.get(*gname) {
                    if same_ref(ctor, g) {
                        let m = |v: &JsValue| -> bool {
                            let t = js_typeof(v);
                            variants.contains(&t)
                        };
                        let _ = m;
                        let ok = match *variants {
                            ["Array"] => matches!(obj, JsValue::Array(_)),
                            ["Object", "Array", "Instance"] => {
                                matches!(obj, JsValue::Object(_) | JsValue::Array(_) | JsValue::Instance(_))
                            }
                            ["Regex"] => matches!(obj, JsValue::Regex(_)),
                            ["Map"] => matches!(obj, JsValue::Map(_)),
                            ["Set"] => matches!(obj, JsValue::Set(_)),
                            ["Date"] => matches!(obj, JsValue::Date(_)),
                            ["Str"] => matches!(obj, JsValue::Str(_)),
                            ["Number"] => matches!(obj, JsValue::Number(_)),
                            _ => false,
                        };
                        return Ok(ok);
                    }
                }
            }
            return Err(JsError::js(
                "Right-hand side of 'instanceof' is not callable",
            ));
        }
    };
    let inst = match obj {
        JsValue::Instance(i) => i,
        _ => return Ok(false),
    };
    let mut p = Some(inst.borrow().proto.clone());
    while let Some(pp) = p {
        if Rc::ptr_eq(&pp, &target) {
            return Ok(true);
        }
        let next = pp.borrow().get("__proto__").cloned();
        p = match next {
            Some(JsValue::Object(m)) => Some(m),
            _ => None,
        };
    }
    Ok(false)
}

// -- evaluator: statements -------------------------------------------------

pub fn exec(this: &Rc<Interpreter>, node: &Rc<Node>, env: Env) -> StResult {
    let this = this.clone();
    let node = node.clone();
    Box::pin(async move { exec_inner(&this, &node, env).await })
}

pub fn exec_block(this: &Rc<Interpreter>, stmts: &[Rc<Node>], env: Env) -> StResult {
    let this = this.clone();
    let stmts = stmts.to_vec();
    Box::pin(async move {
        for stmt in &stmts {
            if let Node::FunctionDecl(f) = &**stmt {
                env.set_var(&f.name, JsValue::Function(js_function_from(f, env.clone())));
            }
        }
        for stmt in &stmts {
            exec_inner(&this, stmt, env.clone()).await?;
        }
        Ok(())
    })
}

async fn exec_inner(
    this: &Rc<Interpreter>,
    node: &Rc<Node>,
    env: Env,
) -> Result<(), JsError> {
    tick(this)?;
    match &**node {
        Node::Program(stmts) => exec_block(this, stmts, env.clone()).await,
        Node::Block(stmts) => {
            exec_block(this, stmts, Environment::new(Some(env.clone()))).await
        }
        Node::VarDecl { kind, decls } => {
            for (target, expr) in decls {
                if kind == "const" && expr.is_none() {
                    let name = match target {
                        DeclTarget::Name(n) => n.clone(),
                        DeclTarget::Pattern(_) => "...".to_string(),
                    };
                    return Err(JsError::js(format!(
                        "Missing initializer in const declaration '{name}'."
                    )));
                }
                let value = match expr {
                    Some(e) => eval(this, e, env.clone()).await?,
                    None => JsValue::Undefined,
                };
                match target {
                    DeclTarget::Pattern(p) => {
                        let setter: fn(&Environment, &str, JsValue) = match kind.as_str() {
                            "var" => Environment::set_var,
                            "let" => Environment::set_let,
                            _ => Environment::set_const,
                        };
                        bind_pattern(this, p, &value, env.clone(), setter).await?;
                    }
                    DeclTarget::Name(name) => {
                        let setter: fn(&Environment, &str, JsValue) = match kind.as_str() {
                            "var" => Environment::set_var,
                            "let" => Environment::set_let,
                            _ => Environment::set_const,
                        };
                        setter(&env, name, value);
                    }
                }
            }
            Ok(())
        }
        Node::ClassDecl(c) => {
            let cls = eval_class(this, c, env.clone()).await?;
            env.set_var(&c.name, cls);
            Ok(())
        }
        Node::FunctionDecl(_) => Ok(()), // hoisted by exec_block
        Node::ExprStmt(e) => {
            eval(this, e, env.clone()).await?;
            Ok(())
        }
        Node::If {
            cond,
            then,
            else_,
        } => {
            if truthy(&eval(this, cond, env.clone()).await?) {
                exec(this, then, env.clone()).await
            } else if let Some(e) = else_ {
                exec(this, e, env.clone()).await
            } else {
                Ok(())
            }
        }
        Node::While { cond, body, label } => {
            while truthy(&eval(this, cond, env.clone()).await?) {
                match exec(this, body, env.clone()).await {
                    Ok(()) => {}
                    Err(e) => match loop_signal(e, label) {
                        LoopSignal::Break => break,
                        LoopSignal::Continue => continue,
                        LoopSignal::Propagate(e) => return Err(e),
                    },
                }
            }
            Ok(())
        }
        Node::DoWhile { body, cond, label } => {
            loop {
                match exec(this, body, env.clone()).await {
                    Ok(()) => {}
                    Err(e) => match loop_signal(e, label) {
                        LoopSignal::Break => break,
                        LoopSignal::Continue => {}
                        LoopSignal::Propagate(e) => return Err(e),
                    },
                }
                if !truthy(&eval(this, cond, env.clone()).await?) {
                    break;
                }
            }
            Ok(())
        }
        Node::Switch { expr, cases } => exec_switch(this, expr, cases, env.clone()).await,
        Node::For {
            init,
            cond,
            update,
            body,
            label,
        } => exec_for(this, init, cond, update, body, label, env.clone()).await,
        Node::ForIn {
            var_kind,
            name,
            iterable,
            body,
            label,
        } => exec_for_in(this, var_kind, name, iterable, body, label, env.clone()).await,
        Node::ForOf {
            var_kind,
            name,
            iterable,
            body,
            label,
        } => exec_for_of(this, var_kind, name, iterable, body, label, env.clone()).await,
        Node::Return(v) => {
            let value = match v {
                Some(e) => eval(this, e, env.clone()).await?,
                None => JsValue::Undefined,
            };
            Err(JsError::Return(value))
        }
        Node::Break(label) => Err(JsError::Break(label.clone())),
        Node::Continue(label) => Err(JsError::Continue(label.clone())),
        Node::Labelled { name, body } => match exec(this, body, env.clone()).await {
            // A loop swallows a `continue` aimed at its own name itself; all
            // that can still reach here is the `break` out of a labelled
            // block, which is where it stops.
            Err(JsError::Break(Some(l))) if l == *name => Ok(()),
            other => other,
        },
        Node::Throw(e) => Err(JsError::Thrown(eval(this, e, env.clone()).await?)),
        Node::TryCatch {
            try_block,
            catch_param,
            catch_block,
            finally_block,
        } => {
            exec_try(
                this,
                try_block,
                catch_param,
                catch_block,
                finally_block,
                env.clone(),
            )
            .await
        }
        _ => Err(JsError::js("Unknown statement.")),
    }
}

/// What a loop should do with a control-flow signal that reached it.
enum LoopSignal {
    Break,
    Continue,
    Propagate(JsError),
}

/// A bare `break`/`continue` belongs to the nearest loop. A named one belongs
/// to the loop that carries that name, and travels past every loop in between.
fn loop_signal(e: JsError, label: &Option<String>) -> LoopSignal {
    match e {
        JsError::Break(None) => LoopSignal::Break,
        JsError::Continue(None) => LoopSignal::Continue,
        JsError::Break(Some(ref l)) if Some(l) == label.as_ref() => LoopSignal::Break,
        JsError::Continue(Some(ref l)) if Some(l) == label.as_ref() => LoopSignal::Continue,
        other => LoopSignal::Propagate(other),
    }
}

async fn exec_for(
    this: &Rc<Interpreter>,
    init: &Option<Rc<Node>>,
    cond: &Option<Rc<Node>>,
    update: &Option<Rc<Node>>,
    body: &Rc<Node>,
    label: &Option<String>,
    env: Env,
) -> Result<(), JsError> {
    let child = Environment::new(Some(env.clone()));
    if let Some(init) = init {
        exec(this, init, child.clone()).await?;
    }
    loop {
        if let Some(c) = cond {
            if !truthy(&eval(this, c, child.clone()).await?) {
                break;
            }
        }
        match exec(this, body, child.clone()).await {
            Ok(()) => {}
            Err(e) => match loop_signal(e, label) {
                LoopSignal::Break => break,
                // `continue` still owes the header its update expression.
                LoopSignal::Continue => {}
                LoopSignal::Propagate(e) => return Err(e),
            },
        }
        if let Some(u) = update {
            eval(this, u, child.clone()).await?;
        }
    }
    Ok(())
}

async fn exec_switch(
    this: &Rc<Interpreter>,
    expr: &Rc<Node>,
    cases: &[(String, Option<Rc<Node>>, Vec<Rc<Node>>)],
    env: Env,
) -> Result<(), JsError> {
    let value = eval(this, expr, env.clone()).await?;
    let mut start: Option<usize> = None;
    let mut default: Option<usize> = None;
    for (i, (kind, test, _)) in cases.iter().enumerate() {
        if kind == "default" {
            default = Some(i);
            continue;
        }
        if let Some(test) = test {
            let tv = eval(this, test, env.clone()).await?;
            if strict_eq(&value, &tv) {
                start = Some(i);
                break;
            }
        }
    }
    let start = start.or(default);
    if let Some(start) = start {
        for (_, _, stmts) in &cases[start..] {
            for stmt in stmts {
                match exec(this, stmt, env.clone()).await {
                    Ok(()) => {}
                    Err(JsError::Break(None)) => return Ok(()),
                    Err(e) => return Err(e),
                }
            }
        }
    }
    Ok(())
}

fn bind_pattern(
    this: &Rc<Interpreter>,
    pattern: &PatternNode,
    value: &JsValue,
    env: Env,
    setter: fn(&Environment, &str, JsValue),
) -> StResult {
    let this = this.clone();
    let pattern = pattern.clone();
    let value = value.clone();
    Box::pin(async move {
        if pattern.kind == "array" {
            let items: Vec<JsValue> = match &value {
                JsValue::Array(a) => a.borrow().clone(),
                _ => vec![],
            };
            for (i, part) in pattern.parts.iter().enumerate() {
                let (target, default) = match part {
                    PatternPart::Array { target, default } => (target, default),
                    _ => return Err(JsError::js("Invalid array pattern")),
                };
                let mut item = items.get(i).cloned().unwrap_or(JsValue::Undefined);
                if matches!(item, JsValue::Undefined) {
                    if let Some(d) = default {
                        item = eval(&this, d, env.clone()).await?;
                    }
                }
                bind_target(&this, target, &item, env.clone(), setter).await?;
            }
            if let Some(rest) = &pattern.rest {
                let rest_val = JsValue::array(items[pattern.parts.len()..].to_vec());
                bind_target(&this, rest, &rest_val, env.clone(), setter).await?;
            }
            return Ok(());
        }
        let src: BTreeMap<String, JsValue> = match &value {
            JsValue::Object(map) => map.borrow().clone(),
            _ => {
                let mut m = BTreeMap::new();
                for part in &pattern.parts {
                    if let PatternPart::Object { key, .. } = part {
                        m.insert(key.clone(), js_get(&this, &value, key)?);
                    }
                }
                if let Some(rest) = &pattern.rest {
                    for key in own_keys(&this, &value) {
                        m.entry(key.clone())
                            .or_insert_with(|| js_get(&this, &value, &key).unwrap_or(JsValue::Undefined));
                    }
                }
                m
            }
        };
        for part in &pattern.parts {
            if let PatternPart::Object { key, target, default } = part {
                let mut item = src.get(key).cloned().unwrap_or(JsValue::Undefined);
                if matches!(item, JsValue::Undefined) {
                    if let Some(d) = default {
                        item = eval(&this, d, env.clone()).await?;
                    }
                }
                bind_target(&this, target, &item, env.clone(), setter).await?;
            }
        }
        if let Some(rest) = &pattern.rest {
            let rest_val = JsValue::Object(Rc::new(RefCell::new(src)));
            bind_target(&this, rest, &rest_val, env.clone(), setter).await?;
        }
        Ok(())
    })
}

fn bind_target(
    this: &Rc<Interpreter>,
    target: &DeclTarget,
    value: &JsValue,
    env: Env,
    setter: fn(&Environment, &str, JsValue),
) -> StResult {
    let this = this.clone();
    let target = target.clone();
    let value = value.clone();
    Box::pin(async move {
        match target {
            DeclTarget::Name(name) => {
                setter(&env, &name, value.clone());
                Ok(())
            }
            DeclTarget::Pattern(p) => bind_pattern(&this, &p, &value, env, setter).await,
        }
    })
}

fn bind_loop_var(env: &Env, var_kind: &Option<String>, name: &str, value: JsValue) {
    if let Some(kind) = var_kind {
        match kind.as_str() {
            "var" => env.set_var(name, value),
            "let" => env.set_let(name, value),
            _ => env.set_const(name, value),
        }
    } else {
        let _ = env.assign(name, value);
    }
}

async fn exec_for_in(
    this: &Rc<Interpreter>,
    var_kind: &Option<String>,
    name: &str,
    iterable: &Rc<Node>,
    body: &Rc<Node>,
    label: &Option<String>,
    env: Env,
) -> Result<(), JsError> {
    let obj = eval(this, iterable, env.clone()).await?;
    let keys: Vec<JsValue> = match &obj {
        JsValue::Object(map) => map.borrow().keys().map(|k| JsValue::str(k.clone())).collect(),
        JsValue::Instance(inst) => {
            let i = inst.borrow();
            let mut seen = std::collections::HashSet::new();
            let mut keys = Vec::new();
            for k in i.props.borrow().keys() {
                keys.push(JsValue::str(k.clone()));
                seen.insert(k.clone());
            }
            let mut p = Some(i.proto.clone());
            while let Some(pp) = p {
                for k in pp.borrow().keys() {
                    if k != "__proto__" && !seen.contains(k) {
                        keys.push(JsValue::str(k.clone()));
                        seen.insert(k.clone());
                    }
                }
                let next = pp.borrow().get("__proto__").cloned();
                p = match next {
                    Some(JsValue::Object(m)) => Some(m),
                    _ => None,
                };
            }
            keys
        }
        JsValue::Array(a) => (0..a.borrow().len())
            .map(|i| JsValue::Number(i as f64))
            .collect(),
        _ => vec![],
    };
    for key in keys {
        let child = Environment::new(Some(env.clone()));
        bind_loop_var(&child, var_kind, name, key);
        match exec(this, body, child.clone()).await {
            Ok(()) => {}
            Err(e) => match loop_signal(e, label) {
                LoopSignal::Break => break,
                LoopSignal::Continue => continue,
                LoopSignal::Propagate(e) => return Err(e),
            },
        }
    }
    Ok(())
}

async fn exec_for_of(
    this: &Rc<Interpreter>,
    var_kind: &Option<String>,
    name: &str,
    iterable: &Rc<Node>,
    body: &Rc<Node>,
    label: &Option<String>,
    env: Env,
) -> Result<(), JsError> {
    let obj = eval(this, iterable, env.clone()).await?;
    let items: Vec<JsValue> = match &obj {
        JsValue::Array(a) => a.borrow().clone(),
        JsValue::Str(s) => s.chars().map(|c| JsValue::str(c.to_string())).collect(),
        _ => vec![],
    };
    for item in items {
        let child = Environment::new(Some(env.clone()));
        bind_loop_var(&child, var_kind, name, item);
        match exec(this, body, child.clone()).await {
            Ok(()) => {}
            Err(e) => match loop_signal(e, label) {
                LoopSignal::Break => break,
                LoopSignal::Continue => continue,
                LoopSignal::Propagate(e) => return Err(e),
            },
        }
    }
    Ok(())
}

async fn exec_try(
    this: &Rc<Interpreter>,
    try_block: &Rc<Node>,
    catch_param: &Option<String>,
    catch_block: &Option<Rc<Node>>,
    finally_block: &Option<Rc<Node>>,
    env: Env,
) -> Result<(), JsError> {
    enum Caught {
        Throw(JsValue),
        Error(String),
    }
    let error: Option<Caught> = match exec(this, try_block, Environment::new(Some(env.clone()))).await {
        Ok(()) => None,
        Err(JsError::Thrown(t)) => Some(Caught::Throw(t)),
        Err(JsError::Js(m)) => Some(Caught::Error(m)),
        Err(e) => {
            if let Some(fb) = finally_block {
                exec(this, fb, env.clone()).await?;
            }
            return Err(e);
        }
    };
    if error.is_some() && catch_block.is_some() {
        let child = Environment::new(Some(env.clone()));
        if let Some(param) = catch_param {
            let v = match error.as_ref().unwrap() {
                Caught::Throw(t) => t.clone(),
                Caught::Error(m) => JsValue::str(m.clone()),
            };
            child.set_let(param, v);
        }
        exec(this, catch_block.as_ref().unwrap(), child.clone()).await?;
    } else if let Some(err) = error {
        if let Some(fb) = finally_block {
            exec(this, fb, env.clone()).await?;
        }
        match err {
            Caught::Throw(t) => return Err(JsError::Thrown(t)),
            Caught::Error(m) => return Err(JsError::Js(m)),
        }
    }
    if let Some(fb) = finally_block {
        exec(this, fb, env.clone()).await?;
    }
    Ok(())
}
