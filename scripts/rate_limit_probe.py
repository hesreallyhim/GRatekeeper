#!/usr/bin/env python3
"""Utility script that hammers the GitHub API until a rate limit kicks in.

Run this twice—first with the raw `requests` client and no auth to observe
the unauthenticated throttling (expect a 403/429 after ~10 search requests
within 60 seconds), then again with `gratekeeper` to compare latency and
rate-limit behavior under proactive throttling.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import requests

from dotenv import load_dotenv

load_dotenv()

try:
    from gratekeeper import RateLimitedGitHubClient
except ImportError:  # pragma: no cover - convenience for standalone use
    RateLimitedGitHubClient = None

DEFAULT_BASE_URL = "https://api.github.com/"
DEFAULT_ACCEPT = "application/vnd.github+json"
SEARCH_PARAMS = {"q": "stars:>1", "per_page": "1"}
GRAPHQL_QUERY = "query($login:String!){user(login:$login){login createdAt}}"


def load_token(cli_token: Optional[str], dotenv_path: Optional[str]) -> Optional[str]:
    if cli_token:
        return cli_token
    env_token = os.getenv("GITHUB_TOKEN")
    if env_token:
        return env_token
    path = Path(dotenv_path or ".env")
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        line: str = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "GITHUB_TOKEN":
            return value.strip().strip('"').strip("'")
    return None


@dataclass
class ProbeStats:
    client_name: str
    mode: str
    token_present: bool
    total_requests: int = 0
    limit_hit: bool = False
    limit_status: Optional[int] = None
    limit_reason: Optional[str] = None
    total_time: float = 0.0
    latencies: list[float] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def record_latency(self, elapsed: float) -> None:
        self.latencies.append(elapsed)

    def to_dict(self) -> Mapping[str, Any]:
        avg_latency = (
            sum(self.latencies) / len(self.latencies) * 1000 if self.latencies else 0.0
        )
        duration = self.total_time or (time.time() - self.started_at)
        rpm = self.total_requests / (duration / 60) if duration else 0.0
        return {
            "client": self.client_name,
            "mode": self.mode,
            "token_present": self.token_present,
            "total_requests": self.total_requests,
            "duration_seconds": round(duration, 3),
            "requests_per_minute_observed": round(rpm, 2),
            "avg_latency_ms": round(avg_latency, 2),
            "limit_hit": self.limit_hit,
            "limit_status": self.limit_status,
            "limit_reason": self.limit_reason,
        }


class BaseAdapter:
    name = "base"

    def __init__(self, base_url: str, token: Optional[str]) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token

    def close(self) -> None:
        return

    def enable_killswitch(
        self, *, seconds: float, reason: Optional[str] = None
    ) -> None:
        raise NotImplementedError("Killswitch is only available for certain adapters")

    # Methods implemented by subclasses
    def rest_call(self) -> requests.Response:
        raise NotImplementedError

    def graphql_call(self) -> requests.Response:
        raise NotImplementedError


class RequestsAdapter(BaseAdapter):
    name = "requests"

    def __init__(self, base_url: str, token: Optional[str], timeout: float) -> None:
        super().__init__(base_url, token)
        self._session = requests.Session()
        self._timeout = timeout

    def close(self) -> None:
        self._session.close()

    def _common_headers(self) -> dict[str, str]:
        headers = {"Accept": DEFAULT_ACCEPT, "User-Agent": "rate-limit-probe/0.1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def rest_call(self) -> requests.Response:
        url = f"{self.base_url}search/repositories"
        response = self._session.get(
            url,
            params=SEARCH_PARAMS,
            headers=self._common_headers(),
            timeout=self._timeout,
        )
        return response

    def graphql_call(self) -> requests.Response:
        url = f"{self.base_url}graphql"
        payload = {"query": GRAPHQL_QUERY, "variables": {"login": "octocat"}}
        response = self._session.post(
            url,
            json=payload,
            headers=self._common_headers(),
            timeout=self._timeout,
        )
        return response


class GratekeeperAdapter(BaseAdapter):
    name = "gratekeeper"

    def __init__(self, base_url: str, token: Optional[str], timeout: float) -> None:
        if RateLimitedGitHubClient is None:
            raise RuntimeError("gratekeeper is not installed in this environment")
        super().__init__(base_url, token)
        self._client = RateLimitedGitHubClient(
            token=token,
            base_url=base_url,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def rest_call(self) -> requests.Response:
        return self._client.get(
            "/search/repositories",
            params=SEARCH_PARAMS,
            bucket="search",
            raise_for_status=False,
        )

    def graphql_call(self) -> requests.Response:
        return self._client.graphql(
            GRAPHQL_QUERY,
            variables={"login": "octocat"},
            bucket="graphql",
            raise_for_status=False,
        )

    def enable_killswitch(
        self, *, seconds: float, reason: Optional[str] = None
    ) -> None:
        self._client.schedule_killswitch(after_seconds=seconds, reason=reason)


def _choose_adapter(name: str, base_url: str, token: Optional[str], timeout: float):
    if name == "requests":
        return RequestsAdapter(base_url, token, timeout)
    if name == "gratekeeper":
        return GratekeeperAdapter(base_url, token, timeout)
    raise ValueError(f"Unknown client '{name}'")


def _is_rate_limit(response: requests.Response) -> bool:
    remaining = response.headers.get("X-RateLimit-Remaining")
    if response.status_code == 429:
        return True
    if response.status_code == 403 and remaining == "0":
        return True
    return False


def _limit_reason(response: requests.Response) -> str:
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset = response.headers.get("X-RateLimit-Reset")
    return (
        f"status={response.status_code} remaining={remaining} reset={reset} "
        f"path={response.url}"
    )


def run_probe(
    adapter: BaseAdapter,
    *,
    mode: str,
    max_requests: int,
    inter_request_sleep: float,
    verbose: bool,
) -> ProbeStats:
    stats = ProbeStats(
        client_name=adapter.name,
        mode=mode,
        token_present=bool(adapter.token),
    )
    start = time.time()
    try:
        for _ in range(max_requests):
            before = time.time()
            if mode == "rest":
                response = adapter.rest_call()
            elif mode == "graphql":
                response = adapter.graphql_call()
            elif mode == "mixed":
                response = (
                    adapter.rest_call()
                    if stats.total_requests % 2 == 0
                    else adapter.graphql_call()
                )
            else:
                raise ValueError(f"Unsupported mode '{mode}'")
            elapsed = time.time() - before
            stats.record_latency(elapsed)
            stats.total_requests += 1
            if verbose:
                print(
                    f"{stats.total_requests:03d} -> {response.status_code} "
                    f"remaining={response.headers.get('X-RateLimit-Remaining')} "
                    f"latency={elapsed*1000:.2f}ms"
                )
            if _is_rate_limit(response):
                stats.limit_hit = True
                stats.limit_status = response.status_code
                stats.limit_reason = _limit_reason(response)
                break
            if inter_request_sleep:
                time.sleep(inter_request_sleep)
    finally:
        stats.total_time = time.time() - start
        adapter.close()
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hammer the GitHub API to observe rate limits with either raw requests "
            "or the gratekeeper client."
        )
    )
    parser.add_argument(
        "--client",
        choices=("requests", "gratekeeper"),
        default="requests",
        help="HTTP client implementation to use.",
    )
    parser.add_argument(
        "--mode",
        choices=("rest", "graphql", "mixed"),
        default="rest",
        help="Which workload to generate.",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="GitHub API base URL (defaults to public api.github.com).",
    )
    parser.add_argument(
        "--token",
        help="Explicit GitHub token (overrides environment / .env).",
    )
    parser.add_argument(
        "--dotenv",
        help="Path to .env file for fallback token loading (default: ./.env).",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=40,
        help="Maximum number of requests to attempt before stopping.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout per request in seconds.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Optional delay between requests.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-request status lines.",
    )
    parser.add_argument(
        "--killswitch-seconds",
        type=float,
        help="Enable the client killswitch after this many seconds (gratekeeper only).",
    )
    parser.add_argument(
        "--killswitch-reason",
        help="Optional message explaining why the killswitch is active.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = load_token(args.token, args.dotenv)
    adapter = _choose_adapter(args.client, args.base_url, token, args.timeout)
    if args.client == "gratekeeper" and args.killswitch_seconds is not None:
        adapter.enable_killswitch(
            seconds=max(args.killswitch_seconds, 0),
            reason=args.killswitch_reason,
        )
    stats = run_probe(
        adapter,
        mode=args.mode,
        max_requests=args.max_requests,
        inter_request_sleep=args.sleep,
        verbose=args.verbose,
    )
    print(json.dumps(stats.to_dict(), indent=2))
    if not stats.limit_hit:
        print(
            "No rate limit encountered. Increase --max-requests "
            "or remove auth to trigger a 429/403.",
            flush=True,
        )
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
