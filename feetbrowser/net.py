"""Networking and URL handling for FeetBrowser.

This is the "from scratch" transport layer: raw sockets speaking HTTP/1.1,
TLS for https, plus support for data:, file: and view-source: URLs.
Nothing here wraps an existing HTTP client engine beyond Python's socket/ssl.
"""

import base64
import codecs
import gzip
import os
import socket
import ssl
import time
import urllib.parse
import zlib

# A tiny in-process cache keyed by URL string. Honors a very small subset of
# Cache-Control (max-age). Good enough to avoid re-fetching stylesheets.
_CACHE = {}
CACHE_MAX_SIZE = 1000

DEFAULT_HEADERS = {
    "User-Agent": "FeetBrowser/0.1.1 (https://github.com/JuiceyDew/FeetBrowser)",
    "Accept": "text/html,application/xhtml+xml,text/css,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

MAX_REDIRECTS = 10

# Schemes the URL parser understands (used to distinguish a bare host from a
# scheme-less string like "example.com:8080").
KNOWN_SCHEMES = {"http", "https", "file", "data"}


class URL:
    """A parsed URL plus the logic to fetch it."""

    def __init__(self, url):
        self.raw = url
        self.view_source = False
        self.fragment = ""
        self.host = ""
        self.port = None
        self.path = ""

        if url.startswith("view-source:"):
            self.view_source = True
            url = url[len("view-source:"):]
            self.raw = url

        # Split scheme. Anything without a known scheme is treated as a bare
        # host/path (with optional port) and assumed to be https.
        if "://" not in url:
            head = url.split(":", 1)[0].lower()
            if head not in KNOWN_SCHEMES:
                url = "https://" + url

        self.scheme, rest = url.split(":", 1)
        self.scheme = self.scheme.lower()

        if self.scheme in ("http", "https"):
            self._parse_http(rest)
        elif self.scheme == "file":
            # file:///path, file://host/path (host ignored) or file:/path
            if rest.startswith("//"):
                remainder = rest[2:]
                slash = remainder.find("/")
                self.path = remainder[slash:] if slash != -1 else "/"
            else:
                self.path = rest or "/"
        elif self.scheme == "data":
            self.data_payload = rest
        else:
            # Unknown scheme: parse it anyway so extensions can intercept it
            # through the toes handle hook. Fetching it still fails loudly.
            if rest.startswith("//"):
                self._parse_http(rest)
            else:
                self.host = ""
                self.path = rest or "/"
                self.port = None

    def _parse_http(self, rest):
        # rest looks like //host[:port]/path?query#frag
        if rest.startswith("//"):
            rest = rest[2:]
        # Strip fragment (self.fragment is already "" by default).
        if "#" in rest:
            rest, self.fragment = rest.split("#", 1)
        if "/" in rest:
            authority, path = rest.split("/", 1)
            self.path = "/" + path
        else:
            authority, self.path = rest, "/"
        if "@" in authority:
            authority = authority.rsplit("@", 1)[1]  # drop userinfo
        # IPv6 literal (optionally with port): [::1]:8080
        if authority.startswith("["):
            if "]" in authority:
                host, _, port_part = authority.partition("]")
                self.host = host[1:]
                port = port_part[1:] if port_part.startswith(":") else None
            else:
                raise ValueError(f"Malformed IPv6 address in URL: {authority!r}")
        elif ":" in authority:
            self.host, port = authority.rsplit(":", 1)
        else:
            self.host, port = authority, None

        if not self.host:
            raise ValueError(f"Missing host in URL: {authority!r}")
        self.host = self.host.lower()
        if port is None or port == "":
            self.port = 443 if self.scheme == "https" else 80
        else:
            try:
                self.port = int(port)
            except ValueError:
                raise ValueError(f"Invalid port in URL: {port!r}")
        if not (0 < self.port <= 65535):
            raise ValueError(f"Port out of range in URL: {self.port}")

    # -- URL resolution --------------------------------------------------

    def resolve(self, url):
        """Resolve a possibly-relative URL against this one."""
        if "://" in url or url.startswith(("data:", "view-source:")):
            return URL(url)
        if url.startswith("//"):
            return URL(self.scheme + ":" + url)
        if url.startswith("#"):
            new = URL(str(self))
            new.fragment = url[1:]
            return new
        if not url.startswith("/"):
            # Relative to current directory.
            dir_path = self.path.rpartition("/")[0] + "/"
            url = dir_path + url
        # Normalize ../ and ./
        parts = []
        for seg in url.split("/"):
            if seg == ".." and parts:
                parts.pop()
            elif seg not in ("", ".", ".."):
                parts.append(seg)
        new_url = URL(f"{self.scheme}://{self.netloc()}{'/' + '/'.join(parts)}")
        new_url.view_source = self.view_source
        return new_url

    def netloc(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if (self.port in (80, 443, None)) else f":{self.port}"
        return f"{host}{port}"

    def __str__(self):
        prefix = "view-source:" if self.view_source else ""
        if self.scheme in ("http", "https"):
            frag = f"#{self.fragment}" if getattr(self, "fragment", "") else ""
            return f"{prefix}{self.scheme}://{self.netloc()}{self.path}{frag}"
        if self.scheme == "file":
            return f"{prefix}file://{self.path}"
        if self.scheme == "data":
            return f"{prefix}data:{self.data_payload}"
        return self.raw

    # -- Fetching --------------------------------------------------------

    def request(self, redirects_left=MAX_REDIRECTS, payload=None, raw=False):
        """Return (headers_dict, body, content_type).

        `raw=True` skips text decoding and returns the body as bytes (used by
        image fetches). The flag is threaded through redirects so an image
        served from a redirect location still comes back undecoded.
        """
        if self.scheme == "file":
            return self._request_file(raw)
        if self.scheme == "data":
            return self._request_data(raw)
        return self._request_http(redirects_left, payload, raw)

    def request_bytes(self, redirects_left=MAX_REDIRECTS):
        """Return (headers_dict, body_bytes, content_type) for binary data
        (images), skipping the text decoding that request() applies."""
        return self.request(redirects_left, raw=True)

    def _request_file(self, raw=False):
        try:
            with open(self.path, "rb") as f:
                payload = f.read()
        except (FileNotFoundError, IsADirectoryError, PermissionError) as e:
            if os.path.isdir(self.path):
                items = sorted(os.listdir(self.path))
                links = "".join(
                    f'<li><a href="file://{urllib.parse.quote(os.path.join(self.path, i))}">{i}</a></li>'
                    for i in items)
                body = f"<h1>Index of {self.path}</h1><ul>{links}</ul>"
            else:
                body = f"<h1>Cannot open file</h1><p>{e}</p>"
            return {}, body.encode("utf8", "replace") if raw else body, \
                "text/html"
        ext = os.path.splitext(self.path)[1].lower()
        ctype = "text/html" if ext in (".html", ".htm") else "text/plain"
        return {}, payload if raw else payload.decode("utf8", "replace"), ctype

    def _request_data(self, raw=False):
        # data:[<mediatype>][;base64],<data>
        meta, _, data = self.data_payload.partition(",")
        ctype = meta.split(";")[0] or "text/plain"
        if meta.endswith(";base64"):
            decoded = base64.b64decode(data)
        else:
            decoded = urllib.parse.unquote(data)
        if isinstance(decoded, bytes):
            body = decoded if raw else decoded.decode("utf8", "replace")
        else:
            body = decoded.encode("utf8", "replace") if raw else decoded
        return {}, body, ctype

    def _request_http(self, redirects_left, payload, raw=False):
        # Two documents that differ only by fragment are the same resource.
        cache_key = str(self).split("#", 1)[0]
        if payload is None and cache_key in _CACHE:
            expires, entry = _CACHE[cache_key]
            if expires is None or expires > time.time():
                return entry
            else:
                del _CACHE[cache_key]

        # create_connection handles both IPv4 and IPv6 hosts.
        s = socket.create_connection((self.host, self.port), timeout=20)
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            s = ctx.wrap_socket(s, server_hostname=self.host)
        # Guard against a server that streams forever / stalls: bound each
        # recv() call so a dead or hostile peer can't hang the UI.
        s.settimeout(30)

        method = "POST" if payload is not None else "GET"
        headers = dict(DEFAULT_HEADERS)
        headers["Host"] = self.netloc()  # brackets IPv6, includes the port
        headers["Connection"] = "close"
        body_bytes = b""
        if payload is not None:
            body_bytes = payload.encode("utf8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Content-Length"] = str(len(body_bytes))

        lines = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
        req = f"{method} {self.path} HTTP/1.1\r\n{lines}\r\n\r\n"
        try:
            s.sendall(req.encode("utf8") + body_bytes)
            status, resp_headers, body = self._read_response(s)
        finally:
            s.close()

        # Redirects.
        if status in (301, 302, 303, 307, 308) and "location" in resp_headers:
            if redirects_left <= 0:
                raise RuntimeError("Too many redirects")
            location = resp_headers["location"]
            new_url = self.resolve(location)
            follow_payload = payload if status in (307, 308) else None
            return new_url.request(redirects_left - 1, follow_payload, raw=raw)

        # Decode transfer-encoding and content-encoding.
        body = self._decode_body(body, resp_headers)
        charset = self._charset(resp_headers)
        text = body.decode(charset, "replace")
        content_type = resp_headers.get(
            "content-type", "text/html").split(";")[0].strip()

        if raw:
            return (resp_headers, body, content_type)

        result = (resp_headers, text, content_type)

        # Cache if allowed.
        cc = resp_headers.get("cache-control", "")
        if payload is None and status == 200 and "no-store" not in cc:
            expires = None
            for part in cc.split(","):
                part = part.strip()
                if part.startswith("max-age="):
                    try:
                        expires = time.time() + int(part.split("=", 1)[1])
                    except ValueError:
                        expires = None
            if "max-age" in cc and len(_CACHE) >= CACHE_MAX_SIZE:
                # Evict expired entries first, then the oldest live one.
                now = time.time()
                for k in [k for k, (exp, _) in _CACHE.items()
                          if exp is not None and exp <= now]:
                    del _CACHE[k]
                if len(_CACHE) >= CACHE_MAX_SIZE and _CACHE:
                    del _CACHE[min(_CACHE, key=lambda k: _CACHE[k][0] or 0)]
            if "max-age" in cc:
                _CACHE[cache_key] = (expires, result)

        return result

    @staticmethod
    def _read_response(s):
        """Read a full HTTP response, honoring Content-Length / chunked
        framing so a keep-alive peer that does not close the socket doesn't
        make us wait for EOF (which could stall for the socket timeout).

        Returns (status, headers, body_bytes)."""
        buf = bytearray()

        def read_more(n=65536):
            chunk = s.recv(n)
            if chunk:
                buf.extend(chunk)
            return chunk

        # Read the header block (terminated by an empty line).
        while b"\r\n\r\n" not in buf and read_more():
            pass
        head, _, body = bytes(buf).partition(b"\r\n\r\n")
        headers = URL._parse_headers(head)

        if headers.get("transfer-encoding", "").lower() == "chunked":
            # Read until the terminating zero-length chunk.
            while b"\r\n0\r\n\r\n" not in buf and read_more():
                pass
        elif "content-length" in headers:
            try:
                remaining = int(headers["content-length"]) - len(body)
            except ValueError:
                remaining = -1
            while remaining > 0:
                chunk = read_more(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        else:
            # No framing hints: read until EOF (socket timeout guards stalls).
            while read_more():
                pass

        head, _, body = bytes(buf).partition(b"\r\n\r\n")
        # Status line is: HTTP/x.y <code> <explanation>
        status_line = head.split(b"\r\n")[0].decode("latin1")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise RuntimeError(f"Malformed status line: {status_line!r}")
        return int(parts[1]), headers, body

    @staticmethod
    def _parse_headers(head):
        headers = {}
        for line in head.split(b"\r\n")[1:]:
            if not line:
                continue
            k, _, v = line.decode("latin1").partition(":")
            headers[k.strip().lower()] = v.strip()
        return headers

    @staticmethod
    def _decode_body(body, headers):
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body = URL._dechunk(body)
        enc = headers.get("content-encoding", "").lower()
        if enc == "gzip":
            try:
                body = gzip.decompress(body)
            except (OSError, EOFError):
                pass
        elif enc == "deflate":
            try:
                body = zlib.decompress(body)
            except zlib.error:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
        return body

    @staticmethod
    def _dechunk(body):
        out = bytearray()
        while body:
            size_line, _, body = body.partition(b"\r\n")
            try:
                size = int(size_line.strip().split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                break
            if len(body) < size + 2:
                break  # truncated chunk
            out += body[:size]
            body = body[size + 2:]  # skip trailing CRLF
        return bytes(out)

    @staticmethod
    def _charset(headers):
        ctype = headers.get("content-type", "")
        if "charset=" in ctype:
            value = ctype.split("charset=", 1)[1].split(";")[0].strip()
            value = value.strip("\"'")  # servers send charset="utf-8" too
            try:
                codecs.lookup(value)
                return value
            except LookupError:
                pass
        return "utf8"
