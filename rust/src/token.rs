//! Tokenizer ported from `jsengine.py::_Tokenizer`.

use crate::value::JsError;

pub const MAX_TOKENS: usize = 200_000;

#[derive(Debug, Clone, PartialEq)]
pub enum TokKind {
    Number,
    Str,
    Template,
    Ident,
    Kw,
    Punct,
    Regex,
}

#[derive(Debug, Clone)]
pub struct Token {
    pub kind: TokKind,
    pub text: String,
    pub payload: TokPayload,
    pub offset: usize,
}

#[derive(Debug, Clone)]
pub enum TokPayload {
    None,
    Str(String),
    Number(f64),
    Regex(String, String),
}

pub fn keywords() -> &'static std::collections::HashSet<&'static str> {
    use std::sync::OnceLock;
    static KW: OnceLock<std::collections::HashSet<&'static str>> = OnceLock::new();
    KW.get_or_init(|| {
        [
            "var", "let", "const", "function", "return", "if", "else", "while",
            "for", "break", "continue", "true", "false", "null", "undefined",
            "typeof", "throw", "try", "catch", "finally", "new", "this", "await",
            "class", "extends", "super", "static", "in", "instanceof", "delete",
            "void", "of", "switch", "case", "default", "do",
        ]
        .into_iter()
        .collect()
    })
}

const PUNCT: &[(&str, usize)] = &[
    (">>>=", 4),
    ("...", 3), ("===", 3), ("!==", 3), ("**=", 3), ("&&=", 3),
    ("||=", 3), ("??=", 3), (">>>", 3),
    ("==", 2), ("!=", 2), ("<=", 2), (">=", 2), ("&&", 2), ("||", 2),
    ("+=", 2), ("-=", 2), ("*=", 2), ("/=", 2), ("%=", 2), ("++", 2),
    ("--", 2), ("**", 2), (">>=", 2), ("<<=", 2), ("&=", 2), ("|=", 2),
    ("^=", 2), ("??", 2), ("=>", 2), (">>", 2), ("<<", 2),
    ("{", 1), ("}", 1), ("(", 1), (")", 1), ("[", 1), ("]", 1),
    (";", 1), (",", 1), (".", 1), (":", 1), ("?", 1), ("=", 1), ("!", 1),
    ("+", 1), ("-", 1), ("*", 1), ("/", 1), ("%", 1), ("<", 1), (">", 1),
    ("&", 1), ("|", 1), ("^", 1), ("~", 1), ("`", 1),
];

fn simple_esc(c: char) -> Option<char> {
    match c {
        'n' => Some('\n'),
        't' => Some('\t'),
        '\\' => Some('\\'),
        '\'' => Some('\''),
        '"' => Some('"'),
        '\n' => Some('\0'),
        _ => None,
    }
}

/// A `/` starts a regex literal unless the previous token ends a value.
fn regex_allowed(prev: Option<&Token>) -> bool {
    let Some(prev) = prev else { return true };
    match prev.kind {
        TokKind::Ident | TokKind::Number | TokKind::Str | TokKind::Template
        | TokKind::Regex => false,
        TokKind::Kw => !matches!(
            prev.text.as_str(),
            "true" | "false" | "null" | "undefined" | "this" | "super"
        ),
        TokKind::Punct => !matches!(prev.text.as_str(), ")" | "]" | "}" | "++" | "--"),
    }
}

fn find_template_end(s: &str, start: usize) -> Option<usize> {
    let chars: Vec<char> = s.chars().collect();
    let n = chars.len();
    let mut i = start + 1;
    while i < n {
        match chars[i] {
            '\\' => i += 2,
            '$' if i + 1 < n && chars[i + 1] == '{' => {
                let mut j = i + 2;
                let mut d = 1i64;
                let mut q: Option<char> = None;
                while j < n {
                    let c = chars[j];
                    if let Some(quote) = q {
                        if c == '\\' {
                            j += 2;
                            continue;
                        }
                        if c == quote {
                            q = None;
                        }
                    } else if c == '\'' || c == '"' || c == '`' {
                        q = Some(c);
                    } else if c == '{' {
                        d += 1;
                    } else if c == '}' {
                        d -= 1;
                        if d == 0 {
                            break;
                        }
                    }
                    j += 1;
                }
                if j >= n {
                    return None;
                }
                i = j + 1;
            }
            '`' => return Some(i + 1),
            _ => i += 1,
        }
    }
    None
}

/// The character at byte offset `i`, which is not the same thing as the byte
/// there. Reading `bytes[i] as char` is a Latin-1 misreading of the leading
/// byte of a UTF-8 sequence: `×` scans as `Ã` followed by a control character,
/// which is both wrong (string and regex literals came out as mojibake) and
/// fatal (`Ã` is alphabetic, so the identifier scanner accepted it, then
/// stopped one byte in and sliced through the middle of the character). Every
/// scanner below decodes a real character and advances by `len_utf8`, so the
/// cursor only ever lands on a boundary.
fn char_at(source: &str, i: usize) -> char {
    source[i..].chars().next().unwrap_or('\0')
}

pub fn tokenize(source: &str) -> Result<Vec<Token>, JsError> {
    let kw = keywords();
    let mut tokens: Vec<Token> = Vec::new();
    let bytes = source.as_bytes();
    let mut i = 0usize;
    let n = bytes.len();
    let fail = |offset: usize, msg: &str| -> JsError {
        let line = source[..offset].matches('\n').count() + 1;
        JsError::js(format!("SyntaxError on line {line}: {msg}"))
    };

    while i < n {
        let ch = char_at(source, i);
        let prev = tokens.last();
        match ch {
            ' ' | '\t' | '\r' | '\n' => i += 1,
            _ if source[i..].starts_with("//") => {
                match source[i..].find('\n') {
                    Some(d) => i += d + 1,
                    None => i = n,
                }
            }
            _ if source[i..].starts_with("/*") => {
                match source[i + 2..].find("*/") {
                    Some(d) => i += 2 + d + 2,
                    None => return Err(fail(i, "unterminated block comment")),
                }
            }
            _ if ch.is_ascii_digit()
                || (ch == '.' && i + 1 < n && (bytes[i + 1] as char).is_ascii_digit()) =>
            {
                let mut j = i;
                if ch == '0' && i + 1 < n && matches!(bytes[i + 1] as char, 'x' | 'X') {
                    j = i + 2;
                    while j < n && (bytes[j] as char).is_ascii_hexdigit() {
                        j += 1;
                    }
                    let digits = &source[i + 2..j];
                    let v = if j > i + 2 {
                        u64::from_str_radix(digits, 16).unwrap_or(0)
                    } else {
                        0
                    };
                    tokens.push(Token {
                        kind: TokKind::Number,
                        text: source[i..j].to_string(),
                        payload: TokPayload::Number(v as f64),
                        offset: i,
                    });
                    i = j;
                } else if ch == '0' && i + 1 < n && matches!(bytes[i + 1] as char, 'b' | 'B') {
                    j = i + 2;
                    while j < n && matches!(bytes[j] as char, '0' | '1') {
                        j += 1;
                    }
                    let digits = &source[i + 2..j];
                    let v = if j > i + 2 {
                        u64::from_str_radix(digits, 2).unwrap_or(0)
                    } else {
                        0
                    };
                    tokens.push(Token {
                        kind: TokKind::Number,
                        text: source[i..j].to_string(),
                        payload: TokPayload::Number(v as f64),
                        offset: i,
                    });
                    i = j;
                } else {
                    while j < n && (bytes[j] as char).is_ascii_digit() {
                        j += 1;
                    }
                    if j < n && bytes[j] == b'.' {
                        j += 1;
                        while j < n && (bytes[j] as char).is_ascii_digit() {
                            j += 1;
                        }
                    }
                    if j < n && matches!(bytes[j] as char, 'e' | 'E') {
                        let mut k = j + 1;
                        if k < n && matches!(bytes[k] as char, '+' | '-') {
                            k += 1;
                        }
                        if k < n && (bytes[k] as char).is_ascii_digit() {
                            while k < n && (bytes[k] as char).is_ascii_digit() {
                                k += 1;
                            }
                            j = k;
                        }
                    }
                    let num = parse_number_text(&source[i..j]);
                    tokens.push(Token {
                        kind: TokKind::Number,
                        text: source[i..j].to_string(),
                        payload: TokPayload::Number(num),
                        offset: i,
                    });
                    i = j;
                }
            }
            '"' | '\'' => {
                let quote = ch;
                i += 1;
                let mut buf = String::new();
                loop {
                    if i >= n {
                        return Err(fail(i, "unterminated string literal"));
                    }
                    let c = char_at(source, i);
                    if c == '\\' {
                        i += 1;
                        if i >= n {
                            return Err(fail(i, "unterminated string literal"));
                        }
                        let esc = char_at(source, i);
                        i += esc.len_utf8();
                        if let Some(s) = simple_esc(esc) {
                            if s == '\0' {
                                // continuation: skip the newline
                            } else {
                                buf.push(s);
                            }
                        } else if matches!(esc, 'x' | 'u') {
                            let size = if esc == 'u' { 4 } else { 2 };
                            // `get` declines both a short tail and a range
                            // that would cut a character in half.
                            if let Some(hex) = source.get(i..i + size) {
                                if let Ok(cp) = u32::from_str_radix(hex, 16) {
                                    if let Some(chr) = char::from_u32(cp) {
                                        buf.push(chr);
                                    }
                                }
                                i += size;
                            }
                        } else {
                            buf.push(esc);
                        }
                    } else if c == quote {
                        i += 1;
                        tokens.push(Token {
                            kind: TokKind::Str,
                            text: buf.clone(),
                            payload: TokPayload::Str(buf),
                            offset: i,
                        });
                        break;
                    } else if c == '\n' {
                        return Err(fail(i, "unterminated string literal"));
                    } else {
                        buf.push(c);
                        i += c.len_utf8();
                    }
                }
            }
            '`' => {
                let Some(j) = find_template_end(source, i) else {
                    return Err(fail(i, "unterminated template literal"));
                };
                tokens.push(Token {
                    kind: TokKind::Template,
                    text: source[i + 1..j - 1].to_string(),
                    payload: TokPayload::Str(source[i + 1..j - 1].to_string()),
                    offset: i,
                });
                i = j;
            }
            _ if ch.is_alphabetic() || ch == '_' || ch == '$' => {
                let mut j = i;
                while j < n {
                    let c = char_at(source, j);
                    if !(c.is_alphanumeric() || c == '_' || c == '$') {
                        break;
                    }
                    j += c.len_utf8();
                }
                let word = &source[i..j];
                let is_kw = kw.contains(word);
                tokens.push(Token {
                    kind: if is_kw { TokKind::Kw } else { TokKind::Ident },
                    text: word.to_string(),
                    payload: TokPayload::None,
                    offset: i,
                });
                i = j;
            }
            '/' if regex_allowed(prev) => {
                let mut j = i + 1;
                let mut buf = String::new();
                let mut in_class = false;
                let mut terminated = false;
                while j < n {
                    let c = char_at(source, j);
                    if c == '\\' {
                        buf.push(c);
                        j += 1;
                        if j < n {
                            let esc = char_at(source, j);
                            buf.push(esc);
                            j += esc.len_utf8();
                        }
                        continue;
                    }
                    if c == '[' {
                        in_class = true;
                    } else if c == ']' {
                        in_class = false;
                    } else if c == '/' && !in_class {
                        j += 1;
                        terminated = true;
                        break;
                    } else if c == '\n' {
                        return Err(fail(i, "unterminated regular expression"));
                    }
                    buf.push(c);
                    j += c.len_utf8();
                }
                if !terminated {
                    return Err(fail(i, "unterminated regular expression"));
                }
                let mut flags = String::new();
                while j < n {
                    let c = char_at(source, j);
                    if !c.is_alphabetic() {
                        break;
                    }
                    flags.push(c);
                    j += c.len_utf8();
                }
                tokens.push(Token {
                    kind: TokKind::Regex,
                    text: buf.clone(),
                    payload: TokPayload::Regex(buf, flags),
                    offset: i,
                });
                i = j;
            }
            _ if "{ } ( ) [ ] ; , . : ? ! < > = + - * / % & | ^ ~ @ # `"
                .contains(ch)
            =>
            {
                if ch == '?'
                    && i + 1 < n
                    && bytes[i + 1] == b'.'
                    && (i + 2 >= n || !(bytes[i + 2] as char).is_ascii_digit())
                {
                    tokens.push(Token {
                        kind: TokKind::Punct,
                        text: "?.".to_string(),
                        payload: TokPayload::None,
                        offset: i,
                    });
                    i += 2;
                } else {
                    let mut matched = false;
                    for (text, len) in PUNCT {
                        // Compared as bytes: `i` is on a boundary but `i + len`
                        // need not be, and `+×` would slice into the `×`.
                        if bytes.get(i..i + len) == Some(text.as_bytes()) {
                            tokens.push(Token {
                                kind: TokKind::Punct,
                                text: text.to_string(),
                                payload: TokPayload::None,
                                offset: i,
                            });
                            i += len;
                            matched = true;
                            break;
                        }
                    }
                    if !matched {
                        tokens.push(Token {
                            kind: TokKind::Punct,
                            text: ch.to_string(),
                            payload: TokPayload::None,
                            offset: i,
                        });
                        i += 1;
                    }
                }
            }
            _ => {
                return Err(fail(i, &format!("unexpected character {ch:?}")));
            }
        }
        if tokens.len() > MAX_TOKENS {
            return Err(JsError::js("Too many tokens"));
        }
    }
    Ok(tokens)
}

fn parse_number_text(text: &str) -> f64 {
    let t = if text.starts_with('.') {
        format!("0{text}")
    } else if let Some(rest) = text.strip_suffix('.') {
        rest.to_string()
    } else {
        text.to_string()
    };
    if !t.is_empty() && t.chars().all(|c| c.is_ascii_digit()) {
        t.parse::<f64>().unwrap_or(f64::NAN)
    } else {
        t.parse::<f64>().unwrap_or(f64::NAN)
    }
}