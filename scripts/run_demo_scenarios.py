#!/usr/bin/env python3
"""CLI wrapper that runs curated demo scenarios sequentially."""

from __future__ import annotations

import argparse
import os
from dotenv import load_dotenv

from gratekeeper.demo_scenarios import run_scenarios

load_dotenv()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run curated demo scenarios.")
    parser.add_argument(
        "--python",
        help="Python interpreter for subprocess workloads (default: current).",
    )
    parser.add_argument(
        "--token",
        help="Explicit GitHub token (defaults to $GITHUB_TOKEN).",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Abort after the first failing scenario.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = args.token or os.environ.get("GITHUB_TOKEN")
    if token:
        preview = token[:4] + "…" if len(token) > 4 else token
        print(f"GITHUB_TOKEN detected (prefix: {preview})")
    else:
        print("GITHUB_TOKEN not set; token-required scenarios will be skipped")
    results = run_scenarios(
        python=args.python,
        token=token,
        fail_fast=args.fail_fast,
    )
    overall_success = True
    for result in results:
        status = "SKIPPED" if result.skipped else ("OK" if result.success else "FAIL")
        print(f"\n=== Scenario: {result.name} ===")
        print(result.description)
        print(f"Result: {status} ({result.duration:.1f}s)")
        print(result.details)
        if not result.success and not result.skipped:
            overall_success = False
            if args.fail_fast:
                break
    return 0 if overall_success else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
