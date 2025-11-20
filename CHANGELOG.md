# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2025-01-19

### Added

#### Core Client
- **RateLimitedGitHubClient**: Drop-in replacement for `requests.Session` with automatic rate-limit throttling
- **LocalGratekeeper**: Proactive rate-limit management with configurable soft floor
- **Automatic token loading**: Reads `GITHUB_TOKEN` from environment
- **HTTP verb support**: GET, POST, and GraphQL request methods with `requests`-compatible API
- **Rich logging**: Color-coded output for successful calls, errors, sleeps, and rate-limit hits
- **Background refresh**: Optional periodic polling of `/rate_limit` endpoint when idle
- **Listener callbacks**: External hooks receive notifications when fresh rate-limit headers arrive

#### Dashboard
- **Textual UI**: Modern terminal dashboard with cards and gauges (default)
- **Rich table UI**: Legacy minimalist table view (via `--ui table`)
- **Real-time rate-limit monitoring**: All GitHub API quota buckets in one view
- **GitHub Actions integration**: Workflow run queue and status tracking (via `--actions`)
- **Billing minutes tracking**: Actions billing for user/org (via `--actions-billing-user`/`--actions-billing-org`)
- **Interactive controls**: Keyboard shortcuts (`u`/`d` for refresh rate, `r` to refresh, `q` to quit)
- **Socket bridge**: Unix socket server for live updates from external processes (`/tmp/gratekeeper.sock`)
- **Graceful degradation**: Automatically switches between live updates and periodic polling
- **Tmux integration**: Optional split-pane support

#### Developer Experience
- **CLI aliases**: Both `gratekeeper-dashboard` and `gk-dash` entrypoints
- **Comprehensive documentation**: README with examples, EXPLAINER with architecture details, CONTRIBUTING guide
- **Test suite**: 30+ tests with 92% coverage on core components
- **Type hints**: Full type annotations throughout codebase
- **Pre-commit hooks**: Automated linting and formatting with Ruff
- **Modern packaging**: PEP 621 compliant with hatchling build backend

### Security
- **Secondary rate-limit killswitch**: Automatic protection against abuse detection limits
- **Safe defaults**: Sensible soft-floor thresholds to prevent rate-limit penalties

### Documentation
- Installation and quick-start guides
- Detailed usage examples for client and dashboard
- Architecture documentation (EXPLAINER.md)
- Contributing guidelines
- Demo test scenarios

## [0.2.0] - 2025-01-17

### Added
- Pre-commit configuration
- Enhanced documentation
- Build system improvements

### Changed
- Package naming refinements
- Linting and code quality improvements

## [0.1.0] - 2025-01-15

### Added
- Initial release with basic rate-limiting functionality
- Core client implementation
- Basic dashboard

[1.0.0]: https://github.com/hesreallyhim/gratekeeper/releases/tag/v1.0.0
[0.2.0]: https://github.com/hesreallyhim/gratekeeper/releases/tag/v0.2.0
[0.1.0]: https://github.com/hesreallyhim/gratekeeper/releases/tag/v0.1.0
