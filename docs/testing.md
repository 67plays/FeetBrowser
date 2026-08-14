# Tests

```bash
./test.sh          # builds the Rust engine, then pyflakes + unit + JS + navigation + toe + live smoke
```

`test.sh` builds the Rust JS engine (`feetbrowser_engine`) into the local
`.venv` with `maturin develop --release` on first use, then runs every suite
from that venv — so a first run needs the Rust toolchain (maturin is
installed into the venv automatically).

Most suites run fully offline: `test_units.py`, `test_js.py` (which serves its
`fetch`/`XMLHttpRequest` cases from a local HTTP server), and `test_toes.py`
(which points the hub at a `file://` catalog in a temp dir). `test_nav.py` and
`smoke.py` load real sites over the network, so both need connectivity — which
is why [CI](../.github/workflows/ci.yml) runs only the offline suites.

The test files live in `tests/`:

```
tests/
  test_units.py     offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py        offline tests for the JS engine + DOM bridge
  test_nav.py       click-to-navigate, history, view-source (needs network)
  test_shoes.py     Shoes theme manager tests
  test_toes.py      toe engine + ToeHub tests (install/uninstall/toggle)
  smoke.py          end-to-end pipeline on real pages (needs network)
```
