"""Data models for sea-gang task orchestrator."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    """Lifecycle states for a job."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED_SOFT = "killed_soft"
    KILLED_HARD = "killed_hard"
    REJECTED = "rejected"


@dataclass
class HealthCheck:
    """Pre-flight check before running a task."""
    name: str
    command: str
    expect_exit_code: int | None = 0
    expect_contains: str | None = None


@dataclass
class TaskDef:
    """Definition of a runnable task within a project."""
    name: str
    command: str
    soft_timeout_minutes: float = 30.0
    hard_timeout_minutes: float = 60.0
    expected_minutes: float = 15.0
    schedule: str | None = None  # cron expression

    @property
    def soft_timeout_seconds(self) -> float:
        return self.soft_timeout_minutes * 60

    @property
    def hard_timeout_seconds(self) -> float:
        return self.hard_timeout_minutes * 60

    @property
    def expected_seconds(self) -> float:
        return self.expected_minutes * 60


@dataclass
class ProjectDef:
    """Definition of a managed project."""
    name: str
    working_dir: str
    tasks: dict[str, TaskDef] = field(default_factory=dict)
    healthchecks: list[HealthCheck] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Job:
    """A job instance — one execution of a task."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    project: str = ""
    task: str = ""
    command: str = ""
    working_dir: str = ""
    env: dict[str, str] = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    pid: int | None = None
    output_tail: str = ""
    error_message: str = ""
    soft_timeout_seconds: float = 1800.0
    hard_timeout_seconds: float = 3600.0
    expected_seconds: float = 900.0

    @property
    def runtime_seconds(self) -> float | None:
        """Current runtime in seconds, or None if not started."""
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    @property
    def is_terminal(self) -> bool:
        """Whether this job is in a final state."""
        return self.status in (
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.KILLED_SOFT,
            JobStatus.KILLED_HARD,
            JobStatus.REJECTED,
        )

    @property
    def display_name(self) -> str:
        return f"{self.project}/{self.task}"


@dataclass
class TaskStats:
    """Aggregated timing stats for a task."""
    task_key: str  # "project/task"
    run_count: int = 0
    avg_seconds: float = 0.0
    p95_seconds: float = 0.0
    max_seconds: float = 0.0
    last_duration_seconds: float = 0.0
    last_exit_code: int | None = None
    success_count: int = 0
    failure_count: int = 0


@dataclass
class QueueSnapshot:
    """Point-in-time snapshot of queue state for display."""
    current_job: Job | None
    pending_jobs: list[Job]
    depth: int
    max_size: int
    is_draining: bool = False
