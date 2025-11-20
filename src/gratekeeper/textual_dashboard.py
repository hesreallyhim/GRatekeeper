from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, MutableMapping, Optional

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Footer, Header, Static
from textual.worker import Worker

from .client import RateLimitedGitHubClient
from .dashboard import (
    ActionsBillingStatus,
    ActionsRepoStatus,
    RateLimitResource,
    _coerce_resources,
    _fmt_delta,
    _fmt_int,
    _resource_from_snapshot,
)
from .bridge import DEFAULT_SOCKET_PATH, RateLimitUpdate
from .ratekeeper import BucketState


class _RateLimitSocketServer:
    """Async Unix socket server that pushes updates into the dashboard."""

    def __init__(
        self,
        path: Optional[str],
        handler: Callable[[RateLimitUpdate], None],
    ) -> None:
        self._path = path
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if not self._path:
            return
        try:
            if os.path.exists(self._path):
                os.remove(self._path)
        except OSError:
            pass
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client, path=self._path
            )
        except Exception as exc:  # pragma: no cover - OS dependent
            logger.warning("Failed to start socket bridge at %s: %s", self._path, exc)
            self._server = None

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line.decode("utf-8"))
                    bucket = payload.get("bucket")
                    if not bucket:
                        continue
                    update = RateLimitUpdate(
                        bucket=str(bucket),
                        limit=_coerce_int(payload.get("limit")),
                        remaining=_coerce_int(payload.get("remaining")),
                        reset_ts=_coerce_int(payload.get("reset_ts")),
                    )
                    self._handler(update)
                except Exception:
                    continue
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


logger = logging.getLogger("gratekeeper.textual-dashboard")

CONFIG_PATH = Path.home() / ".gratekeeper_ui.json"
ACTIVITY_WINDOW_SECONDS = 300.0  # five minutes
IGNORED_BUCKETS = {
    # Rare/long-tail buckets; still viewable via --buckets override.
    "integration_manifest",
    "code_scanning_upload",
    "actions_runner_registration",
    "source_import",
}
THEMES = {
    "dark": {
        "bg": "#0c1118",
        "fg": "#e6edf3",
        "panel_bg": "#0f1622",
        "hero_bg": "#101826",
        "border": "#7bdff2",
        "muted": "#9fb3c8",
    },
    "light": {
        "bg": "#f6f8fa",
        "fg": "#2c2f33",
        "panel_bg": "#ffffff",
        "hero_bg": "#e5f2ff",
        "border": "#5b9bff",
        "muted": "#6b7280",
    },
    "contrast": {
        "bg": "#000000",
        "fg": "#ffffff",
        "panel_bg": "#0f0f0f",
        "hero_bg": "#111111",
        "border": "#00e0ff",
        "muted": "#9ca3af",
    },
}


@dataclass
class DashboardMeta:
    last_update_ts: Optional[float] = None
    last_update_source: Optional[str] = None
    last_error: Optional[str] = None


class BucketCard(Static):
    """Small Rich panel that shows a single bucket."""

    resource: RateLimitResource | None = None

    def __init__(self, bucket: str) -> None:
        super().__init__(id=f"bucket-{bucket}")
        self.bucket = bucket

    def set_resource(self, resource: Optional[RateLimitResource]) -> None:
        self.resource = resource
        self.refresh()

    def render(self) -> Panel:
        resource = self.resource
        body: Text | Group
        if not resource:
            body = Text("Waiting for data…", style="dim")
            return Panel(body, title=self.bucket, border_style="cyan")

        pct = resource.usage_percent or 0.0
        used = resource.used or 0
        limit = resource.limit or 0
        remaining = resource.remaining
        tone = _bucket_tone(resource)

        bar = _usage_bar(pct, tone)
        reset_at = _fmt_reset_time(resource.reset_ts)
        resets_in = _fmt_delta(resource.reset_ts, datetime.now(timezone.utc))

        body = Group(
            Text.from_markup(bar),
            Text.assemble(
                (" used ", "dim"),
                (f"{used:,}", tone),
                (" of ", "dim"),
                (f"{limit:,}", "bold"),
            ),
            Text.assemble(
                ("remaining ", "dim"),
                (_fmt_int(remaining), "bold"),
                "  ",
                (resets_in, "italic"),
            ),
            Text(reset_at, style="dim"),
        )
        return Panel(
            body,
            title=f"[b]{self.bucket}[/]",
            border_style=tone,
            padding=(0, 1),
        )


class BucketLegend(Static):
    """Compact legend that shows bucket digits and visibility."""

    def __init__(self) -> None:
        super().__init__(id="bucket-legend")
        self._buckets: tuple[str, ...] = tuple()
        self._visible: MutableMapping[str, bool] = {}
        self._tones: MutableMapping[str, str] = {}
        self._recent: MutableMapping[str, bool] = {}
        self._preset: str = "all"
        self._note: Optional[str] = None

    def update_state(
        self,
        buckets: tuple[str, ...],
        *,
        visible: Mapping[str, bool],
        tones: Mapping[str, str],
        recent: Mapping[str, bool],
        preset: str,
        note: Optional[str],
    ) -> None:
        self._buckets = buckets
        self._visible = dict(visible)
        self._tones = dict(tones)
        self._recent = dict(recent)
        self._preset = preset
        self._note = note
        self.refresh()

    def render(self) -> Panel:
        text = Text()
        for idx, bucket in enumerate(self._buckets, start=1):
            label = str(idx if idx < 10 else 0)
            tone = self._tones.get(bucket, "cyan")
            visible = self._visible.get(bucket, True)
            recent = self._recent.get(bucket, False)
            bullet = f"{label} {_abbrev_bucket(bucket)}"
            if not visible:
                text.append(f"{bullet} ", style="dim strike")
            else:
                text.append(f"{bullet} ", style=tone)
            if recent:
                text.append("• ", style=tone)
            else:
                text.append("  ", style="dim")
        footer = Text()
        footer.append(f"Preset: {self._preset}", style="bold cyan")
        footer.append(f" • buckets: {len(self._buckets)}", style="dim")
        hidden = sum(1 for b in self._buckets if not self._visible.get(b, True))
        if hidden:
            footer.append(f" ({hidden} hidden)", style="dim")
        if self._note:
            footer.append(f" • {self._note}", style="italic")
        body = Group(text, footer)
        return Panel(body, title="Buckets", border_style="cyan", padding=(0, 1))


class ActionsPanel(Static):
    statuses: MutableMapping[str, ActionsRepoStatus]
    billing: MutableMapping[str, ActionsBillingStatus]

    def __init__(self) -> None:
        super().__init__(id="actions")
        self.statuses = {}
        self.billing = {}

    def update_data(
        self,
        *,
        statuses: Mapping[str, ActionsRepoStatus],
        billing: Mapping[str, ActionsBillingStatus],
    ) -> None:
        self.statuses = dict(statuses)
        self.billing = dict(billing)
        self.refresh()

    def render(self) -> Panel:
        statuses = self.statuses
        billing = self.billing
        if not statuses and not billing:
            return Panel("Actions: N/A", border_style="cyan", padding=(1, 1))

        table = Table.grid(padding=(0, 1))
        table.expand = True
        table.add_column("Repo", style="bold", justify="left")
        table.add_column("Active", justify="right")
        table.add_column("Queued", justify="right")
        table.add_column("Latest", justify="left")

        for repo in sorted(statuses):
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
            table.add_row("", "", "", "")  # spacer
            table.add_row("[b]Billing[/]", "", "", "")
            for scope in sorted(billing):
                data = billing[scope]
                used = (
                    f"{data.total_minutes_used}m / {data.included_minutes}m"
                    if data.total_minutes_used is not None
                    else "—"
                )
                table.add_row(scope, "—", "—", used)

        return Panel(table, title="Actions", border_style="cyan", padding=(1, 1))


class RateLimitTextualApp(App):
    """Textual UI for rate limits."""

    CSS = ""  # filled from theme on mount

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_now", "Refresh"),
        ("u", "faster", "Faster"),
        ("d", "slower", "Slower"),
        ("l", "preset_all", "All buckets"),
        ("a", "preset_active", "Active-only"),
        ("m", "preset_manual", "Manual toggle mode"),
        ("p", "cycle_preset", "Cycle presets"),
        ("x", "toggle_actions_panel", "Toggle Actions panel"),
        ("t", "cycle_theme", "Theme"),
        ("escape", "dismiss_overlay", "Close panel"),
    ]

    def __init__(
        self,
        client: RateLimitedGitHubClient,
        *,
        buckets: Iterable[str] | None = None,
        refresh_interval: float = 60.0,
        fetch_interval: float = 60.0,
        actions_repos: Iterable[str] | None = None,
        actions_billing_user: bool = False,
        actions_billing_org: Optional[str] = None,
        socket_path: Optional[str] = DEFAULT_SOCKET_PATH,
    ) -> None:
        super().__init__()
        self._client = client
        self._buckets = tuple(buckets) if buckets else None
        self._refresh_interval = max(refresh_interval, 0.25)
        self._fetch_interval = max(fetch_interval, 1.0)
        self._min_refresh_interval = 0.25
        self._max_refresh_interval = 300.0

        self._actions_repos = tuple(actions_repos) if actions_repos else tuple()
        self._actions_billing_user = actions_billing_user
        self._actions_billing_org = actions_billing_org

        self._socket_path = socket_path
        self._bridge_server: _RateLimitSocketServer | None = None

        self._resources: MutableMapping[str, RateLimitResource] = {}
        self._actions_status: MutableMapping[str, ActionsRepoStatus] = {}
        self._actions_billing: MutableMapping[str, ActionsBillingStatus] = {}
        self._meta = DashboardMeta()
        self._bucket_cards: MutableMapping[str, BucketCard] = {}
        self._manual_visibility: MutableMapping[str, bool] = {}
        self._last_remaining: MutableMapping[str, Optional[int]] = {}
        self._last_change_ts: MutableMapping[str, float] = {}
        self._preset: str = "all"
        self._active_note: Optional[str] = None
        self._config_path = CONFIG_PATH
        self._show_actions: bool = True
        self._theme: str = "dark"

        self._last_actions_fetch_ts: Optional[float] = None
        self._ui_timer: Timer | None = None
        self._listener_added = False
        self._poll_worker: Worker[None] | None = None
        self._poll_stop = False

        self._fetch_lock = asyncio.Lock()
        self._load_config()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="hero"):
            with Horizontal(id="hero-row"):
                yield Static(self._hero_text(), id="hero-text")
                yield Static(id="hero-meta")
            yield BucketLegend()
        with Horizontal(id="layout"):
            with VerticalScroll(id="buckets"):
                yield Grid(id="bucket-grid")
            with Vertical(id="sidebar"):
                yield ActionsPanel()
        yield Footer()

    async def on_mount(self) -> None:
        self._apply_theme_css()
        # Live listener for real-time header updates when the client is used elsewhere.
        self._client.add_rate_limit_listener(self._snapshot_listener)
        self._listener_added = True
        self._ui_timer = self.set_interval(self._refresh_interval, self._pulse_ui)
        # Background poller for periodic fetches.
        self._poll_worker = self.run_worker(
            self._poll_loop(),
            group="fetch",
            exclusive=True,
            description="rate-limit poller",
        )
        # Socket bridge for external updates.
        self._bridge_server = _RateLimitSocketServer(
            self._socket_path, self._handle_socket_update
        )
        await self._bridge_server.start()
        self._update_actions_panel()
        await self._fetch_cycle(force=True)

    async def on_unmount(self) -> None:
        self._poll_stop = True
        if self._listener_added:
            self._client.remove_rate_limit_listener(self._snapshot_listener)
        if self._bridge_server:
            await self._bridge_server.stop()

    async def _poll_loop(self) -> None:
        while not self._poll_stop:
            await self._fetch_cycle(force=True)
            await asyncio.sleep(self._fetch_interval)

    async def _fetch_cycle(self, force: bool = False) -> None:
        async with self._fetch_lock:
            await self._fetch_rate_limit(force=force)
            await self._fetch_actions(force=force)

    async def _fetch_rate_limit(self, *, force: bool = False) -> None:
        now = time.time()
        if (
            not force
            and self._meta.last_update_ts
            and now - self._meta.last_update_ts < self._fetch_interval
        ):
            return
        try:
            payload = await asyncio.to_thread(
                self._client.get_json,
                "/rate_limit",
                params=None,
                headers=None,
                raise_for_status=False,
            )
            resources_obj = (
                payload.get("resources", {}) if isinstance(payload, Mapping) else {}
            )
            parsed = _coerce_resources(resources_obj)  # type: ignore[arg-type]
            self._update_resources(parsed, source="fetch", timestamp=now)
            self._meta.last_error = None
        except Exception as exc:  # pragma: no cover - network variability
            logger.debug("fetch error: %s", exc)
            self._meta.last_error = str(exc)
            self._meta.last_update_ts = now
            self._meta.last_update_source = "error"
        self._refresh_meta()

    async def _fetch_actions(self, *, force: bool = False) -> None:
        if (
            not self._actions_repos
            and not self._actions_billing_user
            and not self._actions_billing_org
        ):
            return
        now = time.time()
        if (
            not force
            and self._last_actions_fetch_ts
            and now - self._last_actions_fetch_ts < self._fetch_interval
        ):
            return
        statuses: MutableMapping[str, ActionsRepoStatus] = {}
        for repo in self._actions_repos:
            status = await asyncio.to_thread(self._fetch_actions_repo, repo)
            if status:
                statuses[repo] = status
        billing: MutableMapping[str, ActionsBillingStatus] = {}
        if self._actions_billing_user:
            billing_status = await asyncio.to_thread(
                self._fetch_actions_billing, ("user", None)
            )
            if billing_status:
                billing[billing_status.scope] = billing_status
        if self._actions_billing_org:
            billing_status = await asyncio.to_thread(
                self._fetch_actions_billing,
                ("org", self._actions_billing_org),
            )
            if billing_status:
                billing[billing_status.scope] = billing_status
        self._actions_status = statuses
        self._actions_billing = billing
        self._last_actions_fetch_ts = now
        self._update_actions_panel()

    def _hero_text(self) -> Text:
        text = Text("GRatekeeper Dashboard", style="bold cyan")
        text.append("\nTextual dashboard", style="dim")
        return text

    def _snapshot_listener(self, bucket: str, state: BucketState) -> None:
        resource = _resource_from_snapshot(bucket, state)
        self.call_from_thread(self._apply_snapshot, resource)

    def _apply_snapshot(self, resource: RateLimitResource) -> None:
        self._resources[resource.bucket] = resource
        self._meta.last_update_ts = time.time()
        self._meta.last_update_source = "live"
        self._record_activity(resource)
        self._refresh_buckets()
        self._refresh_meta()

    def _handle_socket_update(self, update: RateLimitUpdate) -> None:
        resource = RateLimitResource(
            bucket=update.bucket,
            limit=update.limit,
            remaining=update.remaining,
            reset_ts=update.reset_ts,
        )
        self._resources[resource.bucket] = resource
        self._meta.last_update_ts = time.time()
        self._meta.last_update_source = "bridge"
        self._record_activity(resource)
        self._refresh_buckets()
        self._refresh_meta()

    def _update_resources(
        self,
        resources: Mapping[str, RateLimitResource],
        *,
        source: str,
        timestamp: float,
    ) -> None:
        self._resources = dict(resources)
        self._meta.last_update_ts = timestamp
        self._meta.last_update_source = source
        for resource in resources.values():
            self._record_activity(resource)
        self._refresh_buckets()

    def _refresh_buckets(self) -> None:
        bucket_order = self._determine_bucket_order(self._resources)
        self._sync_manual_visibility(bucket_order)
        visible_order = self._visible_bucket_order(bucket_order)
        visible_set = set(visible_order)
        grid = self.query_one("#bucket-grid", Grid)
        grid.set_class(len(visible_order) <= 2, "single-col")
        # Create any missing cards.
        for bucket in bucket_order:
            if bucket not in self._bucket_cards:
                card = BucketCard(bucket)
                self._bucket_cards[bucket] = card
                grid.mount(card)
        # Remove cards for buckets that disappeared.
        for bucket in list(self._bucket_cards.keys()):
            if bucket not in bucket_order:
                self._bucket_cards[bucket].remove()
                self._bucket_cards.pop(bucket, None)
        for bucket in bucket_order:
            card = self._bucket_cards[bucket]
            card.display = bucket in visible_set
            card.set_resource(self._resources.get(bucket))
        self._update_legend(bucket_order, visible_set)

    def _refresh_meta(self) -> None:
        dt_text = Text()
        meta = self._meta
        if meta.last_update_ts:
            label = meta.last_update_source or "update"
            dt = datetime.fromtimestamp(meta.last_update_ts, timezone.utc)
            dt_text.append(
                f"Last {label}: {dt.isoformat(timespec='seconds')}Z", style="dim"
            )
        else:
            dt_text.append("Waiting for data…", style="dim")
        dt_text.append("  •  ")
        dt_text.append(f"Refresh: {self._refresh_interval:.1f}s", style="dim")
        dt_text.append("  •  ")
        dt_text.append(f"Fetch: {self._fetch_interval:.1f}s", style="dim")
        if meta.last_error:
            dt_text.append("\n")
            dt_text.append(f"Error: {meta.last_error}", style="bold red")
        try:
            hero_meta = self.query_one("#hero-meta", Static)
            hero_meta.update(dt_text)
        except Exception:
            logger.debug("Unable to update hero meta panel", exc_info=True)

    def _update_actions_panel(self) -> None:
        panel = self.query_one(ActionsPanel)
        panel.update_data(statuses=self._actions_status, billing=self._actions_billing)
        panel.display = self._show_actions
        try:
            sidebar = self.query_one("#sidebar", Vertical)
            sidebar.display = self._show_actions
            sidebar.set_class(not self._show_actions, "compact")
        except Exception:
            pass

    def _record_activity(self, resource: RateLimitResource) -> None:
        remaining = resource.remaining
        bucket = resource.bucket
        prev = self._last_remaining.get(bucket)
        if remaining is not None and prev is not None and remaining != prev:
            self._last_change_ts[bucket] = time.time()
        self._last_remaining[bucket] = remaining

    def _pulse_ui(self) -> None:
        # Redraw countdowns and subtle timers.
        for card in self._bucket_cards.values():
            card.refresh()
        self._refresh_meta()

    async def on_key(self, event: events.Key) -> None:
        key = event.key
        if key and key.isdigit():
            idx = int(key)
            target_index = 9 if idx == 0 else idx - 1
            if self._toggle_bucket_by_index(target_index):
                event.stop()
                return
        # Let Textual handle other keys via bindings/actions.

    def _determine_bucket_order(
        self, resources: Mapping[str, RateLimitResource]
    ) -> tuple[str, ...]:
        if self._buckets:
            return self._buckets
        if resources:
            filtered = tuple(
                bucket for bucket in resources.keys() if bucket not in IGNORED_BUCKETS
            )
            return tuple(sorted(filtered))
        return tuple()

    def _sync_manual_visibility(self, bucket_order: tuple[str, ...]) -> None:
        for bucket in bucket_order:
            if bucket not in self._manual_visibility:
                self._manual_visibility[bucket] = True

    def _visible_bucket_order(self, bucket_order: tuple[str, ...]) -> tuple[str, ...]:
        self._active_note = None
        if self._preset == "manual":
            return tuple(
                bucket
                for bucket in bucket_order
                if self._manual_visibility.get(bucket, True)
            )
        if self._preset == "active":
            now = time.time()
            active = tuple(
                bucket
                for bucket in bucket_order
                if now - self._last_change_ts.get(bucket, 0) <= ACTIVITY_WINDOW_SECONDS
            )
            if active:
                return active
            self._active_note = "Active-only empty; showing all"
            return bucket_order
        return bucket_order

    def _toggle_bucket_by_index(self, index: int) -> bool:
        bucket_order = self._determine_bucket_order(self._resources)
        if index < 0 or index >= len(bucket_order):
            return False
        bucket = bucket_order[index]
        current = self._manual_visibility.get(bucket, True)
        self._manual_visibility[bucket] = not current
        self._preset = "manual"
        self._save_config()
        self._refresh_buckets()
        return True

    def _update_legend(self, bucket_order: tuple[str, ...], visible: set[str]) -> None:
        try:
            legend = self.query_one(BucketLegend)
        except Exception:
            return
        tones: MutableMapping[str, str] = {}
        recent: MutableMapping[str, bool] = {}
        now = time.time()
        for bucket in bucket_order:
            resource = self._resources.get(bucket)
            tones[bucket] = _bucket_tone(resource)
            recent[bucket] = (
                now - self._last_change_ts.get(bucket, 0) <= ACTIVITY_WINDOW_SECONDS
            )
        legend.update_state(
            bucket_order,
            visible={bucket: bucket in visible for bucket in bucket_order},
            tones=tones,
            recent=recent,
            preset=self._preset,
            note=self._active_note,
        )

    async def action_refresh_now(self) -> None:
        await self._fetch_cycle(force=True)

    async def action_faster(self) -> None:
        self._refresh_interval = max(
            self._refresh_interval / 2, self._min_refresh_interval
        )
        if self._ui_timer:
            self._ui_timer.stop()
        self._ui_timer = self.set_interval(self._refresh_interval, self._pulse_ui)
        self._refresh_meta()
        await self.action_refresh_now()

    async def action_slower(self) -> None:
        self._refresh_interval = min(
            self._refresh_interval * 2, self._max_refresh_interval
        )
        if self._ui_timer:
            self._ui_timer.stop()
        self._ui_timer = self.set_interval(self._refresh_interval, self._pulse_ui)
        self._refresh_meta()
        await self.action_refresh_now()

    async def action_preset_all(self) -> None:
        self._preset = "all"
        self._save_config()
        self._refresh_buckets()

    async def action_preset_active(self) -> None:
        self._preset = "active"
        self._save_config()
        self._refresh_buckets()

    async def action_preset_manual(self) -> None:
        self._preset = "manual"
        self._save_config()
        self._refresh_buckets()

    async def action_cycle_preset(self) -> None:
        order = ("all", "active", "manual")
        try:
            idx = order.index(self._preset)
        except ValueError:
            idx = 0
        self._preset = order[(idx + 1) % len(order)]
        self._save_config()
        self._refresh_buckets()

    async def action_toggle_actions_panel(self) -> None:
        self._show_actions = not self._show_actions
        self._save_config()
        self._update_actions_panel()

    async def action_cycle_theme(self) -> None:
        order = tuple(THEMES.keys())
        try:
            idx = order.index(self._theme)
        except ValueError:
            idx = 0
        self._theme = order[(idx + 1) % len(order)] if order else self._theme
        self._save_config()
        self._apply_theme_css()

    async def action_dismiss_overlay(self) -> None:
        # Close help/keys overlay or any pushed screen.
        if self.screen_stack:
            await self.pop_screen()

    def _load_config(self) -> None:
        try:
            data = json.loads(self._config_path.read_text())
        except Exception:
            return
        preset = data.get("preset")
        if isinstance(preset, str):
            self._preset = preset
        manual = data.get("manual_visibility")
        if isinstance(manual, dict):
            for key, value in manual.items():
                if isinstance(value, bool):
                    self._manual_visibility[str(key)] = value
        show_actions = data.get("show_actions")
        if isinstance(show_actions, bool):
            self._show_actions = show_actions
        theme = data.get("theme")
        if isinstance(theme, str) and theme in THEMES:
            self._theme = theme

    def _save_config(self) -> None:
        payload = {
            "preset": self._preset,
            "manual_visibility": self._manual_visibility,
            "show_actions": self._show_actions,
            "theme": self._theme,
        }
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(json.dumps(payload, indent=2))
        except Exception:
            logger.debug("Failed to persist UI config", exc_info=True)

    def _apply_theme_css(self) -> None:
        palette = THEMES.get(self._theme, THEMES["dark"])
        css = _build_css(palette)
        try:
            self.stylesheet.read(css)
            self.refresh_css(reload=True)
            self.refresh()
        except Exception:
            logger.debug("Failed to apply theme %s", self._theme, exc_info=True)

    # ------------------------------------------------------------------ #
    # Actions helpers (REST fetches)

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
        self, scope: tuple[str, Optional[str]]
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
            total_minutes_used=_coerce_int(payload.get("total_minutes_used")),
            included_minutes=_coerce_int(payload.get("included_minutes")),
            total_paid_minutes_used=_coerce_int(payload.get("total_paid_minutes_used")),
        )


def _usage_bar(pct: float, color: str) -> str:
    pct = max(min(pct, 100.0), 0.0)
    width = 20
    filled = int(width * (pct / 100))
    empty = width - filled
    return f"[{color}]{'█' * filled}[/][#22303f]{'·' * empty}[/] {pct:.1f}%"


def _fmt_reset_time(reset_ts: Optional[int]) -> str:
    if reset_ts is None:
        return "Reset: —"
    dt = datetime.fromtimestamp(reset_ts, timezone.utc)
    return f"Reset @ {dt.strftime('%H:%M:%S')} UTC"


def _bucket_tone(resource: Optional[RateLimitResource]) -> str:
    if not resource:
        return "cyan"
    pct = resource.usage_percent or 0.0
    tone = "turquoise2"
    if pct >= 80:
        tone = "red3"
    elif pct >= 60:
        tone = "yellow3"
    return tone


def _abbrev_bucket(bucket: str) -> str:
    known = {
        "dependency_snapshots": "dep snaps",
        "dependency_sbom": "dep sbom",
        "integration_manifest": "integ manifest",
        "code_scanning_upload": "code scan up",
        "actions_runner_registration": "runner reg",
        "source_import": "source import",
    }
    if bucket in known:
        return known[bucket]
    if len(bucket) <= 16:
        return bucket
    parts = bucket.split("_")
    if len(parts) > 1:
        trimmed = " ".join(part[:4] for part in parts)
        if len(trimmed) <= 18:
            return trimmed
    return bucket[:16]


def _build_css(palette: Mapping[str, str]) -> str:
    bg = palette.get("bg", "#0c1118")
    fg = palette.get("fg", "#e6edf3")
    panel_bg = palette.get("panel_bg", "#0f1622")
    hero_bg = palette.get("hero_bg", "#101826")
    border = palette.get("border", "#7bdff2")
    return f"""
    Screen {{
        background: {bg};
        color: {fg};
    }}
    #hero {{
        background: {hero_bg};
        padding: 1 2;
        border: round {border};
        margin: 1 1 0 1;
    }}
    #hero-text {{
        margin-bottom: 0;
    }}
    #hero-row {{
        height: auto;
        width: 1fr;
        padding-bottom: 1;
    }}
    #layout {{
        margin: 1;
        height: 1fr;
    }}
    #buckets {{
        background: {panel_bg};
        border: round {border};
        padding: 1;
        height: 1fr;
    }}
    #bucket-legend {{
        margin: 0 0 1 0;
    }}
    #bucket-grid {{
        grid-size: 2;
        grid-gutter: 1 1;
    }}
    .single-col {{
        grid-size: 1;
    }}
    #sidebar {{
        width: 36;
        min-width: 28;
        background: {panel_bg};
        border: round {border};
        padding: 1;
        height: 1fr;
    }}
    #sidebar.compact {{
        width: 28;
        min-width: 24;
    }}
    Footer, Header {{
        background: {hero_bg};
    }}
    """


def _coerce_int(value: object) -> Optional[int]:
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


def run_textual_dashboard(
    client: RateLimitedGitHubClient,
    *,
    buckets: Iterable[str] | None = None,
    refresh_interval: float = 60.0,
    fetch_interval: float = 60.0,
    actions_repos: Iterable[str] | None = None,
    actions_billing_user: bool = False,
    actions_billing_org: Optional[str] = None,
    socket_path: Optional[str] = DEFAULT_SOCKET_PATH,
) -> int:
    app = RateLimitTextualApp(
        client,
        buckets=buckets,
        refresh_interval=refresh_interval,
        fetch_interval=fetch_interval,
        actions_repos=actions_repos,
        actions_billing_user=actions_billing_user,
        actions_billing_org=actions_billing_org,
        socket_path=socket_path if socket_path not in {"none", ""} else None,
    )
    app.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    client = RateLimitedGitHubClient()
    try:
        run_textual_dashboard(client)
    finally:
        client.close()
