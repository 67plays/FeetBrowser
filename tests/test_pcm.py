"""Uncompressed sound, in the three containers that carry it.

PCM is not decoded, it is read: a width, a byte order, a sign convention and
a scale. That makes it the one audio format where "the samples are right" is
a question with an exact answer, and it makes the wrong answers dangerous.
A byte order read backwards, a sign convention taken from the wrong century
or a width guessed from a stale fourcc does not raise and does not fall
silent -- it produces something that looks like sound, plots like sound and
is not the sound in the file. So nothing here is asserted with a tolerance.

Every fixture in `tests/fixtures/pcm` is the same quarter of a second of two
tones -- 400 Hz on the left, 1300 Hz on the right -- written out by a real
muxer into a different container and a different sample format. The source
waveform ships beside them as `tone.s16le.z`, and the conversions FFmpeg
performed to make the rest are all exact: s16 to s24 is a shift, s16 to s32
is a shift, s16 to float is a divide by 32768, and this decoder scales an
n-bit sample by 2^-(n-1). Thirteen files, one waveform, and the assertion is
equality.

The exceptions are named where they occur. Eight-bit is a real
requantisation and has its own truth file, `tone.u8.z`. `mulaw.wav` and
`adpcm.wav` are there to be refused rather than read.

`tests/fixtures/pcm/make_pcm_vectors.sh` is the offline tool that produced
all of them and says what each one is for. It is not run by the suite and
FFmpeg is not a dependency of anything here.
"""
import array
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import arch, heel, mediacodec

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "pcm")


def eq(a, b, msg=""):
    assert a == b, "%s: %r != %r" % (msg, a, b)


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def truth16():
    """The source waveform as floats, by the scale the decoder must use."""
    samples = array.array("h")
    samples.frombytes(zlib.decompress(fixture("tone.s16le.z")))
    if sys.byteorder == "big":
        samples.byteswap()
    return [value / 32768.0 for value in samples]


def truth8():
    """The same waveform after FFmpeg requantised it to eight bits, which is
    a lossy step and so cannot be derived from `truth16`."""
    samples = array.array("B")
    samples.frombytes(zlib.decompress(fixture("tone.u8.z")))
    return [(value - 128) / 128.0 for value in samples]


def samples_of(track):
    """Every block of a track, concatenated, as a list of floats."""
    out = array.array("f")
    for index in range(track.sample_count):
        block = array.array("f")
        block.frombytes(track.frame(index).samples)
        out.extend(block)
    return list(out)


def decoded(name):
    return samples_of(mediacodec.open_audio(fixture(name)))


def first_difference(got, want):
    """The index of the first sample that differs, or -1. Reported rather
    than asserted so the failure says *where*: a byte-order bug and a
    channel swap and an off-by-one block boundary all read as "not equal"
    and the index is what tells them apart."""
    if len(got) != len(want):
        return min(len(got), len(want))
    for i, (a, b) in enumerate(zip(got, want)):
        if a != b:
            return i
    return -1


def identical(got, want, msg):
    where = first_difference(got, want)
    assert where < 0, ("%s: %d samples against %d, first difference at %d "
                       "(%r against %r)"
                       % (msg, len(got), len(want), where,
                          got[where] if where < len(got) else None,
                          want[where] if where < len(want) else None))


# Every fixture that is the source waveform written out losslessly. The
# comment on each is the thing it is here to prove, because a list of
# filenames is not a list of cases.
LOSSLESS = (
    ("sowt.mov", "QuickTime little-endian s16, version 0 sample entry"),
    ("twos.mov", "QuickTime big-endian s16: the same file, byteswapped"),
    ("in24.mov", "24-bit big-endian, unpacked three bytes at a time"),
    ("in24le.mov", "the same, little-endian because an `enda` box says so"),
    ("in32.mov", "32-bit big-endian, where the scale is 2^-31"),
    ("fl32.mov", "float32, which needs no scaling and only a byteswap"),
    ("fl64.mov", "float64, narrowed to the float32 the mixer takes"),
    ("lpcm96.mov", "`lpcm`, described only by version 2's format flags"),
    ("s16le.wav", "WAVEFORMATEX tag 1, the file everything else is not"),
    ("s24le.wav", "24-bit tag 1, whose block align is not a power of two"),
    ("s24ext.wav", "the same samples as WAVE_FORMAT_EXTENSIBLE, tag 0xFFFE"),
    ("f32le.wav", "WAVEFORMATEX tag 3, IEEE float"),
    ("pcm.avi", "gathered back out of the `01wb` chunks a muxer split it into"),
)


def test_every_container_decodes_the_same_waveform_bit_for_bit():
    """The whole feature in one assertion, thirteen times.

    These are thirteen different widths, byte orders, sample entry versions
    and demuxers over one quarter-second of sound, so a decoder that gets any
    of those wrong disagrees with the others rather than with a tolerance.
    Reading `twos.mov` and `sowt.mov` to the same numbers is a stronger claim
    than either file passing on its own: they are byte-for-byte reverses of
    each other.
    """
    want = truth16()
    eq(len(want), 4000, "the source waveform is 2000 stereo frames")
    for name, why in LOSSLESS:
        got = decoded(name)
        eq(len(got), len(want), "%s (%s) came out a different length"
                                % (name, why))
        identical(got, want, "%s (%s)" % (name, why))


def test_eight_bit_pcm_is_offset_binary_and_not_a_scale_factor():
    """The one asymmetry in the format, in all three containers.

    Eight-bit PCM is unsigned with silence at 128; every wider width is two's
    complement with silence at zero. There is no flag anywhere that says so,
    it is simply what the specifications say, and a decoder that applies the
    signed path to it produces full-scale square-wave noise rather than a
    quiet tone -- which is why the check that it is *not* the sixteen-bit
    truth is here too.
    """
    want = truth8()
    loud = truth16()
    for name in ("raw.mov", "u8.wav", "u8.avi"):
        got = decoded(name)
        identical(got, want, "%s is not the eight-bit truth" % name)
        assert first_difference(got, loud) >= 0, \
            "%s decoded to the 16-bit waveform, which it cannot be" % name
    # And the difference between the two truths is requantisation and
    # nothing else: one step of an eight-bit sample, which is 2^-7. A whole
    # step rather than half of one because the conversion that made the
    # fixture drops the low byte instead of rounding it.
    worst = max(abs(a - b) for a, b in zip(want, loud))
    assert worst <= 1.0 / 128, "eight bits lost more than eight bits: %g" % worst


def test_the_sample_entry_is_believed_where_the_fourcc_is_stale():
    """QuickTime's sound entries contradict themselves, and the newer field
    is the one telling the truth.

    All three cases here are files FFmpeg writes, not hypotheticals:

      * `in24.mov` is a version 1 entry whose `sampleSize` is 16 and whose
        samples are 24 bits wide. The width is `bytesPerPacket` over
        `samplesPerPacket`; `sampleSize` describes the canonical *unpacked*
        form and is a polite fiction about the file.
      * `in24le.mov` has the identical fourcc and an `enda` box saying 1.
        The fourcc says big-endian, the box says little, and the box wins.
      * `lpcm96.mov` is a version 2 entry whose fourcc says nothing at all:
        the width, the sign and the byte order are in `constBitsPerChannel`
        and `formatSpecificFlags`.
    """
    def audio_track(name):
        _duration, tracks = mediacodec._parse_mp4(fixture(name))
        found = [t for t in tracks if t.handler == "soun"]
        eq(len(found), 1, "%s should have one sound track" % name)
        return found[0]

    wide = audio_track("in24.mov")
    eq(wide.entry_version, 1)
    eq(wide.sample_size, 16, "the fixture stopped being the awkward case")
    eq(wide.bytes_per_packet, 3)
    eq(mediacodec._mp4_pcm_layout(wide), (24, True, True, False))

    little = audio_track("in24le.mov")
    eq(little.codec, "in24", "the fourcc is the big-endian one")
    eq(little.endian, 1, "`enda` says little")
    eq(mediacodec._mp4_pcm_layout(little), (24, True, False, False))

    described = audio_track("lpcm96.mov")
    eq(described.entry_version, 2)
    eq(described.codec, "lpcm")
    eq(described.bits_per_channel, 16)
    eq(described.lpcm_flags & mediacodec.LPCM_SIGNED,
       mediacodec.LPCM_SIGNED, "signed integers")
    eq(described.lpcm_flags & mediacodec.LPCM_FLOAT, 0, "not floats")
    eq(described.lpcm_flags & mediacodec.LPCM_BIG_ENDIAN, 0, "little-endian")
    eq(mediacodec._mp4_pcm_layout(described), (16, True, False, False))
    # And version 2 is where the rate lives too, as a float64 rather than the
    # 16.16 fixed point that cannot hold 96000 in the first place.
    eq(mediacodec.probe_audio(fixture("lpcm96.mov")).sample_rate, 96000)

    # Believing `sampleSize` for `in24.mov` is not a small error. Reading
    # those bytes at sixteen bits is what the fourcc-only decoder would do,
    # and this is what it would have produced.
    track = mediacodec.open_audio(fixture("in24.mov"))
    wrong = mediacodec._Pcm(2, 16, True, True, False)
    naive = array.array("f")
    for index in range(track.sample_count):
        _count, _channels, block = wrong.decode(track.packet(index))
        chunk = array.array("f")
        chunk.frombytes(block)
        naive.extend(chunk)
    assert first_difference(list(naive), truth16()) >= 0, \
        "reading 24-bit samples as 16-bit produced the right waveform"

    # The other direction: an entry that describes nothing is refused rather
    # than guessed at. No muxer writes any of these, which is exactly why
    # they are built by hand here -- an entry we cannot read is the case
    # where inventing a width would put noise through a speaker.
    def entry(**fields):
        track = mediacodec._Mp4Track()
        track.codec = "lpcm"
        track.handler = "soun"
        for name, value in fields.items():
            setattr(track, name, value)
        return track

    unreadable = {
        "`lpcm` in a version 0 entry, which has no flags to read":
            entry(entry_version=0, sample_size=16),
        "`lpcm` in a version 1 entry, which has a width and no sign":
            entry(entry_version=1, samples_per_packet=1, bytes_per_packet=2),
        "a version 2 entry with no formatSpecificFlags":
            entry(entry_version=2, bits_per_channel=16, lpcm_flags=-1),
        "non-interleaved PCM, which has nowhere to go":
            entry(entry_version=2, bits_per_channel=16,
                  lpcm_flags=mediacodec.LPCM_SIGNED |
                  mediacodec.LPCM_NON_INTERLEAVED),
        "a width that is not a width":
            entry(entry_version=2, bits_per_channel=20,
                  lpcm_flags=mediacodec.LPCM_SIGNED),
    }
    for what, track in unreadable.items():
        try:
            mediacodec._mp4_pcm_layout(track)
        except mediacodec.MediaError as exc:
            assert "lpcm" in str(exc), "%s: %s" % (what, exc)
        else:
            raise AssertionError("%s was read anyway" % what)


def test_reading_the_bytes_the_wrong_way_round_is_a_different_sound():
    """The negative control, and the reason none of the above is a tolerance.

    Two failures that must be caught, because both of them are what a broken
    PCM path actually looks like:

      * `twos.mov` read little-endian instead of big. Nothing raises, every
        block is the right length and the numbers are all inside [-1, 1].
        The only thing wrong with it is that it is not the file.
      * one sample of the truth moved by one float. If `identical` cannot
        see that, it cannot see anything above it either.
    """
    track = mediacodec.open_audio(fixture("twos.mov"))
    want = truth16()
    backwards = mediacodec._Pcm(2, 16, True, False, False)
    got = array.array("f")
    for index in range(track.sample_count):
        _count, _channels, block = backwards.decode(track.packet(index))
        chunk = array.array("f")
        chunk.frombytes(block)
        got.extend(chunk)
    got = list(got)
    eq(len(got), len(want), "the wrong byte order is still the right length")
    assert max(abs(v) for v in got) <= 1.0, \
        "even read backwards it stays in range, which is the whole problem"
    where = first_difference(got, want)
    assert where >= 0, "a byteswapped file decoded to the right waveform"
    worst = max(abs(a - b) for a, b in zip(got, want))
    assert worst > 0.5, "a byteswapped file should be loudly wrong, not %g" % worst

    # The assertion itself, held against a single perturbed sample. The
    # perturbation is one float step, which is the smallest change that can
    # be made to the data at all.
    good = decoded("sowt.mov")
    identical(good, want, "the control's control")
    nudged = list(want)
    nudged[1234] = struct.unpack("<f", struct.pack(
        "<I", struct.unpack("<I", struct.pack("<f", nudged[1234]))[0] + 1))[0]
    assert nudged[1234] != want[1234], "the nudge did nothing"
    eq(first_difference(good, nudged), 1234,
       "one changed sample went unnoticed")
    try:
        identical(good, nudged, "one changed sample")
    except AssertionError:
        pass
    else:
        raise AssertionError("identical() passed a waveform that differs")


def test_the_blocks_are_a_size_a_player_can_use():
    """PCM has no coded frame, so the block size is ours, and it has to sit
    between two numbers in `arch.py` rather than be a round one.

    `TARGET_QUEUE` is how far ahead the player decodes and `DECODE_BUDGET`
    how many blocks it may decode in one inline `pump()`, so a block smaller
    than their ratio leaves the queue permanently short. A much larger one is
    sound a seek has to throw away. The tenth of a second here is inside both
    with room, and the times it produces have to be a timeline: contiguous,
    increasing, and adding up to the file.
    """
    assert mediacodec.PCM_BLOCK_SECONDS >= \
        arch.TARGET_QUEUE / arch.DECODE_BUDGET, \
        "a block this small cannot fill the queue in one pump"
    assert mediacodec.PCM_BLOCK_SECONDS <= arch.TARGET_QUEUE / 2, \
        "a block this large is latency a pause cannot take back"

    track = mediacodec.open_audio(fixture("s16le.wav"))
    eq(track.sample_count, 3, "a quarter second in tenths is three blocks")
    eq(track.sample_rate, 8000)
    eq(track.channels, 2)
    assert abs(track.duration - 0.25) < 1e-9, track.duration

    played = 0.0
    for index in range(track.sample_count):
        start = track.frame_time(index)
        length = track.frame_duration(index)
        assert abs(start - played) < 1e-9, \
            "block %d starts at %g and the one before ended at %g" \
            % (index, start, played)
        frame = track.frame(index)
        eq(frame.sample_count, int(round(length * track.sample_rate)),
           "block %d holds a different amount of sound than it claims" % index)
        eq(frame.pts, start)
        played += length
    assert abs(played - track.duration) < 1e-9, \
        "the blocks add up to %g and the file is %g" % (played, track.duration)
    # The last block is the short one, which is the case an off-by-one in the
    # cutting hides in: 0.25 seconds is two full tenths and a half.
    assert abs(track.frame_duration(2) - 0.05) < 1e-9, \
        track.frame_duration(2)
    eq(track.index_at(0.0), 0)
    eq(track.index_at(0.15), 1)
    eq(track.index_at(0.24), 2)
    eq(track.index_at(99.0), 2, "past the end is the last block")


def test_a_pcm_track_is_random_access_and_says_so():
    """What `stateless` buys, and the thing it must not cost.

    AAC carries the previous frame's transform tail, so asking it for frame
    500 means decoding five hundred frames. PCM carries nothing across a
    block boundary, so a seek costs one block -- and the proof is that the
    same block asked for out of order comes back byte for byte the same.
    """
    track = mediacodec.open_audio(fixture("sowt.mov"))
    assert track._stateless, "PCM should not be replayed from the start"
    forwards = track.frame(2).samples
    track.frame(0)
    backwards = track.frame(2).samples
    eq(backwards, forwards, "block 2 decoded differently the second time")
    eq(track.frame(1).samples, samples_bytes(track, 1),
       "block 1 decoded differently after a seek")
    try:
        track.frame(track.sample_count)
    except mediacodec.MediaError:
        pass
    else:
        raise AssertionError("a block past the end decoded anyway")


def samples_bytes(track, index):
    """`track.frame(index)` reached the long way round, from a fresh track."""
    fresh = mediacodec.open_audio(track._data)
    return fresh.frame(index).samples


def test_an_avis_sound_arrives_beside_the_pictures_it_belongs_to():
    """The reason to demux an AVI's audio at all.

    `pcm.avi` is MJPEG and PCM interleaved the way a muxer writes them: a
    chunk of sound, a picture, a chunk of sound. Until the `01wb` chunks were
    gathered this file played perfectly and silently. Both halves have to
    come out of the same bytes, and the sound has to be the whole of it --
    the chunks are 40 milliseconds each, so a demuxer that took only the
    first one would still produce a frame and still sound like the file.
    """
    data = fixture("pcm.avi")
    picture = mediacodec.open_video(data)
    eq((picture.info.width, picture.info.height), (32, 24))
    assert picture.info.supported and picture.info.frame_count > 0
    assert picture.frame(0).rgba, "the picture stopped decoding"

    track = mediacodec.open_audio(data)
    eq(track.container, "AVI")
    eq(track.codec_name, "PCM")
    eq((track.sample_rate, track.channels), (8000, 2))
    identical(samples_of(track), truth16(), "the AVI's sound")
    # The chunks are smaller than a block, so this is also the case where
    # `_pcm_blocks` had to merge contiguous ranges before cutting them.
    assert track.sample_count < 10, \
        "one block per `01wb` chunk is %d blocks" % track.sample_count

    # An AVI whose audio stream is declared and never written is not a file
    # with silent sound; it is a file to say so about.
    empty = mediacodec.probe_audio(
        data[:data.index(b"movi") + 4] + b"\x00" * 8)
    assert not empty.supported
    assert "01wb" in empty.reason, empty.reason


def test_companded_and_predicted_formats_are_refused_by_name():
    """Neither of these is PCM and neither becomes PCM by being read harder.

    mu-law is eight bits through a logarithmic curve and A-law through a
    different one; ADPCM is four-bit deltas against a running predictor.
    Each needs a decoder, none of them is written here, and "unsupported" is
    a useless thing to tell somebody whose file will not play -- so each is
    refused with its own sentence, naming itself.
    """
    companded = mediacodec.probe_audio(fixture("mulaw.wav"))
    eq(companded.container, "WAV")
    eq(companded.codec, "mu-law")
    eq((companded.sample_rate, companded.channels), (8000, 2))
    assert not companded.supported
    assert "mu-law" in companded.reason and "companded" in companded.reason

    predicted = mediacodec.probe_audio(fixture("adpcm.wav"))
    eq(predicted.codec, "ADPCM")
    assert not predicted.supported
    assert "ADPCM" in predicted.reason and "predictor" in predicted.reason

    for name in ("mulaw.wav", "adpcm.wav"):
        try:
            mediacodec.open_audio(fixture(name))
        except mediacodec.MediaError as exc:
            assert "no decoder" in str(exc), str(exc)
        else:
            raise AssertionError("%s decoded" % name)

    # The rest of the table, which has no fixture because the point is the
    # sentence rather than the file. A-law and IMA ADPCM are the two most
    # likely to turn up next and both are refused under their own names.
    alaw = mediacodec._wave_pcm_layout
    for tag, word in ((0x0006, "A-law"), (0x0011, "IMA"), (0x2000, "Dolby")):
        fmt = mediacodec._WaveFormat()
        fmt.tag = tag
        fmt.bits = 8
        fmt.name = mediacodec.WAVE_FORMAT_NAMES[tag]
        try:
            alaw(fmt, "WAV")
        except mediacodec.MediaError as exc:
            assert word in str(exc), str(exc)
        else:
            raise AssertionError("0x%04X was accepted as PCM" % tag)

    # And QuickTime's own compressed uncompressed-looking formats, which sit
    # in the same table as the fourccs that now work.
    for fourcc in ("ima4", "ulaw", "alaw"):
        assert fourcc in mediacodec.KNOWN_UNDECODABLE_AUDIO
        assert fourcc not in mediacodec.PCM_FOURCCS
    for fourcc in mediacodec.PCM_FOURCCS:
        assert fourcc not in mediacodec.KNOWN_UNDECODABLE_AUDIO, \
            "%s is both readable and refused" % fourcc


def test_a_wav_is_a_container_with_no_picture_in_it():
    """`<video src="x.wav">` should be told what is wrong with it.

    A WAV is now a container we recognise, so `probe` answers rather than
    raising -- and what it answers is that there is nothing to draw. The
    sound is `probe_audio`'s to describe, and `<audio>` is still not
    implemented, so this is a file the browser can read and has nowhere to
    play from yet.
    """
    data = fixture("s16le.wav")
    eq(mediacodec.sniff(data), "WAV")
    info = mediacodec.probe(data)
    eq(info.container, "WAV")
    assert not info.supported
    assert "no picture" in info.reason, info.reason
    try:
        mediacodec.open_video(data)
    except mediacodec.MediaError as exc:
        assert "no picture" in str(exc), str(exc)
    else:
        raise AssertionError("open_video found a picture in a WAV")
    assert mediacodec.probe_audio(data).supported


def test_a_broken_wav_is_a_file_and_not_a_hang():
    """Every way a RIFF can be wrong, ending in a sentence.

    The lengths in a RIFF are the file's own claims about itself and a
    truncated one is a real file -- a write that stopped. None of these may
    hang, none may read past the end, and each has to be describable.
    """
    def wav(fmt, payload=b"", between=b""):
        body = (b"WAVE" + between + b"fmt " + struct.pack("<I", len(fmt)) +
                fmt + b"data" + struct.pack("<I", len(payload)) + payload)
        return b"RIFF" + struct.pack("<I", len(body)) + body

    def waveformat(tag=1, channels=2, rate=8000, bits=16, tail=b""):
        align = channels * bits // 8
        return struct.pack("<HHIIHH", tag, channels, rate, rate * align,
                           align, bits) + tail

    whole = fixture("s16le.wav")
    for cut in range(0, len(whole), 97):
        try:
            info = mediacodec.probe_audio(whole[:cut])
        except mediacodec.MediaError:
            continue                    # a header we never reached
        if info.supported:
            track = mediacodec.open_audio(whole[:cut])
            got = samples_of(track)
            identical(got, truth16()[:len(got)],
                      "the first %d bytes decoded to something else" % cut)

    # A `data` chunk claiming a gigabyte in an eight-kilobyte file. The walk
    # is bounded by the bytes there are, not by the number in the header.
    lying = bytearray(whole)
    struct.pack_into("<I", lying, lying.index(b"data") + 4, 1 << 30)
    track = mediacodec.open_audio(bytes(lying))
    identical(samples_of(track), truth16(), "a lying data length")

    named = {
        "no whole samples": wav(waveformat(), b"\x00" * 3),
        "empty": wav(waveformat(), b""),
        "12-bit": wav(waveformat(bits=12), b"\x00" * 1200),
        "0 channels": wav(waveformat(channels=0), b"\x00" * 1200),
        "0 Hz": wav(waveformat(rate=0), b"\x00" * 1200),
        "16 channels": wav(waveformat(channels=16), b"\x00" * 1600),
    }
    for what, data in named.items():
        info = mediacodec.probe_audio(data)
        assert not info.supported, "%s decoded" % what
        assert info.reason, "%s was refused without saying why" % what

    for what, data in (("no fmt", b"RIFF\x04\x00\x00\x00WAVE"),
                       ("half a fmt", wav(waveformat()[:12], b"\x00" * 100))):
        try:
            mediacodec.probe_audio(data)
        except mediacodec.MediaError:
            continue
        raise AssertionError("%s was read anyway" % what)

    # WAVE_FORMAT_EXTENSIBLE, where the tag is a GUID and the GUID can be
    # somebody else's. The shipped `s24ext.wav` is the case that works; these
    # are the two that must not be mistaken for it.
    guid = b"\x01\x00" + mediacodec._KSDATAFORMAT_TAIL
    private = wav(waveformat(tag=0xFFFE, tail=struct.pack("<HHI", 22, 16, 3) +
                             b"\xff" * 16), b"\x00" * 400)
    impossible = wav(waveformat(tag=0xFFFE, bits=16,
                                tail=struct.pack("<HHI", 22, 24, 3) + guid),
                     b"\x00" * 400)
    for what, data in (("a private SubFormat", private),
                       ("24 valid bits in 16", impossible)):
        try:
            mediacodec.probe_audio(data)
        except mediacodec.MediaError:
            continue
        raise AssertionError("%s was accepted" % what)


class Capture(heel.NullDevice):
    """A device that keeps what it consumed. The same twenty lines as the one
    in `tests/test_audio.py`, and here for the same reason: what reached the
    driver is the only place the whole chain can be measured at once."""

    name = "capture"

    def __init__(self, rate, channels):
        super().__init__(rate, channels, heel.FLOAT32, paced=False)
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


def test_the_arch_plays_a_pcm_track_through_to_the_device():
    """End to end, with nothing stubbed between the file and the driver.

    The point is that `AudioTrack` was the whole interface: no part of
    `arch.py`, `heel.py` or `media.py` knows that PCM exists, and this test
    is what says so. The device runs at the file's own rate so the resampler
    is a pass-through and the comparison can stay exact -- resampling is
    `tests/test_audio.py`'s subject, and a tolerance here would hide the
    thing this file is about.
    """
    device = Capture(8000, 2)
    output = heel.Output(device, ring_frames=400, threaded=False)
    output.silent = False                  # stand in for a sound card
    player = arch.AudioPlayer(data=fixture("s16le.wav"), output=output,
                              threaded=False)
    try:
        assert player.playable, player.error
        eq(player.sample_rate, 8000)
        eq(player.channels, 2)
        assert abs(player.duration - 0.25) < 1e-9

        assert player.play()
        device.pump(output.ring.backlog)   # the silence `start()` primed
        del device.taken[:]

        for _ in range(12):                # 2400 frames of a 2000-frame file
            player.pump(100)
            output.pump()
            device.pump(200)

        heard = heel.floats_from_float32(bytes(device.taken))
        want = truth16()
        identical(heard[:len(want)], want, "what the device was handed")
        assert all(value == 0.0 for value in heard[len(want):]), \
            "the file ran out and something other than silence followed it"
        assert player.position() > 0.2, \
            "the playhead did not follow the sound: %g" % player.position()
        eq(player.decode_errors, 0)
        eq(player.channel_errors, 0)
    finally:
        player.close()
        output.close()


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
    print(f"\nALL {len(tests)} PCM TESTS PASSED")


if __name__ == "__main__":
    main()
