#!/usr/bin/env bash
# Rebuild the animated-GIF vectors. Committed so the fixtures can be
# re-derived; never run by the suite, which reads the files instead.
#
# Ground truth is ImageMagick 7's `-coalesce`, which is the operation this
# decoder performs: every frame composited onto the logical screen, with
# disposal and transparency applied. It is the right reference and FFmpeg is
# not -- FFmpeg's GIF decoder resamples a variable-delay animation onto a
# constant frame rate, so its frame count is not the file's, and it clears a
# disposal-2 frame to the header's background colour where every browser
# clears it to transparent. FFmpeg still writes one of the files: an encoder
# whose output nobody here has looked at is worth more as an input than as an
# oracle.
#
# Each animation ships as <name>.gif plus <name>.rgba.z, which is zlib over
# the coalesced RGBA of every frame, back to back.
set -euo pipefail
cd "$(dirname "$0")"

need() { command -v "$1" >/dev/null || { echo "need $1" >&2; exit 1; }; }
need magick
need ffmpeg
need python3

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# -- sources ---------------------------------------------------------------
# A block sliding across a solid ground, in four steps. Small enough that a
# frame is legible as sixteen numbers when a test fails.
for i in 0 1 2 3; do
  magick -size 8x6 xc:'#204080' -fill '#e0c020' \
         -draw "rectangle $((i * 2)),0 $((i * 2 + 1)),2" "$work/src$i.png"
done
# The same motion with nothing behind it, for the transparency cases.
for i in 0 1 2; do
  magick -size 8x6 xc:none -fill '#20c040' \
         -draw "rectangle $((i * 2)),1 $((i * 2 + 2)),4" "$work/clear$i.png"
done

# -- the animations --------------------------------------------------------

# Four whole-screen frames, every one a different delay, looping for ever.
magick -delay 5 "$work/src0.png" -delay 12 "$work/src1.png" \
       -delay 7 "$work/src2.png" -delay 3 "$work/src3.png" \
       -loop 0 frames.gif

# The same animation after the optimiser has had it: frames smaller than the
# screen, placed at an offset, each one meaning "and the rest is as it was".
# This is what almost every animated GIF on the web actually looks like, and
# it is the case a decoder that returns the sub-image rather than the screen
# gets visibly wrong.
magick frames.gif -layers optimize offset.gif

# Transparency, and a loop count that runs out.
magick -delay 8 -loop 3 "$work/clear0.png" "$work/clear1.png" \
       "$work/clear2.png" trans.gif

# Disposal: one frame that stays, one that clears its own rectangle back to
# transparent, one that puts back what was underneath it.
magick -delay 6 -dispose none "$work/src0.png" \
       -delay 6 -dispose background "$work/clear1.png" \
       -delay 6 -dispose previous "$work/clear2.png" \
       -delay 6 -dispose none "$work/src3.png" \
       -loop 0 dispose.gif

# Interlaced, which reorders the rows inside each frame and nothing else.
magick -delay 6 "$work/src0.png" "$work/src2.png" \
       -interlace line -loop 0 interlace.gif

# One frame, no loop extension: a still GIF, which has to come back as a
# one-frame animation rather than as an error.
magick "$work/src1.png" still.gif

# A second encoder's bitstream. FFmpeg's palette handling, block sizes and
# optimiser are its own; the only thing this file has in common with the
# others is the format.
ffmpeg -v error -y -framerate 10 -i "$work/src%d.png" \
       -vf "split[a][b];[a]palettegen=max_colors=8[p];[b][p]paletteuse" \
       -loop 0 ffmpeg.gif

# -- truth -----------------------------------------------------------------
for name in frames offset trans dispose interlace still ffmpeg; do
  magick "$name.gif" -coalesce "rgba:$work/$name.rgba"
  python3 -c "
import sys, zlib
raw = open(sys.argv[1], 'rb').read()
open(sys.argv[2], 'wb').write(zlib.compress(raw, 9))
" "$work/$name.rgba" "$name.rgba.z"
done

ls -l ./*.gif ./*.rgba.z
