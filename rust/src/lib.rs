mod ast;
mod css;
mod dom;
mod font;
mod image;
mod interp;
mod layout;
// Hands `footnote`'s arena tree to Python as `htmlparser.Element`/`Text`
// objects, which is what makes the parser crate the browser's real parser.
mod materialize;
mod parser;
mod pybind;
mod pyutil;
mod raster;
mod regexp;
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

    // -- html parsing --
    m.add_function(wrap_pyfunction!(materialize::parse_html, m)?)?;
    m.add_function(wrap_pyfunction!(materialize::parse_fragment_html, m)?)?;

    // -- image codecs --
    m.add("ImageError", py.get_type::<image::ImageError>())?;
    m.add("MAX_PIXELS", image::MAX_PIXELS)?;
    m.add("MAX_INFLATED", image::MAX_INFLATED)?;
    m.add_function(wrap_pyfunction!(image::py_decode, m)?)?;
    m.add_function(wrap_pyfunction!(image::py_decode_png, m)?)?;
    m.add_function(wrap_pyfunction!(image::py_decode_gif, m)?)?;
    m.add_function(wrap_pyfunction!(image::py_decode_gif_frames, m)?)?;
    m.add_function(wrap_pyfunction!(image::py_decode_jpeg, m)?)?;
    m.add_function(wrap_pyfunction!(image::py_decode_pnm, m)?)?;
    m.add_function(wrap_pyfunction!(image::py_sniff, m)?)?;
    m.add_function(wrap_pyfunction!(image::py_resize, m)?)?;

    // -- rasteriser --
    m.add_class::<raster::Surface>()?;
    m.add_function(wrap_pyfunction!(raster::rasterize, m)?)?;
    m.add_function(wrap_pyfunction!(raster::glyph_bitmap, m)?)?;
    m.add_function(wrap_pyfunction!(raster::draw_text, m)?)?;
    m.add_function(wrap_pyfunction!(raster::measure_text, m)?)?;

    // -- css cascade --
    m.add_function(wrap_pyfunction!(css::style, m)?)?;

    // -- fonts --
    m.add_class::<font::Font>()?;
    m.add("FontError", py.get_type::<font::FontError>())?;
    m.add_function(wrap_pyfunction!(font::flatten, m)?)?;
    Ok(())
}
