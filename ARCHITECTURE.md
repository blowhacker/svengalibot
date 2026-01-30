# Svengalibot Architecture

## Overview

A supervisor-worker system for AI-assisted coding:
- **Manager**: ChatGPT (o1) - plans, delegates, reviews, tests, researches
- **Worker**: Claude CLI - executes coding tasks in sandboxed VMs
- **Human**: Ultimate supervisor via web UI

```
┌─────────────────────────────────────────────────────────────────┐
│                         Web UI (Flask)                          │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌───────────┐  │
│  │ Task    │ │ Live    │ │ Diff    │ │ Guide   │ │ Controls  │  │
│  │ List    │ │ Terminal│ │ Viewer  │ │ Editor  │ │ Pause/etc │  │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘ └───────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Orchestrator (Python)                      │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ Task Queue   │  │ State Manager│  │ Git Coordinator       │  │
│  │ (file-based) │  │ (JSON files) │  │ (bare repo on host)   │  │
│  └──────────────┘  └──────────────┘  └───────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
┌──────────────────────┐        ┌──────────────────────────────┐
│   Manager (ChatGPT)  │        │         VM Pool              │
│  ┌────────────────┐  │        │  ┌────────┐ ┌────────┐       │
│  │ Plan tasks     │  │        │  │ VM 1   │ │ VM 2   │ ...   │
│  │ Review code    │  │        │  │(idle)  │ │(active)│       │
│  │ Run tests      │  │        │  └────────┘ └────────┘       │
│  │ Web research   │  │        │                              │
│  │ Approve/reject │  │        │  Each VM has:                │
│  └────────────────┘  │        │  - Claude CLI (authed)       │
│                      │        │  - Git client                │
│  Model: o1           │        │  - Dev tools                 │
│  (configurable)      │        │                              │
└──────────────────────┘        └──────────────────────────────┘
```

---

## Components

### 1. Web UI (Flask + vanilla JS)

**Routes:**
- `GET /` - Dashboard with task list and status
- `GET /task/<id>` - Task detail view
- `POST /task` - Create new task
- `GET /task/<id>/stream` - SSE endpoint for live terminal output
- `POST /task/<id>/pause` - Pause task
- `POST /task/<id>/cancel` - Cancel task
- `POST /task/<id>/approve` - Manual approval for current chunk
- `PUT /task/<id>/plan` - Edit plan mid-execution
- `GET /guide` - View current guide
- `PUT /guide` - Update guide

**Views:**
- Task list with status badges (pending/planning/executing/reviewing/done/failed)
- Live terminal output (websocket or SSE polling)
- Diff viewer for code changes (before/after)
- Guide editor (markdown/YAML)
- Intervention controls

### 2. Orchestrator

Core Python service that coordinates everything.

**Task Queue** (`/data/tasks/`):
```
/data/tasks/
  task_001/
    meta.json        # id, status, created_at, human_approved
    description.txt  # Original task description
    plan.json        # Manager's breakdown into chunks
    chunks/
      chunk_001/
        spec.json    # What to build
        status.json  # pending/in_progress/review/approved/rejected
        attempts/
          attempt_001/
            worker_log.txt
            diff.patch
            review.json
    final_result/
      diff.patch
      summary.md
```

**State Machine per Chunk:**
```
pending → in_progress → review → approved
                ↓           ↓
              failed    rejected → (new attempt)
```

**Human Override States:**
- `paused` - Human paused execution
- `awaiting_approval` - Requires human sign-off before proceeding

### 3. Manager (ChatGPT o1)

Accessed via OpenAI API. Responsibilities:

**Planning:**
- Receives task description + guide
- Breaks into ordered chunks with dependencies
- Specifies acceptance criteria per chunk

**Research:**
- Autonomously decides when to verify implementations
- Uses web search to check algorithms, APIs, best practices
- Example: "Verify CUSUM implementation matches statistical definition"

**Review:**
- Receives diff from worker
- Checks against guide rules
- Checks against chunk acceptance criteria
- Can run functional tests (via VM)

**Prompts stored in:** `/prompts/manager/`
- `plan.txt` - Planning prompt template
- `review.txt` - Review prompt template
- `research.txt` - Research prompt template

### 4. Worker (Claude CLI in VM)

**Invocation:**
```bash
ssh vm-worker-01 "cd /workspace && claude --print 'Build the auth module...'"
```

Or for streaming:
```bash
ssh vm-worker-01 "cd /workspace && claude 'Build the auth module...'"
# Stream stdout back to UI
```

**Worker receives:**
- Chunk specification
- Relevant context (existing code, previous attempt feedback)
- Guide rules

**Worker outputs:**
- Code changes (committed to git)
- Summary of what was done
- Any caveats or questions

### 5. VM Pool (Vagrant)

**Pool Manager:**
- Maintains N pre-warmed VMs (configurable, default 3)
- Assigns VM to task, marks as busy
- Resets VM to clean snapshot after task completes
- Spins up new VMs if pool exhausted

**VM Lifecycle:**
```
[snapshot] → boot → assigned → task runs → git push → reset → [snapshot]
```

**Base Image (`Vagrantfile`):**
- Ubuntu 22.04 (or configurable)
- Claude CLI installed + authenticated
- Git configured (push access to host repo)
- Common dev tools: Python, Node, Go, Rust, etc.
- Shared folder or git clone for code

**Git Flow:**
```
Host (bare repo)
     │
     ├── VM clones on assignment
     │
     ├── Worker commits changes
     │
     ├── Worker pushes to host
     │
     └── Host makes changes available for review
```

### 6. Guide System

SonarQube-inspired rules, stored in `/data/guide.yaml`:

```yaml
code_style:
  - prefer simple code over clever code
  - max function length: 50 lines
  - max file length: 500 lines
  - always handle errors explicitly
  - no magic numbers, use named constants

testing:
  - every public function should be testable
  - prefer unit tests over integration tests
  - test edge cases explicitly

security:
  - never commit secrets
  - validate all external input
  - use parameterized queries

project_specific:
  # User adds their own rules here
```

Guide is:
- Injected into manager prompts (planning, review)
- Injected into worker prompts
- Editable via web UI

---

## Workflow

### Happy Path

```
1. Human submits task via UI
   └── "Build a user authentication system with JWT"

2. Orchestrator creates task, sends to Manager

3. Manager plans (with optional research)
   └── Returns chunks:
       - Chunk 1: User model and database schema
       - Chunk 2: Registration endpoint
       - Chunk 3: Login endpoint with JWT generation
       - Chunk 4: Auth middleware
       - Chunk 5: Integration tests

4. Human reviews plan (optional approval gate)

5. For each chunk:
   a. Orchestrator assigns VM from pool
   b. Worker (Claude CLI) executes in VM
   c. Worker commits and pushes to host git
   d. Manager reviews diff + runs tests
   e. If approved → next chunk
      If rejected → new attempt with feedback

6. All chunks complete → Final review

7. Human reviews final result via UI
```

### Rejection Loop

```
Worker submits chunk
        │
        ▼
Manager reviews ──rejected──→ Manager summarizes issues
        │                            │
     approved                        ▼
        │                     New worker attempt
        ▼                     (fresh context + feedback)
   Next chunk                        │
                                     └──→ (max 3 attempts, then escalate to human)
```

### Human Intervention

At any point, human can:
- **Pause**: Stops after current chunk completes
- **Cancel**: Kills current operation, marks task failed
- **Edit plan**: Modify remaining chunks
- **Approve chunk**: Manually override manager rejection
- **Reject chunk**: Manually override manager approval

---

## File Structure

```
svengalibot/
├── app/
│   ├── __init__.py
│   ├── routes.py           # Flask routes
│   ├── orchestrator.py     # Core coordination logic
│   ├── manager.py          # ChatGPT integration
│   ├── worker.py           # Claude CLI / VM invocation
│   ├── vm_pool.py          # Vagrant VM management
│   ├── git_coordinator.py  # Git operations
│   └── state.py            # File-based state management
├── templates/
│   ├── index.html          # Dashboard
│   ├── task.html           # Task detail view
│   └── guide.html          # Guide editor
├── static/
│   ├── app.js              # Frontend JS
│   └── style.css
├── prompts/
│   └── manager/
│       ├── plan.txt
│       ├── review.txt
│       └── research.txt
├── data/
│   ├── tasks/              # Task state
│   ├── guide.yaml          # Code guide
│   └── config.yaml         # App configuration
├── vagrant/
│   ├── Vagrantfile
│   └── provision.sh        # VM setup script
├── config.py               # Flask config
├── requirements.txt
└── run.py                  # Entry point
```

---

## Configuration

`/data/config.yaml`:
```yaml
manager:
  provider: openai
  model: o1-preview  # or gpt-4o, configurable
  api_key_env: OPENAI_API_KEY

worker:
  type: claude-cli
  vm_pool_size: 3
  max_attempts_per_chunk: 3

vm:
  provider: vagrant
  base_box: ubuntu/jammy64
  memory: 4096
  cpus: 2

git:
  bare_repo_path: /data/repos/workspace.git

ui:
  host: 127.0.0.1
  port: 5000
```

---

## Open Questions / Future Work

1. **Auth**: Should the web UI have authentication? (For v1, assume single-user localhost)

2. **Multiple projects**: One svengalibot instance per project, or multi-project support?

3. **Cost tracking**: Track OpenAI API costs per task?

4. **Caching**: Cache manager research results to avoid repeated lookups?

5. **Parallelism**: Execute independent chunks in parallel? (v2)

---

## Installation

```bash
git clone git@github.com:blowhacker/svengalibot.git
cd svengalibot
./install.sh
```

Or skip prompts with:
```bash
./install.sh --yes
```

The installer handles:
- System dependencies (python3, git, curl)
- Vagrant + VirtualBox
- Claude CLI
- Python venv + pip dependencies
- Directory structure
- Default config files
- Vagrant VM configuration
- Git bare repo for worker communication

---

## Next Steps

1. Set up Flask skeleton with basic routes
2. Implement file-based state management
3. Build ChatGPT manager integration
4. Build Claude CLI worker invocation (local first, then VM)
5. Create Vagrant base image
6. Implement VM pool manager
7. Build web UI views
8. Wire up SSE/polling for live output
9. Add human intervention controls
10. Testing and polish
