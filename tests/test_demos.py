from __future__ import annotations

import os
import sys

import pytest

from gratekeeper.demo_scenarios import run_scenarios


pytestmark = pytest.mark.demo


@pytest.mark.skipif(
    not os.getenv("GRATEKEEPER_RUN_DEMOS"),
    reason="Set GRATEKEEPER_RUN_DEMOS=1 to run live demo scenarios.",
)
def test_demo_scenarios() -> None:
    token = os.getenv("GITHUB_TOKEN")
    results = run_scenarios(python=sys.executable, token=token, fail_fast=False)
    failures = [r for r in results if not r.skipped and not r.success]
    assert (
        not failures
    ), f"Demo scenarios failed: {[f'{r.name}: {r.details}' for r in failures]}"
