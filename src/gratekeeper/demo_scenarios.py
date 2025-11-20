from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional
from pathlib import Path

from .client import RateLimitedGitHubClient


def _find_repo_root() -> Path:
    """Locate the repo root by looking for the probe script relative to this file."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        probe = parent / "scripts" / "rate_limit_probe.py"
        if probe.exists():
            return parent
    raise FileNotFoundError(
        "Could not locate scripts/rate_limit_probe.py relative to demo_scenarios.py"
    )


REPO_ROOT = _find_repo_root()
PROBE_SCRIPT = REPO_ROOT / "scripts" / "rate_limit_probe.py"


@dataclass
class ScenarioResult:
    name: str
    description: str
    success: bool
    duration: float
    details: str
    skipped: bool = False


@dataclass
class Scenario:
    name: str
    description: str
    requires_token: bool
    runner: Callable[["RunContext"], ScenarioResult]


class RunContext:
    def __init__(self, python: str, token: Optional[str]) -> None:
        self.python = python
        self.token = token

    @property
    def env_with_token(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.token:
            env["GITHUB_TOKEN"] = self.token
        return env

    @property
    def env_without_token(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("GITHUB_TOKEN", None)
        return env


def _run_subprocess(
    cmd: list[str], env: Optional[dict[str, str]] = None
) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _skip_result(name: str, description: str) -> ScenarioResult:
    return ScenarioResult(
        name=name,
        description=description,
        success=False,
        duration=0.0,
        details="GITHUB_TOKEN not set; skipping",
        skipped=True,
    )


def scenario_rest_raw(ctx: RunContext) -> ScenarioResult:
    cmd = [
        ctx.python,
        str(PROBE_SCRIPT),
        "--client",
        "requests",
        "--mode",
        "rest",
        "--max-requests",
        "20",
        "--verbose",
    ]
    start = time.time()
    code, out, err = _run_subprocess(cmd, env=ctx.env_without_token)
    duration = time.time() - start
    success = code == 0 or "limit_hit" in out
    details = out + ("\n" + err if err else "")
    return ScenarioResult(
        name="rest_raw",
        description="Unauthenticated REST burst via requests",
        success=success,
        duration=duration,
        details=details or "no output",
    )


def scenario_rest_gratekeeper(ctx: RunContext) -> ScenarioResult:
    if not ctx.token:
        return _skip_result(
            "rest_gratekeeper", "REST burst throttled by RateLimitedGitHubClient"
        )
    cmd = [
        ctx.python,
        str(PROBE_SCRIPT),
        "--client",
        "gratekeeper",
        "--mode",
        "rest",
        "--max-requests",
        "20",
        "--verbose",
    ]
    start = time.time()
    code, out, err = _run_subprocess(cmd, env=ctx.env_with_token)
    duration = time.time() - start
    success = code == 0
    details = out + ("\n" + err if err else "")
    return ScenarioResult(
        name="rest_gratekeeper",
        description="REST burst throttled by RateLimitedGitHubClient",
        success=success,
        duration=duration,
        details=details or "no output",
    )


def scenario_graphql(ctx: RunContext) -> ScenarioResult:
    if not ctx.token:
        return _skip_result("graphql_helper", "GraphQL helper with bucket snapshot")
    client = RateLimitedGitHubClient(token=ctx.token)
    query = """
        query Demo($login: String!) {
            user(login: $login) {
                login
                createdAt
            }
        }
    """
    start = time.time()
    try:
        payload = client.graphql_json(query, variables={"login": "octocat"})
        snapshot = client.rate_limit_snapshot("graphql")
        details = json.dumps(
            {"data": payload.get("data"), "bucket": snapshot.__dict__},
            indent=2,
        )
        success = True
    except Exception as exc:  # pragma: no cover - network dependent
        details = f"GraphQL query failed: {exc}"
        success = False
    finally:
        duration = time.time() - start
        client.close()
    return ScenarioResult(
        name="graphql_helper",
        description="GraphQL helper with bucket snapshot",
        success=success,
        duration=duration,
        details=details,
    )


def scenario_post_markdown(ctx: RunContext) -> ScenarioResult:
    if not ctx.token:
        return _skip_result("post_markdown", "POST helper rendering markdown")
    client = RateLimitedGitHubClient(token=ctx.token)
    payload = {
        "text": "# Gratekeeper Demo\\n\\nRendered via POST /markdown.",
        "mode": "gfm",
    }
    start = time.time()
    try:
        response = client.post("/markdown", json=payload, raise_for_status=True)
        details = response.text[:200]
        success = response.status_code == 200
    except Exception as exc:  # pragma: no cover
        success = False
        details = f"POST /markdown failed: {exc}"
    finally:
        duration = time.time() - start
        client.close()
    return ScenarioResult(
        name="post_markdown",
        description="POST helper rendering markdown",
        success=success,
        duration=duration,
        details=details,
    )


def scenario_killswitch(ctx: RunContext) -> ScenarioResult:
    if not ctx.token:
        return _skip_result("killswitch_probe", "Mixed workload with killswitch")
    cmd = [
        ctx.python,
        str(PROBE_SCRIPT),
        "--client",
        "gratekeeper",
        "--mode",
        "mixed",
        "--max-requests",
        "100",
        "--killswitch-seconds",
        "30",
        "--verbose",
    ]
    start = time.time()
    code, out, err = _run_subprocess(cmd, env=ctx.env_with_token)
    duration = time.time() - start
    success = code == 0
    details = out + ("\n" + err if err else "")
    return ScenarioResult(
        name="killswitch_probe",
        description="Mixed workload reaching killswitch",
        success=success,
        duration=duration,
        details=details or "no output",
    )


SCENARIOS = [
    Scenario(
        name="rest_raw",
        description="Unauthenticated REST burst via requests",
        requires_token=False,
        runner=scenario_rest_raw,
    ),
    Scenario(
        name="rest_gratekeeper",
        description="REST burst throttled by RateLimitedGitHubClient",
        requires_token=True,
        runner=scenario_rest_gratekeeper,
    ),
    Scenario(
        name="graphql_helper",
        description="GraphQL helper with bucket snapshot",
        requires_token=True,
        runner=scenario_graphql,
    ),
    Scenario(
        name="post_markdown",
        description="POST helper rendering markdown",
        requires_token=True,
        runner=scenario_post_markdown,
    ),
    Scenario(
        name="killswitch_probe",
        description="Mixed workload with killswitch",
        requires_token=True,
        runner=scenario_killswitch,
    ),
]


def run_scenarios(
    *,
    python: Optional[str] = None,
    token: Optional[str] = None,
    fail_fast: bool = False,
) -> list[ScenarioResult]:
    python = python or os.environ.get("PYTHON") or sys.executable
    token = token or os.environ.get("GITHUB_TOKEN")
    ctx = RunContext(python=python, token=token)
    results: list[ScenarioResult] = []
    for scenario in SCENARIOS:
        result = scenario.runner(ctx)
        results.append(result)
        if fail_fast and not result.success and not result.skipped:
            break
    return results
