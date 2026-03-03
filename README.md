# 🌊 Sea-Gang

Lightweight serial task orchestrator — protects shared Ollama/Mistral resources by running tasks one at a time, with intelligent timeout management and queue backpressure.

## Why?

When multiple projects share a single local Ollama instance, concurrent LLM calls will serialize and slow everything down. Sea-Gang ensures:

- **Serial execution** — one task at a time, no Ollama contention
- **Zero idle cost** — no LLM calls for orchestration itself  
- **Smart timeouts** — soft timeout checks if the task is still producing output before killing
- **Backpressure** — rejects new tasks when the queue gets too deep
- **Full observability** — rich CLI, JSON output, log files, timing statistics

## Quick Start

```bash
# Activate the environment
source .venv/bin/activate

# See configured projects
sea-gang projects

# Submit a task
sea-gang submit hex-index healthcheck

# Run it (process one job and exit)
sea-gang run --once

# Check status
sea-gang status

# View history
sea-gang history

# Start the daemon (with cron scheduler)
sea-gang run
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `sea-gang status` | Dashboard: current job, queue depth, recent completions |
| `sea-gang queue` | List pending jobs (`--watch` for live view) |
| `sea-gang history` | Recent completed/failed/killed jobs |
| `sea-gang submit <project> <task>` | Manually enqueue a task |
| `sea-gang kill [job_id]` | Kill the currently running job |
| `sea-gang remove <job_id>` | Remove a pending job |
| `sea-gang move <job_id> <pos>` | Reorder a job in the queue |
| `sea-gang clear` | Remove all pending jobs |
| `sea-gang logs [job_id]` | Tail job output (`-f` to follow) |
| `sea-gang stats` | Per-task timing statistics (avg, p95, max) |
| `sea-gang projects` | List configured projects and tasks |
| `sea-gang healthcheck <project>` | Run pre-flight checks |
| `sea-gang run` | Start runner + scheduler daemon |
| `sea-gang run --once` | Process one job and exit |

All commands support `--json-output` for machine consumption.

## Timeout Strategy

```
soft_timeout ──── Is it still producing output? ──── YES → extend
                                                 └── NO  → Is runtime < p95? ── YES → extend
                                                                              └── NO  → KILL (killed_soft)

hard_timeout ──── KILL unconditionally (killed_hard)
```

## Adding Projects

Create a YAML file in `~/.config/sea-gang/projects/`:

```yaml
project:
  name: my-project
  working_dir: /path/to/project
  
  env:
    MY_VAR: "value"
  
  healthchecks:
    - name: check-name
      command: "some check command"
      expect_exit_code: 0

  tasks:
    my-task:
      command: "npm run something"
      soft_timeout_minutes: 30
      hard_timeout_minutes: 60
      expected_minutes: 15
      schedule: "0 6 * * *"  # optional cron
```

## Architecture

- **Python 3.13** + pyenv
- **SQLite** for job queue and statistics (no external deps)
- **APScheduler** for cron triggers
- **Rich** + **Click** for CLI
- **subprocess** for task execution with process group management
