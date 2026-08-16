#!/usr/bin/env bash
#
# Regenerate the MPEG-1/2/2.5 Layer III test vectors in this directory.
#
# THIS IS A ONE-OFF OFFLINE TOOL. It is not run by test.sh, it is not a
# dependency of anything, and nothing in the browser or the test suite needs
# ffmpeg or lame to be installed. What ships is the output: each `.mp3`
# stream and, beside it, the exact 32-bit float PCM FFmpeg 7.1 decoded it
# to, zlib-deflated. The MP3 tests compare against those bytes and never
# shell out to anything.
#
# It exists because a decoder tested against its own output is tested
# against nothing. Layer III is not a bit-exact specification -- the
# standard defines the IMDCT and the synthesis polyphase filterbank in real
# arithmetic and leaves the arithmetic to the implementation -- so the
# comparison downstream is numerical rather than byte-for-byte. Re-running
# this should reproduce the committed files byte for byte given the same
# tool versions:
#
#     ffmpeg 7.1
#     LAME 3.100 (via ffmpeg's libmp3lame)
#
# A different LAME will make different (still valid) bitstreams, so the
# `.mp3` files change and the `.f32.z` files change with them. That is fine
# -- the pair is what matters -- but do not commit a regenerated `.mp3`
# without regenerating its truth file from the same run.
#
# ---------------------------------------------------------------------
# The one thing here that is not a matter of taste: no Xing/Info frame.
#
# Every vector is muxed with `-write_xing 0`. If the muxer writes the Xing/
# LAME header frame, that frame carries the encoder's delay and padding, and
# FFmpeg's *demuxer* turns them into `skip_samples`/`discard_padding` side
# data and trims the decoded PCM accordingly. The truth file would then be
# "what a gapless player outputs", not "every coded frame decoded from a
# zeroed decoder state" -- and the second is what a from-scratch decoder
# produces and what these fixtures have to be able to say is right. Worse,
# the trim is silent: the vectors would just be a few hundred samples short
# at each end and every comparison would fail by an offset.
#
# So the invariant every vector is checked against, and the reason the
# checker below refuses to write a file that misses it, is exactly:
#
#     decoded samples == coded_frames * samples_per_frame * channels
#
# with samples_per_frame 1152 for MPEG-1 Layer III and 576 for MPEG-2 (LSF)
# and MPEG-2.5. The frames are counted here by walking the frame headers and
# stepping by the length the header implies, not by asking a library.
#
# ID3 is off for the same family of reasons (`-id3v2_version 0
# -write_id3v1 0`): the first byte of every file is 0xFF and the last byte of
# every file is the last byte of the last frame, so a decoder can start at
# offset zero and stop at EOF with no container logic at all. The checker
# asserts both.
#
# Usage: ./make_mp3_vectors.sh [output-directory]

set -euo pipefail
out="${1:-$(cd "$(dirname "$0")" && pwd)}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The frame walker, the invariant check and the zlib pack, in one place.
# Called as: check NAME  (with $out/NAME.mp3 and $work/truth.f32 in place)
#
# It parses the header of every frame -- sync word, version, layer, bitrate
# index, sample rate index, padding, channel mode -- and steps forward by
#
#     (144 or 72) * bitrate / sample_rate + padding
#
# bytes, 144 for MPEG-1 and 72 for the half-size LSF and 2.5 frames. If the
# walk does not land exactly on EOF, or lands on something that is not a
# sync word, the file is not the flat sequence of frames it is supposed to
# be and this fails rather than committing it.
pack() {
  local name="$1"
  python3 - "$out/$name.mp3" "$work/truth.f32" "$out/$name.f32.z" "$name" <<'PY'
import sys, zlib

mp3, f32, packed, name = sys.argv[1:5]
data = open(mp3, "rb").read()

# No ID3v2 ("ID3"), no ID3v1 ("TAG" in the last 128 bytes), first byte is
# sync. A decoder for these files needs no container parsing whatsoever.
assert data[:3] != b"ID3", name + ": ID3v2 tag present"
assert data[-128:-125] != b"TAG", name + ": ID3v1 tag present"
assert data[0] == 0xFF, name + ": does not start with a sync byte"

BR1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None]
BR2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None]
SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}

i, n, frames = 0, len(data), 0
ver = layer = rate = ch = None
while i < n:
    assert i + 4 <= n and data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0, \
        "%s: lost frame sync at byte %d" % (name, i)
    h1, h2, h3 = data[i + 1], data[i + 2], data[i + 3]
    ver, layer = (h1 >> 3) & 3, (h1 >> 1) & 3
    bri, sri, pad = (h2 >> 4) & 15, (h2 >> 2) & 3, (h2 >> 1) & 1
    mode = (h3 >> 6) & 3
    assert layer == 1, "%s: not Layer III" % name
    assert ver != 1, "%s: reserved MPEG version" % name
    assert sri != 3, "%s: reserved sample rate" % name
    # A free-format frame (bitrate index 0) carries no length in its header;
    # nothing here should ever produce one, and if one appeared the walk
    # below would be wrong rather than merely unusual.
    assert bri != 0, "%s: free-format frame at byte %d" % (name, i)
    assert bri != 15, "%s: reserved bitrate index at byte %d" % (name, i)
    rate = SR[ver][sri]
    bitrate = (BR1 if ver == 3 else BR2)[bri] * 1000
    ch = 1 if mode == 3 else 2
    i += (144 if ver == 3 else 72) * bitrate // rate + pad
    frames += 1
assert i == n, "%s: %d bytes of tail after the last frame" % (name, i - n)

spf = 1152 if ver == 3 else 576
raw = open(f32, "rb").read()
got, want = len(raw) // 4, frames * spf * ch
# The whole point of -write_xing 0. If this ever fails, something put a
# Xing/Info/LAME frame back and the demuxer is trimming the truth PCM.
assert got == want, ("%s: decoded %d samples but %d frames * %d * %dch = %d"
                     " -- gapless trimming is being applied"
                     % (name, got, frames, spf, ch, want))

blob = zlib.compress(raw, 9)
open(packed, "wb").write(blob)
print("%-10s MPEG%-3s %2dch %6dHz %4dk  %3d frames * %d * %dch = %d samples"
      "  %6d + %6d bytes"
      % (name, {3: "1", 2: "2", 0: "2.5"}[ver], ch, rate, bitrate // 1000,
         frames, spf, ch, want, len(data), len(blob)))
PY
}

# $1 name  $2 rate  $3 channels  $4 bitrate  $5 lavfi graph
# Anything after that is passed to the encoder, which is how `dual` turns
# joint stereo off.
vector() {
  local name="$1" rate="$2" ch="$3" bitrate="$4" graph="$5"
  shift 5
  ffmpeg -v error -y -f lavfi -i "$graph" -c:a libmp3lame -b:a "$bitrate" \
         -ac "$ch" -ar "$rate" "$@" \
         -write_xing 0 -id3v2_version 0 -write_id3v1 0 "$out/$name.mp3"
  ffmpeg -v error -y -i "$out/$name.mp3" -f f32le -acodec pcm_f32le \
         "$work/truth.f32"
  pack "$name"
}

# ---------------------------------------------------------------------
# MPEG-1 Layer III, 44.1 kHz: the format the web actually has.

# A pure tone. Long blocks from start to end -- a couple of large
# coefficients in the low bands and nothing above them, so the big_values
# region is short, the count1 region is long, and most scalefactor bands
# quantise to zero.
vector tone 44100 1 128k \
  "sine=frequency=440:sample_rate=44100:duration=0.35"

# The opposite: broadband noise, which puts energy in every scalefactor
# band. This is the vector where the wide Huffman tables and the scalefactor
# difference chain actually move, and where region0/region1/region2 all have
# something in them.
vector noise 44100 1 128k \
  "anoisesrc=amplitude=0.5:duration=0.35:sample_rate=44100:color=white:seed=20250813"

# Two tones three hertz apart on the two channels. They are nearly the same
# signal, so mid/side is a real win and LAME takes it: mode_extension says
# M/S in fifteen of this file's seventeen frames. A decoder that ignores the
# stereo tools produces a plausible stereo file that is wrong in both
# channels, and it is wrong in a way a mono vector cannot catch.
vector stereo 44100 2 128k \
  "sine=frequency=440:duration=0.4:sample_rate=44100[a];sine=frequency=443:duration=0.4:sample_rate=44100[b];[a][b]amerge=inputs=2"

# Clicks: a 1 kHz tone gated hard on for 6 ms every 60 ms. The encoder
# switches to three short blocks to keep the quantiser noise inside the
# transient, which forces the whole window-switching machinery into the
# stream -- block_type 2 for the transient itself, block_type 1 (start) in
# the granule before it and block_type 3 (stop) in the granule after, the
# three subblock_gain values, and the short-block scalefactor band layout
# with its 3x interleaved spectrum. That reordering is the part of Layer III
# with the most ways to be subtly wrong and no other vector reaches it.
vector transient 44100 1 128k \
  "aevalsrc=0.9*sin(1000*t)*lt(mod(t\,0.06)\,0.006):d=0.4:s=44100"

# Stereo pink noise at a bitrate far too low for it. Starved of bits, the
# encoder leans on the bit reservoir: main_data_begin is nonzero in nearly
# every frame and runs up into the hundreds of bytes, so the main data for a
# granule lives several frames back and a decoder that assumes main data
# starts after the side info of its own frame decodes garbage. This is the
# vector that says the reservoir is implemented.
vector lowrate 44100 2 32k \
  "anoisesrc=amplitude=0.6:duration=0.35:sample_rate=44100:color=pink:seed=1729"

# The other two MPEG-1 sample rates. Not just a different number in the
# header: each one has its own long-block and short-block scalefactor band
# table, and a transposed table shows up nowhere else.
vector sr48 48000 1 128k \
  "sine=frequency=1000:sample_rate=48000:duration=0.35"

vector sr32 32000 1 128k \
  "anoisesrc=amplitude=0.5:duration=0.35:sample_rate=32000:color=white:seed=31337"

# Plain stereo, joint stereo explicitly off. Two unrelated tones at a
# bitrate with room for both, so mode is 0 (stereo) and mode_extension is
# meaningless: neither M/S nor intensity is in play and the two channels are
# simply coded independently. Every other stereo vector here is joint, so
# without this one the "mode == stereo" branch is never taken.
vector dual 44100 2 192k \
  "sine=frequency=440:duration=0.35:sample_rate=44100[a];sine=frequency=660:duration=0.35:sample_rate=44100[b];[a][b]amerge=inputs=2" \
  -joint_stereo 0

# 320 kbit/s stereo noise: the top of the quantiser's range. The big-value
# Huffman tables above 15 code magnitudes up to 15 and then escape to
# `linbits` extra bits, and a stream this dense is the one whose quantised
# values are large enough to take the escape. It also leaves the count1
# region busy rather than a long run of zeros.
vector hi320 44100 2 320k \
  "anoisesrc=amplitude=0.5:duration=0.3:sample_rate=44100:color=white:seed=9001"

# ---------------------------------------------------------------------
# MPEG-2 LSF and MPEG-2.5. A different decoder in most of the ways that
# matter: one granule per frame instead of two, 576 samples per frame
# instead of 1152, side info 9 bytes mono / 17 stereo instead of 17 / 32,
# main_data_begin 8 bits instead of 9, no scfsi, no preflag, and
# scalefac_compress 9 bits selecting a partitioned scalefactor layout
# instead of 4 bits indexing a pair of slen values. Each sample rate has its
# own band tables on top of that.

# 24 kHz mono: LSF, one granule, the LSF scalefactor partitioning.
vector mp2_24 24000 1 64k \
  "anoisesrc=amplitude=0.5:duration=0.4:sample_rate=24000:color=white:seed=24000"

# 22.05 kHz stereo: LSF with two channels, so the one-granule side info is
# read for both and joint stereo is in play at half rate.
vector mp2_22 22050 2 64k \
  "anoisesrc=amplitude=0.5:duration=0.4:sample_rate=22050:color=white:seed=22050"

# 16 kHz mono: the third LSF band layout.
vector mp2_16 16000 1 48k \
  "anoisesrc=amplitude=0.5:duration=0.4:sample_rate=16000:color=brown:seed=16000"

# MPEG-2.5, which is not in the ISO standard at all -- it is Fraunhofer's
# extension, signalled by the version bits the standard calls reserved, and
# it exists only to get below 16 kHz.
vector mp25_11 11025 1 32k \
  "anoisesrc=amplitude=0.5:duration=0.4:sample_rate=11025:color=white:seed=11025"

# 12 kHz: the ninth and last sampling frequency, so that the header field
# that selects it is decoded somewhere rather than only read. Its band
# tables are not new -- 16 kHz, 11.025 kHz and 12 kHz share one layout, long
# and short alike -- so what this vector adds is the rate, not a table.
vector mp25_12 12000 1 32k \
  "anoisesrc=amplitude=0.5:duration=0.4:sample_rate=12000:color=white:seed=12000"

# 8 kHz: the shortest band table there is.
vector mp25_8 8000 1 24k \
  "sine=frequency=800:sample_rate=8000:duration=0.4"

# ---------------------------------------------------------------------
# The two synthesised vectors: intensity stereo.
#
# These two are not encoder output and it would be dishonest to present them
# as if they were. No encoder available here emits intensity stereo -- LAME
# never has, at any setting, in any version; it implements M/S and nothing
# else. Intensity stereo is nevertheless in the format, real files use it,
# and a decoder that gets it wrong is wrong on those files. So these two
# vectors are made by patching a joint-stereo encode:
#
#   * take an ordinary libmp3lame joint-stereo file,
#   * set the two mode_extension bits (header byte 3, bits 5..4) on every
#     frame to signal intensity stereo,
#   * change nothing else -- not one bit of main data, not one frame length.
#
# That is a legal bitstream. mode_extension is a decoder instruction, not a
# description of what the encoder did: with the intensity bit set, the right
# channel's scalefactors above the last nonzero band are read as intensity
# positions and the two channels are rebuilt from the left channel and those
# positions. The bits being reinterpreted were written as scalefactors, so
# the audio that comes out is not the audio that went in -- it is noise with
# a stereo image. That does not matter in the least. The truth PCM is FFmpeg
# decoding *these very bytes*, so the comparison is exactly as honest as
# every other vector here: two decoders, one bitstream, do they agree.
#
# The encode is done without the CRC, which is libmp3lame's default. That is
# deliberate and the patcher asserts it: with protection_bit == 1 there is
# no 16-bit checksum over the header and side info, so flipping header bits
# needs no checksum recomputed and the patch is genuinely two bits per frame.
#
# Both patched files are then decoded by FFmpeg with `-v error`, which must
# print nothing and exit 0, and both go through the same frame-count check
# as everything else.

# $1 name  $2 mode_extension  $3.. the encode (rate, channels, bitrate, graph)
patched_vector() {
  local name="$1" me="$2" rate="$3" ch="$4" bitrate="$5" graph="$6"
  ffmpeg -v error -y -f lavfi -i "$graph" -c:a libmp3lame -b:a "$bitrate" \
         -ac "$ch" -ar "$rate" \
         -write_xing 0 -id3v2_version 0 -write_id3v1 0 "$work/src.mp3"
  python3 - "$work/src.mp3" "$out/$name.mp3" "$me" "$name" <<'PY'
import sys

src, dst, me, name = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
data = bytearray(open(src, "rb").read())

BR1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None]
BR2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None]
SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}

i, n, frames = 0, len(data), 0
while i < n:
    assert data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0, \
        "%s: lost frame sync at byte %d" % (name, i)
    h1, h2, h3 = data[i + 1], data[i + 2], data[i + 3]
    ver, prot = (h1 >> 3) & 3, h1 & 1
    bri, sri, pad = (h2 >> 4) & 15, (h2 >> 2) & 3, (h2 >> 1) & 1
    # protection_bit == 1 means "no CRC", so no checksum covers the bits we
    # are about to change. If LAME ever starts writing the CRC by default
    # this stops rather than silently producing a stream with a stale one.
    assert prot == 1, "%s: frame at %d has a CRC; patch would invalidate it" % (name, i)
    assert ((h3 >> 6) & 3) == 1, "%s: frame at %d is not joint stereo" % (name, i)
    data[i + 3] = (h3 & 0xCF) | (me << 4)
    bitrate = (BR1 if ver == 3 else BR2)[bri] * 1000
    i += (144 if ver == 3 else 72) * bitrate // SR[ver][sri] + pad
    frames += 1
assert i == n, "%s: %d bytes of tail after the last frame" % (name, i - n)
open(dst, "wb").write(bytes(data))
print("%-10s patched mode_extension=%d on all %d frames" % (name, me, frames))
PY
  # -v error prints nothing and exits 0, or this fails: the patched stream
  # has to be one a reference decoder accepts without complaint, not merely
  # one it survives.
  local err
  err="$(ffmpeg -v error -y -i "$out/$name.mp3" -f f32le -acodec pcm_f32le \
         "$work/truth.f32" 2>&1 >/dev/null)"
  if [ -n "$err" ]; then
    echo "$name: ffmpeg complained about the patched stream:" >&2
    echo "$err" >&2
    exit 1
  fi
  pack "$name"
}

# MPEG-1 intensity stereo, mode_extension = 1: the intensity bit alone, with
# M/S off, so the intensity path is exercised on its own rather than layered
# under a mid/side inverse. MPEG-1 derives the two channels from a table of
# tan(is_pos * pi/12) with is_pos == 7 meaning "invalid, leave the band
# alone", and that table is used nowhere else in the format.
patched_vector intensity 1 44100 2 128k \
  "sine=frequency=440:duration=0.4:sample_rate=44100[a];sine=frequency=443:duration=0.4:sample_rate=44100[b];[a][b]amerge=inputs=2"

# MPEG-2 LSF intensity stereo, from a 22.05 kHz encode. This is a completely
# different derivation from the MPEG-1 one above -- not the tangent table at
# all, but a power-of-two law driven by is_pos and by the intensity scale
# taken from the low bit of scalefac_compress, with the odd positions
# scaling one channel and the even positions scaling neither. Sharing code
# between the two is the obvious mistake and this vector is what catches it.
patched_vector lsfint 1 22050 2 64k \
  "sine=frequency=440:duration=0.4:sample_rate=22050[a];sine=frequency=443:duration=0.4:sample_rate=22050[b];[a][b]amerge=inputs=2"

# ---------------------------------------------------------------------
# The hand-written vector: mixed blocks and the CRC.
#
# Two parts of the format no encoder here will produce at all, rather than
# two an encoder produces only under the right signal:
#
#   * mixed_block_flag. LAME has the switch and has never turned it on, and
#     nothing else in common use emits it either. A mixed block keeps the
#     long window on the lowest two subbands and goes short above them, so
#     one granule carries two scalefactor band layouts at once: eight long
#     bands and short bands 3..5 at slen1, short bands 6..11 at slen2, and
#     a reorder that starts at coefficient 36 instead of at zero.
#
#   * the 16-bit CRC over the header and side information. protection_bit
#     is 1 in every byte libmp3lame has ever written.
#
# So this stream is assembled here, field by field, with no encoder in the
# loop: eight MPEG-1 mono 44.1 kHz frames, every granule a mixed block,
# alternate frames carrying the checksum so that both sides of
# protection_bit are decoded from one file. The spectrum is thirty
# big-value pairs -- eighteen under table 1 and twelve under table 2, so
# the implied region boundary at coefficient 36 separates two different
# tables and a decoder that puts it elsewhere decodes different audio --
# followed by twenty-five count1 quadruples, reaching coefficient 160.
#
# What makes it a vector and not a self-fulfilling prophecy is the same
# thing that makes every other file here one: the truth beside it is
# FFmpeg decoding these very bytes. Two independent decoders, one
# bitstream, and the question is whether they agree. If the frames were
# malformed FFmpeg would refuse them, which is why `-v error` must stay
# silent below.
mixed_vector() {
  local name="$1"
  python3 - "$out/$name.mp3" "$name" <<'PY'
import sys

dst, name = sys.argv[1], sys.argv[2]

# ISO Table 3-B.4: scalefac_compress to (slen1, slen2).
SLEN = [(0, 0), (0, 1), (0, 2), (0, 3), (3, 0), (1, 1), (1, 2), (1, 3),
        (2, 1), (2, 2), (2, 3), (3, 1), (3, 2), (3, 3), (4, 2), (4, 3)]
# ISO Table 3-B.7, tables 1 and 2, as (length, codeword) indexed x*ylen+y,
# and their dimensions.
CODES = {1: [(1, 1), (3, 1), (2, 1), (3, 0)],
         2: [(1, 1), (3, 2), (6, 1), (3, 3), (3, 1), (5, 1), (5, 3), (5, 2),
             (6, 0)]}
DIMS = {1: (2, 2), 2: (3, 3)}
# ISO Table 3-B.7 table A, indexed v*8 + w*4 + x*2 + y.
COUNT1_A = [(1, 1), (4, 5), (4, 4), (5, 5), (4, 6), (6, 5), (5, 4), (6, 4),
            (4, 7), (5, 3), (5, 6), (6, 0), (5, 7), (6, 2), (6, 3), (6, 1)]


class Bits:
    def __init__(self):
        self.acc, self.n = 0, 0

    def put(self, value, width):
        assert 0 <= value < (1 << width), (value, width)
        self.acc = (self.acc << width) | value
        self.n += width
        return self

    def bytes(self, pad_to=None):
        acc, n = self.acc, self.n
        if n % 8:
            acc <<= 8 - n % 8
            n += 8 - n % 8
        out = acc.to_bytes(n // 8, "big")
        return out + b"\0" * (pad_to - len(out)) if pad_to else out


def crc16(payload):
    """ISO 11172-3 2.4.3.1: x^16 + x^15 + x^2 + 1, register all ones, fed
    most significant bit first."""
    reg = 0xFFFF
    for byte in payload:
        for k in range(7, -1, -1):
            carry = (reg >> 15) & 1
            reg = (reg << 1) & 0xFFFF
            if carry != ((byte >> k) & 1):
                reg ^= 0x8005
    return reg


bitrate, rate = 128000, 44100
frame_len = (144 * bitrate) // rate                   # 417, no padding
side_len = 17                                         # MPEG-1 mono
compress = 5                                          # slen1 = slen2 = 1
table0, table1, boundary = 1, 2, 36

pairs = [((i % DIMS[table0][0]) * (1 if i % 3 else -1),
          (i % DIMS[table0][1]) * (1 if i % 4 else -1)) for i in range(18)]
pairs += [((i % DIMS[table1][0]) * (1 if i % 3 else -1),
           (i % DIMS[table1][1]) * (1 if i % 5 else -1)) for i in range(12)]
quads = [(1 if i % 2 == 0 else 0, 1 if i % 3 == 0 else 0,
          -1 if i % 5 == 0 else 0, 1 if i % 7 == 0 else 0) for i in range(25)]

body = Bits()
slen1, slen2 = SLEN[compress]
for i in range(17):
    body.put((i % 2) & ((1 << slen1) - 1), slen1)
for i in range(17, 35):
    body.put((i % 2) & ((1 << slen2) - 1), slen2)
for index, (x, y) in enumerate(pairs):
    table = table0 if index * 2 < boundary else table1
    xlen, ylen = DIMS[table]
    assert abs(x) < xlen and abs(y) < ylen, (table, x, y)
    length, code = CODES[table][abs(x) * ylen + abs(y)]
    body.put(code, length)
    for value in (x, y):
        if value:
            body.put(0 if value > 0 else 1, 1)
for quad in quads:
    index = 0
    for value in quad:
        index = (index << 1) | (1 if value else 0)
    length, code = COUNT1_A[index]
    body.put(code, length)
    for value in quad:
        if value:
            body.put(0 if value > 0 else 1, 1)

out = bytearray()
for number in range(8):
    protect = 0 if number % 2 else 1
    header = Bits()
    header.put(0x7FF, 11).put(3, 2).put(1, 2).put(protect, 1)
    header.put(9, 4).put(0, 2).put(0, 1).put(0, 1)
    header.put(3, 2).put(0, 2).put(0, 1).put(0, 1).put(0, 2)

    side = Bits()
    side.put(0, 9)                                    # main_data_begin
    side.put(0, 5)                                    # private_bits
    side.put(0, 4)                                    # scfsi, mono
    for _granule in range(2):
        side.put(body.n, 12)                          # part2_3_length
        side.put(len(pairs), 9)                       # big_values
        side.put(180, 8)                              # global_gain
        side.put(compress, 4)                         # scalefac_compress
        side.put(1, 1)                                # window_switching_flag
        side.put(2, 2)                                # block_type: short
        side.put(1, 1)                                # mixed_block_flag
        side.put(table0, 5).put(table1, 5)            # table_select
        side.put(0, 3).put(1, 3).put(2, 3)            # subblock_gain
        side.put(0, 1)                                # preflag
        side.put(1, 1)                                # scalefac_scale
        side.put(0, 1)                                # count1table_select
    assert side.n == side_len * 8, "%s: side info is %d bits" % (name, side.n)

    main = Bits()
    main.put(body.acc, body.n)
    main.put(body.acc, body.n)
    room = frame_len - 4 - (1 - protect) * 2 - side_len
    assert main.n <= room * 8, "%s: main data does not fit" % name

    head, info = header.bytes(), side.bytes()
    # The checksum covers the last two bytes of the header and the whole of
    # the side information, and nothing else. The main data is unprotected
    # because by the time it is read it may have come from an earlier frame.
    check = crc16(head[2:] + info).to_bytes(2, "big") if protect == 0 else b""
    frame = head + check + info + main.bytes(pad_to=room)
    assert len(frame) == frame_len, "%s: frame is %d bytes" % (name, len(frame))
    out += frame

open(dst, "wb").write(bytes(out))
print("%-10s hand-written, %d frames, part2_3_length = %d bits, 4 with a CRC"
      % (name, 8, body.n))
PY
  local err
  err="$(ffmpeg -v error -y -i "$out/$name.mp3" -f f32le -acodec pcm_f32le \
         "$work/truth.f32" 2>&1 >/dev/null)"
  if [ -n "$err" ]; then
    echo "$name: ffmpeg complained about the hand-written stream:" >&2
    echo "$err" >&2
    exit 1
  fi
  pack "$name"
}

mixed_vector mixed

# ---------------------------------------------------------------------
# What these eighteen vectors do NOT reach, measured rather than guessed.
# A threshold proves nothing about code no vector exercises, so this list
# is part of the fixtures and should be kept true if they are regenerated.
#
#   * mixed_block_flag is set in zero granule-channels across the sixteen
#     encoded vectors, and `mixed` above exists only because of that. Read
#     that way round: the mixed-block path is covered, but by a stream this
#     script assembles rather than by anything an encoder in circulation
#     produces.
#
#   * Free-format frames (bitrate index 0) do not appear and the checker
#     above refuses them. The rest of the format's edges are here: all
#     thirty of the Huffman tables that exist are selected by some vector
#     (4 and 14 are the two the standard leaves undefined), count1 uses
#     both table A and table B, both scalefac_scale values occur, preflag
#     occurs, and block types 1, 2 and 3 all appear in every single vector
#     -- even the pure tones, whose onset and end are transients as far as
#     the encoder is concerned. `transient` is still the vector that puts
#     window switching in most of its granules (25 of 34) rather than in
#     the six at the edges.
#
#   * The CRC is written by nothing libmp3lame produces: protection_bit is
#     1 in all sixteen encoded vectors, and only the hand-written `mixed`
#     has frames that carry the checksum -- four of its eight.
#
#   * No vector has a frame whose CRC is wrong. A decoder has to reject
#     one, but a file a reference decoder also rejects has no truth PCM to
#     compare against, so that belongs in a test that corrupts a byte of
#     `mixed` in memory rather than in a file on disk.
#
#   * Nothing is Layer I or Layer II. These are Layer III vectors only.
