## Contributing

Thanks for helping improve Gratekeeper. This project is preparing its first public release; aim for clean, documented changes that keep the CLI and docs consistent.

### Quick setup

```bash
pip install -e .[dev]
pre-commit install
```

- Python 3.10+ required. Use `gk-dash` in docs/examples (the long alias `gratekeeper-dashboard` remains).
- Avoid new runtime dependencies unless strictly needed; tooling belongs in the `dev` extra.

### Day-to-day commands

```bash
# Format + lint + tests via hooks
pre-commit run --all-files

# Unit tests
make test

# Coverage (prints report)
make coverage
```

### Code style and expectations

- Formatting: Black; linting: Ruff (see `.pre-commit-config.yml`).
- Tests live under `tests/`; prefer adding coverage for new behavior.
- The rate keeper is named `LocalGratekeeper` (no legacy alias shipped).
- Keep PRs focused; explain behavior and rationale in the description.

### UI and socket bridge

- Default UI is the Textual dashboard; `--ui table` keeps the legacy view.
- The socket bridge is Unix-only and unauthenticated. On multi-tenant hosts, set a restrictive socket path or disable it with `--socket none` / `GRATEKEEPER_SOCKET=none`. On Windows, the bridge is disabled automatically.

### Documentation

- Update README and examples when changing CLI flags, defaults, or behavior.
- Note platform caveats (e.g., Unix socket bridge) and security implications (local unauthenticated socket).
