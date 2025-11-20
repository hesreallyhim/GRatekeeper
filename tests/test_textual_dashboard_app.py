from __future__ import annotations

import asyncio
import types

from textual.widgets import Static

from gratekeeper.textual_dashboard import (
    RateLimitResource,
    RateLimitTextualApp,
    _RateLimitSocketServer,
)


class _StubClient:
    def __init__(self) -> None:
        self.listener = None

    def add_rate_limit_listener(self, listener) -> None:  # pragma: no cover - tiny stub
        self.listener = listener

    def remove_rate_limit_listener(self, listener) -> None:  # pragma: no cover
        if self.listener is listener:
            self.listener = None

    def close(self) -> None:  # pragma: no cover
        return


async def _noop_cycle(self, force: bool = False) -> None:
    return None


async def _noop_poll(self) -> None:
    return None


def _make_app(monkeypatch) -> RateLimitTextualApp:
    # Ignore user config to keep tests deterministic.
    monkeypatch.setattr(RateLimitTextualApp, "_load_config", lambda self: None)

    # Disable socket bridge to avoid permission issues in CI.
    async def _noop_start(self):
        return None

    monkeypatch.setattr(_RateLimitSocketServer, "start", _noop_start)
    client = _StubClient()
    app = RateLimitTextualApp(client)
    # Avoid background fetches/polling during tests.
    monkeypatch.setattr(app, "_fetch_cycle", types.MethodType(_noop_cycle, app))
    monkeypatch.setattr(app, "_poll_loop", types.MethodType(_noop_poll, app))
    return app


def test_title_is_rendered(monkeypatch) -> None:
    async def _run():
        app = _make_app(monkeypatch)
        async with app.run_test() as pilot:
            await pilot.pause()
            hero = pilot.app.query_one("#hero-text", expect_type=Static)
            rendered = hero.render()
            assert "GRatekeeper Dashboard" in str(rendered)

    asyncio.run(_run())


def test_actions_toggle_hides_sidebar(monkeypatch) -> None:
    async def _run():
        app = _make_app(monkeypatch)
        async with app.run_test() as pilot:
            await pilot.pause()
            sidebar = pilot.app.query_one("#sidebar")
            actions = pilot.app.query_one("ActionsPanel")
            assert sidebar.display is True
            assert actions.display is True

            await pilot.press("x")
            await pilot.pause()

            assert sidebar.display is False
            assert actions.display is False

    asyncio.run(_run())


def test_active_only_filters_by_recent_activity(monkeypatch) -> None:
    async def _run():
        app = _make_app(monkeypatch)
        async with app.run_test() as pilot:
            await pilot.pause()
            # Establish baseline snapshots.
            app._apply_snapshot(
                RateLimitResource(bucket="core", limit=60, remaining=50, reset_ts=0)
            )
            app._apply_snapshot(
                RateLimitResource(bucket="search", limit=10, remaining=8, reset_ts=0)
            )
            # Change core to mark it active; search stays unchanged.
            app._apply_snapshot(
                RateLimitResource(bucket="core", limit=60, remaining=49, reset_ts=0)
            )

            await pilot.press("a")  # Active-only
            await pilot.pause()

            core_card = app._bucket_cards["core"]
            search_card = app._bucket_cards["search"]
            assert core_card.display is True
            assert search_card.display is False

    asyncio.run(_run())
