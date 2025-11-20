from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from typing import Mapping, Optional

DEFAULT_SOCKET_PATH = os.getenv("GRATEKEEPER_SOCKET", "/tmp/gratekeeper.sock")


@dataclass
class RateLimitUpdate:
    bucket: str
    limit: Optional[int]
    remaining: Optional[int]
    reset_ts: Optional[int]


def _safe_int(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return None
    return None


def update_from_headers(
    headers: Mapping[str, str], *, bucket: str = "core"
) -> RateLimitUpdate:
    """Build a RateLimitUpdate from GitHub response headers."""
    limit = _safe_int(headers.get("X-RateLimit-Limit"))
    remaining = _safe_int(headers.get("X-RateLimit-Remaining"))
    reset = _safe_int(headers.get("X-RateLimit-Reset"))
    return RateLimitUpdate(
        bucket=bucket,
        limit=limit,
        remaining=remaining,
        reset_ts=reset,
    )


def emit_update(
    update: RateLimitUpdate,
    *,
    socket_path: Optional[str] = DEFAULT_SOCKET_PATH,
    timeout: float = 1.0,
) -> bool:
    """Send a single rate-limit update to the local bridge socket.

    Returns True on success, False otherwise.
    """
    if not socket_path:
        return False
    payload = json.dumps(update.__dict__) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path)
            sock.sendall(payload.encode("utf-8"))
        return True
    except OSError:
        return False
