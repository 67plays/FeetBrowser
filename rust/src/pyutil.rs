//! Small conversions shared by the renderer bindings.

use pyo3::buffer::PyBuffer;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyByteArray, PyBytes, PyFloat};
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

/// A coordinate, converted the way Python's `int()` converted it.
///
/// Drawing coordinates arrive as whatever layout computed -- often a float,
/// sometimes an int -- and the renderer used to write `int(x)` at the top of
/// every method. That truncates towards zero rather than flooring, so -2.7
/// becomes -2, and half the browser's box edges land on the pixel they do
/// because of it. A value too large for i64 saturates: it is far outside any
/// surface, and clipping will discard it either way.
pub fn to_int(obj: &Bound<'_, PyAny>) -> PyResult<i64> {
    if let Ok(v) = obj.extract::<i64>() {
        return Ok(v);
    }
    if let Ok(f) = obj.cast::<PyFloat>() {
        let v = f.value();
        if v.is_nan() {
            return Err(PyValueError::new_err(
                "cannot convert float NaN to integer",
            ));
        }
        return Ok(v.trunc().clamp(i64::MIN as f64, i64::MAX as f64) as i64);
    }
    // Anything else that is a number at all (a bool, a Decimal, an int too
    // big for i64) goes the long way round through Python itself.
    match obj.extract::<f64>() {
        Ok(v) => Ok(v.trunc().clamp(i64::MIN as f64, i64::MAX as f64) as i64),
        Err(_) => Err(PyTypeError::new_err(
            "expected a number for a drawing coordinate",
        )),
    }
}

/// An (r, g, b) colour, rejecting out-of-range channels the way `bytes()` did.
pub fn rgb(obj: &Bound<'_, PyAny>) -> PyResult<[u8; 3]> {
    let (r, g, b): (i64, i64, i64) = obj.extract()?;
    let mut out = [0u8; 3];
    for (slot, v) in out.iter_mut().zip([r, g, b]) {
        if !(0..=255).contains(&v) {
            return Err(PyValueError::new_err("bytes must be in range(0, 256)"));
        }
        *slot = v as u8;
    }
    Ok(out)
}
