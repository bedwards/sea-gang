"""SQLite database backend for job queue and statistics."""

from __future__ import annotations

import json
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from .models import Job, JobStatus, TaskStats

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    task TEXT NOT NULL,
    command TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    env TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    enqueued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    pid INTEGER,
    output_tail TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    soft_timeout_seconds REAL NOT NULL DEFAULT 1800,
    hard_timeout_seconds REAL NOT NULL DEFAULT 3600,
    expected_seconds REAL NOT NULL DEFAULT 900,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_position ON jobs(position);
CREATE INDEX IF NOT EXISTS idx_jobs_project_task ON jobs(project, task);

CREATE TABLE IF NOT EXISTS task_stats (
    task_key TEXT PRIMARY KEY,
    run_count INTEGER NOT NULL DEFAULT 0,
    total_seconds REAL NOT NULL DEFAULT 0,
    max_seconds REAL NOT NULL DEFAULT 0,
    last_duration_seconds REAL NOT NULL DEFAULT 0,
    last_exit_code INTEGER,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    durations TEXT NOT NULL DEFAULT '[]'
);
"""

_OUTPUT_TAIL_LINES = 50  # Keep last N lines in output_tail


class Database:
    """SQLite-backed persistence for jobs and stats."""

    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            project=row["project"],
            task=row["task"],
            command=row["command"],
            working_dir=row["working_dir"],
            env=json.loads(row["env"]),
            status=JobStatus(row["status"]),
            enqueued_at=datetime.fromisoformat(row["enqueued_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            exit_code=row["exit_code"],
            pid=row["pid"],
            output_tail=row["output_tail"],
            error_message=row["error_message"],
            soft_timeout_seconds=row["soft_timeout_seconds"],
            hard_timeout_seconds=row["hard_timeout_seconds"],
            expected_seconds=row["expected_seconds"],
        )

    # ─── Job CRUD ─────────────────────────────────────────────

    def insert_job(self, job: Job) -> None:
        """Insert a new job into the database."""
        with self._conn() as conn:
            # Get next position
            row = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 AS pos FROM jobs WHERE status = 'pending'"
            ).fetchone()
            pos = row["pos"]

            conn.execute(
                """INSERT INTO jobs (id, project, task, command, working_dir, env, status,
                   enqueued_at, soft_timeout_seconds, hard_timeout_seconds, expected_seconds, position)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job.id, job.project, job.task, job.command, job.working_dir,
                 json.dumps(job.env), job.status.value,
                 job.enqueued_at.isoformat(), job.soft_timeout_seconds,
                 job.hard_timeout_seconds, job.expected_seconds, pos),
            )

    def get_job(self, job_id: str) -> Job | None:
        """Get a job by ID."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row else None

    def update_job(self, job: Job) -> None:
        """Update an existing job."""
        with self._conn() as conn:
            conn.execute(
                """UPDATE jobs SET status=?, started_at=?, finished_at=?, exit_code=?,
                   pid=?, output_tail=?, error_message=? WHERE id=?""",
                (job.status.value,
                 job.started_at.isoformat() if job.started_at else None,
                 job.finished_at.isoformat() if job.finished_at else None,
                 job.exit_code, job.pid, job.output_tail, job.error_message, job.id),
            )

    # ─── Queue Operations ────────────────────────────────────

    def get_pending_jobs(self) -> list[Job]:
        """Get all pending jobs in queue order."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = 'pending' ORDER BY position ASC"
            ).fetchall()
            return [self._row_to_job(r) for r in rows]

    def get_running_job(self) -> Job | None:
        """Get the currently running job, if any."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'running' LIMIT 1"
            ).fetchone()
            return self._row_to_job(row) if row else None

    def pending_count(self) -> int:
        """Count pending jobs."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM jobs WHERE status = 'pending'"
            ).fetchone()
            return row["cnt"]

    def has_pending_or_running(self, project: str, task: str) -> bool:
        """Check if a task is already pending or running (duplicate detection)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM jobs WHERE project=? AND task=? AND status IN ('pending','running')",
                (project, task),
            ).fetchone()
            return row["cnt"] > 0

    def remove_pending_job(self, job_id: str) -> bool:
        """Remove a pending job. Returns True if removed."""
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM jobs WHERE id = ? AND status = 'pending'", (job_id,)
            )
            return cursor.rowcount > 0

    def clear_pending(self) -> int:
        """Remove all pending jobs. Returns count removed."""
        with self._conn() as conn:
            cursor = conn.execute("DELETE FROM jobs WHERE status = 'pending'")
            return cursor.rowcount

    def reorder_job(self, job_id: str, new_position: int) -> bool:
        """Move a pending job to a new position in the queue."""
        with self._conn() as conn:
            # Verify job is pending
            row = conn.execute(
                "SELECT position FROM jobs WHERE id = ? AND status = 'pending'", (job_id,)
            ).fetchone()
            if not row:
                return False

            old_pos = row["position"]

            if new_position < old_pos:
                # Moving forward — shift others back
                conn.execute(
                    "UPDATE jobs SET position = position + 1 WHERE status='pending' AND position >= ? AND position < ?",
                    (new_position, old_pos),
                )
            else:
                # Moving back — shift others forward
                conn.execute(
                    "UPDATE jobs SET position = position - 1 WHERE status='pending' AND position > ? AND position <= ?",
                    (old_pos, new_position),
                )

            conn.execute("UPDATE jobs SET position = ? WHERE id = ?", (new_position, job_id))
            return True

    def dequeue_next(self) -> Job | None:
        """Get the next pending job and mark it as running."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'pending' ORDER BY position ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None

            now = datetime.now(timezone.utc)
            conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
                (now.isoformat(), row["id"]),
            )
            job = self._row_to_job(row)
            job.status = JobStatus.RUNNING
            job.started_at = now
            return job

    # ─── History ─────────────────────────────────────────────

    def get_recent_jobs(self, limit: int = 20) -> list[Job]:
        """Get recent completed/failed/killed jobs."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM jobs WHERE status NOT IN ('pending', 'running')
                   ORDER BY finished_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [self._row_to_job(r) for r in rows]

    def prune_old_jobs(self, keep_days: int = 30) -> int:
        """Remove jobs older than keep_days. Returns count removed."""
        with self._conn() as conn:
            cursor = conn.execute(
                """DELETE FROM jobs WHERE status NOT IN ('pending', 'running')
                   AND finished_at < datetime('now', ?)""",
                (f"-{keep_days} days",),
            )
            return cursor.rowcount

    # ─── Task Stats ──────────────────────────────────────────

    def record_completion(self, job: Job) -> None:
        """Record a job completion in task_stats."""
        if job.runtime_seconds is None:
            return

        task_key = f"{job.project}/{job.task}"
        duration = job.runtime_seconds
        is_success = job.status == JobStatus.COMPLETED

        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM task_stats WHERE task_key = ?", (task_key,)
            ).fetchone()

            if row:
                durations = json.loads(row["durations"])
                durations.append(duration)
                # Keep last 50 durations for percentile calc
                durations = durations[-50:]

                conn.execute(
                    """UPDATE task_stats SET
                       run_count = run_count + 1,
                       total_seconds = total_seconds + ?,
                       max_seconds = MAX(max_seconds, ?),
                       last_duration_seconds = ?,
                       last_exit_code = ?,
                       success_count = success_count + ?,
                       failure_count = failure_count + ?,
                       durations = ?
                       WHERE task_key = ?""",
                    (duration, duration, duration, job.exit_code,
                     1 if is_success else 0, 0 if is_success else 1,
                     json.dumps(durations), task_key),
                )
            else:
                conn.execute(
                    """INSERT INTO task_stats
                       (task_key, run_count, total_seconds, max_seconds,
                        last_duration_seconds, last_exit_code, success_count,
                        failure_count, durations)
                       VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)""",
                    (task_key, duration, duration, duration, job.exit_code,
                     1 if is_success else 0, 0 if is_success else 1,
                     json.dumps([duration])),
                )

    def get_task_stats(self, task_key: str | None = None) -> list[TaskStats]:
        """Get task stats, optionally filtered by key."""
        with self._conn() as conn:
            if task_key:
                rows = conn.execute(
                    "SELECT * FROM task_stats WHERE task_key = ?", (task_key,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM task_stats ORDER BY task_key").fetchall()

            result = []
            for row in rows:
                durations = json.loads(row["durations"])
                p95 = 0.0
                avg = 0.0
                if durations:
                    avg = statistics.mean(durations)
                    if len(durations) >= 2:
                        sorted_d = sorted(durations)
                        idx = int(len(sorted_d) * 0.95)
                        p95 = sorted_d[min(idx, len(sorted_d) - 1)]
                    else:
                        p95 = durations[0]

                result.append(TaskStats(
                    task_key=row["task_key"],
                    run_count=row["run_count"],
                    avg_seconds=avg,
                    p95_seconds=p95,
                    max_seconds=row["max_seconds"],
                    last_duration_seconds=row["last_duration_seconds"],
                    last_exit_code=row["last_exit_code"],
                    success_count=row["success_count"],
                    failure_count=row["failure_count"],
                ))
            return result

    def get_p95_for_task(self, project: str, task: str) -> float | None:
        """Get p95 duration for a specific task, or None if no history."""
        stats = self.get_task_stats(f"{project}/{task}")
        if stats and stats[0].run_count >= 2:
            return stats[0].p95_seconds
        return None
