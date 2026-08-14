"""Image decoders: PNG, GIF and the Netpbm family, decoded to raw RGBA.

These are the formats Tk's PhotoImage accepted natively, so decoding them
ourselves is what lets the raster backend show the same images. PNG is nearly
free because its compression is DEFLATE and zlib is in the standard library;
GIF needs a hand-written LZW decoder. JPEG is not here -- it stays on the
optional Pillow path the browser already had.

Every decoder returns ``(width, height, rgba)`` where rgba is a bytearray of
4 bytes per pixel, which is what raster.Surface.blit_rgba consumes.
"""
import struct
import zlib


class ImageError(Exception):
    """Raised for malformed or unsupported image data."""


def decode(data):
    """Decode any supported image. Returns ``(width, height, rgba)``."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return decode_png(data)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return decode_gif(data)
    if data[:2] in (b"P1", b"P2", b"P3", b"P4", b"P5", b"P6"):
        return decode_pnm(data)
    raise ImageError("unrecognised image format")


def sniff(data):
    """True if `decode` recognises this data's signature."""
    return (data[:8] == b"\x89PNG\r\n\x1a\n"
            or data[:6] in (b"GIF87a", b"GIF89a")
            or data[:2] in (b"P1", b"P2", b"P3", b"P4", b"P5", b"P6"))


# -- PNG ------------------------------------------------------------------

# Adam7: (x offset, y offset, x step, y step) for each of the seven passes.
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
          (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def decode_png(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ImageError("not a PNG")
    pos = 8
    width = height = depth = color = interlace = 0
    palette = b""
    trns = None
    idat = []
    seen_header = False
    while pos + 8 <= len(data):
        length, tag = struct.unpack(">I4s", data[pos:pos + 8])
        pos += 8
        payload = data[pos:pos + length]
        pos += length + 4  # skip the CRC; we trust the transport
        if tag == b"IHDR":
            (width, height, depth, color, _comp, _filt,
             interlace) = struct.unpack(">IIBBBBB", payload[:13])
            seen_header = True
        elif tag == b"PLTE":
            palette = payload
        elif tag == b"tRNS":
            trns = payload
        elif tag == b"IDAT":
            idat.append(payload)
        elif tag == b"IEND":
            break
    if not seen_header or not width or not height:
        raise ImageError("PNG has no usable header")
    if color not in _CHANNELS:
        raise ImageError("unsupported PNG colour type %d" % color)
    if depth not in (1, 2, 4, 8, 16):
        raise ImageError("unsupported PNG bit depth %d" % depth)

    raw = zlib.decompress(b"".join(idat))
    channels = _CHANNELS[color]

    if interlace == 1:
        samples = bytearray(width * height * channels)
        pos = 0
        for ox, oy, sx, sy in _ADAM7:
            pw = (width - ox + sx - 1) // sx
            ph = (height - oy + sy - 1) // sy
            if pw <= 0 or ph <= 0:
                continue
            size = _pass_size(pw, ph, channels, depth)
            plane = _unfilter(raw[pos:pos + size], pw, ph, channels, depth)
            pos += size
            plane = _to_bytes(plane, pw, ph, channels, depth)
            for y in range(ph):
                for x in range(pw):
                    src = (y * pw + x) * channels
                    dst = ((oy + y * sy) * width + (ox + x * sx)) * channels
                    samples[dst:dst + channels] = plane[src:src + channels]
    elif interlace == 0:
        samples = _to_bytes(_unfilter(raw, width, height, channels, depth),
                            width, height, channels, depth)
    else:
        raise ImageError("unsupported PNG interlace method %d" % interlace)

    return width, height, _to_rgba(samples, width, height, color, palette,
                                   trns, depth)


def _pass_size(width, height, channels, depth):
    return height * (1 + (width * channels * depth + 7) // 8)


def _unfilter(raw, width, height, channels, depth):
    """Reverse the per-scanline PNG filters, returning packed samples."""
    bpp = max(1, (channels * depth + 7) // 8)
    stride = (width * channels * depth + 7) // 8
    out = bytearray(stride * height)
    pos = 0
    prev = bytearray(stride)
    for y in range(height):
        if pos >= len(raw):
            break
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if len(line) < stride:
            line.extend(b"\x00" * (stride - len(line)))
        if ftype == 1:      # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:    # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:    # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    pred = a
                elif pb <= pc:
                    pred = b
                else:
                    pred = c
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            raise ImageError("bad PNG filter type %d" % ftype)
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return out


def _to_bytes(packed, width, height, channels, depth):
    """Expand packed samples to one byte per sample, scaled to 0..255."""
    if depth == 8:
        return packed
    count = width * channels
    out = bytearray(count * height)
    if depth == 16:
        stride = count * 2
        for y in range(height):
            src = y * stride
            out[y * count:(y + 1) * count] = packed[src:src + stride:2]
        return out
    stride = (count * depth + 7) // 8
    mask = (1 << depth) - 1
    per_byte = 8 // depth
    # For greyscale, scale the sample up to full range; palette indices must
    # stay untouched, but callers pass channels==1 for both, so scaling is
    # applied by the caller instead. Here we only unpack.
    for y in range(height):
        base = y * stride
        o = y * count
        for i in range(count):
            byte = packed[base + i // per_byte]
            shift = 8 - depth * (i % per_byte + 1)
            out[o + i] = (byte >> shift) & mask
    return out


def _to_rgba(samples, width, height, color, palette, trns, depth):
    n = width * height
    rgba = bytearray(n * 4)
    # Sub-byte greyscale arrives as raw levels; stretch them to 0..255.
    scale = 255 // ((1 << depth) - 1) if depth < 8 and color != 3 else 1

    if color == 3:
        if not palette:
            raise ImageError("indexed PNG without a palette")
        alpha = trns or b""
        for i in range(n):
            idx = samples[i]
            o = idx * 3
            d = i * 4
            if o + 2 < len(palette):
                rgba[d] = palette[o]
                rgba[d + 1] = palette[o + 1]
                rgba[d + 2] = palette[o + 2]
            rgba[d + 3] = alpha[idx] if idx < len(alpha) else 255
    elif color == 0:
        key = None
        if trns and len(trns) >= 2:
            key = struct.unpack(">H", trns[:2])[0] >> (8 if depth == 16 else 0)
        for i in range(n):
            v = samples[i] * scale
            d = i * 4
            rgba[d] = rgba[d + 1] = rgba[d + 2] = v
            rgba[d + 3] = 0 if (key is not None and samples[i] == key) else 255
    elif color == 4:
        for i in range(n):
            s, d = i * 2, i * 4
            v = samples[s]
            rgba[d] = rgba[d + 1] = rgba[d + 2] = v
            rgba[d + 3] = samples[s + 1]
    elif color == 2:
        key = None
        if trns and len(trns) >= 6:
            r, g, b = struct.unpack(">HHH", trns[:6])
            if depth == 16:
                r, g, b = r >> 8, g >> 8, b >> 8
            key = (r, g, b)
        for i in range(n):
            s, d = i * 3, i * 4
            r, g, b = samples[s], samples[s + 1], samples[s + 2]
            rgba[d], rgba[d + 1], rgba[d + 2] = r, g, b
            rgba[d + 3] = 0 if key == (r, g, b) else 255
    else:  # color == 6
        rgba[:] = samples[:n * 4]
    return rgba


# -- GIF ------------------------------------------------------------------

def decode_gif(data):
    """Decode a GIF's first frame. Animation is out of scope, as it was for
    Tk's PhotoImage, which also showed only the first frame."""
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ImageError("not a GIF")
    screen_w, screen_h, flags, _bg, _ar = struct.unpack("<HHBBB", data[6:13])
    pos = 13
    global_table = b""
    if flags & 0x80:
        size = 3 * (2 << (flags & 0x07))
        global_table = data[pos:pos + size]
        pos += size

    transparent = None
    while pos < len(data):
        block = data[pos]
        if block == 0x21:  # extension
            label = data[pos + 1]
            pos += 2
            if label == 0xF9 and data[pos] >= 4:  # graphic control
                gflags = data[pos + 1]
                if gflags & 0x01:
                    transparent = data[pos + 4]
            pos = _skip_blocks(data, pos)
        elif block == 0x2C:  # image descriptor
            left, top, w, h, iflags = struct.unpack("<HHHHB",
                                                    data[pos + 1:pos + 10])
            pos += 10
            table = global_table
            if iflags & 0x80:
                size = 3 * (2 << (iflags & 0x07))
                table = data[pos:pos + size]
                pos += size
            min_code = data[pos]
            pos += 1
            chunks = []
            while pos < len(data) and data[pos]:
                n = data[pos]
                chunks.append(data[pos + 1:pos + 1 + n])
                pos += 1 + n
            indices = _lzw(b"".join(chunks), min_code, w * h)
            if iflags & 0x40:
                indices = _deinterlace(indices, w, h)
            return _gif_to_rgba(indices, w, h, table, transparent)
        elif block == 0x3B:  # trailer
            break
        else:
            raise ImageError("unexpected GIF block 0x%02X" % block)
    raise ImageError("GIF contains no image")


def _skip_blocks(data, pos):
    while pos < len(data) and data[pos]:
        pos += data[pos] + 1
    return pos + 1


def _deinterlace(indices, w, h):
    out = bytearray(w * h)
    rows = ([r for r in range(0, h, 8)] + [r for r in range(4, h, 8)]
            + [r for r in range(2, h, 4)] + [r for r in range(1, h, 2)])
    for src, dst in enumerate(rows):
        out[dst * w:(dst + 1) * w] = indices[src * w:(src + 1) * w]
    return out


def _gif_to_rgba(indices, w, h, table, transparent):
    rgba = bytearray(w * h * 4)
    for i in range(min(len(indices), w * h)):
        idx = indices[i]
        o = idx * 3
        d = i * 4
        if o + 2 < len(table):
            rgba[d] = table[o]
            rgba[d + 1] = table[o + 1]
            rgba[d + 2] = table[o + 2]
        rgba[d + 3] = 0 if idx == transparent else 255
    return w, h, rgba


def _lzw(data, min_code, expected):
    """GIF's variable-width LZW. Codes are packed little-endian, least
    significant bit first, and the code width grows as the table fills."""
    clear = 1 << min_code
    end = clear + 1
    width = min_code + 1
    table = [bytes([i]) for i in range(clear)] + [b"", b""]
    out = bytearray()
    prev = None
    bitpos = 0
    total = len(data) * 8
    while bitpos + width <= total and len(out) < expected:
        byte = bitpos >> 3
        chunk = int.from_bytes(data[byte:byte + 3].ljust(3, b"\x00"), "little")
        code = (chunk >> (bitpos & 7)) & ((1 << width) - 1)
        bitpos += width
        if code == clear:
            table = [bytes([i]) for i in range(clear)] + [b"", b""]
            width = min_code + 1
            prev = None
            continue
        if code == end:
            break
        if code < len(table):
            entry = table[code]
        elif prev is not None:
            entry = prev + prev[:1]
        else:
            break
        out += entry
        if prev is not None and len(table) < 4096:
            table.append(prev + entry[:1])
            if len(table) == (1 << width) and width < 12:
                width += 1
        prev = entry
    if len(out) < expected:
        out.extend(b"\x00" * (expected - len(out)))
    return out


# -- Netpbm ---------------------------------------------------------------

def decode_pnm(data):
    """PBM/PGM/PPM, ASCII (P1-P3) and binary (P4-P6)."""
    magic = data[:2]
    fields = 2 if magic in (b"P1", b"P4") else 3
    values = []
    pos = 2
    while len(values) < fields and pos < len(data):
        if data[pos:pos + 1].isspace():
            pos += 1
        elif data[pos:pos + 1] == b"#":
            while pos < len(data) and data[pos:pos + 1] != b"\n":
                pos += 1
        else:
            start = pos
            while pos < len(data) and not data[pos:pos + 1].isspace():
                pos += 1
            values.append(int(data[start:pos]))
    if len(values) < fields:
        raise ImageError("truncated Netpbm header")
    width, height = values[0], values[1]
    maxval = values[2] if fields == 3 else 1
    if not width or not height:
        raise ImageError("empty Netpbm image")
    pos += 1  # single whitespace byte after the header

    n = width * height
    rgba = bytearray(n * 4)
    scale = 255.0 / maxval if maxval else 1.0

    if magic == b"P4":  # packed bitmap, 1 = black
        stride = (width + 7) // 8
        for y in range(height):
            for x in range(width):
                bit = (data[pos + y * stride + x // 8] >> (7 - x % 8)) & 1
                v = 0 if bit else 255
                d = (y * width + x) * 4
                rgba[d] = rgba[d + 1] = rgba[d + 2] = v
                rgba[d + 3] = 255
        return width, height, rgba

    if magic in (b"P5", b"P6"):
        step = 1 if magic == b"P5" else 3
        wide = maxval > 255
        size = step * (2 if wide else 1)
        for i in range(n):
            s = pos + i * size
            if s + size > len(data):
                break
            d = i * 4
            for c in range(step):
                o = s + c * (2 if wide else 1)
                v = data[o] if not wide else data[o]
                rgba[d + c] = int(v * (1.0 if wide else scale))
            if step == 1:
                rgba[d + 1] = rgba[d + 2] = rgba[d]
            rgba[d + 3] = 255
        return width, height, rgba

    # ASCII variants: the rest of the file is whitespace-separated numbers.
    nums = data[pos:].split()
    step = 1 if magic in (b"P1", b"P2") else 3
    for i in range(n):
        d = i * 4
        for c in range(step):
            j = i * step + c
            if j >= len(nums):
                break
            v = int(nums[j])
            rgba[d + c] = int((1 - v) * 255) if magic == b"P1" \
                else int(v * scale)
        if step == 1:
            rgba[d + 1] = rgba[d + 2] = rgba[d]
        rgba[d + 3] = 255
    return width, height, rgba


# -- resampling -----------------------------------------------------------

def resize(rgba, width, height, new_width, new_height):
    """Nearest-neighbour resample. CSS width/height on <img> needs it, and
    nearest keeps it a pure index computation with no per-pixel arithmetic."""
    new_width = max(1, int(new_width))
    new_height = max(1, int(new_height))
    if (new_width, new_height) == (width, height):
        return rgba
    out = bytearray(new_width * new_height * 4)
    xmap = [min(width - 1, x * width // new_width) * 4
            for x in range(new_width)]
    for y in range(new_height):
        src_row = min(height - 1, y * height // new_height) * width * 4
        d = y * new_width * 4
        for sx in xmap:
            s = src_row + sx
            out[d:d + 4] = rgba[s:s + 4]
            d += 4
    return out
