"""The H.264 decoder, against ground truth from a reference decoder.

Every `.i420.z` in `tests/fixtures/h264` is exactly what FFmpeg 7.1 decoded
the stream beside it to, zlib-compressed because a QCIF frame is 38016 bytes
of very compressible test pattern. FFmpeg is not involved in running these
tests and is not a dependency of anything: it produced the fixtures once,
offline, and what ships is the numbers it produced. The inter-coded vectors
carry every frame of their stream, not just the first, and
`tests/fixtures/h264/make_inter_vectors.sh` is the offline tool that made
them.

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

# Every intra-only stream with ground truth, and what makes it worth its
# bytes. One picture each, so `decode_i420` is handed the whole file.
VECTORS = [
    ("mb1-noloop", 16, 16, "one macroblock, deblocking off"),
    ("mb1", 16, 16, "one macroblock, deblocking on"),
    ("mb4", 32, 32, "four macroblocks: neighbour availability"),
    ("qcif-high", 176, 144, "High profile, 8x8 transform, deblocking"),
    ("qcif-main", 176, 144, "Main profile: CABAC without the 8x8 transform"),
    ("qcif-scaling", 176, 144, "picture-level scaling matrices"),
    ("qcif-slices", 176, 144, "four slices in one picture"),
    ("qcif-cavlc", 176, 144, "Baseline profile: CAVLC instead of CABAC"),
    ("crop", 100, 60, "frame_cropping: 100x60 out of 112x64"),
    ("tiny-crop", 66, 50, "cropping on both axes at once"),
]

# The inter-coded vectors: an IDR and then P frames, every frame compared.
# Made by tests/fixtures/h264/make_inter_vectors.sh, which says what each
# encoder option is for; the one-line note here is what a failure means.
#
# Checking only frame zero of these would prove nothing at all -- frame zero
# is an IDR and was already covered by the list above -- so the test walks
# every access unit and compares every one. A decoder with a wrong motion
# vector predictor is usually still right for a while: the error appears in
# one macroblock and then spreads by prediction, so the frame it first shows
# up in is the interesting number and the last frame is the one that catches
# a slow drift.
INTER_VECTORS = [
    ("p-basic", 128, 96, 8, "an IDR and seven P frames, 16x16 partitions"),
    ("p-skip", 128, 96, 10, "P_Skip over most of a still background"),
    ("p-sub8x8", 128, 96, 8, "P_8x8 down to 4x4 sub-partitions, 8x8 transform"),
    ("p-multiref", 112, 80, 10, "four reference frames, ref_idx_l0 in use"),
    ("p-edge", 112, 80, 10, "the picture pans off its own edges"),
    ("p-weightp", 112, 80, 10, "weighted prediction across a fade"),
    ("cavlc-intra", 128, 96, 3, "CAVLC with no inter syntax at all"),
    ("cavlc-p", 128, 96, 10, "CAVLC P frames: mb_skip_run, ue(v) mb_type"),
    ("cavlc-highqp", 176, 144, 8, "near-empty blocks: the nC derivation"),
    ("cavlc-lowqp", 96, 64, 4, "huge levels: suffixLength and the escapes"),
    ("cavlc-chromadc", 128, 96, 8, "the 2x2 chroma DC table"),
    ("cavlc-8x8", 128, 96, 8, "CAVLC 8x8: four 4x4 blocks, four nC"),
]


def _stream(name):
    with open(os.path.join(FIXTURES, name + ".264"), "rb") as handle:
        return handle.read()


def _truth(name):
    with open(os.path.join(FIXTURES, name + ".i420.z"), "rb") as handle:
        return zlib.decompress(handle.read())


def _access_units(data):
    """Split an Annex B stream into one chunk per picture.

    A `.264` file is a run of NAL units with no framing above them, and the
    decoder takes one access unit per call, so somebody has to cut it up. In
    the browser that somebody is the container -- an MP4 stores one sample
    per frame and never asks this question -- which is why the rule here is
    the crude one and lives in the tests: a chunk ends at the end of a
    coded-slice NAL. That is only true because these fixtures have one slice
    per picture, which `qcif-slices` deliberately is not and which is why
    that vector is in the intra list, decoded whole.
    """
    units, current, start = [], b"", data.find(b"\x00\x00\x01")
    while start >= 0:
        nxt = data.find(b"\x00\x00\x01", start + 3)
        end = len(data) if nxt < 0 else nxt
        kind = data[start + 3] & 0x1F if start + 3 < len(data) else 0
        current += data[start:end]
        if kind in (1, 5):
            units.append(current)
            current = b""
        start = nxt
    if current:
        units.append(current)
    return units


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


def test_every_inter_vector_is_pixel_exact_on_every_frame():
    if _skip():
        return
    for name, width, height, frames, what in INTER_VECTORS:
        stream = _stream(name)
        ref = _truth(name)
        size = width * height * 3 // 2
        assert len(ref) == frames * size, (
            "%s: ground truth is %d bytes, not %d frames of %d"
            % (name, len(ref), frames, size))
        units = _access_units(stream)
        assert len(units) == frames, (
            "%s: %d access units, %d frames of ground truth"
            % (name, len(units), frames))
        decoder = h264.Decoder()
        for i, unit in enumerate(units):
            got_width, got_height, got = decoder.decode_i420(unit)
            assert (got_width, got_height) == (width, height), (
                "%s frame %d: decoded %dx%d, expected %dx%d"
                % (name, i, got_width, got_height, width, height))
            want = ref[i * size:(i + 1) * size]
            assert got == want, "%s (%s) frame %d: %s" % (
                name, what, i, _first_difference(got, want, width, height))
        print("  ok  %-12s %4dx%-4d %2d frames  %s"
              % (name, width, height, frames, what))


def test_two_decoders_interleaved_do_not_corrupt_each_other():
    """The one hazard that inter prediction introduced. There is a single
    decoder in the process -- the Fortran's state is in COMMON -- and a P
    frame is a difference against pictures that decoder is still holding.
    Two `<video>` elements on one page decode alternately, so `Decoder`
    replays its own history when it finds another instance has been at the
    library. Every frame of both streams must still come out exactly right,
    which is a strictly harder claim than either stream passing alone."""
    if _skip():
        return
    first, second = INTER_VECTORS[0], INTER_VECTORS[3]
    streams = []
    for name, width, height, _frames, _what in (first, second):
        streams.append((name, width, height, width * height * 3 // 2,
                        _access_units(_stream(name)), _truth(name),
                        h264.Decoder()))
    for i in range(max(len(s[4]) for s in streams)):
        for name, width, height, size, units, ref, decoder in streams:
            if i >= len(units):
                continue
            _w, _h, got = decoder.decode_i420(units[i])
            want = ref[i * size:(i + 1) * size]
            assert got == want, "%s frame %d, interleaved: %s" % (
                name, i, _first_difference(got, want, width, height))
    print("  ok  %s and %s decoded a frame at a time in turn"
          % (first[0], second[0]))


def test_cavlc_and_cabac_are_the_same_decoder_underneath():
    """CAVLC is a second entropy layer, not a second decoder. Everything
    below clause 9.2 -- prediction, the transforms, deblocking -- is the
    code the CABAC vectors already exercise, so the thing worth asserting
    separately is that switching entropy coders does not switch anything
    else: the same picture, encoded both ways at the same quantiser,
    reconstructs to two pictures that are close, and each is exactly its
    own ground truth.

    Close and not equal, because the two streams are two encodes and x264
    makes different mode decisions when the bits cost differently. The
    bit-exactness is asserted per stream by the vector tests above; what a
    large difference here would mean is that one of the two paths is
    reconstructing from correctly decoded coefficients differently, which
    no amount of per-stream exactness against a truth file made by the
    same decoder would catch."""
    if _skip():
        return
    size = 128 * 96
    _w, _h, cabac = h264.Decoder().decode_i420(
        _access_units(_stream("p-sub8x8"))[0])
    _w, _h, cavlc = h264.Decoder().decode_i420(
        _access_units(_stream("cavlc-8x8"))[0])
    worst = max(abs(a - b) for a, b in zip(cabac[:size], cavlc[:size]))
    mean = sum(abs(a - b) for a, b in zip(cabac[:size], cavlc[:size])) / size
    assert mean < 4.0, (
        "the same frame decoded %.2f apart on average through the two "
        "entropy coders (worst sample %d)" % (mean, worst))
    print("  ok  one picture through both entropy coders: mean |diff| "
          "%.2f, worst %d" % (mean, worst))


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


def test_an_inter_coded_mp4_plays_all_the_way_through():
    """The shape every video on the web has: an IDR followed by P frames.
    This file used to be the refusal case, and the refusal was honest --
    frame zero decoded perfectly and every frame after it would have been
    the same picture held on screen. Now it plays, and the test that proves
    it has to ask for the last frame, because asking for the first one
    proves exactly what the old refusal was guarding against.

    It also asks for frame 1 after frame 3, out of order. That is the seek
    path: `VideoTrack.frame()` has to notice the jump, reset the decoder and
    replay from the keyframe, and a P frame decoded from the wrong reference
    picture is a picture, not an error."""
    with open(os.path.join(FIXTURES, "interframe.mp4"), "rb") as handle:
        data = handle.read()
    info = mediacodec.probe(data)
    assert info.codec == "avc1", info
    assert (info.width, info.height) == (64, 48), info
    if _skip():
        assert not info.supported, "no decoder, but the file was accepted"
        assert "H.264" in info.reason, info.reason
        return
    assert info.supported, "an inter-coded H.264 file was refused: %s" % (
        info.reason,)
    track = mediacodec.open_video(data)
    assert track.frame_count == 4, track.frame_count
    frames = [track.frame(i).rgba for i in range(track.frame_count)]
    for i, rgba in enumerate(frames):
        assert len(rgba) == 64 * 48 * 4, (i, len(rgba))
    # Four identical pictures would mean the P frames decoded to nothing,
    # which is what a decoder that silently ignores residual and motion
    # produces, and it is the failure this file was chosen to catch. It is
    # also exactly what the old refusal was protecting the viewer from, so
    # the assertion is on the picture and not on the absence of an error.
    assert frames[0] != frames[-1], (
        "the last frame is the first frame: the P frames decoded to nothing")
    assert len(set(frames)) >= 2, "every frame came out the same"
    assert track.frame(1).rgba == frames[1], (
        "frame 1 came out differently when seeked to than when played to")
    print("  ok  IDR + 3 P frames decode, in order and seeked")


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
