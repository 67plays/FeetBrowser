"""A from-scratch JavaScript engine for FeetBrowser.

A hand-written lexer, recursive-descent parser, and tree-walking
interpreter for a practical subset of ECMAScript.

The value model is shared with the DOM bridge (feetbrowser/jsdom.py):

    number     -> Python int or float
    string     -> Python str
    boolean    -> Python bool
    null       -> Python None
    undefined  -> the module singleton `UNDEFINED`
    array      -> Python list
    object     -> Python dict with str keys, or any "host object"
    function   -> JSFunction, or any Python callable (native)
    void       -> UNDEFINED
"""

import re


class JSException(Exception):
    """Raised for any JavaScript-level error: syntax or runtime."""


class _Undefined:
    __slots__ = ()

    def __repr__(self):
        return "undefined"


#: The singleton representing the JS `undefined` value.
UNDEFINED = _Undefined()


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
    if isinstance(value, (int, float, str)):
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


class _Return(BaseException):
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class _Break(BaseException):
    __slots__ = ()


class _Continue(BaseException):
    __slots__ = ()


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


class JSFunction:
    """A JavaScript closure: a function declaration or expression."""

    def __init__(self, params, body, env, interp, name=""):
        self.params = params
        self.body = body
        self.env = env
        self.interp = interp
        self.name = name

    def __repr__(self):
        return f"function {self.name}()"


class _Parser:
    """Tokenizes and parses a JS program into an AST of tuples."""

    KEYWORDS = {
        "var", "let", "const", "function", "return", "if", "else",
        "while", "for", "break", "continue",
        "true", "false", "null", "undefined", "typeof",
    }

    #: Statement parser per leading keyword; the keyword is consumed first.
    _STMT = {
        "var": lambda s: s._declaration("var"),
        "let": lambda s: s._declaration("let"),
        "const": lambda s: s._declaration("const"),
        "function": lambda s: s._function_declaration(),
        "return": lambda s: s._return_statement(),
        "if": lambda s: s._if_statement(),
        "while": lambda s: s._while_statement(),
        "for": lambda s: s._for_statement(),
        "break": lambda s: ("break",),
        "continue": lambda s: ("continue",),
    }

    def __init__(self, source):
        self.source = source
        self.tokens = self._tokenize(source)
        self.pos = 0

    # -- tokenizer ----------------------------------------------------------

    def _tokenize(self, source):
        tokens = []
        i, n = 0, len(source)
        while i < n:
            ch = source[i]
            if ch in " \t\r\n":
                i += 1
            elif source.startswith("//", i):
                nl = source.find("\n", i)
                i = n if nl == -1 else nl + 1
            elif source.startswith("/*", i):
                end = source.find("*/", i + 2)
                if end == -1:
                    self._fail(i, "unterminated block comment")
                i = end + 2
            elif ch in "0123456789" or (
                    ch == "." and i + 1 < n and source[i + 1] in "0123456789"):
                j = i
                while j < n and source[j] in "0123456789":
                    j += 1
                if j < n and source[j] == ".":
                    j += 1
                    while j < n and source[j] in "0123456789":
                        j += 1
                tokens.append(("number", _parse_number(source[i:j]), i))
                i = j
            elif ch in ('"', "'"):
                quote = ch
                i += 1
                buf = []
                while True:
                    if i >= n:
                        self._fail(i, "unterminated string literal")
                    c = source[i]
                    if c == "\\":
                        i += 1
                        if i >= n:
                            self._fail(i, "unterminated string literal")
                        esc = source[i]
                        i += 1
                        if esc in _SIMPLE_ESC:
                            buf.append(_SIMPLE_ESC[esc])
                        elif esc in "xu":
                            size = 4 if esc == "u" else 2
                            try:
                                buf.append(chr(int(source[i:i + size], 16)))
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
                while j < n and (source[j].isalnum() or source[j] in "_$"):
                    j += 1
                word = source[i:j]
                kind = "kw" if word in self.KEYWORDS else "ident"
                tokens.append((kind, word, i))
                i = j
            elif ch in "{}()[];,.:?!<>=+-*/%&|^~@#":
                matched = False
                for length, text in _PUNCT:
                    if length <= n - i and source[i:i + length] == text:
                        tokens.append(("punct", text, i))
                        i += length
                        matched = True
                        break
                if not matched:
                    # Lone & | ^ ~ @ # (or any operator the table lacks) must
                    # still consume a character, else the loop never advances.
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

    # -- token helpers ------------------------------------------------------

    def _peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
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

    def _syntax(self, msg):
        self._fail(self._peek()[2], msg)

    # -- grammar ------------------------------------------------------------

    def parse_program(self):
        return self._parse_stmts_until(None)

    def _statement(self):
        kind, value, _ = self._peek()
        if kind == "punct" and value == "{":
            return self._block()
        if kind == "kw" and value in self._STMT:
            self.pos += 1
            return self._STMT[value](self)
        return ("expr", self._expression())

    def _block(self):
        return ("block", self._parse_stmts_until("}"))

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

    def _declaration(self, kind):
        decls = []
        while True:
            name = self._expect_ident()
            value = None
            if self._match_punct("="):
                value = self._expression()
            decls.append((name, value))
            if self._match_punct(",") is None:
                break
        return (kind, decls)

    def _function_declaration(self):
        name = self._expect_ident()
        params, body = self._function_rest()
        return ("function_decl", name, params, body)

    def _function_rest(self):
        params = self._list("(", ")", self._expect_ident)
        body = self._parse_stmts_until("}")
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
            return ("return", self._expression())
        return ("return", None)

    def _if_statement(self):
        cond, then = self._cond_body()
        else_stmt = self._statement() if self._match_kw("else") else None
        return ("if", cond, then, else_stmt)

    def _while_statement(self):
        cond, body = self._cond_body()
        return ("while", cond, body)

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
                init = self._declaration(value)
            else:
                init = ("expr", self._expression())
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
        return ("for", init, cond, update, body)

    # -- expressions --------------------------------------------------------

    def _expression(self):
        return self._assign()

    def _assign(self):
        left = self._conditional()
        kind, value, _ = self._peek()
        if kind == "punct" and value in ("=", "+=", "-=", "*=", "/="):
            self.pos += 1
            right = self._assign()
            if left[0] not in ("ident", "member", "index"):
                self._syntax("invalid assignment target")
            if value == "=":
                return ("assign", left, right)
            return ("compound", value, left, right)
        return left

    def _conditional(self):
        cond = self._or()
        if self._match_punct("?"):
            then = self._assign()
            self._expect_punct(":")
            else_expr = self._assign()
            return ("conditional", cond, then, else_expr)
        return cond

    def _or(self):
        return self._chain("or", "||", self._and)

    def _and(self):
        return self._chain("and", "&&", self._equality)

    def _chain(self, kind, punct, sub):
        node = sub()
        while self._match_punct(punct):
            right = sub()
            node = (kind, node, right)
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
            node = ("binary", node, value, sub())
        return node

    def _punct_in(self, texts):
        kind, value, _ = self._peek()
        if kind == "punct" and value in texts:
            self.pos += 1
            return value
        return None

    def _unary(self):
        kind, value, _ = self._peek()
        if (kind == "punct" and value in ("!", "-", "++", "--")) or (
                kind == "kw" and value == "typeof"):
            self.pos += 1
            return (("pre_update" if value in ("++", "--") else "unary"),
                    value, self._unary())
        return self._call()

    def _call(self):
        node = self._primary()
        while True:
            if self._match_punct("("):
                node = ("call", node, self._args())
            elif self._match_punct("."):
                node = ("member", node, self._expect_ident())
            elif self._match_punct("["):
                index = self._expression()
                self._expect_punct("]")
                node = ("index", node, index)
            elif self._match_punct("++"):
                node = ("post_update", "++", node)
            elif self._match_punct("--"):
                node = ("post_update", "--", node)
            else:
                break
        return node

    def _args(self):
        return self._list(None, ")", self._expression)

    def _primary(self):
        kind, value, _ = self._peek()
        if kind in ("number", "string"):
            self.pos += 1
            return (kind, value)
        if kind == "kw":
            self.pos += 1
            if value in ("true", "false", "null", "undefined"):
                return (value,)
            if value == "function":
                return self._function_expression()
            self._syntax(f"unexpected keyword '{value}'")
        if kind == "ident":
            self.pos += 1
            return ("ident", value)
        if kind == "punct":
            if value == "(":
                self.pos += 1
                node = self._expression()
                self._expect_punct(")")
                return node
            if value == "[":
                return self._array_literal()
            if value == "{":
                return self._object_literal()
        self._syntax("unexpected token")

    def _function_expression(self):
        name = None
        kind, value, _ = self._peek()
        if kind == "ident":
            self.pos += 1
            name = value
        params, body = self._function_rest()
        return ("function", name, params, body)

    def _array_literal(self):
        return ("array", self._list("[", "]", self._expression, trailing=True))

    def _object_literal(self):
        return ("object", self._list("{", "}", self._object_pair, trailing=True))

    def _object_pair(self):
        kind, value, _ = self._peek()
        if kind in ("ident", "string"):
            self.pos += 1
            key = value
        else:
            self._syntax("expected property name")
        self._expect_punct(":")
        return key, self._expression()


# Longest match first, so tokenizer greedily groups '===', '!=', etc.
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


class Interpreter:
    """Parses and executes JavaScript against a shared global scope."""

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
                base = 16 if hexp else 8 if text.startswith("0") and len(text) > 1 else 10
            prefix_len = 2 if base == 16 and hexp else 0
            digits = 0
            for ch in text[prefix_len:]:
                if ch.lower() in "0123456789abcdefghijklmnopqrstuvwxyz"[:base]:
                    digits += 1
                else:
                    break
            return float("nan") if digits == 0 else int(text[:prefix_len + digits], base)

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
            "document": UNDEFINED,
            "window": UNDEFINED,
        }
        self._global_env = Environment()
        self._global_env.vars = self.globals

    def run(self, source):
        """Parse and execute a whole program statement-by-statement."""
        statements = _Parser(source).parse_program()
        try:
            self._exec_block(statements, self._global_env)
        except (_Return, _Break, _Continue):
            raise JSException("Illegal statement outside its context.") from None
        except Exception as exc:
            raise (exc if isinstance(exc, JSException)
                   else JSException(str(exc))) from None

    def call(self, fn, *args):
        """Call a JSFunction, a plain Python callable, or a host object."""
        try:
            return self._call_value(fn, list(args))
        except Exception as exc:
            raise (exc if isinstance(exc, JSException)
                   else JSException(str(exc))) from None

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
        if _is_objectish(value):
            return "[object Object]"
        return str(value)

    # -- host-object member access -------------------------------------------

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
        return self._member_tail(obj, name, write=True, value=value)

    def _member_tail(self, obj, name, write=False, value=None):
        if _nullish(obj) or isinstance(obj, (str, int, float, bool, JSFunction)):
            return UNDEFINED if not write else None
        method = getattr(obj, "js_set" if write else "js_get", None)
        if method is not None:
            try:
                result = method(str(name)) if not write else method(str(name), value)
            except Exception as exc:
                raise (exc if isinstance(exc, JSException)
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

    # -- native array members ------------------------------------------------

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

    # -- evaluation -----------------------------------------------------------

    def _to_js(self, value):
        return value

    def _call_value(self, fn, args):
        if isinstance(fn, JSFunction):
            return self._call_function(fn, args)
        if fn is UNDEFINED or fn is None:
            raise JSException(f"{self.repr(fn)} is not a function.")
        try:
            if hasattr(fn, "js_call"):
                return self._to_js(fn.js_call(*args))
            if callable(fn):
                return self._to_js(fn(*args))
        except Exception as exc:
            raise (exc if isinstance(exc, JSException)
                   else JSException(str(exc))) from None
        raise JSException(f"{self.repr(fn)} is not a function.")

    def _call_function(self, fn, args):
        scope = Environment(fn.env)
        scope.function_scope = scope  # private var scope per invocation
        for name, value in zip(fn.params, args):
            scope.set_var(name, value)
        try:
            self._exec_block(fn.body, scope)
        except _Return as ret:
            return ret.value
        except (_Break, _Continue):
            raise JSException("Break or continue outside of a loop.") from None
        except Exception as exc:
            raise (exc if isinstance(exc, JSException)
                   else JSException(str(exc))) from None
        return UNDEFINED

    def _exec_block(self, statements, env):
        for stmt in statements:
            if stmt[0] == "function_decl":
                env.set_var(
                    stmt[1], JSFunction(stmt[2], stmt[3], env, self, stmt[1]))
        for stmt in statements:
            self._exec(stmt, env)

    def _exec(self, node, env):
        kind = node[0]
        if kind == "block":
            self._exec_block(node[1], Environment(env))
        elif kind in ("var", "let", "const"):
            setter = {"var": env.set_var, "let": env.set_let,
                      "const": env.set_const}[kind]
            for name, expr in node[1]:
                if kind == "const" and expr is None:
                    raise JSException(
                        f"Missing initializer in const declaration '{name}'.")
                setter(name, UNDEFINED if expr is None else self._eval(expr, env))
        elif kind == "function_decl":
            pass  # hoisted by _exec_block
        elif kind == "return":
            raise _Return(
                UNDEFINED if node[1] is None else self._eval(node[1], env))
        elif kind == "break":
            raise _Break()
        elif kind == "continue":
            raise _Continue()
        elif kind == "if":
            if self._truthy(self._eval(node[1], env)):
                self._exec(node[2], env)
            elif node[3] is not None:
                self._exec(node[3], env)
        elif kind == "while":
            while self._truthy(self._eval(node[1], env)):
                try:
                    self._exec(node[2], env)
                except _Break:
                    break
                except _Continue:
                    continue
        elif kind == "for":
            self._exec_for(node, env)
        elif kind == "expr":
            self._eval(node[1], env)
        else:
            raise JSException(f"Unknown statement '{kind}'.")

    def _exec_for(self, node, env):
        init, cond, update, body = node[1], node[2], node[3], node[4]
        child = Environment(env)
        if init is not None:
            self._exec(init, child)
        while cond is None or self._truthy(self._eval(cond, child)):
            try:
                self._exec(body, child)
            except _Break:
                break
            except _Continue:
                pass
            if update is not None:
                self._eval(update, child)

    def _truthy(self, value):
        if value is False or value is UNDEFINED or value is None:
            return False
        if isinstance(value, (int, float)):
            return value != 0 and value == value  # NaN is falsy
        return value != ""

    def _eval(self, node, env):
        kind = node[0]
        if kind in ("number", "string"):
            return node[1]
        if kind == "true":
            return True
        if kind == "false":
            return False
        if kind == "null":
            return None
        if kind == "undefined":
            return UNDEFINED
        if kind == "ident":
            name = node[1]
            value = env.get(name)
            if value is UNDEFINED and name in self.globals:
                return self.globals[name]
            return value
        if kind == "array":
            return [self._eval(item, env) for item in node[1]]
        if kind == "object":
            return {key: self._eval(expr, env) for key, expr in node[1]}
        if kind == "function":
            return JSFunction(node[2], node[3], env, self, node[1] or "")
        if kind == "assign":
            return self._eval_assign(node, env)
        if kind == "compound":
            return self._eval_compound(node, env)
        if kind == "conditional":
            return self._eval_conditional(node, env)
        if kind in ("or", "and"):
            left = self._eval(node[1], env)
            if self._truthy(left) == (kind == "or"):
                return left
            return self._eval(node[2], env)
        if kind == "binary":
            return self._eval_binary(node, env)
        if kind in ("unary", "pre_update", "post_update"):
            return self._eval_unary(node, env)
        if kind == "call":
            callee = self._eval(node[1], env)
            args = [self._eval(arg, env) for arg in node[2]]
            return self._call_value(callee, args)
        if kind in ("member", "index"):
            obj = self._eval(node[1], env)
            name = (node[2] if kind == "member"
                    else self._index_name(self._eval(node[2], env)))
            return self.js_get(obj, name)
        raise JSException(f"Unknown expression '{kind}'.")

    def _index_name(self, value):
        return value if isinstance(value, str) else self.repr(value)

    def _eval_assign(self, node, env):
        target, value = node[1], self._eval(node[2], env)
        obj, name = self._lvalue(target, env)
        if obj is None:
            env.assign(name, value)
        else:
            self.js_set(obj, name, value)
        return value

    def _eval_compound(self, node, env):
        op, target, value = node[1], node[2], self._eval(node[3], env)
        obj, name = self._lvalue(target, env)
        current = env.get(name) if obj is None else self.js_get(obj, name)
        result = self._binary_op(op[0], current, value)
        if obj is None:
            env.assign(name, result)
        else:
            self.js_set(obj, name, result)
        return result

    def _lvalue(self, target, env):
        """Resolve an assignment target to (obj, name); obj is None for idents."""
        if target[0] == "ident":
            return None, target[1]
        obj = self._eval(target[1], env)
        return obj, (target[2] if target[0] == "member"
                     else self._index_name(self._eval(target[2], env)))

    def _eval_conditional(self, node, env):
        cond, then, else_expr = node[1], node[2], node[3]
        if self._truthy(self._eval(cond, env)):
            return self._eval(then, env)
        return self._eval(else_expr, env)

    def _eval_binary(self, node, env):
        left = self._eval(node[1], env)
        right = self._eval(node[3], env)
        op = node[2]
        if op in ("+", "-", "*", "/", "%"):
            return self._binary_op(op, left, right)
        return self._compare(op, left, right)

    def _compare(self, op, left, right):
        if op in ("==", "!="):
            result = _loose_eq(left, right)
        elif op in ("===", "!=="):
            result = _strict_eq(left, right)
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
        return not result if op[0] == "!" else result

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
        raise JSException(f"Unknown compound operator '{op}'.")

    def _eval_unary(self, node, env):
        kind, op, operand = node[0], node[1], node[2]
        if kind in ("pre_update", "post_update"):
            current = self._read_lvalue(operand, env)
            value = _to_number(current) + (1 if op == "++" else -1)
            self._write_lvalue(operand, env, value)
            return value if kind == "pre_update" else current
        value = self._eval(operand, env)
        if op == "!":
            return not self._truthy(value)
        if op == "-":
            return -_to_number(value)
        if op == "typeof":
            return _typeof(value)
        raise JSException(f"Unknown unary operator '{op}'.")

    def _read_lvalue(self, target, env):
        obj, name = self._lvalue(target, env)
        return env.get(name) if obj is None else self.js_get(obj, name)

    def _write_lvalue(self, target, env, value):
        obj, name = self._lvalue(target, env)
        if obj is None:
            env.assign(name, value)
        else:
            self.js_set(obj, name, value)