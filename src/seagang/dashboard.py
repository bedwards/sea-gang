"""Textual TUI dashboard for sea-gang — high-performance live monitoring."""

from __future__ import annotations

from datetime import datetime, timezone

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Static, Log

from .config import DB_PATH, ensure_dirs
from .db import Database
from .models import Job, JobStatus


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _status_color(status: JobStatus) -> str:
    return {
        JobStatus.PENDING: "yellow",
        JobStatus.RUNNING: "cyan",
        JobStatus.COMPLETED: "green",
        JobStatus.FAILED: "red",
        JobStatus.KILLED_SOFT: "magenta",
        JobStatus.KILLED_HARD: "red",
        JobStatus.REJECTED: "dark_red",
    }.get(status, "white")


# ─── Widgets ─────────────────────────────────────────────────


class CurrentJobPanel(Static):
    """Shows the currently running job."""

    def update_job(self, job: Job | None) -> None:
        if not job:
            self.update("[dim]No job running[/dim]")
            self.border_title = "▶ Idle"
            self.styles.border = ("round", "grey50")
            return

        runtime = job.runtime_seconds or 0
        pct = ""
        if job.expected_seconds > 0:
            p = min(runtime / job.expected_seconds * 100, 999)
            pct = f" ({p:.0f}%)"

        # Build a simple progress bar
        bar_width = 30
        if job.hard_timeout_seconds > 0:
            filled = int(min(runtime / job.hard_timeout_seconds, 1.0) * bar_width)
        else:
            filled = 0

        # Color the bar based on timeout proximity
        if runtime >= job.soft_timeout_seconds:
            bar_char = "█"
            bar_color = "red" if runtime >= job.hard_timeout_seconds * 0.9 else "yellow"
        else:
            bar_char = "█"
            bar_color = "cyan"

        bar = f"[{bar_color}]{bar_char * filled}[/{bar_color}]{'░' * (bar_width - filled)}"

        content = (
            f"[bold cyan]{job.display_name}[/bold cyan]  [dim]id:{job.id}[/dim]\n"
            f"⏱  {_fmt_duration(runtime)}{pct}  │  "
            f"soft: {_fmt_duration(job.soft_timeout_seconds)}  │  "
            f"hard: {_fmt_duration(job.hard_timeout_seconds)}\n"
            f"{bar}"
        )
        self.update(content)
        self.border_title = "▶ Running"
        self.styles.border = ("round", "cyan")


class QueueStatsPanel(Static):
    """Shows queue depth and stats."""

    def update_stats(self, depth: int, max_size: int, draining: bool) -> None:
        drain_text = " [bold red]DRAINING[/bold red]" if draining else ""
        fill_pct = depth / max_size * 100 if max_size else 0

        if fill_pct >= 80:
            depth_color = "red"
        elif fill_pct >= 50:
            depth_color = "yellow"
        else:
            depth_color = "green"

        self.update(
            f"[{depth_color}]{depth}[/{depth_color}]/{max_size}{drain_text}"
        )


class StatsPanel(Static):
    """Shows per-task timing stats."""

    def update_stats(self, db: Database) -> None:
        all_stats = db.get_task_stats()
        if not all_stats:
            self.update("[dim]No statistics yet[/dim]")
            return

        lines = []
        for s in all_stats:
            success_rate = ""
            total = s.success_count + s.failure_count
            if total > 0:
                rate = s.success_count / total * 100
                color = "green" if rate >= 90 else ("yellow" if rate >= 50 else "red")
                success_rate = f"  [{color}]{rate:.0f}%[/{color}]"

            lines.append(
                f"[bold]{s.task_key}[/bold]  "
                f"runs:{s.run_count}  "
                f"avg:{_fmt_duration(s.avg_seconds)}  "
                f"p95:{_fmt_duration(s.p95_seconds)}{success_rate}"
            )

        self.update("\n".join(lines))


# ─── Main App ────────────────────────────────────────────────


class SeaGangDashboard(App):
    """Sea-Gang live monitoring dashboard."""

    TITLE = "🌊 Sea-Gang Dashboard"
    CSS = """
    Screen {
        layout: grid;
        grid-size: 2 3;
        grid-columns: 2fr 1fr;
        grid-rows: auto 1fr auto;
        grid-gutter: 1;
        padding: 1;
    }

    #current-job {
        column-span: 2;
        height: auto;
        min-height: 5;
        border: round grey50;
        padding: 1;
    }

    #queue-table {
        border: round grey50;
        border-title-color: white;
    }

    #history-table {
        border: round grey50;
        border-title-color: white;
    }

    #stats-panel {
        border: round grey50;
        padding: 1;
        height: auto;
        min-height: 3;
    }

    #queue-stats {
        height: auto;
        min-height: 1;
        padding: 0 1;
    }

    #bottom-bar {
        column-span: 2;
        layout: horizontal;
        height: auto;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("k", "kill_job", "Kill Job"),
    ]

    def __init__(self) -> None:
        super().__init__()
        ensure_dirs()
        self.db = Database(DB_PATH)
        self._refresh_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Header()

        yield CurrentJobPanel(id="current-job")

        queue_table = DataTable(id="queue-table")
        queue_table.border_title = "📋 Queue"
        queue_table.add_columns("#", "ID", "Task", "Enqueued")
        yield queue_table

        history_table = DataTable(id="history-table")
        history_table.border_title = "📜 History"
        history_table.add_columns("ID", "Task", "Status", "Exit", "Runtime")
        yield history_table

        with Container(id="bottom-bar"):
            yield QueueStatsPanel(id="queue-stats")
            yield StatsPanel(id="stats-panel")

        yield Footer()

    def on_mount(self) -> None:
        self._refresh_timer = self.set_interval(2.0, self._refresh_data)
        self._refresh_data()

    def _refresh_data(self) -> None:
        """Refresh all dashboard data from SQLite."""
        # Current job
        current_panel = self.query_one("#current-job", CurrentJobPanel)
        running = self.db.get_running_job()
        current_panel.update_job(running)

        # Queue table
        queue_table = self.query_one("#queue-table", DataTable)
        queue_table.clear()
        pending = self.db.get_pending_jobs()
        for i, job in enumerate(pending[:20], 1):
            queue_table.add_row(
                str(i),
                job.id,
                job.display_name,
                job.enqueued_at.strftime("%H:%M:%S"),
            )
        queue_table.border_title = f"📋 Queue ({len(pending)})"

        # History table
        history_table = self.query_one("#history-table", DataTable)
        history_table.clear()
        recent = self.db.get_recent_jobs(15)
        for job in recent:
            status_display = job.status.value
            history_table.add_row(
                job.id,
                job.display_name,
                status_display,
                str(job.exit_code or "—"),
                _fmt_duration(job.runtime_seconds),
            )

        # Queue stats
        from .queue import JobQueue
        queue_mgr = JobQueue(self.db)
        snap = queue_mgr.snapshot()
        self.query_one("#queue-stats", QueueStatsPanel).update_stats(
            snap.depth, snap.max_size, snap.is_draining
        )

        # Task stats
        self.query_one("#stats-panel", StatsPanel).update_stats(self.db)

    def action_refresh(self) -> None:
        self._refresh_data()

    def action_kill_job(self) -> None:
        import os
        import signal as sig

        running = self.db.get_running_job()
        if running and running.pid:
            try:
                os.killpg(os.getpgid(running.pid), sig.SIGTERM)
                self.notify(f"Sent SIGTERM to {running.display_name}", severity="warning")
            except (OSError, ProcessLookupError):
                self.notify("Could not kill process", severity="error")
        else:
            self.notify("No job running", severity="information")


def run_dashboard() -> None:
    """Entry point for the dashboard."""
    app = SeaGangDashboard()
    app.run()
