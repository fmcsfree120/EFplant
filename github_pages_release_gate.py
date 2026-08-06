"""EFplant GitHub Pages release gate.

Exactly one Pages deployment may be active for this repository.  GitHub's
legacy Pages workflow cancels lower-priority deployments when a later request
arrives, so callers must defer their push until the current deployment ends.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


OWNER = "fmcsfree120"
REPOSITORY = "EFplant"
PAGES_WORKFLOW_NAME = "pages build and deployment"
BUSY_EXIT_CODE = 75


def _github_token(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        cwd=repo_dir,
        check=True,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    token = values.get("password", "")
    if not token:
        raise RuntimeError("GitHub credential is unavailable")
    return token


def active_pages_runs(repo_dir: Path) -> list[dict]:
    token = _github_token(repo_dir)
    request = urllib.request.Request(
        f"https://api.github.com/repos/{OWNER}/{REPOSITORY}/actions/runs?per_page=20",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "EFplant-release-gate",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            runs = json.load(response).get("workflow_runs", [])
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub Pages status check failed: {exc}") from exc

    return [
        run for run in runs
        if run.get("name") == PAGES_WORKFLOW_NAME
        and run.get("status") in {"queued", "in_progress"}
    ]


def main() -> int:
    repo_dir = Path(__file__).resolve().parent
    try:
        active = active_pages_runs(repo_dir)
    except Exception as exc:
        print(f"[BLOCKED] EFplant release gate: {exc}", file=sys.stderr)
        return 2

    if active:
        run = active[0]
        print(
            "[DEFERRED] GitHub Pages deployment is already active "
            f"(run={run.get('id')}, commit={str(run.get('head_sha', ''))[:7]}).",
            file=sys.stderr,
        )
        return BUSY_EXIT_CODE

    print("[OK] EFplant release gate: no active Pages deployment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
