"""APScheduler-based cron scheduler for sea-gang tasks."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import load_all_projects
from .models import ProjectDef, TaskDef
from .queue import DuplicateJobError, JobQueue, QueueDrainingError, QueueFullError

logger = logging.getLogger("seagang.scheduler")


class Scheduler:
    """Cron-based task scheduler using APScheduler."""

    def __init__(self, queue: JobQueue):
        self.queue = queue
        self._scheduler = BackgroundScheduler(daemon=True)
        self._projects: dict[str, ProjectDef] = {}

    def load_projects(self) -> int:
        """Load all project configs and register their scheduled tasks."""
        self._projects = load_all_projects()
        count = 0

        for proj_name, proj in self._projects.items():
            for task_name, task_def in proj.tasks.items():
                if task_def.schedule:
                    self._register_task(proj, task_def)
                    count += 1
                    logger.info(
                        f"Scheduled {proj_name}/{task_name}: {task_def.schedule}"
                    )

        return count

    def _register_task(self, project: ProjectDef, task_def: TaskDef) -> None:
        """Register a single cron-triggered task."""
        job_id = f"{project.name}_{task_def.name}"
        trigger = CronTrigger.from_crontab(task_def.schedule)

        self._scheduler.add_job(
            self._enqueue_task,
            trigger=trigger,
            id=job_id,
            name=f"{project.name}/{task_def.name}",
            args=[project, task_def],
            replace_existing=True,
            misfire_grace_time=300,  # 5 min grace for misfires
        )

    def _enqueue_task(self, project: ProjectDef, task_def: TaskDef) -> None:
        """Callback for scheduled triggers — enqueue the task."""
        try:
            self.queue.enqueue(project, task_def)
        except QueueFullError as e:
            logger.warning(f"Schedule trigger rejected: {e}")
        except DuplicateJobError as e:
            logger.info(f"Schedule skipped (already queued): {e}")
        except QueueDrainingError as e:
            logger.info(f"Schedule skipped (draining): {e}")

    def start(self) -> None:
        """Start the scheduler."""
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("Scheduler started")

    def stop(self) -> None:
        """Stop the scheduler."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def get_scheduled_jobs(self) -> list[dict]:
        """Get info about all scheduled jobs."""
        result = []
        for job in self._scheduler.get_jobs():
            result.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else "paused",
                "trigger": str(job.trigger),
            })
        return result

    def pause(self) -> None:
        """Pause all scheduled jobs."""
        self._scheduler.pause()
        logger.info("Scheduler paused")

    def resume(self) -> None:
        """Resume all scheduled jobs."""
        self._scheduler.resume()
        logger.info("Scheduler resumed")

    @property
    def projects(self) -> dict[str, ProjectDef]:
        return self._projects
