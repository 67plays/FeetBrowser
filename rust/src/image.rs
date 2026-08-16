//! Image decoders: PNG, GIF, JPEG and the Netpbm family, decoded to raw RGBA.
//!
//! PNG, GIF and Netpbm are a straight port of what `imagecodec.py` did, byte
//! for byte. JPEG was written here: baseline and progressive Huffman-coded
//! frames, with an AAN inverse transform and libjpeg's triangle filter for
//! the halved chroma channels. What it does not do -- arithmetic coding,
//! CMYK, 12-bit samples, lossless and hierarchical modes -- it refuses,
//! because a picture decoded wrong is worse than a picture not decoded.
//!
//! The reason all of it is worth having in Rust is not only speed: this is
//! the one part of the
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
    } else if signature_jpeg(data) {
        jpeg(data)
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

/// Every frame of a GIF: `(width, height, [(rgba, delay_ms)], loop_count)`.
///
/// A still GIF comes back as a one-frame animation rather than as an error,
/// so a caller does not have to know which it has before it asks.
#[pyfunction]
#[pyo3(name = "decode_gif_frames")]
pub fn py_decode_gif_frames(
    py: Python<'_>,
    data: &Bound<'_, PyAny>,
) -> PyResult<(i64, i64, Vec<(Py<PyBytes>, i64)>, i64)> {
    let buf = bytes_arg(data)?;
    let (w, h, frames, loops) = gif_frames(&buf, None)?;
    let out = frames
        .into_iter()
        .map(|f| (PyBytes::new(py, &f.rgba).unbind(), f.delay_ms))
        .collect();
    Ok((w, h, out, loops))
}

#[pyfunction]
#[pyo3(name = "decode_jpeg")]
pub fn py_decode_jpeg(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<(i64, i64, Py<PyBytes>)> {
    let buf = bytes_arg(data)?;
    let (w, h, rgba) = jpeg(&buf)?;
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
    Ok(signature_png(&buf) || signature_gif(&buf) || signature_jpeg(&buf) || signature_pnm(&buf))
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

/// One frame of a GIF: the whole logical screen as it looks once this frame
/// has been composited onto what came before, and how long it stays there.
///
/// Whole-screen rather than just the sub-rectangle the file stored, because
/// that rectangle is a compression detail and not a picture: a frame of an
/// animation is routinely a dozen pixels in one corner, meaning "and
/// everything else is as it was". Handing that to a caller as an image would
/// be handing it a dozen pixels.
pub struct GifFrame {
    pub rgba: Vec<u8>,
    /// What the file asked for, in milliseconds, faithfully -- including the
    /// zero that means "as fast as you can", which is a request no browser
    /// grants. Deciding what to do about that is animation policy and lives
    /// with the animation, in `canvas.PhotoImage`; the decoder's job is to
    /// report what the bytes said.
    pub delay_ms: i64,
}

/// Decode a GIF's first frame, at the size of its logical screen.
fn gif(data: &[u8]) -> PyResult<(i64, i64, Vec<u8>)> {
    let (w, h, mut frames, _loops) = gif_frames(data, Some(1))?;
    let first = frames
        .drain(..)
        .next()
        .ok_or_else(|| bad("GIF contains no image"))?;
    Ok((w, h, first.rgba))
}

/// Decode a GIF, composited frame by frame onto its logical screen.
///
/// `limit` stops after that many frames, which is what the still-image entry
/// point passes: the frames after the first cost their own LZW pass and a
/// screen-sized copy each, and nothing is going to look at them.
///
/// Returns the screen size, the frames, and the loop count the file asked
/// for: 0 for "for ever", -1 when the file never said.
fn gif_frames(data: &[u8], limit: Option<usize>) -> PyResult<(i64, i64, Vec<GifFrame>, i64)> {
    if !signature_gif(data) {
        return Err(bad("not a GIF"));
    }
    let screen_w = le16(data, 6).ok_or_else(|| malformed("unpack requires a buffer of 7 bytes"))? as i64;
    let screen_h = le16(data, 8).ok_or_else(|| malformed("unpack requires a buffer of 7 bytes"))? as i64;
    let flags = at(data, 10).ok_or_else(|| malformed("unpack requires a buffer of 7 bytes"))?;
    at(data, 12).ok_or_else(|| malformed("unpack requires a buffer of 7 bytes"))?;
    check_size(screen_w, screen_h)?;
    let mut pos: usize = 13;
    let mut global_table: &[u8] = b"";
    if flags & 0x80 != 0 {
        let size = 3 * (2usize << (flags & 0x07));
        global_table = slice(data, pos, pos + size);
        pos += size;
    }

    // The screen starts transparent rather than filled with the background
    // colour the header names. That is what browsers show, and it is also the
    // only choice that composites: a GIF laid over a page has to let the page
    // through where it never drew.
    let pixels = (screen_w as usize).saturating_mul(screen_h as usize);
    let mut canvas = vec![0u8; pixels.saturating_mul(4)];
    let mut frames: Vec<GifFrame> = Vec::new();
    let mut loops: i64 = -1;

    // Graphic control state. The spec scopes it to the single image that
    // follows, so it is cleared after each one rather than carried.
    let mut transparent: Option<u8> = None;
    let mut delay_cs: i64 = 0;
    let mut disposal: u8 = 0;

    while pos < data.len() {
        let block = data[pos];
        if block == 0x21 {
            // extension
            let label = at(data, pos + 1).ok_or_else(|| malformed("index out of range"))?;
            pos += 2;
            if label == 0xF9 && at(data, pos).ok_or_else(|| malformed("index out of range"))? >= 4 {
                let gflags = at(data, pos + 1).ok_or_else(|| malformed("index out of range"))?;
                disposal = (gflags >> 2) & 0x07;
                delay_cs = le16(data, pos + 2).unwrap_or(0) as i64;
                if gflags & 0x01 != 0 {
                    transparent =
                        Some(at(data, pos + 4).ok_or_else(|| malformed("index out of range"))?);
                }
            } else if label == 0xFF {
                loops = netscape_loops(data, pos).unwrap_or(loops);
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
            pos = pos.saturating_add(1); // the block terminator
            let expected = (w as usize).saturating_mul(h as usize);
            let mut indices = lzw(&chunks, min_code, expected)?;
            if iflags & 0x40 != 0 {
                indices = deinterlace(&indices, w as usize, h as usize);
            }
            // A frame that means to put back what was underneath it needs a
            // copy of that taken before it draws, not after.
            let saved = if disposal == 3 {
                Some(canvas.clone())
            } else {
                None
            };
            gif_blit(
                &mut canvas, screen_w, screen_h, &indices, left, top, w, h, table, transparent,
            );
            // Every frame is a screen-sized copy, so a file with a large
            // canvas and a great many frames is a decompression bomb with a
            // palette. Same ceiling as the one inflate answers to.
            if frames.len().saturating_add(1).saturating_mul(canvas.len()) > MAX_INFLATED {
                return Err(bad("animated GIF expands too far"));
            }
            frames.push(GifFrame {
                rgba: canvas.clone(),
                delay_ms: delay_cs.saturating_mul(10),
            });
            if let Some(max) = limit {
                if frames.len() >= max {
                    break;
                }
            }
            match disposal {
                2 => gif_clear(&mut canvas, screen_w, screen_h, left, top, w, h),
                3 => {
                    if let Some(previous) = saved {
                        canvas = previous;
                    }
                }
                _ => {}
            }
            transparent = None;
            delay_cs = 0;
            disposal = 0;
        } else if block == 0x3B {
            break; // trailer
        } else {
            return Err(bad(format!("unexpected GIF block 0x{:02X}", block)));
        }
    }
    if frames.is_empty() {
        return Err(bad("GIF contains no image"));
    }
    Ok((screen_w, screen_h, frames, loops))
}

/// The loop count out of a NETSCAPE2.0 application extension, if this is one.
///
/// `pos` is the first sub-block's length byte, just past the 0xFF label. The
/// extension is eleven bytes of identifier followed by a three-byte
/// sub-block: a 1, then the count little-endian. Anything else shaped like an
/// application extension -- XMP, ImageMagick's own -- is not this and is left
/// to `skip_blocks`.
fn netscape_loops(data: &[u8], pos: usize) -> Option<i64> {
    if at(data, pos)? != 11 || slice(data, pos + 1, pos + 12) != b"NETSCAPE2.0" {
        return None;
    }
    let sub = pos + 12;
    if at(data, sub)? < 3 || at(data, sub + 1)? != 1 {
        return None;
    }
    Some(le16(data, sub + 2)? as i64)
}

/// Draw one sub-image onto the screen, clipped, skipping the index the file
/// declared transparent -- which is what leaves the previous frame showing
/// through, and is the whole of how a GIF animates without storing every
/// pixel every time.
#[allow(clippy::too_many_arguments)]
fn gif_blit(
    canvas: &mut [u8],
    screen_w: i64,
    screen_h: i64,
    indices: &[u8],
    left: i64,
    top: i64,
    w: i64,
    h: i64,
    table: &[u8],
    transparent: Option<u8>,
) {
    for row in 0..h {
        let y = top + row;
        if y < 0 || y >= screen_h {
            continue;
        }
        for col in 0..w {
            let x = left + col;
            if x < 0 || x >= screen_w {
                continue;
            }
            let idx = match indices.get((row.saturating_mul(w) + col) as usize) {
                Some(v) => *v,
                None => continue,
            };
            if Some(idx) == transparent {
                continue;
            }
            let o = idx as usize * 3;
            let d = ((y.saturating_mul(screen_w) + x) as usize).saturating_mul(4);
            if d + 3 >= canvas.len() {
                continue;
            }
            // A palette index past the end of the table draws black and
            // opaque, which is what the still decoder has always done.
            if o + 2 < table.len() {
                canvas[d] = table[o];
                canvas[d + 1] = table[o + 1];
                canvas[d + 2] = table[o + 2];
            } else {
                canvas[d] = 0;
                canvas[d + 1] = 0;
                canvas[d + 2] = 0;
            }
            canvas[d + 3] = 255;
        }
    }
}

/// Disposal method 2: put the frame's own rectangle back to transparent.
fn gif_clear(canvas: &mut [u8], screen_w: i64, screen_h: i64, left: i64, top: i64, w: i64, h: i64) {
    for row in 0..h {
        let y = top + row;
        if y < 0 || y >= screen_h {
            continue;
        }
        for col in 0..w {
            let x = left + col;
            if x < 0 || x >= screen_w {
                continue;
            }
            let d = ((y.saturating_mul(screen_w) + x) as usize).saturating_mul(4);
            if d + 3 < canvas.len() {
                canvas[d..d + 4].copy_from_slice(&[0, 0, 0, 0]);
            }
        }
    }
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

// -- JPEG ------------------------------------------------------------------

// Coefficients arrive in zig-zag order and have to be put back on the 8x8
// grid before the transform sees them.
const ZIGZAG: [usize; 64] = [
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27,
    20, 13, 6, 7, 14, 21, 28, 35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58,
    59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
];

// The inverse transform is the AAN factorisation, the one libjpeg's float
// IDCT uses. It costs five multiplications per one-dimensional pass where a
// matrix multiply costs sixty-four, and the constants it leaves behind fold
// into the quantisation table -- which is why a table is scaled once, when it
// is read, and never again.
const AAN: [f64; 8] = [
    1.0,
    1.387_039_845,
    1.306_562_965,
    1.175_875_602,
    1.0,
    0.785_694_958,
    0.541_196_100,
    0.275_899_379,
];

/// The transform's own clamp, and the reason it is a mask rather than a
/// bound. Valid data lands within a few hundred of the sample range, but
/// corrupt data is not bounded at all; the offset of 2048 puts every
/// plausible sample on the right value and the mask puts an implausible one
/// on *some* value rather than saturating a whole block to black or white.
/// libjpeg masks its range-limit table for the same reason.
fn range_limit(v: f64) -> u8 {
    ((trunc_to_byte(v) & 4095) - 2048).clamp(0, 255) as u8
}

fn signature_jpeg(data: &[u8]) -> bool {
    data.len() >= 2 && data[0] == 0xFF && data[1] == 0xD8
}

/// Short input, which for a photograph off the network is ordinary rather
/// than exceptional. Anything past the frame header decodes to as much of
/// the picture as arrived; anything before it has nothing to draw.
fn truncated() -> PyErr {
    malformed("JPEG ended inside its headers")
}

/// One channel of the frame, and everything decoding it needs.
struct Component {
    cid: u8,
    h: usize,
    v: usize,
    tq: usize,
    /// Blocks across and down, padded out to whole MCUs.
    bw: usize,
    bh: usize,
    /// Blocks that cover picture rather than padding, which is what a scan
    /// carrying this component alone is counted in.
    cols: usize,
    rows: usize,
    coefs: Vec<i32>,
    plane: Vec<u8>,
    stride: usize,
    pred: i32,
}

/// The entropy-coded bits of one scan.
///
/// A scan is split at its restart markers and unstuffed before any of it is
/// decoded, so this walks plain bytes and never has to watch for 0xFF --
/// worth having, because reading a byte is the thing the decoder does most.
/// Running off the end yields zero bits instead of failing, which is what
/// makes a photograph whose download was cut short decode to as much of
/// itself as arrived.
struct Bits {
    chunks: Vec<Vec<u8>>,
    index: usize,
    pos: usize,
    /// At most 23 live bits, so the shift below cannot reach the top of the
    /// word: a fill only runs while there are fewer than 16, and adds 8.
    bits: u32,
    nbits: u32,
}

impl Bits {
    fn new(chunks: Vec<Vec<u8>>) -> Bits {
        Bits { chunks, index: 0, pos: 0, bits: 0, nbits: 0 }
    }

    /// Continue with the bits after the next restart marker.
    fn restart(&mut self) {
        self.index += 1;
        self.pos = 0;
        self.bits = 0;
        self.nbits = 0;
    }

    fn byte(&self) -> u32 {
        match self.chunks.get(self.index) {
            Some(chunk) => chunk.get(self.pos).copied().unwrap_or(0) as u32,
            None => 0,
        }
    }

    /// The next `count` bits, most significant first. `count` is never more
    /// than 16, which every caller checks before it gets here.
    fn take(&mut self, count: u32) -> u32 {
        while self.nbits < count {
            self.bits = (self.bits << 8) | self.byte();
            self.pos += 1;
            self.nbits += 8;
        }
        self.nbits -= count;
        let out = self.bits >> self.nbits;
        self.bits &= (1 << self.nbits) - 1;
        out
    }

    /// One Huffman symbol, through the flat lookup built below.
    fn huffman(&mut self, table: &[u16]) -> PyResult<u8> {
        while self.nbits < 16 {
            self.bits = (self.bits << 8) | self.byte();
            self.pos += 1;
            self.nbits += 8;
        }
        let entry = table[((self.bits >> (self.nbits - 16)) & 0xFFFF) as usize];
        if entry == 0 {
            return Err(bad("bad Huffman code in JPEG scan"));
        }
        self.nbits -= (entry >> 8) as u32;
        self.bits &= (1 << self.nbits) - 1;
        Ok((entry & 0xFF) as u8)
    }
}

/// Expand a JPEG Huffman table into one flat lookup on 16 bits.
///
/// A code is at most 16 bits long, so a table indexed by the next 16 bits of
/// the stream answers any of them in a single subscript. The obvious
/// alternative -- walking the code lengths a bit at a time -- is the
/// innermost loop in the decoder and runs millions of times per photograph,
/// which is the difference between slow and unusable. Each entry is the code
/// length in the high byte and the symbol in the low one, and a zero entry is
/// a code the file never defined.
fn build_huffman(counts: &[u8], symbols: &[u8]) -> PyResult<Vec<u16>> {
    let mut table = vec![0u16; 65536];
    let mut code: usize = 0;
    let mut k = 0;
    for length in 1..=16usize {
        let span = 1usize << (16 - length);
        for _ in 0..counts[length - 1] {
            let symbol = match symbols.get(k) {
                Some(s) => *s as u16,
                None => return Err(bad("truncated JPEG Huffman table")),
            };
            let start = code * span;
            if start + span > 65536 {
                return Err(bad("over-long JPEG Huffman code"));
            }
            table[start..start + span].fill(((length as u16) << 8) | symbol);
            code += 1;
            k += 1;
        }
        code <<= 1;
    }
    Ok(table)
}

/// JPEG's sign convention: the low half of each magnitude class is negative,
/// and the encoder wrote it with no sign bit.
fn extend(value: u32, count: u32) -> i32 {
    let value = value as i32;
    if value < 1 << (count - 1) {
        value - (1 << count) + 1
    } else {
        value
    }
}

/// A magnitude class wide enough to be a table the file never meant. Twelve
/// bits is the most a legal 8-bit frame uses; the check exists because a
/// hostile table can name any of 256 symbols and the bit reader would be
/// asked for a shift the machine does not have.
fn magnitude(size: u8) -> PyResult<u32> {
    if size > 16 {
        return Err(bad(format!("bad JPEG magnitude category {}", size)));
    }
    Ok(size as u32)
}

/// Index of the code byte of the next marker at or after `pos`.
fn next_marker(data: &[u8], mut pos: usize) -> PyResult<usize> {
    let end = data.len();
    while pos < end && data[pos] != 0xFF {
        pos += 1;
    }
    while pos < end && data[pos] == 0xFF {
        pos += 1; // a run of 0xFF bytes is legal fill before the code
    }
    if pos >= end {
        return Err(bad("JPEG ended in the middle of a marker"));
    }
    Ok(pos)
}

/// Split a scan's entropy-coded bytes at its restart markers.
///
/// Returns the chunks, each already unstuffed, and the offset of whatever
/// marker ended the scan. Within a chunk every 0xFF is followed by a 0x00 the
/// encoder inserted, so undoing that is one pass.
fn scan_chunks(data: &[u8], pos: usize) -> (Vec<Vec<u8>>, usize) {
    fn unstuff(run: &[u8]) -> Vec<u8> {
        let mut out = Vec::with_capacity(run.len());
        let mut i = 0;
        while i < run.len() {
            out.push(run[i]);
            i += if run[i] == 0xFF && run.get(i + 1) == Some(&0x00) { 2 } else { 1 };
        }
        out
    }

    let mut chunks = Vec::new();
    let end = data.len();
    let mut start = pos;
    let mut i = pos;
    let mut stopped = false;
    while i + 1 < end {
        if data[i] != 0xFF {
            i += 1;
        } else if data[i + 1] == 0x00 {
            i += 2;
        } else if data[i + 1] == 0xFF {
            i += 1;
        } else if (0xD0..=0xD7).contains(&data[i + 1]) {
            chunks.push(unstuff(&data[start..i]));
            i += 2;
            start = i;
        } else {
            stopped = true;
            break;
        }
    }
    if !stopped {
        i = end;
    }
    chunks.push(unstuff(slice(data, start, i)));
    (chunks, i)
}

fn read_quant(seg: &[u8], quant: &mut [Option<Vec<f64>>; 4]) -> PyResult<()> {
    let mut pos = 0;
    while pos + 64 <= seg.len() {
        let precision = seg[pos] >> 4;
        let index = (seg[pos] & 15) as usize;
        pos += 1;
        if index > 3 {
            return Err(bad(format!("bad JPEG quantisation table index {}", index)));
        }
        let mut table = vec![0.0f64; 64];
        for i in 0..64 {
            let value = if precision != 0 {
                let v = be16(seg, pos).ok_or_else(truncated)? as f64;
                pos += 2;
                v
            } else {
                let v = at(seg, pos).ok_or_else(truncated)? as f64;
                pos += 1;
                v
            };
            // Scaled here, once, so the transform multiplies straight
            // through: dequantisation and the AAN's constants are one step.
            let z = ZIGZAG[i];
            table[z] = value * AAN[z >> 3] * AAN[z & 7] / 8.0;
        }
        quant[index] = Some(table);
    }
    Ok(())
}

fn read_huffman(
    seg: &[u8],
    dc_tables: &mut [Option<Vec<u16>>; 4],
    ac_tables: &mut [Option<Vec<u16>>; 4],
) -> PyResult<()> {
    let mut pos = 0;
    while pos + 17 <= seg.len() {
        let table_class = seg[pos] >> 4;
        let index = (seg[pos] & 15) as usize;
        let counts = &seg[pos + 1..pos + 17];
        let total: usize = counts.iter().map(|c| *c as usize).sum();
        let symbols = slice(seg, pos + 17, pos + 17 + total);
        pos += 17 + total;
        if index > 3 || table_class > 1 {
            return Err(bad(format!(
                "bad JPEG Huffman table id {}/{}",
                table_class, index
            )));
        }
        let table = build_huffman(counts, symbols)?;
        if table_class != 0 {
            ac_tables[index] = Some(table);
        } else {
            dc_tables[index] = Some(table);
        }
    }
    Ok(())
}

/// The SOF segment: what the picture is, before any of it is decoded.
fn read_frame(seg: &[u8]) -> PyResult<(i64, i64, Vec<Component>)> {
    let precision = at(seg, 0).ok_or_else(truncated)?;
    let count = at(seg, 5).ok_or_else(truncated)? as usize;
    let height = be16(seg, 1).ok_or_else(truncated)? as i64;
    let width = be16(seg, 3).ok_or_else(truncated)? as i64;
    if precision != 8 {
        return Err(bad(format!(
            "unsupported JPEG sample precision {}",
            precision
        )));
    }
    if count != 1 && count != 3 {
        // Four components is CMYK or YCCK, which needs Adobe's inverted-ink
        // convention on top of everything here; two is not a thing.
        return Err(bad(format!("unsupported JPEG with {} components", count)));
    }
    if height == 0 {
        // The height is allowed to arrive in a DNL marker after the scan.
        // Almost nothing writes one, and guessing is worse than saying so.
        return Err(bad("JPEG does not declare its height"));
    }
    check_size(width, height)?;
    let mut comps = Vec::with_capacity(count);
    for i in 0..count {
        let cid = at(seg, 6 + 3 * i).ok_or_else(truncated)?;
        let sampling = at(seg, 7 + 3 * i).ok_or_else(truncated)?;
        let tq = at(seg, 8 + 3 * i).ok_or_else(truncated)? as usize;
        let (h, v) = ((sampling >> 4) as usize, (sampling & 15) as usize);
        if !(1..=4).contains(&h) || !(1..=4).contains(&v) {
            return Err(bad(format!("bad JPEG sampling factors {}x{}", h, v)));
        }
        if tq > 3 {
            return Err(bad(format!("bad JPEG quantisation table index {}", tq)));
        }
        comps.push(Component {
            cid,
            h,
            v,
            tq,
            bw: 0,
            bh: 0,
            cols: 0,
            rows: 0,
            coefs: Vec::new(),
            plane: Vec::new(),
            stride: 0,
            pred: 0,
        });
    }
    Ok((width, height, comps))
}

fn ceil_div(value: usize, divisor: usize) -> usize {
    value / divisor + usize::from(value % divisor != 0)
}

/// Give every component its block grid and somewhere to decode into.
///
/// The grid is padded out to whole MCUs, because that is how the encoder
/// wrote it; the padding lies outside the picture and is never read back.
fn plan(width: usize, height: usize, comps: &mut [Component]) -> PyResult<(usize, usize, usize, usize)> {
    let hmax = comps.iter().map(|c| c.h).max().unwrap_or(1);
    let vmax = comps.iter().map(|c| c.v).max().unwrap_or(1);
    let mcux = ceil_div(width, 8 * hmax);
    let mcuy = ceil_div(height, 8 * vmax);
    for comp in comps.iter_mut() {
        comp.bw = mcux * comp.h;
        comp.bh = mcuy * comp.v;
        comp.cols = ceil_div(ceil_div(width * comp.h, hmax), 8);
        comp.rows = ceil_div(ceil_div(height * comp.v, vmax), 8);
        // Bounded by the picture area a component covers, which check_size
        // has already capped, but the multiplication is done in checked
        // arithmetic because the sampling factors come off the network.
        let cells = comp
            .bw
            .checked_mul(comp.bh)
            .and_then(|n| n.checked_mul(64))
            .ok_or_else(|| bad("JPEG block grid is too large"))?;
        comp.coefs = vec![0i32; cells];
    }
    Ok((hmax, vmax, mcux, mcuy))
}

/// Which component of the frame this entry of a scan header names, and the
/// tables it decodes that component with.
struct ScanComp {
    ci: usize,
    dc: usize,
    ac: usize,
}

/// The SOS segment: which components this scan carries, which tables it
/// decodes them with, and which coefficients it is bringing.
fn read_scan_header(
    seg: &[u8],
    comps: &[Component],
    progressive: bool,
) -> PyResult<(Vec<ScanComp>, usize, usize, u32, u32)> {
    let count = at(seg, 0).ok_or_else(truncated)? as usize;
    let mut scan = Vec::with_capacity(count);
    for i in 0..count {
        let cid = at(seg, 1 + 2 * i).ok_or_else(truncated)?;
        let tables = at(seg, 2 + 2 * i).ok_or_else(truncated)?;
        let ci = match comps.iter().position(|c| c.cid == cid) {
            Some(ci) => ci,
            None => {
                return Err(bad(format!(
                    "JPEG scan names component {}, which the frame does not have",
                    cid
                )))
            }
        };
        scan.push(ScanComp { ci, dc: (tables >> 4) as usize, ac: (tables & 15) as usize });
    }
    let (mut ss, mut se) = (
        at(seg, 1 + 2 * count).ok_or_else(truncated)? as usize,
        at(seg, 2 + 2 * count).ok_or_else(truncated)? as usize,
    );
    let approx = at(seg, 3 + 2 * count).ok_or_else(truncated)?;
    let (mut ah, mut al) = ((approx >> 4) as u32, (approx & 15) as u32);
    if !progressive {
        ss = 0;
        se = 63;
        ah = 0;
        al = 0;
    }
    if ss > se || se > 63 {
        return Err(bad(format!(
            "bad JPEG spectral selection {}..{}",
            ss, se
        )));
    }
    if al > 13 {
        // A shift wider than the coefficients are is a corrupt header, not a
        // picture; the arithmetic below would be undefined rather than wrong.
        return Err(bad(format!("bad JPEG successive approximation {}", al)));
    }
    Ok((scan, ss, se, ah, al))
}

/// A Huffman table a scan header named, which the file has to have sent.
fn table<'a>(tables: &'a [Option<Vec<u16>>; 4], index: usize) -> PyResult<&'a [u16]> {
    tables
        .get(index)
        .and_then(|t| t.as_deref())
        .ok_or_else(|| bad("JPEG scan wants a Huffman table that is not in the file"))
}

/// A whole sequential scan: every coefficient of every block it covers.
fn decode_sequential(
    bits: &mut Bits,
    scan: &[ScanComp],
    comps: &mut [Component],
    dc_tables: &[Option<Vec<u16>>; 4],
    ac_tables: &[Option<Vec<u16>>; 4],
    restart: usize,
    mcux: usize,
    mcuy: usize,
) -> PyResult<()> {
    if scan.len() == 1 {
        let (ci, dct, act) = (scan[0].ci, table(dc_tables, scan[0].dc)?, table(ac_tables, scan[0].ac)?);
        let (rows, cols, bw) = (comps[ci].rows, comps[ci].cols, comps[ci].bw);
        for row in 0..rows {
            for col in 0..cols {
                let unit = row * cols + col;
                if restart != 0 && unit != 0 && unit % restart == 0 {
                    bits.restart();
                    comps[ci].pred = 0;
                }
                decode_block(bits, &mut comps[ci], dct, act, (row * bw + col) * 64)?;
            }
        }
        return Ok(());
    }
    for unit in 0..mcux * mcuy {
        if restart != 0 && unit != 0 && unit % restart == 0 {
            bits.restart();
            for entry in scan {
                comps[entry.ci].pred = 0;
            }
        }
        let (my, mx) = (unit / mcux, unit % mcux);
        for entry in scan {
            let (dct, act) = (table(dc_tables, entry.dc)?, table(ac_tables, entry.ac)?);
            let comp = &mut comps[entry.ci];
            let (h, v, bw) = (comp.h, comp.v, comp.bw);
            for by in 0..v {
                let row = my * v + by;
                for bx in 0..h {
                    decode_block(bits, comp, dct, act, (row * bw + mx * h + bx) * 64)?;
                }
            }
        }
    }
    Ok(())
}

/// One 8x8 block: a DC difference, then runs of AC coefficients.
fn decode_block(
    bits: &mut Bits,
    comp: &mut Component,
    dc_table: &[u16],
    ac_table: &[u16],
    base: usize,
) -> PyResult<()> {
    let size = magnitude(bits.huffman(dc_table)?)?;
    if size != 0 {
        comp.pred = comp.pred.wrapping_add(extend(bits.take(size), size));
    }
    comp.coefs[base] = comp.pred;
    let mut k = 1usize;
    while k < 64 {
        let rs = bits.huffman(ac_table)?;
        let size = (rs & 15) as u32;
        if size == 0 {
            if rs != 0xF0 {
                break; // end of block
            }
            k += 16; // sixteen zeroes and no coefficient
            continue;
        }
        k += (rs >> 4) as usize;
        if k > 63 {
            break;
        }
        comp.coefs[base + ZIGZAG[k]] = extend(bits.take(size), size);
        k += 1;
    }
    Ok(())
}

/// One progressive scan.
///
/// A progressive file sends the same coefficients over several scans: a band
/// at a time (spectral selection, `ss` to `se`) and the high bits before the
/// low ones (successive approximation, down to bit `al`). Every scan lands in
/// the same coefficient array and nothing is drawn until all of them have
/// been read, so this is progressive decoding without progressive display.
#[allow(clippy::too_many_arguments)]
fn decode_progressive(
    bits: &mut Bits,
    scan: &[ScanComp],
    comps: &mut [Component],
    dc_tables: &[Option<Vec<u16>>; 4],
    ac_tables: &[Option<Vec<u16>>; 4],
    restart: usize,
    mcux: usize,
    mcuy: usize,
    ss: usize,
    se: usize,
    ah: u32,
    al: u32,
) -> PyResult<()> {
    let mut eobrun: u32 = 0; // the end-of-band run, which carries across blocks
    if ss == 0 && scan.len() > 1 {
        for unit in 0..mcux * mcuy {
            if restart != 0 && unit != 0 && unit % restart == 0 {
                bits.restart();
                for entry in scan {
                    comps[entry.ci].pred = 0;
                }
            }
            let (my, mx) = (unit / mcux, unit % mcux);
            for entry in scan {
                let comp = &mut comps[entry.ci];
                let (h, v, bw) = (comp.h, comp.v, comp.bw);
                for by in 0..v {
                    let row = my * v + by;
                    for bx in 0..h {
                        let base = (row * bw + mx * h + bx) * 64;
                        if ah != 0 {
                            dc_refine(bits, comp, base, al);
                        } else {
                            dc_first(bits, comp, table(dc_tables, entry.dc)?, base, al)?;
                        }
                    }
                }
            }
        }
        return Ok(());
    }
    // Everything else is one component, counted in that component's blocks.
    let entry = &scan[0];
    let comp = &mut comps[entry.ci];
    let (rows, cols, bw) = (comp.rows, comp.cols, comp.bw);
    for row in 0..rows {
        for col in 0..cols {
            let unit = row * cols + col;
            if restart != 0 && unit != 0 && unit % restart == 0 {
                bits.restart();
                eobrun = 0;
                comp.pred = 0;
            }
            let base = (row * bw + col) * 64;
            if ss == 0 {
                if ah != 0 {
                    dc_refine(bits, comp, base, al);
                } else {
                    dc_first(bits, comp, table(dc_tables, entry.dc)?, base, al)?;
                }
            } else if ah != 0 {
                ac_refine(bits, comp, table(ac_tables, entry.ac)?, base, ss, se, al, &mut eobrun)?;
            } else {
                ac_first(bits, comp, table(ac_tables, entry.ac)?, base, ss, se, al, &mut eobrun)?;
            }
        }
    }
    Ok(())
}

/// A DC refinement carries one raw bit per block and no Huffman code.
fn dc_refine(bits: &mut Bits, comp: &mut Component, base: usize, al: u32) {
    if bits.take(1) != 0 {
        comp.coefs[base] |= 1 << al;
    }
}

fn dc_first(
    bits: &mut Bits,
    comp: &mut Component,
    dc_table: &[u16],
    base: usize,
    al: u32,
) -> PyResult<()> {
    let size = magnitude(bits.huffman(dc_table)?)?;
    if size != 0 {
        comp.pred = comp.pred.wrapping_add(extend(bits.take(size), size));
    }
    comp.coefs[base] = comp.pred.wrapping_shl(al);
    Ok(())
}

/// The first pass over a band: the same runs a sequential scan uses, plus an
/// end-of-band run that can skip whole blocks at a time.
#[allow(clippy::too_many_arguments)]
fn ac_first(
    bits: &mut Bits,
    comp: &mut Component,
    ac_table: &[u16],
    base: usize,
    ss: usize,
    se: usize,
    al: u32,
    eobrun: &mut u32,
) -> PyResult<()> {
    if *eobrun != 0 {
        *eobrun -= 1;
        return Ok(());
    }
    let mut k = ss;
    while k <= se {
        let rs = bits.huffman(ac_table)?;
        let (run, size) = ((rs >> 4) as usize, (rs & 15) as u32);
        if size == 0 {
            if run != 15 {
                *eobrun = (1 << run) - 1;
                if run != 0 {
                    *eobrun += bits.take(run as u32);
                }
                return Ok(());
            }
            k += 16;
            continue;
        }
        k += run;
        if k > se {
            return Ok(());
        }
        comp.coefs[base + ZIGZAG[k]] = extend(bits.take(size), size) << al;
        k += 1;
    }
    Ok(())
}

/// The correction pass over a band, which is the fiddly one.
///
/// Coefficients that are already nonzero take one bit each, in stream order,
/// saying whether they gain this pass's bit; the runs between them are
/// counted only over coefficients that are still zero, and a coefficient that
/// arrives in a refinement pass can only ever be plus or minus one bit.
#[allow(clippy::too_many_arguments)]
fn ac_refine(
    bits: &mut Bits,
    comp: &mut Component,
    ac_table: &[u16],
    base: usize,
    ss: usize,
    se: usize,
    al: u32,
    eobrun: &mut u32,
) -> PyResult<()> {
    let plus = 1i32 << al;
    let minus = -1i32 << al;
    let mut k = ss;
    if *eobrun == 0 {
        while k <= se {
            let rs = bits.huffman(ac_table)?;
            let (mut run, size) = ((rs >> 4) as i32, (rs & 15) as u32);
            let mut value = 0i32;
            if size == 0 {
                if run != 15 {
                    // The current block is part of this run and its remaining
                    // coefficients still take correction bits, so the run is
                    // counted down at the bottom rather than here.
                    *eobrun = 1 << run;
                    if run != 0 {
                        *eobrun += bits.take(run as u32);
                    }
                    break;
                }
            } else {
                value = if bits.take(1) != 0 { plus } else { minus };
            }
            while k <= se {
                let index = base + ZIGZAG[k];
                let coef = comp.coefs[index];
                if coef != 0 {
                    if bits.take(1) != 0 && coef & plus == 0 {
                        comp.coefs[index] = coef + if coef > 0 { plus } else { minus };
                    }
                } else {
                    run -= 1;
                    if run < 0 {
                        break;
                    }
                }
                k += 1;
            }
            if value != 0 && k <= se {
                comp.coefs[base + ZIGZAG[k]] = value;
            }
            k += 1;
        }
    }
    if *eobrun != 0 {
        while k <= se {
            let index = base + ZIGZAG[k];
            let coef = comp.coefs[index];
            if coef != 0 && bits.take(1) != 0 && coef & plus == 0 {
                comp.coefs[index] = coef + if coef > 0 { plus } else { minus };
            }
            k += 1;
        }
        *eobrun -= 1;
    }
    Ok(())
}

/// Dequantise and transform every block of one component.
///
/// What comes out is that component's samples, a byte each, on the grid
/// padded to whole blocks; the colour conversion reads only the part of it
/// the picture covers.
fn idct_plane(comp: &mut Component, quant: &[f64]) {
    const SQRT2: f64 = 1.414_213_562;
    let stride = comp.bw * 8;
    let mut plane = vec![0u8; stride * comp.bh * 8];
    let mut work = [0.0f64; 64];
    for by in 0..comp.bh {
        let top = by * 8 * stride;
        for bx in 0..comp.bw {
            let base = (by * comp.bw + bx) * 64;
            let block = &comp.coefs[base..base + 64];
            if block[1..].iter().all(|c| *c == 0) {
                // A flat block is the common case in any photograph's sky or
                // shadow, and it needs no transform at all.
                let value = range_limit(block[0] as f64 * quant[0] + 2176.5);
                let mut row = top + bx * 8;
                for _ in 0..8 {
                    plane[row..row + 8].fill(value);
                    row += stride;
                }
                continue;
            }

            // Columns first, into the workspace, then rows out of it.
            for i in 0..8 {
                if block[i + 8] == 0
                    && block[i + 16] == 0
                    && block[i + 24] == 0
                    && block[i + 32] == 0
                    && block[i + 40] == 0
                    && block[i + 48] == 0
                    && block[i + 56] == 0
                {
                    let value = block[i] as f64 * quant[i];
                    for j in 0..8 {
                        work[i + j * 8] = value;
                    }
                    continue;
                }
                let t0 = block[i] as f64 * quant[i];
                let t1 = block[i + 16] as f64 * quant[i + 16];
                let t2 = block[i + 32] as f64 * quant[i + 32];
                let t3 = block[i + 48] as f64 * quant[i + 48];
                let t10 = t0 + t2;
                let t11 = t0 - t2;
                let t13 = t1 + t3;
                let t12 = (t1 - t3) * SQRT2 - t13;
                let (t0, t3) = (t10 + t13, t10 - t13);
                let (t1, t2) = (t11 + t12, t11 - t12);
                let t4 = block[i + 8] as f64 * quant[i + 8];
                let t5 = block[i + 24] as f64 * quant[i + 24];
                let t6 = block[i + 40] as f64 * quant[i + 40];
                let t7 = block[i + 56] as f64 * quant[i + 56];
                let z13 = t6 + t5;
                let z10 = t6 - t5;
                let z11 = t4 + t7;
                let z12 = t4 - t7;
                let t7 = z11 + z13;
                let t11 = (z11 - z13) * SQRT2;
                let z5 = (z10 + z12) * 1.847_759_065;
                let t10 = 1.082_392_200 * z12 - z5;
                let t12 = -2.613_125_930 * z10 + z5;
                let t6 = t12 - t7;
                let t5 = t11 - t6;
                let t4 = t10 + t5;
                work[i] = t0 + t7;
                work[i + 56] = t0 - t7;
                work[i + 8] = t1 + t6;
                work[i + 48] = t1 - t6;
                work[i + 16] = t2 + t5;
                work[i + 40] = t2 - t5;
                work[i + 32] = t3 + t4;
                work[i + 24] = t3 - t4;
            }

            let mut out = top + bx * 8;
            for i in (0..64).step_by(8) {
                let t0 = work[i];
                let t1 = work[i + 2];
                let t2 = work[i + 4];
                let t3 = work[i + 6];
                let t10 = t0 + t2;
                let t11 = t0 - t2;
                let t13 = t1 + t3;
                let t12 = (t1 - t3) * SQRT2 - t13;
                let (t0, t3) = (t10 + t13, t10 - t13);
                let (t1, t2) = (t11 + t12, t11 - t12);
                let t4 = work[i + 1];
                let t5 = work[i + 3];
                let t6 = work[i + 5];
                let t7 = work[i + 7];
                let z13 = t6 + t5;
                let z10 = t6 - t5;
                let z11 = t4 + t7;
                let z12 = t4 - t7;
                let t7 = z11 + z13;
                let t11 = (z11 - z13) * SQRT2;
                let z5 = (z10 + z12) * 1.847_759_065;
                let t10 = 1.082_392_200 * z12 - z5;
                let t12 = -2.613_125_930 * z10 + z5;
                let t6 = t12 - t7;
                let t5 = t11 - t6;
                let t4 = t10 + t5;
                plane[out] = range_limit(t0 + t7 + 2176.5);
                plane[out + 7] = range_limit(t0 - t7 + 2176.5);
                plane[out + 1] = range_limit(t1 + t6 + 2176.5);
                plane[out + 6] = range_limit(t1 - t6 + 2176.5);
                plane[out + 2] = range_limit(t2 + t5 + 2176.5);
                plane[out + 5] = range_limit(t2 - t5 + 2176.5);
                plane[out + 4] = range_limit(t3 + t4 + 2176.5);
                plane[out + 3] = range_limit(t3 - t4 + 2176.5);
                out += stride;
            }
        }
    }
    comp.plane = plane;
    comp.stride = stride;
    comp.coefs = Vec::new(); // the coefficients are spent, and they are large
}

/// How one component's samples are stretched back to the picture's size.
enum Upsample {
    /// Not subsampled: the row is already the right length.
    Full,
    /// Any sampling factor that is not a plain halving. libjpeg does not
    /// filter those either, and they are rare enough on the web that the
    /// difference has never been worth measuring.
    Nearest { xmap: Vec<usize>, v: usize, vmax: usize },
    /// Halved across, and possibly down as well.
    Triangle { halved: bool, cols: usize, rows: usize },
}

fn upsample_for(comp: &Component, width: usize, height: usize, hmax: usize, vmax: usize) -> Upsample {
    if comp.h == hmax && comp.v == vmax {
        Upsample::Full
    } else if comp.h * 2 != hmax || (comp.v != vmax && comp.v * 2 != vmax) {
        Upsample::Nearest {
            xmap: (0..width).map(|x| x * comp.h / hmax).collect(),
            v: comp.v,
            vmax,
        }
    } else {
        Upsample::Triangle {
            halved: comp.v != vmax,
            cols: ceil_div(width * comp.h, hmax),
            rows: ceil_div(height * comp.v, vmax),
        }
    }
}

/// Write one full-width row of one component's samples.
///
/// A component halved in either direction is put back with the triangle
/// filter: the sample nearer the output pixel counts three times and the one
/// on the far side once. Replicating the sample instead is cheaper and
/// visibly wrong -- measured over seventy-seven photographs off the web it
/// moves a channel by as much as 87 levels away from what libjpeg produces,
/// on the saturated edges where the eye reads it as a coloured fringe.
///
/// The rounding is deliberately lopsided, and libjpeg leans the two patterns
/// opposite ways: when only the columns were halved the left of each output
/// pair rounds down and the right rounds up, and when both were halved it is
/// the other way about. Copying one bias to both costs an extra level on a
/// quarter of the pixels, so each pattern keeps the constants it was written
/// with.
fn upsample_row(comp: &Component, mode: &Upsample, y: usize, out: &mut [u8], col: &mut Vec<u32>) {
    let (plane, stride) = (&comp.plane, comp.stride);
    match mode {
        Upsample::Full => {
            let row = y * stride;
            out.copy_from_slice(&plane[row..row + out.len()]);
        }
        Upsample::Nearest { xmap, v, vmax } => {
            let row = (y * v / vmax) * stride;
            for (d, x) in out.iter_mut().zip(xmap) {
                *d = plane[row + x];
            }
        }
        Upsample::Triangle { halved, cols, rows } => {
            let (cols, rows) = (*cols, *rows);
            let near = if *halved { y / 2 } else { y }.min(rows - 1);
            // At the top and bottom edges, and whenever only the columns were
            // halved, the far row is the near one and the sum below comes to
            // four times the sample -- which is the scale the horizontal pass
            // wants either way, so neither case needs a branch of its own.
            let far = if *halved {
                if y & 1 == 1 { (near + 1).min(rows - 1) } else { near.saturating_sub(1) }
            } else {
                near
            };
            let this = &plane[near * stride..near * stride + cols];
            let other = &plane[far * stride..far * stride + cols];
            col.clear();
            col.extend(this.iter().zip(other).map(|(a, b)| 3 * *a as u32 + *b as u32));
            let (roundl, roundr) = if *halved { (8u32, 7u32) } else { (4u32, 8u32) };
            for i in 0..cols {
                let middle = 3 * col[i];
                let left = (middle + col[i.saturating_sub(1)] + roundl) >> 4;
                let right = (middle + col[(i + 1).min(cols - 1)] + roundr) >> 4;
                if let Some(d) = out.get_mut(i * 2) {
                    *d = left as u8;
                }
                if let Some(d) = out.get_mut(i * 2 + 1) {
                    *d = right as u8;
                }
            }
        }
    }
}

/// Upsample the subsampled channels and convert to RGBA.
fn jpeg_to_rgba(
    width: usize,
    height: usize,
    comps: &[Component],
    hmax: usize,
    vmax: usize,
    ycbcr: bool,
) -> Vec<u8> {
    let mut rgba = vec![0u8; width * height * 4];
    let modes: Vec<Upsample> =
        comps.iter().map(|c| upsample_for(c, width, height, hmax, vmax)).collect();
    let mut rows: Vec<Vec<u8>> = comps.iter().map(|_| vec![0u8; width]).collect();
    let mut scratch: Vec<Vec<u32>> = comps.iter().map(|_| Vec::new()).collect();

    for y in 0..height {
        for (i, comp) in comps.iter().enumerate() {
            upsample_row(comp, &modes[i], y, &mut rows[i], &mut scratch[i]);
        }
        let mut d = y * width * 4;
        if comps.len() == 1 {
            for x in 0..width {
                let grey = rows[0][x];
                rgba[d] = grey;
                rgba[d + 1] = grey;
                rgba[d + 2] = grey;
                rgba[d + 3] = 255;
                d += 4;
            }
            continue;
        }
        for x in 0..width {
            if ycbcr {
                let luma = rows[0][x] as i32;
                let cb = rows[1][x] as i32 - 128;
                let cr = rows[2][x] as i32 - 128;
                rgba[d] = (luma + ((91881 * cr + 32768) >> 16)).clamp(0, 255) as u8;
                rgba[d + 1] =
                    (luma + ((-22554 * cb - 46802 * cr + 32768) >> 16)).clamp(0, 255) as u8;
                rgba[d + 2] = (luma + ((116130 * cb + 32768) >> 16)).clamp(0, 255) as u8;
            } else {
                // An Adobe file with transform 0, or one whose components are
                // labelled R, G and B: the samples are already RGB.
                rgba[d] = rows[0][x];
                rgba[d + 1] = rows[1][x];
                rgba[d + 2] = rows[2][x];
            }
            rgba[d + 3] = 255;
            d += 4;
        }
    }
    rgba
}

/// Decode a Huffman-coded baseline, extended-sequential or progressive JPEG.
/// Every other member of the family fails rather than guessing.
fn jpeg(data: &[u8]) -> PyResult<(i64, i64, Vec<u8>)> {
    if !signature_jpeg(data) {
        return Err(bad("not a JPEG"));
    }
    let mut quant: [Option<Vec<f64>>; 4] = [None, None, None, None];
    let mut dc_tables: [Option<Vec<u16>>; 4] = [None, None, None, None];
    let mut ac_tables: [Option<Vec<u16>>; 4] = [None, None, None, None];
    let mut comps: Option<Vec<Component>> = None;
    let (mut width, mut height) = (0i64, 0i64);
    let (mut hmax, mut vmax, mut mcux, mut mcuy) = (1usize, 1usize, 1usize, 1usize);
    let mut progressive = false;
    let mut restart = 0usize;
    let mut transform: Option<u8> = None;
    let mut pos = 2usize;
    while pos < data.len() {
        pos = next_marker(data, pos)?;
        let marker = data[pos];
        pos += 1;
        if marker == 0xD9 {
            break; // end of image
        }
        if marker == 0x01 || (0xD0..=0xD7).contains(&marker) {
            continue; // no payload of their own
        }
        let length = match be16(data, pos) {
            Some(length) => length as usize,
            None => break,
        };
        let seg = slice(data, pos + 2, pos + length);
        let mut end = pos + length;
        match marker {
            0xDB => read_quant(seg, &mut quant)?,
            0xC4 => read_huffman(seg, &mut dc_tables, &mut ac_tables)?,
            0xDD => restart = be16(seg, 0).ok_or_else(truncated)? as usize,
            0xEE if seg.starts_with(b"Adobe") => transform = at(seg, 11),
            0xC0 | 0xC1 | 0xC2 => {
                progressive = marker == 0xC2;
                let (w, h, mut planned) = read_frame(seg)?;
                let (a, b, c, d) = plan(w as usize, h as usize, &mut planned)?;
                width = w;
                height = h;
                hmax = a;
                vmax = b;
                mcux = c;
                mcuy = d;
                comps = Some(planned);
            }
            0xCC => return Err(bad("arithmetic-coded JPEG is not supported")),
            0xC0..=0xCF if marker != 0xC8 => {
                return Err(bad(format!(
                    "unsupported JPEG mode (SOF{})",
                    marker - 0xC0
                )))
            }
            0xDA => {
                let comps = match comps.as_mut() {
                    Some(comps) => comps,
                    None => return Err(bad("JPEG scan before its frame header")),
                };
                let (scan, ss, se, ah, al) = read_scan_header(seg, comps, progressive)?;
                if scan.is_empty() {
                    return Err(bad("JPEG scan carries no components"));
                }
                let (chunks, next) = scan_chunks(data, end);
                end = next;
                let mut bits = Bits::new(chunks);
                for entry in &scan {
                    comps[entry.ci].pred = 0;
                }
                if progressive {
                    decode_progressive(
                        &mut bits, &scan, comps, &dc_tables, &ac_tables, restart, mcux, mcuy,
                        ss, se, ah, al,
                    )?;
                } else {
                    decode_sequential(
                        &mut bits, &scan, comps, &dc_tables, &ac_tables, restart, mcux, mcuy,
                    )?;
                }
            }
            _ => {}
        }
        pos = end;
    }
    let mut comps = match comps {
        Some(comps) => comps,
        None => return Err(bad("JPEG has no frame header")),
    };

    for comp in comps.iter_mut() {
        let table = match quant[comp.tq].clone() {
            Some(table) => table,
            None => {
                return Err(bad(format!(
                    "JPEG component wants quantisation table {}, which is not in the file",
                    comp.tq
                )))
            }
        };
        idct_plane(comp, &table);
    }
    // Three components are YCbCr unless the file says otherwise: Adobe's
    // marker with transform 0, or components labelled R, G and B.
    let ycbcr = comps.len() == 3
        && transform != Some(0)
        && comps.iter().map(|c| c.cid).collect::<Vec<u8>>() != vec![b'R', b'G', b'B'];
    let rgba = jpeg_to_rgba(width as usize, height as usize, &comps, hmax, vmax, ycbcr);
    Ok((width, height, rgba))
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
