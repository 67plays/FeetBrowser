"""The X11 framebuffer-packing kernel in raw x86-64 assembly, ctypes-loaded.

The Linux window backend in this project -- see ``feetbrowser/x11.py`` --
converts the browser's packed RGB framebuffer into the X server's Z-format
through ``_pack_generic`` whenever the TrueColor visual is depth 15 or 16:
one Python loop per pixel, three 256-entry lookup tables and an endian-aware
``to_bytes`` each.  This module is what that loop is when you stop
pretending, hand-written in the machine's own language in
``feetbrowser/asm/x11pack.S``, compiled on demand, called through ``ctypes``.

The compilation, the on-disk cache and the host/compiler checks all live in
``feetbrowser/asmlib.load_assembly``; this module is only the ctypes
plumbing and the fallback, which is byte-identical to the loop it replaces.

If there is no compiler, or the host is not Linux/x86-64, the kernel falls
back to that pure-Python loop so nothing else breaks.
"""

import os
import ctypes

from .asmlib import _as_dst, _as_src, load_assembly

_ASM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asm", "x11pack.S")

_lib = None
if os.path.exists(_ASM):
    _lib = load_assembly(_ASM)
    if _lib is not None:
        _lib.pack_rows.restype = None
        _lib.pack_rows.argtypes = [ctypes.POINTER(ctypes.c_ubyte)] * 2 \
            + [ctypes.c_size_t] * 4 \
            + [ctypes.POINTER(ctypes.c_ubyte)] * 3 \
            + [ctypes.c_uint, ctypes.c_uint]


def using_assembly():
    """True when the raw assembly kernel is active, not the fallback."""
    return _lib is not None


def pack_rows(src, dst, width, height, src_stride, dst_stride,
              red, green, blue, pixel_bytes, big_endian):
    """Pack `src` (packed RGB, 3 bytes a pixel) into `dst` (Z-format).

    Each row advances ``src`` by ``src_stride`` and ``dst`` by
    ``dst_stride``; each pixel is ``red[src[i]] | green[src[i+1]] |
    blue[src[i+2]]`` written as ``pixel_bytes`` bytes, least significant
    first unless ``big_endian`` is set. The tables carry each channel
    already scaled into its bit position, so a pixel is a merge and a
    store, exactly as the Python reference does it.
    """
    if _lib is not None:
        def table32(table):
            return b"".join(v.to_bytes(4, "little") for v in table)
        return _lib.pack_rows(_as_src(src, height * src_stride),
                              _as_dst(dst, height * dst_stride),
                              width, height, src_stride, dst_stride,
                              _as_src(table32(red), 1024),
                              _as_src(table32(green), 1024),
                              _as_src(table32(blue), 1024),
                              pixel_bytes, big_endian)
    order = "little" if not big_endian else "big"
    for row in range(height):
        s = row * src_stride
        d = row * dst_stride
        for _column in range(width):
            value = (red[src[s]] | green[src[s + 1]] | blue[src[s + 2]])
            dst[d:d + pixel_bytes] = value.to_bytes(pixel_bytes, order)
            s += 3
            d += pixel_bytes


__all__ = ["pack_rows", "using_assembly"]