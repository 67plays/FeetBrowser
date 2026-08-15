"""Test the raw-assembly selection nearest-boundary kernel against its
pure-Python reference: per-character advances and a pen, the boundary nearest
to a pixel, strict-< tie-breaking, and the empty run."""
import sys, os, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser.asmselect import offset_scan, _offset_scan_py, using_assembly
from feetbrowser.layout import get_font, DrawText
from feetbrowser.selection import Run

random.seed(13)


def ref_offset_at(x, left, adv):
    """The exact loop `Run.offset_at` ran before the kernel: best boundary,
    strict <, so a tie keeps the earlier one."""
    pen = left
    best, best_d = 0, abs(x - pen)
    for i, w in enumerate(adv):
        pen += w
        distance = abs(x - pen)
        if distance < best_d:
            best, best_d = i + 1, distance
    return best


def main():
    # Kernel vs the pure-Python walk, over proportional-style widths.
    for _trial in range(20000):
        n = random.randrange(0, 41)
        adv = [random.uniform(0.0, 30.0) for _ in range(n)]
        x = random.uniform(-10.0, n * 30.0 + 10.0)
        pen = random.uniform(0.0, 40.0)
        got = offset_scan(adv, x, pen)
        exp = _offset_scan_py(adv, x, pen)
        assert got == exp, f"kernel n={n} x={x} pen={pen} adv={adv}: {got} != {exp}"
    print("kernel: OK")

    # Ties keep the earlier boundary; the far end and the exact point are
    # each reachable; the empty run is 0.
    assert offset_scan([10.0, 10.0], 10, 0) == 1, "tie must keep earlier"
    assert offset_scan([10.0, 10.0], 20, 0) == 2, "exact right edge"
    assert offset_scan([], 5, 0) == 0, "empty run"
    print("tie/empty: OK")

    # Run.offset_at end to end, against the reference loop, for a
    # proportional face whose per-character widths are not all equal.
    font = get_font(16, "normal", "roman", "Helvetica")
    text = "iiiiWWWWiii WWWii iWWWWii"
    cmd = DrawText(40, 0, text, font, 0)
    run = Run(cmd, None, 0, 0, 0)
    left = run.left
    adv = [font.measure(ch) for ch in text]
    for x in range(int(left) - 20, int(left + sum(adv)) + 20):
        assert run.offset_at(x) == ref_offset_at(x, left, adv), f"x={x}"
    assert Run(DrawText(0, 0, "", font, 0), None, 0, 0, 0).offset_at(50) == 0
    print("offset_at: OK")

    print(f"backend: {'raw assembly' if using_assembly() else 'python fallback'}")
    print("asmselect: OK")


if __name__ == "__main__":
    main()