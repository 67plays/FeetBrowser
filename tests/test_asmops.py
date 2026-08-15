"""Test the raw-assembly image-codec kernels against their pure-Python
references: PNG scanline filters, the RGBA expansions and resize rows."""
import sys, os, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import asmops

random.seed(11)


def ref_unfilter(line, prev, bpp, ftype):
    out = bytearray(line)
    n = len(line)
    if ftype == 1:      # Sub
        for i in range(bpp, n):
            out[i] = (out[i] + out[i - bpp]) & 0xFF
    elif ftype == 2:    # Up
        for i in range(n):
            out[i] = (out[i] + prev[i]) & 0xFF
    elif ftype == 3:    # Average
        for i in range(n):
            left = out[i - bpp] if i >= bpp else 0
            out[i] = (out[i] + ((left + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:    # Paeth
        for i in range(n):
            a = out[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            out[i] = (out[i] + pred) & 0xFF
    return bytes(out)


def ref_palette(src, palette, trns):
    out = bytearray(len(src) * 4)
    for i in range(len(src)):
        idx = src[i]
        o = idx * 3
        d = i * 4
        if o + 2 < len(palette):
            out[d] = palette[o]
            out[d + 1] = palette[o + 1]
            out[d + 2] = palette[o + 2]
        out[d + 3] = trns[idx] if idx < len(trns) else 255
    return bytes(out)


def main():
    # unfilter_line: all five filter types, bpp 1..4, several lengths.
    for ftype in range(5):
        for bpp in range(1, 5):
            for n in (1, 7, 4096, 99999):
                line = bytearray(random.randrange(256) for _ in range(n))
                prev = bytearray(random.randrange(256) for _ in range(n))
                got = bytearray(line)
                asmops.unfilter_line(got, prev, n, bpp, ftype)
                exp = ref_unfilter(line, prev, bpp, ftype)
                assert bytes(got) == exp, \
                    f"unfilter_line ftype={ftype} bpp={bpp} n={n}"
    print("unfilter_line: OK")

    # palette_expand: palette indices in and out of range, trns of every
    # relative length, including empty and longer than the samples.
    for pal_len in (0, 2, 3, 6, 12, 768):
        palette = bytes(random.randrange(256) for _ in range(pal_len))
        for trns_len in (0, 1, 12, 300):
            trns = bytes(random.randrange(256) for _ in range(trns_len))
            for n in (1, 7, 4096):
                src = bytearray(random.randrange(256) for _ in range(n))
                dst = bytearray(n * 4)
                asmops.palette_expand(dst, src, n, palette, pal_len,
                                      trns, trns_len)
                assert bytes(dst) == ref_palette(src, palette, trns), \
                    f"palette_expand pal_len={pal_len} trns_len={trns_len} n={n}"
    print("palette_expand: OK")

    # grey_expand: every stretch factor, key present and absent.
    for scale in (1, 17, 85, 255):
        for has_key in (0, 1):
            key = random.randrange(256)
            for n in (1, 7, 4096, 99999):
                src = bytearray(random.randrange(256) for _ in range(n))
                dst = bytearray(n * 4)
                asmops.grey_expand(dst, src, n, scale, key, has_key)
                exp = bytearray(n * 4)
                for i in range(n):
                    v = (src[i] * scale) & 0xFF
                    d = i * 4
                    exp[d] = exp[d + 1] = exp[d + 2] = v
                    exp[d + 3] = 0 if (has_key and src[i] == key) else 255
                assert bytes(dst) == bytes(exp), \
                    f"grey_expand scale={scale} has_key={has_key} n={n}"
    print("grey_expand: OK")

    # grey_alpha_expand
    for n in (1, 7, 4096, 99999):
        src = bytearray(random.randrange(256) for _ in range(n * 2))
        dst = bytearray(n * 4)
        asmops.grey_alpha_expand(dst, src, n)
        exp = bytearray(n * 4)
        for i in range(n):
            s, d = i * 2, i * 4
            v = src[s]
            exp[d] = exp[d + 1] = exp[d + 2] = v
            exp[d + 3] = src[s + 1]
        assert bytes(dst) == bytes(exp), f"grey_alpha_expand n={n}"
    print("grey_alpha_expand: OK")

    # rgb_expand: with and without a matching key.
    for has_key in (0, 1):
        kr, kg, kb = (random.randrange(256) for _ in range(3))
        for n in (1, 7, 4096, 99999):
            src = bytearray(random.randrange(256) for _ in range(n * 3))
            dst = bytearray(n * 4)
            asmops.rgb_expand(dst, src, n, kr, kg, kb, has_key)
            exp = bytearray(n * 4)
            for i in range(n):
                s, d = i * 3, i * 4
                r, g, b = src[s], src[s + 1], src[s + 2]
                exp[d], exp[d + 1], exp[d + 2] = r, g, b
                exp[d + 3] = 0 if (has_key and r == kr and g == kg
                                   and b == kb) else 255
            assert bytes(dst) == bytes(exp), \
                f"rgb_expand has_key={has_key} n={n}"

    # rgb_expand with a key that actually matches some pixels.
    n = 17
    src = bytearray(random.randrange(256) for _ in range(n * 3))
    src[0:3] = bytes([10, 20, 30])
    src[9:12] = bytes([10, 20, 30])
    dst = bytearray(n * 4)
    asmops.rgb_expand(dst, src, n, 10, 20, 30, 1)
    assert dst[3] == 0 and dst[4 * 3 + 3] == 0 and dst[4 * 1 + 3] == 255
    print("rgb_expand: OK")

    # resize_row: a small xmap with duplicate offsets, several lengths.
    src = bytearray(random.randrange(256) for _ in range(64))
    xmap = [random.randrange(60) for _ in range(21)]
    for n in (1, 7, 21, 100):
        x = [random.randrange(60) for _ in range(n)]
        dst = bytearray(n * 4)
        asmops.resize_row(dst, src, x, n)
        exp = bytearray(n * 4)
        for i in range(n):
            s = x[i]
            d = i * 4
            exp[d:d + 4] = src[s:s + 4]
        assert bytes(dst) == bytes(exp), f"resize_row n={n}"
    assert xmap and len(set(xmap)) < len(xmap), "xmap has duplicates"
    print("resize_row: OK")

    print(f"backend: {'raw assembly' if asmops.using_assembly() else 'python fallback'}")
    print("asmops: OK")


if __name__ == "__main__":
    main()