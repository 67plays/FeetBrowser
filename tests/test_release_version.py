"""The version guard release.yml runs before it builds anything.

packaging/release_version.py is the one piece of Python in the release
path, and the thing it protects against -- a tag and __init__.py drifting
apart -- is by nature something nobody sees happen. It is also the kind of
code that only ever runs on a runner, at the one moment when a mistake is
most expensive, so it is checked here instead.
"""
import importlib.util
import os
import sys
import tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)

sys.path.insert(0, ROOT)

# Loaded by path rather than imported. packaging/ is a directory of build
# scripts and not a package: it has no __init__.py, and giving it one
# would put a directory called `packaging` on sys.path under the same name
# as the well-known third-party library, which is a trap for a later
# reader rather than a convenience for this one.
_spec = importlib.util.spec_from_file_location(
    "release_version",
    os.path.join(ROOT, "packaging", "release_version.py"))
release_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_version)


def _repository(version):
    """A throwaway tree with just enough of a package to read a version."""
    root = tempfile.mkdtemp()
    os.mkdir(os.path.join(root, "feetbrowser"))
    path = os.path.join(root, "feetbrowser", "__init__.py")
    with open(path, "w", encoding="utf8") as handle:
        handle.write('"""A stand-in."""\n\n__version__ = "%s"\n' % version)
    return root


def test_every_way_of_asking_means_the_same_release():
    """A tag push, github.ref_name and a human all spell it differently."""
    for asked in ("refs/tags/v0.6.0", "v0.6.0", "0.6.0", "  v0.6.0  ",
                  "V0.6.0"):
        got = release_version.requested_version(asked)
        assert got == "0.6.0", "%r gave %r" % (asked, got)


def test_a_pre_release_suffix_survives():
    for asked in ("v1.2.3rc1", "refs/tags/v1.2.3.dev4", "10.0.1-beta"):
        release_version.requested_version(asked)


def test_something_that_is_not_a_version_is_refused():
    """The failure this catches is a branch arriving where a tag was
    expected, which would otherwise cut a release called "main"."""
    for asked in ("main", "refs/heads/main", "", "latest", "v", "1"):
        try:
            release_version.requested_version(asked)
        except ValueError:
            continue
        raise AssertionError("%r was accepted as a version" % asked)


def test_the_helper_reads_this_repository_s_own_version():
    import feetbrowser
    read = release_version.package_version(ROOT)
    assert read == feetbrowser.__version__, (
        "read %r from the file, package says %r"
        % (read, feetbrowser.__version__))


def test_a_matching_tag_resolves_to_the_number():
    root = _repository("0.6.0")
    assert release_version.resolve("refs/tags/v0.6.0", root) == "0.6.0"


def test_a_tag_that_ran_ahead_of_the_package_is_refused():
    """The whole point: v0.7.0 with __init__.py still on 0.6.0 would
    otherwise publish a v0.7.0 release full of 0.6.0 files."""
    root = _repository("0.6.0")
    try:
        release_version.resolve("v0.7.0", root)
    except ValueError as complaint:
        # Both numbers have to be in the message, because the reader's
        # next question is always "which one is wrong?".
        assert "0.7.0" in str(complaint), complaint
        assert "0.6.0" in str(complaint), complaint
        return
    raise AssertionError("a tag ahead of __version__ was accepted")


def test_a_package_with_no_version_line_is_a_clear_error():
    root = tempfile.mkdtemp()
    os.mkdir(os.path.join(root, "feetbrowser"))
    path = os.path.join(root, "feetbrowser", "__init__.py")
    with open(path, "w", encoding="utf8") as handle:
        handle.write('"""No version here."""\n')
    try:
        release_version.package_version(root)
    except ValueError as complaint:
        assert "__version__" in str(complaint), complaint
        return
    raise AssertionError("a package with no __version__ was accepted")


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_")]
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
    print(f"\nALL {len(tests)} RELEASE VERSION TESTS PASSED")


if __name__ == "__main__":
    main()
