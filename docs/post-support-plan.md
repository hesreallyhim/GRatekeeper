# Plan: POST Support & Drop-In Compatibility

## Goals

1. Expose a `post` helper that mirrors `requests.Session.post`, allowing users to
   write `client.post(...)` with the same arguments and return value they would
   get from `requests`.
2. Refactor the client internals so all HTTP verbs share the same throttling and
   logging code path, making it trivial to add `graphql()`/other verbs next.
3. Document the new surface area so the README can truthfully describe the
   library as a drop-in replacement for `requests` that adds observability and
   proactive rate limiting.

Non-goal: building a full GitHub-specific high-level SDK. The client should stay
thin—just a `requests` wrapper with throttling.

## API design

### RateLimitedGitHubClient.post

```python
def post(
    self,
    path: str,
    *,
    params: Optional[Mapping[str, str]] = None,
    data: Any | None = None,
    json: Any | None = None,
    files: Optional[Mapping[str, IO[Any]]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
    bucket: str = "core",
    raise_for_status: bool = True,
    **request_kwargs: Any,
) -> requests.Response:
    ...
```

- Mirrors the common subset of `requests.post` keyword arguments.
- `**request_kwargs` keeps the method flexible (e.g., `stream=True`, `auth=...`).
- Returns the raw `Response`, preserving drop-in expectations.
- Rate keeper + listeners operate exactly like `.get`.

### Private helper

Introduce `_request` (or `_send_request`) that encapsulates the shared logic:

1. Resolve URL + build headers.
2. Run `before_request`.
3. Send the HTTP call via `self._session.request(...)`.
4. Run `after_response`, notify listeners, logging, and `raise_for_status`.

`get` and `post` will just forward parameters to this helper, keeping parity.

## Implementation steps

1. **Refactor `RateLimitedGitHubClient`**
   - Add `_request` helper that takes `method` and keyword arguments.
   - Update `get` to call `_request("GET", ...)`.
   - Add new `post` method forwarding to `_request("POST", ...)`.
   - Ensure logging message includes the HTTP method (e.g., `POST https://...`).
   - Keep `get_json` as a thin wrapper around `get`.

2. **Type hints**
   - Use `Mapping[str, str] | None` and `MutableMapping[str, str]` as today.
   - For `data/json/files`, accept `Any` to mirror `requests`.
   - Add `**request_kwargs: Any` to `_request` and `post`.

3. **Testing**
   - Extend `StubSession` in `tests/test_client.py` to implement `.request`
     instead of `.get`, so GET/POST share the same recording path.
   - Update existing GET tests to inspect `session.calls[0]["method"]`.
   - Add a new test verifying `post` passes through `data/json`, updates the
     rate keeper, and respects `raise_for_status`.
   - Optionally add a test showing `bucket="graphql"` works identically.

4. **Documentation**
   - Update README highlights to advertise drop-in compatibility and POST
     support.
   - Add a short “POST requests” snippet under Usage, showing how to send JSON
     payloads while keeping throttling and observability.
   - Mention that GraphQL support is next and will reuse the same plumbing.

5. **Follow-up considerations (not in this diff)**
   - Once POST is in place, add a dedicated `graphql()` helper that sets
     `bucket="graphql"`, defaults `Accept` to
     `application/vnd.github+json`, and sends POST requests to
     `/graphql`.
   - Consider exposing a generic `request` method for users who need other verbs
     (`PUT`, `PATCH`, `DELETE`).

## Validation

- Unit tests (pytest) ensure POST shares logging and rate-keeping behaviors.
- Manual smoke test: run `gratekeeper-dashboard` locally, send a POST via the
  client, and confirm the dashboard updates instantly via listener callbacks.

With this work, users can replace `requests.Session` with
`RateLimitedGitHubClient` everywhere: GETs continue to behave as before,
POSTs become first-class, and the dashboard/listener ecosystem lights up
without extra effort.
