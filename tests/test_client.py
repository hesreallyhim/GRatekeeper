from __future__ import annotations

import json
import time
import unittest

import requests
from requests import Response
from requests.structures import CaseInsensitiveDict

from gratekeeper.client import RateLimitedGitHubClient  # type: ignore[import-not-found]
from gratekeeper.ratekeeper import BucketState, LocalRateKeeper  # type: ignore[import-not-found]


class StubSession(requests.Session):
    def __init__(self, responses: list[Response]) -> None:
        super().__init__()
        self._responses = responses
        self.calls: list[dict] = []

    def get(self, url, **kwargs):  # type: ignore[override]
        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError("No more stub responses available")
        response = self._responses.pop(0)
        response.url = url
        return response


def make_response(
    status: int = 200, headers: dict | None = None, payload: dict | None = None
) -> Response:
    response = Response()
    response.status_code = status
    response.headers = CaseInsensitiveDict(headers or {})
    body = json.dumps(payload or {"ok": True}).encode("utf-8")
    response._content = body
    response.encoding = "utf-8"
    return response


class RateLimitedGitHubClientTests(unittest.TestCase):
    def test_get_injects_token_header(self) -> None:
        resp = make_response(
            headers={
                "X-RateLimit-Limit": "60",
                "X-RateLimit-Remaining": "59",
                "X-RateLimit-Reset": "10",
            }
        )
        session = StubSession([resp])
        client = RateLimitedGitHubClient(token="abc123", session=session)

        client.get("/user")

        sent_headers = session.calls[0]["headers"]
        self.assertEqual(sent_headers["Authorization"], "Bearer abc123")
        self.assertEqual(sent_headers["Accept"], "application/vnd.github+json")

    def test_get_updates_ratekeeper(self) -> None:
        resp_headers = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4999",
            "X-RateLimit-Reset": "123",
        }
        resp = make_response(headers=resp_headers)
        session = StubSession([resp])
        keeper = LocalRateKeeper(now_fn=lambda: 0, sleep_fn=lambda _: None)
        client = RateLimitedGitHubClient(session=session, rate_keeper=keeper)

        data = client.get_json("/user")

        self.assertTrue(data["ok"])
        snapshot = client.rate_limit_snapshot()
        self.assertEqual(snapshot.limit, 5000)
        self.assertEqual(snapshot.remaining, 4999)
        self.assertEqual(snapshot.reset_ts, 123)

    def test_get_raises_for_status(self) -> None:
        resp = make_response(status=404, payload={"message": "Not Found"})
        session = StubSession([resp])
        client = RateLimitedGitHubClient(session=session)

        with self.assertRaises(requests.HTTPError):
            client.get("/missing")

    def test_polling_hits_rate_limit_endpoint(self) -> None:
        resp_headers = {
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": "59",
            "X-RateLimit-Reset": "10",
        }
        responses = [make_response(headers=resp_headers) for _ in range(10)]
        session = StubSession(responses)
        client = RateLimitedGitHubClient(session=session)

        client.start_rate_limit_polling(interval_seconds=0.05)
        time.sleep(0.15)
        client.stop_rate_limit_polling()

        poll_calls = [
            call for call in session.calls if call["url"].endswith("/rate_limit")
        ]
        self.assertGreaterEqual(len(poll_calls), 1)

    def test_rate_limit_listener_receives_updates(self) -> None:
        resp_headers = {
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": "58",
            "X-RateLimit-Reset": "25",
        }
        resp = make_response(headers=resp_headers)
        session = StubSession([resp])
        client = RateLimitedGitHubClient(session=session)

        snapshots: list[tuple[str, BucketState]] = []
        client.add_rate_limit_listener(
            lambda bucket, state: snapshots.append((bucket, state))
        )

        client.get("/user")

        self.assertEqual(len(snapshots), 1)
        bucket, state = snapshots[0]
        self.assertEqual(bucket, "core")
        self.assertEqual(state.limit, 60)
        self.assertEqual(state.remaining, 58)
        self.assertEqual(state.reset_ts, 25)


if __name__ == "__main__":
    unittest.main()
