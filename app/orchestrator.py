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

from app.state import ProjectManager, Project, StateManager, Task, TaskStatus, ChunkStatus
from app.manager import Manager
from app.worker import Worker, WorkerResult

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
        project_manager: ProjectManager,
        manager: Manager,
        guide_path: Path,
        prompts_dir: Path,
        max_attempts: int = 3,
    ):
        self.project_manager = project_manager
        self.manager = manager
        self.guide_path = guide_path
        self.prompts_dir = prompts_dir
        self.max_attempts = max_attempts

        # Event subscribers
        self._subscribers: list[Callable[[Event], None]] = []
        self._event_queue: queue.Queue[Event] = queue.Queue()

        # Task processing - keyed by "project:task_id"
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

    def _task_key(self, project_name: str, task_id: str) -> str:
        """Create a unique key for a project:task pair."""
        return f"{project_name}:{task_id}"

    def _load_guide(self, project: Project) -> dict:
        """Load the code guide, preferring project-specific if available."""
        # Try project-specific guide first
        if project.guide_path.exists():
            with open(project.guide_path) as f:
                return yaml.safe_load(f) or {}
        # Fall back to global guide
        if self.guide_path.exists():
            with open(self.guide_path) as f:
                return yaml.safe_load(f) or {}
        return {}

    def start_task(self, project: Project, description: str) -> Task:
        """Start a new task within a project."""
        state = self.project_manager.get_state_manager(project)
        task = state.create_task(description)

        self._emit(Event(
            type=EventType.TASK_CREATED,
            task_id=task.id,
            data={
                "project": project.name,
                "description": description,
                "workspace": str(project.path),
            },
        ))

        # Start processing in background
        task_key = self._task_key(project.name, task.id)
        thread = threading.Thread(
            target=self._process_task,
            args=(project, task.id),
            daemon=True,
        )
        self._task_threads[task_key] = thread
        thread.start()

        return task

    def _process_task(self, project: Project, task_id: str):
        """Process a task through the full workflow."""
        state = self.project_manager.get_state_manager(project)
        task_key = self._task_key(project.name, task_id)

        try:
            logger.info(f"Starting task processing: {task_key}")
            logger.info(f"Project workspace: {project.path}")

            # Planning phase
            self._plan_task(project, task_id)

            # Check for pause/cancel
            if self._should_stop(task_key):
                return

            # Execute chunks in project workspace
            self._execute_chunks(project, task_id)

        except Exception as e:
            import traceback
            error_msg = f"{str(e)}\n{traceback.format_exc()}"
            logger.error(f"Task {task_key} failed: {error_msg}")
            self._emit(Event(
                type=EventType.ERROR,
                task_id=task_id,
                data={"project": project.name, "error": str(e)},
            ))
            state.update_task(task_id, status=TaskStatus.FAILED, error=str(e))

    def _plan_task(self, project: Project, task_id: str):
        """Have the manager plan the task."""
        state = self.project_manager.get_state_manager(project)
        task = state.get_task(task_id)
        if not task:
            return

        state.update_task(task_id, status=TaskStatus.PLANNING)
        self._emit(Event(type=EventType.TASK_PLANNING, task_id=task_id, data={"project": project.name}))

        guide = self._load_guide(project)

        # Get codebase context from project workspace
        self._emit(Event(
            type=EventType.MANAGER_THINKING,
            task_id=task_id,
            data={"project": project.name, "message": "Analyzing codebase context..."},
        ))
        context = self._get_codebase_context(project)

        # Get plan from manager
        self._emit(Event(
            type=EventType.MANAGER_THINKING,
            task_id=task_id,
            data={"project": project.name, "message": "Calling OpenAI to create execution plan..."},
        ))
        plan = self.manager.plan_task(task.description, guide, context)

        self._emit(Event(
            type=EventType.MANAGER_RESPONSE,
            task_id=task_id,
            data={"project": project.name, "message": f"Plan received: {len(plan.get('chunks', []))} chunks identified"},
        ))

        # Handle research if needed
        if plan.get("research_needed"):
            for topic in plan["research_needed"]:
                self._emit(Event(
                    type=EventType.RESEARCH_STARTED,
                    task_id=task_id,
                    data={"project": project.name, "topic": topic},
                ))
                research_result = self.manager.research(topic, task.description)
                self._emit(Event(
                    type=EventType.RESEARCH_COMPLETED,
                    task_id=task_id,
                    data={"project": project.name, "topic": topic, "result": research_result.get("summary", "")},
                ))
                plan.setdefault("research_results", []).append(research_result)

        # Update task with plan
        state.set_plan(task_id, plan)

        self._emit(Event(
            type=EventType.TASK_PLANNED,
            task_id=task_id,
            data={"project": project.name, "plan": plan},
        ))

    def _execute_chunks(self, project: Project, task_id: str):
        """Execute all chunks in order."""
        state = self.project_manager.get_state_manager(project)
        task_key = self._task_key(project.name, task_id)

        while True:
            if self._should_stop(task_key):
                return

            # Get next chunk
            chunk = state.get_next_chunk(task_id)
            if not chunk:
                # All chunks done
                task = state.get_task(task_id)
                if task and all(c.status == ChunkStatus.APPROVED for c in task.chunks):
                    state.update_task(task_id, status=TaskStatus.DONE)
                    self._emit(Event(type=EventType.TASK_COMPLETED, task_id=task_id, data={"project": project.name}))
                return

            # Execute chunk in project workspace
            success = self._execute_chunk(project, task_id, chunk.id)

            if not success:
                # Instead of failing, pause for human review
                task = state.get_task(task_id)
                if task:
                    state.update_task(task_id, status=TaskStatus.AWAITING_APPROVAL)
                    self._emit(Event(
                        type=EventType.TASK_PAUSED,
                        task_id=task_id,
                        data={
                            "project": project.name,
                            "chunk_id": chunk.id,
                            "reason": "max_attempts_reached",
                            "message": f"Chunk '{chunk.id}' failed after {self.max_attempts} attempts. Please review and either approve, retry, or cancel.",
                        },
                    ))
                return

    def _execute_chunk(self, project: Project, task_id: str, chunk_id: str) -> bool:
        """Execute a single chunk with retry logic."""
        state = self.project_manager.get_state_manager(project)
        task_key = self._task_key(project.name, task_id)

        task = state.get_task(task_id)
        chunk = state.get_chunk(task_id, chunk_id)
        if not task or not chunk:
            return False

        guide = self._load_guide(project)
        previous_feedback = None

        for attempt_num in range(self.max_attempts):
            if self._should_stop(task_key):
                return False

            # Create attempt
            attempt = state.create_attempt(task_id, chunk_id)
            if not attempt:
                return False

            self._emit(Event(
                type=EventType.CHUNK_STARTED,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"project": project.name, "attempt": attempt_num + 1},
            ))

            # Build chunk spec
            chunk_spec = {
                "title": chunk.title,
                "description": chunk.description,
                "acceptance_criteria": chunk.acceptance_criteria,
            }

            # Execute worker in project workspace
            self._emit(Event(
                type=EventType.WORKER_STARTED,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"project": project.name, "message": f"Starting Claude CLI for: {chunk.title}", "workspace": str(project.path)},
            ))

            worker = Worker(project.path)

            output_buffer = []

            def on_output(line: str):
                output_buffer.append(line)
                self._emit(Event(
                    type=EventType.CHUNK_OUTPUT,
                    task_id=task_id,
                    chunk_id=chunk_id,
                    data={"project": project.name, "content": line},
                ))

            result = worker.execute_local(
                chunk_spec,
                context="",
                guide=guide,
                previous_feedback=previous_feedback,
                on_output=on_output,
            )

            # Complete attempt
            state.complete_attempt(
                task_id, chunk_id, attempt.id,
                worker_log="".join(output_buffer),
                diff=result.diff,
                success=result.success,
            )

            self._emit(Event(
                type=EventType.CHUNK_COMPLETED,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"project": project.name, "success": result.success, "diff": result.diff},
            ))

            if not result.success:
                previous_feedback = f"Worker failed: {result.error}"
                continue

            # Review
            self._emit(Event(
                type=EventType.CHUNK_REVIEWING,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"project": project.name},
            ))

            review = self.manager.review_chunk(
                chunk_spec,
                result.diff,
                result.summary,
                guide,
            )

            state.set_review(task_id, chunk_id, attempt.id, review)

            if review.get("decision") == "approved":
                self._emit(Event(
                    type=EventType.CHUNK_APPROVED,
                    task_id=task_id,
                    chunk_id=chunk_id,
                    data={"project": project.name},
                ))

                # Commit changes in project workspace
                self._commit_chunk(project.path, f"Complete {chunk_id}: {chunk.title}")

                return True
            else:
                self._emit(Event(
                    type=EventType.CHUNK_REJECTED,
                    task_id=task_id,
                    chunk_id=chunk_id,
                    data={"project": project.name, "review": review},
                ))

                previous_feedback = self.manager.summarize_feedback(review)

        # All attempts failed
        return False

    def _commit_chunk(self, workspace: Path, message: str):
        """Commit changes in the project workspace."""
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

    def _get_codebase_context(self, project: Project) -> str:
        """Get context about the project codebase."""
        # List of files, recent commits, etc.
        context_parts = []

        # File tree (limited depth)
        try:
            import subprocess
            result = subprocess.run(
                ["find", ".", "-type", "f", "-name", "*.py", "-o", "-name", "*.js"],
                cwd=project.path,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                context_parts.append("Project files:\n" + result.stdout[:2000])
        except Exception:
            pass

        # Recent git commits
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-5"],
                cwd=project.path,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                context_parts.append(f"Recent commits:\n{result.stdout}")
        except Exception:
            pass

        return "\n\n".join(context_parts)

    def _should_stop(self, task_key: str) -> bool:
        """Check if task should stop processing (uses project:task_id key)."""
        return task_key in self._cancelled_tasks or task_key in self._paused_tasks

    def pause_task(self, project_name: str, task_id: str):
        """Pause a running task."""
        task_key = self._task_key(project_name, task_id)
        self._paused_tasks.add(task_key)

        # Update state
        project = self.project_manager.get_project(project_name)
        if project:
            state = self.project_manager.get_state_manager(project)
            state.update_task(task_id, status=TaskStatus.PAUSED)
            self._emit(Event(type=EventType.TASK_PAUSED, task_id=task_id, data={"project": project_name}))

    def resume_task(self, project_name: str, task_id: str):
        """Resume a paused task."""
        task_key = self._task_key(project_name, task_id)
        self._paused_tasks.discard(task_key)

        project = self.project_manager.get_project(project_name)
        if not project:
            return

        state = self.project_manager.get_state_manager(project)
        task = state.get_task(task_id)
        if task and task.status == TaskStatus.PAUSED:
            # Restart processing
            thread = threading.Thread(
                target=self._process_task,
                args=(project, task_id),
                daemon=True,
            )
            self._task_threads[task_key] = thread
            thread.start()

    def cancel_task(self, project_name: str, task_id: str):
        """Cancel a task."""
        task_key = self._task_key(project_name, task_id)
        self._cancelled_tasks.add(task_key)

        # Update state
        project = self.project_manager.get_project(project_name)
        if project:
            state = self.project_manager.get_state_manager(project)
            state.update_task(task_id, status=TaskStatus.CANCELLED)
            self._emit(Event(type=EventType.TASK_CANCELLED, task_id=task_id, data={"project": project_name}))

    def approve_chunk(self, project_name: str, task_id: str, chunk_id: str):
        """Manually approve a chunk (human override)."""
        project = self.project_manager.get_project(project_name)
        if not project:
            return

        state = self.project_manager.get_state_manager(project)
        chunk = state.get_chunk(task_id, chunk_id)
        if chunk:
            state.update_chunk(task_id, chunk_id, status=ChunkStatus.APPROVED)
            self._emit(Event(
                type=EventType.CHUNK_APPROVED,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"project": project_name, "manual": True},
            ))

            # If task was awaiting approval, resume execution
            task = state.get_task(task_id)
            if task and task.status == TaskStatus.AWAITING_APPROVAL:
                self._continue_task(project, task_id)

    def retry_chunk(self, project_name: str, task_id: str, chunk_id: str):
        """Reset a chunk and retry execution."""
        project = self.project_manager.get_project(project_name)
        if not project:
            return

        state = self.project_manager.get_state_manager(project)
        chunk = state.get_chunk(task_id, chunk_id)
        if chunk:
            # Reset chunk to pending (keeps attempt history for reference)
            state.update_chunk(task_id, chunk_id, status=ChunkStatus.PENDING)
            self._emit(Event(
                type=EventType.CHUNK_STARTED,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"project": project_name, "retry": True, "attempt": len(chunk.attempts) + 1},
            ))

            # Resume task execution
            self._continue_task(project, task_id)

    def _continue_task(self, project: Project, task_id: str):
        """Continue task execution from current state."""
        task_key = self._task_key(project.name, task_id)
        self._paused_tasks.discard(task_key)

        state = self.project_manager.get_state_manager(project)
        state.update_task(task_id, status=TaskStatus.EXECUTING)

        thread = threading.Thread(
            target=self._execute_chunks,
            args=(project, task_id),
            daemon=True,
        )
        self._task_threads[task_key] = thread
        thread.start()

    def update_plan(self, project_name: str, task_id: str, new_plan: dict):
        """Update the plan for a task."""
        project = self.project_manager.get_project(project_name)
        if project:
            state = self.project_manager.get_state_manager(project)
            state.set_plan(task_id, new_plan)
