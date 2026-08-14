# Tests

```bash
./test.sh          # pyflakes + unit + navigation + toe + live smoke tests
```

`test_units.py` and `test_nav.py` are deterministic; `smoke.py` fetches a few
real sites, so it needs network access.

The test files live in `tests/`:

```
tests/
  test_units.py  offline unit tests (URL, HTML, CSS, layout, internal pages)
  test_js.py     offline tests for the JS engine + DOM bridge
  test_nav.py    click-to-navigate, history, view-source
  test_toes.py   toe engine + ToeHub tests (install/uninstall/toggle)
  smoke.py       end-to-end pipeline on real pages
```
