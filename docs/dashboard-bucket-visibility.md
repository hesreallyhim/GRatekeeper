# Dashboard bucket visibility design

Documenting the target UX for bucket visibility controls in the Textual dashboard so implementation can stay aligned. Update this file if the design evolves.

## Goals
- Let users quickly show/hide buckets without leaving the keyboard.
- Keep visibility obvious at a glance (legend + footer hint).
- Scale from minimal (1–2 buckets) to worst-case (10–12 buckets) without clutter.
- Offer an “Active-only” preset that reduces noise when nothing is moving.

## Legend and toggles
- Legend shows every detected bucket as numbered chips (`1 core`, `2 search`, …) colored by health (green/yellow/red).
- Legend layout wraps or horizontally scrolls as needed; long names use meaningful abbreviations (e.g., `dependency_snapshots` → `dep snaps`, `dependency_sbom` → `dep sbom`) instead of ellipses.
- Keyboard: digits toggle matching bucket visibility (`1`→core). `0` maps to the 10th bucket; beyond that use the list and enter/space.
- Hidden buckets stay listed in the legend but appear dimmed/struck so they are easy to re-enable.
- Footer hint shows the preset name and count, e.g., `Preset: Manual • 9 buckets (3 hidden)`, instead of enumerating everything.
- Per-bucket visibility persists to a small config (e.g., `~/.gratekeeper_ui.json`) and is restored on launch.
- Rare buckets (e.g., `integration_manifest`, `code_scanning_upload`, `actions_runner_registration`, `source_import`) are ignored by default to keep the legend thin; users can still include them via `--buckets`.
- Last-updated/refresh/fetch meta lives in the header next to the title to reclaim sidebar space when Actions is hidden.
- Themes: basic options (`dark`, `light`, `contrast`) with a `t` keybinding to cycle and persisted in the same config.
- Overlay/help panel: `escape` closes the keys/help overlay (and any pushed screen).

## Presets and modes
- Presets: `All`, `Active-only`, `Manual` (digit toggles). Bindings `l/a/m` switch between them; `p` cycles. The current preset is shown in the footer.
- Active-only: show buckets whose `remaining` changed within the last 5 minutes (initial load sets the baseline; only subsequent changes count). Tracks last-seen remaining/timestamp per bucket.
- Active-only safety: if no buckets qualify, automatically fall back to `All` and flash a brief note so the screen is never blank.
- Manual preserves the user’s toggles; changing presets should not lose Manual selections (persist on every change).
- Actions panel can be shown/hidden via a toggle keybinding (`x`), persisting in the same config.

## Demo capture (VHS)
- `scripts/mock_rate_limit_feed.py` sends deterministic bucket updates over the socket so UI interactions are stable.
- `scripts/dashboard_demo.tape` drives the Textual dashboard with overlays: theme change (`t`), Active-only (`a`), Manual (`m`) + digits, hide Actions (`x`), keys overlay + `esc`.
- `scripts/run_dashboard_demo.sh` starts the mock feed and runs the VHS tape in one shot, producing `demo.cast` (render to GIF/MP4 via `vhs render demo.cast --output demo.gif`).
- Usage: `make demo-ui` to record, `make demo-ui-gif` to record and render (requires `vhs` in PATH).

## Cards and layout
- Cards stay mountable for all known buckets; visibility toggles hide/show rather than destroying nodes to keep order stable.
- Collapsible cards: header with name + remaining/limit + reset summary; enter/space collapses/expands. Collapsed cards shrink to a single line.
- Grid adapts to width: default 1 column for narrow/two-bucket views; 2–3 columns when width is generous. Keeps small sets from feeling empty.
- Sidebar (meta/actions) remains visible to give balance even when few buckets show.

## Edge cases and sizing
- Worst case (10–12 buckets): legend wraps/scrolls; footer remains short; cards collapse to keep vertical footprint reasonable.
- Minimal case (Active-only with 1–2 buckets): single-column cards plus sidebar; no large blank areas; collapsed headers remain visible.
- Hidden bucket turns critical (yellow/red): highlight its legend chip and optionally auto-reveal or toast so issues are not missed while hidden.

## Persistence
- Store: per-bucket visibility, last-used preset, and maybe compact/grid mode in a simple JSON file under the user’s home (e.g., `~/.gratekeeper_ui.json`).
- Load on mount; write on change and on clean exit.

## Open decisions
- Exact keybinding for “bucket mode” beyond 10 buckets (`b` + digit vs. `g` + digit).
- Whether to auto-reveal hidden buckets on critical state vs. only highlight legend.
