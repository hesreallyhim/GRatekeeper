# Rate Limit Explainer

This document captures how GitHub’s public REST/GraphQL limits are structured,
how they differ for anonymous vs authenticated traffic, and how Gratekeeper’s
`LocalGratekeeper` uses that information to keep workloads out of trouble.

## Tokens vs logins

- GitHub’s rate limits are enforced per credential: a PAT, GitHub App
  installation token, Actions GITHUB_TOKEN, etc. If you issue multiple tokens
  that map to the same login, each token gets its own independent `core`
  bucket.
- Anonymous requests (no token) are keyed by IP address and share the much
  smaller unauthenticated quotas. GitHub currently caps the unauthenticated
  `core` bucket at 60 requests/hour and the Search API at roughly 10 requests
  per minute.
- GitHub Apps may additionally have “secondary rate limits” that clamp bursts
  faster than the documented caps. Those show up as 403 (abuse detection) or
  429 responses with custom headers (`Retry-After`, `X-RateLimit-Resource`, etc.).

## Bucket cheat sheet

| Bucket        | Sample endpoints                               | Hourly limit (auth) | Short-term limit | Notes |
|---------------|------------------------------------------------|---------------------|------------------|-------|
| `core`        | `/user`, `/repos/*`, `/orgs/*`                 | 5000/hr             | GitHub applies abuse heuristics; no documented per-minute cap, but bursts can trigger temporary slowdowns. |
| `search`      | `/search/*`                                    | 30/min * 60/hr      | 30/min (auth) / ~10/min (no auth) | Documented in the [Search API rate limit](https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28#rate-limit). |
| `graphql`     | `/graphql`                                     | 5000 cost points/hr | Cost shipped in response payload; no explicit per-minute doc, but abuse detection still applies. |
| `actions`     | `/repos/*/actions`, billing endpoints          | 1000/hr (GitHub-managed) | Not explicitly documented. |
| `code_scanning` / others | specialty services                  | Varies              | Varies           | Always check `/rate_limit` to discover the buckets available to your token. |

> **Tip:** `/rate_limit` returns the live limit/remaining/reset triplet for
> each bucket. Gratekeeper calls it automatically when idle.

### `/rate_limit` caveats

GitHub documents that `GET /rate_limit` does **not** decrement any bucket, but
they also note (in support posts and the GitHub Community forum) that excessive
polling can be treated as abusive traffic. Practically, the endpoint still goes
through the same abuse-detection layer as other REST calls:

- If you hammer `/rate_limit` multiple times per second, you may see temporary
  403 responses with `X-RateLimit-Remaining: 0` even though no bucket was truly
  exhausted.
- Secondary rate limits can trigger if your app interleaves high-frequency
  `/rate_limit` calls with other endpoints, because all of that traffic flows
  through the same front-end.

Gratekeeper’s dashboard polls `/rate_limit` opportunistically (default once per
minute when idle) and resets the timer whenever “real” requests provide fresh
headers. This keeps us well below any abuse thresholds while still giving you
near-live telemetry.

## How rate-limit headers are interpreted

Every REST and GraphQL response includes:

- `X-RateLimit-Limit`: the total requests/cost available in the current hour.
- `X-RateLimit-Remaining`: the requests/cost left before exhaustion.
- `X-RateLimit-Reset`: the UNIX timestamp (UTC seconds) when the bucket refills.

Gratekeeper passes those headers into `LocalGratekeeper.after_response()` so the
latest state is cached per bucket. The same data powers the dashboard.

## LocalGratekeeper calculations

The rate keeper uses a “soft floor” to slow down before you hit zero:

1. `soft_floor = max(limit * soft_floor_fraction, soft_floor_min)`. Defaults:
   20% of the bucket with a floor of 10.
2. Before every request, the bucket is checked:
   - If `remaining` > soft floor, decrement locally and continue.
   - If `remaining` <= soft floor and `reset_ts` is in the future, compute the
     sleep: `(reset_ts - now) + safety_buffer_seconds` (default 5 seconds).
     This is why you might see ~60-second pauses even though technically you
     could have squeezed in a few more Search API calls; the soft floor ensures
     you never touch zero.
3. If GitHub returns `remaining=0` or a true 429/403, those headers immediately
   propagate to the listeners and the dashboard, and the client will pause
   until the reset time expires.

You can tune the behavior via `LocalGratekeeper` constructor parameters:

```python
from gratekeeper import LocalGratekeeper

keeper = LocalGratekeeper(
    soft_floor_fraction=0.1,
    soft_floor_min=5,
    safety_buffer_seconds=2,
)
```

Lowering the floor/buffer pushes more throughput at the risk of hitting the
hard cap; raising them makes the client more conservative.

### Secondary rate limits

GitHub enforces a “secondary rate limit” to catch abusive bursts that would not
necessarily violate the primary per-hour quota. When triggered, responses come
back with HTTP 403 (sometimes 429) and headers such as:

- `Retry-After`: how many seconds to wait before retrying.
- `X-RateLimit-Resource`: the bucket GitHub considers affected (`core`, `search`, etc.).
- `X-RateLimit-Used` / `X-RateLimit-Remaining`: often set to `limit`/`0` temporarily.

This mechanism is intentionally opaque; the docs simply advise “avoid sending
many requests in a short period of time” and space out bursts. Factors known to
trigger it include:

- Rapid-fire POST/PUT/PATCH/DELETE calls (write-heavy workloads).
- Large fan-out across many endpoints without per-request delay.
- Repeated `/rate_limit` or `/graphql` calls interleaved with other traffic.

Gratekeeper mitigates this by:

1. Sleeping before buckets reach zero (soft floor).
2. Spreading background `/rate_limit` polls apart.
3. Logging any 403/429 responses prominently so you can adjust scripts quickly.

If you routinely bump into the secondary limit, lower the `soft_floor_fraction`
and add per-request sleeps in your own code—or consider batching operations via
GraphQL where practical.

## GraphQL quirks

GraphQL limits are cost-based:

- Each query response includes `{"rateLimit": {"cost": X, "remaining": Y}}` if
  you request it, and the HTTP headers still surface `X-RateLimit-*`.
- The default allowance is 5000 points/hour per token. Trivial queries cost 1,
  more complex ones can cost hundreds.
- Gratekeeper assigns GraphQL calls to the `graphql` bucket by default so REST
  and GraphQL usage stay isolated. Use `bucket="core"` if you explicitly want
  to blend the counters (e.g., self-imposed global throttling).

## Running your own experiments

Use `scripts/rate_limit_probe.py` to see how raw `requests` compares to the
throttled client:

```bash
# Anonymous REST burst – expect a 403/429 after ~10 search requests
python scripts/rate_limit_probe.py --client requests --mode rest --verbose

# Authenticated mixed workload under Gratekeeper
GITHUB_TOKEN=ghp_example python scripts/rate_limit_probe.py \
  --client gratekeeper --mode mixed --verbose
```

The script prints per-request latencies plus a JSON summary (requests/minute,
reset timestamps, etc.) so you can document the observed behavior in your own
environment.

## Killswitch guard rails

For unattended automation that prefers to run without throttling, Gratekeeper
exposes a “killswitch” built into `RateLimitedGitHubClient`:

```python
import time

client.schedule_killswitch(after_seconds=1800, reason="nightly batch complete")
# or schedule an absolute timestamp
client.set_killswitch(until_epoch=time.time() + 120, reason="tmp freeze")
```

- Once the killswitch triggers, every new request raises `RuntimeError` until
  the timestamp elapses (or `clear_killswitch()` is called).
- The dashboard and probe CLI expose `--killswitch-seconds/--killswitch-reason`
  flags so you can run demos or unattended monitors with a guaranteed stop time.
- This is separate from rate keeping; you can disable throttling entirely but
  still avoid runaway scripts that might otherwise trip secondary limits.
