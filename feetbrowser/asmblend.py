"""Span-blend kernels in raw x86-64 assembly, loaded as a shared object.

The rasteriser in this project -- see the raster backend of the rendering
engine -- blends translucent spans with strided ``bytes.translate()`` calls
against cached 256-byte lookup tables: three "C-level" operations per row,
which is how you survive pure Python.  This module is what that is when you
stop pretending.  The same loop, hand-written in the machine's own language
in ``feetbrowser/asm/spanblend.S``, compiled on demand, called through
``ctypes``.  No tables, no translate(), no C.

If there is no compiler, or the host is not Linux/x86-64, the kernels fall
back to identical pure-Python implementations so nothing else breaks.
"""

import os
import sys
import ctypes
import hashlib
import subprocess
import tempfile
import platform

_ASM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asm", "spanblend.S")


def _supports_asm():
    return (platform.system() == "Linux" and sys.maxsize > 2 ** 32
            and platform.machine().lower() in ("x86_64", "amd64"))


def _find_cc():
    for cc in ("cc", "gcc", "clang"):
        try:
            subprocess.run([cc, "--version"], check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return cc
        except (OSError, subprocess.CalledProcessError):
            continue
    return None


def _build_library():
    """Compile spanblend.S to a shared object, cached by source hash."""
    if not _supports_asm():
        return None
    cc = _find_cc()
    if cc is None:
        return None
    with open(_ASM, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()[:16]
    out = os.path.join(tempfile.gettempdir(), f"feetbrowser_spanblend_{digest}.so")
    if not os.path.exists(out):
        tmp = out + ".tmp"
        subprocess.run([cc, "-shared", "-fPIC", "-o", tmp, _ASM],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.replace(tmp, out)
    return out


_lib = None
if os.path.exists(_ASM):
    _path = _build_library()
    if _path is not None:
        _lib = ctypes.CDLL(_path)
        _lib.translate.restype = None
        _lib.translate.argtypes = [ctypes.POINTER(ctypes.c_ubyte)] * 3 \
            + [ctypes.c_size_t]
        _lib.blend_rgb.restype = None
        _lib.blend_rgb.argtypes = [ctypes.POINTER(ctypes.c_ubyte)] * 2 \
            + [ctypes.c_size_t, ctypes.c_uint]
        _lib.fill_span.restype = None
        _lib.fill_span.argtypes = [ctypes.POINTER(ctypes.c_ubyte),
                                   ctypes.c_ubyte, ctypes.c_size_t]


def _as_dst(buf, n):
    if isinstance(buf, ctypes.Array) and buf._type_ is ctypes.c_ubyte:
        return buf
    return (ctypes.c_ubyte * n).from_buffer(buf)


def _as_src(buf, n):
    if isinstance(buf, ctypes.Array) and buf._type_ is ctypes.c_ubyte:
        return buf
    return (ctypes.c_ubyte * n).from_buffer_copy(buf)


def translate(dst, src, table, n):
    """dst[i] = table[src[i]] for i in [0, n)."""
    if _lib is not None:
        return _lib.translate(_as_dst(dst, n), _as_src(src, n),
                              _as_src(table, 256), n)
    for i in range(n):
        dst[i] = table[src[i]]


def blend_rgb(dst, src, n, alpha):
    """Per-channel 8-bit alpha over an RGB run: (src*a + dst*(255-a)) >> 8."""
    if _lib is not None:
        return _lib.blend_rgb(_as_dst(dst, n), _as_src(src, n), n, alpha)
    inv = 255 - alpha
    for i in range(n):
        dst[i] = (src[i] * alpha + dst[i] * inv) >> 8


def fill_span(dst, value, n):
    """dst[0..n) = value."""
    if _lib is not None:
        return _lib.fill_span(_as_dst(dst, n), value, n)
    for i in range(n):
        dst[i] = value


def using_assembly():
    """True when the raw assembly kernels are active, not the fallback."""
    return _lib is not None


__all__ = ["translate", "blend_rgb", "fill_span", "using_assembly"]