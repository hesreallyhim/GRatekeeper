from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, IO, Mapping, MutableMapping, Optional
from urllib.parse import urljoin

import requests  # type: ignore[import-untyped]
from dotenv import load_dotenv

from .logging_utils import ensure_rich_logging, format_status, style_text
from .ratekeeper import BucketState, LocalGratekeeper

logger = logging.getLogger("gratekeeper")
ensure_rich_logging()

DEFAULT_BASE_URL = "https://api.github.com/"
DEFAULT_ACCEPT = "application/vnd.github+json"
DEFAULT_USER_AGENT = "gratekeeper/1.0.0"
_DOTENV_LOADED = False


def _ensure_dotenv_loaded() -> None:
    """Load environment variables from .env once per process."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    load_dotenv()
    _DOTENV_LOADED = True


class RateLimitedGitHubClient:
    """Minimal GitHub helper that throttles HTTP requests before exhaustion."""

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        rate_keeper: Optional[LocalGratekeeper] = None,
        session: Optional[requests.Session] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 15.0,
        enable_ratekeeping: bool = True,
        killswitch_until: Optional[float] = None,
    ) -> None:
        self.base_url = self._normalize_base_url(base_url)
        _ensure_dotenv_loaded()
        self._token = token or os.getenv("GITHUB_TOKEN")
        self._session = session or requests.Session()
        self._user_agent = user_agent
        self._timeout = timeout
        self._enable_ratekeeping = enable_ratekeeping
        self._rate_keeper = rate_keeper or LocalGratekeeper()
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop_event = threading.Event()
        self._poll_reset_event = threading.Event()
        self._poll_interval = 60.0
        self._poll_bucket = "core"
        self._rate_listeners: list[Callable[[str, BucketState], None]] = []
        self._listener_lock = threading.Lock()
        self._killswitch_until: Optional[float] = killswitch_until
        self._killswitch_reason: Optional[str] = None

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

    def set_killswitch(
        self, *, until_epoch: Optional[float], reason: Optional[str] = None
    ) -> None:
        """Prevent any further requests until the given UNIX timestamp."""
        self._killswitch_until = until_epoch
        self._killswitch_reason = reason

    def clear_killswitch(self) -> None:
        """Re-enable requests immediately."""
        self._killswitch_until = None
        self._killswitch_reason = None

    def schedule_killswitch(
        self, *, after_seconds: float, reason: Optional[str] = None
    ) -> None:
        """Enable the killswitch after a relative delay."""
        target = time.time() + max(after_seconds, 0)
        self.set_killswitch(until_epoch=target, reason=reason)

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
        **request_kwargs: Any,
    ) -> requests.Response:
        """Perform a throttled GET request and return the raw response."""
        return self._request(
            "GET",
            path,
            params=params,
            headers=headers,
            timeout=timeout,
            bucket=bucket,
            raise_for_status=raise_for_status,
            **request_kwargs,
        )

    def post(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        data: Any = None,
        json: Any = None,
        files: Optional[Mapping[str, IO[Any]]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        bucket: str = "core",
        raise_for_status: bool = True,
        **request_kwargs: Any,
    ) -> requests.Response:
        """Perform a throttled POST request and return the raw response."""
        return self._request(
            "POST",
            path,
            params=params,
            data=data,
            json=json,
            files=files,
            headers=headers,
            timeout=timeout,
            bucket=bucket,
            raise_for_status=raise_for_status,
            **request_kwargs,
        )

    def graphql(
        self,
        query: str,
        *,
        variables: Optional[Mapping[str, Any]] = None,
        operation_name: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        bucket: str = "graphql",
        raise_for_status: bool = True,
        **request_kwargs: Any,
    ) -> requests.Response:
        """Send a GraphQL POST to /graphql and return the raw response."""
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        if operation_name is not None:
            payload["operationName"] = operation_name
        return self._request(
            "POST",
            "/graphql",
            json=payload,
            headers=headers,
            timeout=timeout,
            bucket=bucket,
            raise_for_status=raise_for_status,
            **request_kwargs,
        )

    def get_json(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        bucket: str = "core",
        raise_for_status: bool = True,
        **request_kwargs: Any,
    ):
        """Convenience helper that returns parsed JSON content."""
        response = self.get(
            path,
            params=params,
            headers=headers,
            timeout=timeout,
            bucket=bucket,
            raise_for_status=raise_for_status,
            **request_kwargs,
        )
        return response.json()

    def graphql_json(
        self,
        query: str,
        *,
        variables: Optional[Mapping[str, Any]] = None,
        operation_name: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        bucket: str = "graphql",
        raise_for_status: bool = True,
        **request_kwargs: Any,
    ):
        """Convenience helper that returns parsed GraphQL JSON data."""
        response = self.graphql(
            query,
            variables=variables,
            operation_name=operation_name,
            headers=headers,
            timeout=timeout,
            bucket=bucket,
            raise_for_status=raise_for_status,
            **request_kwargs,
        )
        return response.json()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        data: Any = None,
        json: Any = None,
        files: Optional[Mapping[str, IO[Any]]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        bucket: str = "core",
        raise_for_status: bool = True,
        **request_kwargs: Any,
    ) -> requests.Response:
        method = method.upper()
        if self._killswitch_until is not None:
            now = time.time()
            if now < self._killswitch_until:
                reason = self._killswitch_reason or "killswitch active"
                raise RuntimeError(
                    f"RateLimitedGitHubClient killswitch active until {self._killswitch_until}: {reason}"
                )
            self.clear_killswitch()
        url = self._resolve_url(path)
        req_headers = self._build_headers(headers)
        logger.debug("%s %s params=%s", method, url, params)
        if self._enable_ratekeeping:
            self._rate_keeper.before_request(bucket)

        response = self._session.request(
            method,
            url,
            params=params,
            data=data,
            json=json,
            files=files,
            headers=req_headers,
            timeout=timeout or self._timeout,
            **request_kwargs,
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
        message = f"{method} {url} -> {status_text} (remaining={remaining} limit={limit} reset={reset})"

        if is_rate_limit_error:
            logger.error(style_text(message, "bold red", escape_message=False))
        elif response.status_code >= 400:
            logger.warning(message)
        else:
            logger.info(message)

        if raise_for_status:
            response.raise_for_status()
        return response

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
