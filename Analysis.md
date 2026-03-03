# Hex-Index → CrewAI Integration Analysis

## 1. Is Hex-Index Set Up to Use Mistral on Local Ollama?

**Yes — mostly.** The code is fully wired for local Ollama with Mistral:

| Component | Status | Detail |
|-----------|--------|--------|
| [ollama.ts](file:///Users/bedwards/hex-index/src/wikipedia/ollama.ts) | ✅ Ready | HTTP client hitting `OLLAMA_URL/api/chat`, retry logic for connection/timeout failures |
| [analyzer.ts](file:///Users/bedwards/hex-index/src/wikipedia/analyzer.ts) | ✅ Ready | Imports `generateText` from `ollama.ts` for topic analysis |
| [rewriter.ts](file:///Users/bedwards/hex-index/src/wikipedia/rewriter.ts) | ✅ Ready | Imports `generateText` from `ollama.ts` for article rewriting |
| [.env.example](file:///Users/bedwards/hex-index/.env.example) | ✅ Has vars | `OLLAMA_URL=http://127.0.0.1:11434` and `OLLAMA_MODEL=mistral-large:123b` |
| [.env](file:///Users/bedwards/hex-index/.env) | ⚠️ **Missing** | `OLLAMA_URL` and `OLLAMA_MODEL` are **not present** in your actual `.env` |

> [!WARNING]
> Your `.env` never had the Ollama vars added. The code will still work because `ollama.ts` has hardcoded defaults (`http://127.0.0.1:11434` and `mistral-large:123b`), but add them to `.env` for explicit configuration. Also: you need to have `mistral-large:123b` actually pulled in Ollama (`ollama pull mistral-large:123b`). This is a huge model (~70GB). If you want something lighter, `mistral:7b` or `mistral-nemo` work fine — just change the env var.

---

## 2. What Needs to Change in Hex-Index for CrewAI Orchestration?

The good news: **very little.** The project is already well-structured for external orchestration.

### Already Good ✅
- **Clean CLI entry points** with proper exit codes: `npm run ingest` (exit 0/1), `npx tsx src/wikipedia/cli.ts retrofit|enrich|stats` (exit 0/1)
- **Environment-driven config** via `.env` — no hardcoded secrets
- **Docker Compose** for Postgres — can be started independently
- **Idempotent operations** — `retrofit` skips already-enriched articles, `ingest` skips already-fetched articles
- **Structured output** to stdout — easy to capture/parse

### Recommended Changes 🔧

#### a) Add OLLAMA vars to `.env`
```bash
# Ollama (for Wikipedia topic analysis and rewriting)
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=mistral-large:123b
```

#### b) Add a "health check" CLI command
CrewAI tools work best when they can pre-validate. A single command that checks all dependencies:
```bash
npx tsx src/wikipedia/cli.ts healthcheck
# → Checks: Postgres connection, Ollama reachability, model availability
# → Exit 0 if all good, exit 1 with details of what's broken
```

#### c) Consider a JSON output mode
CrewAI agents parse structured output better than human-readable text. Adding `--json` flags to CLI commands would help:
```bash
npm run ingest -- --source content/sources.json --json
# → {"totalSources": 5, "successfulSources": 5, "articlesStored": 42, ...}
```

#### d) Add a timeout env var for long-running operations
The rewriter has a 15-minute default timeout per article. For scheduled queue management, an env var like `HEX_TASK_TIMEOUT_MS=900000` would let the orchestrator control timeouts externally.

---

## 3. Advice for the CrewAI Orchestrator Project

### Architecture: Serial Queue with Backpressure

```
┌─────────────┐     ┌──────────────┐     ┌───────────────┐
│  Scheduler   │────▶│  Job Queue   │────▶│  Worker Loop  │
│  (cron/APScheduler)│  (Redis/file)  │     │  (serial exec) │
└─────────────┘     └──────────────┘     └───────────────┘
                          │                       │
                    max_queue_size          timeout_per_job
                    reject if full         kill if exceeded
```

### Key Design Decisions

| Decision | Recommendation |
|----------|---------------|
| **Queue backend** | Start with SQLite or a JSON file — you're running serial jobs locally, Redis is overkill. Move to Redis if you later want distributed workers. |
| **Scheduler** | `APScheduler` (Python) with `CronTrigger` — built-in, no external deps needed for cron-like scheduling. |
| **Process execution** | `subprocess.run()` or `asyncio.create_subprocess_exec()` — shell out to each project's CLI commands. Don't import Node.js code into Python. |
| **CrewAI's role** | CrewAI manages the *decision-making layer* (what to run, what to do on failure, how to report). The actual work is done by shelling out to project CLIs. |

### Recommended CrewAI Crew Structure

```python
from crewai import Agent, Task, Crew, Process

# Agent 1: The Scheduler/Dispatcher
scheduler_agent = Agent(
    role="Job Orchestrator",
    goal="Manage serial execution of background tasks across projects",
    tools=[QueueManagerTool, HealthCheckTool, ShellExecutorTool],
    llm="ollama/mistral-nemo"  # Use a small, fast model for orchestration
)

# Agent 2: The Monitor  
monitor_agent = Agent(
    role="Job Monitor",
    goal="Track job health, kill hung jobs, report failures",
    tools=[ProcessMonitorTool, NotificationTool],
    llm="ollama/mistral-nemo"
)
```

### Custom Tools to Build

1. **`ShellExecutorTool`** — Runs a shell command with timeout, captures stdout/stderr, returns exit code. This is the core tool that executes hex-index CLI commands.

2. **`QueueManagerTool`** — Manages the FIFO queue:
   - `enqueue(job)` — Adds a job; rejects if queue > `max_queue_size`
   - `dequeue()` — Gets next job
   - `status()` — Returns queue depth and current job info

3. **`HealthCheckTool`** — Runs pre-flight checks before a job:
   - Is Docker/Postgres running?
   - Is Ollama running and has the right model?
   - Is the target project directory valid?

4. **`ProcessMonitorTool`** — Watches running subprocess:
   - Kill if exceeds `max_runtime`
   - Capture output for logging

### Project-Agnostic Job Definition

Define each project's tasks in a YAML config so CrewAI doesn't need hex-index-specific knowledge:

```yaml
# ~/.crewai-scheduler/projects/hex-index.yaml
project:
  name: hex-index
  working_dir: /Users/bedwards/hex-index
  
  # Pre-flight checks
  healthchecks:
    - command: "docker compose ps postgres --format json"
      expect_contains: "running"
    - command: "curl -s http://127.0.0.1:11434/api/tags"
      expect_status: 200

  tasks:
    ingest:
      command: "npm run ingest -- --source content/sources.json --verbose"
      timeout_minutes: 30
      schedule: "0 6 * * *"   # Daily at 6 AM
      
    wikipedia_enrich:
      command: "npx tsx src/wikipedia/cli.ts retrofit --limit 5"
      timeout_minutes: 120     # LLM rewriting is slow
      schedule: "0 8 * * *"   # Daily at 8 AM
      depends_on: ingest       # Run after ingest completes
      
    static_site:
      command: "npm run static:generate"
      timeout_minutes: 10
      schedule: "0 10 * * *"
      depends_on: wikipedia_enrich
```

### Queue Backpressure Strategy

```python
MAX_QUEUE_SIZE = 5          # Reject new jobs if queue exceeds this
MAX_JOB_RUNTIME = 7200      # Kill job after 2 hours (seconds)
STALE_JOB_THRESHOLD = 3600  # Consider job stale after 1 hour

class JobQueue:
    def enqueue(self, job: Job) -> bool:
        if len(self.pending) >= MAX_QUEUE_SIZE:
            logger.warning(f"Queue full ({len(self.pending)}), rejecting: {job.name}")
            return False
        self.pending.append(job)
        return True
    
    def check_current_job(self):
        if self.current and self.current.runtime > MAX_JOB_RUNTIME:
            self.current.process.kill()
            logger.error(f"Killed hung job: {self.current.name}")
```

### Model Choice for Orchestration

> [!TIP]
> Use a **small, fast model** for the CrewAI orchestrator itself (e.g., `mistral-nemo` or `qwen2.5:7b`). The orchestrator is making simple decisions (should I run this job? is this job stuck?). Save `mistral-large:123b` for the actual hex-index Wikipedia analysis/rewriting work. Running both on the same Ollama instance is fine since jobs are serial.

### Key Pitfalls to Avoid

1. **Don't let CrewAI agents call Ollama while hex-index is also using it** — Ollama serializes requests, so a CrewAI agent waiting for a response will block hex-index and vice versa. Use the small model for orchestration, big model for content work.

2. **Don't over-agent this** — A simple Python script with APScheduler + subprocess is 90% of what you need. Use CrewAI for the *smart* parts: deciding whether to retry a failed job, summarizing results, adapting schedules based on outcomes.

3. **Make jobs idempotent** — Hex-index already does this (skips already-processed articles). Ensure all future projects follow the same pattern.

4. **Log everything** — Capture all stdout/stderr from subprocesses to files. CrewAI's `verbose=True` plus subprocess output capture gives you full observability.

5. **Start simple, add complexity later** — Don't build Redis queues and Celery workers on day one. A SQLite-backed queue with `subprocess.run()` handles serial local jobs perfectly.
