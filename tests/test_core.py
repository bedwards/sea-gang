"""Tests for sea-gang queue, db, and monitor modules."""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from seagang.db import Database
from seagang.models import Job, JobStatus, ProjectDef, TaskDef
from seagang.monitor import OutputTracker, should_extend_soft_timeout
from seagang.queue import DuplicateJobError, JobQueue, QueueFullError


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


@pytest.fixture
def project() -> ProjectDef:
    return ProjectDef(
        name="test-proj",
        working_dir="/tmp",
        tasks={
            "fast": TaskDef(
                name="fast", command="echo hello",
                soft_timeout_minutes=1, hard_timeout_minutes=2, expected_minutes=0.5,
            ),
            "slow": TaskDef(
                name="slow", command="sleep 10",
                soft_timeout_minutes=5, hard_timeout_minutes=10, expected_minutes=3,
            ),
        },
    )


class TestDatabase:
    def test_insert_and_get_job(self, db: Database):
        job = Job(project="p", task="t", command="echo 1", working_dir="/tmp")
        db.insert_job(job)
        got = db.get_job(job.id)
        assert got is not None
        assert got.project == "p"
        assert got.task == "t"
        assert got.status == JobStatus.PENDING

    def test_dequeue_fifo_order(self, db: Database):
        j1 = Job(project="p", task="t1", command="echo 1", working_dir="/tmp")
        j2 = Job(project="p", task="t2", command="echo 2", working_dir="/tmp")
        db.insert_job(j1)
        db.insert_job(j2)
        got = db.dequeue_next()
        assert got is not None
        assert got.id == j1.id
        assert got.status == JobStatus.RUNNING

    def test_pending_count(self, db: Database):
        assert db.pending_count() == 0
        db.insert_job(Job(project="p", task="t", command="echo", working_dir="/tmp"))
        assert db.pending_count() == 1

    def test_has_pending_or_running(self, db: Database):
        assert not db.has_pending_or_running("p", "t")
        db.insert_job(Job(project="p", task="t", command="echo", working_dir="/tmp"))
        assert db.has_pending_or_running("p", "t")

    def test_remove_pending(self, db: Database):
        job = Job(project="p", task="t", command="echo", working_dir="/tmp")
        db.insert_job(job)
        assert db.remove_pending_job(job.id)
        assert db.pending_count() == 0

    def test_clear_pending(self, db: Database):
        for i in range(5):
            db.insert_job(Job(project="p", task=f"t{i}", command="echo", working_dir="/tmp"))
        assert db.clear_pending() == 5
        assert db.pending_count() == 0

    def test_record_completion_and_stats(self, db: Database):
        job = Job(project="p", task="t", command="echo", working_dir="/tmp",
                  status=JobStatus.COMPLETED)
        job.started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        job.finished_at = datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
        job.exit_code = 0
        db.record_completion(job)
        stats = db.get_task_stats("p/t")
        assert len(stats) == 1
        assert stats[0].run_count == 1
        assert stats[0].avg_seconds == pytest.approx(10.0, abs=0.5)
        assert stats[0].success_count == 1

    def test_get_recent_jobs(self, db: Database):
        job = Job(project="p", task="t", command="echo", working_dir="/tmp",
                  status=JobStatus.COMPLETED)
        job.finished_at = datetime.now(timezone.utc)
        db.insert_job(job)
        db.update_job(job)
        recent = db.get_recent_jobs(10)
        assert len(recent) == 1


class TestQueue:
    def test_enqueue_and_depth(self, db: Database, project: ProjectDef):
        queue = JobQueue(db)
        queue.enqueue(project, project.tasks["fast"])
        assert queue.depth() == 1

    def test_backpressure_rejects(self, db: Database, project: ProjectDef):
        queue = JobQueue(db, max_size=2)
        queue.enqueue(project, project.tasks["fast"])
        queue.enqueue(project, project.tasks["slow"])
        with pytest.raises(QueueFullError):
            # Need a third distinct task
            third = TaskDef(name="third", command="echo", soft_timeout_minutes=1,
                           hard_timeout_minutes=2, expected_minutes=0.5)
            project.tasks["third"] = third
            queue.enqueue(project, third)

    def test_duplicate_detection(self, db: Database, project: ProjectDef):
        queue = JobQueue(db)
        queue.enqueue(project, project.tasks["fast"])
        with pytest.raises(DuplicateJobError):
            queue.enqueue(project, project.tasks["fast"])

    def test_dequeue_returns_job(self, db: Database, project: ProjectDef):
        queue = JobQueue(db)
        queue.enqueue(project, project.tasks["fast"])
        job = queue.dequeue()
        assert job is not None
        assert job.status == JobStatus.RUNNING
        assert queue.depth() == 0

    def test_remove_job(self, db: Database, project: ProjectDef):
        queue = JobQueue(db)
        job = queue.enqueue(project, project.tasks["fast"])
        assert queue.remove(job.id)
        assert queue.depth() == 0

    def test_clear_queue(self, db: Database, project: ProjectDef):
        queue = JobQueue(db)
        queue.enqueue(project, project.tasks["fast"])
        queue.enqueue(project, project.tasks["slow"])
        assert queue.clear() == 2
        assert queue.depth() == 0

    def test_snapshot(self, db: Database, project: ProjectDef):
        queue = JobQueue(db, max_size=5)
        queue.enqueue(project, project.tasks["fast"])
        snap = queue.snapshot()
        assert snap.depth == 1
        assert snap.max_size == 5
        assert snap.current_job is None
        assert len(snap.pending_jobs) == 1

    def test_drain_rejects(self, db: Database, project: ProjectDef):
        queue = JobQueue(db)
        queue.drain()
        from seagang.queue import QueueDrainingError
        with pytest.raises(QueueDrainingError):
            queue.enqueue(project, project.tasks["fast"])
        queue.undrain()
        queue.enqueue(project, project.tasks["fast"])
        assert queue.depth() == 1


class TestMonitor:
    def test_output_tracker_update(self):
        tracker = OutputTracker()
        tracker.update("line 1\nline 2\n")
        assert tracker.total_lines == 2
        assert tracker.is_producing_output

    def test_should_extend_with_output(self):
        job = Job(project="p", task="t", command="echo", working_dir="/tmp",
                  soft_timeout_seconds=60, expected_seconds=120)
        job.started_at = datetime.now(timezone.utc)
        tracker = OutputTracker()
        tracker.update("still working\n")
        extend, reason = should_extend_soft_timeout(job, tracker, None)
        assert extend
        assert "producing output" in reason

    def test_should_kill_when_silent_and_past_expected(self):
        job = Job(project="p", task="t", command="echo", working_dir="/tmp",
                  soft_timeout_seconds=60, expected_seconds=30)
        job.started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        tracker = OutputTracker()
        # Simulate old last_check_time
        tracker.last_check_time = time.monotonic() - 120
        extend, reason = should_extend_soft_timeout(job, tracker, None)
        assert not extend

    def test_should_extend_under_p95(self):
        job = Job(project="p", task="t", command="echo", working_dir="/tmp",
                  soft_timeout_seconds=60, expected_seconds=30)
        job.started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        tracker = OutputTracker()
        tracker.last_check_time = time.monotonic() - 120  # no recent output
        # But runtime is under p95
        runtime = job.runtime_seconds or 0
        extend, reason = should_extend_soft_timeout(job, tracker, p95_duration=runtime + 1000)
        assert extend
        assert "p95" in reason
