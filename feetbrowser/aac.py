"""AAC-LC audio decoding, in Fortran, loaded through ctypes.

The decoder itself is in ``fortran/inst*.f`` -- fixed-form FORTRAN 77,
compiled by gfortran into a shared library that this module loads and
calls. Nothing here decodes anything; this is the part that finds the
library, keeps the build cached, tells one stream's decoding apart from
another's, and gets out of the way when the machine has no Fortran on it.

Where the library comes from depends on who is running, and the order is
the one ``h264.py`` uses next door: a library the packaging built on the
build machine and shipped inside this package under the name
``prebuilt_name()`` is preferred, and compiling from a checkout is the
fallback. That way round because a packaged application has no compiler
and never will -- see ``build_library``, which is the one entry point all
three packagers use, and which is deliberately the same shape as the
video decoder's so that the two cannot drift apart.

The subsystem is called the *instep* -- the arch between the toes and the
heel, which is where the weight goes. It shares nothing with the H.264
decoder next door but the build machinery; its routines are ``IP*`` and
its COMMON blocks ``/IP*/`` precisely so that the two can live in one
process without Fortran's global namespace introducing them to each
other.

Why Fortran: an AAC frame is 1024 inverse-MDCT outputs per channel, and
the transform behind them is 43 frames a second of FFT arithmetic. Python
does that at something under a tenth of realtime; the Fortran does it at
roughly two hundred times realtime on one core. The rest of the codec
followed the transform across the boundary because splitting a decoder
between the Huffman layer and the DSP layer would mean marshalling a
thousand coefficients per frame per channel across ctypes, which costs
more than the decoding.

What decodes today: AAC-LC, mono and stereo, every sampling frequency
index the standard defines, from raw ``raw_data_block`` frames as MP4
carries them or from ADTS. All eleven spectral codebooks and the escape
sequence, sections, scalefactors, pulses, TNS, mid/side and intensity
stereo, perceptual noise substitution, and all four window sequences.

What does not: HE-AAC's SBR and Parametric Stereo, Main profile's
backward prediction, LTP, SSR's gain control, coupling channels, LFE,
anything above two channels, and 960-sample frames. Every one of those is
refused by name with a status code of its own rather than mis-decoded --
see ``_STATUS`` below, and the header comment in ``fortran/instapi.f``.

There is no platform audio output here and there should not be. This
module's job ends at correct PCM in memory.
"""

import array
import ctypes
import hashlib
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_FORTRAN = os.path.join(os.path.dirname(_HERE), "fortran")

# Compilation order does not matter to gfortran here -- there are no
# modules, only COMMON blocks and an INCLUDE -- but a fixed order keeps the
# cache key stable across filesystems that list a directory in their own
# order.
_SOURCES = ("insttab.f", "instbit.f", "instics.f", "instdsp.f", "instapi.f")
_INCLUDES = ("instcom.inc",)

# The version instep_version reports. A library left in the cache by an
# older checkout has the old entry points and the old meanings, and calling
# it would be worse than not having one.
_ABI = 1

_LOCK = threading.Lock()
_lib = None
_load_error = None
_loaded = False

# Which Decoder's overlap is in the library's COMMON blocks. Guarded by
# _LOCK, like the blocks themselves.
_owner = None


class AacError(Exception):
    """A stream this decoder cannot decode, or a build that did not
    happen."""


# -- building ----------------------------------------------------------------

def _find_gfortran():
    for name in ("gfortran", "gfortran-15", "gfortran-14", "gfortran-13",
                 "gfortran-12", "gfortran-11"):
        try:
            subprocess.run([name, "--version"], check=True,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return name
        except (OSError, subprocess.CalledProcessError):
            continue
    return None


def _library_suffix():
    system = platform.system()
    if system == "Darwin":
        return ".dylib"
    if system == "Windows":
        return ".dll"
    return ".so"


def _digest():
    """A hash over the ABI and every source, so a changed decoder rebuilds.

    Nothing about the machine goes in, deliberately: the same sources hash
    the same everywhere, which is what lets the packaging build a library
    on one machine, name it after this digest, and have _open_library find
    it on another. Which gfortran built a given file is a separate
    question, answered by _compiler_id for the cache and by the
    instep_version check for anything that gets loaded.
    """
    sha = hashlib.sha256()
    sha.update(("abi%d" % _ABI).encode("ascii"))
    for name in _INCLUDES + _SOURCES:
        with open(os.path.join(_FORTRAN, name), "rb") as handle:
            sha.update(handle.read())
    return sha.hexdigest()[:16]


def _compiler_id(fc):
    """Which gfortran this is, in eight hex digits.

    The same sources built by two gfortrans are two different libraries --
    different runtime, different instruction set, different bugs -- and a
    cache in a shared temporary directory that cannot tell them apart hands
    the wrong one to whichever process runs second.
    """
    sha = hashlib.sha256()
    for flag in ("--version", "-dumpmachine"):
        try:
            done = subprocess.run([fc, flag], capture_output=True)
        except OSError:
            return "unknown0"
        sha.update(done.stdout)
    return sha.hexdigest()[:8]


# What a library has to be built with to be *shipped* rather than used where
# it was built: gfortran's own runtime linked in, instead of left behind as
# three dependencies on a compiler installation the user does not have.
# -march=native is left out for the same reason the rest is in: the machine
# that runs this is not the machine that built it.
#
# It is a list of attempts rather than one flag set because the three
# platforms disagree about which of these a toolchain will accept, and the
# compiler is the only authority on that. -static-libquadmath is GCC 10 and
# later and older gfortrans reject it outright, so every platform has a second
# attempt without it. The rest is per-platform and explained in
# _ship_attempts. This is the same set the video decoder ships with, and for
# the same reasons: the two libraries come out of one gfortran on one build
# machine, and a bundle where only one of them is self-contained is a bundle
# that plays pictures without sound.
_PORTABLE = ["-static-libgfortran", "-static-libgcc"]
_QUADMATH = _PORTABLE + ["-static-libquadmath"]


def _ship_attempts(system=None):
    """The flag sets ``build_library`` tries, most self-contained first."""
    if system is None:
        system = platform.system()
    if system == "Windows":
        # MinGW's static flags cover libgfortran, libgcc and libquadmath and
        # not libwinpthread, which a posix-threading-model libgcc pulls in
        # whatever else is asked for. The result links, runs on the build
        # machine, which has the compiler's bin/ on PATH, and fails on the
        # user's with "could not find module ... (or one of its dependencies)"
        # naming the decoder and not the dependency. -static covers everything
        # and is tried first; _dangling below is what decides whether it
        # worked, because on this platform the link succeeding proves nothing.
        return (["-static"], _QUADMATH, _PORTABLE)
    if system == "Linux":
        # manylinux's libgfortran.a is not built -fPIC -- the link fails on a
        # TPOFF32 relocation against a thread-local in async.o -- and no flag
        # makes a local-exec TLS relocation legal in a shared object. So the
        # last attempt links the runtime dynamically, which on this platform
        # is not a dead end: packaging/linux copies every NEEDED library into
        # the image and points an $ORIGIN rpath at it, so the AppImage is
        # still self-contained. -static-libgcc stays, being PIC either way.
        return (_QUADMATH, _PORTABLE, ["-static-libgcc"])
    return (_QUADMATH, _PORTABLE)


# From a checkout: tuned for this machine, because it will only ever run on
# this machine, and dropped if the compiler will not have it (gfortran on
# Apple silicon rejects -march=native outright).
_LOCAL_ATTEMPTS = (["-march=native"], [])


def _compile(fc, out, attempts=_LOCAL_ATTEMPTS, check=None):
    """Build the shared library at `out`, or raise.

    `attempts` is a list of extra-flag lists, tried in order until one
    compiles: the compiler is the only authority on what it accepts.

    `check` is given the library that came out and returns the reasons it is
    not fit to ship, empty when it is. A flag set whose output fails it is
    treated exactly like one the compiler rejected, and the next is tried --
    which is the only way to choose between flag sets that all link and do not
    all produce something that will load elsewhere.
    """
    tmp = out + ".%d.tmp" % os.getpid()
    base = ["-O3", "-shared", "-fPIC", "-std=legacy", "-fno-align-commons",
            "-I", _FORTRAN, "-o", tmp]
    if platform.system() == "Darwin":
        # Otherwise the library records its own bare filename as its install
        # name, which resolves against nothing once it is inside a bundle --
        # and packaging/macos/verify.sh rejects it, correctly.
        base += ["-Wl,-install_name,@loader_path/" + os.path.basename(out)]
    sources = [os.path.join(_FORTRAN, name) for name in _SOURCES]
    failures = []
    for extra in attempts:
        try:
            subprocess.run([fc] + base + list(extra) + sources, check=True,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:
            noise = (exc.stderr or b"").decode("utf8", "replace").strip()
            failures.append((list(extra), noise))
            continue
        except OSError as exc:
            raise AacError("could not run %s: %s" % (fc, exc))
        complaints = check(tmp) if check is not None else []
        if complaints:
            failures.append((list(extra), "\n".join(complaints)))
            os.unlink(tmp)
            continue
        os.replace(tmp, out)
        return list(extra)
    raise AacError("gfortran is on PATH but could not build the AAC "
                   "decoder.\n%s" % _why(fc, failures))


# How many lines of a failed compile are worth keeping. A missing static
# runtime says so on the first line and an undefined symbol on the last, so
# both ends are kept and only the middle of a long one is dropped.
_KEEP = 12


def _why(fc, failures):
    """Every attempt and what it said, laid out to be read in a CI log.

    Reporting only the last line of the last attempt is how "ld returned 1
    exit status" comes to be the whole of a build failure -- which is the
    exit status and not the reason, and names neither the flag that was
    tried nor the symbol that was missing.
    """
    report = ["%s tried %d flag set%s and none of them worked:"
              % (fc, len(failures), "" if len(failures) == 1 else "s")]
    for extra, noise in failures:
        report.append("  with %s:" % (" ".join(extra) if extra
                                      else "no extra flags"))
        lines = [line for line in noise.splitlines() if line.strip()]
        if not lines:
            report.append("    (it said nothing at all)")
            continue
        if len(lines) > _KEEP:
            head, tail = lines[:_KEEP // 2], lines[-(_KEEP // 2):]
            lines = head + ["    ... %d lines omitted ..."
                            % (len(lines) - _KEEP)] + tail
        report.extend("    " + line for line in lines)
    return "\n".join(report)


# -- what the built library still needs from outside itself -------------------
#
# A library that loads on the machine that built it and not on the machine
# that runs it is the failure this section exists to catch. It has one
# symptom, and the symptom names the wrong file: Windows says "could not find
# module _aac_<digest>.dll (or one of its dependencies)" when the module it
# could not find is the dependency, which it does not name, ever. Reading the
# dependencies out of the file here -- on the build machine, while there is
# still something to be done about them -- is the difference between a
# packaging job that fails with a name in it and one that ships.


def _pe_imports(path):
    """Every DLL named in the import table of a PE file.

    A parser rather than a call to dumpbin or objdump: the first is MSVC's and
    the second is the compiler's own, and a packaging script should not have
    to go looking for a second tool to check the output of the first. This
    reads one table out of a file gfortran has just written, and raises rather
    than guesses at anything it does not recognise.
    """
    with open(path, "rb") as handle:
        data = handle.read()

    def u16(off):
        return struct.unpack_from("<H", data, off)[0]

    def u32(off):
        return struct.unpack_from("<I", data, off)[0]

    if data[:2] != b"MZ":
        raise AacError("%s does not begin with a DOS header" % path)
    pe = u32(0x3C)
    if data[pe:pe + 4] != b"PE\0\0":
        raise AacError("%s has no PE signature at 0x%x" % (path, pe))
    sections_n = u16(pe + 6)
    optional_size = u16(pe + 20)
    optional = pe + 24
    magic = u16(optional)
    if magic == 0x10B:                          # PE32
        directories = optional + 96
    elif magic == 0x20B:                        # PE32+
        directories = optional + 112
    else:
        raise AacError("%s: unknown optional header magic 0x%04x"
                       % (path, magic))
    # NumberOfRvaAndSizes is the field immediately before the array it counts.
    # Data directory 1 is the import table: an RVA and a size.
    if u32(directories - 4) < 2:
        return []
    imports = u32(directories + 8)
    if imports == 0 or u32(directories + 12) == 0:
        return []

    sections = []
    for i in range(sections_n):
        head = optional + optional_size + i * 40
        # Virtual size is zero in object files and short in some linkers'
        # output, so the larger of it and the raw size is what covers the RVA.
        sections.append((u32(head + 12),
                         max(u32(head + 8), u32(head + 16)),
                         u32(head + 20)))

    def offset(rva):
        for start, size, raw in sections:
            if start <= rva < start + size:
                return raw + (rva - start)
        raise AacError("%s: RVA 0x%x falls in no section" % (path, rva))

    names, entry = [], offset(imports)
    while True:
        descriptor = data[entry:entry + 20]
        # The table ends with an all-zero descriptor and nothing else. A table
        # that runs off the end of the file has no end, which is a truncated
        # file and not a library with no dependencies -- and answering "none"
        # for it would be the one wrong answer this whole check cannot afford.
        if len(descriptor) < 20:
            raise AacError("%s: the import table runs off the end" % path)
        if not any(descriptor):
            break
        start = offset(u32(entry + 12))
        end = data.find(b"\0", start)
        if end < 0:
            raise AacError("%s: an import name runs off the end" % path)
        names.append(data[start:end].decode("ascii", "replace"))
        entry += 20
    return names


def _dangling(path):
    """The dependencies of `path` that a machine without a compiler lacks.

    Windows only. The other two platforms answer this question elsewhere and
    better: packaging/linux copies every NEEDED library into the image, and
    packaging/macos/verify.sh runs otool over the finished bundle. Windows has
    neither, and is the one platform where the loader's own error message
    withholds the name of what is missing.

    "A machine without a compiler" is taken literally rather than guessed at
    from a list of known-good DLL names: a dependency is satisfied if the
    system directory has it, or if it sits in the same directory as the file
    that wants it -- which is where _load's LOAD_WITH_ALTERED_SEARCH_PATH
    makes the loader look first, and so where _ship_runtime_beside puts
    things. Anything else is ours to deal with.
    """
    if platform.system() != "Windows":
        return []
    directories = [os.path.dirname(os.path.abspath(path)),
                   os.path.join(os.environ.get("SystemRoot", "C:\\Windows"),
                                "System32")]
    missing = []
    for name in _pe_imports(path):
        # api-ms-win-*.dll are API set contract names. They are resolved from
        # a table inside the loader and need not exist as files anywhere.
        if name.lower().startswith("api-ms-win-"):
            continue
        if any(os.path.exists(os.path.join(d, name)) for d in directories):
            continue
        missing.append(name)
    return missing


def _find_beside_compiler(fc, name):
    """Where the compiler keeps `name`, or None."""
    directories = [os.path.dirname(os.path.abspath(fc))] if fc else []
    directories += os.environ.get("PATH", "").split(os.pathsep)
    for directory in directories:
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _ship_runtime_beside(out, fc, names):
    """Copy the compiler's runtime DLLs next to the library that needs them.

    The last resort, when no flag set produced a self-contained file. What
    gets copied is gfortran's own runtime -- the same code -static-libgfortran
    would have put inside the library -- so putting it beside the library
    instead of in it changes the size of the bundle and nothing else.

    The walk is transitive, because libgfortran needs libquadmath which needs
    libgcc which needs libwinpthread, and a bundle that stops after the first
    of those fails in exactly the way this is here to prevent.

    Copying rather than moving matters here in a way it does not next door:
    the video decoder is in this same directory and wants the same DLLs, so
    whichever of the two is built second finds them already present, reports
    nothing dangling, and copies nothing. Two decoders, one runtime.

    Returns (copied, still missing), both sorted, so the caller can print the
    first and refuse over the second.
    """
    directory = os.path.dirname(os.path.abspath(out))
    copied, missing, queue = [], [], list(names)
    seen = set(name.lower() for name in queue)
    while queue:
        name = queue.pop(0)
        source = _find_beside_compiler(fc, name)
        if source is None:
            missing.append(name)
            continue
        target = os.path.join(directory, name)
        shutil.copyfile(source, target)
        copied.append(name)
        for further in _dangling(target):
            if further.lower() not in seen:
                seen.add(further.lower())
                queue.append(further)
    return sorted(copied), sorted(missing)


def prebuilt_name():
    """What a library built by the packaging has to be called.

    The digest is the whole guarantee. It comes from the sources in
    ``fortran/``, which ship beside the package in every bundle exactly as
    they sit beside it in a checkout, so a file under this name was built
    from precisely the decoder this Python code was written against. Change
    a line of Fortran or the ABI and the name changes with it: a stale
    prebuilt is not preferred over the sources, it is simply not found.
    """
    return "_aac_%s%s" % (_digest(), _library_suffix())


def prebuilt_path():
    """Where a bundled prebuilt library lives -- inside the package, next to
    this file, so that whatever copied the package copied the decoder too."""
    return os.path.join(_HERE, prebuilt_name())


def build_library(out, fc=None, report=None):
    """Compile the decoder for shipping, into `out`. Returns `out`.

    This is what packaging/{macos,linux,windows} call, and the only caller
    there is. Keeping the flags here rather than in three scripts is what
    stops what a library is built with from drifting away from what the
    loader expects of it -- and the same argument puts the check that the
    result is fit to ship here rather than in the scripts, because a check
    three scripts each own is a check two of them are out of date.

    `report` is called with a line at a time about what was built and what it
    still needs, which is what the packaging prints into its log.
    """
    if not os.path.isdir(_FORTRAN):
        raise AacError("the fortran/ directory is missing from this checkout")
    if fc is None:
        fc = _find_gfortran()
    if fc is None:
        raise AacError("no gfortran on PATH")
    if report is None:
        report = lambda line: None                          # noqa: E731
    directory = os.path.dirname(os.path.abspath(out))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    used = _compile(fc, out, _ship_attempts(), _dangling)
    report("built with %s" % (" ".join(used) if used else "no extra flags"))

    # Only reachable where _dangling has an opinion, which is Windows. Every
    # flag set left something behind, so the runtime ships beside the library
    # instead of inside it -- and _load opens the library in a way that finds
    # it there. Anything not found at all is fatal: a bundle that is missing a
    # DLL says so here, with the name in it, rather than on a stranger's
    # machine without.
    left = _dangling(out)
    if left:
        copied, still = _ship_runtime_beside(out, fc, left)
        for name in copied:
            report("shipped %s beside it" % name)
        if still:
            raise AacError(
                "the decoder needs %s, and neither the compiler's directory "
                "nor PATH has %s. A bundle shipped like this would install, "
                "start, and play video with no sound."
                % (", ".join(still), "it" if len(still) == 1 else "them"))
    return out


_ENTRY_POINTS = ("instep_version", "instep_reset", "instep_flush",
                 "instep_config", "instep_adts", "instep_decode",
                 "instep_pcm", "instep_save", "instep_restore",
                 "instep_qspec", "instep_spec", "instep_bands",
                 "instep_shape", "instep_imdct", "instep_window")


def _load(path):
    """Open a built library and check it is the one we think it is."""
    if platform.system() == "Windows":
        # LOAD_WITH_ALTERED_SEARCH_PATH. Without it Windows resolves the
        # library's dependencies against the *process's* search path, which in
        # a bundle is the interpreter's directory and never this one, so a
        # runtime DLL shipped beside the decoder by build_library sits there
        # unfound. With it, the directory the library came out of is searched
        # first -- which is the only reason shipping it beside works.
        lib = ctypes.CDLL(os.path.abspath(path), winmode=0x00000008)
    else:
        lib = ctypes.CDLL(path)
    for name in _ENTRY_POINTS:
        getattr(lib, name).restype = None
    version = ctypes.c_int(0)
    lib.instep_version(ctypes.byref(version))
    if version.value != _ABI:
        raise AacError("the decoder at %s reports ABI %d, expected %d -- it "
                       "was built from a different version of fortran/ and "
                       "cannot be called by this one. Delete it; a checkout "
                       "will build another." % (path, version.value, _ABI))
    lib.instep_reset()
    return lib


def _open_library():
    if not os.path.isdir(_FORTRAN):
        raise AacError("the fortran/ directory is missing from this checkout")
    # A library the packaging built and shipped beside this file. Preferred
    # over compiling, because in a bundle there is no compiler to fall back
    # on -- and a shipped library that will not load is worth saying out
    # loud, so its failure is only swallowed if there is a gfortran to try.
    shipped = prebuilt_path()
    failure = None
    if os.path.exists(shipped):
        try:
            return _load(shipped)
        except (AacError, OSError) as exc:
            failure = AacError("the bundled AAC decoder %s did not load: %s"
                               % (prebuilt_name(), exc))
    fc = _find_gfortran()
    if fc is None:
        raise failure or AacError(
            "no gfortran on PATH, and no AAC decoder was shipped beside this "
            "module. Install gfortran -- brew install gcc, dnf install "
            "gcc-gfortran, MinGW-w64 on Windows -- or use a packaged build, "
            "which carries the decoder inside it.")
    out = os.path.join(tempfile.gettempdir(),
                       "feetbrowser_aac_%s_%s%s"
                       % (_digest(), _compiler_id(fc), _library_suffix()))
    if not os.path.exists(out):
        _compile(fc, out)
    return _load(out)


def _library():
    """The loaded library, or None. Every failure is remembered: a machine
    with no gfortran must not try to run it once per frame."""
    global _lib, _load_error, _loaded
    if _loaded:
        return _lib
    with _LOCK:
        if _loaded:
            return _lib
        try:
            _lib = _open_library()
        except (AacError, OSError) as exc:
            _lib = None
            _load_error = str(exc)
        _loaded = True
    return _lib


def available():
    """True when this machine can decode AAC.

    False rather than an exception for every way it can go wrong: no
    compiler, a compile that failed, a library that will not load, a
    library that loads and reports the wrong ABI. A browser on a machine
    with no toolchain is a browser that says "no decoder", not one that
    raises out of the media layer.
    """
    return _library() is not None


def library_path():
    """Which file the decoder was loaded from, or None if there is none.

    A bundled library and one a compiler made thirty seconds ago behave
    identically, which is exactly why a check that sound works has to be
    able to say which of the two answered.
    """
    lib = _library()
    return getattr(lib, "_name", None) if lib is not None else None


def unavailable_reason():
    """Why not, in a form fit to show a user. None when it is available.

    The four ways this fails want four different sentences, because they
    want four different things done about them: no compiler is something
    the user can install or route around by taking a packaged build, a
    failed compile is a log with the compiler's own words in it, a
    bundled library that will not load is a bundle to rebuild, and an ABI
    mismatch is a stale file to delete. _open_library and _load raise them
    apart; this only hands the sentence on.
    """
    if _library() is not None:
        return None
    return _load_error or "the AAC decoder is not available"


# -- what the Fortran says ---------------------------------------------------

# The numbers are grouped by what produced them, so that a bug report says
# where to look: -1..-19 configuration, -20..-29 a tool we refuse by name,
# -30..-39 the bitstream contradicting itself, -40..-49 the frame's element
# structure.
_STATUS = {
    -1: "AAC: no AudioSpecificConfig -- the decoder is not configured",
    -2: "AAC: the AudioSpecificConfig ends in the middle of itself",
    -3: "AAC: an empty frame",
    -4: "AAC: a frame larger than the decoder's buffer",
    -5: "AAC: no ADTS header where one was expected",
    -6: "AAC: an ADTS header that is not MPEG layer zero",
    -7: "AAC: an ADTS frame longer than the bytes it came with",
    -8: "AAC: no decoded frame to read samples from",
    -9: "AAC: the samples did not fit their buffer",
    -11: "AAC: an explicitly coded sampling frequency, which has no "
         "scalefactor band layout defined for it",
    -12: "AAC: a reserved sampling frequency index",
    -13: "AAC: more than two channels -- this decoder does mono and stereo",
    -14: "AAC: an LFE channel, which this decoder does not implement",
    -15: "AAC: 960-sample frames, which this decoder does not implement",
    -16: "AAC: a stream that depends on a separate core coder",
    -20: "AAC: Main profile, whose backward prediction this decoder does "
         "not implement",
    -21: "AAC: Scalable Sample Rate profile, which this decoder does not "
         "implement",
    -22: "AAC: a coupling channel element, which this decoder does not "
         "implement",
    -23: "AAC: gain control data, which belongs to SSR and which this "
         "decoder does not implement",
    -24: "AAC: Long Term Prediction, which this decoder does not implement",
    -25: "AAC: HE-AAC -- Spectral Band Replication, which this decoder does "
         "not implement",
    -26: "AAC: HE-AAC v2 -- Parametric Stereo, which this decoder does not "
         "implement",
    -27: "AAC: an audio object type that is not AAC-LC",
    -30: "AAC: the frame ended in the middle of a syntax element",
    -31: "AAC: a reserved Huffman codebook",
    -32: "AAC: a scalefactor outside the range the standard allows",
    -33: "AAC: a scalefactor band layout the sampling frequency cannot have",
    -34: "AAC: a TNS filter of an order this decoder does not keep room for",
    -35: "AAC: pulse data that points outside the spectrum",
    -36: "AAC: a reserved value of ms_mask_present",
    -39: "AAC: a quantised coefficient larger than the standard allows",
    -40: "AAC: a frame with more elements than a frame can have",
    -41: "AAC: a frame whose channels do not add up to the ones configured",
    -42: "AAC: several raw data blocks behind one ADTS header",
    -43: "AAC: a programme config that contradicts the stream's own",
}


def _explain(status):
    return _STATUS.get(status, "AAC: decoder status %d" % status)


# The thirteen sampling frequencies the four-bit index can name. Indices 13
# and 14 are reserved and 15 means "the rate follows explicitly", which the
# Fortran refuses -- there is no band layout defined for an arbitrary rate.
SAMPLE_RATES = (96000, 88200, 64000, 48000, 44100, 32000,
                24000, 22050, 16000, 12000, 11025, 8000, 7350)


def probe(asc):
    """Why this AudioSpecificConfig cannot be decoded, or None if it can.

    Cheap and side-effect free in the sense that matters: it parses the
    config and throws the result away, so a container can put a reason on
    screen without constructing a decoder or reserving the library.
    """
    lib = _library()
    if lib is None:
        return unavailable_reason()
    if not asc:
        return "AAC: this track carries no AudioSpecificConfig"
    with _LOCK:
        info = _config(lib, asc)
    if info[0] != 0:
        return _explain(info[0])
    return None


def _config(lib, asc):
    """instep_config, with the library already reserved."""
    global _owner
    _owner = None
    lib.instep_reset()
    buf = (ctypes.c_char * len(asc)).from_buffer_copy(bytes(asc))
    size = ctypes.c_int(len(asc))
    info = (ctypes.c_int * 8)()
    lib.instep_config(buf, ctypes.byref(size), info)
    return list(info)


# -- ADTS --------------------------------------------------------------------

def adts_frames(data):
    """Walk an ADTS stream, yielding ``(header length, frame length)``.

    ADTS is the framing a bare ``.aac`` file uses: a seven-byte header in
    front of every frame, carrying the same facts an AudioSpecificConfig
    would and a length so the next one can be found. This is the one piece
    of parsing done in Python rather than in Fortran, because it is
    framing rather than decoding and because a caller often wants the
    frame boundaries without decoding anything at all.
    """
    pos = 0
    end = len(data)
    while pos + 7 <= end:
        if data[pos] != 0xFF or (data[pos + 1] & 0xF0) != 0xF0:
            raise AacError("AAC: no ADTS sync word at byte %d" % pos)
        protection_absent = data[pos + 1] & 1
        length = (((data[pos + 3] & 3) << 11) | (data[pos + 4] << 3)
                  | (data[pos + 5] >> 5))
        header = 7 if protection_absent else 9
        if length < header or pos + length > end:
            raise AacError("AAC: an ADTS frame runs past the end of the "
                           "stream at byte %d" % pos)
        yield header, length
        pos += length
    if pos != end:
        raise AacError("AAC: %d bytes of trailing rubbish after the last "
                       "ADTS frame" % (end - pos))


def asc_from_adts(data):
    """An AudioSpecificConfig equivalent to an ADTS stream's first header.

    The two carry the same three facts -- object type, sampling frequency
    index, channel configuration -- in a different order and with two bits
    fewer of channel configuration in ADTS. Building the config once and
    decoding through the ordinary path afterwards means the ADTS and MP4
    routes into this decoder are the same code below the first frame,
    which is a property the test suite checks by decoding both and
    comparing sample for sample.
    """
    if len(data) < 7 or data[0] != 0xFF or (data[1] & 0xF0) != 0xF0:
        raise AacError("AAC: this is not an ADTS stream")
    profile = (data[2] >> 6) & 3
    rate_index = (data[2] >> 2) & 0xF
    channels = ((data[2] & 1) << 2) | (data[3] >> 6)
    object_type = profile + 1
    bits = (object_type << 11) | (rate_index << 7) | (channels << 3)
    return struct.pack(">H", bits)


# -- the decoder -------------------------------------------------------------

class Decoder:
    """One AAC-LC stream, decoded a frame at a time.

    Every instance shares the library's single set of COMMON blocks, so
    every call takes ``_LOCK``. That is the price of a decoder whose state
    is static storage, and it is paid here rather than in the caller.

    Sharing is sharper for audio than for video. A frame's samples are the
    second half of the *previous* frame's windowed transform added to the
    first half of this one's; there is no keyframe and no way to start
    clean. So each decoder keeps its own copy of the overlap and puts it
    back whenever it finds another decoder has been at the library in
    between. Two ``<audio>`` elements playing at once are then slow and
    correct rather than fast and clicking.
    """

    def __init__(self, asc):
        lib = _library()
        if lib is None:
            raise AacError(unavailable_reason())
        self._lib = lib
        if not asc:
            # Answered here rather than in the Fortran, which is handed a
            # length of zero and can only say "an empty frame". A track
            # with no AudioSpecificConfig is a real thing to find in a real
            # MP4 and deserves the same sentence `probe` gives it.
            raise AacError("AAC: this track carries no AudioSpecificConfig")
        self._asc = bytes(asc)
        self._overlap = None
        self._ints = None
        with _LOCK:
            info = _config(lib, self._asc)
            if info[0] != 0:
                raise AacError(_explain(info[0]))
            global _owner
            _owner = self
        self.sample_rate = info[1]
        self.channels = info[2]
        self.frame_length = info[3]
        self.object_type = info[4]
        # Bits consumed by the last frame, and bits it was offered. A frame
        # whose parse stops anywhere but the end of it is a frame this
        # decoder read differently from the encoder that wrote it, and that
        # is worth being able to see from outside.
        self.last_bits = 0
        self.last_bits_offered = 0

    def reset(self):
        """Forget the overlap. The configuration came from the container
        and is kept.

        This is what a seek needs and it is not free of consequence: the
        first frame after it is decoded against silence, so its first half
        is attenuated. Every AAC decoder has this property and it is why
        seeking in AAC replays a frame or two before the target rather
        than starting at it.
        """
        with _LOCK:
            self._overlap = None
            self._ints = None
            if _owner is self:
                self._lib.instep_flush()

    def _enter(self):
        """Make the library ours. _LOCK is already held."""
        global _owner
        if _owner is self:
            return
        info = _config(self._lib, self._asc)
        if info[0] != 0:
            raise AacError(_explain(info[0]))
        if self._overlap is not None:
            self._lib.instep_restore(self._overlap,
                                     ctypes.byref(ctypes.c_int(2048)),
                                     self._ints,
                                     ctypes.byref(ctypes.c_int(16)),
                                     ctypes.byref(ctypes.c_int(0)))
        _owner = self

    def _leave(self):
        """Take our continuity back out of the library."""
        if self._overlap is None:
            self._overlap = (ctypes.c_double * 2048)()
            self._ints = (ctypes.c_int * 16)()
        self._lib.instep_save(self._overlap,
                              ctypes.byref(ctypes.c_int(2048)),
                              self._ints, ctypes.byref(ctypes.c_int(16)),
                              ctypes.byref(ctypes.c_int(0)))

    def decode(self, packet):
        """One ``raw_data_block`` in, ``(samples per channel, channels,
        interleaved float32 bytes)`` out.

        The samples are floats in [-1, 1] and are not clipped: the inverse
        transform of a loud frame can exceed unity by a little and it is
        the mixer's business, not the decoder's, what to do about that.
        """
        if not packet:
            return 0, self.channels, b""
        with _LOCK:
            self._enter()
            info = self._feed(bytes(packet))
            out = self._read(info)
            self._leave()
        return out

    def decode_adts(self, data):
        """A whole ADTS stream in, one ``(samples, channels, bytes)`` out.

        Every frame decoded and concatenated, which is what a test wants
        and what the command line below prints. A player wants
        ``decode()`` and its own framing.
        """
        pcm = bytearray()
        total = 0
        for header, length in adts_frames(data):
            count, _channels, chunk = self.decode(data[header:length])
            total += count
            pcm += chunk
            data = data[length:]
        return total, self.channels, bytes(pcm)

    def _feed(self, data):
        """One raw_data_block through the Fortran. _LOCK is already held,
        and the library is ours."""
        global _owner
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        size = ctypes.c_int(len(data))
        info = (ctypes.c_int * 8)()
        self._lib.instep_decode(buf, ctypes.byref(size), info)
        if info[0] != 0:
            # The library now holds half a frame that no longer matches this
            # decoder's history. Disown it rather than let the next call
            # overlap-add onto wreckage.
            _owner = None
            raise AacError(_explain(info[0]))
        self.last_bits = info[3]
        self.last_bits_offered = info[4]
        return info

    def _read(self, info):
        count, channels = info[1], info[2]
        need = count * channels
        if need <= 0:
            raise AacError("AAC: the decoder produced no samples")
        out = (ctypes.c_float * need)()
        status = ctypes.c_int(0)
        self._lib.instep_pcm(out, ctypes.byref(ctypes.c_int(need)),
                             ctypes.byref(status))
        if status.value < 0:
            raise AacError(_explain(status.value))
        return count, channels, bytes(memoryview(out).cast("B"))

    # -- what the tests look at ---------------------------------------------
    #
    # A decoder compared only against its own output is compared against
    # nothing, and a tolerance at the end of a chain this long hides almost
    # any bug in the middle of it. These expose the stages that can be
    # compared exactly, so that a difference can be located rather than
    # merely detected. They read whatever the last decode left behind and
    # are only meaningful straight after one.

    def _snapshot(self, getter, kind, count):
        buf = (kind * count)()
        status = ctypes.c_int(0)
        getter(buf, ctypes.byref(ctypes.c_int(count)), ctypes.byref(status))
        if status.value < 0:
            raise AacError(_explain(status.value))
        return list(buf)

    def quantised_spectrum(self, channel=1):
        """The last frame's quantised coefficients for one channel, before
        anything was done to them. Integers, and exactly comparable."""
        with _LOCK:
            self._enter()
            return self._snapshot(
                lambda b, c, s: self._lib.instep_qspec(
                    ctypes.byref(ctypes.c_int(channel)), b, c, s),
                ctypes.c_int, 1024)

    def spectrum(self, channel=1):
        """The same coefficients dequantised, after the stereo tools and
        TNS and before the transform."""
        with _LOCK:
            self._enter()
            return self._snapshot(
                lambda b, c, s: self._lib.instep_spec(
                    ctypes.byref(ctypes.c_int(channel)), b, c, s),
                ctypes.c_double, 1024)

    def bands(self, channel=1):
        """``(codebooks, scalefactors, band offsets)`` for the last frame.

        The first two are flattened as ``band + 52 * group``; the third is
        the band layout in force, which is a property of the window
        sequence rather than of the frame's contents.
        """
        count = 52 * 8
        types = (ctypes.c_int * count)()
        factors = (ctypes.c_int * count)()
        offsets = (ctypes.c_int * count)()
        status = ctypes.c_int(0)
        with _LOCK:
            self._enter()
            self._lib.instep_bands(ctypes.byref(ctypes.c_int(channel)),
                                   types, factors, offsets,
                                   ctypes.byref(ctypes.c_int(count)),
                                   ctypes.byref(status))
        if status.value < 0:
            raise AacError(_explain(status.value))
        return list(types), list(factors), list(offsets[:52])

    def shape(self, channel=1):
        """The last frame's window sequence and grouping, as a dict."""
        out = (ctypes.c_int * 16)()
        status = ctypes.c_int(0)
        with _LOCK:
            self._enter()
            self._lib.instep_shape(ctypes.byref(ctypes.c_int(channel)), out,
                                   ctypes.byref(ctypes.c_int(16)),
                                   ctypes.byref(status))
        if status.value < 0:
            raise AacError(_explain(status.value))
        return {
            "window_sequence": out[0],
            "window_shape": out[1],
            "max_sfb": out[2],
            "windows": out[3],
            "groups": out[4],
            "num_swb": out[5],
            "ms_mask_present": out[6],
            "tns": out[7],
            "group_lengths": list(out[8:8 + out[4]]),
        }


def imdct(coefficients, fast=True):
    """The inverse MDCT on its own, either implementation.

    Exposed so that the fast transform can be held against the formula it
    claims to compute rather than against itself. 128 or 1024 coefficients
    in, twice that many samples out.
    """
    lib = _library()
    if lib is None:
        raise AacError(unavailable_reason())
    size = len(coefficients)
    if size not in (128, 1024):
        raise AacError("AAC: the transform is 128 or 1024 points")
    src = (ctypes.c_double * size)(*coefficients)
    dst = (ctypes.c_double * (2 * size))()
    with _LOCK:
        lib.instep_imdct(src, ctypes.byref(ctypes.c_int(size)),
                         ctypes.byref(ctypes.c_int(0 if fast else 1)), dst)
    return list(dst)


def window(which):
    """One of the four window halves: 0 long sine, 1 long KBD, 2 short
    sine, 3 short KBD. The rising half; the falling half is the same
    numbers backwards, which is why only one is stored."""
    lib = _library()
    if lib is None:
        raise AacError(unavailable_reason())
    size = 1024 if which <= 1 else 128
    dst = (ctypes.c_double * size)()
    status = ctypes.c_int(0)
    with _LOCK:
        lib.instep_window(ctypes.byref(ctypes.c_int(which)), dst,
                          ctypes.byref(ctypes.c_int(size)),
                          ctypes.byref(status))
    if status.value < 0:
        raise AacError(_explain(status.value))
    return list(dst)


def pcm_to_int16(samples):
    """Interleaved float32 bytes as interleaved signed 16-bit ones.

    Rounds half away from zero and clamps, which is what everything that
    writes a WAV file does. Lives here so that the one place a decoder's
    output gets quantised is the same in the tests and in the command line
    below.
    """
    floats = array.array("f")
    floats.frombytes(samples)
    out = array.array("h", [0]) * len(floats)
    for i, value in enumerate(floats):
        scaled = value * 32768.0
        if scaled >= 32767.0:
            out[i] = 32767
        elif scaled <= -32768.0:
            out[i] = -32768
        elif scaled >= 0:
            out[i] = int(scaled + 0.5)
        else:
            out[i] = -int(-scaled + 0.5)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


# -- proving a build can actually decode ------------------------------------

# How far a decoded sample may sit from the reference decoder's. AAC is not
# a bit-exact specification -- the standard defines the inverse transform in
# real arithmetic -- so the comparison is numerical and this is the number
# tests/test_aac.py measured and uses: about three times the worst error
# seen across fourteen vectors, which is room for a different libm's sine in
# the twiddle table and not room for a decoding bug.
CHECK_TOLERANCE = 1.0e-06


def check(args, out=print):
    """Can this build decode audio, and does it decode right? An exit code.

    Lives here rather than in ``__main__`` because the interesting caller is
    the packaging, and because the answer is this module's to give: a bundle
    that shipped no decoder starts, renders, plays video and says nothing
    about sound until somebody opens a file, so all three packaging scripts
    ask here instead, once, at build time and with the compiler off PATH.

    With an ADTS stream it decodes one; with the float32 samples a reference
    decoder produced from that stream it compares them, because a decoder
    that loads and returns rubbish has passed no test worth having.
    """
    reason = unavailable_reason()
    if reason is not None:
        out("audio: no AAC decoder: %s" % reason)
        return 1
    out("audio: AAC decoder ready, from %s" % library_path())
    if not args:
        return 0
    try:
        with open(args[0], "rb") as handle:
            stream = handle.read()
    except OSError as exc:
        out("audio: cannot read %s: %s" % (args[0], exc.strerror or exc))
        return 1
    try:
        decoder = Decoder(asc_from_adts(stream))
        count, channels, pcm = decoder.decode_adts(stream)
    except AacError as exc:
        out("audio: %s did not decode: %s" % (args[0], exc))
        return 1
    out("audio: decoded %s: %d samples x %d channels at %d Hz (%.2f s)"
        % (os.path.basename(args[0]), count, channels, decoder.sample_rate,
           count / float(decoder.sample_rate or 1)))
    if len(args) < 2:
        return 0
    import zlib
    try:
        with open(args[1], "rb") as handle:
            truth = array.array("f")
            truth.frombytes(zlib.decompress(handle.read()))
    except (OSError, zlib.error, ValueError) as exc:
        out("audio: cannot read %s: %s" % (args[1], exc))
        return 1
    if sys.byteorder == "big":
        truth.byteswap()
    got = array.array("f")
    got.frombytes(pcm)
    if sys.byteorder == "big":
        got.byteswap()
    if len(got) != len(truth):
        out("audio: decoded %d samples, %s has %d"
            % (len(got), os.path.basename(args[1]), len(truth)))
        return 1
    worst = max((abs(a - b) for a, b in zip(got, truth)), default=0.0)
    if worst > CHECK_TOLERANCE:
        out("audio: the samples differ from %s by up to %.3e, limit %.1e"
            % (os.path.basename(args[1]), worst, CHECK_TOLERANCE))
        return 1
    out("audio: the samples match the reference decoder's to %.3e" % worst)
    return 0


_USAGE = """usage: python3 -m feetbrowser.aac \
[--name | --build PATH [--fc GFORTRAN] | --check [stream.aac [truth.f32.z]] \
| FILE...]

  --name          print the filename a prebuilt library must have
  --build PATH    compile the decoder for shipping, into PATH
  --fc GFORTRAN   which compiler --build should use
  --check         say whether this build can decode AAC, and prove it
  FILE...         decode an ADTS .aac stream and say what came out
"""


def _cli(argv):                                 # pragma: no cover
    """The packaging's entry point, and a way to ask this module a question
    without a browser in the way."""
    if argv and argv[0] == "--help":
        sys.stdout.write(_USAGE)
        return 0
    if argv and argv[0] == "--name":
        print(prebuilt_name())
        return 0
    if argv and argv[0] == "--check":
        return check(argv[1:])
    if argv and argv[0] == "--build":
        rest = argv[1:]
        if not rest:
            sys.stderr.write(_USAGE)
            return 2
        out, fc = rest[0], None
        if rest[1:2] == ["--fc"]:
            fc = rest[2] if len(rest) > 2 else None
        try:
            build_library(out, fc, report=lambda line: print("    %s" % line))
        except AacError as exc:
            sys.stderr.write("%s\n" % exc)
            return 1
        print(out)
        return 0
    if not available():
        sys.stderr.write("no decoder: %s\n" % unavailable_reason())
        return 1
    for path in argv:
        with open(path, "rb") as handle:
            blob = handle.read()
        decoder = Decoder(asc_from_adts(blob))
        count, channels, pcm = decoder.decode_adts(blob)
        print("%s: %d samples x %d channels at %d Hz (%.2f s)"
              % (path, count, channels, decoder.sample_rate,
                 count / float(decoder.sample_rate or 1)))
    return 0


if __name__ == "__main__":                      # pragma: no cover
    sys.exit(_cli(sys.argv[1:]))
