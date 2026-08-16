"""Discord Rich Presence, from scratch.

Talks to the local Discord client over its Rich Presence IPC socket: a Unix
domain socket on Linux/macOS, a named pipe on Windows. The wire format is a
4-byte little-endian opcode, a 4-byte little-endian length, then a UTF-8 JSON
body -- nothing to import, just socket + struct (ctypes on Windows).

The RPC lives on its own daemon thread so it never blocks the browser. The
UI thread pushes ``(details, state)`` updates through a queue; the thread
sends them only when they change, coalesced to at most one a second, and
reconnects on its own if Discord restarts or closes the socket.
"""

import json
import os
import queue
import socket
import struct
import sys
import threading
import time
import uuid

# Frame opcodes.
HANDSHAKE = 0
FRAME = 1
CLOSE = 2
PING = 3
PONG = 4

#: Set when the peer had nothing for us yet (a read timeout), distinct from
#: the connection having gone away.
_TIMEOUT = object()

#: The Discord application this browser reports to. Override with
#: FEETBROWSER_DISCORD_CLIENT_ID.
DEFAULT_CLIENT_ID = "1538384416634699806"

_RECONNECT_SECONDS = 5
_MIN_UPDATE_SECONDS = 1.0
_READ_TIMEOUT = 0.5


def _pack(op, payload):
    """Build one IPC frame: opcode + length + UTF-8 JSON body."""
    if isinstance(payload, dict):
        payload = json.dumps(payload, separators=(",", ":"))
    data = payload.encode("utf-8")
    return struct.pack("<II", op, len(data)) + data


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = conn.recv(n - len(buf))
        except socket.timeout:
            return _TIMEOUT if not buf else None
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _unpack(conn):
    """Read one frame. Returns (opcode, payload-dict), _TIMEOUT when nothing
    arrived yet, or None when the connection closed."""
    head = _recv_exact(conn, 8)
    if head is _TIMEOUT or head is None:
        return head
    op, length = struct.unpack("<II", head)
    body = _recv_exact(conn, length)
    if body is _TIMEOUT or body is None:
        return None
    try:
        return op, json.loads(body.decode("utf-8"))
    except ValueError:
        return op, body.decode("utf-8", "replace")


# -- IPC path discovery --------------------------------------------------


def _unix_ipc_paths():
    """Yield candidate `discord-ipc-*` socket paths, mirroring where the
    desktop clients put them (stable, snap, and the flatpak/app dirs)."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    tempdir = os.environ.get("XDG_RUNTIME_DIR")
    if not tempdir and uid is not None and os.path.isdir(f"/run/user/{uid}"):
        tempdir = f"/run/user/{uid}"
    if not tempdir:
        import tempfile
        tempdir = tempfile.gettempdir()
    bases = []
    for sub in (".", "..", "snap.discord",
                "app/com.discordapp.Discord",
                "app/com.discordapp.DiscordCanary"):
        base = os.path.abspath(os.path.join(tempdir, sub))
        if base not in bases:
            bases.append(base)
    for base in bases:
        if not os.path.isdir(base):
            continue
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for entry in entries:
            if not entry.startswith("discord-ipc-"):
                continue
            path = os.path.join(base, entry)
            if os.path.exists(path):
                yield path


class _WinPipe:
    """A Windows named pipe, used through the same recv()/sendall() surface
    as a Unix socket. Reads are polled with PeekNamedPipe so an empty pipe
    reads as a timeout rather than a disconnect."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle = None

    def open(self, name):
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        self._handle = self._kernel32.CreateFileW(
            name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
        return self._handle not in (0, -1)

    def recv(self, n):
        wintypes = self._wintypes
        avail = wintypes.DWORD(0)
        ok = self._kernel32.PeekNamedPipe(
            self._handle, None, 0, None, self._ctypes.byref(avail), None)
        if not ok or avail.value == 0:
            raise socket.timeout("no data")
        buf = self._ctypes.create_string_buffer(avail.value)
        read = wintypes.DWORD(0)
        ok = self._kernel32.ReadFile(
            self._handle, buf, avail.value, self._ctypes.byref(read), None)
        if not ok:
            raise OSError("pipe read failed")
        return buf.raw[:read.value]

    def sendall(self, data):
        written = self._wintypes.DWORD(0)
        ok = self._kernel32.WriteFile(
            self._handle, data, len(data), self._ctypes.byref(written), None)
        if not ok:
            raise OSError("pipe write failed")
        return None

    def close(self):
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _connect():
    """Open a connection to whichever local Discord client answers."""
    if sys.platform == "win32":
        pipe = _WinPipe()
        for i in range(10):
            if pipe.open(fr"\\.\pipe\discord-ipc-{i}"):
                return pipe
        return None
    for path in _unix_ipc_paths():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(_READ_TIMEOUT)
        try:
            s.connect(path)
            return s
        except OSError:
            s.close()
    return None


# -- the client ----------------------------------------------------------


class DiscordRPC:
    """One daemon thread that owns the Discord IPC connection.

    ``set_activity(details, state, start)`` is safe to call from any thread;
    it queues an update that the worker sends (coalesced, at most once a
    second) whenever the presence actually changes.
    """

    def __init__(self, client_id=DEFAULT_CLIENT_ID, pid=None):
        self._client_id = client_id
        self._pid = pid if pid is not None else os.getpid()
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="discord-rpc")

    def start(self):
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread.is_alive() or self._thread.ident is not None:
            self._thread.join(timeout=2)

    def set_activity(self, details, state, start=None):
        self._queue.put((details, state, start))

    # -- worker ---------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            conn = _connect()
            if conn is None:
                self._stop.wait(_RECONNECT_SECONDS)
                continue
            try:
                if self._handshake(conn):
                    self._serve(conn)
            except Exception:
                pass  # anything that went wrong just triggers a reconnect
            finally:
                conn.close()

    def _handshake(self, conn):
        conn.sendall(_pack(HANDSHAKE, {"v": 1, "client_id": self._client_id}))
        while not self._stop.is_set():
            msg = _unpack(conn)
            if msg is _TIMEOUT:
                continue
            if msg is None:
                return False
            op, payload = msg
            if op == PING:
                conn.sendall(_pack(PONG, None))
            elif op == FRAME and payload.get("evt") == "READY":
                return True
            elif op == CLOSE:
                return False

    def _serve(self, conn):
        pending = None
        sent = None
        last_sent = 0.0
        while not self._stop.is_set():
            msg = _unpack(conn)
            if msg is _TIMEOUT:
                pass
            elif msg is None:
                return
            else:
                op, payload = msg
                if op == PING:
                    conn.sendall(_pack(PONG, None))
                elif op == CLOSE:
                    return
            try:
                pending = self._queue.get_nowait()
            except queue.Empty:
                pass
            if pending is not None and pending != sent and (
                    sent is None or time.monotonic() - last_sent
                    >= _MIN_UPDATE_SECONDS):
                frame = _pack(FRAME, self._build_activity(*pending))
                conn.sendall(frame)
                sent = pending
                last_sent = time.monotonic()

    def _build_activity(self, details, state, start):
        activity = {
            "details": details,
            "state": state,
            "instance": False,
            "buttons": [{
                "label": "GitHub",
                "url": "https://github.com/juiceydew/feetbrowser",
            }],
        }
        if start:
            activity["timestamps"] = {"start": int(start)}
        return {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": self._pid, "activity": activity},
            "nonce": uuid.uuid4().hex,
        }