"""The selection nearest-boundary kernel, wrapped for ctypes.

``offset_scan(adv, x, pen)`` finds the character boundary of a run nearest to
pixel ``x``, from an array of per-character advances and the run's left edge.
It is the assembly ``nearest_offset`` from ``asm/seloffset.S`` when this host
can build it (Linux, x86-64, a C compiler on the PATH); otherwise it is the
exact same walk in Python. Both produce identical results -- the kernel was
written to match the Python loop instruction for instruction.

The kernel is used *per mouse event*, not per character: it takes the whole
advances array in one call, so the fixed ctypes dispatch cost (well under a
microsecond) is paid once per scan rather than once per character, which is
the entire point. A drag that would have stepped a Python loop over a
hundred-character line steps over the array in the CPU instead.
"""

import array
import ctypes
import os

from .asmlib import load_assembly

_ASM = os.path.join(os.path.dirname(__file__), "asm", "seloffset.S")

_lib = load_assembly(_ASM) if os.path.exists(_ASM) else None
if _lib is not None:
    _lib.nearest_offset.restype = ctypes.c_size_t
    _lib.nearest_offset.argtypes = [
        ctypes.POINTER(ctypes.c_double),
        ctypes.c_size_t,
        ctypes.c_double,
        ctypes.c_double,
    ]

__all__ = ["offset_scan", "using_assembly"]


def using_assembly():
    """True when the compiled kernel is loaded for this process."""
    return _lib is not None


def _offset_scan_py(adv, x, pen):
    """The exact Python loop the kernel replaces."""
    best, best_d = 0, abs(x - pen)
    for i, w in enumerate(adv):
        pen += w
        distance = abs(x - pen)
        if distance < best_d:
            best, best_d = i + 1, distance
    return best


def offset_scan(adv, x, pen):
    """The character boundary nearest to pixel ``x``: 0 for before the first
    character, ``len(adv)`` for after the last, and the run-local index of
    the closest boundary in between. ``adv`` is the per-character advances
    of the run (an ``array.array("d")``, or anything iterable of floats),
    ``pen`` the pixel x of its left edge."""
    if _lib is not None and adv:
        if isinstance(adv, array.array):
            buf = (ctypes.c_double * len(adv)).from_buffer(adv)
        else:
            buf = (ctypes.c_double * len(adv)).from_buffer(array.array("d", adv))
        return _lib.nearest_offset(buf, len(adv), x, pen)
    return _offset_scan_py(adv, x, pen)