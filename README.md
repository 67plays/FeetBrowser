# 🦶 FeetBrowser

A **functional web browser written from scratch** in pure Python. It does not
wrap Chromium, WebKit, Gecko, or any HTTP library — it implements its own:

- **Networking** — raw TCP sockets speaking HTTP/1.1, TLS for `https`,
  redirect following, `gzip`/`deflate` decoding, chunked transfer decoding,
  plus `data:`, `file:` and `view-source:` schemes and a small bounded
  response cache.
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
  scrolling, a scrollbar, and a status bar.
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

## Running

```bash
./run.sh                 # opens the welcome page
./run.sh https://example.com
./run.sh view-source:https://example.com
```

`run.sh` uses your system Python if it has Tkinter; on NixOS it fetches one
on the fly via `nix-shell`. On other distros install Tk first
(`python3-tk` on Debian/Ubuntu, `python3-tkinter` on Fedora, `tk` on Arch)
and then `python3 -m feetbrowser <url>`.

## Keyboard shortcuts

| Key | Action | Key | Action |
|-----|--------|-----|--------|
| `Ctrl-L` | focus address bar | `Ctrl-T` | new tab |
| `Ctrl-W` | close tab | `Ctrl-R` | reload |
| `Ctrl-H` | home page | `Alt-←` / `Alt-→` | back / forward |
| `↑` / `↓` | scroll | `PgUp` / `PgDn` | page scroll |
| `Home` / `End` | jump to top / bottom | `Esc` | blur address / input |
| `Ctrl-Tab`, `Ctrl-PgUp/Dn` | cycle tabs | middle / `Ctrl`-click | open link in new tab |

Type a URL in the address bar and press Enter, or type words to search
(DuckDuckGo HTML). Bare hosts without a scheme (`example.com:8080`,
`localhost:8000`) are assumed to be `https://`.

## Forms

Basic form support is wired up: `input[type=text/password]` fields are
focusable and typeable, checkboxes toggle, and submitting a form (clicking a
submit button or pressing Enter in a field) sends `GET` or `POST` to the form
`action`, which is resolved against the document's `<base href>` when one is
present.

## Layout of the code

```
feetbrowser/
  net.py         URL parsing + HTTP/HTTPS/data/file transport
  htmlparser.py  HTML tokenizer + DOM tree builder
  cssparser.py   CSS parser, selectors, specificity, cascade
  jsengine.py    JavaScript lexer, parser, interpreter
  jsdom.py       JavaScript <-> DOM bridge (document/element/style)
  layout.py      block/inline layout -> display list, painting
  browser.py     Tk window, chrome, tabs, history, event loop
  ua.css         default user-agent stylesheet
tests/
  test_units.py  offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py     offline tests for the JS engine + DOM bridge
  test_nav.py    click-to-navigate, history, view-source
  smoke.py       end-to-end pipeline on real pages
```

## What it does and doesn't do

**Does:** fetch and render real websites over HTTPS, apply their CSS
(text styling, colors, backgrounds, layout), follow links, keep per-tab
history, submit forms (GET/POST), show page source, open links in new tabs,
and run JavaScript (scripts on load, DOM reads/writes, click handlers, with
`console.log` surfaced in the page's log buffer).

**Doesn't (yet):** flexbox wrapping, `<textarea>`/`<select>` selection (beyond
read-only), or the full ECMAScript feature set (no `async`, no `Promise`, no
`XMLHttpRequest`/`fetch`). These are natural next milestones — the architecture
has clean seams for each.

## Tests

```bash
./test.sh          # pyflakes + unit + navigation + live smoke tests
```

`test_units.py` and `test_nav.py` are deterministic; `smoke.py` fetches a few
real sites, so it needs network access.
