#!/usr/bin/env bash
#
# Regenerate the inter-coded (P slice) test vectors in this directory.
#
# THIS IS A ONE-OFF OFFLINE TOOL. It is not run by test.sh, it is not a
# dependency of anything, and nothing in the browser or the test suite needs
# ffmpeg or x264 to be installed. What ships is the output: each `.264`
# stream and, beside it, the exact I420 bytes FFmpeg 7.1 decoded it to,
# zlib-deflated. tests/test_h264.py compares against those bytes and never
# shells out to anything.
#
# It exists because a decoder tested against its own output is tested against
# nothing, and because six months from now somebody will want to know what
# these streams actually are. Re-running it should reproduce the committed
# files byte for byte given the same tool versions:
#
#     ffmpeg 7.1
#     x264 0.164.3108
#
# A different x264 will make different (still valid) bitstreams, so the
# `.264` files change and the `.i420.z` files change with them. That is fine
# -- the pair is what matters -- but do not commit a regenerated `.264`
# without regenerating its truth file from the same run.
#
# The nine intra-only vectors (mb1, mb4, qcif-*, crop, tiny-crop, and the
# qcif-cavlc refusal case) predate this script and are not reproduced by it.
#
# Usage: ./make_inter_vectors.sh [output-directory]

set -euo pipefail
out="${1:-$(cd "$(dirname "$0")" && pwd)}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Common encoder settings. --bframes 0 because B slices are out of scope;
# --keyint infinite --no-scenecut so that exactly one IDR appears and every
# other frame is a P frame (a second IDR would silently hide a reference
# picture bug behind a fresh start); --tune psnr to keep psy-rd from making
# the mode decisions depend on x264's visual model.
common="--bframes 0 --keyint infinite --no-scenecut --tune psnr --quiet"

# $1 name  $2 WxH  $3 frames  $4 lavfi graph  $5 extra x264 options
vector() {
  local name="$1" size="$2" frames="$3" graph="$4" opts="$5"
  local w="${size%x*}" h="${size#*x}"
  ffmpeg -v error -y -f lavfi -i "$graph" -vf "format=yuv420p,scale=$w:$h" \
         -frames:v "$frames" -f rawvideo "$work/src.yuv"
  x264 --input-res "$size" --fps 25 --input-csp i420 $common $opts \
       -o "$out/$name.264" "$work/src.yuv"
  ffmpeg -v error -y -i "$out/$name.264" -pix_fmt yuv420p \
         -f rawvideo "$work/truth.i420"
  python3 -c 'import sys, zlib
raw = open(sys.argv[1], "rb").read()
open(sys.argv[2], "wb").write(zlib.compress(raw, 9))
print("%-14s %s %2d frames  %7d -> %6d bytes"
      % (sys.argv[3], sys.argv[4], int(sys.argv[5]), len(raw),
         len(zlib.compress(raw, 9))))' \
      "$work/truth.i420" "$out/$name.i420.z" "$name" "$size" "$frames"
}

# A plain I frame followed by P frames, one reference, no fancy partitions:
# the shape of the smallest useful inter stream there is.
vector p-basic 128x96 8 \
  "testsrc2=size=256x192:rate=25" \
  "--ref 1 --partitions p16x16 --no-8x8dct --qp 26 --weightp 0"

# A still background with one small thing moving across it. Most of every
# frame is P_Skip, which is its own code path: no residual, no motion vector
# in the bitstream, and a predicted vector that has to come out right anyway.
vector p-skip 128x96 10 \
  "color=c=0x1a3050:size=128x96:rate=25[bg];testsrc2=size=24x24:rate=25[fg];[bg][fg]overlay=x='8+t*90':y=40" \
  "--ref 1 --partitions all --no-8x8dct --qp 24 --weightp 0"

# Detail that moves in several directions at once, encoded with every
# partition shape enabled and a motion search good enough to use them. This
# is the vector that exercises sub_mb_type and the 4x4 partitions.
vector p-sub8x8 128x96 8 \
  "mandelbrot=size=128x96:rate=25:maxiter=200" \
  "--ref 2 --partitions all --8x8dct --subme 9 --me umh --qp 22"

# Content that goes back and forth, so that the best match for a block is
# often two or three pictures back rather than one. ref_idx_l0 is then a real
# syntax element rather than a constant zero.
vector p-multiref 112x80 10 \
  "testsrc2=size=224x160:rate=25,rotate=a='0.35*sin(2*PI*t*3)':c=black" \
  "--ref 4 --partitions all --no-8x8dct --subme 9 --me umh --qp 24 --weightp 0"

# The picture pans off its own edges, so the motion search finds its matches
# outside the reference picture and the interpolator has to clamp. Every
# decoder that gets this wrong gets it wrong at the border, and 8.4.2.2.1
# clamps the *sample coordinates*, not the vector, which is the distinction
# this vector is here to hold us to.
vector p-edge 112x80 10 \
  "color=c=gray:size=112x80:rate=25[bg];testsrc2=size=176x128:rate=25[fg];[bg][fg]overlay=x='-52+t*220':y='-30+t*140'" \
  "--ref 2 --partitions all --no-8x8dct --me esa --merange 40 --qp 24 --weightp 0"

# A fade, encoded with weighted prediction on. luma_log2_weight_denom and the
# per-reference weights then appear in the slice header and 8.4.2.3 has to be
# applied to every predicted sample.
vector p-weightp 112x80 10 \
  "testsrc2=size=112x80:rate=25,fade=t=out:st=0:d=0.4" \
  "--ref 3 --partitions all --no-8x8dct --qp 24 --weightp 2"
