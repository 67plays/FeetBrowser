"""Image-codec kernels in raw x86-64 assembly, loaded as a shared object.

The PNG decoder and the image resampler in this project -- see
``feetbrowser/imagecodec.py`` -- expand packed samples and reverse the
per-scanline filters with per-pixel Python loops.  This module is what that
is when you stop pretending.  The same loops, hand-written in the machine's
own language in ``feetbrowser/asm/pixelops.S``, compiled on demand, called
through ``ctypes``.

The compilation, the on-disk cache and the host/compiler checks all live in
``feetbrowser/asmblend.load_assembly``, which builds every kernel module in
the project; this module is only the ctypes plumbing and the fallbacks.

If there is no compiler, or the host is not Linux/x86-64, the kernels fall
back to identical pure-Python implementations so nothing else breaks.
"""

import os
import ctypes

from .asmblend import load_assembly

_ASM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asm", "pixelops.S")

_lib = None
if os.path.exists(_ASM):
    _lib = load_assembly(_ASM)
    if _lib is not None:
        _lib.unfilter_line.restype = None
        _lib.unfilter_line.argtypes = [ctypes.POINTER(ctypes.c_ubyte)] * 2 \
            + [ctypes.c_size_t, ctypes.c_uint, ctypes.c_uint]
        _lib.palette_expand.restype = None
        _lib.palette_expand.argtypes = [ctypes.POINTER(ctypes.c_ubyte)] * 2 \
            + [ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint,
               ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint]
        _lib.grey_expand.restype = None
        _lib.grey_expand.argtypes = [ctypes.POINTER(ctypes.c_ubyte)] * 2 \
            + [ctypes.c_size_t, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        _lib.grey_alpha_expand.restype = None
        _lib.grey_alpha_expand.argtypes = [ctypes.POINTER(ctypes.c_ubyte)] * 2 \
            + [ctypes.c_size_t]
        _lib.rgb_expand.restype = None
        _lib.rgb_expand.argtypes = [ctypes.POINTER(ctypes.c_ubyte)] * 2 \
            + [ctypes.c_size_t, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
               ctypes.c_uint]
        _lib.resize_row.restype = None
        _lib.resize_row.argtypes = [ctypes.POINTER(ctypes.c_ubyte)] * 2 \
            + [ctypes.POINTER(ctypes.c_uint), ctypes.c_size_t]


def _as_dst(buf, n):
    if isinstance(buf, ctypes.Array) and buf._type_ is ctypes.c_ubyte:
        return buf
    return (ctypes.c_ubyte * n).from_buffer(buf)


def _as_src(buf, n):
    if isinstance(buf, ctypes.Array) and buf._type_ is ctypes.c_ubyte:
        return buf
    return (ctypes.c_ubyte * n).from_buffer_copy(buf)


def _as_xmap(xmap, n):
    if isinstance(xmap, ctypes.Array) and xmap._type_ is ctypes.c_uint:
        return xmap
    return (ctypes.c_uint * n)(*xmap)


def unfilter_line(line, prev, n, bpp, ftype):
    """Reverse one PNG scanline filter in place, masking every byte to 0xFF."""
    if _lib is not None:
        return _lib.unfilter_line(_as_dst(line, n), _as_src(prev, n),
                                  n, bpp, ftype)
    if ftype == 1:      # Sub
        for i in range(bpp, n):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif ftype == 2:    # Up
        for i in range(n):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif ftype == 3:    # Average
        for i in range(n):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:    # Paeth
        for i in range(n):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = (line[i] + pred) & 0xFF


def palette_expand(dst, src, n, palette, pal_len, trns, trns_len):
    """Indexed samples to RGBA; out-of-palette indices go black."""
    if _lib is not None:
        return _lib.palette_expand(_as_dst(dst, n * 4), _as_src(src, n), n,
                                   _as_src(palette, pal_len), pal_len,
                                   _as_src(trns, trns_len), trns_len)
    for i in range(n):
        idx = src[i]
        o = idx * 3
        d = i * 4
        if o + 2 < pal_len:
            dst[d] = palette[o]
            dst[d + 1] = palette[o + 1]
            dst[d + 2] = palette[o + 2]
        dst[d + 3] = trns[idx] if idx < trns_len else 255


def grey_expand(dst, src, n, scale, key, has_key):
    """Greyscale samples to RGBA, stretched by `scale`, key-transparent."""
    if _lib is not None:
        return _lib.grey_expand(_as_dst(dst, n * 4), _as_src(src, n), n,
                                scale, key, has_key)
    for i in range(n):
        v = (src[i] * scale) & 0xFF
        d = i * 4
        dst[d] = dst[d + 1] = dst[d + 2] = v
        dst[d + 3] = 0 if (has_key and src[i] == key) else 255


def grey_alpha_expand(dst, src, n):
    """Grey + alpha samples to RGBA."""
    if _lib is not None:
        return _lib.grey_alpha_expand(_as_dst(dst, n * 4), _as_src(src, n * 2), n)
    for i in range(n):
        s, d = i * 2, i * 4
        v = src[s]
        dst[d] = dst[d + 1] = dst[d + 2] = v
        dst[d + 3] = src[s + 1]


def rgb_expand(dst, src, n, kr, kg, kb, has_key):
    """Truecolour samples to RGBA, transparent on an exact RGB key match."""
    if _lib is not None:
        return _lib.rgb_expand(_as_dst(dst, n * 4), _as_src(src, n * 3), n,
                               kr, kg, kb, has_key)
    for i in range(n):
        s, d = i * 3, i * 4
        r, g, b = src[s], src[s + 1], src[s + 2]
        dst[d], dst[d + 1], dst[d + 2] = r, g, b
        dst[d + 3] = 0 if (has_key and r == kr and g == kg and b == kb) else 255


def resize_row(dst, src, xmap, n):
    """Copy 4 bytes from src + xmap[i] to dst + i*4 for i in [0, n)."""
    if _lib is not None:
        return _lib.resize_row(_as_dst(dst, n * 4), _as_src(src, len(src)),
                               _as_xmap(xmap, n), n)
    for i in range(n):
        s = xmap[i]
        d = i * 4
        dst[d] = src[s]
        dst[d + 1] = src[s + 1]
        dst[d + 2] = src[s + 2]
        dst[d + 3] = src[s + 3]


def using_assembly():
    """True when the raw assembly kernels are active, not the fallback."""
    return _lib is not None


__all__ = ["unfilter_line", "palette_expand", "grey_expand",
           "grey_alpha_expand", "rgb_expand", "resize_row", "using_assembly"]