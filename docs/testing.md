# Tests

```bash
./test.sh          # pyflakes + unit + JS + navigation + toe + pseudo-toe + live smoke
```

Most suites run fully offline: `test_units.py`, `test_js.py` (which serves its
`fetch`/`XMLHttpRequest` cases from a local HTTP server), `test_toes.py` (which
points the hub at a `file://` catalog in a temp dir), and `test_gh_scroll.py`
(which stubs the pseudo-toe's fetches). `test_nav.py` and `smoke.py` load real
sites over the network, so both need connectivity — which is why
[CI](../.github/workflows/ci.yml) runs only the offline suites.

The test files live in `tests/`:

```
tests/
  test_units.py     offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py        offline tests for the JS engine + DOM bridge
  test_nav.py       click-to-navigate, history, view-source (needs network)
  test_toes.py      toe engine + ToeHub tests (install/uninstall/toggle)
  test_gh_scroll.py pseudo-toe (gh-scroll) hooks and gh:// navigation
  smoke.py          end-to-end pipeline on real pages (needs network)
```
