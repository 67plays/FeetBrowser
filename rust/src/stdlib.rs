//! The JavaScript standard library globals, ported from
//! `jsengine.py::Interpreter.__init__` (and its native ctors).

use crate::interp::*;
use crate::value::*;
use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::rc::Rc;
use std::sync::atomic::{AtomicUsize, Ordering};

// -- helpers ---------------------------------------------------------------

fn first(args: &[JsValue]) -> JsValue {
    args.first().cloned().unwrap_or(JsValue::Undefined)
}

fn first_num(args: &[JsValue]) -> f64 {
    to_number(&first(args))
}

fn native(name: &str, call: NativeFn) -> JsValue {
    JsValue::Native(Rc::new(Native {
        name: Rc::from(name),
        call: Some(call),
        ctor: None,
        get: None,
        set: None,
        method_of: None,
    }))
}

fn getter(
    name: &str,
    get: NativeGet,
) -> JsValue {
    JsValue::Native(Rc::new(Native {
        name: Rc::from(name),
        call: None,
        ctor: None,
        get: Some(get),
        set: None,
        method_of: None,
    }))
}

fn ctor(name: &str, call: NativeFn, ctor_: NativeFn, get: Option<NativeGet>, set: Option<NativeSet>) -> JsValue {
    JsValue::Native(Rc::new(Native {
        name: Rc::from(name),
        call: Some(call),
        ctor: Some(ctor_),
        get,
        set,
        method_of: None,
    }))
}

// -- base64 (btoa/atob) -----------------------------------------------------

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

fn base64_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = chunk.get(1).copied().unwrap_or(0) as u32;
        let b2 = chunk.get(2).copied().unwrap_or(0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(B64[(n >> 18) as usize & 63] as char);
        out.push(B64[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { B64[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { B64[n as usize & 63] as char } else { '=' });
    }
    out
}

fn base64_char(c: u8) -> Option<u32> {
    match c {
        b'A'..=b'Z' => Some((c - b'A') as u32),
        b'a'..=b'z' => Some((c - b'a') as u32 + 26),
        b'0'..=b'9' => Some((c - b'0') as u32 + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

fn base64_decode(text: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(text.len() * 3 / 4);
    let mut acc: u32 = 0;
    let mut bits = 0;
    for c in text.bytes() {
        if c == b'=' {
            break;
        }
        let Some(v) = base64_char(c) else { continue };
        acc = (acc << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8 & 0xFF);
        }
    }
    out
}

// -- percent-encoding (encodeURI/decodeURI/...) ------------------------------

fn is_unreserved(c: u8) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, b'-' | b'_' | b'.' | b'!' | b'~' | b'*' | b'\'' | b'(' | b')')
}

/// Characters encodeURI leaves alone in addition to the unreserved set.
fn is_uri_reserved(c: u8) -> bool {
    matches!(c, b';' | b',' | b'/' | b'?' | b':' | b'@' | b'&' | b'=' | b'+' | b'$' | b'#')
}

fn percent_encode(text: &str, component: bool) -> String {
    let mut out = String::new();
    for b in text.as_bytes() {
        if is_unreserved(*b) || (!component && is_uri_reserved(*b)) {
            out.push(*b as char);
        } else {
            out.push_str(&format!("%{:02X}", b));
        }
    }
    out
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hi = (bytes[i + 1] as char).to_digit(16);
            let lo = (bytes[i + 2] as char).to_digit(16);
            if let (Some(h), Some(l)) = (hi, lo) {
                out.push((h * 16 + l) as u8);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    // Decode UTF-8 byte runs to characters (invalid bytes stay as-is).
    String::from_utf8_lossy(&out).into_owned()
}

// -- console / window / localStorage ---------------------------------------

fn console_log(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let line = args
            .iter()
            .map(|a| this.repr(a))
            .collect::<Vec<_>>()
            .join(" ");
        this.logs.borrow_mut().push(line);
        Ok(JsValue::Undefined)
    })
}

fn window_get(this: &Rc<Interpreter>, _obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    Ok(this
        .globals
        .borrow()
        .get(name)
        .cloned()
        .unwrap_or(JsValue::Undefined))
}

fn window_set(this: &Rc<Interpreter>, _obj: &JsValue, name: &str, value: &JsValue) -> Result<(), JsError> {
    this.globals.borrow_mut().insert(name.to_string(), value.clone());
    Ok(())
}

fn ls_get_item(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let k = this.repr(&first(&args));
        Ok(match this.local_storage.borrow().get(&k) {
            Some(v) => JsValue::str(v.clone()),
            None => JsValue::Null,
        })
    })
}

fn ls_set_item(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let k = this.repr(&args[0]);
        let v = this.repr(&args[1]);
        this.local_storage.borrow_mut().insert(k, v);
        Ok(JsValue::Undefined)
    })
}

fn ls_remove_item(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let k = this.repr(&first(&args));
        this.local_storage.borrow_mut().remove(&k);
        Ok(JsValue::Undefined)
    })
}

fn ls_clear(this: &Rc<Interpreter>, _obj: &JsValue, _args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        this.local_storage.borrow_mut().clear();
        Ok(JsValue::Undefined)
    })
}

fn ls_key(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let idx = to_int32(&first(&args));
        let keys: Vec<String> = this.local_storage.borrow().keys().cloned().collect();
        if idx >= 0 && (idx as usize) < keys.len() {
            Ok(JsValue::str(keys[idx as usize].clone()))
        } else {
            Ok(JsValue::Null)
        }
    })
}

fn ls_length(this: &Rc<Interpreter>, _obj: &JsValue, _args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        Ok(JsValue::Number(this.local_storage.borrow().len() as f64))
    })
}

fn ls_get(_this: &Rc<Interpreter>, _obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    Ok(match name {
        "getItem" => native("getItem", ls_get_item),
        "setItem" => native("setItem", ls_set_item),
        "removeItem" => native("removeItem", ls_remove_item),
        "clear" => native("clear", ls_clear),
        "key" => native("key", ls_key),
        "length" => native("length", ls_length),
        _ => JsValue::Undefined,
    })
}

fn ls_set(this: &Rc<Interpreter>, _obj: &JsValue, name: &str, value: &JsValue) -> Result<(), JsError> {
    this.local_storage
        .borrow_mut()
        .insert(name.to_string(), this.repr(value));
    Ok(())
}

// -- conversions ------------------------------------------------------------

fn string_call(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let s = match args.first() {
            Some(v) => this.repr(v),
            None => String::new(),
        };
        Ok(JsValue::str(s))
    })
}

fn string_from_char_code(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let mut out = String::new();
        for c in args {
            let v = to_int32(&c).max(0) as u32;
            out.push(char::from_u32(v).unwrap_or('\u{FFFD}'));
        }
        Ok(JsValue::str(out))
    })
}

fn string_from_code_point(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let mut out = String::new();
        for c in args {
            let v = to_int32(&c) as u32;
            out.push(char::from_u32(v).unwrap_or('\u{FFFD}'));
        }
        Ok(JsValue::str(out))
    })
}

fn string_raw(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        // The pieces to join are the ones on the `raw` property, not the array
        // itself: that is the entire difference between `String.raw` and
        // interpolating the template normally, and it is why every Windows
        // path and every regex source written as `` String.raw`\d+` `` comes
        // out with its backslashes intact. A caller handing over a plain array
        // with no `raw` -- which happens, `String.raw` gets used as an ordinary
        // function -- falls back to the array.
        let parts: Vec<String> = match args.first() {
            Some(JsValue::Array(a)) => {
                let raw = a.props.borrow().get("raw").cloned();
                match raw {
                    Some(JsValue::Array(r)) => r.borrow().iter().map(|v| this.repr(v)).collect(),
                    _ => a.borrow().iter().map(|v| this.repr(v)).collect(),
                }
            }
            _ => vec![],
        };
        let subs = &args[1..];
        let mut out = parts.first().cloned().unwrap_or_default();
        for (i, s) in subs.iter().enumerate() {
            out.push_str(&this.repr(s));
            if i + 1 < parts.len() {
                out.push_str(&parts[i + 1]);
            }
        }
        Ok(JsValue::str(out))
    })
}

fn string_get(_this: &Rc<Interpreter>, _obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    Ok(match name {
        "fromCharCode" => native("fromCharCode", string_from_char_code),
        "fromCodePoint" => native("fromCodePoint", string_from_code_point),
        "raw" => native("raw", string_raw),
        _ => JsValue::Undefined,
    })
}

fn number_call(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        Ok(JsValue::Number(match args.first() {
            Some(v) => to_number(v),
            None => 0.0,
        }))
    })
}

fn number_is_nan(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let v = first(&args);
        Ok(JsValue::Bool(matches!(v, JsValue::Number(n) if n.is_nan())))
    })
}

fn number_is_finite(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let v = first(&args);
        Ok(JsValue::Bool(matches!(v, JsValue::Number(n) if !n.is_nan() && !n.is_infinite())))
    })
}

fn number_get(_this: &Rc<Interpreter>, _obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    Ok(match name {
        "isNaN" => native("isNaN", number_is_nan),
        "isFinite" => native("isFinite", number_is_finite),
        "isInteger" => native("isInteger", number_is_integer),
        "isSafeInteger" => native("isSafeInteger", number_is_safe_integer),
        "EPSILON" => JsValue::Number(f64::EPSILON),
        "parseInt" => native("parseInt", parse_int_call),
        "parseFloat" => native("parseFloat", parse_float_call),
        "MAX_VALUE" => JsValue::Number(1.7976931348623157e308),
        "MIN_VALUE" => JsValue::Number(5e-324),
        "MAX_SAFE_INTEGER" => JsValue::Number(9_007_199_254_740_991.0),
        "MIN_SAFE_INTEGER" => JsValue::Number(-9_007_199_254_740_991.0),
        "POSITIVE_INFINITY" => JsValue::Number(f64::INFINITY),
        "NEGATIVE_INFINITY" => JsValue::Number(f64::NEG_INFINITY),
        _ => JsValue::Undefined,
    })
}

fn boolean_call(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let v = first(&args);
    Box::pin(async move { Ok(JsValue::Bool(truthy(&v))) })
}

fn parse_int_call(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let text = match args.first() {
            Some(v) => this.repr(v),
            None => String::new(),
        };
        let text = text.trim_start();
        let hexp = text.to_ascii_lowercase().starts_with("0x");
        let radix_arg = args.get(1).cloned().unwrap_or(JsValue::Undefined);
        let radix: Option<i32> = if nullish(&radix_arg) {
            None
        } else {
            Some(to_int32(&radix_arg))
        };
        let mut base = if radix.is_none() && hexp {
            16
        } else if let Some(r) = radix {
            r
        } else {
            10
        };
        if base == 0 {
            base = if hexp {
                16
            } else if text.starts_with('0') && text.len() > 1 {
                8
            } else {
                10
            };
        }
        if base < 2 || base > 36 {
            return Ok(JsValue::Number(f64::NAN));
        }
        let prefix_len = if base == 16 && hexp { 2 } else { 0 };
        let digits_src = "0123456789abcdefghijklmnopqrstuvwxyz";
        let valid: Vec<char> = digits_src[..base as usize].chars().collect();
        let mut digits = 0usize;
        for ch in text[prefix_len..].chars() {
            if valid.contains(&ch.to_ascii_lowercase()) {
                digits += 1;
            } else {
                break;
            }
        }
        if digits == 0 {
            return Ok(JsValue::Number(f64::NAN));
        }
        let int_str = &text[..prefix_len + digits];
        let sign = if int_str.starts_with('-') { -1.0 } else { 1.0 };
        let clean = int_str.trim_start_matches(['+', '-']);
        let clean = if base == 16 && hexp {
            clean.get(2..).unwrap_or(clean)
        } else {
            clean
        };
        let val = i64::from_str_radix(clean, base as u32).unwrap_or(i64::MAX) as f64;
        Ok(JsValue::Number(sign * val))
    })
}

fn parse_float_call(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let text = match args.first() {
            Some(v) => this.repr(v),
            None => String::new(),
        };
        let text = text.trim().to_string();
        match float_prefix(&text) {
            Some(tok) => {
                if tok.trim_start_matches(['+', '-']).eq_ignore_ascii_case("infinity") {
                    let sign = if tok.starts_with('-') { -1.0 } else { 1.0 };
                    Ok(JsValue::Number(sign * f64::INFINITY))
                } else {
                    Ok(JsValue::Number(tok.parse().unwrap_or(f64::NAN)))
                }
            }
            None => Ok(JsValue::Number(f64::NAN)),
        }
    })
}

/// The longest prefix of `text` that reads as a decimal literal, which is what
/// `parseFloat` is defined in terms of: it takes as much as it understands and
/// ignores the rest, so `parseFloat("3.5px")` is 3.5 and `parseFloat("px")` is
/// NaN. Written out by hand rather than pattern-matched, because the project
/// takes no third-party crates and its own regexp engine has no business being
/// dragged into number parsing.
fn float_prefix(text: &str) -> Option<&str> {
    let b = text.as_bytes();
    let mut i = 0usize;
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i += 1;
    }
    // "Infinity" is a literal here, spelled in full but accepted in any case.
    // `get` rather than a byte slice, so a multibyte character crossing the
    // eighth-byte boundary cannot panic the string slicing.
    if text.get(i..i + 8).map_or(false, |s| s.eq_ignore_ascii_case("infinity")) {
        return Some(&text[..i + 8]);
    }
    let mut digits = 0usize;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
        digits += 1;
    }
    if i < b.len() && b[i] == b'.' {
        i += 1;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
            digits += 1;
        }
    }
    if digits == 0 {
        return None;
    }
    // An exponent only counts when it is complete: "1e" is the number 1
    // followed by junk, not a malformed literal.
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        let mut j = i + 1;
        if j < b.len() && (b[j] == b'+' || b[j] == b'-') {
            j += 1;
        }
        let start = j;
        while j < b.len() && b[j].is_ascii_digit() {
            j += 1;
        }
        if j > start {
            i = j;
        }
    }
    Some(&text[..i])
}

// -- base64 ----------------------------------------------------------------

fn btoa_call(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let text = this.repr(&first(&args));
        // btoa operates on code units 0..=255; mask higher values (lenient).
        let bytes: Vec<u8> = text.encode_utf16().map(|u| u as u8).collect();
        Ok(JsValue::str(base64_encode(&bytes)))
    })
}

fn atob_call(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let text = this.repr(&first(&args));
        let decoded = base64_decode(&text);
        // atob returns a "binary string": one Latin-1 char per byte.
        let out: String = decoded.iter().map(|b| *b as char).collect();
        Ok(JsValue::str(out))
    })
}

// -- URI encoding ----------------------------------------------------------

fn encode_uri_component(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        Ok(JsValue::str(percent_encode(&this.repr(&first(&args)), true)))
    })
}

fn encode_uri(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        Ok(JsValue::str(percent_encode(&this.repr(&first(&args)), false)))
    })
}

fn decode_uri_component(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        Ok(JsValue::str(percent_decode(&this.repr(&first(&args)))))
    })
}

fn decode_uri(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        Ok(JsValue::str(percent_decode(&this.repr(&first(&args)))))
    })
}

// -- isNaN / isFinite ------------------------------------------------------

fn global_is_nan(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let _ = this;
    Box::pin(async move { Ok(JsValue::Bool(to_number(&first(&args)).is_nan())) })
}

fn global_is_finite(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let _ = this;
    Box::pin(async move {
        let n = to_number(&first(&args));
        Ok(JsValue::Bool(!n.is_nan() && !n.is_infinite()))
    })
}

// -- Number.isInteger ------------------------------------------------------

/// An integer small enough that no other integer shares its double. Code that
/// asks is usually about to use the value as an id or an array index, and the
/// answer it wants is "yes" for the ordinary numbers and "no" for the ones
/// that have quietly stopped being exact.
fn number_is_safe_integer(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let _ = this;
    Box::pin(async move {
        let v = first(&args);
        Ok(JsValue::Bool(match v {
            JsValue::Number(n) => {
                n.fract() == 0.0 && n.abs() <= 9_007_199_254_740_991.0
            }
            _ => false,
        }))
    })
}

fn number_is_integer(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let _ = this;
    Box::pin(async move {
        let v = first(&args);
        match v {
            JsValue::Number(n) => Ok(JsValue::Bool(n.fract() == 0.0 && !n.is_nan() && !n.is_infinite())),
            _ => Ok(JsValue::Bool(false)),
        }
    })
}

// -- Array / Object --------------------------------------------------------

fn array_call(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        if args.len() == 1 {
            if let Some(JsValue::Number(n)) = args.first() {
                if n.is_finite() && n.fract() == 0.0 && *n >= 0.0 {
                    let length = *n as usize;
                    if length > MAX_ARRAY_LEN {
                        return Err(JsError::js(format!(
                            "Array length {length} exceeds the allowed maximum"
                        )));
                    }
                    return Ok(JsValue::array(vec![JsValue::Undefined; length]));
                }
            }
        }
        let _ = &this;
        Ok(JsValue::array(args))
    })
}

fn array_is_array(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let v = first(&args);
    Box::pin(async move { Ok(JsValue::Bool(matches!(v, JsValue::Array(_)))) })
}

/// `Array.from(src)` and `Array.from(src, fn)`. Three shapes reach it in
/// practice and all three have to work: something already iterable (an array,
/// a string, a Set), the `arguments`-style array-like that only has a
/// `length` and numeric keys, and the same again with a mapping function --
/// `Array.from(nodeList, n => n.id)` is how half the DOM code in the wild
/// turns a live collection into something it can `map` over.
fn array_from(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        // `iterate` knows all three shapes -- iterable, iterator protocol, and
        // the `length`-and-numeric-keys array-like -- and knowing them in one
        // place is what keeps `Array.from(x)` and `[...x]` from disagreeing.
        // Strings come apart by code point there, not by byte or by UTF-16
        // unit, which is most of why anyone reaches for `Array.from` on one.
        let v = first(&args);
        let items: Vec<JsValue> = iterate(&this, &v).await?.unwrap_or_default();
        let f = args.get(1).cloned().unwrap_or(JsValue::Undefined);
        if !nullish(&f) && !is_js_function(&f) {
            // The spec throws a TypeError when a mapping function is given
            // but is not callable; silently ignoring it would hide a caller
            // bug (`Array.from(x, 3)`).
            return Err(JsError::Thrown(JsValue::Error(Rc::new(RefCell::new(
                JsHostError {
                    message: "Array.from: the mapping function is not callable"
                        .to_string(),
                    name: "TypeError".to_string(),
                },
            )))));
        }
        if !is_js_function(&f) {
            return Ok(JsValue::array(items));
        }
        let mut out = Vec::with_capacity(items.len());
        for (i, item) in items.into_iter().enumerate() {
            out.push(
                call_value(
                    &this,
                    &f,
                    vec![item, JsValue::Number(i as f64)],
                    JsValue::Undefined,
                )
                .await?,
            );
        }
        Ok(JsValue::array(out))
    })
}

/// `Array.of(1, 2, 3)`. It exists because `Array(3)` means "three empty slots"
/// rather than "the array [3]", and there has to be one spelling that does not
/// change meaning when it is handed exactly one number.
fn array_of(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move { Ok(JsValue::array(args)) })
}

/// Every array method that can sensibly be borrowed by something that is not
/// an array.
///
/// The list is closed rather than open on purpose. Handing back a function for
/// any name at all would make `if (!Array.prototype.includes) { …polyfill… }`
/// skip a polyfill we do not actually have, which turns a missing method into
/// a wrong answer much later on -- feature detection has to be allowed to
/// fail.
const ARRAY_PROTO_METHODS: &[&str] = &[
    "at", "concat", "every", "fill", "filter", "find", "findIndex", "flat", "flatMap", "forEach",
    "includes", "indexOf", "join", "lastIndexOf", "map", "pop", "push", "reduce", "reduceRight",
    "reverse", "shift", "slice", "some", "sort", "splice", "toString", "unshift",
];

/// Coerce a borrowed receiver into a real array.
///
/// `Array.prototype.slice.call(arguments)` is the oldest idiom in the language
/// and the reason this exists: the thing on the left is almost never an array,
/// it is `arguments`, a `NodeList`, or a string, and all of those answer
/// `length` and numeric keys. An actual array is passed through by reference
/// so that the mutating methods still mutate it.
fn array_like(this: &Rc<Interpreter>, v: &JsValue) -> Result<Rc<JsArray>, JsError> {
    if let JsValue::Array(a) = v {
        return Ok(a.clone());
    }
    let items: Vec<JsValue> = match v {
        JsValue::Str(s) => s.chars().map(|c| JsValue::str(c.to_string())).collect(),
        JsValue::Undefined | JsValue::Null => vec![],
        other => {
            let n = to_number(&js_get(this, other, "length")?);
            if n.is_nan() || n <= 0.0 {
                vec![]
            } else {
                let n = n.min(MAX_ARRAY_LEN as f64) as usize;
                let mut out = Vec::with_capacity(n);
                for i in 0..n {
                    out.push(js_get(this, other, &i.to_string())?);
                }
                out
            }
        }
    };
    Ok(Rc::new(JsArray::new(items)))
}

/// `Array.prototype.<method>`, as a function that takes its receiver first.
///
/// That argument order is not a shortcut, it is the convention every native
/// here follows: `make_method_wrapper` prepends the `this` argument for
/// natives and callbacks, so `f.call(x, y)` arrives as `[x, y]`, which is
/// exactly the shape a borrowed method wants anyway.
fn array_proto_get(_this: &Rc<Interpreter>, _obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    if !ARRAY_PROTO_METHODS.contains(&name) {
        return Ok(JsValue::Undefined);
    }
    let name = name.to_string();
    Ok(JsValue::Callback(Rc::new(
        move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
            let name = name.clone();
            let i2 = i.clone();
            Box::pin(async move {
                let recv = args.first().cloned().unwrap_or(JsValue::Undefined);
                let rest: Vec<JsValue> = args.into_iter().skip(1).collect();
                let list = array_like(&i2, &recv)?;
                let m = list_get(&i2, &list, &name);
                call_value(&i2, &m, rest, JsValue::Array(list)).await
            })
        },
    )))
}

fn array_get(_this: &Rc<Interpreter>, _obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    Ok(match name {
        "isArray" => native("isArray", array_is_array),
        "from" => native("from", array_from),
        "of" => native("of", array_of),
        "prototype" => getter("prototype", array_proto_get),
        _ => JsValue::Undefined,
    })
}

fn object_call(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let v = first(&args);
        Ok(match v {
            JsValue::Undefined | JsValue::Null => JsValue::object(),
            JsValue::Object(_) => v,
            _ => JsValue::object(),
        })
    })
}

fn obj_keys(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let _this = this.clone();
    Box::pin(async move {
        let v = first(&args);
        // Own keys only, and `__proto__` is a link rather than a property, so
        // it never appears -- listing it would have every `for (k in o)` in a
        // page walk into the prototype as if it were data.
        let keys: Vec<String> = match v {
            JsValue::Object(m) => m.borrow().keys().filter(|k| *k != "__proto__").cloned().collect(),
            JsValue::Instance(i) => i.borrow().props.borrow().keys().cloned().collect(),
            JsValue::Array(a) => (0..a.borrow().len()).map(|i| i.to_string()).collect(),
            _ => vec![],
        };
        Ok(JsValue::array(keys.into_iter().map(JsValue::str).collect()))
    })
}

fn obj_values(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let v = first(&args);
        Ok(match v {
            JsValue::Object(m) => JsValue::array(
                m.borrow()
                    .iter()
                    .filter(|(k, _)| k.as_str() != "__proto__")
                    .map(|(_, v)| v.clone())
                    .collect(),
            ),
            JsValue::Instance(i) => {
                JsValue::array(i.borrow().props.borrow().values().cloned().collect())
            }
            JsValue::Array(a) => JsValue::array(a.borrow().clone()),
            _ => JsValue::array(vec![]),
        })
    })
}

fn obj_entries(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let v = first(&args);
        Ok(match v {
            JsValue::Object(m) => JsValue::array(
                m.borrow()
                    .iter()
                    .map(|(k, val)| JsValue::array(vec![JsValue::str(k.clone()), val.clone()]))
                    .collect(),
            ),
            _ => JsValue::array(vec![]),
        })
    })
}

fn obj_assign(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let mut out = BTreeMap::new();
        for o in args {
            if let JsValue::Object(m) = o {
                for (k, v) in m.borrow().iter() {
                    out.insert(k.clone(), v.clone());
                }
            }
        }
        Ok(JsValue::Object(Rc::new(RefCell::new(out))))
    })
}

fn obj_create(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let proto = first(&args);
        let proto_map = match proto {
            JsValue::Object(m) => m,
            _ => Rc::new(RefCell::new(BTreeMap::new())),
        };
        Ok(JsValue::Instance(Rc::new(RefCell::new(JsClassInstance {
            proto: proto_map,
            props: Rc::new(RefCell::new(BTreeMap::new())),
        }))))
    })
}

fn obj_get_proto_of(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let v = first(&args);
        Ok(match v {
            JsValue::Instance(i) => JsValue::Object(i.borrow().proto.clone()),
            // A plain object wears its link as a `__proto__` entry, the same
            // one the lookup walks.
            JsValue::Object(m) => m.borrow().get("__proto__").cloned().unwrap_or(JsValue::Null),
            JsValue::Function(f) => JsValue::Object(JSFunction::prototype_obj(&f)),
            _ => JsValue::Undefined,
        })
    })
}

fn obj_set_proto_of(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        if args.len() >= 2 {
            match (&args[0], &args[1]) {
                (JsValue::Instance(i), JsValue::Object(m)) => i.borrow_mut().proto = m.clone(),
                // Re-parenting a plain object is the same operation written
                // one level down: leave the link where the lookup will find it.
                (JsValue::Object(o), JsValue::Object(m)) => {
                    o.borrow_mut()
                        .insert("__proto__".to_string(), JsValue::Object(m.clone()));
                }
                (JsValue::Object(o), JsValue::Instance(i)) => {
                    let (own, parent) = {
                        let b = i.borrow();
                        (b.props.clone(), b.proto.clone())
                    };
                    own.borrow_mut()
                        .insert("__proto__".to_string(), JsValue::Object(parent));
                    o.borrow_mut()
                        .insert("__proto__".to_string(), JsValue::Object(own));
                }
                (JsValue::Instance(o), JsValue::Instance(i)) => {
                    let (own, parent) = {
                        let b = i.borrow();
                        (b.props.clone(), b.proto.clone())
                    };
                    own.borrow_mut()
                        .insert("__proto__".to_string(), JsValue::Object(parent));
                    o.borrow_mut().proto = own;
                }
                _ => {}
            }
        }
        Ok(first(&args))
    })
}

/// One property, described the long way round. A descriptor is either a data
/// one (`value`) or an accessor one (`get`/`set`); the accessor form used to be
/// stored as the getter function itself, so reading the property handed back
/// the function instead of calling it -- which is the difference between
/// `el.length` being 3 and being `function`. `JsValue::Accessor` is the same
/// thing object literals build for `get x()`, so writing one here means both
/// spellings of the same idea end up as the same value.
fn define_one(map: &Rc<RefCell<BTreeMap<String, JsValue>>>, key: String, desc: &JsValue) {
    let desc = match desc {
        JsValue::Object(d) => d,
        _ => return,
    };
    let get = desc.borrow().get("get").cloned();
    let set = desc.borrow().get("set").cloned();
    let has_accessor = get.as_ref().is_some_and(is_js_function)
        || set.as_ref().is_some_and(is_js_function);
    if has_accessor {
        let acc = Rc::new(JsAccessor::default());
        *acc.get.borrow_mut() = get.filter(is_js_function);
        *acc.set.borrow_mut() = set.filter(is_js_function);
        map.borrow_mut().insert(key, JsValue::Accessor(acc));
        return;
    }
    let value = desc.borrow().get("value").cloned();
    if let Some(v) = value {
        map.borrow_mut().insert(key, v);
    }
}

fn obj_define_property(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        if args.len() >= 3 {
            if let JsValue::Object(map) = &args[0] {
                define_one(map, index_name(&this, &args[1]), &args[2]);
            }
        }
        Ok(args.into_iter().next().unwrap_or(JsValue::Undefined))
    })
}

/// `Object.defineProperties(o, {a: {...}, b: {...}})`. The compiled output of
/// every transpiler that has to emulate a class reaches for this, and Google's
/// own bundles alias it into a one-letter local at the top of the file -- so
/// its absence took out the whole script, not one property.
fn obj_define_properties(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let _ = this;
    Box::pin(async move {
        if args.len() >= 2 {
            if let (JsValue::Object(map), JsValue::Object(descs)) = (&args[0], &args[1]) {
                let pairs: Vec<(String, JsValue)> = descs
                    .borrow()
                    .iter()
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect();
                for (k, d) in pairs {
                    define_one(map, k, &d);
                }
            }
        }
        Ok(args.into_iter().next().unwrap_or(JsValue::Undefined))
    })
}

/// The descriptor for one own property, or `undefined`. Ours are all writable,
/// enumerable and configurable, because nothing here can make them otherwise;
/// reporting that honestly is better than reporting nothing, since the code
/// that asks is usually copying a property from one object to another and
/// needs the shape of the answer more than its finer details.
fn obj_get_own_descriptor(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        if args.len() >= 2 {
            if let JsValue::Object(map) = &args[0] {
                let key = index_name(&this, &args[1]);
                let slot = map.borrow().get(&key).cloned();
                if let Some(slot) = slot {
                    return Ok(descriptor_for(&slot));
                }
            }
        }
        Ok(JsValue::Undefined)
    })
}

fn descriptor_for(slot: &JsValue) -> JsValue {
    let mut d = BTreeMap::new();
    match slot {
        JsValue::Accessor(a) => {
            d.insert(
                "get".to_string(),
                a.get.borrow().clone().unwrap_or(JsValue::Undefined),
            );
            d.insert(
                "set".to_string(),
                a.set.borrow().clone().unwrap_or(JsValue::Undefined),
            );
        }
        other => {
            d.insert("value".to_string(), other.clone());
            d.insert("writable".to_string(), JsValue::Bool(true));
        }
    }
    d.insert("enumerable".to_string(), JsValue::Bool(true));
    d.insert("configurable".to_string(), JsValue::Bool(true));
    JsValue::Object(Rc::new(RefCell::new(d)))
}

fn obj_get_own_descriptors(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let _ = this;
    Box::pin(async move {
        let mut out = BTreeMap::new();
        if let Some(JsValue::Object(map)) = args.first() {
            for (k, v) in map.borrow().iter() {
                out.insert(k.clone(), descriptor_for(v));
            }
        }
        Ok(JsValue::Object(Rc::new(RefCell::new(out))))
    })
}

fn obj_freeze(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let v = first(&args);
    Box::pin(async move { Ok(v) })
}

fn obj_false(_this: &Rc<Interpreter>, _obj: &JsValue, _args: Vec<JsValue>) -> EvResult {
    Box::pin(async move { Ok(JsValue::Bool(false)) })
}

fn obj_true(_this: &Rc<Interpreter>, _obj: &JsValue, _args: Vec<JsValue>) -> EvResult {
    Box::pin(async move { Ok(JsValue::Bool(true)) })
}

fn obj_has_own(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        if args.len() >= 2 {
            if let JsValue::Object(m) = &args[0] {
                let key = index_name(&this, &args[1]);
                return Ok(JsValue::Bool(m.borrow().contains_key(&key)));
            }
        }
        Ok(JsValue::Bool(false))
    })
}

fn obj_is(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let _ = this;
    Box::pin(async move {
        let a = args.first().cloned().unwrap_or(JsValue::Undefined);
        let b = args.get(1).cloned().unwrap_or(JsValue::Undefined);
        match (&a, &b) {
            (JsValue::Number(x), JsValue::Number(y)) => {
                if x.is_nan() && y.is_nan() {
                    return Ok(JsValue::Bool(true));
                }
                if *x == 0.0 && *y == 0.0 && x.is_sign_negative() != y.is_sign_negative() {
                    return Ok(JsValue::Bool(false));
                }
                Ok(JsValue::Bool(x == y))
            }
            _ => Ok(JsValue::Bool(strict_eq(&a, &b))),
        }
    })
}

fn obj_from_entries(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let mut out = BTreeMap::new();
        if let JsValue::Array(items) = first(&args) {
            for pair in items.borrow().iter() {
                if let JsValue::Array(entry) = pair {
                    let e = entry.borrow();
                    let key = index_name(&this, e.first().unwrap_or(&JsValue::Undefined));
                    let val = e.get(1).cloned().unwrap_or(JsValue::Undefined);
                    out.insert(key, val);
                }
            }
        }
        Ok(JsValue::Object(Rc::new(RefCell::new(out))))
    })
}

fn object_proto_has_own(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        if args.len() >= 2 {
            if let JsValue::Object(m) = &args[0] {
                let key = index_name(&this, &args[1]);
                return Ok(JsValue::Bool(m.borrow().contains_key(&key)));
            }
        }
        Ok(JsValue::Bool(false))
    })
}

/// `Object.prototype.toString.call(x)` is not a way to print `x`; it is the
/// oldest type test in the language, and the whole point of it is that it
/// ignores everything the value has been taught to say about itself. Library
/// code still reaches for it in preference to `instanceof` -- a value that
/// crossed a frame boundary fails `instanceof` and passes this -- so it has to
/// answer with the tag, `[object Array]`, and never with the contents.
pub fn object_proto_to_string_native() -> JsValue {
    native("toString", object_proto_to_string)
}

fn object_proto_to_string(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let v = first(&args);
    let tag = match &v {
        JsValue::Undefined => "Undefined",
        JsValue::Null => "Null",
        JsValue::Bool(_) => "Boolean",
        JsValue::Number(_) => "Number",
        JsValue::Str(_) => "String",
        JsValue::Array(_) => "Array",
        JsValue::Date(_) => "Date",
        JsValue::Regex(_) => "RegExp",
        JsValue::Error(_) => "Error",
        JsValue::Map(_) => "Map",
        JsValue::Set(_) => "Set",
        JsValue::Promise(_) => "Promise",
        JsValue::Function(_) | JsValue::Class(_) | JsValue::Native(_) | JsValue::Callback(_) => {
            "Function"
        }
        _ => "Object",
    };
    Box::pin(async move { Ok(JsValue::str(format!("[object {tag}]"))) })
}

fn object_proto_value_of(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let v = first(&args);
    Box::pin(async move { Ok(v) })
}

/// `new Function(...)` and `Function(...)` compile a string, which is the one
/// thing this engine will not do: a page that reaches for it is either
/// building code out of data or feature-testing for a place that forbids it,
/// and the second is far more common than the first. Throwing is what a page
/// with a strict Content-Security-Policy sees, so the surrounding `try` that
/// real code already wraps this in does the right thing with it.
fn function_call(_this: &Rc<Interpreter>, _obj: &JsValue, _args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        Err(JsError::js(
            "Refused to evaluate a string as JavaScript: the Function constructor is disabled",
        ))
    })
}

fn function_get(_this: &Rc<Interpreter>, _obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    Ok(match name {
        "prototype" => function_proto_object(),
        _ => JsValue::Undefined,
    })
}

fn object_get(_this: &Rc<Interpreter>, _obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    Ok(match name {
        "keys" => native("keys", obj_keys),
        "values" => native("values", obj_values),
        "entries" => native("entries", obj_entries),
        "assign" => native("assign", obj_assign),
        "create" => native("create", obj_create),
        "getPrototypeOf" => native("getPrototypeOf", obj_get_proto_of),
        "setPrototypeOf" => native("setPrototypeOf", obj_set_proto_of),
        "defineProperty" => native("defineProperty", obj_define_property),
        "defineProperties" => native("defineProperties", obj_define_properties),
        "getOwnPropertyDescriptor" => {
            native("getOwnPropertyDescriptor", obj_get_own_descriptor)
        }
        "getOwnPropertyDescriptors" => {
            native("getOwnPropertyDescriptors", obj_get_own_descriptors)
        }
        // Nothing here can actually make an object immutable, so `freeze` and
        // friends are the identity and the three questions answer for a world
        // where no one ever froze anything. That is a lie only to code that
        // freezes and then checks, and the truth -- a thrown TypeError on the
        // next write -- would be a worse one.
        "getOwnPropertyNames" => native("getOwnPropertyNames", obj_keys),
        "seal" => native("seal", obj_freeze),
        "preventExtensions" => native("preventExtensions", obj_freeze),
        "isFrozen" => native("isFrozen", obj_false),
        "isSealed" => native("isSealed", obj_false),
        "isExtensible" => native("isExtensible", obj_true),
        "freeze" => native("freeze", obj_freeze),
        "hasOwnProperty" => native("hasOwnProperty", obj_has_own),
        "hasOwn" => native("hasOwn", obj_has_own),
        "is" => native("is", obj_is),
        "fromEntries" => native("fromEntries", obj_from_entries),
        "prototype" => {
            let mut proto = BTreeMap::new();
            proto.insert(
                "hasOwnProperty".to_string(),
                native("hasOwnProperty", object_proto_has_own),
            );
            proto.insert("toString".to_string(), native("toString", object_proto_to_string));
            proto.insert("valueOf".to_string(), native("valueOf", object_proto_value_of));
            JsValue::Object(Rc::new(RefCell::new(proto)))
        }
        _ => JsValue::Undefined,
    })
}

// -- Math / JSON -----------------------------------------------------------

macro_rules! math_unary {
    ($name:ident, $body:expr) => {
        fn $name(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
            Box::pin(async move {
                let x = first_num(&args);
                let _ = this;
                Ok(JsValue::Number($body(x)))
            })
        }
    };
}

math_unary!(math_abs, |x: f64| x.abs());
math_unary!(math_ceil, |x: f64| x.ceil());
math_unary!(math_floor, |x: f64| x.floor());
math_unary!(math_trunc, |x: f64| x.trunc());
math_unary!(math_sqrt, |x: f64| x.sqrt());
math_unary!(math_cbrt, |x: f64| x.cbrt());
math_unary!(math_exp, |x: f64| x.exp());
math_unary!(math_log, |x: f64| x.ln());
math_unary!(math_log2, |x: f64| x.log2());
math_unary!(math_log10, |x: f64| x.log10());
math_unary!(math_sin, |x: f64| x.sin());
math_unary!(math_cos, |x: f64| x.cos());
math_unary!(math_tan, |x: f64| x.tan());
math_unary!(math_asin, |x: f64| x.asin());
math_unary!(math_acos, |x: f64| x.acos());
math_unary!(math_atan, |x: f64| x.atan());
math_unary!(math_sinh, |x: f64| x.sinh());
math_unary!(math_cosh, |x: f64| x.cosh());
math_unary!(math_tanh, |x: f64| x.tanh());

fn math_round(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let x = first_num(&args);
        let r = if x >= 0.0 { (x + 0.5).floor() } else { (x - 0.5).ceil() };
        Ok(JsValue::Number(r))
    })
}

fn math_sign(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let x = first_num(&args);
        let r = if x.is_nan() || x == 0.0 {
            x
        } else if x < 0.0 {
            -1.0
        } else {
            1.0
        };
        Ok(JsValue::Number(r))
    })
}

fn math_pow(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let a = first_num(&args);
        let b = args.get(1).map(to_number).unwrap_or(f64::NAN);
        Ok(JsValue::Number(a.powf(b)))
    })
}

fn math_atan2(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let y = first_num(&args);
        let x = args.get(1).map(to_number).unwrap_or(f64::NAN);
        Ok(JsValue::Number(y.atan2(x)))
    })
}

fn math_hypot(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let s: f64 = args.iter().map(to_number).map(|x| x * x).sum();
        Ok(JsValue::Number(s.sqrt()))
    })
}

fn math_max(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let mut best = f64::NEG_INFINITY;
        for a in args {
            let n = to_number(&a);
            if n > best || n.is_nan() {
                best = n;
            }
        }
        Ok(JsValue::Number(best))
    })
}

fn math_min(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let mut best = f64::INFINITY;
        for a in args {
            let n = to_number(&a);
            if n < best || n.is_nan() {
                best = n;
            }
        }
        Ok(JsValue::Number(best))
    })
}

fn math_random(_this: &Rc<Interpreter>, _obj: &JsValue, _args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        use std::time::{SystemTime, UNIX_EPOCH};
        let t = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);
        let mut x = t ^ 0x9E3779B97F4A7C15;
        x ^= x >> 30;
        x = x.wrapping_mul(0xBF58476D1CE4E5B9);
        x ^= x >> 27;
        x = x.wrapping_mul(0x94D049BB133111EB);
        x ^= x >> 31;
        Ok(JsValue::Number((x >> 11) as f64 / (1u64 << 53) as f64))
    })
}

fn math_fround(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move { Ok(JsValue::Number(first_num(&args))) })
}

fn math_value(name: &str) -> JsValue {
    let call: Option<NativeFn> = Some(match name {
        "abs" => math_abs,
        "ceil" => math_ceil,
        "floor" => math_floor,
        "round" => math_round,
        "trunc" => math_trunc,
        "sign" => math_sign,
        "sqrt" => math_sqrt,
        "cbrt" => math_cbrt,
        "exp" => math_exp,
        "log" => math_log,
        "log2" => math_log2,
        "log10" => math_log10,
        "pow" => math_pow,
        "sin" => math_sin,
        "cos" => math_cos,
        "tan" => math_tan,
        "asin" => math_asin,
        "acos" => math_acos,
        "atan" => math_atan,
        "atan2" => math_atan2,
        "sinh" => math_sinh,
        "cosh" => math_cosh,
        "tanh" => math_tanh,
        "hypot" => math_hypot,
        "max" => math_max,
        "min" => math_min,
        "random" => math_random,
        "fround" => math_fround,
        _ => return JsValue::Undefined,
    });
    JsValue::Native(Rc::new(Native {
        name: Rc::from(name),
        call,
        ctor: None,
        get: None,
        set: None,
        method_of: None,
    }))
}

fn math_const(name: &str) -> JsValue {
    let v = match name {
        "PI" => std::f64::consts::PI,
        "E" => std::f64::consts::E,
        "LN2" => std::f64::consts::LN_2,
        "LN10" => std::f64::consts::LN_10,
        "LOG2E" => std::f64::consts::LOG2_E,
        "LOG10E" => std::f64::consts::LOG10_E,
        "SQRT2" => std::f64::consts::SQRT_2,
        "SQRT1_2" => std::f64::consts::FRAC_1_SQRT_2,
        _ => return JsValue::Undefined,
    };
    JsValue::Number(v)
}

fn json_escape(s: &str) -> String {
    let mut out = String::new();
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0C}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out
}

fn json_stringify_value(this: &Interpreter, v: &JsValue, seen: &mut Vec<usize>) -> Option<String> {
    match v {
        JsValue::Null => Some("null".to_string()),
        JsValue::Undefined => None,
        JsValue::Bool(b) => Some(if *b { "true" } else { "false" }.to_string()),
        JsValue::Number(n) => {
            if n.is_nan() || n.is_infinite() {
                Some("null".to_string())
            } else {
                Some(number_to_string(*n))
            }
        }
        JsValue::Str(s) => Some(format!("\"{}\"", json_escape(s))),
        JsValue::Array(a) => {
            let ptr = Rc::as_ptr(a) as usize;
            if seen.contains(&ptr) {
                return None;
            }
            seen.push(ptr);
            let parts: Vec<String> = a
                .borrow()
                .iter()
                .map(|item| {
                    json_stringify_value(this, item, seen).unwrap_or_else(|| "null".to_string())
                })
                .collect();
            seen.pop();
            Some(format!("[{}]", parts.join(",")))
        }
        JsValue::Object(map) => {
            let ptr = Rc::as_ptr(map) as usize;
            if seen.contains(&ptr) {
                return None;
            }
            seen.push(ptr);
            let mut parts = Vec::new();
            for (k, val) in map.borrow().iter() {
                if let Some(p) = json_stringify_value(this, val, seen) {
                    parts.push(format!("\"{}\":{}", json_escape(k), p));
                }
            }
            seen.pop();
            Some(format!("{{{}}}", parts.join(",")))
        }
        JsValue::Instance(inst) => {
            let ptr = Rc::as_ptr(inst) as usize;
            if seen.contains(&ptr) {
                return None;
            }
            seen.push(ptr);
            let mut parts = Vec::new();
            for (k, val) in inst.borrow().props.borrow().iter() {
                if let Some(p) = json_stringify_value(this, val, seen) {
                    parts.push(format!("\"{}\":{}", json_escape(k), p));
                }
            }
            seen.pop();
            Some(format!("{{{}}}", parts.join(",")))
        }
        _ => None,
    }
}

fn json_stringify(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let value = first(&args);
        let s = json_stringify_value(&this, &value, &mut Vec::new());
        Ok(match s {
            Some(s) => JsValue::str(s),
            None => JsValue::Undefined,
        })
    })
}

fn json_value_to_js(v: &serde_json::Value) -> JsValue {
    match v {
        serde_json::Value::Null => JsValue::Null,
        serde_json::Value::Bool(b) => JsValue::Bool(*b),
        serde_json::Value::Number(n) => JsValue::Number(n.as_f64().unwrap_or(f64::NAN)),
        serde_json::Value::String(s) => JsValue::str(s.clone()),
        serde_json::Value::Array(items) => {
            JsValue::array(items.iter().map(json_value_to_js).collect())
        }
        serde_json::Value::Object(map) => {
            let mut m = BTreeMap::new();
            for (k, val) in map {
                m.insert(k.clone(), json_value_to_js(val));
            }
            JsValue::Object(Rc::new(RefCell::new(m)))
        }
    }
}

fn json_parse(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let text = match args.first() {
            Some(v) => this.repr(v),
            None => String::new(),
        };
        if text.trim().is_empty() {
            return Err(JsError::js("Unexpected end of JSON input"));
        }
        let v: serde_json::Value = serde_json::from_str(&text)
            .map_err(|e| JsError::js(format!("JSON.parse: {e}")))?;
        Ok(json_value_to_js(&v))
    })
}

// -- Error / RegExp / Date -------------------------------------------------

/// Every error constructor, told apart by the name of the native being
/// called. `TypeError` and its siblings differ from `Error` in exactly one
/// visible way -- the `name` they carry -- and library code throws and
/// compares them constantly, so the alternative to having them was a page
/// dying on `new TypeError(...)` with "is not a constructor".
fn error_make(this: &Rc<Interpreter>, obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    let name = match obj {
        JsValue::Native(n) => n.name.to_string(),
        _ => "Error".to_string(),
    };
    Box::pin(async move {
        let msg = match args.first() {
            Some(v) if !nullish(v) => this.repr(v),
            _ => String::new(),
        };
        Ok(JsValue::Error(Rc::new(RefCell::new(JsHostError {
            message: msg,
            name,
        }))))
    })
}

fn make_regexp(this: &Interpreter, args: &[JsValue]) -> JsValue {
    match args.first() {
        None | Some(JsValue::Undefined) => {
            JsValue::Regex(Rc::new(RefCell::new(compile_regex("", ""))))
        }
        Some(JsValue::Regex(_)) if args.len() == 1 => args[0].clone(),
        Some(v) => {
            let pattern = this.repr(v);
            let flags = match args.get(1) {
                Some(f) if !nullish(f) => this.repr(f),
                _ => String::new(),
            };
            JsValue::Regex(Rc::new(RefCell::new(compile_regex(&pattern, &flags))))
        }
    }
}

fn regexp_call(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let r = make_regexp(&this, &args);
        Ok(r)
    })
}

fn date_now(_this: &Rc<Interpreter>, _obj: &JsValue, _args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        use std::time::{SystemTime, UNIX_EPOCH};
        let ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64() * 1000.0)
            .unwrap_or(0.0);
        Ok(JsValue::Number(ms))
    })
}

/// ISO 8601, and only ISO 8601: `YYYY-MM-DD` with an optional
/// `THH:MM:SS(.mmm)(Z)`. Every other spelling a date has ever been written in
/// is implementation-defined, and guessing at them is how you end up parsing
/// `03/04/2020` differently from the page that wrote it. Anything unrecognised
/// is NaN, which is what an invalid date is.
fn parse_ms(text: &str) -> f64 {
    let b = text.as_bytes();
    if b.len() < 10 {
        return f64::NAN;
    }
    let num = |s: &str| -> Option<i64> { s.parse::<i64>().ok() };
    if b[4] != b'-' || b[7] != b'-' {
        return f64::NAN;
    }
    let (y, mo, d) = match (num(&text[0..4]), num(&text[5..7]), num(&text[8..10])) {
        (Some(y), Some(mo), Some(d)) => (y, mo, d),
        _ => return f64::NAN,
    };
    if !(1..=12).contains(&mo) || !(1..=31).contains(&d) {
        return f64::NAN;
    }
    let mut ms = days_from_civil(y, mo, d) * 86_400_000;
    let mut i = 10usize;
    if b.len() >= 16 && (b[10] == b'T' || b[10] == b' ') && b[13] == b':' {
        let h = num(&text[11..13]).unwrap_or(0);
        let mi = num(&text[14..16]).unwrap_or(0);
        ms += h * 3_600_000 + mi * 60_000;
        i = 16;
        if b.len() >= 19 && b[16] == b':' {
            ms += num(&text[17..19]).unwrap_or(0) * 1000;
            i = 19;
            if b.len() >= 23 && b[19] == b'.' {
                ms += num(&text[20..23]).unwrap_or(0);
                i = 23;
            }
        }
    }
    // An explicit numeric UTC offset `±HH:MM` is applied to the naive time;
    // `Z` is UTC. Anything else after the fields is not something this
    // parser recognises, and silently treating it as UTC would be a lie.
    if i < b.len() {
        match b[i] {
            b'Z' => {
                if i + 1 != b.len() {
                    return f64::NAN;
                }
            }
            b'+' | b'-' => {
                if i + 6 != b.len() || b[i + 3] != b':' {
                    return f64::NAN;
                }
                let oh = num(&text[i + 1..i + 3]).unwrap_or(0);
                let om = num(&text[i + 4..i + 6]).unwrap_or(0);
                let off = oh * 3_600_000 + om * 60_000;
                ms += if b[i] == b'-' { off } else { -off };
            }
            _ => return f64::NAN,
        }
    }
    ms as f64
}

/// `days_from_civil` done in f64 so a huge but finite field cannot overflow
/// i64; the caller clamps the result to JavaScript's ±8.64e15 ms range.
fn days_from_civil_f64(y_in: f64, m: f64, d: f64) -> f64 {
    let y = if m <= 2.0 { y_in - 1.0 } else { y_in };
    let era = y.div_euclid(400.0);
    let yoe = y - era * 400.0;
    let mp = if m > 2.0 { m - 3.0 } else { m + 9.0 };
    // Integer division, kept exact: with f64, `/` would turn 1532/5 into
    // 306.4 instead of the 306 the civil-date arithmetic needs.
    let doy = (153.0 * mp + 2.0).div_euclid(5.0) + d - 1.0;
    let doe = yoe * 365.0 + yoe.div_euclid(4.0) - yoe.div_euclid(100.0) + doy;
    era * 146_097.0 + doe - 719_468.0
}

/// The millisecond count a `new Date(...)` call describes. One argument is a
/// timestamp or an ISO string; two or more are the calendar fields, and they
/// are *carried* rather than validated -- month 12 is January of the next
/// year, day 0 is the last day of the previous month, and code in the wild
/// leans on both.
fn make_ms(this: &Rc<Interpreter>, args: &[JsValue]) -> f64 {
    if args.is_empty() {
        return this.now.get() * 1000.0;
    }
    if args.len() == 1 {
        return match &args[0] {
            JsValue::Str(s) => parse_ms(s),
            JsValue::Date(d) => d.borrow().ms,
            v => to_number(v),
        };
    }
    let nums: Vec<f64> = args.iter().map(to_number).collect();
    if nums.iter().any(|n| !n.is_finite()) {
        return f64::NAN;
    }
    let y = nums[0];
    let mo = nums[1];
    let d = nums.get(2).copied().unwrap_or(1.0);
    let h = nums.get(3).copied().unwrap_or(0.0);
    let mi = nums.get(4).copied().unwrap_or(0.0);
    let s = nums.get(5).copied().unwrap_or(0.0);
    let milli = nums.get(6).copied().unwrap_or(0.0);
    // Two-digit years mean the 1900s, a rule kept alive entirely by pages
    // written when that was the only way to spell a year.
    let year = if (0.0..=99.0).contains(&y) { y + 1900.0 } else { y };
    // Carried fields and the epoch shift are done in f64: a huge but finite
    // year must not overflow i64 on the way to a NaN.
    let days = days_from_civil_f64(
        year + mo.div_euclid(12.0),
        mo.rem_euclid(12.0) + 1.0,
        d,
    );
    let ms = days * 86_400_000.0 + h * 3_600_000.0 + mi * 60_000.0 + s * 1000.0 + milli;
    if !ms.is_finite() || ms.abs() > 8_640_000_000_000_000.0 {
        return f64::NAN;
    }
    ms
}

fn date_make(this: &Rc<Interpreter>, args: &[JsValue]) -> JsValue {
    make_js_date(make_ms(this, args))
}

fn make_js_date(ms: f64) -> JsValue {
    JsValue::Date(Rc::new(RefCell::new(JsDate { ms })))
}

fn date_call(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let d = date_make(&this, &args);
        Ok(d)
    })
}

fn date_parse(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let s = match args.first() {
            Some(v) => this.repr(v),
            None => String::new(),
        };
        Ok(JsValue::Number(parse_ms(&s)))
    })
}

/// `Date.UTC(...)`. With local time defined as UTC it is the same arithmetic
/// as the constructor's; it exists so that code which spells the intent out
/// still gets an answer, and gets the same one.
fn date_utc(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        // `Date.UTC()` with no arguments is NaN; a single argument is a year
        // (with a default month), not a timestamp.
        let ms = if args.is_empty() {
            f64::NAN
        } else if args.len() == 1 {
            let mut pair = args.clone();
            pair.push(JsValue::Number(0.0));
            make_ms(&this, &pair)
        } else {
            make_ms(&this, &args)
        };
        Ok(JsValue::Number(ms))
    })
}

fn date_get(_this: &Rc<Interpreter>, _obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    Ok(match name {
        "now" => native("now", date_now),
        "parse" => native("parse", date_parse),
        "UTC" => native("UTC", date_utc),
        _ => JsValue::Undefined,
    })
}

// -- Symbol ----------------------------------------------------------------

// A symbol is a value type of its own: `typeof Symbol()` is "symbol", two
// `Symbol("x")` calls are distinct values, and `Symbol.for` shares one value
// per key. What a page actually uses symbols for is a property name that
// cannot collide with a string, with `Symbol.iterator` as the well-known key
// the iterator protocol is wired through -- transpiled bundles test for the
// `Symbol` global and take a much worse path when it is missing, and Google's
// opens by handing it to a helper that then calls it, so its absence took the
// whole script down.
//
// Symbols are stored in property maps by their unique key string -- the
// well-known ones pin fixed keys like `"@@iterator"`, which is the name every
// polyfill of the last decade has used -- so `obj[Symbol.iterator]` reaches
// the same `"@@iterator"` slot that `iterate()` reads, and no page-visible
// symbol ever collides with a string key.
static SYMBOL_SEQ: AtomicUsize = AtomicUsize::new(0);

/// The namespace `Symbol.for`'s property keys live in. See `symbol_for`.
const SYMBOL_FOR_PREFIX: &str = "@@for:";

thread_local! {
    /// `Symbol.for`'s registry, keyed by the symbol's property key rather than
    /// by the string the script passed, so `Symbol.keyFor` can recognise one
    /// of its own symbols by looking its `key` straight up.
    static SYMBOL_REGISTRY: RefCell<BTreeMap<String, Rc<JsSymbol>>> = RefCell::new(BTreeMap::new());
    /// The well-known symbols, cached so `Symbol.iterator === Symbol.iterator`.
    static WELL_KNOWN: RefCell<BTreeMap<String, Rc<JsSymbol>>> = RefCell::new(BTreeMap::new());
}

/// A well-known symbol, cached per key so identity is stable: `Symbol.iterator`
/// read twice is the same symbol, which is what `===` and `Symbol.keyFor` rely
/// on.
fn well_known(name: &str) -> JsValue {
    let key = format!("@@{name}");
    WELL_KNOWN.with(|w| {
        if let Some(s) = w.borrow().get(&key) {
            return JsValue::Symbol(s.clone());
        }
        let sym = Rc::new(JsSymbol {
            key: key.clone(),
            desc: format!("Symbol.{name}"),
        });
        w.borrow_mut().insert(key, sym.clone());
        JsValue::Symbol(sym)
    })
}

/// A fresh, page-unique symbol: `Symbol(desc)`.
fn new_symbol(desc: &str) -> JsValue {
    let key = format!("@@sym{}", SYMBOL_SEQ.fetch_add(1, Ordering::Relaxed));
    JsValue::Symbol(Rc::new(JsSymbol {
        key,
        desc: desc.to_string(),
    }))
}

fn symbol_call(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let desc = match args.first() {
            Some(v) if !nullish(v) => this.repr(v),
            _ => String::new(),
        };
        Ok(new_symbol(&desc))
    })
}

/// `Symbol.for` shares one symbol per key across the whole page.
///
/// The registry key a script passes is an arbitrary string, so it cannot be
/// the property-map key as well: `Symbol.for("length")` would then address the
/// very slot `o.length` lives in, and `o[Symbol.for("length")] = 99` would
/// quietly overwrite it. Worse, `Symbol.for("@@iterator")` would land on the
/// well-known iterator slot and make an object iterable by accident. The
/// prefix keeps registry symbols in their own space, where they can collide
/// neither with a string key a script wrote nor with a well-known symbol,
/// while `desc` keeps the original string for `Symbol.keyFor` to hand back.
fn symbol_for(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let name = match args.first() {
            Some(v) if !nullish(v) => this.repr(v),
            _ => String::new(),
        };
        let key = format!("{SYMBOL_FOR_PREFIX}{name}");
        if let Some(s) = SYMBOL_REGISTRY.with(|r| r.borrow().get(&key).cloned()) {
            return Ok(JsValue::Symbol(s));
        }
        let sym = Rc::new(JsSymbol {
            key: key.clone(),
            desc: name,
        });
        SYMBOL_REGISTRY.with(|r| r.borrow_mut().insert(key, sym.clone()));
        Ok(JsValue::Symbol(sym))
    })
}

fn symbol_key_for(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        match args.first() {
            Some(JsValue::Symbol(s)) => {
                let registered = SYMBOL_REGISTRY.with(|r| r.borrow().contains_key(&s.key));
                if registered {
                    Ok(JsValue::str(s.desc.clone()))
                } else {
                    Ok(JsValue::Undefined)
                }
            }
            _ => Err(JsError::js("Symbol.keyFor requires a symbol argument")),
        }
    })
}

fn symbol_get(_this: &Rc<Interpreter>, _obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    Ok(match name {
        "iterator" | "asyncIterator" | "hasInstance" | "toPrimitive" | "toStringTag"
        | "species" | "isConcatSpreadable" | "unscopables" | "match" | "replace"
        | "search" | "split" => well_known(name),
        "for" => native("for", symbol_for),
        "keyFor" => native("keyFor", symbol_key_for),
        _ => JsValue::Undefined,
    })
}

// -- Map / Set -------------------------------------------------------------

fn map_new(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let _this = this.clone();
    Box::pin(async move {
        let m = Rc::new(RefCell::new(JsMap {
            store: RefCell::new(BTreeMap::new()),
        }));
        if let Some(first) = args.first() {
            match first {
                JsValue::Array(a) => {
                    for p in a.borrow().iter() {
                        if let JsValue::Array(pair) = p {
                            if pair.borrow().len() == 2 {
                                let k = pair.borrow()[0].clone();
                                let v = pair.borrow()[1].clone();
                                m.borrow().store.borrow_mut().insert(map_key(&k), (k, v));
                                continue;
                            }
                        }
                        m.borrow()
                            .store
                            .borrow_mut()
                            .insert(map_key(p), (p.clone(), JsValue::Undefined));
                    }
                }
                JsValue::Object(o) => {
                    for (k, v) in o.borrow().iter() {
                        let k = JsValue::str(k.clone());
                        m.borrow()
                            .store
                            .borrow_mut()
                            .insert(map_key(&k), (k, v.clone()));
                    }
                }
                _ => {}
            }
        }
        Ok(JsValue::Map(m))
    })
}

fn map_call(_this: &Rc<Interpreter>, _obj: &JsValue, _args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        Ok(JsValue::Map(Rc::new(RefCell::new(JsMap {
            store: RefCell::new(BTreeMap::new()),
        }))))
    })
}

fn set_new(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let s = Rc::new(RefCell::new(JsSet {
            store: RefCell::new(BTreeMap::new()),
        }));
        if let Some(JsValue::Array(a)) = args.first() {
            for v in a.borrow().iter() {
                s.borrow().store.borrow_mut().insert(map_key(v), v.clone());
            }
        }
        Ok(JsValue::Set(s))
    })
}

fn set_call(_this: &Rc<Interpreter>, _obj: &JsValue, _args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        Ok(JsValue::Set(Rc::new(RefCell::new(JsSet {
            store: RefCell::new(BTreeMap::new()),
        }))))
    })
}

// -- Promise ---------------------------------------------------------------

fn promise_resolve_static(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let value = first(&args);
        let p = JsPromise::new();
        promise_resolve(&this, &p, value);
        Ok(JsValue::Promise(p))
    })
}

fn promise_reject_static(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let reason = first(&args);
        let p = JsPromise::new();
        promise_reject(&this, &p, reason);
        Ok(JsValue::Promise(p))
    })
}

fn promise_all_static(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let p = JsPromise::new();
        let items: Vec<JsValue> = match first(&args) {
            JsValue::Array(a) => a.borrow().clone(),
            _ => vec![],
        };
        if items.is_empty() {
            promise_resolve(&this, &p, JsValue::array(vec![]));
            return Ok(JsValue::Promise(p));
        }
        let n = items.len();
        let results = Rc::new(RefCell::new(vec![JsValue::Undefined; n]));
        let remaining = Rc::new(Cell::new(n));
        for (i, item) in items.iter().enumerate() {
            let pj = as_promise(&this, item);
            let this2 = this.clone();
            let p2 = p.clone();
            let results2 = results.clone();
            let remaining2 = remaining.clone();
            promise_on_settle(&this, &pj, Rc::new(move |value, rejected| {
                if rejected {
                    promise_reject(&this2, &p2, value);
                    return;
                }
                results2.borrow_mut()[i] = value;
                remaining2.set(remaining2.get() - 1);
                if remaining2.get() == 0 {
                    promise_resolve(
                        &this2,
                        &p2,
                        JsValue::array(results2.borrow().clone()),
                    );
                }
            }));
        }
        Ok(JsValue::Promise(p))
    })
}

fn promise_all_settled_static(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let p = JsPromise::new();
        let items: Vec<JsValue> = match first(&args) {
            JsValue::Array(a) => a.borrow().clone(),
            _ => vec![],
        };
        let n = items.len();
        if n == 0 {
            promise_resolve(&this, &p, JsValue::array(vec![]));
            return Ok(JsValue::Promise(p));
        }
        let results = Rc::new(RefCell::new(vec![JsValue::Undefined; n]));
        let remaining = Rc::new(Cell::new(n));
        for (i, item) in items.iter().enumerate() {
            let pj = as_promise(&this, item);
            let this2 = this.clone();
            let p2 = p.clone();
            let results2 = results.clone();
            let remaining2 = remaining.clone();
            promise_on_settle(&this, &pj, Rc::new(move |value, rejected| {
                let mut entry = BTreeMap::new();
                if rejected {
                    entry.insert("status".to_string(), JsValue::str("rejected"));
                    entry.insert("reason".to_string(), value);
                } else {
                    entry.insert("status".to_string(), JsValue::str("fulfilled"));
                    entry.insert("value".to_string(), value);
                }
                results2.borrow_mut()[i] = JsValue::Object(Rc::new(RefCell::new(entry)));
                remaining2.set(remaining2.get() - 1);
                if remaining2.get() == 0 {
                    promise_resolve(
                        &this2,
                        &p2,
                        JsValue::array(results2.borrow().clone()),
                    );
                }
            }));
        }
        Ok(JsValue::Promise(p))
    })
}

fn promise_any_static(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let p = JsPromise::new();
        let items: Vec<JsValue> = match first(&args) {
            JsValue::Array(a) => a.borrow().clone(),
            _ => vec![],
        };
        let n = items.len();
        if n == 0 {
            promise_reject(&this, &p, JsValue::str("All promises were rejected"));
            return Ok(JsValue::Promise(p));
        }
        let remaining = Rc::new(Cell::new(n));
        for item in items {
            let pj = as_promise(&this, &item);
            let this2 = this.clone();
            let p2 = p.clone();
            let remaining2 = remaining.clone();
            promise_on_settle(&this, &pj, Rc::new(move |value, rejected| {
                if !rejected {
                    promise_resolve(&this2, &p2, value);
                    return;
                }
                remaining2.set(remaining2.get() - 1);
                if remaining2.get() == 0 {
                    promise_reject(
                        &this2,
                        &p2,
                        JsValue::str("All promises were rejected"),
                    );
                }
            }));
        }
        Ok(JsValue::Promise(p))
    })
}

fn promise_race_static(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let p = JsPromise::new();
        let items: Vec<JsValue> = match first(&args) {
            JsValue::Array(a) => a.borrow().clone(),
            _ => vec![],
        };
        for item in items {
            let pj = as_promise(&this, &item);
            let this2 = this.clone();
            let p2 = p.clone();
            promise_on_settle(&this, &pj, Rc::new(move |value, rejected| {
                if rejected {
                    promise_reject(&this2, &p2, value);
                } else {
                    promise_resolve(&this2, &p2, value);
                }
            }));
        }
        Ok(JsValue::Promise(p))
    })
}

fn promise_ctor(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let p = JsPromise::new();
        let executor = first(&args);
        if !nullish(&executor) {
            let this2 = this.clone();
            let p2 = p.clone();
            let resolve = JsValue::Callback(Rc::new(
                move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                    let v = first(&args);
                    promise_resolve(i, &p2, v);
                    Box::pin(async { Ok(JsValue::Undefined) })
                },
            ));
            let this3 = this.clone();
            let p3 = p.clone();
            let reject = JsValue::Callback(Rc::new(
                move |i: &Rc<Interpreter>, args: Vec<JsValue>| -> EvResult {
                    let v = first(&args);
                    promise_reject(i, &p3, v);
                    Box::pin(async { Ok(JsValue::Undefined) })
                },
            ));
            let r = call_value(&this2, &executor, vec![resolve, reject], JsValue::Undefined).await;
            if r.is_err() {
                promise_reject(&this3, &p, JsValue::str("Promise executor threw"));
            }
        }
        Ok(JsValue::Promise(p))
    })
}

fn promise_get_static(_this: &Rc<Interpreter>, _obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    Ok(match name {
        "resolve" => native("resolve", promise_resolve_static),
        "reject" => native("reject", promise_reject_static),
        "all" => native("all", promise_all_static),
        "allSettled" => native("allSettled", promise_all_settled_static),
        "race" => native("race", promise_race_static),
        "any" => native("any", promise_any_static),
        _ => JsValue::Undefined,
    })
}

// -- timers ----------------------------------------------------------------

fn set_timeout(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let fn_ = first(&args);
        let ms = args.get(1).cloned().unwrap_or(JsValue::Undefined);
        let ms = if nullish(&ms) { 0.0 } else { to_number(&ms) };
        let id = this.schedule_timer(fn_, ms, false)?;
        Ok(JsValue::Number(to_number(&id)))
    })
}

fn set_interval(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let fn_ = first(&args);
        let ms = args.get(1).cloned().unwrap_or(JsValue::Undefined);
        let ms = if nullish(&ms) { 0.0 } else { to_number(&ms) };
        let id = this.schedule_timer(fn_, ms, true)?;
        Ok(JsValue::Number(to_number(&id)))
    })
}

fn clear_timer(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let id = first(&args);
        let _ = this.clear_timer(&id);
        Ok(JsValue::Undefined)
    })
}

fn queue_microtask(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let fn_ = first(&args);
        let this2 = this.clone();
        this.enqueue(Rc::new(move || {
            let _ = drive_sync(&this2, call_value(&this2, &fn_, vec![], JsValue::Undefined));
        }));
        Ok(JsValue::Undefined)
    })
}

// -- init ------------------------------------------------------------------

pub fn init_globals(this: &Rc<Interpreter>) -> Result<(), JsError> {
    let globals = &mut this.globals.borrow_mut();
    globals.clear();

    let mut console = BTreeMap::new();
    console.insert("log".to_string(), native("log", console_log));
    globals.insert("console".to_string(), JsValue::Object(Rc::new(RefCell::new(console))));

    globals.insert("String".to_string(), ctor("String", string_call, string_call, Some(string_get), None));
    globals.insert("Number".to_string(), ctor("Number", number_call, number_call, Some(number_get), None));
    globals.insert("Boolean".to_string(), native("Boolean", boolean_call));
    globals.insert("Array".to_string(), ctor("Array", array_call, array_call, Some(array_get), None));
    globals.insert("Object".to_string(), ctor("Object", object_call, object_call, Some(object_get), None));
    globals.insert("Function".to_string(), ctor("Function", function_call, function_call, Some(function_get), None));
    globals.insert("parseInt".to_string(), native("parseInt", parse_int_call));
    globals.insert("parseFloat".to_string(), native("parseFloat", parse_float_call));
    globals.insert("isNaN".to_string(), native("isNaN", global_is_nan));
    globals.insert("isFinite".to_string(), native("isFinite", global_is_finite));
    globals.insert("btoa".to_string(), native("btoa", btoa_call));
    globals.insert("atob".to_string(), native("atob", atob_call));
    globals.insert("encodeURIComponent".to_string(), native("encodeURIComponent", encode_uri_component));
    globals.insert("decodeURIComponent".to_string(), native("decodeURIComponent", decode_uri_component));
    globals.insert("encodeURI".to_string(), native("encodeURI", encode_uri));
    globals.insert("decodeURI".to_string(), native("decodeURI", decode_uri));
    globals.insert("NaN".to_string(), JsValue::Number(f64::NAN));
    globals.insert("Infinity".to_string(), JsValue::Number(f64::INFINITY));
    globals.insert("Promise".to_string(), ctor("Promise", promise_ctor, promise_ctor, Some(promise_get_static), None));
    globals.insert("Error".to_string(), ctor("Error", error_make, error_make, None, None));
    for name in [
        "TypeError",
        "RangeError",
        "SyntaxError",
        "ReferenceError",
        "EvalError",
        "URIError",
    ] {
        globals.insert(name.to_string(), ctor(name, error_make, error_make, None, None));
    }
    globals.insert("RegExp".to_string(), ctor("RegExp", regexp_call, regexp_call, None, None));
    globals.insert("Date".to_string(), ctor("Date", date_call, date_call, Some(date_get), None));
    globals.insert(
        "Symbol".to_string(),
        // `new Symbol()` is a TypeError in a real engine and nobody writes it,
        // so there is no constructor here -- only the call form.
        JsValue::Native(Rc::new(Native {
            name: Rc::from("Symbol"),
            call: Some(symbol_call),
            ctor: None,
            get: Some(symbol_get),
            set: None,
            method_of: None,
        })),
    );
    globals.insert("Map".to_string(), ctor("Map", map_call, map_new, None, None));
    globals.insert("Set".to_string(), ctor("Set", set_call, set_new, None, None));
    // WeakMap and WeakSet are Map and Set that hold their keys weakly, and
    // weakly is the one thing this engine cannot do -- nothing here is ever
    // collected, so every reference is already strong. What is left of the
    // difference is the API, which is a strict subset of the strong one, so
    // the strong one stands in. A page keeps its private data alive longer
    // than it meant to; a page that cannot construct a WeakMap at all stops
    // dead, and one of those two is a bundle that runs.
    globals.insert("WeakMap".to_string(), ctor("WeakMap", map_call, map_new, None, None));
    globals.insert("WeakSet".to_string(), ctor("WeakSet", set_call, set_new, None, None));

    let mut math = BTreeMap::new();
    for name in [
        "abs", "ceil", "floor", "round", "trunc", "sign", "sqrt", "cbrt", "exp",
        "log", "log2", "log10", "pow", "sin", "cos", "tan", "asin", "acos", "atan",
        "atan2", "sinh", "cosh", "tanh", "hypot", "max", "min", "random", "fround",
    ] {
        math.insert(name.to_string(), math_value(name));
    }
    for name in [
        "PI", "E", "LN2", "LN10", "LOG2E", "LOG10E", "SQRT2", "SQRT1_2",
    ] {
        math.insert(name.to_string(), math_const(name));
    }
    globals.insert("Math".to_string(), JsValue::Object(Rc::new(RefCell::new(math))));

    let mut json = BTreeMap::new();
    json.insert("parse".to_string(), native("parse", json_parse));
    json.insert("stringify".to_string(), native("stringify", json_stringify));
    globals.insert("JSON".to_string(), JsValue::Object(Rc::new(RefCell::new(json))));

    globals.insert("setTimeout".to_string(), native("setTimeout", set_timeout));
    globals.insert("setInterval".to_string(), native("setInterval", set_interval));
    globals.insert("clearTimeout".to_string(), native("clearTimeout", clear_timer));
    globals.insert("clearInterval".to_string(), native("clearInterval", clear_timer));
    globals.insert("queueMicrotask".to_string(), native("queueMicrotask", queue_microtask));

    globals.insert("document".to_string(), JsValue::Undefined);
    globals.insert("fetch".to_string(), JsValue::Undefined);
    globals.insert("XMLHttpRequest".to_string(), JsValue::Undefined);

    let window = getter("window", window_get);
    // attach the setter
    let window_native = match window {
        JsValue::Native(ref _n) => Rc::new(Native {
            name: Rc::from("window"),
            call: None,
            ctor: None,
            get: Some(window_get),
            set: Some(window_set),
            method_of: None,
        }),
        _ => unreachable!(),
    };
    let window = JsValue::Native(window_native);
    globals.insert("window".to_string(), window.clone());
    globals.insert("globalThis".to_string(), window.clone());

    let ls = JsValue::Native(Rc::new(Native {
        name: Rc::from("localStorage"),
        call: None,
        ctor: None,
        get: Some(ls_get),
        set: Some(ls_set),
        method_of: None,
    }));
    globals.insert("localStorage".to_string(), ls);

    Ok(())
}
