"""Tests for the from-scratch Discord Rich Presence client."""
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feetbrowser import discord as D


def eq(a, b, msg=""):
    assert a == b, f"{msg}: {a!r} != {b!r}"


def test_frame_roundtrip():
    a, b = socket.socketpair()
    try:
        a.sendall(D._pack(D.HANDSHAKE, {"v": 1, "client_id": "abc"}))
        op, payload = D._unpack(b)
        eq(op, D.HANDSHAKE)
        eq(payload, {"v": 1, "client_id": "abc"})
        a.sendall(D._pack(D.FRAME, {"cmd": "SET_ACTIVITY"}))
        op2, payload2 = D._unpack(b)
        eq(op2, D.FRAME)
        eq(payload2, {"cmd": "SET_ACTIVITY"})
    finally:
        a.close(); b.close()


def test_ipc_path_discovery():
    tmp = tempfile.mkdtemp()
    sock = os.path.join(tmp, "discord-ipc-0")
    with open(sock, "w") as f:
        f.write("")
    old = os.environ.get("XDG_RUNTIME_DIR")
    os.environ["XDG_RUNTIME_DIR"] = tmp
    try:
        paths = list(D._unix_ipc_paths())
        assert sock in paths, f"discovered paths: {paths}"
    finally:
        if old is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = old


def test_build_activity_payload():
    rpc = D.DiscordRPC("1234")
    frame = rpc._build_activity("Google", "3 tabs open", 1234)
    eq(frame["cmd"], "SET_ACTIVITY")
    act = frame["args"]["activity"]
    eq(act["details"], "Google")
    eq(act["state"], "3 tabs open")
    eq(act["timestamps"]["start"], 1234)
    eq(act["buttons"], [{
        "label": "GitHub",
        "url": "https://github.com/juiceydew/feetbrowser",
    }])
    assert frame["nonce"], "a nonce is included"
    assert frame["args"]["pid"] == os.getpid()


def test_handshake_and_set_activity_over_a_real_socket():
    """The whole wire exchange against a local AF_UNIX peer: handshake,
    READY, then a SET_ACTIVITY frame carrying the presence we asked for."""
    if not hasattr(socket, "AF_UNIX"):
        return  # Windows before 10 1809, or a platform without them
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "discord-ipc-0")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(path)
    except OSError:
        server.close()
        return  # AF_UNIX exists but does not work here; skip, not fail
    server.listen(1)
    received = {}

    def serve():
        conn, _ = server.accept()
        msg = D._unpack(conn)
        if msg is not D._TIMEOUT and msg is not None:
            received["handshake"] = msg
            conn.sendall(D._pack(
                D.FRAME, {"cmd": "DISPATCH", "evt": "READY",
                          "data": {"user": {"id": "1"}}}))
        while True:
            msg = D._unpack(conn)
            if msg is D._TIMEOUT:
                continue
            if msg is None:
                break
            op, payload = msg
            if op == D.FRAME and payload.get("cmd") == "SET_ACTIVITY":
                received["activity"] = payload
                break
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    orig = D._connect

    def fake_connect():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(D._READ_TIMEOUT)
        s.connect(path)
        return s

    D._connect = fake_connect
    try:
        rpc = D.DiscordRPC("discord-test")
        rpc.start()
        rpc.set_activity("Google", "3 tabs open", 1234)
        deadline = time.time() + 3
        while not received.get("activity") and time.time() < deadline:
            time.sleep(0.02)
        rpc.close()
    finally:
        D._connect = orig
        server.close()

    op, hs = received.get("handshake", (None, {}))
    eq(op, D.HANDSHAKE, "first frame is the handshake")
    eq(hs.get("client_id"), "discord-test")
    act = received["activity"]["args"]["activity"]
    eq(act["details"], "Google")
    eq(act["state"], "3 tabs open")


def test_no_client_id_is_fine():
    # A bogus/absent client id must never raise or hang anything.
    rpc = D.DiscordRPC("")
    rpc.set_activity("x", "y", 1)
    rpc.close()  # nothing started: close is a no-op
    assert not rpc._thread.is_alive()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback; traceback.print_exc()
            print(f" FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(tests)} DISCORD RPC TESTS PASSED")


if __name__ == "__main__":
    main()