"""Serve tests/fixtures/ over HTTP on loopback.

The end-to-end tests want the whole pipeline -- URL parsing, a socket, HTTP
framing, the parser, the cascade, layout, the rasteriser -- not a shortcut
into the middle of it. They also want to run on a pull request, where a real
site being slow, moved or simply not interested in our traffic is noise.
So the pages come from this directory instead of the web, over a real
connection to a real server on 127.0.0.1.

`tests/fixtures/example.com/` and `tests/fixtures/iana.org/` are offline
stand-ins for the two pages the navigation suite has always used, laid out
under the host names they replace so a URL built from them still reads as
the site it came from.

Every page in there is a file with an extension rather than a directory,
which is not tidiness: URL.resolve() drops the trailing slash off a
directory URL, so a link to one redirects to itself until the redirect limit
gives up. Fixtures are for exercising the browser, not for tripping over
that, so they sidestep it.

Run it as a program to hand the fixtures to another script: any `{base}` in
the arguments becomes the server's URL.

    python tests/fixture_server.py tests/smoke.py '{base}example.com/'
"""
import functools
import http.server
import os
import threading

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    """A request log per image would bury the output of every test."""

    def log_message(self, *args):
        pass


class FixtureServer:
    """A server on a free port, started on enter and stopped on exit."""

    def __init__(self, directory=FIXTURES):
        handler = functools.partial(_Quiet, directory=directory)
        self._server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.base = "http://127.0.0.1:%d/" % self._server.server_address[1]

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever,
                         daemon=True).start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()

    def url(self, path):
        return self.base + path.lstrip("/")


def main():
    import subprocess
    import sys

    if len(sys.argv) < 2:
        sys.exit("usage: python tests/fixture_server.py <script> [args...]")
    with FixtureServer() as fixtures:
        argv = [a.replace("{base}", fixtures.base) for a in sys.argv[1:]]
        sys.exit(subprocess.call([sys.executable] + argv))


if __name__ == "__main__":
    main()
