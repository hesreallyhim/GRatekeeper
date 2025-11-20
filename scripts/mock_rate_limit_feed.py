#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import socket
import time
from typing import Iterable


def build_payloads(buckets: Iterable[str]) -> list[dict[str, object]]:
    now = int(time.time())
    payloads: list[dict[str, object]] = []
    base = [
        (bucket, 60, 50, now + 120) if bucket == "core" else (bucket, 30, 25, now + 90)
        for bucket in buckets
    ]
    for bucket, limit, remaining, reset in base:
        payloads.append(
            {
                "bucket": bucket,
                "limit": limit,
                "remaining": remaining,
                "reset_ts": reset,
            }
        )
    # Simulate some activity on core/search to drive Active-only.
    payloads.append(
        {"bucket": "core", "limit": 60, "remaining": 48, "reset_ts": now + 90}
    )
    payloads.append(
        {"bucket": "search", "limit": 30, "remaining": 22, "reset_ts": now + 80}
    )
    return payloads


def send_updates(
    sock_path: str, payloads: list[dict[str, object]], delay: float
) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(sock_path)
        for payload in payloads:
            line = json.dumps(payload).encode("utf-8") + b"\n"
            client.sendall(line)
            time.sleep(delay)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send mock rate-limit updates.")
    parser.add_argument(
        "--socket",
        default="/tmp/gratekeeper.sock",
        help="Path to the dashboard socket (default: /tmp/gratekeeper.sock)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay between updates in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--buckets",
        nargs="+",
        default=["core", "search", "graphql"],
        help="Buckets to simulate (default: core search graphql)",
    )
    args = parser.parse_args()
    payloads = build_payloads(args.buckets)
    send_updates(args.socket, payloads, args.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
