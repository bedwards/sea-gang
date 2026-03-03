"""Serial subprocess executor with soft/hard timeout management."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import DEFAULTS, LOG_DIR
from .db import Database
from .models import Job, JobStatus
from .monitor import OutputTracker, should_extend_soft_timeout

logger = logging.getLogger("seagang.runner")

# Max lines kept in output_tail (stored in DB)
OUTPUT_TAIL_MAX = 50


class Runner:
    """
    Serial subprocess executor.
    
    Runs one job at a time. Uses soft/hard timeout strategy:
    - At soft_timeout: check if task is still producing output or within p95
    - If yes: extend by grace period, re-check later
    - If no: kill immediately (killed_soft)
    - At hard_timeout: kill unconditionally (killed_hard)
    """

    def __init__(self, db: Database):
        self.db = db
        self._current_process: subprocess.Popen | None = None
        self._current_job: Job | None = None
        self._tracker: OutputTracker | None = None
        self._stop_event = threading.Event()
        self._output_lock = threading.Lock()
        self._output_lines: list[str] = []

    @property
    def is_busy(self) -> bool:
        return self._current_process is not None

    @property
    def current_job(self) -> Job | None:
        return self._current_job

    @property
    def current_tracker(self) -> OutputTracker | None:
        return self._tracker

    def stop(self) -> None:
        """Signal the runner to stop after the current job."""
        self._stop_event.set()

    def run_job(self, job: Job) -> Job:
        """
        Execute a job synchronously. Returns the updated job with final status.
        
        This blocks until the job completes, is killed, or fails.
        """
        self._current_job = job
        self._tracker = OutputTracker()
        self._output_lines = []

        # Set up log file
        log_dir = LOG_DIR / job.project
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job.task}_{job.id}.log"

        logger.info(f"Starting {job.display_name} (id={job.id})")
        logger.info(f"  Command: {job.command}")
        logger.info(f"  Working dir: {job.working_dir}")
        logger.info(f"  Soft timeout: {job.soft_timeout_seconds:.0f}s")
        logger.info(f"  Hard timeout: {job.hard_timeout_seconds:.0f}s")
        logger.info(f"  Log: {log_path}")

        # Merge environment
        env = os.environ.copy()
        env.update(job.env)

        try:
            self._current_process = subprocess.Popen(
                job.command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=job.working_dir,
                env=env,
                preexec_fn=os.setsid,  # Create process group for clean kill
            )
            job.pid = self._current_process.pid
            self.db.update_job(job)

            # Start output reader thread
            reader_thread = threading.Thread(
                target=self._read_output,
                args=(self._current_process, log_path),
                daemon=True,
            )
            reader_thread.start()

            # Wait with timeout management
            final_status = self._wait_with_timeouts(job)

            # Collect exit code
            try:
                job.exit_code = self._current_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._force_kill()
                job.exit_code = -9

            # Set final status
            if final_status:
                job.status = final_status
            elif job.exit_code == 0:
                job.status = JobStatus.COMPLETED
            else:
                job.status = JobStatus.FAILED

        except FileNotFoundError as e:
            job.status = JobStatus.FAILED
            job.error_message = f"Command not found: {e}"
            job.exit_code = 127
            logger.error(f"Command not found: {job.command}")
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error_message = str(e)
            job.exit_code = -1
            logger.error(f"Unexpected error running {job.display_name}: {e}")
        finally:
            job.finished_at = datetime.now(timezone.utc)

            # Store output tail
            with self._output_lock:
                job.output_tail = "\n".join(self._output_lines[-OUTPUT_TAIL_MAX:])

            self.db.update_job(job)
            self.db.record_completion(job)

            runtime = job.runtime_seconds or 0
            logger.info(
                f"Finished {job.display_name}: {job.status.value} "
                f"(exit={job.exit_code}, runtime={runtime:.1f}s)"
            )

            self._current_process = None
            self._current_job = None
            self._tracker = None

        return job

    def _wait_with_timeouts(self, job: Job) -> JobStatus | None:
        """
        Wait for process completion with soft/hard timeout management.
        
        Returns a kill status if the job was killed, None if it completed normally.
        """
        proc = self._current_process
        assert proc is not None

        check_interval = DEFAULTS["output_check_interval_seconds"]
        grace_extension = DEFAULTS["output_grace_extension_seconds"]

        soft_hit = False
        soft_extended_until = 0.0

        while True:
            # Check if we should stop
            if self._stop_event.is_set():
                self._kill_process(signal.SIGTERM)
                return JobStatus.KILLED_SOFT

            # Check if process finished
            try:
                proc.wait(timeout=min(check_interval, 5.0))
                return None  # Process completed naturally
            except subprocess.TimeoutExpired:
                pass

            runtime = job.runtime_seconds or 0

            # HARD TIMEOUT — absolute ceiling, no mercy
            if runtime >= job.hard_timeout_seconds:
                logger.warning(
                    f"HARD TIMEOUT: {job.display_name} at {runtime:.0f}s "
                    f"(limit={job.hard_timeout_seconds:.0f}s)"
                )
                self._kill_process(signal.SIGKILL)
                job.error_message = f"Hard timeout at {runtime:.0f}s"
                return JobStatus.KILLED_HARD

            # SOFT TIMEOUT — check if task is still useful
            if runtime >= job.soft_timeout_seconds and not soft_hit:
                soft_hit = True
                logger.info(f"Soft timeout reached for {job.display_name} at {runtime:.0f}s")

            if soft_hit and (not soft_extended_until or runtime >= soft_extended_until):
                p95 = self.db.get_p95_for_task(job.project, job.task)
                extend, reason = should_extend_soft_timeout(
                    job, self._tracker, p95, grace_extension
                )

                if extend:
                    soft_extended_until = runtime + grace_extension
                    logger.info(
                        f"Extending {job.display_name}: {reason} "
                        f"(next check at {soft_extended_until:.0f}s)"
                    )
                else:
                    logger.warning(
                        f"SOFT KILL: {job.display_name}: {reason}"
                    )
                    self._kill_process(signal.SIGTERM)
                    # Give it 10s to clean up after SIGTERM
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self._kill_process(signal.SIGKILL)
                    job.error_message = f"Soft timeout kill: {reason}"
                    return JobStatus.KILLED_SOFT

    def _read_output(self, proc: subprocess.Popen, log_path: Path) -> None:
        """Read stdout/stderr from process, store in buffer and log file."""
        assert proc.stdout is not None
        try:
            with open(log_path, "w") as log_file:
                for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                    log_file.write(line + "\n")
                    log_file.flush()

                    with self._output_lock:
                        self._output_lines.append(line)
                        # Trim buffer to prevent memory growth
                        if len(self._output_lines) > OUTPUT_TAIL_MAX * 2:
                            self._output_lines = self._output_lines[-OUTPUT_TAIL_MAX:]

                    if self._tracker:
                        self._tracker.update(line + "\n")
        except (OSError, ValueError):
            pass  # Process was killed, pipe closed

    def _kill_process(self, sig: int = signal.SIGTERM) -> None:
        """Kill the current process and its process group."""
        proc = self._current_process
        if proc and proc.poll() is None:
            try:
                # Kill the whole process group
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, sig)
                logger.debug(f"Sent signal {sig} to process group {pgid}")
            except (OSError, ProcessLookupError):
                try:
                    proc.kill()
                except (OSError, ProcessLookupError):
                    pass

    def _force_kill(self) -> None:
        """Force kill with SIGKILL."""
        self._kill_process(signal.SIGKILL)

    def get_output_tail(self, lines: int = 20) -> list[str]:
        """Get last N lines of current job output."""
        with self._output_lock:
            return self._output_lines[-lines:]
