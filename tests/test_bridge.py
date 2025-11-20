import json
import os
import queue
import socket
import tempfile
import threading

from gratekeeper.bridge import (
    RateLimitUpdate,
    emit_update,
    update_from_headers,
)


def test_update_from_headers_parses_values() -> None:
    headers = {
        "X-RateLimit-Limit": "5000",
        "X-RateLimit-Remaining": "4999",
        "X-RateLimit-Reset": "1700000000",
    }
    update = update_from_headers(headers, bucket="core")
    assert update.bucket == "core"
    assert update.limit == 5000
    assert update.remaining == 4999
    assert update.reset_ts == 1_700_000_000


def test_update_from_headers_handles_missing_values() -> None:
    headers = {}
    update = update_from_headers(headers, bucket="search")
    assert update.bucket == "search"
    assert update.limit is None
    assert update.remaining is None
    assert update.reset_ts is None


def test_emit_update_sends_payload_over_socket() -> None:
    update = RateLimitUpdate(bucket="core", limit=10, remaining=9, reset_ts=123)
    tmpdir = tempfile.mkdtemp(prefix="gratekeeper-")
    socket_path = os.path.join(tmpdir, "bridge.sock")
    seen = queue.Queue()

    ready = threading.Event()

    def server() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(socket_path)
            srv.listen(1)
            ready.set()
            conn, _ = srv.accept()
            with conn:
                data = conn.recv(1024)
        seen.put(data.decode("utf-8"))

    thread = threading.Thread(target=server, daemon=True)
    thread.start()

    try:
        ready.wait(timeout=1.0)
        success = emit_update(update, socket_path=socket_path, timeout=1.0)
        assert success is True
        payload = seen.get(timeout=1.0)
        parsed = json.loads(payload.strip())
        assert parsed["bucket"] == "core"
        assert parsed["limit"] == 10
        assert parsed["remaining"] == 9
        assert parsed["reset_ts"] == 123
    finally:
        try:
            os.remove(socket_path)
        except OSError:
            pass
