"""Orchestrator that coordinates the manager-worker workflow."""

import logging
import threading
import queue
import time
import yaml
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass
from enum import Enum

from app.state import StateManager, Task, TaskStatus, ChunkStatus
from app.manager import Manager
from app.worker import Worker, WorkerResult
from app.git_coordinator import GitCoordinator
from app.vm_pool import VMPool, VMPoolConfig

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


class EventType(Enum):
    TASK_CREATED = "task_created"
    TASK_PLANNING = "task_planning"
    MANAGER_THINKING = "manager_thinking"
    MANAGER_RESPONSE = "manager_response"
    TASK_PLANNED = "task_planned"
    RESEARCH_STARTED = "research_started"
    RESEARCH_COMPLETED = "research_completed"
    CHUNK_STARTED = "chunk_started"
    WORKER_STARTED = "worker_started"
    CHUNK_OUTPUT = "chunk_output"
    CHUNK_COMPLETED = "chunk_completed"
    CHUNK_REVIEWING = "chunk_reviewing"
    CHUNK_APPROVED = "chunk_approved"
    CHUNK_REJECTED = "chunk_rejected"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_PAUSED = "task_paused"
    TASK_CANCELLED = "task_cancelled"
    ERROR = "error"


@dataclass
class Event:
    type: EventType
    task_id: str
    chunk_id: Optional[str] = None
    data: Optional[dict] = None


class Orchestrator:
    """Coordinates the full supervisor-worker workflow."""

    def __init__(
        self,
        state_manager: StateManager,
        manager: Manager,
        git_coordinator: GitCoordinator,
        guide_path: Path,
        workspace_path: Path,
        vm_pool: Optional[VMPool] = None,
        max_attempts: int = 3,
    ):
        self.state = state_manager
        self.manager = manager
        self.git = git_coordinator
        self.guide_path = guide_path
        self.workspace_path = workspace_path
        self.vm_pool = vm_pool
        self.max_attempts = max_attempts

        # Event subscribers
        self._subscribers: list[Callable[[Event], None]] = []
        self._event_queue: queue.Queue[Event] = queue.Queue()

        # Task processing
        self._task_threads: dict[str, threading.Thread] = {}
        self._paused_tasks: set[str] = set()
        self._cancelled_tasks: set[str] = set()

        # Start event dispatcher
        self._dispatcher_thread = threading.Thread(target=self._dispatch_events, daemon=True)
        self._dispatcher_thread.start()

    def subscribe(self, callback: Callable[[Event], None]):
        """Subscribe to orchestrator events."""
        self._subscribers.append(callback)

    def _emit(self, event: Event):
        """Emit an event to subscribers."""
        self._event_queue.put(event)

    def _dispatch_events(self):
        """Dispatch events to subscribers."""
        while True:
            try:
                event = self._event_queue.get(timeout=1)
                for subscriber in self._subscribers:
                    try:
                        subscriber(event)
                    except Exception:
                        pass
            except queue.Empty:
                continue

    def _load_guide(self) -> dict:
        """Load the code guide."""
        if self.guide_path.exists():
            with open(self.guide_path) as f:
                return yaml.safe_load(f) or {}
        return {}

    def start_task(self, description: str) -> Task:
        """Start a new task."""
        task = self.state.create_task(description)

        # Create task-specific workspace with git repo
        task_workspace = self.workspace_path / task.id
        task_workspace.mkdir(parents=True, exist_ok=True)
        self._init_task_workspace(task_workspace)

        self._emit(Event(
            type=EventType.TASK_CREATED,
            task_id=task.id,
            data={"description": description, "workspace": str(task_workspace)},
        ))

        # Start processing in background
        thread = threading.Thread(
            target=self._process_task,
            args=(task.id, task_workspace),
            daemon=True,
        )
        self._task_threads[task.id] = thread
        thread.start()

        return task

    def _init_task_workspace(self, workspace: Path):
        """Initialize a git repo in the task workspace."""
        import subprocess
        git_dir = workspace / ".git"
        if not git_dir.exists():
            subprocess.run(
                ["git", "init"],
                cwd=workspace,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "svengalibot@local"],
                cwd=workspace,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Svengalibot"],
                cwd=workspace,
                capture_output=True,
            )
            # Create initial commit
            readme = workspace / "README.md"
            readme.write_text("# Project\n\nGenerated by Svengalibot\n")
            subprocess.run(["git", "add", "."], cwd=workspace, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=workspace,
                capture_output=True,
            )

    def _process_task(self, task_id: str, task_workspace: Path = None):
        """Process a task through the full workflow."""
        try:
            logger.info(f"Starting task processing: {task_id}")
            if task_workspace:
                logger.info(f"Task workspace: {task_workspace}")

            # Planning phase
            self._plan_task(task_id)

            # Check for pause/cancel
            if self._should_stop(task_id):
                return

            # Execute chunks
            self._execute_chunks(task_id, task_workspace)

        except Exception as e:
            import traceback
            error_msg = f"{str(e)}\n{traceback.format_exc()}"
            logger.error(f"Task {task_id} failed: {error_msg}")
            self._emit(Event(
                type=EventType.ERROR,
                task_id=task_id,
                data={"error": str(e)},
            ))
            self.state.update_task(task_id, status=TaskStatus.FAILED, error=str(e))

    def _plan_task(self, task_id: str):
        """Have the manager plan the task."""
        task = self.state.get_task(task_id)
        if not task:
            return

        self.state.update_task(task_id, status=TaskStatus.PLANNING)
        self._emit(Event(type=EventType.TASK_PLANNING, task_id=task_id))

        guide = self._load_guide()

        # Get codebase context
        self._emit(Event(
            type=EventType.MANAGER_THINKING,
            task_id=task_id,
            data={"message": "Analyzing codebase context..."},
        ))
        context = self._get_codebase_context()

        # Get plan from manager
        self._emit(Event(
            type=EventType.MANAGER_THINKING,
            task_id=task_id,
            data={"message": "Calling OpenAI to create execution plan..."},
        ))
        plan = self.manager.plan_task(task.description, guide, context)

        self._emit(Event(
            type=EventType.MANAGER_RESPONSE,
            task_id=task_id,
            data={"message": f"Plan received: {len(plan.get('chunks', []))} chunks identified"},
        ))

        # Handle research if needed
        if plan.get("research_needed"):
            for topic in plan["research_needed"]:
                self._emit(Event(
                    type=EventType.RESEARCH_STARTED,
                    task_id=task_id,
                    data={"topic": topic},
                ))
                research_result = self.manager.research(topic, task.description)
                self._emit(Event(
                    type=EventType.RESEARCH_COMPLETED,
                    task_id=task_id,
                    data={"topic": topic, "result": research_result.get("summary", "")},
                ))
                plan.setdefault("research_results", []).append(research_result)

        # Update task with plan
        self.state.set_plan(task_id, plan)

        self._emit(Event(
            type=EventType.TASK_PLANNED,
            task_id=task_id,
            data={"plan": plan},
        ))

    def _execute_chunks(self, task_id: str, task_workspace: Path = None):
        """Execute all chunks in order."""
        # Use task-specific workspace or fall back to default
        workspace = task_workspace or self.workspace_path / task_id

        while True:
            if self._should_stop(task_id):
                return

            # Get next chunk
            chunk = self.state.get_next_chunk(task_id)
            if not chunk:
                # All chunks done
                task = self.state.get_task(task_id)
                if task and all(c.status == ChunkStatus.APPROVED for c in task.chunks):
                    self.state.update_task(task_id, status=TaskStatus.DONE)
                    self._emit(Event(type=EventType.TASK_COMPLETED, task_id=task_id))
                return

            # Execute chunk
            success = self._execute_chunk(task_id, chunk.id, workspace)

            if not success:
                task = self.state.get_task(task_id)
                if task:
                    self.state.update_task(task_id, status=TaskStatus.FAILED)
                    self._emit(Event(
                        type=EventType.TASK_FAILED,
                        task_id=task_id,
                        data={"chunk_id": chunk.id},
                    ))
                return

    def _execute_chunk(self, task_id: str, chunk_id: str, workspace: Path = None) -> bool:
        """Execute a single chunk with retry logic."""
        task = self.state.get_task(task_id)
        chunk = self.state.get_chunk(task_id, chunk_id)
        if not task or not chunk:
            return False

        # Use provided workspace or fall back to default
        chunk_workspace = workspace or self.workspace_path / task_id

        guide = self._load_guide()
        previous_feedback = None

        for attempt_num in range(self.max_attempts):
            if self._should_stop(task_id):
                return False

            # Create attempt
            attempt = self.state.create_attempt(task_id, chunk_id)
            if not attempt:
                return False

            self._emit(Event(
                type=EventType.CHUNK_STARTED,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"attempt": attempt_num + 1},
            ))

            # Build chunk spec
            chunk_spec = {
                "title": chunk.title,
                "description": chunk.description,
                "acceptance_criteria": chunk.acceptance_criteria,
            }

            # Execute worker
            self._emit(Event(
                type=EventType.WORKER_STARTED,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"message": f"Starting Claude CLI for: {chunk.title}", "workspace": str(chunk_workspace)},
            ))

            worker = Worker(chunk_workspace)

            output_buffer = []

            def on_output(line: str):
                output_buffer.append(line)
                self._emit(Event(
                    type=EventType.CHUNK_OUTPUT,
                    task_id=task_id,
                    chunk_id=chunk_id,
                    data={"content": line},
                ))

            result = worker.execute_local(
                chunk_spec,
                context="",
                guide=guide,
                previous_feedback=previous_feedback,
                on_output=on_output,
            )

            # Complete attempt
            self.state.complete_attempt(
                task_id, chunk_id, attempt.id,
                worker_log="".join(output_buffer),
                diff=result.diff,
                success=result.success,
            )

            self._emit(Event(
                type=EventType.CHUNK_COMPLETED,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"success": result.success, "diff": result.diff},
            ))

            if not result.success:
                previous_feedback = f"Worker failed: {result.error}"
                continue

            # Review
            self._emit(Event(
                type=EventType.CHUNK_REVIEWING,
                task_id=task_id,
                chunk_id=chunk_id,
            ))

            review = self.manager.review_chunk(
                chunk_spec,
                result.diff,
                result.summary,
                guide,
            )

            self.state.set_review(task_id, chunk_id, attempt.id, review)

            if review.get("decision") == "approved":
                self._emit(Event(
                    type=EventType.CHUNK_APPROVED,
                    task_id=task_id,
                    chunk_id=chunk_id,
                ))

                # Commit changes in task workspace
                self._commit_chunk(chunk_workspace, f"Complete {chunk_id}: {chunk.title}")

                return True
            else:
                self._emit(Event(
                    type=EventType.CHUNK_REJECTED,
                    task_id=task_id,
                    chunk_id=chunk_id,
                    data={"review": review},
                ))

                previous_feedback = self.manager.summarize_feedback(review)

        # All attempts failed
        return False

    def _commit_chunk(self, workspace: Path, message: str):
        """Commit changes in the task workspace."""
        import subprocess
        try:
            subprocess.run(["git", "add", "-A"], cwd=workspace, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=workspace,
                capture_output=True,
            )
        except Exception as e:
            logger.warning(f"Failed to commit: {e}")

    def _get_codebase_context(self) -> str:
        """Get context about the current codebase."""
        # List of files, recent commits, etc.
        context_parts = []

        # File tree (limited depth)
        try:
            import subprocess
            result = subprocess.run(
                ["find", ".", "-type", "f", "-name", "*.py", "-o", "-name", "*.js"],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                context_parts.append("Project files:\n" + result.stdout[:2000])
        except Exception:
            pass

        # Recent git commits
        commits = self.git.get_commit_log(5)
        if commits:
            commits_text = "\n".join(
                f"- {c['message']}" for c in commits
            )
            context_parts.append(f"Recent commits:\n{commits_text}")

        return "\n\n".join(context_parts)

    def _should_stop(self, task_id: str) -> bool:
        """Check if task should stop processing."""
        if task_id in self._cancelled_tasks:
            self.state.update_task(task_id, status=TaskStatus.CANCELLED)
            self._emit(Event(type=EventType.TASK_CANCELLED, task_id=task_id))
            return True

        if task_id in self._paused_tasks:
            self.state.update_task(task_id, status=TaskStatus.PAUSED)
            self._emit(Event(type=EventType.TASK_PAUSED, task_id=task_id))
            return True

        return False

    def pause_task(self, task_id: str):
        """Pause a running task."""
        self._paused_tasks.add(task_id)

    def resume_task(self, task_id: str):
        """Resume a paused task."""
        self._paused_tasks.discard(task_id)

        task = self.state.get_task(task_id)
        if task and task.status == TaskStatus.PAUSED:
            # Reconstruct workspace path
            task_workspace = self.workspace_path / task_id
            # Restart processing
            thread = threading.Thread(
                target=self._process_task,
                args=(task_id, task_workspace),
                daemon=True,
            )
            self._task_threads[task_id] = thread
            thread.start()

    def cancel_task(self, task_id: str):
        """Cancel a task."""
        self._cancelled_tasks.add(task_id)

    def approve_chunk(self, task_id: str, chunk_id: str):
        """Manually approve a chunk (human override)."""
        chunk = self.state.get_chunk(task_id, chunk_id)
        if chunk:
            self.state.update_chunk(task_id, chunk_id, status=ChunkStatus.APPROVED)
            self._emit(Event(
                type=EventType.CHUNK_APPROVED,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"manual": True},
            ))

    def update_plan(self, task_id: str, new_plan: dict):
        """Update the plan for a task."""
        self.state.set_plan(task_id, new_plan)


def create_orchestrator_from_config(
    config_path: Path,
    tasks_dir: Path,
    repos_dir: Path,
    guide_path: Path,
    workspace_path: Path,
    prompts_dir: Path,
    vagrant_dir: Path,
) -> Orchestrator:
    """Create an orchestrator from config file."""
    import os

    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Create components
    state_manager = StateManager(tasks_dir)

    api_key = os.environ.get("OPENAI_API_KEY", config.get("manager", {}).get("api_key", ""))
    model = config.get("manager", {}).get("model", "gpt-5.2")
    manager = Manager(api_key=api_key, model=model, prompts_dir=prompts_dir)

    git_coordinator = GitCoordinator(
        bare_repo_path=repos_dir / "workspace.git",
        workspace_path=workspace_path,
    )

    # VM pool is optional
    vm_pool = None
    if config.get("vm", {}).get("enabled", False):
        from app.vm_pool import create_pool_from_config
        vm_pool = create_pool_from_config(config_path, vagrant_dir)

    max_attempts = config.get("worker", {}).get("max_attempts_per_chunk", 3)

    return Orchestrator(
        state_manager=state_manager,
        manager=manager,
        git_coordinator=git_coordinator,
        guide_path=guide_path,
        workspace_path=workspace_path,
        vm_pool=vm_pool,
        max_attempts=max_attempts,
    )
