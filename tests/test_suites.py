"""Check that every suite in tests/ is actually run by something.

A test file that nothing invokes is worse than no test file: it looks like
coverage in the directory listing and reports nothing. Every place that runs
the suite names its files one at a time -- `test.sh` so a developer can see
the order and the comments, `test.cmd` because cmd.exe cannot read `test.sh`,
the workflow so a red job says which suite went red -- and a list written out
three times is a list that drifts. This is the fourth copy, and the only one
that is derived rather than typed, so a suite added to one and forgotten in
another fails here by name.

`test.cmd` is the one most likely to drift, because it is the only runner
nobody developing on Unix ever executes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)

# Where a suite has to be mentioned, and what to say when it is not.
RUNNERS = (
    ("test.sh", "test.sh"),
    ("test.cmd", "test.cmd"),
    (os.path.join(".github", "workflows"), "the CI workflow"),
)


def _suites():
    return sorted(name for name in os.listdir(TESTS)
                  if name.startswith("test_") and name.endswith(".py"))


def _text(relative):
    """Everything in a file, or everything in every file in a directory.

    Backslashes become forward slashes on the way out, because test.cmd
    writes `tests\\test_render.py` and everything else writes
    `tests/test_render.py`, and the difference is not one worth checking for
    twice.
    """
    path = os.path.join(ROOT, relative)
    if os.path.isfile(path):
        with open(path, encoding="utf8") as handle:
            return handle.read().replace("\\", "/")
    out = []
    for folder, _dirs, files in os.walk(path):
        for name in sorted(files):
            with open(os.path.join(folder, name), encoding="utf8") as handle:
                out.append(handle.read())
    return "\n".join(out).replace("\\", "/")


def test_every_suite_is_run():
    suites = _suites()
    assert len(suites) > 5, "tests/ went missing: %s" % suites
    missing = []
    for relative, description in RUNNERS:
        text = _text(relative)
        for suite in suites:
            if "tests/%s" % suite not in text:
                missing.append("%s is not run by %s" % (suite, description))
    assert not missing, (
        "a test file exists that nothing runs:\n  " + "\n  ".join(missing)
        + "\nAdd it to test.sh, test.cmd and .github/workflows/ci.yml,"
        " or delete it.")
    print("  %d suites, all named in test.sh, test.cmd and the workflow"
          % len(suites))


def test_no_runner_names_a_suite_that_is_gone():
    """The other direction: a renamed file leaves a line behind that still
    looks like it runs something."""
    suites = set(_suites())
    stale = []
    for relative, description in RUNNERS:
        for word in _text(relative).replace("'", " ").replace('"', " ").split():
            word = word.strip(",;)")
            if not word.startswith("tests/test_") or not word.endswith(".py"):
                continue
            if os.path.basename(word) not in suites:
                stale.append("%s names %s, which does not exist"
                             % (description, word))
    assert not stale, "\n  ".join([""] + sorted(set(stale)))


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f" FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(tests)} SUITE-COVERAGE TESTS PASSED")


if __name__ == "__main__":
    main()
