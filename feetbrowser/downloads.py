"""Downloads: a file off the network and onto the disk, honestly.

A page is read into memory because the parser needs all of it at once. A
file is not: it can be larger than the machine's RAM, and the only thing
that ever wants all of it is the disk. So a download here is a loop over
`net.open_stream` chunks writing straight through to a `.part` file, with a
fixed-size buffer between the socket and the filesystem no matter whether
the file is 12 bytes or 12 gigabytes. The finished file only appears under
its real name once the last byte is in (os.replace, atomic), so an
interrupted download can never be mistaken for a complete one.

Three things in here are less obvious than the transfer itself:

Filenames. The name comes from the server -- Content-Disposition, or the
last path segment -- which makes it hostile input pointed straight at the
filesystem. `sanitize_filename` is a security boundary: nothing a server
sends may escape the download directory, overwrite something outside it, or
collide with a name Windows reserves for a device. Everything that reaches
the filesystem goes through it.

Progress. Content-Length is optional, and a chunked response genuinely has
no total. Where that happens the percentage is None rather than a number
made up on the spot -- callers show bytes and a rate instead of a lie.

Locking. Workers hold `Download._lock` for the microseconds it takes to
update a counter and never across a recv() or a write(): one slow server
must not be able to freeze the UI thread that is reading these fields sixty
times a second.
"""

import errno
import os
import re
import sys
import threading
import time
import urllib.parse
from collections import deque

from .net import URL, IncompleteRead, open_stream

#: Bytes moved between the socket and the file per iteration. This, times
#: the number of concurrent downloads, is the whole memory cost of a
#: transfer -- the file size does not enter into it.
CHUNK = 256 * 1024

#: Downloads running at once. The rest queue up behind the semaphore.
MAX_CONCURRENT_DOWNLOADS = 4

#: How long a connection may stall before the transfer is called dead.
TIMEOUT = 30

#: How many times a broken transfer is resumed with a Range request before
#: it is reported as failed.
RESUME_ATTEMPTS = 3

#: Window (seconds) the transfer rate is averaged over. Short enough to
#: react, long enough not to flicker.
RATE_WINDOW = 3.0

#: Longest filename we will write, in characters. Comfortably under the 255
#: bytes that ext4/APFS/NTFS allow for a single component even after UTF-8
#: expansion, and the extension is preserved when trimming.
MAX_NAME = 120

# Download states.
PENDING = "pending"
ACTIVE = "downloading"
COMPLETE = "complete"
FAILED = "failed"
CANCELLED = "cancelled"

FINISHED_STATES = (COMPLETE, FAILED, CANCELLED)

#: Content types this browser can put on screen. Anything else is a file,
#: not a page, and gets downloaded instead of rendered as mojibake.
DISPLAYABLE_TYPES = {
    "application/xhtml+xml", "application/xml", "application/json",
    "application/javascript", "application/x-javascript", "application/rss+xml",
    "application/atom+xml", "image/svg+xml",
}
DISPLAYABLE_PREFIXES = ("text/", "image/")

#: Names Windows resolves to devices rather than files, in any directory and
#: with any extension. Writing "CON" there talks to the console; writing
#: "LPT1" talks to a port. We are not always on Windows, but the file we
#: write may be read on one, so the name is made safe wherever it is made.
_WINDOWS_DEVICES = frozenset(
    ["con", "prn", "aux", "nul", "com0", "lpt0"]
    + ["com%d" % i for i in range(1, 10)]
    + ["lpt%d" % i for i in range(1, 10)])

#: Everything below 0x20 plus DEL, and the characters Windows forbids in a
#: name. NUL matters most: it truncates the name inside any C library the
#: path is later handed to, so "safe.txt\x00.exe" is two different names
#: depending on who is looking.
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"|?*\\]')

_FILENAME_STAR = re.compile(r"""filename\*\s*=\s*([^;]+)""", re.I)
_FILENAME_PLAIN = re.compile(
    r"""filename\s*=\s*("(?:[^"\\]|\\.)*"|[^;]*)""", re.I)


# -- the download directory ---------------------------------------------

def default_download_dir():
    """Where downloads go on this machine, created if it is missing.

    FEETBROWSER_DOWNLOAD_DIR wins when set (the tests use it, and so does
    anyone who keeps downloads off the home volume). Otherwise it is the
    platform's own answer: the XDG user directory on Linux when the desktop
    configured one, the shell's known folder on Windows, and ~/Downloads
    everywhere else -- which is also where both of the others fall back to,
    since that is the name every desktop starts with.
    """
    override = os.environ.get("FEETBROWSER_DOWNLOAD_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    home = os.path.expanduser("~")
    if sys.platform.startswith("linux"):
        configured = _xdg_download_dir(home)
        if configured:
            return configured
    elif os.name == "nt":
        configured = _windows_download_dir()
        if configured:
            return configured
    return os.path.join(home, "Downloads")


def _windows_download_dir():
    """FOLDERID_Downloads out of the shell, or None if it will not say.

    A Windows user can move Downloads anywhere, and only the shell knows
    where it went -- %USERPROFILE%\\Downloads is where it started, not
    where it is. SHGetKnownFolderPath is the one call that answers, so ask
    it through ctypes like the rest of the Win32 code does.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, ValueError):     # pragma: no cover - not Windows
        return None
    # {374DE290-123F-4565-9164-39C4925E467B}, laid out as a GUID struct.
    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    folder = _GUID(0x374DE290, 0x123F, 0x4565,
                   (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4,
                                        0x92, 0x5E, 0x46, 0x7B))
    out = ctypes.c_wchar_p()
    try:
        shell32 = ctypes.WinDLL("shell32.dll")
        ole32 = ctypes.WinDLL("ole32.dll")
        status = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder), 0, None, ctypes.byref(out))
    except (AttributeError, OSError):     # pragma: no cover - not Windows
        return None
    if status != 0 or not out.value:      # pragma: no cover - not Windows
        return None
    path = out.value
    ole32.CoTaskMemFree(out)
    return os.path.abspath(path)


def _xdg_download_dir(home):
    """XDG_DOWNLOAD_DIR out of the environment or ~/.config/user-dirs.dirs."""
    value = os.environ.get("XDG_DOWNLOAD_DIR")
    if not value:
        config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home,
                                                                   ".config")
        path = os.path.join(config, "user-dirs.dirs")
        try:
            with open(path, encoding="utf8") as handle:
                for line in handle:
                    line = line.strip()
                    if line.startswith("XDG_DOWNLOAD_DIR"):
                        value = line.split("=", 1)[1].strip().strip('"')
                        break
        except OSError:
            return None
    if not value:
        return None
    value = value.replace("$HOME", home)
    return os.path.abspath(os.path.expanduser(value))


def ensure_dir(path):
    """Create `path` (and its parents) if it is not there already."""
    os.makedirs(path, exist_ok=True)
    return path


# -- filenames ----------------------------------------------------------

def sanitize_filename(name, fallback="download"):
    """Reduce a server-supplied name to one safe component.

    The rules, and what each of them is for:

    * Everything up to the last "/" or "\\" is discarded, so "../../etc/
      passwd" is "passwd" and "/etc/passwd" is "passwd". A traversal cannot
      survive keeping only the final component.
    * A drive prefix ("C:", "\\\\server\\share") goes with it, both because
      of the separators and because the colon is stripped as unsafe.
    * NUL and the other control characters are removed. NUL is the sharp
      one: it ends the string for any C library the path reaches, so a name
      that reads "safe.txt" to Python and "safe.txt" to the kernel and
      something else to a viewer is not allowed to exist.
    * Leading dots are dropped, so a server cannot write a dotfile, and a
      name that is nothing but dots ("." or "..") is refused outright.
    * A Windows device name (CON, NUL, COM3, LPT9 ... with or without an
      extension) is prefixed with an underscore. On Windows the bare name
      is not a file at all.
    * The result is trimmed to MAX_NAME characters with its extension kept,
      and an empty result becomes `fallback`.

    The return value is always a single path component: never absolute,
    never containing a separator, never "." or "..".
    """
    if not isinstance(name, str):
        name = ""
    # Percent-escapes first: "%2e%2e%2ffoo" is a traversal in disguise, and
    # unquoting after the separator check would let it through.
    if "%" in name:
        name = urllib.parse.unquote(name)
    name = name.replace("\\", "/")
    name = name.split("/")[-1]
    name = _UNSAFE_CHARS.sub("", name)
    name = name.strip().strip(".").strip()
    if not name or name in (".", ".."):
        return fallback
    stem, ext = os.path.splitext(name)
    # Windows resolves the device before the extension, and before any
    # extension: NUL.tar.gz is NUL. Compare the first dot-segment, not the
    # stem, or "CON.txt" is caught and "CON.tar.gz" is not.
    if name.split(".")[0].strip().lower() in _WINDOWS_DEVICES:
        name = "_" + name
        stem = "_" + stem
    if len(name) > MAX_NAME:
        ext = ext[:16]
        name = stem[:MAX_NAME - len(ext)] + ext
    # A last belt-and-braces pass: whatever the rules above did, the result
    # has to be one relative component.
    name = os.path.basename(name)
    if not name or name in (".", ".."):
        return fallback
    return name


def parse_content_disposition(value):
    """Return (type, filename or None) for a Content-Disposition header."""
    if not value:
        return "", None
    kind = value.split(";", 1)[0].strip().lower()
    filename = None
    star = _FILENAME_STAR.search(value)
    if star:
        # RFC 5987: charset'language'percent-encoded-value
        raw = star.group(1).strip()
        parts = raw.split("'", 2)
        encoded = parts[2] if len(parts) == 3 else raw
        charset = parts[0] or "utf8" if len(parts) == 3 else "utf8"
        try:
            filename = urllib.parse.unquote(encoded, encoding=charset,
                                            errors="replace")
        except LookupError:
            filename = urllib.parse.unquote(encoded, errors="replace")
    if filename is None:
        plain = _FILENAME_PLAIN.search(value)
        if plain:
            filename = plain.group(1).strip()
            if filename.startswith('"') and filename.endswith('"'):
                filename = filename[1:-1].replace('\\"', '"')
    return kind, (filename or None)


def filename_for(url, headers=None):
    """The name to save `url` under: header, then URL, then "download".

    Content-Disposition is what the server explicitly asked for and wins.
    Failing that it is the last non-empty segment of the URL path, with its
    query string dropped and its percent-escapes undone. Failing that -- a
    URL that is nothing but a host -- it is "download". Every branch ends in
    sanitize_filename, so no path this function has ever produces a name
    that leaves the download directory.
    """
    headers = headers or {}
    _kind, suggested = parse_content_disposition(
        headers.get("content-disposition", ""))
    if suggested:
        return sanitize_filename(suggested)
    path = getattr(url, "path", "") or ""
    path = path.split("?", 1)[0].split("#", 1)[0]
    segment = ""
    for part in reversed(path.split("/")):
        if part:
            segment = part
            break
    if segment:
        return sanitize_filename(urllib.parse.unquote(segment))
    host = (getattr(url, "host", "") or "").replace(":", "_")
    return sanitize_filename(host or "download")


def _name_variants(name):
    """Yield "file.txt", "file (1).txt", "file (2).txt", ... forever."""
    stem, ext = os.path.splitext(name)
    yield name
    count = 1
    while True:
        yield "%s (%d)%s" % (stem, count, ext)
        count += 1


def unique_path(directory, name):
    """A path in `directory` for `name` that nothing is using yet.

    Advisory only -- two processes racing can still pick the same answer,
    which is why the file itself is created with O_EXCL below.
    """
    for candidate in _name_variants(sanitize_filename(name)):
        path = os.path.join(directory, candidate)
        if not os.path.exists(path):
            return path
    raise RuntimeError("unreachable")  # pragma: no cover


def _create_exclusive(directory, name, suffix=""):
    """Create `directory/name+suffix`, uniquifying until one is free.

    Returns (fd, path). O_EXCL is what makes two downloads of the same file
    land on two different names instead of one truncated mess: the name is
    taken by the creation itself, not by a check that something else can
    win between.

    O_BINARY where it exists, which is Windows: without it the CRT rewrites
    every 0x0A on the way out and a downloaded PNG arrives corrupt.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    last = None
    for candidate in _name_variants(sanitize_filename(name)):
        path = os.path.join(directory, candidate + suffix)
        try:
            fd = os.open(path, flags, 0o644)
            return fd, path
        except FileExistsError:
            continue
        except OSError as exc:  # no directory, no permission, read-only fs
            last = exc
            break
    raise last or OSError(errno.EEXIST, "no free filename in %s" % directory)


def is_displayable(content_type):
    """True when the renderer can put this content type on screen."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if not ctype:
        return True  # no opinion from the server: try to render it
    if ctype in DISPLAYABLE_TYPES:
        return True
    return ctype.startswith(DISPLAYABLE_PREFIXES)


def should_download(headers, content_type=None):
    """Is this response a file to save rather than a page to show?

    Two signals, in the order browsers weigh them: an explicit
    `Content-Disposition: attachment`, and a content type nothing here can
    render. A 4xx/5xx body is a page (an error page) whatever it claims.
    """
    headers = headers or {}
    kind, _name = parse_content_disposition(
        headers.get("content-disposition", ""))
    if kind == "attachment":
        return True
    if content_type is None:
        content_type = headers.get("content-type", "")
    return not is_displayable(content_type)


# -- formatting ---------------------------------------------------------

def format_size(count):
    """Bytes as something a person reads, or "?" for an unknown total."""
    if count is None:
        return "?"
    step = 1024.0
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            if unit == "B":
                return "%d B" % int(value)
            return "%.1f %s" % (value, unit)
        value /= step
    return "%.1f TB" % value  # pragma: no cover


def format_rate(rate):
    return "--" if not rate else "%s/s" % format_size(int(rate))


def format_eta(seconds):
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return "%ds left" % seconds
    if seconds < 3600:
        return "%dm %02ds left" % (seconds // 60, seconds % 60)
    return "%dh %02dm left" % (seconds // 3600, (seconds % 3600) // 60)


# -- one download -------------------------------------------------------

class Download:
    """One transfer: where it came from, where it is going, how far it got.

    Written by a worker thread, read by the UI thread. Every field a reader
    cares about is behind `_lock`, and the lock is never held across the
    socket read or the disk write -- those are the two operations that can
    block for seconds, and a lock held across either is a browser that
    freezes because one server is slow.
    """

    def __init__(self, url, directory, filename=None, manager=None):
        self.url = url
        self.directory = directory
        # A name the caller insisted on, if any. It outranks anything the
        # server suggests; when it is None the response headers decide, and
        # they only arrive once the transfer has started.
        self.explicit_name = filename
        self.filename = filename or "download"
        self.path = None            # final path, set when the file lands
        self.part_path = None       # the .part file while it is in flight
        self.state = PENDING
        self.error = None
        self.total = None           # bytes expected, or None if unknown
        self.received = 0
        self.started = time.time()
        self.ended = None
        self.resumed = 0            # how many Range restarts it took
        self._samples = deque()     # (monotonic, received) for the rate
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._stream = None
        self._manager = manager

    # -- state ----------------------------------------------------------

    def _touch(self):
        if self._manager is not None:
            self._manager.note_change()

    def set_state(self, state, error=None):
        with self._lock:
            self.state = state
            if error is not None:
                self.error = error
            if state in FINISHED_STATES and self.ended is None:
                self.ended = time.time()
        self._touch()

    def begin(self, total, filename, part_path):
        with self._lock:
            self.state = ACTIVE
            self.total = total
            self.filename = filename
            self.part_path = part_path
            self.started = time.time()
            self._samples.clear()
            self._samples.append((time.monotonic(), self.received))
        self._touch()

    def advance(self, count):
        now = time.monotonic()
        with self._lock:
            self.received += count
            self._samples.append((now, self.received))
            while len(self._samples) > 2 \
                    and now - self._samples[0][0] > RATE_WINDOW:
                self._samples.popleft()

    def rewind(self, received):
        """A resume that had to start over from zero."""
        with self._lock:
            self.received = received
            self._samples.clear()
            self._samples.append((time.monotonic(), received))

    def finish(self, path):
        with self._lock:
            self.state = COMPLETE
            self.path = path
            self.filename = os.path.basename(path)
            self.part_path = None
            self.ended = time.time()
        self._touch()

    # -- reading (UI thread) --------------------------------------------

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def cancel(self):
        """Ask the worker to stop, and unblock it if it is in a recv().

        Setting the flag is enough for a transfer that is moving; a transfer
        parked on a silent server would otherwise sit there until the socket
        timeout, so the socket is shut down too. Nothing here waits for the
        worker: cancel returns immediately and the state flips to CANCELLED
        when the worker notices, which is the next chunk boundary.
        """
        if self.state in FINISHED_STATES:
            return False
        self._cancel.set()
        stream = self._stream
        if stream is not None:
            stream.shutdown()
        self._touch()
        return True

    def is_active(self):
        return self.state in (PENDING, ACTIVE)

    def percent(self):
        """Fraction complete in 0..1, or None when the total is unknown.

        None is the honest answer for a chunked response, and callers are
        expected to render it as such rather than as 0%.
        """
        with self._lock:
            if not self.total:
                return None
            return min(1.0, self.received / float(self.total))

    def rate(self):
        """Bytes per second over the last RATE_WINDOW seconds, or 0."""
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
            # Two samples microseconds apart divide a real number of bytes
            # by nearly nothing and report gigabytes per second. Until the
            # window is wide enough to mean something, there is no rate yet.
            if t1 - t0 < 0.05:
                return 0.0
            return max(0.0, (b1 - b0) / (t1 - t0))

    def eta(self):
        """Seconds remaining, or None when that cannot be known."""
        rate = self.rate()
        with self._lock:
            if not self.total or not rate:
                return None
            remaining = max(0, self.total - self.received)
        return remaining / rate

    def describe(self):
        """One line of status for the manager UI."""
        with self._lock:
            state, received, total = self.state, self.received, self.total
            error = self.error
        if state == FAILED:
            return "Failed — %s" % (error or "unknown error")
        if state == CANCELLED:
            return "Cancelled — %s received" % format_size(received)
        if state == COMPLETE:
            return "Complete — %s" % format_size(received)
        if state == PENDING:
            return "Waiting…"
        got = format_size(received)
        rate = format_rate(self.rate())
        if total:
            eta = format_eta(self.eta())
            tail = ", %s" % eta if eta else ""
            return "%s of %s (%s%s)" % (got, format_size(total), rate, tail)
        # No Content-Length: report what has arrived and how fast, and do
        # not pretend to know the share of a total nobody stated.
        return "%s of unknown size (%s)" % (got, rate)


# -- the manager --------------------------------------------------------

class DownloadManager:
    """Every download this session, and the threads moving them.

    The manager's own lock covers the list of downloads and the change flag
    and nothing else; transfers happen outside it. A worker takes the
    semaphore, opens its own connection, and writes its own file, so a stuck
    download costs one thread and one socket and blocks nothing.
    """

    def __init__(self, directory=None, max_concurrent=MAX_CONCURRENT_DOWNLOADS):
        self._directory = directory
        self._downloads = []
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(max_concurrent)
        self._changed = True
        self._announce = False

    # -- where files go --------------------------------------------------

    def directory(self):
        """The download directory, created on first use.

        Resolved lazily so importing the module never touches the disk, and
        re-read each time so FEETBROWSER_DOWNLOAD_DIR can move it.
        """
        return ensure_dir(self._directory or default_download_dir())

    # -- the list --------------------------------------------------------

    def items(self):
        """Newest first, which is the order a manager panel wants them."""
        with self._lock:
            return list(reversed(self._downloads))

    def active(self):
        with self._lock:
            return [d for d in self._downloads if d.is_active()]

    def note_change(self):
        with self._lock:
            self._changed = True

    def take_changed(self):
        """True once after anything moved. The UI polls this to repaint."""
        with self._lock:
            changed, self._changed = self._changed, False
            return changed

    def take_announcement(self):
        """True once after a download starts, so the UI can reveal itself."""
        with self._lock:
            new, self._announce = self._announce, False
            return new

    def clear_finished(self):
        with self._lock:
            self._downloads = [d for d in self._downloads if d.is_active()]
            self._changed = True

    def cancel_all(self):
        for download in self.active():
            download.cancel()

    # -- starting one ----------------------------------------------------

    def start(self, url, filename=None, stream=None, threaded=True):
        """Begin a download and return its Download record immediately.

        `stream` is for the navigation that turned out to be a file: the
        response is already open and its first bytes may already be in
        hand, so it is handed over rather than re-requested.
        """
        if isinstance(url, str):
            url = URL(url)
        download = Download(url, self._directory, filename=filename,
                            manager=self)
        download.filename = filename or filename_for(
            url, stream.headers if stream is not None else None)
        with self._lock:
            self._downloads.append(download)
            self._changed = True
            self._announce = True
        if not threaded:
            self._run(download, stream)
            return download
        threading.Thread(target=self._run, args=(download, stream),
                         name="download", daemon=True).start()
        return download

    # -- the worker ------------------------------------------------------

    def _run(self, download, stream=None):
        """Worker thread: the whole life of one transfer.

        Every failure mode ends the same way -- a state on the Download and
        a message a person can read -- because a download that vanishes with
        a traceback on stderr is indistinguishable from one that never
        started.
        """
        try:
            with self._semaphore:
                if download.cancelled:
                    download.set_state(CANCELLED)
                    return
                self._transfer(download, stream)
        except _Cancelled:
            download.set_state(CANCELLED)
        except IncompleteRead as exc:
            self._ended(download, "connection lost: %s" % exc)
        except OSError as exc:
            # Cancelling shuts the socket down under a blocked read, and what
            # that raises is the platform's business: b"" here, ECONNABORTED
            # there. A download the user stopped is cancelled either way.
            self._ended(download, _describe_os_error(exc))
        except Exception as exc:  # noqa: BLE001 - a failed download, not a crash
            self._ended(download, "%s: %s" % (type(exc).__name__, exc))
        finally:
            if stream is not None:
                stream.close()

    @staticmethod
    def _ended(download, message):
        """A transfer that stopped early: cancelled if the user asked for
        that, failed with a reason if it stopped on its own."""
        if download.cancelled:
            download.set_state(CANCELLED)
        else:
            download.set_state(FAILED, message)

    def _transfer(self, download, stream):
        directory = self.directory()
        download.directory = directory
        if stream is None:
            stream = open_stream(download.url, extra_headers={"Accept": "*/*"},
                                 timeout=TIMEOUT)
        download._stream = stream
        try:
            if stream.status >= 400:
                raise DownloadError("server said HTTP %d" % stream.status)
            # Now that the headers are in, the server's own suggestion wins
            # over the guess made from the URL when the download was queued.
            name = download.explicit_name or filename_for(stream.url,
                                                          stream.headers)
            fd, part_path = _create_exclusive(directory, name, suffix=".part")
            download.begin(stream.length, os.path.basename(part_path)[:-5],
                           part_path)
            handle = os.fdopen(fd, "wb")
            try:
                # _pump may replace the connection (a resume opens a new
                # one); the stream it hands back is the one to close.
                stream = self._pump(download, stream, handle)
                handle.flush()
            finally:
                handle.close()
            if download.cancelled:
                raise _Cancelled()
            fd, final = _create_exclusive(directory, download.filename)
            os.close(fd)
            os.replace(part_path, final)
            download.finish(final)
        except BaseException:
            # Nothing half-written is left behind wearing a real name: the
            # .part file is the only thing that ever held the bytes, and it
            # goes with the failure.
            part = download.part_path
            if part:
                try:
                    os.unlink(part)
                except OSError:
                    pass
            raise
        finally:
            download._stream = None
            stream.close()

    def _pump(self, download, stream, handle):
        """Socket to disk, with a Range retry when the connection breaks.

        Returns the stream it finished on, which is not necessarily the one
        it started with -- a resume opens a fresh connection, and the caller
        is the one that closes it.
        """
        attempts = 0
        while True:
            try:
                for piece in stream.chunks(CHUNK):
                    if download.cancelled:
                        raise _Cancelled()
                    handle.write(piece)
                    download.advance(len(piece))
                return stream
            except IncompleteRead:
                if download.cancelled:
                    raise _Cancelled()
                attempts += 1
                if attempts > RESUME_ATTEMPTS:
                    raise
                resumed = self._resume(download, stream, handle)
                if resumed is None:
                    raise
                stream = resumed

    def _resume(self, download, stream, handle):
        """Reopen the transfer with a Range request; None if that is no use.

        A resume is only worth trying when the server stated a total (so
        there is something to be short of) and something arrived (so there
        is a byte to resume from). The reply is checked, not trusted: a 206
        whose Content-Range starts somewhere else, or a plain 200, means the
        server is sending the file from the top and the part file has to be
        truncated to match, and anything else means give up and report the
        original break.
        """
        stream.close()
        download._stream = None
        if not download.total or not download.received:
            return None
        offset = download.received
        try:
            resumed = open_stream(
                download.url, timeout=TIMEOUT,
                extra_headers={"Accept": "*/*",
                               "Range": "bytes=%d-" % offset})
        except OSError:
            return None
        download._stream = resumed
        if resumed.status == 206:
            start = _content_range_start(resumed.headers.get("content-range"))
            if start == offset:
                download.resumed += 1
                return resumed
            resumed.close()
            download._stream = None
            return None
        if resumed.status == 200:
            # No Range support: start over, and rewind the file with it.
            handle.seek(0)
            handle.truncate()
            download.rewind(0)
            download.resumed += 1
            return resumed
        resumed.close()
        download._stream = None
        return None


class DownloadError(OSError):
    """A download that failed for a reason above the socket layer."""


class _Cancelled(Exception):
    """Internal: the user asked for this transfer to stop."""


def _content_range_start(value):
    """First byte offset in a `Content-Range: bytes 100-999/1000`, or None."""
    if not value:
        return None
    match = re.search(r"bytes\s+(\d+)-", value, re.I)
    return int(match.group(1)) if match else None


def _describe_os_error(exc):
    """A disk or socket error in words, with the OS's own message kept."""
    if isinstance(exc, DownloadError):
        return str(exc)
    if getattr(exc, "errno", None) == errno.ENOSPC:
        return "disk full"
    if getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
        return "permission denied: %s" % (exc.filename or exc.strerror)
    text = exc.strerror or str(exc)
    return "%s: %s" % (type(exc).__name__, text) if text else type(exc).__name__
