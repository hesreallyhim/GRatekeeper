#!/usr/bin/env python3
"""Send periodic bursts of GitHub requests to exercise the dashboard refresh."""

from __future__ import annotations

import argparse
import os
import time

from dotenv import load_dotenv

from gratekeeper import RateLimitedGitHubClient
from gratekeeper.bridge import DEFAULT_SOCKET_PATH, emit_update, update_from_headers

load_dotenv()


def burst(
    client: RateLimitedGitHubClient,
    *,
    size: int,
    delay: float,
    bucket: str,
    socket_path: str,
) -> None:
    for _ in range(size):
        response = client.get(
            "/user",
            raise_for_status=False,
            bucket=bucket,
        )
        emit_update(
            update_from_headers(response.headers, bucket=bucket),
            socket_path=socket_path,
        )
        time.sleep(delay)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send a small, repeatable burst of GitHub requests to exercise rate-limit updates."
    )
    parser.add_argument("--token", help="GitHub token (defaults to $GITHUB_TOKEN)")
    parser.add_argument("--base-url", help="GitHub API base URL")
    parser.add_argument(
        "--burst-size", type=int, default=5, help="Requests per burst (default 5)"
    )
    parser.add_argument(
        "--burst-delay",
        type=float,
        default=1.0,
        help="Seconds between requests inside a burst (default 1.0s)",
    )
    parser.add_argument(
        "--between-bursts",
        type=float,
        default=2.0,
        help="Seconds to sleep after each burst (default 2.0s)",
    )
    parser.add_argument(
        "--bucket",
        default="core",
        help="Bucket name to attribute requests to (default: core)",
    )
    parser.add_argument(
        "--socket",
        default=os.getenv("GRATEKEEPER_SOCKET", DEFAULT_SOCKET_PATH),
        help="Unix socket path to notify the dashboard (use 'none' to skip).",
    )
    args = parser.parse_args()

    client_kwargs = {}
    if args.token:
        client_kwargs["token"] = args.token
    if args.base_url:
        client_kwargs["base_url"] = args.base_url

    client = RateLimitedGitHubClient(**client_kwargs)
    try:
        socket_path = None if args.socket in {"none", ""} else args.socket
        while True:
            burst(
                client,
                size=args.burst_size,
                delay=args.burst_delay,
                bucket=args.bucket,
                socket_path=socket_path or "",
            )
            time.sleep(args.between_bursts)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
