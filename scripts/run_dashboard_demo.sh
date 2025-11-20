#!/usr/bin/env bash
set -euo pipefail

# One-shot demo recorder using VHS with mock data feed.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

SOCKET_PATH="/tmp/gratekeeper.sock"

# Start mock feed in background
python "$SCRIPT_DIR/mock_rate_limit_feed.py" --socket "$SOCKET_PATH" --delay 1.0 --buckets core search graphql >/dev/null 2>&1 &
FEED_PID=$!

cleanup() {
  kill "$FEED_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Run VHS
vhs < "$SCRIPT_DIR/dashboard_demo.tape"
