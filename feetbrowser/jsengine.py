"""A from-scratch JavaScript engine for FeetBrowser.

A hand-written lexer, recursive-descent parser, and tree-walking interpreter
for a practical subset of ECMAScript, built around typed AST nodes.

The value model is shared with the DOM bridge (feetbrowser/jsdom.py):

    number     -> Python int or float
    string     -> Python str
    boolean    -> Python bool
    null       -> Python None
    undefined  -> the module singleton `UNDEFINED`
    array      -> Python list
    object     -> Python dict with str keys, or any "host object"
    function   -> JSFunction, or any Python callable (native)
    promise    -> JSPromise
    void       -> UNDEFINED

Asynchronous code is supported with generator coroutines: `_eval`/`_exec`
are generators that only `yield` at an `await` expression. A synchronous
driver (`_pump_sync`) runs them to completion in one pass (no `await`), while
an async driver (`_resume_async`) suspends the frame on a pending promise and
resumes it with the resolved value once the microtask runs.

Host objects implement the `js_get`/`js_set`/`js_call`/`js_new` protocol so
the DOM bridge and browser-provided natives (fetch, XMLHttpRequest, timers)
can plug straight into the interpreter.
"""

import re
from collections import deque
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


class JSException(Exception):
    """Raised for any JavaScript-level error: syntax or runtime."""


class _Undefined:
    __slots__ = ()

    def __repr__(self):
        return "undefined"


#: The singleton representing the JS `undefined` value.
UNDEFINED = _Undefined()


class _JSThrow(Exception):
    """A `throw` of an arbitrary JS value; carries the thrown value."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class _Return(BaseException):
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class _Break(BaseException):
    __slots__ = ()


class _Continue(BaseException):
    __slots__ = ()


class _Suspend:
    """Yielded by `await` to ask the async driver to resume later."""

    __slots__ = ("promise",)

    def __init__(self, promise):
        self.promise = promise


def _nullish(value):
    return value is None or value is UNDEFINED


def _is_numberish(value):
    return isinstance(value, (int, float, str))


def _to_number(value):
    """Coerce a JS value to a number; non-numeric values become NaN."""
    if value is None:
        return 0  # Number(null) === 0
    if value is UNDEFINED:
        return float("nan")
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return 0
        try:
            return _parse_number(text)
        except ValueError:
            return float("nan")
    if isinstance(value, (int, float)):
        return value
    return float("nan")


def _parse_number(text):
    if text.startswith("."):
        text = "0" + text
    if text.endswith("."):
        text = text[:-1]
    return int(text) if _all_digits(text) else float(text)


def _all_digits(text):
    return all(ch in "0123456789" for ch in text) and text != ""


def _int_index(name):
    try:
        index = int(name)
    except (TypeError, ValueError):
        return None
    if name != str(index):
        return None
    return index


def _is_objectish(value):
    if value is None or value is UNDEFINED:
        return False
    if isinstance(value, (int, float, str, bool)):
        return False
    return True


def _divide(a, b):
    if b == 0:
        if a == 0:
            return float("nan")
        return float("inf") if a > 0 else float("-inf")
    return a / b


def _modulo(a, b):
    if b == 0:
        return float("nan")
    return a % b


def _loose_eq(a, b):
    na, nb = _nullish(a), _nullish(b)
    if na or nb:
        return na and nb
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if _is_numberish(a) or _is_numberish(b):
        ca, cb = _to_number(a), _to_number(b)
        if ca != ca or cb != cb:
            return False  # NaN never equals anything
        return ca == cb
    if _is_objectish(a) and _is_objectish(b):
        return a is b
    return False


def _strict_eq(a, b):
    ta = _typeof(a)
    if ta != _typeof(b):
        return False
    if ta in ("object", "function"):
        return a is b
    if a != a and b != b:
        return False  # NaN is never equal to anything
    return a == b


def _typeof(value):
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "object"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, JSFunction) or callable(value):
        return "function"
    return "object"


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------


@dataclass
class Literal:
    value: object


@dataclass
class Identifier:
    name: str


@dataclass
class This:
    pass


@dataclass
class ArrayLit:
    items: list = field(default_factory=list)


@dataclass
class ObjectLit:
    pairs: list = field(default_factory=list)  # [(key, expr), ...]


@dataclass
class Unary:
    op: str
    operand: object


@dataclass
class Update:
    op: str            # "++" or "--"
    operand: object
    prefix: bool


@dataclass
class Binary:
    op: str
    left: object
    right: object


@dataclass
class Logical:
    op: str            # "&&" or "||"
    left: object
    right: object


@dataclass
class Conditional:
    cond: object
    then_expr: object
    else_expr: object


@dataclass
class Assign:
    op: str            # "=", "+=", "-=", "*=", "/="
    target: object
    value: object


@dataclass
class Call:
    callee: object
    args: list = field(default_factory=list)


@dataclass
class New:
    callee: object
    args: list = field(default_factory=list)


@dataclass
class Member:
    obj: object
    name: str


@dataclass
class Index:
    obj: object
    index: object


@dataclass
class FunctionExpr:
    name: str
    params: list = field(default_factory=list)
    body: list = field(default_factory=list)
    async_: bool = False


@dataclass
class Await:
    expr: object


# --- statements ---------------------------------------------------------


@dataclass
class Program:
    statements: list = field(default_factory=list)


@dataclass
class Block:
    statements: list = field(default_factory=list)


@dataclass
class VarDecl:
    kind: str          # "var", "let", "const"
    decls: list = field(default_factory=list)  # [(name, expr|None), ...]


@dataclass
class FunctionDecl:
    name: str
    params: list = field(default_factory=list)
    body: list = field(default_factory=list)
    async_: bool = False


@dataclass
class ExprStmt:
    expr: object


@dataclass
class If:
    cond: object
    then: object
    else_: object = None


@dataclass
class While:
    cond: object
    body: object


@dataclass
class For:
    init: object
    cond: object
    update: object
    body: object


@dataclass
class Return:
    value: object = None


@dataclass
class Break:
    pass


@dataclass
class Continue:
    pass


@dataclass
class Throw:
    expr: object


@dataclass
class TryCatch:
    try_block: object
    catch_param: str = None
    catch_block: object = None
    finally_block: object = None


# --------------------------------------------------------------------------
# Environments
# --------------------------------------------------------------------------


class Environment:
    """A lexical scope. `var` bindings live on the function scope."""

    __slots__ = ("vars", "lets", "consts", "parent", "function_scope")

    def __init__(self, parent=None):
        self.vars = {}
        self.lets = {}
        self.consts = {}
        self.parent = parent
        self.function_scope = parent.function_scope if parent else self

    def get(self, name):
        env = self
        while env is not None:
            if name in env.lets:
                return env.lets[name]
            if name in env.consts:
                return env.consts[name]
            if name in env.vars:
                return env.vars[name]
            env = env.parent
        return UNDEFINED

    def assign(self, name, value):
        env = self
        while env is not None:
            if name in env.lets:
                env.lets[name] = value
                return
            if name in env.consts:
                raise JSException(f"Assignment to constant variable '{name}'.")
            if name in env.vars:
                env.vars[name] = value
                return
            env = env.parent
        self.vars[name] = value

    def set_var(self, name, value):
        self.function_scope.vars[name] = value

    def set_let(self, name, value):
        self.lets[name] = value

    def set_const(self, name, value):
        self.consts[name] = value


# --------------------------------------------------------------------------
# Functions, promises
# --------------------------------------------------------------------------


class JSFunction:
    """A JavaScript closure: a function declaration or expression."""

    __slots__ = ("params", "body", "env", "interp", "name", "async_")

    def __init__(self, params, body, env, interp, name="", async_=False):
        self.params = params
        self.body = body
        self.env = env
        self.interp = interp
        self.name = name
        self.async_ = async_

    def __repr__(self):
        return f"function {self.name}()"


class JSPromise:
    """A real Promise with a microtask-scheduled `then` chain."""

    PENDING, FULFILLED, REJECTED = 0, 1, 2

    def __init__(self, interp):
        self._interp = interp
        self._state = JSPromise.PENDING
        self._value = UNDEFINED
        self._observers = []  # Python callbacks: cb(value, rejected)

    # -- state ------------------------------------------------------------

    @property
    def pending(self):
        return self._state == JSPromise.PENDING

    @property
    def rejected(self):
        return self._state == JSPromise.REJECTED

    @property
    def value(self):
        return self._value

    def resolve(self, value):
        self._settle(True, value)

    def reject(self, reason):
        self._settle(False, reason)

    def _settle(self, ok, value):
        if self._state != JSPromise.PENDING:
            return
        if ok:
            if isinstance(value, JSPromise):
                if value is self:
                    return self._settle(False, "Chaining cycle detected")
                if value.pending:
                    value._observers.append(self._adopt)
                    return
                if value.rejected:
                    return self._settle(False, value.value)
                value = value.value
            else:
                thenable = self._interp._thenable_method(value)
                if thenable is not None:
                    self._assimilate(value, thenable)
                    return
        self._state = JSPromise.FULFILLED if ok else JSPromise.REJECTED
        self._value = value
        observers, self._observers = self._observers, []
        for cb in observers:
            self._interp.enqueue(lambda cb=cb: cb(value, not ok))
        if not ok and not self._observers:
            self._interp._note_unhandled_rejection(value)

    def _adopt(self, value, rejected):
        self._settle(not rejected, value)

    def _assimilate(self, thenable, then):
        def on_ok(v):
            self.resolve(v)

        def on_err(e):
            self.reject(e)

        try:
            self._interp._call_value(then, [on_ok, on_err])
        except Exception:
            self.reject("Error while assimilating thenable")

    def _on_settle(self, cb):
        """Register a Python callback; scheduled as a microtask when settled."""
        if self._state == JSPromise.PENDING:
            self._observers.append(cb)
        else:
            ok = self._state == JSPromise.FULFILLED
            value = self._value
            self._interp.enqueue(lambda: cb(value, not ok))

    # -- JS surface -------------------------------------------------------

    def then(self, on_ok=None, on_err=None):
        child = JSPromise(self._interp)

        def cb(value, rejected):
            handler = on_err if rejected else on_ok
            if handler is None or handler is UNDEFINED:
                if rejected:
                    child.reject(value)
                else:
                    child.resolve(value)
                return
            try:
                result = self._interp._call_value(handler, [value])
            except _JSThrow as t:
                child.reject(t.value)
                return
            except JSException as e:
                child.reject(str(e))
                return
            child.resolve(result)

        self._on_settle(cb)
        return child

    def catch(self, on_err=None):
        return self.then(None, on_err)

    def finally_(self, cb=None):
        child = JSPromise(self._interp)

        def run_settle(value, rejected):
            try:
                result = self._interp._call_value(
                    cb, []) if cb is not None and cb is not UNDEFINED \
                    else UNDEFINED
            except (_JSThrow, JSException) as e:
                child.reject(e.value if isinstance(e, _JSThrow) else str(e))
                return
            if isinstance(result, JSPromise):
                def cont(_v, _r):
                    if _r:
                        child.reject(_v)
                    elif rejected:
                        child.reject(value)
                    else:
                        child.resolve(value)
                result._on_settle(cont)
            else:
                if rejected:
                    child.reject(value)
                else:
                    child.resolve(value)

        self._on_settle(run_settle)
        return child

    def js_get(self, name):
        if name == "then":
            return self.then
        if name == "catch":
            return self.catch
        if name == "finally":
            return self.finally_
        return UNDEFINED


class JSPromiseCtor:
    """The `Promise` global: constructor + statics."""

    def __init__(self, interp):
        self._interp = interp

    def js_get(self, name):
        if name == "resolve":
            return lambda value=UNDEFINED: self._static_resolve(value)
        if name == "reject":
            return lambda reason=UNDEFINED: self._static_reject(reason)
        if name == "all":
            return self._static_all
        if name == "race":
            return self._static_race
        return UNDEFINED

    def js_new(self, executor=UNDEFINED):
        p = JSPromise(self._interp)
        if executor is not UNDEFINED and executor is not None:
            try:
                self._interp._call_value(executor, [p.resolve, p.reject])
            except (_JSThrow, JSException):
                p.reject("Promise executor threw")
        return p

    def js_call(self, *args):
        return self.js_new(args[0] if args else UNDEFINED)

    def _static_resolve(self, value):
        p = JSPromise(self._interp)
        p.resolve(value)
        return p

    def _static_reject(self, reason):
        p = JSPromise(self._interp)
        p.reject(reason)
        return p

    def _static_all(self, iterable):
        interp = self._interp
        p = JSPromise(interp)
        items = list(iterable) if isinstance(iterable, list) else []
        if not items:
            p.resolve([])
            return p
        results = [UNDEFINED] * len(items)
        remaining = [len(items)]

        def attach(index):
            item = interp._as_promise(items[index])

            def cb(value, rejected):
                if rejected:
                    p.reject(value)
                    return
                results[index] = value
                remaining[0] -= 1
                if remaining[0] == 0:
                    p.resolve(results)

            item._on_settle(cb)

        for i in range(len(items)):
            attach(i)
        return p

    def _static_race(self, iterable):
        interp = self._interp
        p = JSPromise(interp)
        items = list(iterable) if isinstance(iterable, list) else []
        for item in items:
            interp._as_promise(item)._on_settle(
                lambda v, r: p.reject(v) if r else p.resolve(v))
        return p


class JSError:
    """Minimal Error host object: `.message`, `.name`."""

    def __init__(self, message=""):
        self.message = str(message if message is not UNDEFINED else "")
        self.name = "Error"

    def js_get(self, name):
        if name == "message":
            return self.message
        if name == "name":
            return self.name
        if name == "stack":
            return f"{self.name}: {self.message}"
        return UNDEFINED

    def js_repr(self):
        return f"{self.name}: {self.message}"


class _ErrorCtor:
    """The `Error` global: a constructor object returning JSError instances."""

    def js_new(self, message=""):
        return JSError(message)

    def js_call(self, *args):
        return JSError(args[0] if args else "")


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

_KEYWORDS = {
    "var", "let", "const", "function", "return", "if", "else", "while",
    "for", "break", "continue", "true", "false", "null", "undefined",
    "typeof", "throw", "try", "catch", "finally", "new", "this", "await",
}

# Longest match first, so the tokenizer greedily groups '===', '!=', etc.
_PUNCT = (
    (3, "..."), (3, "==="), (3, "!=="),
    (2, "=="), (2, "!="), (2, "<="), (2, ">="), (2, "&&"), (2, "||"),
    (2, "+="), (2, "-="), (2, "*="), (2, "/="), (2, "++"), (2, "--"),
    (1, "{"), (1, "}"), (1, "("), (1, ")"), (1, "["), (1, "]"),
    (1, ";"), (1, ","), (1, "."), (1, ":"), (1, "?"), (1, "="), (1, "!"),
    (1, "+"), (1, "-"), (1, "*"), (1, "/"), (1, "%"), (1, "<"), (1, ">"),
)

#: Simple backslash escapes in string literals; "\n" is a line continuation.
_SIMPLE_ESC = {"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"',
               "\n": ""}

#: Defensive cap so pathological inputs cannot exhaust memory in the lexer.
_MAX_TOKENS = 200_000


class _Tokenizer:
    """Tokenize a source string into (kind, value, offset) triples."""

    def __init__(self, source):
        self.source = source

    def tokenize(self):
        tokens = []
        s, i, n = self.source, 0, len(self.source)
        while i < n:
            ch = s[i]
            if ch in " \t\r\n":
                i += 1
            elif s.startswith("//", i):
                nl = s.find("\n", i)
                i = n if nl == -1 else nl + 1
            elif s.startswith("/*", i):
                end = s.find("*/", i + 2)
                if end == -1:
                    self._fail(i, "unterminated block comment")
                i = end + 2
            elif ch in "0123456789" or (
                    ch == "." and i + 1 < n and s[i + 1] in "0123456789"):
                j = i
                while j < n and s[j] in "0123456789":
                    j += 1
                if j < n and s[j] == ".":
                    j += 1
                    while j < n and s[j] in "0123456789":
                        j += 1
                tokens.append(("number", _parse_number(s[i:j]), i))
                i = j
            elif ch in ('"', "'"):
                quote = ch
                i += 1
                buf = []
                while True:
                    if i >= n:
                        self._fail(i, "unterminated string literal")
                    c = s[i]
                    if c == "\\":
                        i += 1
                        if i >= n:
                            self._fail(i, "unterminated string literal")
                        esc = s[i]
                        i += 1
                        if esc in _SIMPLE_ESC:
                            buf.append(_SIMPLE_ESC[esc])
                        elif esc in "xu":
                            size = 4 if esc == "u" else 2
                            try:
                                buf.append(chr(int(s[i:i + size], 16)))
                                i += size
                            except ValueError:
                                pass
                        else:
                            buf.append(esc)
                    elif c == quote:
                        i += 1
                        tokens.append(("string", "".join(buf), i))
                        break
                    elif c == "\n":
                        self._fail(i, "unterminated string literal")
                    else:
                        buf.append(c)
                        i += 1
            elif ch.isalpha() or ch in "_$":
                j = i
                while j < n and (s[j].isalnum() or s[j] in "_$"):
                    j += 1
                word = s[i:j]
                kind = "kw" if word in _KEYWORDS else "ident"
                tokens.append((kind, word, i))
                i = j
            elif ch in "{}()[];,.;:?!<>=+-*/%&|^~@#":
                matched = False
                for length, text in _PUNCT:
                    if length <= n - i and s[i:i + length] == text:
                        tokens.append(("punct", text, i))
                        i += length
                        matched = True
                        break
                if not matched:
                    tokens.append(("punct", ch, i))
                    i += 1
            else:
                self._fail(i, f"unexpected character {ch!r}")
            if len(tokens) > _MAX_TOKENS:
                raise JSException("Too many tokens")
        return tokens

    def _fail(self, offset, msg):
        line = self.source.count("\n", 0, offset) + 1
        raise JSException(f"SyntaxError on line {line}: {msg}")


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


class _Parser:
    _STMT = None  # set below the class

    def __init__(self, source):
        self.source = source
        self.tokens = _Tokenizer(source).tokenize()
        self.pos = 0
        self.async_depth = 0

    # -- token helpers ------------------------------------------------------

    def _peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return (None, None, len(self.source))

    def _peek2(self):
        if self.pos + 1 < len(self.tokens):
            return self.tokens[self.pos + 1]
        return (None, None, len(self.source))

    def _match_punct(self, text):
        kind, value, _ = self._peek()
        if kind == "punct" and value == text:
            self.pos += 1
            return text
        return None

    def _match_kw(self, text):
        kind, value, _ = self._peek()
        if kind == "kw" and value == text:
            self.pos += 1
            return text
        return None

    def _expect_punct(self, text):
        if self._match_punct(text) is None:
            self._syntax(f"expected '{text}'")

    def _match_ident(self):
        kind, value, _ = self._peek()
        if kind == "ident":
            self.pos += 1
            return value
        return None

    def _expect_ident(self):
        name = self._match_ident()
        if name is None:
            self._syntax("expected identifier")
        return name

    def _expect_property_name(self):
        """Property names may be keywords too (promise.catch, obj.default)."""
        kind, value, _ = self._peek()
        if kind in ("ident", "kw"):
            self.pos += 1
            return value
        self._syntax("expected property name")

    def _next_is_kw(self, text):
        kind, value, _ = self._peek2()
        return kind == "kw" and value == text

    def _syntax(self, msg):
        self._fail(self._peek()[2], msg)

    def _fail(self, offset, msg):
        line = self.source.count("\n", 0, offset) + 1
        raise JSException(f"SyntaxError on line {line}: {msg}")

    # -- grammar ------------------------------------------------------------

    def parse_program(self):
        return Program(self._parse_stmts_until(None))

    def _statement(self):
        kind, value, _ = self._peek()
        if kind == "punct" and value == "{":
            return Block(self._parse_stmts_until("}"))
        if kind == "kw" and value in self._STMT:
            self.pos += 1
            return self._STMT[value](self)
        if kind == "ident" and value == "async" \
                and self._next_is_kw("function"):
            self.pos += 1
            self.pos += 1  # consume 'function'
            name = self._expect_ident()
            params, body = self._function_rest(True)
            return FunctionDecl(name, params, body, True)
        return ExprStmt(self._expression())

    def _parse_stmts_until(self, closing):
        if closing is not None:
            self._expect_punct("{")
        stmts = []
        while True:
            kind, value, _ = self._peek()
            if kind is None:
                if closing is not None:
                    self._syntax(f"expected '{closing}'")
                break
            if kind == "punct" and value == closing:
                self.pos += 1
                break
            if self._match_punct(";"):
                continue
            stmts.append(self._statement())
            self._match_punct(";")
        return stmts

    def _block(self):
        return Block(self._parse_stmts_until("}"))

    def _declaration_list(self):
        decls = []
        while True:
            name = self._expect_ident()
            value = None
            if self._match_punct("="):
                value = self._expression()
            decls.append((name, value))
            if self._match_punct(",") is None:
                break
        return decls

    def _function_declaration(self, async_):
        name = self._expect_ident()
        params, body = self._function_rest(async_)
        return FunctionDecl(name, params, body, async_)

    def _function_rest(self, async_):
        params = self._list("(", ")", self._expect_ident)
        if async_:
            self.async_depth += 1
        try:
            body = self._parse_stmts_until("}")
        finally:
            if async_:
                self.async_depth -= 1
        return params, body

    def _list(self, opener, closer, item, trailing=False):
        """Parse a comma-separated list; `trailing` allows a trailing comma
        (arrays/objects) instead of rejecting it (args/params)."""
        if opener is not None:
            self._expect_punct(opener)
        out = []
        if trailing or self._match_punct(closer) is None:
            while True:
                if trailing and self._match_punct(closer):
                    break
                out.append(item())
                if self._match_punct(closer):
                    break
                self._expect_punct(",")
        return out

    def _return_statement(self):
        kind, value, _ = self._peek()
        if kind is not None and not (kind == "punct" and value in (";", "}")):
            return Return(self._expression())
        return Return(None)

    def _if_statement(self):
        cond, then = self._cond_body()
        else_stmt = self._statement() if self._match_kw("else") else None
        return If(cond, then, else_stmt)

    def _while_statement(self):
        cond, body = self._cond_body()
        return While(cond, body)

    def _cond_body(self):
        """Parse `(expression)` then a statement body."""
        self._expect_punct("(")
        cond = self._expression()
        self._expect_punct(")")
        return cond, self._statement()

    def _for_statement(self):
        self._expect_punct("(")
        init = None
        kind, value, _ = self._peek()
        if not (kind == "punct" and value == ";"):
            if kind == "kw" and value in ("var", "let", "const"):
                self.pos += 1
                init = VarDecl(value, self._declaration_list())
            else:
                init = ExprStmt(self._expression())
        self._expect_punct(";")
        cond = None
        kind, value, _ = self._peek()
        if not (kind == "punct" and value == ";"):
            cond = self._expression()
        self._expect_punct(";")
        update = None
        kind, value, _ = self._peek()
        if not (kind == "punct" and value == ")"):
            update = self._expression()
        self._expect_punct(")")
        body = self._statement()
        return For(init, cond, update, body)

    def _throw_statement(self):
        return Throw(self._expression())

    def _try_statement(self):
        try_block = self._block()
        catch_param = None
        catch_block = None
        if self._match_kw("catch"):
            self._expect_punct("(")
            catch_param = self._expect_ident()
            self._expect_punct(")")
            catch_block = self._block()
        finally_block = self._block() if self._match_kw("finally") else None
        return TryCatch(try_block, catch_param, catch_block, finally_block)

    # -- expressions --------------------------------------------------------

    def _expression(self):
        return self._assign()

    def _assign(self):
        left = self._conditional()
        kind, value, _ = self._peek()
        if kind == "punct" and value in ("=", "+=", "-=", "*=", "/="):
            self.pos += 1
            right = self._assign()
            if not isinstance(left, (Identifier, Member, Index)):
                self._syntax("invalid assignment target")
            return Assign(value, left, right)
        return left

    def _conditional(self):
        cond = self._or()
        if self._match_punct("?"):
            then_expr = self._assign()
            self._expect_punct(":")
            else_expr = self._assign()
            return Conditional(cond, then_expr, else_expr)
        return cond

    def _or(self):
        return self._logical_chain("||", self._and)

    def _and(self):
        return self._logical_chain("&&", self._equality)

    def _logical_chain(self, op, sub):
        node = sub()
        while self._match_punct(op):
            node = Logical(op, node, sub())
        return node

    def _equality(self):
        return self._binop(self._relational, ("==", "!=", "===", "!=="))

    def _relational(self):
        return self._binop(self._additive, ("<", "<=", ">", ">="))

    def _additive(self):
        return self._binop(self._multiplicative, ("+", "-"))

    def _multiplicative(self):
        return self._binop(self._unary, ("*", "/", "%"))

    def _binop(self, sub, ops):
        node = sub()
        while True:
            value = self._punct_in(ops)
            if value is None:
                break
            node = Binary(value, node, sub())
        return node

    def _punct_in(self, texts):
        kind, value, _ = self._peek()
        if kind == "punct" and value in texts:
            self.pos += 1
            return value
        return None

    def _unary(self):
        kind, value, _ = self._peek()
        if kind == "punct" and value in ("!", "-", "++", "--"):
            self.pos += 1
            if value in ("++", "--"):
                return Update(value, self._unary(), True)
            return Unary(value, self._unary())
        if kind == "kw" and value == "typeof":
            self.pos += 1
            return Unary("typeof", self._unary())
        if kind == "kw" and value == "await":
            if self.async_depth == 0:
                self._syntax("await is only valid in async functions")
            self.pos += 1
            return Await(self._unary())
        return self._call()

    def _call(self):
        node = self._primary()
        while True:
            if self._match_punct("("):
                node = Call(node, self._args())
            elif self._match_punct("."):
                node = Member(node, self._expect_property_name())
            elif self._match_punct("["):
                index = self._expression()
                self._expect_punct("]")
                node = Index(node, index)
            elif self._match_punct("++"):
                node = Update("++", node, False)
            elif self._match_punct("--"):
                node = Update("--", node, False)
            else:
                break
        return node

    def _args(self):
        return self._list(None, ")", self._expression)

    def _new_expression(self):
        callee = self._primary()
        args = self._args() if self._match_punct("(") else []
        return New(callee, args)

    def _primary(self):
        kind, value, _ = self._peek()
        if kind in ("number", "string"):
            self.pos += 1
            return Literal(value)
        if kind == "kw":
            self.pos += 1
            if value in ("true", "false", "null", "undefined"):
                return Literal({"true": True, "false": False,
                                "null": None, "undefined": UNDEFINED}[value])
            if value == "function":
                return self._function_expression(False)
            if value == "this":
                return This()
            if value == "new":
                return self._new_expression()
            self._syntax(f"unexpected keyword '{value}'")
        if kind == "ident":
            if value == "async" and self._next_is_kw("function"):
                self.pos += 1
                self.pos += 1  # consume 'function'
                return self._function_expression(True)
            self.pos += 1
            return Identifier(value)
        if kind == "punct":
            if value == "(":
                self.pos += 1
                node = self._expression()
                self._expect_punct(")")
                return node
            if value == "[":
                return ArrayLit(self._list("[", "]", self._expression,
                                           trailing=True))
            if value == "{":
                return ObjectLit(self._list("{", "}", self._object_pair,
                                            trailing=True))
        self._syntax("unexpected token")

    def _function_expression(self, async_):
        name = None
        kind, value, _ = self._peek()
        if kind == "ident":
            self.pos += 1
            name = value
        params, body = self._function_rest(async_)
        return FunctionExpr(name, params, body, async_)

    def _object_pair(self):
        kind, value, _ = self._peek()
        if kind in ("ident", "string", "kw"):
            self.pos += 1
            key = value
        else:
            self._syntax("expected property name")
        self._expect_punct(":")
        return key, self._expression()


_Parser._STMT = {
    "var": lambda s: VarDecl("var", s._declaration_list()),
    "let": lambda s: VarDecl("let", s._declaration_list()),
    "const": lambda s: VarDecl("const", s._declaration_list()),
    "function": lambda s: s._function_declaration(False),
    "return": lambda s: s._return_statement(),
    "if": lambda s: s._if_statement(),
    "while": lambda s: s._while_statement(),
    "for": lambda s: s._for_statement(),
    "break": lambda s: Break(),
    "continue": lambda s: Continue(),
    "throw": lambda s: s._throw_statement(),
    "try": lambda s: s._try_statement(),
}


# --------------------------------------------------------------------------
# Interpreter
# --------------------------------------------------------------------------


@dataclass
class _Timer:
    id: int
    due: float
    fn: object
    args: list
    interval: float = 0
    repeat: bool = False


class Interpreter:
    """Parses and executes JavaScript against a shared global scope.

    `run(source)` executes a whole program; `call(fn, *args)` invokes a
    function. `drain()` runs pending microtasks and due timers, and
    `advance(ms)` moves the virtual clock forward (used by the host's poll).
    """

    def __init__(self):
        def _js_log(*args):
            self.logs.append(" ".join(self.repr(a) for a in args))

        def _js_string(*args):
            return self.repr(args[0]) if args else ""

        def _js_number(*args):
            return _to_number(args[0]) if args else 0.0

        def _js_boolean(*args):
            return self._truthy(args[0]) if args else False

        def _js_parse_int(text, radix=None):
            text = str(text).lstrip()
            hexp = text.lower().startswith("0x")
            base = (16 if radix is None and hexp
                    else int(radix) if radix is not None else 10)
            if base == 0:
                base = 16 if hexp else \
                    8 if text.startswith("0") and len(text) > 1 else 10
            prefix_len = 2 if base == 16 and hexp else 0
            digits = 0
            for ch in text[prefix_len:]:
                if ch.lower() in "0123456789abcdefghijklmnopqrstuvwxyz"[:base]:
                    digits += 1
                else:
                    break
            return float("nan") if digits == 0 \
                else int(text[:prefix_len + digits], base)

        def _js_parse_float(text):
            match = re.match(
                r"^[+-]?(?:\d+\.?\d*|\.\d+|[iI][nN][fF]i?n?i?t?y?)",
                str(text).strip())
            if not match:
                return float("nan")
            tok = match.group(0)
            if tok.lower() == "infinity":
                return float("inf")
            try:
                return float(tok)
            except ValueError:
                return float("nan")

        self.logs = []
        self.globals = {
            "console": {"log": _js_log},
            "String": _js_string,
            "Number": _js_number,
            "Boolean": _js_boolean,
            "parseInt": _js_parse_int,
            "parseFloat": _js_parse_float,
            "NaN": float("nan"),
            "Infinity": float("inf"),
            "Promise": JSPromiseCtor(self),
            "Error": _ErrorCtor(),
            "setTimeout": self._native_set_timeout,
            "setInterval": self._native_set_interval,
            "clearTimeout": self._native_clear_timer,
            "clearInterval": self._native_clear_timer,
            "queueMicrotask": self._native_queue_microtask,
            "document": UNDEFINED,
            "window": UNDEFINED,
        }
        self._global_env = Environment()
        self._global_env.vars = self.globals
        self._microtasks = deque()
        self._timers = []
        self._timer_seq = 0
        self._now = 0.0

    # -- public API ------------------------------------------------------

    def run(self, source):
        """Parse and execute a whole program statement-by-statement."""
        program = _Parser(source).parse_program()
        try:
            self._pump_sync(self._exec_block(program.statements,
                                             self._global_env))
        except (_Return, _Break, _Continue):
            raise JSException("Illegal statement outside its context.") from None
        except _JSThrow as t:
            raise JSException(self.repr(t.value)) from None
        except JSException:
            raise
        except Exception as exc:
            raise JSException(str(exc)) from None

    def call(self, fn, *args):
        """Call a JSFunction, a plain Python callable, or a host object."""
        try:
            return self._call_value(fn, list(args))
        except _JSThrow as t:
            raise JSException(self.repr(t.value)) from None
        except Exception as exc:
            raise (exc if isinstance(exc, JSException)
                   else JSException(str(exc))) from None

    def create_promise(self):
        return JSPromise(self)

    def repr(self, value):
        """JS-style string of a value."""
        if value is UNDEFINED:
            return "undefined"
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)):
            if value == float("inf"):
                return "Infinity"
            if value == float("-inf"):
                return "-Infinity"
            if value != value:
                return "NaN"
            return str(int(value)) if float(value).is_integer() else str(value)
        if isinstance(value, list):
            return ",".join(self.repr(item) for item in value)
        if isinstance(value, JSFunction):
            return f"function {value.name}"
        js_repr = getattr(value, "js_repr", None)
        if callable(js_repr):
            return js_repr()
        if _is_objectish(value):
            return "[object Object]"
        return str(value)

    # -- host-object member access ---------------------------------------

    def js_get(self, obj, name):
        """Read member `name` off `obj` using the shared value model."""
        if isinstance(obj, dict):
            return obj.get(str(name), UNDEFINED)
        if isinstance(obj, list):
            return self._list_get(obj, name)
        if isinstance(obj, str):
            return self._string_get(obj, name)
        if isinstance(obj, JSFunction):
            if name == "length":
                return len(obj.params)
            if name == "name":
                return obj.name
            return UNDEFINED
        return self._member_tail(obj, name)

    def js_set(self, obj, name, value):
        """Write member `name` on `obj` using the shared value model."""
        if isinstance(obj, dict):
            obj[str(name)] = value
            return
        if isinstance(obj, list):
            if name == "length":
                obj[:] = obj[:max(0, int(value))]
                return
            index = _int_index(name)
            if index is not None and index >= 0:
                # arr[5] = x grows the array with holes filled by undefined.
                obj.extend([UNDEFINED] * (index + 1 - len(obj)))
                obj[index] = value
                return
            return
        self._member_tail(obj, name, write=True, value=value)

    def _member_tail(self, obj, name, write=False, value=None):
        if _nullish(obj) or isinstance(obj, (str, int, float, bool, JSFunction)):
            return UNDEFINED if not write else None
        method = getattr(obj, "js_set" if write else "js_get", None)
        if method is not None:
            try:
                result = method(str(name)) if not write \
                    else method(str(name), value)
            except Exception as exc:
                raise (exc if isinstance(exc, (JSException, _JSThrow))
                       else JSException(str(exc))) from None
            return None if write else self._to_js(result)
        if write:
            return None
        if hasattr(obj, "__getitem__"):
            try:
                return self._to_js(obj[str(name)])
            except Exception:
                return UNDEFINED
        return UNDEFINED

    # -- native array/string members -------------------------------------

    def _list_get(self, arr, name):
        if name == "length":
            return len(arr)
        if name == "push":
            def push(*values):
                arr.extend(values)
                return len(arr)
            return push
        if name == "pop":
            def pop():
                if not arr:
                    return UNDEFINED
                return arr.pop()
            return pop
        if name == "join":
            def join(sep=","):
                return (sep if isinstance(sep, str) else ",").join(
                    self.repr(item) for item in arr)
            return join
        index = _int_index(name)
        if index is not None and -len(arr) <= index < len(arr):
            return arr[index]
        return UNDEFINED

    def _string_get(self, text, name):
        if name == "length":
            return len(text)
        index = _int_index(name)
        if index is not None and -len(text) <= index < len(text):
            return text[index]
        return UNDEFINED

    # -- timers / microtasks ---------------------------------------------

    def advance(self, ms):
        """Move the virtual clock forward; due timers fire on the next drain."""
        self._now += float(ms)

    def enqueue(self, job):
        self._microtasks.append(job)

    def drain(self):
        """Run pending microtasks and due timers until quiescent."""
        while True:
            while self._microtasks:
                job = self._microtasks.popleft()
                try:
                    job()
                except (_JSThrow, JSException) as e:
                    self.logs.append(self._error_text(e))
            due = [t for t in self._timers if t.due <= self._now]
            if not due:
                break
            for t in due:
                self._timers.remove(t)
                try:
                    self._call_value(t.fn, t.args)
                except (_JSThrow, JSException) as e:
                    self.logs.append(self._error_text(e))
                if t.repeat:
                    t.due += t.interval
                    self._timers.append(t)

    def _error_text(self, e):
        if isinstance(e, _JSThrow):
            return "JS error: " + self.repr(e.value)
        return "JS error: " + str(e)

    def _note_unhandled_rejection(self, reason):
        self.logs.append("Unhandled promise rejection: " + self.repr(reason))

    def _native_set_timeout(self, fn, ms=0):
        return self._schedule_timer(fn, _to_number(ms), repeat=False)

    def _native_set_interval(self, fn, ms=0):
        return self._schedule_timer(fn, _to_number(ms), repeat=True)

    def _schedule_timer(self, fn, ms, repeat):
        self._timer_seq += 1
        timer_id = self._timer_seq
        self._timers.append(_Timer(timer_id, self._now + max(0, ms), fn, [],
                                   interval=max(0, ms), repeat=repeat))
        return timer_id

    def _native_clear_timer(self, timer_id):
        for i, t in enumerate(self._timers):
            if t.id == timer_id:
                del self._timers[i]
                return
        return UNDEFINED

    def _native_queue_microtask(self, fn):
        self.enqueue(lambda: self._call_value(fn, []))

    # -- evaluation ---------------------------------------------------------

    def _to_js(self, value):
        return value

    def _truthy(self, value):
        if value is False or value is UNDEFINED or value is None:
            return False
        if isinstance(value, (int, float)):
            return value != 0 and value == value  # NaN is falsy
        return value != ""

    def _index_name(self, value):
        return value if isinstance(value, str) else self.repr(value)

    def _pump_sync(self, gen):
        """Drive a generator to completion. Suspension means an `await`
        leaked into synchronous code, which is a parser-level error."""
        value = None
        while True:
            try:
                value = gen.send(value)
            except StopIteration as stop:
                return stop.value
            if isinstance(value, _Suspend):
                raise JSException("await is only valid in async functions")

    def _call_value(self, fn, args, this_arg=UNDEFINED):
        if isinstance(fn, JSFunction):
            if fn.async_:
                return self._start_async_call(fn, args, this_arg)
            return self._pump_sync(self._call_function(fn, args, this_arg))
        if fn is UNDEFINED or fn is None:
            raise JSException(f"{self.repr(fn)} is not a function.")
        try:
            if hasattr(fn, "js_call"):
                return self._to_js(fn.js_call(*args))
            if callable(fn):
                return self._to_js(fn(*args))
        except Exception as exc:
            raise (exc if isinstance(exc, (JSException, _JSThrow))
                   else JSException(str(exc))) from None
        raise JSException(f"{self.repr(fn)} is not a function.")

    def _construct(self, callee, args):
        if hasattr(callee, "js_new"):
            return self._to_js(callee.js_new(*args))
        if isinstance(callee, JSFunction):
            obj = {}
            result = self._pump_sync(self._call_function(callee, args, obj))
            if _is_objectish(result):
                return result
            return obj
        raise JSException(f"{self.repr(callee)} is not a constructor")

    def _call_function(self, fn, args, this_arg=UNDEFINED):
        scope = Environment(fn.env)
        scope.function_scope = scope  # private var scope per invocation
        for name, value in zip(fn.params, args):
            scope.set_var(name, value)
        if this_arg is not UNDEFINED:
            scope.vars["this"] = this_arg
        try:
            yield from self._exec_block(fn.body, scope)
        except _Return as ret:
            return ret.value
        except (_Break, _Continue):
            raise JSException("Break or continue outside of a loop.") from None
        return UNDEFINED

    def _start_async_call(self, fn, args, this_arg=UNDEFINED):
        promise = JSPromise(self)
        scope = Environment(fn.env)
        scope.function_scope = scope
        for name, value in zip(fn.params, args):
            scope.set_var(name, value)
        if this_arg is not UNDEFINED:
            scope.vars["this"] = this_arg
        gen = self._exec_block(fn.body, scope)
        self._resume_async(gen, promise, None, False)
        return promise

    def _resume_async(self, gen, promise, send_value, is_throw):
        """Advance an async coroutine until it completes or suspends."""
        try:
            if is_throw:
                value = gen.throw(_JSThrow(send_value))
            else:
                value = gen.send(send_value)
        except StopIteration as stop:
            promise.resolve(stop.value)
            return
        except _Return as ret:
            promise.resolve(ret.value)
            return
        except (_Break, _Continue):
            promise.reject("Break or continue outside of a loop.")
            return
        except _JSThrow as t:
            promise.reject(t.value)
            return
        except JSException as e:
            promise.reject(str(e))
            return
        if isinstance(value, _Suspend):
            p = value.promise

            def cont(settled_value, rejected):
                self._resume_async(gen, promise, settled_value, rejected)

            p._on_settle(cont)
            return
        promise.resolve(value)

    def _as_promise(self, value):
        if isinstance(value, JSPromise):
            return value
        then = self._thenable_method(value)
        if then is not None:
            p = JSPromise(self)
            p._assimilate(value, then)
            return p
        p = JSPromise(self)
        p.resolve(value)
        return p

    def _thenable_method(self, value):
        if not _is_objectish(value):
            return None
        try:
            then = self.js_get(value, "then")
        except Exception:
            return None
        if isinstance(then, JSFunction) or callable(then):
            return then
        return None

    def _eval(self, node, env):
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, Identifier):
            name = node.name
            value = env.get(name)
            if value is UNDEFINED and name in self.globals:
                return self.globals[name]
            return value
        if isinstance(node, This):
            return env.get("this")
        if isinstance(node, ArrayLit):
            out = []
            for item in node.items:
                out.append((yield from self._eval(item, env)))
            return out
        if isinstance(node, ObjectLit):
            out = {}
            for key, expr in node.pairs:
                out[key] = yield from self._eval(expr, env)
            return out
        if isinstance(node, FunctionExpr):
            return JSFunction(node.params, node.body, env, self,
                              node.name or "", node.async_)
        if isinstance(node, Unary):
            return (yield from self._eval_unary(node, env))
        if isinstance(node, Update):
            return (yield from self._eval_update(node, env))
        if isinstance(node, Binary):
            left = yield from self._eval(node.left, env)
            right = yield from self._eval(node.right, env)
            return self._eval_binary(node.op, left, right)
        if isinstance(node, Logical):
            left = yield from self._eval(node.left, env)
            if self._truthy(left) == (node.op == "or"):
                return left
            return (yield from self._eval(node.right, env))
        if isinstance(node, Conditional):
            if self._truthy((yield from self._eval(node.cond, env))):
                return (yield from self._eval(node.then_expr, env))
            return (yield from self._eval(node.else_expr, env))
        if isinstance(node, Assign):
            return (yield from self._eval_assign(node, env))
        if isinstance(node, Call):
            return (yield from self._eval_call(node, env))
        if isinstance(node, New):
            callee = yield from self._eval(node.callee, env)
            args = []
            for a in node.args:
                args.append((yield from self._eval(a, env)))
            return self._construct(callee, args)
        if isinstance(node, Member):
            obj = yield from self._eval(node.obj, env)
            return self.js_get(obj, node.name)
        if isinstance(node, Index):
            obj = yield from self._eval(node.obj, env)
            name = self._index_name((yield from self._eval(node.index, env)))
            return self.js_get(obj, name)
        if isinstance(node, Await):
            value = yield from self._eval(node.expr, env)
            promise = self._as_promise(value)
            if promise.rejected:
                raise _JSThrow(promise.value)
            if promise.pending:
                return (yield _Suspend(promise))
            return promise.value
        raise JSException(f"Unknown expression {type(node).__name__}.")

    def _eval_unary(self, node, env):
        operand = yield from self._eval(node.operand, env)
        if node.op == "!":
            return not self._truthy(operand)
        if node.op == "-":
            return -_to_number(operand)
        if node.op == "typeof":
            return _typeof(operand)
        raise JSException(f"Unknown unary operator '{node.op}'.")

    def _eval_update(self, node, env):
        current = yield from self._read_lvalue(node.operand, env)
        value = _to_number(current) + (1 if node.op == "++" else -1)
        yield from self._write_lvalue(node.operand, env, value)
        return value if node.prefix else current

    def _eval_binary(self, op, left, right):
        if op in ("+", "-", "*", "/", "%"):
            return self._binary_op(op, left, right)
        return self._compare(op, left, right)

    def _compare(self, op, left, right):
        if op == "==":
            result = _loose_eq(left, right)
        elif op == "!=":
            result = not _loose_eq(left, right)
        elif op == "===":
            result = _strict_eq(left, right)
        elif op == "!==":
            result = not _strict_eq(left, right)
        elif op == "<":
            return self._ordered(left, right)
        elif op == "<=":
            return not self._ordered(right, left)
        elif op == ">":
            return self._ordered(right, left)
        elif op == ">=":
            return not self._ordered(left, right)
        else:
            raise JSException(f"Unknown operator '{op}'.")
        return result

    def _ordered(self, left, right):
        if isinstance(left, str) and isinstance(right, str):
            return left < right
        return _to_number(left) < _to_number(right)

    def _binary_op(self, op, left, right):
        if op == "+":
            # Arrays participate in string concatenation via their join
            # representation (so [] + [] === "", [] + 5 === "5"), matching JS
            # ToPrimitive on arrays.
            if isinstance(left, (str, list)) or isinstance(right, (str, list)):
                return self.repr(left) + self.repr(right)
            return _to_number(left) + _to_number(right)
        left, right = _to_number(left), _to_number(right)
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            return _divide(left, right)
        if op == "%":
            return _modulo(left, right)
        raise JSException(f"Unknown binary operator '{op}'.")

    def _eval_assign(self, node, env):
        value = yield from self._eval(node.value, env)
        obj, name = yield from self._lvalue(node.target, env)
        if node.op == "=":
            if obj is None:
                env.assign(name, value)
            else:
                self.js_set(obj, name, value)
            return value
        current = env.get(name) if obj is None else self.js_get(obj, name)
        result = self._binary_op(node.op[0], current, value)
        if obj is None:
            env.assign(name, result)
        else:
            self.js_set(obj, name, result)
        return result

    def _eval_call(self, node, env):
        callee_node = node.callee
        if isinstance(callee_node, (Member, Index)):
            obj = yield from self._eval(callee_node.obj, env)
            if isinstance(callee_node, Member):
                name = callee_node.name
            else:
                name = self._index_name(
                    (yield from self._eval(callee_node.index, env)))
            fn = self.js_get(obj, name)
            args = []
            for a in node.args:
                args.append((yield from self._eval(a, env)))
            return self._call_value(fn, args, this_arg=obj)
        fn = yield from self._eval(callee_node, env)
        args = []
        for a in node.args:
            args.append((yield from self._eval(a, env)))
        return self._call_value(fn, args)

    def _lvalue(self, target, env):
        if isinstance(target, Identifier):
            return None, target.name
        if isinstance(target, Member):
            obj = yield from self._eval(target.obj, env)
            return obj, target.name
        if isinstance(target, Index):
            obj = yield from self._eval(target.obj, env)
            name = self._index_name((yield from self._eval(target.index, env)))
            return obj, name
        raise JSException("Invalid assignment target")

    def _read_lvalue(self, target, env):
        obj, name = yield from self._lvalue(target, env)
        return env.get(name) if obj is None else self.js_get(obj, name)

    def _write_lvalue(self, target, env, value):
        obj, name = yield from self._lvalue(target, env)
        if obj is None:
            env.assign(name, value)
        else:
            self.js_set(obj, name, value)

    # -- statements ---------------------------------------------------------

    def _exec_block(self, statements, env):
        for stmt in statements:
            if isinstance(stmt, FunctionDecl):
                env.set_var(stmt.name, JSFunction(
                    stmt.params, stmt.body, env, self, stmt.name, stmt.async_))
        for stmt in statements:
            yield from self._exec(stmt, env)

    def _exec(self, node, env):
        if isinstance(node, Block):
            yield from self._exec_block(node.statements, Environment(env))
        elif isinstance(node, VarDecl):
            setter = {"var": env.set_var, "let": env.set_let,
                      "const": env.set_const}[node.kind]
            for name, expr in node.decls:
                if node.kind == "const" and expr is None:
                    raise JSException(
                        f"Missing initializer in const declaration '{name}'.")
                value = UNDEFINED if expr is None \
                    else (yield from self._eval(expr, env))
                setter(name, value)
        elif isinstance(node, FunctionDecl):
            pass  # hoisted by _exec_block
        elif isinstance(node, ExprStmt):
            yield from self._eval(node.expr, env)
        elif isinstance(node, If):
            if self._truthy((yield from self._eval(node.cond, env))):
                yield from self._exec(node.then, env)
            elif node.else_ is not None:
                yield from self._exec(node.else_, env)
        elif isinstance(node, While):
            while self._truthy((yield from self._eval(node.cond, env))):
                try:
                    yield from self._exec(node.body, env)
                except _Break:
                    break
                except _Continue:
                    continue
        elif isinstance(node, For):
            yield from self._exec_for(node, env)
        elif isinstance(node, Return):
            value = UNDEFINED if node.value is None \
                else (yield from self._eval(node.value, env))
            raise _Return(value)
        elif isinstance(node, Break):
            raise _Break()
        elif isinstance(node, Continue):
            raise _Continue()
        elif isinstance(node, Throw):
            raise _JSThrow((yield from self._eval(node.expr, env)))
        elif isinstance(node, TryCatch):
            yield from self._exec_try(node, env)
        else:
            raise JSException(f"Unknown statement {type(node).__name__}.")

    def _exec_for(self, node, env):
        child = Environment(env)
        if node.init is not None:
            yield from self._exec(node.init, child)
        while node.cond is None or \
                self._truthy((yield from self._eval(node.cond, child))):
            try:
                yield from self._exec(node.body, child)
            except _Break:
                break
            except _Continue:
                pass
            if node.update is not None:
                yield from self._eval(node.update, child)

    def _exec_try(self, node, env):
        error = None  # ("throw", value) or ("error", message)
        try:
            yield from self._exec(node.try_block, Environment(env))
        except _JSThrow as t:
            error = ("throw", t.value)
        except JSException as e:
            error = ("error", str(e))
        except (_Return, _Break, _Continue):
            if node.finally_block is not None:
                yield from self._exec(node.finally_block, env)
            raise
        if error is not None and node.catch_block is not None:
            child = Environment(env)
            if node.catch_param:
                child.set_let(node.catch_param, error[1])
            yield from self._exec(node.catch_block, child)
        elif error is not None:
            if node.finally_block is not None:
                yield from self._exec(node.finally_block, env)
            if error[0] == "throw":
                raise _JSThrow(error[1])
            raise JSException(error[1])
        if node.finally_block is not None:
            yield from self._exec(node.finally_block, env)