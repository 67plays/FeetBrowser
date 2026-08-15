"""The H.264 decoder, against ground truth from a reference decoder.

Every `.i420.z` in `tests/fixtures/h264` is exactly what FFmpeg 7.1 decoded
the stream beside it to, zlib-compressed because a QCIF frame is 38016 bytes
of very compressible test pattern. FFmpeg is not involved in running these
tests and is not a dependency of anything: it produced the fixtures once,
offline, and what ships is the numbers it produced.

The comparison is exact. H.264 is a bit-exact specification in the YUV
domain -- two conforming decoders produce identical samples, not similar
ones -- so a single differing luma sample is a bug, and a PSNR threshold
here would be a way of not finding it. RGB is deliberately not compared:
the standard says nothing about anybody's colour matrix, so a mismatch
there would be a disagreement about display rather than about decoding.

The whole suite skips where there is no gfortran. That is the same
arrangement as the assembly kernels in test_asmblend.py: a machine without
the toolchain still has a browser, it just has a browser that says "no
decoder" for H.264, and the tests have to prove that path works too.
"""
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import h264, mediacodec

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "h264")

# Every stream with ground truth, and what makes it worth its bytes.
VECTORS = [
    ("mb1-noloop", 16, 16, "one macroblock, deblocking off"),
    ("mb1", 16, 16, "one macroblock, deblocking on"),
    ("mb4", 32, 32, "four macroblocks: neighbour availability"),
    ("qcif-high", 176, 144, "High profile, 8x8 transform, deblocking"),
    ("qcif-main", 176, 144, "Main profile: CABAC without the 8x8 transform"),
    ("qcif-scaling", 176, 144, "picture-level scaling matrices"),
    ("qcif-slices", 176, 144, "four slices in one picture"),
    ("crop", 100, 60, "frame_cropping: 100x60 out of 112x64"),
    ("tiny-crop", 66, 50, "cropping on both axes at once"),
]


def _stream(name):
    with open(os.path.join(FIXTURES, name + ".264"), "rb") as handle:
        return handle.read()


def _truth(name):
    with open(os.path.join(FIXTURES, name + ".i420.z"), "rb") as handle:
        return zlib.decompress(handle.read())


def _skip():
    if h264.available():
        return False
    print("  skipping: %s" % h264.unavailable_reason())
    return True


def _first_difference(got, ref, width, height):
    """Where the two pictures part company, in a form worth reading.

    A count of differing bytes says a decoder is broken; the plane, the
    coordinate and the macroblock say which part of it. Every bug found
    while writing this decoder was found by looking at that triple.
    """
    luma = width * height
    for i, (a, b) in enumerate(zip(got, ref)):
        if a == b:
            continue
        if i < luma:
            x, y = i % width, i // width
            return ("luma (%d,%d) mb(%d,%d): got %d, expected %d"
                    % (x, y, x // 16, y // 16, a, b))
        j = i - luma
        cw, ch = width // 2, height // 2
        plane = "Cb" if j < cw * ch else "Cr"
        j %= max(cw * ch, 1)
        x, y = j % cw, j // cw
        return ("%s (%d,%d) mb(%d,%d): got %d, expected %d"
                % (plane, x, y, x // 8, y // 8, a, b))
    return "the pictures are the same length and differ nowhere"


def test_every_vector_is_pixel_exact():
    if _skip():
        return
    for name, width, height, what in VECTORS:
        ref = _truth(name)
        got_width, got_height, got = h264.Decoder().decode_i420(_stream(name))
        assert (got_width, got_height) == (width, height), (
            "%s: decoded %dx%d, expected %dx%d"
            % (name, got_width, got_height, width, height))
        assert len(got) == len(ref), (
            "%s: %d bytes of I420, expected %d"
            % (name, len(got), len(ref)))
        assert got == ref, "%s (%s): %s" % (
            name, what, _first_difference(got, ref, width, height))
        print("  ok  %-14s %4dx%-4d %s" % (name, width, height, what))


def test_cavlc_is_refused_and_says_so():
    """The one thing worse than not implementing CAVLC is implementing half
    of it. A Baseline stream must come back as a sentence, not as a grey
    picture and not as an exception from somewhere inside the decoder."""
    if _skip():
        return
    try:
        h264.Decoder().decode_i420(_stream("qcif-cavlc"))
    except h264.H264Error as exc:
        assert "CAVLC" in str(exc), "unhelpful refusal: %s" % exc
        return
    raise AssertionError("a CAVLC stream decoded, which it should not have")


def test_garbage_is_refused_rather_than_crashing():
    """Every byte in these streams came from a stranger, so the decoder is
    fed some that came from nowhere at all."""
    if _skip():
        return
    good = _stream("mb1")
    cases = [
        (b"", "empty"),
        (b"\x00" * 64, "all zeroes"),
        (b"\x00\x00\x01" + b"\xff" * 200, "one NAL of 0xff"),
        (good[:len(good) // 2], "truncated halfway"),
        (good[:40] + b"\x5a" * 60 + good[100:], "corrupted in the middle"),
    ]
    for data, what in cases:
        try:
            h264.Decoder().decode_i420(data)
        except h264.H264Error:
            pass
        print("  ok  survived %s" % what)


def test_annexb_from_avcc_reframes_without_changing_payload():
    sample = (b"\x00\x00\x00\x04" + b"ABCD"
              + b"\x00\x00\x00\x02" + b"EF")
    out = h264.annexb_from_avcc(sample, 4)
    assert out == b"\x00\x00\x00\x01ABCD\x00\x00\x00\x01EF", out
    # A length that runs off the end is what a file cut by a careless tool
    # looks like; keep the part that is whole.
    short = b"\x00\x00\x00\x04" + b"ABCD" + b"\x00\x00\x00\x09" + b"EF"
    assert h264.annexb_from_avcc(short, 4) == b"\x00\x00\x00\x01ABCD"
    for bad in (0, 3, 5, 8):
        try:
            h264.annexb_from_avcc(sample, bad)
        except h264.H264Error:
            continue
        raise AssertionError("accepted a %d-byte NAL length" % bad)


def test_parameter_sets_come_out_of_an_avcc_box():
    avcc = (b"\x01\x64\x00\x0a\xff"          # version, profile, level, 4-byte
            + b"\xe1" + b"\x00\x03" + b"SPS"  # one SPS
            + b"\x01" + b"\x00\x03" + b"PPS")  # one PPS
    sets, length_size = h264.parameter_sets_from_avcc(avcc)
    assert length_size == 4, length_size
    assert sets == b"\x00\x00\x00\x01SPS\x00\x00\x00\x01PPS", sets
    for bad in (b"", b"\x01\x64\x00", b"\x02" + avcc[1:], avcc[:8]):
        try:
            h264.parameter_sets_from_avcc(bad)
        except h264.H264Error:
            continue
        raise AssertionError("accepted a broken avcC: %r" % bad)


def test_mp4_plays_through_the_container_layer():
    """The whole way through: an MP4 off disk, the `avcC` out of its sample
    entry, the length-prefixed sample reframed, RGBA out the other end."""
    with open(os.path.join(FIXTURES, "qcif.mp4"), "rb") as handle:
        data = handle.read()
    info = mediacodec.probe(data)
    assert info.codec == "avc1", info
    assert (info.width, info.height) == (176, 144), info
    if _skip():
        assert not info.supported, "no decoder, but the file was accepted"
        assert info.reason, "refused an H.264 file without saying why"
        return
    assert info.supported, "H.264 MP4 refused: %s" % info.reason
    track = mediacodec.open_video(data)
    frame = track.frame(0)
    assert len(frame.rgba) == 176 * 144 * 4, len(frame.rgba)
    # Against the same picture in YUV, converted here rather than in the
    # decoder, so the two colour conversions have to agree.
    ref = _truth("qcif-high")
    luma = 176 * 144
    worst = 0
    for y in range(0, 144, 7):
        for x in range(0, 176, 5):
            yy = ref[y * 176 + x]
            cb = ref[luma + (y // 2) * 88 + x // 2] - 128
            cr = ref[luma + 88 * 72 + (y // 2) * 88 + x // 2] - 128
            base = 298 * (yy - 16)
            want = (max(0, min(255, (base + 409 * cr + 128) >> 8)),
                    max(0, min(255, (base - 100 * cb - 208 * cr + 128) >> 8)),
                    max(0, min(255, (base + 516 * cb + 128) >> 8)))
            at = (y * 176 + x) * 4
            got = (frame.rgba[at], frame.rgba[at + 1], frame.rgba[at + 2])
            assert frame.rgba[at + 3] == 255, "alpha is not opaque"
            worst = max(worst, max(abs(a - b) for a, b in zip(got, want)))
    assert worst == 0, "RGB conversion differs by up to %d" % worst
    print("  ok  MP4 -> avcC -> Annex B -> RGBA, %dx%d" % (176, 144))


def test_an_inter_coded_mp4_is_refused_before_it_can_freeze():
    """The shape every video on the web has, and the one phase one does not
    decode: an IDR followed by P frames. Its first frame decodes perfectly,
    which is exactly the trap -- accepting the file on that evidence would
    put one picture on screen and then hold it there for the rest of the
    clip, with the element reporting no error at all. The sync flags say so
    before any of it is decoded, so the file is refused and named."""
    with open(os.path.join(FIXTURES, "interframe.mp4"), "rb") as handle:
        data = handle.read()
    info = mediacodec.probe(data)
    assert info.codec == "avc1", info
    assert (info.width, info.height) == (64, 48), info
    assert not info.supported, "an inter-coded stream was accepted"
    assert "H.264" in info.reason, info.reason
    if not _skip():
        assert "inter-coded" in info.reason, info.reason
    try:
        mediacodec.open_video(data)
    except mediacodec.MediaError:
        pass
    else:
        raise AssertionError("open_video accepted an inter-coded stream")
    print("  ok  IDR + 3 P frames refused: %s" % info.reason)


def test_a_machine_without_gfortran_still_has_a_browser():
    """The degradation path, forced. Nothing here may raise: an absent
    toolchain has to look like an unsupported codec, not like a crash."""
    saved = (h264._loaded, h264._lib, h264._load_error)
    try:
        h264._loaded = True
        h264._lib = None
        h264._load_error = "no gfortran on PATH"
        assert not h264.available()
        assert h264.unavailable_reason() == "no gfortran on PATH"
        assert h264.probe() == "no gfortran on PATH"
        try:
            h264.Decoder()
        except h264.H264Error as exc:
            assert "gfortran" in str(exc), exc
        else:
            raise AssertionError("built a decoder with no library")
        with open(os.path.join(FIXTURES, "qcif.mp4"), "rb") as handle:
            data = handle.read()
        info = mediacodec.probe(data)
        assert info.codec == "avc1" and not info.supported, info
        assert (info.width, info.height) == (176, 144), info
        assert "gfortran" in info.reason, info.reason
        try:
            mediacodec.open_video(data)
        except mediacodec.MediaError:
            pass
        else:
            raise AssertionError("opened an H.264 file with no decoder")
        print("  ok  no toolchain: probed, refused, said why")
    finally:
        h264._loaded, h264._lib, h264._load_error = saved


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f" FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(tests)} H.264 TESTS PASSED")


if __name__ == "__main__":
    main()
