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
import re
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

# The B vectors. These are the first streams in this suite whose decode
# order is not their presentation order, which is the whole point of them:
# a B picture is coded after the future picture it predicts from and shown
# before it. FFmpeg wrote its raw output in presentation order, so the test
# below sorts what comes out of the decoder by picture order count before
# comparing -- exactly the job a container does with its composition
# offsets, done here with the decoder's own numbers so that a wrong POC is
# a test failure and not a silently reordered success.
B_VECTORS = [
    ("b-basic", 128, 96, 12, "IBBP: one list-1 reference, no pyramid"),
    ("b-pyramid", 128, 96, 16, "B pyramid: B pictures used as references"),
    ("b-direct-spatial", 112, 80, 14, "spatial direct, 8.4.1.2.2"),
    ("b-direct-temporal", 112, 80, 14,
     "temporal direct on identical content, 8.4.1.2.3"),
    ("b-weightb", 112, 80, 14, "implicit weighted bi-prediction, 8.4.2.3.2"),
    ("b-skip", 128, 96, 14, "long runs of B_Skip over a still background"),
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


def test_every_b_vector_is_pixel_exact_on_every_frame():
    """B pictures, compared frame for frame after reordering by POC.

    The reordering is the only difference from the P test above, and it is
    done the strict way: the picture order counts the decoder reports must
    be distinct and must sort into exactly the presentation order FFmpeg
    wrote, so a decoder that got the POCs wrong fails here even if every
    sample it produced was right. Sorting the decoded pictures by anything
    softer -- their own content, say -- would turn this test into one that
    cannot fail.
    """
    if _skip():
        return
    for name, width, height, frames, what in B_VECTORS:
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
        decoded = []
        for i, unit in enumerate(units):
            got_width, got_height, got = decoder.decode_i420(unit)
            assert (got_width, got_height) == (width, height), (
                "%s unit %d: decoded %dx%d, expected %dx%d"
                % (name, i, got_width, got_height, width, height))
            decoded.append((decoder.poc, i, got))
        pocs = [poc for poc, _i, _p in decoded]
        assert len(set(pocs)) == frames, (
            "%s: %d access units but only %d distinct picture order counts %r"
            % (name, frames, len(set(pocs)), pocs))
        assert pocs != sorted(pocs), (
            "%s: decode order and presentation order agree, so this vector "
            "has no B pictures in it and is testing nothing: %r"
            % (name, pocs))
        for shown, (_poc, unit, got) in enumerate(sorted(decoded)):
            want = ref[shown * size:(shown + 1) * size]
            assert got == want, "%s (%s) frame %d (access unit %d): %s" % (
                name, what, shown, unit,
                _first_difference(got, want, width, height))
        print("  ok  %-16s %4dx%-4d %2d frames  %s"
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


def test_an_mp4_with_b_frames_comes_out_in_presentation_order():
    """The container half of B frames, which is a separate bug from the
    decoder half and fails in a way that looks like nothing much: the file
    plays, every frame is a real frame, and the motion stutters back and
    forth because frames are shown in the order they were coded.

    So the check is against pixels and not against a picture merely being
    there. `bframes.i420.z` is FFmpeg's decode of this exact MP4, in
    presentation order, and every frame of it has to match sample for
    sample after the RGB conversion -- which means `ctts` was read, the
    samples were sorted by composition time, and the reorder buffer handed
    them out in that order rather than in decode order.

    Then the seek, twice over. Backwards past the reorder buffer has to
    reset and replay; forwards into a frame that is still buffered has to
    come out of the buffer rather than being decoded a second time. Both
    have to produce the same bytes as playing straight through, because a
    frame that depends on how you arrived at it is the whole failure mode
    this is guarding."""
    with open(os.path.join(FIXTURES, "bframes.mp4"), "rb") as handle:
        data = handle.read()
    info = mediacodec.probe(data)
    assert info.codec == "avc1", info
    assert (info.width, info.height) == (128, 96), info
    if _skip():
        assert not info.supported, "no decoder, but the file was accepted"
        assert "H.264" in info.reason, info.reason
        return
    assert info.supported, "an H.264 MP4 with B frames was refused: %s" % (
        info.reason,)
    width, height, count = 128, 96, 12
    track = mediacodec.open_video(data)
    assert track.frame_count == count, track.frame_count
    # The file is IBBP, so decode order is not presentation order and the
    # track has to say so. If this is an identity mapping the test below
    # still runs but proves nothing, which is the trap worth failing on.
    assert track._order is not None and track._order != list(range(count)), (
        "the track thinks decode order is presentation order: %r"
        % (track._order,))
    ref = _truth("bframes")
    size = width * height * 3 // 2
    luma = width * height
    cw, ch = width // 2, height // 2
    assert len(ref) == count * size, len(ref)
    played = []
    for i in range(count):
        frame = track.frame(i)
        played.append(bytes(frame.rgba))
        for y in range(0, height, 7):
            for x in range(0, width, 5):
                yy = ref[i * size + y * width + x]
                cb = ref[i * size + luma + (y // 2) * cw + x // 2] - 128
                cr = ref[i * size + luma + cw * ch
                         + (y // 2) * cw + x // 2] - 128
                base = 298 * (yy - 16)
                want = (max(0, min(255, (base + 409 * cr + 128) >> 8)),
                        max(0, min(255, (base - 100 * cb - 208 * cr + 128)
                                   >> 8)),
                        max(0, min(255, (base + 516 * cb + 128) >> 8)))
                at = (y * width + x) * 4
                got = (frame.rgba[at], frame.rgba[at + 1], frame.rgba[at + 2])
                assert got == want, (
                    "frame %d (%d,%d): got %r, expected %r -- the frames are "
                    "in the wrong order or decoded wrong" % (i, x, y, got,
                                                             want))
    # Times have to be presentation times too: a `ctts` read and then
    # ignored leaves them in decode order and they stop being sorted.
    times = [track.frame_time(i) for i in range(count)]
    assert times == sorted(times), "presentation times run backwards: %r" % (
        times,)
    for jumps in ([5, 2, 11, 0, 7, 3], [11, 10, 9, 8, 1, 0], [3, 3, 4]):
        track.reset()
        for i in jumps:
            assert bytes(track.frame(i).rgba) == played[i], (
                "frame %d differs when reached by %r" % (i, jumps))
    fresh = mediacodec.open_video(data)
    for i in [7, 1, 7, 6, 2]:
        assert bytes(fresh.frame(i).rgba) == played[i], (
            "frame %d differs when seeked to without a reset" % i)
    print("  ok  IBBP MP4: 12 frames in presentation order, and seeked")


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


# Streams committed for what the decoder must *refuse*, so they have no
# `.i420.z` beside them: there is no right picture to compare against, only
# a right error. Both are combinations that decode to a plausible-looking
# wrong picture if the refusal is missing, which is the failure mode worth
# a fixture.
REFUSALS = [
    ("lossless", "lossless coding",
     "x264 at --qp 0 sets qpprime_y_zero_transform_bypass_flag, and a "
     "decoder that reads the flag and ignores it runs an inverse transform "
     "over residuals that never had a forward one"),
    ("b-cavlc", "CAVLC",
     "B slices were built for CABAC and CAVLC for I and P, and their "
     "overlap reads the wrong number of bits rather than failing: CAVLC's "
     "mb_type table stops at the four P shapes and its sub_mb_type at four "
     "rather than B's thirteen"),
]


def test_the_two_refusal_cases_are_refused_by_name():
    """A refusal is a feature and gets a test like any other. Each of these
    streams is well formed and decodable by FFmpeg; what is asserted is that
    this decoder says so rather than producing a picture."""
    if _skip():
        return
    for name, wanted, why in REFUSALS:
        data = _stream(name)
        decoder = h264.Decoder()
        try:
            for unit in _access_units(data):
                decoder.decode_i420(unit)
        except h264.H264Error as exc:
            assert wanted in str(exc), (
                "%s was refused, but for the wrong reason: %s" % (name, exc))
            print("  ok  %-10s refused: %s" % (name, exc))
            continue
        raise AssertionError("%s decoded to a picture. %s" % (name, why))


def test_no_fortran_routine_is_called_with_the_wrong_number_of_arguments():
    """FORTRAN 77 has no prototypes, so a routine that grew an argument and
    a caller that did not are a link that succeeds and a decoder that writes
    through whatever was next on the stack. That is not a hypothetical: it
    is how the CAVLC and B-slice branches met, and it segfaulted on the
    second frame of `cavlc-p` rather than at the call.

    This reads the sources rather than running anything, so it is the one
    test here that is worth something on a machine with no gfortran."""
    fortran = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fortran")
    declared, bad = {}, []
    for name in sorted(os.listdir(fortran)):
        if not name.endswith(".f"):
            continue
        for line in _folded(os.path.join(fortran, name)):
            head = re.match(r"\s*(?:SUBROUTINE|(?:INTEGER|REAL|LOGICAL)\s+"
                            r"FUNCTION)\s+(\w+)\s*(?:\(([^)]*)\))?", line)
            if head:
                args = (head.group(2) or "").strip()
                declared[head.group(1).upper()] = (
                    len(args.split(",")) if args else 0, name)
    for name in sorted(os.listdir(fortran)):
        if not name.endswith(".f"):
            continue
        for line in _folded(os.path.join(fortran, name)):
            if re.match(r"\s*(?:SUBROUTINE|(?:INTEGER|REAL|LOGICAL)\s+"
                        r"FUNCTION)\s", line):
                continue
            for call in re.finditer(r"\b(H2\w+)\s*\(", line):
                routine = call.group(1).upper()
                if routine not in declared:
                    continue
                count = _arity_at(line, call.end() - 1)
                if count is None:
                    continue
                want, where = declared[routine]
                if count != want:
                    bad.append("%s calls %s with %d, declared with %d in %s"
                               % (name, routine, count, want, where))
    assert not bad, "argument count mismatches:\n  " + "\n  ".join(bad)
    print("  ok  %d Fortran routines, every call site the declared arity"
          % len(declared))


def _folded(path):
    """Fixed-form source with comments dropped, column 73 onwards cut off,
    and continuation lines folded onto the statement they continue."""
    out = []
    with open(path) as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line or line[0] in "Cc*!":
                continue
            line = line[:72]
            if len(line) > 5 and line[5] not in (" ", "0"):
                if out:
                    out[-1] += line[6:]
                continue
            out.append(line)
    return out


def _arity_at(line, open_paren):
    """Arguments in the call whose "(" is at `open_paren`, or None if the
    parenthesis does not close on this statement."""
    depth, count, anything = 0, 1, False
    for char in line[open_paren:]:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return count if anything else 0
        elif char == "," and depth == 1:
            count += 1
        if depth == 1 and char not in "( \t":
            anything = True
    return None


# -- the shipping checks -----------------------------------------------------
#
# None of these can run on the platform that breaks: the Windows bundle is
# built by CI and the failure they exist to stop -- a decoder that loads on the
# build machine and not on the user's -- shows up two jobs later as "could not
# find module (or one of its dependencies)". So the PE reader is fed files
# made here, byte by byte, with the answer known in advance, and the flag
# selection is driven with a compiler that is a shell script.


def _fake_pe(names, magic=0x20B):
    """A PE file with `names` in its import table and nothing else in it."""
    import struct

    # The descriptor array, a null descriptor to end it, then the name
    # strings, all inside one section mapped at RVA 0x1000.
    table = 20 * (len(names) + 1)
    descriptors, strings = b"", b""
    for name in names:
        descriptors += struct.pack("<IIIII", 0, 0, 0,
                                   0x1000 + table + len(strings), 0)
        strings += name.encode("ascii") + b"\0"
    blob = descriptors + b"\0" * 20 + strings

    # PE32 puts the data directories sixteen bytes earlier than PE32+ does,
    # NumberOfRvaAndSizes being the field immediately before them.
    dirs_at = 96 if magic == 0x10B else 112
    optional = struct.pack("<H", magic) + b"\0" * (dirs_at - 6)
    optional += struct.pack("<I", 16)                    # NumberOfRvaAndSizes
    dirs = [(0, 0)] * 16
    dirs[1] = (0x1000, len(blob))
    optional += b"".join(struct.pack("<II", rva, size) for rva, size in dirs)

    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, len(optional), 0x2022)
    section = struct.pack("<8sIIIIIIHHI", b".rdata", len(blob), 0x1000,
                          (len(blob) + 511) // 512 * 512, 0x400,
                          0, 0, 0, 0, 0x40000040)
    head = b"PE\0\0" + coff + optional + section

    out = bytearray(0x400 + (len(blob) + 511) // 512 * 512)
    out[0:2] = b"MZ"
    out[0x3C:0x40] = struct.pack("<I", 0x80)
    out[0x80:0x80 + len(head)] = head
    out[0x400:0x400 + len(blob)] = blob
    return bytes(out)


def _write(tmp, data):
    path = os.path.join(tmp, "fake.dll")
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def test_the_pe_reader_names_every_dll_a_library_depends_on():
    """The reader, against files whose import table this test wrote."""
    import tempfile

    wanted = ["KERNEL32.dll", "libgfortran-5.dll", "libwinpthread-1.dll",
              "api-ms-win-crt-runtime-l1-1-0.dll"]
    with tempfile.TemporaryDirectory() as tmp:
        got = h264._pe_imports(_write(tmp, _fake_pe(wanted)))
        assert got == wanted, got
        # PE32 as well as PE32+: the data directories sit sixteen bytes
        # earlier and everything found above would be found in the wrong
        # place if that offset were wrong.
        got = h264._pe_imports(_write(tmp, _fake_pe(wanted, magic=0x10B)))
        assert got == wanted, got
        # A library that imports nothing is not an error.
        assert h264._pe_imports(_write(tmp, _fake_pe([]))) == []
    print("  ok  PE import table read, PE32 and PE32+")


def test_the_pe_reader_refuses_what_it_cannot_read():
    """Negative controls. A reader that answers "no dependencies" for a file
    it did not understand is worse than one that raises: the whole point of
    the check is that an empty answer means the library is self-contained."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        good = _fake_pe(["KERNEL32.dll"])
        cases = [
            ("not a PE file at all", b"#!/bin/sh\n" + good[10:]),
            ("no PE signature", good[:0x80] + b"XX\0\0" + good[0x84:]),
            ("an unknown optional header magic",
             good[:0x98] + b"\x0c\x02" + good[0x9A:]),
            # Truncated two ways: mid-descriptor, and after a descriptor whose
            # name string is no longer in the file. Neither may come back as
            # "this library depends on nothing", which is what the caller
            # reads as "fit to ship".
            ("a file cut off inside the import table",
             _fake_pe(["a.dll", "b.dll"])[:0x400 + 10]),
            ("a file cut off before its import names",
             _fake_pe(["a.dll", "b.dll"])[:0x400 + 20]),
        ]
        for what, data in cases:
            try:
                h264._pe_imports(_write(tmp, data))
            except h264.H264Error:
                pass
            else:
                raise AssertionError("read imports out of %s" % what)
    print("  ok  a file it cannot read raises rather than answering 'none'")


def test_only_dependencies_the_system_lacks_are_reported():
    """_dangling's rule, which is the whole judgement: what the system
    directory has is the system's, and the rest is ours to ship."""
    import tempfile

    saved = h264.platform.system
    with tempfile.TemporaryDirectory() as tmp:
        system32 = os.path.join(tmp, "System32")
        os.makedirs(system32)
        for present in ("KERNEL32.dll", "msvcrt.dll"):
            open(os.path.join(system32, present), "wb").close()
        data = _fake_pe(["KERNEL32.dll", "msvcrt.dll",
                         "api-ms-win-crt-runtime-l1-1-0.dll",
                         "libgfortran-5.dll", "libwinpthread-1.dll"])
        path = _write(tmp, data)
        try:
            h264.platform.system = lambda: "Windows"
            os.environ["SystemRoot"] = tmp
            assert h264._dangling(path) == ["libgfortran-5.dll",
                                            "libwinpthread-1.dll"], \
                h264._dangling(path)
            h264.platform.system = lambda: "Darwin"
            assert h264._dangling(path) == []
        finally:
            h264.platform.system = saved
            os.environ.pop("SystemRoot", None)
    print("  ok  system DLLs and API sets ignored, the compiler's reported")


def test_the_runtime_shipped_beside_the_decoder_is_the_whole_chain():
    """The last resort, when no flag set produced a self-contained library.

    It has to be transitive. libgfortran needs libquadmath, which needs
    libwinpthread, and a bundle that copies the first and stops fails in
    exactly the way copying it was meant to prevent -- and fails identically,
    with the loader naming the decoder and not the DLL it could not find."""
    import tempfile

    saved_system, saved_path = h264.platform.system, os.environ.get("PATH", "")
    with tempfile.TemporaryDirectory() as tmp:
        binaries = os.path.join(tmp, "bin")
        system32 = os.path.join(tmp, "System32")
        package = os.path.join(tmp, "feetbrowser")
        for directory in (binaries, system32, package):
            os.makedirs(directory)
        open(os.path.join(system32, "KERNEL32.dll"), "wb").close()

        chain = {
            "libgfortran-5.dll": ["libquadmath-0.dll", "KERNEL32.dll"],
            "libquadmath-0.dll": ["libwinpthread-1.dll"],
            "libwinpthread-1.dll": ["KERNEL32.dll"],
        }
        for name, needs in chain.items():
            with open(os.path.join(binaries, name), "wb") as handle:
                handle.write(_fake_pe(needs))
        out = os.path.join(package, "_h264_deadbeef.dll")
        with open(out, "wb") as handle:
            handle.write(_fake_pe(["libgfortran-5.dll", "KERNEL32.dll"]))
        try:
            h264.platform.system = lambda: "Windows"
            os.environ["SystemRoot"] = tmp
            os.environ["PATH"] = binaries
            assert h264._dangling(out) == ["libgfortran-5.dll"]
            copied, missing = h264._ship_runtime_beside(out, None,
                                                        h264._dangling(out))
            assert copied == sorted(chain), copied
            assert missing == [], missing
            assert h264._dangling(out) == [], "still dangling after the copy"

            # And what cannot be found anywhere is reported by name rather
            # than shipped as a hole in the bundle. A second package, because
            # the first now has the chain in it and nothing would be looked
            # for at all.
            second = os.path.join(tmp, "feetbrowser2")
            os.makedirs(second)
            other = os.path.join(second, "_h264_deadbeef.dll")
            with open(other, "wb") as handle:
                handle.write(_fake_pe(["libgfortran-5.dll", "KERNEL32.dll"]))
            os.remove(os.path.join(binaries, "libwinpthread-1.dll"))
            copied, missing = h264._ship_runtime_beside(other, None,
                                                        ["libgfortran-5.dll"])
            assert missing == ["libwinpthread-1.dll"], missing
            assert copied == ["libgfortran-5.dll", "libquadmath-0.dll"], copied
        finally:
            h264.platform.system = saved_system
            os.environ["PATH"] = saved_path
            os.environ.pop("SystemRoot", None)
    print("  ok  the compiler's runtime ships beside the decoder, transitively")


def test_a_flag_set_that_links_but_does_not_ship_is_not_used():
    """The reason _compile takes a check at all.

    Every Windows flag set links. Only some of them produce a library that
    will load on a machine without the compiler, and the link succeeding says
    nothing about which. This drives _compile with a compiler that always
    succeeds and a check that only accepts the third flag set, and asserts it
    got there rather than stopping at the first."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        fc = os.path.join(tmp, "fakegfortran")
        with open(fc, "w") as handle:
            handle.write('#!/bin/sh\n'
                         'while [ "$1" != "-o" ]; do shift; done\n'
                         'echo "$*" > "$2"\n')
        os.chmod(fc, 0o755)
        out = os.path.join(tmp, "lib.so")
        attempts = (["-first"], ["-second"], ["-third"])

        def only_the_third(path):
            with open(path) as handle:
                if "-third" in handle.read():
                    return []
            return ["still needs libgfortran-5.dll"]

        used = h264._compile(fc, out, attempts, only_the_third)
        assert used == ["-third"], used
        assert os.path.exists(out)
        # And a tried-and-rejected attempt leaves nothing behind: the .tmp
        # files would otherwise pile up inside the package being built.
        leftovers = [n for n in os.listdir(tmp) if ".tmp" in n]
        assert not leftovers, leftovers

        # When nothing satisfies the check, it fails -- naming every flag set
        # and what was still wrong with it, because a packaging log that says
        # only "could not build" is a log nobody can act on.
        try:
            h264._compile(fc, out, attempts, lambda p: ["needs libquadmath"])
        except h264.H264Error as exc:
            assert "-first" in str(exc) and "-third" in str(exc), exc
            assert "libquadmath" in str(exc), exc
        else:
            raise AssertionError("shipped a library nothing accepted")
    print("  ok  _compile walks past a flag set whose output would not load")


def test_the_flag_sets_answer_each_platforms_own_problem():
    """Windows leaves libwinpthread behind unless everything is static;
    manylinux cannot link libgfortran.a into a shared object at all."""
    windows = h264._ship_attempts("Windows")
    assert windows[0] == ["-static"], windows
    linux = h264._ship_attempts("Linux")
    assert "-static-libgfortran" not in linux[-1], linux
    assert "-static-libgfortran" in linux[0], linux
    for system in ("Windows", "Linux", "Darwin"):
        sets = h264._ship_attempts(system)
        assert "-march=native" not in sum(sets, []), system
        # A fallback that is the same as what it falls back from is not a
        # fallback, it is the same link done twice.
        assert len(set(map(tuple, sets))) == len(sets), (system, sets)
    print("  ok  Windows tries -static first, Linux falls back to dynamic")


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
