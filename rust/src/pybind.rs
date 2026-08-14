//! PyO3 bindings: `Interpreter`, `JSException`, `UNDEFINED`, and the
//! `JSValue`/globals proxies that bridge the Rust interpreter to Python.

use crate::interp::*;
use crate::value::*;
use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyList;
use std::cell::RefCell;
use std::rc::Rc;

create_exception!(feetbrowser_engine, JSException, PyException);

pub fn to_py_err(this: &Rc<Interpreter>, e: &JsError) -> PyErr {
    let msg = js_error_message(this, e);
    Python::attach(|py| {
        let cls = this.js_exception.bind(py);
        match cls.call1((msg.clone(),)) {
            Ok(exc) => PyErr::from_value(exc),
            Err(_) => PyErr::new::<PyException, _>(msg),
        }
    })
}

// -- the UNDEFINED sentinel ------------------------------------------------

#[pyclass(module = "feetbrowser_engine", name = "_Undefined")]
pub struct Undefined {}

#[pymethods]
impl Undefined {
    fn __str__(&self) -> &'static str {
        "undefined"
    }

    fn __repr__(&self) -> &'static str {
        "undefined"
    }
}

// -- the Interpreter -------------------------------------------------------

#[pyclass(module = "feetbrowser_engine", name = "Interpreter", unsendable)]
pub struct PyInterpreter {
    pub inner: Rc<Interpreter>,
    pub logs_list: RefCell<Option<Py<PyList>>>,
}

#[pymethods]
impl PyInterpreter {
    #[new]
    fn new(py: Python<'_>) -> PyResult<PyInterpreter> {
        let inner = Interpreter::new(py)?;
        Ok(PyInterpreter {
            inner,
            logs_list: RefCell::new(None),
        })
    }

    fn run(&self, source: &str) -> PyResult<()> {
        self.inner.run(source).map_err(|e| to_py_err(&self.inner, &e))
    }

    #[pyo3(signature = (*args))]
    fn call(&self, py: Python<'_>, args: Vec<Py<PyAny>>) -> PyResult<Py<PyAny>> {
        let fn_ = match args.first() {
            Some(f) => py_to_js(&self.inner, py, f.bind(py)),
            None => {
                return Err(PyErr::new::<pyo3::exceptions::PyTypeError, _>(
                    "call() missing fn argument",
                ))
            }
        };
        let rest: Vec<JsValue> = args[1..]
            .iter()
            .map(|a| py_to_js(&self.inner, py, a.bind(py)))
            .collect();
        let r = self
            .inner
            .call(&fn_, rest)
            .map_err(|e| to_py_err(&self.inner, &e))?;
        js_to_py(&self.inner, py, &r)
    }

    fn create_promise(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let p = JsPromise::new();
        js_to_py(&self.inner, py, &JsValue::Promise(p))
    }

    fn drain(&self) {
        self.inner.drain();
    }

    fn advance(&self, ms: f64) {
        self.inner.advance(ms);
    }

    #[getter]
    fn logs(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let cached = self.logs_list.borrow().clone();
        let list = match cached {
            Some(l) => l,
            None => {
                let l = PyList::empty(py).unbind();
                *self.logs_list.borrow_mut() = Some(l.clone_ref(py));
                l
            }
        };
        let rust_logs = std::mem::take(&mut *self.inner.logs.borrow_mut());
        for s in rust_logs {
            list.bind(py).append(s)?;
        }
        Ok(list)
    }

    #[getter]
    fn globals(&self, py: Python<'_>) -> PyResult<Py<JsGlobals>> {
        Py::new(py, JsGlobals {
            inner: self.inner.clone(),
        })
    }

    fn __repr__(&self) -> String {
        "<Interpreter>".to_string()
    }
}

// -- the globals mapping proxy ---------------------------------------------

#[pyclass(module = "feetbrowser_engine", name = "JSGlobals", unsendable)]
pub struct JsGlobals {
    pub inner: Rc<Interpreter>,
}

#[pymethods]
impl JsGlobals {
    fn __getitem__(&self, py: Python<'_>, key: &str) -> PyResult<Py<PyAny>> {
        let v = self
            .inner
            .globals
            .borrow()
            .get(key)
            .cloned()
            .unwrap_or(JsValue::Undefined);
        js_to_py(&self.inner, py, &v)
    }

    fn __setitem__(&self, py: Python<'_>, key: &str, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let v = py_to_js(&self.inner, py, value);
        self.inner
            .globals
            .borrow_mut()
            .insert(key.to_string(), v);
        Ok(())
    }

    fn __delitem__(&self, key: &str) {
        self.inner.globals.borrow_mut().remove(key);
    }

    fn __contains__(&self, key: &str) -> bool {
        self.inner.globals.borrow().contains_key(key)
    }

    fn __len__(&self) -> usize {
        self.inner.globals.borrow().len()
    }

    fn keys(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let keys: Vec<String> = self.inner.globals.borrow().keys().cloned().collect();
        let list = PyList::empty(py);
        for k in keys {
            list.append(k)?;
        }
        Ok(list.unbind())
    }

    fn get(&self, py: Python<'_>, key: &str) -> PyResult<Py<PyAny>> {
        self.__getitem__(py, key)
    }

    fn __iter__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let keys: Vec<String> = self.inner.globals.borrow().keys().cloned().collect();
        let list = PyList::empty(py);
        for k in keys {
            list.append(k)?;
        }
        Ok(list.unbind().into_any())
    }

    fn __repr__(&self) -> String {
        format!("<JSGlobals {} entries>", self.inner.globals.borrow().len())
    }
}

// -- a JS value handed across the boundary ---------------------------------

// `from_py_object` is asked for rather than inherited: pyo3 derives it for a
// Clone pyclass today but is making that opt-in, and py_to_js does extract
// one of these -- it is how a JS value that went out to Python and came back
// is recognised as itself instead of being rebuilt from its repr.
#[pyclass(
    module = "feetbrowser_engine",
    name = "JSValue",
    unsendable,
    from_py_object
)]
#[derive(Clone)]
pub struct PyJsValue {
    pub inner: Rc<Interpreter>,
    pub value: JsValue,
}

impl PyJsValue {
    pub fn new(this: &Rc<Interpreter>, value: JsValue) -> PyResult<PyJsValue> {
        Ok(PyJsValue {
            inner: this.clone(),
            value,
        })
    }

    pub fn take(&self) -> JsValue {
        self.value.clone()
    }
}

#[pymethods]
impl PyJsValue {
    fn js_get(&self, py: Python<'_>, name: &str) -> PyResult<Py<PyAny>> {
        let v = js_get(&self.inner, &self.value, name).map_err(|e| to_py_err(&self.inner, &e))?;
        js_to_py(&self.inner, py, &v)
    }

    fn js_set(&self, py: Python<'_>, name: &str, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let v = py_to_js(&self.inner, py, value);
        js_set(&self.inner, &self.value, name, &v).map_err(|e| to_py_err(&self.inner, &e))
    }

    #[pyo3(signature = (*args))]
    fn js_call(&self, py: Python<'_>, args: Vec<Py<PyAny>>) -> PyResult<Py<PyAny>> {
        let js_args: Vec<JsValue> = args
            .iter()
            .map(|a| py_to_js(&self.inner, py, a.bind(py)))
            .collect();
        let r = drive_sync(
            &self.inner,
            call_value(&self.inner, &self.value, js_args, JsValue::Undefined),
        )
        .map_err(|e| to_py_err(&self.inner, &e))?;
        js_to_py(&self.inner, py, &r)
    }

    #[pyo3(signature = (*args))]
    fn js_new(&self, py: Python<'_>, args: Vec<Py<PyAny>>) -> PyResult<Py<PyAny>> {
        let js_args: Vec<JsValue> = args
            .iter()
            .map(|a| py_to_js(&self.inner, py, a.bind(py)))
            .collect();
        let r = drive_sync(&self.inner, construct(&self.inner, &self.value, js_args))
            .map_err(|e| to_py_err(&self.inner, &e))?;
        js_to_py(&self.inner, py, &r)
    }

    fn js_repr(&self) -> String {
        self.inner.repr(&self.value)
    }

    /// Return the raw Python object a Host value wraps (JSElement,
    /// JSNodeList, ...) so browser-provided host functions can get at it
    /// directly; non-Host values come back unchanged.
    fn js_unwrap(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if let JsValue::Host(h) = &self.value {
            Ok(h.clone_ref(py))
        } else {
            Ok(Py::new(py, self.clone())?.into_any())
        }
    }

    fn resolve(&self, py: Python<'_>, value: &Bound<'_, PyAny>) {
        if let JsValue::Promise(p) = &self.value {
            let v = py_to_js(&self.inner, py, value);
            promise_resolve(&self.inner, p, v);
        }
    }

    fn reject(&self, py: Python<'_>, reason: &Bound<'_, PyAny>) {
        if let JsValue::Promise(p) = &self.value {
            let v = py_to_js(&self.inner, py, reason);
            promise_reject(&self.inner, p, v);
        }
    }
}