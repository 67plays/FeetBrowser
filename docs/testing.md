# Tests

```bash
./test.sh          # pyflakes + unit + navigation + toe + live smoke tests
```

`test_units.py` and `test_nav.py` are deterministic; `smoke.py` fetches a few
real sites, so it needs network access.

The test files live in `tests/`:

```
tests/
  test_render.py offline tests for fonts, rasteriser, image codecs, canvas
  test_units.py  offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py     offline tests for the JS engine + DOM bridge
  test_nav.py    click-to-navigate, history, view-source
  test_toes.py   toe engine + ToeHub tests (install/uninstall/toggle)
  test_gh_scroll.py  the bundled pseudo-toe
  smoke.py       end-to-end pipeline on real pages
```

Nothing here needs a display or a GUI toolkit: the renderer draws into its own
framebuffer, so the whole suite runs headless. `test_render.py` does need at
least one system font, which every platform we support ships.
