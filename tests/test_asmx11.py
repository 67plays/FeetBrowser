"""Test the raw-assembly X11 pixel-packing kernel against its pure-Python
reference: packed RGB rows through three lookup tables into a Z-format
buffer, in either byte order, with per-row padding."""
import sys, os, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import asmx11

random.seed(11)


def channel_table(mask):
    """The same scaling x11.py's `channel_table` applies: an 8-bit channel
    value -> its bits in place, for all 256 values. A list, because a
    channel wider than eight bits holds values a byte cannot."""
    if not mask:
        return [0] * 256
    shift = (mask & -mask).bit_length() - 1
    top = mask >> shift
    return [((value * top) // 255) << shift for value in range(256)]


def ref_pack(src, width, height, src_stride, dst_stride, red, green, blue,
             pixel_bytes, big_endian):
    out = bytearray(dst_stride * height)
    order = "little" if not big_endian else "big"
    for row in range(height):
        s = row * src_stride
        d = row * dst_stride
        for _column in range(width):
            value = red[src[s]] | green[src[s + 1]] | blue[src[s + 2]]
            out[d:d + pixel_bytes] = value.to_bytes(pixel_bytes, order)
            s += 3
            d += pixel_bytes
    return bytes(out)


# TrueColor mask triples that byte_layout would *not* have caught: narrow
# channels that do not line up with whole bytes, and the 24-bit oddity.
MASKS = {
    2: (0x7C00, 0x03E0, 0x001F),          # 5-5-5 (depth 15)
    3: (0xFFE000, 0x001FFC, 0x000003),    # 24-bit, channels off byte lanes
    4: (0x0FF00000, 0x000FFC00, 0x000003FF),  # 32-bit, nothing aligned
}


def main():
    for pixel_bytes, (rm, gm, bm) in MASKS.items():
        red = channel_table(rm)
        green = channel_table(gm)
        blue = channel_table(bm)
        for big_endian in (0, 1):
            for width, height in ((1, 1), (7, 3), (64, 5), (255, 2), (300, 1)):
                for pad in (0, 3):
                    src_stride = width * 3 + pad
                    dst_stride = width * pixel_bytes + pad
                    src = bytes(random.randrange(256)
                                for _ in range(src_stride * height))
                    dst = bytearray(dst_stride * height)
                    asmx11.pack_rows(src, dst, width, height, src_stride,
                                     dst_stride, red, green, blue,
                                     pixel_bytes, big_endian)
                    exp = ref_pack(src, width, height, src_stride, dst_stride,
                                   red, green, blue, pixel_bytes, big_endian)
                    assert bytes(dst) == exp, (
                        f"pack_rows pb={pixel_bytes} be={big_endian} "
                        f"{width}x{height} pad={pad}")
    print("pack_rows: OK")

    # A key in every table: the two extremes must not collide with anything.
    red, green, blue = (channel_table(m) for m in MASKS[2])
    src = bytes([255, 0, 255]) + bytes(6)
    dst = bytearray(4)
    asmx11.pack_rows(src, dst, 1, 1, 9, 4, red, green, blue, 2, 0)
    exp = ref_pack(src, 1, 1, 9, 4, red, green, blue, 2, 0)
    assert bytes(dst) == exp and dst[0] | dst[1], "white pixel lost"
    print("key values: OK")

    print(f"backend: {'raw assembly' if asmx11.using_assembly() else 'python fallback'}")
    print("asmx11: OK")


if __name__ == "__main__":
    main()