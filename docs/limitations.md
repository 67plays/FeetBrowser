# What it does and doesn't do

**Does:** fetch and render real websites over HTTPS, apply their CSS
(text styling, colors, backgrounds, layout), follow links, keep per-tab
history, submit forms (GET/POST), show page source, open links in new tabs,
run JavaScript (scripts on load, DOM reads/writes, click handlers, `Promise`
with microtasks, `async`/`await`, timers, `fetch`/`XMLHttpRequest`, and
`throw`/`try`/`catch`, with `console.log` surfaced in the page's log buffer),
manage extensions ("toes") from the built-in ToeHub — install, uninstall,
enable, and disable them without a restart — and restyle the whole browser
with **Shoes** color themes (`about:shoes`, or `Ctrl+Shift+S`).

**Doesn't (yet):** flexbox wrapping, `<textarea>`/`<select>` selection (beyond
read-only), or the full ECMAScript feature set (no getters/setters, no
generators, no ES modules, no `Proxy`). Shoes themes are preset solid-color
palettes only — there's no custom color editor, and page colors aren't themed
(only the browser chrome and the built-in pages). These are natural next
milestones — the architecture has clean seams for each.
