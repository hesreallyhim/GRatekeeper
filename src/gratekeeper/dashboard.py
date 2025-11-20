from __future__ import annotations

import argparse
import logging
import os
import select
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, MutableMapping, Optional

from rich.console import Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .client import RateLimitedGitHubClient
from .ratekeeper import BucketState
from .bridge import DEFAULT_SOCKET_PATH

logger = logging.getLogger("gratekeeper.dashboard")

try:  # pragma: no cover - platform dependent
    import termios
    import tty
except ImportError:  # pragma: no cover
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]


@dataclass
class RateLimitResource:
    bucket: str
    limit: Optional[int]
    remaining: Optional[int]
    reset_ts: Optional[int]

    @property
    def used(self) -> Optional[int]:
        if self.limit is None or self.remaining is None:
            return None
        return max(self.limit - self.remaining, 0)

    @property
    def usage_percent(self) -> Optional[float]:
        limit = self.limit
        remaining = self.remaining
        if limit is None or limit == 0 or remaining is None:
            return None
        return (limit - remaining) / limit * 100


@dataclass
class ActionsRepoStatus:
    repo: str
    in_progress: int = 0
    queued: int = 0
    latest_status: Optional[str] = None
    latest_conclusion: Optional[str] = None
    latest_updated: Optional[str] = None


@dataclass
class ActionsBillingStatus:
    scope: str
    total_minutes_used: Optional[int] = None
    included_minutes: Optional[int] = None
    total_paid_minutes_used: Optional[int] = None


class RateLimitDashboard:
    """Simple Rich-powered TUI that displays live GitHub rate-limit data."""

    def __init__(
        self,
        client: RateLimitedGitHubClient,
        *,
        buckets: Iterable[str] | None = None,
        auto_fetch: bool = True,
        refresh_interval: float = 60.0,
        fetch_interval: float = 60.0,
        enable_keybindings: bool = True,
        actions_repos: Iterable[str] | None = None,
        actions_billing_user: bool = False,
        actions_billing_org: Optional[str] = None,
    ) -> None:
        self._client = client
        self._buckets = tuple(buckets) if buckets else None
        self._auto_fetch = auto_fetch
        self._refresh_interval = max(refresh_interval, 0.1)
        self._fetch_interval = max(fetch_interval, 1.0)
        self._keybindings_enabled = enable_keybindings
        self._actions_repos = tuple(actions_repos) if actions_repos else tuple()
        self._actions_billing_user = actions_billing_user
        self._actions_billing_org = actions_billing_org
        if self._actions_billing_user or self._actions_billing_org:
            logger.info(
                "Actions billing display enabled (user=%s, org=%s)",
                self._actions_billing_user,
                self._actions_billing_org,
            )
        self._lock = threading.Lock()
        self._resources: MutableMapping[str, RateLimitResource] = {}
        self._last_fetch_error: Optional[str] = None
        self._last_fetch_ts: Optional[float] = None
        self._last_update_ts: Optional[float] = None
        self._last_update_source: Optional[str] = None
        self._actions_status: MutableMapping[str, ActionsRepoStatus] = {}
        self._actions_billing: MutableMapping[str, ActionsBillingStatus] = {}
        self._last_actions_fetch_ts: Optional[float] = None
        self._manual_fetch_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._input_stop_event = threading.Event()
        self._input_thread: Optional[threading.Thread] = None
        self._min_refresh_interval = 0.5
        self._max_refresh_interval = 300.0

    def run(self) -> None:
        """Start the dashboard loop until interrupted."""
        listener = self._handle_snapshot
        self._client.add_rate_limit_listener(listener)
        self._start_input_listener()
        try:
            with Live(
                self._render_panel(),
                refresh_per_second=self._refresh_rate_value(),
                screen=False,
            ) as live:
                try:
                    next_refresh = 0.0
                    while True:
                        if next_refresh:
                            self._wait_for_next_iteration(next_refresh)
                        force_fetch = self._manual_fetch_event.is_set()
                        if force_fetch:
                            self._manual_fetch_event.clear()
                        if self._auto_fetch:
                            self._maybe_fetch(force=force_fetch)
                        live.refresh_per_second = self._refresh_rate_value()
                        live.update(self._render_panel(), refresh=force_fetch)
                        next_refresh = time.time() + self._current_refresh_interval()
                except KeyboardInterrupt:
                    pass
        finally:
            self._client.remove_rate_limit_listener(listener)
            self._stop_input_listener()

    def _handle_snapshot(self, bucket: str, state: BucketState) -> None:
        resource = _resource_from_snapshot(bucket, state)
        with self._lock:
            self._resources[bucket] = resource
            self._last_update_ts = time.time()
            self._last_update_source = "client"
            self._last_fetch_error = None
        self._trigger_wakeup()

    # ------------------------------------------------------------------
    # Rendering helpers

    def _render_panel(self) -> Panel:
        table = self._build_table()
        subtitle = self._build_subtitle()
        title = "GitHub Rate Limit"
        actions_panel = self._build_actions_table()
        body: RenderableType = Group(table, actions_panel) if actions_panel else table
        return Panel(body, title=title, subtitle=subtitle, border_style="cyan")

    def _build_table(self) -> Table:
        table = Table(expand=True)
        table.add_column("Bucket", style="bold")
        table.add_column("Limit", justify="right")
        table.add_column("Remaining", justify="right")
        table.add_column("Usage", justify="right")
        table.add_column("Resets In", justify="right")
        table.add_column("Reset (UTC)", justify="left")

        with self._lock:
            resources = dict(self._resources)
        bucket_order = self._determine_bucket_order(resources)
        if not bucket_order:
            table.add_row("—", "—", "—", "—", "—", "—")
            return table

        now = datetime.now(timezone.utc)
        for bucket in bucket_order:
            resource = resources.get(bucket)
            if not resource:
                table.add_row(bucket, "—", "—", "—", "—", "—")
                continue

            limit = _fmt_int(resource.limit)
            remaining = _fmt_int(resource.remaining)
            usage = _fmt_usage(resource)
            reset_in = _fmt_delta(resource.reset_ts, now)
            reset_at = _fmt_reset_time(resource.reset_ts)

            table.add_row(bucket, limit, remaining, usage, reset_in, reset_at)
        return table

    def _build_actions_table(self) -> Optional[Table]:
        with self._lock:
            statuses = dict(self._actions_status)
            billing = dict(self._actions_billing)
        if not statuses and not billing:
            return None

        table = Table(title="GitHub Actions", expand=True)
        table.add_column("Repo / Scope", style="bold")
        table.add_column("In progress", justify="right")
        table.add_column("Queued", justify="right")
        table.add_column("Latest", justify="left")

        if statuses:
            for repo in sorted(statuses.keys()):
                status = statuses[repo]
                latest = status.latest_status or "—"
                if status.latest_conclusion:
                    latest += f" ({status.latest_conclusion})"
                table.add_row(
                    repo,
                    str(status.in_progress),
                    str(status.queued),
                    latest,
                )

        if billing:
            for scope, data in sorted(billing.items()):
                used = (
                    f"{data.total_minutes_used}m / {data.included_minutes}m"
                    if data.total_minutes_used is not None
                    else "—"
                )
                table.add_row(
                    scope,
                    "—",
                    "—",
                    used,
                )

        return table

    def _build_subtitle(self) -> Text:
        with self._lock:
            last_update = self._last_update_ts
            source = self._last_update_source
            last_fetch_error = self._last_fetch_error
        subtitle = Text()
        if last_update:
            last_dt = datetime.fromtimestamp(last_update, timezone.utc)
            label = "client" if source == "client" else "fetch"
            subtitle.append(
                f"Last update ({label}): {last_dt.isoformat(timespec='seconds')}Z"
            )
        else:
            subtitle.append("Waiting for first update…")

        if last_fetch_error:
            subtitle.append("  •  ")
            subtitle.append(f"Error: {last_fetch_error}", style="bold red")
        else:
            subtitle.append(f"  •  Refresh every {self._refresh_interval:.1f}s")
        subtitle.append(
            f"  •  Fallback fetch every {self._fetch_interval:.0f}s if idle"
        )
        subtitle.append("  •  Press Ctrl+C to exit")
        subtitle.append(
            "\nControls: u faster, d slower, r refresh "
            f"(current {self._current_refresh_interval():.1f}s)"
        )
        return subtitle

    def _determine_bucket_order(
        self, resources: Mapping[str, RateLimitResource]
    ) -> tuple[str, ...]:
        if self._buckets:
            return self._buckets
        if resources:
            return tuple(sorted(resources.keys()))
        return tuple()

    # ------------------------------------------------------------------
    # Fetching

    def _maybe_fetch(self, *, force: bool = False) -> None:
        now = time.time()
        fetch_rate_limit = True
        with self._lock:
            last_update = self._last_update_ts
            if not force and last_update and now - last_update < self._fetch_interval:
                fetch_rate_limit = False

        if fetch_rate_limit:
            try:
                payload = self._client.get_json("/rate_limit", raise_for_status=False)
                resources_obj = (
                    payload.get("resources", {}) if isinstance(payload, Mapping) else {}
                )
                resources = resources_obj if isinstance(resources_obj, Mapping) else {}
                parsed = _coerce_resources(resources)  # type: ignore[arg-type]
                with self._lock:
                    self._resources = parsed
                    self._last_fetch_error = None
                    self._last_fetch_ts = now
                    self._last_update_ts = now
                    self._last_update_source = "manual" if force else "fetch"
            except Exception as exc:  # pragma: no cover - network variability
                with self._lock:
                    self._last_fetch_error = str(exc)

        self._maybe_fetch_actions(force=force)

    def _maybe_fetch_actions(self, *, force: bool = False) -> None:
        if (
            not self._actions_repos
            and not self._actions_billing_user
            and not self._actions_billing_org
        ):
            return
        now = time.time()
        with self._lock:
            last_fetch = self._last_actions_fetch_ts
        if not force and last_fetch and now - last_fetch < self._fetch_interval:
            return
        statuses: MutableMapping[str, ActionsRepoStatus] = {}
        if self._actions_repos:
            for repo in self._actions_repos:
                status = self._fetch_actions_repo(repo)
                if status:
                    statuses[repo] = status
        billing: MutableMapping[str, ActionsBillingStatus] = {}
        if self._actions_billing_user:
            billing_status = self._fetch_actions_billing(scope=("user", None))
            if billing_status:
                billing[billing_status.scope] = billing_status
        if self._actions_billing_org:
            billing_status = self._fetch_actions_billing(
                scope=("org", self._actions_billing_org)
            )
            if billing_status:
                billing[billing_status.scope] = billing_status

        with self._lock:
            self._actions_status = statuses
            self._actions_billing = billing
            self._last_actions_fetch_ts = now

    def _fetch_actions_repo(self, repo: str) -> Optional[ActionsRepoStatus]:
        path = f"/repos/{repo}/actions/runs"
        try:
            payload = self._client.get_json(
                path,
                params={"per_page": "20"},
                raise_for_status=False,
            )
        except Exception:
            return None
        runs = payload.get("workflow_runs") if isinstance(payload, Mapping) else None
        if not isinstance(runs, list):
            return None
        in_progress = 0
        queued = 0
        latest_status: Optional[str] = None
        latest_conclusion: Optional[str] = None
        latest_updated: Optional[str] = None
        for run in runs:
            status = run.get("status")
            conclusion = run.get("conclusion")
            if status == "in_progress":
                in_progress += 1
            elif status == "queued":
                queued += 1
            if latest_status is None:
                latest_status = status
                latest_conclusion = conclusion
                latest_updated = run.get("updated_at")
        return ActionsRepoStatus(
            repo=repo,
            in_progress=in_progress,
            queued=queued,
            latest_status=latest_status,
            latest_conclusion=latest_conclusion,
            latest_updated=latest_updated,
        )

    def _fetch_actions_billing(
        self, *, scope: tuple[str, Optional[str]]
    ) -> Optional[ActionsBillingStatus]:
        target, value = scope
        if target == "user":
            path = "/user/settings/billing/actions"
            scope_label = "user"
        elif target == "org" and value:
            path = f"/orgs/{value}/settings/billing/actions"
            scope_label = f"org:{value}"
        else:
            return None
        try:
            payload = self._client.get_json(path, raise_for_status=False)
        except Exception:
            return None
        if not isinstance(payload, Mapping):
            return None
        return ActionsBillingStatus(
            scope=scope_label,
            total_minutes_used=_safe_int(payload.get("total_minutes_used")),
            included_minutes=_safe_int(payload.get("included_minutes")),
            total_paid_minutes_used=_safe_int(payload.get("total_paid_minutes_used")),
        )

    # ------------------------------------------------------------------
    # Input handling

    def _start_input_listener(self) -> None:
        if not self._keybindings_enabled:
            return
        if not sys.stdin.isatty():
            return
        if termios is None or tty is None:
            return
        if self._input_thread and self._input_thread.is_alive():
            return
        self._input_stop_event.clear()
        self._input_thread = threading.Thread(
            target=self._input_loop, name="GratekeeperInput", daemon=True
        )
        self._input_thread.start()

    def _stop_input_listener(self) -> None:
        if not self._input_thread:
            return
        self._input_stop_event.set()
        self._input_thread.join(timeout=0.2)
        self._input_thread = None

    def _input_loop(self) -> None:  # pragma: no cover - requires interactive TTY
        assert termios is not None and tty is not None
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._input_stop_event.is_set():
                readable, _, _ = select.select([fd], [], [], 0.1)
                if fd in readable:
                    key = sys.stdin.read(1)
                    if key:
                        self._handle_keypress(key)
        except Exception:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _handle_keypress(self, key: str) -> None:
        key = key.lower()
        if key == "u":
            self._adjust_refresh_speed(faster=True)
            self._manual_fetch_event.set()
            self._trigger_wakeup()
        elif key == "d":
            self._adjust_refresh_speed(faster=False)
            self._manual_fetch_event.set()
            self._trigger_wakeup()
        elif key == "r":
            self._manual_fetch_event.set()
            self._trigger_wakeup()

    def _adjust_refresh_speed(self, *, faster: bool) -> None:
        with self._lock:
            current = self._refresh_interval
            if faster:
                new_value = max(current / 2, self._min_refresh_interval)
            else:
                new_value = min(current * 2, self._max_refresh_interval)
            self._refresh_interval = new_value

    def _current_refresh_interval(self) -> float:
        with self._lock:
            return self._refresh_interval

    def _refresh_rate_value(self) -> float:
        interval = self._current_refresh_interval()
        if interval <= 0:
            return 5.0
        return max(1.0 / interval, 0.2)

    def _wait_for_next_iteration(self, deadline: float) -> None:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            triggered = self._wakeup_event.wait(timeout=remaining)
            if triggered:
                self._wakeup_event.clear()
                return

    def _trigger_wakeup(self) -> None:
        self._wakeup_event.set()


def _fmt_int(value: Optional[int]) -> str:
    return f"{value:,}" if value is not None else "—"


def _fmt_usage(resource: RateLimitResource) -> str:
    pct = resource.usage_percent
    if pct is None:
        return "—"
    used = resource.used or 0
    return f"{used:,} used ({pct:.1f}%)"


def _fmt_delta(reset_ts: Optional[int], now: datetime) -> str:
    if reset_ts is None:
        return "—"
    delta_seconds = reset_ts - int(now.timestamp())
    if delta_seconds <= 0:
        return "reset"
    hours, remainder = divmod(delta_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes:02}m"
    if minutes > 0:
        return f"{minutes}m {seconds:02}s"
    return f"{seconds}s"


def _fmt_reset_time(reset_ts: Optional[int]) -> str:
    if reset_ts is None:
        return "—"
    dt = datetime.fromtimestamp(reset_ts, timezone.utc)
    return dt.strftime("%H:%M:%S")


def _resource_from_snapshot(bucket: str, state: BucketState) -> RateLimitResource:
    return RateLimitResource(
        bucket=bucket,
        limit=state.limit,
        remaining=state.remaining,
        reset_ts=state.reset_ts,
    )


def _coerce_resources(
    resources: Mapping[str, Mapping[str, object]],
) -> MutableMapping[str, RateLimitResource]:
    parsed: MutableMapping[str, RateLimitResource] = {}
    for bucket, data in resources.items():
        limit = _safe_int(data.get("limit"))
        remaining = _safe_int(data.get("remaining"))
        reset = _safe_int(data.get("reset"))
        parsed[bucket] = RateLimitResource(
            bucket=bucket, limit=limit, remaining=remaining, reset_ts=reset
        )
    return parsed


def _safe_int(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _launch_in_tmux(argv: list[str]) -> bool:
    if shutil.which("tmux") is None:
        return False
    if not os.environ.get("TMUX"):
        return False
    filtered_args: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--tmux-pane":
            continue
        filtered_args.append(arg)
    command = ["gratekeeper-dashboard", *filtered_args]
    command_str = shlex.join(command)
    try:
        result = subprocess.run(
            ["tmux", "split-window", "-v", "-p", "40", command_str],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Display a live GitHub rate-limit dashboard."
    )
    parser.add_argument(
        "--token", help="GitHub token (defaults to $GITHUB_TOKEN)", default=None
    )
    parser.add_argument("--base-url", help="GitHub API base URL", default=None)
    parser.add_argument(
        "--buckets", nargs="*", help="Buckets to display (default: all)", default=None
    )
    parser.add_argument(
        "--refresh", type=float, default=60.0, help="UI refresh interval in seconds"
    )
    parser.add_argument(
        "--fetch", type=float, default=60.0, help="Rate-limit fetch interval in seconds"
    )
    parser.add_argument(
        "--tmux-pane",
        action="store_true",
        help="Try to launch inside a new tmux pane (no-op if unavailable)",
    )
    parser.add_argument(
        "--actions",
        nargs="*",
        help="owner/repo identifiers to monitor for active workflows",
        default=None,
    )
    parser.add_argument(
        "--actions-billing-user",
        action="store_true",
        help="Display GitHub Actions billing minutes for the authenticated user",
    )
    parser.add_argument(
        "--actions-billing-org",
        help="Display GitHub Actions billing minutes for the given organization",
    )
    parser.add_argument(
        "--killswitch-seconds",
        type=float,
        help="Stop sending requests after this many seconds (fails closed).",
    )
    parser.add_argument(
        "--killswitch-reason",
        help="Optional note logged when the killswitch activates.",
    )
    parser.add_argument(
        "--ui",
        choices=["textual", "table"],
        default="textual",
        help="Choose between the new Textual dashboard or the legacy table view.",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help="Unix socket path for external rate-limit updates (default: $GRATEKEEPER_SOCKET or /tmp/gratekeeper.sock). Set to 'none' to disable.",
    )
    args = parser.parse_args(argv)

    if args.tmux_pane:
        if _launch_in_tmux(argv):
            return 0
        print(
            "--tmux-pane requires running inside an existing tmux session. "
            "Re-run without the flag.",
            file=sys.stderr,
        )
        return 1

    client_kwargs = {}
    if args.token:
        client_kwargs["token"] = args.token
    if args.base_url:
        client_kwargs["base_url"] = args.base_url
    socket_path = args.socket
    if socket_path is None:
        socket_path = os.getenv("GRATEKEEPER_SOCKET", DEFAULT_SOCKET_PATH)

    client = RateLimitedGitHubClient(**client_kwargs)
    if args.killswitch_seconds is not None:
        client.schedule_killswitch(
            after_seconds=max(args.killswitch_seconds, 0),
            reason=args.killswitch_reason,
        )
    try:
        if args.ui == "textual":
            from .textual_dashboard import run_textual_dashboard

            return run_textual_dashboard(
                client,
                buckets=args.buckets,
                refresh_interval=args.refresh,
                fetch_interval=args.fetch,
                actions_repos=args.actions,
                actions_billing_user=args.actions_billing_user,
                actions_billing_org=args.actions_billing_org,
                socket_path=socket_path,
            )

        dashboard = RateLimitDashboard(
            client,
            buckets=args.buckets,
            refresh_interval=args.refresh,
            fetch_interval=args.fetch,
            actions_repos=args.actions,
            actions_billing_user=args.actions_billing_user,
            actions_billing_org=args.actions_billing_org,
        )
        dashboard.run()
    finally:
        client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
