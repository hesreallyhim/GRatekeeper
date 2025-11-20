# Live Integration Scenarios

These exercises hit the real GitHub API so they are **opt-in** and should only
run from a personal playground token or throwaway repo. Each scenario highlights
a different facet of Gratekeeper (REST vs GraphQL, GET vs POST, throttling,
killswitch, dashboard usage) and can double as scripting for demos/VHS captures.

| Scenario | Method          | API       | Uses Gratekeeper |
|----------|-----------------|-----------|------------------|
| 1A       | GET (search)    | REST      | F                |
| 1B       | GET (search)    | REST      | T                |
| 2        | GraphQL query   | GraphQL   | T                |
| 3        | POST (gists)    | REST      | T                |
| 4        | Mixed (GET/POST)| REST/GraphQL | T             |
| 5        | Dashboard (GET) | REST      | T                |
| 6        | Mixed + Actions | REST/GraphQL | T             |

The curated “demo run” orchestrator (`scripts/run_demo_scenarios.py`) executes a
subset covering 1A, 1B, 2, 3, and 4. Use `make demo` to run them sequentially
when you need fresh recordings or sanity checks, and `make demo-test` (which
sets `GRATEKEEPER_RUN_DEMOS=1 pytest -m demo`) to incorporate them into live
regression runs.

## Prereqs

- Export `GITHUB_TOKEN` for a user or GitHub App installation with basic repo
  scope (read-only is fine).
- Respect GitHub’s terms: keep `max_requests` conservative and run these
  sparingly (e.g., before releases or when recording demos).
- Use `python -m pytest tests/test_client.py -m 'integration'` when you later
  wire these into real tests; for now they are manual flows.

## Scenario 1 — REST GET burst vs rate keeper

**Goal:** Show how the probe script behaves with/without throttling on the
Search API (which allows ~30 req/min authenticated, ~10 unauth).

```bash
# Raw requests (expect 403/429 within ~30 calls)
python scripts/rate_limit_probe.py --client requests --mode rest --max-requests 40 --verbose

# Same workload through Gratekeeper, watch it slow itself down
GITHUB_TOKEN=$TOKEN python scripts/rate_limit_probe.py \
  --client gratekeeper --mode rest --max-requests 40 --verbose
```

Capture: JSON output and log snippets showing sleeps + remaining headers.

## Scenario 2 — GraphQL helper

**Goal:** Demonstrate `RateLimitedGitHubClient.graphql_json()` plus bucket
tracking. Use the probe’s `--mode graphql` or a short script:

```python
from gratekeeper import RateLimitedGitHubClient

client = RateLimitedGitHubClient()
query = "query($login:String!){user(login:$login){login repositories(first:5){nodes{name}}}}"
payload = client.graphql_json(query, variables={"login": "octocat"})
print(payload["data"]["user"]["repositories"]["nodes"])
print(client.rate_limit_snapshot("graphql"))
```

Record: output plus log line with `bucket=graphql`.

## Scenario 3 — POST + throttling

**Goal:** Use `client.post()` against a harmless endpoint (e.g., create/delete a
gist) and show bucket isolation via `bucket="core"` vs `bucket="actions"`.

Steps:

1. `client.post("https://api.github.com/gists", json={...}, bucket="core")`
2. `client.rate_limit_snapshot("core")` to prove counters change.
3. Cleanup gist to avoid clutter.

## Scenario 4 — Killswitch safety net

**Goal:** Show the hard stop triggered mid-run.

```bash
GITHUB_TOKEN=$TOKEN python scripts/rate_limit_probe.py \
  --client gratekeeper --mode mixed --max-requests 200 \
  --killswitch-seconds 30 --verbose
```

Observe: near the 30-second mark the client raises `RuntimeError`. Use this in
VHS to emphasize unattended safeguards.

## Scenario 5 — Dashboard observability

**Goal:** Run the TUI with background workloads to show live bucket updates.

1. `gratekeeper-dashboard --refresh 5 --fetch 30 --killswitch-seconds 1800`.
2. In another pane run scenario 1 or 2 to feed rate-limit events.
3. Capture the bucket table updating plus the Actions section if relevant.

## Scenario 6 — Mixed workload + Actions polling

**Goal:** Highlight GraphQL, REST, and Actions in one view.

```bash
GITHUB_TOKEN=$TOKEN python scripts/rate_limit_probe.py \
  --client gratekeeper --mode mixed --max-requests 60 --sleep 1 \
  --actions repo1/repo2 (optional future flag)
```

While it runs, keep the dashboard open to show both rate and Actions tables.

---

Once you’re ready to formalize these as integration tests:

1. Introduce a `pytest` marker (e.g., `@pytest.mark.integration`) and use
   `@pytest.mark.skipif(not os.getenv("GITHUB_TOKEN"), ...)`.
2. Derive fixtures for the shared `RateLimitedGitHubClient`.
3. Gate CI so these only run manually (`make test-integration`).

For demos/VHS captures, the commands above are ready to paste into scripts. Keep
the workloads short and annotate the recordings with the relevant bucket state
so viewers see why throttling matters.
