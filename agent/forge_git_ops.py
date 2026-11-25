#!/usr/bin/env python3
"""Collect Actions metadata with the admin droplet's scoped Pool Watch App."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable
    from http.client import HTTPResponse

Obj = dict[str, object]
RUN_LIMIT = 40
HISTORY_LIMIT = 12
WINDOW_S = 86400
GITHUB_API = "https://api.github.com"
RUN_STATES = ("in_progress", "queued", "waiting", "pending", "requested")
FINISHED = "completed"


class WatchConfig(Protocol):
    @property
    def watch_repos(self) -> tuple[str, ...]: ...


class WatchApp(Protocol):
    def scoped_token(self, owner: str, repos: tuple[str, ...], permissions: Obj) -> str: ...


class WatchModule(Protocol):
    def parse_config(self, text: str) -> WatchConfig: ...
    def make_app(self, cfg: WatchConfig) -> WatchApp: ...


def obj(value: object) -> Obj:
    if not isinstance(value, dict):
        msg = "expected GitHub object"
        raise TypeError(msg)
    return cast("Obj", value)


def items(value: object) -> list[Obj]:
    if not isinstance(value, list):
        msg = "expected GitHub list"
        raise TypeError(msg)
    return [obj(item) for item in cast("list[object]", value)]


def epoch(value: object) -> float | None:
    return datetime.fromisoformat(value).timestamp() if isinstance(value, str) else None


def placement(name: str, group: str) -> str:
    if name.lower().startswith(("dell-fsx3mn2-", "dell-jw22zc3-")):
        return "Dell"
    if name.startswith("GitHub Actions") or group == "GitHub Actions":
        return "GitHub hosted"
    return "Other runner" if name else "Unassigned"


def read_json(path: str, token: str) -> Obj:
    request = urllib.request.Request(  # noqa: S310 -- fixed GitHub HTTPS origin
        GITHUB_API + path,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
            "User-Agent": "forge-ci-git-ops",
        },
    )
    with cast("HTTPResponse", urllib.request.urlopen(request, timeout=10)) as response:  # noqa: S310
        return obj(cast("object", json.loads(response.read())))


def job_record(run: Obj, job: Obj) -> Obj:
    name = str(job.get("runner_name") or "")
    return {
        "id": job["id"],
        "run_id": run["id"],
        "attempt": run["run_attempt"],
        "workflow": run["name"],
        "title": run["display_title"],
        "branch": run["head_branch"],
        "name": job["name"],
        "status": job["status"],
        "conclusion": job.get("conclusion"),
        "runner": name or None,
        "placement": placement(name, str(job.get("runner_group_name") or "")),
        "created_at": epoch(job.get("created_at")) or epoch(run["created_at"]),
        "started_at": epoch(job.get("started_at")),
        "completed_at": epoch(job.get("completed_at")),
        "steps": [
            [step["name"], step["status"], step.get("conclusion")]
            for step in items(job.get("steps") or [])[:200]
        ],
    }


def collect_repo(
    repo: str, now: float, get: Callable[[str], Obj], cache: dict[str, list[Obj]]
) -> tuple[Obj, dict[str, list[Obj]]]:
    base = f"/repos/{repo}/actions/runs"
    runs: dict[object, Obj] = {}
    partial = False
    since = datetime.fromtimestamp(now - WINDOW_S, UTC).isoformat().replace("+00:00", "Z")
    for state in (*RUN_STATES, FINISHED):
        limit = HISTORY_LIMIT if state == FINISHED else RUN_LIMIT
        params: dict[str, str | int] = {"status": state, "per_page": limit}
        if state == FINISHED:
            params["created"] = f">={since}"
        page = get(f"{base}?{urllib.parse.urlencode(params)}")
        batch = items(page["workflow_runs"])
        partial |= int(str(page["total_count"])) > len(batch)
        runs.update((run["id"], run) for run in batch)
    jobs: dict[object, Obj] = {}
    saved: dict[str, list[Obj]] = {}
    for run in runs.values():
        key = f"{run['id']}:{run['run_attempt']}"
        finished = run["status"] == FINISHED
        records = cache.get(key) if finished else None
        if records is None:
            path = f"{base}/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100"
            batch = get(path)
            rows = items(batch["jobs"])
            if int(str(batch["total_count"])) > len(rows):
                rows.extend(items(get(f"{path}&page=2")["jobs"]))
            partial |= int(str(batch["total_count"])) > len(rows)
            records = [job_record(run, job) for job in rows]
        if finished:
            saved[key] = records
        jobs.update((job["id"], job) for job in records)
    return {
        "name": repo,
        "observed_at": now,
        "partial": partial,
        "jobs": list(jobs.values()),
    }, saved


def collect_one(repo: str, token: str, now: float, directory: Path, previous: Obj | None) -> Obj:
    path = directory / f"{repo.replace('/', '__')}.json"
    try:
        raw = obj(cast("object", json.loads(path.read_text()))) if path.is_file() else {}
        cache = {key: items(value) for key, value in raw.items()}
        snapshot, saved = collect_repo(repo, now, lambda url: read_json(url, token), cache)
        staged = path.with_suffix(".tmp")
        _ = staged.write_text(json.dumps(saved), encoding="utf-8")
        staged.replace(path)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"{repo}: Actions collection failed ({type(exc).__name__})", file=sys.stderr)
        return {**(previous or {"name": repo, "observed_at": None, "jobs": []}), "partial": True}
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--watch-dir", type=Path, default=Path("/usr/local/lib/forge-quick"))
    _ = parser.add_argument(
        "--watch-config", type=Path, default=Path("/etc/forge-pool-watch/config.toml")
    )
    _ = parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/forge-git-ops"))
    _ = parser.add_argument("--snapshot-only", action="store_true")
    args = parser.parse_args()
    watch_dir = cast("Path", args.watch_dir)
    watch_config = cast("Path", args.watch_config)
    state = cast("Path", args.state_dir)
    sys.path.insert(0, str(watch_dir))
    watch = cast("WatchModule", cast("object", importlib.import_module("pool_watch")))
    config = watch.parse_config(watch_config.read_text())
    app = watch.make_app(config)
    now = time.time()
    latest = state / "snapshot.json"
    previous = {
        row["name"]: row
        for row in (
            items(obj(cast("object", json.loads(latest.read_text())))["repositories"])
            if latest.is_file()
            else []
        )
    }
    owners = {repo.split("/")[0] for repo in config.watch_repos}
    tokens: dict[str, str] = {}
    for owner in sorted(owners):
        repositories = tuple(
            repo.split("/")[1] for repo in config.watch_repos if repo.startswith(f"{owner}/")
        )
        try:
            token = app.scoped_token(owner, repositories, {"actions": "read"})
        except Exception:  # noqa: BLE001 -- app installations can cover selected repositories
            for name in repositories:
                try:
                    tokens[f"{owner}/{name}"] = app.scoped_token(
                        owner, (name,), {"actions": "read"}
                    )
                except Exception:  # noqa: BLE001 -- do not fail other repositories
                    print(f"{owner}/{name}: no Actions read access", file=sys.stderr)
        else:
            tokens.update((f"{owner}/{name}", token) for name in repositories)

    def collect(repo: str) -> Obj:
        token = tokens.get(repo)
        if token is None:
            return {
                **previous.get(repo, {"name": repo, "observed_at": None, "jobs": []}),
                "partial": True,
            }
        return collect_one(repo, token, now, state, previous.get(repo))

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(collect, config.watch_repos))
    data = json.dumps({"observed_at": now, "repositories": rows}, separators=(",", ":"))
    staged = latest.with_suffix(".tmp")
    _ = staged.write_text(data, encoding="utf-8")
    staged.replace(latest)
    if cast("bool", args.snapshot_only):
        return
    token = Path("/var/lib/forge-metrics/token").read_text().strip()
    request = urllib.request.Request(
        "https://forge-ci.com/api/git-ops/ingest",
        data=data.encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with cast("HTTPResponse", urllib.request.urlopen(request, timeout=20)) as response:  # noqa: S310
        print(f"Git Ops snapshot: {len(rows)} repositories (HTTP {response.status})")


if __name__ == "__main__":
    main()
