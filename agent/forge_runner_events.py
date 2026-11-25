#!/usr/bin/env python3
"""Relay job lifecycle messages from the host runner journal to Forge CI."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import cast

START = re.compile(r"(?:^|Z: )Running job: (.+)$")
FINISH = re.compile(r"(?:^|Z: )Job (.+) completed with result: (\w+)$")
RUNNER = re.compile(r"(?:dyn-[a-z0-9_-]+-\d+|[a-z0-9_-]+-runner-\d+)$")
URL = "https://forge-ci.com/api/git-ops/runner-event"


def runner_event(line: str) -> dict[str, object] | None:
    raw = cast("object", json.loads(line))
    if not isinstance(raw, dict):
        return None
    entry = cast("dict[str, object]", raw)
    name = entry.get("CONTAINER_NAME")
    message = entry.get("MESSAGE")
    cursor = entry.get("__CURSOR")
    timestamp = entry.get("__REALTIME_TIMESTAMP")
    if not (
        isinstance(name, str)
        and RUNNER.fullmatch(name)
        and isinstance(message, str)
        and isinstance(cursor, str)
        and isinstance(timestamp, str)
        and timestamp.isdecimal()
    ):
        return None
    finish = FINISH.search(message)
    start = START.search(message)
    match = finish or start
    if match is None:
        return None
    return {
        "id": cursor,
        "runner": name,
        "job": match.group(1)[:500],
        "phase": "finished" if finish else "started",
        "result": finish.group(2) if finish else None,
        "observed_at": int(timestamp) / 1_000_000,
    }


def journal_lines(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=20)  # noqa: S603


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--state", type=Path, default=Path("/var/lib/forge-runner-events/cursor")
    )
    state = cast("Path", parser.parse_args().state)
    args = [
        "journalctl",
        "--output=json",
        "--no-pager",
        "--grep=Running job:|completed with result:",
        "--since=-30 minutes",
    ]
    cursor_args = ["--after-cursor", state.read_text().strip()] if state.is_file() else []
    output = journal_lines(args + cursor_args)
    if output.returncode and output.stderr and cursor_args:
        # A journal rotation can remove the last acknowledged cursor.
        output = journal_lines(args)
    if output.returncode == 1 and not output.stdout and not output.stderr:
        return
    if output.returncode:
        msg = "could not read runner journal"
        raise RuntimeError(msg)
    token = Path("/var/lib/forge-metrics/token").read_text().strip()
    for line in output.stdout.splitlines():
        event = runner_event(line)
        if event is None:
            continue
        request = urllib.request.Request(
            URL,
            data=json.dumps(event).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10):  # noqa: S310 -- fixed HTTPS origin
            _ = state.write_text(str(event["id"]), encoding="utf-8")


if __name__ == "__main__":
    main()
