"""Job output monitoring — detects whether a task is still doing useful work."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .models import Job

logger = logging.getLogger("seagang.monitor")


@dataclass
class OutputTracker:
    """Tracks output progress for a running job."""
    last_line_count: int = 0
    last_byte_count: int = 0
    last_check_time: float = field(default_factory=time.monotonic)
    total_lines: int = 0
    total_bytes: int = 0

    def update(self, new_output: str) -> None:
        """Record new output received."""
        if new_output:
            lines = new_output.count("\n")
            self.total_lines += lines
            self.total_bytes += len(new_output)
            self.last_line_count = lines
            self.last_byte_count = len(new_output)
            self.last_check_time = time.monotonic()

    @property
    def seconds_since_output(self) -> float:
        """Seconds since last output was received."""
        return time.monotonic() - self.last_check_time

    @property
    def is_producing_output(self) -> bool:
        """Whether output has been received recently (last 60s)."""
        return self.seconds_since_output < 60.0


def should_extend_soft_timeout(
    job: Job,
    tracker: OutputTracker,
    p95_duration: float | None,
    grace_seconds: float = 300.0,
) -> tuple[bool, str]:
    """
    Decide whether to extend a job past its soft timeout.
    
    Returns (should_extend, reason).
    
    Logic:
    1. If producing output in the last 60s → extend (it's doing work)
    2. If runtime < historical p95 → extend (it might just be in a slow phase)
    3. Otherwise → kill it (it's stuck)
    """
    runtime = job.runtime_seconds or 0

    # Check 1: Recent output activity
    if tracker.is_producing_output:
        return True, (
            f"Still producing output ({tracker.total_lines} total lines, "
            f"last {tracker.seconds_since_output:.0f}s ago)"
        )

    # Check 2: Still within historical p95 range
    if p95_duration and runtime < p95_duration:
        return True, (
            f"Runtime ({runtime:.0f}s) still under historical p95 ({p95_duration:.0f}s) — "
            f"may be in a long compute phase"
        )

    # Check 3: Still within expected duration (configured by user)
    if runtime < job.expected_seconds:
        return True, (
            f"Runtime ({runtime:.0f}s) still under expected duration ({job.expected_seconds:.0f}s)"
        )

    # No good reason to keep it alive
    return False, (
        f"No output for {tracker.seconds_since_output:.0f}s, "
        f"runtime ({runtime:.0f}s) exceeds expected ({job.expected_seconds:.0f}s)"
        + (f" and p95 ({p95_duration:.0f}s)" if p95_duration else "")
    )


def format_job_health(
    job: Job,
    tracker: OutputTracker | None = None,
) -> dict:
    """Format job health info for display."""
    info = {
        "id": job.id,
        "name": job.display_name,
        "status": job.status.value,
        "runtime_seconds": round(job.runtime_seconds or 0, 1),
        "soft_timeout": round(job.soft_timeout_seconds, 0),
        "hard_timeout": round(job.hard_timeout_seconds, 0),
    }

    if tracker:
        info["total_output_lines"] = tracker.total_lines
        info["total_output_bytes"] = tracker.total_bytes
        info["seconds_since_output"] = round(tracker.seconds_since_output, 1)
        info["producing_output"] = tracker.is_producing_output

    return info
