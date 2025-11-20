# Advanced Usage

Deep dives for features that go beyond the basics. Pair this with
[EXPLAINER.md](EXPLAINER.md) for rate-limit math and rationale.

## Observability APIs

- **Snapshots:** Grab a single bucket’s latest state.

  ```python
  state = client.rate_limit_snapshot("core")
  print(state.limit, state.remaining, state.reset_ts)
  ```

- **Listeners:** Subscribe to live updates whenever headers change.

  ```python
  def handle_update(bucket, state):
      print(f"{bucket} remaining: {state.remaining}")

  client.add_rate_limit_listener(handle_update)
  # ... later
  client.remove_rate_limit_listener(handle_update)
  ```

## Background polling

Keep a bucket fresh even when idle:

```python
client.start_rate_limit_polling(interval_seconds=90, bucket="core")
# ...
client.stop_rate_limit_polling()
```

The poller calls `GET /rate_limit` on a cadence and backs off whenever normal
requests deliver fresh headers.

## Killswitch guard

Use as a last-resort brake for runaway jobs. Full behavior and rationale live in
[EXPLAINER.md](EXPLAINER.md#killswitch).

```python
import time

client.schedule_killswitch(after_seconds=3600, reason="end-of-batch")
client.set_killswitch(until_epoch=time.time() + 60, reason="maintenance window")
```

While active, new requests raise `RuntimeError` until cleared or the timeout
expires.

## Dashboard tips

- Textual UI (`gk-dash`): buckets, Actions, presets, and visibility controls are
  documented in `docs/dashboard-bucket-visibility.md` (themes, Active-only,
  digit toggles, Actions hide/show, etc.).
- Legacy table UI: use `--ui table` if you prefer the minimal Rich table.
- Socket bridge: default `/tmp/gratekeeper.sock` accepts local updates; disable
  with `--socket none` (see README Security notes). On Windows the bridge is
  disabled automatically.

## Logging

Importing GRatekeeper attaches a Rich `RichHandler` to the `gratekeeper` logger
unless you already configured handlers. Customize logging by configuring your
own handlers before importing the library.

## Development and tests

- Local setup, linting, and test commands live in [CONTRIBUTING.md](CONTRIBUTING.md).
- Demo/testing helpers: see `scripts/run_demo_scenarios.py` and `make demo` /
  `make demo-test` for live API demos; `make demo-ui` for the Textual UI VHS
  capture.
