//! Small conversions shared by the renderer bindings.

use pyo3::buffer::PyBuffer;
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyByteArray, PyBytes};
use std::borrow::Cow;

/// Read a Python bytes-like argument without copying when we can help it.
///
/// The renderer is handed `bytes` almost everywhere (glyph coverage, decoded
/// pixels, network payloads), which borrows straight through. `bytearray` and
/// anything else exporting a buffer still work, at the cost of one copy --
/// callers that care about the copy pass `bytes`.
pub fn bytes_arg<'a>(obj: &'a Bound<'_, PyAny>) -> PyResult<Cow<'a, [u8]>> {
    if let Ok(b) = obj.cast::<PyBytes>() {
        return Ok(Cow::Borrowed(b.as_bytes()));
    }
    if let Ok(b) = obj.cast::<PyByteArray>() {
        return Ok(Cow::Owned(b.to_vec()));
    }
    match PyBuffer::<u8>::get(obj) {
        Ok(buf) => Ok(Cow::Owned(buf.to_vec(obj.py())?)),
        Err(_) => Err(PyTypeError::new_err(
            "expected a bytes-like object",
        )),
    }
}
