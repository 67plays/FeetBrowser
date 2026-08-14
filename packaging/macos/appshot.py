#!/usr/bin/env python3
"""Run the built FeetBrowser.app and check what comes out of it.

verify.sh reads the bundle. This runs it, which is a different question: a
bundle can pass every static check and still fail to start because a module
is missing, because the trust store is not where the launcher says it is, or
because the app is only ever launched from the directory it was built in.

The assertion is the one tests/x11_shot.py makes about a real X11 window --
render a page whose colours are known, count them, and insist they arrived in
the right order -- moved to the only thing that can be photographed here: the
app's own `--screenshot`, produced by the interpreter, the extension and the
fonts that ship inside the bundle. A picture whose swatches are the wrong way
round is a broken build even though the PNG is valid and the exit status is
zero, and that is exactly the failure a file-size check misses.

Four stages, each of which can be skipped:

  render   the page above, from an unrelated working directory, with the
           environment stripped down to nothing and then poisoned -- if
           anything in the bundle is being resolved through PYTHONHOME,
           PYTHONPATH or a virtualenv, it fails here rather than on a
           stranger's Mac.
  https    a real https:// page, twice: once normally and once with the
           bundled trust store taken away. The first has to succeed, and the
           two have to differ -- which is what says certificates are being
           verified against the roots in the bundle, rather than the page
           having failed identically both times.
  gui      launch it the way a user does and ask LaunchServices who it
           thinks is running. This is the check that the app has its own
           identity: without a real Mach-O in Contents/MacOS the answer is
           "Python", with the framework's icon and bundle identifier.
  frozen   nothing inside the bundle was written to by any of the above. A
           .dmg is mounted read-only, so a bundle that writes to itself at
           startup works everywhere except where users get it.

    python3 packaging/macos/appshot.py path/to/FeetBrowser.app [out.png]
"""
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

PAGE = """<!doctype html>
<html><head><title>FeetBrowser.app</title><style>
body { font: 16px sans-serif; margin: 0; background: #f4f6f8; color: #1c2430; }
header { background: #2f6fb0; color: #fff; padding: 24px 32px; }
h1 { margin: 0 0 6px; font-size: 30px; }
main { padding: 24px 32px; }
.row { display: block; margin: 0 0 18px; }
.swatch { display: inline-block; width: 60px; height: 60px;
          border: 2px solid #1c2430; }
table { border-collapse: collapse; margin-top: 16px; }
td, th { border: 1px solid #9aa7b4; padding: 6px 14px; text-align: left; }
th { background: #dde5ec; }
</style></head><body>
<header><h1>FeetBrowser.app</h1>
<p>Rendered by the CPython, the Rust engine and the fonts that live inside
the application bundle, on a Mac that is not required to have any of them.</p>
</header>
<main>
<p class="row">
<span class="swatch" style="background:#d92b2b"></span>
<span class="swatch" style="background:#2bd94f"></span>
<span class="swatch" style="background:#2b6bd9"></span>
<span class="swatch" style="background:#111111"></span>
<span class="swatch" style="background:#ffffff"></span>
</p>
<p>If the three colour patches read red, green and blue in that order, the
whole path from the parser to the PNG ran out of the bundle.</p>
<table>
<tr><th>Piece</th><th>What it proves</th></tr>
<tr><td>Layout</td><td>the stylesheet and the box model</td></tr>
<tr><td>Text</td><td>the font engine, drawn into our own surface</td></tr>
<tr><td>Colour</td><td>the rasteriser and the PNG writer</td></tr>
<tr><td>Borders</td><td>the extension module loaded and was importable</td></tr>
</table>
</main></body></html>
"""

SWATCHES = ((0xD9, 0x2B, 0x2B), (0x2B, 0xD9, 0x4F), (0x2B, 0x6B, 0xD9))
HEADER = (0x2F, 0x6F, 0xB0)


# -- reading the picture back ------------------------------------------------
#
# The browser writes PNGs and cannot read them; imagecodec can, but importing
# it would mean running this out of a working checkout, and the whole point
# is to test a bundle that has been moved away from one. So the eleven lines
# of inverse filter are here. Bit depth 8, no interlacing, which is what
# Surface.save_png writes.

def read_png(path):
    """`(width, height, channels, pixels)` from a PNG file."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s is not a PNG" % path)
    at, idat, head = 8, bytearray(), None
    while at < len(data):
        length = int.from_bytes(data[at:at + 4], "big")
        kind = data[at + 4:at + 8]
        body = data[at + 8:at + 8 + length]
        at += 12 + length
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    width, height, depth, colour, _, _, interlace = head
    if depth != 8 or interlace:
        raise ValueError("only 8-bit non-interlaced PNGs are read here")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[colour]
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(stride * height)
    prev = bytearray(stride)
    at = 0
    for y in range(height):
        kind = raw[at]
        at += 1
        line = bytearray(raw[at:at + stride])
        at += stride
        if kind == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                near = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + near) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, channels, bytes(out)


def check(width, height, channels, pixels):
    """Say what is wrong with the render, or nothing if it is right."""
    total = {colour: 0 for colour in SWATCHES + (HEADER,)}
    sum_x = dict(total)
    for y in range(height):
        row = y * width * channels
        for x in range(width):
            at = row + x * channels
            colour = (pixels[at], pixels[at + 1], pixels[at + 2])
            if colour in total:
                total[colour] += 1
                sum_x[colour] += x
    if total[HEADER] < 5000:
        return "the header block is missing (%d px of #2f6fb0)" % total[HEADER]
    for colour in SWATCHES:
        if total[colour] < 2000:
            return ("the #%02x%02x%02x swatch is missing (%d px)"
                    % (colour + (total[colour],)))
    middles = [sum_x[colour] / total[colour] for colour in SWATCHES]
    if not middles[0] < middles[1] < middles[2]:
        return ("red, green and blue came out at x=%s, which is not the "
                "order they were drawn in" % [round(m) for m in middles])
    return None


# -- running the app ---------------------------------------------------------

def run(app, args, home, env=None, cwd="/", timeout=180):
    """The bundle's own executable, with a controlled environment.

    Not `open -a`: that hands the launch to LaunchServices, which answers
    before the app has done anything and swallows its exit status. The
    executable inside Contents/MacOS is the same program, and it is a good
    deal easier to know when it has finished.

    The environment is built from nothing rather than filtered, so anything
    the bundle needs and does not carry shows up as a failure here. HOME is
    a scratch directory: the browser keeps its history and its cache under
    it, and a test that writes into the real one is a test nobody can run
    twice.
    """
    base = {"HOME": home, "TMPDIR": os.path.join(home, "tmp"),
            "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
    base.update(env or {})
    os.makedirs(base["TMPDIR"], exist_ok=True)
    return subprocess.run([os.path.join(app, "Contents/MacOS/FeetBrowser")]
                          + list(args), env=base, cwd=cwd, timeout=timeout,
                          capture_output=True, text=True)


def census(app):
    """Every file in the bundle, with its size and modification time."""
    seen = {}
    for folder, _, files in os.walk(app):
        for name in files:
            path = os.path.join(folder, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            seen[path[len(app):]] = (info.st_size, info.st_mtime_ns)
    return seen


def stage_render(app, home, out):
    """Render the known page and count its colours."""
    scratch = os.path.dirname(out)
    page = os.path.join(scratch, "appshot page.html")   # a space, on purpose
    with open(page, "w") as f:
        f.write(PAGE)
    # The space stays a space: the browser's file:// handler takes the path
    # after the scheme literally, so percent-encoding it here would be
    # testing an encoder that is not in the loop.
    url = "file://" + page

    # Twice: once with nothing in the environment, and once with the two
    # variables that would redirect a badly built bundle to the machine's own
    # Python set to somewhere that does not exist. The second run proves the
    # launcher's PyConfig is the thing deciding where the stdlib is.
    for label, env in (("a bare environment", {}),
                       ("PYTHONHOME and PYTHONPATH poisoned",
                        {"PYTHONHOME": "/nonexistent/python",
                         "PYTHONPATH": "/nonexistent/site-packages",
                         "VIRTUAL_ENV": "/nonexistent/venv"})):
        done = run(app, ["--screenshot", url, out], home, env)
        if done.returncode != 0:
            return ("--screenshot failed with %s (exit %d)\n%s"
                    % (label, done.returncode, done.stderr.strip()))
        if not os.path.exists(out):
            return "--screenshot wrote nothing with %s" % label
        print("  ran with %s: %s" % (label, done.stdout.strip() or "ok"))

    size = os.path.getsize(out)
    width, height, channels, pixels = read_png(out)
    print("  %s (%dx%d, %d bytes)" % (out, width, height, size))
    if size < 5000:
        return "the screenshot is suspiciously small; nothing was drawn"
    problem = check(width, height, channels, pixels)
    if problem:
        return "the page came out wrong: %s" % problem
    print("  red, green and blue arrived in order; the header block is there")
    return None


def stage_https(app, home, scratch):
    """Fetch a real https:// page, and prove the certificates mattered."""
    url = os.environ.get("FEETBROWSER_HTTPS_URL", "https://example.com/")
    good = os.path.join(scratch, "https-trusted.png")
    bad = os.path.join(scratch, "https-untrusted.png")

    done = run(app, ["--screenshot", url, good], home)
    if done.returncode != 0 or not os.path.exists(good):
        return ("could not load %s: exit %d\n%s"
                % (url, done.returncode, done.stderr.strip()))

    # SSL_CERT_FILE set from outside wins, because launcher.c sets it with
    # setenv's overwrite flag at 0. Pointing it at an empty file leaves the
    # bundle with no roots at all, so the same fetch has to come out
    # differently -- and if it does not, the first one was not verifying
    # anything either.
    empty = os.path.join(scratch, "no-roots.pem")
    open(empty, "w").close()
    run(app, ["--screenshot", url, bad], home,
        {"SSL_CERT_FILE": empty, "SSL_CERT_DIR": os.path.join(scratch, "nope")})

    trusted = hashlib.sha256(open(good, "rb").read()).hexdigest()
    print("  %s with the bundled roots: %d bytes, sha256 %s"
          % (url, os.path.getsize(good), trusted[:16]))
    if not os.path.exists(bad):
        print("  without them: nothing rendered at all")
        return None
    untrusted = hashlib.sha256(open(bad, "rb").read()).hexdigest()
    print("  without them: %d bytes, sha256 %s"
          % (os.path.getsize(bad), untrusted[:16]))
    if trusted == untrusted:
        return ("the page rendered identically with and without a trust "
                "store, so nothing was being verified")
    return None


def lsappinfo(pid):
    """The `lsappinfo list` entry for `pid`, as lines, or None.

    The listing is one indented block per registered application, each
    beginning with a numbered line, so the blocks are split on that and the
    one whose `pid = ` field matches is the answer. Grepping the whole
    listing for the identifier would pass while some other copy of the app
    was open.
    """
    listing = subprocess.run(["/usr/bin/lsappinfo", "list"],
                             capture_output=True, text=True).stdout
    block = []
    for line in listing.splitlines():
        if re.match(r"^\s*\d+\)\s", line):
            if any(re.search(r"\bpid = %d\b" % pid, l) for l in block):
                return block
            block = []
        block.append(line)
    if any(re.search(r"\bpid = %d\b" % pid, l) for l in block):
        return block
    return None


def stage_gui(app, home, scratch):
    """Launch it as an app and ask the system whose window it is."""
    page = os.path.join(scratch, "appshot page.html")
    executable = os.path.join(app, "Contents/MacOS/FeetBrowser")
    env = {"HOME": home, "TMPDIR": os.path.join(home, "tmp"),
           "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
    child = subprocess.Popen([executable, "file://" + page],
                             env=env, cwd="/", stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        # LaunchServices is the authority on what an app *is*: it reads the
        # Info.plist of the bundle the running image sits in. A launcher that
        # exec'd an interpreter would be registered as Python here, with
        # Python's name and Python's bundle identifier, which is the whole
        # reason Contents/MacOS holds a real Mach-O.
        deadline = time.time() + 30
        entry = None
        while time.time() < deadline and entry is None:
            if child.poll() is not None:
                out, err = child.communicate()
                return "the app exited early (%d)\n%s%s" % (
                    child.returncode, out, err)
            time.sleep(1)
            entry = lsappinfo(child.pid)
        if entry is None:
            return ("the app never registered with LaunchServices, so it "
                    "would have no Dock tile and no keyboard focus")
        for line in entry:
            print("    %s" % line.strip())
        if not any("io.github.67plays.FeetBrowser" in line for line in entry):
            return ("LaunchServices does not know it as FeetBrowser:\n%s"
                    % "\n".join(entry))
        print("  the running app is FeetBrowser, not Python")
        return None
    finally:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip().splitlines()[-1].strip())
    app = os.path.abspath(sys.argv[1])
    if not os.path.isdir(app):
        sys.exit("no such bundle: %s" % app)
    skip = set(os.environ.get("FEETBROWSER_SKIP", "").split(","))

    # A directory with a space in its name, because a path that is quoted
    # everywhere except one place works until the day somebody drags the app
    # into "Applications 2" or runs it from a volume called "Macintosh HD".
    scratch = tempfile.mkdtemp(prefix="FeetBrowser shot ")
    home = os.path.join(scratch, "a home")
    os.makedirs(home)
    out = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 \
        else os.path.join(scratch, "feetbrowser-app.png")
    # The caller names a file, not a directory that already exists, and the
    # render stage writes its page next to the screenshot. Make the place
    # rather than requiring every caller to remember to.
    os.makedirs(os.path.dirname(out), exist_ok=True)
    before = census(app)
    failures = []
    try:
        for name, stage in (("render", lambda: stage_render(app, home, out)),
                            ("https", lambda: stage_https(app, home, scratch)),
                            ("gui", lambda: stage_gui(app, home, scratch))):
            print("\n== %s" % name)
            if name in skip:
                print("  skipped")
                continue
            problem = stage()
            if problem:
                print("FAIL: %s" % problem)
                failures.append(name)

        print("\n== frozen")
        after = census(app)
        changed = sorted(k for k in set(before) | set(after)
                         if before.get(k) != after.get(k))
        if changed:
            print("FAIL: the app wrote inside its own bundle:")
            for path in changed[:20]:
                print("   %s" % path)
            failures.append("frozen")
        else:
            print("  nothing in the bundle changed; it can live on a "
                  "read-only volume")
    finally:
        if out.startswith(scratch):
            print("\n(screenshot left in %s)" % scratch)
        else:
            shutil.rmtree(scratch, ignore_errors=True)

    print()
    if failures:
        sys.exit("appshot.py: %s failed" % ", ".join(failures))
    print("appshot.py: the app runs, renders, fetches over TLS and is itself")


if __name__ == "__main__":
    main()
