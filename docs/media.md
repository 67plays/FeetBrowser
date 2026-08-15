# Video

A `<video>` element lays out at the right size, decodes real frames out of a
real file, and presents them through the existing rasteriser against a real
clock, with play, pause and a scrubber. Motion JPEG plays — in AVI, in
QuickTime, and as a bare stream of JPEGs — as do uncompressed and RLE AVI and
QuickTime's `raw ` and `png `. Everything else is identified by name and
refused in public. There is no audio at all.

Read this before adding a codec. The point of writing it down is that the
hard parts here are not the codec — they are the seams the codec plugs into,
and those are done.

## Why Motion JPEG

Because it is the only genuinely compressed video format this project can
decode for free, and "for free" is doing real work in that sentence.

The rule is no third-party code, so a codec has to be written here. H.264 is
a multi-month project on its own — CABAC, deblocking, six intra prediction
modes, quarter-pel motion compensation — before the patent question is even
asked, and VP8, VP9 and AV1 are the same shape of problem. What is left is
the small set of formats whose decoder is short enough to read: uncompressed
frames, run-length frames, and pictures.

MJPEG is pictures. Every frame is a complete baseline JPEG, and the JPEG
decoder was already written for `<img>` and already lives in Rust next to the
rasteriser, where it decodes a 320x224 frame in about a millisecond. That is
around two and a half per cent of a core to play 25 frames a second, against
the 13 ms a frame that pure-Python `BI_RGB` costs at 320x240 — a format that
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
start for a codec in which every frame is independent by definition — so for
MJPEG the index is overruled rather than believed.

## Why AVI as well, and why uncompressed and RLE

The brief for a from-scratch browser rules out the obvious answer. We cannot
link ffmpeg, and H.264 is a multi-month project on its own — CABAC,
deblocking, six intra prediction modes, quarter-pel motion compensation —
before the patent question is even asked. So the choice was between formats
that are genuinely writable in readable Python: animated GIF, MJPEG,
container parsing on its own, and an uncompressed or RLE codec in a simple
container. Animated GIF belongs to the image decoder and is being rewritten
by someone else right now; MJPEG needed a JPEG decoder, which at the time was
also somebody else's live work and has since landed. Container parsing on its
own puts nothing on screen. What is left is AVI carrying `BI_RGB` and
`BI_RLE8`: a container simple enough to parse exactly (RIFF is
length-prefixed chunks and nothing else), with two codecs that are
patent-free, decodable in a few
hundred lines, and — this is the part that matters for a *foundation* —
between them exercise both halves of the problem. `BI_RGB` is intra-only, so
any frame is a seek target. `BI_RLE8` has delta and end-of-bitmap escapes, so
a frame depends on the one before it, and the player has to keep decoder
state, find the keyframe before a seek target, and replay forward. That is
the shape every real codec has. Getting it right against a codec you can read
in an afternoon is the whole reason to start here.

## The three layers

The split is deliberate: **bytes**, **time**, **pixels**. Each is testable
without the other two, and only the last one knows what a canvas is.

### `feetbrowser/mediacodec.py` — bytes

Containers and codecs. No clocks, no threads, no canvas, no imports from the
browser. Everything in it is a pure function of a `bytes` object.

- `sniff(data)` returns `"AVI"`, `"MJPEG"`, `"MOV"`, `"MP4"`, `"WebM"` or
  `""` from the magic. `"MJPEG"` is the containerless case: a file that
  begins with a JPEG and holds nothing but JPEGs.
- `probe(data)` returns a `MediaInfo` — container, codec fourcc or name,
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
rather than an average rate — `VideoTrack.index_at()` bisects them, and the
scheduler asks it rather than dividing.

WebM is still parsed only far enough to answer `probe()`: EBML
variable-length IDs and sizes to `PixelWidth`, `PixelHeight`, `CodecID`, and
`Duration` times `TimecodeScale`. It decodes no pixels and does not pretend
to. An MP4 whose codec we lack is the same — the boxes are read, the numbers
are real, and the answer is still no.

### `feetbrowser/media.py` — time

Decoding and presenting are two different jobs on two different schedules,
and conflating them is how a player drifts.

- `Clock` is injected. `SystemClock` reads `time.monotonic`; `ManualClock`
  is set and advanced by hand, which is what the tests use, so frame timing
  is asserted deterministically rather than by sleeping and hoping.
- `Scheduler` owns the playhead. Position is always
  `clock.now() - origin + offset` — never a count of frames presented. A
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

### `layout.py` and `browser.py` — pixels

Both changes are additive and local.

`layout.py` treats `video` as inline, sizes the box from `width`/`height`
attributes, then the container's declared intrinsic size, then 300x150 —
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
*geometry* changes without layout running again — the frame timer repaints
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

## What is not supported

Bluntly, because a foundation that overstates itself is worse than none:

- **No audio.** Not decoded, not parsed, not mixed, not synchronised. AVI
  audio streams are skipped. This is not a small omission — A/V sync is its
  own engineering problem — and nothing here is designed around it yet.
- **No inter-frame codec.** No H.264, no VP8/VP9, no AV1, no MPEG-4 ASP.
  A file carrying one is named in the placeholder, not guessed at. This is
  the big one: it is why YouTube, and essentially every video on a modern
  site, does not play.
- **WebM is probe-only.** Geometry and duration, no pixels. An MP4 is
  demuxed but only plays if its codec is `jpeg`, `mjpa`, `raw ` or `png `,
  which in practice means a QuickTime file rather than a web MP4.
- **No streaming.** The whole file is fetched into memory before the first
  frame. No range requests, no progressive start, no HLS or DASH. An MJPEG
  camera stream over HTTP, which never ends, therefore cannot be played even
  though its frames are the format that does.
- **Controls are play/pause and a scrubber.** No volume — there is nothing
  to make quieter. No fullscreen, no poster frame, no playback rate, no
  buffered ranges, no keyboard focus or shortcuts, no captions.
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
past budget — which is exactly why the drop-frames-and-resync path exists and
is tested rather than assumed. MJPEG is a different story, because the decode
is in Rust: about a millisecond a frame at 320x224, which leaves the
rasteriser and the rest of the browser the other 39 ms of a 25 fps tick.

## Slotting in a real codec

The seam is `_Codec`: `reset()`, and `decode(packet, keyframe)` returning
RGBA bytes for one frame. A new codec is a class implementing those two
methods, plus a line in the container's codec selection mapping its fourcc,
and a keyframe rule so `keyframe_before()` is truthful. Nothing above that
line changes — the scheduler, the drop logic, the resync, the layout box and
the paint path are all codec-agnostic today, and the RLE8 path exists
specifically to prove that an inter-frame codec needs no new machinery.

Two things a real codec will want that are not there yet. First, output that
is not RGBA: every real video codec produces planar YUV, and converting in
Python per frame will dominate the decode. The colour conversion belongs in
Rust next to the rasteriser, and `_Codec.decode` should be allowed to return
a plane triple that the player converts once. Second, an inter-frame codec
with B-frames needs decode order and presentation order to differ; `VideoFrame`
carries a pts already, but `VideoTrack` currently assumes the two orders
agree, and a reorder buffer belongs in `VideoTrack.frame()`.

## Ordered next steps

1. **Move the pixel loops to Rust.** `BI_RGB` row unpacking and the RLE8
   run loop are the two hot spots, and both are small, self-contained
   functions over `bytes`. This is the change that turns 640x480 from a
   slideshow into playback, and it needs no design decisions.
2. **Add a YUV plane path and a Rust YUV-to-RGBA converter**, before a codec
   that needs it arrives rather than after.
3. **Animated GIF through the same player.** The frames already exist in the
   image decoder; this is a `_Codec` adapter and a layout rule, and it gives
   `<img src=x.gif>` real timing instead of a first frame.
4. **`HTMLMediaElement` on the DOM bridge** — `play`, `pause`,
   `currentTime`, `duration`, `paused`, `ended`, and the `timeupdate` and
   `ended` events. Cheap, and it is what makes video scriptable.
5. **Streaming.** Range requests and a demuxer that can start before the
   last byte arrives. Worth doing now rather than later, because MJPEG over
   HTTP is a format we can already decode and cannot currently play at all:
   the stream never ends, so waiting for the last byte waits forever.
6. **Audio.** A decoder, a resampler, a platform output device on three
   platforms, and A/V sync against the same clock. This is the largest item
   on the list by a wide margin and should be planned on its own. Nothing in
   the project outputs a sample today — there is no CoreAudio, ALSA or
   WASAPI binding anywhere in it — so this starts from zero.
7. **A real inter-frame codec.** VP8 keyframes first, if any: intra-only VP8
   is a bounded problem and it is the on-ramp to WebM. Everything past that
   is months.

## Tests

In `tests/test_render.py`, with muxers in `tests/media_fixtures.py` — real
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
pixel, so a test can tell you *which* frame it is looking at — for JPEG
fixtures the colour is constant across each 8x8 block, so the round trip is
exact to within a count, and a single bright pixel in a flat field would be
measuring the transform rather than the codec). Robustness is tested by
feeding every truncated prefix of a valid file, hostile chunk sizes, RLE
opcodes that run off the frame, and an RLE stream that never terminates, each
under a deadline, asserting a clean error rather than a crash or a hang. The
scheduling tests run entirely on `ManualClock`: a slow decoder given a
fraction of the budget it needs is asserted to end at exactly the right
position with a bounded lag and a nonzero drop count. The integration tests
go through the real browser — page load, layout, click to play, and reading
the presented pixel back off the rendered surface — and the control bar is
driven the same way, through `Tab.click`, so what is tested is the path a
mouse takes rather than a method call.
