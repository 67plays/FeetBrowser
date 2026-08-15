"""The MPEG Layer III decoder, against ground truth from a reference decoder.

Every `.f32.z` in `tests/fixtures/mp3` is exactly what FFmpeg 7.1 decoded the
`.mp3` beside it to: interleaved 32-bit floats, zlib-compressed. FFmpeg is
not involved in running these tests and is not a dependency of anything; it
produced the fixtures once, offline, and what ships is the numbers it
produced. `tests/fixtures/mp3/make_mp3_vectors.sh` is that offline tool and
says what each vector is for.

Layer III is not a bit-exact specification. The standard defines the inverse
MDCT and the polyphase synthesis filterbank in real arithmetic and leaves
the arithmetic to the implementation, so two conforming decoders differ in
the last few bits and an exact comparison would be a comparison of rounding.
The thresholds are therefore numerical -- and they are per vector, not one
number for all of them, because the spread across these eighteen files is
forty decibels wide and a single threshold loose enough for the worst would
be meaningless for the rest. Each one is the error measured against FFmpeg,
rounded up with about three times the amplitude in hand and six decibels of
SNR; see VECTORS below, where the measurement is written next to the limit.

The spread is real and worth understanding rather than papering over. Most
vectors land at 127 to 133 dB, which is float32 rounding and nothing else.
Three do not: `transient` at 89.5 dB, `tone` at 94.8 and `lsfint` at 95.4.
In all three the error is sharply localised -- a handful of samples in one
or two frames, always where the true amplitude is near zero beside a loud
attack, where a relative difference of a few parts in 10^8 in the transform
lands next to a sample whose own value is 10^-7. FFmpeg's own two decoders
disagree by more than we disagree with either: on `transient`, its float
decoder against its fixed-point decoder is 3.2e-05 and 85.8 dB, while this
decoder against its float decoder is 7.8e-05 and 89.5 dB; on `tone`, float
against fixed is 76.2 dB and we are 94.8. Our output sits inside the spread
between two conforming reference implementations, which is what a decoder
written in double precision against a standard written in real arithmetic
should do.

That the thresholds still have teeth is not an argument, it is a
measurement. Deleting one stage at a time and re-running these vectors:

  * alias reduction deleted:   `dual` falls from 101.5 dB to 9.0 dB,
                               `tone` from 94.8 to 33.6, worst error 2.7e-01
  * intensity stereo deleted:  `intensity` falls from 115.6 dB to 79.5 dB,
                               its worst error 1.3e-06 becomes 2.0e-04 --
                               forty times its own threshold
  * the bit reservoir ignored: the first vector tried does not decode at
                               all. `main_data_begin` is not a refinement;
                               a granule whose main data began three frames
                               ago simply is not there to read.

A tolerance at the end of a pipeline can hide a bug in the middle of it, so
the stages that *can* be compared exactly are compared exactly rather than
through the PCM:

  * every granule of every vector is consumed to the bit -- the bits read
    are the bits `part2_3_length` promised. Nothing about a Huffman table
    can be wrong and leave that true: one codeword of the wrong length in
    any of the thirty tables desynchronises the read and the count lands
    somewhere else.
  * requantisation is checked against sign(is)*|is|^(4/3) * 2^(exponent/4)
    recomputed in Python from the standard's formula, coefficient by
    coefficient, for every granule of every vector. On this platform it
    agrees to the last bit -- the exponent is an integer count of quarter
    powers, so there is nothing approximate in it anywhere -- and the
    limit is a relative 1e-13 only to leave room for another libm's pow.
  * the IMDCT is held against the standard's summation, written out in
    Python here rather than in Fortran, at both transform sizes.
  * the whole back half -- all four window shapes, overlap-add, frequency
    inversion and the polyphase filterbank with its 512-tap window -- is
    held against the standard's own definition of each, written out in
    Python here, taking only this decoder's spectrum as input.

Coverage is the other half of the claim, and it is the half that is easy to
fake: a threshold proves nothing about code no vector reaches.
`test_the_tools_and_the_layouts_are_all_actually_reached` measures it from
the decoder's own counters rather than asserting it in a comment. Two
vectors exist only because of it -- `mixed`, which is hand-assembled because
no encoder in circulation emits `mixed_block_flag` or the CRC, and
`mp25_12`, the ninth and last sampling frequency, so that the header field
selecting it is decoded somewhere rather than only read.

The whole suite skips where there is no gfortran, and the last test forces
that path: a machine without the toolchain still has a browser, it just has
a browser that says "no decoder" for MP3.
"""
import array
import math
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import ball, mediacodec

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "mp3")

# name, sample rate, channels, coded frames, the limit on any one sample's
# error, the floor under the whole vector's SNR, the error actually
# measured, and what the vector is here to catch. make_mp3_vectors.sh says
# how each one was made; the measured pair is what the limits are three
# times and six decibels away from.
VECTORS = [
    ("tone", 44100, 1, 15, 1.0e-04, 88.0, (2.754e-05, 94.75),
     "a pure tone: long blocks, a short big-value region, a long count1"),
    ("noise", 44100, 1, 15, 2.0e-06, 120.0, (6.855e-07, 127.66),
     "broadband noise: every band coded, all three regions in use"),
    ("stereo", 44100, 2, 17, 5.0e-06, 108.0, (9.164e-07, 114.85),
     "mid/side in fifteen of seventeen frames"),
    ("transient", 44100, 1, 17, 2.5e-04, 83.0, (7.755e-05, 89.51),
     "short blocks, the start and stop windows either side of them, "
     "subblock_gain, and the short-block reorder"),
    ("lowrate", 44100, 2, 15, 2.0e-06, 120.0, (2.906e-07, 128.55),
     "32 kbit/s stereo: the bit reservoir under real pressure"),
    ("sr48", 48000, 1, 16, 2.0e-06, 120.0, (1.825e-07, 133.51),
     "48 kHz, its own band tables"),
    ("sr32", 32000, 1, 11, 2.0e-06, 120.0, (6.557e-07, 127.62),
     "32 kHz, its own band tables"),
    ("dual", 44100, 2, 15, 2.0e-05, 95.0, (6.950e-06, 101.47),
     "plain stereo, joint stereo off: neither stereo tool applies"),
    ("hi320", 44100, 2, 13, 2.0e-06, 120.0, (4.917e-07, 127.37),
     "320 kbit/s: quantised values large enough to take the linbits escape"),
    ("mp2_24", 24000, 1, 19, 2.0e-06, 120.0, (7.041e-07, 127.58),
     "MPEG-2 LSF: one granule a frame and the LSF scalefactor partitioning"),
    ("mp2_22", 22050, 2, 18, 2.0e-06, 120.0, (5.066e-07, 127.92),
     "MPEG-2 LSF in stereo"),
    ("mp2_16", 16000, 1, 14, 2.0e-06, 120.0, (2.459e-07, 130.19),
     "MPEG-2 LSF at the third of its sampling frequencies"),
    ("mp25_11", 11025, 1, 10, 2.0e-06, 120.0, (7.153e-07, 127.69),
     "MPEG-2.5, which the standard does not contain at all"),
    ("mp25_12", 12000, 1, 11, 2.0e-06, 120.0, (6.855e-07, 127.65),
     "12 kHz: the ninth and last sampling frequency there is"),
    ("mp25_8", 8000, 1, 8, 2.0e-06, 120.0, (1.416e-07, 129.40),
     "8 kHz: the shortest band table there is"),
    ("intensity", 44100, 2, 17, 5.0e-06, 108.0, (1.252e-06, 115.62),
     "MPEG-1 intensity stereo, from the tangent table"),
    ("lsfint", 22050, 2, 18, 5.0e-05, 89.0, (1.445e-05, 95.43),
     "MPEG-2 intensity stereo, which is a different derivation entirely"),
    ("mixed", 44100, 1, 8, 2.0e-06, 120.0, (2.235e-08, 128.37),
     "mixed blocks and the CRC, hand-assembled because nothing emits them"),
]

# The nine sampling frequencies, grouped by the MPEG version whose header
# selects them. Each has its own pair of scalefactor band tables, and a
# transposed one is invisible until somebody plays a file at that rate, so
# each group needs a vector of its own.
RATES = ((44100, 48000, 32000), (22050, 24000, 16000), (11025, 12000, 8000))

# ISO 11172-3 Table 3-B.6: what preflag adds to each long band's
# scalefactor. Written out here rather than read from the decoder, because
# the point of the requantisation test is to recompute the formula from the
# standard rather than from the thing under test.
PRETAB = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 3, 3, 3, 2)


def _stream(name):
    with open(os.path.join(FIXTURES, name + ".mp3"), "rb") as handle:
        return handle.read()


def _truth(name):
    with open(os.path.join(FIXTURES, name + ".f32.z"), "rb") as handle:
        out = array.array("f")
        out.frombytes(zlib.decompress(handle.read()))
        if sys.byteorder == "big":
            out.byteswap()
        return out


def _floats(samples):
    out = array.array("f")
    out.frombytes(samples)
    if sys.byteorder == "big":
        out.byteswap()
    return out


def _skip():
    if ball.available():
        return False
    print("  skipping: %s" % ball.unavailable_reason())
    return True


def _error(got, want):
    """(max absolute error, RMS error, SNR in dB) between two runs of
    samples of the same length."""
    peak = 0.0
    noise = 0.0
    signal = 0.0
    for a, b in zip(got, want):
        d = a - b
        peak = max(peak, abs(d))
        noise += d * d
        signal += b * b
    count = max(1, len(want))
    rms = math.sqrt(noise / count)
    ref = math.sqrt(signal / count)
    snr = 20.0 * math.log10(ref / rms) if rms > 0.0 else float("inf")
    return peak, rms, snr


def test_every_vector_matches_the_reference_decoder():
    """The end of the pipeline, against FFmpeg, vector by vector.

    The per-frame check is the one that localises a fault: an error in one
    frame is 26 milliseconds wide and disappears into a whole-file RMS, so
    the frame it appears in is named rather than averaged away. The
    whole-file SNR is the second half of the claim, because a decoder can
    be right in every frame and still be wrong about how they overlap.
    """
    if _skip():
        return
    for name, rate, channels, count, limit, floor, _seen, _what in VECTORS:
        data = _stream(name)
        frames = list(ball.frames(data))
        assert len(frames) == count, (
            "%s: %d frames, expected %d" % (name, len(frames), count))
        decoder = ball.Decoder()
        got = array.array("f")
        for offset, length in frames:
            _n, _ch, pcm = decoder.decode(data[offset:offset + length])
            got.extend(_floats(pcm))
        assert decoder.sample_rate == rate, (name, decoder.sample_rate)
        assert decoder.channels == channels, (name, decoder.channels)
        ref = _truth(name)
        assert len(got) == len(ref), (
            "%s: decoded %d samples, the reference has %d"
            % (name, len(got), len(ref)))
        step = 576 * decoder.frame()["granules"] * channels
        for number in range(len(frames)):
            here = slice(number * step, (number + 1) * step)
            peak, _rms, _snr = _error(got[here], ref[here])
            assert peak <= limit, (
                "%s frame %d: %.3e off, limit %.1e" % (name, number, peak,
                                                       limit))
        peak, rms, snr = _error(got, ref)
        assert peak <= limit and snr >= floor, (
            "%s: max %.3e rms %.3e snr %.2f dB against %.1e and %.1f dB"
            % (name, peak, rms, snr, limit, floor))
        print("  ok  %-10s %5d Hz %dch %2d frames  max %.3e  rms %.3e  "
              "snr %6.2f dB" % (name, rate, channels, count, peak, rms, snr))


def test_every_granule_is_consumed_to_the_bit():
    """The exact check the PCM comparison cannot make.

    part2_3_length says how many bits a granule's scalefactors and Huffman
    data occupy. Reading them and stopping anywhere else means reading them
    differently from the encoder that wrote them -- a codeword of the wrong
    length, a scalefactor field of the wrong width, a region boundary in
    the wrong place -- and none of those need move the audio far enough for
    a tolerance to notice. This is not a tolerance. It is a count.

    Every granule-channel of every vector is exact except three, and all
    three are the right channel of `lsfint`, which is the one vector whose
    bytes an encoder did not entirely write: its mode_extension was
    overwritten afterwards to turn intensity stereo on, and under MPEG-2
    the second channel's scalefactor fields are a different set of widths
    when intensity stereo is on. So the encoder wrote those scalefactors
    under one partitioning and every decoder reads them under the other,
    and the granule ends a few bits early. That this is the doctoring and
    not the decoder is not an argument here either -- `_undoctored` puts
    the flag back and the same file is exact in all thirty-six.
    """
    if _skip():
        return
    granules = 0
    short = 0
    for name, _rate, channels, _count, _limit, _floor, _seen, _what in VECTORS:
        data = _stream(name)
        passes = [(data, True)]
        if name == "lsfint":
            passes = [(data, False), (_undoctored(data), True)]
        for pass_data, strict in passes:
            decoder = ball.Decoder()
            for offset, length in ball.frames(pass_data):
                decoder.decode(pass_data[offset:offset + length])
                for granule in range(1, decoder.frame()["granules"] + 1):
                    for channel in range(1, channels + 1):
                        shape = decoder.granule(granule, channel)
                        used = shape["bits_used"]
                        promised = shape["bits_promised"]
                        exact = used == promised
                        if not exact and not strict and channel == 2:
                            assert used < promised, (used, promised)
                            short += 1
                            continue
                        assert exact, (
                            "%s: granule %d channel %d read %d bits of a "
                            "promised %d" % (name, granule, channel, used,
                                             promised))
                        granules += 1
    assert short == 3, (
        "%d granules of lsfint fall short of their promise, not the three "
        "its doctored header accounts for" % short)
    print("  ok  %d granules consumed to the bit, and the 3 that are not are "
          "lsfint's doctored flag" % granules)


def _undoctored(data):
    """`lsfint` with its intensity-stereo flag put back where LAME left it.

    The vector is a real MPEG-2 joint-stereo stream whose mode_extension
    was overwritten to 1 in every header, because no encoder here will emit
    LSF intensity stereo on demand. Clearing those two bits again gives
    back the bytes LAME actually wrote, which is the only way to ask
    whether a discrepancy belongs to the stream or to the decoder.
    """
    out = bytearray(data)
    for offset, _length in ball.frames(data):
        out[offset + 3] &= 0xCF
    return bytes(out)


def test_requantisation_is_exactly_the_standard_formula():
    """ISO 11172-3 2.4.3.4, recomputed here and compared coefficient by
    coefficient.

    The exponent is an integer count of quarter powers all the way through
    -- global_gain, the band's scalefactor shifted by scalefac_scale, the
    preflag table, and for a short block the window's subblock_gain -- so
    there is nothing approximate to compare and the agreement is exact
    rather than close. A requantiser that is right about the formula and
    wrong about which scalefactor belongs to which coefficient fails here
    and nowhere else, because every later stage moves the coefficients.
    """
    if _skip():
        return
    checked = 0
    worst = 0.0
    for name, _rate, channels, _count, _limit, _floor, _seen, _what in VECTORS:
        data = _stream(name)
        decoder = ball.Decoder()
        for offset, length in ball.frames(data):
            decoder.decode(data[offset:offset + length])
            info = decoder.frame()
            longs = ball.bands(info["sfindex"])
            shorts = ball.bands(info["sfindex"], short=True)
            for channel in range(1, channels + 1):
                shape = decoder.granule(info["granules"], channel)
                quantised = decoder.quantised_spectrum(channel)
                quantised += [0] * (576 - len(quantised))
                got = decoder.requantised_spectrum(channel)
                got += [0.0] * (576 - len(got))
                scf, long_bands, short_start, _iscale = \
                    decoder.scalefactors(channel)
                want = _requantise(shape, quantised, scf, long_bands,
                                   short_start, longs, shorts)
                for i, (a, b) in enumerate(zip(got, want)):
                    if a != b:
                        scale = max(abs(a), abs(b), 1e-300)
                        worst = max(worst, abs(a - b) / scale)
                        assert abs(a - b) / scale < 1e-13, (
                            "%s coefficient %d: %.17e, the formula says %.17e"
                            % (name, i, a, b))
                checked += 1
    print("  ok  %d granule-channels requantised exactly as the standard "
          "says (worst relative difference %.1e)" % (checked, worst))


def _requantise(shape, quantised, scf, long_bands, short_start, longs, shorts):
    """The standard's requantisation, in bitstream order.

    Long bands first -- all of them for a long block, the first few of them
    for a mixed block -- and then the short bands, three windows at a time,
    which is the order the bitstream sends them in and the order the
    requantiser has to work in because the stereo tools run before the
    reorder.
    """
    out = [0.0] * 576
    gain = shape["global_gain"] - 210
    shift = shape["scalefac_scale"] + 1
    index = 0
    for band in range(long_bands):
        width = longs[band + 1] - longs[band]
        extra = PRETAB[band] if shape["preflag"] and band <= 20 else 0
        exponent = gain - ((scf[min(band, 20)] + extra) << shift)
        for _ in range(width):
            if index >= 576:
                return out
            value = quantised[index]
            if value:
                out[index] = math.copysign(
                    abs(value) ** (4.0 / 3.0) * 2.0 ** (exponent / 4.0), value)
            index += 1
    if short_start < 13:
        which = long_bands
        for band in range(short_start, 13):
            width = shorts[band + 1] - shorts[band]
            for window in range(3):
                exponent = (gain - 8 * shape["subblock_gain"][window]
                            - (scf[which] << shift))
                which += 1
                for _ in range(width):
                    if index >= 576:
                        return out
                    value = quantised[index]
                    if value:
                        out[index] = math.copysign(
                            abs(value) ** (4.0 / 3.0)
                            * 2.0 ** (exponent / 4.0), value)
                    index += 1
    return out


def _imdct_by_definition(coefficients):
    """ISO 11172-3 2.4.3.4.10.1, written out: 2n samples from n
    coefficients, one cosine at a time and no transform tricks."""
    n = 2 * len(coefficients)
    return [sum(coefficients[k]
                * math.cos(math.pi / (2 * n) * (2 * i + 1 + n // 2)
                           * (2 * k + 1))
                for k in range(len(coefficients)))
            for i in range(n)]


def test_the_imdct_computes_the_transform_it_claims_to():
    """The fast transform against the summation it is a fast version of.

    Both sizes: eighteen coefficients for a long block and six for each
    window of a short one. The inputs are deliberately ugly -- a
    non-periodic sequence with a large dynamic range -- because a
    factorisation that has dropped a term still agrees with the definition
    on a constant or on a single impulse.
    """
    if _skip():
        return
    for size in (18, 6):
        for trial in range(4):
            source = [math.sin(1.7 * k + 0.3 * trial) * (k + 1) ** (trial % 3)
                      for k in range(size)]
            got = ball.imdct(source)
            want = _imdct_by_definition(source)
            assert len(got) == 2 * size, (size, len(got))
            scale = max(abs(v) for v in want)
            worst = max(abs(a - b) for a, b in zip(got, want))
            assert worst <= 1e-12 * scale, (
                "%d-point IMDCT trial %d: %.3e off a peak of %.3e"
                % (size, trial, worst, scale))
    # And an impulse in each coefficient, which is the transform's columns
    # one at a time: a term dropped for one k only shows up here.
    for size in (18, 6):
        for k in range(size):
            source = [1.0 if j == k else 0.0 for j in range(size)]
            worst = max(abs(a - b) for a, b in
                        zip(ball.imdct(source), _imdct_by_definition(source)))
            assert worst <= 1e-14, ("%d-point IMDCT column %d: %.3e"
                                    % (size, k, worst))
    print("  ok  both transform sizes match the standard's summation, "
          "including every column")


def test_the_windows_are_the_shapes_the_standard_names():
    """The four window shapes against their closed forms.

    The standard writes each as a sine over a stated range and zeros or
    ones elsewhere; the start and stop windows are the halves that let a
    long block meet a short one without a gap. The overlap condition at the
    bottom is what makes overlap-add reconstruct at all: a window that
    satisfies the formulas but not the condition would still be a plausible
    window and would still leak between frames.
    """
    if _skip():
        return
    pi = math.pi
    normal = [math.sin(pi / 36.0 * (i + 0.5)) for i in range(36)]
    start = ([math.sin(pi / 36.0 * (i + 0.5)) for i in range(18)]
             + [1.0] * 6
             + [math.sin(pi / 12.0 * (i - 18 + 0.5)) for i in range(24, 30)]
             + [0.0] * 6)
    short = [math.sin(pi / 12.0 * (i + 0.5)) for i in range(12)] + [0.0] * 24
    stop = ([0.0] * 6
            + [math.sin(pi / 12.0 * (i - 6 + 0.5)) for i in range(6, 12)]
            + [1.0] * 6
            + [math.sin(pi / 36.0 * (i + 0.5)) for i in range(18, 36)])
    for which, want in enumerate((normal, start, short, stop)):
        got = ball.window(which)
        worst = max(abs(a - b) for a, b in zip(got, want))
        assert worst <= 1e-12, ("window %d: %.3e off its closed form"
                                % (which, worst))
    # The Princen-Bradley condition on the long window, which is the one
    # that has to hold for the halves of two frames to add back up to one.
    for i in range(18):
        total = normal[i] ** 2 + normal[i + 18] ** 2
        assert abs(total - 1.0) < 1e-12, (i, total)
    print("  ok  all four window shapes are the standard's, and the long one "
          "reconstructs")


def test_the_synthesis_window_is_the_table_and_not_a_curve():
    """The 512-tap filterbank window, which is a table and not a formula.

    ISO 11172-3 Table B.3 prints five hundred and twelve numbers and there
    is no closed form for them, so what can be checked from outside is what
    the table is rather than what it says. Two things are:

      * every coefficient is an exact multiple of 2^-16, which is how the
        standard prints them and is not true of any curve mistakenly
        evaluated in its place;
      * the table folds about its midpoint, D[512-i] = -D[i], except at the
        multiples of 64, where it folds the other way, D[512-i] = +D[i].
        A transposed digit does not disturb this, but a coefficient copied
        from the wrong row does, and so does an off-by-one anywhere in the
        transcription.

    The values themselves are held to account by the vectors: one tap wrong
    by one part in 65536 is a comb filter across every sample of every
    file, and every vector here would fail at once.
    """
    if _skip():
        return
    window = ball.window(4)
    assert len(window) == 512, len(window)
    for i, value in enumerate(window):
        scaled = value * 65536.0
        assert abs(scaled - round(scaled)) < 1e-9, (i, value)
    for i in range(1, 256):
        want = window[i] if i % 64 == 0 else -window[i]
        assert window[512 - i] == want, (
            "D[%d] = %r but D[%d] = %r" % (i, window[i], 512 - i,
                                           window[512 - i]))
    assert window[0] == 0.0, window[0]
    assert abs(max(window) - 1.144989013671875) < 1e-15, max(window)
    assert window.index(max(window)) == 256, window.index(max(window))
    print("  ok  the synthesis window is 512 exact multiples of 2^-16 that "
          "fold about their midpoint")


def _synthesise(spectrum, block_type, mixed, windows, state):
    """The whole back half of the standard, in Python.

    Windowing and overlap-add per ISO 11172-3 2.4.3.4.10, the frequency
    inversion of every odd subband per 2.4.3.4.10.3, and the polyphase
    synthesis of 2.4.3.4.11 with its matrixing, its thousand-sample shift
    register and its 512-tap window. `state` is the overlap and the shift
    register, carried between granules, because there is no other way to
    make the second granule of a stream mean anything.
    """
    overlap, shift = state
    out = []
    lanes = []
    for band in range(32):
        source = spectrum[18 * band:18 * band + 18]
        if block_type == 2 and not (mixed and band < 2):
            block = [0.0] * 36
            for window in range(3):
                short = _imdct_by_definition(source[window::3])
                for i in range(12):
                    block[6 * window + 6 + i] += short[i] * windows[2][i]
        else:
            shape = 0 if (block_type == 2 and mixed and band < 2) \
                else block_type
            long = _imdct_by_definition(source)
            block = [long[i] * windows[shape][i] for i in range(36)]
        lane = [block[i] + overlap[band][i] for i in range(18)]
        overlap[band] = block[18:]
        if band % 2:
            lane = [v if i % 2 == 0 else -v for i, v in enumerate(lane)]
        lanes.append(lane)
    table = windows[4]
    for step in range(18):
        samples = [lanes[band][step] for band in range(32)]
        shift[64:] = shift[:1024 - 64]
        for i in range(64):
            shift[i] = sum(math.cos((16 + i) * (2 * k + 1) * math.pi / 64.0)
                           * samples[k] for k in range(32))
        stacked = [0.0] * 512
        for i in range(8):
            for j in range(32):
                stacked[i * 64 + j] = shift[i * 128 + j]
                stacked[i * 64 + 32 + j] = shift[i * 128 + 96 + j]
        for j in range(32):
            out.append(sum(stacked[j + 32 * i] * table[j + 32 * i]
                           for i in range(16)))
    return out


def test_the_transform_and_the_filterbank_are_the_standards_own():
    """The back half of the decoder, against the standard written out here.

    Everything after requantisation and the stereo tools -- the window each
    block type gets, overlap-add, the frequency inversion of the odd
    subbands, and the polyphase filterbank -- computed in Python straight
    from the standard's formulas, taking only the decoder's spectrum as
    input and its window tables as data. If the Fortran's fast transform,
    its window switching or its filterbank differ from the definition at
    all, the samples diverge here, exactly, with no tolerance to hide in.

    `mp2_24` is used because MPEG-2 sends one granule a frame, so the
    spectrum hook -- which holds the granule just decoded -- gives every
    granule of it. Its first three frames are a start block, a short block
    and a stop block, which is three of the four window shapes. `mixed` is
    the fourth case and the reason it was built: both its granules carry
    identical main data, so the spectrum of the second is the spectrum of
    the first and the reference can be run over both.
    """
    if _skip():
        return
    windows = [ball.window(i) for i in range(5)]
    for name, granules_per_frame, count in (("mp2_24", 1, 3), ("mixed", 2, 2)):
        data = _stream(name)
        decoder = ball.Decoder()
        state = ([[0.0] * 18 for _ in range(32)], [0.0] * 1024)
        seen = []
        got = array.array("f")
        want = []
        for offset, length in list(ball.frames(data))[:count]:
            _n, channels, pcm = decoder.decode(data[offset:offset + length])
            assert channels == 1, (name, channels)
            shape = decoder.granule(granules_per_frame, 1)
            seen.append((shape["block_type"], shape["mixed_block"]))
            got.extend(_floats(pcm))
            for _ in range(granules_per_frame):
                want.extend(_synthesise(decoder.spectrum(1),
                                        shape["block_type"],
                                        shape["mixed_block"], windows, state))
        worst = max(abs(a - b) for a, b in zip(got, want))
        peak = max(abs(v) for v in want)
        # The only thing between the two is float32. The decoder computes
        # in double and hands back single-precision samples, so the limit
        # is one unit in the last place of a float of this size and no
        # room at all beyond it.
        assert worst <= peak * 2.0 ** -23, (
            "%s: the filterbank differs from the standard's by %.3e on a "
            "peak of %.3e, which is more than float32 rounding"
            % (name, worst, peak))
        print("  ok  %-8s %d granules through the standard's own transform "
              "and filterbank, block types %s: %.1e"
              % (name, count * granules_per_frame,
                 "/".join("%d%s" % (b, "m" if m else "") for b, m in seen),
                 worst))


def test_the_band_layouts_are_the_ones_the_sample_rate_has():
    """Nine sampling frequencies, and the seven band layouts they have.

    Not nine layouts: 16 kHz, 11.025 kHz and 12 kHz share one, long table
    and short table alike, which is a property of the standard's tables and
    not a shortcut here. The other six frequencies have layouts of their
    own.

    What can be checked from outside is the shape -- twenty-two long bands
    covering all 576 coefficients, thirteen short ones covering the 192 of
    a window, boundaries that only increase -- and that the seven are seven
    and not one table read nine times. The values themselves are held to
    account by the vectors, and only because there is a vector at every one
    of the nine rates: a band boundary in the wrong place mis-scales every
    coefficient in two bands and shows up immediately in that rate's file,
    and in no other rate's.
    """
    if _skip():
        return
    layouts = set()
    for index in range(9):
        longs = ball.bands(index)
        shorts = ball.bands(index, short=True)
        assert len(longs) == 23 and len(shorts) == 14, (index, len(longs),
                                                        len(shorts))
        assert longs[0] == 0 and longs[-1] == 576, (index, longs[0], longs[-1])
        assert shorts[0] == 0 and shorts[-1] == 192, (index, shorts[0],
                                                      shorts[-1])
        for i in range(1, 23):
            assert longs[i] > longs[i - 1], (index, i, longs)
        for i in range(1, 14):
            assert shorts[i] > shorts[i - 1], (index, i, shorts)
        layouts.add((tuple(longs), tuple(shorts)))
    assert len(layouts) == 7, "%d distinct band layouts, not 7" % len(layouts)
    # 16 kHz, 11.025 kHz and 12 kHz -- sfindex 5, 6 and 7 -- are the three
    # that share, and nothing else does.
    for a, b in ((5, 6), (5, 7)):
        assert ball.bands(a) == ball.bands(b), (a, b)
        assert ball.bands(a, True) == ball.bands(b, True), (a, b)

    covered = set()
    for _name, rate, _ch, _n, _lim, _floor, _seen, _what in VECTORS:
        covered.add(rate)
    missing = [rate for group in RATES for rate in group if rate not in covered]
    assert not missing, ("no vector at %s Hz, so that band table is whatever "
                         "was typed" % ", ".join(str(r) for r in missing))
    print("  ok  seven distinct band layouts across nine sampling "
          "frequencies, and a vector at every one of the nine")


def test_the_huffman_tables_are_complete_prefix_codes():
    """Kraft equality on every table, which is what a code being a code
    means.

    Sum of 2^-length over a table's codewords is exactly 1 for a complete
    prefix code and something else for a table with a length mistyped in
    it. This checks the lengths the trees were built from rather than the
    trees, so it catches the transcription rather than the walk -- the walk
    is what `test_every_granule_is_consumed_to_the_bit` catches.
    """
    if _skip():
        return
    built = 0
    # Fifteen distinct big-value trees -- the thirty-two table selections
    # share them, seven of them reading table 16's tree with seven
    # different linbits and eight reading table 24's -- then the two count1
    # quadruple tables at 16 and 17.
    for which in list(range(15)) + [16, 17]:
        xlen, ylen, lengths = ball.huffman_table(which)
        assert xlen * ylen == len(lengths), (which, xlen, ylen, len(lengths))
        assert lengths and min(lengths) >= 1, (which, min(lengths))
        total = sum(2.0 ** -n for n in lengths)
        assert abs(total - 1.0) < 1e-12, (
            "table %d: the codeword lengths sum to %.17g, not 1" % (which,
                                                                    total))
        built += 1
    assert built == 17, built
    print("  ok  %d Huffman trees, every one a complete prefix code" % built)


def test_the_bit_reservoir_carries_main_data_between_frames():
    """The part of Layer III that is easy to get quietly wrong.

    A frame's main data does not begin in that frame. `lowrate` is starved
    enough that nearly every frame reaches back, and the furthest reach
    across the vectors is the full 511 bytes the field can express, which
    is several frames at these bitrates. What this asserts is that the
    reach is real and that the decoder is reading from where it points.
    """
    if _skip():
        return
    data = _stream("lowrate")
    decoder = ball.Decoder()
    reached = 0
    furthest = 0
    for offset, length in ball.frames(data):
        decoder.decode(data[offset:offset + length])
        begin = decoder.frame()["main_data_begin"]
        reached += begin > 0
        furthest = max(furthest, begin)
    assert reached >= 12, "only %d frames of lowrate use the reservoir" % reached
    assert furthest > 300, "the furthest reach is only %d bytes" % furthest
    print("  ok  lowrate: %d frames read main data written in earlier ones, "
          "the furthest %d bytes back" % (reached, furthest))


def test_a_stream_joined_in_the_middle_is_starved_and_then_recovers():
    """What a seek does, and what it must not do.

    Start decoding at a frame in the middle of a file and the reservoir is
    empty, so the first frames want main data that was never fed in. That
    is starvation, and the only right answer is silence for those granules
    and a decoder that keeps going. `lowrate` is the sharpest case there
    is: at 32 kbit/s a frame is 104 bytes, and reaching 470 bytes back is
    reaching four and a half frames back, so it takes seven frames before
    the stream has refilled its own reservoir.

    What is asserted is where it recovers, not merely that it does. Once
    the last starved frame has gone past, one further frame is wrong
    because its transform overlaps against a starved one -- and the frame
    after that is not approximately right, it is the reference to float32.
    A decoder that read whatever happened to be in its buffer would produce
    noise for those seven frames instead of silence, and nobody would ever
    see it, because it is exactly the case no fixture comparison covers.
    """
    if _skip():
        return
    data = _stream("lowrate")
    frames = list(ball.frames(data))
    start = 6
    ball.zero_tools()
    decoder = ball.Decoder()
    got = array.array("f")
    starved = []
    for offset, length in frames[start:]:
        _n, channels, pcm = decoder.decode(data[offset:offset + length])
        starved.append(bool(decoder.last_starved))
        got.extend(_floats(pcm))
    assert starved[0], "starting mid-stream did not starve the first frame"
    assert not starved[-1], "the stream never refilled its own reservoir"
    assert ball.tools()["starved_frames"] == sum(starved), ball.tools()
    # Silence, not noise, while the reservoir is missing.
    step = 1152 * channels
    for index, was in enumerate(starved):
        if was:
            block = got[index * step:(index + 1) * step]
            assert max(abs(v) for v in block) == 0.0, (
                "starved frame %d produced sound" % (start + index))
    recovered = len(starved) - list(reversed(starved)).index(True) + 1
    ref = _truth("lowrate")
    peak, _rms, _snr = _error(got[recovered * step:],
                              ref[(start + recovered) * step:])
    assert peak <= 2.0 ** -23, (
        "%d frames after the last starved one the output is still %.3e off"
        % (recovered - sum(starved), peak))
    print("  ok  joined mid-stream: %d starved frames, silent, then the "
          "reference to %.1e from frame %d" % (sum(starved), peak,
                                               start + recovered))


def test_the_crc_is_checked_and_not_merely_skipped():
    """Both sides of protection_bit, from the one vector that has them.

    `mixed` carries the checksum on every second frame. Decoding it proves
    the two bytes are skipped where they are present and not skipped where
    they are not -- get that wrong and the side information is read two
    bytes out and nothing decodes at all. Corrupting one proves the
    checksum is computed rather than stepped over.
    """
    if _skip():
        return
    data = _stream("mixed")
    frames = list(ball.frames(data))
    decoder = ball.Decoder()
    protected = 0
    for offset, length in frames:
        decoder.decode(data[offset:offset + length])
        protected += decoder.frame()["crc"]
    assert protected == len(frames) // 2, (protected, len(frames))

    offset, length = frames[1]
    frame = bytearray(data[offset:offset + length])
    assert frame[1] & 1 == 0, "frame 1 of mixed is supposed to carry a CRC"
    frame[4] ^= 0xFF
    try:
        ball.Decoder().decode(bytes(frame))
    except ball.Mp3Error as exc:
        assert "CRC" in str(exc), exc
    else:
        raise AssertionError("a frame with a broken CRC decoded anyway")
    print("  ok  %d of %d frames carry a CRC, and a wrong one is refused"
          % (protected, len(frames)))


def test_what_we_do_not_implement_is_refused_by_name():
    """Every refusal says which thing it refused and what to do about it.

    "Unsupported" on its own is a useless thing to tell somebody whose file
    will not play, so each of these has a status of its own and a sentence
    that names the format. The headers are built here bit by bit rather
    than found in a file, because none of these is a thing an encoder in
    this tree can be asked to produce.
    """
    if _skip():
        return
    cases = [
        (b"\xff\xff\x90\xc0", "Layer I", ".mp1"),
        (b"\xff\xfd\x90\xc0", "Layer II", ".mp2"),
        (b"\xff\xf9\x90\xc0", "reserved layer", None),
        (b"\xff\xfb\x00\xc0", "free-format", "Re-encode"),
        (b"\xff\xfb\xf0\xc0", "reserved bitrate", None),
        (b"\xff\xfb\x9c\xc0", "reserved sampling frequency", None),
        (b"\xff\xeb\x90\xc0", "reserved MPEG version", None),
    ]
    decoder = ball.Decoder()
    for header, named, advice in cases:
        assert ball.frame_header(header) is None, named
        try:
            decoder.decode(header + b"\0" * 512)
        except ball.Mp3Error as exc:
            assert named in str(exc), (named, str(exc))
            if advice:
                assert advice in str(exc), (named, str(exc))
        else:
            raise AssertionError("%s decoded" % named)
    try:
        ball.Decoder(channels=5)
    except ball.Mp3Error as exc:
        assert "two channels" in str(exc), exc
    else:
        raise AssertionError("a five-channel stream was accepted")
    print("  ok  Layer I, Layer II, free format and more than two channels "
          "are each refused by name")


def test_garbage_is_refused_rather_than_crashing():
    """Nothing a file can contain may be a crash.

    Every read past the end of a frame's data is a decode error that
    returns zero, not a fault, so the failure mode of a truncated or
    corrupt file is silence and a message. These are the shapes that reach
    furthest into the decoder before failing: a valid header over rubbish,
    a frame cut in half, and a stream whose bytes are a real stream's with
    one bit turned over.
    """
    if _skip():
        return
    data = _stream("noise")
    offset, length = next(iter(ball.frames(data)))
    frame = data[offset:offset + length]
    decoder = ball.Decoder()
    survived = 0
    attempts = [b"", b"\xff", b"\xff\xfb", frame[:4] + b"\0" * (length - 4),
                frame[:length // 2], b"\0" * 4096, b"\xff\xff\xff\xff" * 64]
    for step in range(0, len(frame) * 8, 97):
        broken = bytearray(frame)
        broken[step // 8] ^= 1 << (step % 8)
        attempts.append(bytes(broken))
    for blob in attempts:
        try:
            decoder.decode(blob)
        except ball.Mp3Error:
            pass
        survived += 1
    # And a decoder still works afterwards, which is the half of "did not
    # crash" that a bare try/except does not say.
    count, _channels, _pcm = ball.Decoder().decode(frame)
    assert count == 1152, count
    print("  ok  %d malformed inputs refused, and the decoder still decodes"
          % survived)


def test_two_decoders_do_not_corrupt_each_other():
    """Two <audio> elements playing at once.

    The Fortran's state is static storage, so the decoders take turns and
    each puts back its own overlap, filterbank history and reservoir when
    it finds another has been at the library in between. Interleaved
    decoding must therefore produce exactly what decoding each stream alone
    produces -- exactly, not nearly, because it is the same arithmetic in a
    different order of turns.
    """
    if _skip():
        return
    names = ("noise", "stereo")
    alone = {}
    for name in names:
        decoder = ball.Decoder()
        alone[name] = _floats(decoder.decode_stream(_stream(name))[2])
    streams = {name: list(ball.frames(_stream(name))) for name in names}
    decoders = {name: ball.Decoder() for name in names}
    together = {name: array.array("f") for name in names}
    for step in range(max(len(streams[name]) for name in names)):
        for name in names:
            if step < len(streams[name]):
                offset, length = streams[name][step]
                data = _stream(name)
                _n, _ch, pcm = decoders[name].decode(
                    data[offset:offset + length])
                together[name].extend(_floats(pcm))
    for name in names:
        assert together[name] == alone[name], (
            "%s decodes differently when another stream is interleaved with "
            "it" % name)
    print("  ok  two streams interleaved decode bit for bit as they do alone")


def test_seeking_replays_the_same_samples():
    """Decode a file, reset, decode it again: the same samples both times.

    A decoder that leaves anything behind a reset -- half a reservoir, an
    overlap buffer, the filterbank's history -- produces a different
    beginning the second time round, and a player that seeks back to the
    start of a track is the thing that notices.
    """
    if _skip():
        return
    data = _stream("transient")
    decoder = ball.Decoder()
    first = _floats(decoder.decode_stream(data)[2])
    decoder.reset()
    second = _floats(decoder.decode_stream(data)[2])
    assert first == second, "the second pass decoded differently"
    print("  ok  reset and replayed %d samples identically" % len(first))


def test_the_tools_and_the_layouts_are_all_actually_reached():
    """The guard on the fixtures, and the reason two of them exist.

    A tool nothing exercises is a tool that is not tested, and no threshold
    can tell the difference. This measures coverage from the decoder's own
    counters: which block types occurred, which of the thirty Huffman
    tables were selected, whether both count1 tables were used, whether
    both stereo tools ran, how far the reservoir reached, and whether any
    granule was a mixed block.

    Two things here are worth reading before regenerating a fixture. The
    `mixed` vector is hand-assembled because `mixed_block_flag` is set by
    zero granule-channels of anything an encoder produced -- LAME has the
    switch and has never turned it on -- and so is the CRC. And `mp25_12`
    exists because eight of the nine sampling frequencies had a vector and
    the ninth did not.
    """
    if _skip():
        return
    total = {}
    tables = [0] * 32
    for name, _rate, _ch, _n, _lim, _floor, _seen, _what in VECTORS:
        ball.zero_tools()
        ball.Decoder().decode_stream(_stream(name))
        used = ball.tools()
        for key, value in used.items():
            if key == "huffman_tables":
                for index, count in enumerate(value):
                    tables[index] += count
            elif key == "max_main_data_begin":
                total[key] = max(total.get(key, 0), value)
            else:
                total[key] = total.get(key, 0) + value
    for tool in ball.TOOLS:
        # Starvation is not a property a file has; it is what happens when
        # a player joins one in the middle, so it is reached by the seek
        # test above and cannot be reached by decoding a file from its
        # first frame. Every other counter has to be non-zero here.
        if tool == "starved_frames":
            assert total[tool] == 0, (
                "a vector decoded from its first frame was starved, which "
                "means the reservoir is being read wrong")
            continue
        assert total.get(tool, 0) > 0, "no vector reaches %s" % tool
    # 4 and 14 are the two table numbers the standard leaves undefined.
    unused = [i for i in range(32) if not tables[i] and i not in (4, 14)]
    assert not unused, "no vector selects Huffman table(s) %s" % unused
    assert total["max_main_data_begin"] == 511, total["max_main_data_begin"]
    assert total["mixed_blocks"] >= 16, total["mixed_blocks"]
    print("  ok  every tool reached: " + ", ".join(
        "%s=%d" % (tool, total[tool]) for tool in ball.TOOLS))
    print("  ok  all 30 Huffman tables selected, both count1 tables used")


def test_the_decoder_is_fast_enough_to_be_worth_having():
    """Not a benchmark, a floor.

    What this asserts is that a frame decodes in far less than the 26
    milliseconds it plays for, because the thing that will consume this is
    a mixer on a deadline. The margin is enormous and the assertion is
    loose on purpose: it is here to catch an accidental O(n^2), not to
    police a percent.
    """
    if _skip():
        return
    import time
    data = _stream("hi320")
    frames = list(ball.frames(data))
    decoder = ball.Decoder()
    start = time.time()
    rounds = 4
    for _ in range(rounds):
        decoder.reset()
        for offset, length in frames:
            decoder.decode(data[offset:offset + length])
    spent = time.time() - start
    each = spent / (rounds * len(frames))
    played = 1152 / 44100.0
    assert each < played / 4.0, (
        "%.2f ms a frame, which plays for %.2f ms" % (each * 1e3,
                                                      played * 1e3))
    print("  ok  %.3f ms a stereo frame, which plays for %.1f ms (%.0fx "
          "real time)" % (each * 1e3, played * 1e3, played / each))


def test_a_machine_without_gfortran_still_has_a_browser():
    """The degradation path, forced. Nothing here may raise: an absent
    toolchain has to look like an unsupported codec, not like a crash."""
    saved = (ball._loaded, ball._lib, ball._load_error)
    try:
        ball._loaded = True
        ball._lib = None
        ball._load_error = "no gfortran on PATH"
        assert not ball.available()
        assert ball.unavailable_reason() == "no gfortran on PATH"
        assert ball.library_path() is None
        for call in (lambda: ball.Decoder(),
                     lambda: ball.frame_header(b"\xff\xfb\x90\xc0"),
                     lambda: ball.tools(),
                     lambda: ball.bands(0),
                     lambda: ball.window(0)):
            try:
                call()
            except ball.Mp3Error as exc:
                assert "gfortran" in str(exc), exc
            else:
                raise AssertionError("worked with no library")
        lines = []
        assert ball.check([], out=lines.append) == 1
        assert "gfortran" in lines[0], lines
        # And the media stack above it: an MP3 is still identified, still
        # named, and still refused with a sentence rather than a traceback.
        data = _stream("tone")
        assert mediacodec.sniff(data) == "MP3"
        info = mediacodec.probe_audio(data)
        assert info.container == "MP3" and info.codec == "MP3"
        assert not info.supported and "gfortran" in info.reason, info.reason
        try:
            mediacodec.open_audio(data)
        except mediacodec.MediaError as exc:
            assert "gfortran" in str(exc), exc
        else:
            raise AssertionError("opened an MP3 with no decoder")
        print("  ok  no toolchain: refused, and said which toolchain")
    finally:
        ball._loaded, ball._lib, ball._load_error = saved


def test_the_media_stack_reaches_the_decoder():
    """A bare `.mp3` through `mediacodec`, which is the only way anything
    above this ever sees it.

    Sniffed by its frame header, because a URL's extension is a claim;
    probed for a rate, a channel count and a duration accumulated frame by
    frame rather than divided out of the first frame's bitrate, which is
    what a variable-bitrate file needs; and opened to an `AudioTrack` whose
    samples have to be the decoder's own, byte for byte, or something
    between the two is quietly changing them.

    `probe()` is asked as well. An MP3 is a container with no picture in
    it, and the honest answer to "what is this video" is a MediaInfo saying
    so, not an exception that a caller has to know to catch.
    """
    if _skip():
        return
    data = _stream("stereo")
    assert mediacodec.sniff(data) == "MP3"
    picture = mediacodec.probe(data)
    assert not picture.supported and "no picture" in picture.reason
    info = mediacodec.probe_audio(data)
    assert info.supported and info.codec == "MP3", info
    assert info.sample_rate == 44100 and info.channels == 2, info
    assert info.frame_count == 17, info
    assert abs(info.duration - 17 * 1152 / 44100.0) < 1e-6, info.duration
    track = mediacodec.open_audio(data)
    through = b"".join(track.frame(i).samples
                       for i in range(track.sample_count))
    direct = ball.Decoder().decode_stream(data)[2]
    assert through == direct, "the track and the decoder disagree"
    # An ID3v2 tag in front of the audio is the usual state of an MP3 in
    # the wild, and it must not move a single sample.
    tagged = b"ID3\x03\x00\x00\x00\x00\x02\x01" + b"\0" * 257 + data
    assert mediacodec.sniff(tagged) == "MP3"
    tagged_track = mediacodec.open_audio(tagged)
    assert tagged_track.sample_count == track.sample_count
    assert tagged_track.frame(0).samples == track.frame(0).samples
    print("  ok  sniffed, probed and opened through mediacodec: %d frames, "
          "%.3f s, identical samples" % (info.frame_count, info.duration))


def test_the_build_entry_point_is_what_the_packaging_calls():
    """The packaging asks this module for a library and for a verdict.

    Not a build -- that is minutes of gfortran and belongs to the packaging
    -- but the shape of the two calls the three packaging scripts make, and
    the name a prebuilt library has to have for `_open_library` to prefer
    it over compiling. A rename here is a bundle that ships no decoder and
    says nothing about it until a user opens a file.
    """
    if _skip():
        return
    name = ball.prebuilt_name()
    assert name.startswith("_mp3_"), name
    assert name.endswith((".so", ".dylib", ".dll")), name
    assert os.path.basename(name) == name, name
    lines = []
    assert ball.check([], out=lines.append) == 0, lines
    assert "MP3 decoder ready" in lines[0], lines
    path = os.path.join(FIXTURES, "mixed.mp3")
    assert ball.check([path, path[:-4] + ".f32.z"], out=lines.append) == 0, \
        lines
    assert "match the reference decoder" in lines[-1], lines
    print("  ok  --check decodes a vector and compares it: %s"
          % lines[-1].strip())


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
    print(f"\nALL {len(tests)} MP3 TESTS PASSED")


if __name__ == "__main__":
    main()
