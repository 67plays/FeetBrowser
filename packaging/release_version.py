#!/usr/bin/env python3
"""Work out which version a release is being cut for, and refuse a mismatch.

All three packaging scripts name what they produce after
feetbrowser.__version__: packaging/macos/build.sh writes
FeetBrowser-<version>.dmg, packaging/linux/build-appimage.sh writes
FeetBrowser-<version>-x86_64.AppImage, and packaging/windows/build.ps1
reads the same string back out of the launcher it just built. The release
itself, on the other hand, is named after the tag that triggered it.

Nothing has ever forced those two to agree, and the way they come apart is
quiet: tagging v0.7.0 without bumping __init__.py first produces a release
called v0.7.0 containing three files called FeetBrowser-0.6.0, which
nobody notices until somebody downloads one and asks which of the two
numbers is the truth. So release.yml asks this question first, in a job
that costs seconds, rather than finding out after three quarters of an
hour of building on three operating systems.

    python3 packaging/release_version.py refs/tags/v0.6.0

prints `0.6.0` and exits 0, or says what is wrong and exits 1.
"""
import os
import re
import sys

# Two or more dot-separated numbers, and then whatever a pre-release
# suffix wants to be. Deliberately not a full PEP 440 parser: the only job
# here is to tell a version from a branch name, so that `refs/heads/main`
# arriving where a tag was expected fails loudly instead of becoming a
# release called "main".
VERSION = re.compile(r"^[0-9]+(\.[0-9]+)+[0-9A-Za-z.+-]*$")


def package_version(root):
    """__version__, out of feetbrowser/__init__.py.

    Read rather than imported. This runs before the Rust engine is built,
    and on a machine with no compiled extension `import feetbrowser` is a
    different failure with a much less useful message -- one that looks
    like a broken checkout rather than a version question.
    """
    path = os.path.join(root, "feetbrowser", "__init__.py")
    with open(path, encoding="utf8") as handle:
        found = re.search(r'^__version__ = "([^"]+)"', handle.read(),
                          re.MULTILINE)
    if not found:
        raise ValueError("no __version__ line in %s" % path)
    return found.group(1)


def requested_version(text):
    """The version a run was asked for, however it was asked.

    `refs/tags/v0.6.0` is what a tag push puts in github.ref;
    `v0.6.0` is what github.ref_name gives; `0.6.0` is what a person
    types into the workflow_dispatch box. All three mean one release.
    """
    text = text.strip()
    if text.startswith("refs/tags/"):
        text = text[len("refs/tags/"):]
    if text[:1] in ("v", "V"):
        text = text[1:]
    if not VERSION.match(text):
        raise ValueError(
            'cannot read a version out of "%s". Expected a tag like '
            'v0.6.0, or the number on its own.' % text)
    return text


def resolve(text, root):
    """The version to cut, or an explanation of why the two disagree."""
    asked = requested_version(text)
    have = package_version(root)
    if asked != have:
        raise ValueError(
            "asked to release %s, but feetbrowser/__init__.py says %s.\n"
            "The .dmg, the .AppImage and the Windows bundle all take "
            "their names from __version__, so this would publish a %s "
            "release full of files called FeetBrowser-%s. Bump "
            "__version__ and commit it, or tag the version that is "
            "already there." % (asked, have, asked, have))
    return asked


def main(argv):
    if len(argv) != 2:
        print("usage: release_version.py <tag-ref-or-version>",
              file=sys.stderr)
        return 2
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        print(resolve(argv[1], root))
    except (ValueError, OSError) as complaint:
        print("release version: %s" % complaint, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
