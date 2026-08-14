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