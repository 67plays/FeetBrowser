"""Tests for the audio output stack.

Two halves, and the split is deliberate. The ring, the filter design, the
resampler, the mixer and the clock are pure functions over plain numbers, so
they are tested first and everywhere, including in a container with no
``/dev/snd`` and on a CI runner with no sound card at all. That is nearly the
whole subsystem, and none of it needs hardware: a null device that consumes
from the ring exactly the way a real one does carries the end-to-end tests,
right down to reading the mixed bytes back and measuring the tone in them.

The second half needs a speaker. It opens whatever this platform's real
backend is, plays two hundred milliseconds of a very quiet tone, and checks
that the device consumed the frames it should have in the time it should have
taken. Nothing is stubbed. Where there is no device to talk to, that half says
so and the first half still runs.

What no test here can prove is that a noise came out of the speaker; that is
what ears are for. What it can prove is that the driver was handed the right
samples at the right rate, which is the half that goes wrong in software.
"""
import ctypes
import math
import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import alsa, coreaudio, heel, winmm


def eq(a, b, msg=""):
    assert a == b, "%s: %r != %r" % (msg, a, b)


def close(a, b, tolerance, msg=""):
    assert abs(a - b) <= tolerance, "%s: %r != %r (+-%g)" % (msg, a, b,
                                                             tolerance)


# -- the ring --------------------------------------------------------------

def test_an_empty_ring_is_all_space_and_no_backlog():
    ring = heel.Ring(4, 8)
    eq(ring.capacity, 8)
    eq(ring.backlog, 0)
    eq(ring.space, 8, "an empty ring has room for everything")
    eq(ring.read(16), b"", "reading an empty ring is not an error")
    eq(ring.written_frames, 0)
    eq(ring.read_frames, 0)


def test_bytes_come_back_in_the_order_they_went_in():
    ring = heel.Ring(2, 16)
    eq(ring.write(b"abcdef"), 6)
    eq(ring.backlog, 3, "three two-byte frames")
    eq(ring.read(6), b"abcdef")
    eq(ring.backlog, 0)
    eq(ring.read_frames, 3)
    eq(ring.written_frames, 3)


def test_a_ring_never_hands_out_a_partial_frame():
    """A partial frame is a channel swap that lasts until the next seek, and
    it is silent in every sense: nothing raises, the speakers just move to
    the wrong side of the room. The producer counts in bytes -- a caller with
    three bytes of a frame has to be able to put them somewhere -- and every
    way out of the ring rounds down to a frame."""
    ring = heel.Ring(4, 4)
    eq(ring.write(b"0123456"), 7, "the producer's half is bytes")
    eq(ring.backlog, 1, "and only whole frames are visible")
    eq(ring.read(7), b"0123", "the spare three stay in")
    eq(ring.write(b"789"), 3, "and are completed by the next write")
    eq(ring.read(8), b"4567", "then come out in order, a frame at a time")
    eq(ring.backlog, 0, "with two bytes of the next frame still waiting")
    ring.clear()
    ring.write(b"abcdefgh")
    eq(ring.discard(6), 4, "discarding rounds down too")


def test_a_clipped_write_is_clipped_to_a_frame_boundary():
    """The producer may write bytes, but the ring may not *drop* a partial
    frame at the end of a short write -- that is the channel swap, arriving
    the moment the machine gets busy."""
    ring = heel.Ring(4, 2)
    ring.write(b"0123")
    eq(ring.write(b"456789"), 4, "one frame of room, one frame written")
    eq(ring.backlog, 2)


def test_writing_more_than_fits_takes_what_fits_and_says_so():
    """Overrun. The producer is ahead, which is the good problem, and the
    answer is to write less rather than to grow the buffer or to block."""
    ring = heel.Ring(2, 4)
    eq(ring.write(b"a" * 20), 8, "a four-frame ring holds eight bytes")
    eq(ring.space, 0)
    eq(ring.write(b"zz"), 0, "a full ring takes nothing")
    eq(ring.backlog, 4)


def test_reading_more_than_there_is_takes_what_there_is():
    """Underrun. The consumer is ahead, which is the bad problem, and the
    answer is still to do less rather than to wait -- see rule 2."""
    ring = heel.Ring(2, 8)
    ring.write(b"abcd")
    eq(ring.read(100), b"abcd")
    eq(ring.read(100), b"", "and then nothing at all")


def test_data_that_straddles_the_end_of_the_buffer_comes_back_whole():
    """Wraparound, which is the only interesting thing a ring does. Write
    past the end, read across the seam, and check every byte."""
    ring = heel.Ring(1, 8)
    ring.write(b"12345")
    eq(ring.read(5), b"12345")
    eq(ring.write(b"abcdefgh"), 8, "the read freed the whole ring")
    eq(ring.read(8), b"abcdefgh", "three bytes at the end, five at the start")
    eq(ring.written_frames, 13, "counters do not wrap with the buffer")


def test_read_into_wraps_the_same_way_write_does():
    """The realtime path. Same seam, same answer, into raw memory."""
    ring = heel.Ring(2, 8)
    ring.write(b"xy" * 6)
    ring.read(10)                       # leave the cursor near the end
    ring.write(b"ABCDEFGHIJ")
    target = ctypes.create_string_buffer(16)
    address = ctypes.addressof(target)
    eq(ring.read_into(address, 2), 2, "two bytes of the tail first")
    eq(ring.read_into(address + 2, 100), 10, "then everything left")
    eq(target.raw[:12], b"xyABCDEFGHIJ")


def test_read_into_will_not_move_a_partial_frame_either():
    ring = heel.Ring(4, 4)
    ring.write(b"wxyz")
    target = ctypes.create_string_buffer(8)
    eq(ring.read_into(ctypes.addressof(target), 3), 0, "3 < one frame")
    eq(ring.backlog, 1, "and it is still there")


def test_clear_throws_away_the_backlog_without_moving_the_counters_back():
    ring = heel.Ring(2, 8)
    ring.write(b"abcdef")
    ring.clear()
    eq(ring.backlog, 0)
    eq(ring.read(2), b"")
    eq(ring.written_frames, 3, "a seek does not un-write history")
    eq(ring.read_frames, 3, "the consumer is credited with the drop")


def test_a_producer_and_a_consumer_can_run_at_once():
    """The property that matters, exercised the only way it can be: two real
    threads, no lock, and every byte of a known sequence checked on the way
    out. A torn counter shows up here as a byte in the wrong place."""
    ring = heel.Ring(4, 64)
    total = 4000                                # frames
    payload = b"".join(struct.pack("<I", n) for n in range(total))
    written = []
    def produce():
        at = 0
        while at < len(payload):
            took = ring.write(payload[at:at + 404])
            at += took
            if not took:
                time.sleep(0)
        written.append(at)

    got = bytearray()
    producer = threading.Thread(target=produce)
    producer.start()
    deadline = time.monotonic() + 30.0
    while len(got) < len(payload) and time.monotonic() < deadline:
        chunk = ring.read(97)               # not a multiple of the frame size
        if chunk:
            got.extend(chunk)
        else:
            time.sleep(0)
    producer.join(timeout=5.0)
    eq(written, [len(payload)], "the producer did not finish")
    eq(bytes(got), payload, "the stream came out reordered or torn")
    eq(ring.backlog, 0)


def test_a_ring_needs_a_positive_size():
    for args in ((0, 8), (4, 0), (-1, 4)):
        try:
            heel.Ring(*args)
        except ValueError:
            continue
        raise AssertionError("Ring%r should not exist" % (args,))


# -- filter design ---------------------------------------------------------

def test_the_bessel_function_matches_its_published_values():
    """I0 is the window's whole shape, so a wrong series is a wrong filter
    that still looks like a filter. These are the standard table values."""
    close(heel.bessel_i0(0.0), 1.0, 1e-12)
    close(heel.bessel_i0(1.0), 1.2660658778, 1e-9)
    close(heel.bessel_i0(2.0), 2.2795853024, 1e-9)
    close(heel.bessel_i0(5.0), 27.2398718236, 1e-7)
    close(heel.bessel_i0(9.0), 1093.5883841, 1e-4)


def test_the_kaiser_window_is_symmetric_and_tapers():
    window = heel.kaiser_window(65, 9.0)
    eq(len(window), 65)
    close(window[32], 1.0, 1e-12, "the middle is unity")
    for i in range(33):
        close(window[i], window[64 - i], 1e-12, "asymmetric at %d" % i)
    assert window[0] < 1e-3, "the ends should be nearly closed: %g" % window[0]
    assert all(window[i] <= window[i + 1] for i in range(32)), "not monotonic"


def test_sinc_is_one_at_zero_and_nothing_at_every_other_integer():
    close(heel.sinc(0.0), 1.0, 1e-15)
    for n in range(1, 12):
        close(heel.sinc(float(n)), 0.0, 1e-15, "sinc(%d)" % n)
        close(heel.sinc(float(-n)), 0.0, 1e-15, "sinc(-%d)" % n)
    close(heel.sinc(0.5), 2 / math.pi, 1e-12)


def test_a_lowpass_is_symmetric_and_passes_dc():
    taps = heel.lowpass(64, 0.25, 9.0)
    eq(len(taps), 64)
    for i in range(32):
        close(taps[i], taps[63 - i], 1e-15, "asymmetric at %d" % i)
    # The sum is the gain at DC, and a low-pass passes DC, so it is one --
    # whatever the cutoff is. A design that gets this wrong by a factor of
    # two is a design that is six decibels quiet, which sounds like nothing
    # at all until it is next to something that is not.
    close(math.fsum(taps), 1.0, 1e-3, "DC gain drifted")
    close(math.fsum(heel.lowpass(64, 0.1, 9.0)), 1.0, 1e-3,
          "and it does not depend on the cutoff")


def test_a_rational_ratio_is_found_or_approximated():
    """Every ordinary rate pair reduces and comes back untouched. The ones
    that do not get the nearest convergent that fits, and getting the
    continued fraction's seeds backwards returns the *reciprocal* -- which is
    a tape running slow rather than an error."""
    eq(heel.best_ratio(160, 147, 1024), (160, 147), "48/44.1 fits exactly")
    eq(heel.best_ratio(2, 1, 1024), (2, 1))
    for numerator, denominator in ((96001, 44101), (44101, 96001),
                                   (48000, 44101), (100003, 99991)):
        up, down = heel.best_ratio(numerator, denominator, 64)
        assert up <= 64, "the limit was ignored: %d" % up
        assert down > 0, "a ratio over nothing"
        wanted = numerator / denominator
        close(up / down, wanted, wanted * 2e-3,
              "%d/%d approximated poorly" % (numerator, denominator))


def test_the_ratio_is_the_best_one_the_phase_budget_allows():
    """Not merely close: the closest that fits under the limit.

    Stopping at the last whole convergent leaves accuracy on the table,
    because a semiconvergent below the one that overshot can still fit and
    still be nearer. Checked against Fraction.limit_denominator, which is the
    standard best-rational-approximation and is applied to d/n here because
    it bounds the denominator where best_ratio bounds the numerator.

    Every rate pair a browser can meet, both directions, no exceptions: the
    case that used to fail is 11025 Hz into 32 kHz, which took 119/41 where
    923/318 fits and is two and a half times nearer.
    """
    from fractions import Fraction
    rates = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000,
             64000, 88200, 96000, 176400, 192000)
    worst = 0.0
    for source in rates:
        for target in rates:
            if source == target:
                continue
            divisor = math.gcd(target, source)
            n, d = target // divisor, source // divisor
            up, down = heel.best_ratio(n, d, heel.MAX_PHASES)
            assert 1 <= up <= heel.MAX_PHASES, \
                "%d->%d gave %d phases" % (source, target, up)
            assert down >= 1, "%d->%d gave a zero step" % (source, target)
            wanted = n / d
            error = abs(up / down - wanted) / wanted
            worst = max(worst, error)
            ideal = Fraction(d, n).limit_denominator(heel.MAX_PHASES)
            best = abs(ideal.denominator / ideal.numerator - wanted) / wanted
            assert error <= best * 1.0001 + 1e-15, (
                "%d -> %d took %d/%d (off by %.3g) when %d/%d fits and is "
                "off by only %.3g" % (source, target, up, down, error,
                                      ideal.denominator, ideal.numerator,
                                      best))
    assert worst < 1e-5, "worst approximation over all rate pairs: %.3g" % worst


def test_an_unknown_backend_name_is_refused_rather_than_swapped():
    """``backend="alsa"`` on a Mac used to return CoreAudio without a word.

    Nothing was broken by it at runtime, but it is precisely how a test comes
    to believe it has exercised a backend that was never even imported, so
    the argument now only answers to the one name it actually implements.
    """
    for name in ("alsa", "winmm", "coreaudio", "pulse", ""):
        try:
            output = heel.open_output(backend=name, threaded=False)
        except ValueError as exc:
            assert name in str(exc) or not name, \
                "the refusal should name what was asked for: %s" % exc
        else:
            output.close()
            raise AssertionError("backend=%r was silently accepted" % name)


def test_every_polyphase_branch_has_exactly_unity_gain():
    """The one property that makes the filter usable. A branch whose taps sum
    to something other than one is a branch that plays that phase of the
    signal at a different volume -- which is a buzz at the difference
    frequency, and the classic way a hand-written resampler sounds wrong."""
    for up, down in ((160, 147), (147, 160), (2, 1), (1, 3)):
        table = heel.phase_table(up, down, taps=32)
        eq(len(table), up, "one branch per output phase")
        for index, branch in enumerate(table):
            eq(len(branch), 32, "branch %d is the wrong length" % index)
            close(math.fsum(branch), 1.0, 1e-12,
                  "branch %d of %d/%d" % (index, up, down))


def test_phase_tables_are_shared_rather_than_rebuilt():
    """Designing a 160-branch 64-tap filter is ten thousand sincs, and a
    browser opens a source per video. Twice is once too many."""
    first = heel.cached_phase_table(160, 147, 32, 9.0)
    second = heel.cached_phase_table(160, 147, 32, 9.0)
    assert first is second, "the table cache missed"


# -- the resampler ---------------------------------------------------------

def test_equal_rates_are_a_passthrough_and_not_a_filter():
    resampler = heel.Resampler(48000, 48000)
    assert resampler.passthrough
    eq(resampler.delay, 0.0)
    samples = [0.1, -0.2, 0.3, -0.4]
    eq(resampler.process(samples), samples, "a no-op filtered the signal")


def test_the_output_is_as_long_as_the_ratio_says():
    resampler = heel.Resampler(44100, 48000)
    out = resampler.process([0.0] * 44100)
    close(len(out), 48000, 64, "a second in, a second out")
    more = resampler.process([0.0] * 44100)
    close(len(out) + len(more), 96000, 64, "and it stays lined up")


def test_dc_survives_a_rate_change():
    """Constant in, constant out. This is what per-branch normalisation buys,
    and if it is wrong the error is a hum, not a wrong number."""
    resampler = heel.Resampler(44100, 48000)
    out = resampler.process([1.0] * 20000)
    settled = out[200:-200]
    close(min(settled), 1.0, 1e-9, "DC sagged")
    close(max(settled), 1.0, 1e-9, "DC overshot")


def test_a_stream_resamples_the_same_as_one_big_call():
    """The property a decoder depends on: packets arrive in whatever size
    they arrive in, and the sound must not depend on that."""
    signal = [math.sin(2 * math.pi * 997 * i / 44100) for i in range(9000)]
    whole = heel.Resampler(44100, 48000).process(signal)
    chunked = heel.Resampler(44100, 48000)
    pieces = []
    at = 0
    for size in (1, 2, 3, 512, 4000, 100, 4382):
        pieces.extend(chunked.process(signal[at:at + size]))
        at += size
    eq(at, len(signal), "the test data was not all fed in")
    eq(len(pieces), len(whole), "chunking changed the frame count")
    for i, (a, b) in enumerate(zip(whole, pieces)):
        close(a, b, 1e-12, "sample %d diverged" % i)


def test_channels_do_not_leak_into_each_other():
    """Stereo is two independent filters over one shared phase. Sharing the
    phase is required; sharing anything else is a stereo image that rotates."""
    frames = 4000
    left = [math.sin(2 * math.pi * 1000 * i / 44100) for i in range(frames)]
    interleaved = []
    for value in left:
        interleaved.extend((value, 0.0))
    out = heel.Resampler(44100, 48000, 2).process(interleaved)
    eq(len(out) % 2, 0, "an odd number of samples in a stereo stream")
    silent = out[1::2]
    assert max(abs(v) for v in silent) < 1e-15, \
        "the right channel picked up %g of the left" % max(map(abs, silent))
    assert max(abs(v) for v in out[0::2]) > 0.9, "the left channel vanished"


def test_a_reset_forgets_the_tail():
    resampler = heel.Resampler(44100, 48000)
    resampler.process([1.0] * 5000)
    resampler.reset()
    eq(resampler.pending(), 0, "history survived the seek")
    out = resampler.process([0.0] * 200)
    assert all(v == 0.0 for v in out), "the old signal bled through the seek"


def _measure(in_rate, out_rate, cycles, length, taps=heel.TAPS):
    """SNR and worst spur of a resampled sine, in dB.

    The frequency is chosen so that exactly ``cycles`` periods fit in
    ``length`` output samples, which puts the fundamental on a DFT bin
    exactly. That matters: a tone between two bins leaks into every other
    bin, and measuring the leak gives you a number in the sixties no matter
    how good the filter is.
    """
    freq = cycles * out_rate / length
    count = int(in_rate * (length / out_rate * 1.6 + 0.05))
    signal = [math.sin(2 * math.pi * freq * i / in_rate) for i in range(count)]
    resampler = heel.Resampler(in_rate, out_rate, 1, taps=taps)
    out = resampler.process(signal)
    start = int(resampler.delay) + taps * 4
    segment = out[start:start + length]
    assert len(segment) == length, "not enough output to measure"
    step = 2 * math.pi * cycles / length
    re = math.fsum(v * math.cos(step * i) for i, v in enumerate(segment))
    im = math.fsum(v * math.sin(step * i) for i, v in enumerate(segment))
    a, b = 2.0 * re / length, 2.0 * im / length
    residual = [v - (a * math.cos(step * i) + b * math.sin(step * i))
                for i, v in enumerate(segment)]
    signal_power = math.fsum(v * v for v in segment)
    noise_power = math.fsum(v * v for v in residual)
    snr = 10 * math.log10(signal_power / noise_power)
    return freq, snr, math.hypot(a, b)


def test_the_resampler_is_clean_across_the_audio_band():
    """The measurement the whole design exists for. Sixty-four taps of Kaiser
    window at beta 9 hold a hundred decibels of signal-to-noise everywhere a
    human can hear, including up at nineteen kilohertz where a short filter
    falls apart -- see the test below for what that looks like."""
    for cycles, floor in ((8, 100.0), (426, 100.0), (853, 100.0),
                          (1280, 100.0), (1621, 95.0)):
        freq, snr, amp = _measure(44100, 48000, cycles, 4096)
        assert snr > floor, "44.1->48 at %.0f Hz: only %.2f dB" % (freq, snr)
        close(20 * math.log10(amp), 0.0, 0.01,
              "44.1->48 at %.0f Hz changed the level" % freq)
    for cycles, floor in ((85, 100.0), (853, 100.0), (1621, 95.0)):
        freq, snr, amp = _measure(48000, 44100, cycles, 4096)
        assert snr > floor, "48->44.1 at %.0f Hz: only %.2f dB" % (freq, snr)
        close(20 * math.log10(amp), 0.0, 0.01,
              "48->44.1 at %.0f Hz changed the level" % freq)


def test_a_short_filter_is_measurably_worse_than_the_one_we_ship():
    """Proof that the default was chosen rather than guessed. Sixteen taps is
    what a resampler written in an afternoon has, and at nineteen kilohertz
    it is thirty decibels of aliasing and a whole decibel of level error."""
    _, long_snr, long_amp = _measure(44100, 48000, 1621, 4096, taps=64)
    _, short_snr, short_amp = _measure(44100, 48000, 1621, 4096, taps=16)
    assert short_snr < 40.0, "16 taps should be poor, got %.2f dB" % short_snr
    assert long_snr > short_snr + 50.0, \
        "64 taps bought only %.1f dB" % (long_snr - short_snr)
    assert abs(20 * math.log10(short_amp)) > 0.5, "16 taps kept the level?"
    close(20 * math.log10(long_amp), 0.0, 0.01, "64 taps lost the level")


def test_a_rate_pair_with_no_small_ratio_still_works():
    """44100 to 48000 is 147:160 and lands exactly. Two rates that do not are
    approximated, and the approximation has to stay in tune."""
    resampler = heel.Resampler(44101, 48000)
    assert resampler.up <= heel.MAX_PHASES
    out = resampler.process([1.0] * 10000)
    close(len(out), 10000 * 48000 / 44101, 32, "the ratio drifted")
    close(min(out[200:-200]), 1.0, 1e-6, "DC sagged on an approximate ratio")


def test_a_resampler_needs_real_rates():
    for args in ((0, 48000), (48000, -1), (48000, 48000, 0)):
        try:
            heel.Resampler(*args)
        except ValueError:
            continue
        raise AssertionError("Resampler%r should not exist" % (args,))


# -- sample formats --------------------------------------------------------

def test_sixteen_bit_pcm_round_trips_exactly():
    values = [0.0, 0.5, -0.5, 0.25, -1.0]
    raw = heel.pack(values, heel.INT16)
    eq(len(raw), len(values) * 2)
    back = heel.floats_from_int16(raw)
    for a, b in zip(values, back):
        close(a, b, 1e-9, "int16 round trip")


def test_floats_round_trip_exactly():
    values = [0.0, 0.5, -0.5, 1.0, -1.0]
    back = heel.floats_from_float32(heel.pack(values, heel.FLOAT32))
    eq(back, values, "float32 is not lossless for these")


def test_packing_clamps_rather_than_wrapping():
    """The difference between a mix that is a bit loud and a mix that is a
    burst of white noise: signed wraparound turns +1.2 into a hard negative
    spike, which is the loudest sound a 16-bit file can contain."""
    raw = heel.pack([2.0, -2.0, 1.0, -1.0], heel.INT16)
    eq(struct.unpack("<4h", raw), (32767, -32768, 32767, -32768))
    floats = heel.floats_from_float32(heel.pack([9.0, -9.0], heel.FLOAT32))
    eq(floats, [1.0, -1.0])


def test_an_unknown_format_is_refused_rather_than_guessed():
    try:
        heel.pack([0.0], "u8")
    except ValueError:
        return
    raise AssertionError("packing accepted a format it does not have")


def test_silence_is_the_right_number_of_zero_bytes():
    eq(heel.silence(4, 2, heel.INT16), b"\0" * 16)
    eq(heel.silence(4, 2, heel.FLOAT32), b"\0" * 32)


def test_mono_becomes_stereo_by_duplication_and_back_by_averaging():
    eq(heel.remap_channels([0.5, -0.25], 1, 2), [0.5, 0.5, -0.25, -0.25])
    eq(heel.remap_channels([1.0, 0.0, 0.0, 0.5], 2, 1), [0.5, 0.25])
    same = [0.1, 0.2, 0.3, 0.4]
    eq(heel.remap_channels(same, 2, 2), same, "a no-op copied wrong")


def test_a_missing_channel_is_silent_rather_than_missing():
    """Three channels into four leaves one with nothing in it. Anything other
    than zero there is either a crash or somebody else's audio."""
    out = heel.remap_channels([1.0, 2.0, 3.0], 3, 4)
    eq(out, [1.0, 2.0, 3.0, 0.0])


# -- the clock -------------------------------------------------------------

def test_the_clock_counts_frames_and_reports_seconds():
    clock = heel.AudioClock(48000)
    eq(clock.now(), 0.0)
    clock.frames += 24000
    close(clock.now(), 0.5, 1e-12)
    eq(clock.seconds(), clock.now(), "two names, one number")
    clock.frames += 24000
    close(clock.now(), 1.0, 1e-12)


def test_the_clock_counts_invented_silence_separately():
    """``frames`` is time as the device measures it and includes the silence
    we made up; ``silent_frames`` is how much of it was made up. Confusing
    the two makes a struggling machine look like it is keeping perfect
    time."""
    clock = heel.AudioClock(48000)
    clock.frames += 1024
    clock.silent_frames += 256
    clock.underruns += 1
    close(clock.now(), 1024 / 48000, 1e-12, "silence still took real time")
    eq(clock.silent_frames, 256)
    clock.reset()
    eq((clock.frames, clock.silent_frames, clock.underruns), (0, 0, 0))


def test_the_clock_is_what_a_media_scheduler_wants():
    """Duck-compatible with media.Clock on purpose, so that A/V sync can be
    handed one with no adapter at all."""
    from feetbrowser import media
    clock = heel.AudioClock(48000)
    scheduler = media.Scheduler(10.0, 25.0, clock=clock)
    assert scheduler.clock is clock, "media took a clock and kept another"
    scheduler.play()
    clock.frames += 48000
    close(scheduler.position(), 1.0, 1e-9,
          "a second of audio should be a second of video")


# -- the mixer -------------------------------------------------------------

class FakeSource:
    """Just enough of a Source for the mixer: a gain and a queue."""

    def __init__(self, samples, gain=1.0):
        self.samples = list(samples)
        self.gain = gain

    def _take(self, frames):
        chunk = self.samples[:frames * 2]
        del self.samples[:frames * 2]
        return chunk


def _mixer(*sources):
    mixer = heel.Mixer(48000, 2, heel.FLOAT32)
    for source in sources:
        mixer.add(source)
    return mixer


def test_a_silent_mixer_renders_exactly_the_frames_asked_for():
    eq(_mixer().render_floats(3), [0.0] * 6)
    eq(_mixer().render(3), b"\0" * 24, "silence in float32 is zero bytes")


def test_one_source_passes_through_at_gain_one():
    samples = [0.1, 0.2, 0.3, 0.4]
    eq(_mixer(FakeSource(samples)).render_floats(2), samples)


def test_per_source_gain_is_applied_once():
    out = _mixer(FakeSource([1.0, 1.0], gain=0.25)).render_floats(1)
    eq(out, [0.25, 0.25])


def test_sources_are_summed():
    mixer = _mixer(FakeSource([0.25, 0.25]), FakeSource([0.5, -0.5]))
    out = mixer.render_floats(1)
    close(out[0], 0.75, 1e-12)
    close(out[1], -0.25, 1e-12)


def test_a_short_source_is_padded_rather_than_truncating_the_mix():
    """One stream ending must not cut the other off, and must not shorten the
    buffer the device is about to be handed either."""
    mixer = _mixer(FakeSource([1.0, 1.0]), FakeSource([0.5] * 8))
    out = mixer.render_floats(4)
    eq(len(out), 8, "the mix came out short")
    close(out[0], 1.5, 1e-12, "the frame both sources had")
    close(out[2], 0.5, 1e-12, "the frames only one did")


def test_master_volume_scales_everything_and_gain_still_applies():
    mixer = _mixer(FakeSource([1.0, 1.0], gain=0.5),
                   FakeSource([1.0, 1.0], gain=1.0))
    mixer.volume = 0.5
    out = mixer.render_floats(1)
    close(out[0], 0.75, 1e-12, "0.5*0.5 + 1.0*0.5")


def test_a_loud_mix_is_clamped_once_at_the_end():
    """Two sources at full scale sum to twice full scale. Clamping per source
    would make the quiet one change the loud one's shape; clamping in
    integers at every step is how a mix acquires a crunch nobody can find."""
    mixer = _mixer(FakeSource([1.0, 1.0]), FakeSource([1.0, 1.0]))
    mixer.fmt = heel.INT16
    eq(struct.unpack("<2h", mixer.render(1)), (32767, 32767))


def test_a_removed_source_stops_being_heard():
    source = FakeSource([1.0] * 10)
    mixer = _mixer(source)
    assert any(mixer.render_floats(1))
    mixer.remove(source)
    eq(mixer.render_floats(1), [0.0, 0.0])
    mixer.remove(source)                # removing twice is not an error


# -- sources, end to end ---------------------------------------------------

class Capture(heel.NullDevice):
    """A null device that keeps what it consumed instead of dropping it.

    Implementing the whole device contract in twenty lines is the point: it
    is what lets the end-to-end tests measure the bytes a driver would have
    been handed, on a machine with no driver.
    """

    name = "capture"

    def __init__(self, rate=48000, channels=2, fmt=heel.FLOAT32):
        super().__init__(rate, channels, fmt, paced=False)
        self.taken = bytearray()

    def pump(self, frames):
        data = self._ring.read(frames * self.frame_bytes)
        self.taken.extend(data)
        got = len(data) // self.frame_bytes
        self._clock.frames += frames
        if got < frames:
            self.taken.extend(b"\0" * ((frames - got) * self.frame_bytes))
            self._clock.silent_frames += frames - got
            self._clock.underruns += 1
        return got

    def floats(self):
        return heel.floats_from_float32(bytes(self.taken))


def _drive(output, frames, block=512):
    """Mix and consume ``frames``, the way the two threads would have."""
    done = 0
    while done < frames:
        step = min(block, frames - done)
        output.pump()
        output.device.pump(step)
        done += step


def test_a_tone_written_at_one_rate_reaches_the_device_at_another():
    """The whole stack in one test: floats in at 44100, resampled to 48000,
    mono mapped to stereo, mixed, gain applied, packed, through the ring, out
    of the device -- and then measured. Anything wrong anywhere in that chain
    shows up as a frequency, an amplitude or a channel that is wrong."""
    device = Capture(48000, 2)
    output = heel.Output(device, ring_frames=2048, threaded=False)
    source = output.add_source(44100, channels=1, gain=0.5, name="tone")
    source.write(heel.tone(1.0, 1000.0, 44100, 1, amplitude=0.8),
                 fmt="float")
    output.start()
    _drive(output, 24000)
    output.close()
    values = device.floats()
    eq(len(values), 24000 * 2, "the device did not get what it asked for")
    left, right = values[0::2], values[1::2]
    eq(left, right, "mono did not reach both channels identically")
    settled = left[4000:20000]
    peak = max(abs(v) for v in settled)
    close(peak, 0.4, 0.01, "0.8 at gain 0.5 should arrive at 0.4")
    # Count the zero crossings rather than run a DFT: 1000 Hz over 16000
    # frames at 48 kHz is 333.3 cycles and so 666 or 667 crossings.
    crossings = sum(1 for a, b in zip(settled, settled[1:])
                    if (a < 0) != (b < 0))
    assert 664 <= crossings <= 669, "%d crossings is not 1000 Hz" % crossings


def test_master_volume_reaches_the_device():
    """docs/media.md has said for a while that the video controls have no
    volume because there is nothing to make quieter. This is the test that
    stops being true."""
    device = Capture(48000, 2)
    output = heel.Output(device, ring_frames=1024, threaded=False)
    output.volume = 0.25
    source = output.add_source(48000, channels=1)
    source.write(heel.tone(0.5, 1000.0, 48000, 1, amplitude=1.0), fmt="float")
    output.start()
    _drive(output, 8000)
    output.close()
    peak = max(abs(v) for v in device.floats()[2000:])
    close(peak, 0.25, 0.01, "the master volume did not arrive")


def test_the_master_volume_cannot_be_set_out_of_range():
    output = heel.Output(Capture(), threaded=False)
    output.volume = 5.0
    eq(output.volume, 1.0)
    output.volume = -2.0
    eq(output.volume, 0.0)
    output.volume = 0.3
    close(output.volume, 0.3, 1e-9)
    output.close()


def test_two_sources_play_at_once_and_can_be_removed_separately():
    device = Capture(48000, 2)
    output = heel.Output(device, ring_frames=1024, threaded=False)
    quiet = output.add_source(48000, channels=1, gain=0.25, name="quiet")
    loud = output.add_source(48000, channels=1, gain=0.5, name="loud")
    eq(len(output.sources), 2)
    quiet.write([1.0] * 8000, fmt="float")
    loud.write([1.0] * 8000, fmt="float")
    output.start()
    _drive(output, 2000)
    close(device.floats()[-1], 0.75, 1e-6, "both sources should be audible")
    output.remove_source(loud)
    eq(len(output.sources), 1)
    _drive(output, 2000)
    close(device.floats()[-1], 0.25, 1e-6, "removing one silenced the wrong")
    output.close()


def test_an_underrun_is_counted_and_filled_with_silence():
    """Nothing to play is not an error. It is a statistic and a quiet
    moment -- rule 2, tested from the outside."""
    device = Capture(48000, 2)
    output = heel.Output(device, ring_frames=512, threaded=False)
    source = output.add_source(48000, channels=1)
    source.write([0.5] * 100, fmt="float")
    output.start()
    device.pump(4000)
    output.close()
    assert output.clock.underruns >= 1, "an empty ring went unnoticed"
    assert output.clock.silent_frames >= 3000, \
        "only %d silent frames" % output.clock.silent_frames
    eq(output.clock.frames, 4000, "the clock stopped when the sound did")
    assert all(v == 0.0 for v in device.floats()[1000:]), "silence was noisy"


def test_a_source_written_to_faster_than_it_plays_drops_the_excess():
    """A two-hour podcast decoded in one go is two hours of float samples on
    the heap. The queue has a ceiling and says how much it threw away."""
    output = heel.Output(Capture(), threaded=False)
    source = output.add_source(48000, channels=1, max_queue=0.1)
    source.write([0.0] * 48000, fmt="float")
    close(source.queued_seconds(), 0.1, 0.01, "the ceiling did not hold")
    assert source.dropped > 40000, "only %d dropped" % source.dropped
    output.close()


def test_a_closed_source_ends_once_it_has_played_out():
    output = heel.Output(Capture(), threaded=False)
    source = output.add_source(48000, channels=1)
    source.write([0.5] * 480, fmt="float")
    source.close()
    eq(source.write([0.5] * 480, fmt="float"), 0, "a closed source took more")
    assert not source.ended, "it has not played out yet"
    _drive(output.start(), 2048)
    assert source.ended, "a played-out source should say so"
    output.close()


def test_clearing_throws_the_queue_away_without_ending_the_source():
    output = heel.Output(Capture(), threaded=False)
    source = output.add_source(44100, channels=1)
    source.write([0.5] * 44100, fmt="float")
    assert source.queued_seconds() > 0.5
    source.clear()
    eq(source.queued_seconds(), 0.0)
    assert not source.ended
    eq(source.write([0.5] * 441, fmt="float"), 441, "a clear closed the source")
    output.close()


def test_a_sources_position_is_what_has_been_heard_not_what_was_written():
    """The number `<video>` schedules pictures against. Everything still in
    the ring has been mixed and not yet played, and has to come off."""
    device = Capture(48000, 2)
    output = heel.Output(device, ring_frames=4800, threaded=False)
    source = output.add_source(48000, channels=1)
    source.write([0.1] * 48000, fmt="float")
    eq(source.position(), 0.0, "nothing has been heard yet")
    output.start()                      # start() primes the whole ring
    eq(source.position(), 0.0, "a primed ring has still been heard by nobody")
    device.pump(2400)
    close(source.position(), 0.05, 0.001, "half the ring is 50 ms")
    output.close()


# -- restart(): a seek is a new timeline through the same speaker -----------

def test_a_source_nobody_restarts_reports_exactly_what_it_always_did():
    """The identity case. `restart()` added two fields to every source in the
    process, and a source that never seeks must not notice them."""
    device = Capture(48000, 2)
    output = heel.Output(device, ring_frames=4800, threaded=False)
    source = output.add_source(48000, channels=1)
    eq(source._origin_pulled, 0, "a fresh source starts at the origin")
    eq(source._origin_at, 0.0, "a fresh source starts at zero seconds")
    source.write([0.1] * 48000, fmt="float")
    output.start()
    device.pump(2400)
    close(source.position(), 0.05, 1e-9, "the untouched timeline moved")
    output.close()


def test_a_restart_moves_the_timeline_to_where_it_was_told_to():
    output = heel.Output(Capture(48000, 2), ring_frames=4800, threaded=False)
    source = output.add_source(48000, channels=1)
    eq(source.position(), 0.0)
    source.restart(12.5)
    eq(source.position(), 12.5, "a restart did not move the timeline")
    source.restart()
    eq(source.position(), 0.0, "restart() with no argument is a seek to zero")
    output.close()


def test_a_restart_throws_the_queue_away_without_ending_the_source():
    output = heel.Output(Capture(), threaded=False)
    source = output.add_source(44100, channels=1)
    source.write([0.5] * 44100, fmt="float")
    assert source.queued_seconds() > 0.5
    source.restart(3.0)
    eq(source.queued_seconds(), 0.0, "a seek kept the old sound")
    assert not source.ended, "a seek is not the end of the stream"
    eq(source.write([0.5] * 441, fmt="float"), 441, "a seek closed the source")
    output.close()


def test_a_restart_revives_a_source_that_had_played_itself_out():
    """`ended` is how the player above finds out the file is over. Seeking
    backwards into a stream that has drained has to take that back, or the
    element stays finished for ever."""
    output = heel.Output(Capture(), threaded=False)
    source = output.add_source(48000, channels=1)
    source.write([0.5] * 480, fmt="float")
    source.close()
    _drive(output.start(), 2048)
    assert source.ended, "the source should have played out"
    source.restart(0.0)
    assert not source.ended, "a seek left the source ended"
    output.close()


def test_a_restart_while_sound_is_still_in_the_ring_leaves_no_offset():
    """The load-bearing one, and the whole reason `restart()` is not `clear()`
    with two assignments after it.

    What is queued in the source is dropped by the seek. What has already
    been handed to the mixer cannot be: it is in the ring, it is going to
    come out of the speaker, and the new timeline must not start until it
    has. Charging that backlog to the new segment instead is a permanent
    offset between the sound and the picture for as long as the file plays,
    and it is silent -- nothing errors, the video is just wrong by a ring's
    depth from the seek onwards.
    """
    device = Capture(48000, 2)
    output = heel.Output(device, ring_frames=4800, threaded=False)   # 100 ms
    source = output.add_source(48000, channels=1)
    source.write([0.1] * 48000, fmt="float")     # a second of the old segment
    output.start()                               # primes the whole ring
    device.pump(2400)                            # 50 ms of it has been heard
    close(source.position(), 0.05, 1e-9, "the old timeline")

    # Seek to ten seconds. 50 ms of the old segment is still in the ring.
    source.restart(10.0)
    source.write([0.2] * 48000, fmt="float")
    eq(source.position(), 10.0, "a restart starts where it was told to")

    # Play out what was left of the old segment. None of the new sound has
    # been heard yet, so the new timeline has not started to run.
    device.pump(2400)
    eq(source.position(), 10.0,
       "the old segment's ring backlog was charged to the new one")

    # Now the new segment, ten milliseconds of it.
    output.pump()
    device.pump(480)
    close(source.position(), 10.01, 1e-9, "the new timeline runs slow or fast")
    device.pump(480)
    close(source.position(), 10.02, 1e-9, "the new timeline drifted")
    output.close()


def test_a_restart_measures_the_new_segment_from_the_seek_not_from_zero():
    """Two seeks in a row, with sound heard in between, so an implementation
    that reset the counters instead of remembering them shows up as a
    position that keeps sliding."""
    device = Capture(48000, 2)
    output = heel.Output(device, ring_frames=4800, threaded=False)
    source = output.add_source(48000, channels=1)
    output.start()
    for target in (4.0, 30.0, 7.5):
        source.restart(target)
        source.write([0.3] * 48000, fmt="float")
        # Whatever is in the ring belongs to the segment we just left. Play
        # it out before the new one is mixed, so the sound after this line is
        # entirely the new segment's.
        device.pump(output.ring.backlog)
        eq(source.position(), target,
           "seeking to %.1f s did not start the timeline there" % target)
        _drive(output, 4800, block=480)          # 100 ms of this segment
        close(source.position(), target + 0.1, 1e-9,
              "the segment starting at %.1f s did not run from there" % target)
    output.close()


def test_writing_int16_and_float32_bytes_agree_with_writing_floats():
    """Three ways in, one sound out. A decoder hands over bytes; a test hands
    over floats; they must not be different code paths with different bugs."""
    wanted = None
    for fmt in (heel.INT16, heel.FLOAT32, "float"):
        device = Capture(48000, 2)
        output = heel.Output(device, ring_frames=1024, threaded=False)
        source = output.add_source(48000, channels=1)
        values = heel.tone(0.05, 1000.0, 48000, 1, amplitude=0.5)
        source.write(values if fmt == "float" else heel.pack(values, fmt),
                     fmt=fmt)
        output.start()
        _drive(output, 2000)
        output.close()
        got = device.floats()
        if wanted is None:
            wanted = got
            continue
        for i, (a, b) in enumerate(zip(wanted, got)):
            close(a, b, 1e-4, "%s diverged at %d" % (fmt, i))


def test_a_tone_generator_makes_the_tone_it_claims_to():
    """The tests above lean on this, so it gets one of its own."""
    values = heel.tone(0.1, 1000.0, 48000, 1, amplitude=0.5)
    eq(len(values), 4800)
    close(max(values), 0.5, 0.001)
    close(min(values), -0.5, 0.001)
    stereo = heel.tone(0.01, 1000.0, 48000, 2, amplitude=0.5)
    eq(len(stereo), 960, "two channels is twice the samples")
    eq(stereo[0::2], stereo[1::2], "the channels should match")


# -- the null device and graceful degradation ------------------------------

def test_the_null_device_keeps_real_time():
    """A machine with no sound card still has to play a video at the right
    speed. The paced null device is what makes that not a special case."""
    output = heel.open_output(backend="null", ring_frames=8192)
    assert output.silent, "the null backend should say it is silent"
    source = output.add_source(48000, channels=1)
    source.write([0.0] * 48000, fmt="float")
    started = time.monotonic()
    time.sleep(0.25)
    elapsed = time.monotonic() - started
    played = output.clock.now()
    output.close()
    close(played, elapsed, 0.08, "the silent clock lost time")
    assert played > 0.1, "the silent clock did not run at all"


def test_asking_for_no_device_is_answered_rather_than_refused():
    """The contract: open_output never raises and always returns something
    you can write samples to, so no caller has an untested no-audio path."""
    for name in ("null", "none"):
        output = heel.open_output(backend=name, threaded=False)
        assert output.silent
        assert output.reason, "a silent output should say why"
        output.close()


def test_the_environment_can_force_silence():
    """What CI and a bug report use. FEETBROWSER_AUDIO=null takes the
    hardware out of the picture without taking the clock out with it."""
    before = os.environ.get("FEETBROWSER_AUDIO")
    os.environ["FEETBROWSER_AUDIO"] = "null"
    try:
        heel.forget_probe()
        output = heel.open_output(threaded=False)
        assert output.silent, "FEETBROWSER_AUDIO=null was ignored"
        assert "FEETBROWSER_AUDIO" in output.reason
        output.close()
        eq(heel.available(), False, "available() ignored it too")
        assert heel.unavailable_reason(), "and said nothing about why"
    finally:
        if before is None:
            os.environ.pop("FEETBROWSER_AUDIO", None)
        else:
            os.environ["FEETBROWSER_AUDIO"] = before
        heel.forget_probe()


def test_availability_is_worked_out_once_rather_than_per_frame():
    """h264.py's rule: a machine with no device must not be asked again on
    every buffer. The answer is memoised until something asks for it to be
    forgotten."""
    heel.forget_probe()
    first = heel.available()
    calls = []
    real = heel._make_device

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    heel._make_device = counting
    try:
        for _ in range(5):
            eq(heel.available(), first, "the answer changed under us")
        eq(calls, [], "available() probed the hardware again")
    finally:
        heel._make_device = real
        heel.forget_probe()


def test_closing_twice_is_not_an_error():
    output = heel.open_output(backend="null", threaded=False)
    output.close()
    output.close()
    with heel.open_output(backend="null", threaded=False) as inner:
        assert inner is not None


def test_close_all_lets_go_of_everything_still_open():
    """The atexit hook's body. Calling into Python from a device thread
    during interpreter shutdown is a crash rather than an exception."""
    outputs = [heel.open_output(backend="null") for _ in range(3)]
    heel.close_all()
    for output in outputs:
        assert output._closed, "close_all missed one"


# -- the platform layers, without their platforms --------------------------

def test_the_fourcc_helpers_agree_with_each_other():
    """Half of CoreAudio's constants are ASCII spelled sideways, and writing
    them as hex is how a typo becomes an unexplained -10879."""
    eq(coreaudio.fourcc("lpcm"), 0x6C70636D)
    eq(coreaudio.fourcc("auou"), 0x61756F75)
    for text in ("lpcm", "def ", "appl", "dOut"):
        eq(coreaudio.fourcc_name(coreaudio.fourcc(text)), "'%s'" % text)
    eq(coreaudio.fourcc_name(7), "7", "a number that is not text stays one")


def test_a_stream_format_describes_what_we_actually_send():
    """CoreAudio believes this structure. Bytes-per-frame that disagrees with
    the channel count plays at the wrong speed rather than failing."""
    asbd = coreaudio.stream_format(48000, 2, heel.FLOAT32)
    eq(asbd.mSampleRate, 48000.0)
    eq(asbd.mChannelsPerFrame, 2)
    eq(asbd.mBitsPerChannel, 32)
    eq(asbd.mBytesPerFrame, 8)
    eq(asbd.mBytesPerPacket, 8, "one frame per packet for linear PCM")
    eq(asbd.mFramesPerPacket, 1)
    assert coreaudio.describes_interleaved(asbd)
    short = coreaudio.stream_format(44100, 1, heel.INT16)
    eq(short.mBitsPerChannel, 16)
    eq(short.mBytesPerFrame, 2)


def test_a_non_interleaved_format_is_recognised_as_one():
    """The one difference we cannot survive: the callback would be writing
    one channel into a buffer sized for two."""
    asbd = coreaudio.stream_format(48000, 2, heel.FLOAT32)
    asbd.mFormatFlags |= coreaudio._FLAG_IS_NON_INTERLEAVED
    assert not coreaudio.describes_interleaved(asbd)


def test_the_alsa_helpers_answer_without_a_sound_card():
    eq(alsa.sample_format(heel.FLOAT32), 14, "SND_PCM_FORMAT_FLOAT_LE")
    eq(alsa.sample_format(heel.INT16), 2, "SND_PCM_FORMAT_S16_LE")
    try:
        alsa.sample_format("u8")
    except heel.AudioUnavailable:
        pass
    else:
        raise AssertionError("ALSA accepted a format it cannot be given")
    eq(alsa.rate_candidates(44100)[0], 44100, "the asked-for rate goes first")
    assert 48000 in alsa.rate_candidates(44100)
    eq(len(set(alsa.rate_candidates(48000))), len(alsa.rate_candidates(48000)),
       "a rate is offered twice")


def test_the_alsa_device_can_be_named_by_the_environment():
    before = os.environ.get("FEETBROWSER_ALSA_DEVICE")
    try:
        os.environ["FEETBROWSER_ALSA_DEVICE"] = "hw:1,0"
        eq(alsa.pcm_name(), "hw:1,0")
        os.environ["FEETBROWSER_ALSA_DEVICE"] = "  "
        eq(alsa.pcm_name(), alsa.DEFAULT_PCM, "blank should mean default")
    finally:
        if before is None:
            os.environ.pop("FEETBROWSER_ALSA_DEVICE", None)
        else:
            os.environ["FEETBROWSER_ALSA_DEVICE"] = before


def test_the_wave_structures_are_the_sizes_windows_expects():
    """mmsystem.h byte-packs its structures, which is why WAVEFORMATEX is the
    famous 18 rather than the 20 natural alignment would give it. WAVEHDR's
    size is passed to every call that takes it, so a wrong one is a driver
    reading its linked list out of our sample data."""
    eq(ctypes.sizeof(winmm.WAVEFORMATEX), 18)
    eq(ctypes.sizeof(winmm.WAVEHDR), 48 if ctypes.sizeof(ctypes.c_void_p) == 8
       else 32)


def test_a_wave_format_describes_what_we_actually_send():
    fmt = winmm.wave_format(48000, 2)
    eq(fmt.wFormatTag, 1, "WAVE_FORMAT_PCM")
    eq(fmt.nChannels, 2)
    eq(fmt.nSamplesPerSec, 48000)
    eq(fmt.wBitsPerSample, 16)
    eq(fmt.nBlockAlign, 4, "a wrong block align plays at the wrong speed")
    eq(fmt.nAvgBytesPerSec, 48000 * 4)
    eq(fmt.cbSize, 0, "no extension follows")


def test_waveout_refuses_floats_rather_than_playing_noise():
    """Some drivers take WAVE_FORMAT_IEEE_FLOAT through plain WAVEFORMATEX
    and some need WAVEFORMATEXTENSIBLE, and the failure is noise rather than
    an error. Sixteen-bit is the format every card has had since 1992."""
    try:
        winmm.wave_format(48000, 2, heel.FLOAT32)
    except heel.AudioUnavailable:
        return
    raise AssertionError("waveOut was handed floats")


def test_a_wave_block_is_a_sane_size():
    eq(winmm.block_frames(48000, 20), 960)
    eq(winmm.block_frames(44100, 20), 882)
    assert winmm.block_frames(8000, 1) >= 64, "a block must not be tiny"


def test_every_backend_reports_availability_without_raising():
    """Called from a browser starting up on a machine nobody has described,
    so it has to answer rather than explode -- including on the two platforms
    out of three that are always the wrong one."""
    for module in (coreaudio, alsa, winmm):
        answer = module.available()
        assert answer in (True, False), "%s said %r" % (module, answer)
        reason = module.unavailable_reason()
        assert isinstance(reason, str), "%s gave %r" % (module, reason)
        if answer:
            eq(reason, "", "%s is available and complaining" % module)


# -- the live half ---------------------------------------------------------

def _live_reason():
    """Whether there is a device here that actually consumes samples.

    Opening one is not enough to prove it. A CI runner with no sound hardware
    can still have a backend that opens, initialises and starts without ever
    asking for a buffer, and every test below would then fail for the one
    reason that is not a bug. So the gate is empirical: start a device, wait
    a moment, and see whether the clock moved.
    """
    if (os.environ.get("FEETBROWSER_AUDIO") or "").strip().lower() in (
            "null", "off", "none", "silent"):
        return "FEETBROWSER_AUDIO asked for no device"
    if not heel.available():
        return heel.unavailable_reason() or "no audio device on this platform"
    output = heel.open_output()
    try:
        if output.silent:
            return output.reason or "the device went away between two calls"
        deadline = time.monotonic() + 1.0
        while not output.clock.frames and time.monotonic() < deadline:
            time.sleep(0.01)
        if not output.clock.frames:
            return "the device opened but never asked for a sample"
        if output.device.failure is not None:
            return "the device failed at once: %s" % (output.device.failure,)
    finally:
        output.close()
    return ""


LIVE_REASON = _live_reason()
LIVE = not LIVE_REASON


def live_a_real_device_consumes_frames_in_real_time():
    """The one thing only hardware can show: that the device pulls from the
    ring at the rate its own crystal runs at. A quarter second of a tone quiet
    enough not to startle anybody, and then the clock is compared against the
    wall.

    Both ends of the window are taken after the device is already running, and
    that is the whole care in this test. ``start()`` is not instant -- it hands
    the format to the operating system, which finds a device, may resample and
    then schedules a thread -- and on a busy machine with no sound card
    attached, which is to say on CI, the gap between asking and the first
    callback has been measured at over 200 ms. Timing from before ``start()``
    charges all of that to the crystal and fails a device that is keeping
    perfect time; it is a start-up latency test wearing a rate test's name.
    So: wait for the first frame, then measure a window inside the run."""
    output = heel.open_output()
    assert not output.silent, "available() said yes and open_output said no"
    source = output.add_source(44100, channels=1, gain=0.2, name="live")
    source.write(heel.tone(1.0, 440.0, 44100, 1, amplitude=0.2), fmt="float")
    output.start()
    deadline = time.monotonic() + 1.0
    while not output.clock.frames and time.monotonic() < deadline:
        time.sleep(0.005)
    assert output.clock.frames, "the device never asked for a sample"
    started = time.monotonic()
    base = output.clock.now()
    before = output.clock.underruns
    time.sleep(0.25)
    elapsed = time.monotonic() - started
    played = output.clock.now() - base
    underruns = output.clock.underruns - before
    failure = output.device.failure
    output.close()
    eq(failure, None, "the device thread recorded a failure")
    close(played, elapsed, 0.06, "the device clock does not match the wall")
    assert played > 0.15, "only %.3f s came out" % played
    assert underruns <= 1, "%d underruns in a quarter second" % underruns


def live_the_device_agrees_a_format_it_can_actually_use():
    output = heel.open_output()
    device = output.device
    try:
        assert device.rate >= 8000, "%d Hz is not a rate" % device.rate
        eq(device.frame_bytes, device.channels * heel.SAMPLE_BYTES[device.fmt])
        eq(output.ring.frame_bytes, device.frame_bytes,
           "the ring and the device disagree about a frame")
        assert 0.0 <= device.latency < 1.0, \
            "%.3f s of latency is not credible" % device.latency
        assert repr(device)
    finally:
        output.close()


def live_a_device_can_be_opened_and_closed_repeatedly():
    """A browser opens one per video and closes it at the end of the tab.
    Leaking a unit or freeing a running one is a crash on the audio thread,
    which is a crash with no Python frame to blame it on."""
    for _ in range(4):
        output = heel.open_output()
        assert not output.silent
        output.start()
        time.sleep(0.02)
        output.close()
        output.close()


def live_two_sounds_play_at_once_on_real_hardware():
    output = heel.open_output()
    try:
        first = output.add_source(48000, channels=1, gain=0.15, name="a")
        second = output.add_source(48000, channels=1, gain=0.15, name="b")
        first.write(heel.tone(0.3, 440.0, 48000, 1, amplitude=0.2),
                    fmt="float")
        second.write(heel.tone(0.3, 660.0, 48000, 1, amplitude=0.2),
                     fmt="float")
        output.volume = 0.5
        output.start()
        time.sleep(0.2)
        eq(output.device.failure, None, "mixing two sources broke the device")
        assert output.clock.frames > 0, "nothing was consumed"
    finally:
        output.close()


def main():
    everything = sorted(globals().items())
    pure = [v for k, v in everything if k.startswith("test_")]
    live = [v for k, v in everything if k.startswith("live_")]
    if not LIVE:
        print("SKIP the live half of test_audio.py: %s" % LIVE_REASON)
        live = []
    failed = 0
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    for test in pure + live:
        if only and test.__name__ not in only:
            continue
        try:
            test()
            print("  ok  %s" % test.__name__, flush=True)
        except Exception as exc:
            failed += 1
            import traceback
            traceback.print_exc()
            print(" FAIL %s: %s" % (test.__name__, exc), flush=True)
    heel.close_all()
    if failed:
        print("\n%d FAILED" % failed)
        sys.exit(1)
    print("\nALL %d AUDIO TESTS PASSED (%d against real hardware)"
          % (len(pure) + len(live), len(live)))


if __name__ == "__main__":
    main()
