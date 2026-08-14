"""TrueType font parsing: metrics, character mapping, and glyph outlines.

We read the font ourselves. Everything the layout engine needs from a font
comes from tables in the file itself:

    ascent / descent / linespace   <- hhea (or OS/2 when hhea is degenerate)
    advance width per glyph        <- hmtx
    character -> glyph id          <- cmap
    outline                        <- glyf + loca

Outlines come back as quadratic contours in font units; scaling to a pixel
size is a multiply by ``size / unitsPerEm``, so one parse serves every size.

Only the tables above are read. Hinting, kerning and GPOS shaping are all
deliberately skipped: the layout engine caches text widths as the sum of
per-character advances, and that identity only holds while no glyph's
placement depends on its neighbours. Adding kerning later means changing that
cache too -- see docs/rendering.md.

The parser itself -- ``Font`` and ``flatten`` -- lives in Rust, in the
`feetbrowser_engine` extension, because a page of text asks it for outlines a
few thousand times before the glyph cache is warm. What stays here is the part
that talks to the operating system: where fonts live on each platform, walking
those directories, and the family index built from what is found. That is
policy rather than parsing, it differs per platform, and it runs once.
"""
import os
import struct
import sys

from feetbrowser_engine import Font, FontError, flatten

# Where to look for fonts, most-specific first. User directories win so a
# locally installed family shadows the system copy of the same name.
FONT_DIRS = {
    "darwin": ["~/Library/Fonts", "/Library/Fonts", "/System/Library/Fonts",
               "/System/Library/Fonts/Supplemental"],
    # Per-user fonts (installed from the Settings app) first, then the
    # machine-wide store. %WINDIR% rather than a hardcoded C: -- Windows is
    # not always on C:, and a machine that moved it has no fonts at all
    # otherwise. See _dirs() for how the environment variable is filled in.
    "win32": ["~/AppData/Local/Microsoft/Windows/Fonts",
              "${WINDIR}/Fonts", "C:/Windows/Fonts"],
}
FONT_DIRS_DEFAULT = ["~/.fonts", "~/.local/share/fonts",
                     "/usr/share/fonts", "/usr/local/share/fonts"]

# Python under Cygwin and MSYS2 reports its own sys.platform but is looking
# at a Windows filesystem, so it wants the Windows font directories.
FONT_DIR_ALIASES = {"cygwin": "win32", "msys": "win32"}

__all__ = ["FONT_DIRS", "FONT_DIRS_DEFAULT", "Font", "FontError", "flatten",
           "find", "index", "load"]


def _dirs():
    """The font directories to scan, expanded and de-duplicated.

    Both ``~`` and ``${VAR}`` are expanded, because the Windows font store
    lives wherever %WINDIR% points -- and if the variable is unset,
    expandvars leaves the placeholder behind, which is not a directory and so
    drops out on its own.
    """
    platform = FONT_DIR_ALIASES.get(sys.platform, sys.platform)
    raw = FONT_DIRS.get(platform, FONT_DIRS_DEFAULT)
    dirs, seen = [], set()
    for entry in raw:
        path = os.path.normpath(
            os.path.expanduser(os.path.expandvars(entry)))
        # normcase, because %WINDIR%/Fonts and C:/Windows/Fonts are the same
        # directory spelled two ways and scanning it twice is pure waste.
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            dirs.append(path)
    return dirs


# -- system font discovery -----------------------------------------------

_INDEX = None


def _scan():
    """Map lowercased family name -> {(bold, italic): path} across the system."""
    index = {}
    for d in _dirs():
        if not os.path.isdir(d):
            continue
        for root, _dirs_, files in os.walk(d):
            for fn in files:
                if not fn.lower().endswith((".ttf", ".ttc", ".otf")):
                    continue
                path = os.path.join(root, fn)
                try:
                    with open(path, "rb") as f:
                        head = f.read(4)
                        f.seek(0)
                        data = f.read()
                    count = 1
                    if head == b"ttcf":
                        count = struct.unpack(">I", data[8:12])[0]
                    # A cap, because the count comes off disk and a corrupt
                    # one would have us parsing the same file forever. 64 is
                    # well clear of the real collections Windows ships.
                    for i in range(min(count, 64)):
                        font = Font(data, i)
                        if font.cff:
                            continue  # metrics only; cannot rasterise
                        names = font.names()
                        family = names.get(16) or names.get(1)
                        if not family:
                            continue
                        key = family.lower()
                        slot = (font.is_bold, font.is_italic)
                        index.setdefault(key, {}).setdefault(slot, (path, i))
                except (OSError, FontError, struct.error, IndexError):
                    continue
    return index


def index(refresh=False):
    """The system font index, scanned once per process."""
    global _INDEX
    if _INDEX is None or refresh:
        _INDEX = _scan()
    return _INDEX


_LOADED = {}


def load(path, face=0):
    """Parse a font file, caching by path so repeated lookups are free."""
    key = (path, face)
    if key not in _LOADED:
        with open(path, "rb") as f:
            _LOADED[key] = Font(f.read(), face)
    return _LOADED[key]


def find(family, bold=False, italic=False):
    """Best available face for a family, or None when nothing matches.

    Falls back within the family before giving up: an exact style match wins,
    then any face of that family, so a family shipping only Regular still
    renders when bold is asked for.
    """
    fam = index().get((family or "").lower())
    if not fam:
        return None
    for slot in ((bold, italic), (bold, False), (False, italic), (False, False)):
        if slot in fam:
            return load(*fam[slot])
    return load(*next(iter(fam.values())))
