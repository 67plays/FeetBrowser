mod ast;
mod dom;
mod interp;
mod parser;
mod pybind;
mod stdlib;
mod token;
mod value;

use pyo3::prelude::*;

#[pymodule]
fn feetbrowser_engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add_class::<pybind::PyInterpreter>()?;
    m.add_class::<pybind::PyJsValue>()?;
    m.add_class::<pybind::JsGlobals>()?;
    m.add("JSException", py.get_type::<pybind::JSException>())?;
    let undef = Py::new(py, pybind::Undefined {})?;
    m.add("UNDEFINED", undef)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(dom::dom_get, m)?)?;
    m.add_function(wrap_pyfunction!(dom::dom_set, m)?)?;
    m.add_function(wrap_pyfunction!(dom::dom_call, m)?)?;
    Ok(())
}
