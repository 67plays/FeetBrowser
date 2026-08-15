"""Test the raw-assembly span kernels against their pure-Python references."""
import sys, os, ctypes, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import asmblend

random.seed(11)


def main():
    UB = ctypes.c_ubyte

    # translate: dst[i] = table[src[i]]
    table = bytes(random.randrange(256) for _ in range(256))
    for n in (1, 3, 64, 4096, 100003):
        src = bytes(random.randrange(256) for _ in range(n))
        dst = (UB * n)()
        asmblend.translate(dst, (UB * n).from_buffer_copy(src),
                           (UB * 256).from_buffer_copy(table), n)
        assert bytes(dst) == bytes(table[b] for b in src), f"translate n={n}"
    print("translate: OK")

    # blend_rgb: dst = (src*a + dst*(255-a)) >> 8
    for a in (0, 1, 128, 255):
        for n in (1, 7, 4096, 99999):
            src = bytes(random.randrange(256) for _ in range(n))
            dst = bytes(random.randrange(256) for _ in range(n))
            buf = (UB * n).from_buffer_copy(dst)
            asmblend.blend_rgb(buf, (UB * n).from_buffer_copy(src), n, a)
            got = bytes(buf)
            exp = bytes(((s * a + d * (255 - a)) >> 8) & 0xFF
                        for s, d in zip(src, dst))
            assert got == exp, f"blend a={a} n={n}"
    print("blend_rgb: OK")

    # blit_cov: a==0 untouched, a==255 solid (r,g,b), else (c*a + dst*(255-a)) >> 8
    for n in (1, 3, 100, 4096, 100003):
        cov = bytes(random.choice((0, 255, random.randrange(1, 255)))
                    for _ in range(n))
        r, g, b = (random.randrange(256) for _ in range(3))
        dst = bytes(random.randrange(256) for _ in range(n * 3))
        buf = (UB * (n * 3)).from_buffer_copy(dst)
        asmblend.blit_cov(buf, (UB * n).from_buffer_copy(cov), n, r, g, b)
        got = bytes(buf)
        exp = bytearray(dst)
        for i in range(n):
            a = cov[i]
            if a == 0:
                continue
            j = i * 3
            if a >= 255:
                exp[j] = r
                exp[j + 1] = g
                exp[j + 2] = b
            else:
                inv = 255 - a
                exp[j] = (r * a + exp[j] * inv) >> 8
                exp[j + 1] = (g * a + exp[j + 1] * inv) >> 8
                exp[j + 2] = (b * a + exp[j + 2] * inv) >> 8
        assert bytes(exp) == got, f"blit_cov n={n}"
    print("blit_cov: OK")

    # blend_rgba: a==0 untouched, a==255 copy, else (srcc*a + dst*(255-a)) >> 8
    for n in (1, 3, 100, 4096, 100003):
        src = bytes(random.randrange(256) for _ in range(n * 4))
        dst = bytes(random.randrange(256) for _ in range(n * 3))
        buf = (UB * (n * 3)).from_buffer_copy(dst)
        asmblend.blend_rgba(buf, (UB * (n * 4)).from_buffer_copy(src), n)
        got = bytes(buf)
        exp = bytearray(dst)
        for i in range(n):
            a = src[i * 4 + 3]
            if a == 0:
                continue
            j = i * 3
            s = i * 4
            if a >= 255:
                exp[j] = src[s]
                exp[j + 1] = src[s + 1]
                exp[j + 2] = src[s + 2]
            else:
                inv = 255 - a
                exp[j] = (src[s] * a + exp[j] * inv) >> 8
                exp[j + 1] = (src[s + 1] * a + exp[j + 1] * inv) >> 8
                exp[j + 2] = (src[s + 2] * a + exp[j + 2] * inv) >> 8
        assert bytes(exp) == got, f"blend_rgba n={n}"
    print("blend_rgba: OK")

    # fill_span
    n = 100000
    dst = (UB * n)()
    asmblend.fill_span(dst, 0x42, n)
    assert bytes(dst) == b"\x42" * n
    print("fill_span: OK")

    print(f"backend: {'raw assembly' if asmblend.using_assembly() else 'python fallback'}")
    print("asmblend: OK")


if __name__ == "__main__":
    main()