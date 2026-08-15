#!/usr/bin/env bash
#
# Regenerate the inter-coded (P and B slice) test vectors in this directory.
#
# THIS IS A ONE-OFF OFFLINE TOOL. It is not run by test.sh, it is not a
# dependency of anything, and nothing in the browser or the test suite needs
# ffmpeg or x264 to be installed. What ships is the output: each `.264`
# stream (and one `.mp4`) and, beside it, the exact I420 bytes FFmpeg 7.1
# decoded it to, zlib-deflated. tests/test_h264.py compares against those
# bytes and never shells out to anything.
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
# The nine intra-only vectors (mb1, mb4, qcif-*, crop, tiny-crop and
# qcif-cavlc) predate this script and their `.264` files are not reproduced by
# it. qcif-cavlc.i420.z is, though -- see the bottom of this file. That stream
# arrived as a refusal case, with no truth beside it, and once CAVLC decoded
# it needed one; regenerating the truth for a committed stream is a different
# operation from encoding a new one, and `truth_only` below is it.
#
# Usage: ./make_inter_vectors.sh [output-directory]

set -euo pipefail
out="${1:-$(cd "$(dirname "$0")" && pwd)}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Common encoder settings. --bframes 0 is what the P vectors want and every B
# vector below overrides it; --keyint infinite --no-scenecut so that exactly
# one IDR appears and every other frame is an inter frame (a second IDR would
# silently hide a reference picture bug behind a fresh start); --tune psnr to
# keep psy-rd from making the mode decisions depend on x264's visual model.
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

# ---------------------------------------------------------------------------
# CAVLC. Everything above is CABAC; clause 9.2 is a wholly separate entropy
# layer and shares no code with 9.3, so it needs its own vectors rather than a
# flag on the existing ones.
#
# The switch is --no-cabac. (FFmpeg spells the same thing -coder 0 when it
# drives libx264; the x264 binary does not take that name.) It comes after
# $common on the command line, so putting it in the per-vector options is
# enough.

# Intra only: --keyint 1 makes every frame an IDR, so this is the CAVLC
# residual layer and nothing else -- no mb_skip_run, no mvd, no ref_idx.
vector cavlc-intra 128x96 3 \
  "testsrc2=size=256x192:rate=25" \
  "--no-cabac --keyint 1 --partitions all --no-8x8dct --qp 26"

# CAVLC with P frames and motion: mb_skip_run in place of mb_skip_flag,
# mb_type and sub_mb_type as ue(v), mvd as se(v), ref_idx as te(v), and
# coded_block_pattern through the inter column of Table 9-4.
vector cavlc-p 128x96 10 \
  "mandelbrot=size=128x96:rate=25:maxiter=200" \
  "--no-cabac --ref 3 --partitions all --no-8x8dct --subme 9 --me umh --qp 24"

# High QP: almost every block is empty or nearly so. That is the nC path --
# with TotalCoeff mostly zero the coeff_token table a block is decoded with
# depends entirely on getting its two neighbours' counts and their
# availability right, and a slice full of long skip runs ends without an
# end_of_slice_flag to say so.
vector cavlc-highqp 176x144 8 \
  "testsrc2=size=352x288:rate=25" \
  "--no-cabac --ref 2 --partitions all --no-8x8dct --qp 44 --weightp 0"

# Low QP on noise: the opposite end. Levels are large, so suffixLength
# escalates through its whole range and level_prefix reaches the >= 15 and
# >= 16 escapes -- the cases that never appear in an ordinary picture and so
# never appear in a test that uses one.
vector cavlc-lowqp 96x64 4 \
  "nullsrc=size=96x64:rate=25,format=yuv444p,geq=lum_expr='random(1)*255':cb_expr='random(2)*255':cr_expr='random(3)*255'" \
  "--no-cabac --ref 1 --partitions all --no-8x8dct --qp 3"

# Smooth, saturated colour that keeps moving: the luma is nearly flat and
# most of what is coded is chroma, which puts the weight on the 2x2 chroma DC
# block -- its own coeff_token table (nC = -1), its own total_zeros table, and
# no neighbours to derive anything from.
vector cavlc-chromadc 128x96 8 \
  "gradients=size=128x96:rate=25:c0=0xff0040:c1=0x0040ff:speed=0.15" \
  "--no-cabac --ref 2 --partitions all --no-8x8dct --qp 20 --weightp 0"

# CAVLC and the 8x8 transform together. There is no 8x8 CAVLC block: an 8x8
# is coded as four 4x4 blocks, each with its own nC and its own TotalCoeff,
# whose scans interleave into the 8x8 scan. Nothing else in the suite covers
# that, and it is where a decoder that stores one count per 8x8 breaks.
vector cavlc-8x8 128x96 8 \
  "mandelbrot=size=128x96:rate=25:maxiter=200" \
  "--no-cabac --ref 2 --partitions all --8x8dct --subme 9 --me umh --qp 22"

# ---------------------------------------------------------------------------
# Truth for a stream this script did not encode. qcif-cavlc.264 was committed
# as a refusal case with nothing beside it; the stream is unchanged and only
# its truth file is produced here.
truth_only() {
  local name="$1"
  ffmpeg -v error -y -i "$out/$name.264" -pix_fmt yuv420p \
         -f rawvideo "$work/truth.i420"
  python3 -c 'import sys, zlib
raw = open(sys.argv[1], "rb").read()
open(sys.argv[2], "wb").write(zlib.compress(raw, 9))
print("%-14s (existing stream) %7d -> %6d bytes"
      % (sys.argv[3], len(raw), len(zlib.compress(raw, 9))))' \
      "$work/truth.i420" "$out/$name.i420.z" "$name"
}

truth_only qcif-cavlc
# -- B slices ----------------------------------------------------------------
#
# Everything below overrides --bframes 0, so each one passes its own --bframes
# and, where it matters, its own --b-pyramid, --direct and --weightb. Note that
# ffmpeg writes its raw output in presentation order while the .264 stream is
# in decode order: tests/test_h264.py reorders by picture order count before
# comparing, which is the same job a container does with its composition
# offsets and is worth exercising here rather than only on an MP4.

# IBBP: one I frame and then a steady pattern of two B frames between each pair
# of P frames. No pyramid, so no B frame is ever a reference and list 1 has
# exactly one entry. The smallest stream that has a B slice in it at all.
vector b-basic 128x96 12 \
  "testsrc2=size=256x192:rate=25" \
  "--bframes 2 --b-pyramid none --ref 2 --partitions all --no-8x8dct --qp 26 --weightp 0 --no-weightb"

# B frames used as references for other B frames. The decoded picture buffer
# then holds pictures that are neither purely past nor purely future, list 0
# and list 1 both have several entries, and the two lists are genuinely
# different orderings of the same set rather than reverses of each other.
vector b-pyramid 128x96 16 \
  "testsrc2=size=256x192:rate=25,rotate=a='0.25*sin(2*PI*t*2)':c=black" \
  "--bframes 3 --b-pyramid normal --ref 4 --partitions all --8x8dct --subme 9 --me umh --qp 24"

# Spatial direct with a lot of small motion, so that B_Skip and B_Direct_8x8
# are chosen often and the minimum-over-neighbours derivation has real indices
# to choose between rather than a picture full of zeroes.
vector b-direct-spatial 112x80 14 \
  "mandelbrot=size=112x80:rate=25:maxiter=200" \
  "--bframes 2 --b-pyramid none --direct spatial --ref 3 --partitions all --8x8dct --subme 9 --me umh --qp 24"

# Temporal direct: the same content, so the two vectors differ only in the
# derivation. This is the one that scales a colocated vector by the ratio of
# two picture order count differences, and a decoder that gets the arithmetic
# subtly wrong still produces a plausible picture.
vector b-direct-temporal 112x80 14 \
  "mandelbrot=size=112x80:rate=25:maxiter=200" \
  "--bframes 2 --b-pyramid none --direct temporal --ref 3 --partitions all --8x8dct --subme 9 --me umh --qp 24"

# A fade with implicit weighted bi-prediction. The weights are not in the
# bitstream at all: both sides derive them from where the B frame sits between
# its two references, so an off-by-one in the picture order count arithmetic
# shows up as a wrong picture and nothing else.
vector b-weightb 112x80 14 \
  "testsrc2=size=112x80:rate=25,fade=t=out:st=0.1:d=0.45" \
  "--bframes 3 --b-pyramid none --ref 3 --partitions all --no-8x8dct --qp 24 --weightb"

# A still background with one small thing crossing it, with B frames. Almost
# every macroblock is B_Skip, which reads no syntax element at all beyond the
# skip flag and derives its motion from the direct process.
#
# --b-adapt 0 is load-bearing: with the adaptive decision left on, x264 looks
# at content this static and concludes that B frames buy it nothing, and the
# vector comes out as all-P and tests the P path a seventh time. Turning the
# decision off forces the requested cadence whether it pays or not, which is
# the point here.
vector b-skip 128x96 14 \
  "color=c=0x1a3050:size=128x96:rate=25[bg];testsrc2=size=24x24:rate=25[fg];[bg][fg]overlay=x='8+t*90':y=40" \
  "--bframes 3 --b-adapt 0 --b-pyramid none --direct spatial --ref 2 --partitions all --no-8x8dct --qp 24 --weightp 0 --no-weightb"

# -- and one of them in a container ------------------------------------------
#
# bframes.mp4 is the same IBBP content as b-basic, but muxed rather than raw,
# because decode order and presentation order being two different things is
# only half a problem in a `.264` file and a whole one in an MP4: the `ctts`
# box is where the composition offsets live and tests/test_h264.py checks that
# the container layer puts the frames back in the order a viewer sees them.
#
# It is encoded straight to MP4 rather than muxed from b-basic.264, which is
# deliberate: ffmpeg's raw H.264 demuxer hands the muxer pts == dts, so
# `-c copy` from a `.264` writes a file with no `ctts` at all and every frame
# declared in the wrong place. A file that lies about its own order would make
# this test pass for the wrong reason. Its truth file is ffmpeg's decode of
# the MP4, which is in presentation order.
mp4_out="$out/bframes.mp4"
ffmpeg -v error -y -f lavfi -i "testsrc2=size=256x192:rate=25" \
       -vf "format=yuv420p,scale=128:96" -frames:v 12 \
       -c:v libx264 -preset medium \
       -x264-params "bframes=2:b-adapt=0:b-pyramid=none:ref=2:keyint=infinite:no-scenecut=1:qp=26:weightp=0:weightb=0:8x8dct=0:tune=psnr" \
       -f mp4 "$mp4_out"
ffmpeg -v error -y -i "$mp4_out" -pix_fmt yuv420p -f rawvideo "$work/truth.i420"
python3 -c 'import sys, zlib
raw = open(sys.argv[1], "rb").read()
open(sys.argv[2], "wb").write(zlib.compress(raw, 9))
print("%-14s %s %2d frames  %7d -> %6d bytes"
      % ("bframes.mp4", "128x96", 12, len(raw), len(zlib.compress(raw, 9))))' \
    "$work/truth.i420" "$out/bframes.i420.z"

# -- the two refusal fixtures -------------------------------------------------
#
# No truth file, because there is no right picture: what is asserted is the
# error. Both are well formed and FFmpeg decodes both, which is the point --
# a decoder missing either refusal produces a plausible picture rather than a
# complaint, and a fixture is the only way to notice.
#
# They are encoded with ffmpeg directly rather than through `vector` because
# neither goes near the common settings and both want to be as small as a
# stream can be while still containing the thing.

# --qp 0 is x264's lossless mode: profile 244 and
# qpprime_y_zero_transform_bypass_flag set, so a macroblock at QP 0 skips the
# transform and the deblocking filter and adds its residual as it stands.
ffmpeg -v error -y -f lavfi -i "testsrc2=size=64x48:rate=25" \
       -vf format=yuv420p -frames:v 1 \
       -c:v libx264 -preset veryfast -x264-params "qp=0:bframes=0:tune=psnr" \
       -f h264 "$out/lossless.264"

# B slices under CAVLC. Nothing on the web is encoded this way -- Baseline has
# no B slices and everything above it uses CABAC -- and the two halves of the
# syntax have never been read together.
ffmpeg -v error -y -f lavfi -i "testsrc2=size=64x48:rate=25" \
       -vf format=yuv420p -frames:v 6 \
       -c:v libx264 -preset veryfast \
       -x264-params "cabac=0:bframes=2:b-adapt=0:b-pyramid=none:ref=2:qp=30:tune=psnr" \
       -f h264 "$out/b-cavlc.264"

printf '%-14s %s\n' lossless.264 "(refusal case, no truth file)" \
                    b-cavlc.264  "(refusal case, no truth file)"
