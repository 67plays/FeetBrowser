#!/usr/bin/env bash
#
# Regenerate the PCM test vectors in this directory.
#
# THIS IS A ONE-OFF OFFLINE TOOL. It is not run by test.sh, it is not a
# dependency of anything, and nothing in the browser or the test suite needs
# ffmpeg to be installed. What ships is the output: the source waveform, and
# the same waveform written out by a real muxer into every container and
# every sample format the PCM path claims to read. tests/test_pcm.py reads
# those bytes and never shells out to anything.
#
# It exists because PCM is byte order and sample width, and both of those
# have exactly one failure mode that matters: a file that decodes to
# something. A wrong endianness, a wrong sign convention or a wrong width
# does not raise and does not fall silent -- it produces samples that look
# like sound, plot like sound and are not the sound in the file. The only
# way to catch that is to compare against a waveform whose every sample is
# known, which is what `tone.s16le.z` is.
#
# Everything below is derived from `tone.s16le` by a conversion that is
# exact, which is the property the whole suite leans on:
#
#   s16 -> s24 is a shift left by 8, s16 -> s32 a shift left by 16, and
#   s16 -> f32/f64 a divide by 32768. Our decoder scales an n-bit sample by
#   2^-(n-1). So every one of those files must decode to *bit-identical*
#   floats, and the test asserts equality rather than a tolerance.
#
# The two exceptions are named where they occur: `u8` is a genuine
# requantisation and gets its own truth file, and `mulaw`/`adpcm` are there
# to be refused rather than decoded.
#
# Reproducing this needs:
#
#     ffmpeg 7.1
#
# A different FFmpeg may lay its boxes out differently, which is fine -- the
# files are read for their content, not their byte offsets -- but do not
# commit a regenerated container without regenerating the truth files from
# the same run.
#
# Usage: ./make_pcm_vectors.sh [output-directory]

set -euo pipefail
out="${1:-$(cd "$(dirname "$0")" && pwd)}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

RATE=8000
DUR=0.25

# Two different tones, one per channel, so that a decoder which swaps the
# channels fails as loudly as one which swaps the bytes. 8 kHz keeps the
# fixtures small; 0.25 s is three of the tenth-of-a-second blocks the demuxer
# cuts, plus a short last one, which is the case where an off-by-one in the
# block cutting shows up.
ffmpeg -y -v error \
  -f lavfi -i "sine=frequency=400:sample_rate=$RATE:duration=$DUR" \
  -f lavfi -i "sine=frequency=1300:sample_rate=$RATE:duration=$DUR" \
  -filter_complex "[0:a][1:a]amerge=inputs=2,volume=0.8[a]" -map "[a]" \
  -c:a pcm_s16le -f s16le "$work/tone.s16le"

# The 8-bit truth, which is a real requantisation of the above and so cannot
# be derived from it by the rule at the top of this file.
ffmpeg -y -v error -f s16le -ar $RATE -ac 2 -i "$work/tone.s16le" \
  -c:a pcm_u8 -f u8 "$work/tone.u8"

src=(-f s16le -ar $RATE -ac 2 -i "$work/tone.s16le")

# QuickTime. Each fourcc is a different (width, byte order, kind) and the
# sample entry version differs across them, which is the point:
#   sowt/twos/raw  version 0, described by the fourcc and `sampleSize` alone
#   in24/in32/fl32/fl64  version 1, whose `sampleSize` says 16 whatever the
#                        real width is -- the width is bytesPerPacket
#   in24le         the same `in24` fourcc with an `enda` box saying little,
#                  which is the only thing standing between it and being
#                  decoded backwards
#   lpcm96         version 2, where the fourcc says nothing at all and the
#                  format flags say everything. Written at 96 kHz because
#                  that is what makes FFmpeg reach for version 2; the samples
#                  are the same bytes, read at a different rate.
ffmpeg -y -v error "${src[@]}" -c:a pcm_s16le "$out/sowt.mov"
ffmpeg -y -v error "${src[@]}" -c:a pcm_s16be "$out/twos.mov"
ffmpeg -y -v error "${src[@]}" -c:a pcm_u8    "$out/raw.mov"
ffmpeg -y -v error "${src[@]}" -c:a pcm_s24be "$out/in24.mov"
ffmpeg -y -v error "${src[@]}" -c:a pcm_s24le "$out/in24le.mov"
ffmpeg -y -v error "${src[@]}" -c:a pcm_s32be "$out/in32.mov"
ffmpeg -y -v error "${src[@]}" -c:a pcm_f32be "$out/fl32.mov"
ffmpeg -y -v error "${src[@]}" -c:a pcm_f64be "$out/fl64.mov"
ffmpeg -y -v error -f s16le -ar 96000 -ac 2 -i "$work/tone.s16le" \
  -c:a pcm_s16le "$out/lpcm96.mov"

# WAV. Tag 1 for the integer widths, tag 3 for float -- and tag 0xFFFE,
# WAVE_FORMAT_EXTENSIBLE, for the last one, which is a second parse of the
# same chunk and not a detail. What decides is whether the input declares a
# channel layout: with one, FFmpeg has a channel mask to write and reaches
# for the extended header above sixteen bits; the raw `s16le` input above
# declares none, which is why `s24le.wav` is plain tag 1 and `s24ext.wav`,
# the same samples with `-channel_layout stereo`, is not.
ffmpeg -y -v error "${src[@]}" -c:a pcm_s16le "$out/s16le.wav"
ffmpeg -y -v error "${src[@]}" -c:a pcm_u8    "$out/u8.wav"
ffmpeg -y -v error "${src[@]}" -c:a pcm_s24le "$out/s24le.wav"
ffmpeg -y -v error "${src[@]}" -c:a pcm_f32le "$out/f32le.wav"
ffmpeg -y -v error -f s16le -ar $RATE -ac 2 -channel_layout stereo \
  -i "$work/tone.s16le" -c:a pcm_s24le "$out/s24ext.wav"

# The two that must be refused by name. Both are ordinary files that an
# ordinary tool writes, and both are a codec rather than a packing.
ffmpeg -y -v error "${src[@]}" -c:a pcm_mulaw "$out/mulaw.wav"
ffmpeg -y -v error "${src[@]}" -c:a adpcm_ms  "$out/adpcm.wav"

# AVI, with a picture beside the sound, because that is the whole reason to
# demux an AVI's audio: this browser already decodes the MJPEG in this file
# and until now played it silently. `-shortest` keeps the two the same
# length so the last `01wb` chunk is not a tail nobody sees.
ffmpeg -y -v error \
  -f lavfi -i "testsrc=size=32x24:rate=25:duration=$DUR" "${src[@]}" \
  -c:v mjpeg -q:v 5 -c:a pcm_s16le -shortest "$out/pcm.avi"
ffmpeg -y -v error "${src[@]}" -c:a pcm_u8 -f avi "$out/u8.avi"

python3 - "$work" "$out" <<'PY'
import sys, zlib
work, out = sys.argv[1], sys.argv[2]
for name in ("tone.s16le", "tone.u8"):
    with open("%s/%s" % (work, name), "rb") as handle:
        raw = handle.read()
    with open("%s/%s.z" % (out, name), "wb") as handle:
        handle.write(zlib.compress(raw, 9))
    print("  %-14s %6d bytes -> %d deflated" % (name, len(raw),
                                                len(zlib.compress(raw, 9))))
PY

ls -l "$out"
