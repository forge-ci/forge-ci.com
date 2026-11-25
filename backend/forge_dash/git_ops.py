"""Validated GitHub Actions metadata for the signed-in dashboard."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
Epoch = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ActionsJob(BaseModel):
    id: int
    run_id: int
    attempt: int
    workflow: str | None
    title: str
    branch: str | None
    name: str
    status: str
    conclusion: str | None
    runner: str | None
    placement: Literal["Dell", "GitHub hosted", "Other runner", "Unassigned"]
    created_at: Epoch
    started_at: Epoch | None
    completed_at: Epoch | None
    steps: list[tuple[str, str, str | None]] = Field(max_length=200)


class ActionsRepository(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
    observed_at: Epoch | None
    partial: bool
    jobs: list[ActionsJob] = Field(max_length=4500)


class ActionsSnapshot(BaseModel):
    observed_at: Epoch
    repositories: list[ActionsRepository] = Field(max_length=100)


class RunnerEvent(BaseModel):
    id: str = Field(min_length=1, max_length=384)
    runner: str = Field(min_length=1, max_length=150)
    job: str = Field(min_length=1, max_length=500)
    phase: Literal["started", "finished"]
    result: str | None = Field(default=None, max_length=40)
    observed_at: Epoch


class HostedRunnerEvent(RunnerEvent):
    host: str


class GitOpsFeed(BaseModel):
    actions: ActionsSnapshot | None
    runner_events: list[HostedRunnerEvent]
