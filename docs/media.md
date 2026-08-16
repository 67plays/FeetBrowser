# Video

A `<video>` element lays out at the right size, decodes real frames out of a
real file, and presents them through the existing rasteriser against a real
clock, with play, pause and a scrubber. Motion JPEG plays (in AVI, in
QuickTime, and as a bare stream of JPEGs), as do uncompressed and RLE AVI and
QuickTime's `raw ` and `png `. H.264 plays as far as its decoder goes, which
is every frame of a stream of I, P and B slices -- an ordinary
well-compressed web MP4 -- and not one with SP or SI slices, nor a B slice
coded with CAVLC; see [H.264, in Fortran](#h264-in-fortran). Everything else
is identified by name and refused in public.

Audio has both halves and the join between them. The AAC-LC track of an
ordinary web MP4 comes out as PCM samples in memory, exactly as a reference
decoder produces them -- see [AAC, in Fortran](#aac-in-fortran) -- there is a
platform output device on all three platforms, a lock-free ring, a polyphase
resampler, a mixer with per-source gain and a master volume, and a monotonic
audio clock, see [Audio output](#audio-output); and `feetplayer/arch.py`
pumps one into the other and hangs the pictures off the result, see
[Sound and pictures together](#sound-and-pictures-together). An MP4 with an
AAC-LC track plays with sound, and its frames are scheduled against the
sound rather than against the wall clock. Uncompressed sound plays too, in
all three of the containers that carry it -- MP4/MOV, AVI and `.wav` -- see
[PCM](#pcm), and a bare `.mp3` decodes as well, see
[MPEG Layer III, in Fortran](#mpeg-layer-iii-in-fortran). What is left is
still named and refused: no Vorbis, no Opus, no ADPCM and no mu-law or A-law.

Read this before adding a codec. The point of writing it down is that the
hard parts here are not the codec; they are the seams the codec plugs into,
and those are done.

## Where this code lives

Most of what this page describes is no longer in this repository. The
decoders, the containers and the audio output moved to
[feetplayer](https://github.com/67plays/feetplayer), which is a package with
no dependencies of its own, and the browser installs it: one line in
`requirements.txt`, pinned to a full commit sha rather than to a branch, and
`test.sh`, `run.sh`, `test.cmd`, `run.cmd`, the CI build action and all three
packagers install from that one file. There is no `pyproject.toml` at the
root of FeetBrowser and this change did not add one.

What moved:

```
feetplayer/mediacodec.py   containers: MP4/MOV, AVI, WebM, .wav, bare MJPEG
                           and bare MP3, plus the PCM path
feetplayer/h264.py         the H.264 decoder's Python side
feetplayer/aac.py          the AAC-LC decoder's Python side
feetplayer/ball.py         the MPEG Layer III decoder's Python side
feetplayer/heel.py         the ring, the resampler, the mixer, the clock
feetplayer/arch.py         the join: decoded sound into a speaker
feetplayer/coreaudio.py    the three platform backends
feetplayer/alsa.py
feetplayer/winmm.py
feetplayer/fortran/        ~14,800 lines of FORTRAN 77, the three decoders
```

What stayed, because it is the browser and not the media stack:
`feetbrowser/media.py` (the clock, the scheduler, `VideoPlayer`),
`feetbrowser/imagecodec.py`, the `<video>` box in `layout.py`, the control
bar and the tick in `browser.py`, and `tests/test_render.py`'s container
tests -- which now import `feetplayer.mediacodec` and are the proof that the
seam is real rather than a rename.

The Fortran is compiled by gfortran while pip installs feetplayer, into the
installed package directory. That is an install-time requirement and never a
runtime one: without a compiler feetplayer installs with no libraries beside
it, falls back to compiling on demand, and failing that reports H.264, AAC
and Layer III as codecs it does not have -- which is the same graceful
degradation the browser always had, moved one repository over. The three
packagers do not rely on either path: they compile the libraries explicitly
on the build machine and ship them inside the installed package, because the
machine that runs a `.dmg` has no compiler. See the packaging READMEs.

The rest of this page is written as it was, because the code is the same
code at the same commits; only the paths changed.

## Why Motion JPEG

Because it is the only genuinely compressed video format this project can
decode for free, and "for free" is doing real work in that sentence.

The rule is no third-party code, so a codec has to be written here. H.264 is
a multi-month project on its own (CABAC, deblocking, six intra prediction
modes, quarter-pel motion compensation) before the patent question is even
asked, and VP8, VP9 and AV1 are the same shape of problem. What is left is
the small set of formats whose decoder is short enough to read: uncompressed
frames, run-length frames, and pictures.

MJPEG is pictures. Every frame is a complete baseline JPEG, and the JPEG
decoder was already written for `<img>` and already lives in Rust next to the
rasteriser, where it decodes a 320x224 frame in about a millisecond. That is
around two and a half per cent of a core to play 25 frames a second, against
the 13 ms a frame that pure-Python `BI_RGB` costs at 320x240, a format that
compresses ten to one *and* decodes ten times faster, for the price of a
fourcc case and a call. Nothing else on the list comes close.

It is also a real format rather than a demonstration. Webcams, action
cameras, microscopes, dashcams and scientific capture cards all write MJPEG,
in AVI or QuickTime, and MJPEG-over-HTTP is what an enormous number of IP
cameras still serve. A `<video src="clip.avi">` on an ordinary page is the
case this was built for, and it is a case that exists.

There are two details in MJPEG worth knowing before reading the code. Frames
very often carry no Huffman tables, because the tables would be identical in
all of them; the decoder is expected to know the standard ones from Annex K
of the JPEG specification and splice them in, which is what `_jpeg_frame`
does. And the keyframe flags in an AVI's `idx1` are routinely all zero in
camera-written files, which would make every seek replay the clip from the
start for a codec in which every frame is independent by definition, so for
MJPEG the index is overruled rather than believed.

## Why AVI as well, and why uncompressed and RLE

The brief for a from-scratch browser rules out the obvious answer. We cannot
link ffmpeg, and H.264 is a multi-month project on its own (CABAC,
deblocking, six intra prediction modes, quarter-pel motion compensation)
before the patent question is even asked. So the choice was between formats
that are genuinely writable in readable Python: animated GIF, MJPEG,
container parsing on its own, and an uncompressed or RLE codec in a simple
container. Animated GIF belongs to the image decoder, and has since been
built there rather than here; MJPEG needed a JPEG decoder, which at the time was
also somebody else's live work and has since landed. Container parsing on its
own puts nothing on screen. What is left is AVI carrying `BI_RGB` and
`BI_RLE8`: a container simple enough to parse exactly (RIFF is
length-prefixed chunks and nothing else), with two codecs that are
patent-free, decodable in a few
hundred lines, and (this is the part that matters for a *foundation*)
between them exercise both halves of the problem. `BI_RGB` is intra-only, so
any frame is a seek target. `BI_RLE8` has delta and end-of-bitmap escapes, so
a frame depends on the one before it, and the player has to keep decoder
state, find the keyframe before a seek target, and replay forward. That is
the shape every real codec has. Getting it right against a codec you can read
in an afternoon is the whole reason to start here.

## The three layers

The split is deliberate: **bytes**, **time**, **pixels**. Each is testable
without the other two, and only the last one knows what a canvas is.

### `feetplayer/mediacodec.py`: bytes

Containers and codecs. No clocks, no threads, no canvas, no imports from the
browser. Everything in it is a pure function of a `bytes` object.

- `sniff(data)` returns `"AVI"`, `"MJPEG"`, `"MOV"`, `"MP3"`, `"MP4"`,
  `"WebM"` or `""` from the magic. `"MJPEG"` and `"MP3"` are the
  containerless cases: a file that begins with a JPEG and holds nothing but
  JPEGs, and a file that is a run of MPEG audio frames with nothing wrapped
  around them. Both are recognised last, because neither has magic so much
  as a shape, and a real container should win first.
- `probe(data)` returns a `MediaInfo` (container, codec fourcc or name,
  width, height, duration, frame count, `supported`, and a `reason` when it
  is not. It does not raise for a file it merely cannot decode, because "an
  H.264 MP4, 320x180, 3 seconds" is information the layout wants.
- `open_video(data)` returns a `VideoTrack`, or raises `MediaError`. The
  unsupported case raises a subclass carrying the `MediaInfo`, so the caller
  gets the geometry it needs to reserve a box even on the failure path.
- `VideoTrack` is random access over frames: `frame(i)` returns a
  `VideoFrame` (index, pts, duration, width, height, and RGBA bytes),
  `packet(i)`, `frame_time(i)`, `is_keyframe(i)`, `keyframe_before(i)`,
  `reset()`. When `frame(i)` is not the sequential next one it rewinds to
  `keyframe_before(i)` and replays. Sequential playback costs one decode per
  frame; a seek costs the distance back to the keyframe.
- `probe_audio(data)` and `open_audio(data)` are the same pair for sound,
  returning an `AudioInfo` (container, codec, sample rate, channels,
  duration, coded frame count, `supported`, `reason`) and an `AudioTrack`.
  Deliberately a separate pair rather than more fields on `MediaInfo`: a
  file can have a video track we play and an audio track we can only name,
  and one answer for both would have to be wrong about one of them. A file
  with no sound at all comes back as an `AudioInfo` saying so rather than as
  an error.
- `AudioTrack` is the shape of `VideoTrack`, because the thing above them --
  a scheduler holding a clock -- wants to ask both the same questions.
  `frame(i)` returns an `AudioFrame` (index, pts, duration, sample rate,
  channels, and interleaved float32 bytes); one "frame" is one coded AAC
  frame, 1024 samples per channel, about 23 milliseconds. There is no
  keyframe in AAC -- every frame carries a whole spectrum but its first half
  is the previous frame's transform tail -- so an out-of-order `frame(i)`
  replays from the start of the track rather than from a sync sample.

Every read goes through a `_Reader` that bounds-checks, and every walk is
capped: `MAX_FRAMES`, `MAX_CHUNKS`, `MAX_FRAME_BYTES`, `MAX_DEPTH`, plus
`MAX_PIXELS` borrowed from `imagecodec` so that one module owns the
dimensions policy. A chunk header claiming 4 GB inside a 200-byte file is
clamped, not trusted. This matters more than it sounds: a container parser is
the part of a media stack that reads attacker-controlled length fields, and
the failure mode to design against is not a wrong picture, it is a loop that
never ends.

MP4 and MOV are demuxed properly, because MJPEG in QuickTime is a real file
you are handed and there is no way to play one without walking the sample
tables. `stsd` gives the codec and the geometry, `stts` the per-sample
durations, `stsc` the samples-per-chunk runs, `stsz` the sizes and
`stco`/`co64` the chunk offsets; putting those together is what turns "sample
17" into a byte range. `stss` lists the sync samples, and a track without one
is all sync samples, which is exactly right for MJPEG. `stts` is run-length
encoded and need not be constant, so the track carries real per-frame times
rather than an average rate: `VideoTrack.index_at()` bisects them, and the
scheduler asks it rather than dividing.

WebM is still parsed only far enough to answer `probe()`: EBML
variable-length IDs and sizes to `PixelWidth`, `PixelHeight`, `CodecID`, and
`Duration` times `TimecodeScale`. It decodes no pixels and does not pretend
to. An MP4 whose codec we lack is the same: the boxes are read, the numbers
are real, and the answer is still no.

### `feetbrowser/media.py`: time

Decoding and presenting are two different jobs on two different schedules,
and conflating them is how a player drifts.

- `Clock` is injected. `SystemClock` reads `time.monotonic`; `ManualClock`
  is set and advanced by hand, which is what the tests use, so frame timing
  is asserted deterministically rather than by sleeping and hoping.
- `Scheduler` owns the playhead. Position is always
  `clock.now() - origin + offset`, never a count of frames presented. A
  decoder that falls behind therefore cannot slow playback down; it can only
  lose frames, which is the correct trade. `due_index()` maps position to a
  frame index; `tick()` pops the queue, discards anything already late, and
  returns at most one frame to present. It counts `presented`, `dropped`,
  `starved`, `resyncs` and `ended`.
- `VideoPlayer` joins the two. It decodes ahead into a bounded queue
  (`QUEUE_DEPTH`), either on a daemon thread (`threaded=True`, what the
  browser uses) or inline via `pump()` (what the tests use, so a test is a
  sequence of calls rather than a race). If the decoder falls more than
  `RESYNC_FRAMES` behind the playhead it stops trying to catch up frame by
  frame, jumps to the keyframe before where the clock will actually be, and
  counts the skipped span as dropped. `set_display_size()` scales through
  `imagecodec.resize()` into a `canvas.PhotoImage`, so the paint path blits
  at natural size and never scales per frame.

`VideoPlayer` also constructs successfully for a file it cannot decode. It
carries the `MediaInfo`, reports `status()` as a human-readable line, and
returns no frames. Layout needs that object to exist.

### `layout.py` and `browser.py`: pixels

Both changes are additive and local.

`layout.py` treats `video` as inline, sizes the box from `width`/`height`
attributes, then the container's declared intrinsic size, then 300x150,
which means an H.264 MP4 still reserves its true 320x180, because the
container told us even though the codec did not. A playable element emits
`DrawVideo`, holding the player's `PhotoImage` and a `hit()` so clicks find
it. An unplayable one emits a dark rectangle and a label naming the codec and
why it is not playing. `ua.css` hides `source` and `track`, which otherwise
spill into the flow as empty inline boxes.

An element with `controls` also emits `DrawVideoControls`: a play/pause
button, a scrubber with a played portion and a knob, and a `position /
duration` readout, over the bottom 28 pixels of the picture. It is the only
display list command that paints several primitives, and the only one whose
*geometry* changes without layout running again; the frame timer repaints
the existing display list rather than rebuilding it, which `DrawVideo` gets
away with because its photo is rewritten in place and a scrubber cannot,
because where the knob goes is a number rather than a buffer. So the bar
reads the player at `execute()` time. Hit testing lives in the same class for
the same reason: `action_at()` compares a click against the rectangles
`execute()` draws, and having two copies of that arithmetic is how a button
ends up half a pixel from where it looks. A box narrower than 120 or shorter
than 72 pixels gets no bar, because covering the film is worse than having no
controls.

`browser.py` fetches video bytes on the existing image-fetch thread pool,
builds **one player per element** (two `<video>` tags on the same URL are two
independent playheads), and drives them from a single `Window.after` timer
chain at 40 ms that marks the active tab for repaint. `Browser.busy()` counts
pending video *fetches* but not playback, so `settle()` returns on a loaded
page instead of waiting for a loop to finish. Clicking a video toggles it,
with or without a control bar, because that is the only transport a `<video>`
without `controls` has; a click that lands on the bar goes to the bar
instead, and is swallowed there even when it means nothing, so a miss between
the groove and the readout does not fall through and start the film.
Navigating away or closing a tab stops the decode threads.

## H.264, in Fortran

The sections above say twice that H.264 is a multi-month project. They were
written before anyone started it, and they were right about the size; what
they got wrong was that the size is a reason not to begin. It is in
feetplayer's `fortran/`, wrapped by `feetplayer/h264.py`, and it decodes I, P and B
slices to the exact pixels a reference decoder produces.

**Exactly what it does.** Annex B and AVCC framing with emulation-prevention
removal; SPS and PPS including the High-profile block and both scaling-matrix
fall-back rules; I-, P- and B-slice headers, including the reference list
modification and weighted prediction tables; CABAC, which is the whole of
clause 9.3: context initialisation from the (m, n) tables, decode-decision,
decode-bypass, decode-terminate and renormalisation; CAVLC, which is the
whole of clause 9.2: all five `coeff_token` tables with the `nC` derivation
that picks between them, the `level_prefix`/`level_suffix` escalation and
both of its escapes, `total_zeros` and `run_before`, and the macroblock and
slice syntax that goes with them -- `mb_skip_run`, the two columns of Table
9-4, and a residual loop terminated by `more_rbsp_data()` rather than an
`end_of_slice_flag`; I macroblocks in all of Intra_4x4, Intra_8x8,
Intra_16x16 and the four chroma modes; residual decoding with the 4x4 and 8x8
integer inverse transforms and the chroma DC Hadamard; the deblocking filter;
and the colour conversion out to RGBA.

**And what inter prediction added.** A decoded picture buffer; picture order
count (8.2.1) for types 0 and 2; reference picture list initialisation and
reordering (8.2.4); sliding-window and MMCO reference marking (8.2.5); the
median motion vector predictor with the 16x8 and 8x16 directional cases
(8.4.1.3) and P_Skip (8.4.1.1); the six-tap quarter-sample luma interpolator
(8.4.2.2.1) and the eighth-sample bilinear chroma one (8.4.2.2.2), both
clamping at the picture edge; weighted prediction (8.4.2.3); the four P
macroblock types with their sub-macroblock partitions; the CABAC contexts for
`mb_skip_flag`, `ref_idx_l0`, `mvd_l0` and `sub_mb_type`; and a boundary
strength derivation (8.7.2.1) that now compares motion vectors and reference
*pictures* rather than reference indices, which is not the same thing the
moment two slices order their lists differently.

**And what B slices added.** Reference list 1 and the B initialisation
(8.2.4.2.3), with modification applied to both lists; the B macroblock and
sub-macroblock types including B_Skip and B_Direct_16x16; bi-prediction with
its own rounding (8.4.2.3.1) and weighted bi-prediction in both the explicit
and the implicit mode (8.4.2.3.2), whose weights come from where the picture
sits between its two references and appear nowhere in the bitstream; both
direct modes -- spatial (8.4.1.2.2) and temporal (8.4.1.2.3), with the
colocated picture and its motion field saved per reference frame; the CABAC
contexts for the B forms of `mb_skip_flag`, `mb_type` and `sub_mb_type` and
for `ref_idx_l1` and `mvd_l1`; and a boundary strength derivation that now
compares two lists of motion against two, which is a matching problem rather
than a pairwise comparison when both sides predict twice from the same
picture.

Storing the colocated motion field is what B slices cost in memory: 5.5 MB
of `COMMON`, on top of doubling the current picture's motion field to hold
two lists. That is the price of temporal direct, which reads the motion of
the picture at reference index 0 of list 1 as it was when *that* picture was
decoded, and there is nowhere else to keep it.

Decode order stops being presentation order here, and that half of the
problem is deliberately not in the Fortran: the decoder hands pictures over
in the order they are coded and reports each one's picture order count, and
`VideoTrack` sorts them into the order they are shown using the container's
`ctts` composition offsets. A reorder buffer holds the handful of pictures
that have been decoded but not yet shown, so playing an IBBP file straight
through still costs one packet per frame; a seek backwards past that buffer
resets and replays from the keyframe, and `bframes.mp4` in the fixtures is
the test that both paths produce the same bytes as playing straight through.

**Four reference frames, not sixteen.** The buffer is dimensioned `MXREF =
4`, and a stream whose SPS asks for more is refused rather than decoded
wrongly. The level limit is 16, but there is no allocator here -- the planes
are `COMMON` and are sized at compile time for 1920x1088 -- so the cap is a
real trade of memory against reach. Sixteen reference frames is 50 MB of
static storage carried by every process whether it plays a video or not, and
nothing on the web needs it: x264's default is three, its `--preset slow` is
four, and the hardware encoders in phones and cameras use one or two. Four
covers all of those, and the failure mode for the rest is a named refusal
rather than a wrong picture. Raising it is one number.

The reference planes are `INTEGER*1` even though the working picture is
`INTEGER`. A reference sample is a byte and is only ever read back through
one masking accessor, so a reference frame costs 3.1 MB against the working
picture's 12.5, and all four of them together cost what the one picture the
decoder is building costs.

**Why Fortran.** Because the hot loop of a CABAC decoder is a handful of
integer comparisons, table lookups and shifts in a dependent chain, and that
is the one shape of code where a Fortran compiler's assumptions cost nothing
and its aliasing rules pay. Measured on the decode-decision loop before any
of this was written: 119.7 Mbin/s for Fortran against 153.7 for C, close
enough that the language stopped being the interesting variable. It is also
the only compiled language in this tree that arrives with no crates, no
package manager and no lock file, which is the standing constraint here.

**How it is built and loaded.** In a packaged application the library is
already there: the packaging compiled it on the build machine and put it
inside the installed feetplayer package as
`_h264_<digest>.dll`/`.dylib`/`.so`, with feetplayer's `fortran/` beside it.
`h264.py` prefers it, and the digest is the whole check -- it is a hash of the
shipped sources and the ABI number, recomputed at load, so a library built
from a different decoder is not loaded in error, it is not found. The
`h264_version` check still runs on it.

From a checkout the library is usually there too, because pip compiles it
while it installs feetplayer -- see
[Where this code lives](#where-this-code-lives) -- and it lands in the same
place under the same digest.

Failing both -- a machine with no gfortran at install time, or a compiler
that appeared afterwards -- `h264.py` finds a
`gfortran`, compiles the eleven sources into a shared library in the
temporary directory under a name keyed on a hash of those sources *and of the
compiler*, and loads it with `ctypes`. The hash is what makes the cache safe:
edit a `.f` file, or build with a different gfortran, and the next run builds
a different library rather than loading the old one. Nothing about this is
required. No compiler, a compiler that fails, or a library that reports the
wrong ABI version all end in `h264.available()` returning false, and a file
carrying H.264 is then named and refused exactly as it was before any of this
existed. `run.sh` and `run.cmd` warm the cache at startup so the first
`<video>` on a page does not stall for the build, and both ignore whether it
worked.

`python3 -m feetbrowser --check-video [stream.264 [truth.i420.z]]` asks a
build whether it can decode, and with the fixtures from
`tests/fixtures/h264/` makes it prove it. It exists because the answer used
to be "no" in every shipped copy of the browser and "yes" on every machine
that could have noticed. The packaging scripts run it inside the artifact
they just built, with `PATH` cut back to the system directories.

The decoder's state lives in `COMMON` blocks, which is to say there is one
decoder in the process no matter how many `Decoder` objects Python holds. The
`threading.Lock` in `h264.py` is what keeps that honest, and it is load-
bearing rather than defensive: the browser decodes video on a worker thread
per element.

**What it is not.** No SP or SI slices, no interlaced coding in any of its
forms, no long-term reference pictures, no picture order count type 1, and no
lossless coding -- `qpprime_y_zero_transform_bypass_flag`, which is what
x264's `--qp 0` turns on, and where a macroblock's residual is added to the
prediction without ever going through a transform. A B slice coded with CAVLC
is refused too: the two features were built for
different halves of the syntax, the combination does not occur in a real
stream, and reading it would produce a plausible picture rather than an error.
Temporal direct prediction is refused when `direct_8x8_inference_flag` is
clear, which is a corner x264 never produces and which there is therefore no
way to test bit-exactly here; spatial direct works either way. Those are not
stubbed or half-written: the parser sees what it does not handle and refuses
the stream by name.

Refusing has to happen before the poster goes up rather than in the middle of
playback, and for a per-slice property that is harder than it sounds.
Trial-decoding frame zero cannot see frame four hundred, and an encoder may
introduce an SP slice anywhere. So `_H264` reads `slice_type` out of every
sample's slice header first -- two exp-Golomb fields per NAL, no arithmetic
decoding, so it
costs nothing -- and refuses the whole file if any of them is a kind it
cannot finish. Then it trial-decodes the first keyframe for everything a
header cannot tell you, and throws the result away so that frame zero is
decoded once, by whoever asks for frame zero.

The other thing inter prediction changed above the decoder: there is one
decoder in the process, and a P frame is a difference against pictures that
decoder is still holding, so two `<video>` elements decoding alternately
would each be predicting from the other's pictures. `Decoder` keeps the
access units it has fed since its last IDR and replays them when it finds
another instance has been at the library in between. The history is bounded
by the stream's keyframe interval rather than by its length, and
`test_two_decoders_interleaved_do_not_corrupt_each_other` is the proof.

## Audio output

Everything downstream of "here are some PCM samples". Named `heel`, because
it is the part of a foot that makes a noise, and because LICENSE condition 3
says so.

Nothing here decodes anything. A `Source` is a rate, a channel count and a
stream of samples; where they came from is not this code's business. That
line is deliberate -- it is what let the output stack be written, measured
and finished while no audio codec exists.

### The pieces

- `feetplayer/heel.py`: the pure engine, and nearly all of it.
  - `Ring` is a single-producer, single-consumer byte ring over one
    preallocated ctypes buffer, with no lock in it. Two monotonically
    increasing byte counters; the producer alone writes one and the consumer
    alone writes the other, so a stale read of the other side's counter is
    always stale in the safe direction. The counters are Python integers and
    do not wrap.
  - `Resampler` is polyphase windowed-sinc: upsample by L, low-pass at the
    lower of the two Nyquist frequencies, decimate by M, with the filter
    decomposed into L branches so that no multiplication by a stuffed zero
    is ever performed. 64 taps, Kaiser window at beta 9, each branch
    normalised to unity gain. State carries across calls, so
    `process(a) + process(b)` is sample-for-sample `process(a + b)` -- which
    is what makes it usable against a decoder handing over whatever a packet
    happened to contain. Rate pairs that do not reduce to a small ratio are
    approximated by continued fractions.
  - `Mixer` and `Source`: several sounds at once, summed in floating point,
    per-source `gain`, one master `volume`, clamped exactly once at the end.
    Resampling and channel mapping happen on the *writing* side, because the
    decoder thread has a packet's worth of slack and the mixer thread has two
    milliseconds.
  - `AudioClock` is frames the device has actually consumed. `now()` returns
    seconds and is duck-compatible with `media.Clock`, so a `Scheduler` takes
    one with no adapter. **Video follows audio, never the reverse**: a
    dropped picture costs one frame nobody sees, and a gap in sound is a
    click everybody hears.

    There are two clocks here and picking the wrong one is the mistake
    waiting to be made. `AudioClock.now()` is the *device's* timeline: how
    much the hardware has swallowed since the stream started. It is the right
    thing to measure underruns against and the wrong thing to hang a picture
    on, because it does not know when a particular stream started, does not
    move backwards over a seek, and counts frames that are still in a buffer
    somewhere rather than in the air. `Source.position()` is the *stream's*
    timeline and is the one `<video>` wants: it takes off the ring's backlog
    and the device's own reported latency, so it is seconds of that source
    the listener has actually heard. On the machine this was written on, over
    Bluetooth, the two differ by 171 ms -- about four frames of video, which
    is well past the point where a viewer sees lips out of step.
  - `open_output()` is the entry point and never raises. On a machine with
    no sound card it returns an `Output` over a `NullDevice` that consumes
    in real time and throws the samples away, with `silent` true and
    `reason` set to a sentence fit to show a user. That is not a test stub:
    a browser on a headless box still has to play a video at the right
    speed. `available()` and `unavailable_reason()` follow `h264.py` --
    probed once, and the answer remembered, so a container with no
    `/dev/snd` is not asked about it once per packet.
- `feetplayer/coreaudio.py`, `alsa.py`, `winmm.py`: one per platform,
  ctypes against the system library, the same shape as `cocoa.py`, `x11.py`
  and `win32.py`. Each knows how to take bytes out of a ring and nothing
  else at all.

### Rules for the realtime side

On macOS the device callback runs on a CoreAudio thread with a deadline of a
couple of milliseconds. Six rules, and every one of them is about *not*
doing something: the callback never blocks on a lock, never waits for data
(an empty ring is silence and a counter, not an error path), never raises
(the body is wrapped and the failure stashed for the main thread), allocates
nothing that can grow (the payload moves by `memmove` between two buffers
that existed before the stream started), does no work that belongs to
somebody else (no mixing, gain, conversion or resampling on that thread),
and nothing is freed while the device is running. They are written out in
full at the top of `heel.py`.

The GIL is the thing that cannot be designed away: calling a Python function
at all means taking it, CoreAudio's deadline is about 2 ms and CPython's
switch interval is 5. So the callback is made as short as it can be and the
ring is 4096 frames -- 85 ms -- deep, which is how late the mixer thread is
allowed to be. A design where the callback does real work in Python clicks
whenever the browser lays out a page, and no amount of making the callback
faster recovers it.

Linux and Windows do not need most of that and get it anyway. Neither ALSA
nor `waveOut` requires a callback on a foreign thread: both are driven from
an ordinary Python thread that blocks on the device and pulls from the same
ring, using the same `read_into`, with the same short-read handling.

### Platforms

| | binding | driven by | notes |
|---|---|---|---|
| macOS | AudioToolbox / CoreAudio | an `AURenderCallback` on CoreAudio's realtime thread | takes the hardware's own rate rather than asking it to convert |
| Linux | `libasound.so.2` | a thread of ours blocking in `snd_pcm_writei` | opens `default`, so PulseAudio or PipeWire is what it actually reaches; `FEETBROWSER_ALSA_DEVICE` overrides |
| Windows | `winmm.dll` (`waveOut`) | a thread of ours, woken by `CALLBACK_EVENT` | 16-bit samples; see below |

`waveOut` rather than WASAPI, and the reason is ctypes rather than taste.
WASAPI is COM and only COM, so through ctypes every call is a hand-counted
vtable slot -- and a mis-numbered slot is not an exception, it is an access
violation on a machine nobody working on this has, in a subsystem whose
failure mode is already "silence, and you cannot tell why". `waveOut` is a
flat C API of eight functions, is implemented over WASAPI shared mode by the
OS on Windows 10 and 11, and costs exclusive mode and some latency. A
browser tab has no business taking a device exclusively, and the latency is
a fixed offset, which is the one kind of error a sync loop does not mind.
The interface a backend has to satisfy is three methods -- `start(ring,
clock)`, `stop()`, `close()` -- and seven attributes, so replacing this file
with a WASAPI one later changes nothing above it.

### Measured

Resampler, against an analytically generated tone, measured with an
exact-bin DFT (the frequency chosen so that a whole number of cycles fits
the analysis window; a tone between two bins leaks into every other bin and
gives you a number in the sixties however good the filter is):

| | 93.8 Hz | 1 kHz | 5 kHz | 10 kHz | 15 kHz | 19 kHz |
|---|---|---|---|---|---|---|
| 44.1 -> 48 kHz, SNR | 123.9 dB | 110.2 | 108.3 | 105.4 | 106.2 | 99.1 |
| 44.1 -> 48 kHz, worst spur | -125.3 dBc | -110.0 | -108.2 | -105.2 | -105.9 | -99.8 |

48 -> 44.1 kHz is 107.6 dB at 915 Hz, 105.7 at 9.2 kHz and 99.7 at 17.5 kHz,
worst spur -101 to -110 dBc. Passband gain is flat to within 0.0001 dB
across the band, and DC through 44.1 -> 48 comes back as 1.0 to within
2e-16.

That is what 64 taps buys, and the number was chosen by measuring rather
than by taste: at 19 kHz, 32 taps gives 44.7 dB and 16 taps gives 17.2 dB
with a whole decibel of level error. There is a test that measures exactly
that and fails if the shipped filter ever stops beating a short one. Cost is
about 9% of one core for 64 taps of mono at 48 kHz on this laptop, which is
affordable in pure Python only because the inner loop is
`sum(map(mul, coeffs, history[a:b]))` and runs in C.

Verified audible on macOS by wrapping the render callback with a spy that
reads the bytes back out of CoreAudio's own buffer *after* the memmove and
analyses them: 2.496 s consumed at 48 kHz with zero underruns and zero
invented silence, a 440 Hz sine at the amplitude asked for to within 0.02%,
mono correctly duplicated to both channels, and no discontinuity across any
of 39 buffer seams.

### Testing sound without a sound card

feetplayer's own `tests/test_audio.py`, split the way this repository's
`tests/test_x11.py` is. The pure half is the ring (wraparound, underrun,
overrun, partial frames, and a real two-thread producer/consumer test that
checks every byte of a known sequence), the filter design, the resampler,
the formats, the mixer, the clock, and the whole pipeline end to end against
a device that keeps what it consumed so the mixed bytes can be measured. It
runs everywhere, including in a container with no sound. The live half opens
the real backend, plays two hundred milliseconds of something quiet and
checks the device consumed it in the time it should have; where there is no
device it prints why and skips. `FEETBROWSER_AUDIO=null` forces the silent
path, which is how CI and a bug report ask for it.

This repository's own `tests/test_audio.py` is the browser's half of the
same seam -- see [Tests](#tests) -- and uses the same environment variable
for the same reason.

### Not tested

The ALSA and `waveOut` backends have never been run. Their pure helpers are
tested everywhere, including the structure sizes `waveOut` depends on, but
no Linux or Windows machine was available while they were written, and CI
runners have no sound device to exercise the live half on either. Read them
as carefully-written and unproven. The CoreAudio backend is the one that has
actually made a noise.
## Sound and pictures together

`feetplayer/arch.py`, the *arch*, because it carries the load between the
heel and the toes. `mediacodec.py` says what frame 37 sounds like and
`heel.py` says how samples reach a speaker; this is the only module in the
tree that knows both, and it exists so that neither has to know the other.

`AudioPlayer` is `media.VideoPlayer` in the other medium, deliberately down
to the shape of the calls -- `play`, `pause`, `seek`, `position`, `pump`,
`close`, `gain`, `muted` -- including the `threaded=False` plus `pump()`
bargain, because the two are driven by one `<video>` element and an element
that has to remember which half wants which spelling will get it wrong. A
worker decodes ahead until the source has about half a second queued and
then stops; a `<video>` in a background tab is not holding seconds of PCM.

Three things in it are less obvious than they look.

- **The position is the source's, not the device's.** See the warning under
  [Audio output](#audio-output): `AudioClock.now()` is the device timeline
  and `Source.position()` is the stream timeline. Only the second can have a
  picture scheduled against it.
- **A seek throws sound away and nothing else may.** `seek()` is
  `Source.restart(t)`, which drops the queue and starts a new timeline --
  right for a scrubber, wrong for a loop or a change of playback rate, where
  the sound already decoded is still the sound that should be heard next.
  Those push a boundary onto a `deque` instead, in `Source.position()`
  coordinates, saying "when the playhead reaches here, media time is *that*
  and runs at *this* rate from then on"; the boundary is popped as the
  playhead crosses it.
- **A frame whose channel count disagrees with the source's is not
  written.** The source interleaves at a fixed channel count, so a stereo
  frame written to a mono source is not quiet or wrong-eared, it is noise
  for the rest of the file. Such a frame is counted in `channel_errors` and
  its *duration* is written as silence, so everything after it still lands
  in the right place.

The sync needed no change to `Scheduler` at all. `Scheduler.position()` is
`offset + (clock.now() - origin)`, so `media._AudioClock`, whose `now()` is
the audio player's position, makes both terms cancel and the pictures land
wherever the sound is. `VideoPlayer.attach_audio(player)` installs it and
routes `play`, `pause` and `seek` to both sides -- the sound first, because
`Scheduler.seek()` re-origins itself against the clock and a clock seeked
afterwards leaves every picture out by exactly the size of the seek.

`attach_audio` declines, leaving the video exactly as it was, when there is
no audio track or the output is the null fallback. A video whose clock is a
device nobody can hear is a video with a new way to stop, and a headless run
must play its pictures the way it always did.

`browser.py` builds an `AudioPlayer` alongside the `VideoPlayer` for every
`<video>` whose file has sound, hands it the element's own `loop` and
`muted`, and offers it to the video. Silence is never a failure there: a
file with no audio track and a machine with no sound card both give a video
that plays as it did before. The only thing said out loud is a track the
container names and we have no decoder for. Every player on a page shares
one `heel.Output`, opened the first time something actually has sound to
play, because `Mixer` exists precisely so the second sound does not have to
ask the sound card for a second exclusive stream and lose.

## AAC, in Fortran

The audio half of an ordinary web MP4, decoded to PCM. It is in
feetplayer's `fortran/inst*.f`, wrapped by `feetplayer/aac.py`, and it is called the
*instep* -- the top of the foot, over the arch that carries the weight. It
shares nothing with the H.264 decoder next door but the build machinery: `IP*` routines and `/IP*/`
COMMON blocks against H.264's `H2*` and `/H2*/`, because Fortran has one
global namespace and two decoders in one process have to stay out of each
other's way.

**Exactly what it does.** ADTS framing and MP4's `AudioSpecificConfig` out
of an `esds` box, including the backward-compatible SBR sync word *and the
flag inside it*, and the `program_config_element` that an implicit channel
configuration needs;
`raw_data_block` with its SCE, CPE, LFE, CCE, DSE, PCE and FIL elements;
`ics_info`, section data, the three scalefactor difference chains, pulse
data, TNS and spectral data; all eleven spectral Huffman codebooks and
codebook 11's escape sequence; inverse quantisation; mid/side and intensity
stereo; perceptual noise substitution; temporal noise shaping; all four
window sequences (ONLY_LONG, LONG_START, EIGHT_SHORT with grouping, and
LONG_STOP) in both the sine and KBD window shapes; the inverse MDCT; and
windowed overlap-add out to float samples in [-1, 1]. Mono and stereo.

**The transform.** An AAC frame is 1024 inverse-MDCT outputs per channel or
eight interleaved 128-point ones, forty-three times a second, and the
definition of the transform is an O(N^2) sum that costs about a thousand
times what it needs to. So the long transform is a DCT-IV done as a
512-point complex FFT with the -pi(l + 1/8)/M twiddle on both sides and the
standard's fold applied to the result, and the short one is the same thing
at 64 points. The O(N^2) definition is written out as well, in `IPIMDS`, and
nothing calls it to decode anything: it is there so that
`test_the_fast_imdct_computes_the_transform_it_claims_to` can hold the fast
path to the formula it claims to compute rather than to itself. They agree
to 3.4e-13 relative at 1024 points.

Measured on this machine, one core: 0.49 seconds of 44.1 kHz stereo decodes
in 1.0 millisecond, which is about 500x realtime, or 20 microseconds per
frame per channel against the 23 milliseconds a frame plays for. That is the
number that matters, because what will consume this is a mixer on a
deadline.

**Ground truth.** feetplayer's `tests/fixtures/aac` holds fourteen ADTS
streams and, beside each, the exact float samples FFmpeg 7.1 decoded it to,
zlib-compressed. Two of them, `lowrate.aac` and `lowrate.f32.z`, are also
committed here, because the packagers decode them inside a finished bundle
-- see [Tests](#tests). They cover a pure tone, broadband noise, a stereo file the
encoder really does code in mid/side, a transient that forces all four
window sequences, 32 kbit/s stereo under bit starvation, 320 kbit/s where
the quantised coefficients run into the thousands and codebook 11's escape
reaches eight leading ones, all eight scalefactor band layouts the standard
defines (8, 16, 24, 32, 44.1, 48, 64 and 96 kHz), and one stream from a
different encoder entirely, for temporal noise shaping. Between them they
exercise every one of the eleven codebooks.

Those last two groups are there because of what was missing. Four of the
eight band tables had no vector at all, and the TNS filter could be deleted
outright without changing one sample of anything: FFmpeg's encoder set
`tns_data_present` in two channel-frames out of 203 and signalled no filters
in both. Coverage a threshold cannot see is coverage that is not there, so
`test_the_tools_and_the_layouts_are_all_actually_reached` now asserts it
directly, and the TNS vector comes from Apple's AudioToolbox encoder --
which is macOS-only, and so the one vector `make_aac_vectors.sh` cannot
regenerate everywhere. Nothing at test time needs any encoder.

AAC is not a bit-exact specification -- the standard defines the transform
in real arithmetic -- so the comparison is numerical, and the numbers are:
worst case across all fourteen vectors, a maximum absolute sample error of
3.6e-07, an RMS error of 3.4e-08 and an SNR of 137.9 dB. FFmpeg's own
float32 output quantises at 6e-08 near full scale, so that is a handful of
ulps and not a disagreement about decoding. The test asserts 1e-06, 1e-07
and 130 dB. For scale, with the tools deleted one at a time: no TNS is
29 dB, and a noise seed one bit out is 2e-04 in the first frame of a tone.

A threshold at the end of a pipeline can hide a bug in the middle of it, so
the stages that can be compared exactly are. Every frame of every vector is
consumed to the bit -- the number of bits read equals the number the ADTS
header promised, which no wrong codeword length in any codebook can leave
true. Dequantisation is recomputed in Python from the decoder's own
quantised values and scalefactors and agrees to the last bit. The windows
are checked against their closed forms and against Princen-Bradley.

**Noise substitution is a deliberate copy.** A PNS band carries an energy
and no coefficients: the decoder is told to put noise of that loudness
there, and *which* noise is left to the decoder. There is no correct answer,
which means there is also no way to compare a PNS band against a reference
decoder unless both draw the same numbers. So `IPRAND` reproduces FFmpeg's
generator and its seed exactly, and this is the one place in the project
where the implementation was chosen to match another program rather than a
standard. Without it these vectors would agree everywhere except in the
bands that happen to be noise-substituted, which is most bands of a quiet
tone, and the SNR would fall from 138 dB to about 72 -- which is what it
did, for a day, before the seed was right.

**Why Fortran.** The same reason as H.264, and the licence's condition 6,
but the shape of the code is different: this is floating-point kernels over
contiguous arrays -- an FFT butterfly, a windowed overlap-add, a Levinson
step-up -- which is the workload Fortran compilers have been aimed at for
fifty years, and it benchmarks within a small factor of C without any of it
being written cleverly.

**How it is built and loaded.** Exactly as `h264.py` does it: find a
gfortran, compile the five sources into a shared library named after a hash
of them, load it with `ctypes`, check the ABI integer, and remember the
failure if any of that does not happen. No compiler means
`aac.available()` is false and an AAC track is named and refused in public,
which is the same contract every other unplayable file gets.

The state is in COMMON, so there is one decoder in the process. Audio makes
that sharper than video did: there is no keyframe in AAC and every frame's
first half is the previous frame's windowed transform tail, so a decoder
cannot start clean anywhere. Rather than replaying the stream the way
`h264.Decoder` replays access units, each `aac.Decoder` saves its own 2048
doubles of overlap out of the library and puts them back when it finds
another instance has been at it in between. Two `<audio>` elements are then
slow and correct rather than fast and clicking.

**What it is not.** No SBR, so no HE-AAC; no Parametric Stereo; no Main
profile prediction; no LTP; no SSR gain control; no coupling channels; no
LFE; nothing above two channels; no 960-sample frames. None of those is
stubbed or half-written -- each is recognised and refused with a status code
and a sentence of its own, because a decoder that silently ignores SBR
produces something that sounds like a bad phone line and a decoder that
ignores PS produces mono.

Refusing SBR is a decision about the *tool*, though, and not about the
signalling, and conflating the two cost us most of the web's AAC for a
while. An `AudioSpecificConfig` may append an 11-bit sync word (`0x2B7`)
and an object type of 5 to say "SBR is described below", and what follows
that object type is `sbrPresentFlag`, which is allowed to say no. FFmpeg's
MP4 muxer writes the whole extension into every file it encodes straight
into MP4 -- `121056e500` -- whether or not the encoder used the tool, and
writes the bare `1210` for the identical coded frames when it remuxes from
ADTS instead. Refusing on the object type alone therefore turned down
ordinary AAC-LC that we decode perfectly, and the two configurations are
committed as a pair of fixtures (`sbr_signalled.mp4`, `sbr_absent.mp4`)
whose samples are asserted to be identical, because the config parse is
allowed to permit a decode and not to change one. With the flag set, the
stream really is HE-AAC and is refused as before.

Two smaller gaps worth naming. Pulse data is implemented and is not covered
by any fixture, because FFmpeg's encoder never emits it. And the decoder
refuses more than two channels rather than downmixing, so a 5.1 soundtrack
is silent rather than folded.

## MPEG Layer III, in Fortran

The other half of the web's audio, and the older half. It is in
feetplayer's `fortran/ball*.f`, wrapped by `feetplayer/ball.py`, and it is called the
*ball of the foot* -- the pad behind the toes that takes the push. `BL*`
routines and `/BL*/` COMMON blocks, against AAC's `IP*` and H.264's `H2*`,
for the same reason as before: Fortran has one global namespace, this
process may hold all three, and a collision there links cleanly and
corrupts memory at runtime.

**Exactly what it does.** MPEG-1, MPEG-2 (LSF) and MPEG-2.5 Layer III --
ISO/IEC 11172-3 and 13818-3, and the low-rate extension the standards do
not contain -- at all nine sampling frequencies from 8 kHz to 48 kHz. Frame
header and its CRC-16; ID3v2 skipping and resynchronisation; side
information; the bit reservoir; scalefactors on the long path, the
mixed/short path, MPEG-1's `scfsi` inheritance and MPEG-2's
`scalefac_compress` partitioning including its intensity-stereo variant;
Huffman decoding across all thirty-two table selections, their linbits
escapes and both count1 quadruple tables; requantisation; mid/side and
intensity stereo in both the MPEG-1 and MPEG-2 forms; alias reduction; the
IMDCT with all three block types and window switching; the short-block
reorder; frequency inversion; and the polyphase synthesis filterbank. Mono
and stereo, out to float samples in [-1, 1].

**The bit reservoir is the part people get wrong.** A Layer III frame's
main data does not begin in that frame: `main_data_begin` points back up to
511 bytes, which at 32 kbit/s is four and a half frames. So the decoder
keeps a reservoir behind the bit reader rather than a frame buffer, and a
frame that reaches back further than the reservoir goes is *starved* --
its granules decode as silence and decoding continues, which is what a
player that has just seeked needs. `lowrate` is the vector for this:
started from its seventh frame it starves for seven frames, is silent for
exactly those seven, and then rejoins the reference to float32 precision
two frames later. Reading whatever was in the buffer instead would make
noise that no comparison against a whole file would ever show.

Measured on this machine, one core: 0.11 ms for a 44.1 kHz stereo frame,
which plays for 26.1 ms -- about 225x realtime.

**Ground truth.** feetplayer's `tests/fixtures/mp3` holds eighteen streams
and, beside each, the exact float samples FFmpeg 7.1 decoded it to, zlib-compressed.
`make_mp3_vectors.sh` regenerates them and says what each is for. They
cover a tone, noise, mid/side, a transient that forces short blocks with
the start and stop windows either side, 32 kbit/s under reservoir
pressure, 320 kbit/s into the linbits escapes, plain stereo where neither
stereo tool applies, all nine sampling frequencies, MPEG-1 intensity
stereo, MPEG-2 intensity stereo, and mixed blocks with the CRC.

Two of the eighteen are not simply an encoder's output, and both say so in
the script and in the test file. `mixed.mp3` is assembled bit by bit,
because no encoder in circulation sets `mixed_block_flag` and libmp3lame
does not write the optional CRC either; the frames conform, FFmpeg reads
them, and FFmpeg's reading of them is the truth beside them, exactly as
for every other vector. `lsfint.mp3` is a real MPEG-2 joint-stereo stream
whose `mode_extension` was overwritten to 1 in every header, because
nothing here will emit LSF intensity stereo on demand; it validates our
LSF intensity path against FFmpeg reading the same bytes, and it is not a
stream LAME would have produced.

Layer III is not a bit-exact specification either, so the comparison is
numerical -- and the threshold is per vector rather than one number for
all of them, because the spread is forty decibels wide and a single
threshold loose enough for the worst would be meaningless for the rest.
Most vectors land at 127 to 133 dB, which is float32 rounding. Three do
not: `transient` at 89.5 dB, `tone` at 94.8, `lsfint` at 95.4, in all
three because the error is localised in a handful of samples where the
true amplitude is near zero beside a loud attack. FFmpeg's own two
decoders disagree by more than we disagree with either -- on `transient`,
its float against its fixed-point is 85.8 dB where we are 89.5 -- so our
double-precision output sits inside the spread between two conforming
implementations.

For scale, with a stage deleted at a time: no alias reduction takes
`dual` from 101.5 dB to 9.0 and `tone` from 94.8 to 33.6; no intensity
stereo takes `intensity` from 115.6 dB to 79.5, which is forty times its
own threshold; and ignoring the reservoir is not a tolerance question at
all, because the first vector tried then fails to decode.

The stages that can be compared exactly are, as for AAC and for the same
reason. Every granule of every vector is consumed to the bit -- 639 of
them -- which no wrong codeword length in any of the thirty tables can
leave true. Requantisation is recomputed in Python from the standard's
formula and agrees to the last bit, because the exponent is an integer
count of quarter powers all the way through. The IMDCT is held against the
standard's summation at both sizes and on every column. And the whole back
half -- the four window shapes, overlap-add, frequency inversion and the
filterbank with its 512-tap window -- is written out again in Python
straight from the standard and agrees with the decoder's own output to
float32 rounding.

**How it is built and loaded.** Exactly as `aac.py` does it, which is
exactly as `h264.py` does it: find a gfortran, compile into a library
named after a digest of the sources and the compiler, prefer a shipped
prebuilt over compiling, load with `ctypes`, and remember the failure if
any of that does not happen. No compiler means `ball.available()` is false
and an MP3 is named and refused in public. `python3 -m feetplayer.ball
--check stream.mp3 truth.f32.z` is what the packaging asks, and it decodes
a real vector rather than merely loading the library.

The state is in COMMON, so each `ball.Decoder` saves its overlap, its
filterbank history *and* its reservoir out of the library and puts them
back when it finds another decoder has been at it in between. Layer III
needs the third of those where AAC needed only the first.

**What it is not.** Layer I, Layer II, free-format bitrates and more than
two channels are each refused by name, with a status code and a sentence
that says what to do -- because "unsupported" on its own is a useless
thing to tell somebody whose file will not play.

**How it is reached.** `mediacodec.sniff()` recognises a bare MP3 by its
ID3 tag or by a frame header that is self-consistent, `probe_audio()`
names it, and `open_audio()` walks the frames into an `AudioTrack` whose
packets and timestamps come from the headers. That is the whole of it: an
MP3 carried inside an MP4 as `mp4a` is still named and not decoded, and
the `<audio>` element does not exist, so an `.mp3` opened through
`mediacodec` is the only route in.

## PCM

The other kind of sound, and it is not a codec. A PCM packet already *is*
the waveform; the work is reading it at the right width, with the right
sign convention, the right way round, and scaling it into the [-1, 1]
floats the mixer takes. `_Pcm` in `mediacodec.py` is that, and it is forty
lines because there is nothing else to it.

**What plays.** In MP4 and MOV: `sowt` and `twos` (16-bit, the two byte
orders), `raw ` (unsigned 8-bit), `in24`, `in32`, `fl32`, `fl64`, and
`lpcm`. In AVI: WAVEFORMATEX tag 1 and tag 3, gathered out of the `##wb`
chunks the `movi` list interleaves between the pictures. And `.wav` itself,
which is a container this now reads: a RIFF walk to `fmt ` and `data`, the
same WAVEFORMATEX parse as AVI's, and one range of bytes.

**The fourcc is where it starts and not where it ends.** A QuickTime sound
sample entry has three versions and the later two carry the width, the byte
order and the sign convention explicitly. Where they disagree with the
fourcc, the fourcc is the stale one, and the disagreements are not
hypothetical: FFmpeg writes a version 1 `in24` whose `sampleSize` is 16 and
whose samples are 24 bits wide, because `sampleSize` and `bytesPerSample`
describe the canonical *unpacked* form rather than what is on disk -- the
real width is `bytesPerPacket` over `samplesPerPacket`. It writes the same
`in24` fourcc with an `enda` box saying 1 for little-endian audio, which is
the only signal anywhere that the bytes are the other way round. And a
version 2 entry's fourcc is `lpcm`, which says nothing at all: the width is
in `constBitsPerChannel` and the sign, the byte order and floating-pointness
are bits in `formatSpecificFlags`. `_mp4_pcm_layout` is where all of that is
resolved, and the comment on it names each case.

**Blocks are ours to choose.** Uncompressed sound has no coded frame, so
every container invents one and none of them is a size worth playing: a
QuickTime `stsz` for a `sowt` track has one entry per PCM frame -- 22050
four-byte "samples" for half a second of 44.1 kHz stereo -- and an AVI's
`##wb` chunks are however much sound fits beside one picture. `_pcm_blocks`
merges the file-contiguous ranges and re-cuts them at a tenth of a second,
which it is allowed to do only because PCM carries nothing across a join.
The tenth is bounded on both sides by `arch.py`: below `TARGET_QUEUE /
DECODE_BUDGET` the player cannot fill its queue in one `pump()`, and much
above it a block is latency a pause cannot take back and sound a seek must
throw away.

**It is also the first random-access track.** `_Pcm` sets `stateless`, and
`AudioTrack.frame()` reads that and answers an out-of-order request by
decoding the one block. AAC cannot: every frame's first half is the previous
frame's transform tail, so a seek there replays from the start.

**What is refused, by name.** ADPCM (Microsoft's and IMA's), mu-law and
A-law each get their own sentence rather than a shared "unsupported",
because each is a real decoder we have not written and none of them becomes
PCM by being read harder -- two are companded through a curve and two are
deltas against a running predictor. QuickTime's `ima4`, `ulaw` and `alaw`
sit in the same table.

Nothing above changes anything downstream. `AudioTrack` was already the
interface, so `arch.py`, `heel.py` and `media.py` are untouched by this and
do not know PCM exists.

## What is not supported

Bluntly, because a foundation that overstates itself is worse than none:

- **Sound still only arrives alongside a picture.** The `<audio>` element
  is not implemented at all, so a `.wav`, an `.mp3` or a soundtrack has to
  be the audio half of a `<video>` to be heard. MP4 with AAC-LC plays in
  sync, and so does uncompressed PCM in MP4/MOV, AVI and `.wav` -- see
  [Sound and pictures together](#sound-and-pictures-together) and
  [PCM](#pcm). A bare `.mp3` decodes through `open_audio` as well -- see
  [MPEG Layer III, in Fortran](#mpeg-layer-iii-in-fortran) -- but an MP3
  stored inside an MP4 under `mp4a` is still named and not decoded. WebM's
  audio is named by `probe_audio` and not decoded, for want of a Vorbis or
  Opus decoder, as are ADPCM, mu-law and A-law.
- **Layer III only, of the MPEG audio layers.** Layer I, Layer II, a
  free-format bitrate and more than two channels are each refused by name
  with a status code of their own. Free format is the one worth knowing
  about: such a file carries no bitrate in its header at all, so there is
  no frame length to trust, and it is refused rather than guessed at.
- **No user-facing volume or mute.** `VideoPlayer.set_volume()` and
  `AudioPlayer.muted` exist and are tested; nothing in the GUI calls them,
  and `volume` and `muted` are not scriptable from JavaScript yet.
- **AAC-LC only.** HE-AAC (SBR), HE-AAC v2 (Parametric Stereo), Main
  profile, LTP, SSR, coupling channels, LFE, more than two channels and
  960-sample frames are each refused by name with a status code of their
  own. SBR is the one worth knowing about: a low-bitrate web stream is
  often HE-AAC, and it is refused rather than played at half its bandwidth.
  What is *not* refused, and is worth knowing about for the opposite
  reason, is an AAC-LC config that merely carries SBR's signalling with
  the flag clear -- see below.
- **No inter-frame codec other than H.264.** I, P and B slices decode, under
  either entropy coder, which is what an ordinary well-compressed web MP4 is;
  but there is no VP8, VP9, AV1 or MPEG-4 ASP at all. A stream with SP or SI
  slices is refused by name before anything is drawn, as is one whose SPS
  uses picture order count type 1 or asks for more than four reference
  frames, and so is the one combination the two halves of the entropy layer
  do not meet in: a B slice coded with CAVLC.
- **WebM is probe-only.** Geometry and duration, no pixels. An MP4 is
  demuxed but only plays if its codec is `jpeg`, `mjpa`, `raw `, `png `, or
  H.264 without SP or SI slices.
- **No streaming.** The whole file is fetched into memory before the first
  frame. No range requests, no progressive start, no HLS or DASH. An MJPEG
  camera stream over HTTP, which never ends, therefore cannot be played even
  though its frames are the format that does.
- **Controls are play/pause and a scrubber.** Still no volume slider, but
  the reason has changed: there is now something to make quieter
  (`Output.volume`, and a per-source `gain` under it), and what is missing
  is the widget and the mute state, not the machinery. No fullscreen, no
  poster frame, no playback rate, no buffered ranges, no keyboard focus or
  shortcuts, no captions.
- **No JavaScript media API.** No `HTMLMediaElement` on the DOM bridge:
  `play()`, `pause()`, `currentTime`, `timeupdate` and friends do not exist.
  `autoplay`, `muted` and `preload` are ignored; `loop` is honoured.
- **No progressive or 12-bit JPEG in MJPEG.** Whatever
  `imagecodec.decode_jpeg` reads is what a frame can be, which is baseline
  and progressive 8-bit; arithmetic coding and 12-bit are not.
- **AVI subsetting.** `BI_RGB` at 8/24/32 bpp, `BI_RLE8` and MJPEG. Not
  `BI_RLE4`, not 1/4/16 bpp, not `BI_BITFIELDS`, not OpenDML `indx` for
  files above 2 GB.

Performance is honest too. Pure-Python `BI_RGB` decode measures roughly 3 ms
per frame at 160x120, 13 ms at 320x240 and 53 ms at 640x480 on the machine
this was written on. So uncompressed standard definition at 24 fps is already
past budget, which is exactly why the drop-frames-and-resync path exists and
is tested rather than assumed. MJPEG is a different story, because the decode
is in Rust: about a millisecond a frame at 320x224, which leaves the
rasteriser and the rest of the browser the other 39 ms of a 25 fps tick.

## Slotting in a real codec

The seam is `_Codec`: `reset()`, and `decode(packet, keyframe)` returning
RGBA bytes for one frame. A new codec is a class implementing those two
methods, plus a line in the container's codec selection mapping its fourcc,
and a keyframe rule so `keyframe_before()` is truthful. Nothing above that
line changes: the scheduler, the drop logic, the resync, the layout box and
the paint path are all codec-agnostic today, and the RLE8 path exists
specifically to prove that an inter-frame codec needs no new machinery.

One thing a real codec will want that is not there yet: output that is not
RGBA. Every real video codec produces planar YUV, and converting in Python
per frame will dominate the decode. The colour conversion belongs in Rust
next to the rasteriser, and `_Codec.decode` should be allowed to return a
plane triple that the player converts once.

The other thing on this list used to be reordering, and it is now done. A
codec whose decode order is not its presentation order needs nothing new:
`VideoTrack` indexes everything by presentation order, reads `ctts` to find
out what that order is, and keeps a reorder buffer. A `_Codec` still sees
packets in decode order and returns one picture per packet, which is what
every codec wants to be handed.

## Ordered next steps

1. **Move the pixel loops to Rust.** `BI_RGB` row unpacking and the RLE8
   run loop are the two hot spots, and both are small, self-contained
   functions over `bytes`. This is the change that turns 640x480 from a
   slideshow into playback, and it needs no design decisions.
2. **Add a YUV plane path and a Rust YUV-to-RGBA converter**, before a codec
   that needs it arrives rather than after.
3. ~~**Animated GIF through the same player.**~~ Done, and not through this
   player: it turned out to want no player at all. The decoder returns every
   frame with its delay and `canvas.PhotoImage` steps through them off the
   tick that was already running for video, because the draw path blits
   whatever `photo.rgba` currently is -- so nothing in layout, the display
   list or the element tree had to learn that an image moves, and a GIF stays
   an `<img>` rather than acquiring a playhead and controls it has no use
   for. See
   [Animation lives in PhotoImage](rendering.md#animation-lives-in-photoimage-not-in-layout).
4. **`HTMLMediaElement` on the DOM bridge**: `play`, `pause`,
   `currentTime`, `duration`, `paused`, `ended`, and the `timeupdate` and
   `ended` events. Cheap, and it is what makes video scriptable.
5. **Streaming.** Range requests and a demuxer that can start before the
   last byte arrives. Worth doing now rather than later, because MJPEG over
   HTTP is a format we can already decode and cannot currently play at all:
   the stream never ends, so waiting for the last byte waits forever.
6. **Vorbis or Opus, and then SBR.** The join is done -- see [Sound and
   pictures together](#sound-and-pictures-together) -- and four containers
   now reach it: MP4/MOV, AVI, `.wav` and a bare `.mp3`. Two *compressed*
   formats do, AAC-LC and Layer III. What is left of the web's audio is
   WebM's, identified and refused for want of a Vorbis or Opus decoder;
   that is a codec-sized job rather than a demuxer-sized one, which is what
   makes it the next thing rather than the easy thing. After it, SBR, so
   that low-bitrate HE-AAC streams play at all. The smaller loose end is
   that an MP3 inside an MP4 under `mp4a` is still named and not decoded,
   even though the decoder for it is now here.

   The end-to-end path is no longer proved a half at a time:
   `media_fixtures.mp4_av()` writes both traks over real coded frames --
   Motion JPEG pictures and the AAC packets out of a committed vector -- and
   `test_one_file_with_both_codecs_in_it_plays_in_sync` drives one such file
   through both decoders and the clock between them. `mov()` could always
   write two traks, but every caller passed filler bytes as the audio, which
   is enough to test a demuxer and not enough to test a decoder: a file built
   that way probes as a supported 82-frame AAC track, decodes to nothing at
   all, and hands the device two hundred kilobytes of silence.
   feetplayer's `tests/fixtures/pcm/pcm.avi` is the same case in the other
   container: a
   real picture track and a real sound track in one AVI, this time with the
   sound uncompressed.
7. **Per-decoder H.264 state.** Both entropy coders and all three slice types
   are done, so what is left in the Fortran is not a feature but the shape of
   the thing: the decoder's state is `COMMON`, which is to say there is one
   decoder in the process, and two `<video>` elements share it by replaying
   their history at each other. That is correct and it is quadratic. Picture
   order count type 1 and long-term reference pictures are smaller items in
   the same file and are refused by name today.

## Tests

In `tests/test_render.py`, with muxers in `tests/media_fixtures.py`: real
AVI, MOV, MP4 and WebM files built byte by byte at test time, so nothing
binary is committed. That includes a baseline JPEG *encoder*, because an
MJPEG fixture has to contain real JPEGs and there is nowhere to get one from
that is not a committed binary or a library. It uses the Annex K Huffman
tables rather than codes of its own, which is what makes the abbreviated-form
test real: strip the `DHT` segments back out and the frame must still decode
to the same pixels, which it can only do if the decoder's copy of the
standard tables is right.

The data-layer tests assert frame counts, dimensions, per-frame
timing from `dwRate`/`dwScale` and from `stts`, and exact pixel values for
frames whose content is known (the frame index is literally written into a
pixel, so a test can tell you *which* frame it is looking at: for JPEG
fixtures the colour is constant across each 8x8 block, so the round trip is
exact to within a count, and a single bright pixel in a flat field would be
measuring the transform rather than the codec). Robustness is tested by
feeding every truncated prefix of a valid file, hostile chunk sizes, RLE
opcodes that run off the frame, and an RLE stream that never terminates, each
under a deadline, asserting a clean error rather than a crash or a hang. The
scheduling tests run entirely on `ManualClock`: a slow decoder given a
fraction of the budget it needs is asserted to end at exactly the right
position with a bounded lag and a nonzero drop count. The integration tests
go through the real browser (page load, layout, click to play, and reading
the presented pixel back off the rendered surface), and the control bar is
driven the same way, through `Tab.click`, so what is tested is the path a
mouse takes rather than a method call.
That suite is also where the seam to feetplayer is proved. It imports
`feetplayer.mediacodec` directly and drives it through `feetbrowser.media`,
so a version of feetplayer whose container layer had drifted would fail here
by name rather than in somebody's browser -- which is what a pinned
dependency buys and what an unpinned one would not.

**The decoder suites moved with the decoders.** `test_h264.py`,
`test_aac.py`, `test_mp3.py` and `test_pcm.py`, and the fixture trees they
read -- fifty-odd streams and the exact pictures and samples a reference
decoder produced from them -- are feetplayer's now, and feetplayer's own
`test.sh` runs them. They are the same tests: H.264 compared byte for byte,
AAC and Layer III numerically against thresholds that are the measured error
with a small margin, PCM bit-identical where the arithmetic is exact, and in
each of them a test that takes the loaded library away and asserts the
browser-shaped degradation that follows. Nothing was weakened to move them,
and nothing about them is asserted here any more, because a test that runs
in two repositories is a test that is maintained in neither.

**Four fixtures stayed.** `tests/fixtures/h264/mb1.264`, `mb1.i420.z`,
`tests/fixtures/aac/lowrate.aac` and `lowrate.f32.z` are still committed
here, and they are not read by any suite in `tests/`. They are read by the
three packagers: each builds an artifact, cuts `PATH` back to the system
directories so no compiler and no Homebrew library can be reached, and runs
`--check-video` and `--check-audio` inside the finished bundle against those
four files. A bundle that shipped no decoder -- or, now, no feetplayer at
all -- passes every other check a packager makes, installs, starts, renders,
and admits it only to whoever opens a video. Deleting these four would make
all three verifications pass for no reason at all, which is why they are
called out here.

Audio has its own suite, `tests/test_audio.py`, and it is the browser's half
of the sound seam rather than a copy of feetplayer's suite of the same name: the clock a `<video>`
is scheduled against, `restart()` across a seek, the pictures following the
sound, and a `<video>` element asking for its own audio through
`browser.py`. What `arch.AudioPlayer` decodes, and every property of the
ring, the resampler and the mixer under it, is tested over in feetplayer.
`FEETBROWSER_AUDIO=null` still forces a machine with a working sound card to
behave like one without. See
[Testing sound without a sound card](#testing-sound-without-a-sound-card).
