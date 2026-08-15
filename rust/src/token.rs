//! Tokenizer ported from `jsengine.py::_Tokenizer`.

use crate::value::JsError;

/// A ceiling on how much of a page's JavaScript we will hold tokens for.
///
/// It is a guard against a runaway generator, not a judgement about what a
/// real script looks like, and at 200_000 it was making that judgement badly:
/// vimeo.com serves a single 4MB bundle that tokenizes to a little over a
/// million, so the browser refused the largest and most important script on
/// the page and reported "Too many tokens" -- which reads as a limit the
/// author of the page overstepped rather than one we picked too low.
///
/// The cost of raising it is bounded and payable: a `Token` is around 90 bytes
/// once its text is counted, so four million of them is a few hundred MB in
/// the worst case, and reaching that worst case takes a ~16MB script, larger
/// than anything the web actually ships to a browser today. The tokens are
/// also freed as soon as the parse finishes.
pub const MAX_TOKENS: usize = 4_000_000;

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
            "class", "extends", "super", "in", "instanceof", "delete",
            "void", "switch", "case", "default", "do",
        ]
        .into_iter()
        .collect()
    })
}

/// Longest match first: the scanner takes the first entry that fits, so
/// `>>>=` has to come before `>>>`, which has to come before `>>`.
///
/// The length used to be written out beside each punctuator, and two of them
/// were wrong: `>>=` and `<<=` were both listed as 2, so the scanner compared
/// two source bytes against a three-byte string, never matched, and fell
/// through to a plain `>>` followed by a separate `=`. Every `x>>=1` in every
/// minified bundle then parsed as `x >> (= 1)` and died on the `=` -- three of
/// React's shift-heavy internals on vimeo.com alone. Taking the length from
/// the string is the fix that cannot rot; they are all ASCII, so `len()` is
/// the byte count the scanner wants.
const PUNCT: &[&str] = &[
    ">>>=",
    "...", "===", "!==", "**=", "&&=", "||=", "??=", ">>>", ">>=", "<<=",
    "==", "!=", "<=", ">=", "&&", "||",
    "+=", "-=", "*=", "/=", "%=", "++", "--", "**", "&=", "|=",
    "^=", "??", "=>", ">>", "<<",
    "{", "}", "(", ")", "[", "]",
    ";", ",", ".", ":", "?", "=", "!",
    "+", "-", "*", "/", "%", "<", ">",
    "&", "|", "^", "~", "`",
];

/// The one-character escapes. `\0` is the NUL character rather than the
/// digit, and the rest of the C set (`\r`, `\v`, `\f`, `\b`) used to be
/// missing, so `"\r\n"` came out as the two letters `rn`: harmless-looking
/// until it is a delimiter something later splits on.
fn simple_esc(c: char) -> Option<char> {
    match c {
        'n' => Some('\n'),
        't' => Some('\t'),
        'r' => Some('\r'),
        'v' => Some('\u{b}'),
        'f' => Some('\u{c}'),
        'b' => Some('\u{8}'),
        '0' => Some('\0'),
        '\\' => Some('\\'),
        '\'' => Some('\''),
        '"' => Some('"'),
        '`' => Some('`'),
        _ => None,
    }
}

/// Everything after the backslash of an escape that begins at byte `i`,
/// decoded, plus the offset just past it. Shared by the string scanner and the
/// template cooker so the two cannot drift apart.
///
/// `None` for the character means the escape produced nothing at all, which is
/// what a line continuation is: a backslash and the newline it hides both
/// vanish. CRLF counts as one such newline. That last case used to be a hard
/// error -- `\` then `\r` fell through to "an unknown escape stands for
/// itself", leaving the `\n` to terminate the literal and report an
/// unterminated string on a line nowhere near the real one.
pub fn read_escape(source: &str, i: usize) -> (Option<char>, usize) {
    let n = source.len();
    if i >= n {
        return (None, n);
    }
    let esc = char_at(source, i);
    let after = i + esc.len_utf8();
    match esc {
        '\r' => {
            // A continuation, and the LF of a CRLF pair goes with it.
            if source.as_bytes().get(after) == Some(&b'\n') {
                (None, after + 1)
            } else {
                (None, after)
            }
        }
        '\n' | '\u{2028}' | '\u{2029}' => (None, after),
        'x' | 'u' => {
            // `\u{1F600}` is the variable-width form; the rest are fixed.
            if esc == 'u' && source.as_bytes().get(after) == Some(&b'{') {
                if let Some(end) = source[after + 1..].find('}') {
                    let hex = &source[after + 1..after + 1 + end];
                    let cp = u32::from_str_radix(hex, 16).ok().and_then(char::from_u32);
                    if let Some(c) = cp {
                        return (Some(c), after + 1 + end + 1);
                    }
                }
                return (Some('u'), after);
            }
            let size = if esc == 'u' { 4 } else { 2 };
            // `get` declines both a short tail and a range that would cut a
            // character in half.
            match source.get(after..after + size) {
                Some(hex) => {
                    let c = u32::from_str_radix(hex, 16).ok().and_then(char::from_u32);
                    (c, after + size)
                }
                None => (Some(esc), after),
            }
        }
        _ => match simple_esc(esc) {
            Some(c) => (Some(c), after),
            // An unrecognised escape stands for the character itself, which
            // is how `\d` inside a plain string survives.
            None => (Some(esc), after),
        },
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
        // `}` is deliberately missing from that list. It closes a block far
        // more often than it closes anything divisible, and minifiers emit
        // `...continue}}}/^.+[.-]min\.js$/.test(x)` -- a statement that starts
        // with a regex, hard up against the braces of the loop before it --
        // without a second thought. Reading that `/` as division swallowed the
        // rest of the file and reported a stray backslash hundreds of lines
        // later. The case it gives up is dividing by an object literal,
        // `{a:1}/2`, which is not something any real program writes: an object
        // literal in a position where division could follow it needs
        // parentheses, and then it is `)` that precedes the slash, not `}`.
        TokKind::Punct => !matches!(prev.text.as_str(), ")" | "]" | "++" | "--"),
    }
}

/// The byte offset just past the backtick that closes the template starting at
/// byte offset `start`.
///
/// Everything here counts in bytes. It used to collect the whole source into a
/// `Vec<char>` and index that, while its caller passed -- and then used the
/// result as -- a byte offset. The two agree exactly as long as the file is
/// pure ASCII, which is why it survived the test suite and died on the open
/// web: one `×` or one emoji anywhere earlier in a bundle slides every
/// subsequent char index behind its byte offset, so the scan starts in the
/// middle of some unrelated expression and the token it hands back spans the
/// wrong bytes. From there the tokenizer resumes at a nonsense offset and the
/// rest of the file is garbage -- which is what the "unexpected character
/// '\'" and "unterminated string literal" reports from vimeo.com and MDN
/// really were, several hundred kilobytes downstream of the actual fault.
///
/// Collecting the source per template was also quadratic, and a 4MB bundle has
/// thousands of them; walking bytes fixes that as a side effect.
fn find_template_end(s: &str, start: usize) -> Option<usize> {
    let b = s.as_bytes();
    let n = b.len();
    // A `\` escape always covers exactly one following character, and the
    // scanner only ever needs to step over it, so the character's own width is
    // what matters -- stepping a fixed two bytes would land inside a `\×`.
    let step = |i: usize| -> usize {
        if i >= n {
            return n;
        }
        i + char_at(s, i).len_utf8()
    };
    let mut i = start + 1;
    while i < n {
        match b[i] {
            b'\\' => i = step(i + 1),
            b'$' if i + 1 < n && b[i + 1] == b'{' => match find_subst_end(s, i) {
                Some(close) => i = close + 1,
                None => return None,
            },
            b'`' => return Some(i + 1),
            _ => i = step(i),
        }
    }
    None
}

/// The byte offset of the `}` closing the `${` that begins at byte offset
/// `dollar`.
///
/// Braces nest, a brace inside a string does not count, and a nested template
/// brings both of those back again one level down -- `` `${ {a: `${b}`} }` ``
/// is a thing people write. Recursing through `find_template_end` for the
/// nested case is what keeps that straight; treating a backtick as just
/// another quote character, as this used to, loses count the moment the inner
/// template has a substitution of its own.
///
/// Both the tokenizer (to find where a template token ends) and the parser (to
/// cut the template into its literal and expression halves) need exactly this
/// walk, and they disagreed subtly when they each had their own copy.
pub fn find_subst_end(s: &str, dollar: usize) -> Option<usize> {
    let b = s.as_bytes();
    let n = b.len();
    let step = |i: usize| -> usize {
        if i >= n {
            n
        } else {
            i + char_at(s, i).len_utf8()
        }
    };
    let mut j = dollar + 2;
    let mut depth = 1i64;
    let mut quote: Option<u8> = None;
    while j < n {
        let c = b[j];
        match quote {
            Some(q) => {
                if c == b'\\' {
                    j = step(j + 1);
                    continue;
                }
                if c == q {
                    quote = None;
                }
            }
            None => match c {
                b'\'' | b'"' => quote = Some(c),
                b'`' => match find_template_end(s, j) {
                    Some(end) => {
                        j = end;
                        continue;
                    }
                    None => return None,
                },
                b'{' => depth += 1,
                b'}' => {
                    depth -= 1;
                    if depth == 0 {
                        return Some(j);
                    }
                }
                _ => {}
            },
        }
        j = step(j);
    }
    None
}

/// A line number is a fine thing to be told about a hand-written script and
/// almost useless for a minified bundle, where the whole file is line 1 and
/// the fault is somewhere in the next four megabytes. This appends the source
/// either side of the offending offset, which is the difference between
/// "something is wrong with vimeo.com" and a snippet you can paste into a
/// test. It is off unless FEETBROWSER_JS_DEBUG is set, because the excerpt is
/// page-controlled text and the error string ends up in the UI.
pub fn near(source: &str, offset: usize) -> String {
    if std::env::var_os("FEETBROWSER_JS_DEBUG").is_none() {
        return String::new();
    }
    // Byte offsets from the scanners land on character boundaries, but the
    // window either side of one need not, so walk in to the nearest.
    let at = offset.min(source.len());
    let mut lo = at.saturating_sub(60);
    while lo < at && !source.is_char_boundary(lo) {
        lo += 1;
    }
    let mut hi = (at + 60).min(source.len());
    while hi > at && !source.is_char_boundary(hi) {
        hi -= 1;
    }
    format!(" at {offset} near {:?}", &source[lo..hi])
}

/// The character at byte offset `i`, which is not the same thing as the byte
/// there. Reading `bytes[i] as char` is a Latin-1 misreading of the leading
/// byte of a UTF-8 sequence: `×` scans as `Ã` followed by a control character,
/// which is both wrong (string and regex literals came out as mojibake) and
/// fatal (`Ã` is alphabetic, so the identifier scanner accepted it, then
/// stopped one byte in and sliced through the middle of the character). Every
/// scanner below decodes a real character and advances by `len_utf8`, so the
/// cursor only ever lands on a boundary.
pub fn char_at(source: &str, i: usize) -> char {
    source[i..].chars().next().unwrap_or('\0')
}

pub fn tokenize(source: &str) -> Result<Vec<Token>, JsError> {
    let kw = keywords();
    // Minified JavaScript runs about six bytes to the token, so this is one
    // allocation instead of the twenty-odd doublings a megabyte-scale bundle
    // would otherwise walk through.
    let mut tokens: Vec<Token> = Vec::with_capacity(source.len() / 6 + 16);
    let bytes = source.as_bytes();
    let mut i = 0usize;
    let n = bytes.len();
    let fail = |offset: usize, msg: &str| -> JsError {
        let line = source[..offset.min(source.len())].matches('\n').count() + 1;
        JsError::js(format!("SyntaxError on line {line}: {msg}{}", near(source, offset)))
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
                // `_` is a digit separator anywhere inside a numeric literal,
                // and carries no meaning beyond being readable, so every
                // radix below simply drops it before converting. `0o` (octal)
                // used to be missing altogether, which did not fail loudly:
                // `0o17` scanned as the number `0` followed by the identifier
                // `o17`, and the expression quietly went on with the wrong
                // value.
                let radix = if ch == '0' && i + 1 < n {
                    match bytes[i + 1] {
                        b'x' | b'X' => Some(16u32),
                        b'o' | b'O' => Some(8),
                        b'b' | b'B' => Some(2),
                        _ => None,
                    }
                } else {
                    None
                };
                let mut j = i;
                if let Some(radix) = radix {
                    j = i + 2;
                    while j < n
                        && (bytes[j] == b'_' || (bytes[j] as char).is_digit(radix))
                    {
                        j += 1;
                    }
                    let digits: String =
                        source[i + 2..j].chars().filter(|&c| c != '_').collect();
                    // Past 2^53 an f64 cannot hold the integer exactly, but
                    // that is the only number type there is here, so the
                    // rounding is the same one every JS engine does.
                    let v = u128::from_str_radix(&digits, radix).unwrap_or(0);
                    // A trailing `n` makes it a BigInt. There is no BigInt
                    // here, but swallowing the suffix keeps the token stream
                    // in step instead of leaving a stray identifier behind.
                    if j < n && bytes[j] == b'n' {
                        j += 1;
                    }
                    tokens.push(Token {
                        kind: TokKind::Number,
                        text: source[i..j].to_string(),
                        payload: TokPayload::Number(v as f64),
                        offset: i,
                    });
                    i = j;
                } else {
                    let digit = |k: usize| {
                        k < n && (bytes[k] == b'_' || (bytes[k] as char).is_ascii_digit())
                    };
                    while digit(j) {
                        j += 1;
                    }
                    if j < n && bytes[j] == b'.' {
                        j += 1;
                        while digit(j) {
                            j += 1;
                        }
                    }
                    if j < n && matches!(bytes[j], b'e' | b'E') {
                        let mut k = j + 1;
                        if k < n && matches!(bytes[k], b'+' | b'-') {
                            k += 1;
                        }
                        if k < n && (bytes[k] as char).is_ascii_digit() {
                            while digit(k) {
                                k += 1;
                            }
                            j = k;
                        }
                    }
                    let text = &source[i..j];
                    let num = parse_number_text(&text.replace('_', ""));
                    if j < n && bytes[j] == b'n' {
                        j += 1;
                    }
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
                        if i + 1 >= n {
                            return Err(fail(i, "unterminated string literal"));
                        }
                        let (decoded, next) = read_escape(source, i + 1);
                        if let Some(d) = decoded {
                            buf.push(d);
                        }
                        i = next;
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
            _ if ch.is_alphabetic()
                || ch == '_'
                || ch == '$'
                || (ch == '#' && bytes.get(i + 1).is_some_and(|c| {
                    let c = *c as char;
                    c.is_alphabetic() || c == '_' || c == '$'
                }))
                || (ch == '\\' && bytes.get(i + 1) == Some(&b'u')) =>
            {
                // An identifier may spell any of its characters as `\uXXXX`,
                // and the name that results is the decoded one -- `abc`
                // and `abc` are the same variable. Minifiers have no reason to
                // emit it, but hand-written code on the open web does, and the
                // scanner did not start on a backslash at all, so the `\` fell
                // through to "unexpected character" and took the rest of the
                // file with it.
                let mut name = String::new();
                let mut j = i;
                // A `#name` is one token, not a punctuator and a name. Keeping
                // the hash inside the identifier is the whole of this engine's
                // support for private members: `#count` becomes an ordinary
                // property whose name happens to start with a character no
                // source file can spell any other way, so `this.#count`,
                // `#count = 0` in a class body and `#count in obj` all fall
                // out of the code that was already there. What it does not
                // give is the privacy -- the field is enumerable and reachable
                // from outside -- which no page has ever depended on, while
                // the syntax is now common enough that refusing it cost whole
                // bundles.
                if ch == '#' {
                    name.push('#');
                    j += 1;
                }
                while j < n {
                    let c = char_at(source, j);
                    if c == '\\' && bytes.get(j + 1) == Some(&b'u') {
                        let (decoded, next) = read_escape(source, j + 1);
                        match decoded {
                            Some(d) if d.is_alphanumeric() || d == '_' || d == '$' => {
                                name.push(d);
                                j = next;
                                continue;
                            }
                            _ => break,
                        }
                    }
                    if !(c.is_alphanumeric() || c == '_' || c == '$') {
                        break;
                    }
                    name.push(c);
                    j += c.len_utf8();
                }
                if name.is_empty() {
                    return Err(fail(i, &format!("unexpected character {ch:?}")));
                }
                let is_kw = kw.contains(name.as_str());
                tokens.push(Token {
                    kind: if is_kw { TokKind::Kw } else { TokKind::Ident },
                    text: name,
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
                    for text in PUNCT {
                        // Compared as bytes: `i` is on a boundary but
                        // `i + len` need not be, and `+×` would slice into
                        // the `×`.
                        let len = text.len();
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