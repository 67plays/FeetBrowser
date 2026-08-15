//! A JavaScript-flavoured regular-expression engine, ported from `regex.zig`.
//!
//! The shape is deliberately boring: a recursive-descent parser turns the
//! pattern into a small tree of `Node`s held in flat arrays, and a
//! backtracking matcher walks that tree with an explicit continuation chain
//! threaded through the Rust call stack. Backreferences and lookaround rule
//! out a Thompson/Pike NFA, so backtracking it is -- and backtracking means
//! the engine has to be able to give up: every step through `run`/`run_cont`
//! burns one unit of a fixed budget, and blowing the budget fails the whole
//! `exec` rather than hanging the browser on `/(a+)+b/`.
//!
//! Matching is over UTF-8 *code points*, not bytes: `.`, character classes
//! and literals decode one code point at a time, so a match can never end
//! halfway through a multi-byte sequence. Bytes that are not valid UTF-8 are
//! treated as one-byte Latin-1 code points, which keeps the decoder total.
//! Offsets in and out are therefore byte offsets into the subject.
//!
//! This is the whole of our regex support: the browser owns its stack, and a
//! third-party crate would neither give us lookbehind nor let us bound the
//! work a hostile page can ask for.

/// Half-open byte range of the subject, as handed back in a capture list.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Span {
    pub start: u32,
    pub end: u32,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Flags {
    pub global: bool,
    pub ignore_case: bool,
    pub multiline: bool,
    pub dot_all: bool,
    pub sticky: bool,
    pub unicode: bool,
}

/// The one error the compiler reports. A page whose pattern we cannot parse
/// gets a `SyntaxError`, not a guess at what it meant.
#[derive(Debug, Clone, Copy)]
pub struct BadPattern;

/// How many matcher steps a single `exec` call may take before it gives up
/// and reports "no match". One step is roughly one node visit.
pub const MAX_STEPS: u64 = 1_000_000;

/// How deep the mutual recursion between `run` and `run_cont` may go. Simple
/// quantifiers (`a*`, `.+`, `[a-z]{2,}`) run iteratively and do not count
/// against this, so only nested-group repetition can reach it.
pub const MAX_DEPTH: u32 = 4000;

/// How deep groups may nest before we call the pattern unreasonable.
const MAX_NESTING: u32 = 200;

// -- compiled form ---------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Tag {
    Empty,
    Lit,
    Any,
    Class,
    Seq,
    Alt,
    Repeat,
    Group,
    Look,
    Backref,
    Bol,
    Eol,
    Wordb,
    Nwordb,
}

#[derive(Debug, Clone, Copy)]
struct Node {
    tag: Tag,
    /// `Lit`: the code point.
    ch: u32,
    /// `Class`: index into `classes`.
    cls: u32,
    /// `Seq`/`Alt`: window into `kids`.
    kid_start: u32,
    kid_len: u32,
    /// `Repeat`/`Group`/`Look`: the single child node.
    child: u32,
    /// `Repeat`: bounds and laziness.
    min: u32,
    max: u32,
    greedy: bool,
    /// `Group`: 1-based capture index, 0 for a non-capturing group.
    cap: u32,
    /// `Look`: direction, polarity, and (behind only) fixed width in code points.
    ahead: bool,
    negate: bool,
    width: u32,
    /// `Repeat`/`Look`: inclusive range of capture indices underneath, for the
    /// per-iteration capture reset. `cap_lo > cap_hi` means "no groups".
    cap_lo: u32,
    cap_hi: u32,
    /// `Backref`: the group it refers to.
    ref_: u32,
}

impl Node {
    fn new(tag: Tag) -> Node {
        Node {
            tag,
            ch: 0,
            cls: 0,
            kid_start: 0,
            kid_len: 0,
            child: 0,
            min: 0,
            max: 0,
            greedy: true,
            cap: 0,
            ahead: true,
            negate: false,
            width: 0,
            cap_lo: 1,
            cap_hi: 0,
            ref_: 0,
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct Range {
    lo: u32,
    hi: u32,
}

#[derive(Debug, Clone, Copy)]
struct Class {
    /// Membership for code points 0..127, the hot path.
    bitmap: [u64; 2],
    /// Sorted, disjoint ranges for code points >= 128, in `ranges`.
    r_start: u32,
    r_len: u32,
}

pub struct Regex {
    /// Number of capturing groups, NOT counting group 0.
    pub group_count: u32,
    pub flags: Flags,
    /// Names of named groups, parallel to group index (1-based); an empty
    /// string for unnamed groups. Length is `group_count + 1`, index 0 unused.
    pub group_names: Vec<String>,

    // internals
    nodes: Vec<Node>,
    kids: Vec<u32>,
    ranges: Vec<Range>,
    classes: Vec<Class>,
    root: u32,
}

impl Regex {
    /// `flags` is the raw flag string, e.g. "gi". Unknown flag letters are a
    /// `BadPattern`.
    pub fn compile(pattern: &str, flags: &str) -> Result<Regex, BadPattern> {
        let f = parse_flags(flags)?;

        let mut names: Vec<String> = vec![String::new()];
        prescan_groups(pattern.as_bytes(), &mut names)?;
        let group_count = (names.len() - 1) as u32;

        let mut p = P {
            pat: pattern.as_bytes(),
            i: 0,
            nodes: Vec::new(),
            kids: Vec::new(),
            ranges: Vec::new(),
            classes: Vec::new(),
            flags: f,
            group_count,
            names,
            next_group: 0,
            depth: 0,
        };

        let root = parse_alternation(&mut p)?;
        if p.i != p.pat.len() {
            return Err(BadPattern); // stray ')'
        }

        Ok(Regex {
            group_count,
            flags: f,
            group_names: p.names,
            nodes: p.nodes,
            kids: p.kids,
            ranges: p.ranges,
            classes: p.classes,
            root,
        })
    }

    /// Index of the named group, or `None`. Handy for the `groups` object.
    pub fn group_index(&self, name: &str) -> Option<u32> {
        for (i, n) in self.group_names.iter().enumerate() {
            if i > 0 && n == name {
                return Some(i as u32);
            }
        }
        None
    }

    /// True if any group in the pattern was given a name.
    pub fn has_named_groups(&self) -> bool {
        self.group_names.iter().skip(1).any(|n| !n.is_empty())
    }

    /// Try to match starting at or after byte offset `start`. On success the
    /// returned list has `group_count + 1` entries: entry 0 is the whole
    /// match and entry i is group i, or `None` if that group did not take
    /// part. If the sticky flag is set, only offset `start` itself is tried.
    pub fn exec(&self, input: &str, start: usize) -> Option<Vec<Option<Span>>> {
        let bytes = input.as_bytes();
        if start > bytes.len() {
            return None;
        }
        let mut st = St {
            re: self,
            input: bytes,
            caps: vec![None; self.group_count as usize + 1],
            steps: 0,
            depth: 0,
            aborted: false,
            match_start: start as u32,
        };

        let mut at = start as u32;
        loop {
            for c in st.caps.iter_mut() {
                *c = None;
            }
            st.depth = 0;
            st.match_start = at;
            if run(&mut st, self.root, at, None) {
                return Some(st.caps);
            }
            if st.aborted || self.flags.sticky || at as usize >= bytes.len() {
                return None;
            }
            at += decode(bytes, at).len;
        }
    }
}

// -- flags -----------------------------------------------------------------

fn parse_flags(s: &str) -> Result<Flags, BadPattern> {
    let mut f = Flags::default();
    for c in s.chars() {
        let slot = match c {
            'g' => &mut f.global,
            'i' => &mut f.ignore_case,
            'm' => &mut f.multiline,
            's' => &mut f.dot_all,
            'y' => &mut f.sticky,
            'u' => &mut f.unicode,
            _ => return Err(BadPattern),
        };
        if *slot {
            return Err(BadPattern);
        }
        *slot = true;
    }
    Ok(f)
}

// -- UTF-8 -----------------------------------------------------------------

#[derive(Clone, Copy)]
struct Dec {
    cp: u32,
    len: u32,
}

fn is_cont(b: u8) -> bool {
    b & 0xC0 == 0x80
}

/// Total decoder: anything that is not a well-formed sequence comes back as a
/// single Latin-1 byte, so the matcher can never trip over bad input.
fn decode(s: &[u8], i: u32) -> Dec {
    let i = i as usize;
    let b = s[i];
    if b < 0x80 {
        return Dec { cp: b as u32, len: 1 };
    }
    let rest = s.len() - i;
    if (0xC2..=0xDF).contains(&b) && rest >= 2 && is_cont(s[i + 1]) {
        return Dec {
            cp: (((b & 0x1F) as u32) << 6) | (s[i + 1] & 0x3F) as u32,
            len: 2,
        };
    }
    if (0xE0..=0xEF).contains(&b) && rest >= 3 && is_cont(s[i + 1]) && is_cont(s[i + 2]) {
        return Dec {
            cp: (((b & 0x0F) as u32) << 12)
                | (((s[i + 1] & 0x3F) as u32) << 6)
                | (s[i + 2] & 0x3F) as u32,
            len: 3,
        };
    }
    if (0xF0..=0xF4).contains(&b)
        && rest >= 4
        && is_cont(s[i + 1])
        && is_cont(s[i + 2])
        && is_cont(s[i + 3])
    {
        return Dec {
            cp: (((b & 0x07) as u32) << 18)
                | (((s[i + 1] & 0x3F) as u32) << 12)
                | (((s[i + 2] & 0x3F) as u32) << 6)
                | (s[i + 3] & 0x3F) as u32,
            len: 4,
        };
    }
    Dec { cp: b as u32, len: 1 }
}

/// Start offset of the code point that ends at `p`. Exactly inverts `decode`.
fn prev_start(s: &[u8], p: u32) -> u32 {
    let mut j = p - 1;
    while j > 0 && p - j < 4 && is_cont(s[j as usize]) {
        j -= 1;
    }
    if decode(s, j).len == p - j {
        j
    } else {
        p - 1
    }
}

fn step_back(s: &[u8], pos: u32, count: u32) -> Option<u32> {
    let mut p = pos;
    for _ in 0..count {
        if p == 0 {
            return None;
        }
        p = prev_start(s, p);
    }
    Some(p)
}

fn fold_ascii(c: u32) -> u32 {
    if (b'A' as u32..=b'Z' as u32).contains(&c) {
        c + 32
    } else {
        c
    }
}

fn is_line_term(cp: u32) -> bool {
    cp == 0x0A || cp == 0x0D || cp == 0x2028 || cp == 0x2029
}

fn is_word_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

// -- predefined sets -------------------------------------------------------
// Each list is sorted and disjoint; `append_complement` relies on that.

const DIGIT_SET: [Range; 1] = [Range { lo: 0x30, hi: 0x39 }];

const WORD_SET: [Range; 4] = [
    Range { lo: 0x30, hi: 0x39 },
    Range { lo: 0x41, hi: 0x5A },
    Range { lo: 0x5F, hi: 0x5F },
    Range { lo: 0x61, hi: 0x7A },
];

const SPACE_SET: [Range; 10] = [
    Range { lo: 0x09, hi: 0x0D },
    Range { lo: 0x20, hi: 0x20 },
    Range { lo: 0xA0, hi: 0xA0 },
    Range { lo: 0x1680, hi: 0x1680 },
    Range { lo: 0x2000, hi: 0x200A },
    Range { lo: 0x2028, hi: 0x2029 },
    Range { lo: 0x202F, hi: 0x202F },
    Range { lo: 0x205F, hi: 0x205F },
    Range { lo: 0x3000, hi: 0x3000 },
    Range { lo: 0xFEFF, hi: 0xFEFF },
];

fn append_complement(list: &mut Vec<Range>, src: &[Range]) {
    let mut next: u32 = 0;
    for r in src {
        if r.lo > next {
            list.push(Range { lo: next, hi: r.lo - 1 });
        }
        if r.hi + 1 > next {
            next = r.hi + 1;
        }
    }
    if next <= 0x10FFFF {
        list.push(Range { lo: next, hi: 0x10FFFF });
    }
}

fn sort_merge(list: &mut Vec<Range>) {
    if list.is_empty() {
        return;
    }
    list.sort_by_key(|r| r.lo);
    let mut w = 0usize;
    for i in 1..list.len() {
        let r = list[i];
        if list[w].hi + 1 >= r.lo {
            if r.hi > list[w].hi {
                list[w].hi = r.hi;
            }
        } else {
            w += 1;
            list[w] = r;
        }
    }
    list.truncate(w + 1);
}

// -- parser ----------------------------------------------------------------

struct P<'a> {
    pat: &'a [u8],
    i: usize,
    nodes: Vec<Node>,
    kids: Vec<u32>,
    ranges: Vec<Range>,
    classes: Vec<Class>,
    flags: Flags,
    group_count: u32,
    names: Vec<String>,
    next_group: u32,
    /// Group nesting, so a hostile page cannot overflow the parser's stack
    /// with `((((((...))))))`.
    depth: u32,
}

impl<'a> P<'a> {
    fn at(&self) -> Option<u8> {
        self.pat.get(self.i).copied()
    }
}

fn add(p: &mut P, n: Node) -> Result<u32, BadPattern> {
    p.nodes.push(n);
    Ok((p.nodes.len() - 1) as u32)
}

/// Walk the pattern once up front so backreferences can point forwards and
/// `\k<name>` can be resolved before the group is parsed.
fn prescan_groups(pat: &[u8], names: &mut Vec<String>) -> Result<(), BadPattern> {
    let mut i = 0usize;
    let mut in_class = false;
    while i < pat.len() {
        let c = pat[i];
        if c == b'\\' {
            i += 2;
            continue;
        }
        if in_class {
            if c == b']' {
                in_class = false;
            }
            i += 1;
            continue;
        }
        if c == b'[' {
            in_class = true;
            i += 1;
            continue;
        }
        if c == b'(' {
            if i + 1 < pat.len() && pat[i + 1] == b'?' {
                if i + 3 < pat.len() && pat[i + 2] == b'<' && pat[i + 3] != b'=' && pat[i + 3] != b'!'
                {
                    let mut j = i + 3;
                    while j < pat.len() && pat[j] != b'>' {
                        j += 1;
                    }
                    if j >= pat.len() || j == i + 3 {
                        return Err(BadPattern);
                    }
                    names.push(String::from_utf8_lossy(&pat[i + 3..j]).into_owned());
                    i = j + 1;
                    continue;
                }
            } else {
                names.push(String::new());
            }
        }
        i += 1;
    }
    Ok(())
}

fn parse_alternation(p: &mut P) -> Result<u32, BadPattern> {
    let mut branches = vec![parse_sequence(p)?];
    while p.at() == Some(b'|') {
        p.i += 1;
        branches.push(parse_sequence(p)?);
    }
    if branches.len() == 1 {
        return Ok(branches[0]);
    }
    let start = p.kids.len() as u32;
    p.kids.extend_from_slice(&branches);
    let mut n = Node::new(Tag::Alt);
    n.kid_start = start;
    n.kid_len = branches.len() as u32;
    add(p, n)
}

fn parse_sequence(p: &mut P) -> Result<u32, BadPattern> {
    let mut items: Vec<u32> = Vec::new();
    while p.i < p.pat.len() && p.pat[p.i] != b'|' && p.pat[p.i] != b')' {
        items.push(parse_term(p)?);
    }
    if items.is_empty() {
        return add(p, Node::new(Tag::Empty));
    }
    if items.len() == 1 {
        return Ok(items[0]);
    }
    let start = p.kids.len() as u32;
    p.kids.extend_from_slice(&items);
    let mut n = Node::new(Tag::Seq);
    n.kid_start = start;
    n.kid_len = items.len() as u32;
    add(p, n)
}

struct Quant {
    min: u32,
    max: u32,
    greedy: bool,
}

fn parse_quantifier(p: &mut P) -> Result<Option<Quant>, BadPattern> {
    let c = match p.at() {
        Some(c) => c,
        None => return Ok(None),
    };
    let min;
    let mut max;
    match c {
        b'*' => {
            p.i += 1;
            min = 0;
            max = u32::MAX;
        }
        b'+' => {
            p.i += 1;
            min = 1;
            max = u32::MAX;
        }
        b'?' => {
            p.i += 1;
            min = 0;
            max = 1;
        }
        b'{' => {
            let save = p.i;
            p.i += 1;
            let lo = match read_int(p) {
                Some(v) => v,
                None => {
                    p.i = save;
                    return Ok(None);
                }
            };
            min = lo;
            max = lo;
            if p.at() == Some(b',') {
                p.i += 1;
                if p.at() == Some(b'}') {
                    max = u32::MAX;
                } else {
                    max = match read_int(p) {
                        Some(v) => v,
                        None => {
                            p.i = save;
                            return Ok(None);
                        }
                    };
                }
            }
            if p.at() != Some(b'}') {
                p.i = save;
                return Ok(None);
            }
            p.i += 1;
            // A well-formed but backwards range is a syntax error, not a literal.
            if min > max {
                return Err(BadPattern);
            }
        }
        _ => return Ok(None),
    }
    let mut greedy = true;
    if p.at() == Some(b'?') {
        p.i += 1;
        greedy = false;
    }
    Ok(Some(Quant { min, max, greedy }))
}

fn read_int(p: &mut P) -> Option<u32> {
    let start = p.i;
    let mut v: u64 = 0;
    while p.i < p.pat.len() && p.pat[p.i].is_ascii_digit() {
        v = v * 10 + (p.pat[p.i] - b'0') as u64;
        if v > 1_000_000 {
            v = 1_000_000; // saturate; nobody means it
        }
        p.i += 1;
    }
    if p.i == start {
        return None;
    }
    Some(v as u32)
}

fn parse_term(p: &mut P) -> Result<u32, BadPattern> {
    let before = p.next_group;
    let atom = parse_atom(p)?;
    let q = match parse_quantifier(p)? {
        Some(q) => q,
        None => return Ok(atom),
    };

    // `a**` and friends: one quantifier per atom.
    if let Some(c) = p.at() {
        if c == b'*' || c == b'+' || c == b'?' {
            return Err(BadPattern);
        }
    }

    let mut n = Node::new(Tag::Repeat);
    n.child = atom;
    n.min = q.min;
    n.max = q.max;
    n.greedy = q.greedy;
    n.cap_lo = before + 1;
    n.cap_hi = p.next_group;
    add(p, n)
}

fn parse_atom(p: &mut P) -> Result<u32, BadPattern> {
    let c = p.at().ok_or(BadPattern)?;
    match c {
        b'^' => {
            p.i += 1;
            add(p, Node::new(Tag::Bol))
        }
        b'$' => {
            p.i += 1;
            add(p, Node::new(Tag::Eol))
        }
        b'.' => {
            p.i += 1;
            add(p, Node::new(Tag::Any))
        }
        b'(' => parse_group(p),
        b'[' => {
            let ci = parse_class(p)?;
            let mut n = Node::new(Tag::Class);
            n.cls = ci;
            add(p, n)
        }
        b'\\' => parse_escape(p),
        b'*' | b'+' | b'?' => Err(BadPattern), // nothing to repeat
        b'{' => {
            // A `{` that parses as a quantifier here has nothing to quantify.
            let save = p.i;
            if parse_quantifier(p)?.is_some() {
                return Err(BadPattern);
            }
            p.i = save + 1;
            let mut n = Node::new(Tag::Lit);
            n.ch = b'{' as u32;
            add(p, n)
        }
        _ => {
            let d = decode(p.pat, p.i as u32);
            p.i += d.len as usize;
            let mut n = Node::new(Tag::Lit);
            n.ch = d.cp;
            add(p, n)
        }
    }
}

#[derive(PartialEq, Eq, Clone, Copy)]
enum GroupKind {
    Cap,
    Ncap,
    Ahead,
    Nahead,
    Behind,
    Nbehind,
}

fn parse_group(p: &mut P) -> Result<u32, BadPattern> {
    p.depth += 1;
    if p.depth > MAX_NESTING {
        return Err(BadPattern);
    }
    let out = parse_group_inner(p);
    p.depth -= 1;
    out
}

fn parse_group_inner(p: &mut P) -> Result<u32, BadPattern> {
    let before = p.next_group;
    p.i += 1; // '('

    let mut kind = GroupKind::Cap;
    if p.at() == Some(b'?') {
        p.i += 1;
        let c = p.at().ok_or(BadPattern)?;
        match c {
            b':' => {
                p.i += 1;
                kind = GroupKind::Ncap;
            }
            b'=' => {
                p.i += 1;
                kind = GroupKind::Ahead;
            }
            b'!' => {
                p.i += 1;
                kind = GroupKind::Nahead;
            }
            b'<' => {
                p.i += 1;
                let d = p.at().ok_or(BadPattern)?;
                if d == b'=' {
                    p.i += 1;
                    kind = GroupKind::Behind;
                } else if d == b'!' {
                    p.i += 1;
                    kind = GroupKind::Nbehind;
                } else {
                    // (?<name>...) -- the name was already recorded by the prescan.
                    while p.i < p.pat.len() && p.pat[p.i] != b'>' {
                        p.i += 1;
                    }
                    if p.i >= p.pat.len() {
                        return Err(BadPattern);
                    }
                    p.i += 1;
                    kind = GroupKind::Cap;
                }
            }
            _ => return Err(BadPattern),
        }
    }

    let mut cap_idx = 0u32;
    if kind == GroupKind::Cap {
        p.next_group += 1;
        cap_idx = p.next_group;
        if cap_idx > p.group_count {
            return Err(BadPattern);
        }
    }

    let child = parse_alternation(p)?;
    if p.at() != Some(b')') {
        return Err(BadPattern);
    }
    p.i += 1;

    match kind {
        GroupKind::Cap | GroupKind::Ncap => {
            let mut n = Node::new(Tag::Group);
            n.child = child;
            n.cap = cap_idx;
            add(p, n)
        }
        GroupKind::Ahead | GroupKind::Nahead => {
            let mut n = Node::new(Tag::Look);
            n.child = child;
            n.ahead = true;
            n.negate = kind == GroupKind::Nahead;
            n.cap_lo = before + 1;
            n.cap_hi = p.next_group;
            add(p, n)
        }
        GroupKind::Behind | GroupKind::Nbehind => {
            let w = fixed_width(&p.nodes, &p.kids, child).ok_or(BadPattern)?;
            let mut n = Node::new(Tag::Look);
            n.child = child;
            n.ahead = false;
            n.negate = kind == GroupKind::Nbehind;
            n.width = w;
            n.cap_lo = before + 1;
            n.cap_hi = p.next_group;
            add(p, n)
        }
    }
}

/// Width of a subpattern in code points, or `None` if it is not fixed. Only
/// lookbehind needs this -- we match lookbehind bodies left-to-right from a
/// computed start offset instead of implementing the spec's right-to-left
/// matcher, so variable-width lookbehind is rejected at compile time.
fn fixed_width(nodes: &[Node], kids: &[u32], idx: u32) -> Option<u32> {
    let n = nodes[idx as usize];
    match n.tag {
        Tag::Empty | Tag::Bol | Tag::Eol | Tag::Wordb | Tag::Nwordb | Tag::Look => Some(0),
        Tag::Lit | Tag::Any | Tag::Class => Some(1),
        Tag::Group => fixed_width(nodes, kids, n.child),
        Tag::Seq => {
            let mut total = 0u32;
            for i in 0..n.kid_len {
                total += fixed_width(nodes, kids, kids[(n.kid_start + i) as usize])?;
            }
            Some(total)
        }
        Tag::Alt => {
            let mut first: Option<u32> = None;
            for i in 0..n.kid_len {
                let w = fixed_width(nodes, kids, kids[(n.kid_start + i) as usize])?;
                match first {
                    Some(f) if f != w => return None,
                    Some(_) => {}
                    None => first = Some(w),
                }
            }
            Some(first.unwrap_or(0))
        }
        Tag::Repeat => {
            if n.min != n.max {
                return None;
            }
            let w = fixed_width(nodes, kids, n.child)?;
            Some(n.min * w)
        }
        Tag::Backref => None,
    }
}

fn parse_escape(p: &mut P) -> Result<u32, BadPattern> {
    p.i += 1; // '\'
    let e = p.at().ok_or(BadPattern)?; // trailing backslash
    match e {
        b'd' | b'D' | b'w' | b'W' | b's' | b'S' => {
            p.i += 1;
            let src: &[Range] = match e {
                b'd' | b'D' => &DIGIT_SET,
                b'w' | b'W' => &WORD_SET,
                _ => &SPACE_SET,
            };
            let mut list = src.to_vec();
            let neg = e == b'D' || e == b'W' || e == b'S';
            let ci = build_class(p, &mut list, neg);
            let mut n = Node::new(Tag::Class);
            n.cls = ci;
            add(p, n)
        }
        b'b' => {
            p.i += 1;
            add(p, Node::new(Tag::Wordb))
        }
        b'B' => {
            p.i += 1;
            add(p, Node::new(Tag::Nwordb))
        }
        b'1'..=b'9' => {
            let save = p.i;
            let n = read_int(p).unwrap();
            if n >= 1 && n <= p.group_count {
                let mut node = Node::new(Tag::Backref);
                node.ref_ = n;
                return add(p, node);
            }
            // No such group: Annex B rereads it as a legacy octal escape
            // (`\8` and `\9` as plain identity escapes). V8 does this too, and
            // a page whose regex is subtly wrong should still load.
            p.i = save;
            let mut node = Node::new(Tag::Lit);
            node.ch = read_octal(p);
            add(p, node)
        }
        b'k' => {
            p.i += 1;
            if p.at() != Some(b'<') {
                return Err(BadPattern);
            }
            p.i += 1;
            let s = p.i;
            while p.i < p.pat.len() && p.pat[p.i] != b'>' {
                p.i += 1;
            }
            if p.i >= p.pat.len() {
                return Err(BadPattern);
            }
            let name = String::from_utf8_lossy(&p.pat[s..p.i]).into_owned();
            p.i += 1;
            // Resolve the name to a group number first: `add` wants the whole
            // parser state mutably, so the lookup has to be finished before it.
            let found = p
                .names
                .iter()
                .enumerate()
                .find(|(gi, nm)| *gi > 0 && !nm.is_empty() && **nm == name)
                .map(|(gi, _)| gi as u32);
            match found {
                Some(gi) => {
                    let mut node = Node::new(Tag::Backref);
                    node.ref_ = gi;
                    add(p, node)
                }
                None => Err(BadPattern),
            }
        }
        _ => {
            let cp = escape_char(p)?;
            let mut n = Node::new(Tag::Lit);
            n.ch = cp;
            add(p, n)
        }
    }
}

/// Decodes an escape that stands for a single code point. `p.i` points at the
/// character after the backslash.
fn escape_char(p: &mut P) -> Result<u32, BadPattern> {
    let e = p.pat[p.i];
    match e {
        b'n' => {
            p.i += 1;
            Ok(0x0A)
        }
        b'r' => {
            p.i += 1;
            Ok(0x0D)
        }
        b't' => {
            p.i += 1;
            Ok(0x09)
        }
        b'f' => {
            p.i += 1;
            Ok(0x0C)
        }
        b'v' => {
            p.i += 1;
            Ok(0x0B)
        }
        b'0'..=b'7' => Ok(read_octal(p)),
        b'x' => {
            p.i += 1;
            // `\x` without two hex digits is an identity escape for 'x'.
            Ok(read_hex(p, 2).unwrap_or(b'x' as u32))
        }
        b'u' => {
            p.i += 1;
            if p.at() == Some(b'{') {
                let save = p.i;
                p.i += 1;
                let mut v: u32 = 0;
                let mut count = 0u32;
                while p.i < p.pat.len() && p.pat[p.i] != b'}' {
                    match hex_val(p.pat[p.i]) {
                        Some(h) => v = v * 16 + h,
                        None => {
                            v = 0x110000;
                            break;
                        }
                    }
                    count += 1;
                    p.i += 1;
                    if v > 0x10FFFF {
                        break;
                    }
                }
                if count > 0 && v <= 0x10FFFF && p.i < p.pat.len() && p.pat[p.i] == b'}' {
                    p.i += 1; // '}'
                    return Ok(v);
                }
                p.i = save; // malformed: identity escape for 'u'
                return Ok(b'u' as u32);
            }
            Ok(read_hex(p, 4).unwrap_or(b'u' as u32))
        }
        b'c' => {
            if p.i + 1 < p.pat.len() {
                let l = p.pat[p.i + 1];
                if l.is_ascii_alphabetic() {
                    p.i += 2;
                    return Ok((l % 32) as u32);
                }
            }
            p.i += 1;
            Ok(b'c' as u32)
        }
        _ => {
            // Identity escape: `\.` `\/` `\\` `\$` and, permissively, anything
            // else we do not recognise. Real pages lean on this.
            let d = decode(p.pat, p.i as u32);
            p.i += d.len as usize;
            Ok(d.cp)
        }
    }
}

fn hex_val(c: u8) -> Option<u32> {
    (c as char).to_digit(16)
}

/// Exactly `n` hex digits, or `None` with `p.i` left untouched.
fn read_hex(p: &mut P, n: u32) -> Option<u32> {
    let save = p.i;
    let mut v: u32 = 0;
    for _ in 0..n {
        if p.i >= p.pat.len() {
            p.i = save;
            return None;
        }
        match hex_val(p.pat[p.i]) {
            Some(h) => v = v * 16 + h,
            None => {
                p.i = save;
                return None;
            }
        }
        p.i += 1;
    }
    Some(v)
}

/// Annex B legacy octal: up to three octal digits, capped at 255. `\8` and
/// `\9` are not octal, so they come back as themselves.
fn read_octal(p: &mut P) -> u32 {
    let first = p.pat[p.i];
    if first == b'8' || first == b'9' {
        p.i += 1;
        return first as u32;
    }
    let mut v: u32 = 0;
    let mut n = 0;
    while n < 3 && p.i < p.pat.len() && (b'0'..=b'7').contains(&p.pat[p.i]) {
        let next = v * 8 + (p.pat[p.i] - b'0') as u32;
        if next > 255 {
            break;
        }
        v = next;
        p.i += 1;
        n += 1;
    }
    v
}

fn parse_class(p: &mut P) -> Result<u32, BadPattern> {
    p.i += 1; // '['
    let mut neg = false;
    if p.at() == Some(b'^') {
        p.i += 1;
        neg = true;
    }

    let mut list: Vec<Range> = Vec::new();
    let mut closed = false;
    while p.i < p.pat.len() {
        if p.pat[p.i] == b']' {
            p.i += 1;
            closed = true;
            break;
        }
        if let Some(lo) = class_atom(p, &mut list)? {
            // `a-z` is a range; a trailing `-` before `]` is a literal.
            if p.i + 1 < p.pat.len() && p.pat[p.i] == b'-' && p.pat[p.i + 1] != b']' {
                p.i += 1;
                match class_atom(p, &mut list)? {
                    Some(hi) => {
                        if lo > hi {
                            return Err(BadPattern);
                        }
                        list.push(Range { lo, hi });
                    }
                    None => {
                        // `[\d-a]`: the set already went in; keep `-` and `a` literal.
                        list.push(Range { lo, hi: lo });
                        list.push(Range { lo: b'-' as u32, hi: b'-' as u32 });
                    }
                }
            } else {
                list.push(Range { lo, hi: lo });
            }
        }
    }
    if !closed {
        return Err(BadPattern);
    }
    Ok(build_class(p, &mut list, neg))
}

/// Returns the code point, or `None` if a predefined set was appended instead.
fn class_atom(p: &mut P, list: &mut Vec<Range>) -> Result<Option<u32>, BadPattern> {
    if p.pat[p.i] != b'\\' {
        let d = decode(p.pat, p.i as u32);
        p.i += d.len as usize;
        return Ok(Some(d.cp));
    }
    p.i += 1;
    if p.i >= p.pat.len() {
        return Err(BadPattern);
    }
    match p.pat[p.i] {
        b'd' => {
            p.i += 1;
            list.extend_from_slice(&DIGIT_SET);
            Ok(None)
        }
        b'D' => {
            p.i += 1;
            append_complement(list, &DIGIT_SET);
            Ok(None)
        }
        b'w' => {
            p.i += 1;
            list.extend_from_slice(&WORD_SET);
            Ok(None)
        }
        b'W' => {
            p.i += 1;
            append_complement(list, &WORD_SET);
            Ok(None)
        }
        b's' => {
            p.i += 1;
            list.extend_from_slice(&SPACE_SET);
            Ok(None)
        }
        b'S' => {
            p.i += 1;
            append_complement(list, &SPACE_SET);
            Ok(None)
        }
        // Inside a class `\b` is a backspace, not a boundary.
        b'b' => {
            p.i += 1;
            Ok(Some(0x08))
        }
        _ => Ok(Some(escape_char(p)?)),
    }
}

/// Canonicalise (case-fold when `i` is set) *then* complement, which is what
/// makes `[^a]/i` correctly reject `A`.
fn build_class(p: &mut P, list: &mut Vec<Range>, neg: bool) -> u32 {
    if p.flags.ignore_case {
        let n0 = list.len();
        for i in 0..n0 {
            let r = list[i];
            if r.hi >= b'A' as u32 && r.lo <= b'Z' as u32 {
                list.push(Range {
                    lo: r.lo.max(b'A' as u32) + 32,
                    hi: r.hi.min(b'Z' as u32) + 32,
                });
            }
            if r.hi >= b'a' as u32 && r.lo <= b'z' as u32 {
                list.push(Range {
                    lo: r.lo.max(b'a' as u32) - 32,
                    hi: r.hi.min(b'z' as u32) - 32,
                });
            }
        }
    }
    sort_merge(list);

    if neg {
        let mut out: Vec<Range> = Vec::new();
        append_complement(&mut out, list);
        *list = out;
    }

    let mut bm = [0u64; 2];
    let r_start = p.ranges.len() as u32;
    let mut r_len = 0u32;
    for r in list.iter() {
        let mut lo = r.lo;
        if lo < 128 {
            let hi = r.hi.min(127);
            let mut c = lo;
            loop {
                bm[(c >> 6) as usize] |= 1u64 << (c & 63);
                if c == hi {
                    break;
                }
                c += 1;
            }
            if r.hi < 128 {
                continue;
            }
            lo = 128;
        }
        p.ranges.push(Range { lo, hi: r.hi });
        r_len += 1;
    }
    p.classes.push(Class { bitmap: bm, r_start, r_len });
    (p.classes.len() - 1) as u32
}

// -- matcher ---------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq)]
enum CTag {
    /// Finish the remaining items of a `Seq`.
    Seq,
    /// Come back around a `Repeat`.
    Repeat,
    /// Close a capturing group.
    GroupEnd,
    /// Terminal inside a lookaround: succeed, without unwinding captures.
    AssertOk,
    /// Terminal inside a lookbehind: succeed only if we landed exactly here.
    BehindEnd,
}

struct Cont<'a> {
    tag: CTag,
    node: u32,
    idx: u32,
    pos: u32,
    next: Option<&'a Cont<'a>>,
}

impl<'a> Cont<'a> {
    fn new(tag: CTag) -> Cont<'a> {
        Cont { tag, node: 0, idx: 0, pos: 0, next: None }
    }
}

struct St<'a> {
    re: &'a Regex,
    input: &'a [u8],
    caps: Vec<Option<Span>>,
    steps: u64,
    depth: u32,
    aborted: bool,
    match_start: u32,
}

impl<'a> St<'a> {
    fn tick(&mut self) -> bool {
        self.steps += 1;
        if self.steps > MAX_STEPS {
            self.aborted = true;
            return false;
        }
        true
    }
}

fn class_has(re: &Regex, ci: u32, cp: u32) -> bool {
    let c = re.classes[ci as usize];
    if cp < 128 {
        return (c.bitmap[(cp >> 6) as usize] >> (cp & 63)) & 1 != 0;
    }
    for i in c.r_start..c.r_start + c.r_len {
        let r = re.ranges[i as usize];
        if cp < r.lo {
            return false;
        }
        if cp <= r.hi {
            return true;
        }
    }
    false
}

/// Single-code-point atoms: the only nodes the iterative quantifier can drive.
fn is_simple(t: Tag) -> bool {
    matches!(t, Tag::Lit | Tag::Any | Tag::Class)
}

fn match_atom(st: &St, n: &Node, pos: u32) -> Option<u32> {
    if pos as usize >= st.input.len() {
        return None;
    }
    let d = decode(st.input, pos);
    match n.tag {
        Tag::Lit => {
            if d.cp == n.ch {
                return Some(d.len);
            }
            if st.re.flags.ignore_case && fold_ascii(d.cp) == fold_ascii(n.ch) {
                return Some(d.len);
            }
            None
        }
        Tag::Any => {
            if !st.re.flags.dot_all && is_line_term(d.cp) {
                return None;
            }
            Some(d.len)
        }
        Tag::Class => {
            if class_has(st.re, n.cls, d.cp) {
                Some(d.len)
            } else {
                None
            }
        }
        _ => None,
    }
}

fn at_line_start(st: &St, pos: u32) -> bool {
    if pos == 0 {
        return true;
    }
    if !st.re.flags.multiline {
        return false;
    }
    let j = prev_start(st.input, pos);
    is_line_term(decode(st.input, j).cp)
}

fn at_line_end(st: &St, pos: u32) -> bool {
    if pos as usize == st.input.len() {
        return true;
    }
    if !st.re.flags.multiline {
        return false;
    }
    is_line_term(decode(st.input, pos).cp)
}

fn is_word_at(st: &St, pos: u32) -> bool {
    match st.input.get(pos as usize) {
        Some(b) => is_word_byte(*b),
        None => false,
    }
}

fn is_word_before(st: &St, pos: u32) -> bool {
    if pos == 0 {
        return false;
    }
    is_word_byte(st.input[pos as usize - 1])
}

fn run(st: &mut St, ni: u32, pos: u32, k: Option<&Cont>) -> bool {
    if !st.tick() {
        return false;
    }
    st.depth += 1;
    if st.depth > MAX_DEPTH {
        st.depth -= 1;
        return false;
    }
    let out = run_node(st, ni, pos, k);
    st.depth -= 1;
    out
}

fn run_node(st: &mut St, ni: u32, pos: u32, k: Option<&Cont>) -> bool {
    let n = st.re.nodes[ni as usize];
    match n.tag {
        Tag::Empty => run_cont(st, k, pos),

        Tag::Lit | Tag::Any | Tag::Class => match match_atom(st, &n, pos) {
            Some(len) => run_cont(st, k, pos + len),
            None => false,
        },

        Tag::Bol => at_line_start(st, pos) && run_cont(st, k, pos),
        Tag::Eol => at_line_end(st, pos) && run_cont(st, k, pos),

        Tag::Wordb | Tag::Nwordb => {
            let b = is_word_before(st, pos) != is_word_at(st, pos);
            let want = n.tag == Tag::Wordb;
            b == want && run_cont(st, k, pos)
        }

        Tag::Seq => {
            if n.kid_len == 0 {
                return run_cont(st, k, pos);
            }
            let f = Cont { tag: CTag::Seq, node: ni, idx: 1, pos: 0, next: k };
            let first = st.re.kids[n.kid_start as usize];
            run(st, first, pos, Some(&f))
        }

        Tag::Alt => {
            for i in 0..n.kid_len {
                let kid = st.re.kids[(n.kid_start + i) as usize];
                if run(st, kid, pos, k) {
                    return true;
                }
                if st.aborted {
                    return false;
                }
            }
            false
        }

        Tag::Group => {
            if n.cap == 0 {
                return run(st, n.child, pos, k);
            }
            let f = Cont { tag: CTag::GroupEnd, node: ni, idx: 0, pos, next: k };
            run(st, n.child, pos, Some(&f))
        }

        Tag::Repeat => {
            if is_simple(st.re.nodes[n.child as usize].tag) {
                return run_simple_repeat(st, &n, pos, k);
            }
            run_repeat(st, ni, 0, pos, k)
        }

        Tag::Look => run_look(st, &n, pos, k),

        Tag::Backref => {
            let sp = match st.caps[n.ref_ as usize] {
                Some(sp) => sp,
                None => return run_cont(st, k, pos),
            };
            let len = sp.end - sp.start;
            if (pos + len) as usize > st.input.len() {
                return false;
            }
            let a = &st.input[sp.start as usize..sp.end as usize];
            let b = &st.input[pos as usize..(pos + len) as usize];
            if st.re.flags.ignore_case {
                if a.iter().zip(b).any(|(x, y)| {
                    fold_ascii(*x as u32) != fold_ascii(*y as u32)
                }) {
                    return false;
                }
            } else if a != b {
                return false;
            }
            run_cont(st, k, pos + len)
        }
    }
}

fn run_cont(st: &mut St, k: Option<&Cont>, pos: u32) -> bool {
    if !st.tick() {
        return false;
    }
    let f = match k {
        Some(f) => f,
        None => {
            st.caps[0] = Some(Span { start: st.match_start, end: pos });
            return true;
        }
    };
    st.depth += 1;
    if st.depth > MAX_DEPTH {
        st.depth -= 1;
        return false;
    }
    let out = run_cont_frame(st, f, pos);
    st.depth -= 1;
    out
}

fn run_cont_frame(st: &mut St, f: &Cont, pos: u32) -> bool {
    match f.tag {
        CTag::AssertOk => true,
        CTag::BehindEnd => pos == f.pos,

        CTag::Seq => {
            let n = st.re.nodes[f.node as usize];
            if f.idx >= n.kid_len {
                return run_cont(st, f.next, pos);
            }
            let g = Cont { tag: CTag::Seq, node: f.node, idx: f.idx + 1, pos: 0, next: f.next };
            let kid = st.re.kids[(n.kid_start + f.idx) as usize];
            run(st, kid, pos, Some(&g))
        }

        CTag::GroupEnd => {
            let n = st.re.nodes[f.node as usize];
            let old = st.caps[n.cap as usize];
            st.caps[n.cap as usize] = Some(Span { start: f.pos, end: pos });
            if run_cont(st, f.next, pos) {
                return true;
            }
            st.caps[n.cap as usize] = old;
            false
        }

        CTag::Repeat => {
            let n = st.re.nodes[f.node as usize];
            if pos == f.pos {
                // The body matched empty. Iterating again would do the same
                // forever, so jump straight to the minimum and get out.
                if f.idx >= n.min {
                    return run_cont(st, f.next, pos);
                }
                return run_repeat(st, f.node, n.min, pos, f.next);
            }
            run_repeat(st, f.node, f.idx, pos, f.next)
        }
    }
}

/// Per ECMAScript, every iteration of a quantifier clears the captures inside
/// it, so `/(?:(a)|b)+/` on "ab" leaves group 1 unset. We only bother when the
/// group range is small enough to save cheaply.
const MAX_RESET: u32 = 8;

fn try_body(st: &mut St, ni: u32, pos: u32, f: &Cont) -> bool {
    let n = st.re.nodes[ni as usize];
    if n.cap_lo <= n.cap_hi && n.cap_hi - n.cap_lo < MAX_RESET {
        let mut saved: [Option<Span>; MAX_RESET as usize] = [None; MAX_RESET as usize];
        for i in n.cap_lo..=n.cap_hi {
            saved[(i - n.cap_lo) as usize] = st.caps[i as usize];
            st.caps[i as usize] = None;
        }
        if run(st, n.child, pos, Some(f)) {
            return true;
        }
        for i in n.cap_lo..=n.cap_hi {
            st.caps[i as usize] = saved[(i - n.cap_lo) as usize];
        }
        return false;
    }
    run(st, n.child, pos, Some(f))
}

fn run_repeat(st: &mut St, ni: u32, count: u32, pos: u32, k: Option<&Cont>) -> bool {
    if !st.tick() {
        return false;
    }
    st.depth += 1;
    if st.depth > MAX_DEPTH {
        st.depth -= 1;
        return false;
    }
    let out = run_repeat_inner(st, ni, count, pos, k);
    st.depth -= 1;
    out
}

fn run_repeat_inner(st: &mut St, ni: u32, count: u32, pos: u32, k: Option<&Cont>) -> bool {
    let n = st.re.nodes[ni as usize];
    let more = count < n.max;
    let exit_ok = count >= n.min;

    if n.greedy {
        if more {
            let f = Cont { tag: CTag::Repeat, node: ni, idx: count + 1, pos, next: k };
            if try_body(st, ni, pos, &f) {
                return true;
            }
            if st.aborted {
                return false;
            }
        }
        return exit_ok && run_cont(st, k, pos);
    }

    if exit_ok {
        if run_cont(st, k, pos) {
            return true;
        }
        if st.aborted {
            return false;
        }
    }
    if more {
        let f = Cont { tag: CTag::Repeat, node: ni, idx: count + 1, pos, next: k };
        return try_body(st, ni, pos, &f);
    }
    false
}

/// `a*`, `.+?`, `[a-z]{2,5}` -- a quantifier over a single-code-point atom.
/// Driven with a loop rather than recursion so that matching a megabyte of
/// text does not put a megabyte of frames on the stack.
fn run_simple_repeat(st: &mut St, n: &Node, pos: u32, k: Option<&Cont>) -> bool {
    let child = st.re.nodes[n.child as usize];

    if n.greedy {
        let mut p = pos;
        let mut cnt = 0u32;
        while cnt < n.max {
            if !st.tick() {
                return false;
            }
            match match_atom(st, &child, p) {
                Some(len) => {
                    p += len;
                    cnt += 1;
                }
                None => break,
            }
        }
        while cnt >= n.min {
            if run_cont(st, k, p) {
                return true;
            }
            if st.aborted {
                return false;
            }
            if cnt == 0 {
                break;
            }
            p = prev_start(st.input, p);
            cnt -= 1;
        }
        return false;
    }

    let mut p = pos;
    let mut cnt = 0u32;
    while cnt < n.min {
        if !st.tick() {
            return false;
        }
        match match_atom(st, &child, p) {
            Some(len) => p += len,
            None => return false,
        }
        cnt += 1;
    }
    loop {
        if run_cont(st, k, p) {
            return true;
        }
        if st.aborted || cnt >= n.max || !st.tick() {
            return false;
        }
        match match_atom(st, &child, p) {
            Some(len) => p += len,
            None => return false,
        }
        cnt += 1;
    }
}

fn run_look(st: &mut St, n: &Node, pos: u32, k: Option<&Cont>) -> bool {
    let ok = Cont::new(CTag::AssertOk);

    let mut start = pos;
    let mut end_frame = ok;
    if !n.ahead {
        match step_back(st.input, pos, n.width) {
            Some(s) => start = s,
            None => {
                // Not enough text behind us: the assertion simply fails.
                if n.negate {
                    return run_cont(st, k, pos);
                }
                return false;
            }
        }
        end_frame = Cont { tag: CTag::BehindEnd, node: 0, idx: 0, pos, next: None };
    }

    if n.negate {
        // Undo anything the (successful, and therefore discarded) attempt set.
        let hit = if n.cap_lo <= n.cap_hi && n.cap_hi - n.cap_lo < MAX_RESET {
            let mut saved: [Option<Span>; MAX_RESET as usize] = [None; MAX_RESET as usize];
            for i in n.cap_lo..=n.cap_hi {
                saved[(i - n.cap_lo) as usize] = st.caps[i as usize];
            }
            let hit = run(st, n.child, start, Some(&end_frame));
            for i in n.cap_lo..=n.cap_hi {
                st.caps[i as usize] = saved[(i - n.cap_lo) as usize];
            }
            hit
        } else {
            run(st, n.child, start, Some(&end_frame))
        };
        if st.aborted || hit {
            return false;
        }
        return run_cont(st, k, pos);
    }

    // A positive lookaround is atomic: the body matches once, with a
    // continuation that always succeeds, and we never backtrack into it.
    // Because the frames unwind on `true` they leave their captures in place.
    if !run(st, n.child, start, Some(&end_frame)) {
        return false;
    }
    run_cont(st, k, pos)
}
