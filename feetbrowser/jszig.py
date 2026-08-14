"""The Zig JavaScript engine, loaded over its C ABI with ctypes.

The engine itself lives in ``zig/`` and builds to a plain dynamic library.
Nothing here knows about CPython's C API, which is the point: the same
library works on any Python that can run ctypes, and building it needs a Zig
compiler and nothing else. This is the same arrangement as `cocoa.py` and
`win32.py`, which reach the platform through ctypes rather than a compiled
extension, so the house style is already established.

Values cross the boundary as a small ``CValue`` record -- a tag, a double,
and a pointer/length pair. Ownership is deliberately lopsided:

* Strings coming *out* of the engine point into its scratch buffer and are
  only good until the next call into it, so every read decodes immediately.
* Strings going *in* are borrowed for the duration of the call; the engine
  copies what it keeps. Python only has to hold the bytes object alive until
  the call returns, which `_pin` does.
* Anything that is not a primitive comes out as a handle that roots the value
  until it is released. Every ``js`` handle this module receives is released
  exactly once -- immediately for arrays and objects that get copied into
  Python containers, and in `JSValue.__del__` for everything else.

Python objects going the other way are registered in `_objs` under an integer
handle, and the engine hands that integer back with every property read,
property write and call. The engine tells us when its wrapper for one dies,
and only then do we drop our reference.
"""

import ctypes
import os
import sys
from collections import deque

__all__ = ["Interpreter", "JSException", "JSValue", "UNDEFINED"]


class JSException(Exception):
    """A JavaScript exception that reached Python."""


class _Undefined:
    """The one and only `undefined`.

    JavaScript distinguishes "no value" from "the null value" and the browser
    code above us relies on it, so `None` cannot stand in for both.
    """

    __slots__ = ()

    def __str__(self):
        return "undefined"

    def __repr__(self):
        return "undefined"


UNDEFINED = _Undefined()


# -- the library ------------------------------------------------------------

# Tags shared with `zig/src/vm.zig`.
_T_UNDEF = 0
_T_NULL = 1
_T_BOOL = 2
_T_NUMBER = 3
_T_STRING = 4
_T_HOST = 5
_T_JS = 6
_T_THROW = 7


class CValue(ctypes.Structure):
    _fields_ = [
        ("tag", ctypes.c_int32),
        ("len", ctypes.c_uint32),
        ("num", ctypes.c_double),
        ("ptr", ctypes.c_uint64),
    ]


_P_CV = ctypes.POINTER(CValue)

_GET_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p,
    ctypes.c_uint32, _P_CV)
_SET_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p,
    ctypes.c_uint32, _P_CV)
_CALL_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint64, _P_CV, _P_CV,
    ctypes.c_uint32, _P_CV)
_NEW_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint64, _P_CV, ctypes.c_uint32, _P_CV)
_FREE_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint64)

_lib = None


def library_path():
    """Where the built engine should be, honouring an explicit override."""
    override = os.environ.get("FEETBROWSER_JS_LIB", "").strip()
    if override:
        return override
    if sys.platform == "darwin":
        name = "libfeetjs.dylib"
    elif os.name == "nt":
        name = "feetjs.dll"
    else:
        name = "libfeetjs.so"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "zig", "zig-out", "lib", name)


def load():
    """Open the engine library, once per process."""
    global _lib
    if _lib is not None:
        return _lib
    path = library_path()
    if not os.path.exists(path):
        raise ImportError(
            "the Zig JavaScript engine is not built: %s is missing. "
            "Run `zig build` in zig/, or set FEETBROWSER_JS=rust." % path)
    lib = ctypes.CDLL(path)
    _declare(lib)
    _lib = lib
    return lib


def _declare(lib):
    u32, u64, f64, i32 = (ctypes.c_uint32, ctypes.c_uint64,
                          ctypes.c_double, ctypes.c_int32)
    vp, cp = ctypes.c_void_p, ctypes.c_char_p
    pu32 = ctypes.POINTER(u32)
    sigs = {
        "js_new": ([], vp),
        "js_free": ([vp], None),
        "js_set_host": ([vp, vp, _GET_CB, _SET_CB, _CALL_CB, _NEW_CB,
                         _FREE_CB], None),
        "js_run": ([vp, cp, u32, cp, u32, _P_CV], i32),
        "js_drain": ([vp], None),
        "js_advance": ([vp, f64], None),
        "js_collect": ([vp], None),
        "js_heap_bytes": ([vp], u64),
        "js_log_count": ([vp], u32),
        "js_log_at": ([vp, u32, pu32], vp),
        "js_logs_clear": ([vp], None),
        "js_global_get": ([vp, cp, u32, _P_CV], i32),
        "js_global_set": ([vp, cp, u32, _P_CV], i32),
        "js_global_del": ([vp, cp, u32], None),
        "js_global_has": ([vp, cp, u32], i32),
        "js_global_count": ([vp], u32),
        "js_global_key_at": ([vp, u32, pu32], vp),
        "js_release": ([vp, u64], None),
        "js_class": ([vp, u64], i32),
        "js_get": ([vp, u64, cp, u32, _P_CV], i32),
        "js_set": ([vp, u64, cp, u32, _P_CV], i32),
        "js_length": ([vp, u64], u32),
        "js_index": ([vp, u64, u32, _P_CV], i32),
        "js_key_count": ([vp, u64], u32),
        "js_key_at": ([vp, u64, u32, pu32], vp),
        "js_call": ([vp, u64, _P_CV, _P_CV, u32, _P_CV], i32),
        "js_construct": ([vp, u64, _P_CV, u32, _P_CV], i32),
        "js_new_array": ([vp, _P_CV, u32, _P_CV], i32),
        "js_new_object": ([vp, _P_CV], i32),
        "js_repr": ([vp, u64, pu32], vp),
        "js_promise_new": ([vp, _P_CV], i32),
        "js_promise_settle": ([vp, u64, _P_CV, i32], i32),
        "js_host_value": ([vp, u64, i32, _P_CV], i32),
    }
    for name, (argtypes, restype) in sigs.items():
        fn = getattr(lib, name)
        fn.argtypes = argtypes
        fn.restype = restype


def _text(ptr, length):
    if not ptr or not length:
        return ""
    return ctypes.string_at(ptr, length).decode("utf-8", "replace")


# -- a live JavaScript value ------------------------------------------------

class JSValue:
    """A handle on a JavaScript value Python could not usefully copy.

    Functions, promises, class instances, DOM-ish wrappers: anything that is
    not a primitive, a plain object or an array arrives here. The handle roots
    the value in the engine, so the wrapper must be dropped for the value to
    become collectable -- which is what `__del__` is for.
    """

    __slots__ = ("_interp", "_handle", "__weakref__")

    def __init__(self, interp, handle):
        self._interp = interp
        self._handle = handle

    def js_get(self, name):
        return self._interp._get(self._handle, name)

    def js_set(self, name, value):
        self._interp._set(self._handle, name, value)

    def js_call(self, *args):
        return self._interp._call(self._handle, UNDEFINED, args)

    # A JavaScript function handed to a Python host object arrives here, and
    # host code should not have to know which engine produced it. The Rust
    # engine hands over something Python can call, so this one does too:
    # `callback(a, b)` in the DOM bridge means the same thing either way.
    __call__ = js_call

    def js_new(self, *args):
        return self._interp._construct(self._handle, args)

    def js_repr(self):
        return self._interp._repr_handle(self._handle)

    def resolve(self, value):
        self._interp._settle(self._handle, value, True)

    def reject(self, reason):
        self._interp._settle(self._handle, reason, False)

    def __repr__(self):
        return "<JSValue %s>" % self.js_repr()

    def __del__(self):
        try:
            self._interp._drop(self._handle)
        except Exception:  # noqa: BLE001 - interpreter teardown
            pass


class _Globals:
    """A mapping view of the global object.

    Reads and writes go straight through to the engine, so this stays a live
    view rather than a snapshot; the tests assign a global from Python and
    then read what a script did to it.
    """

    __slots__ = ("_interp",)

    def __init__(self, interp):
        self._interp = interp

    def __getitem__(self, key):
        return self._interp._global_get(str(key))

    def __setitem__(self, key, value):
        self._interp._global_set(str(key), value)

    def __delitem__(self, key):
        i = self._interp
        i._lib.js_global_del(i._vm, str(key).encode("utf-8"),
                             len(str(key).encode("utf-8")))

    def __contains__(self, key):
        i = self._interp
        raw = str(key).encode("utf-8")
        return bool(i._lib.js_global_has(i._vm, raw, len(raw)))

    def __len__(self):
        i = self._interp
        return int(i._lib.js_global_count(i._vm))

    def keys(self):
        i = self._interp
        out = []
        for n in range(int(i._lib.js_global_count(i._vm))):
            ln = ctypes.c_uint32(0)
            p = i._lib.js_global_key_at(i._vm, n, ctypes.byref(ln))
            out.append(_text(p, ln.value))
        return out

    def get(self, key, default=None):
        if key in self:
            return self[key]
        return default

    def __iter__(self):
        return iter(self.keys())

    def __repr__(self):
        return "<JSGlobals>"


# -- the interpreter --------------------------------------------------------

class Interpreter:
    """One JavaScript realm: globals, heap, microtask queue and timers."""

    def __init__(self):
        self._lib = load()
        vm = self._lib.js_new()
        if not vm:
            raise JSException("could not create a JavaScript engine")
        self._vm = vm
        self._log_list = []
        # Python objects the engine holds, by handle.
        self._objs = {}
        self._handles = {}
        self._next_handle = 1
        # A callback fills in its result and returns, and only then does the
        # engine read the bytes and handles that result points at. Python
        # calls into the engine keep those in a local instead; here there is
        # no local to keep, so each callback's batch goes in this ring and is
        # dropped once sixty-four more have come and gone.
        self._pins = deque()
        # ctypes discards a callback the moment nothing references it, and
        # the engine would then call into freed memory, so keep them here.
        self._callbacks = (
            _GET_CB(self._on_get),
            _SET_CB(self._on_set),
            _CALL_CB(self._on_call),
            _NEW_CB(self._on_construct),
            _FREE_CB(self._on_release),
        )
        self._lib.js_set_host(self._vm, None, *self._callbacks)

    def __repr__(self):
        return "<Interpreter>"

    def __del__(self):
        try:
            vm, self._vm = self._vm, None
            if vm:
                self._lib.js_free(vm)
        except Exception:  # noqa: BLE001 - interpreter teardown
            pass

    # -- running -----------------------------------------------------------

    def run(self, source, name="script"):
        src = source.encode("utf-8") if isinstance(source, str) else source
        who = name.encode("utf-8")
        dst = CValue()
        rc = self._lib.js_run(self._vm, src, len(src), who, len(who),
                              ctypes.byref(dst))
        if rc != 0:
            raise JSException(_text(dst.ptr, dst.len) or "error")
        self._from_c(dst)
        return None

    def call(self, *args):
        if not args:
            raise TypeError("call() missing fn argument")
        fn, rest = args[0], args[1:]
        if isinstance(fn, JSValue):
            return self._call(fn._handle, UNDEFINED, rest)
        if callable(fn):
            return fn(*rest)
        raise JSException("value is not a function")

    def create_promise(self):
        dst = CValue()
        if self._lib.js_promise_new(self._vm, ctypes.byref(dst)) != 0:
            raise JSException("could not create a promise")
        return self._from_c(dst)

    def drain(self):
        self._lib.js_drain(self._vm)

    def advance(self, ms):
        self._lib.js_advance(self._vm, float(ms))

    def collect(self):
        self._lib.js_collect(self._vm)

    def heap_bytes(self):
        return int(self._lib.js_heap_bytes(self._vm))

    def repr(self, value):
        """The engine's string form of a value, for logging and XHR bodies."""
        if isinstance(value, JSValue):
            return self._repr_handle(value._handle)
        if value is UNDEFINED:
            return "undefined"
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @property
    def logs(self):
        n = int(self._lib.js_log_count(self._vm))
        for i in range(n):
            ln = ctypes.c_uint32(0)
            p = self._lib.js_log_at(self._vm, i, ctypes.byref(ln))
            self._log_list.append(_text(p, ln.value))
        if n:
            self._lib.js_logs_clear(self._vm)
        return self._log_list

    @property
    def globals(self):
        return _Globals(self)

    # -- value plumbing ----------------------------------------------------

    def _pin(self, keep):
        """Hand a callback's batch of borrowed buffers to the ring."""
        self._pins.append(keep)
        if len(self._pins) > 64:
            self._pins.popleft()

    def _drop(self, handle):
        if self._vm:
            self._lib.js_release(self._vm, handle)

    def _handle_for(self, obj):
        key = id(obj)
        found = self._handles.get(key)
        if found is not None:
            return found
        handle = self._next_handle
        self._next_handle += 1
        self._objs[handle] = obj
        self._handles[key] = handle
        return handle

    def _to_c(self, value, dst, keep):
        """Fill `dst` with the engine's view of a Python value.

        `keep` collects everything `dst` merely points at -- encoded strings
        the engine borrows, and handles rooting temporary arrays and objects.
        The caller drops it once the engine has read the value, and not one
        moment sooner.
        """
        if value is UNDEFINED:
            dst.tag = _T_UNDEF
            return
        if value is None:
            dst.tag = _T_NULL
            return
        if isinstance(value, bool):
            dst.tag = _T_BOOL
            dst.num = 1.0 if value else 0.0
            return
        if isinstance(value, (int, float)):
            dst.tag = _T_NUMBER
            dst.num = float(value)
            return
        if isinstance(value, str):
            raw = value.encode("utf-8")
            keep.append(raw)
            dst.tag = _T_STRING
            dst.ptr = ctypes.cast(raw, ctypes.c_void_p).value
            dst.len = len(raw)
            return
        if isinstance(value, JSValue):
            dst.tag = _T_JS
            dst.ptr = value._handle
            keep.append(value)
            return
        if isinstance(value, (list, tuple)):
            items = (CValue * max(len(value), 1))()
            for i, item in enumerate(value):
                self._to_c(item, items[i], keep)
            out = CValue()
            if self._lib.js_new_array(self._vm, items, len(value),
                                      ctypes.byref(out)) != 0:
                dst.tag = _T_UNDEF
                return
            # The wrapper roots the new array; dropping `keep` releases it.
            keep.append(JSValue(self, out.ptr))
            dst.tag = out.tag
            dst.ptr = out.ptr
            return
        if isinstance(value, dict):
            out = CValue()
            if self._lib.js_new_object(self._vm, ctypes.byref(out)) != 0:
                dst.tag = _T_UNDEF
                return
            keep.append(JSValue(self, out.ptr))
            for k, v in value.items():
                self._set(out.ptr, str(k), v)
            dst.tag = out.tag
            dst.ptr = out.ptr
            return
        # Everything else stays in Python and is reached through callbacks.
        dst.tag = _T_HOST
        dst.ptr = self._handle_for(value)
        dst.num = 1.0 if _host_callable(value) else 0.0

    def _from_c(self, c):
        """Turn the engine's view of a value into a Python one.

        Arrays become lists and plain objects become dicts, matching what the
        browser code already expects; everything else keeps its identity as a
        `JSValue` so that calling it, or comparing it, still works.
        """
        tag = c.tag
        if tag == _T_UNDEF:
            return UNDEFINED
        if tag == _T_NULL:
            return None
        if tag == _T_BOOL:
            return c.num != 0
        if tag == _T_NUMBER:
            return c.num
        if tag == _T_STRING:
            return _text(c.ptr, c.len)
        if tag == _T_HOST:
            return self._objs.get(c.ptr, UNDEFINED)
        if tag == _T_THROW:
            raise JSException(_text(c.ptr, c.len) or "error")
        if tag != _T_JS:
            return UNDEFINED
        handle = c.ptr
        kind = self._lib.js_class(self._vm, handle)
        if kind == 1:
            out = []
            for i in range(int(self._lib.js_length(self._vm, handle))):
                item = CValue()
                if self._lib.js_index(self._vm, handle, i,
                                      ctypes.byref(item)) != 0:
                    break
                out.append(self._from_c(item))
            self._drop(handle)
            return out
        if kind == 2:
            out = {}
            for i in range(int(self._lib.js_key_count(self._vm, handle))):
                ln = ctypes.c_uint32(0)
                p = self._lib.js_key_at(self._vm, handle, i, ctypes.byref(ln))
                key = _text(p, ln.value)
                out[key] = self._get(handle, key)
            self._drop(handle)
            return out
        return JSValue(self, handle)

    # -- operations on a handle -------------------------------------------

    def _get(self, handle, name):
        raw = str(name).encode("utf-8")
        dst = CValue()
        if self._lib.js_get(self._vm, handle, raw, len(raw),
                            ctypes.byref(dst)) != 0:
            raise JSException(_text(dst.ptr, dst.len) or "error")
        return self._from_c(dst)

    def _set(self, handle, name, value):
        raw = str(name).encode("utf-8")
        src = CValue()
        keep = []
        self._to_c(value, src, keep)
        if self._lib.js_set(self._vm, handle, raw, len(raw),
                            ctypes.byref(src)) != 0:
            raise JSException("could not set %s" % name)

    def _call(self, handle, this, args):
        argv = (CValue * max(len(args), 1))()
        keep = []
        for i, a in enumerate(args):
            self._to_c(a, argv[i], keep)
        recv = CValue()
        self._to_c(this, recv, keep)
        dst = CValue()
        if self._lib.js_call(self._vm, handle, ctypes.byref(recv), argv,
                             len(args), ctypes.byref(dst)) != 0:
            raise JSException(_text(dst.ptr, dst.len) or "error")
        return self._from_c(dst)

    def _construct(self, handle, args):
        argv = (CValue * max(len(args), 1))()
        keep = []
        for i, a in enumerate(args):
            self._to_c(a, argv[i], keep)
        dst = CValue()
        if self._lib.js_construct(self._vm, handle, argv, len(args),
                                  ctypes.byref(dst)) != 0:
            raise JSException(_text(dst.ptr, dst.len) or "error")
        return self._from_c(dst)

    def _repr_handle(self, handle):
        ln = ctypes.c_uint32(0)
        p = self._lib.js_repr(self._vm, handle, ctypes.byref(ln))
        return _text(p, ln.value)

    def _settle(self, handle, value, ok):
        src = CValue()
        keep = []
        self._to_c(value, src, keep)
        self._lib.js_promise_settle(self._vm, handle, ctypes.byref(src),
                                    1 if ok else 0)

    def _global_get(self, name):
        raw = name.encode("utf-8")
        dst = CValue()
        if self._lib.js_global_get(self._vm, raw, len(raw),
                                   ctypes.byref(dst)) != 0:
            raise JSException(_text(dst.ptr, dst.len) or "error")
        return self._from_c(dst)

    def _global_set(self, name, value):
        raw = name.encode("utf-8")
        src = CValue()
        keep = []
        self._to_c(value, src, keep)
        if self._lib.js_global_set(self._vm, raw, len(raw),
                                   ctypes.byref(src)) != 0:
            raise JSException("could not set global %s" % name)

    # -- callbacks the engine makes into us --------------------------------
    #
    # None of these may let an exception escape: the engine is C code and a
    # Python exception crossing it would be swallowed at best. A failure
    # becomes a `throw` tag, which the engine turns back into a JavaScript
    # exception at the point of the property access or the call.

    def _fail(self, dst, message):
        raw = str(message).encode("utf-8") or b"error"
        self._pin([raw])
        dst.tag = _T_THROW
        dst.ptr = ctypes.cast(raw, ctypes.c_void_p).value
        dst.len = len(raw)

    def _reply(self, value, out):
        keep = []
        self._to_c(value, out, keep)
        self._pin(keep)

    def _on_get(self, _ctx, handle, name_ptr, name_len, dst):
        out = dst.contents
        try:
            obj = self._objs.get(handle)
            if obj is None:
                out.tag = _T_UNDEF
                return
            name = _text(name_ptr, name_len)
            getter = getattr(obj, "js_get", None)
            if getter is not None:
                self._reply(getter(name), out)
                return
            item = getattr(obj, "__getitem__", None)
            if item is None:
                out.tag = _T_UNDEF
                return
            try:
                value = item(name)
            except Exception:  # noqa: BLE001 - a missing key is `undefined`
                out.tag = _T_UNDEF
                return
            self._reply(value, out)
        except Exception as e:  # noqa: BLE001 - must not cross the boundary
            self._fail(out, e)

    def _on_set(self, _ctx, handle, name_ptr, name_len, src):
        try:
            obj = self._objs.get(handle)
            if obj is None:
                return
            setter = getattr(obj, "js_set", None)
            if setter is None:
                return
            setter(_text(name_ptr, name_len), self._from_c(src.contents))
        except Exception:  # noqa: BLE001 - a failed write is not fatal
            pass

    def _on_call(self, _ctx, handle, this, argv, argc, dst):
        out = dst.contents
        try:
            # The receiver arrives rooted; nothing here wants it, and letting
            # it through the usual conversion is what releases that root.
            self._from_c(this.contents)
            obj = self._objs.get(handle)
            if obj is None:
                out.tag = _T_UNDEF
                return
            args = [self._from_c(argv[i]) for i in range(argc)]
            fn = getattr(obj, "js_call", None)
            if fn is None:
                if not callable(obj):
                    self._fail(out, "host value is not a function")
                    return
                fn = obj
            self._reply(fn(*args), out)
        except Exception as e:  # noqa: BLE001 - must not cross the boundary
            self._fail(out, e)

    def _on_construct(self, _ctx, handle, argv, argc, dst):
        out = dst.contents
        try:
            obj = self._objs.get(handle)
            if obj is None:
                out.tag = _T_UNDEF
                return
            args = [self._from_c(argv[i]) for i in range(argc)]
            fn = getattr(obj, "js_new", None) or getattr(obj, "js_call", None)
            if fn is None:
                if not callable(obj):
                    self._fail(out, "host value is not a constructor")
                    return
                fn = obj
            self._reply(fn(*args), out)
        except Exception as e:  # noqa: BLE001 - must not cross the boundary
            self._fail(out, e)

    def _on_release(self, _ctx, handle):
        try:
            obj = self._objs.pop(handle, None)
            if obj is not None:
                self._handles.pop(id(obj), None)
        except Exception:  # noqa: BLE001 - teardown
            pass


def _host_callable(obj):
    return (callable(obj) or hasattr(obj, "js_call")
            or hasattr(obj, "js_new"))
