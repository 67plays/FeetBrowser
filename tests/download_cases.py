"""Downloads: filename safety, streaming, progress, cancellation, resume.

Everything here runs against a server this file starts on a loopback port,
so no assertion depends on the live web being up or on anybody's bandwidth.
The interesting cases are the ones a download manager gets wrong in the
field: a body with no Content-Length, a peer that hangs up halfway, a server
that suggests "../../etc/passwd" as a filename, two files that want the same
name, a user who changes their mind, and a file far larger than memory.

Why this is not tests/test_downloads.py: test.sh and the CI workflow name
every suite explicitly and tests/test_suites.py checks that they do. This
change is not allowed to edit either file, so a suite named test_*.py here
would be a suite nothing runs -- exactly what test_suites.py exists to
catch. tests/test_nav.py calls main() below instead, which puts these cases
in ./test.sh and in CI without touching a runner.
"""

import http.server
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import tracemalloc

try:
    import resource            # POSIX only; Windows has no getrusage
except ImportError:            # pragma: no cover - only taken on Windows
    resource = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import downloads
from feetbrowser.downloads import (
    CANCELLED, COMPLETE, FAILED, DownloadManager, filename_for,
    sanitize_filename, should_download)
from feetbrowser.net import URL

#: Body size for the "much bigger than any buffer" case: past the 64 MB cap
#: net.py puts on a buffered response, so passing it is proof the bytes
#: never went through one.
BIG = 128 * 1024 * 1024

SMALL = 64 * 1024


def payload(size):
    """A body whose every byte is a function of its offset, so a file that
    is the right length but assembled in the wrong order still fails."""
    block = bytes(range(256))
    return (block * (size // 256 + 1))[:size]


class _Handler(http.server.BaseHTTPRequestHandler):
    """Every response shape the download path has to cope with."""

    protocol_version = "HTTP/1.1"
    # Requests seen per path, so a test can prove a resume actually happened
    # on the wire and not just in the bookkeeping.
    seen = {}

    def log_message(self, *args):
        pass

    def _head(self, status, length=None, extra=(), ctype="application/octet-stream"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()

    def _hangup(self):
        """Drop the connection the way a flaky server or a dead link does."""
        try:
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()

    def do_GET(self):
        path = self.path.split("?")[0]
        _Handler.seen[path] = _Handler.seen.get(path, 0) + 1
        try:
            handler = getattr(self, "route_" + path.strip("/").replace("-", "_"),
                              None)
            if handler is None:
                self._head(404, length=9, ctype="text/html")
                self.wfile.write(b"<i>gone</i>")
                return
            handler()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    # -- routes ----------------------------------------------------------

    def route_known(self):
        body = payload(SMALL)
        self._head(200, len(body))
        self.wfile.write(body)

    def route_named(self):
        body = payload(1024)
        self._head(200, len(body), ctype="text/plain",
                   extra=[("Content-Disposition",
                           'attachment; filename="quarterly report.csv"')])
        self.wfile.write(body)

    def route_hostile(self):
        body = payload(512)
        self._head(200, len(body), extra=[
            ("Content-Disposition", 'attachment; filename="../../etc/passwd"')])
        self.wfile.write(body)

    def route_device(self):
        body = payload(512)
        self._head(200, len(body),
                   extra=[("Content-Disposition", 'attachment; filename="CON"')])
        self.wfile.write(body)

    def route_chunked_slow(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        body = payload(SMALL)
        for start in range(0, len(body), 4096):
            piece = body[start:start + 4096]
            self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            self.wfile.flush()
            time.sleep(0.05)
        self.wfile.write(b"0\r\n\r\n")

    def route_chunked(self):
        # No Content-Length anywhere: the client cannot know the total, and
        # must not invent one.
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        body = payload(SMALL)
        for start in range(0, len(body), 4096):
            piece = body[start:start + 4096]
            self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
        self.wfile.write(b"0\r\n\r\n")

    def route_drop(self):
        self._head(200, SMALL)
        self.wfile.write(payload(SMALL)[:SMALL // 4])
        self._hangup()

    def route_resume(self):
        body = payload(SMALL)
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            self._head(206, len(body) - start, extra=[
                ("Content-Range", "bytes %d-%d/%d"
                 % (start, len(body) - 1, len(body))),
                ("Accept-Ranges", "bytes")])
            self.wfile.write(body[start:])
            return
        self._head(200, len(body), extra=[("Accept-Ranges", "bytes")])
        self.wfile.write(body[:len(body) // 2])
        self._hangup()

    def route_slow(self):
        self._head(200, SMALL * 64)
        for _ in range(64):
            self.wfile.write(payload(SMALL))
            self.wfile.flush()
            time.sleep(0.05)

    def route_big(self):
        self._head(200, BIG)
        block = payload(64 * 1024)
        for _ in range(BIG // len(block)):
            self.wfile.write(block)

    def route_page(self):
        body = b"<html><body><h1>a page</h1></body></html>"
        self._head(200, len(body), ctype="text/html")
        self.wfile.write(body)


class _Server:
    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                     _Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d%s" % (host, port, path)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


SERVER = None
TMP = None


def manager():
    return DownloadManager(directory=TMP)


def run(path, filename=None):
    """Download `path` here and now, on this thread. Returns the record."""
    return manager().start(URL(SERVER.url(path)), filename=filename,
                           threaded=False)


def wait_for(predicate, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    raise AssertionError("timed out waiting for %s" % what)


def leftovers():
    return sorted(os.listdir(TMP))


# -- filenames ----------------------------------------------------------

def test_sanitize_filename_defuses_hostile_names():
    cases = {
        "../../etc/passwd": "passwd",
        "..\\..\\windows\\system32\\cmd.exe": "cmd.exe",
        "/etc/passwd": "passwd",
        "/": "download",
        "C:\\Windows\\System32\\drivers\\etc\\hosts": "hosts",
        "\\\\server\\share\\payroll.xlsx": "payroll.xlsx",
        "%2e%2e%2f%2e%2e%2fetc%2fshadow": "shadow",
        "report\x00.exe": "report.exe",
        "bell\x07.txt": "bell.txt",
        "..": "download",
        ".": "download",
        "": "download",
        "   ": "download",
        ".bashrc": "bashrc",
        "CON": "_CON",
        "con.txt": "_con.txt",
        "NUL.tar.gz": "_NUL.tar.gz",
        "com1": "_com1",
        "COM9.log": "_COM9.log",
        "lpt1.txt": "_lpt1.txt",
        "LPT9": "_LPT9",
        "aux": "_aux",
        "PRN.pdf": "_PRN.pdf",
        "ordinary name.tar.gz": "ordinary name.tar.gz",
        "héllo.txt": "héllo.txt",
    }
    for given, expected in cases.items():
        got = sanitize_filename(given)
        assert got == expected, "%r -> %r, expected %r" % (given, got, expected)
    long_name = "a" * 500 + ".txt"
    trimmed = sanitize_filename(long_name)
    assert len(trimmed) <= downloads.MAX_NAME and trimmed.endswith(".txt"), \
        trimmed
    print("  %d hostile names defused" % (len(cases) + 1))


def test_no_sanitised_name_escapes_the_directory():
    """The property that actually matters: whatever the server sends, the
    file lands inside the download directory and nowhere else."""
    root = os.path.realpath(TMP)
    hostile = ["../../etc/passwd", "/etc/passwd", "..", "../..",
               "....//....//etc/passwd", "C:\\boot.ini", "a/b/c/d",
               "%2e%2e%2fescape", "\x00/../../root/.ssh/authorized_keys",
               "CON", "nul", "sub/../../../../../../tmp/pwned"]
    for name in hostile:
        target = os.path.join(root, sanitize_filename(name))
        parent = os.path.dirname(os.path.realpath(target))
        assert parent == root, "%r escaped to %s" % (name, target)
        assert os.sep not in sanitize_filename(name)
    print("  %d traversal attempts stayed inside %s" % (len(hostile), root))


def test_filename_comes_from_the_header_then_the_url():
    header = {"content-disposition": 'attachment; filename="notes (final).txt"'}
    assert filename_for(URL("http://x/y/z"), header) == "notes (final).txt"
    rfc5987 = {"content-disposition":
               "attachment; filename*=UTF-8''%E2%82%AC%20rates.csv"}
    assert filename_for(URL("http://x/y"), rfc5987) == "€ rates.csv"
    escaped = {"content-disposition":
               "attachment; filename*=UTF-8''%2e%2e%2f%2e%2e%2fpasswd"}
    assert filename_for(URL("http://x/y"), escaped) == "passwd"
    assert filename_for(URL("http://x/dir/file.tar.gz")) == "file.tar.gz"
    assert filename_for(URL("http://x/dir/a%20b.pdf")) == "a b.pdf"
    assert filename_for(URL("http://x/dir/file.zip?v=2")) == "file.zip"
    assert filename_for(URL("http://x/dir/")) == "dir"
    assert filename_for(URL("http://example.com")) == "example.com"
    print("  header, URL and fallback names all resolve")


def test_download_or_render_decision():
    assert should_download({"content-disposition": "attachment"})
    assert should_download({"content-disposition": 'attachment; filename="a"'},
                           "text/html")
    assert should_download({}, "application/octet-stream")
    assert should_download({}, "application/zip")
    assert should_download({}, "video/mp4")
    assert not should_download({}, "text/html")
    assert not should_download({}, "text/html; charset=utf-8")
    assert not should_download({}, "image/png")
    assert not should_download({}, "application/json")
    assert not should_download({}, "")
    assert not should_download({"content-disposition": "inline"}, "text/plain")
    print("  attachments and unrenderable types download, pages do not")


# -- transfers ----------------------------------------------------------

def test_known_length_download_lands_intact():
    download = run("/known")
    assert download.state == COMPLETE, (download.state, download.error)
    assert download.total == SMALL and download.received == SMALL
    assert download.percent() == 1.0
    with open(download.path, "rb") as handle:
        assert handle.read() == payload(SMALL), "bytes on disk differ"
    assert os.path.basename(download.path) == "known", download.path
    assert leftovers() == ["known"], leftovers()
    os.unlink(download.path)
    print("  %d bytes, byte-for-byte, no .part left behind" % SMALL)


def test_unknown_length_download_does_not_invent_a_percentage():
    download = run("/chunked")
    assert download.state == COMPLETE, (download.state, download.error)
    assert download.total is None, download.total
    assert download.percent() is None, "a chunked body has no honest percent"
    assert download.eta() is None
    with open(download.path, "rb") as handle:
        assert handle.read() == payload(SMALL)
    os.unlink(download.path)

    # And while it is still running, the status line says so in words
    # instead of showing a bar filled to a fraction nobody sent.
    running = manager().start(URL(SERVER.url("/chunked-slow")))
    wait_for(lambda: running.received > 0, what="the first chunk")
    assert running.percent() is None and running.eta() is None
    line = running.describe()
    assert "unknown size" in line, line
    running.cancel()
    wait_for(lambda: running.state == CANCELLED, what="the cancel")
    print("  chunked body arrived whole; mid-flight status was %r" % line)


def test_attachment_filename_is_sanitised_on_the_way_to_disk():
    download = run("/hostile")
    assert download.state == COMPLETE, (download.state, download.error)
    assert os.path.basename(download.path) == "passwd", download.path
    assert os.path.dirname(os.path.realpath(download.path)) \
        == os.path.realpath(TMP), download.path
    assert not os.path.exists("/etc/passwd.part")
    os.unlink(download.path)

    device = run("/device")
    assert os.path.basename(device.path) == "_CON", device.path
    os.unlink(device.path)

    named = run("/named")
    assert os.path.basename(named.path) == "quarterly report.csv", named.path
    os.unlink(named.path)
    print("  '../../etc/passwd' saved as 'passwd', 'CON' as '_CON'")


def test_name_collisions_get_a_counter():
    first = run("/known")
    second = run("/known")
    third = run("/known")
    names = sorted(os.path.basename(d.path) for d in (first, second, third))
    assert names == ["known", "known (1)", "known (2)"], names
    for download in (first, second, third):
        assert os.path.getsize(download.path) == SMALL
        os.unlink(download.path)
    print("  three downloads of one name: %s" % ", ".join(names))


def test_a_dropped_connection_fails_the_download_and_leaves_nothing():
    download = run("/drop")
    assert download.state == FAILED, (download.state, download.error)
    assert "connection" in (download.error or "").lower(), download.error
    assert download.path is None
    assert leftovers() == [], "a broken transfer left %s" % leftovers()
    print("  mid-transfer hangup -> failed (%s)" % download.error)


def test_a_404_fails_the_download():
    download = run("/nothing-here")
    assert download.state == FAILED, download.state
    assert "404" in (download.error or ""), download.error
    assert leftovers() == [], leftovers()
    print("  404 -> failed (%s)" % download.error)


def test_a_directory_that_cannot_be_written_fails_the_download():
    if os.name == "nt":
        _unusable_directory_on_windows()
        return
    if os.geteuid() == 0:
        print("  skipped: running as root, permissions do not apply")
        return
    locked = tempfile.mkdtemp()
    os.chmod(locked, 0o500)
    try:
        blocked = DownloadManager(directory=locked)
        download = blocked.start(URL(SERVER.url("/known")), threaded=False)
        assert download.state == FAILED, download.state
        assert "permission" in (download.error or "").lower(), download.error
        print("  unwritable directory -> failed (%s)" % download.error)
    finally:
        os.chmod(locked, 0o700)
        shutil.rmtree(locked)


def _unusable_directory_on_windows():
    """The same test where chmod means nothing.

    Taking write off a directory needs an ACL edit on Windows, and a mode
    bit will not do it. A file sitting where the download directory has to
    be refuses just as firmly and asks the same question: does an
    unusable directory end as a failed download, or as a traceback?
    """
    handle, blocking_file = tempfile.mkstemp()
    os.close(handle)
    try:
        blocked = DownloadManager(directory=blocking_file)
        download = blocked.start(URL(SERVER.url("/known")), threaded=False)
        assert download.state == FAILED, download.state
        assert download.error, "a failed download with nothing to say"
        print("  unusable directory -> failed (%s)" % download.error)
    finally:
        os.unlink(blocking_file)


def test_cancel_stops_the_transfer_and_removes_the_part_file():
    downloads_manager = manager()
    download = downloads_manager.start(URL(SERVER.url("/slow")))
    wait_for(lambda: download.received > 0, what="the first bytes")
    assert download.percent() is not None and download.percent() < 1.0
    part = download.part_path
    assert part and part.endswith(".part") and os.path.exists(part), part
    assert download.cancel() is True
    wait_for(lambda: download.state == CANCELLED, what="the cancelled state")
    assert not os.path.exists(part), "the .part file outlived the cancel"
    assert leftovers() == [], leftovers()
    assert download.cancel() is False, "cancelling a finished download is a no-op"
    print("  cancelled after %d bytes, nothing left on disk"
          % download.received)


def test_progress_reports_rate_and_eta_while_running():
    downloads_manager = manager()
    download = downloads_manager.start(URL(SERVER.url("/slow")))
    wait_for(lambda: download.received > SMALL * 4, what="a few blocks")
    fraction, rate, eta = download.percent(), download.rate(), download.eta()
    assert 0 < fraction < 1, fraction
    assert rate > 0, rate
    assert eta is not None and eta > 0, eta
    line = download.describe()
    assert "of" in line and "/s" in line, line
    download.cancel()
    wait_for(lambda: download.state == CANCELLED, what="the cancel")
    print("  mid-flight: %.0f%%, %s" % (fraction * 100, line))


def test_a_resumable_break_is_resumed_with_a_range_request():
    _Handler.seen.pop("/resume", None)
    download = run("/resume")
    assert download.state == COMPLETE, (download.state, download.error)
    assert download.resumed == 1, download.resumed
    assert _Handler.seen["/resume"] == 2, _Handler.seen
    with open(download.path, "rb") as handle:
        assert handle.read() == payload(SMALL), \
            "the resumed half did not join the first half correctly"
    os.unlink(download.path)
    print("  dropped at half, resumed by Range, %d bytes verified" % SMALL)


def test_memory_stays_flat_for_a_file_far_larger_than_any_buffer():
    """Proof, not assertion: 128 MB through a process that never holds more
    than a chunk of it. tracemalloc measures what Python allocated; maxrss
    catches anything it would miss, where the platform offers it -- Windows
    has no getrusage, so there the tracemalloc half stands on its own."""
    before_rss = _maxrss()
    tracemalloc.start()
    start = time.monotonic()
    download = run("/big")
    traced, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    grew = _maxrss() - before_rss
    assert download.state == COMPLETE, (download.state, download.error)
    assert os.path.getsize(download.path) == BIG, os.path.getsize(download.path)
    assert download.received == BIG
    budget = 8 * 1024 * 1024
    assert peak < budget, "peak python memory %d for a %d byte file" % (peak,
                                                                       BIG)
    assert grew < 64 * 1024 * 1024, "rss grew %d bytes" % grew
    os.unlink(download.path)
    print("  %d MB in %.1fs: python peak %.1f MB, rss +%.1f MB (traced %.1f MB)"
          % (BIG // (1024 * 1024), time.monotonic() - start,
             peak / 1e6, grew / 1e6, traced / 1e6))


def _maxrss():
    """Peak resident bytes so far, or 0 where the platform will not say.

    getrusage reports kilobytes on Linux and bytes on macOS, which is a
    documented difference and not a bug to work around. Windows has no
    getrusage at all; 0 there makes the delta 0, which passes the bound
    without pretending it was measured."""
    if resource is None:
        return 0
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def test_two_downloads_run_at_the_same_time():
    downloads_manager = manager()
    first = downloads_manager.start(URL(SERVER.url("/slow")))
    second = downloads_manager.start(URL(SERVER.url("/slow")))
    wait_for(lambda: first.received > 0 and second.received > 0,
             what="both transfers to be moving")
    assert len(downloads_manager.active()) == 2
    for download in (first, second):
        download.cancel()
    wait_for(lambda: all(d.state == CANCELLED for d in (first, second)),
             what="both cancels")
    downloads_manager.clear_finished()
    assert downloads_manager.items() == []
    print("  two transfers in flight at once, both cancelled, list cleared")


# -- the browser ---------------------------------------------------------

def test_a_link_to_an_attachment_downloads_instead_of_navigating():
    from feetbrowser.browser import Browser
    browser = Browser()
    browser.downloads = DownloadManager(directory=TMP)
    browser.new_tab(SERVER.url("/page"))
    browser.settle(10)
    tab = browser.active_tab
    assert "a page" in " ".join(
        getattr(cmd, "text", "") for cmd in tab.display_list)
    where_it_was = str(tab.url)

    tab.load(SERVER.url("/named"))
    browser.settle(10)
    assert str(tab.url) == where_it_was, \
        "the tab navigated to an attachment instead of downloading it"
    wait_for(lambda: browser.downloads.items(), what="the download to appear")
    download = browser.downloads.items()[0]
    wait_for(lambda: download.state == COMPLETE, what="the download to finish")
    assert os.path.basename(download.path) == "quarterly report.csv"
    os.unlink(download.path)
    print("  clicking an attachment left the page alone and saved the file")
    browser.window.destroy()


def test_the_panel_draws_every_state_and_cancels_the_row_it_is_asked_to():
    from feetbrowser.browser import Browser
    browser = Browser()
    browser.downloads = DownloadManager(directory=TMP)
    panel = browser.downloads_panel
    assert panel.hit(10, 10) is None, "a closed panel eats no clicks"

    running = browser.downloads.start(URL(SERVER.url("/slow")))
    wait_for(lambda: running.received > 0, what="the transfer to start")
    done = browser.downloads.start(URL(SERVER.url("/known")), threaded=False)
    failed = browser.downloads.start(URL(SERVER.url("/nothing-here")),
                                     threaded=False)
    assert (done.state, failed.state) == (COMPLETE, FAILED)

    panel.open_ = True
    browser.draw()
    drawn = [item.opts.get("text", "")
             for item in browser.canvas._by_tag.get("downloads", [])]
    assert "Downloads" in drawn
    assert any(text.startswith("slow") for text in drawn), drawn
    assert any("Failed" in text for text in drawn), drawn
    assert any("Complete" in text for text in drawn), drawn
    bars = [item for item in browser.canvas._by_tag["downloads"]
            if item.kind == "rectangle"]
    assert len(bars) >= 3 + 2, "one progress bar per row, plus the frame"

    # The × on the running row, in the coordinates the panel just laid out.
    row_y = dict((d, y) for d, y in panel._rows)[running]
    action, target = panel.hit(panel.x + panel.width - 18, row_y + 16)
    assert (action, target) == ("cancel", running), (action, target)
    browser._downloads_click(panel.x + panel.width - 18, row_y + 16)
    wait_for(lambda: running.state == CANCELLED, what="the cancel")

    # A click on the header's × closes it; the footer clears what finished.
    assert panel.hit(panel.x + panel.width - 14, panel.y + 10)[0] == "close"
    browser._downloads_click(panel.x + 20,
                             panel.y + panel.height - panel.FOOTER_H / 2)
    assert browser.downloads.items() == [], browser.downloads.items()
    os.unlink(done.path)
    print("  panel drew 3 rows, cancelled the one it was asked to, cleared")
    browser.window.destroy()


def main():
    global SERVER, TMP
    SERVER = _Server()
    TMP = tempfile.mkdtemp(prefix="feetbrowser-downloads-")
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_")]
    failed = []
    try:
        for test in tests:
            print("- %s" % test.__name__)
            # A fresh directory per test: several of them assert on exactly
            # what is left behind, and a leftover from the case before is
            # the kind of failure that sends you looking in the wrong file.
            for name in os.listdir(TMP):
                os.unlink(os.path.join(TMP, name))
            try:
                test()
            except Exception as exc:  # noqa: BLE001 - report and keep going
                import traceback
                traceback.print_exc()
                failed.append("%s: %s" % (test.__name__, exc))
    finally:
        SERVER.stop()
        shutil.rmtree(TMP, ignore_errors=True)
    if failed:
        print("\n%d DOWNLOAD TESTS FAILED:\n  %s"
              % (len(failed), "\n  ".join(failed)))
        sys.exit(1)
    print("\nALL %d DOWNLOAD TESTS PASSED" % len(tests))


if __name__ == "__main__":
    main()
