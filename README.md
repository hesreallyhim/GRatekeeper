# GRatekeeper

GRatekeeper (“GitHub Ratekeeper”) is a lightweight companion for monitoring and controlling GitHub API usage. It gives you:

- A lean, configurable dashboard (`gk-dash` / `gratekeeper-dashboard`) that shows every quota bucket, Actions activity, and billing minutes, with quick toggles and themes.
- A drop-in `requests.Session` wrapper (`RateLimitedGitHubClient`) that enforces a soft floor before you hit zero, plus optional killswitches for runaway jobs.

Why it’s different from simple retries/backoff:

- **Adaptive throttling before zero:** While other libraries offer automatic retries and backoff, Gratekeeper slows down your requests _before_ you hit the limit, making it safe to aggressively script your application's API calls without worrying about crossing over into the dreaded _secondary rate limit_, possibly encurring penalties or prolonged cooldown periods.
- **Multi-process friendly:** The dashboard/socket lets multiple scripts share live bucket state so one noisy job doesn’t blind the others.
- **Last-resort brakes:** A killswitch can stop all new requests during incidents, avoiding secondary rate limits.

## Quick start

Install (Python 3.10+):

```bash
pip install gratekeeper
```

Use the client:

```python
from gratekeeper import RateLimitedGitHubClient

client = RateLimitedGitHubClient()  # picks up $GITHUB_TOKEN
me = client.get_json("/user")
print(me["login"])
```

Run the dashboard:

```bash
gk-dash --refresh 10 --fetch 30 --actions my-org/my-repo
# prefer a simpler table? add --ui table
```

Common tasks:

- Customize HTTP: pass `token`, `base_url`, `user_agent`, or `bucket="search"` to isolate quotas.
- Disable throttling temporarily: `enable_ratekeeping=False` on the client.
- Themes/presets/bucket toggles/Actions on/off are built into the Textual UI; see the dashboard guide below.

## Security and compatibility

- The socket bridge is local-only but unauthenticated; change the path or disable with `--socket none` / `GRATEKEEPER_SOCKET=none` on shared hosts. On Windows, the bridge is disabled automatically.
- Works with REST and GraphQL; buckets stay separate unless you intentionally merge them via `bucket=...`.

## Where to go next

- Rate-limit math, soft floor, killswitch details: [EXPLAINER.md](EXPLAINER.md)
- Advanced client features (polling, listeners, socket bridge, logging, dev/demos): [ADVANCED_USAGE.md](ADVANCED_USAGE.md)
- Dashboard controls (themes, Active-only/manual presets, digit toggles, Hide Actions): [docs/dashboard-bucket-visibility.md](docs/dashboard-bucket-visibility.md)
- Local dev, tests, demos: [CONTRIBUTING.md](CONTRIBUTING.md) and `scripts/run_demo_scenarios.py`
