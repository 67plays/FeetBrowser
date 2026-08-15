#!/usr/bin/env bash
#
# Regenerate the AAC-LC test vectors in this directory.
#
# THIS IS A ONE-OFF OFFLINE TOOL. It is not run by test.sh, it is not a
# dependency of anything, and nothing in the browser or the test suite needs
# ffmpeg to be installed. What ships is the output: each `.aac` (ADTS)
# stream and, beside it, the exact 32-bit float PCM FFmpeg 7.1 decoded it
# to, zlib-deflated. tests/test_aac.py compares against those bytes and
# never shells out to anything.
#
# It exists because a decoder tested against its own output is tested
# against nothing. AAC is not a bit-exact specification the way H.264 is --
# the standard defines the transform in real arithmetic and leaves the
# arithmetic to the implementation -- so the comparison downstream is
# numerical rather than byte-for-byte, and the threshold it uses is
# justified from the numbers measured here. Re-running this should reproduce
# the committed files byte for byte given the same tool version:
#
#     ffmpeg 7.1
#
# A different FFmpeg will make different (still valid) bitstreams, so the
# `.aac` files change and the `.f32.z` files change with them. That is fine
# -- the pair is what matters -- but do not commit a regenerated `.aac`
# without regenerating its truth file from the same run.
#
# One thing here is not a matter of taste. Perceptual noise substitution
# codes a band as "noise of this energy" and leaves the noise itself to the
# decoder, so a PNS band has no correct sample values, only a correct
# spectrum. The instep decoder reproduces FFmpeg's noise generator and its
# seed exactly (see IPRAND in fortran/instdsp.f), which is the only reason
# the PNS bands in these vectors can be compared at all. Streams encoded by
# something else would agree everywhere except there.
#
# Usage: ./make_aac_vectors.sh [output-directory]

set -euo pipefail
out="${1:-$(cd "$(dirname "$0")" && pwd)}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# $1 name  $2 rate  $3 channels  $4 bitrate  $5 lavfi graph
# Anything after that is passed to the encoder, which is how the vectors
# below choose a different encoder or turn a tool off.
vector() {
  local name="$1" rate="$2" ch="$3" bitrate="$4" graph="$5"
  shift 5
  ffmpeg -v error -y -f lavfi -i "$graph" -c:a aac -b:a "$bitrate" \
         -ac "$ch" -ar "$rate" "$@" "$out/$name.aac"
  ffmpeg -v error -y -i "$out/$name.aac" -f f32le -acodec pcm_f32le \
         "$work/truth.f32"
  python3 -c 'import sys, zlib
raw = open(sys.argv[1], "rb").read()
packed = zlib.compress(raw, 9)
open(sys.argv[2], "wb").write(packed)
print("%-10s %2dch %6dHz %5s  %7d -> %6d bytes  %.3f s"
      % (sys.argv[3], int(sys.argv[5]), int(sys.argv[4]), sys.argv[6],
         len(raw), len(packed),
         len(raw) / 4.0 / int(sys.argv[5]) / int(sys.argv[4])))' \
      "$work/truth.f32" "$out/$name.f32.z" "$name" "$rate" "$ch" "$bitrate"
}

# A pure tone. Long windows from start to end, a handful of large
# coefficients and nothing else, and -- because the encoder has nothing to
# spend bits on above the tone -- perceptual noise substitution in almost
# every frame.
vector tone 44100 1 96k \
  "sine=frequency=440:sample_rate=44100:duration=0.35"

# The opposite: broadband noise, which spreads energy over every scalefactor
# band, drives the wide codebooks and makes the scalefactor difference chain
# actually move.
vector noise 44100 1 128k \
  "anoisesrc=amplitude=0.5:duration=0.3:sample_rate=44100:color=white"

# Two tones five hertz apart on the two channels. They are nearly the same
# signal, so mid/side is a real win and the encoder takes it: ms_mask_present
# is set in every frame of this file and intensity stereo appears in the top
# bands. A decoder that ignores the stereo tools produces a plausible stereo
# file that is wrong in both channels.
vector stereo 44100 2 128k \
  "sine=frequency=300:duration=0.45[a];sine=frequency=305:duration=0.45[b];[a][b]amerge=inputs=2"

# Clicks. The encoder switches to eight short windows to keep the quantiser
# noise inside the transient, which means LONG_START before it and LONG_STOP
# after it, window grouping, and the interleaved spectrum -- the part of the
# format with the most ways to be subtly wrong.
vector transient 44100 1 128k \
  "aevalsrc=0.9*sin(1000*t)*lt(mod(t\,0.2)\,0.02):d=0.6:s=44100"

# Stereo pink noise at a bitrate too low for it. Starved of bits the encoder
# reaches for everything it has: temporal noise shaping, mid/side over most
# of the spectrum, intensity stereo at the top.
vector lowrate 44100 2 32k \
  "anoisesrc=amplitude=0.6:duration=0.3:sample_rate=44100:color=pink"

# 48 kHz, the other rate the web actually uses. A different scalefactor band
# layout, not just a different number in the header.
vector sr48 48000 1 96k \
  "sine=frequency=1000:sample_rate=48000:duration=0.35"

# 16 kHz and 8 kHz: the low-rate layouts, where a long block has 43 and 40
# scalefactor bands instead of 49 and the short-block tables change too.
vector sr16 16000 1 32k \
  "anoisesrc=amplitude=0.5:duration=0.4:sample_rate=16000:color=brown"

vector sr8 8000 1 24k \
  "sine=frequency=800:sample_rate=8000:duration=0.4"

# 320 kbit/s stereo noise: the top of the quantiser's range. Codebook 11 is
# the only one with an escape, and an escape is a run of N ones followed by
# N+4 bits; this is the vector whose coefficients run into the thousands and
# so the only one where N gets as far as eight.
vector hi320 44100 2 320k \
  "anoisesrc=amplitude=0.5:duration=0.25:sample_rate=44100:color=white"

# The four scalefactor band layouts none of the vectors above reaches. The
# standard has eight distinct long-block tables, keyed by sample rate:
# 96/88.2, 64, 48, 44.1, 32, 24/22.05, 16/12/11.025 and 8/7.35 kHz. Without
# these four, half of them were never decoded and a transposed table would
# have shown up only on somebody's actual file.
vector sr96 96000 1 128k \
  "anoisesrc=amplitude=0.5:duration=0.12:sample_rate=96000:color=white"

vector sr64 64000 1 96k \
  "anoisesrc=amplitude=0.5:duration=0.15:sample_rate=64000:color=pink"

vector sr32 32000 1 96k \
  "anoisesrc=amplitude=0.5:duration=0.2:sample_rate=32000:color=white"

vector sr24 24000 1 48k \
  "anoisesrc=amplitude=0.5:duration=0.25:sample_rate=24000:color=brown"

# Temporal noise shaping, from a different encoder.
#
# FFmpeg's own AAC encoder has -aac_tns on by default and almost never
# decides to use it: across every vector above, two channel-frames out of
# 203 set tns_data_present, and both of those signal no filters. Deleting
# the call to the TNS filter entirely changed not one sample of any of them.
# So the filter was shipping untested, and a vector from an encoder that
# does use it is the only way to say otherwise -- this one puts a real
# filter in a quarter of its frames, and without it the decode is 28 dB
# wrong instead of 140 dB right.
#
# aac_at is Apple's AudioToolbox encoder and exists only on macOS, so this
# is the one vector this script cannot regenerate on Linux. It is committed
# like all the others and nothing at test time needs the encoder.
if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q ' aac_at '; then
  vector tns 44100 1 128k \
    "aevalsrc=0.9*sin(1000*t)*lt(mod(t\,0.2)\,0.02):d=0.35:s=44100" \
    -c:a aac_at
else
  echo "tns: skipped -- no aac_at encoder (macOS only); keeping the" \
       "committed vector" >&2
fi

# The same coded frames in an MP4, for the container path: no ADTS headers,
# an AudioSpecificConfig in an `esds` box, and per-sample timing from `stts`.
# `-c copy` because the point is that the two paths decode the same frames to
# the same samples, which is only a test if the frames really are the same.
ffmpeg -v error -y -i "$out/stereo.aac" -c copy "$out/stereo.mp4"
echo "stereo.mp4 $(wc -c < "$out/stereo.mp4") bytes"

# The two configurations ffmpeg writes for one encode, as a pair.
#
# Muxing straight into MP4 writes an AudioSpecificConfig with the backward
# compatible SBR signalling appended -- 121056e500 -- whether or not the
# encoder used SBR; the flag at the end of it is what says whether it did,
# and here it is clear. Remuxing the same coded frames from ADTS writes the
# bare 1210 instead. Both are AAC-LC, both must decode, and they must decode
# to the same samples: that is the whole point of the pair. Short and quiet
# because nothing here is compared against a reference, only against itself.
ffmpeg -v error -y -f lavfi \
  -i "sine=frequency=440:sample_rate=44100:duration=0.5" \
  -ac 2 -c:a aac -b:a 64k "$out/sbr_signalled.mp4"
ffmpeg -v error -y -f lavfi \
  -i "sine=frequency=440:sample_rate=44100:duration=0.5" \
  -ac 2 -c:a aac -b:a 64k -f adts "$out/sbr_pair.aac"
ffmpeg -v error -y -i "$out/sbr_pair.aac" -c copy "$out/sbr_absent.mp4"
rm -f "$out/sbr_pair.aac"
echo "sbr_signalled.mp4 $(wc -c < "$out/sbr_signalled.mp4") bytes," \
     "sbr_absent.mp4 $(wc -c < "$out/sbr_absent.mp4") bytes"
