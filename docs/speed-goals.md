# Speed goals

*Where the browser is today, where it could get to, and what each step costs.*

This document is the ledger for speed the way [rendering.md](rendering.md) is
the ledger for pixels: numbers, not adjectives. Every claim below is either a
number already measured in this repo (cited) or an estimate derived from the
architecture and *marked as such*. Nothing here is a promise; it is a map.

## What "speed" means here

A browser is two different complaints, and they are not the same complaint:

- **settle** — wall clock from `new_tab(url)` to a settled page, network
  included. A page that takes three seconds to finish arriving is slow.
- **stall** — the longest single span the UI thread spent inside one batch of
  timer callbacks, i.e. the longest stretch in which the window could not have
  repainted, scrolled or answered a click. A page that holds the UI thread for
  three of those seconds is a *frozen browser*.

Both are read off `tests/bench_pageload.py`, which defines the difference this
way: "A page that takes three seconds to finish arriving is a slow page. A page
that holds the UI thread for three of those seconds is a frozen browser, and
those are not the same complaint. `stall` is the one the user filed."

So the goal is not a single number. It is: **settle close to a modern browser,
and stall small enough that nobody can feel it.** "Total speed parity" with a
modern engine is a direction, not a deliverable; the tiers in this document
say which parts of it are worth paying for.

## The honest headline

The renderer's *leaves* are already at modern speed. The parts that touch
pixels one at a time — the rasteriser, the font engine, the image and video
codecs — are compiled, and the numbers in the ledger below are respectable
even against a browser that has a GPU. The gap lives in the *tree* that stands
on those leaves: an interpreted JavaScript engine, a Python layout pass, a
per-operation host bridge, and one UI thread. That is where the one to two
orders of magnitude sit.

Nothing in this document proposes fixing the leaves. Everything proposes
fixing the tree, and the tiers say in which order.

## Current speed: the ledger

### How to measure

Two scripts, both already in the repo, both written to be run before and after
a change so a change is argued about with numbers:

```bash
.venv/bin/python tests/bench_render.py      # the pixel pipeline
.venv/bin/python tests/bench_pageload.py    # a page load, settle and stall
```

`bench_render.py` prints milliseconds per call, the mean over enough
repetitions to outrun the clock. `bench_pageload.py` serves a page locally with
a fixed delay on every request (4 sheets, 16 scripts, 6 videos by default) and
prints `settle` in seconds and the worst `stall` in seconds. Every figure in
this document came from those two scripts or from a benchmark cited below; run
them on your machine before trusting the numbers here, because these are one
machine's numbers, and the interesting number is the shape, not the digit.

### Measured

The numbers this repo has already earned, with where they live:

| What | Measured | Where it was measured |
|---|---|---|
| JPEG decode, 800×600 | 6.5 ms | [dependencies.md](dependencies.md#pillow-and-the-jpeg-decoder-that-replaced-it), [rendering.md](rendering.md) |
| JPEG decode, 1800×1200 | 27 ms | dependencies.md |
| JPEG throughput | 60–90 Mpixel/s | dependencies.md |
| Warm-cache text draw, 40×135 characters | 0.97 ms (was 8.4 ms in Python) | [rendering.md](rendering.md) |
| MJPEG frame decode, 320×224 | ~1 ms | [media.md](media.md) |
| MJPEG playback, 25 fps | ~2.5% of a core | media.md |
| Rasteriser | full-surface fill, text, glyph, PNG-out per `bench_render.py` | tests/bench_render.py |
| Page load, local server, 50 ms/request | `settle` in seconds; worst `stall` per run | tests/bench_pageload.py |

The full pixel-pipeline list is in `tests/bench_render.py`: rectangle fills,
90-character text draw, text measurement, uncached glyph bitmaps, a 200×200
star rasterisation, PNG encode of a 1000×700 surface, PNG decode, image
resize, font parse + cmap, cold glyph-contour extraction and outline
flattening. These are the leaves, and the leaves are not the problem.

### Estimated (not yet benchmarked)

Everything that is not a pixel touches one of these. None of them has a
benchmark script yet, so each is a range derived from the architecture, and
each is flagged as an estimate:

| What | Estimated | Basis |
|---|---|---|
| JavaScript execution | ~20–100× slower than a JIT engine | tree-walking interpreter with an 8M-step execution budget; no bytecode, no hidden classes, no inline caches |
| DOM property access | ~50–500× per operation | every `js_get`/`js_set`/`js_call` crosses Rust → PyO3 → Python attribute dispatch (`dom_get`/`dom_call`), then back |
| Layout | ~10–50× slower | `layout.py` (185 functions) tree-walks in Python; the compiled cascade in `css.rs` is the only compiled part of the path |
| Networking | ~1.5–3× slower | Python HTTP/1.1 plus an in-house HTTP/2; no QUIC, no native TLS, latency-bound rather than throughput-bound |
| Interaction/repaint | ~10–50× slower | no compositor: a scroll, hover or JS mutation re-lays-out and re-paints a region on the one UI thread |
| Whole-browser, simple content page | ~5–10× slower | network + parse + one paint pass dominate; JS is light |
| Whole-browser, modern JS-heavy page | ~20–100× slower, and janky-to-frozen | interpreter cost + per-op DOM crossing + a Python repaint per state change compound |

The one that gets reported is the last one. The one this document most wants
to see measured is the DOM-access row, because it is the cheapest row to fix
and the biggest single tax on real pages.

### Where the gap lives

Four layers, each one sentence:

1. **The JavaScript engine** is a from-scratch tree-walker. It is correct
   where it goes, but it has no JIT, no bytecode, no shape-based property
   lookup, so hot JS costs tens to hundreds of times what a modern engine pays.
2. **The DOM bridge** is a per-operation host call. Every property read or
   method call on a DOM object leaves the interpreter, crosses PyO3, and
   dispatches through Python attributes before touching the node tree.
3. **Layout is Python.** The cascade is compiled, but the block/inline layout,
   the flex/grid passes and the scene graph tree-walk in `layout.py`.
4. **Everything shares one UI thread.** Timers, fetch results, layout, paint
   and clicks all queue on the same thread; heavy work is a freeze, not a dip.

## The goals: tiers with gates

Every tier lands behind a gate, because the repo's own rule is that changes
are argued with numbers, not adjectives. The gates are the two bench scripts,
the JS suite (`tests/test_js.py`), the render suite and the e2e suite — the
same gates every other change already runs behind.

### Tier 0 — gates first (days)

Make `bench_render.py` and `bench_pageload.py` required gates: checked in CI
alongside the test suites, with the current numbers as the baseline and a
floor below which a change cannot land. Nothing below is safe to attempt
until a regression is a failed build rather than a hunch.

*Gate: the two benches run in CI; a 20% regression fails the build.*

### Tier 1 — the fast path (about 1–2 weeks of focused agents; 4–7 months solo)

The biggest chunk of the gap, and the part that does not require rewriting the
JavaScript engine at all:

- Make DOM nodes **native Rust objects** inside the interpreter, so a property
  read or method call is a direct call instead of a PyO3 round trip
  (`rust/src/dom.rs` already holds the DOM logic; what dies is the Python
  shim hop in `jsdom_rust.py`).
- **Wire in the existing Rust layout engine** (`rust/src/layout/`, 5,172
  lines): a containing-block/formatting-context engine that already compiles
  but is not yet the live path. The live path is still `layout.py`.

Honest gaps before this tier is done, not hidden: the Rust layout builds its
box tree off the Rust DOM arena (`footnote::domtree`), not the live Python tree the
browser renders, so the tree moves to Rust first; flex/grid are still
"separate ad-hoc passes" in `layout.py` that the Rust engine names as future
work; and painting/scene-graph are still Python. Tier 1 is a real 1–2 week
agent sprint only because the Rust for the hard parts already exists.

*Gate: `bench_pageload` settle and stall both fall; `test_js.py`, render and
e2e suites stay green; the DOM-access row of the ledger gains a measured
number.*

### Tier 2 — a real JavaScript engine (months, serial)

The keystone, and the one thing headcount cannot compress: a bytecode compiler
plus a VM with **hidden classes and inline caches**, replacing the tree-walker
in `interp.rs` and the string-keyed property model in `value.rs`. This is not
"write a new interpreter"; it is *reprove every behaviour the old one has*
(the JS suite, jQuery 1.8.2, every page that works today) while being faster.
It is one contiguous rewrite of the most-coupled file in the repo, and it is
the part of the whole map that is Amdahl-bound: nobody helps, and agents are
weakest exactly here, because the curve is verification, not codegen.

*Gate: the new VM passes `test_js.py` and jQuery, is measurably faster than
the tree-walker, and `bench_pageload` is no worse.*

### Tier 3 — a compositor thread (months)

Move paint and scroll off the UI thread so the `stall` freeze class dies. The
rasteriser is already fast and owns its framebuffer; what is missing is a
thread to run it on while the main thread does JS and layout. This is the
riskiest surgery on `browser.py` (6,743 lines, single-threaded by design), and
it is independent of Tier 2, so it can run beside it.

*Gate: the worst `stall` in `bench_pageload` drops below the frame budget;
scrolling during a busy main thread stays smooth.*

### Tier 4 — full parity (years, and probably not worth it)

A JIT (Cranelift on top of the Tier 2 VM) to close the last 10–50× on hot JS,
plus incremental layout and the remaining long tail. Stated honestly: this is
re-deriving a meaningful slice of a modern engine's compiler, and even the
engines that have it spend decades on it. Tier 4 is the direction, not the
commitment. Tiers 1–3 are where the felt difference lives.

## What each can become: the matrix

Subsystem × current → target → effort → time. Agent time assumes max-throughput
AI agents on the parts that parallelise; solo time is one person. The keystone
row (JS engine) is the same in both columns, which is the point.

| Subsystem | Today | Target | Effort | Agents | Solo |
|---|---|---|---|---|---|
| DOM bridge | ~50–500×/op PyO3 hop | native Rust VM objects, direct calls | wire `dom.rs`, kill shims | 1–2 wk | 2–4 mo |
| Layout | Python tree-walk | wire the existing Rust engine + incremental reflow | integrate 5,172 lines; flex/grid, paint remain | 1–2 wk | 2–3 mo |
| JavaScript engine | tree-walker, ~20–100× off | bytecode VM + hidden classes + inline caches | rewrite `interp.rs`/`value.rs` | 3–5 mo (serial) | 5–7 mo |
| Rendering/compositor | one UI thread, seconds-long stalls | compositor thread | surgery on `browser.py` | 2–3 mo | 2–4 mo |
| JavaScript, full parity | — | JIT (Cranelift) on the VM | on top of Tier 2 | +6–12 mo | +1–2 yr |

Three caveats that keep the matrix honest:

- **Verification dominates codegen.** Writing a bytecode compiler is fast; making
  it preserve every behaviour the tree-walker already has is where the time
  goes, and it is the same for a person and an agent.
- **The keystone is serial.** Tiers parallelise around it, never through it.
- **The binding constraint is "grow it yourself."** This repo's rule is that
  there is no crate for that, which is why every row is built in-house — and
  why the fastest-to-deploy option is not on this map (below).

## What we will not do

One footnote, because it is asked often: this map is in-house only. A
third-party JavaScript engine (Boa and friends) is the fastest route to a
bytecode VM and a full standard library on paper — a week, not months — but
it is a "sock" under this project's own rules: the README promises a browser
that "does its own … JavaScript," and the licence is explicit that "there is a
crate for that" is not a defence. It would also not deliver speed parity: such
an engine is still an interpreter, 10–50× off a JIT, and the DOM bridge, the
Python layout and the one UI thread would still be the slow parts. The map
above is the map of the project we are actually building.

## How progress is judged

The same six principles every change is judged on, applied to speed:

- **Simple** — one tier at a time; no architecture that exists to impress.
- **True to spec** — speed is never a licence to break a behaviour the JS
  suite, jQuery or the render suites already own.
- **Readable** — a fast path nobody can read is a bug deferred.
- **Iterative** — every tier lands behind its gate, continuously, with the
  numbers attached.
- **Don't Repeat Yourself** — one benchmark per measurement; the scripts that
  exist are the ones that get used.
- **Efficient** — the browser is judged on the two numbers, settle and stall,
  and nothing else gets to call itself fast until both move.

*Measured, not estimated; argued with numbers, not adjectives.*