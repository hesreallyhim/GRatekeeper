from __future__ import annotations

import unittest

from gratekeeper.ratekeeper import LocalGratekeeper  # type: ignore[import-not-found]


class LocalGratekeeperTests(unittest.TestCase):
    def test_after_response_updates_state(self) -> None:
        keeper = LocalGratekeeper(now_fn=lambda: 0)
        headers = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4500",
            "X-RateLimit-Reset": "1234567890",
        }

        keeper.after_response(headers)
        state = keeper.snapshot()

        self.assertEqual(state.limit, 5000)
        self.assertEqual(state.remaining, 4500)
        self.assertEqual(state.reset_ts, 1234567890)

    def test_before_request_decrements_remaining(self) -> None:
        keeper = LocalGratekeeper(now_fn=lambda: 0)
        keeper.after_response(
            {
                "X-RateLimit-Limit": "100",
                "X-RateLimit-Remaining": "50",
                "X-RateLimit-Reset": "10",
            }
        )

        keeper.before_request()
        state = keeper.snapshot()
        self.assertEqual(state.remaining, 49)

    def test_before_request_sleeps_when_under_floor(self) -> None:
        sleep_calls: list[float] = []
        keeper = LocalGratekeeper(
            soft_floor_fraction=0.5,
            soft_floor_min=1,
            safety_buffer_seconds=3,
            now_fn=lambda: 0,
            sleep_fn=lambda seconds: sleep_calls.append(seconds),
        )
        keeper.after_response(
            {
                "X-RateLimit-Limit": "10",
                "X-RateLimit-Remaining": "2",
                "X-RateLimit-Reset": "20",
            }
        )

        keeper.before_request()
        state = keeper.snapshot()

        self.assertEqual(sleep_calls, [23])
        self.assertEqual(state.remaining, 2)


if __name__ == "__main__":
    unittest.main()
