//! TrueType font parsing: metrics, character mapping, and glyph outlines.
//!
//! This is the font half of what used to be Tk. Everything the layout engine
//! needs comes out of tables in the file: ascent and descent from `hhea` (or
//! `OS/2` when `hhea` is zeroed), advances from `hmtx`, the character to
//! glyph mapping from `cmap`, and outlines from `glyf` and `loca`.
//!
//! Font files are not hostile in the way a web page is -- they come off the
//! local disk -- but they are full of offsets that point at other offsets,
//! and a truncated or simply strange one is common enough that the Python
//! version was written to give up on a glyph rather than raise. That
//! behaviour is kept exactly: a table that is too short, a `loca` entry
//! pointing past `glyf`, a composite nested too deep, all return an empty
//! outline, and only a file that is not a font at all raises FontError. In
//! Rust it also has to be true that none of those cases indexes past the end
//! of a buffer, so every read goes through a checked helper.

use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::HashMap;

create_exception!(feetbrowser_engine, FontError, PyException);

// Glyph outline flags (spec: "Simple Glyph Description").
const ON_CURVE: u8 = 0x01;
const X_SHORT: u8 = 0x02;
const Y_SHORT: u8 = 0x04;
const REPEAT: u8 = 0x08;
const X_SAME: u8 = 0x10;
const Y_SAME: u8 = 0x20;

// Composite glyph flags (spec: "Composite Glyph Description").
const ARGS_ARE_WORDS: u16 = 0x0001;
const ARGS_ARE_XY: u16 = 0x0002;
const HAVE_SCALE: u16 = 0x0008;
const MORE_COMPONENTS: u16 = 0x0020;
const HAVE_XY_SCALE: u16 = 0x0040;
const HAVE_2X2: u16 = 0x0080;

/// A point of an outline: font units, plus whether it is on the curve.
pub type Point = (i64, i64, bool);
pub type Contours = Vec<Vec<Point>>;

fn err(msg: impl Into<String>) -> PyErr {
    FontError::new_err(msg.into())
}

fn be16(b: &[u8], at: usize) -> Option<u16> {
    b.get(at..at + 2).map(|s| u16::from_be_bytes([s[0], s[1]]))
}

fn be16s(b: &[u8], at: usize) -> Option<i16> {
    be16(b, at).map(|v| v as i16)
}

fn be32(b: &[u8], at: usize) -> Option<u32> {
    b.get(at..at + 4)
        .map(|s| u32::from_be_bytes([s[0], s[1], s[2], s[3]]))
}

/// One parsed font face.
///
/// Metric accessors return font units; callers scale by `self.scale(size)`.
/// Glyph outlines are cached because composites can require several lookups,
/// and so are rasterised glyph bitmaps -- see `raster::glyph_bitmap`, which
/// keeps them here so that the cache lives and dies with the face.
#[pyclass(module = "feetbrowser_engine")]
pub struct Font {
    data: Vec<u8>,
    tables: HashMap<String, (usize, usize)>,
    #[pyo3(get)]
    pub cff: bool,
    #[pyo3(get)]
    pub units_per_em: i64,
    #[pyo3(get)]
    pub index_to_loc: i64,
    #[pyo3(get)]
    pub ascent: i64,
    #[pyo3(get)]
    pub descent: i64,
    #[pyo3(get)]
    pub line_gap: i64,
    #[pyo3(get)]
    pub num_h_metrics: i64,
    #[pyo3(get)]
    pub num_glyphs: i64,
    advances: Vec<u16>,
    cmap: Option<HashMap<u32, u32>>,
    glyphs: HashMap<u32, Contours>,
    /// Rasterised glyphs, keyed by (size bits, glyph id). The tuple objects
    /// are handed back as they are, so asking twice returns the same object.
    pub bitmaps: HashMap<(u64, u32), Py<pyo3::types::PyTuple>>,
}

#[pymethods]
impl Font {
    #[new]
    #[pyo3(signature = (data, index = 0))]
    fn new(data: &Bound<'_, PyAny>, index: usize) -> PyResult<Self> {
        let bytes = crate::pyutil::bytes_arg(data)?.into_owned();
        let mut font = Font {
            data: bytes,
            tables: HashMap::new(),
            cff: false,
            units_per_em: 1000,
            index_to_loc: 0,
            ascent: 0,
            descent: 0,
            line_gap: 0,
            num_h_metrics: 0,
            num_glyphs: 0,
            advances: Vec::new(),
            cmap: None,
            glyphs: HashMap::new(),
            bitmaps: HashMap::new(),
        };
        font.read_directory(index)?;
        font.read_head()?;
        font.read_hhea()?;
        font.read_maxp();
        font.read_hmtx();
        Ok(font)
    }

    // -- public metrics --------------------------------------------------

    /// Multiplier converting font units to pixels at `size` px.
    fn scale(&self, size: f64) -> f64 {
        self.scale_of(size)
    }

    /// Horizontal advance for a glyph, in font units.
    fn advance(&self, gid: i64) -> i64 {
        self.advance_of(gid)
    }

    /// Default line height in font units.
    fn linespace(&self) -> i64 {
        self.ascent - self.descent + self.line_gap
    }

    // -- character mapping -----------------------------------------------

    /// Glyph index for a character, or 0 (.notdef) when unmapped.
    fn glyph_id(&mut self, ch: &Bound<'_, PyAny>) -> PyResult<u32> {
        let code = crate::pyutil::one_char(ch)?;
        Ok(self.glyph_of(code))
    }

    fn has_char(&mut self, ch: &Bound<'_, PyAny>) -> PyResult<bool> {
        let code = crate::pyutil::one_char(ch)?;
        self.ensure_cmap();
        Ok(self.cmap.as_ref().is_some_and(|m| m.contains_key(&code)))
    }

    // -- outlines --------------------------------------------------------

    /// Outline as a list of contours of `(x, y, on_curve)` font-unit points.
    #[pyo3(signature = (gid, depth = 0))]
    fn glyph_contours<'py>(
        &mut self,
        py: Python<'py>,
        gid: u32,
        depth: u32,
    ) -> PyResult<Bound<'py, PyList>> {
        let contours = self.contours(gid, depth);
        let out = PyList::empty(py);
        for c in contours {
            out.append(PyList::new(py, c.iter().map(|p| (p.0, p.1, p.2)))?)?;
        }
        Ok(out)
    }

    // -- naming ----------------------------------------------------------

    /// `{nameID: text}` from the name table, for family identification.
    fn names<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        let t = match self.table("name") {
            Some(t) if t.len() >= 6 => t,
            _ => return Ok(out),
        };
        let count = be16(t, 2).unwrap_or(0) as usize;
        let str_off = be16(t, 4).unwrap_or(0) as usize;
        for i in 0..count {
            let rec = 6 + i * 12;
            if rec + 12 > t.len() {
                break;
            }
            let plat = be16(t, rec).unwrap_or(0);
            let nid = be16(t, rec + 6).unwrap_or(0);
            let length = be16(t, rec + 8).unwrap_or(0) as usize;
            let off = be16(t, rec + 10).unwrap_or(0) as usize;
            // Clamped, not checked: a record whose length runs past the end
            // of the name table still names the font, and the Python this
            // replaces read it that way because a slice clamps.
            let start = str_off.saturating_add(off).min(t.len());
            let end = start.saturating_add(length).min(t.len());
            let s = match t.get(start..end) {
                Some(s) if !s.is_empty() => s,
                _ => continue,
            };
            // Platform 3 (Windows) and platform 0 (Unicode) are UTF-16BE.
            let text = if plat == 0 || plat == 3 {
                match decode_utf16be(s) {
                    Some(t) => t,
                    None => continue, // Python raised UnicodeDecodeError here
                }
            } else {
                s.iter().map(|&b| b as char).collect::<String>()
            };
            let text = text.trim_matches('\0').trim_matches(py_space);
            if text.is_empty() {
                continue;
            }
            if plat == 3 || !out.contains(nid)? {
                out.set_item(nid, text)?;
            }
        }
        Ok(out)
    }

    #[getter]
    fn is_bold(&self) -> bool {
        let os2 = match self.table("OS/2") {
            Some(t) => t,
            None => return false,
        };
        if os2.len() >= 6 && be16(os2, 4).unwrap_or(0) >= 600 {
            return true;
        }
        if os2.len() >= 64 {
            return be16(os2, 62).unwrap_or(0) & 0x20 != 0;
        }
        false
    }

    #[getter]
    fn is_italic(&self) -> bool {
        if let Some(os2) = self.table("OS/2") {
            if os2.len() >= 64 {
                return be16(os2, 62).unwrap_or(0) & 0x01 != 0;
            }
        }
        if let Some(head) = self.table("head") {
            if head.len() >= 46 {
                return be16(head, 44).unwrap_or(0) & 0x02 != 0;
            }
        }
        false
    }
}

impl Font {
    // -- container -------------------------------------------------------

    fn read_directory(&mut self, index: usize) -> PyResult<()> {
        if self.data.len() < 12 {
            return Err(err("file too short to be a font"));
        }
        let mut offset = 0usize;
        let mut tag: [u8; 4] = [self.data[0], self.data[1], self.data[2], self.data[3]];
        if &tag == b"ttcf" {
            // A collection: the real font directories start further in.
            let count = be32(&self.data, 8).unwrap_or(0) as usize;
            if index >= count {
                return Err(err(format!(
                    "collection has {} fonts, wanted {}",
                    count, index
                )));
            }
            offset = be32(&self.data, 12 + index * 4).unwrap_or(0) as usize;
            match self.data.get(offset..offset + 4) {
                Some(t) => tag = [t[0], t[1], t[2], t[3]],
                None => return Err(err("collection entry points past the file")),
            }
        }
        if !matches!(&tag, b"\x00\x01\x00\x00" | b"true" | b"ttcf" | b"OTTO") {
            return Err(err(format!("unrecognised sfnt tag {:?}", tag)));
        }
        // CFF outlines: metrics are readable but we cannot rasterise them.
        self.cff = &tag == b"OTTO";
        let num = be16(&self.data, offset + 4).unwrap_or(0) as usize;
        for i in 0..num {
            let rec = offset + 12 + i * 16;
            if rec + 16 > self.data.len() {
                break;
            }
            let name: String = self.data[rec..rec + 4].iter().map(|&b| b as char).collect();
            let off = be32(&self.data, rec + 8).unwrap_or(0) as usize;
            let length = be32(&self.data, rec + 12).unwrap_or(0) as usize;
            self.tables.insert(name, (off, length));
        }
        Ok(())
    }

    /// A table's bytes, clamped to the file the way Python's slicing was.
    /// An entry pointing past the end reads as absent rather than as an
    /// error, which is how the parser survives a truncated font.
    fn table(&self, name: &str) -> Option<&[u8]> {
        let &(off, length) = self.tables.get(name)?;
        let start = off.min(self.data.len());
        let end = off.saturating_add(length).min(self.data.len());
        let slice = &self.data[start..end];
        if slice.is_empty() {
            None
        } else {
            Some(slice)
        }
    }

    // -- metric tables ---------------------------------------------------

    fn read_head(&mut self) -> PyResult<()> {
        let (upem, index_to_loc) = {
            let head = self
                .table("head")
                .filter(|t| t.len() >= 54)
                .ok_or_else(|| err("missing or short head table"))?;
            (be16(head, 18).unwrap_or(0), be16s(head, 50).unwrap_or(0) as i64)
        };
        self.units_per_em = if upem == 0 { 1000 } else { upem as i64 };
        self.index_to_loc = index_to_loc;
        Ok(())
    }

    fn read_hhea(&mut self) -> PyResult<()> {
        let (ascent, descent, line_gap, num_h) = {
            let hhea = self.table("hhea").filter(|t| t.len() >= 36);
            let hhea = hhea.ok_or_else(|| err("missing or short hhea table"))?;
            (
                be16s(hhea, 4).unwrap_or(0) as i64,
                be16s(hhea, 6).unwrap_or(0) as i64,
                be16s(hhea, 8).unwrap_or(0) as i64,
                be16(hhea, 34).unwrap_or(0) as i64,
            )
        };
        self.ascent = ascent;
        self.descent = descent;
        self.line_gap = line_gap;
        self.num_h_metrics = num_h;
        // Some fonts ship a zeroed hhea and put the real numbers in OS/2.
        if self.ascent == 0 && self.descent == 0 {
            let typo = self
                .table("OS/2")
                .filter(|t| t.len() >= 72)
                .map(|os2| {
                    (
                        be16s(os2, 68).unwrap_or(0) as i64,
                        be16s(os2, 70).unwrap_or(0) as i64,
                    )
                });
            if let Some((ascent, descent)) = typo {
                self.ascent = ascent;
                self.descent = descent;
            }
        }
        self.descent = -self.descent.abs();
        Ok(())
    }

    fn read_maxp(&mut self) {
        self.num_glyphs = match self.table("maxp") {
            Some(maxp) => be16(maxp, 4).unwrap_or(0) as i64,
            None => 0,
        };
    }

    fn read_hmtx(&mut self) {
        let hmtx = match self.table("hmtx") {
            Some(t) => t,
            None => return,
        };
        let n = (self.num_h_metrics as usize).min(hmtx.len() / 4);
        let mut advances = Vec::with_capacity(n);
        for i in 0..n {
            advances.push(be16(hmtx, i * 4).unwrap_or(0));
        }
        self.advances = advances;
    }

    pub fn scale_of(&self, size: f64) -> f64 {
        size / self.units_per_em as f64
    }

    pub fn advance_of(&self, gid: i64) -> i64 {
        if self.advances.is_empty() {
            return self.units_per_em / 2;
        }
        if gid >= 0 && (gid as usize) < self.advances.len() {
            return self.advances[gid as usize] as i64;
        }
        // A glyph past the end of hmtx has the last advance: that is how the
        // format spells a monospaced tail.
        self.advances[self.advances.len() - 1] as i64
    }

    pub fn glyph_of(&mut self, code: u32) -> u32 {
        self.ensure_cmap();
        self.cmap
            .as_ref()
            .and_then(|m| m.get(&code))
            .copied()
            .unwrap_or(0)
    }

    // -- character mapping -----------------------------------------------

    fn ensure_cmap(&mut self) {
        if self.cmap.is_some() {
            return;
        }
        self.cmap = Some(self.build_cmap());
    }

    /// Pick the best available subtable and decode it into a map.
    fn build_cmap(&self) -> HashMap<u32, u32> {
        let mut map = HashMap::new();
        let t = match self.table("cmap").filter(|t| t.len() >= 4) {
            Some(t) => t,
            None => return map,
        };
        let count = be16(t, 2).unwrap_or(0) as usize;
        let mut best: Option<usize> = None;
        let mut best_score = -1i32;
        for i in 0..count {
            let rec = 4 + i * 8;
            if rec + 8 > t.len() {
                break;
            }
            let plat = be16(t, rec).unwrap_or(0);
            let enc = be16(t, rec + 2).unwrap_or(0);
            let off = be32(t, rec + 4).unwrap_or(0) as usize;
            // Prefer full Unicode over BMP-only, and Windows over Mac.
            let score = match (plat, enc) {
                (3, 10) | (0, 4) | (0, 6) => 5,
                (3, 1) | (0, 3) => 4,
                (0, 2) | (0, 1) => 3,
                (3, 0) => 2,
                (1, 0) => 1,
                _ => 0,
            };
            if score > best_score {
                best = Some(off);
                best_score = score;
            }
        }
        let off = match best {
            Some(o) if o + 4 <= t.len() => o,
            _ => return map,
        };
        match be16(t, off).unwrap_or(0) {
            0 => read_cmap0(t, off, &mut map),
            4 => read_cmap4(t, off, &mut map),
            6 => read_cmap6(t, off, &mut map),
            12 => read_cmap12(t, off, &mut map),
            _ => {}
        }
        map
    }

    // -- outlines --------------------------------------------------------

    /// Byte range of a glyph inside glyf, or None when it is blank.
    fn loca(&self, gid: u32) -> Option<(usize, usize)> {
        let loca = self.table("loca")?;
        let (start, end) = if self.index_to_loc != 0 {
            let pos = (gid as usize).checked_mul(4)?;
            if pos + 8 > loca.len() {
                return None;
            }
            (be32(loca, pos)? as usize, be32(loca, pos + 4)? as usize)
        } else {
            let pos = (gid as usize).checked_mul(2)?;
            if pos + 4 > loca.len() {
                return None;
            }
            (
                be16(loca, pos)? as usize * 2,
                be16(loca, pos + 2)? as usize * 2,
            )
        };
        if end <= start {
            None
        } else {
            Some((start, end))
        }
    }

    pub fn contours(&mut self, gid: u32, depth: u32) -> Contours {
        if let Some(c) = self.glyphs.get(&gid) {
            return c.clone();
        }
        let contours = self.parse_glyph(gid, depth);
        // A composite nested deeper than the recursion limit comes back
        // empty. Caching that under the glyph id alone would blank the glyph
        // for good, including for the top-level request that draws it.
        if !contours.is_empty() || depth == 0 {
            self.glyphs.insert(gid, contours.clone());
        }
        contours
    }

    fn parse_glyph(&mut self, gid: u32, depth: u32) -> Contours {
        if self.cff {
            return Vec::new(); // CFF outlines are a different format entirely
        }
        let span = match self.loca(gid) {
            Some(s) => s,
            None => return Vec::new(),
        };
        // Copied out rather than borrowed: parsing a composite calls back
        // into the font for its components, which needs the font again.
        let g: Vec<u8> = match self.table("glyf") {
            Some(glyf) => {
                let (start, end) = span;
                if end > glyf.len() || end - start < 10 {
                    return Vec::new();
                }
                glyf[start..end].to_vec()
            }
            None => return Vec::new(),
        };
        let n_contours = be16s(&g, 0).unwrap_or(0);
        if n_contours < 0 {
            self.parse_composite(&g, depth)
        } else {
            parse_simple(&g, n_contours as usize)
        }
    }

    fn parse_composite(&mut self, g: &[u8], depth: u32) -> Contours {
        if depth > 5 {
            return Vec::new(); // cyclic or absurdly nested composite
        }
        let mut contours: Contours = Vec::new();
        let mut pos = 10usize;
        while pos + 4 <= g.len() {
            let flags = be16(g, pos).unwrap_or(0);
            let sub_gid = be16(g, pos + 2).unwrap_or(0) as u32;
            pos += 4;
            let (a1, a2);
            if flags & ARGS_ARE_WORDS != 0 {
                if pos + 4 > g.len() {
                    break;
                }
                a1 = be16s(g, pos).unwrap_or(0) as i64;
                a2 = be16s(g, pos + 2).unwrap_or(0) as i64;
                pos += 4;
            } else {
                if pos + 2 > g.len() {
                    break;
                }
                a1 = g[pos] as i8 as i64;
                a2 = g[pos + 1] as i8 as i64;
                pos += 2;
            }
            let (mut xx, mut yy) = (1.0f64, 1.0f64);
            let (mut xy, mut yx) = (0.0f64, 0.0f64);
            if flags & HAVE_SCALE != 0 {
                xx = f2dot14(g, pos);
                yy = xx;
                pos += 2;
            } else if flags & HAVE_XY_SCALE != 0 {
                xx = f2dot14(g, pos);
                yy = f2dot14(g, pos + 2);
                pos += 4;
            } else if flags & HAVE_2X2 != 0 {
                xx = f2dot14(g, pos);
                xy = f2dot14(g, pos + 2);
                yx = f2dot14(g, pos + 4);
                yy = f2dot14(g, pos + 6);
                pos += 8;
            }
            let (dx, dy) = if flags & ARGS_ARE_XY != 0 {
                (a1 as f64, a2 as f64)
            } else {
                (0.0, 0.0)
            };
            for c in self.contours(sub_gid, depth + 1) {
                contours.push(
                    c.iter()
                        .map(|&(px, py, on)| {
                            let (px, py) = (px as f64, py as f64);
                            (
                                trunc(px * xx + py * yx + dx),
                                trunc(px * xy + py * yy + dy),
                                on,
                            )
                        })
                        .collect(),
                );
            }
            if flags & MORE_COMPONENTS == 0 {
                break;
            }
        }
        contours
    }
}

/// Whitespace as Python's `str.strip()` understands it.
///
/// Unicode's White_Space property and Python's `isspace()` agree on
/// everything except the four ASCII separators, which Python treats as space
/// and Unicode does not. A name record padded with one of those is the
/// difference between finding a family and not.
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python's `int()` on a float: truncate towards zero.
fn trunc(v: f64) -> i64 {
    if v.is_nan() {
        return 0;
    }
    v.trunc().clamp(i64::MIN as f64, i64::MAX as f64) as i64
}

fn f2dot14(buf: &[u8], pos: usize) -> f64 {
    match be16s(buf, pos) {
        Some(v) => v as f64 / 16384.0,
        None => 1.0,
    }
}

fn decode_utf16be(s: &[u8]) -> Option<String> {
    if s.len() % 2 != 0 {
        return None;
    }
    let units: Vec<u16> = s.chunks_exact(2).map(|c| u16::from_be_bytes([c[0], c[1]])).collect();
    char::decode_utf16(units)
        .collect::<Result<String, _>>()
        .ok()
}

fn read_cmap0(t: &[u8], off: usize, map: &mut HashMap<u32, u32>) {
    for code in 0..256usize {
        let pos = off + 6 + code;
        if let Some(&gid) = t.get(pos) {
            if gid != 0 {
                map.insert(code as u32, gid as u32);
            }
        }
    }
}

fn read_cmap4(t: &[u8], off: usize, map: &mut HashMap<u32, u32>) {
    let seg2 = be16(t, off + 6).unwrap_or(0) as usize;
    let seg = seg2 / 2;
    let ends = off + 14;
    let starts = ends + seg2 + 2;
    let deltas = starts + seg2;
    let ranges = deltas + seg2;
    for i in 0..seg {
        let end = match be16(t, ends + i * 2) {
            Some(v) => v as u32,
            None => continue,
        };
        let start = match be16(t, starts + i * 2) {
            Some(v) => v as u32,
            None => continue,
        };
        let delta = be16(t, deltas + i * 2).unwrap_or(0) as i32;
        let ro_at = ranges + i * 2;
        let ro = be16(t, ro_at).unwrap_or(0) as usize;
        if start > end {
            continue;
        }
        for code in start..=end.min(0xFFFF) {
            let gid = if ro == 0 {
                ((code as i32 + delta) & 0xFFFF) as u32
            } else {
                let gpos = ro_at + ro + (code - start) as usize * 2;
                match be16(t, gpos) {
                    Some(0) => 0,
                    Some(g) => ((g as i32 + delta) & 0xFFFF) as u32,
                    None => continue,
                }
            };
            if gid != 0 {
                map.insert(code, gid);
            }
        }
    }
}

fn read_cmap6(t: &[u8], off: usize, map: &mut HashMap<u32, u32>) {
    let first = be16(t, off + 6).unwrap_or(0) as u32;
    let count = be16(t, off + 8).unwrap_or(0) as usize;
    for i in 0..count {
        let pos = off + 10 + i * 2;
        match be16(t, pos) {
            Some(gid) => {
                map.insert(first + i as u32, gid as u32);
            }
            None => break,
        }
    }
}

fn read_cmap12(t: &[u8], off: usize, map: &mut HashMap<u32, u32>) {
    let n = be32(t, off + 12).unwrap_or(0) as usize;
    for i in 0..n {
        let rec = off + 16 + i * 12;
        if rec + 12 > t.len() {
            break;
        }
        let start = be32(t, rec).unwrap_or(0);
        let end = be32(t, rec + 4).unwrap_or(0);
        let gid = be32(t, rec + 8).unwrap_or(0);
        // A group spanning more than Unicode itself is a corrupt record, not
        // a reason to spend the afternoon inserting keys.
        if end as i64 - start as i64 > 0x10FFFF || end < start {
            continue;
        }
        for c in start..=end {
            map.insert(c, gid.wrapping_add(c - start));
        }
    }
}

fn parse_simple(g: &[u8], n_contours: usize) -> Contours {
    let mut pos = 10usize;
    let mut ends: Vec<usize> = Vec::with_capacity(n_contours);
    for _ in 0..n_contours {
        match be16(g, pos) {
            Some(v) => ends.push(v as usize),
            None => return Vec::new(),
        }
        pos += 2;
    }
    let n_points = ends.last().map_or(0, |&e| e + 1);
    let instr = match be16(g, pos) {
        Some(v) => v as usize,
        None => return Vec::new(),
    };
    pos += 2 + instr;

    let mut flags: Vec<u8> = Vec::with_capacity(n_points);
    while flags.len() < n_points && pos < g.len() {
        let f = g[pos];
        pos += 1;
        flags.push(f);
        if f & REPEAT != 0 && pos < g.len() {
            let rep = g[pos] as usize;
            pos += 1;
            for _ in 0..rep {
                flags.push(f);
            }
        }
    }
    if flags.len() < n_points {
        return Vec::new();
    }
    flags.truncate(n_points);

    let mut xs: Vec<i64> = Vec::with_capacity(n_points);
    let mut x: i64 = 0;
    for &f in &flags {
        if f & X_SHORT != 0 {
            let d = match g.get(pos) {
                Some(&d) => d as i64,
                None => return Vec::new(),
            };
            pos += 1;
            x += if f & X_SAME != 0 { d } else { -d };
        } else if f & X_SAME == 0 {
            match be16s(g, pos) {
                Some(d) => x += d as i64,
                None => return Vec::new(),
            }
            pos += 2;
        }
        xs.push(x);
    }

    let mut ys: Vec<i64> = Vec::with_capacity(n_points);
    let mut y: i64 = 0;
    for &f in &flags {
        if f & Y_SHORT != 0 {
            let d = match g.get(pos) {
                Some(&d) => d as i64,
                None => return Vec::new(),
            };
            pos += 1;
            y += if f & Y_SAME != 0 { d } else { -d };
        } else if f & Y_SAME == 0 {
            match be16s(g, pos) {
                Some(d) => y += d as i64,
                None => return Vec::new(),
            }
            pos += 2;
        }
        ys.push(y);
    }

    let mut contours: Contours = Vec::new();
    let mut first = 0usize;
    for &last in &ends {
        let stop = (last + 1).min(n_points);
        if first < stop {
            let pts: Vec<Point> = (first..stop)
                .map(|i| (xs[i], ys[i], flags[i] & ON_CURVE != 0))
                .collect();
            contours.push(pts);
        }
        first = last + 1;
    }
    contours
}

// -- flattening ------------------------------------------------------------

/// Convert quadratic contours to polygons in pixel space.
///
/// y is flipped here: fonts put the origin on the baseline with y growing
/// upward, and every surface we draw to grows downward.
pub fn flatten_contours(contours: &[Vec<Point>], scale: f64, steps: usize) -> Vec<Vec<(f64, f64)>> {
    let mut polys = Vec::new();
    for c in contours {
        if c.len() < 2 {
            continue;
        }
        let pts = resolve_implied(c);
        let n = pts.len();
        // Start on an on-curve point so the segment walk below is uniform.
        let start = pts.iter().position(|p| p.2).unwrap_or(0);
        let mut cur = (pts[start].0 as f64 * scale, -(pts[start].1 as f64) * scale);
        let mut poly = vec![cur];
        let mut i = 1usize;
        while i <= n {
            let p = pts[(start + i) % n];
            let (px, py) = (p.0 as f64 * scale, -(p.1 as f64) * scale);
            if p.2 {
                poly.push((px, py));
                cur = (px, py);
                i += 1;
                continue;
            }
            let nxt = pts[(start + i + 1) % n];
            let (nx, ny) = (nxt.0 as f64 * scale, -(nxt.1 as f64) * scale);
            for s in 1..=steps {
                let t = s as f64 / steps as f64;
                let u = 1.0 - t;
                poly.push((
                    u * u * cur.0 + 2.0 * u * t * px + t * t * nx,
                    u * u * cur.1 + 2.0 * u * t * py + t * t * ny,
                ));
            }
            cur = (nx, ny);
            i += 2;
        }
        if poly.len() > 2 {
            polys.push(poly);
        }
    }
    polys
}

/// Insert the on-curve midpoints TrueType leaves out between two off points.
fn resolve_implied(contour: &[Point]) -> Vec<Point> {
    let n = contour.len();
    let mut out = Vec::with_capacity(n);
    for (i, &p) in contour.iter().enumerate() {
        out.push(p);
        let nxt = contour[(i + 1) % n];
        if !p.2 && !nxt.2 {
            // Floor division, as Python's `//` on two ints: the midpoint of
            // an odd sum leans the same way it always did.
            out.push((
                (p.0 + nxt.0).div_euclid(2),
                (p.1 + nxt.1).div_euclid(2),
                true,
            ));
        }
    }
    out
}

#[pyfunction]
#[pyo3(signature = (contours, scale, steps = 8))]
pub fn flatten<'py>(
    py: Python<'py>,
    contours: Vec<Vec<Point>>,
    scale: f64,
    steps: usize,
) -> PyResult<Bound<'py, PyList>> {
    let polys = flatten_contours(&contours, scale, steps);
    let out = PyList::empty(py);
    for poly in polys {
        out.append(PyList::new(py, poly)?)?;
    }
    Ok(out)
}
