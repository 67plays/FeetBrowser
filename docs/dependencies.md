# Third-party dependencies

FeetBrowser owns its stack: the rasteriser, the font engine, the event loop,
the transport layer, the image codecs and the JavaScript engine are all ours.
This file is the standing account of what is *not*, why it is still there, and
what it would cost to remove. Every number in it was measured on the tree, not
estimated from memory, and the JavaScript behaviour below was checked by
running the engine rather than by reading it.

Nothing here is a plan of record. It is reconnaissance, so that the next
person to pick one of these up starts from a real number.

## Where it stands

No Python package is *required*. `feetbrowser/` imports the standard library
and `feetbrowser_engine`, which is our own code in another language. One
Python package is optional, and the browser runs without it:

| package | what it buys | without it |
| --- | --- | --- |
| curl_cffi | Chrome's TLS fingerprint for sites that block ours | falls back to our own transport |

There were three. Pillow and cairosvg are gone: JPEG is decoded in
`rust/src/image.rs` now, and SVG is not decoded at all. The sections below
that argued about both are kept because the reasoning is what the next removal
will need, but they describe finished work rather than open questions.

The build side is heavier than the run side, and that is where most of the
cost actually is: Rust with five crates, `maturin` and a virtualenv to install
the extension into, `pyflakes` for lint, a Go toolchain for a package the
browser does not call, and `gfortran` for the H.264 and AAC decoders. The last of
those is the only compiler here that is genuinely optional at runtime; see
[Fortran](#fortran) below.

## The Rust crates

`rust/Cargo.toml` declares five dependencies. `Cargo.lock` holds **57
third-party crates**, and the distribution is extremely lopsided. Walking the
lock file's dependency graph and attributing each crate to the root that pulls
it in:

| declared crate | crates in its subtree | crates *only* it needs |
| --- | --- | --- |
| `chrono` | 35 | **30** |
| `pyo3` | 13 | 8 |
| `serde_json` | 10 | 6 |
| `regex` | 5 | 4 |
| `miniz_oxide` | 2 | 2 |

Fifty-six of the 57 are reachable from those five. The odd one out is `syn`,
which no package in the lock file lists as a dependency: it is a stale entry,
and `cargo update` would drop it.

### chrono: 30 of the 57 crates, for one line of real work

`chrono` backs JS `Date`. It brings in the entire `wasm-bindgen` family, six
`windows-*` crates, `iana-time-zone`, `core-foundation-sys` and
`android_system_properties`: 30 crates that nothing else in the tree wants.

What it is actually used for is smaller than that suggests. `Date` is 255
lines across `stdlib.rs:1161-1291` and `interp.rs:2047-2170`, and it is 26
read-only methods (eighteen getters, two formatter bodies, three statics),
with **none of the roughly nineteen mutators** (`setTime`, `setFullYear`,
`setMonth` and the rest are all absent). `Utc::now()` and `Local::now()` are
never called; `Date.now()` already goes through `std::time::SystemTime`. The
only thing the 30-crate subtree genuinely provides is `chrono::Local` at
`stdlib.rs:1241`, one line, which converts an instant to local civil time for
the eight non-UTC getters.

Civil-date arithmetic in both directions is about 40 lines of exact integer
code over the whole range JS can represent. A formatter is about 60, a parser
covering the five formats `Date.parse` accepts today about 80, and rewiring
the 26 getters about 120. Call it **250-350 lines of Rust to replace 255**,
and 30 crates gone.

Local time is the part that does not fall out for free. Three options: stay on
UTC, which is honest and is very close to what the code already pretends,
since `interp.rs:2058` and `2160` print the literal string `GMT+0000` while
formatting local fields; call libc `localtime_r` over FFI, which is the same
move `cocoa.py` and `x11.py` already make for their libraries; or parse TZif,
which is not worth it. Start with UTC.

Two bugs are worth fixing in the same change, because both are live today and
neither is covered: the entire `Date` test surface is one assertion,
`tests/test_js.py:724`, that `Date.now() > 0`:

```js
new Date(Date.UTC(2024, 0, 7)).getUTCDay()   // we return 6, JS says 0
```

`getDay` and `getUTCDay` use chrono's `num_days_from_monday`
(`interp.rs:2091`, `2099`), so **every weekday is off by one**. And time
components are formatted with `{}` rather than `{:02}` (`interp.rs:2134`,
`2154`), so 09:05:03 prints as `9:5:3`.

**Estimate: 2-4 days including the tests, which are most of the value.**

### serde_json: 6 crates, and hand-rolling is the more correct option

`serde_json` is used in eight places, all in `stdlib.rs`, all within thirty
lines, and all on the parse side. `serde_json::to_string` is never called and
nothing derives `Serialize` or `Deserialize`: `JSON.stringify` is already 92
hand-written lines (`stdlib.rs:990-1083`). So the split today is that serde
parses, we stringify, and an 18-line adapter at `stdlib.rs:1085-1102` converts
`serde_json::Value` into `JsValue`. A hand-rolled parser would build `JsValue`
directly and that adapter would simply disappear.

RFC 8259 is a page and a half. A correct JSON parser in Rust is **180-250
lines** and removes 6 crates.

Hand-rolling is also the more *correct* option, because JS's JSON has
semantics serde has no opinion about, and four of them are wrong today. All
four were confirmed by running the engine:

```js
JSON.stringify({b:1, a:2})        // we return {"a":2,"b":1}: keys sorted
JSON.stringify({d: new Date(0)})  // we return {}: the key is silently dropped
JSON.stringify({a:1}, null, 2)    // we ignore `space` and always minify
```

The ordering bug is structural: `JsValue::Object` is a `BTreeMap`
(`value.rs:85`), so output is alphabetical where JS requires insertion order.
`toJSON` does not exist anywhere in the tree, which is why the `Date` key
vanishes rather than becoming an ISO string. `replacer` and `space` are read
but never applied (`stdlib.rs:1076`), and `JSON.parse` ignores its `reviver`.
Cycles return `None`, so `"null"` in an array, and an elided key in an object
where JS throws `TypeError`.

The existing tests cannot see any of this: all four JSON assertions use
single-key objects, which is exactly the case where sorted and insertion order
agree.

**Estimate: 1-2 days for the parser alone. 3-5 days, and 350-500 lines, to
also make `JSON.stringify` correct**: insertion-ordered object storage is the
bulk of it, and it touches `value.rs` repo-wide.

### miniz_oxide: 2 crates, and the project already has a second answer

`miniz_oxide` arrived with the Rust renderer and is used in exactly one place,
`image.rs:88-89`, to inflate PNG `IDAT` with a 256 MB ceiling on the output so
a crafted file cannot exhaust memory.

The thing worth noticing is that PNG *encoding* does not use it.
`raster.rs:888-916` writes PNGs by calling back into Python's standard library
(`py.import("zlib")` then `zlib.compress(raw, 6)`). So the tree already
contains two deflate implementations for the same byte format, and the one the
project reaches for when writing is the one it did not have to depend on.

Making decode symmetric with encode is a small change:
`zlib.decompressobj().decompress(data, MAX_INFLATED)` enforces the same output
ceiling, and `unconsumed_tail` tells you when the limit was hit. That is
perhaps 15 lines in `inflate()` and removes 2 crates. The cost is a Python
call on the decode path, which matters more here than on encode, since decode
runs once per image on a page and encode runs only for `--screenshot`.

The other direction is to write the inflater. RFC 1951 is small and closed:
fixed and dynamic Huffman blocks, stored blocks, and a 32 KB window, which is
**300-400 lines of Rust** and would leave `Cargo.toml` free of it entirely. It
is a genuinely good candidate (bounded, well-specified, and easy to test
against the PNGs already in `tests/fixtures/`), but it is a decompressor
reading hostile bytes, so it wants fuzzing rather than confidence.

**Estimate: an hour to route it through `zlib`, or 2-3 days to write it.**

### regex: the largest item, and the largest correctness win

`regex` backs JS `RegExp`: `compile_regex` builds the `regex::Regex`, and the
compiled matcher is used at eleven sites across `interp.rs` and `stdlib.rs`.
There is also one unrelated use as `parseFloat`'s number scanner
(`stdlib.rs:431`).

The gap between Rust's `regex` and JS's `RegExp` is not bridged, because Rust's
`regex` is a finite-automata engine that deliberately has no backreferences and
no lookaround, and JS has both. `compile_regex` (`interp.rs:774-795`) does no
syntax translation at all: the JS pattern goes to `regex::Regex::new`
verbatim, wrapped only in `(?m:)` and `(?i:)`. And then line 785:

```rust
let re = regex::Regex::new(&pat).unwrap_or_else(|_| regex::Regex::new(r"[^\s\S]").unwrap());
```

**Any pattern Rust's `regex` rejects is silently replaced by one that can never
match.** No `SyntaxError`, no warning, no log line. Confirmed by running the
engine:

```js
/\d+/.test("abc123")                // true  (correct)
/(\w+)\s+\1/.test("hello hello")    // false (backreference)
/foo(?=bar)/.test("foobar")         // false (lookahead)
/(?<=x)\d+/.test("x42")             // false (lookbehind)
```

Each of those constructs successfully and quietly returns the wrong answer.
That is worse than a dependency; it is a dependency producing incorrect results
in a way nothing can observe. Only three of the eight JS flags are read
(`g`, `i`, `m`); `s`, `u`, `v`, `y` and `d` are lexed and discarded.
`String.prototype.search` is not implemented at all: it throws.

A JS-compatible backtracking engine is roughly **1,050-1,500 lines**: 350-450
for the pattern parser, 200-300 to compile it, 350-500 for the matcher, and
150-250 to rewire the seven entry points and fix what is broken around them.
It removes 4 crates. **Estimate: one to two weeks.** The matcher needs a step
budget so that a catastrophic pattern fails instead of hanging the browser;
that is a requirement, not a refinement, on something that runs untrusted code
from the network.

**One change is worth making immediately, whatever happens to the crate.**
Replace that `unwrap_or_else` with a thrown `SyntaxError`. It is about five
lines, it converts a silent wrong answer into a visible failure, and it tells
you how much of the real web actually needs lookaround before anyone commits
to writing a regex engine.

## maturin and pyo3

`maturin` is installed and invoked in three places: `run.sh:90-91`,
`test.sh:52-53`, and `.github/actions/build/action.yml:72-73`. It exists to
build `feetbrowser_engine` as a CPython extension module and install it into a
virtualenv. Most of `run.sh`'s 106 lines are in service of that: locating the
extension, comparing its mtime against `rust/src`, creating the venv,
unsealing a venv made before `--system-site-packages` was added, and printing
the three different failure messages the process can produce.

**What stands between this and a ctypes arrangement is `dom.rs`.**

The pyo3 decorator count is small (6 `#[pyclass]`, 6 `#[pymethods]`, 3
`#[pyfunction]`, one `#[pymodule]`), and that count is misleading. `dom.rs` is
1,686 lines that manipulate Python objects directly: 95 `getattr` calls, 13
`setattr`, 6 `call_method`, 5 `call1` and 14 `py.import`. It imports
`feetbrowser.htmlparser` and `feetbrowser.jsdom` by name, constructs
`Element`, `Text`, `JSElement`, `JSNodeList` and six more Python classes, runs
`HTMLParser(...).parse()` for `innerHTML`, and mutates `node.children` and
`node.parent` in place. On top of that, `JsValue::Host(Py<PyAny>)` is a
first-class variant of the interpreter's value enum (`value.rs:98`), and
`Host` is referenced 26 times across five of the nine Rust sources, so a
Python object is not something `dom.rs` merely touches at the edges; it is a
kind of JavaScript value the interpreter carries everywhere.

None of that survives a ctypes boundary. `getattr`, refcounting, `PyDict`
casts and constructing Python classes *are* the CPython C API, which is
precisely what ctypes does not give you. `jsdom_rust.py` is **214 lines** of
shims that forward every `js_get`/`js_set` into `dom.rs`; dropping pyo3 means
replacing those shims with a handle table (a Python DOM `dom.rs` would ask
through callbacks on opaque handles), which is the 700-1,100 lines this
section has long estimated, and the risk is not the FFI. It is that every DOM
operation changes from "Rust reaches into a Python object" to "Rust asks
Python through a handle table", which moves behaviour at the edges (identity,
exception propagation, mutation ordering), and `tests/test_js.py` is the only
thing standing between that and a class of quiet regressions.

What it would return is real, and the last item is the strongest argument:

- 8 crates, and `Cargo.toml` down to nothing at all if the other three go too.
- `run.sh` from 106 lines to something near 50, and no virtualenv on a user's
  machine. Today `run.sh` cannot start the browser without creating one.
- `test.sh` from 91 lines to about 50, with `.venv/bin/python` becoming
  `python3` throughout.
- **CI Rust builds from 8 per run to 2.** The matrix covers six Python versions
  on Linux and two on macOS, and a pyo3 extension has to be compiled against
  each interpreter separately, so the same LTO release build runs eight times.
  A cdylib is built once per operating system. The cargo cache at
  `action.yml:48-60` exists because of this, and its own comment calls the
  release build by a wide margin the slowest thing in the run.

**Recommended order: do chrono, serde_json, regex and miniz_oxide first.**
Together they remove **42 of the 57 crates** for roughly 1,600-2,100 lines of
Rust, with no architectural change and several confirmed correctness fixes on
the way. pyo3 removes the remaining 8 for 1,500-2,500 lines and a change of
architecture. Doing the others first also makes the pyo3 conversation much
shorter, because "why does a crate with no dependencies of its own need a
build tool and a virtualenv" answers itself.

## Pillow, and the JPEG decoder that replaced it

**Done.** `Tab._photo_from_pillow` is gone from `browser.py` and the decoder
is in `image.rs`, beside the others. What follows is what it cost against what
this section estimated, because an estimate nobody checks afterwards teaches
nobody anything.

The estimate was 700 lines for a baseline decoder and the JPEG block in
`image.rs` is **1,137**, which is less the estimate being wrong than the scope
changing: 215 of those lines are the four progressive coefficient passes and
their scan manager, which this section had put out of scope and which was
written anyway, and much of the remainder is the guard on every table index
and shift count that the section itself demanded two paragraphs later. The
line-by-line breakdown was close where the scope matched. Byte stuffing and
the MCU interleave were the two places the first draft was wrong, exactly as
predicted.

What it decodes: baseline (SOF0), extended sequential (SOF1) and progressive
(SOF2), Huffman-coded, 8-bit, one or three components, any sampling factors,
restart intervals. What it refuses, with `ImageError` and the `[img]`
placeholder: arithmetic coding, CMYK and YCCK, 12-bit samples, lossless and
hierarchical frames, and any other component count. EXIF orientation is
ignored, so a photograph relying on it appears rotated, the same as before,
since nothing ever honoured it. The inverse transform is libjpeg's AAN one and
chroma is reconstructed with its triangle filter; against libjpeg over 77
JPEGs off the web the largest per-channel difference is 3.

Performance was never the interesting number in Rust and is not: 800x600 in
6.5 ms, 1800x1200 in 27 ms, 60-90 Mpixel/s. The two-codec-pass absurdity this
section flagged (Pillow decoding a JPEG and re-encoding it to PNG so our own
decoder could decode it again) went away with the branch that did it.

Safety was the part that was not free, as predicted. Two failure modes exist
in Rust that did not exist in the Python this was ported from, and both come
from a file being allowed to name a number: a Huffman table can name a
magnitude category of 200, which asks the bit reader for a shift the machine
does not have, and a progressive scan header can name a point transform that
shifts a coefficient out of range. Both are checked at the point the number is
read rather than where it is used. The suite corrupts the real fixtures 1,500
times a run and asserts `ImageError` every time.

Still open, and unchanged by any of this: image *fetching* is off the UI
thread (`Tab._fetch_image`) but image *decoding* is not: `_drain_images`
calls `_decode_image` synchronously, which holds the GIL for the length of the
decode. At 6.5 ms a photograph this is no longer urgent, but it is still the
right shape and still a small change.

## cairosvg, and why SVG is a different project

**Done, by taking the recommendation below rather than by writing anything.**
cairosvg is no longer imported and SVG draws as the `[img]` placeholder
everywhere, which is what it already did on every machine that did not have
the library installed, including every CI job but one. The reasoning is kept
in full because it is the argument for not revisiting this casually.

The recommendation was to **drop cairosvg without replacing it** and let SVG
draw as the `[img]` placeholder.

The rasteriser is better placed for this than it might look. `rasterize()` in
`rust/src/raster.rs` does nonzero-winding scan conversion with 4x vertical
subsampling and analytic horizontal coverage, so anti-aliased filled paths in
a flat colour already work, and that is the single hardest primitive. What
does not exist is everything around it: there is no stroker at all
(`draw_line` is Bresenham with square dots: no joins, no caps, no dashes, and
a real stroker is 400-700 lines of genuinely difficult geometry);
`blit_coverage` takes exactly one solid colour, so gradients need a
paint-source abstraction and a breaking change to the compositing API;
`Surface` carries a single clip rectangle, so `clipPath`, `mask` and group
opacity need off-screen surfaces; and `flatten_contours` in `rust/src/font.rs`
handles TrueType quadratics only, where SVG needs cubics and elliptical arcs.

Being in Rust now makes the work faster to run but no smaller to write, and
the API break is wider than it was, because the compositing API is a pyo3
boundary rather than a Python function signature.

`htmlparser.py` does not help either, for three separate disqualifying
reasons. It lowercases every tag name (`htmlparser.py:259`), and SVG is
case-sensitive camelCase throughout: `linearGradient`, `clipPath`,
`viewBox`. Its `VOID_ELEMENTS` list is the HTML one, so `<rect/>` is pushed
and never closed, corrupting the tree. And `implicit_tags` injects
`<html>`/`<head>`/`<body>`. SVG needs a namespace-aware XML parser: another
250-400 lines.

The subset that would render the SVGs you actually meet (paths, basic shapes,
transforms, solid fills, strokes with joins and caps, linear and radial
gradients, `viewBox`, `use`/`defs`, and explicitly no filters and no
text-on-path) is **2,500-4,000 new lines plus that rasteriser API break**.
For scale, `layout.py` is the largest file in the project at 3,570 lines. Full
text, filters and markers push past 6,000.

The structural argument matters more than the line count. **JPEG terminates.**
T.81 was finished in 1992 and a baseline decoder is bounded and checkable. SVG
1.1 plus SVG 2 plus the CSS that applies to it is open-ended, and a partial SVG
renderer produces *wrong pictures* rather than *no picture*: a gradient that
comes out flat black is worse than a placeholder, because it looks like it
worked.

If SVG is ever wanted, scope it deliberately as a subset of roughly 800-1,200
lines that **refuses** documents using gradients, filters, masks or text
instead of rendering them wrong. That is a defensible milestone. A full SVG
renderer is not a dependency-removal task; it is the next project.

## pyflakes

Installed conditionally in `test.sh:51-52` and unconditionally in
`.github/actions/build/action.yml:72`, and run in exactly two places with the
same arguments: `python -m pyflakes feetbrowser tests`. There is no
configuration file anywhere, so it runs at its defaults.

**Recommendation: keep it, and write down why.** The distinction that justifies
it is that pyflakes never ships, never runs in the browser, and is not part of
the artefact. The project's claim is that the browser owns its stack; a linter
is not in the stack. Drawing that line explicitly (no runtime dependencies,
development tooling is fine) is more defensible than pretending there is no
difference.

The replacement also splits unevenly. Unused-import detection over `ast` is
genuinely easy, about 120-200 lines. Undefined-name detection is not: doing it
without false positives needs full scope analysis (module, class, function
and comprehension scopes, `global`/`nonlocal`, star imports, `del`,
conditional imports, `__all__`), which is the bulk of pyflakes and the part
that makes people switch a linter off when it gets it wrong. Undefined names
are also the high-value catch here, because `browser.py` is 4,510 lines and
lazy imports still hide in branches nothing routinely takes (`curl_cffi` at
`net.py:348` is the last one), and a name used outside the branch that defines
it is exactly what a test suite finds late and a linter finds instantly.

One oddity worth knowing: there are 41 `# noqa` markers across 9 files, 38 of
them `BLE001` and 3 `E402`. Neither is a pyflakes code, and pyflakes has no
suppression mechanism, so **all 41 are inert** under the linter that actually
runs. They are annotations for tools this project does not use, and today they
are self-documenting comments and nothing more.

## Fortran

`fortran/` is fixed-form FORTRAN 77 and holds two decoders that share
nothing but the build machinery: eleven sources and an include file for
H.264, five and an include file for AAC-LC, described in
[media.md](media.md#h264-in-fortran) and
[media.md](media.md#aac-in-fortran). Neither has dependencies of any kind
(no library, no package manager, no lock file, nothing linked but `libc`),
so the only entry they earn in this file is a compiler on the build side,
and even that is conditional.

`feetbrowser/h264.py` and `feetbrowser/aac.py` each shell out to whatever
`gfortran` they can find, compile their own sources into a shared library in
the temporary directory named after a hash of them and of the compiler, and
load it with `ctypes`. There is no build step in `run.sh`, no target in CI
that has to succeed, and nothing in `rust/Cargo.toml` or `pyproject.toml`
that mentions it.

The packaged applications are the exception, and they have to be: a user has
no gfortran, so a bundle that carried only the sources carried no video and
no sound at all. Each of the three packaging scripts compiles both decoders
on the build machine through `python3 -m feetbrowser.h264 --build` and
`python3 -m feetbrowser.aac --build`, ships the results inside the package
next to the modules that load them, and checks with `otool -L`, `ldd` or a
stripped `PATH` that gfortran's runtime went in with them rather than being
left behind as a dependency on the build machine. So gfortran is a build-time
requirement for packaging on all three platforms, and a run-time requirement
nowhere.

Each script then asks the thing it has just built, rather than trusting that
the copy succeeded: `--check-video` and `--check-audio` run inside the
bundle with `PATH` cut back to the system directories, decode a fixture and
compare the result against what a reference decoder produced. They are two
questions because they are two libraries built from two sets of sources, and
the failure that only the second one catches is the quiet one -- a bundle
carrying the video decoder and not the sound decoder installs, starts,
renders, plays a video, and is silent.

Missing compiler, failed compile or an ABI mismatch all resolve to
`h264.available()` or `aac.available()` being false, at which point the file
is named and refused the way it was before the decoders were written.
`tests/test_h264.py` and `tests/test_aac.py` assert that path by forcing it,
so it is a tested behaviour rather than an intention, and the whole suite
passes on a machine with no Fortran toolchain.

The choice of language is worth one line each, because it is the obvious
question. The CABAC decode-decision loop is a dependent chain of integer
compares, table lookups and shifts, benchmarked at 119.7 Mbin/s in Fortran
against 153.7 in C. The AAC side is the opposite shape -- floating-point
kernels over contiguous arrays, which is the workload Fortran compilers have
been aimed at for fifty years -- and decodes
44.1 kHz stereo at about 500x realtime on one core. Close enough in both
cases that the deciding factor was the rest of this file: Fortran arrives
with no crates to audit.

**Verdict: optional to run, required to package.** `dnf install
gcc-gfortran` in the AppImage container, `brew install gcc` on both macOS
runners, and MinGW-w64 on the Windows one. No runtime dependency on any of
them: what ships is the compiled library, and the compiler's own runtime is
linked into it statically.

## Go

`go.mod` is three lines with no `require` block, and `net/net.go` imports 24
standard-library packages and nothing else, so the Go code has zero
dependencies of its own. What it has is a **Go toolchain** on the build side:
`test.sh:70-78` runs `go vet` and `go test` where one is on `PATH`, and
`.github/workflows/ci.yml:225-241` installs Go 1.22 and runs build, vet and
test.

`net/net.go` is 1,091 lines and its own header calls it a port of
`feetbrowser/net.py`. It is a near-complete one-to-one port down to the
tunables (`MAX_REDIRECTS = 10`, `CACHE_MAX_SIZE = 1000`, the same 64 MB body
cap and the same timeouts), with a hand-rolled HTTP/1.1 client on raw sockets,
TLS with SNI, chunked decoding, gzip and deflate, a DNS cache, a keep-alive
pool and a `Cache-Control`-aware response cache. `net/net_test.go` is 421 more
lines and 19 test functions, and they are good tests.

**Nothing uses it.** Greps for `go run`, `go build`, `cgo`, `net.go`, a
subprocess call or a ctypes load turn up only the CI job and the `test.sh`
block that run its own tests. The only shared libraries the browser loads are
the assembled span kernel and `libX11.so.6`. All three consumers of the
transport layer (`browser.py`, `toehub.py`, `toes.py`) import the Python
`net.py`. No documentation mentions it. It has one real feature gap already:
`RequestImpersonated` (`net.go:372`) is a one-line stub that returns a plain
request, which is the opposite of what the method is for.

It also drifts. Two commits created it, the second being a correction because
it had already fallen out of step with `net.py` within a day, and 49 commits
have landed since without touching it.

The CI job is honest about what it is; its own comment says the port arrived
with tests and nothing that ran them, and that the job is the only thing
standing between the package and rot. Since `ci.yml` declares no `needs:`
relationships, every job independently fails the workflow, so a Go compile
error does turn the run red. Whether it is a *required* status check is a
branch-protection setting that does not live in the repository, so that part
is not something this file can answer.

**Verdict: optional, and currently dead weight.** The decision to make is
whether a Go transport has a job in a Python browser (a subprocess, or a
cdylib over ctypes), and to wire it in, or to delete it. Keeping it as it
stands costs a toolchain in CI and guarantees continued drift.

## Suggested order

1. Turn the silent regex fallback at `interp.rs:785` into a thrown
   `SyntaxError`. About five lines, and it measures how much of the real web
   needs lookaround before anyone commits to writing a regex engine.
2. Route PNG inflate through the standard library's `zlib`, the way PNG
   encoding already goes. About fifteen lines, two crates, an hour.
3. `chrono`. Thirty of 57 crates for 250-350 lines and 2-4 days, which is the
   best ratio available by a wide margin.
4. `serde_json`. Six crates for 180-250 lines, or 350-500 to make
   `JSON.stringify` correct as well.
5. ~~A JPEG decoder in `image.rs`, which removes Pillow and ends the
   decode-then-re-encode round trip.~~ Done, with progressive as well.
6. ~~Drop `cairosvg` without replacing it, and record SVG as deliberately out
   of scope.~~ Done.
7. `regex`. The largest item at 1,050-1,500 lines and one to two weeks, and the
   largest correctness win in the list.
8. Decide about Go: wire it in or delete it.
9. Reassess `pyo3` once it is the only entry left in `Cargo.toml`.
10. Keep `pyflakes`, and write down why a development tool is not part of the
    stack the project claims to own.

Steps 1 through 4 remove **42 of the 57 crates** and need no architectural
change at all.
