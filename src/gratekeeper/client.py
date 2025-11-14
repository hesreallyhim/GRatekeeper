from __future__ import annotations

import os
import logging
import threading
from typing import Callable, Mapping, MutableMapping, Optional
from urllib.parse import urljoin

import requests

from .logging_utils import ensure_rich_logging, format_status, style_text
from .ratekeeper import BucketState, LocalRateKeeper

logger = logging.getLogger("gratekeeper")
ensure_rich_logging()

DEFAULT_BASE_URL = "https://api.github.com/"
DEFAULT_ACCEPT = "application/vnd.github+json"
DEFAULT_USER_AGENT = "gratekeeper/0.1"


class RateLimitedGitHubClient:
    """Minimal GitHub REST helper that throttles GET requests before exhaustion."""

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        rate_keeper: Optional[LocalRateKeeper] = None,
        session: Optional[requests.Session] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 15.0,
        enable_ratekeeping: bool = True,
    ) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self._token = token or os.getenv("GITHUB_TOKEN")
        self._session = session or requests.Session()
        self._user_agent = user_agent
        self._timeout = timeout
        self._enable_ratekeeping = enable_ratekeeping
        self._rate_keeper = rate_keeper or LocalRateKeeper()
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop_event = threading.Event()
        self._poll_reset_event = threading.Event()
        self._poll_interval = 60.0
        self._poll_bucket = "core"
        self._rate_listeners: list[Callable[[str, BucketState], None]] = []
        self._listener_lock = threading.Lock()

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        if not base_url:
            raise ValueError("base_url cannot be empty")
        if not base_url.endswith("/"):
            base_url = f"{base_url}/"
        return base_url

    def close(self) -> None:
        self.stop_rate_limit_polling()
        self._session.close()

    def __enter__(self) -> RateLimitedGitHubClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.close()

    def _resolve_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(self.base_url, path.lstrip("/"))

    def _build_headers(
        self, headers: Optional[Mapping[str, str]]
    ) -> MutableMapping[str, str]:
        combined: MutableMapping[str, str] = {
            "Accept": DEFAULT_ACCEPT,
            "User-Agent": self._user_agent,
        }
        if self._token:
            combined["Authorization"] = f"Bearer {self._token}"
        if headers:
            combined.update(headers)
        return combined

    def get(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        bucket: str = "core",
        raise_for_status: bool = True,
    ) -> requests.Response:
        """Perform a throttled GET request and return the raw response."""
        url = self._resolve_url(path)
        req_headers = self._build_headers(headers)
        logger.debug("GET %s params=%s", url, params)
        if self._enable_ratekeeping:
            self._rate_keeper.before_request(bucket)

        response = self._session.get(
            url, params=params, headers=req_headers, timeout=timeout or self._timeout
        )

        if self._enable_ratekeeping:
            updated = self._rate_keeper.after_response(response.headers, bucket)
            if updated:
                self._reset_poll_timer()
                self._notify_rate_limit_listeners(bucket)
        remaining = response.headers.get("X-RateLimit-Remaining")
        limit = response.headers.get("X-RateLimit-Limit")
        reset = response.headers.get("X-RateLimit-Reset")
        is_rate_limit_error = response.status_code == 429 or (
            response.status_code == 403 and remaining == "0"
        )
        status_text = format_status(
            response.status_code, is_rate_limit=is_rate_limit_error
        )
        message = f"GET {url} -> {status_text} (remaining={remaining} limit={limit} reset={reset})"

        if is_rate_limit_error:
            logger.error(style_text(message, "bold red", escape_message=False))
        elif response.status_code >= 400:
            logger.warning(message)
        else:
            logger.info(message)

        if raise_for_status:
            response.raise_for_status()
        return response

    def get_json(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        bucket: str = "core",
        raise_for_status: bool = True,
    ):
        """Convenience helper that returns parsed JSON content."""
        response = self.get(
            path,
            params=params,
            headers=headers,
            timeout=timeout,
            bucket=bucket,
            raise_for_status=raise_for_status,
        )
        return response.json()

    def rate_limit_snapshot(self, bucket: str = "core") -> BucketState:
        """Expose the current bucket state for observability or logging."""
        return self._rate_keeper.snapshot(bucket)

    # ------------------------------------------------------------------
    # Rate-limit listener support

    def add_rate_limit_listener(
        self, listener: Callable[[str, BucketState], None]
    ) -> None:
        """Register a callback invoked when rate-limit headers update."""
        with self._listener_lock:
            if listener not in self._rate_listeners:
                self._rate_listeners.append(listener)

    def remove_rate_limit_listener(
        self, listener: Callable[[str, BucketState], None]
    ) -> None:
        """Unregister a previously added callback."""
        with self._listener_lock:
            if listener in self._rate_listeners:
                self._rate_listeners.remove(listener)

    def _notify_rate_limit_listeners(self, bucket: str) -> None:
        with self._listener_lock:
            listeners = list(self._rate_listeners)
        if not listeners:
            return
        snapshot = self.rate_limit_snapshot(bucket)
        for listener in listeners:
            try:
                listener(bucket, snapshot)
            except Exception:  # pragma: no cover - user callback errors
                logger.exception("Rate-limit listener failed; continuing")

    # ------------------------------------------------------------------
    # Polling support

    def start_rate_limit_polling(
        self, *, interval_seconds: float = 60.0, bucket: str = "core"
    ) -> None:
        """Start polling GET /rate_limit to refresh headers in the background."""
        self._poll_interval = max(0.01, interval_seconds)
        self._poll_bucket = bucket

        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_reset_event.set()
            return

        self._poll_stop_event.clear()
        self._poll_reset_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="GratekeeperPoller", daemon=True
        )
        self._poll_thread.start()
        logger.info(
            "Started rate-limit polling every %.1f seconds", self._poll_interval
        )

    def stop_rate_limit_polling(self) -> None:
        """Stop the poller thread if it is running."""
        if not self._poll_thread:
            return
        self._poll_stop_event.set()
        self._poll_reset_event.set()
        self._poll_thread.join(timeout=1.0)
        self._poll_thread = None
        logger.info("Stopped rate-limit polling")

    def _poll_loop(self) -> None:
        while not self._poll_stop_event.is_set():
            # Wait for either reset signal or interval timeout.
            triggered = self._poll_reset_event.wait(timeout=self._poll_interval)
            self._poll_reset_event.clear()
            if triggered:
                # Fresh headers arrived; restart the timer.
                continue

            try:
                self.get_json("/rate_limit", bucket=self._poll_bucket)
            except Exception as exc:  # pragma: no cover - best-effort logging
                logger.warning("Rate limit poll failed: %s", exc)

    def _reset_poll_timer(self) -> None:
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_reset_event.set()
