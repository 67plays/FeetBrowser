"""TrueType font parsing: metrics, character mapping, and glyph outlines.

This replaces the font half of Tk. Everything the layout engine needs from a
font comes from tables in the file itself:

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
"""
import os
import struct
import sys

# Where to look for fonts, most-specific first. User directories win so a
# locally installed family shadows the system copy of the same name.
FONT_DIRS = {
    "darwin": ["~/Library/Fonts", "/Library/Fonts", "/System/Library/Fonts",
               "/System/Library/Fonts/Supplemental"],
    "win32": ["~/AppData/Local/Microsoft/Windows/Fonts", "C:/Windows/Fonts"],
}
FONT_DIRS_DEFAULT = ["~/.fonts", "~/.local/share/fonts",
                     "/usr/share/fonts", "/usr/local/share/fonts"]

# Glyph outline flags (spec: "Simple Glyph Description").
_ON_CURVE = 0x01
_X_SHORT = 0x02
_Y_SHORT = 0x04
_REPEAT = 0x08
_X_SAME = 0x10
_Y_SAME = 0x20

# Composite glyph flags (spec: "Composite Glyph Description").
_ARGS_ARE_WORDS = 0x0001
_ARGS_ARE_XY = 0x0002
_HAVE_SCALE = 0x0008
_MORE_COMPONENTS = 0x0020
_HAVE_XY_SCALE = 0x0040
_HAVE_2X2 = 0x0080


class FontError(Exception):
    """Raised when a file is not a font we can read."""


def _dirs():
    raw = FONT_DIRS.get(sys.platform, FONT_DIRS_DEFAULT)
    return [os.path.expanduser(d) for d in raw]


class Font:
    """One parsed font face.

    Metric accessors return font units; callers scale by ``self.scale(size)``.
    Glyph outlines are cached because composites can require several lookups.
    """

    def __init__(self, data, index=0):
        self.data = data
        self.tables = {}
        self._cmap = None
        self._glyph_cache = {}
        self._read_directory(index)
        self._read_head()
        self._read_hhea()
        self._read_maxp()
        self._read_hmtx()

    # -- container -------------------------------------------------------

    def _read_directory(self, index):
        d = self.data
        if len(d) < 12:
            raise FontError("file too short to be a font")
        tag = d[:4]
        offset = 0
        if tag == b"ttcf":
            # A collection: the real font directories start further in.
            count = struct.unpack(">I", d[8:12])[0]
            if index >= count:
                raise FontError(f"collection has {count} fonts, wanted {index}")
            offset = struct.unpack(">I", d[12 + index * 4:16 + index * 4])[0]
            tag = d[offset:offset + 4]
        if tag not in (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO"):
            raise FontError(f"unrecognised sfnt tag {tag!r}")
        if tag == b"OTTO":
            # CFF outlines: metrics are readable but we cannot rasterise them.
            self.cff = True
        else:
            self.cff = False
        num = struct.unpack(">H", d[offset + 4:offset + 6])[0]
        for i in range(num):
            rec = offset + 12 + i * 16
            if rec + 16 > len(d):
                break
            name, _sum, off, length = struct.unpack(">4sIII", d[rec:rec + 16])
            self.tables[name.decode("latin-1")] = (off, length)

    def _table(self, name):
        entry = self.tables.get(name)
        if not entry:
            return None
        off, length = entry
        return self.data[off:off + length]

    # -- metric tables ---------------------------------------------------

    def _read_head(self):
        head = self._table("head")
        if not head or len(head) < 54:
            raise FontError("missing or short head table")
        self.units_per_em = struct.unpack(">H", head[18:20])[0] or 1000
        self.index_to_loc = struct.unpack(">h", head[50:52])[0]

    def _read_hhea(self):
        hhea = self._table("hhea")
        if not hhea or len(hhea) < 36:
            raise FontError("missing or short hhea table")
        self.ascent, self.descent, self.line_gap = struct.unpack(
            ">hhh", hhea[4:10])
        self.num_h_metrics = struct.unpack(">H", hhea[34:36])[0]
        # Some fonts ship a zeroed hhea and put the real numbers in OS/2.
        if self.ascent == 0 and self.descent == 0:
            os2 = self._table("OS/2")
            if os2 and len(os2) >= 72:
                self.ascent, self.descent = struct.unpack(">hh", os2[68:72])
        self.descent = -abs(self.descent)

    def _read_maxp(self):
        maxp = self._table("maxp")
        self.num_glyphs = struct.unpack(">H", maxp[4:6])[0] if maxp else 0

    def _read_hmtx(self):
        self.advances = []
        hmtx = self._table("hmtx")
        if not hmtx:
            return
        n = min(self.num_h_metrics, len(hmtx) // 4)
        for i in range(n):
            self.advances.append(struct.unpack(">H", hmtx[i * 4:i * 4 + 2])[0])

    # -- public metrics --------------------------------------------------

    def scale(self, size):
        """Multiplier converting font units to pixels at ``size`` px."""
        return float(size) / self.units_per_em

    def advance(self, gid):
        """Horizontal advance for a glyph, in font units."""
        if not self.advances:
            return self.units_per_em // 2
        if gid < len(self.advances):
            return self.advances[gid]
        return self.advances[-1]  # monospaced tail

    def linespace(self):
        """Default line height in font units."""
        return self.ascent - self.descent + self.line_gap

    # -- character mapping -----------------------------------------------

    def _build_cmap(self):
        """Pick the best available subtable and decode it into a dict."""
        table = self._table("cmap")
        self._cmap = {}
        if not table or len(table) < 4:
            return
        count = struct.unpack(">H", table[2:4])[0]
        best, best_score = None, -1
        for i in range(count):
            rec = 4 + i * 8
            if rec + 8 > len(table):
                break
            plat, enc, off = struct.unpack(">HHI", table[rec:rec + 8])
            # Prefer full Unicode over BMP-only, and Windows over Mac.
            score = {(3, 10): 5, (0, 4): 5, (0, 6): 5,
                     (3, 1): 4, (0, 3): 4, (0, 2): 3, (0, 1): 3,
                     (3, 0): 2, (1, 0): 1}.get((plat, enc), 0)
            if score > best_score:
                best, best_score = off, score
        if best is None or best + 4 > len(table):
            return
        fmt = struct.unpack(">H", table[best:best + 2])[0]
        if fmt == 4:
            self._read_cmap4(table, best)
        elif fmt == 12:
            self._read_cmap12(table, best)
        elif fmt == 6:
            self._read_cmap6(table, best)
        elif fmt == 0:
            self._read_cmap0(table, best)

    def _read_cmap0(self, t, off):
        for code in range(256):
            pos = off + 6 + code
            if pos < len(t) and t[pos]:
                self._cmap[code] = t[pos]

    def _read_cmap4(self, t, off):
        seg2 = struct.unpack(">H", t[off + 6:off + 8])[0]
        seg = seg2 // 2
        ends = off + 14
        starts = ends + seg2 + 2
        deltas = starts + seg2
        ranges = deltas + seg2
        for i in range(seg):
            end = struct.unpack(">H", t[ends + i * 2:ends + i * 2 + 2])[0]
            start = struct.unpack(">H", t[starts + i * 2:starts + i * 2 + 2])[0]
            delta = struct.unpack(">h", t[deltas + i * 2:deltas + i * 2 + 2])[0]
            ro_at = ranges + i * 2
            ro = struct.unpack(">H", t[ro_at:ro_at + 2])[0]
            if start > end:
                continue
            for code in range(start, min(end, 0xFFFF) + 1):
                if ro == 0:
                    gid = (code + delta) & 0xFFFF
                else:
                    gpos = ro_at + ro + (code - start) * 2
                    if gpos + 2 > len(t):
                        continue
                    gid = struct.unpack(">H", t[gpos:gpos + 2])[0]
                    if gid:
                        gid = (gid + delta) & 0xFFFF
                if gid:
                    self._cmap[code] = gid

    def _read_cmap6(self, t, off):
        first, count = struct.unpack(">HH", t[off + 6:off + 10])
        for i in range(count):
            pos = off + 10 + i * 2
            if pos + 2 > len(t):
                break
            self._cmap[first + i] = struct.unpack(">H", t[pos:pos + 2])[0]

    def _read_cmap12(self, t, off):
        n = struct.unpack(">I", t[off + 12:off + 16])[0]
        for i in range(n):
            rec = off + 16 + i * 12
            if rec + 12 > len(t):
                break
            start, end, gid = struct.unpack(">III", t[rec:rec + 12])
            if end - start > 0x10FFFF:
                continue
            for c in range(start, end + 1):
                self._cmap[c] = gid + (c - start)

    def glyph_id(self, ch):
        """Glyph index for a character, or 0 (.notdef) when unmapped."""
        if self._cmap is None:
            self._build_cmap()
        return self._cmap.get(ord(ch), 0)

    def has_char(self, ch):
        if self._cmap is None:
            self._build_cmap()
        return ord(ch) in self._cmap

    # -- outlines --------------------------------------------------------

    def _loca(self, gid):
        """Byte range of a glyph inside glyf, or None when it is blank."""
        loca = self._table("loca")
        if not loca:
            return None
        if self.index_to_loc:
            pos = gid * 4
            if pos + 8 > len(loca):
                return None
            start, end = struct.unpack(">II", loca[pos:pos + 8])
        else:
            pos = gid * 2
            if pos + 4 > len(loca):
                return None
            start, end = struct.unpack(">HH", loca[pos:pos + 4])
            start, end = start * 2, end * 2
        return None if end <= start else (start, end)

    def glyph_contours(self, gid, depth=0):
        """Outline as a list of contours of ``(x, y, on_curve)`` font-unit points."""
        if gid in self._glyph_cache:
            return self._glyph_cache[gid]
        contours = self._parse_glyph(gid, depth)
        self._glyph_cache[gid] = contours
        return contours

    def _parse_glyph(self, gid, depth):
        if self.cff:
            return []  # CFF outlines are a different format entirely
        span = self._loca(gid)
        glyf = self._table("glyf")
        if not span or not glyf:
            return []
        start, end = span
        if end > len(glyf) or end - start < 10:
            return []
        g = glyf[start:end]
        n_contours = struct.unpack(">h", g[0:2])[0]
        if n_contours < 0:
            return self._parse_composite(g, depth)
        return self._parse_simple(g, n_contours)

    def _parse_simple(self, g, n_contours):
        pos = 10
        ends = []
        for i in range(n_contours):
            if pos + 2 > len(g):
                return []
            ends.append(struct.unpack(">H", g[pos:pos + 2])[0])
            pos += 2
        n_points = (ends[-1] + 1) if ends else 0
        if pos + 2 > len(g):
            return []
        instr = struct.unpack(">H", g[pos:pos + 2])[0]
        pos += 2 + instr

        flags = []
        while len(flags) < n_points and pos < len(g):
            f = g[pos]
            pos += 1
            flags.append(f)
            if f & _REPEAT and pos < len(g):
                rep = g[pos]
                pos += 1
                flags.extend([f] * rep)
        if len(flags) < n_points:
            return []
        flags = flags[:n_points]

        xs, x = [], 0
        for f in flags:
            if f & _X_SHORT:
                if pos >= len(g):
                    return []
                d = g[pos]
                pos += 1
                x += d if f & _X_SAME else -d
            elif not f & _X_SAME:
                if pos + 2 > len(g):
                    return []
                x += struct.unpack(">h", g[pos:pos + 2])[0]
                pos += 2
            xs.append(x)

        ys, y = [], 0
        for f in flags:
            if f & _Y_SHORT:
                if pos >= len(g):
                    return []
                d = g[pos]
                pos += 1
                y += d if f & _Y_SAME else -d
            elif not f & _Y_SAME:
                if pos + 2 > len(g):
                    return []
                y += struct.unpack(">h", g[pos:pos + 2])[0]
                pos += 2
            ys.append(y)

        contours, first = [], 0
        for last in ends:
            pts = [(xs[i], ys[i], bool(flags[i] & _ON_CURVE))
                   for i in range(first, min(last + 1, n_points))]
            if pts:
                contours.append(pts)
            first = last + 1
        return contours

    def _parse_composite(self, g, depth):
        if depth > 5:
            return []  # cyclic or absurdly nested composite
        contours = []
        pos = 10
        while pos + 4 <= len(g):
            flags, sub_gid = struct.unpack(">HH", g[pos:pos + 4])
            pos += 4
            if flags & _ARGS_ARE_WORDS:
                if pos + 4 > len(g):
                    break
                a1, a2 = struct.unpack(">hh", g[pos:pos + 4])
                pos += 4
            else:
                if pos + 2 > len(g):
                    break
                a1, a2 = struct.unpack(">bb", g[pos:pos + 2])
                pos += 2
            xx = yy = 1.0
            xy = yx = 0.0
            if flags & _HAVE_SCALE:
                xx = yy = _f2dot14(g, pos)
                pos += 2
            elif flags & _HAVE_XY_SCALE:
                xx, yy = _f2dot14(g, pos), _f2dot14(g, pos + 2)
                pos += 4
            elif flags & _HAVE_2X2:
                xx, xy = _f2dot14(g, pos), _f2dot14(g, pos + 2)
                yx, yy = _f2dot14(g, pos + 4), _f2dot14(g, pos + 6)
                pos += 8
            dx, dy = (a1, a2) if flags & _ARGS_ARE_XY else (0, 0)
            for c in self.glyph_contours(sub_gid, depth + 1):
                contours.append([
                    (int(px * xx + py * yx + dx), int(px * xy + py * yy + dy), on)
                    for px, py, on in c])
            if not flags & _MORE_COMPONENTS:
                break
        return contours

    # -- naming ----------------------------------------------------------

    def names(self):
        """``{nameID: text}`` from the name table, for family identification."""
        t = self._table("name")
        out = {}
        if not t or len(t) < 6:
            return out
        count, str_off = struct.unpack(">HH", t[2:6])
        for i in range(count):
            rec = 6 + i * 12
            if rec + 12 > len(t):
                break
            plat, enc, _lang, nid, length, off = struct.unpack(
                ">HHHHHH", t[rec:rec + 12])
            s = t[str_off + off:str_off + off + length]
            if not s:
                continue
            try:
                # Platform 3 (Windows) and platform 0 (Unicode) are UTF-16BE.
                text = s.decode("utf-16-be" if plat in (0, 3) else "latin-1")
            except UnicodeDecodeError:
                continue
            text = text.strip("\x00").strip()
            if text and (nid not in out or plat == 3):
                out[nid] = text
        return out

    @property
    def is_bold(self):
        os2 = self._table("OS/2")
        if os2 and len(os2) >= 6:
            if struct.unpack(">H", os2[4:6])[0] >= 600:
                return True
        if os2 and len(os2) >= 64:
            return bool(struct.unpack(">H", os2[62:64])[0] & 0x20)
        return False

    @property
    def is_italic(self):
        os2 = self._table("OS/2")
        if os2 and len(os2) >= 64:
            return bool(struct.unpack(">H", os2[62:64])[0] & 0x01)
        head = self._table("head")
        if head and len(head) >= 46:
            return bool(struct.unpack(">H", head[44:46])[0] & 0x02)
        return False


def _f2dot14(buf, pos):
    if pos + 2 > len(buf):
        return 1.0
    return struct.unpack(">h", buf[pos:pos + 2])[0] / 16384.0


def flatten(contours, scale, steps=8):
    """Convert quadratic contours to polygons in pixel space.

    y is flipped here: fonts put the origin on the baseline with y growing
    upward, and every surface we draw to grows downward.
    """
    polys = []
    for c in contours:
        if len(c) < 2:
            continue
        pts = _resolve_implied(c)
        poly, i, n = [], 0, len(pts)
        # Start on an on-curve point so the segment walk below is uniform.
        start = next((k for k, p in enumerate(pts) if p[2]), 0)
        cur = (pts[start][0] * scale, -pts[start][1] * scale)
        poly.append(cur)
        i = 1
        while i <= n:
            p = pts[(start + i) % n]
            px, py = p[0] * scale, -p[1] * scale
            if p[2]:
                poly.append((px, py))
                cur = (px, py)
                i += 1
                continue
            nxt = pts[(start + i + 1) % n]
            nx, ny = nxt[0] * scale, -nxt[1] * scale
            for s in range(1, steps + 1):
                t = s / steps
                u = 1 - t
                poly.append((u * u * cur[0] + 2 * u * t * px + t * t * nx,
                             u * u * cur[1] + 2 * u * t * py + t * t * ny))
            cur = (nx, ny)
            i += 2
        if len(poly) > 2:
            polys.append(poly)
    return polys


def _resolve_implied(contour):
    """Insert the on-curve midpoints TrueType leaves out between two off points."""
    out = []
    n = len(contour)
    for i, p in enumerate(contour):
        out.append(p)
        nxt = contour[(i + 1) % n]
        if not p[2] and not nxt[2]:
            out.append(((p[0] + nxt[0]) // 2, (p[1] + nxt[1]) // 2, True))
    return out


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
                    for i in range(min(count, 24)):
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
