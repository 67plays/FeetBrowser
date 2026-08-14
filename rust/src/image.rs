//! Image decoders: PNG, GIF and the Netpbm family, decoded to raw RGBA.
//!
//! A straight port of what `imagecodec.py` did, byte for byte. The reason it
//! is worth having in Rust is not only speed: this is the one part of the
//! renderer that parses bytes an arbitrary site handed us, and Python's
//! bounds-checked indexing was doing real work for us there. Every read here
//! goes through `at`/`slice`/`be16` and friends, which return an Option or a
//! short slice rather than panicking, because a panic crossing the FFI
//! boundary is a `PanicException` that takes the whole page load with it --
//! there is no `except` clause on the Python side that catches that.

use crate::pyutil::bytes_arg;
use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

create_exception!(feetbrowser_engine, ImageError, PyException);

// These bytes come off the network, and a header is a claim rather than a
// fact: nothing stops a page declaring 40000x40000 pixels, or shipping a few
// hundred bytes that inflate to gigabytes.
pub const MAX_PIXELS: u64 = 20_000_000;
pub const MAX_INFLATED: usize = 256 << 20;

fn bad(msg: impl Into<String>) -> PyErr {
    ImageError::new_err(msg.into())
}

/// The message shape Python's `decode` produced when a decoder came apart on
/// short input, kept so error text in the browser's console does not change.
fn malformed(msg: impl std::fmt::Display) -> PyErr {
    ImageError::new_err(format!("malformed image data: {}", msg))
}

// -- bounded reads ---------------------------------------------------------

fn at(data: &[u8], i: usize) -> Option<u8> {
    data.get(i).copied()
}

/// `data[start:end]` with Python's clamping, never a panic.
fn slice(data: &[u8], start: usize, end: usize) -> &[u8] {
    let start = start.min(data.len());
    let end = end.max(start).min(data.len());
    &data[start..end]
}

fn be16(data: &[u8], i: usize) -> Option<u16> {
    Some(u16::from_be_bytes([at(data, i)?, at(data, i + 1)?]))
}

fn be32(data: &[u8], i: usize) -> Option<u32> {
    Some(u32::from_be_bytes([
        at(data, i)?,
        at(data, i + 1)?,
        at(data, i + 2)?,
        at(data, i + 3)?,
    ]))
}

fn le16(data: &[u8], i: usize) -> Option<u16> {
    Some(u16::from_le_bytes([at(data, i)?, at(data, i + 1)?]))
}

fn check_size(width: i64, height: i64) -> PyResult<()> {
    if width <= 0 || height <= 0 {
        return Err(bad("image has no area"));
    }
    if (width as u64).saturating_mul(height as u64) > MAX_PIXELS {
        return Err(bad(format!(
            "image too large: {}x{} pixels",
            width, height
        )));
    }
    Ok(())
}

// -- inflate ---------------------------------------------------------------

/// zlib, with a ceiling on what comes out the other end.
///
/// Truncated compressed data is the normal case rather than the exceptional
/// one -- a connection drops mid-image on any page -- and Python's
/// decompressobj handed back whatever it had managed to inflate, so a short
/// stream has to come back as partial samples rather than as an error.
fn inflate(data: &[u8]) -> PyResult<Vec<u8>> {
    use miniz_oxide::inflate::TINFLStatus;
    match miniz_oxide::inflate::decompress_to_vec_zlib_with_limit(data, MAX_INFLATED) {
        Ok(out) => Ok(out),
        Err(e) => match e.status {
            TINFLStatus::HasMoreOutput => Err(bad("compressed image data expands too far")),
            // Ran out of input mid-stream: keep what inflated, as zlib did.
            TINFLStatus::FailedCannotMakeProgress | TINFLStatus::NeedsMoreInput => Ok(e.output),
            other => Err(malformed(format!("Error -3 while decompressing data: {:?}", other))),
        },
    }
}

// -- entry points ----------------------------------------------------------

fn signature_png(data: &[u8]) -> bool {
    data.len() >= 8 && &data[..8] == b"\x89PNG\r\n\x1a\n"
}

fn signature_gif(data: &[u8]) -> bool {
    data.len() >= 6 && (&data[..6] == b"GIF87a" || &data[..6] == b"GIF89a")
}

fn signature_pnm(data: &[u8]) -> bool {
    data.len() >= 2
        && data[0] == b'P'
        && matches!(data[1], b'1' | b'2' | b'3' | b'4' | b'5' | b'6')
}

pub fn decode_bytes(data: &[u8]) -> PyResult<(i64, i64, Vec<u8>)> {
    if signature_png(data) {
        png(data)
    } else if signature_gif(data) {
        gif(data)
    } else if signature_pnm(data) {
        pnm(data)
    } else {
        Err(bad("unrecognised image format"))
    }
}

#[pyfunction]
#[pyo3(name = "decode")]
pub fn py_decode(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<(i64, i64, Py<PyBytes>)> {
    let buf = bytes_arg(data)?;
    let (w, h, rgba) = decode_bytes(&buf)?;
    Ok((w, h, PyBytes::new(py, &rgba).unbind()))
}

#[pyfunction]
#[pyo3(name = "decode_png")]
pub fn py_decode_png(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<(i64, i64, Py<PyBytes>)> {
    let buf = bytes_arg(data)?;
    let (w, h, rgba) = png(&buf)?;
    Ok((w, h, PyBytes::new(py, &rgba).unbind()))
}

#[pyfunction]
#[pyo3(name = "decode_gif")]
pub fn py_decode_gif(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<(i64, i64, Py<PyBytes>)> {
    let buf = bytes_arg(data)?;
    let (w, h, rgba) = gif(&buf)?;
    Ok((w, h, PyBytes::new(py, &rgba).unbind()))
}

#[pyfunction]
#[pyo3(name = "decode_pnm")]
pub fn py_decode_pnm(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<(i64, i64, Py<PyBytes>)> {
    let buf = bytes_arg(data)?;
    let (w, h, rgba) = pnm(&buf)?;
    Ok((w, h, PyBytes::new(py, &rgba).unbind()))
}

#[pyfunction]
#[pyo3(name = "sniff")]
pub fn py_sniff(data: &Bound<'_, PyAny>) -> PyResult<bool> {
    let buf = bytes_arg(data)?;
    Ok(signature_png(&buf) || signature_gif(&buf) || signature_pnm(&buf))
}

// -- PNG -------------------------------------------------------------------

// Adam7: (x offset, y offset, x step, y step) for each of the seven passes.
const ADAM7: [(i64, i64, i64, i64); 7] = [
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
];

fn channels_for(color: u8) -> Option<usize> {
    match color {
        0 => Some(1),
        2 => Some(3),
        3 => Some(1),
        4 => Some(2),
        6 => Some(4),
        _ => None,
    }
}

fn png(data: &[u8]) -> PyResult<(i64, i64, Vec<u8>)> {
    if !signature_png(data) {
        return Err(bad("not a PNG"));
    }
    let mut pos: usize = 8;
    let (mut width, mut height): (u32, u32) = (0, 0);
    let (mut depth, mut color, mut interlace): (u8, u8, u8) = (0, 0, 0);
    let mut palette: &[u8] = b"";
    let mut trns: Option<&[u8]> = None;
    let mut idat: Vec<u8> = Vec::new();
    let mut seen_header = false;

    while pos + 8 <= data.len() {
        let length = be32(data, pos).unwrap_or(0) as usize;
        let tag = slice(data, pos + 4, pos + 8);
        let tag = [tag[0], tag[1], tag[2], tag[3]];
        pos += 8;
        let payload = slice(data, pos, pos.saturating_add(length));
        // Skip the CRC too; we trust the transport.
        pos = pos.saturating_add(length).saturating_add(4);
        match &tag {
            b"IHDR" => {
                if payload.len() < 13 {
                    return Err(malformed("unpack requires a buffer of 13 bytes"));
                }
                width = be32(payload, 0).unwrap_or(0);
                height = be32(payload, 4).unwrap_or(0);
                depth = payload[8];
                color = payload[9];
                interlace = payload[12];
                seen_header = true;
            }
            b"PLTE" => palette = payload,
            b"tRNS" => trns = Some(payload),
            b"IDAT" => idat.extend_from_slice(payload),
            b"IEND" => break,
            _ => {}
        }
    }
    if !seen_header || width == 0 || height == 0 {
        return Err(bad("PNG has no usable header"));
    }
    let channels = match channels_for(color) {
        Some(c) => c,
        None => return Err(bad(format!("unsupported PNG colour type {}", color))),
    };
    if !matches!(depth, 1 | 2 | 4 | 8 | 16) {
        return Err(bad(format!("unsupported PNG bit depth {}", depth)));
    }
    check_size(width as i64, height as i64)?;
    let raw = inflate(&idat)?;
    let width = width as usize;
    let height = height as usize;

    let samples = if interlace == 1 {
        let mut samples = vec![0u8; width * height * channels];
        let mut pos = 0usize;
        for (ox, oy, sx, sy) in ADAM7 {
            let pw = (width as i64 - ox + sx - 1).div_euclid(sx);
            let ph = (height as i64 - oy + sy - 1).div_euclid(sy);
            if pw <= 0 || ph <= 0 {
                continue;
            }
            let (pw, ph) = (pw as usize, ph as usize);
            let size = pass_size(pw, ph, channels, depth);
            let plane = unfilter(slice(&raw, pos, pos.saturating_add(size)), pw, ph, channels, depth)?;
            pos = pos.saturating_add(size);
            let plane = to_bytes(plane, pw, ph, channels, depth);
            for y in 0..ph {
                for x in 0..pw {
                    let src = (y * pw + x) * channels;
                    let dst = ((oy as usize + y * sy as usize) * width
                        + (ox as usize + x * sx as usize))
                        * channels;
                    if dst + channels <= samples.len() && src + channels <= plane.len() {
                        samples[dst..dst + channels]
                            .copy_from_slice(&plane[src..src + channels]);
                    }
                }
            }
        }
        samples
    } else if interlace == 0 {
        to_bytes(
            unfilter(&raw, width, height, channels, depth)?,
            width,
            height,
            channels,
            depth,
        )
    } else {
        return Err(bad(format!(
            "unsupported PNG interlace method {}",
            interlace
        )));
    };

    let rgba = to_rgba(&samples, width, height, color, palette, trns, depth)?;
    Ok((width as i64, height as i64, rgba))
}

fn pass_size(width: usize, height: usize, channels: usize, depth: u8) -> usize {
    height * (1 + (width * channels * depth as usize + 7) / 8)
}

/// Reverse the per-scanline PNG filters, returning packed samples.
fn unfilter(
    raw: &[u8],
    width: usize,
    height: usize,
    channels: usize,
    depth: u8,
) -> PyResult<Vec<u8>> {
    let bpp = std::cmp::max(1, (channels * depth as usize + 7) / 8);
    let stride = (width * channels * depth as usize + 7) / 8;
    let mut out = vec![0u8; stride * height];
    let mut pos = 0usize;
    let mut prev = vec![0u8; stride];
    let mut line = vec![0u8; stride];
    for y in 0..height {
        if pos >= raw.len() {
            break;
        }
        let ftype = raw[pos];
        pos += 1;
        let avail = slice(raw, pos, pos.saturating_add(stride));
        line[..avail.len()].copy_from_slice(avail);
        for b in line[avail.len()..].iter_mut() {
            *b = 0;
        }
        pos = pos.saturating_add(stride);
        match ftype {
            0 => {}
            1 => {
                for i in bpp..stride {
                    line[i] = line[i].wrapping_add(line[i - bpp]);
                }
            }
            2 => {
                for i in 0..stride {
                    line[i] = line[i].wrapping_add(prev[i]);
                }
            }
            3 => {
                for i in 0..stride {
                    let left = if i >= bpp { line[i - bpp] as u32 } else { 0 };
                    let up = prev[i] as u32;
                    line[i] = line[i].wrapping_add(((left + up) >> 1) as u8);
                }
            }
            4 => {
                for i in 0..stride {
                    let a = if i >= bpp { line[i - bpp] as i32 } else { 0 };
                    let b = prev[i] as i32;
                    let c = if i >= bpp { prev[i - bpp] as i32 } else { 0 };
                    let p = a + b - c;
                    let (pa, pb, pc) = ((p - a).abs(), (p - b).abs(), (p - c).abs());
                    let pred = if pa <= pb && pa <= pc {
                        a
                    } else if pb <= pc {
                        b
                    } else {
                        c
                    };
                    line[i] = line[i].wrapping_add(pred as u8);
                }
            }
            other => return Err(bad(format!("bad PNG filter type {}", other))),
        }
        out[y * stride..(y + 1) * stride].copy_from_slice(&line);
        prev.copy_from_slice(&line);
    }
    Ok(out)
}

/// Expand packed samples to one byte per sample.
fn to_bytes(packed: Vec<u8>, width: usize, height: usize, channels: usize, depth: u8) -> Vec<u8> {
    if depth == 8 {
        return packed;
    }
    let count = width * channels;
    let mut out = vec![0u8; count * height];
    if depth == 16 {
        let stride = count * 2;
        for y in 0..height {
            let src = y * stride;
            for i in 0..count {
                if let Some(v) = packed.get(src + i * 2) {
                    out[y * count + i] = *v;
                }
            }
        }
        return out;
    }
    let stride = (count * depth as usize + 7) / 8;
    let mask = (1u16 << depth) - 1;
    let per_byte = 8 / depth as usize;
    // Greyscale levels get stretched to the full range by the caller; palette
    // indices must stay untouched. Here we only unpack.
    for y in 0..height {
        let base = y * stride;
        let o = y * count;
        for i in 0..count {
            let byte = packed.get(base + i / per_byte).copied().unwrap_or(0) as u16;
            let shift = 8 - depth as usize * (i % per_byte + 1);
            out[o + i] = ((byte >> shift) & mask) as u8;
        }
    }
    out
}

fn to_rgba(
    samples: &[u8],
    width: usize,
    height: usize,
    color: u8,
    palette: &[u8],
    trns: Option<&[u8]>,
    depth: u8,
) -> PyResult<Vec<u8>> {
    let n = width * height;
    let mut rgba = vec![0u8; n * 4];
    // Sub-byte greyscale arrives as raw levels; stretch them to 0..255.
    let scale: u32 = if depth < 8 && color != 3 {
        255 / ((1u32 << depth) - 1)
    } else {
        1
    };

    match color {
        3 => {
            if palette.is_empty() {
                return Err(bad("indexed PNG without a palette"));
            }
            let alpha = trns.unwrap_or(b"");
            for i in 0..n {
                let idx = *samples.get(i).unwrap_or(&0) as usize;
                let o = idx * 3;
                let d = i * 4;
                if o + 2 < palette.len() {
                    rgba[d] = palette[o];
                    rgba[d + 1] = palette[o + 1];
                    rgba[d + 2] = palette[o + 2];
                }
                rgba[d + 3] = if idx < alpha.len() { alpha[idx] } else { 255 };
            }
        }
        0 => {
            let key = trns
                .and_then(|t| be16(t, 0))
                .map(|raw| if depth == 16 { raw >> 8 } else { raw });
            for i in 0..n {
                let s = *samples.get(i).unwrap_or(&0);
                let v = (s as u32 * scale).min(255) as u8;
                let d = i * 4;
                rgba[d] = v;
                rgba[d + 1] = v;
                rgba[d + 2] = v;
                rgba[d + 3] = if key == Some(s as u16) { 0 } else { 255 };
            }
        }
        4 => {
            for i in 0..n {
                let (s, d) = (i * 2, i * 4);
                let v = *samples.get(s).unwrap_or(&0);
                rgba[d] = v;
                rgba[d + 1] = v;
                rgba[d + 2] = v;
                rgba[d + 3] = *samples.get(s + 1).unwrap_or(&0);
            }
        }
        2 => {
            let key = trns
                .and_then(|t| Some([be16(t, 0)?, be16(t, 2)?, be16(t, 4)?]))
                .map(|c| {
                    if depth == 16 {
                        [c[0] >> 8, c[1] >> 8, c[2] >> 8]
                    } else {
                        c
                    }
                });
            for i in 0..n {
                let (s, d) = (i * 3, i * 4);
                let r = *samples.get(s).unwrap_or(&0);
                let g = *samples.get(s + 1).unwrap_or(&0);
                let b = *samples.get(s + 2).unwrap_or(&0);
                rgba[d] = r;
                rgba[d + 1] = g;
                rgba[d + 2] = b;
                rgba[d + 3] = if key == Some([r as u16, g as u16, b as u16]) {
                    0
                } else {
                    255
                };
            }
        }
        _ => {
            let take = std::cmp::min(n * 4, samples.len());
            rgba[..take].copy_from_slice(&samples[..take]);
        }
    }
    Ok(rgba)
}

// -- GIF -------------------------------------------------------------------

/// Decode a GIF's first frame. Animation is out of scope, as it was for Tk's
/// PhotoImage, which also showed only the first frame.
fn gif(data: &[u8]) -> PyResult<(i64, i64, Vec<u8>)> {
    if !signature_gif(data) {
        return Err(bad("not a GIF"));
    }
    let screen_w = le16(data, 6).ok_or_else(|| malformed("unpack requires a buffer of 7 bytes"))? as i64;
    let screen_h = le16(data, 8).ok_or_else(|| malformed("unpack requires a buffer of 7 bytes"))? as i64;
    let flags = at(data, 10).ok_or_else(|| malformed("unpack requires a buffer of 7 bytes"))?;
    at(data, 12).ok_or_else(|| malformed("unpack requires a buffer of 7 bytes"))?;
    let mut pos: usize = 13;
    let mut global_table: &[u8] = b"";
    if flags & 0x80 != 0 {
        let size = 3 * (2usize << (flags & 0x07));
        global_table = slice(data, pos, pos + size);
        pos += size;
    }

    let mut transparent: Option<u8> = None;
    while pos < data.len() {
        let block = data[pos];
        if block == 0x21 {
            // extension
            let label = at(data, pos + 1).ok_or_else(|| malformed("index out of range"))?;
            pos += 2;
            if label == 0xF9 && at(data, pos).ok_or_else(|| malformed("index out of range"))? >= 4 {
                let gflags = at(data, pos + 1).ok_or_else(|| malformed("index out of range"))?;
                if gflags & 0x01 != 0 {
                    transparent =
                        Some(at(data, pos + 4).ok_or_else(|| malformed("index out of range"))?);
                }
            }
            pos = skip_blocks(data, pos);
        } else if block == 0x2C {
            // image descriptor
            let left = le16(data, pos + 1).ok_or_else(|| malformed("unpack requires a buffer of 9 bytes"))? as i64;
            let top = le16(data, pos + 3).ok_or_else(|| malformed("unpack requires a buffer of 9 bytes"))? as i64;
            let w = le16(data, pos + 5).ok_or_else(|| malformed("unpack requires a buffer of 9 bytes"))? as i64;
            let h = le16(data, pos + 7).ok_or_else(|| malformed("unpack requires a buffer of 9 bytes"))? as i64;
            let iflags = at(data, pos + 9).ok_or_else(|| malformed("unpack requires a buffer of 9 bytes"))?;
            check_size(
                std::cmp::max(screen_w, left + w),
                std::cmp::max(screen_h, top + h),
            )?;
            pos += 10;
            let mut table = global_table;
            if iflags & 0x80 != 0 {
                let size = 3 * (2usize << (iflags & 0x07));
                table = slice(data, pos, pos + size);
                pos += size;
            }
            let min_code = at(data, pos).ok_or_else(|| malformed("index out of range"))?;
            pos += 1;
            let mut chunks: Vec<u8> = Vec::new();
            while pos < data.len() && data[pos] != 0 {
                let n = data[pos] as usize;
                chunks.extend_from_slice(slice(data, pos + 1, pos + 1 + n));
                pos += 1 + n;
            }
            let expected = (w as usize).saturating_mul(h as usize);
            let mut indices = lzw(&chunks, min_code, expected)?;
            if iflags & 0x40 != 0 {
                indices = deinterlace(&indices, w as usize, h as usize);
            }
            return Ok(gif_to_rgba(&indices, w, h, table, transparent));
        } else if block == 0x3B {
            break; // trailer
        } else {
            return Err(bad(format!("unexpected GIF block 0x{:02X}", block)));
        }
    }
    Err(bad("GIF contains no image"))
}

fn skip_blocks(data: &[u8], mut pos: usize) -> usize {
    while pos < data.len() && data[pos] != 0 {
        pos = pos.saturating_add(data[pos] as usize + 1);
    }
    pos.saturating_add(1)
}

fn deinterlace(indices: &[u8], w: usize, h: usize) -> Vec<u8> {
    let mut out = vec![0u8; w * h];
    let mut rows: Vec<usize> = Vec::with_capacity(h);
    rows.extend((0..h).step_by(8));
    rows.extend((4..h).step_by(8));
    rows.extend((2..h).step_by(4));
    rows.extend((1..h).step_by(2));
    for (src, dst) in rows.into_iter().enumerate() {
        let from = slice(indices, src * w, (src + 1) * w);
        let start = dst * w;
        let end = std::cmp::min(start + from.len(), out.len());
        if start < end {
            out[start..end].copy_from_slice(&from[..end - start]);
        }
    }
    out
}

fn gif_to_rgba(
    indices: &[u8],
    w: i64,
    h: i64,
    table: &[u8],
    transparent: Option<u8>,
) -> (i64, i64, Vec<u8>) {
    let n = (w as usize).saturating_mul(h as usize);
    let mut rgba = vec![0u8; n * 4];
    for i in 0..std::cmp::min(indices.len(), n) {
        let idx = indices[i];
        let o = idx as usize * 3;
        let d = i * 4;
        if o + 2 < table.len() {
            rgba[d] = table[o];
            rgba[d + 1] = table[o + 1];
            rgba[d + 2] = table[o + 2];
        }
        rgba[d + 3] = if Some(idx) == transparent { 0 } else { 255 };
    }
    (w, h, rgba)
}

/// GIF's variable-width LZW. Codes are packed little-endian, least
/// significant bit first, and the code width grows as the table fills.
fn lzw(data: &[u8], min_code: u8, expected: usize) -> PyResult<Vec<u8>> {
    // A GIF palette holds at most 256 colours, so the initial code size is
    // never above 8 and the seed table never has more than 256 literals. The
    // Python decoder enforced that by accident -- it seeded the table with
    // `bytes([i])`, which threw as soon as i reached 256 -- and a file
    // declaring more would otherwise ask us for a 2**min_code entry table on
    // the strength of one byte it made up.
    if min_code > 8 {
        return Err(bad(format!("GIF code size {} out of range", min_code)));
    }
    let clear = 1usize << min_code;
    let end = clear + 1;
    let mut width = min_code as usize + 1;
    let fresh = || -> Vec<Vec<u8>> {
        let mut t: Vec<Vec<u8>> = (0..clear).map(|i| vec![i as u8]).collect();
        t.push(Vec::new());
        t.push(Vec::new());
        t
    };
    let mut table = fresh();
    let mut out: Vec<u8> = Vec::new();
    let mut prev: Option<Vec<u8>> = None;
    let mut bitpos: usize = 0;
    let total = data.len().saturating_mul(8);
    while bitpos + width <= total && out.len() < expected {
        let byte = bitpos >> 3;
        let chunk = u32::from_le_bytes([
            at(data, byte).unwrap_or(0),
            at(data, byte + 1).unwrap_or(0),
            at(data, byte + 2).unwrap_or(0),
            0,
        ]);
        let code = ((chunk >> (bitpos & 7)) & ((1u32 << width) - 1)) as usize;
        bitpos += width;
        if code == clear {
            table = fresh();
            width = min_code as usize + 1;
            prev = None;
            continue;
        }
        if code == end {
            break;
        }
        let entry: Vec<u8> = if code < table.len() {
            table[code].clone()
        } else if let Some(p) = &prev {
            let mut e = p.clone();
            if let Some(first) = p.first() {
                e.push(*first);
            }
            e
        } else {
            break;
        };
        out.extend_from_slice(&entry);
        if let Some(p) = &prev {
            if table.len() < 4096 {
                let mut next = p.clone();
                if let Some(first) = entry.first() {
                    next.push(*first);
                }
                table.push(next);
                if table.len() == (1usize << width) && width < 12 {
                    width += 1;
                }
            }
        }
        prev = Some(entry);
    }
    if out.len() < expected {
        out.resize(expected, 0);
    }
    Ok(out)
}

// -- Netpbm ----------------------------------------------------------------

fn is_pnm_space(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c)
}

fn parse_int(field: &[u8]) -> PyResult<i64> {
    let mut i = 0usize;
    let mut sign: i64 = 1;
    if let Some(&b) = field.first() {
        if b == b'+' || b == b'-' {
            if b == b'-' {
                sign = -1;
            }
            i = 1;
        }
    }
    if i >= field.len() {
        return Err(malformed(format!(
            "invalid literal for int() with base 10: {:?}",
            String::from_utf8_lossy(field)
        )));
    }
    let mut value: i64 = 0;
    // Python's int() is what read these fields before, so it is the spec:
    // that includes its digit separators, where an underscore is legal
    // between two digits and nowhere else. No real Netpbm file writes
    // `2_5`, but a decoder that changed its mind about one would be a
    // difference in what pages render, so we keep it.
    let mut after_digit = false;
    for &b in &field[i..] {
        if b == b'_' {
            if !after_digit {
                return Err(malformed(format!(
                    "invalid literal for int() with base 10: {:?}",
                    String::from_utf8_lossy(field)
                )));
            }
            after_digit = false;
            continue;
        }
        if !b.is_ascii_digit() {
            return Err(malformed(format!(
                "invalid literal for int() with base 10: {:?}",
                String::from_utf8_lossy(field)
            )));
        }
        after_digit = true;
        // A header claiming an astronomical size is rejected by the pixel cap
        // just as well when the number saturates, so saturating here loses
        // nothing an honest file cares about.
        value = value.saturating_mul(10).saturating_add((b - b'0') as i64);
    }
    if !after_digit {
        return Err(malformed(format!(
            "invalid literal for int() with base 10: {:?}",
            String::from_utf8_lossy(field)
        )));
    }
    Ok(sign * value)
}

/// PBM/PGM/PPM, ASCII (P1-P3) and binary (P4-P6).
fn pnm(data: &[u8]) -> PyResult<(i64, i64, Vec<u8>)> {
    let magic = slice(data, 0, 2);
    let fields = if magic == b"P1" || magic == b"P4" { 2 } else { 3 };
    let mut values: Vec<i64> = Vec::new();
    let mut pos = 2usize;
    while values.len() < fields && pos < data.len() {
        if is_pnm_space(data[pos]) {
            pos += 1;
        } else if data[pos] == b'#' {
            while pos < data.len() && data[pos] != b'\n' {
                pos += 1;
            }
        } else {
            let start = pos;
            while pos < data.len() && !is_pnm_space(data[pos]) {
                pos += 1;
            }
            values.push(parse_int(&data[start..pos])?);
        }
    }
    if values.len() < fields {
        return Err(bad("truncated Netpbm header"));
    }
    let (width, height) = (values[0], values[1]);
    let maxval = if fields == 3 { values[2] } else { 1 };
    check_size(width, height)?;
    pos += 1; // single whitespace byte after the header

    let width = width as usize;
    let height = height as usize;
    let n = width * height;
    let mut rgba = vec![0u8; n * 4];
    let scale: f64 = if maxval != 0 {
        255.0 / maxval as f64
    } else {
        1.0
    };

    if magic == b"P4" {
        // packed bitmap, 1 = black
        let stride = (width + 7) / 8;
        for y in 0..height {
            for x in 0..width {
                let byte = at(data, pos + y * stride + x / 8)
                    .ok_or_else(|| malformed("index out of range"))?;
                let bit = (byte >> (7 - x % 8)) & 1;
                let v = if bit != 0 { 0u8 } else { 255u8 };
                let d = (y * width + x) * 4;
                rgba[d] = v;
                rgba[d + 1] = v;
                rgba[d + 2] = v;
                rgba[d + 3] = 255;
            }
        }
        return Ok((width as i64, height as i64, rgba));
    }

    if magic == b"P5" || magic == b"P6" {
        let step = if magic == b"P5" { 1 } else { 3 };
        let wide = maxval > 255;
        let unit = if wide { 2 } else { 1 };
        let size = step * unit;
        for i in 0..n {
            let s = pos + i * size;
            if s + size > data.len() {
                break;
            }
            let d = i * 4;
            for c in 0..step {
                let o = s + c * unit;
                // A wide sample is two bytes, most significant first, and it
                // scales against maxval like a narrow one does -- reading
                // only the high byte is right for maxval 65535 and nothing
                // else.
                let v = if wide {
                    ((at(data, o).unwrap_or(0) as u32) << 8) | at(data, o + 1).unwrap_or(0) as u32
                } else {
                    at(data, o).unwrap_or(0) as u32
                };
                let scaled = std::cmp::min(255, trunc_to_byte(v as f64 * scale));
                // A negative maxval makes every sample negative, which was a
                // ValueError on the way into the bytearray; keep it an error
                // rather than letting the cast wrap.
                if scaled < 0 {
                    return Err(malformed("byte must be in range(0, 256)"));
                }
                rgba[d + c] = scaled as u8;
            }
            if step == 1 {
                rgba[d + 1] = rgba[d];
                rgba[d + 2] = rgba[d];
            }
            rgba[d + 3] = 255;
        }
        return Ok((width as i64, height as i64, rgba));
    }

    // ASCII variants: the rest of the file is whitespace-separated numbers.
    let nums: Vec<&[u8]> = slice(data, pos, data.len())
        .split(|b| is_pnm_space(*b))
        .filter(|f| !f.is_empty())
        .collect();
    let step = if magic == b"P1" || magic == b"P2" { 1 } else { 3 };
    for i in 0..n {
        let d = i * 4;
        for c in 0..step {
            let j = i * step + c;
            if j >= nums.len() {
                break;
            }
            let v = parse_int(nums[j])?;
            let scaled = if magic == b"P1" {
                (1 - v) * 255
            } else {
                trunc_to_byte(v as f64 * scale)
            };
            // Assigning outside 0..255 was a ValueError in Python, which
            // decode() turned into an ImageError; keep that.
            if !(0..=255).contains(&scaled) {
                return Err(malformed("byte must be in range(0, 256)"));
            }
            rgba[d + c] = scaled as u8;
        }
        if step == 1 {
            rgba[d + 1] = rgba[d];
            rgba[d + 2] = rgba[d];
        }
        rgba[d + 3] = 255;
    }
    Ok((width as i64, height as i64, rgba))
}

/// Python's `int()` on a float: truncate toward zero, saturating rather than
/// wrapping so a nonsense maxval cannot produce an undefined cast.
fn trunc_to_byte(v: f64) -> i64 {
    if v.is_nan() {
        0
    } else if v >= i64::MAX as f64 {
        i64::MAX
    } else if v <= i64::MIN as f64 {
        i64::MIN
    } else {
        v.trunc() as i64
    }
}

// -- resampling ------------------------------------------------------------

/// Nearest-neighbour resample. CSS width/height on <img> needs it, and
/// nearest keeps it a pure index computation with no per-pixel arithmetic.
#[pyfunction]
#[pyo3(name = "resize")]
pub fn py_resize(
    py: Python<'_>,
    rgba: &Bound<'_, PyAny>,
    width: i64,
    height: i64,
    new_width: f64,
    new_height: f64,
) -> PyResult<Py<PyAny>> {
    let nw = std::cmp::max(1, trunc_to_byte(new_width));
    let nh = std::cmp::max(1, trunc_to_byte(new_height));
    if (nw, nh) == (width, height) {
        // Callers rely on getting their own buffer back untouched.
        return Ok(rgba.clone().unbind());
    }
    if (nw as u64).saturating_mul(nh as u64) > MAX_PIXELS {
        return Err(bad(format!("image too large: {}x{} pixels", nw, nh)));
    }
    let src = bytes_arg(rgba)?;
    let (nw, nh) = (nw as usize, nh as usize);
    let mut out = vec![0u8; nw * nh * 4];
    if width <= 0 || height <= 0 {
        return Ok(PyBytes::new(py, &out).unbind().into_any());
    }
    let (width, height) = (width as usize, height as usize);
    let xmap: Vec<usize> = (0..nw)
        .map(|x| std::cmp::min(width - 1, x * width / nw) * 4)
        .collect();
    for y in 0..nh {
        let src_row = std::cmp::min(height - 1, y * height / nh) * width * 4;
        let mut d = y * nw * 4;
        for sx in &xmap {
            let s = src_row + sx;
            if s + 4 <= src.len() {
                out[d..d + 4].copy_from_slice(&src[s..s + 4]);
            }
            d += 4;
        }
    }
    Ok(PyBytes::new(py, &out).unbind().into_any())
}
