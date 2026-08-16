"""Timings for loading a page, as felt by the person looking at it.

Not a test: nothing here asserts. bench_render.py measures the pixel
pipeline; this measures the other half of a page load -- the subresources a
document names and the work their arrival sets off -- because that is where
a real site's seconds go. Run it before and after.

    .venv/bin/python tests/bench_pageload.py

Two numbers per case, and the second is the interesting one:

  settle    wall clock from new_tab() to a settled page. Includes network.
  stall     the longest single span the UI thread spent inside one batch of
            timer callbacks, i.e. the longest stretch in which the window
            could not have repainted, scrolled or answered a click.

A page that takes three seconds to finish arriving is a slow page. A page
that holds the UI thread for three of those seconds is a frozen browser, and
those are not the same complaint. `stall` is the one the user filed.

Everything is served from a local HTTP server that sleeps a fixed DELAY
before answering, so the figures are about how many round trips are in
series, not about whose wifi ran it.
"""
import http.server
import os
import socketserver
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import media_fixtures  # noqa: E402
from feetbrowser import browser, window  # noqa: E402

# What one round trip to a real CDN costs, near enough. Small enough that the
# whole bench runs in a few seconds, big enough that serialising twenty of
# them is unmistakable.
DELAY = 0.05
SHEETS = 4
SCRIPTS = 16
VIDEOS = 6
# Big enough that opening the container and decoding frame zero is work worth
# measuring, small enough to build and serve in no time.
VIDEO_W, VIDEO_H, VIDEO_FRAMES = 320, 240, 12


def _clip():
    frames = [media_fixtures.rgb24_frame(
        VIDEO_W, VIDEO_H, lambda x, y: (x & 255, y & 255, i * 8))
        for i in range(VIDEO_FRAMES)]
    return media_fixtures.avi(frames, VIDEO_W, VIDEO_H, fps=12.0)


def _page(sheets, scripts, videos):
    parts = ["<html><head>"]
    parts += ['<link rel="stylesheet" href="/sheet%d.css">' % i
              for i in range(sheets)]
    parts += ['<script src="/script%d.js"></script>' % i
              for i in range(scripts)]
    parts.append("</head><body><h1>bench</h1>")
    parts += ['<video src="/clip%d.avi"></video>' % i for i in range(videos)]
    parts.append("</body></html>")
    return "".join(parts).encode("utf8")


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answers the four kinds of thing the page names, after DELAY."""

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802 - the name http.server dispatches to
        path = self.path
        if path.endswith(".css"):
            body, ctype = b"h1 { color: #123456 }", "text/css"
        elif path.endswith(".js"):
            body, ctype = b"var x = 1;", "text/javascript"
        elif path.endswith(".avi"):
            body, ctype = self.server.clip, "video/x-msvideo"
        else:
            body, ctype = self.server.page, "text/html"
        time.sleep(DELAY)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _measure(url):
    """Load `url` in a headless browser; return (settle seconds, worst stall).

    The stall is read off `flush_timers`, which is the only door the UI
    thread's work comes through while settle() is pumping: whatever one call
    to it takes is a span in which nothing else on that thread could run.
    """
    worst = [0.0]
    flush = window.Window.flush_timers

    def timed(self, deadline=None):
        start = time.perf_counter()
        try:
            return flush(self, deadline)
        finally:
            worst[0] = max(worst[0], time.perf_counter() - start)

    window.Window.flush_timers = timed
    try:
        tab = browser.Browser()
        tab.window.geometry("1000x700")
        tab.canvas.resize(1000, 700)
        tab._apply_resize()
        start = time.perf_counter()
        tab.new_tab(url)
        tab.settle(60.0)
        return time.perf_counter() - start, worst[0]
    finally:
        window.Window.flush_timers = flush


def main():
    clip = _clip()
    cases = (
        ("%d sheets + %d scripts" % (SHEETS, SCRIPTS), SHEETS, SCRIPTS, 0),
        ("%d videos" % VIDEOS, 0, 0, VIDEOS),
        ("all of it", SHEETS, SCRIPTS, VIDEOS),
    )
    print("%-28s %10s %10s" % ("case", "settle", "stall"))
    for label, sheets, scripts, videos in cases:
        server = _Server(("127.0.0.1", 0), _Handler)
        server.page = _page(sheets, scripts, videos)
        server.clip = clip
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/page.html" % server.server_address[1]
            settled, stall = _measure(url)
        finally:
            server.shutdown()
            server.server_close()
        print("%-28s %8.3f s %8.3f s" % (label, settled, stall))
    print("\nserved with a %.0f ms delay on every request; "
          "serial would be %.2f s of sheets+scripts alone"
          % (DELAY * 1000, DELAY * (SHEETS + SCRIPTS)))


if __name__ == "__main__":
    main()
