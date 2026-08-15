"""The AAC-LC decoder, against ground truth from a reference decoder.

Every `.f32.z` in `tests/fixtures/aac` is exactly what FFmpeg 7.1 decoded
the `.aac` beside it to: interleaved 32-bit floats, zlib-compressed. FFmpeg
is not involved in running these tests and is not a dependency of anything;
it produced the fixtures once, offline, and what ships is the numbers it
produced. `tests/fixtures/aac/make_aac_vectors.sh` is that offline tool and
says what each vector is for.

Unlike H.264, AAC is not a bit-exact specification. The standard defines the
inverse transform in real arithmetic and leaves the arithmetic to the
implementation, so two conforming decoders produce very slightly different
samples and an exact comparison would be a comparison of rounding. The
thresholds here are therefore numerical, and they are the measured numbers
rather than round ones that happen to pass. Across the fourteen vectors, the
instep decoder differs from FFmpeg by at worst

    max |error|   3.6e-07      (1.2 LSB of a 24-bit sample; FFmpeg's own
                                float32 output quantises at 6e-08 near full
                                scale, so this is a handful of ulps)
    RMS error     3.4e-08
    SNR         137.9 dB       (worst vector; best 139.9 dB)

and the assertions below are 1e-06, 1e-07 and 130 dB -- roughly three times
the measured error and eight decibels of margin, which is room for a
different libm's sine in the twiddle table and not room for a decoding bug.
For scale, measured by deleting each in turn: a one-bit change to the noise
generator's seed takes the `tone` vector from 6e-08 to 2e-04 in its first
frame, and deleting the call to the TNS filter takes `tns` from 140 dB to
29 dB. A tolerance this far above the floor cannot hide a tool.

That last number is only true because of a vector added for it.
`test_the_tools_and_the_layouts_are_all_actually_reached` is the guard, and
the comment on it is worth reading before regenerating any fixture: a
threshold proves nothing about code no vector reaches.

A tolerance at the end of a pipeline can hide a bug in the middle of it, so
the stages that *can* be compared exactly are compared exactly and not
through the PCM:

  * every frame of every vector is consumed to the bit -- the number of bits
    read is the number the ADTS header said the frame contained. Nothing
    about a Huffman table can be wrong and leave that true: one codeword of
    the wrong length in any of the eleven codebooks desynchronises the read
    and the count lands somewhere else.
  * dequantisation is checked against sign(q)*|q|^(4/3)*2^((sf-100)/4)
    recomputed in Python, coefficient by coefficient. It agrees to the last
    bit -- the measured relative error is 0.0 -- because the Fortran's
    lookup tables hold exactly that.
  * the fast IMDCT is held against the standard's summation, written out in
    Python here rather than in Fortran, at both transform sizes.
  * the windows are checked against their closed forms and against the
    Princen-Bradley condition, which is what makes overlap-add reconstruct.

The whole suite skips where there is no gfortran, and the last test forces
that path: a machine without the toolchain still has a browser, it just has
a browser that says "no decoder" for AAC.
"""
import array
import math
import os
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import aac, mediacodec

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "aac")

# name, sample rate, channels, coded frames, and what the vector is here to
# catch. make_aac_vectors.sh says how each one was made.
VECTORS = [
    ("tone", 44100, 1, 17, "a pure tone: long windows and noise substitution"),
    ("noise", 44100, 1, 14, "broadband noise: every band coded, wide books"),
    ("stereo", 44100, 2, 21, "mid/side in every frame, intensity at the top"),
    ("transient", 44100, 1, 27, "eight short windows, grouped, and the "
                                "start/stop windows either side"),
    ("lowrate", 44100, 2, 14, "32 kbit/s stereo: mid/side and intensity "
                              "under pressure"),
    ("sr48", 48000, 1, 18, "48 kHz, a different band layout"),
    ("sr16", 16000, 1, 8, "16 kHz, the low-rate band layout"),
    ("sr8", 8000, 1, 5, "8 kHz, the shortest band table there is"),
    ("hi320", 44100, 2, 12, "320 kbit/s: coefficients into the thousands, so "
                            "codebook 11's escape at full length"),
    ("sr96", 96000, 1, 13, "96 kHz, the shortest long-block table: 41 bands"),
    ("sr64", 64000, 1, 11, "64 kHz, a layout of its own"),
    ("sr32", 32000, 1, 8, "32 kHz, 51 bands -- the longest table there is"),
    ("sr24", 24000, 1, 7, "24 kHz, the layout 22.05 kHz shares"),
    ("tns", 44100, 1, 19, "temporal noise shaping, from an encoder that "
                          "actually emits it"),
]

# The eight long-block scalefactor band layouts the standard defines, keyed
# by the sample rates that share each one. Every group needs a vector: a
# transposed table in one of them is invisible until somebody plays a file
# at that rate.
LAYOUTS = [(96000, 88200), (64000,), (48000,), (44100,), (32000,),
           (24000, 22050), (16000, 12000, 11025), (8000, 7350)]

# The measured error is 3.6e-07 / 3.4e-08 / 137.9 dB; see the module
# docstring for why these are the numbers and not looser ones.
MAX_ABS = 1.0e-06
MAX_RMS = 1.0e-07
MIN_SNR = 130.0


def _stream(name):
    with open(os.path.join(FIXTURES, name + ".aac"), "rb") as handle:
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
    if aac.available():
        return False
    print("  skipping: %s" % aac.unavailable_reason())
    return True


def _decode_all(name):
    """Every frame of a vector, as a list of per-frame float arrays."""
    blob = _stream(name)
    decoder = aac.Decoder(aac.asc_from_adts(blob))
    frames = []
    rest = blob
    for head, length in aac.adts_frames(blob):
        _count, _channels, samples = decoder.decode(rest[head:length])
        rest = rest[length:]
        frames.append(_floats(samples))
    return decoder, frames


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
    frame's spectrum is 23 milliseconds wide and disappears into a
    whole-file RMS, so the frame it appears in is reported rather than
    averaged away. The whole-file SNR is the second half of the claim,
    because a decoder can be right in every frame and still be wrong about
    how the frames overlap.
    """
    if _skip():
        return
    for name, rate, channels, frames, what in VECTORS:
        decoder, decoded = _decode_all(name)
        assert decoder.sample_rate == rate, (name, decoder.sample_rate)
        assert decoder.channels == channels, (name, decoder.channels)
        assert len(decoded) == frames, (
            "%s: %d coded frames, expected %d" % (name, len(decoded), frames))
        ref = _truth(name)
        got = array.array("f")
        for frame in decoded:
            got.extend(frame)
        assert len(got) == len(ref), (
            "%s: decoded %d samples, the reference has %d"
            % (name, len(got), len(ref)))
        step = 1024 * channels
        for i, frame in enumerate(decoded):
            want = ref[i * step:(i + 1) * step]
            peak, _rms, _snr = _error(frame, want)
            assert peak <= MAX_ABS, (
                "%s (%s) frame %d: worst sample off by %.3e, limit %.1e"
                % (name, what, i, peak, MAX_ABS))
        peak, rms, snr = _error(got, ref)
        assert rms <= MAX_RMS, "%s: RMS error %.3e, limit %.1e" % (
            name, rms, MAX_RMS)
        assert snr >= MIN_SNR, "%s: %.2f dB SNR, floor %.1f dB" % (
            name, snr, MIN_SNR)
        print("  ok  %-10s %dch %5dHz %2d frames  max %.2e  rms %.2e  "
              "%6.2f dB  %s" % (name, channels, rate, frames, peak, rms,
                                snr, what))


def test_every_frame_is_consumed_to_the_bit():
    """The exact test that a tolerance cannot hide anything behind.

    An ADTS header carries the frame's length, so the stream says how many
    bits the frame is. The decoder reports how many it actually read. Those
    two numbers agreeing on every frame of every vector means every section,
    every scalefactor difference, every Huffman codeword in all eleven
    codebooks and every escape sequence was read at exactly the right
    length -- a decoder that gets a codeword wrong reads the next one from
    the wrong bit and never finds its way back to the boundary.
    """
    if _skip():
        return
    total = 0
    for name, _rate, _channels, _frames, _what in VECTORS:
        blob = _stream(name)
        decoder = aac.Decoder(aac.asc_from_adts(blob))
        rest = blob
        for index, (head, length) in enumerate(aac.adts_frames(blob)):
            decoder.decode(rest[head:length])
            rest = rest[length:]
            assert decoder.last_bits == decoder.last_bits_offered, (
                "%s frame %d: read %d bits of the %d the header promised"
                % (name, index, decoder.last_bits,
                   decoder.last_bits_offered))
            total += 1
    print("  ok  %d frames, every one read to the last bit" % total)


def test_dequantisation_is_exactly_the_standard_formula():
    """4.6.2, coefficient by coefficient, against Python's own arithmetic.

    The Fortran does this with two lookup tables -- |q|^(4/3) for every
    representable q, and 2^(sf/4) for every scalefactor -- because doing it
    with a pow() per coefficient is the hot loop nobody needs. Tables are
    exactly the sort of thing that is right for the first few thousand
    entries and wrong at the end, so every coefficient of every long-window
    frame in the mono vectors is recomputed here from the quantised value
    and the scalefactor that the decoder itself reports.

    Only long windows, and only bands whose codebook is a spectral one:
    a short-window frame interleaves its groups, and codebooks 13, 14 and 15
    are noise and intensity, which are not quantised coefficients at all.
    Frames using TNS are skipped because `spectrum()` is taken after the
    filter has run over them.
    """
    if _skip():
        return
    worst = 0.0
    checked = 0
    for name, _rate, channels, _frames, _what in VECTORS:
        if channels != 1:
            continue
        blob = _stream(name)
        decoder = aac.Decoder(aac.asc_from_adts(blob))
        rest = blob
        for head, length in aac.adts_frames(blob):
            decoder.decode(rest[head:length])
            rest = rest[length:]
            shape = decoder.shape(1)
            if shape["window_sequence"] == 2 or shape["tns"]:
                continue
            quantised = decoder.quantised_spectrum(1)
            spectrum = decoder.spectrum(1)
            books, factors, offsets = decoder.bands(1)
            for band in range(shape["num_swb"]):
                book = books[band]
                if not 1 <= book <= 11:
                    continue
                gain = 2.0 ** (0.25 * (factors[band] - 100))
                for i in range(offsets[band], offsets[band + 1]):
                    q = quantised[i]
                    want = math.copysign(abs(q) ** (4.0 / 3.0), q) * gain
                    got = spectrum[i]
                    if want == 0.0:
                        assert got == 0.0, (name, i, got)
                        continue
                    worst = max(worst, abs(got - want) / abs(want))
                    checked += 1
    assert checked > 20000, "only %d coefficients checked" % checked
    # Measured 0.0 -- the tables are exact. Two ulps of headroom is for a
    # libm whose pow() rounds the reference differently, not for the tables.
    assert worst <= 4.0e-16, "dequantisation is off by %.3e relative" % worst
    print("  ok  %d coefficients dequantised, worst relative error %.1e"
          % (checked, worst))


def _imdct_by_definition(coefficients, outputs=None):
    """The standard's inverse MDCT, written as the sum it is defined as.

    x[i] = 2/N * sum_k X[k] cos(2*pi/N * (i + 1/2 + N/4) * (k + 1/2))

    Slow on purpose and independent on purpose: the Fortran's fast path is a
    complex FFT with a twiddle and a fold, which is a different program that
    is supposed to compute this. `outputs` picks a subset of i, because at
    1024 coefficients the whole thing is two million cosines.
    """
    size = len(coefficients)
    n = 2 * size
    if outputs is None:
        outputs = range(n)
    step = 2.0 * math.pi / n
    out = {}
    for i in outputs:
        total = 0.0
        base = step * (i + 0.5 + n / 4.0)
        for k, value in enumerate(coefficients):
            if value:
                total += value * math.cos(base * (k + 0.5))
        out[i] = 2.0 / n * total
    return out


def test_the_fast_imdct_computes_the_transform_it_claims_to():
    """Both sizes, against the summation and against the slow twin.

    The short transform is checked at every one of its 256 outputs; the long
    one at 48 of its 2048, spread across the range, because a fold error
    shows up as a whole region of the output being the wrong sign or the
    wrong way round rather than as one bad sample.
    """
    if _skip():
        return
    seed = 12345
    def rand():
        nonlocal seed
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        return seed / 0x40000000 - 1.0

    for size, sample in ((128, None), (1024, 48)):
        coefficients = [rand() * 100.0 for _ in range(size)]
        fast = aac.imdct(coefficients, fast=True)
        slow = aac.imdct(coefficients, fast=False)
        scale = max(abs(v) for v in slow)
        picks = (None if sample is None
                 else [i * (2 * size) // sample for i in range(sample)])
        want = _imdct_by_definition(coefficients, picks)
        worst_pair = max(abs(a - b) for a, b in zip(fast, slow)) / scale
        worst_def = max(abs(fast[i] - v) for i, v in want.items()) / scale
        assert worst_pair <= 1.0e-12, (
            "%d-point: the two implementations differ by %.3e relative"
            % (size, worst_pair))
        assert worst_def <= 1.0e-12, (
            "%d-point: the fast transform is %.3e from the definition"
            % (size, worst_def))
        # An impulse in one coefficient is one cosine out, which is the case
        # where a wrong twiddle phase is unmistakable rather than averaged.
        for index in (0, 1, size // 2, size - 1):
            impulse = [0.0] * size
            impulse[index] = 1.0
            got = aac.imdct(impulse, fast=True)
            ref = _imdct_by_definition(impulse)
            # Absolute, because the output of a unit impulse is 2/N tall
            # while the FFT inside works on numbers of order one: what is
            # being bounded here is the transform's own rounding, and a
            # double has 2.2e-16 of that to give away per operation.
            worst = max(abs(got[i] - ref[i]) for i in range(2 * size))
            assert worst <= 1.0e-14, (
                "%d-point, coefficient %d alone: off by %.3e"
                % (size, index, worst))
        print("  ok  %4d-point IMDCT: %.1e from the definition, %.1e from "
              "the slow path" % (size, worst_def, worst_pair))


def test_the_windows_are_the_windows_the_standard_names():
    """Sine against its closed form, KBD against Princen-Bradley.

    The sine window is written down in the standard and can be checked
    exactly. The KBD window is defined through a Kaiser-Bessel kernel and a
    cumulative sum, which is a recipe rather than a formula, so what is
    checked is the property that makes it usable: w[n]^2 + w[N-1-n]^2 = 1,
    which is what makes the overlap of two adjacent frames add back up to
    one. A window that fails this reconstructs with a slow amplitude ripple
    that no single frame's error would show.
    """
    if _skip():
        return
    for which, size, kind in ((0, 1024, "long sine"), (1, 1024, "long KBD"),
                              (2, 128, "short sine"), (3, 128, "short KBD")):
        rising = aac.window(which)
        assert len(rising) == size, (kind, len(rising))
        n = 2 * size
        worst_pr = max(abs(rising[i] ** 2 + rising[size - 1 - i] ** 2 - 1.0)
                       for i in range(size))
        assert worst_pr <= 1.0e-14, (
            "%s: Princen-Bradley off by %.3e" % (kind, worst_pr))
        assert rising[0] < rising[-1], "%s does not rise" % kind
        assert all(0.0 <= v <= 1.0 for v in rising), "%s leaves [0,1]" % kind
        if which in (0, 2):
            worst = max(abs(rising[i]
                            - math.sin(math.pi / n * (i + 0.5)))
                        for i in range(size))
            assert worst <= 1.0e-15, "%s: %.3e from sin()" % (kind, worst)
            print("  ok  %-10s exact to %.1e, Princen-Bradley to %.1e"
                  % (kind, worst, worst_pr))
        else:
            print("  ok  %-10s Princen-Bradley to %.1e" % (kind, worst_pr))


def test_the_band_layouts_are_the_ones_the_sample_rate_has():
    """What `bands()` reports has to be a legal layout, on every frame.

    Cheap, and it catches the class of bug where a decoder reads a plausible
    spectrum out of the wrong table: offsets that do not increase, a last
    band that does not end at the end of the spectrum, a codebook of 12
    (reserved), a scalefactor outside 0..255.
    """
    if _skip():
        return
    for name, _rate, channels, _frames, _what in VECTORS:
        blob = _stream(name)
        decoder = aac.Decoder(aac.asc_from_adts(blob))
        rest = blob
        for index, (head, length) in enumerate(aac.adts_frames(blob)):
            decoder.decode(rest[head:length])
            rest = rest[length:]
            for channel in range(1, channels + 1):
                shape = decoder.shape(channel)
                books, factors, offsets = decoder.bands(channel)
                count = shape["num_swb"]
                width = 128 if shape["window_sequence"] == 2 else 1024
                assert 0 < count < 52, (name, index, count)
                assert offsets[0] == 0, (name, index, offsets[0])
                assert offsets[count] == width, (
                    "%s frame %d: the last band ends at %d, not %d"
                    % (name, index, offsets[count], width))
                for b in range(count):
                    assert offsets[b] < offsets[b + 1], (name, index, b)
                assert shape["max_sfb"] <= count, (name, index)
                groups = shape["groups"]
                assert sum(shape["group_lengths"]) == shape["windows"], (
                    name, index, shape)
                for g in range(groups):
                    for b in range(shape["max_sfb"]):
                        book = books[b + 52 * g]
                        assert 0 <= book <= 15 and book != 12, (
                            "%s frame %d: codebook %d" % (name, index, book))
                        # 0..255 is the range for a spectral band's
                        # scalefactor. The same syntax element carries the
                        # noise energy of a PNS band and the position of an
                        # intensity band, and neither of those is bounded
                        # that way, so they are not held to it.
                        if 1 <= book <= 11:
                            factor = factors[b + 52 * g]
                            assert 0 <= factor <= 255, (
                                "%s frame %d: scalefactor %d"
                                % (name, index, factor))
    print("  ok  every frame's band layout, codebooks and scalefactors are "
          "in range")


def test_the_mp4_path_decodes_the_same_samples_as_the_adts_path():
    """The container layer: the same coded frames with the headers stripped
    and an AudioSpecificConfig in an `esds` box instead.

    `stereo.mp4` is `stereo.aac` remuxed without re-encoding, so the frames
    really are the same frames and the samples must be the same samples --
    not close, identical, because it is one decoder decoding one bitstream
    twice. That also makes the reference PCM apply to both, which is why the
    MP4 is compared against FFmpeg's numbers as well.
    """
    with open(os.path.join(FIXTURES, "stereo.mp4"), "rb") as handle:
        data = handle.read()
    info = mediacodec.probe_audio(data)
    assert info.codec == "mp4a", info
    assert (info.sample_rate, info.channels) == (44100, 2), info
    assert info.frame_count == 21, info
    assert abs(info.duration - 21 * 1024 / 44100.0) < 1e-6, info
    if _skip():
        assert not info.supported, "no decoder, but the file was accepted"
        assert info.reason, "refused an AAC file without saying why"
        return
    assert info.supported, "an AAC MP4 was refused: %s" % info.reason
    track = mediacodec.open_audio(data)
    assert track.asc == bytes([0x12, 0x10]), track.asc.hex()
    assert track.sample_count == 21, track.sample_count
    ref = _truth("stereo")
    got = array.array("f")
    for i in range(track.sample_count):
        frame = track.frame(i)
        assert frame.sample_count == 1024, (i, frame.sample_count)
        assert frame.channels == 2, (i, frame.channels)
        assert abs(frame.pts - i * 1024 / 44100.0) < 1e-9, (i, frame.pts)
        got.extend(_floats(frame.samples))
    assert len(got) == len(ref), (len(got), len(ref))
    peak, rms, snr = _error(got, ref)
    assert peak <= MAX_ABS and rms <= MAX_RMS and snr >= MIN_SNR, (
        "MP4 path: max %.3e rms %.3e SNR %.2f dB" % (peak, rms, snr))
    # And against the ADTS path, exactly.
    _decoder, frames = _decode_all("stereo")
    adts = array.array("f")
    for frame in frames:
        adts.extend(frame)
    assert list(adts) == list(got), (
        "the MP4 and the ADTS file decoded to different samples")
    print("  ok  MP4 -> esds -> AudioSpecificConfig -> PCM, identical to "
          "the ADTS path, %.2f dB against FFmpeg" % snr)


def test_seeking_replays_and_two_decoders_do_not_corrupt_each_other():
    """The two hazards the shared state creates.

    There is one decoder in the process -- the Fortran keeps its state in
    COMMON -- and every AAC frame depends on the previous frame's second
    transform half, so both a seek and a second `<audio>` element have to
    put that state back. Asking for a frame out of order must replay from
    the start of the file, and two files decoded alternately must each come
    out exactly as they do alone. Exact, both of them: this is the same
    arithmetic on the same bits, so anything but equality is state leaking.
    """
    if _skip():
        return
    _decoder, frames = _decode_all("stereo")
    with open(os.path.join(FIXTURES, "stereo.mp4"), "rb") as handle:
        track = mediacodec.open_audio(handle.read())
    for index in (5, 0, 12, 3, 20):
        got = _floats(track.frame(index).samples)
        assert list(got) == list(frames[index]), (
            "frame %d decoded differently when seeked to" % index)

    first, second = "tone", "noise"
    alone = {name: _decode_all(name)[1] for name in (first, second)}
    streams = []
    for name in (first, second):
        blob = _stream(name)
        streams.append([name, aac.Decoder(aac.asc_from_adts(blob)), blob,
                        list(aac.adts_frames(blob)), 0])
    for i in range(max(len(s[3]) for s in streams)):
        for state in streams:
            name, decoder, blob, bounds, _pos = state
            if i >= len(bounds):
                continue
            head, length = bounds[i]
            offset = sum(b[1] for b in bounds[:i])
            _n, _c, samples = decoder.decode(blob[offset + head:
                                                  offset + length])
            assert list(_floats(samples)) == list(alone[name][i]), (
                "%s frame %d changed when interleaved with the other stream"
                % (name, i))
    print("  ok  seeks replay, and two streams decode a frame at a time in "
          "turn without touching each other")


def test_garbage_is_refused_rather_than_crashing():
    """Every byte in these streams came from a stranger, so the decoder is
    fed some that came from nowhere at all. None of this may segfault, hang
    or come back as samples; `AacError` is the only acceptable answer."""
    if _skip():
        return
    good = _stream("tone")
    asc = aac.asc_from_adts(good)
    head, length = next(iter(aac.adts_frames(good)))
    frame = good[head:length]
    empty = aac.Decoder(asc).decode(b"")
    assert empty == (0, 1, b""), "an empty packet decoded to %r" % (empty,)
    cases = [
        (b"\x00" * 512, "all zeroes"),
        (b"\xff" * 512, "all ones"),
        (frame[:len(frame) // 2], "a frame truncated halfway"),
        (frame[:8] + b"\x5a" * 24 + frame[32:], "a frame corrupted inside"),
        (frame[:8] + b"\xff" * (len(frame) - 8), "a frame of 0xff"),
        (bytes(reversed(frame)), "a frame backwards"),
    ]
    for data, what in cases:
        decoder = aac.Decoder(asc)
        try:
            count, _channels, samples = decoder.decode(data)
        except aac.AacError:
            print("  ok  refused %s" % what)
            continue
        # Decoding something is allowed -- a corrupted frame can still be a
        # legal one -- but it has to be a whole frame of samples and not a
        # walk off the end of a buffer.
        assert count == 1024 and len(samples) == 1024 * 4 * decoder.channels, (
            "%s: decoded %d samples into %d bytes" % (what, count,
                                                      len(samples)))
        print("  ok  survived %s" % what)
    # Whole files, through the ADTS scanner rather than a frame at a time.
    for data, what in ((b"\x00" * 4096, "a file of zeroes"),
                       (good[:len(good) // 3], "a file cut short"),
                       (good[:200] + b"\x5a" * 400 + good[600:],
                        "a file corrupted in the middle")):
        try:
            aac.Decoder(aac.asc_from_adts(data)).decode_adts(data)
        except aac.AacError:
            pass
        print("  ok  survived %s" % what)


def _asc(object_type, rate_index=4, channels=1):
    """An AudioSpecificConfig by hand: 5 bits of object type, 4 of sampling
    frequency index, 4 of channel configuration."""
    value = (object_type << 11) | (rate_index << 7) | (channels << 3)
    return bytes([(value >> 8) & 0xFF, value & 0xFF])


def test_what_we_do_not_implement_is_refused_by_name():
    """The tools that are out of scope, each with its own answer.

    A decoder that quietly ignores SBR produces half-bandwidth audio that
    sounds like a bad connection; one that ignores Parametric Stereo
    produces mono. Both are worse than a refusal, and a refusal is only
    useful if it names the thing, so each of these is checked for the words
    a person would search for.
    """
    if _skip():
        return
    cases = [
        (_asc(1), "Main profile", "Main profile prediction"),
        (_asc(3), "Scalable Sample Rate", "SSR"),
        (_asc(4), "Long Term Prediction", "LTP"),
        (_asc(5), "Spectral Band Replication", "HE-AAC's SBR"),
        (_asc(29), "Parametric Stereo", "HE-AAC v2's PS"),
        (_asc(23), "not AAC-LC", "AAC-LD"),
        (_asc(2, 4, 6), "mono and stereo", "5.1"),
        (_asc(2, 4, 7), "mono and stereo", "7.1"),
        (_asc(2, 13, 1), "reserved sampling frequency", "a reserved rate"),
        (_asc(2, 15, 1), "explicitly coded sampling frequency",
         "an explicit rate"),
        (b"", "no AudioSpecificConfig", "an empty config"),
        (b"\x12", "ends in the middle", "a one-byte config"),
    ]
    for asc, wanted, what in cases:
        reason = aac.probe(asc)
        assert reason is not None, "%s was accepted" % what
        assert wanted in reason, "%s: unhelpful refusal %r" % (what, reason)
        try:
            aac.Decoder(asc)
        except aac.AacError as exc:
            assert wanted in str(exc), "%s: %s" % (what, exc)
        else:
            raise AssertionError("built a decoder for %s" % what)
        print("  ok  refused %-16s %s" % (what + ":", reason))
    # And the one that is a decision rather than a limit: 960-sample frames,
    # which are frameLengthFlag in the GASpecificConfig -- the fourteenth
    # bit of the config, straight after the thirteen that name the object
    # type, the sampling frequency and the channels.
    short_frames = bytes([0x12, 0x14])
    reason = aac.probe(short_frames)
    assert reason and "960" in reason, reason
    print("  ok  refused 960-frames:  %s" % reason)


def test_a_stereo_stream_really_uses_the_stereo_tools():
    """A guard on the fixtures, not on the decoder.

    `stereo` is here because the encoder chose mid/side and intensity for
    it. If a regenerated fixture stopped using them the vector would still
    pass every test above while proving nothing, and the failure would be
    invisible. So the tools are counted, and the count has to be real.
    """
    if _skip():
        return
    blob = _stream("stereo")
    decoder = aac.Decoder(aac.asc_from_adts(blob))
    rest = blob
    mid_side = intensity = noise = short = 0
    for head, length in aac.adts_frames(blob):
        decoder.decode(rest[head:length])
        rest = rest[length:]
        for channel in (1, 2):
            shape = decoder.shape(channel)
            mid_side += bool(shape["ms_mask_present"])
            short += shape["window_sequence"] == 2
            books, _factors, _offsets = decoder.bands(channel)
            intensity += any(b in (14, 15) for b in books)
            noise += any(b == 13 for b in books)
    assert mid_side >= 20, "mid/side in only %d channel-frames" % mid_side
    assert intensity >= 5, "intensity stereo in only %d" % intensity
    assert noise >= 10, "noise substitution in only %d" % noise
    assert short >= 2, "no short windows in the stereo vector"
    print("  ok  stereo vector: M/S %d, intensity %d, PNS %d, short %d "
          "channel-frames" % (mid_side, intensity, noise, short))
    # And the transient vector really transitions, which is what makes it
    # worth its bytes: LONG_START, EIGHT_SHORT and LONG_STOP all appear.
    blob = _stream("transient")
    decoder = aac.Decoder(aac.asc_from_adts(blob))
    rest = blob
    seen = set()
    grouped = 0
    for head, length in aac.adts_frames(blob):
        decoder.decode(rest[head:length])
        rest = rest[length:]
        shape = decoder.shape(1)
        seen.add(shape["window_sequence"])
        grouped += shape["groups"] > 1
    assert seen == {0, 1, 2, 3}, "window sequences seen: %s" % sorted(seen)
    assert grouped >= 2, "no window grouping anywhere in the transient vector"
    print("  ok  transient vector: all four window sequences, %d frames "
          "grouped" % grouped)


def test_the_tools_and_the_layouts_are_all_actually_reached():
    """The other guard on the fixtures, and the reason two of them exist.

    A tool nothing exercises is a tool that is not tested, and the tolerance
    above cannot tell the difference: deleting the call to the TNS filter
    outright once changed not one sample of any vector here, because FFmpeg's
    own encoder set tns_data_present twice in 203 channel-frames and signalled
    no filters both times. The `tns` vector comes from a different encoder and
    puts a real filter in a fifth of its frames; without the filter it decodes
    28 dB wrong. Likewise the eight scalefactor band layouts: four of them had
    no vector at all, so a transposed table would have waited for a user.

    This asserts both are still true of whatever the fixtures currently are.
    """
    if _skip():
        return
    blob = _stream("tns")
    decoder = aac.Decoder(aac.asc_from_adts(blob))
    rest = blob
    filtered = 0
    for head, length in aac.adts_frames(blob):
        decoder.decode(rest[head:length])
        rest = rest[length:]
        filtered += bool(decoder.shape(1)["tns"])
    assert filtered >= 3, "TNS in only %d frames of the tns vector" % filtered
    print("  ok  tns vector: temporal noise shaping in %d of its frames"
          % filtered)

    rates = set(rate for _name, rate, _ch, _frames, _what in VECTORS)
    missing = [group for group in LAYOUTS if not rates & set(group)]
    assert not missing, ("no vector for the band layout(s) at %s Hz"
                         % ", ".join("/".join(str(r) for r in g)
                                     for g in missing))
    # And the layouts really are different tables, not the same one read
    # eight times: every group's vector reports its own band count.
    counts = {}
    for name, rate, _ch, _frames, _what in VECTORS:
        blob = _stream(name)
        decoder = aac.Decoder(aac.asc_from_adts(blob))
        head, length = next(iter(aac.adts_frames(blob)))
        decoder.decode(blob[head:length])
        counts.setdefault(decoder.shape(1)["num_swb"], []).append(rate)
    assert len(counts) >= 6, "only %d distinct band counts: %s" % (
        len(counts), counts)
    print("  ok  all %d band layouts covered, %d distinct long-block band "
          "counts: %s" % (len(LAYOUTS), len(counts),
                          ", ".join(str(c) for c in sorted(counts))))


def test_the_decoder_is_fast_enough_to_be_worth_having():
    """Not a benchmark, a floor.

    The IMDCT is the whole cost of an AAC decoder and it is written as an
    FFT for that reason; the O(N^2) definition is 1024 times the work and is
    only there for the test above. What this asserts is that a stereo frame
    decodes in far less than the 23 milliseconds it plays for, because the
    thing that will consume this is a mixer on a deadline. It measures
    decode only -- no file reading, no comparison, no allocation of the
    reference -- and prints the multiple of realtime it got.
    """
    if _skip():
        return
    blob = _stream("stereo")
    bounds = list(aac.adts_frames(blob))
    packets = []
    rest = blob
    for head, length in bounds:
        packets.append(rest[head:length])
        rest = rest[length:]
    decoder = aac.Decoder(aac.asc_from_adts(blob))
    best = None
    for _ in range(3):
        decoder.reset()
        start = time.perf_counter()
        for packet in packets:
            decoder.decode(packet)
        taken = time.perf_counter() - start
        best = taken if best is None else min(best, taken)
    played = len(packets) * 1024 / 44100.0
    ratio = played / best
    assert ratio >= 20.0, "only %.1fx realtime" % ratio
    print("  ok  %.2f s of 44.1 kHz stereo in %.1f ms: %.0fx realtime"
          % (played, best * 1000.0, ratio))


def test_a_machine_without_gfortran_still_has_a_browser():
    """The degradation path, forced. Nothing here may raise: an absent
    toolchain has to look like an unsupported codec, not like a crash."""
    saved = (aac._loaded, aac._lib, aac._load_error)
    try:
        aac._loaded = True
        aac._lib = None
        aac._load_error = "no gfortran on PATH"
        assert not aac.available()
        assert aac.unavailable_reason() == "no gfortran on PATH"
        assert aac.probe(b"\x12\x10") == "no gfortran on PATH"
        try:
            aac.Decoder(b"\x12\x10")
        except aac.AacError as exc:
            assert "gfortran" in str(exc), exc
        else:
            raise AssertionError("built a decoder with no library")
        with open(os.path.join(FIXTURES, "stereo.mp4"), "rb") as handle:
            data = handle.read()
        info = mediacodec.probe_audio(data)
        assert info.codec == "mp4a" and not info.supported, info
        assert (info.sample_rate, info.channels) == (44100, 2), info
        assert "gfortran" in info.reason, info.reason
        try:
            mediacodec.open_audio(data)
        except mediacodec.MediaError:
            pass
        else:
            raise AssertionError("opened an AAC file with no decoder")
        print("  ok  no toolchain: probed, refused, said why")
    finally:
        aac._loaded, aac._lib, aac._load_error = saved


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
    print(f"\nALL {len(tests)} AAC TESTS PASSED")


if __name__ == "__main__":
    main()
