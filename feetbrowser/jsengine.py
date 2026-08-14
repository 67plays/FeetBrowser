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

import datetime
import json
import math
import random
import re
import time
import urllib.parse
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


def _to_int32(x):
    """Coerce a value to a signed 32-bit integer (JS bitwise semantics)."""
    n = int(_to_number(x)) if not _nullish(x) else 0
    return ((n & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000


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
    defaults: list = field(default_factory=list)
    body: list = field(default_factory=list)
    async_: bool = False
    arrow: bool = False


@dataclass
class Await:
    expr: object


@dataclass
class Sequence:
    left: object
    right: object


@dataclass
class TemplateLiteral:
    parts: list = field(default_factory=list)  # [("lit", str) | ("expr", node)]


@dataclass
class RegexLiteral:
    pattern: str = ""
    flags: str = ""


@dataclass
class Spread:
    expr: object


@dataclass
class OptionalMember:
    obj: object
    name: str


@dataclass
class OptionalIndex:
    obj: object
    index: object


@dataclass
class OptionalCall:
    callee: object
    args: list = field(default_factory=list)


@dataclass
class Nullish:
    left: object
    right: object


@dataclass
class InOp:
    left: object
    right: object


@dataclass
class InstanceOf:
    left: object
    right: object


@dataclass
class ClassDecl:
    name: str
    methods: list = field(default_factory=list)  # [(name, params, body), ...]


@dataclass
class ClassExpr:
    name: str
    methods: list = field(default_factory=list)


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
    defaults: list = field(default_factory=list)
    body: list = field(default_factory=list)
    async_: bool = False


@dataclass
class ExprStmt:
    expr: object


@dataclass
class Empty:
    pass


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
class DoWhile:
    body: object
    cond: object


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


@dataclass
class Switch:
    expr: object
    cases: list = field(default_factory=list)  # [("case"|"default", test, stmts)]


@dataclass
class ForOf:
    var_name: object  # str or Pattern
    iterable: object
    body: object
    kind: str = "var"  # var/let/const


@dataclass
class ForIn:
    var_name: object
    obj: object
    body: object
    kind: str = "var"


@dataclass
class Pattern:
    kind: str            # "array" or "object"
    parts: list = field(default_factory=list)
    rest: object = None  # rest target (str or Pattern) or None
    # For "object" parts: (key, target, default); for "array": (target, default)


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

    __slots__ = ("params", "defaults", "body", "env", "interp", "name",
                 "async_", "arrow", "prototype", "statics")

    def __init__(self, params, body, env, interp, name="", async_=False,
                 arrow=False, defaults=None):
        self.params = params
        self.defaults = defaults or [None] * len(params)
        self.body = body
        self.env = env
        self.interp = interp
        self.name = name
        self.async_ = async_
        self.arrow = arrow
        self.prototype = None
        self.statics = None

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

    def __init__(self, message="", name="Error"):
        self.message = str(message if message is not UNDEFINED else "")
        self.name = name

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

    def __init__(self, name="Error"):
        self._name = name

    def js_new(self, message=""):
        return JSError(message, self._name)

    def js_call(self, *args):
        return JSError(args[0] if args else "", self._name)


def _js_repl_to_py(repl):
    """Turn a JS replacement string (`$1`, `$&`) into Python re syntax."""
    out = []
    i = 0
    while i < len(repl):
        ch = repl[i]
        if ch == "$" and i + 1 < len(repl):
            nxt = repl[i + 1]
            if nxt == "&":
                out.append(r"\g<0>")
                i += 2
                continue
            if nxt.isdigit():
                out.append(r"\g<" + nxt + ">")
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


class JSRegExp:
    """A RegExp value backed by Python's `re`. Used by /.../ literals, the
    RegExp constructor, and String methods (replace/split/match/search)."""

    def __init__(self, pattern, flags=""):
        self.pattern = pattern
        self.flags = flags
        self.global_ = "g" in flags
        self.ignore_case = "i" in flags
        self.multiline = "m" in flags
        self.dotall = "s" in flags
        self.unicode_ = "u" in flags
        pyflags = 0
        if self.ignore_case:
            pyflags |= re.I
        if self.multiline:
            pyflags |= re.M
        if self.dotall:
            pyflags |= re.S
        self._flags = pyflags
        self._re = re.compile(pattern, pyflags)

    def js_get(self, name):
        if name == "source":
            return self.pattern
        if name == "flags":
            return self.flags
        if name == "global":
            return self.global_
        if name == "ignoreCase":
            return self.ignore_case
        if name == "multiline":
            return self.multiline
        if name == "dotAll":
            return self.dotall
        if name == "test":
            return self.test
        if name == "exec":
            return self.exec_
        if name == "lastIndex":
            return 0
        return UNDEFINED

    def js_repr(self):
        return f"/{self.pattern}/{self.flags}"

    def test(self, s):
        return self._re.search(str(s)) is not None

    def exec_(self, s):
        m = self._re.search(str(s))
        if m is None:
            return None
        return [m.group(0)] + list(m.groups())

    def search(self, s):
        return self._re.search(str(s))

    def finditer(self, s):
        return self._re.finditer(str(s))

    def split(self, s):
        return self._re.split(str(s))


class _RegExpCtor:
    """The `RegExp` global: constructor + the RegExp object type."""

    def js_new(self, pattern="", flags=""):
        if isinstance(pattern, JSRegExp):
            pattern = pattern.pattern
        return JSRegExp(str(pattern), str(flags))

    def js_call(self, *args):
        return self.js_new(*(args or ["", ""]))


class _ArrayCtor:
    """The `Array` global: `Array.isArray` plus a constructor."""

    def __init__(self, interp):
        self._interp = interp

    def js_get(self, name):
        if name == "isArray":
            return lambda v: isinstance(v, list)
        if name == "from":
            return lambda v: list(v) if isinstance(v, list) else []
        if name == "of":
            return lambda *items: list(items)
        if name == "prototype":
            return {}
        return UNDEFINED

    def js_new(self, *args):
        if len(args) == 1 and isinstance(args[0], (int, float)) \
                and float(args[0]).is_integer() and args[0] >= 0:
            return [UNDEFINED] * int(args[0])
        return list(args)

    def js_call(self, *args):
        return self.js_new(*args)


class _ObjectCtor:
    """The `Object` global: statics plus a constructor."""

    def __init__(self, interp):
        self._interp = interp

    def js_get(self, name):
        if name == "prototype":
            return self._prototype()
        m = {
            "keys": lambda o: self._keys(o),
            "values": lambda o: [o[k] for k in self._keys(o)],
            "entries": lambda o: [[k, o[k]] for k in self._keys(o)],
            "assign": lambda t, *sources: self._assign(t, sources),
            "create": lambda proto=None: dict(__proto__=proto) if proto is not None else {},
            "freeze": lambda o: o,
            "seal": lambda o: o,
            "isFrozen": lambda o: False,
            "isSealed": lambda o: False,
            "isExtensible": lambda o: True,
            "isArray": lambda v: isinstance(v, list),
            "getPrototypeOf": lambda o: self._proto_of(o),
            "setPrototypeOf": lambda o, p: self._set_proto(o, p),
            "getOwnPropertyNames": lambda o: self._keys(o),
            "getOwnPropertyDescriptor": lambda o, k: UNDEFINED,
            "defineProperty": lambda o, k, d: o,
            "defineProperties": lambda o, ds: o,
            "hasOwn": lambda o, k: (isinstance(o, dict) and k in o),
        }
        return m.get(name, UNDEFINED)

    def _prototype(self):
        proto = {}
        proto["hasOwnProperty"] = lambda obj, key: (
            isinstance(obj, dict) and key in obj)
        proto["toString"] = lambda obj: self._interp.repr(obj)
        proto["valueOf"] = lambda obj: obj
        proto["isPrototypeOf"] = lambda a, b: False
        proto["propertyIsEnumerable"] = lambda obj, key: True
        proto["__proto__"] = None
        return proto

    def js_new(self, *args):
        return {}

    def js_call(self, *args):
        return {}

    def _keys(self, obj):
        if isinstance(obj, dict):
            return [k for k in obj if k != "__proto__"]
        if isinstance(obj, list):
            return [str(i) for i in range(len(obj))]
        if isinstance(obj, str):
            return [str(i) for i in range(len(obj))]
        return []

    def _assign(self, target, sources):
        if not isinstance(target, dict):
            target = {}
        for src in sources:
            if isinstance(src, dict):
                for k, v in src.items():
                    if k != "__proto__":
                        target[k] = v
        return target

    def _proto_of(self, obj):
        if isinstance(obj, dict):
            return obj.get("__proto__", None)
        if isinstance(obj, list):
            return {"__proto__": None}
        return None

    def _set_proto(self, obj, proto):
        if isinstance(obj, dict):
            obj["__proto__"] = proto
        return obj


class JSDate:
    """A Date value backed by a Python datetime."""

    def __init__(self, interp, dt=None):
        self._interp = interp
        self._dt = dt if dt is not None else datetime.datetime.now()

    @property
    def timestamp_ms(self):
        return int(self._dt.timestamp() * 1000)

    def js_get(self, name):
        d = self._dt
        m = {
            "getTime": lambda: self.timestamp_ms,
            "valueOf": lambda: self.timestamp_ms,
            "getFullYear": lambda: d.year,
            "getMonth": lambda: d.month - 1,
            "getDate": lambda: d.day,
            "getDay": lambda: d.weekday(),
            "getHours": lambda: d.hour,
            "getMinutes": lambda: d.minute,
            "getSeconds": lambda: d.second,
            "getMilliseconds": lambda: d.microsecond // 1000,
            "getTimezoneOffset": lambda: 0,
            "toISOString": lambda: d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "toJSON": lambda: d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "toString": self._to_string,
            "toDateString": lambda: d.strftime("%a %b %d %Y"),
            "toLocaleString": self._to_string,
            "toLocaleDateString": lambda: d.strftime("%m/%d/%Y"),
        }
        return m.get(name, UNDEFINED)

    def js_repr(self):
        return self._to_string()

    def _to_string(self):
        d = self._dt
        return (d.strftime("%a %b %d %Y %H:%M:%S")
                + " GMT+0000 (Coordinated Universal Time)")


class _DateCtor:
    """The `Date` global: `Date.now()`, `new Date(...)`, `Date(...)`."""

    def __init__(self, interp):
        self._interp = interp

    def js_get(self, name):
        if name == "now":
            return lambda: time.time() * 1000
        if name == "parse":
            return lambda s: 0
        if name == "UTC":
            return lambda *a: 0
        return UNDEFINED

    def js_new(self, *args):
        if not args or args[0] is UNDEFINED:
            return JSDate(self._interp)
        first = args[0]
        if isinstance(first, (int, float)):
            return JSDate(self._interp,
                          datetime.datetime.fromtimestamp(float(first) / 1000))
        return JSDate(self._interp, datetime.datetime.now())

    def js_call(self, *args):
        return JSDate(self._interp).toString()


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

_KEYWORDS = {
    "var", "let", "const", "function", "return", "if", "else", "while",
    "for", "break", "continue", "true", "false", "null", "undefined",
    "typeof", "throw", "try", "catch", "finally", "new", "this", "await",
    "in", "instanceof", "of", "class", "extends", "void", "delete", "do",
    "switch", "case", "default",
}

# Longest match first, so the tokenizer greedily groups '===', '!=', etc.
_PUNCT = (
    (3, "..."), (3, "==="), (3, "!=="), (3, "&&="), (3, "||="), (3, "??="),
    (3, ">>>"), (3, ">>>="), (3, "<<="), (3, ">>="),
    (2, "=="), (2, "!="), (2, "<="), (2, ">="), (2, "&&"), (2, "||"),
    (2, "??"), (2, "?."), (2, "=>"), (2, "+="), (2, "-="), (2, "*="),
    (2, "/="), (2, "++"), (2, "--"), (2, "<<"), (2, ">>"), (2, "&="),
    (2, "|="), (2, "^="),
    (1, "{"), (1, "}"), (1, "("), (1, ")"), (1, "["), (1, "]"),
    (1, ";"), (1, ","), (1, "."), (1, ":"), (1, "?"), (1, "="), (1, "!"),
    (1, "+"), (1, "-"), (1, "*"), (1, "/"), (1, "%"), (1, "<"), (1, ">"),
    (1, "&"), (1, "|"), (1, "^"), (1, "~"),
)

#: Simple backslash escapes in string literals; "\n" is a line continuation.
_SIMPLE_ESC = {"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"',
               "\n": ""}

#: Defensive cap so pathological inputs cannot exhaust memory in the lexer.
_MAX_TOKENS = 1_000_000

#: Per-drain caps so runaway microtask/timer loops can't stall the UI thread.
_MAX_MICROTASKS = 100_000
_MAX_TIMER_FIRES = 10_000


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
            elif ch == "`":
                # Template literal: literal parts + ${expr} substitutions.
                i += 1
                parts = []
                buf = []
                while True:
                    if i >= n:
                        self._fail(i, "unterminated template literal")
                    c = s[i]
                    if c == "\\":
                        i += 1
                        if i >= n:
                            self._fail(i, "unterminated template literal")
                        esc = s[i]
                        i += 1
                        if esc in _SIMPLE_ESC:
                            buf.append(_SIMPLE_ESC[esc])
                        else:
                            buf.append(esc)
                    elif c == "`":
                        i += 1
                        break
                    elif s.startswith("${", i):
                        if buf:
                            parts.append(("lit", "".join(buf)))
                            buf = []
                        i += 2
                        depth = 1
                        start = i
                        while i < n and depth > 0:
                            if s[i] in "'\"`":
                                close = s[i]
                                i += 1
                                while i < n and s[i] != close:
                                    if s[i] == "\\":
                                        i += 1
                                    i += 1
                                i += 1
                            elif s.startswith("${", i):
                                depth += 1
                                i += 2
                            elif s[i] == "}":
                                depth -= 1
                                if depth == 0:
                                    break
                                i += 1
                            else:
                                i += 1
                        parts.append(("expr", s[start:i]))
                        i += 1  # consume '}'
                    else:
                        buf.append(c)
                        i += 1
                if buf:
                    parts.append(("lit", "".join(buf)))
                if len(parts) == 1 and parts[0][0] == "lit":
                    tokens.append(("string", parts[0][1], i))
                else:
                    tokens.append(("template", parts, i))
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
            elif ch == "/" and i + 1 < n and s[i + 1] not in "/" \
                    and not self._prev_is_operand(tokens):
                # Regex literal: /pattern/flags (not division).
                j = i + 1
                in_class = False
                while j < n:
                    c = s[j]
                    if c == "\\":
                        j += 2
                        continue
                    if c == "[":
                        in_class = True
                    elif c == "]":
                        in_class = False
                    elif c == "/" and not in_class:
                        break
                    elif c == "\n":
                        self._fail(j, "unterminated regex literal")
                    j += 1
                if j >= n:
                    self._fail(i, "unterminated regex literal")
                pattern = s[i + 1:j]
                j += 1
                k = j
                while k < n and s[k].isalpha():
                    k += 1
                flags = s[j:k]
                tokens.append(("regex", (pattern, flags), i))
                i = k
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

    @staticmethod
    def _prev_is_operand(tokens):
        """A `/` is division (not a regex) when it follows an operand."""
        if not tokens:
            return False
        kind, value, _ = tokens[-1]
        if kind in ("number", "string", "regex", "template"):
            return True
        if kind == "ident":
            return True
        if kind == "kw":
            return value in ("true", "false", "null", "undefined", "this")
        if kind == "punct":
            return value in (")", "]", "}")
        return False

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
        ctx = self.source[max(0, offset - 35):offset + 35].replace("\n", " ")
        raise JSException(f"SyntaxError on line {line}: {msg} near {ctx!r}")

    # -- grammar ------------------------------------------------------------

    def parse_program(self):
        return Program(self._parse_stmts_until(None))

    def _statement(self):
        kind, value, _ = self._peek()
        if kind == "punct" and value == ";":
            self.pos += 1
            return Empty()
        if kind == "punct" and value == "{":
            return Block(self._parse_stmts_until("}"))
        if kind == "kw" and value == "function":
            nk, nv, _ = self._peek2()
            if nk != "ident":
                return ExprStmt(self._expression())  # anonymous fn expression
        if kind == "kw" and value in self._STMT:
            self.pos += 1
            return self._STMT[value](self)
        if kind == "kw" and value == "class":
            self.pos += 1
            return self._class_declaration()
        if kind == "ident" and value == "async" \
                and self._next_is_kw("function"):
            self.pos += 1
            self.pos += 1  # consume 'function'
            name = self._expect_ident()
            params, defaults, body = self._function_rest(True)
            return FunctionDecl(name, params, defaults, body, True)
        if kind == "ident":
            nk, nv, _ = self._peek2()
            if nk == "punct" and nv == ":":
                self.pos += 2  # skip `label:` (labeled statements)
                return self._statement()
        return ExprStmt(self._expression())

    def _class_declaration(self):
        name = self._expect_ident()
        return ClassDecl(name, self._class_body())

    def _class_body(self):
        self._expect_punct("{")
        methods = []
        while True:
            kind, value, _ = self._peek()
            if kind is None:
                self._syntax("expected '}'")
            if self._match_punct("}"):
                break
            if self._match_punct(";"):
                continue
            # Optional `static` / `get` / `set` modifiers.
            is_static = False
            if kind == "ident" and value in ("static", "get", "set") \
                    and self._next_is_ident():
                is_static = value == "static"
                self.pos += 1
            mname = self._expect_property_name()
            if self._match_punct("("):
                params = [p[0] for p in self._list(None, ")", self._parameter)]
                body = self._parse_stmts_until("}")
                if mname == "constructor":
                    methods.insert(0, ("__construct", params, body, False))
                else:
                    methods.append((mname, params, body, is_static))
            else:
                self._syntax("expected '('")
        return methods

    def _next_is_ident(self):
        kind, value, _ = self._peek2()
        return kind == "ident"

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
            target = self._declaration_target()
            value = None
            if self._match_punct("="):
                value = self._assign()
            decls.append((target, value))
            if self._match_punct(",") is None:
                break
        return decls

    def _declaration_target(self):
        kind, value, _ = self._peek()
        if kind == "ident":
            self.pos += 1
            return value
        pat = self._pattern()
        if pat is not None:
            return pat
        self._syntax("expected identifier")

    def _pattern(self):
        kind, value, _ = self._peek()
        if kind == "punct" and value == "[":
            self.pos += 1
            parts = []
            rest = None
            while True:
                if self._match_punct("]"):
                    break
                if self._match_punct(","):
                    parts.append(None)
                    continue
                if self._match_punct("..."):
                    rest = self._pattern_target()
                    self._expect_punct("]")
                    break
                target = self._pattern_target()
                default = None
                if self._match_punct("="):
                    default = self._assign()
                parts.append((target, default))
                if self._match_punct("]"):
                    break
                self._expect_punct(",")
            return Pattern("array", parts, rest)
        if kind == "punct" and value == "{":
            self.pos += 1
            parts = []
            rest = None
            while True:
                if self._match_punct("}"):
                    break
                if self._match_punct("..."):
                    rest = self._pattern_target()
                    self._expect_punct("}")
                    break
                key = self._expect_property_name()
                target = key
                if self._match_punct(":"):
                    target = self._pattern_target()
                default = None
                if self._match_punct("="):
                    default = self._assign()
                parts.append((key, target, default))
                if self._match_punct("}"):
                    break
                self._expect_punct(",")
            return Pattern("object", parts, rest)
        return None

    def _pattern_target(self):
        kind, value, _ = self._peek()
        if kind == "ident":
            self.pos += 1
            return value
        pat = self._pattern()
        if pat is not None:
            return pat
        self._syntax("expected pattern target")

    def _function_declaration(self, async_):
        name = self._expect_ident()
        params, defaults, body = self._function_rest(async_)
        return FunctionDecl(name, params, defaults, body, async_)

    def _function_rest(self, async_):
        params = self._list("(", ")", self._parameter)
        if async_:
            self.async_depth += 1
        try:
            body = self._parse_stmts_until("}")
        finally:
            if async_:
                self.async_depth -= 1
        return ([p[0] for p in params], [p[1] for p in params], body)

    def _parameter(self):
        """A function parameter, possibly with a default value: `name = expr`."""
        name = self._expect_ident()
        if self._match_punct("="):
            return (name, self._assign())
        return (name, None)

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
        # ASI: `if (x) stmt; else ...` — the `;` terminates the then-statement
        # and the `else` still binds to this if.
        self._match_punct(";")
        else_stmt = self._statement() if self._match_kw("else") else None
        return If(cond, then, else_stmt)

    def _do_while_statement(self):
        body = self._statement()
        self._match_punct(";")  # ASI: `do stmt; while(...)`
        self._expect_kw("while")
        self._expect_punct("(")
        cond = self._expression()
        self._expect_punct(")")
        self._match_punct(";")
        return DoWhile(body, cond)

    def _expect_kw(self, text):
        if self._match_kw(text) is None:
            self._syntax(f"expected '{text}'")

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
                name = self._declaration_target()
                nk, nv, _ = self._peek()
                if nk == "kw" and nv == "of":
                    self.pos += 1
                    iterable = self._expression()
                    self._expect_punct(")")
                    body = self._statement()
                    return ForOf(name, iterable, body, value)
                if nk == "kw" and nv == "in":
                    self.pos += 1
                    obj = self._expression()
                    self._expect_punct(")")
                    body = self._statement()
                    return ForIn(name, obj, body, value)
                # Classic var init with one or more declarators.
                decls = [(name, None)]
                if self._match_punct("="):
                    decls[0] = (name, self._assign())
                while self._match_punct(","):
                    n2 = self._expect_ident()
                    v2 = None
                    if self._match_punct("="):
                        v2 = self._assign()
                    decls.append((n2, v2))
                init = VarDecl(value, decls)
            else:
                # `for (ident of/in expr)` without a declaration keyword.
                nk, nv, _ = self._peek2()
                if kind == "ident" and nk == "kw" and nv in ("of", "in"):
                    name = self._expect_ident()
                    self.pos += 1  # consume of/in
                    target = self._expression()
                    self._expect_punct(")")
                    body = self._statement()
                    return ForOf(name, target, body) if nv == "of" \
                        else ForIn(name, target, body)
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

    def _switch_statement(self):
        self._expect_punct("(")
        expr = self._expression()
        self._expect_punct(")")
        self._expect_punct("{")
        cases = []
        while True:
            kind, value, _ = self._peek()
            if kind is None:
                self._syntax("expected '}'")
            if self._match_punct("}"):
                break
            kind2, value2, _ = self._peek()
            if kind2 == "kw" and value2 == "case":
                self.pos += 1
                test = self._expression()
                self._expect_punct(":")
                cases.append(("case", test, self._case_body()))
            elif kind2 == "kw" and value2 == "default":
                self.pos += 1
                self._expect_punct(":")
                cases.append(("default", None, self._case_body()))
            else:
                self._syntax("expected 'case' or 'default'")
        return Switch(expr, cases)

    def _case_body(self):
        stmts = []
        while True:
            kind, value, _ = self._peek()
            if kind is None:
                self._syntax("expected '}'")
            if kind == "punct" and value == "}":
                break
            if kind == "kw" and value in ("case", "default"):
                break
            if self._match_punct(";"):
                continue
            stmts.append(self._statement())
            self._match_punct(";")
        return stmts

    # -- expressions --------------------------------------------------------

    def _expression(self):
        return self._sequence()

    def _sequence(self):
        node = self._assign()
        while self._match_punct(","):
            node = Sequence(node, self._assign())
        return node

    def _assign(self):
        arrow = self._try_arrow()
        if arrow is not None:
            return arrow
        left = self._conditional()
        kind, value, _ = self._peek()
        if kind == "punct" and value in ("=", "+=", "-=", "*=", "/=",
                                         "||=", "&&=", "??=", "&=", "|=",
                                         "^=", "<<=", ">>=", ">>>="):
            if value == "=" and isinstance(left, (ArrayLit, ObjectLit)):
                pattern = self._lit_to_pattern(left)
                if pattern is not None:
                    self.pos += 1
                    right = self._assign()
                    return Assign("=", pattern, right)
            self.pos += 1
            right = self._assign()
            if not isinstance(left, (Identifier, Member, Index)):
                self._syntax("invalid assignment target")
            return Assign(value, left, right)
        return left

    def _lit_to_pattern(self, node):
        """Convert an array/object literal used as an assignment target into
        a destructuring Pattern."""
        if isinstance(node, ArrayLit):
            parts = []
            rest = None
            for item in node.items:
                if isinstance(item, Spread):
                    t = self._expr_to_pattern_target(item.expr)
                    if t is not None:
                        rest = t
                    continue
                if isinstance(item, Assign) and item.op == "=":
                    t = self._expr_to_pattern_target(item.target)
                    parts.append((t, item.value))
                else:
                    t = self._expr_to_pattern_target(item)
                    parts.append((t, None))
            return Pattern("array", parts, rest)
        if isinstance(node, ObjectLit):
            parts = []
            rest = None
            for pair in node.pairs:
                if isinstance(pair, tuple) and pair[0] == "spread":
                    t = self._expr_to_pattern_target(pair[1])
                    if t is not None:
                        rest = t
                    continue
                key, expr = pair
                if isinstance(expr, Assign) and expr.op == "=":
                    t = self._expr_to_pattern_target(expr.target)
                    parts.append((key, t, expr.value))
                else:
                    t = self._expr_to_pattern_target(expr)
                    parts.append((key, t, None))
            return Pattern("object", parts, rest)
        return None

    def _expr_to_pattern_target(self, expr):
        if isinstance(expr, Identifier):
            return expr.name
        if isinstance(expr, (ArrayLit, ObjectLit)):
            return self._lit_to_pattern(expr)
        return None

    def _try_arrow(self):
        """Detect arrow functions: (a, b) => ... or x => ..."""
        kind, value, _ = self._peek()
        if kind == "ident":
            nk, nv, _ = self._peek2()
            if nk == "punct" and nv == "=>":
                self.pos += 1
                self.pos += 1
                return self._arrow_rest([(value, None)])
            return None
        if kind == "punct" and value == "(" \
                and self._peek_matching_paren_then("=>"):
            self.pos += 1
            params = self._list(None, ")", self._parameter)
            self._match_punct("=>")
            return self._arrow_rest(params)
        return None

    def _arrow_rest(self, params):
        names = [p[0] for p in params]
        defaults = [p[1] for p in params]
        kind, value, _ = self._peek()
        if kind == "punct" and value == "{":
            body = self._parse_stmts_until("}")
            return FunctionExpr(None, names, defaults, body, False,
                                arrow=True)
        expr = self._assign()
        return FunctionExpr(None, names, defaults, [Return(expr)], False,
                            arrow=True)

    def _peek_matching_paren_then(self, text):
        depth = 0
        i = self.pos
        while i < len(self.tokens):
            kind, value, _ = self.tokens[i]
            if kind == "punct":
                if value == "(":
                    depth += 1
                elif value == ")":
                    depth -= 1
                    if depth == 0:
                        nk, nv, _ = self.tokens[i + 1] \
                            if i + 1 < len(self.tokens) else (None, None, 0)
                        return nk == "punct" and nv == text
            i += 1
        return False

    def _conditional(self):
        cond = self._or()
        if self._match_punct("?"):
            then_expr = self._assign()
            self._expect_punct(":")
            else_expr = self._assign()
            return Conditional(cond, then_expr, else_expr)
        return cond

    def _or(self):
        node = self._coalesce()
        while self._match_punct("||"):
            node = Logical("||", node, self._coalesce())
        return node

    def _coalesce(self):
        node = self._and()
        while self._match_punct("??"):
            node = Nullish(node, self._and())
        return node

    def _and(self):
        return self._logical_chain("&&", self._bitor)

    def _bitor(self):
        return self._binop(self._bitxor, ("|",))

    def _bitxor(self):
        return self._binop(self._bitand, ("^",))

    def _bitand(self):
        return self._binop(self._equality, ("&",))

    def _logical_chain(self, op, sub):
        node = sub()
        while self._match_punct(op):
            node = Logical(op, node, sub())
        return node

    def _equality(self):
        return self._binop(self._relational, ("==", "!=", "===", "!=="))

    def _relational(self):
        return self._binop(self._shift, ("<", "<=", ">", ">=",
                                         "in", "instanceof"))

    def _shift(self):
        return self._binop(self._additive, ("<<", ">>", ">>>"))

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
        if kind in ("punct", "kw") and value in texts:
            self.pos += 1
            return value
        return None

    def _unary(self):
        kind, value, _ = self._peek()
        if kind == "punct" and value in ("!", "-", "++", "--", "~"):
            self.pos += 1
            if value in ("++", "--"):
                return Update(value, self._unary(), True)
            return Unary(value, self._unary())
        if kind == "kw" and value in ("typeof", "void", "delete"):
            self.pos += 1
            return Unary(value, self._unary())
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
            elif self._match_punct("?."):
                nk, nv, _ = self._peek()
                if nk == "punct" and nv == "(":
                    node = OptionalCall(node, self._args())
                elif nk == "punct" and nv == "[":
                    self.pos += 1
                    index = self._expression()
                    self._expect_punct("]")
                    node = OptionalIndex(node, index)
                else:
                    node = OptionalMember(node, self._expect_property_name())
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

    def _argument(self):
        if self._match_punct("..."):
            return Spread(self._assign())
        return self._assign()

    def _args(self):
        return self._list(None, ")", self._argument)

    def _array_item(self):
        if self._match_punct("..."):
            return Spread(self._assign())
        return self._assign()

    def _new_expression(self):
        callee = self._primary()
        args = self._args() if self._match_punct("(") else []
        return New(callee, args)

    def _parse_expr_source(self, source):
        sub = _Parser(source)
        return sub._expression()

    def _primary(self):
        kind, value, _ = self._peek()
        if kind in ("number", "string"):
            self.pos += 1
            return Literal(value)
        if kind == "regex":
            self.pos += 1
            pattern, flags = value
            return RegexLiteral(pattern, flags)
        if kind == "template":
            self.pos += 1
            parts = []
            for pkind, pval in value:
                if pkind == "lit":
                    parts.append(("lit", pval))
                else:
                    parts.append(("expr", self._parse_expr_source(pval)))
            return TemplateLiteral(parts)
        if kind == "kw":
            self.pos += 1
            if value in ("true", "false", "null", "undefined"):
                return Literal({"true": True, "false": False,
                                "null": None, "undefined": UNDEFINED}[value])
            if value == "function":
                return self._function_expression(False)
            if value == "class":
                return self._class_expression()
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
                return ArrayLit(self._list("[", "]", self._array_item,
                                           trailing=True))
            if value == "{":
                return ObjectLit(self._list("{", "}", self._object_pair,
                                            trailing=True))
        self._syntax("unexpected token")

    def _class_expression(self):
        name = None
        kind, value, _ = self._peek()
        if kind == "ident":
            self.pos += 1
            name = value
        return ClassExpr(name, self._class_body())

    def _function_expression(self, async_):
        name = None
        kind, value, _ = self._peek()
        if kind == "ident":
            self.pos += 1
            name = value
        params, defaults, body = self._function_rest(async_)
        return FunctionExpr(name, params, defaults, body, async_)

    def _object_pair(self):
        if self._match_punct("..."):
            return ("spread", self._expression())
        kind, value, _ = self._peek()
        if kind in ("ident", "string", "kw"):
            self.pos += 1
            key = value
        else:
            self._syntax("expected property name")
        nk, nv, _ = self._peek()
        if nk == "punct" and nv == ":":
            self.pos += 1
            return (key, self._assign())
        if nk == "punct" and nv == "(":
            self.pos += 1  # consume '('
            params = [p[0] for p in self._list(None, ")", self._parameter)]
            body = self._parse_stmts_until("}")
            return (key, FunctionExpr(None, params, [None] * len(params),
                                      body, False))
        if kind in ("ident", "kw"):
            return (key, Identifier(key))
        self._syntax("expected ':'")


_Parser._STMT = {
    "var": lambda s: VarDecl("var", s._declaration_list()),
    "let": lambda s: VarDecl("let", s._declaration_list()),
    "const": lambda s: VarDecl("const", s._declaration_list()),
    "function": lambda s: s._function_declaration(False),
    "return": lambda s: s._return_statement(),
    "if": lambda s: s._if_statement(),
    "while": lambda s: s._while_statement(),
    "do": lambda s: s._do_while_statement(),
    "for": lambda s: s._for_statement(),
    "break": lambda s: Break(),
    "continue": lambda s: Continue(),
    "throw": lambda s: s._throw_statement(),
    "try": lambda s: s._try_statement(),
    "switch": lambda s: s._switch_statement(),
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
    cancelled: bool = False


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
            "isNaN": lambda v: _to_number(v) != _to_number(v),
            "isFinite": lambda v: math.isfinite(_to_number(v)),
            "encodeURIComponent": lambda s: urllib.parse.quote(
                self.repr(s), safe="!'()*-._~"),
            "decodeURIComponent": lambda s: urllib.parse.unquote(str(s)),
            "encodeURI": lambda s: urllib.parse.quote(
                self.repr(s), safe="!'()*-._~;/?:@&=+$,#"),
            "decodeURI": lambda s: urllib.parse.unquote(str(s)),
            "Array": _ArrayCtor(self),
            "Object": _ObjectCtor(self),
            "Math": self._make_math(),
            "JSON": {"parse": self._json_parse, "stringify": self._json_stringify},
            "Date": _DateCtor(self),
            "RegExp": _RegExpCtor(),
            "NaN": float("nan"),
            "Infinity": float("inf"),
            "Promise": JSPromiseCtor(self),
            "Error": _ErrorCtor("Error"),
            "TypeError": _ErrorCtor("TypeError"),
            "RangeError": _ErrorCtor("RangeError"),
            "SyntaxError": _ErrorCtor("SyntaxError"),
            "ReferenceError": _ErrorCtor("ReferenceError"),
            "EvalError": _ErrorCtor("EvalError"),
            "URIError": _ErrorCtor("URIError"),
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
            node = obj
            while isinstance(node, dict):
                if name in node:
                    return node[name]
                proto = node.get("__proto__")
                node = proto if isinstance(proto, dict) else None
            return UNDEFINED
        if isinstance(obj, list):
            return self._list_get(obj, name)
        if isinstance(obj, str):
            return self._string_get(obj, name)
        if isinstance(obj, (int, float)):
            return self._number_get(obj, name)
        if isinstance(obj, JSFunction):
            return self._function_get(obj, name)
        if callable(obj):
            return self._pycallable_get(obj, name)
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
        if name == "shift":
            def shift():
                if not arr:
                    return UNDEFINED
                return arr.pop(0)
            return shift
        if name == "unshift":
            def unshift(*values):
                arr[0:0] = list(values)
                return len(arr)
            return unshift
        if name == "join":
            def join(sep=","):
                return (sep if isinstance(sep, str) else ",").join(
                    self.repr(item) for item in arr)
            return join
        if name == "slice":
            def slice(a=0, b=None):
                a = _to_number(a) if not _nullish(a) else 0
                b = _to_number(b) if not _nullish(b) else len(arr)
                return arr[int(a):int(b)]
            return slice
        if name == "concat":
            def concat(*items):
                out = list(arr)
                for item in items:
                    out.extend(item if isinstance(item, list) else [item])
                return out
            return concat
        if name == "indexOf":
            def index_of(needle, f=0):
                try:
                    return arr.index(needle, int(f))
                except ValueError:
                    return -1
            return index_of
        if name == "lastIndexOf":
            def last_index_of(needle, f=None):
                f = len(arr) - 1 if f is None else int(f)
                for i in range(min(f, len(arr) - 1), -1, -1):
                    if arr[i] == needle:
                        return i
                return -1
            return last_index_of
        if name == "includes":
            def includes(needle, f=0):
                return needle in arr[int(f):]
            return includes
        if name == "reverse":
            def reverse():
                arr.reverse()
                return arr
            return reverse
        if name == "sort":
            def sort(fn=None):
                if fn is None or fn is UNDEFINED:
                    arr.sort(key=lambda v: self.repr(v))
                else:
                    from functools import cmp_to_key

                    def cmp(a, b):
                        r = self._call_value(fn, [a, b])
                        r = _to_number(r)
                        return -1 if r < 0 else (1 if r > 0 else 0)
                    arr.sort(key=cmp_to_key(cmp))
                return arr
            return sort
        if name == "map":
            def map(fn):
                return [self._call_value(fn, [item, i, arr])
                        for i, item in enumerate(arr)]
            return map
        if name == "forEach":
            def for_each(fn):
                for i, item in enumerate(arr):
                    self._call_value(fn, [item, i, arr])
                return UNDEFINED
            return for_each
        if name == "filter":
            def filter(fn):
                return [item for i, item in enumerate(arr)
                        if self._truthy(self._call_value(fn, [item, i, arr]))]
            return filter
        if name == "reduce":
            def reduce(fn, initial=UNDEFINED):
                acc = initial
                start = 0
                if acc is UNDEFINED:
                    if not arr:
                        raise JSException("Reduce of empty array with no initial value")
                    acc = arr[0]
                    start = 1
                for i in range(start, len(arr)):
                    acc = self._call_value(fn, [acc, arr[i], i, arr])
                return acc
            return reduce
        if name == "reduceRight":
            def reduce_right(fn, initial=UNDEFINED):
                acc = initial
                start = len(arr) - 1
                if acc is UNDEFINED:
                    if not arr:
                        raise JSException("Reduce of empty array with no initial value")
                    acc = arr[-1]
                    start = len(arr) - 2
                for i in range(start, -1, -1):
                    acc = self._call_value(fn, [acc, arr[i], i, arr])
                return acc
            return reduce_right
        if name == "find":
            def find(fn):
                for i, item in enumerate(arr):
                    if self._truthy(self._call_value(fn, [item, i, arr])):
                        return item
                return UNDEFINED
            return find
        if name == "findIndex":
            def find_index(fn):
                for i, item in enumerate(arr):
                    if self._truthy(self._call_value(fn, [item, i, arr])):
                        return i
                return -1
            return find_index
        if name == "some":
            def some(fn):
                for i, item in enumerate(arr):
                    if self._truthy(self._call_value(fn, [item, i, arr])):
                        return True
                return False
            return some
        if name == "every":
            def every(fn):
                for i, item in enumerate(arr):
                    if not self._truthy(self._call_value(fn, [item, i, arr])):
                        return False
                return True
            return every
        if name == "splice":
            def splice(start, delete_count=0, *items):
                start = int(_to_number(start)) if not _nullish(start) else 0
                if start < 0:
                    start = max(0, len(arr) + start)
                removed = arr[start:start + int(delete_count)]
                arr[start:start + int(delete_count)] = list(items)
                return removed
            return splice
        if name == "keys":
            return list(range(len(arr)))
        if name == "values":
            return list(arr)
        if name == "entries":
            return [[i, v] for i, v in enumerate(arr)]
        if name == "at":
            def at(i=0):
                i = int(_to_number(i))
                if -len(arr) <= i < len(arr):
                    return arr[i]
                return UNDEFINED
            return at
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
        methods = {
            "toUpperCase": lambda: text.upper(),
            "toLowerCase": lambda: text.lower(),
            "trim": lambda: text.strip(),
            "trimStart": lambda: text.lstrip(),
            "trimEnd": lambda: text.rstrip(),
            "toString": lambda: text,
            "valueOf": lambda: text,
            "concat": lambda *args: text + "".join(self.repr(a) for a in args),
            "charAt": lambda i=0: text[int(i)] if 0 <= int(i) < len(text) else "",
            "charCodeAt": lambda i=0: ord(text[int(i)]) if 0 <= int(i) < len(text) else float("nan"),
            "codePointAt": lambda i=0: ord(text[int(i)]) if 0 <= int(i) < len(text) else UNDEFINED,
            "at": lambda i=0: text[int(i)] if -len(text) <= int(i) < len(text) else "",
            "indexOf": lambda s, f=0: self._str_index(text, s, f, first=True),
            "lastIndexOf": lambda s, f=None: self._str_index(text, s, f, first=False),
            "includes": lambda s, f=0: self._str_index(text, s, f, first=True) != -1,
            "startsWith": lambda s, f=0: text[int(f):].startswith(str(s)),
            "endsWith": lambda s: text.endswith(str(s)),
            "slice": lambda a=0, b=None: self._str_slice(text, a, b),
            "substring": lambda a=0, b=None: self._str_substring(text, a, b),
            "substr": lambda a=0, l=None: self._str_substr(text, a, l),
            "split": lambda sep=None, limit=None: self._str_split(text, sep, limit),
            "replace": lambda a, b: self._str_replace(text, a, b),
            "replaceAll": lambda a, b: self._str_replace(text, a, b, all_=True),
            "padStart": lambda n, s=" ": self._str_pad(text, n, s, left=True),
            "padEnd": lambda n, s=" ": self._str_pad(text, n, s, left=False),
            "repeat": lambda n: text * max(0, int(_to_number(n))),
            "localeCompare": lambda o: (text > str(o)) - (text < str(o)),
            "match": lambda rx: self._str_match(text, rx),
            "search": lambda rx: self._str_search(text, rx),
            "toLocaleLowerCase": lambda: text.lower(),
            "toLocaleUpperCase": lambda: text.upper(),
        }
        return methods.get(name, UNDEFINED)

    def _number_get(self, num, name):
        methods = {
            "toFixed": lambda d=0: self._to_fixed(num, int(_to_number(d))),
            "toExponential": lambda d=None: self._to_exp(num, d),
            "toString": lambda: self.repr(num),
            "valueOf": lambda: num,
            "toPrecision": lambda p=None: self.repr(num),
        }
        return methods.get(name, UNDEFINED)

    def _pycallable_get(self, fn, name):
        if name == "call":
            return lambda this_arg=UNDEFINED, *args: \
                self._call_value(fn, [this_arg, *args])
        if name == "apply":
            def apply(this_arg=UNDEFINED, args=None):
                arg_list = list(args) if isinstance(args, list) else []
                return self._call_value(fn, [this_arg, *arg_list])
            return apply
        if name == "bind":
            def bind(this_arg=UNDEFINED, *pre):
                def bound(*args):
                    return self._call_value(fn, [this_arg, *pre, *args])
                return bound
            return bind
        return UNDEFINED

    def _function_get(self, fn, name):
        if fn.statics is not None and name in fn.statics:
            return fn.statics[name]
        if name == "length":
            return len(fn.params)
        if name == "name":
            return fn.name
        if name == "call":
            return lambda this_arg=UNDEFINED, *args: \
                self._call_value(fn, list(args), this_arg)
        if name == "apply":
            def apply(this_arg=UNDEFINED, args=None):
                arg_list = list(args) if isinstance(args, list) else []
                return self._call_value(fn, arg_list, this_arg)
            return apply
        if name == "bind":
            def bind(this_arg=UNDEFINED, *pre):
                def bound(*args):
                    return self._call_value(fn, list(pre) + list(args), this_arg)
                bound.__name__ = "bound " + fn.name
                return bound
            return bind
        return UNDEFINED

    # -- string helpers ---------------------------------------------------

    def _str_index(self, text, needle, start, first):
        needle = self.repr(needle)
        start = int(_to_number(start)) if not _nullish(start) else 0
        if first:
            return text.find(needle, start)
        end = len(text)
        if not _nullish(start):
            end = min(len(text), max(0, int(_to_number(start)) + 1))
        return text.rfind(needle, 0, end)

    def _str_slice(self, text, a, b):
        a = int(_to_number(a)) if not _nullish(a) else 0
        b = len(text) if _nullish(b) else int(_to_number(b))
        if a < 0:
            a = max(0, len(text) + a)
        if b < 0:
            b = max(0, len(text) + b)
        return text[a:b]

    def _str_substring(self, text, a, b):
        a = max(0, int(_to_number(a))) if not _nullish(a) else 0
        b = len(text) if _nullish(b) else max(0, int(_to_number(b)))
        lo, hi = min(a, b), max(a, b)
        return text[lo:hi]

    def _str_substr(self, text, a, l):
        a = int(_to_number(a)) if not _nullish(a) else 0
        if a < 0:
            a = max(0, len(text) + a)
        length = len(text) if _nullish(l) else max(0, int(_to_number(l)))
        return text[a:a + length]

    def _str_split(self, text, sep, limit):
        limit = int(_to_number(limit)) if not _nullish(limit) else -1
        if sep is UNDEFINED or sep is None:
            return [text]
        if isinstance(sep, JSRegExp):
            parts = sep.split(text)
        else:
            parts = text.split(str(sep))
        if limit >= 0:
            parts = parts[:limit]
        return parts

    def _str_replace(self, text, needle, repl, all_=False):
        if isinstance(needle, JSRegExp):
            pattern, flags = needle.pattern, needle.flags
            count = 0 if (all_ or "g" in flags) else 1
            if callable(repl) or isinstance(repl, JSFunction):
                def sub(m):
                    args = [m.group(0)] + list(m.groups())
                    return self.repr(self._call_value(repl, args))
                return re.sub(pattern, sub, text, count=count,
                              flags=needle._flags)
            out = re.sub(pattern, _js_repl_to_py(repl), text, count=count,
                         flags=needle._flags)
            return out
        needle = str(needle)
        repl = _js_repl_to_py(repl) if not callable(repl) else repl
        if callable(repl):
            return repl(needle)  # rare; treat as 1:1
        if all_:
            return text.replace(needle, repl)
        return text.replace(needle, repl, 1)

    def _str_match(self, text, rx):
        if not isinstance(rx, JSRegExp):
            rx = JSRegExp(self.repr(rx))
        m = rx.search(text)
        if m is None:
            return None
        if rx.global_:
            return [g.group(0) for g in rx.finditer(text)]
        return list(m.groups()) if m.groups() else [m.group(0)]

    def _str_search(self, text, rx):
        if not isinstance(rx, JSRegExp):
            rx = JSRegExp(self.repr(rx))
        m = rx.search(text)
        return m.start() if m else -1

    def _str_pad(self, text, n, s, left):
        n = int(_to_number(n)) if not _nullish(n) else 0
        s = str(s) or " "
        need = n - len(text)
        if need <= 0:
            return text
        fill = (s * ((need // len(s)) + 1))[:need]
        return (fill + text) if left else (text + fill)

    def _to_fixed(self, num, digits):
        digits = max(0, min(100, digits))
        return f"{num:.{digits}f}"

    def _to_exp(self, num, digits):
        if digits is None:
            return repr(float(num))
        return f"{float(num):.{max(0, int(_to_number(digits)))}e}"

    # -- timers / microtasks ---------------------------------------------

    def advance(self, ms):
        """Move the virtual clock forward; due timers fire on the next drain."""
        self._now += float(ms)

    def enqueue(self, job):
        self._microtasks.append(job)

    def drain(self):
        """Run pending microtasks and due timers until quiescent. Per-call
        caps bound the work so pages that reschedule 0-delay timers
        (requestAnimationFrame / setInterval(..., 0) loops) can't stall the
        UI thread; leftovers fire on the next drain.
        """
        microtasks_run = 0
        timers_fired = 0
        while True:
            while self._microtasks:
                job = self._microtasks.popleft()
                microtasks_run += 1
                try:
                    job()
                except (_JSThrow, JSException) as e:
                    self.logs.append(self._error_text(e))
                if microtasks_run >= _MAX_MICROTASKS:
                    return
            due = [t for t in self._timers if t.due <= self._now]
            if not due:
                break
            for t in due:
                if t.cancelled:
                    continue
                timers_fired += 1
                try:
                    self._call_value(t.fn, t.args)
                except (_JSThrow, JSException) as e:
                    self.logs.append(self._error_text(e))
                if t.cancelled:
                    # clearInterval() ran inside the callback.
                    if t in self._timers:
                        self._timers.remove(t)
                    continue
                if t.repeat:
                    t.due += t.interval
                    if t not in self._timers:
                        self._timers.append(t)
                elif t in self._timers:
                    # One-shot timer: consumed.
                    self._timers.remove(t)
                if timers_fired >= _MAX_TIMER_FIRES:
                    return

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
        for t in self._timers:
            if t.id == timer_id:
                t.cancelled = True
                self._timers.remove(t)
                return
        return UNDEFINED

    def _native_queue_microtask(self, fn):
        self.enqueue(lambda: self._call_value(fn, []))

    # -- Math / JSON / Date helpers ----------------------------------------

    def _make_math(self):
        def _num(fn):
            return lambda *args: fn(*[_to_number(a) for a in args])

        return {
            "abs": _num(lambda x: abs(x)),
            "floor": _num(lambda x: math.floor(x)),
            "ceil": _num(lambda x: math.ceil(x)),
            "round": _num(lambda x: math.floor(x + 0.5) if x >= 0
                          else math.ceil(x - 0.5)),
            "trunc": _num(lambda x: math.trunc(x)),
            "sign": _num(lambda x: (1 if x > 0 else -1 if x < 0
                                    else 0 if x == 0 else float("nan"))),
            "max": lambda *args: max((_to_number(a) for a in args),
                                     default=float("-inf")),
            "min": lambda *args: min((_to_number(a) for a in args),
                                     default=float("inf")),
            "pow": _num(lambda a, b: a ** b),
            "sqrt": _num(lambda x: math.sqrt(x)),
            "cbrt": _num(lambda x: math.cbrt(x)),
            "log": _num(lambda x: math.log(x)),
            "log2": _num(lambda x: math.log2(x)),
            "log10": _num(lambda x: math.log10(x)),
            "log1p": _num(lambda x: math.log1p(x)),
            "exp": _num(lambda x: math.exp(x)),
            "sin": _num(lambda x: math.sin(x)),
            "cos": _num(lambda x: math.cos(x)),
            "tan": _num(lambda x: math.tan(x)),
            "asin": _num(lambda x: math.asin(x)),
            "acos": _num(lambda x: math.acos(x)),
            "atan": _num(lambda x: math.atan(x)),
            "atan2": _num(lambda y, x: math.atan2(y, x)),
            "hypot": lambda *args: math.hypot(*[_to_number(a) for a in args]),
            "random": lambda: random.random(),
            "imul": lambda a, b: int(_to_number(a)) * int(_to_number(b)),
            "fround": lambda x: float(_to_number(x)),
            "clz32": self._math_clz32,
            "PI": math.pi,            "E": math.e,
            "LN2": math.log(2),
            "LN10": math.log(10),
            "LOG2E": math.log2(math.e),
            "LOG10E": math.log10(math.e),
            "SQRT2": math.sqrt(2),
            "SQRT1_2": math.sqrt(0.5),
        }

    def _math_clz32(self, x):
        n = int(_to_number(x)) & 0xFFFFFFFF
        return 32 - len(bin(n)[2:]) if n else 32

    def _json_parse(self, text):
        try:
            return json.loads(str(text))
        except Exception as e:  # noqa: BLE001 - JSON.parse throws
            raise JSException(f"Unexpected token in JSON: {e}") from None

    def _json_stringify(self, value, replacer=None, space=None):
        def ser(v, seen):
            if v is None:
                return "null"
            if v is UNDEFINED:
                return None
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, str):
                return json.dumps(v)
            if isinstance(v, (int, float)):
                if v != v or v in (float("inf"), float("-inf")):
                    return "null"
                return repr(v)
            if isinstance(v, JSFunction) or callable(v):
                return None
            if isinstance(v, list):
                if id(v) in seen:
                    raise JSException("Converting circular structure to JSON")
                seen = seen | {id(v)}
                items = [ser(item, seen) for item in v]
                return "[" + ",".join(i for i in items if i is not None) + "]"
            if isinstance(v, dict):
                if id(v) in seen:
                    raise JSException("Converting circular structure to JSON")
                seen = seen | {id(v)}
                pairs = []
                for k in v:
                    if k == "__proto__":
                        continue
                    s = ser(v[k], seen)
                    if s is not None:
                        pairs.append(json.dumps(str(k)) + ":" + s)
                return "{" + ",".join(pairs) + "}"
            if _is_objectish(v):
                to_json = None
                try:
                    to_json = self.js_get(v, "toJSON")
                except Exception:
                    to_json = None
                if callable(to_json):
                    return ser(self._call_value(to_json, []), seen)
            return None
        out = ser(value, frozenset())
        return "undefined" if out is None else out

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
            if callee.prototype is not None:
                obj["__proto__"] = callee.prototype
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
        for i in range(len(args), len(fn.params)):
            default = fn.defaults[i]
            if default is not None:
                value = yield from self._eval(default, scope)
            else:
                value = UNDEFINED
            scope.set_var(fn.params[i], value)
        if this_arg is not UNDEFINED and not fn.arrow:
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
        for i in range(len(args), len(fn.params)):
            default = fn.defaults[i]
            if default is not None:
                value = self._pump_sync(self._eval(default, scope))
            else:
                value = UNDEFINED
            scope.set_var(fn.params[i], value)
        if this_arg is not UNDEFINED and not fn.arrow:
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
                if isinstance(item, Spread):
                    src = yield from self._eval(item.expr, env)
                    if isinstance(src, list):
                        out.extend(src)
                    else:
                        out.append(src)
                else:
                    out.append((yield from self._eval(item, env)))
            return out
        if isinstance(node, ObjectLit):
            out = {}
            for pair in node.pairs:
                if isinstance(pair, tuple) and pair[0] == "spread":
                    src = yield from self._eval(pair[1], env)
                    if isinstance(src, dict):
                        for k, v in src.items():
                            if k != "__proto__":
                                out[k] = v
                    continue
                key, expr = pair
                out[key] = yield from self._eval(expr, env)
            return out
        if isinstance(node, FunctionExpr):
            return JSFunction(node.params, node.body, env, self,
                              node.name or "", node.async_, node.arrow,
                              defaults=node.defaults)
        if isinstance(node, TemplateLiteral):
            out = []
            for pkind, part in node.parts:
                if pkind == "lit":
                    out.append(part)
                else:
                    out.append(self.repr((yield from self._eval(part, env))))
            return "".join(out)
        if isinstance(node, RegexLiteral):
            return JSRegExp(node.pattern, node.flags)
        if isinstance(node, Spread):
            return (yield from self._eval(node.expr, env))
        if isinstance(node, ClassExpr):
            return self._make_class_ctor(node)
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
        if isinstance(node, Nullish):
            left = yield from self._eval(node.left, env)
            if not _nullish(left):
                return left
            return (yield from self._eval(node.right, env))
        if isinstance(node, Sequence):
            yield from self._eval(node.left, env)
            return (yield from self._eval(node.right, env))
        if isinstance(node, Conditional):
            if self._truthy((yield from self._eval(node.cond, env))):
                return (yield from self._eval(node.then_expr, env))
            return (yield from self._eval(node.else_expr, env))
        if isinstance(node, Assign):
            target = node.target
            if isinstance(target, Pattern):
                value = yield from self._eval(node.value, env)
                yield from self._bind_pattern(target, value, env, env.assign)
                return value
            return (yield from self._eval_assign(node, env))
        if isinstance(node, Call):
            return (yield from self._eval_call(node, env))
        if isinstance(node, OptionalCall):
            callee = yield from self._eval(node.callee, env)
            if callee is None or callee is UNDEFINED:
                return UNDEFINED
            args = yield from self._eval_args(node.args, env)
            return self._call_value(callee, args)
        if isinstance(node, New):
            callee = yield from self._eval(node.callee, env)
            args = yield from self._eval_args(node.args, env)
            return self._construct(callee, args)
        if isinstance(node, Member):
            obj = yield from self._eval(node.obj, env)
            return self.js_get(obj, node.name)
        if isinstance(node, OptionalMember):
            obj = yield from self._eval(node.obj, env)
            if _nullish(obj):
                return UNDEFINED
            return self.js_get(obj, node.name)
        if isinstance(node, Index):
            obj = yield from self._eval(node.obj, env)
            name = self._index_name((yield from self._eval(node.index, env)))
            return self.js_get(obj, name)
        if isinstance(node, OptionalIndex):
            obj = yield from self._eval(node.obj, env)
            if _nullish(obj):
                return UNDEFINED
            name = self._index_name((yield from self._eval(node.index, env)))
            return self.js_get(obj, name)
        if isinstance(node, InOp):
            left = yield from self._eval(node.left, env)
            right = yield from self._eval(node.right, env)
            return self._in_operator(left, right)
        if isinstance(node, InstanceOf):
            left = yield from self._eval(node.left, env)
            right = yield from self._eval(node.right, env)
            return self._instance_of(left, right)
        if isinstance(node, Await):
            value = yield from self._eval(node.expr, env)
            promise = self._as_promise(value)
            if promise.rejected:
                raise _JSThrow(promise.value)
            if promise.pending:
                return (yield _Suspend(promise))
            return promise.value
        raise JSException(f"Unknown expression {type(node).__name__}.")

    def _eval_args(self, arg_nodes, env):
        args = []
        for a in arg_nodes:
            if isinstance(a, Spread):
                src = yield from self._eval(a.expr, env)
                if isinstance(src, list):
                    args.extend(src)
                else:
                    args.append(src)
            else:
                args.append((yield from self._eval(a, env)))
        return args

    def _in_operator(self, left, right):
        key = self._index_name(left)
        if isinstance(right, dict):
            return key in right
        if isinstance(right, list):
            try:
                idx = int(_to_number(left))
                return 0 <= idx < len(right)
            except (TypeError, ValueError):
                return False
        if isinstance(right, str):
            try:
                idx = int(_to_number(left))
                return 0 <= idx < len(right)
            except (TypeError, ValueError):
                return False
        raise JSException("Right-hand side of 'in' is not an object")

    def _instance_of(self, left, right):
        if right is UNDEFINED or right is None:
            raise JSException("Right-hand side of 'instanceof' is not callable")
        if isinstance(left, list):
            return isinstance(right, _ArrayCtor)
        if isinstance(left, JSRegExp):
            return isinstance(right, _RegExpCtor)
        if isinstance(left, JSDate):
            return isinstance(right, _DateCtor)
        if isinstance(left, JSError):
            return isinstance(right, (_ErrorCtor, JSError))
        if isinstance(left, JSPromise):
            return isinstance(right, JSPromiseCtor)
        if isinstance(left, dict):
            if isinstance(right, JSFunction) and right.prototype is not None:
                node = left
                while isinstance(node, dict):
                    if node.get("__proto__") is right.prototype:
                        return True
                    node = node.get("__proto__")
                return False
            if isinstance(right, dict):
                marker = right.get("__proto__")
                node = left
                while isinstance(node, dict):
                    if node.get("__proto__") is marker:
                        return True
                    node = node.get("__proto__")
        return False

    def _eval_unary(self, node, env):
        if node.op == "void":
            yield from self._eval(node.operand, env)
            return UNDEFINED
        if node.op == "delete":
            target = node.operand
            if isinstance(target, (Member, Index)):
                obj, name = yield from self._lvalue(target, env)
                if isinstance(obj, dict):
                    obj.pop(name, None)
                return True
            return True
        operand = yield from self._eval(node.operand, env)
        if node.op == "!":
            return not self._truthy(operand)
        if node.op == "-":
            return -_to_number(operand)
        if node.op == "~":
            return ~_to_int32(operand)
        if node.op == "typeof":
            return _typeof(operand)
        raise JSException(f"Unknown unary operator '{node.op}'.")

    def _eval_update(self, node, env):
        current = yield from self._read_lvalue(node.operand, env)
        value = _to_number(current) + (1 if node.op == "++" else -1)
        yield from self._write_lvalue(node.operand, env, value)
        return value if node.prefix else current

    def _eval_binary(self, op, left, right):
        if op == "in":
            return self._in_operator(left, right)
        if op == "instanceof":
            return self._instance_of(left, right)
        if op in ("+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>",
                  ">>>"):
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
        if op in ("&", "|", "^", "<<", ">>", ">>>"):
            a = _to_int32(left)
            b = int(_to_number(right)) & 31
            if op == "&":
                return _to_int32(a & _to_int32(right))
            if op == "|":
                return _to_int32(a | _to_int32(right))
            if op == "^":
                return _to_int32(a ^ _to_int32(right))
            if op == "<<":
                return _to_int32(a << b)
            if op == ">>":
                return _to_int32(a >> b)
            return (a & 0xFFFFFFFF) >> b
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
        if node.op == "||=":
            result = current if self._truthy(current) else value
        elif node.op == "&&=":
            result = value if self._truthy(current) else current
        elif node.op == "??=":
            result = current if not _nullish(current) else value
        else:
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
            args = yield from self._eval_args(node.args, env)
            return self._call_value(fn, args, this_arg=obj)
        fn = yield from self._eval(callee_node, env)
        args = yield from self._eval_args(node.args, env)
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
                    stmt.params, stmt.body, env, self, stmt.name, stmt.async_,
                    defaults=stmt.defaults))
        for stmt in statements:
            yield from self._exec(stmt, env)

    def _exec(self, node, env):
        if isinstance(node, Empty):
            return
        if isinstance(node, Block):
            yield from self._exec_block(node.statements, Environment(env))
        elif isinstance(node, VarDecl):
            setter = {"var": env.set_var, "let": env.set_let,
                      "const": env.set_const}[node.kind]
            for target, expr in node.decls:
                if node.kind == "const" and expr is None \
                        and isinstance(target, str):
                    raise JSException(
                        f"Missing initializer in const declaration '{target}'.")
                if expr is None:
                    if isinstance(target, str):
                        setter(target, UNDEFINED)
                    else:
                        yield from self._bind_pattern(
                            target, UNDEFINED, env, setter)
                else:
                    value = yield from self._eval(expr, env)
                    if isinstance(target, str):
                        setter(target, value)
                    else:
                        yield from self._bind_pattern(target, value, env,
                                                      setter)
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
        elif isinstance(node, DoWhile):
            while True:
                try:
                    yield from self._exec(node.body, env)
                except _Break:
                    break
                except _Continue:
                    pass
                if not self._truthy((yield from self._eval(node.cond, env))):
                    break
        elif isinstance(node, For):
            yield from self._exec_for(node, env)
        elif isinstance(node, ForOf):
            yield from self._exec_for_of(node, env)
        elif isinstance(node, ForIn):
            yield from self._exec_for_in(node, env)
        elif isinstance(node, ClassDecl):
            env.set_var(node.name, self._make_class_ctor(node))
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
        elif isinstance(node, Switch):
            yield from self._exec_switch(node, env)
        else:
            raise JSException(f"Unknown statement {type(node).__name__}.")

    def _exec_switch(self, node, env):
        value = yield from self._eval(node.expr, env)
        matched = None
        default_idx = None
        for i, (kind, test, _stmts) in enumerate(node.cases):
            if kind == "default":
                default_idx = i
                continue
            if _strict_eq(value, (yield from self._eval(test, env))):
                matched = i
                break
        if matched is None:
            matched = default_idx
        if matched is None:
            return
        for i in range(matched, len(node.cases)):
            _kind, _test, stmts = node.cases[i]
            try:
                yield from self._exec_block(stmts, Environment(env))
            except _Break:
                return
            except _Continue:
                raise

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

    def _exec_for_of(self, node, env):
        iterable = yield from self._eval(node.iterable, env)
        items = list(iterable) if isinstance(iterable, list) else []
        for item in items:
            child = Environment(env)
            setter = self._pattern_setter(node.kind, child)
            if isinstance(node.var_name, Pattern):
                yield from self._bind_pattern(node.var_name, item, child,
                                              setter)
            else:
                setter(node.var_name, item)
            try:
                yield from self._exec(node.body, child)
            except _Break:
                break
            except _Continue:
                continue

    def _exec_for_in(self, node, env):
        obj = yield from self._eval(node.obj, env)
        for key in self._for_in_keys(obj):
            child = Environment(env)
            setter = self._pattern_setter(node.kind, child)
            if isinstance(node.var_name, Pattern):
                yield from self._bind_pattern(node.var_name, key, child,
                                              setter)
            else:
                setter(node.var_name, key)
            try:
                yield from self._exec(node.body, child)
            except _Break:
                break
            except _Continue:
                continue

    def _pattern_setter(self, kind, env):
        if kind == "let":
            return env.set_let
        if kind == "const":
            return env.set_const
        return env.set_var

    def _bind_pattern(self, pattern, value, env, setter):
        if pattern.kind == "array":
            items = list(value) if isinstance(value, list) else []
            for i, part in enumerate(pattern.parts):
                item_val = items[i] if i < len(items) else UNDEFINED
                if part is not None:
                    target, default = part
                    if _nullish(item_val) and default is not None:
                        item_val = yield from self._eval(default, env)
                    yield from self._bind_target(target, item_val, env, setter)
            if pattern.rest is not None:
                rest_val = items[len(pattern.parts):]
                yield from self._bind_target(pattern.rest, rest_val, env,
                                             setter)
        else:
            obj = value if isinstance(value, dict) else {}
            for key, target, default in pattern.parts:
                v = obj.get(key, UNDEFINED)
                if _nullish(v) and default is not None:
                    v = yield from self._eval(default, env)
                yield from self._bind_target(target, v, env, setter)
            if pattern.rest is not None:
                taken = {p[0] for p in pattern.parts}
                rest_obj = {k: v for k, v in obj.items()
                            if k not in taken and k != "__proto__"}
                yield from self._bind_target(pattern.rest, rest_obj, env,
                                             setter)

    def _bind_target(self, target, value, env, setter):
        if isinstance(target, str):
            setter(target, value)
        else:
            yield from self._bind_pattern(target, value, env, setter)

    def _for_in_keys(self, obj):
        if isinstance(obj, dict):
            return [k for k in obj if k != "__proto__"]
        if isinstance(obj, list):
            return [str(i) for i in range(len(obj))]
        if isinstance(obj, str):
            return [str(i) for i in range(len(obj))]
        return []

    def _make_class_ctor(self, cls):
        ctor_params = []
        ctor_body = None
        proto = {}
        statics = {}
        for m in cls.methods:
            mname, params, body, is_static = m
            if mname == "__construct":
                ctor_params, ctor_body = params, body
            elif is_static:
                statics[mname] = JSFunction(params, body, self._global_env,
                                            self, mname)
            else:
                proto[mname] = JSFunction(params, body, self._global_env,
                                          self, mname)
        ctor = JSFunction(ctor_params or [], ctor_body or [],
                          self._global_env, self, cls.name or "")
        ctor.prototype = proto
        if statics:
            ctor.statics = statics
        return ctor

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