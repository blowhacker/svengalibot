# Svengalibot

A multi-agent AI collaboration system that orchestrates AI models to work together on tasks. Claude acts as the worker (executing tasks, writing code/content), while another AI (OpenAI, Claude, or others) acts as a collaborator, creating an iterative feedback loop.

## Why Svengalibot?

Modern AI assistants are powerful but work in isolation. Svengalibot introduces **collaborative AI** - multiple AI models working together with complementary strengths:

- **Worker (Claude)**: Executes tasks, writes files, makes commits
- **Collaborator (OpenAI/Claude)**: Provides feedback, suggests improvements, validates work
- **Human (You)**: Supervises, approves, intervenes when needed

This creates a workflow similar to how human teams operate - one person does the work, another provides feedback, and iteration continues until the result meets standards.

## Features

- **Multi-Agent Collaboration**: Claude executes tasks, OpenAI (or another Claude) provides feedback
- **Flexible Collaboration Types**: Code review, brainstorming, writing & editing, research, debate
- **Claude-Only Mode**: Skip the collaborator for simple tasks
- **Real-time Streaming**: Watch AI work in real-time via web UI
- **Project Management**: Organize work into projects with their own codebases
- **Code Guide System**: Define coding standards that AIs must follow
- **Human-in-the-Loop**: Pause, approve, reject, or intervene at any point
- **Iteration Control**: Set max iterations for review cycles
- **File Browser**: View and navigate project files in the UI
- **Diff Viewer**: See exactly what changes each iteration made
- **Docker Isolation** (optional): Run Claude CLI in isolated containers

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Web UI (Flask)                          │
│  Project Browser │ Chat Interface │ File Viewer │ Diff Viewer   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Orchestrator (Python)                      │
│   Routes │ State Manager │ Task Queue │ SSE Broadcasting        │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
┌──────────────────────────┐    ┌──────────────────────────────┐
│   Worker (Claude CLI)    │    │ Collaborator (OpenAI/Claude) │
│                          │    │                              │
│  - Reads task spec       │    │  - Reviews work output       │
│  - Writes/edits files    │    │  - Provides feedback         │
│  - Makes git commits     │    │  - Suggests improvements     │
│  - Reports summary       │    │  - Approves or iterates      │
└──────────────────────────┘    └──────────────────────────────┘
```

## Requirements

- **Python 3.10+**
- **Claude CLI** (authenticated via `claude` command)
- **OpenAI API key** (optional, for reviewer mode)
- **Docker** (optional, for isolated execution)
- **Git**

## Installation

### Quick Start

```bash
git clone https://github.com/blowhacker/svengalibot.git
cd svengalibot
./install.sh
```

The installer handles:
- System dependencies (python3, git, curl)
- Python virtual environment
- pip dependencies
- Directory structure
- Default configuration

### Manual Installation

```bash
# Clone the repo
git clone https://github.com/blowhacker/svengalibot.git
cd svengalibot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create data directories
mkdir -p data/projects
```

## Configuration

### API Keys

1. Navigate to `http://localhost:5000/setup` after starting
2. Enter your OpenAI API key (optional - only needed for reviewer mode)
3. Claude CLI uses your existing authentication (run `claude` to authenticate)

### Code Guide

Each project can have a `guide.yaml` that defines coding standards:

```yaml
code_style:
  - prefer simple code over clever code
  - max function length: 50 lines
  - always handle errors explicitly

testing:
  - write tests for new functionality
  - test edge cases

security:
  - never commit secrets
  - validate all external input
```

The guide is injected into both worker and reviewer prompts, ensuring consistent standards.

## Usage

### Starting the Server

```bash
source venv/bin/activate
python run.py
```

Open `http://localhost:5000` in your browser.

### Creating a Project

1. Click "New Project" on the home page
2. Select or create a workspace directory (where code will be written)
3. Optionally set up a code guide

### Starting a Task

1. Open a project
2. Choose collaboration mode:
   - **Claude only**: Fast, no review cycle
   - **Claude + OpenAI**: Claude writes, OpenAI reviews
3. Enter your task description
4. Optionally attach files for context
5. Set max iterations (default: 4)
6. Click "Start"

### During Execution

- **Watch real-time**: See Claude's output as it works
- **View files**: Browse the project files in the sidebar
- **Check diffs**: See what changed in each iteration
- **Intervene**: Pause, add instructions, or stop the task

### Collaboration Modes

| Mode | Worker | Collaborator | Use Case |
|------|--------|--------------|----------|
| Claude only | Claude CLI | None | Simple tasks, prototyping |
| Claude + OpenAI | Claude CLI | GPT-4/GPT-5 | Code review, brainstorming, research |
| Claude + Claude | Claude CLI | Claude API | Self-collaboration (coming soon) |

### Collaboration Types

Svengalibot supports various collaboration patterns:

- **Code Review**: Claude implements, collaborator reviews for bugs and quality
- **Brainstorming**: Claude generates ideas, collaborator evaluates and refines
- **Writing & Editing**: Claude writes content, collaborator provides editorial feedback
- **Research**: Claude researches topics, collaborator fact-checks and identifies gaps
- **Debate**: Claude takes a position, collaborator plays devil's advocate

## Execution Modes

### Local Mode (Default)

Claude CLI runs directly on your machine:
- Uses your existing Claude authentication
- Full access to local tools and environment
- Faster startup, no container overhead

### Docker Mode

Claude CLI runs in an isolated container:
- Sandboxed execution environment
- Consistent, reproducible environment
- Requires Docker and the `svengalibot-worker` image

Build the Docker image:
```bash
cd docker
docker build -t svengalibot-worker .
```

Enable Docker mode in project settings or `/setup`.

**Note**: Docker mode requires re-authentication if you haven't used Claude CLI on the host for 8+ hours (OAuth token expiration).

## Project Structure

```
svengalibot/
├── app/
│   ├── routes.py          # Flask routes & API endpoints
│   ├── orchestrator.py    # Task coordination
│   ├── manager.py         # OpenAI reviewer integration
│   ├── worker.py          # Claude CLI execution
│   ├── conversation.py    # Chat/task data models
│   └── state.py           # File-based state management
├── templates/             # HTML templates
├── static/                # CSS and JS
├── prompts/               # AI prompt templates
│   └── manager/
│       ├── plan.txt
│       ├── review.txt
│       └── feedback.txt
├── docker/
│   └── Dockerfile         # Worker container image
├── data/
│   └── projects/          # Project workspaces
├── config.py              # Flask configuration
├── requirements.txt
├── install.sh
└── run.py                 # Entry point
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Home page - project list |
| `/project/<name>` | GET | Project dashboard |
| `/project/<name>/chat/<id>` | GET | Chat/task interface |
| `/project/<name>/chat/<id>/stream` | GET | SSE stream for real-time updates |
| `/project/<name>/files` | GET | List project files |
| `/project/<name>/file` | GET | Read file contents |
| `/setup` | GET/POST | API keys and configuration |

## Troubleshooting

### Claude CLI not authenticated

```bash
claude  # This will open browser for OAuth
```

### OpenAI quota exhausted

The task will pause automatically. Options:
- Add credits to your OpenAI account
- Resume when quota resets
- Use Claude-only mode

### Docker sees expired credentials

If you haven't used Claude CLI for 8+ hours:
```bash
claude  # Re-authenticate on host first
```

### Port already in use

```bash
# Find and kill existing process
lsof -i :5000
kill <PID>

# Or use a different port
FLASK_RUN_PORT=5001 python run.py
```

## Contributing

Contributions welcome! Areas of interest:

- [ ] Claude as reviewer (Claude API integration)
- [ ] Gemini/DeepSeek integration
- [ ] Parallel task execution
- [ ] Cost tracking and budgets
- [ ] Authentication for multi-user deployment
- [ ] VS Code extension

## License

MIT License - see LICENSE file.

## Acknowledgments

- [Claude CLI](https://github.com/anthropics/claude-cli) by Anthropic
- [OpenAI API](https://platform.openai.com/) for reviewer capabilities
- Built with Flask, vanilla JS, and determination
