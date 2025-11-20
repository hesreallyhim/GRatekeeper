from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock
from typing import Callable, Dict, Mapping, Optional
import time

from .logging_utils import ensure_rich_logging, style_text


TimestampFn = Callable[[], int]
SleepFn = Callable[[float], None]

logger = logging.getLogger("gratekeeper")
ensure_rich_logging()


@dataclass
class BucketState:
    """Observed GitHub rate-limit state for a single logical bucket."""

    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset_ts: Optional[int] = None  # UNIX timestamp (UTC seconds)


class LocalGratekeeper:
    """Tracks GitHub rate-limit headers locally and sleeps before exhaustion."""

    def __init__(
        self,
        *,
        soft_floor_fraction: float = 0.2,
        soft_floor_min: int = 10,
        safety_buffer_seconds: int = 5,
        now_fn: Optional[TimestampFn] = None,
        sleep_fn: Optional[SleepFn] = None,
    ) -> None:
        self._buckets: Dict[str, BucketState] = {}
        self.soft_floor_fraction = soft_floor_fraction
        self.soft_floor_min = soft_floor_min
        self.safety_buffer_seconds = safety_buffer_seconds
        self._now_fn = now_fn or self._default_now
        self._sleep_fn = sleep_fn or time.sleep
        self._lock = Lock()

    @staticmethod
    def _default_now() -> int:
        return int(datetime.now(timezone.utc).timestamp())

    def _now(self) -> int:
        return int(self._now_fn())

    def before_request(self, bucket: str = "core") -> None:
        """Sleep if we are at/under the soft floor and the window has not reset."""
        sleep_for: Optional[float] = None
        sleep_context: Optional[tuple[str, Optional[int], int, Optional[int]]] = None

        with self._lock:
            state = self._buckets.setdefault(bucket, BucketState())
            now = self._now()

            if state.reset_ts is not None and now >= state.reset_ts:
                state.limit = None
                state.remaining = None
                state.reset_ts = None

            if state.limit is None or state.remaining is None:
                return

            soft_floor = max(
                int(state.limit * self.soft_floor_fraction), self.soft_floor_min
            )

            if (
                state.remaining <= soft_floor
                and state.reset_ts is not None
                and now < state.reset_ts
            ):
                sleep_for = max(state.reset_ts - now + self.safety_buffer_seconds, 0)
                sleep_context = (bucket, state.remaining, soft_floor, state.reset_ts)
            elif state.remaining > 0:
                state.remaining -= 1

        if sleep_for and sleep_for > 0:
            bucket_name, remaining, floor, reset_ts = sleep_context or (
                bucket,
                None,
                0,
                None,
            )
            message = (
                f"Rate limit low for bucket '{bucket_name}': remaining={remaining} "
                f"floor={floor} reset_ts={reset_ts}. Sleeping {sleep_for:.1f}s."
            )
            logger.warning(style_text(message, "bold yellow"))
            self._sleep_fn(sleep_for)

    def after_response(self, headers: Mapping[str, str], bucket: str = "core") -> bool:
        """Update bucket state from GitHub X-RateLimit-* headers.

        Returns True if any value was updated.
        """
        limit = self._parse_int_header(headers.get("X-RateLimit-Limit"))
        remaining = self._parse_int_header(headers.get("X-RateLimit-Remaining"))
        reset = self._parse_int_header(headers.get("X-RateLimit-Reset"))

        updated = False
        with self._lock:
            state = self._buckets.setdefault(bucket, BucketState())

            if limit is not None:
                state.limit = limit
                updated = True
            if remaining is not None:
                state.remaining = remaining
                updated = True
            if reset is not None:
                state.reset_ts = reset
                updated = True

            logger.debug(
                "Updated bucket '%s': limit=%s remaining=%s reset_ts=%s",
                bucket,
                state.limit,
                state.remaining,
                state.reset_ts,
            )
        return updated

    @staticmethod
    def _parse_int_header(value: Optional[str]) -> Optional[int]:
        if value is None:
            return None

        if isinstance(value, (list, tuple)):
            value = value[0]

        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def snapshot(self, bucket: str = "core") -> BucketState:
        """Return a shallow copy of the current bucket state for inspection."""
        with self._lock:
            state = self._buckets.setdefault(bucket, BucketState())
            return replace(state)
