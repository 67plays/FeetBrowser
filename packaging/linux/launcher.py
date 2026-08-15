"""The entry point the bundle runs instead of ``python3 -m feetbrowser``.

Two things have to happen before the browser starts inside a bundle that a
stranger downloaded, and neither of them belongs in the browser itself.

The first is fonts. ``fontengine`` looks in the four places a Linux system
keeps them and nowhere else, and ``canvas._resolve_face`` raises
``CanvasError: no usable font`` when that search comes back empty -- which is
exactly what happens on a machine with no fonts installed, and is a crash
rather than a page of blank boxes. The bundle carries a set, so this appends
the bundled directory to ``FONT_DIRS_DEFAULT`` (the same list object
``fontengine._dirs()`` returns on Linux, so mutating it in place is all it
takes). Appended, not prepended: a font the user installed should still win,
and the bundled copies are there for the case where there is nothing else.

The second is saying so when even that fails. A bundle assembled wrong
produces a traceback out of the middle of the layout engine, three frames
deep in something the user has never heard of. One sentence at startup is
worth more than that.

Everything else -- argument parsing, the window, --screenshot -- is the
browser's own ``__main__``, unchanged and unwrapped.
"""
import os
import sys


def bundle_root():
    """The bundle's ``usr`` directory, whatever it was mounted at.

    ``$APPDIR`` is set by the AppImage runtime, but this file is also run
    straight out of an extracted directory during testing, so the path
    relative to this file is the fallback rather than the other way round.
    """
    appdir = os.environ.get("APPDIR")
    if appdir and os.path.isdir(os.path.join(appdir, "usr")):
        return os.path.join(appdir, "usr")
    # .../usr/lib/feetbrowser/launcher.py -> .../usr
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def add_bundled_fonts():
    """Make the fonts we ship visible to the font engine. Returns the path."""
    from feetbrowser import fontengine

    fonts = os.path.join(bundle_root(), "share", "feetbrowser", "fonts")
    if os.path.isdir(fonts) and fonts not in fontengine.FONT_DIRS_DEFAULT:
        # The list FONT_DIRS_DEFAULT is the list _dirs() hands back on Linux,
        # so this is the whole of the change.
        fontengine.FONT_DIRS_DEFAULT.append(fonts)
    return fonts


def run_script(argv):
    """Run a script with this bundle set up the way the browser has it.

    This is what ``AppRun --python`` reaches, and the reason it exists is the
    acceptance test: ``tests/x11_shot.py`` has to run against the bundle's
    interpreter, package, engine and fonts inside a container that has none of
    those of its own. Going through here rather than straight to the
    interpreter is what gets it the fonts -- a script run without them dies in
    the layout engine, which would be a bug in the test rather than in the
    bundle.

    sys.path[0] and sys.argv are set to what Python would have made them had
    the script been named on the command line, so a script that imports its
    neighbours still can.
    """
    import runpy

    script = os.path.abspath(argv[0])
    sys.argv = [script] + list(argv[1:])
    sys.path.insert(0, os.path.dirname(script))
    runpy.run_path(script, run_name="__main__")
    return 0


def main():
    fonts = add_bundled_fonts()
    from feetbrowser import fontengine

    if not fontengine.index():
        # The scan is cached, so paying for it here costs the browser nothing
        # later -- and the alternative is a CanvasError out of the layout
        # engine the first time anything draws text.
        sys.stderr.write(
            "FeetBrowser: no usable font found. The bundle ships its own in\n"
            "  %s\nand that directory is empty or unreadable, so this copy is\n"
            "incomplete. Re-download it.\n" % fonts)
        return 1
    if sys.argv[1:2] == ["--run-script"]:
        return run_script(sys.argv[2:])
    from feetbrowser.__main__ import main as browser_main

    return browser_main()


if __name__ == "__main__":
    sys.exit(main() or 0)
