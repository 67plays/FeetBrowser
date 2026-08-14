# Architecture

FeetBrowser is a **functional web browser written from scratch** — the engine
(JS interpreter + DOM bridge) is a native Rust extension, and the rest is pure
Python. It does not wrap Chromium, WebKit, Gecko, or any HTTP library — it
implements its own:

- **Networking** — raw TCP sockets speaking HTTP/1.1, TLS for `https`,
  redirect following, `gzip`/`deflate` decoding, chunked transfer decoding,
  plus `data:`, `file:` and `view-source:` schemes, a small bounded response
  cache, and a keep-alive connection pool that reuses sockets per origin.
- **HTML parser** — a tokenizer + tree builder producing a real DOM
  (entities, comments, void elements, raw-text `<script>`/`<style>`, and
  implicit `<html>`/`<head>`/`<body>` + `<li>`/`<p>`/`<tr>` insertion, plus
  the spec's "a block element closes a `<p>`" rule).
- **CSS engine** — a parser for tag / class / id / descendant / grouped
  selectors (with pseudo-classes like `:hover` collapsed to their base
  selector), the cascade with specificity, inheritance, inline `style=""`,
  `@media` unwrapping, and a default user-agent stylesheet (`ua.css`).
- **Layout engine** — a block-and-inline flow layout with line breaking and
  word wrapping, font size / weight / style, colors, backgrounds, list
  bullets, and `<hr>`, plus **CSS floats** (with text wrapping and `clear`),
  **`<table>` layout** (thead/tbody/tfoot, `colspan`/`rowspan`), a **flexbox**
  subset (`flex-direction` row/column, `gap`, `flex-grow`, `flex-basis`,
  `justify-content`, `align-items`), a **CSS grid** subset
  (`grid-template-columns` px/%/fr/auto, auto row placement,
  `grid-column`/`grid-row` spans, `gap`), and **`<img>` rendering** (PNG/GIF
  natively, JPEG via Pillow, fetched off the UI thread), plus form controls
  (text fields, checkboxes, submit/reset buttons, `<select>`), producing a
  display list of paint commands.
- **Browser UI** — a hand-drawn chrome on a Tk canvas: tabs, an address bar
  with search fallback, back / forward / reload / home buttons,
  hover + clickable links, middle-click / ctrl-click to open in a new tab,
  scrolling, a scrollbar, bookmark toggling, and a status bar. Repainting is
  layered: page, chrome, selection, and toe overlays are tracked by canvas
  tag, so a small change (a text selection drag, a focused address bar) only
  repaints the damaged region instead of the whole canvas.
- **Extensions (Toes)** — a from-scratch hooking system. See
  [docs/toes.md](toes.md).
- **JavaScript engine** — a from-scratch interpreter compiled to a native
  Rust extension (`feetbrowser_engine`, built with PyO3/maturin): a
  hand-written lexer + recursive-descent parser + tree-walking evaluator in
  `rust/`. It supports closures, `var`/`let`/`const`, objects, classes with
  `extends`/`super`, arrays (index growth, `length` truncation,
  `push`/`pop`/`map`/`reduce`/`join`), `if`/`while`/`for`/`for-of`/`for-in`
  with `break`/`continue`, `try`/`catch`/`throw`, arrow functions (lexical
  `this`), template literals, spread/rest, optional chaining, nullish
  coalescing, `Promise` + microtasks, `async`/`await`, timers, and operators
  with proper precedence and JS coercion rules (`NaN`/`Infinity` globals,
  `NaN` falsiness, `null + 1 === 1`, `[] + [] === ""`). Global builtins:
  `String`, `Number`, `Boolean`, `parseInt`, `parseFloat`, `Array`,
  `Object`, `Map`, `Set`, `Date`, `RegExp`, `Math`, `JSON`, `console.log`,
  `fetch`, `XMLHttpRequest`. Scripts in `<script>` tags run on page load;
  errors are captured instead of crashing the page.
- **DOM bridge** — a Rust DOM (`rust/src/dom.rs`): `getElementById`/
  `querySelector`/`querySelectorAll`, `textContent`, `innerHTML`, `style`,
  `classList`, attributes, and `addEventListener`, exposing `document`,
  elements, node lists, and the `body`/`head`/`documentElement` shortcuts.
  Scripts mutate the page and wire up click handlers, which re-cascade the
  stylesheet and re-render. The DOM objects operate on the Python node tree
  that layout renders; `feetbrowser/jsdom.py` is a thin shim that delegates
  to the Rust functions.

Tk is used **only as the pixel surface** (a canvas to draw text and rectangles
on) and for font metrics — the browser engine itself is all in this repo.

## Layout of the code

```
feetbrowser/
  net.py         URL parsing + HTTP/HTTPS/data/file transport + connection pool
  htmlparser.py  HTML tokenizer + DOM tree builder
  cssparser.py   CSS parser, selectors, specificity, cascade
  jsengine.py    thin shim over the Rust `feetbrowser_engine` extension
  jsdom.py       thin shim over the Rust DOM bridge (dom_get/dom_set/dom_call)
  layout.py      block/inline layout -> display list, painting
  browser.py     Tk window, chrome, tabs, history, event loop, layered repaint
  toes.py        extension hooking (Toes): discovery, dispatch, CLI
  toehub.py      the ToeHub: catalog fetch, install/uninstall/toggle
  ua.css         default user-agent stylesheet
rust/
  lib.rs         PyO3 module wiring; exposes Interpreter, JSException, UNDEFINED
  interp.rs      evaluator, host bridge, promises, microtasks, timers
  parser.rs      recursive-descent parser + AST construction
  token.rs       lexer
  ast.rs         AST node types
  value.rs       JsValue model, scopes, coercion, JsCallback
  stdlib.rs      built-ins (Array/Object/Map/Set/Date/RegExp/Math/JSON/...)
  dom.rs         DOM bridge (document/element/style/classList/...)
  pybind.rs      Python-facing classes (Interpreter, JsGlobals, PyJsValue)
toes/            user-installed toes (gitignored; empty on a fresh checkout)
tests/
  test_units.py  offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py     offline tests for the JS engine + DOM bridge
  test_nav.py    click-to-navigate, history, view-source
  test_shoes.py  Shoes theme manager tests
  test_toes.py   toe engine + ToeHub tests (install/uninstall/toggle)
  smoke.py       end-to-end pipeline on real pages
```

The Rust engine is built with maturin into a local venv; `run.sh` and
`test.sh` build it on first use (`maturin develop --release`).
