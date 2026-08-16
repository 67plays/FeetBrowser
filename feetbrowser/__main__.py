import sys

from . import toes
from . import __version__


USAGE = """usage: python3 -m feetbrowser [options] [url]

FeetBrowser: a functional web browser built from scratch.

options:
  -h, --help       show this help message and exit
  -v, --version    print the version and exit
  --screenshot <url> [out.png]
                   render <url> headlessly and write a PNG, then exit
                   (default: feetbrowser.png)
  --toes           list installed toes and their status
  --toe-search <term>      search the ToeHub catalog
  --toe-install <name>     install a toe from the catalog
  --toe-uninstall <name>   uninstall a toe
  --toe-enable <name>      enable a disabled toe
  --toe-disable <name>     disable an installed toe
  --new-toe <name>         scaffold a new toe
  --toe-docs               print the Toes documentation
  --check-video [stream.264 [truth.i420.z]]
                   say whether this build can decode H.264, and prove it
  --check-audio [stream.aac [truth.f32.z]]
                   say whether this build can decode AAC, and prove it

If no URL is given the browser opens the welcome page.
"""


def check_video(args):
    """--check-video: can this build decode video, and does it decode right?

    The interesting caller is the packaging: run inside a built application,
    this is the difference between an app that plays video and one that only
    looks like it does. A bundle that shipped no decoder starts, renders, and
    says nothing at all until somebody opens a video -- so the packaging asks
    here instead, once, at build time.

    With a stream it decodes one; with the I420 a reference decoder produced
    from that stream it compares the pictures byte for byte, because a
    decoder that loads and returns rubbish has passed no test worth having.
    """
    import os
    from feetplayer import h264

    reason = h264.unavailable_reason()
    if reason is not None:
        print("video: no H.264 decoder: %s" % reason)
        return 1
    print("video: H.264 decoder ready, from %s" % h264.library_path())
    if not args:
        return 0
    try:
        with open(args[0], "rb") as handle:
            stream = handle.read()
    except OSError as exc:
        print("video: cannot read %s: %s" % (args[0], exc.strerror or exc))
        return 1
    try:
        width, height, picture = h264.Decoder().decode_i420(stream)
    except h264.H264Error as exc:
        print("video: %s did not decode: %s" % (args[0], exc))
        return 1
    print("video: decoded %s: %dx%d, %d bytes of I420"
          % (os.path.basename(args[0]), width, height, len(picture)))
    if len(args) < 2:
        return 0
    import zlib
    try:
        with open(args[1], "rb") as handle:
            truth = zlib.decompress(handle.read())
    except (OSError, zlib.error) as exc:
        print("video: cannot read %s: %s" % (args[1], exc))
        return 1
    if picture != truth:
        print("video: the picture does not match %s (%d bytes against %d)"
              % (os.path.basename(args[1]), len(picture), len(truth)))
        return 1
    print("video: the picture is identical to the reference decoder's")
    return 0


def main():
    args = sys.argv[1:]
    if not args:
        from .browser import main as browser_main
        sys.exit(browser_main() or 0)
    flag = args[0]
    if flag == "-h" or flag == "--help":
        print(USAGE)
        return
    if flag == "-v" or flag == "--version":
        print(f"FeetBrowser {__version__}")
        return
    if flag == "--toes":
        toes.list_toes()
        return
    if flag == "--toe-search":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --toe-search <term>")
            sys.exit(1)
        toes.search_toes(args[1])
        return
    if flag == "--toe-install":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --toe-install <name>")
            sys.exit(1)
        toes.install_toe(args[1])
        return
    if flag == "--toe-uninstall":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --toe-uninstall <name>")
            sys.exit(1)
        toes.uninstall_toe(args[1])
        return
    if flag == "--toe-enable" or flag == "--toe-disable":
        if len(args) < 2:
            print(f"usage: python3 -m feetbrowser {flag} <name>")
            sys.exit(1)
        sys.exit(toes.set_enabled(args[1], flag == "--toe-enable"))
    if flag == "--new-toe":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --new-toe <name>")
            sys.exit(1)
        sys.exit(toes.new_toe(args[1]))
    if flag == "--toe-docs":
        toes.toe_docs()
        return
    if flag == "--check-video":
        sys.exit(check_video(args[1:]))
    if flag == "--check-audio":
        # The same question as --check-video, asked of the sound decoder,
        # and by the same caller: all three packaging scripts run this
        # inside what they have just built, with the compiler off PATH.
        # The work is in aac.check() rather than here because the answer
        # is that module's to give -- and because a bundle that plays
        # pictures with no sound is a bundle that passes every other check
        # in this file.
        from feetplayer import aac
        sys.exit(aac.check(args[1:]))
    # Anything else is a URL passed to the browser.
    from .browser import main as browser_main
    sys.exit(browser_main() or 0)


if __name__ == "__main__":
    main()
