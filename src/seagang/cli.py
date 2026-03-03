"""Rich CLI for sea-gang task orchestrator."""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich.panel import Panel
from rich.layout import Layout

from .config import (
    DB_PATH,
    LOG_DIR,
    PROJECTS_DIR,
    ensure_dirs,
    load_all_projects,
    load_project,
    validate_project,
)
from .db import Database
from .models import Job, JobStatus
from .monitor import format_job_health
from .queue import DuplicateJobError, JobQueue, QueueDrainingError, QueueFullError
from .runner import Runner
from .scheduler import Scheduler

console = Console()


def _get_db() -> Database:
    ensure_dirs()
    return Database(DB_PATH)


def _get_queue(db: Database) -> JobQueue:
    return JobQueue(db)


def _status_style(status: JobStatus) -> str:
    return {
        JobStatus.PENDING: "yellow",
        JobStatus.RUNNING: "bold cyan",
        JobStatus.COMPLETED: "green",
        JobStatus.FAILED: "red",
        JobStatus.KILLED_SOFT: "magenta",
        JobStatus.KILLED_HARD: "bold red",
        JobStatus.REJECTED: "dim red",
    }.get(status, "white")


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _job_table(jobs: list[Job], title: str = "Jobs", show_position: bool = False) -> Table:
    table = Table(title=title, show_lines=False, padding=(0, 1))
    if show_position:
        table.add_column("#", style="dim", width=3)
    table.add_column("ID", style="cyan", width=12)
    table.add_column("Task", style="bold")
    table.add_column("Status", width=12)
    table.add_column("Runtime", justify="right", width=8)
    table.add_column("Enqueued", style="dim", width=19)

    for i, job in enumerate(jobs, 1):
        status_text = Text(job.status.value, style=_status_style(job.status))
        row = []
        if show_position:
            row.append(str(i))
        row.extend([
            job.id,
            job.display_name,
            status_text,
            _format_duration(job.runtime_seconds),
            job.enqueued_at.strftime("%Y-%m-%d %H:%M:%S"),
        ])
        table.add_row(*row)

    return table


# ─── CLI Group ───────────────────────────────────────────────


@click.group()
@click.option("--json-output", "json_mode", is_flag=True, help="Output as JSON")
@click.pass_context
def cli(ctx: click.Context, json_mode: bool) -> None:
    """🌊 Sea-Gang — Lightweight serial task orchestrator."""
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_mode


# ─── Status ──────────────────────────────────────────────────


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Dashboard: current job, queue depth, recent completions."""
    db = _get_db()
    queue = _get_queue(db)
    snap = queue.snapshot()

    if ctx.obj["json"]:
        data = {
            "current_job": format_job_health(snap.current_job) if snap.current_job else None,
            "queue_depth": snap.depth,
            "max_queue_size": snap.max_size,
            "draining": snap.is_draining,
            "recent": [
                {"id": j.id, "name": j.display_name, "status": j.status.value,
                 "runtime": round(j.runtime_seconds or 0, 1)}
                for j in db.get_recent_jobs(5)
            ],
        }
        click.echo(json.dumps(data, indent=2))
        return

    # Current job panel
    if snap.current_job:
        j = snap.current_job
        runtime = _format_duration(j.runtime_seconds)
        pct = ""
        if j.expected_seconds > 0 and j.runtime_seconds:
            pct = f" ({min(j.runtime_seconds / j.expected_seconds * 100, 999):.0f}%)"
        content = (
            f"[bold cyan]{j.display_name}[/] (id: {j.id})\n"
            f"Runtime: {runtime}{pct}  |  "
            f"Soft: {_format_duration(j.soft_timeout_seconds)}  |  "
            f"Hard: {_format_duration(j.hard_timeout_seconds)}"
        )
        console.print(Panel(content, title="▶ Running", border_style="cyan"))
    else:
        console.print(Panel("[dim]No job running[/]", title="▶ Running", border_style="dim"))

    # Queue depth
    drain_text = " [bold red](DRAINING)[/]" if snap.is_draining else ""
    console.print(f"\n📋 Queue: {snap.depth}/{snap.max_size}{drain_text}")

    if snap.pending_jobs:
        console.print(_job_table(snap.pending_jobs[:5], "Pending", show_position=True))

    # Recent history
    recent = db.get_recent_jobs(5)
    if recent:
        console.print()
        console.print(_job_table(recent, "Recent"))


# ─── Queue ───────────────────────────────────────────────────


@cli.command()
@click.option("--watch", is_flag=True, help="Live-updating view")
@click.pass_context
def queue(ctx: click.Context, watch: bool) -> None:
    """List all pending jobs."""
    db = _get_db()

    if watch:
        try:
            with Live(console=console, refresh_per_second=0.5) as live:
                while True:
                    q = JobQueue(db)
                    snap = q.snapshot()
                    table = _job_table(snap.pending_jobs, f"Queue ({snap.depth}/{snap.max_size})", show_position=True)
                    if snap.current_job:
                        j = snap.current_job
                        header = Text(f"▶ {j.display_name} ({_format_duration(j.runtime_seconds)})", style="bold cyan")
                        live.update(Panel.fit(table, title=str(header)))
                    else:
                        live.update(table)
                    time.sleep(2)
        except KeyboardInterrupt:
            return
    else:
        jobs = db.get_pending_jobs()
        if ctx.obj["json"]:
            click.echo(json.dumps([
                {"position": i, "id": j.id, "name": j.display_name,
                 "enqueued": j.enqueued_at.isoformat()}
                for i, j in enumerate(jobs, 1)
            ], indent=2))
        elif jobs:
            console.print(_job_table(jobs, "Pending Queue", show_position=True))
        else:
            console.print("[dim]Queue is empty[/]")


# ─── History ─────────────────────────────────────────────────


@cli.command()
@click.option("--limit", "-n", default=20, help="Number of recent jobs")
@click.pass_context
def history(ctx: click.Context, limit: int) -> None:
    """Recent completed/failed/killed jobs."""
    db = _get_db()
    jobs = db.get_recent_jobs(limit)

    if ctx.obj["json"]:
        click.echo(json.dumps([
            {"id": j.id, "name": j.display_name, "status": j.status.value,
             "exit_code": j.exit_code, "runtime": round(j.runtime_seconds or 0, 1),
             "finished": j.finished_at.isoformat() if j.finished_at else None}
            for j in jobs
        ], indent=2))
    elif jobs:
        table = Table(title="Job History", show_lines=False, padding=(0, 1))
        table.add_column("ID", style="cyan", width=12)
        table.add_column("Task", style="bold")
        table.add_column("Status", width=12)
        table.add_column("Exit", justify="right", width=5)
        table.add_column("Runtime", justify="right", width=8)
        table.add_column("Finished", style="dim", width=19)

        for j in jobs:
            table.add_row(
                j.id, j.display_name,
                Text(j.status.value, style=_status_style(j.status)),
                str(j.exit_code or "—"),
                _format_duration(j.runtime_seconds),
                j.finished_at.strftime("%Y-%m-%d %H:%M:%S") if j.finished_at else "—",
            )
        console.print(table)
    else:
        console.print("[dim]No job history[/]")


# ─── Submit ──────────────────────────────────────────────────


@cli.command()
@click.argument("project")
@click.argument("task")
@click.pass_context
def submit(ctx: click.Context, project: str, task: str) -> None:
    """Manually enqueue a task: sea-gang submit <project> <task>."""
    db = _get_db()
    queue_mgr = _get_queue(db)
    projects = load_all_projects()

    if project not in projects:
        available = ", ".join(projects.keys()) or "none"
        console.print(f"[red]Unknown project: {project}[/] (available: {available})")
        sys.exit(1)

    proj = projects[project]
    if task not in proj.tasks:
        available = ", ".join(proj.tasks.keys()) or "none"
        console.print(f"[red]Unknown task: {task}[/] (available: {available})")
        sys.exit(1)

    task_def = proj.tasks[task]
    try:
        job = queue_mgr.enqueue(proj, task_def)
        if ctx.obj["json"]:
            click.echo(json.dumps({"id": job.id, "status": "enqueued", "depth": queue_mgr.depth()}))
        else:
            console.print(f"[green]✓[/] Enqueued {job.display_name} (id: [cyan]{job.id}[/], depth: {queue_mgr.depth()})")
    except (QueueFullError, DuplicateJobError, QueueDrainingError) as e:
        if ctx.obj["json"]:
            click.echo(json.dumps({"error": str(e)}))
        else:
            console.print(f"[red]✗ Rejected:[/] {e}")
        sys.exit(1)


# ─── Kill ────────────────────────────────────────────────────


@cli.command("kill")
@click.argument("job_id", required=False)
def kill_job(job_id: str | None) -> None:
    """Kill the currently running job (or a specific job by ID)."""
    db = _get_db()
    running = db.get_running_job()

    if not running:
        console.print("[dim]No job currently running[/]")
        return

    if job_id and running.id != job_id:
        console.print(f"[red]Job {job_id} is not the running job[/] (running: {running.id})")
        return

    if running.pid:
        import os
        try:
            os.killpg(os.getpgid(running.pid), signal.SIGTERM)
            console.print(f"[yellow]Sent SIGTERM to {running.display_name} (pid={running.pid})[/]")
        except (OSError, ProcessLookupError):
            console.print(f"[red]Could not kill process {running.pid}[/]")
    else:
        console.print("[red]No PID recorded for running job[/]")


# ─── Remove ──────────────────────────────────────────────────


@cli.command()
@click.argument("job_id")
def remove(job_id: str) -> None:
    """Remove a pending job from the queue."""
    db = _get_db()
    queue_mgr = _get_queue(db)
    if queue_mgr.remove(job_id):
        console.print(f"[green]✓[/] Removed job {job_id}")
    else:
        console.print(f"[red]✗[/] Job {job_id} not found or not pending")


# ─── Move ────────────────────────────────────────────────────


@cli.command()
@click.argument("job_id")
@click.argument("position", type=int)
def move(job_id: str, position: int) -> None:
    """Move a pending job to a new position in the queue."""
    db = _get_db()
    queue_mgr = _get_queue(db)
    if queue_mgr.move(job_id, position):
        console.print(f"[green]✓[/] Moved job {job_id} to position {position}")
    else:
        console.print(f"[red]✗[/] Job {job_id} not found or not pending")


# ─── Clear ───────────────────────────────────────────────────


@cli.command()
@click.confirmation_option(prompt="Clear all pending jobs?")
def clear() -> None:
    """Remove all pending jobs from the queue."""
    db = _get_db()
    queue_mgr = _get_queue(db)
    count = queue_mgr.clear()
    console.print(f"[green]✓[/] Cleared {count} pending jobs")


# ─── Drain ───────────────────────────────────────────────────


@cli.command()
def drain() -> None:
    """Stop accepting new jobs, finish current work."""
    console.print("[yellow]Drain mode is a runtime state — use with 'sea-gang run'[/]")
    console.print("To clear the queue instead: [bold]sea-gang clear[/]")


# ─── Logs ────────────────────────────────────────────────────


@cli.command()
@click.argument("job_id", required=False)
@click.option("--lines", "-n", default=20, help="Number of lines to show")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def logs(job_id: str | None, lines: int, follow: bool) -> None:
    """Tail output of current or specific job."""
    db = _get_db()

    if job_id:
        job = db.get_job(job_id)
    else:
        job = db.get_running_job()

    if not job:
        console.print("[dim]No job found[/]")
        return

    # Try log file first
    log_path = LOG_DIR / job.project / f"{job.task}_{job.id}.log"
    if log_path.exists():
        if follow:
            console.print(f"[dim]Following {log_path}...[/]")
            try:
                proc = subprocess.Popen(
                    ["tail", "-f", "-n", str(lines), str(log_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for line in proc.stdout:
                    click.echo(line.decode("utf-8", errors="replace"), nl=False)
            except KeyboardInterrupt:
                proc.terminate()
        else:
            with open(log_path) as f:
                all_lines = f.readlines()
                for line in all_lines[-lines:]:
                    click.echo(line, nl=False)
    elif job.output_tail:
        tail = job.output_tail.split("\n")[-lines:]
        for line in tail:
            click.echo(line)
    else:
        console.print("[dim]No output available[/]")


# ─── Stats ───────────────────────────────────────────────────


@cli.command()
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Per-task timing statistics."""
    db = _get_db()
    all_stats = db.get_task_stats()

    if ctx.obj["json"]:
        click.echo(json.dumps([
            {"task": s.task_key, "runs": s.run_count, "avg": round(s.avg_seconds, 1),
             "p95": round(s.p95_seconds, 1), "max": round(s.max_seconds, 1),
             "success": s.success_count, "failure": s.failure_count}
            for s in all_stats
        ], indent=2))
        return

    if not all_stats:
        console.print("[dim]No stats yet — run some tasks first[/]")
        return

    table = Table(title="Task Statistics", show_lines=False, padding=(0, 1))
    table.add_column("Task", style="bold")
    table.add_column("Runs", justify="right")
    table.add_column("Avg", justify="right")
    table.add_column("P95", justify="right")
    table.add_column("Max", justify="right")
    table.add_column("✓/✗", justify="right")

    for s in all_stats:
        table.add_row(
            s.task_key,
            str(s.run_count),
            _format_duration(s.avg_seconds),
            _format_duration(s.p95_seconds),
            _format_duration(s.max_seconds),
            f"[green]{s.success_count}[/]/[red]{s.failure_count}[/]",
        )
    console.print(table)


# ─── Projects ────────────────────────────────────────────────


@cli.command()
@click.pass_context
def projects(ctx: click.Context) -> None:
    """List configured projects and their tasks."""
    all_projects = load_all_projects()

    if ctx.obj["json"]:
        data = {}
        for name, proj in all_projects.items():
            data[name] = {
                "working_dir": proj.working_dir,
                "tasks": {
                    t: {"schedule": td.schedule, "soft_timeout": td.soft_timeout_minutes,
                        "hard_timeout": td.hard_timeout_minutes}
                    for t, td in proj.tasks.items()
                },
            }
        click.echo(json.dumps(data, indent=2))
        return

    if not all_projects:
        console.print(f"[dim]No projects configured in {PROJECTS_DIR}[/]")
        return

    for name, proj in all_projects.items():
        issues = validate_project(proj)
        status_icon = "🔴" if issues else "🟢"
        console.print(f"\n{status_icon} [bold]{name}[/] — {proj.working_dir}")

        for task_name, task_def in proj.tasks.items():
            sched = f"  ⏰ {task_def.schedule}" if task_def.schedule else ""
            console.print(
                f"   [cyan]{task_name}[/]: "
                f"soft={task_def.soft_timeout_minutes}m, "
                f"hard={task_def.hard_timeout_minutes}m"
                f"{sched}"
            )

        for issue in issues:
            console.print(f"   [red]⚠ {issue}[/]")


# ─── Healthcheck ─────────────────────────────────────────────


@cli.command()
@click.argument("project")
@click.pass_context
def healthcheck(ctx: click.Context, project: str) -> None:
    """Run healthchecks for a project."""
    all_projects = load_all_projects()
    if project not in all_projects:
        console.print(f"[red]Unknown project: {project}[/]")
        sys.exit(1)

    proj = all_projects[project]
    all_ok = True
    results = []

    for hc in proj.healthchecks:
        try:
            result = subprocess.run(
                hc.command, shell=True, capture_output=True, text=True,
                cwd=proj.working_dir, timeout=30,
            )
            ok = True
            if hc.expect_exit_code is not None and result.returncode != hc.expect_exit_code:
                ok = False
            if hc.expect_contains and hc.expect_contains not in result.stdout:
                ok = False

            results.append({"name": hc.name, "ok": ok, "exit_code": result.returncode})
            if not ok:
                all_ok = False
        except Exception as e:
            results.append({"name": hc.name, "ok": False, "error": str(e)})
            all_ok = False

    if ctx.obj["json"]:
        click.echo(json.dumps({"project": project, "ok": all_ok, "checks": results}, indent=2))
    else:
        for r in results:
            icon = "[green]✓[/]" if r["ok"] else "[red]✗[/]"
            console.print(f"  {icon} {r['name']}")
        console.print(f"\n{'[green]All checks passed[/]' if all_ok else '[red]Some checks failed[/]'}")

    sys.exit(0 if all_ok else 1)


# ─── Run (daemon) ────────────────────────────────────────────


# ─── Dashboard ───────────────────────────────────────────────


@cli.command()
def dashboard() -> None:
    """Launch the live TUI dashboard."""
    from .dashboard import run_dashboard
    run_dashboard()


# ─── Run (daemon) ────────────────────────────────────────────


@cli.command()
@click.option("--once", is_flag=True, help="Process one job and exit")
@click.option("--no-schedule", is_flag=True, help="Don't start the cron scheduler")
def run(once: bool, no_schedule: bool) -> None:
    """Start the task runner (and optionally the cron scheduler)."""
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    db = _get_db()
    queue_mgr = _get_queue(db)
    runner = Runner(db)
    scheduler = None

    if not no_schedule and not once:
        scheduler = Scheduler(queue_mgr)
        count = scheduler.load_projects()
        if count > 0:
            scheduler.start()
            console.print(f"[green]Scheduler started with {count} scheduled tasks[/]")
        else:
            console.print("[yellow]No scheduled tasks found[/]")

    # Handle graceful shutdown
    stop = False

    def handle_signal(signum, frame):
        nonlocal stop
        stop = True
        runner.stop()
        console.print("\n[yellow]Shutting down gracefully...[/]")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    console.print("[bold]🌊 Sea-Gang runner started[/]")

    try:
        while not stop:
            job = queue_mgr.dequeue()
            if job:
                runner.run_job(job)
                if once:
                    break
            else:
                if once:
                    console.print("[dim]No jobs in queue[/]")
                    break
                # Sleep before checking again
                for _ in range(10):  # 10 x 1s = check every 10s
                    if stop:
                        break
                    time.sleep(1)
    finally:
        if scheduler:
            scheduler.stop()
        console.print("[dim]Runner stopped[/]")


if __name__ == "__main__":
    cli()
