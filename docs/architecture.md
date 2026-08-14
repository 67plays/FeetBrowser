# Architecture

FeetBrowser is a **functional web browser written from scratch** in pure Python.
It does not wrap Chromium, WebKit, Gecko, or any HTTP library — it implements
its own:

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
- **JavaScript engine** — a from-scratch interpreter (hand-written lexer +
  parser + tree-walking evaluator): closures, `var`/`let`/`const`, objects,
  arrays (with index growth, `length` truncation, `push`/`pop`/`join`),
  `if`/`while`/`for` with `break`/`continue`, operators with proper
  precedence, JS coercion rules (`NaN`/`Infinity` globals, `NaN` falsiness,
  `null + 1 === 1`, `[] + [] === ""`), and global builtins (`String`,
  `Number`, `Boolean`, `parseInt`, `parseFloat`, `console.log`). Scripts in
  `<script>` tags run on page load; errors are captured instead of crashing
  the page. A small **DOM bridge**
  (`document.getElementById/querySelector`, `textContent`, `innerHTML`,
  `style`, `addEventListener`) lets scripts mutate the page and wire up
  click handlers, which re-cascade the stylesheet and re-render.

Tk is used **only as the pixel surface** (a canvas to draw text and rectangles
on) and for font metrics — the browser engine itself is all in this repo.

## Layout of the code

```
feetbrowser/
  net.py         URL parsing + HTTP/HTTPS/data/file transport + connection pool
  htmlparser.py  HTML tokenizer + DOM tree builder
  cssparser.py   CSS parser, selectors, specificity, cascade
  jsengine.py    JavaScript lexer, parser, interpreter
  jsdom.py       JavaScript <-> DOM bridge (document/element/style)
  layout.py      block/inline layout -> display list, painting
  browser.py     Tk window, chrome, tabs, history, event loop, layered repaint
  toes.py        extension hooking (Toes): discovery, dispatch, CLI
  toehub.py      the ToeHub: catalog fetch, install/uninstall/toggle
  ua.css         default user-agent stylesheet
toes/            user-installed toes (gitignored; empty on a fresh checkout)
tests/
  test_units.py  offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py     offline tests for the JS engine + DOM bridge
  test_nav.py    click-to-navigate, history, view-source
  test_toes.py   toe engine + ToeHub tests (install/uninstall/toggle)
  smoke.py       end-to-end pipeline on real pages
```
