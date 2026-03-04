"""Job queue with backpressure, duplicate detection, and manipulation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .config import DEFAULTS
from .db import Database
from .models import Job, JobStatus, ProjectDef, QueueSnapshot, TaskDef

logger = logging.getLogger("seagang.queue")


class QueueFullError(Exception):
    """Raised when the queue is at capacity."""
    pass


class DuplicateJobError(Exception):
    """Raised when a duplicate task is already pending/running."""
    pass


class QueueDrainingError(Exception):
    """Raised when the queue is draining and not accepting new jobs."""
    pass


class JobQueue:
    """FIFO job queue with backpressure and manipulation."""

    def __init__(self, db: Database, max_size: int | None = None):
        self.db = db
        self.max_size = max_size or DEFAULTS["max_queue_size"]
        self._draining = False

    @property
    def is_draining(self) -> bool:
        return self._draining

    def enqueue(self, project: ProjectDef, task_def: TaskDef) -> Job:
        """
        Enqueue a task for execution.
        
        Raises:
            QueueFullError: if queue >= max_size
            DuplicateJobError: if task is already pending/running
            QueueDrainingError: if queue is in drain mode
        """
        if self._draining:
            raise QueueDrainingError(f"Queue is draining — not accepting new jobs")

        depth = self.db.pending_count()
        if depth >= self.max_size:
            raise QueueFullError(
                f"Queue full ({depth}/{self.max_size}) — rejecting {project.name}/{task_def.name}"
            )

        if self.db.has_pending_or_running(project.name, task_def.name):
            raise DuplicateJobError(
                f"{project.name}/{task_def.name} is already pending or running"
            )

        job = Job(
            project=project.name,
            task=task_def.name,
            command=task_def.command,
            working_dir=project.working_dir,
            env=dict(project.env),
            status=JobStatus.PENDING,
            enqueued_at=datetime.now(timezone.utc),
            soft_timeout_seconds=task_def.soft_timeout_seconds,
            hard_timeout_seconds=task_def.hard_timeout_seconds,
            expected_seconds=task_def.expected_seconds,
            priority=task_def.priority,
        )

        self.db.insert_job(job)
        logger.info(f"Enqueued {job.display_name} (id={job.id}, depth={depth + 1})")
        return job

    def dequeue(self) -> Job | None:
        """Get the next pending job and mark it as running."""
        job = self.db.dequeue_next()
        if job:
            logger.info(f"Dequeued {job.display_name} (id={job.id})")
        return job

    def peek(self, limit: int = 10) -> list[Job]:
        """View the next N pending jobs without consuming them."""
        return self.db.get_pending_jobs()[:limit]

    def remove(self, job_id: str) -> bool:
        """Remove a pending job from the queue."""
        removed = self.db.remove_pending_job(job_id)
        if removed:
            logger.info(f"Removed job {job_id} from queue")
        return removed

    def move(self, job_id: str, position: int) -> bool:
        """Move a pending job to a new position (1-based)."""
        moved = self.db.reorder_job(job_id, position)
        if moved:
            logger.info(f"Moved job {job_id} to position {position}")
        return moved

    def clear(self) -> int:
        """Remove all pending jobs. Returns count."""
        count = self.db.clear_pending()
        logger.info(f"Cleared {count} pending jobs")
        return count

    def depth(self) -> int:
        """Current number of pending jobs."""
        return self.db.pending_count()

    def drain(self) -> None:
        """Stop accepting new jobs. Existing jobs will finish."""
        self._draining = True
        logger.info("Queue entering drain mode — no new jobs accepted")

    def undrain(self) -> None:
        """Resume accepting new jobs."""
        self._draining = False
        logger.info("Queue exiting drain mode — accepting new jobs")

    def snapshot(self) -> QueueSnapshot:
        """Get a point-in-time view of the queue."""
        return QueueSnapshot(
            current_job=self.db.get_running_job(),
            pending_jobs=self.db.get_pending_jobs(),
            depth=self.db.pending_count(),
            max_size=self.max_size,
            is_draining=self._draining,
        )
