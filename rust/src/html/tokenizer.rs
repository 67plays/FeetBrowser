//! The HTML tokenizer, WHATWG HTML §13.2.5.
//!
//! # Shape of this module
//!
//! The spec is written as ~80 named states, each of which consumes at most one
//! character and names its successor. This module keeps that structure
//! literally: [`State`] has one variant per spec state and [`Tokenizer::step`]
//! is one `match` over it. That is a lot of lines, but it means a reader with
//! the spec open can find any state by name and check it in isolation, and it
//! means the awkward states (script-data double-escaped, the four
//! comment-less-than-sign states) are not quietly folded into something
//! "equivalent" that is subtly not.
//!
//! Two places deliberately depart from the literal transcription:
//!
//! * **Character references** (§13.2.5.72-80) are a nine-state sub-machine
//!   whose only interaction with the rest of the tokenizer is "produce some
//!   text, then go back to the state you came from". They are implemented as
//!   [`Tokenizer::consume_character_reference`], a routine that returns that
//!   text, because the "return state" plumbing costs more than it explains.
//! * **Duplicate attributes** are dropped when the tag token is emitted rather
//!   than when each name is completed. Keeping the first occurrence at emit
//!   time is the same observable result as refusing to add the later one.
//!
//! # Who drives whom
//!
//! The tokenizer does not run to completion on its own. The tree builder pulls
//! one token at a time and, for a handful of elements, reaches in and sets
//! [`Tokenizer::state`] afterwards — `<title>` switches it to RCDATA,
//! `<script>` to script-data, `<plaintext>` to PLAINTEXT. It also sets
//! [`Tokenizer::in_foreign`], because whether `<![CDATA[` is a CDATA section or
//! a bogus comment depends on the *adjusted current node*, which only the tree
//! builder knows.

use super::entities::{LONGEST_NAME, NAMED_REFERENCES};

/// One attribute as the tokenizer saw it: no namespace, no case folding beyond
/// the ASCII-lowercasing the spec does inline.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TagAttr {
    pub name: String,
    pub value: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct DoctypeToken {
    pub name: Option<String>,
    pub public_id: Option<String>,
    pub system_id: Option<String>,
    pub force_quirks: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TagToken {
    pub name: String,
    pub attrs: Vec<TagAttr>,
    pub self_closing: bool,
}

impl TagToken {
    pub fn attr(&self, name: &str) -> Option<&str> {
        self.attrs
            .iter()
            .find(|a| a.name == name)
            .map(|a| a.value.as_str())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Token {
    Doctype(DoctypeToken),
    StartTag(TagToken),
    EndTag(TagToken),
    Comment(String),
    /// `<?target data?>`. See [`Tokenizer::processing_instruction`].
    ProcessingInstruction { target: String, data: String },
    Character(char),
    Eof,
}

/// Every state in §13.2.5 except the character-reference sub-machine.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Data,
    Rcdata,
    Rawtext,
    ScriptData,
    Plaintext,
    TagOpen,
    EndTagOpen,
    TagName,
    RcdataLessThanSign,
    RcdataEndTagOpen,
    RcdataEndTagName,
    RawtextLessThanSign,
    RawtextEndTagOpen,
    RawtextEndTagName,
    ScriptDataLessThanSign,
    ScriptDataEndTagOpen,
    ScriptDataEndTagName,
    ScriptDataEscapeStart,
    ScriptDataEscapeStartDash,
    ScriptDataEscaped,
    ScriptDataEscapedDash,
    ScriptDataEscapedDashDash,
    ScriptDataEscapedLessThanSign,
    ScriptDataEscapedEndTagOpen,
    ScriptDataEscapedEndTagName,
    ScriptDataDoubleEscapeStart,
    ScriptDataDoubleEscaped,
    ScriptDataDoubleEscapedDash,
    ScriptDataDoubleEscapedDashDash,
    ScriptDataDoubleEscapedLessThanSign,
    ScriptDataDoubleEscapeEnd,
    BeforeAttributeName,
    AttributeName,
    AfterAttributeName,
    BeforeAttributeValue,
    AttributeValueDoubleQuoted,
    AttributeValueSingleQuoted,
    AttributeValueUnquoted,
    AfterAttributeValueQuoted,
    SelfClosingStartTag,
    BogusComment,
    MarkupDeclarationOpen,
    CommentStart,
    CommentStartDash,
    Comment,
    CommentLessThanSign,
    CommentLessThanSignBang,
    CommentLessThanSignBangDash,
    CommentLessThanSignBangDashDash,
    CommentEndDash,
    CommentEnd,
    CommentEndBang,
    Doctype,
    BeforeDoctypeName,
    DoctypeName,
    AfterDoctypeName,
    AfterDoctypePublicKeyword,
    BeforeDoctypePublicIdentifier,
    DoctypePublicIdentifierDoubleQuoted,
    DoctypePublicIdentifierSingleQuoted,
    AfterDoctypePublicIdentifier,
    BetweenDoctypePublicAndSystemIdentifiers,
    AfterDoctypeSystemKeyword,
    BeforeDoctypeSystemIdentifier,
    DoctypeSystemIdentifierDoubleQuoted,
    DoctypeSystemIdentifierSingleQuoted,
    AfterDoctypeSystemIdentifier,
    BogusDoctype,
    CdataSection,
    CdataSectionBracket,
    CdataSectionEnd,
}

const REPLACEMENT: char = '\u{FFFD}';

#[inline]
fn is_html_whitespace(c: char) -> bool {
    matches!(c, '\t' | '\n' | '\u{0C}' | ' ')
}

/// Which kind of tag the tokenizer is currently building.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TagKind {
    Start,
    End,
}

pub struct Tokenizer {
    input: Vec<char>,
    pos: usize,

    /// The tree builder writes here to force RCDATA/RAWTEXT/script/PLAINTEXT.
    pub state: State,
    /// Set by the tree builder before each pull: is the *adjusted current
    /// node* a non-HTML element? Only `<![CDATA[` cares.
    pub in_foreign: bool,

    /// Tokens produced but not yet handed out. A single `step` can produce
    /// several (a character reference expanding to two code points, a
    /// `</p attr>` that emits a tag after its attributes).
    pending: Vec<Token>,
    /// Index of the next token to hand out of `pending`.
    pending_at: usize,

    /// The tag being built, if any.
    tag_name: String,
    tag_kind: TagKind,
    tag_attrs: Vec<TagAttr>,
    tag_self_closing: bool,
    /// Name of the last *start* tag emitted, for "appropriate end tag token".
    last_start_tag: String,

    attr_name: String,
    attr_value: String,
    /// Is there a half-built attribute waiting to be pushed?
    attr_open: bool,

    comment: String,
    doctype: DoctypeToken,

    /// The spec's "temporary buffer", used by the RCDATA/RAWTEXT/script end-tag
    /// states and by the double-escape start/end states.
    temp: String,

    /// True once EOF has been emitted; further pulls keep returning it.
    done: bool,

    /// Parse errors are counted, not described. The tree-construction fixtures
    /// only assert on the count of errors a conformant parser reports, and
    /// this parser does not yet claim to match those counts; the counter
    /// exists so the harness can report it and so a future error-name pass has
    /// somewhere to land.
    pub errors: usize,
}

impl Tokenizer {
    pub fn new(input: &str) -> Tokenizer {
        Tokenizer {
            input: preprocess(input),
            pos: 0,
            state: State::Data,
            in_foreign: false,
            pending: Vec::new(),
            pending_at: 0,
            tag_name: String::new(),
            tag_kind: TagKind::Start,
            tag_attrs: Vec::new(),
            tag_self_closing: false,
            last_start_tag: String::new(),
            attr_name: String::new(),
            attr_value: String::new(),
            attr_open: false,
            comment: String::new(),
            doctype: DoctypeToken::default(),
            temp: String::new(),
            done: false,
            errors: 0,
        }
    }

    /// Seed "the last start tag" without having tokenized one.
    ///
    /// The fragment parsing algorithm parses `<title>x</title>`-style content
    /// with the context element's name already established, so `</title>` in
    /// the fragment has to count as an appropriate end tag.
    pub fn set_last_start_tag(&mut self, name: &str) {
        self.last_start_tag = name.to_string();
    }

    /// Pull the next token, running states until one is produced.
    pub fn next_token(&mut self) -> Token {
        loop {
            if self.pending_at < self.pending.len() {
                let t = self.pending[self.pending_at].clone();
                self.pending_at += 1;
                if self.pending_at == self.pending.len() {
                    self.pending.clear();
                    self.pending_at = 0;
                }
                return t;
            }
            if self.done {
                return Token::Eof;
            }
            self.step();
        }
    }

    // -- input ---------------------------------------------------------------

    #[inline]
    fn peek(&self) -> Option<char> {
        self.input.get(self.pos).copied()
    }

    #[inline]
    fn peek_at(&self, offset: usize) -> Option<char> {
        self.input.get(self.pos + offset).copied()
    }

    #[inline]
    fn advance(&mut self) -> Option<char> {
        let c = self.peek();
        if c.is_some() {
            self.pos += 1;
        }
        c
    }

    /// Does the input at the cursor match `s`? `ci` selects ASCII-case
    /// insensitivity, which the DOCTYPE keywords and `<![CDATA[` need.
    fn lookahead(&self, s: &str, ci: bool) -> bool {
        for (i, want) in s.chars().enumerate() {
            match self.peek_at(i) {
                Some(got) if ci && got.eq_ignore_ascii_case(&want) => {}
                Some(got) if !ci && got == want => {}
                _ => return false,
            }
        }
        true
    }

    #[inline]
    fn error(&mut self) {
        self.errors += 1;
    }

    // -- token construction --------------------------------------------------

    #[inline]
    fn emit(&mut self, token: Token) {
        self.pending.push(token);
    }

    fn emit_str(&mut self, s: &str) {
        for c in s.chars() {
            self.pending.push(Token::Character(c));
        }
    }

    fn start_tag(&mut self, kind: TagKind) {
        self.tag_name.clear();
        self.tag_kind = kind;
        self.tag_attrs.clear();
        self.tag_self_closing = false;
        self.attr_open = false;
    }

    /// Push the half-built attribute, dropping it if the name is a duplicate.
    fn finish_attribute(&mut self) {
        if !self.attr_open {
            return;
        }
        self.attr_open = false;
        let name = std::mem::take(&mut self.attr_name);
        let value = std::mem::take(&mut self.attr_value);
        if self.tag_attrs.iter().any(|a| a.name == name) {
            // duplicate-attribute parse error; the later one is dropped.
            self.error();
            return;
        }
        self.tag_attrs.push(TagAttr { name, value });
    }

    fn new_attribute(&mut self) {
        self.finish_attribute();
        self.attr_name.clear();
        self.attr_value.clear();
        self.attr_open = true;
    }

    fn emit_tag(&mut self) {
        self.finish_attribute();
        let tag = TagToken {
            name: std::mem::take(&mut self.tag_name),
            attrs: std::mem::take(&mut self.tag_attrs),
            self_closing: self.tag_self_closing,
        };
        match self.tag_kind {
            TagKind::Start => {
                self.last_start_tag = tag.name.clone();
                self.emit(Token::StartTag(tag));
            }
            TagKind::End => {
                // "An end tag ... with attributes / with a self-closing flag"
                // is a parse error, but the token is still emitted and the
                // tree builder ignores the extras.
                if !tag.attrs.is_empty() || tag.self_closing {
                    self.error();
                }
                self.emit(Token::EndTag(tag));
            }
        }
    }

    fn emit_eof(&mut self) {
        self.done = true;
        self.emit(Token::Eof);
    }

    /// Is the end tag under construction the one that closes the RCDATA /
    /// RAWTEXT / script-data element we are inside?
    fn is_appropriate_end_tag(&self) -> bool {
        self.tag_kind == TagKind::End && self.tag_name == self.last_start_tag
    }

    // -- the state machine ---------------------------------------------------

    fn step(&mut self) {
        match self.state {
            State::Data => self.data_state(),
            State::Rcdata => self.rcdata_state(),
            State::Rawtext => self.rawtext_state(),
            State::ScriptData => self.script_data_state(),
            State::Plaintext => self.plaintext_state(),
            State::TagOpen => self.tag_open_state(),
            State::EndTagOpen => self.end_tag_open_state(),
            State::TagName => self.tag_name_state(),
            State::RcdataLessThanSign => self.rcdata_lt_state(),
            State::RcdataEndTagOpen => self.rcdata_end_tag_open_state(),
            State::RcdataEndTagName => self.rcdata_end_tag_name_state(),
            State::RawtextLessThanSign => self.rawtext_lt_state(),
            State::RawtextEndTagOpen => self.rawtext_end_tag_open_state(),
            State::RawtextEndTagName => self.rawtext_end_tag_name_state(),
            State::ScriptDataLessThanSign => self.script_data_lt_state(),
            State::ScriptDataEndTagOpen => self.script_data_end_tag_open_state(),
            State::ScriptDataEndTagName => self.script_data_end_tag_name_state(),
            State::ScriptDataEscapeStart => self.script_data_escape_start_state(),
            State::ScriptDataEscapeStartDash => self.script_data_escape_start_dash_state(),
            State::ScriptDataEscaped => self.script_data_escaped_state(),
            State::ScriptDataEscapedDash => self.script_data_escaped_dash_state(),
            State::ScriptDataEscapedDashDash => self.script_data_escaped_dash_dash_state(),
            State::ScriptDataEscapedLessThanSign => self.script_data_escaped_lt_state(),
            State::ScriptDataEscapedEndTagOpen => self.script_data_escaped_end_tag_open_state(),
            State::ScriptDataEscapedEndTagName => self.script_data_escaped_end_tag_name_state(),
            State::ScriptDataDoubleEscapeStart => self.script_data_double_escape_start_state(),
            State::ScriptDataDoubleEscaped => self.script_data_double_escaped_state(),
            State::ScriptDataDoubleEscapedDash => self.script_data_double_escaped_dash_state(),
            State::ScriptDataDoubleEscapedDashDash => {
                self.script_data_double_escaped_dash_dash_state()
            }
            State::ScriptDataDoubleEscapedLessThanSign => {
                self.script_data_double_escaped_lt_state()
            }
            State::ScriptDataDoubleEscapeEnd => self.script_data_double_escape_end_state(),
            State::BeforeAttributeName => self.before_attribute_name_state(),
            State::AttributeName => self.attribute_name_state(),
            State::AfterAttributeName => self.after_attribute_name_state(),
            State::BeforeAttributeValue => self.before_attribute_value_state(),
            State::AttributeValueDoubleQuoted => self.attribute_value_quoted_state('"'),
            State::AttributeValueSingleQuoted => self.attribute_value_quoted_state('\''),
            State::AttributeValueUnquoted => self.attribute_value_unquoted_state(),
            State::AfterAttributeValueQuoted => self.after_attribute_value_quoted_state(),
            State::SelfClosingStartTag => self.self_closing_start_tag_state(),
            State::BogusComment => self.bogus_comment_state(),
            State::MarkupDeclarationOpen => self.markup_declaration_open_state(),
            State::CommentStart => self.comment_start_state(),
            State::CommentStartDash => self.comment_start_dash_state(),
            State::Comment => self.comment_state(),
            State::CommentLessThanSign => self.comment_lt_state(),
            State::CommentLessThanSignBang => self.comment_lt_bang_state(),
            State::CommentLessThanSignBangDash => self.comment_lt_bang_dash_state(),
            State::CommentLessThanSignBangDashDash => self.comment_lt_bang_dash_dash_state(),
            State::CommentEndDash => self.comment_end_dash_state(),
            State::CommentEnd => self.comment_end_state(),
            State::CommentEndBang => self.comment_end_bang_state(),
            State::Doctype => self.doctype_state(),
            State::BeforeDoctypeName => self.before_doctype_name_state(),
            State::DoctypeName => self.doctype_name_state(),
            State::AfterDoctypeName => self.after_doctype_name_state(),
            State::AfterDoctypePublicKeyword => self.after_doctype_public_keyword_state(),
            State::BeforeDoctypePublicIdentifier => self.before_doctype_public_identifier_state(),
            State::DoctypePublicIdentifierDoubleQuoted => self.doctype_public_identifier_state('"'),
            State::DoctypePublicIdentifierSingleQuoted => self.doctype_public_identifier_state('\''),
            State::AfterDoctypePublicIdentifier => self.after_doctype_public_identifier_state(),
            State::BetweenDoctypePublicAndSystemIdentifiers => self.between_doctype_ids_state(),
            State::AfterDoctypeSystemKeyword => self.after_doctype_system_keyword_state(),
            State::BeforeDoctypeSystemIdentifier => self.before_doctype_system_identifier_state(),
            State::DoctypeSystemIdentifierDoubleQuoted => self.doctype_system_identifier_state('"'),
            State::DoctypeSystemIdentifierSingleQuoted => self.doctype_system_identifier_state('\''),
            State::AfterDoctypeSystemIdentifier => self.after_doctype_system_identifier_state(),
            State::BogusDoctype => self.bogus_doctype_state(),
            State::CdataSection => self.cdata_section_state(),
            State::CdataSectionBracket => self.cdata_section_bracket_state(),
            State::CdataSectionEnd => self.cdata_section_end_state(),
        }
    }

    // §13.2.5.1
    fn data_state(&mut self) {
        match self.advance() {
            Some('<') => self.state = State::TagOpen,
            Some('&') => {
                let text = self.consume_character_reference(false);
                self.emit_str(&text);
            }
            Some('\0') => {
                // unexpected-null-character; in Data the NULL is emitted as-is.
                self.error();
                self.emit(Token::Character('\0'));
            }
            Some(c) => self.emit(Token::Character(c)),
            None => self.emit_eof(),
        }
    }

    // §13.2.5.2
    fn rcdata_state(&mut self) {
        match self.advance() {
            Some('<') => self.state = State::RcdataLessThanSign,
            Some('&') => {
                let text = self.consume_character_reference(false);
                self.emit_str(&text);
            }
            Some('\0') => {
                self.error();
                self.emit(Token::Character(REPLACEMENT));
            }
            Some(c) => self.emit(Token::Character(c)),
            None => self.emit_eof(),
        }
    }

    // §13.2.5.3
    fn rawtext_state(&mut self) {
        match self.advance() {
            Some('<') => self.state = State::RawtextLessThanSign,
            Some('\0') => {
                self.error();
                self.emit(Token::Character(REPLACEMENT));
            }
            Some(c) => self.emit(Token::Character(c)),
            None => self.emit_eof(),
        }
    }

    // §13.2.5.4
    fn script_data_state(&mut self) {
        match self.advance() {
            Some('<') => self.state = State::ScriptDataLessThanSign,
            Some('\0') => {
                self.error();
                self.emit(Token::Character(REPLACEMENT));
            }
            Some(c) => self.emit(Token::Character(c)),
            None => self.emit_eof(),
        }
    }

    // §13.2.5.5
    fn plaintext_state(&mut self) {
        match self.advance() {
            Some('\0') => {
                self.error();
                self.emit(Token::Character(REPLACEMENT));
            }
            Some(c) => self.emit(Token::Character(c)),
            None => self.emit_eof(),
        }
    }

    // §13.2.5.6
    fn tag_open_state(&mut self) {
        match self.peek() {
            Some('!') => {
                self.pos += 1;
                self.state = State::MarkupDeclarationOpen;
            }
            Some('/') => {
                self.pos += 1;
                self.state = State::EndTagOpen;
            }
            Some(c) if c.is_ascii_alphabetic() => {
                self.start_tag(TagKind::Start);
                self.state = State::TagName;
            }
            Some('?') => {
                if self.processing_instruction() {
                    return;
                }
                // unexpected-question-mark-instead-of-tag-name: the "?" is
                // part of the comment data, so do not consume it.
                self.error();
                self.comment.clear();
                self.state = State::BogusComment;
            }
            Some(_) => {
                self.error();
                self.emit(Token::Character('<'));
                self.state = State::Data;
            }
            None => {
                self.error();
                self.emit(Token::Character('<'));
                self.emit_eof();
            }
        }
    }

    // §13.2.5.7
    fn end_tag_open_state(&mut self) {
        match self.peek() {
            Some(c) if c.is_ascii_alphabetic() => {
                self.start_tag(TagKind::End);
                self.state = State::TagName;
            }
            Some('>') => {
                // missing-end-tag-name: nothing is emitted at all.
                self.pos += 1;
                self.error();
                self.state = State::Data;
            }
            Some(_) => {
                self.error();
                self.comment.clear();
                self.state = State::BogusComment;
            }
            None => {
                self.error();
                self.emit(Token::Character('<'));
                self.emit(Token::Character('/'));
                self.emit_eof();
            }
        }
    }

    // §13.2.5.8
    fn tag_name_state(&mut self) {
        match self.advance() {
            Some(c) if is_html_whitespace(c) => self.state = State::BeforeAttributeName,
            Some('/') => self.state = State::SelfClosingStartTag,
            Some('>') => {
                self.state = State::Data;
                self.emit_tag();
            }
            Some('\0') => {
                self.error();
                self.tag_name.push(REPLACEMENT);
            }
            Some(c) => self.tag_name.push(c.to_ascii_lowercase()),
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // -- RCDATA / RAWTEXT / script end-tag machinery -------------------------
    //
    // These nine states are three copies of the same shape. They are written
    // out separately because their `<`-handling differs (script data has the
    // escape-start branch) and because collapsing them would mean threading
    // the "which state do I fall back to" decision through every arm.

    // §13.2.5.9
    fn rcdata_lt_state(&mut self) {
        if self.peek() == Some('/') {
            self.pos += 1;
            self.temp.clear();
            self.state = State::RcdataEndTagOpen;
        } else {
            self.emit(Token::Character('<'));
            self.state = State::Rcdata;
        }
    }

    // §13.2.5.10
    fn rcdata_end_tag_open_state(&mut self) {
        if matches!(self.peek(), Some(c) if c.is_ascii_alphabetic()) {
            self.start_tag(TagKind::End);
            self.state = State::RcdataEndTagName;
        } else {
            self.emit(Token::Character('<'));
            self.emit(Token::Character('/'));
            self.state = State::Rcdata;
        }
    }

    // §13.2.5.11
    fn rcdata_end_tag_name_state(&mut self) {
        if self.end_tag_name_step(State::Rcdata) {
            self.state = State::Rcdata;
        }
    }

    // §13.2.5.12
    fn rawtext_lt_state(&mut self) {
        if self.peek() == Some('/') {
            self.pos += 1;
            self.temp.clear();
            self.state = State::RawtextEndTagOpen;
        } else {
            self.emit(Token::Character('<'));
            self.state = State::Rawtext;
        }
    }

    // §13.2.5.13
    fn rawtext_end_tag_open_state(&mut self) {
        if matches!(self.peek(), Some(c) if c.is_ascii_alphabetic()) {
            self.start_tag(TagKind::End);
            self.state = State::RawtextEndTagName;
        } else {
            self.emit(Token::Character('<'));
            self.emit(Token::Character('/'));
            self.state = State::Rawtext;
        }
    }

    // §13.2.5.14
    fn rawtext_end_tag_name_state(&mut self) {
        if self.end_tag_name_step(State::Rawtext) {
            self.state = State::Rawtext;
        }
    }

    // §13.2.5.15
    fn script_data_lt_state(&mut self) {
        match self.peek() {
            Some('/') => {
                self.pos += 1;
                self.temp.clear();
                self.state = State::ScriptDataEndTagOpen;
            }
            Some('!') => {
                self.pos += 1;
                self.emit(Token::Character('<'));
                self.emit(Token::Character('!'));
                self.state = State::ScriptDataEscapeStart;
            }
            _ => {
                self.emit(Token::Character('<'));
                self.state = State::ScriptData;
            }
        }
    }

    // §13.2.5.16
    fn script_data_end_tag_open_state(&mut self) {
        if matches!(self.peek(), Some(c) if c.is_ascii_alphabetic()) {
            self.start_tag(TagKind::End);
            self.state = State::ScriptDataEndTagName;
        } else {
            self.emit(Token::Character('<'));
            self.emit(Token::Character('/'));
            self.state = State::ScriptData;
        }
    }

    // §13.2.5.17
    fn script_data_end_tag_name_state(&mut self) {
        if self.end_tag_name_step(State::ScriptData) {
            self.state = State::ScriptData;
        }
    }

    /// The body shared by the six `* end tag name` states.
    ///
    /// Returns `true` if the caller should bail back to its text state, having
    /// already emitted `</` plus the partial name as characters. This is the
    /// "anything else" arm, and it is the reason `<script>` bodies containing
    /// a bare `</` do not terminate the element.
    fn end_tag_name_step(&mut self, _fallback: State) -> bool {
        match self.peek() {
            Some(c) if is_html_whitespace(c) && self.is_appropriate_end_tag() => {
                self.pos += 1;
                self.state = State::BeforeAttributeName;
                false
            }
            Some('/') if self.is_appropriate_end_tag() => {
                self.pos += 1;
                self.state = State::SelfClosingStartTag;
                false
            }
            Some('>') if self.is_appropriate_end_tag() => {
                self.pos += 1;
                self.state = State::Data;
                self.emit_tag();
                false
            }
            Some(c) if c.is_ascii_alphabetic() => {
                self.pos += 1;
                self.tag_name.push(c.to_ascii_lowercase());
                self.temp.push(c);
                false
            }
            _ => {
                self.emit(Token::Character('<'));
                self.emit(Token::Character('/'));
                let temp = std::mem::take(&mut self.temp);
                self.emit_str(&temp);
                true
            }
        }
    }

    // §13.2.5.18
    fn script_data_escape_start_state(&mut self) {
        if self.peek() == Some('-') {
            self.pos += 1;
            self.emit(Token::Character('-'));
            self.state = State::ScriptDataEscapeStartDash;
        } else {
            self.state = State::ScriptData;
        }
    }

    // §13.2.5.19
    fn script_data_escape_start_dash_state(&mut self) {
        if self.peek() == Some('-') {
            self.pos += 1;
            self.emit(Token::Character('-'));
            self.state = State::ScriptDataEscapedDashDash;
        } else {
            self.state = State::ScriptData;
        }
    }

    // §13.2.5.20
    fn script_data_escaped_state(&mut self) {
        match self.advance() {
            Some('-') => {
                self.emit(Token::Character('-'));
                self.state = State::ScriptDataEscapedDash;
            }
            Some('<') => self.state = State::ScriptDataEscapedLessThanSign,
            Some('\0') => {
                self.error();
                self.emit(Token::Character(REPLACEMENT));
            }
            Some(c) => self.emit(Token::Character(c)),
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // §13.2.5.21
    fn script_data_escaped_dash_state(&mut self) {
        match self.advance() {
            Some('-') => {
                self.emit(Token::Character('-'));
                self.state = State::ScriptDataEscapedDashDash;
            }
            Some('<') => self.state = State::ScriptDataEscapedLessThanSign,
            Some('\0') => {
                self.error();
                self.emit(Token::Character(REPLACEMENT));
                self.state = State::ScriptDataEscaped;
            }
            Some(c) => {
                self.emit(Token::Character(c));
                self.state = State::ScriptDataEscaped;
            }
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // §13.2.5.22
    fn script_data_escaped_dash_dash_state(&mut self) {
        match self.advance() {
            Some('-') => self.emit(Token::Character('-')),
            Some('<') => self.state = State::ScriptDataEscapedLessThanSign,
            Some('>') => {
                self.emit(Token::Character('>'));
                self.state = State::ScriptData;
            }
            Some('\0') => {
                self.error();
                self.emit(Token::Character(REPLACEMENT));
                self.state = State::ScriptDataEscaped;
            }
            Some(c) => {
                self.emit(Token::Character(c));
                self.state = State::ScriptDataEscaped;
            }
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // §13.2.5.23
    fn script_data_escaped_lt_state(&mut self) {
        match self.peek() {
            Some('/') => {
                self.pos += 1;
                self.temp.clear();
                self.state = State::ScriptDataEscapedEndTagOpen;
            }
            Some(c) if c.is_ascii_alphabetic() => {
                self.temp.clear();
                self.emit(Token::Character('<'));
                self.state = State::ScriptDataDoubleEscapeStart;
            }
            _ => {
                self.emit(Token::Character('<'));
                self.state = State::ScriptDataEscaped;
            }
        }
    }

    // §13.2.5.24
    fn script_data_escaped_end_tag_open_state(&mut self) {
        if matches!(self.peek(), Some(c) if c.is_ascii_alphabetic()) {
            self.start_tag(TagKind::End);
            self.state = State::ScriptDataEscapedEndTagName;
        } else {
            self.emit(Token::Character('<'));
            self.emit(Token::Character('/'));
            self.state = State::ScriptDataEscaped;
        }
    }

    // §13.2.5.25
    fn script_data_escaped_end_tag_name_state(&mut self) {
        if self.end_tag_name_step(State::ScriptDataEscaped) {
            self.state = State::ScriptDataEscaped;
        }
    }

    // §13.2.5.26
    fn script_data_double_escape_start_state(&mut self) {
        match self.peek() {
            Some(c) if is_html_whitespace(c) || c == '/' || c == '>' => {
                self.pos += 1;
                self.emit(Token::Character(c));
                self.state = if self.temp == "script" {
                    State::ScriptDataDoubleEscaped
                } else {
                    State::ScriptDataEscaped
                };
            }
            Some(c) if c.is_ascii_alphabetic() => {
                self.pos += 1;
                self.temp.push(c.to_ascii_lowercase());
                self.emit(Token::Character(c));
            }
            _ => self.state = State::ScriptDataEscaped,
        }
    }

    // §13.2.5.27
    fn script_data_double_escaped_state(&mut self) {
        match self.advance() {
            Some('-') => {
                self.emit(Token::Character('-'));
                self.state = State::ScriptDataDoubleEscapedDash;
            }
            Some('<') => {
                self.emit(Token::Character('<'));
                self.state = State::ScriptDataDoubleEscapedLessThanSign;
            }
            Some('\0') => {
                self.error();
                self.emit(Token::Character(REPLACEMENT));
            }
            Some(c) => self.emit(Token::Character(c)),
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // §13.2.5.28
    fn script_data_double_escaped_dash_state(&mut self) {
        match self.advance() {
            Some('-') => {
                self.emit(Token::Character('-'));
                self.state = State::ScriptDataDoubleEscapedDashDash;
            }
            Some('<') => {
                self.emit(Token::Character('<'));
                self.state = State::ScriptDataDoubleEscapedLessThanSign;
            }
            Some('\0') => {
                self.error();
                self.emit(Token::Character(REPLACEMENT));
                self.state = State::ScriptDataDoubleEscaped;
            }
            Some(c) => {
                self.emit(Token::Character(c));
                self.state = State::ScriptDataDoubleEscaped;
            }
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // §13.2.5.29
    fn script_data_double_escaped_dash_dash_state(&mut self) {
        match self.advance() {
            Some('-') => self.emit(Token::Character('-')),
            Some('<') => {
                self.emit(Token::Character('<'));
                self.state = State::ScriptDataDoubleEscapedLessThanSign;
            }
            Some('>') => {
                self.emit(Token::Character('>'));
                self.state = State::ScriptData;
            }
            Some('\0') => {
                self.error();
                self.emit(Token::Character(REPLACEMENT));
                self.state = State::ScriptDataDoubleEscaped;
            }
            Some(c) => {
                self.emit(Token::Character(c));
                self.state = State::ScriptDataDoubleEscaped;
            }
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // §13.2.5.30
    fn script_data_double_escaped_lt_state(&mut self) {
        if self.peek() == Some('/') {
            self.pos += 1;
            self.temp.clear();
            self.emit(Token::Character('/'));
            self.state = State::ScriptDataDoubleEscapeEnd;
        } else {
            self.state = State::ScriptDataDoubleEscaped;
        }
    }

    // §13.2.5.31
    fn script_data_double_escape_end_state(&mut self) {
        match self.peek() {
            Some(c) if is_html_whitespace(c) || c == '/' || c == '>' => {
                self.pos += 1;
                self.emit(Token::Character(c));
                self.state = if self.temp == "script" {
                    State::ScriptDataEscaped
                } else {
                    State::ScriptDataDoubleEscaped
                };
            }
            Some(c) if c.is_ascii_alphabetic() => {
                self.pos += 1;
                self.temp.push(c.to_ascii_lowercase());
                self.emit(Token::Character(c));
            }
            _ => self.state = State::ScriptDataDoubleEscaped,
        }
    }

    // §13.2.5.32
    fn before_attribute_name_state(&mut self) {
        match self.peek() {
            Some(c) if is_html_whitespace(c) => {
                self.pos += 1;
            }
            Some('/') | Some('>') | None => self.state = State::AfterAttributeName,
            Some('=') => {
                // unexpected-equals-sign-before-attribute-name: the "=" starts
                // the attribute's *name*.
                self.pos += 1;
                self.error();
                self.new_attribute();
                self.attr_name.push('=');
                self.state = State::AttributeName;
            }
            Some(_) => {
                self.new_attribute();
                self.state = State::AttributeName;
            }
        }
    }

    // §13.2.5.33
    fn attribute_name_state(&mut self) {
        match self.peek() {
            Some(c) if is_html_whitespace(c) => {
                self.pos += 1;
                self.state = State::AfterAttributeName;
            }
            Some('/') | Some('>') | None => self.state = State::AfterAttributeName,
            Some('=') => {
                self.pos += 1;
                self.state = State::BeforeAttributeValue;
            }
            Some('\0') => {
                self.pos += 1;
                self.error();
                self.attr_name.push(REPLACEMENT);
            }
            Some(c) => {
                self.pos += 1;
                if matches!(c, '"' | '\'' | '<') {
                    self.error();
                }
                self.attr_name.push(c.to_ascii_lowercase());
            }
        }
    }

    // §13.2.5.34
    fn after_attribute_name_state(&mut self) {
        match self.advance() {
            Some(c) if is_html_whitespace(c) => {}
            Some('/') => self.state = State::SelfClosingStartTag,
            Some('=') => self.state = State::BeforeAttributeValue,
            Some('>') => {
                self.state = State::Data;
                self.emit_tag();
            }
            Some(_) => {
                self.pos -= 1;
                self.new_attribute();
                self.state = State::AttributeName;
            }
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // §13.2.5.35
    fn before_attribute_value_state(&mut self) {
        match self.peek() {
            Some(c) if is_html_whitespace(c) => {
                self.pos += 1;
            }
            Some('"') => {
                self.pos += 1;
                self.state = State::AttributeValueDoubleQuoted;
            }
            Some('\'') => {
                self.pos += 1;
                self.state = State::AttributeValueSingleQuoted;
            }
            Some('>') => {
                self.pos += 1;
                self.error();
                self.state = State::Data;
                self.emit_tag();
            }
            _ => self.state = State::AttributeValueUnquoted,
        }
    }

    // §13.2.5.36 and §13.2.5.37
    fn attribute_value_quoted_state(&mut self, quote: char) {
        match self.advance() {
            Some(c) if c == quote => self.state = State::AfterAttributeValueQuoted,
            Some('&') => {
                let text = self.consume_character_reference(true);
                self.attr_value.push_str(&text);
            }
            Some('\0') => {
                self.error();
                self.attr_value.push(REPLACEMENT);
            }
            Some(c) => self.attr_value.push(c),
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // §13.2.5.38
    fn attribute_value_unquoted_state(&mut self) {
        match self.advance() {
            Some(c) if is_html_whitespace(c) => self.state = State::BeforeAttributeName,
            Some('&') => {
                let text = self.consume_character_reference(true);
                self.attr_value.push_str(&text);
            }
            Some('>') => {
                self.state = State::Data;
                self.emit_tag();
            }
            Some('\0') => {
                self.error();
                self.attr_value.push(REPLACEMENT);
            }
            Some(c) => {
                if matches!(c, '"' | '\'' | '<' | '=' | '`') {
                    self.error();
                }
                self.attr_value.push(c);
            }
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // §13.2.5.39
    fn after_attribute_value_quoted_state(&mut self) {
        match self.peek() {
            Some(c) if is_html_whitespace(c) => {
                self.pos += 1;
                self.state = State::BeforeAttributeName;
            }
            Some('/') => {
                self.pos += 1;
                self.state = State::SelfClosingStartTag;
            }
            Some('>') => {
                self.pos += 1;
                self.state = State::Data;
                self.emit_tag();
            }
            Some(_) => {
                self.error();
                self.state = State::BeforeAttributeName;
            }
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // §13.2.5.40
    fn self_closing_start_tag_state(&mut self) {
        match self.advance() {
            Some('>') => {
                self.tag_self_closing = true;
                self.state = State::Data;
                self.emit_tag();
            }
            Some(_) => {
                self.error();
                self.pos -= 1;
                self.state = State::BeforeAttributeName;
            }
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // §13.2.5.41
    /// Try to read a processing instruction, with the cursor still on the `?`
    /// of a `<?`.
    ///
    /// Returns `false` — cursor untouched — when the markup is not a
    /// well-formed processing instruction, which sends the caller down the
    /// historical bogus-comment path.
    ///
    /// # Provenance
    ///
    /// This is *not* in §13.2.5 as published for most of HTML's life: `<?…>`
    /// has always been a comment. A 2026 revision gives well-formed ones a
    /// real `ProcessingInstruction` node, and the html5lib fixtures
    /// (`processing-instructions.dat`, added to web-platform-tests in July
    /// 2026) encode the rules below:
    ///
    /// * The target starts with an ASCII letter or `_` and continues with
    ///   ASCII alphanumerics, `-` and `_`. Anything else — a digit or `-`
    ///   first, a `$`, a non-ASCII letter — is not a target and the whole
    ///   thing degrades to a comment.
    /// * A target beginning with `xml` in any case is reserved, as in XML,
    ///   and likewise degrades.
    /// * The target is separated from the data by a run of whitespace, which
    ///   is dropped. A `?` may follow the target directly, in which case it
    ///   belongs to the data.
    /// * The data runs to the first `?>` or `>`.
    /// * End of input anywhere inside the construct discards it entirely —
    ///   unlike a bogus comment, which is emitted.
    ///
    /// It is written as a single scan rather than as spec states because
    /// there is no published state machine to mirror.
    fn processing_instruction(&mut self) -> bool {
        let mut i = self.pos + 1;

        match self.input.get(i) {
            Some(&c) if c.is_ascii_alphabetic() || c == '_' => {}
            Some(_) => return false,
            None => return self.abandon_at_eof(),
        }
        let target_start = i;
        while let Some(&c) = self.input.get(i) {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                i += 1;
            } else {
                break;
            }
        }
        let target: String = self.input[target_start..i].iter().collect();
        if target.len() >= 3 && target[..3].eq_ignore_ascii_case("xml") {
            return false;
        }

        let data_start = match self.input.get(i).copied() {
            None => return self.abandon_at_eof(),
            Some('>') => {
                self.pos = i + 1;
                self.state = State::Data;
                self.emit(Token::ProcessingInstruction {
                    target,
                    data: String::new(),
                });
                return true;
            }
            Some('?') => i,
            Some(c) if is_html_whitespace(c) => {
                while matches!(self.input.get(i), Some(&c) if is_html_whitespace(c)) {
                    i += 1;
                }
                i
            }
            Some(_) => return false,
        };

        let mut j = data_start;
        loop {
            let end = match self.input.get(j).copied() {
                None => return self.abandon_at_eof(),
                Some('>') => j + 1,
                Some('?') if self.input.get(j + 1) == Some(&'>') => j + 2,
                Some(_) => {
                    j += 1;
                    continue;
                }
            };
            let data: String = self.input[data_start..j].iter().collect();
            self.pos = end;
            self.state = State::Data;
            self.emit(Token::ProcessingInstruction { target, data });
            return true;
        }
    }

    /// End of input part-way through a processing instruction: consume the
    /// rest and emit nothing but EOF.
    fn abandon_at_eof(&mut self) -> bool {
        self.error();
        self.pos = self.input.len();
        self.emit_eof();
        true
    }

    fn bogus_comment_state(&mut self) {
        match self.advance() {
            Some('>') => {
                let data = std::mem::take(&mut self.comment);
                self.emit(Token::Comment(data));
                self.state = State::Data;
            }
            Some('\0') => {
                self.error();
                self.comment.push(REPLACEMENT);
            }
            Some(c) => self.comment.push(c),
            None => {
                let data = std::mem::take(&mut self.comment);
                self.emit(Token::Comment(data));
                self.emit_eof();
            }
        }
    }

    // §13.2.5.42
    fn markup_declaration_open_state(&mut self) {
        if self.lookahead("--", false) {
            self.pos += 2;
            self.comment.clear();
            self.state = State::CommentStart;
        } else if self.lookahead("DOCTYPE", true) {
            self.pos += 7;
            self.state = State::Doctype;
        } else if self.lookahead("[CDATA[", false) {
            self.pos += 7;
            if self.in_foreign {
                self.state = State::CdataSection;
            } else {
                // cdata-in-html-content: the whole thing becomes a comment
                // whose data starts with "[CDATA[".
                self.error();
                self.comment = "[CDATA[".to_string();
                self.state = State::BogusComment;
            }
        } else {
            self.error();
            self.comment.clear();
            self.state = State::BogusComment;
        }
    }

    // §13.2.5.43
    fn comment_start_state(&mut self) {
        match self.peek() {
            Some('-') => {
                self.pos += 1;
                self.state = State::CommentStartDash;
            }
            Some('>') => {
                self.pos += 1;
                self.error();
                let data = std::mem::take(&mut self.comment);
                self.emit(Token::Comment(data));
                self.state = State::Data;
            }
            _ => self.state = State::Comment,
        }
    }

    // §13.2.5.44
    fn comment_start_dash_state(&mut self) {
        match self.advance() {
            Some('-') => self.state = State::CommentEnd,
            Some('>') => {
                self.error();
                let data = std::mem::take(&mut self.comment);
                self.emit(Token::Comment(data));
                self.state = State::Data;
            }
            Some(_) => {
                self.pos -= 1;
                self.comment.push('-');
                self.state = State::Comment;
            }
            None => {
                self.error();
                let data = std::mem::take(&mut self.comment);
                self.emit(Token::Comment(data));
                self.emit_eof();
            }
        }
    }

    // §13.2.5.45
    fn comment_state(&mut self) {
        match self.advance() {
            Some('<') => {
                self.comment.push('<');
                self.state = State::CommentLessThanSign;
            }
            Some('-') => self.state = State::CommentEndDash,
            Some('\0') => {
                self.error();
                self.comment.push(REPLACEMENT);
            }
            Some(c) => self.comment.push(c),
            None => {
                self.error();
                let data = std::mem::take(&mut self.comment);
                self.emit(Token::Comment(data));
                self.emit_eof();
            }
        }
    }

    // §13.2.5.46
    fn comment_lt_state(&mut self) {
        match self.peek() {
            Some('!') => {
                self.pos += 1;
                self.comment.push('!');
                self.state = State::CommentLessThanSignBang;
            }
            Some('<') => {
                self.pos += 1;
                self.comment.push('<');
            }
            _ => self.state = State::Comment,
        }
    }

    // §13.2.5.47
    fn comment_lt_bang_state(&mut self) {
        if self.peek() == Some('-') {
            self.pos += 1;
            self.state = State::CommentLessThanSignBangDash;
        } else {
            self.state = State::Comment;
        }
    }

    // §13.2.5.48
    fn comment_lt_bang_dash_state(&mut self) {
        if self.peek() == Some('-') {
            self.pos += 1;
            self.state = State::CommentLessThanSignBangDashDash;
        } else {
            self.state = State::CommentEndDash;
        }
    }

    // §13.2.5.49
    fn comment_lt_bang_dash_dash_state(&mut self) {
        match self.peek() {
            Some('>') | None => self.state = State::CommentEnd,
            _ => {
                self.error();
                self.state = State::CommentEnd;
            }
        }
    }

    // §13.2.5.50
    fn comment_end_dash_state(&mut self) {
        match self.advance() {
            Some('-') => self.state = State::CommentEnd,
            Some(_) => {
                self.pos -= 1;
                self.comment.push('-');
                self.state = State::Comment;
            }
            None => {
                self.error();
                let data = std::mem::take(&mut self.comment);
                self.emit(Token::Comment(data));
                self.emit_eof();
            }
        }
    }

    // §13.2.5.51
    fn comment_end_state(&mut self) {
        match self.advance() {
            Some('>') => {
                let data = std::mem::take(&mut self.comment);
                self.emit(Token::Comment(data));
                self.state = State::Data;
            }
            Some('!') => self.state = State::CommentEndBang,
            Some('-') => self.comment.push('-'),
            Some(_) => {
                self.pos -= 1;
                self.comment.push_str("--");
                self.state = State::Comment;
            }
            None => {
                self.error();
                let data = std::mem::take(&mut self.comment);
                self.emit(Token::Comment(data));
                self.emit_eof();
            }
        }
    }

    // §13.2.5.52
    fn comment_end_bang_state(&mut self) {
        match self.advance() {
            Some('-') => {
                self.comment.push_str("--!");
                self.state = State::CommentEndDash;
            }
            Some('>') => {
                self.error();
                let data = std::mem::take(&mut self.comment);
                self.emit(Token::Comment(data));
                self.state = State::Data;
            }
            Some(_) => {
                self.pos -= 1;
                self.comment.push_str("--!");
                self.state = State::Comment;
            }
            None => {
                self.error();
                let data = std::mem::take(&mut self.comment);
                self.emit(Token::Comment(data));
                self.emit_eof();
            }
        }
    }

    // §13.2.5.53
    fn doctype_state(&mut self) {
        match self.peek() {
            Some(c) if is_html_whitespace(c) => {
                self.pos += 1;
                self.state = State::BeforeDoctypeName;
            }
            Some('>') => self.state = State::BeforeDoctypeName,
            Some(_) => {
                self.error();
                self.state = State::BeforeDoctypeName;
            }
            None => {
                self.error();
                self.doctype = DoctypeToken {
                    force_quirks: true,
                    ..Default::default()
                };
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.emit_eof();
            }
        }
    }

    // §13.2.5.54
    fn before_doctype_name_state(&mut self) {
        match self.advance() {
            Some(c) if is_html_whitespace(c) => {}
            Some('\0') => {
                self.error();
                self.doctype = DoctypeToken {
                    name: Some(REPLACEMENT.to_string()),
                    ..Default::default()
                };
                self.state = State::DoctypeName;
            }
            Some('>') => {
                self.error();
                self.emit(Token::Doctype(DoctypeToken {
                    force_quirks: true,
                    ..Default::default()
                }));
                self.state = State::Data;
            }
            Some(c) => {
                self.doctype = DoctypeToken {
                    name: Some(c.to_ascii_lowercase().to_string()),
                    ..Default::default()
                };
                self.state = State::DoctypeName;
            }
            None => {
                self.error();
                self.emit(Token::Doctype(DoctypeToken {
                    force_quirks: true,
                    ..Default::default()
                }));
                self.emit_eof();
            }
        }
    }

    // §13.2.5.55
    fn doctype_name_state(&mut self) {
        match self.advance() {
            Some(c) if is_html_whitespace(c) => self.state = State::AfterDoctypeName,
            Some('>') => {
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.state = State::Data;
            }
            Some('\0') => {
                self.error();
                self.doctype.name.get_or_insert_with(String::new).push(REPLACEMENT);
            }
            Some(c) => self
                .doctype
                .name
                .get_or_insert_with(String::new)
                .push(c.to_ascii_lowercase()),
            None => self.doctype_eof(),
        }
    }

    /// The "EOF in a DOCTYPE" arm, which every DOCTYPE state shares.
    fn doctype_eof(&mut self) {
        self.error();
        self.doctype.force_quirks = true;
        let d = std::mem::take(&mut self.doctype);
        self.emit(Token::Doctype(d));
        self.emit_eof();
    }

    // §13.2.5.56
    fn after_doctype_name_state(&mut self) {
        match self.peek() {
            Some(c) if is_html_whitespace(c) => {
                self.pos += 1;
            }
            Some('>') => {
                self.pos += 1;
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.state = State::Data;
            }
            Some(_) => {
                if self.lookahead("PUBLIC", true) {
                    self.pos += 6;
                    self.state = State::AfterDoctypePublicKeyword;
                } else if self.lookahead("SYSTEM", true) {
                    self.pos += 6;
                    self.state = State::AfterDoctypeSystemKeyword;
                } else {
                    self.error();
                    self.doctype.force_quirks = true;
                    self.state = State::BogusDoctype;
                }
            }
            None => self.doctype_eof(),
        }
    }

    // §13.2.5.57
    fn after_doctype_public_keyword_state(&mut self) {
        match self.advance() {
            Some(c) if is_html_whitespace(c) => self.state = State::BeforeDoctypePublicIdentifier,
            Some('"') => {
                self.error();
                self.doctype.public_id = Some(String::new());
                self.state = State::DoctypePublicIdentifierDoubleQuoted;
            }
            Some('\'') => {
                self.error();
                self.doctype.public_id = Some(String::new());
                self.state = State::DoctypePublicIdentifierSingleQuoted;
            }
            Some('>') => {
                self.error();
                self.doctype.force_quirks = true;
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.state = State::Data;
            }
            Some(_) => {
                self.pos -= 1;
                self.error();
                self.doctype.force_quirks = true;
                self.state = State::BogusDoctype;
            }
            None => self.doctype_eof(),
        }
    }

    // §13.2.5.58
    fn before_doctype_public_identifier_state(&mut self) {
        match self.advance() {
            Some(c) if is_html_whitespace(c) => {}
            Some('"') => {
                self.doctype.public_id = Some(String::new());
                self.state = State::DoctypePublicIdentifierDoubleQuoted;
            }
            Some('\'') => {
                self.doctype.public_id = Some(String::new());
                self.state = State::DoctypePublicIdentifierSingleQuoted;
            }
            Some('>') => {
                self.error();
                self.doctype.force_quirks = true;
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.state = State::Data;
            }
            Some(_) => {
                self.pos -= 1;
                self.error();
                self.doctype.force_quirks = true;
                self.state = State::BogusDoctype;
            }
            None => self.doctype_eof(),
        }
    }

    // §13.2.5.59 and §13.2.5.60
    fn doctype_public_identifier_state(&mut self, quote: char) {
        match self.advance() {
            Some(c) if c == quote => self.state = State::AfterDoctypePublicIdentifier,
            Some('\0') => {
                self.error();
                self.doctype
                    .public_id
                    .get_or_insert_with(String::new)
                    .push(REPLACEMENT);
            }
            Some('>') => {
                self.error();
                self.doctype.force_quirks = true;
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.state = State::Data;
            }
            Some(c) => self
                .doctype
                .public_id
                .get_or_insert_with(String::new)
                .push(c),
            None => self.doctype_eof(),
        }
    }

    // §13.2.5.61
    fn after_doctype_public_identifier_state(&mut self) {
        match self.advance() {
            Some(c) if is_html_whitespace(c) => {
                self.state = State::BetweenDoctypePublicAndSystemIdentifiers
            }
            Some('>') => {
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.state = State::Data;
            }
            Some('"') => {
                self.error();
                self.doctype.system_id = Some(String::new());
                self.state = State::DoctypeSystemIdentifierDoubleQuoted;
            }
            Some('\'') => {
                self.error();
                self.doctype.system_id = Some(String::new());
                self.state = State::DoctypeSystemIdentifierSingleQuoted;
            }
            Some(_) => {
                self.pos -= 1;
                self.error();
                self.doctype.force_quirks = true;
                self.state = State::BogusDoctype;
            }
            None => self.doctype_eof(),
        }
    }

    // §13.2.5.62
    fn between_doctype_ids_state(&mut self) {
        match self.advance() {
            Some(c) if is_html_whitespace(c) => {}
            Some('>') => {
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.state = State::Data;
            }
            Some('"') => {
                self.doctype.system_id = Some(String::new());
                self.state = State::DoctypeSystemIdentifierDoubleQuoted;
            }
            Some('\'') => {
                self.doctype.system_id = Some(String::new());
                self.state = State::DoctypeSystemIdentifierSingleQuoted;
            }
            Some(_) => {
                self.pos -= 1;
                self.error();
                self.doctype.force_quirks = true;
                self.state = State::BogusDoctype;
            }
            None => self.doctype_eof(),
        }
    }

    // §13.2.5.63
    fn after_doctype_system_keyword_state(&mut self) {
        match self.advance() {
            Some(c) if is_html_whitespace(c) => self.state = State::BeforeDoctypeSystemIdentifier,
            Some('"') => {
                self.error();
                self.doctype.system_id = Some(String::new());
                self.state = State::DoctypeSystemIdentifierDoubleQuoted;
            }
            Some('\'') => {
                self.error();
                self.doctype.system_id = Some(String::new());
                self.state = State::DoctypeSystemIdentifierSingleQuoted;
            }
            Some('>') => {
                self.error();
                self.doctype.force_quirks = true;
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.state = State::Data;
            }
            Some(_) => {
                self.pos -= 1;
                self.error();
                self.doctype.force_quirks = true;
                self.state = State::BogusDoctype;
            }
            None => self.doctype_eof(),
        }
    }

    // §13.2.5.64
    fn before_doctype_system_identifier_state(&mut self) {
        match self.advance() {
            Some(c) if is_html_whitespace(c) => {}
            Some('"') => {
                self.doctype.system_id = Some(String::new());
                self.state = State::DoctypeSystemIdentifierDoubleQuoted;
            }
            Some('\'') => {
                self.doctype.system_id = Some(String::new());
                self.state = State::DoctypeSystemIdentifierSingleQuoted;
            }
            Some('>') => {
                self.error();
                self.doctype.force_quirks = true;
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.state = State::Data;
            }
            Some(_) => {
                self.pos -= 1;
                self.error();
                self.doctype.force_quirks = true;
                self.state = State::BogusDoctype;
            }
            None => self.doctype_eof(),
        }
    }

    // §13.2.5.65 and §13.2.5.66
    fn doctype_system_identifier_state(&mut self, quote: char) {
        match self.advance() {
            Some(c) if c == quote => self.state = State::AfterDoctypeSystemIdentifier,
            Some('\0') => {
                self.error();
                self.doctype
                    .system_id
                    .get_or_insert_with(String::new)
                    .push(REPLACEMENT);
            }
            Some('>') => {
                self.error();
                self.doctype.force_quirks = true;
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.state = State::Data;
            }
            Some(c) => self
                .doctype
                .system_id
                .get_or_insert_with(String::new)
                .push(c),
            None => self.doctype_eof(),
        }
    }

    // §13.2.5.67
    fn after_doctype_system_identifier_state(&mut self) {
        match self.advance() {
            Some(c) if is_html_whitespace(c) => {}
            Some('>') => {
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.state = State::Data;
            }
            Some(_) => {
                self.pos -= 1;
                self.error();
                // Note: no force-quirks here. An unexpected character after
                // the system identifier is an error but does not change the
                // document's mode.
                self.state = State::BogusDoctype;
            }
            None => self.doctype_eof(),
        }
    }

    // §13.2.5.68
    fn bogus_doctype_state(&mut self) {
        match self.advance() {
            Some('>') => {
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.state = State::Data;
            }
            Some('\0') => self.error(),
            Some(_) => {}
            None => {
                let d = std::mem::take(&mut self.doctype);
                self.emit(Token::Doctype(d));
                self.emit_eof();
            }
        }
    }

    // §13.2.5.69
    fn cdata_section_state(&mut self) {
        match self.advance() {
            Some(']') => self.state = State::CdataSectionBracket,
            Some(c) => self.emit(Token::Character(c)),
            None => {
                self.error();
                self.emit_eof();
            }
        }
    }

    // §13.2.5.70
    fn cdata_section_bracket_state(&mut self) {
        if self.peek() == Some(']') {
            self.pos += 1;
            self.state = State::CdataSectionEnd;
        } else {
            self.emit(Token::Character(']'));
            self.state = State::CdataSection;
        }
    }

    // §13.2.5.71
    fn cdata_section_end_state(&mut self) {
        match self.peek() {
            Some(']') => {
                self.pos += 1;
                self.emit(Token::Character(']'));
            }
            Some('>') => {
                self.pos += 1;
                self.state = State::Data;
            }
            _ => {
                self.emit(Token::Character(']'));
                self.emit(Token::Character(']'));
                self.state = State::CdataSection;
            }
        }
    }

    // -- character references, §13.2.5.72-80 ---------------------------------

    /// Consume a character reference and return the text it expands to.
    ///
    /// Called with the cursor just past the `&`. `in_attribute` selects the
    /// historical rule that keeps `?a=1&notit=2` from turning into `¬it=2`.
    ///
    /// When nothing matches, this returns a literal `"&"` and leaves the
    /// cursor untouched. That is exactly what the spec's "flush code points
    /// consumed as a character reference" plus the ambiguous-ampersand state
    /// amount to: the consumed characters get reprocessed by the return state,
    /// which appends or emits them unchanged either way.
    fn consume_character_reference(&mut self, in_attribute: bool) -> String {
        match self.peek() {
            Some('#') => self.numeric_character_reference(),
            Some(c) if c.is_ascii_alphanumeric() => self.named_character_reference(in_attribute),
            _ => "&".to_string(),
        }
    }

    // §13.2.5.73 - §13.2.5.74
    fn named_character_reference(&mut self, in_attribute: bool) -> String {
        let start = self.pos;

        // Every name is ASCII alphanumerics plus an optional trailing ";", so
        // the candidate window stops at the first character that cannot be
        // part of one. That bounds the search to LONGEST_NAME characters.
        let mut candidate: Vec<char> = Vec::with_capacity(LONGEST_NAME);
        for i in 0..LONGEST_NAME {
            match self.peek_at(i) {
                Some(c) if c.is_ascii_alphanumeric() => candidate.push(c),
                Some(';') => {
                    candidate.push(';');
                    break;
                }
                _ => break,
            }
        }

        // Longest match wins: "&notin;" must not be read as "&not" + "in;".
        let mut matched: Option<(usize, &'static str)> = None;
        for len in (1..=candidate.len()).rev() {
            let name: String = candidate[..len].iter().collect();
            if let Ok(idx) = NAMED_REFERENCES.binary_search_by(|(n, _)| (*n).cmp(&name.as_str())) {
                matched = Some((len, NAMED_REFERENCES[idx].1));
                break;
            }
        }

        let Some((len, replacement)) = matched else {
            // No match: ambiguous ampersand. The "&" is literal and the
            // characters we peeked at are left for the return state.
            self.error();
            return "&".to_string();
        };

        if in_attribute && candidate[len - 1] != ';' {
            // "If the character reference was consumed as part of an
            // attribute, and the last character matched is not U+003B, and the
            // next input character is either U+003D or an ASCII alphanumeric,
            // then, for historical reasons, flush code points consumed as a
            // character reference and switch to the return state."
            let next = self.peek_at(len);
            if matches!(next, Some('=')) || matches!(next, Some(c) if c.is_ascii_alphanumeric()) {
                self.pos = start + len;
                let mut out = String::from("&");
                out.extend(&candidate[..len]);
                return out;
            }
        }

        if candidate[len - 1] != ';' {
            // missing-semicolon-after-character-reference
            self.error();
        }
        self.pos = start + len;
        replacement.to_string()
    }

    // §13.2.5.75 - §13.2.5.80
    fn numeric_character_reference(&mut self) -> String {
        let start = self.pos; // at the '#'
        self.pos += 1;

        let hex = matches!(self.peek(), Some('x') | Some('X'));
        if hex {
            self.pos += 1;
        }

        let digits_start = self.pos;
        let mut value: u32 = 0;
        let mut overflow = false;
        while let Some(c) = self.peek() {
            let digit = if hex {
                c.to_digit(16)
            } else {
                c.to_digit(10)
            };
            let Some(d) = digit else { break };
            self.pos += 1;
            // Clamp instead of wrapping: a 40-digit reference is still just
            // "out of range", and wrapping could land it back on a valid
            // codepoint.
            if !overflow {
                match value
                    .checked_mul(if hex { 16 } else { 10 })
                    .and_then(|v| v.checked_add(d))
                {
                    Some(v) => value = v,
                    None => overflow = true,
                }
            }
        }

        if self.pos == digits_start {
            // absence-of-digits-in-numeric-character-reference: the whole run
            // is literal text.
            self.error();
            self.pos = start;
            return "&".to_string();
        }

        if self.peek() == Some(';') {
            self.pos += 1;
        } else {
            // missing-semicolon-after-character-reference
            self.error();
        }

        if overflow {
            self.error();
            return REPLACEMENT.to_string();
        }
        self.error_check_numeric(value);
        numeric_reference_char(value).to_string()
    }

    /// Count the parse errors the numeric-character-reference-end state
    /// reports. The substitution itself lives in [`numeric_reference_char`].
    fn error_check_numeric(&mut self, value: u32) {
        let bad = value == 0
            || value > 0x10FFFF
            || (0xD800..=0xDFFF).contains(&value)
            || is_noncharacter(value)
            || value == 0x0D
            || (is_control(value) && !matches!(value, 0x09 | 0x0A | 0x0C | 0x20));
        if bad {
            self.error();
        }
    }
}

/// §13.2.3.5: normalize CRLF and lone CR to LF before anything else sees them.
fn preprocess(input: &str) -> Vec<char> {
    let mut out = Vec::with_capacity(input.len());
    let mut chars = input.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\r' {
            if chars.peek() == Some(&'\n') {
                chars.next();
            }
            out.push('\n');
        } else {
            out.push(c);
        }
    }
    out
}

fn is_control(v: u32) -> bool {
    v <= 0x1F || (0x7F..=0x9F).contains(&v)
}

fn is_noncharacter(v: u32) -> bool {
    (0xFDD0..=0xFDEF).contains(&v) || (v & 0xFFFE) == 0xFFFE
}

/// The numeric-character-reference-end substitutions, §13.2.5.80.
///
/// The 0x80-0x9F block is the interesting one: those code points are C1
/// controls in Unicode, but authors writing `&#147;` meant the windows-1252
/// character at that byte, and the spec bakes that mistake in.
fn numeric_reference_char(value: u32) -> char {
    match value {
        0x00 => REPLACEMENT,
        v if v > 0x10FFFF => REPLACEMENT,
        v if (0xD800..=0xDFFF).contains(&v) => REPLACEMENT,
        0x80 => '\u{20AC}',
        0x82 => '\u{201A}',
        0x83 => '\u{0192}',
        0x84 => '\u{201E}',
        0x85 => '\u{2026}',
        0x86 => '\u{2020}',
        0x87 => '\u{2021}',
        0x88 => '\u{02C6}',
        0x89 => '\u{2030}',
        0x8A => '\u{0160}',
        0x8B => '\u{2039}',
        0x8C => '\u{0152}',
        0x8E => '\u{017D}',
        0x91 => '\u{2018}',
        0x92 => '\u{2019}',
        0x93 => '\u{201C}',
        0x94 => '\u{201D}',
        0x95 => '\u{2022}',
        0x96 => '\u{2013}',
        0x97 => '\u{2014}',
        0x98 => '\u{02DC}',
        0x99 => '\u{2122}',
        0x9A => '\u{0161}',
        0x9B => '\u{203A}',
        0x9C => '\u{0153}',
        0x9E => '\u{017E}',
        0x9F => '\u{0178}',
        v => char::from_u32(v).unwrap_or(REPLACEMENT),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tokenize(input: &str) -> Vec<Token> {
        let mut t = Tokenizer::new(input);
        let mut out = Vec::new();
        loop {
            let tok = t.next_token();
            let end = tok == Token::Eof;
            out.push(tok);
            if end {
                break;
            }
        }
        out
    }

    fn text_of(tokens: &[Token]) -> String {
        tokens
            .iter()
            .filter_map(|t| match t {
                Token::Character(c) => Some(*c),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn plain_text_is_characters_then_eof() {
        let toks = tokenize("hi");
        assert_eq!(text_of(&toks), "hi");
        assert_eq!(toks.last(), Some(&Token::Eof));
    }

    #[test]
    fn tag_names_are_lowercased_but_attribute_values_are_not() {
        let toks = tokenize("<DIV CLASS=Foo>");
        match &toks[0] {
            Token::StartTag(t) => {
                assert_eq!(t.name, "div");
                assert_eq!(t.attrs[0].name, "class");
                assert_eq!(t.attrs[0].value, "Foo");
            }
            other => panic!("{:?}", other),
        }
    }

    #[test]
    fn duplicate_attributes_keep_the_first() {
        let toks = tokenize("<div a=1 a=2>");
        match &toks[0] {
            Token::StartTag(t) => {
                assert_eq!(t.attrs.len(), 1);
                assert_eq!(t.attrs[0].value, "1");
            }
            other => panic!("{:?}", other),
        }
    }

    #[test]
    fn named_reference_prefers_the_longest_match() {
        assert_eq!(text_of(&tokenize("&notin;")), "\u{2209}");
        assert_eq!(text_of(&tokenize("&not;")), "\u{00AC}");
        // Semicolon-less historical form, then the rest as literal text.
        assert_eq!(text_of(&tokenize("&notit;")), "\u{00AC}it;");
    }

    #[test]
    fn ambiguous_ampersand_stays_literal() {
        assert_eq!(text_of(&tokenize("&nosuchthing;")), "&nosuchthing;");
        assert_eq!(text_of(&tokenize("a & b")), "a & b");
    }

    #[test]
    fn attribute_values_keep_semicolonless_references_literal() {
        // The historical rule: in an attribute, "&not" followed by an
        // alphanumeric is *not* expanded, so query strings survive.
        let toks = tokenize("<a href='?a&notit=1'>");
        match &toks[0] {
            Token::StartTag(t) => assert_eq!(t.attr("href"), Some("?a&notit=1")),
            other => panic!("{:?}", other),
        }
        // "=" counts too, which is what actually saves query strings.
        let toks = tokenize("<a href='?a&not=1'>");
        match &toks[0] {
            Token::StartTag(t) => assert_eq!(t.attr("href"), Some("?a&not=1")),
            other => panic!("{:?}", other),
        }
        // ... but followed by anything else, it expands even without the
        // semicolon.
        let toks = tokenize("<a href='?a&not-1'>");
        match &toks[0] {
            Token::StartTag(t) => assert_eq!(t.attr("href"), Some("?a\u{00AC}-1")),
            other => panic!("{:?}", other),
        }
    }

    #[test]
    fn numeric_references_apply_the_windows_1252_swap() {
        assert_eq!(text_of(&tokenize("&#147;")), "\u{201C}");
        assert_eq!(text_of(&tokenize("&#x22;")), "\"");
        assert_eq!(text_of(&tokenize("&#0;")), "\u{FFFD}");
        assert_eq!(text_of(&tokenize("&#xD800;")), "\u{FFFD}");
        // Absurdly large values clamp rather than wrapping into a valid char.
        assert_eq!(text_of(&tokenize("&#9999999999999999;")), "\u{FFFD}");
    }

    #[test]
    fn crlf_is_normalized_before_tokenizing() {
        assert_eq!(text_of(&tokenize("a\r\nb\rc")), "a\nb\nc");
    }

    #[test]
    fn rawtext_keeps_a_bare_less_than_in_script() {
        let mut t = Tokenizer::new("<script>if (a<b) {}</script>");
        assert!(matches!(t.next_token(), Token::StartTag(_)));
        t.state = State::ScriptData;
        let mut text = String::new();
        loop {
            match t.next_token() {
                Token::Character(c) => text.push(c),
                Token::EndTag(tag) => {
                    assert_eq!(tag.name, "script");
                    break;
                }
                other => panic!("{:?}", other),
            }
        }
        assert_eq!(text, "if (a<b) {}");
    }

    #[test]
    fn script_data_double_escape_survives_a_nested_script_string() {
        let mut t = Tokenizer::new("<script><!--<script>--></script>--></script>");
        assert!(matches!(t.next_token(), Token::StartTag(_)));
        t.state = State::ScriptData;
        let mut text = String::new();
        let end = loop {
            match t.next_token() {
                Token::Character(c) => text.push(c),
                Token::EndTag(tag) => break tag,
                other => panic!("{:?}", other),
            }
        };
        assert_eq!(end.name, "script");
        // `<!--<script>` opens a double-escaped run, so the `-->` inside it is
        // text; it also *closes* the run, which is why the `</script>` right
        // after it is a real end tag and not more text.
        assert_eq!(text, "<!--<script>-->");
    }

    #[test]
    fn comments_and_bogus_comments() {
        assert_eq!(tokenize("<!-- x -->")[0], Token::Comment(" x ".into()));
        assert_eq!(tokenize("<!x>")[0], Token::Comment("x".into()));
        assert_eq!(tokenize("</ x>")[0], Token::Comment(" x".into()));
        // `<?...>` is no longer a bogus comment: it is a processing
        // instruction. Only a malformed target falls back to a comment, and
        // then the `?` is part of the comment data.
        assert_eq!(
            tokenize("<?php?>")[0],
            Token::ProcessingInstruction {
                target: "php".into(),
                data: String::new(),
            }
        );
        assert_eq!(tokenize("<?php echo 1; ?>")[0], Token::ProcessingInstruction {
            target: "php".into(),
            data: "echo 1; ".into(),
        });
        assert_eq!(tokenize("<?1?>")[0], Token::Comment("?1?".into()));
        // An `xml` target is reserved, so it degrades to a comment too.
        assert_eq!(tokenize("<?xml v?>")[0], Token::Comment("?xml v?".into()));
    }

    #[test]
    fn doctype_with_public_and_system_ids() {
        let toks = tokenize(r#"<!DOCTYPE html PUBLIC "a" "b">"#);
        match &toks[0] {
            Token::Doctype(d) => {
                assert_eq!(d.name.as_deref(), Some("html"));
                assert_eq!(d.public_id.as_deref(), Some("a"));
                assert_eq!(d.system_id.as_deref(), Some("b"));
                assert!(!d.force_quirks);
            }
            other => panic!("{:?}", other),
        }
    }

    #[test]
    fn cdata_is_a_comment_outside_foreign_content() {
        assert_eq!(tokenize("<![CDATA[x]]>")[0], Token::Comment("[CDATA[x]]".into()));
        let mut t = Tokenizer::new("<![CDATA[x]]>");
        t.in_foreign = true;
        assert_eq!(t.next_token(), Token::Character('x'));
    }

    #[test]
    fn every_entity_in_the_table_round_trips() {
        // Cheap insurance that the generated table is actually reachable:
        // feed each name back through the tokenizer and check it expands.
        for (name, expected) in NAMED_REFERENCES {
            if !name.ends_with(';') {
                continue;
            }
            let got = text_of(&tokenize(&format!("&{}", name)));
            assert_eq!(&got, expected, "entity &{}", name);
        }
    }
}
