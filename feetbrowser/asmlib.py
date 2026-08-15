"""The shared compiler behind every raw-assembly kernel module in the project.

Each kernel module (``asmx11`` and any that follow) is a hand-written
``feetbrowser/asm/*.S`` file -- raw x86-64, no C, no assembler required at
runtime -- compiled on demand into a shared object and called through
``ctypes``. Everything that makes that possible lives here once: the host
and compiler checks, the on-disk cache keyed on the source, and the buffer
wrappers, so a kernel module is only the ctypes plumbing and its fallback.

If there is no compiler, or the host is not Linux/x86-64, every kernel
module falls back to identical pure-Python implementations so nothing else
breaks.
"""

import os
import sys
import ctypes
import hashlib
import subprocess
import tempfile
import platform


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


def load_assembly(asm_path):
    """Compile one ``.S`` file to a cached shared object and load it.

    Available only where it can mean anything -- Linux on x86-64 with a C
    compiler on the PATH. The ``.so`` is keyed by a hash of the source so a
    given source builds at most once per machine. Returns the
    ``ctypes.CDLL``, or ``None`` when this host cannot produce one. Every
    kernel module builds through this so the cache and the host checks live
    in one place.
    """
    if not _supports_asm():
        return None
    cc = _find_cc()
    if cc is None:
        return None
    with open(asm_path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()[:16]
    stem = os.path.splitext(os.path.basename(asm_path))[0]
    out = os.path.join(tempfile.gettempdir(),
                       f"feetbrowser_{stem}_{digest}.so")
    if not os.path.exists(out):
        tmp = out + ".tmp"
        subprocess.run([cc, "-shared", "-fPIC", "-o", tmp, asm_path],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.replace(tmp, out)
    return ctypes.CDLL(out)


def _as_dst(buf, n):
    if isinstance(buf, ctypes.Array) and buf._type_ is ctypes.c_ubyte:
        return buf
    return (ctypes.c_ubyte * n).from_buffer(buf)


def _as_src(buf, n):
    if isinstance(buf, ctypes.Array) and buf._type_ is ctypes.c_ubyte:
        return buf
    return (ctypes.c_ubyte * n).from_buffer_copy(buf)