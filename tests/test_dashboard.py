from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Callable, Optional
from unittest.mock import patch

import os

from gratekeeper.dashboard import (
    RateLimitDashboard,
    RateLimitResource,
    _coerce_resources,
    _fmt_delta,
    _launch_in_tmux,
)
from gratekeeper.ratekeeper import BucketState


class _StubClient:
    def __init__(self, routes: Optional[dict[str, object]] = None) -> None:
        self.listener: Optional[Callable[[str, BucketState], None]] = None
        self.fetches = 0
        self.routes = routes or {}

    def add_rate_limit_listener(
        self, listener: Callable[[str, BucketState], None]
    ) -> None:
        self.listener = listener

    def remove_rate_limit_listener(
        self, listener: Callable[[str, BucketState], None]
    ) -> None:
        if self.listener is listener:
            self.listener = None

    def emit(self, bucket: str, state: BucketState) -> None:
        if self.listener:
            self.listener(bucket, state)

    def get_json(self, path, **kwargs):
        self.fetches += 1
        payload = self.routes.get(path)
        if callable(payload):
            return payload(path, **kwargs)  # type: ignore[misc]
        if payload is not None:
            return payload
        return {
            "resources": {
                "core": {"limit": 60, "remaining": 55, "reset": 999},
            }
        }


def test_dashboard_builds_table_with_known_resource() -> None:
    dash = RateLimitDashboard(
        _StubClient(), buckets=("core",), auto_fetch=False, enable_keybindings=False
    )
    future_reset = int(datetime.now(timezone.utc).timestamp()) + 60
    dash._resources["core"] = RateLimitResource(
        bucket="core",
        limit=60,
        remaining=42,
        reset_ts=future_reset,
    )

    table = dash._build_table()

    assert table.row_count == 1


def test_fmt_delta_handles_past_and_future_times() -> None:
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    future_reset = int(now.timestamp()) + 120
    assert _fmt_delta(future_reset, now) == "2m 00s"
    assert _fmt_delta(int(now.timestamp()) - 5, now) == "reset"


def test_coerce_resources_casts_numeric_values() -> None:
    resources = {
        "core": {"limit": "60", "remaining": "45", "reset": "123"},
        "search": {"limit": None, "remaining": None, "reset": None},
    }

    parsed = _coerce_resources(resources)

    assert parsed["core"].limit == 60
    assert parsed["core"].remaining == 45
    assert parsed["search"].limit is None


def test_handle_snapshot_stores_latest_state() -> None:
    client = _StubClient()
    dash = RateLimitDashboard(client, auto_fetch=False, enable_keybindings=False)
    state = BucketState(limit=100, remaining=80, reset_ts=500)

    dash._handle_snapshot("core", state)

    assert dash._resources["core"].remaining == 80


def test_maybe_fetch_runs_when_idle() -> None:
    client = _StubClient()
    dash = RateLimitDashboard(
        client, auto_fetch=True, fetch_interval=0.1, enable_keybindings=False
    )

    dash._maybe_fetch()

    assert client.fetches == 1
    assert "core" in dash._resources


def test_maybe_fetch_skips_after_recent_update() -> None:
    client = _StubClient()
    dash = RateLimitDashboard(
        client, auto_fetch=True, fetch_interval=60.0, enable_keybindings=False
    )
    dash._handle_snapshot("core", BucketState(limit=60, remaining=50, reset_ts=10))

    dash._maybe_fetch()

    assert client.fetches == 0


def test_keypress_adjusts_refresh_speed_and_manual_fetch() -> None:
    client = _StubClient()
    dash = RateLimitDashboard(
        client, auto_fetch=False, refresh_interval=8.0, enable_keybindings=False
    )

    dash._handle_keypress("u")

    assert dash._current_refresh_interval() == 4.0

    dash._handle_keypress("d")
    assert dash._current_refresh_interval() == 8.0

    dash._handle_keypress("r")
    assert dash._manual_fetch_event.is_set()


def test_actions_repo_status_fetch() -> None:
    routes = {
        "/repos/foo/bar/actions/runs": {
            "workflow_runs": [
                {"status": "in_progress", "conclusion": None, "updated_at": "now"},
                {"status": "queued", "conclusion": None},
            ]
        }
    }
    client = _StubClient(routes=routes)
    dash = RateLimitDashboard(
        client,
        auto_fetch=False,
        enable_keybindings=False,
        actions_repos=("foo/bar",),
    )

    dash._maybe_fetch_actions(force=True)

    assert dash._actions_status["foo/bar"].in_progress == 1
    assert dash._actions_status["foo/bar"].queued == 1


def test_actions_billing_fetch_user() -> None:
    routes = {
        "/user/settings/billing/actions": {
            "total_minutes_used": 120,
            "included_minutes": 2000,
            "total_paid_minutes_used": 0,
        }
    }
    client = _StubClient(routes=routes)
    dash = RateLimitDashboard(
        client,
        auto_fetch=False,
        enable_keybindings=False,
        actions_billing_user=True,
    )

    dash._maybe_fetch_actions(force=True)

    assert dash._actions_billing["user"].total_minutes_used == 120


def test_actions_data_clears_when_fetch_returns_nothing() -> None:
    repo_path = "/repos/foo/bar/actions/runs"
    routes = {
        repo_path: {
            "workflow_runs": [
                {"status": "in_progress", "conclusion": None, "updated_at": "now"},
            ]
        }
    }
    client = _StubClient(routes=routes)
    dash = RateLimitDashboard(
        client,
        auto_fetch=False,
        enable_keybindings=False,
        actions_repos=("foo/bar",),
    )

    dash._maybe_fetch_actions(force=True)
    assert "foo/bar" in dash._actions_status

    client.routes[repo_path] = {"workflow_runs": "invalid"}
    dash._maybe_fetch_actions(force=True)

    assert dash._actions_status == {}


def test_actions_billing_clears_when_unavailable() -> None:
    billing_path = "/user/settings/billing/actions"
    routes = {
        billing_path: {
            "total_minutes_used": 1,
            "included_minutes": 2,
            "total_paid_minutes_used": 0,
        }
    }
    client = _StubClient(routes=routes)
    dash = RateLimitDashboard(
        client,
        auto_fetch=False,
        enable_keybindings=False,
        actions_billing_user=True,
    )

    dash._maybe_fetch_actions(force=True)
    assert "user" in dash._actions_billing

    client.routes[billing_path] = "oops"
    dash._maybe_fetch_actions(force=True)

    assert dash._actions_billing == {}


def test_launch_in_tmux_no_binary() -> None:
    with patch("gratekeeper.dashboard.shutil.which", return_value=None):
        assert _launch_in_tmux(["--tmux-pane"]) is False


def test_launch_in_tmux_invokes_tmux_command() -> None:
    with patch("gratekeeper.dashboard.shutil.which", return_value="/usr/bin/tmux"):
        with patch.dict(os.environ, {"TMUX": "1"}, clear=True):
            captured: dict[str, list[str]] = {}

            class Result:
                returncode = 0

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                return Result()

            with patch("gratekeeper.dashboard.subprocess.run", side_effect=fake_run):
                assert _launch_in_tmux(["--tmux-pane", "--refresh", "5"]) is True


def test_main_tmux_flag_without_session_exits_with_message() -> None:
    import gratekeeper.dashboard as dashboard

    stderr = io.StringIO()

    with patch("gratekeeper.dashboard._launch_in_tmux", return_value=False):
        with patch("sys.stderr", new=stderr):
            code = dashboard.main(["--tmux-pane"])

    assert code == 1
    assert "tmux" in stderr.getvalue().lower()
