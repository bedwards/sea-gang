"""YAML configuration loader for sea-gang projects."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .models import HealthCheck, ProjectDef, TaskDef

# Default locations
CONFIG_DIR = Path(os.environ.get("SEA_GANG_CONFIG_DIR", "~/.config/sea-gang")).expanduser()
PROJECTS_DIR = CONFIG_DIR / "projects"
DB_PATH = CONFIG_DIR / "sea-gang.db"
LOG_DIR = CONFIG_DIR / "logs"

# Global defaults
DEFAULTS = {
    "max_queue_size": 10,
    "default_soft_timeout_minutes": 30,
    "default_hard_timeout_minutes": 60,
    "output_check_interval_seconds": 60,
    "output_grace_extension_seconds": 300,
}


def ensure_dirs() -> None:
    """Create config directories if they don't exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_project(path: Path) -> ProjectDef:
    """Load a single project definition from a YAML file."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    proj_data = raw.get("project", raw)

    # Parse healthchecks
    healthchecks = []
    for hc in proj_data.get("healthchecks", []):
        healthchecks.append(HealthCheck(
            name=hc["name"],
            command=hc["command"],
            expect_exit_code=hc.get("expect_exit_code"),
            expect_contains=hc.get("expect_contains"),
        ))

    # Parse tasks
    tasks: dict[str, TaskDef] = {}
    for task_name, task_data in proj_data.get("tasks", {}).items():
        tasks[task_name] = TaskDef(
            name=task_name,
            command=task_data["command"],
            soft_timeout_minutes=task_data.get("soft_timeout_minutes", DEFAULTS["default_soft_timeout_minutes"]),
            hard_timeout_minutes=task_data.get("hard_timeout_minutes", DEFAULTS["default_hard_timeout_minutes"]),
            expected_minutes=task_data.get("expected_minutes", 15),
            schedule=task_data.get("schedule"),
        )

    return ProjectDef(
        name=proj_data["name"],
        working_dir=proj_data["working_dir"],
        tasks=tasks,
        healthchecks=healthchecks,
        env=proj_data.get("env", {}),
    )


def load_all_projects() -> dict[str, ProjectDef]:
    """Load all project definitions from the config directory."""
    projects: dict[str, ProjectDef] = {}
    if not PROJECTS_DIR.exists():
        return projects

    for path in sorted(PROJECTS_DIR.glob("*.yaml")):
        try:
            proj = load_project(path)
            projects[proj.name] = proj
        except Exception as e:
            # Log but don't crash — one bad config shouldn't kill everything
            print(f"[warning] Failed to load {path.name}: {e}")

    return projects


def validate_project(proj: ProjectDef) -> list[str]:
    """Validate a project definition. Returns list of issues."""
    issues: list[str] = []

    if not Path(proj.working_dir).is_dir():
        issues.append(f"Working directory does not exist: {proj.working_dir}")

    for task_name, task_def in proj.tasks.items():
        if task_def.soft_timeout_minutes > task_def.hard_timeout_minutes:
            issues.append(
                f"Task '{task_name}': soft_timeout ({task_def.soft_timeout_minutes}m) "
                f"> hard_timeout ({task_def.hard_timeout_minutes}m)"
            )
        if not task_def.command.strip():
            issues.append(f"Task '{task_name}': empty command")

    return issues
