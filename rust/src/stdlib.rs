//! The JavaScript standard library globals, ported from
//! `jsengine.py::Interpreter.__init__` (and its native ctors).

use crate::interp::*;
use crate::value::*;
use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::rc::Rc;

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
    }))
}

fn ctor(name: &str, call: NativeFn, ctor_: NativeFn, get: Option<NativeGet>, set: Option<NativeSet>) -> JsValue {
    JsValue::Native(Rc::new(Native {
        name: Rc::from(name),
        call: Some(call),
        ctor: Some(ctor_),
        get,
        set,
    }))
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
        let parts: Vec<String> = match args.first() {
            Some(JsValue::Array(a)) => {
                a.borrow().iter().map(|v| this.repr(v)).collect()
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
        let re = regex::Regex::new(r"^[+-]?(?:\d+\.?\d*|\.\d+|[iI][nN][fF]i?n?i?t?y?)")
            .unwrap();
        match re.find(&text) {
            Some(m) => {
                let tok = m.as_str();
                if tok.to_lowercase() == "infinity" {
                    Ok(JsValue::Number(f64::INFINITY))
                } else {
                    let v: f64 = tok.parse().unwrap_or(f64::NAN);
                    Ok(JsValue::Number(v))
                }
            }
            None => Ok(JsValue::Number(f64::NAN)),
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

fn array_from(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let _this = this.clone();
    Box::pin(async move {
        let v = first(&args);
        Ok(match v {
            JsValue::Array(a) => JsValue::array(a.borrow().clone()),
            JsValue::Str(s) => JsValue::array(
                s.chars().map(|c| JsValue::str(c.to_string())).collect(),
            ),
            _ => JsValue::array(vec![]),
        })
    })
}

fn array_get(_this: &Rc<Interpreter>, _obj: &JsValue, name: &str) -> Result<JsValue, JsError> {
    Ok(match name {
        "isArray" => native("isArray", array_is_array),
        "from" => native("from", array_from),
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
        let keys: Vec<String> = match v {
            JsValue::Object(m) => m.borrow().keys().cloned().collect(),
            _ => vec![],
        };
        Ok(JsValue::array(keys.into_iter().map(JsValue::str).collect()))
    })
}

fn obj_values(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        let v = first(&args);
        Ok(match v {
            JsValue::Object(m) => JsValue::array(m.borrow().values().cloned().collect()),
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
            _ => JsValue::Undefined,
        })
    })
}

fn obj_set_proto_of(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    Box::pin(async move {
        if args.len() >= 2 {
            if let JsValue::Instance(i) = &args[0] {
                if let JsValue::Object(m) = &args[1] {
                    i.borrow_mut().proto = m.clone();
                }
            }
        }
        Ok(first(&args))
    })
}

fn obj_define_property(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        if args.len() >= 3 {
            if let JsValue::Object(map) = &args[0] {
                let key = this.repr(&args[1]);
                if let JsValue::Object(desc) = &args[2] {
                    if let Some(v) = desc.borrow().get("value") {
                        map.borrow_mut().insert(key, v.clone());
                    } else if let Some(g) = desc.borrow().get("get") {
                        if is_js_function(g) {
                            map.borrow_mut().insert(key, g.clone());
                        }
                    }
                }
            }
        }
        Ok(args.into_iter().next().unwrap_or(JsValue::Undefined))
    })
}

fn obj_freeze(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let v = first(&args);
    Box::pin(async move { Ok(v) })
}

fn obj_has_own(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        if args.len() >= 2 {
            if let JsValue::Object(m) = &args[0] {
                let key = this.repr(&args[1]);
                return Ok(JsValue::Bool(m.borrow().contains_key(&key)));
            }
        }
        Ok(JsValue::Bool(false))
    })
}

fn object_proto_has_own(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        if args.len() >= 2 {
            if let JsValue::Object(m) = &args[0] {
                let key = this.repr(&args[1]);
                return Ok(JsValue::Bool(m.borrow().contains_key(&key)));
            }
        }
        Ok(JsValue::Bool(false))
    })
}

fn object_proto_to_string(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    let v = first(&args);
    Box::pin(async move { Ok(JsValue::str(this.repr(&v))) })
}

fn object_proto_value_of(_this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let v = first(&args);
    Box::pin(async move { Ok(v) })
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
        "freeze" => native("freeze", obj_freeze),
        "hasOwnProperty" => native("hasOwnProperty", obj_has_own),
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

fn error_make(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let msg = match args.first() {
            Some(v) if !nullish(v) => this.repr(v),
            _ => String::new(),
        };
        Ok(JsValue::Error(Rc::new(RefCell::new(JsHostError {
            message: msg,
            name: "Error".to_string(),
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

fn parse_ms(text: &str) -> f64 {
    use chrono::{NaiveDate, NaiveDateTime};
    if let Ok(dt) = NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%.fZ") {
        return dt.and_utc().timestamp() as f64 * 1000.0;
    }
    if let Ok(dt) = NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%SZ") {
        return dt.and_utc().timestamp() as f64 * 1000.0;
    }
    if let Ok(dt) = NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S") {
        return dt.and_utc().timestamp() as f64 * 1000.0;
    }
    if let Ok(d) = NaiveDate::parse_from_str(text, "%Y-%m-%d") {
        if let Some(dt) = d.and_hms_opt(0, 0, 0) {
            return dt.and_utc().timestamp() as f64 * 1000.0;
        }
    }
    if let Ok(dt) = NaiveDateTime::parse_from_str(text, "%a %b %d %Y %H:%M:%S") {
        return dt.and_utc().timestamp() as f64 * 1000.0;
    }
    f64::NAN
}

fn make_ms(this: &Rc<Interpreter>, args: &[JsValue], utc: bool) -> f64 {
    if args.is_empty() {
        return this.now.get() * 1000.0;
    }
    if args.len() == 1 {
        match &args[0] {
            JsValue::Number(n) => return *n,
            v => return parse_ms(&this.repr(v)),
        }
    }
    let nums: Vec<f64> = args.iter().map(to_number).collect();
    let y = nums[0] as i32;
    let mo = nums[1] as u32;
    let d = nums.get(2).copied().unwrap_or(1.0) as u32;
    let h = nums.get(3).copied().unwrap_or(0.0) as u32;
    let mi = nums.get(4).copied().unwrap_or(0.0) as u32;
    let s = nums.get(5).copied().unwrap_or(0.0) as u32;
    let ms = nums.get(6).copied().unwrap_or(0.0) as u32;
    let year = if (0..=99).contains(&y) { y + 1900 } else { y };
    use chrono::{NaiveDate, TimeZone, Utc};
    if utc {
        let dt = Utc
            .with_ymd_and_hms(year, mo + 1, d, h, mi, s)
            .earliest()
            .and_then(|dt| dt.checked_add_signed(chrono::Duration::milliseconds(ms as i64)));
        match dt {
            Some(dt) => dt.timestamp_millis() as f64,
            None => f64::NAN,
        }
    } else {
        let dt = NaiveDate::from_ymd_opt(year, mo + 1, d)
            .and_then(|nd| nd.and_hms_milli_opt(h, mi, s, ms));
        match dt {
            Some(dt) => dt.and_utc().timestamp() as f64 * 1000.0,
            None => f64::NAN,
        }
    }
}

fn date_make(this: &Rc<Interpreter>, args: &[JsValue], utc: bool) -> JsValue {
    let ms = make_ms(this, args, utc);
    make_js_date(ms)
}

fn make_js_date(ms: f64) -> JsValue {
    use chrono::{TimeZone, Utc};
    let local = if ms.is_finite() {
        chrono::Local.timestamp_millis_opt(ms as i64).single()
    } else {
        None
    };
    let utc = if ms.is_finite() {
        Utc.timestamp_millis_opt(ms as i64).single()
    } else {
        None
    };
    JsValue::Date(Rc::new(RefCell::new(JsDate {
        ms,
        local: local.map(|l| l.naive_local()),
        utc: utc.map(|u| u.naive_utc().and_utc()),
    })))
}

fn date_call(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let d = date_make(&this, &args, false);
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

fn date_utc(this: &Rc<Interpreter>, _obj: &JsValue, args: Vec<JsValue>) -> EvResult {
    let this = this.clone();
    Box::pin(async move {
        let ms = make_ms(&this, &args, true);
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
                                m.borrow().store.borrow_mut().insert(map_key(&k), v);
                                continue;
                            }
                        }
                        m.borrow()
                            .store
                            .borrow_mut()
                            .insert(map_key(p), JsValue::Undefined);
                    }
                }
                JsValue::Object(o) => {
                    for (k, v) in o.borrow().iter() {
                        m.borrow()
                            .store
                            .borrow_mut()
                            .insert(map_key(&JsValue::str(k.clone())), v.clone());
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
        "race" => native("race", promise_race_static),
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
    globals.insert("parseInt".to_string(), native("parseInt", parse_int_call));
    globals.insert("parseFloat".to_string(), native("parseFloat", parse_float_call));
    globals.insert("NaN".to_string(), JsValue::Number(f64::NAN));
    globals.insert("Infinity".to_string(), JsValue::Number(f64::INFINITY));
    globals.insert("Promise".to_string(), ctor("Promise", promise_ctor, promise_ctor, Some(promise_get_static), None));
    globals.insert("Error".to_string(), ctor("Error", error_make, error_make, None, None));
    globals.insert("RegExp".to_string(), ctor("RegExp", regexp_call, regexp_call, None, None));
    globals.insert("Date".to_string(), ctor("Date", date_call, date_call, Some(date_get), None));
    globals.insert("Map".to_string(), ctor("Map", map_call, map_new, None, None));
    globals.insert("Set".to_string(), ctor("Set", set_call, set_new, None, None));

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
    }));
    globals.insert("localStorage".to_string(), ls);

    Ok(())
}
