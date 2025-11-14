# GRatekeeper

Gratekeeper is a tiny helper for personal scripts that talk to the GitHub REST
API. It wraps `requests` with a local rate keeper that watches the
`X-RateLimit-*` headers GitHub already returns and sleeps before the bucket is
fully depleted. This keeps you on the safe side of the per-authentication
limits without waiting for a 403 first.

## Installation

```bash
pip install gratekeeper
```

The package pulls in `requests` and supports Python 3.10+.

For local development (tests, typing, linters):

```bash
pip install -e .[dev]
```

## Quick start

```python
from gratekeeper import RateLimitedGitHubClient

client = RateLimitedGitHubClient()  # loads GITHUB_TOKEN automatically
me = client.get_json("/user")
print(me["login"])
```

### Authentication

- `GITHUB_TOKEN` is loaded automatically.
- You can also pass `token="ghp_..."` when constructing the client.
- Requests always send `Accept: application/vnd.github+json` and a simple
  `User-Agent`.

### Throttling behavior

The bundled `LocalRateKeeper` maintains the last seen `limit`,
`remaining`, and `reset` timestamp. Before each request it:

1. Clears its counters if the reset time has passed.
2. Computes a **soft floor** (default: 20% of the limit, minimum 10 requests).
3. Sleeps until the reset window plus a small safety buffer if `remaining`
   is already below that floor.
4. Otherwise, decrements its local `remaining` estimate and allows the
   request to proceed.

This keeps a healthy margin even if other tools consume the shared quota.

### Inspecting the budget

Call `client.rate_limit_snapshot()` to read the last known state:

```python
state = client.rate_limit_snapshot()
print(state.limit, state.remaining, state.reset_ts)
```

## Realtime dashboard

Gratekeeper ships with a tiny Rich-powered TUI that watches `GET /rate_limit`
and shows each quota bucket updating in realtime. Provide a GitHub token via
`--token` or `GITHUB_TOKEN` and run:

```bash
python -m gratekeeper.dashboard --refresh 1 --fetch 15 --actions owner/repo
# or use the installed entry point
gratekeeper-dashboard --refresh 1 --fetch 15 --actions owner/repo
# tmux users can spawn it in a split pane automatically
gratekeeper-dashboard --tmux-pane --refresh 1 --fetch 15 --actions owner/repo
```

- `--refresh` (default: 60s) controls how often the UI redraws.
- `--fetch` (default: 60s) is the fallback polling interval used only when no
  live updates have been observed recently.
- Pass `--buckets core search` to restrict the table to specific resources.
- Provide `--actions owner/repo another/repo` to include GitHub Actions stats for
  specific repositories, and add `--actions-billing-user` or
  `--actions-billing-org my-org` if you want to see consumed minutes.
- Inside the dashboard: press `u` to speed up redraws, `d` to slow down, and `r`
  to request an immediate refresh.
- Add `--tmux-pane` to attempt launching the dashboard in a new tmux split (it
  quietly does nothing when tmux isn’t available).

Press `Ctrl+C` to exit.

When you instantiate `RateLimitDashboard` inside a script that already uses
`RateLimitedGitHubClient`, the dashboard subscribes to the client's internal
rate-limit updates so each request instantly updates the table. If no requests
have fired for roughly a minute, the dashboard automatically calls
`GET /rate_limit` once to stay fresh.

## Testing

```bash
pip install -e .[dev]
python -m unittest discover -s tests -t .
```

## Logging

Gratekeeper ships with [Rich](https://github.com/Textualize/rich) and attaches a
`RichHandler` to the `gratekeeper` logger automatically (unless you already have
handlers configured).

- Green lines for successful requests.
- Yellow lines for other 4xx responses.
- Bold red lines for rate-limit hits (429 or 403 with `X-RateLimit-Remaining: 0`).
- Bold yellow warnings when the rate keeper pauses before the reset window.

If you want a different style, configure the logger yourself before importing
Gratekeeper or remove the provided handler and attach your own.

## Background polling

To stay in sync when other tools consume the same GitHub rate limit, you can ask
the client to poll `GET /rate_limit` in the background:

```python
client.start_rate_limit_polling(interval_seconds=60)
# ...
client.stop_rate_limit_polling()
```

Whenever a normal request returns fresh `X-RateLimit-*` headers, the poller’s
timer resets so periodic calls do not pile up unnecessarily.

## Roadmap

This branch focuses on a simple GET-only helper. Future iterations may add
POST/PUT helpers, richer configuration, or a small dashboard, but the primary
goal remains “keep my scripts from tripping the rate limiter.”*** End Patch
