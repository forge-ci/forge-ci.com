from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from forge_git_ops import collect_repo, placement
from forge_runner_events import runner_event


def test_active_and_completed_job_details_share_a_repository() -> None:
    run = {
        "id": 3,
        "run_attempt": 2,
        "name": "CI",
        "display_title": "Build branch",
        "head_branch": "main",
        "created_at": "2026-11-25T03:57:00Z",
        "status": "in_progress",
    }

    def get(path: str) -> dict[str, object]:
        query = parse_qs(urlsplit(path).query)
        if path.endswith("jobs?per_page=100"):
            return {
                "total_count": 2,
                "jobs": [
                    {
                        "id": 1,
                        "name": "Dell job",
                        "status": "in_progress",
                        "runner_name": "dell-fsx3mn2-dyn-obs-rust-1",
                        "steps": [{"name": "Build", "status": "in_progress"}],
                    },
                    {"id": 2, "name": "Waiting job", "status": "queued"},
                ],
            }
        return {
            "total_count": int(query["status"] == ["in_progress"]),
            "workflow_runs": [run] if query["status"] == ["in_progress"] else [],
        }

    snapshot, saved = collect_repo("forge-ci/soldr", 1_795_580_100, get, {})
    assert snapshot["name"] == "forge-ci/soldr"
    assert not snapshot["partial"]
    assert "Build" in json.dumps(snapshot["jobs"])
    assert "Dell" in json.dumps(snapshot["jobs"])
    assert saved == {}
    assert placement("GitHub Actions 100", "") == "GitHub hosted"


def test_runner_journal_emits_lifecycle_metadata_only() -> None:
    raw = {
        "__CURSOR": "runner-cursor",
        "__REALTIME_TIMESTAMP": "1795580100000000",
        "CONTAINER_NAME": "dyn-obs-rust-4",
        "MESSAGE": "2026-11-25 04:15:00Z: Job Build completed with result: Succeeded",
    }
    assert runner_event(json.dumps(raw)) == {
        "id": "runner-cursor",
        "runner": "dyn-obs-rust-4",
        "job": "Build",
        "phase": "finished",
        "result": "Succeeded",
        "observed_at": 1_795_580_100.0,
    }
    raw["CONTAINER_NAME"] = "forge-metrics-agent"
    assert runner_event(json.dumps(raw)) is None
