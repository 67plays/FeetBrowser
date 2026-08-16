"""MPEG audio Layer III decoding, in Fortran, loaded through ctypes.

The decoder itself is in ``fortran/ball*.f`` -- fixed-form FORTRAN 77,
compiled by gfortran into a shared library that this module loads and
calls. Nothing here decodes anything; this is the part that finds the
library, keeps the build cached, tells one stream's decoding apart from
another's, and gets out of the way when the machine has no Fortran on it.

Where the library comes from depends on who is running, and the order is
the one ``aac.py`` and ``h264.py`` use next door: a library the packaging
built on the build machine and shipped inside this package under the name
``prebuilt_name()`` is preferred, and compiling from a checkout is the
fallback. That way round because a packaged application has no compiler
and never will -- see ``build_library``, which is the one entry point all
three packagers use, and which is deliberately the same shape as the
other two decoders' so that the three cannot drift apart.

The subsystem is called the *ball of the foot* -- the pad behind the toes
that carries the weight through a step. It shares nothing with the AAC
and H.264 decoders next door but the build machinery; its routines are
``BL*`` and its COMMON blocks ``/BL*/``, where AAC's are ``IP*`` and
H.264's ``H2*``, precisely so that three decoders can live in one process
without Fortran's single global namespace introducing them to each other.
Getting that wrong does not fail to link. It links cleanly and corrupts
memory at runtime, which is why the prefixes are a rule and not a habit.

What decodes today: MPEG-1, MPEG-2 (LSF) and MPEG-2.5 Layer III, mono and
stereo, at all nine sampling frequencies the three versions between them
define and every bitrate in their tables. The frame header and its CRC,
the side information, scalefactors on the long path and on the mixed and
short paths, MPEG-2's own scalefactor partitioning and its intensity
variant, all thirty Huffman tables and both count1 quadruple tables,
requantisation, mid/side and intensity stereo in both the MPEG-1 and the
MPEG-2 forms, alias reduction, the inverse MDCT with all four window
shapes and the switching between them, frequency inversion, and the
polyphase synthesis filterbank.

And the bit reservoir, which is the part of Layer III that is easy to get
wrong quietly: a frame's main data does not begin in that frame. Up to
511 bytes of it may sit in frames already decoded, so the decoder carries
a running buffer of main data with the headers and side information taken
out, and a granule begins at a negative offset from the end of it. A
decoder that ignores this decodes most frames of most files correctly and
falls apart on exactly the dense ones.

What does not: Layer I, Layer II, free-format bitrates and more than two
channels. Every one of those is refused by name with a status code of its
own rather than mis-decoded -- see ``_STATUS`` below, and the header
comment in ``fortran/ballapi.f``. "Unsupported" on its own is a useless
thing to tell somebody whose file will not play.

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
_SOURCES = ("balldat.f", "balltab.f", "ballbit.f", "ballhdr.f",
            "ballsf.f", "ballhuf.f", "ballste.f", "balldsp.f",
            "ballapi.f")
_INCLUDES = ("ballcom.inc",)

# The version ball_version reports. A library left in the cache by an
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


class Mp3Error(Exception):
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
    ball_version check for anything that gets loaded.
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
            raise Mp3Error("could not run %s: %s" % (fc, exc))
        complaints = check(tmp) if check is not None else []
        if complaints:
            failures.append((list(extra), "\n".join(complaints)))
            os.unlink(tmp)
            continue
        os.replace(tmp, out)
        return list(extra)
    raise Mp3Error("gfortran is on PATH but could not build the MP3 "
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
# module _mp3_<digest>.dll (or one of its dependencies)" when the module it
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
        raise Mp3Error("%s does not begin with a DOS header" % path)
    pe = u32(0x3C)
    if data[pe:pe + 4] != b"PE\0\0":
        raise Mp3Error("%s has no PE signature at 0x%x" % (path, pe))
    sections_n = u16(pe + 6)
    optional_size = u16(pe + 20)
    optional = pe + 24
    magic = u16(optional)
    if magic == 0x10B:                          # PE32
        directories = optional + 96
    elif magic == 0x20B:                        # PE32+
        directories = optional + 112
    else:
        raise Mp3Error("%s: unknown optional header magic 0x%04x"
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
        raise Mp3Error("%s: RVA 0x%x falls in no section" % (path, rva))

    names, entry = [], offset(imports)
    while True:
        descriptor = data[entry:entry + 20]
        # The table ends with an all-zero descriptor and nothing else. A table
        # that runs off the end of the file has no end, which is a truncated
        # file and not a library with no dependencies -- and answering "none"
        # for it would be the one wrong answer this whole check cannot afford.
        if len(descriptor) < 20:
            raise Mp3Error("%s: the import table runs off the end" % path)
        if not any(descriptor):
            break
        start = offset(u32(entry + 12))
        end = data.find(b"\0", start)
        if end < 0:
            raise Mp3Error("%s: an import name runs off the end" % path)
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
    return "_mp3_%s%s" % (_digest(), _library_suffix())


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
        raise Mp3Error("the fortran/ directory is missing from this checkout")
    if fc is None:
        fc = _find_gfortran()
    if fc is None:
        raise Mp3Error("no gfortran on PATH")
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
            raise Mp3Error(
                "the decoder needs %s, and neither the compiler's directory "
                "nor PATH has %s. A bundle shipped like this would install, "
                "start, and play video with no sound."
                % (", ".join(still), "it" if len(still) == 1 else "them"))
    return out


_ENTRY_POINTS = ("ball_version", "ball_reset", "ball_zero_tools",
                 "ball_flush", "ball_config", "ball_header", "ball_decode",
                 "ball_pcm", "ball_frame", "ball_granule", "ball_tools",
                 "ball_is", "ball_xrq", "ball_xr", "ball_scf", "ball_bands",
                 "ball_window", "ball_imdct", "ball_htable", "ball_save",
                 "ball_restore")


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
    lib.ball_version(ctypes.byref(version))
    if version.value != _ABI:
        raise Mp3Error("the decoder at %s reports ABI %d, expected %d -- it "
                       "was built from a different version of fortran/ and "
                       "cannot be called by this one. Delete it; a checkout "
                       "will build another." % (path, version.value, _ABI))
    lib.ball_reset()
    return lib


def _open_library():
    if not os.path.isdir(_FORTRAN):
        raise Mp3Error("the fortran/ directory is missing from this checkout")
    # A library the packaging built and shipped beside this file. Preferred
    # over compiling, because in a bundle there is no compiler to fall back
    # on -- and a shipped library that will not load is worth saying out
    # loud, so its failure is only swallowed if there is a gfortran to try.
    shipped = prebuilt_path()
    failure = None
    if os.path.exists(shipped):
        try:
            return _load(shipped)
        except (Mp3Error, OSError) as exc:
            failure = Mp3Error("the bundled MP3 decoder %s did not load: %s"
                               % (prebuilt_name(), exc))
    fc = _find_gfortran()
    if fc is None:
        raise failure or Mp3Error(
            "no gfortran on PATH, and no MP3 decoder was shipped beside this "
            "module. Install gfortran -- brew install gcc, dnf install "
            "gcc-gfortran, MinGW-w64 on Windows -- or use a packaged build, "
            "which carries the decoder inside it.")
    out = os.path.join(tempfile.gettempdir(),
                       "feetbrowser_mp3_%s_%s%s"
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
        except (Mp3Error, OSError) as exc:
            _lib = None
            _load_error = str(exc)
        _loaded = True
    return _lib


def available():
    """True when this machine can decode MP3.

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
    return _load_error or "the MP3 decoder is not available"



def _config(lib, channels):
    """ball_config, with the library already reserved."""
    global _owner
    _owner = None
    lib.ball_reset()
    info = (ctypes.c_int * 8)()
    lib.ball_config(ctypes.byref(ctypes.c_int(channels)), info)
    return list(info)


# -- what the Fortran says ---------------------------------------------------

# The numbers are grouped by what produced them, so that a bug report says
# where to look: -1..-9 the caller and the bytes it handed over, -10..-19 the
# bitstream contradicting itself, -20..-29 a tool this decoder refuses by name
# rather than mis-decode.
_STATUS = {
    -3: "MP3: an empty frame",
    -4: "MP3: a frame larger than the decoder's buffer",
    -5: "MP3: no frame sync where one was expected",
    -6: "MP3: fewer bytes than a frame header",
    -8: "MP3: no decoded frame to read samples from",
    -9: "MP3: the samples did not fit their buffer",
    -10: "MP3: a frame whose CRC does not match its own header and side "
         "information",
    -11: "MP3: a reserved sampling frequency",
    -12: "MP3: a reserved bitrate index",
    -13: "MP3: the frame's main data ended in the middle of a granule",
    -15: "MP3: a frame shorter than the side information it claims",
    -16: "MP3: window switching signalled with a block type of zero, which "
         "no encoder can have meant",
    -17: "MP3: a granule with more big values than a granule can hold",
    -18: "MP3: the Huffman tables did not build -- this is a fault in the "
         "decoder, not in the stream",
    -20: "MP3: Layer I, which this decoder does not implement -- it decodes "
         "Layer III only, and a .mp1 file needs a different decoder",
    -21: "MP3: Layer II, which this decoder does not implement -- it decodes "
         "Layer III only, and a .mp2 file needs a different decoder",
    -22: "MP3: a free-format bitrate, which carries no bitrate in the header "
         "at all and so has no frame length this decoder can trust. Re-encode "
         "the file at any standard bitrate and it will play",
    -23: "MP3: more than two channels -- this decoder does mono and stereo",
    -24: "MP3: a reserved layer",
    -25: "MP3: a reserved MPEG version",
}


def _explain(status):
    return _STATUS.get(status, "MP3: decoder status %d" % status)


# -- framing -----------------------------------------------------------------
#
# Finding frames is done here rather than in the Fortran because it is
# framing rather than decoding, and because a caller often wants the frame
# boundaries -- a duration, a seek point -- without decoding anything.

_MPEG_VERSION = {0: "MPEG-2.5", 2: "MPEG-2", 3: "MPEG-1"}


def _id3_length(data):
    """How many bytes of ID3v2 tag sit at the front, if any.

    Tags are not part of the audio and a sync-word search that does not skip
    them will happily find a sync pattern inside cover art. The size is a
    syncsafe integer: seven bits of each of four bytes, so that the size
    field itself can never contain a sync word.
    """
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    total = 10 + size
    # A footer, if the flags say there is one.
    if data[5] & 0x10:
        total += 10
    return total if total <= len(data) else 0


def frames(data):
    """Walk an MPEG audio stream, yielding ``(offset, length)`` per frame.

    Resynchronises rather than raising: a stream with a tag in the middle,
    or a byte of rubbish between frames, is a stream a player is expected
    to keep playing. What it will not do is accept a sync word it cannot
    turn into a frame length, so a false sync inside audio data costs one
    byte of search and not a frame.
    """
    lib = _library()
    if lib is None:
        raise Mp3Error(unavailable_reason())
    pos = _id3_length(data)
    end = len(data)
    info = (ctypes.c_int * 16)()
    while pos + 4 <= end:
        if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue
        # Only the header is parsed, so only the header is copied. Handing
        # over the whole remainder of the file would make walking a stream
        # quadratic in its length, which is what a caller uses this to
        # avoid.
        chunk = bytes(data[pos:pos + 4])
        size = ctypes.c_int(len(chunk))
        buf = (ctypes.c_char * len(chunk)).from_buffer_copy(chunk)
        # The lock is taken per header and not around the walk. This is a
        # generator, and a caller that decodes each frame as it is yielded
        # -- which is the whole point of yielding them -- would deadlock
        # against a lock held across the yield.
        with _LOCK:
            lib.ball_header(buf, ctypes.byref(size), info)
        length = info[1]
        if info[0] != 0 or length <= 0 or pos + length > end:
            pos += 1
            continue
        yield pos, length
        pos += length


def frame_header(data):
    """What one frame header says, as a dict, without decoding the frame.

    ``None`` if there is no usable header at the front of ``data``. A
    demuxer walks a file with this, and walking a file should not cost a
    transform.
    """
    lib = _library()
    if lib is None:
        raise Mp3Error(unavailable_reason())
    if len(data) < 4:
        return None
    info = (ctypes.c_int * 16)()
    size = ctypes.c_int(len(data))
    buf = (ctypes.c_char * len(data)).from_buffer_copy(bytes(data))
    with _LOCK:
        lib.ball_header(buf, ctypes.byref(size), info)
    if info[0] != 0:
        return None
    return {
        "frame_length": info[1],
        "sample_rate": info[2],
        "channels": info[3],
        "bitrate": info[4] * 1000,
        "samples": info[5],
        "version": _MPEG_VERSION.get(info[6], "reserved"),
        "mode": info[7],
        "mode_extension": info[8],
    }


# -- the decoder -------------------------------------------------------------

class Decoder:
    """One Layer III stream, decoded a frame at a time.

    Every instance shares the library's single set of COMMON blocks, so
    every call takes ``_LOCK``. That is the price of a decoder whose state
    is static storage, and it is paid here rather than in the caller.

    Sharing is sharper for Layer III than for anything else in this tree.
    There is no key frame: a frame's samples are the second half of the
    previous frame's windowed transform added to the first half of this
    one's, the synthesis filterbank carries a thousand-sample history of
    its own, and the bit reservoir means this frame's main data may
    physically live in frames already gone past. So each decoder keeps its
    own copy of all three and puts them back whenever it finds another
    decoder has been at the library in between. Two ``<audio>`` elements
    playing at once are then slow and correct rather than fast and
    clicking.
    """

    # What ball_save wants: 576 overlap samples and 1024 filterbank history
    # per channel, and the reservoir plus its bookkeeping.
    _DOUBLES = 2 * (576 + 1024)
    _INTS = 4096 + 16

    def __init__(self, channels=0):
        """``channels`` is what a container claimed, if anything did.

        Zero means nobody claimed one, which is the usual case: a bare
        ``.mp3`` file is self-describing and every frame says how many
        channels it has. A container that says five is refused by name
        rather than decoded as the first two channels of something wider.
        """
        lib = _library()
        if lib is None:
            raise Mp3Error(unavailable_reason())
        self._lib = lib
        self._doubles = None
        self._ints = None
        with _LOCK:
            info = _config(lib, channels)
            if info[0] != 0:
                raise Mp3Error(_explain(info[0]))
            global _owner
            _owner = self
        self._channels_claimed = channels
        # Filled in by the first frame that decodes. A stream's rate and
        # channel count are properties of its frames, not of a header at
        # the front, because there is no header at the front.
        self.sample_rate = 0
        self.channels = 0
        self.bitrate = 0
        self.version = None
        # What the last frame cost and what it was promised, per granule
        # and channel. A granule that stops anywhere but exactly at
        # part2_3_length is one this decoder read differently from the
        # encoder that wrote it, and that is worth seeing from outside.
        self.last_bits = []
        self.last_bits_promised = []
        self.last_reservoir = 0
        self.last_starved = False

    def reset(self):
        """Forget everything carried between frames: the reservoir, the
        transform overlap and the filterbank history.

        This is what a seek needs, and it is not free of consequence. The
        first frame after it has no reservoir to draw on, so if it wanted
        one its granules decode as silence; and its transform is overlapped
        against zeros, so its first half is attenuated. That is why seeking
        in an MP3 means starting a frame or two early and throwing the
        result away, and it is a property of the format rather than of this
        decoder.
        """
        with _LOCK:
            self._doubles = None
            self._ints = None
            if _owner is self:
                self._lib.ball_flush()

    def _enter(self):
        """Make the library ours. _LOCK is already held."""
        global _owner
        if _owner is self:
            return
        info = _config(self._lib, self._channels_claimed)
        if info[0] != 0:
            raise Mp3Error(_explain(info[0]))
        if self._doubles is not None:
            self._lib.ball_restore(
                self._doubles, ctypes.byref(ctypes.c_int(self._DOUBLES)),
                self._ints, ctypes.byref(ctypes.c_int(self._INTS)),
                ctypes.byref(ctypes.c_int(0)))
        _owner = self

    def _leave(self):
        """Take our continuity back out of the library."""
        if self._doubles is None:
            self._doubles = (ctypes.c_double * self._DOUBLES)()
            self._ints = (ctypes.c_int * self._INTS)()
        self._lib.ball_save(
            self._doubles, ctypes.byref(ctypes.c_int(self._DOUBLES)),
            self._ints, ctypes.byref(ctypes.c_int(self._INTS)),
            ctypes.byref(ctypes.c_int(0)))

    def decode(self, packet):
        """One frame in, ``(samples per channel, channels, interleaved
        float32 bytes)`` out.

        ``packet`` starts at the frame's sync word and may be longer than
        the frame; the header says how long the frame really is and the
        rest is ignored, so a caller can hand over the whole remainder of a
        file.

        The samples are floats in [-1, 1] and are not clipped: the inverse
        transform of a loud frame can exceed unity by a little, and it is
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

    def decode_stream(self, data):
        """A whole file in, one ``(samples, channels, bytes)`` out.

        Every frame decoded and concatenated, which is what a test wants
        and what the command line below prints. A player wants ``decode()``
        and its own framing.
        """
        pcm = bytearray()
        total = 0
        for offset, length in frames(data):
            count, _channels, chunk = self.decode(data[offset:offset + length])
            total += count
            pcm += chunk
        return total, self.channels, bytes(pcm)

    def _feed(self, data):
        """One frame through the Fortran. _LOCK is already held, and the
        library is ours."""
        global _owner
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        size = ctypes.c_int(len(data))
        info = (ctypes.c_int * 16)()
        self._lib.ball_decode(buf, ctypes.byref(size), info)
        if info[0] != 0:
            # The library now holds a reservoir and an overlap that no
            # longer match this decoder's history. Disown it rather than
            # let the next call overlap-add onto wreckage.
            _owner = None
            raise Mp3Error(_explain(info[0]))
        self.channels = info[2]
        self.sample_rate = info[7]
        self.last_reservoir = info[5]
        self.last_starved = bool(info[6])
        return info

    def _read(self, info):
        count, channels = info[1], info[2]
        need = count * channels
        if need <= 0:
            raise Mp3Error("MP3: the decoder produced no samples")
        out = (ctypes.c_float * need)()
        status = ctypes.c_int(0)
        self._lib.ball_pcm(out, ctypes.byref(ctypes.c_int(need)),
                           ctypes.byref(status))
        if status.value < 0:
            raise Mp3Error(_explain(status.value))
        return count, channels, bytes(memoryview(out).cast("B"))

    # -- what the tests look at ---------------------------------------------
    #
    # A decoder compared only against its own output is compared against
    # nothing, and a tolerance at the end of a chain this long hides almost
    # any bug in the middle of it. These expose the stages that can be
    # compared exactly -- the quantised integers, the requantised values,
    # the spectrum the transform is given, the scalefactors, and what each
    # granule cost against what its side information promised -- so that a
    # difference can be located rather than merely detected. They read
    # whatever the last decode left behind and are only meaningful straight
    # after one.

    def _snapshot(self, entry, channel, kind, count):
        buf = (kind * count)()
        status = ctypes.c_int(0)
        with _LOCK:
            self._enter()
            entry(ctypes.byref(ctypes.c_int(channel)), buf,
                  ctypes.byref(ctypes.c_int(count)), ctypes.byref(status))
        if status.value < 0:
            raise Mp3Error(_explain(status.value))
        return list(buf)[:status.value]

    def quantised_spectrum(self, channel=1):
        """The last granule's Huffman-decoded integers for one channel,
        before anything was done to them. Exactly comparable."""
        return self._snapshot(self._lib.ball_is, channel, ctypes.c_int, 576)

    def requantised_spectrum(self, channel=1):
        """The same values requantised, in bitstream order: before the
        stereo tools, the reorder, the aliasing and the transform.

        Bitstream order matters. This is the one point at which the
        requantisation formula can be checked against itself recomputed,
        because everything after it has moved the coefficients around.
        """
        return self._snapshot(self._lib.ball_xrq, channel, ctypes.c_double,
                              576)

    def spectrum(self, channel=1):
        """The coefficients as the inverse transform receives them: after
        stereo, after the short-block reorder, after alias reduction."""
        return self._snapshot(self._lib.ball_xr, channel, ctypes.c_double,
                              576)

    def scalefactors(self, channel=1):
        """``(scalefactors, long bands, short windows, intensity scale)``.

        The scalefactors are flat and in the order the bitstream sent them,
        which is also the order the requantiser walks them: 21 long bands,
        or the long part of a mixed block followed by three interleaved
        short windows.
        """
        out = self._snapshot(self._lib.ball_scf, channel, ctypes.c_int, 43)
        return out[:40], out[40], out[41], out[42]

    def granule(self, granule=1, channel=1):
        """One granule's side information and what it actually cost."""
        buf = (ctypes.c_int * 24)()
        status = ctypes.c_int(0)
        with _LOCK:
            self._enter()
            self._lib.ball_granule(ctypes.byref(ctypes.c_int(granule)),
                                   ctypes.byref(ctypes.c_int(channel)), buf,
                                   ctypes.byref(ctypes.c_int(24)),
                                   ctypes.byref(status))
        if status.value < 0:
            raise Mp3Error(_explain(status.value))
        return {
            "part2_3_length": buf[0],
            "big_values": buf[1],
            "global_gain": buf[2],
            "scalefac_compress": buf[3],
            "window_switching": buf[4],
            "block_type": buf[5],
            "mixed_block": buf[6],
            "table_select": [buf[7], buf[8], buf[9]],
            "subblock_gain": [buf[10], buf[11], buf[12]],
            "region0_count": buf[13],
            "region1_count": buf[14],
            "preflag": buf[15],
            "scalefac_scale": buf[16],
            "count1table_select": buf[17],
            "bits_used": buf[18],
            "bits_promised": buf[19],
            "region0_start": buf[20],
            "region1_start": buf[21],
            "rzero": buf[22],
            "scfsi": buf[23],
        }

    def frame(self):
        """The last frame's header and shape, as a dict."""
        buf = (ctypes.c_int * 20)()
        status = ctypes.c_int(0)
        with _LOCK:
            self._enter()
            self._lib.ball_frame(buf, ctypes.byref(ctypes.c_int(20)),
                                 ctypes.byref(status))
        if status.value < 0:
            raise Mp3Error(_explain(status.value))
        return {
            "version": _MPEG_VERSION.get(buf[0], "reserved"),
            "lsf": buf[1],
            "sample_rate": buf[2],
            "bitrate": buf[3] * 1000,
            "channels": buf[4],
            "granules": buf[5],
            "mode": buf[6],
            "mode_extension": buf[7],
            "ms_stereo": buf[8],
            "intensity_stereo": buf[9],
            "main_data_begin": buf[10],
            "frame_length": buf[11],
            "side_info_length": buf[12],
            "crc": buf[13],
            "padding": buf[14],
            "sfindex": buf[15],
            "starved": buf[16],
            "reservoir_bytes": buf[17],
            "emphasis": buf[18],
            "frames_decoded": buf[19],
        }


# -- the tools a stream actually used ----------------------------------------

# The order ball_tools reports in. This exists so that a test can assert its
# vectors reach the code rather than assert a tolerance over code nothing
# runs: a threshold proves nothing about a stage no frame exercises, and a
# coverage claim nobody measured is a coverage claim that is wrong.
TOOLS = ("long_blocks", "start_blocks", "short_blocks", "stop_blocks",
         "mixed_blocks", "count1_table_a", "count1_table_b", "ms_bands",
         "intensity_bands", "reservoir_frames", "max_main_data_begin",
         "preflag_granules", "scalefac_scale_granules", "scfsi_bands",
         "granules", "starved_frames")


def zero_tools():
    """Forget which tools have been used, so that one file's coverage can
    be measured without the file before it counting towards it."""
    lib = _library()
    if lib is None:
        raise Mp3Error(unavailable_reason())
    with _LOCK:
        lib.ball_zero_tools()


def tools():
    """Which tools have been used since ``zero_tools()``.

    A dict of the names in ``TOOLS``, plus ``huffman_tables``: how many
    regions each of the 32 table selections was used for.
    """
    lib = _library()
    if lib is None:
        raise Mp3Error(unavailable_reason())
    buf = (ctypes.c_int * 64)()
    status = ctypes.c_int(0)
    with _LOCK:
        lib.ball_tools(buf, ctypes.byref(ctypes.c_int(64)),
                       ctypes.byref(status))
    if status.value < 0:
        raise Mp3Error(_explain(status.value))
    out = dict(zip(TOOLS, list(buf)[:len(TOOLS)]))
    out["huffman_tables"] = list(buf)[len(TOOLS):len(TOOLS) + 32]
    return out


# -- pieces held to their definitions ----------------------------------------

def bands(sfindex, short=False):
    """The scalefactor band boundaries in force at one sampling frequency.

    Twenty-three long boundaries or fourteen short ones, in coefficients.
    Exposed so that a test can hold them against the standard's own tables
    rather than against themselves.
    """
    lib = _library()
    if lib is None:
        raise Mp3Error(unavailable_reason())
    count = 14 if short else 23
    dst = (ctypes.c_int * count)()
    status = ctypes.c_int(0)
    with _LOCK:
        lib.ball_bands(ctypes.byref(ctypes.c_int(sfindex)),
                       ctypes.byref(ctypes.c_int(1 if short else 0)), dst,
                       ctypes.byref(ctypes.c_int(count)),
                       ctypes.byref(status))
    if status.value < 0:
        raise Mp3Error(_explain(status.value))
    return list(dst)


def window(which):
    """One window shape: 0 normal, 1 start, 2 short, 3 stop, 4 the 512-tap
    synthesis window. The first four are 36 points; the short one is twelve
    points followed by zeros, because it is used three times inside a
    36-point frame rather than once across it."""
    lib = _library()
    if lib is None:
        raise Mp3Error(unavailable_reason())
    count = 512 if which == 4 else 36
    dst = (ctypes.c_double * count)()
    status = ctypes.c_int(0)
    with _LOCK:
        lib.ball_window(ctypes.byref(ctypes.c_int(which)), dst,
                        ctypes.byref(ctypes.c_int(count)),
                        ctypes.byref(status))
    if status.value < 0:
        raise Mp3Error(_explain(status.value))
    return list(dst)


def imdct(coefficients):
    """The inverse MDCT on its own, unwindowed.

    Exposed so that the transform can be held against the standard's
    summation written out independently rather than against itself. 18
    coefficients in and 36 samples out, or 6 in and 12 out.
    """
    lib = _library()
    if lib is None:
        raise Mp3Error(unavailable_reason())
    size = len(coefficients)
    if size not in (6, 18):
        raise Mp3Error("MP3: the transform is 6 or 18 points")
    src = (ctypes.c_double * size)(*coefficients)
    dst = (ctypes.c_double * (2 * size))()
    with _LOCK:
        lib.ball_imdct(src, ctypes.byref(ctypes.c_int(0 if size == 18 else 1)),
                       dst)
    return list(dst)


def huffman_table(which):
    """One Huffman code table as ``(xlen, ylen, codeword lengths)``.

    The lengths are indexed ``x * ylen + y``. Exposed so that a test can
    check the decoder's trees were built from the standard's codes -- by
    holding them to the Kraft equality, which a tree built from the wrong
    lengths cannot satisfy -- rather than check the trees against
    themselves.
    """
    lib = _library()
    if lib is None:
        raise Mp3Error(unavailable_reason())
    dst = (ctypes.c_int * 258)()
    status = ctypes.c_int(0)
    with _LOCK:
        lib.ball_htable(ctypes.byref(ctypes.c_int(which)), dst,
                        ctypes.byref(ctypes.c_int(258)),
                        ctypes.byref(status))
    if status.value < 0:
        raise Mp3Error(_explain(status.value))
    xlen, ylen = dst[0], dst[1]
    return xlen, ylen, list(dst)[2:2 + xlen * ylen]


# -- proving a build can actually decode ------------------------------------

# How far a decoded sample may sit from the reference decoder's. Layer III is
# not a bit-exact specification -- the standard defines the inverse transform
# and the filterbank in real arithmetic -- so the comparison is numerical.
#
# One number here, unlike in tests/test_mp3.py, which sets a threshold per
# vector because the spread across them is forty decibels wide. This one is
# not measuring a decoder, it is asking whether a freshly built library
# decodes audio or rubbish, and it has to hold for whichever vector the
# packaging happens to hand it. Three times the worst error measured across
# the eighteen vectors, which is 7.8e-05 on a hard transient: room for a
# different libm's cosine in the transform tables and not room for a
# decoding bug, which puts a sample out by tenths.
CHECK_TOLERANCE = 2.5e-04


def check(args, out=print):
    """Can this build decode audio, and does it decode right? An exit code.

    Lives here rather than in ``__main__`` because the interesting caller is
    the packaging, and because the answer is this module's to give: a bundle
    that shipped no decoder starts, renders, plays video and says nothing
    about sound until somebody opens a file, so all three packaging scripts
    ask here instead, once, at build time and with the compiler off PATH.

    With an ``.mp3`` file it decodes one; with the float32 samples a
    reference decoder produced from that file it compares them, because a
    decoder that loads and returns rubbish has passed no test worth having.
    """
    reason = unavailable_reason()
    if reason is not None:
        out("audio: no MP3 decoder: %s" % reason)
        return 1
    out("audio: MP3 decoder ready, from %s" % library_path())
    if not args:
        return 0
    try:
        with open(args[0], "rb") as handle:
            stream = handle.read()
    except OSError as exc:
        out("audio: cannot read %s: %s" % (args[0], exc.strerror or exc))
        return 1
    try:
        decoder = Decoder()
        count, channels, pcm = decoder.decode_stream(stream)
    except Mp3Error as exc:
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


_USAGE = """usage: python3 -m feetbrowser.ball \
[--name | --build PATH [--fc GFORTRAN] | --check [stream.mp3 [truth.f32.z]] \
| FILE...]

  --name          print the filename a prebuilt library must have
  --build PATH    compile the decoder for shipping, into PATH
  --fc GFORTRAN   which compiler --build should use
  --check         say whether this build can decode MP3, and prove it
  FILE...         decode an .mp3 file and say what came out
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
        except Mp3Error as exc:
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
        decoder = Decoder()
        count, channels, pcm = decoder.decode_stream(blob)
        print("%s: %d samples x %d channels at %d Hz (%.2f s)"
              % (path, count, channels, decoder.sample_rate,
                 count / float(decoder.sample_rate or 1)))
    return 0


if __name__ == "__main__":                      # pragma: no cover
    sys.exit(_cli(sys.argv[1:]))
