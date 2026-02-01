"""File-based state management for projects, tasks and chunks."""

import json
import logging
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

SVENGALI_DIR = ".svengali"


class TaskStatus(Enum):
    PENDING = "pending"
    PLANNING = "planning"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    DONE = "done"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    AWAITING_APPROVAL = "awaiting_approval"


class TaskPhase(Enum):
    """Fine-grained phase tracking for resume functionality."""
    CREATED = "created"
    PLANNING = "planning"
    PLANNED = "planned"
    EXECUTING = "executing"
    REVIEWING = "reviewing"


@dataclass
class TokenUsage:
    """Token usage and cost tracking."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0

    def add(self, prompt: int, completion: int, cost: float = 0.0):
        """Add usage from an API call."""
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += prompt + completion
        self.cost += cost

    def to_dict(self):
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost": self.cost,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TokenUsage":
        return cls(
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            total_tokens=data.get("total_tokens", 0),
            cost=data.get("cost", 0.0),
        )


class ChunkStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class FlowStep:
    """A single step in a flow definition."""
    type: str           # "worker", "manager", "human"
    action: str         # "execute", "feedback", "review", "approve", etc.
    optional: bool = False
    config: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "type": self.type,
            "action": self.action,
            "optional": self.optional,
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FlowStep":
        return cls(
            type=data.get("type", "worker"),
            action=data.get("action", "execute"),
            optional=data.get("optional", False),
            config=data.get("config", {}),
        )


@dataclass
class FlowDefinition:
    """Defines the execution flow for a task."""
    max_iterations: int = 4
    stop_on_approval: bool = True
    steps: list[FlowStep] = field(default_factory=list)

    def to_dict(self):
        return {
            "max_iterations": self.max_iterations,
            "stop_on_approval": self.stop_on_approval,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FlowDefinition":
        return cls(
            max_iterations=data.get("max_iterations", 4),
            stop_on_approval=data.get("stop_on_approval", True),
            steps=[FlowStep.from_dict(s) for s in data.get("steps", [])],
        )

    @classmethod
    def default(cls) -> "FlowDefinition":
        """Current hardcoded behavior as a flow."""
        return cls(
            max_iterations=6,
            stop_on_approval=True,
            steps=[
                FlowStep("worker", "execute"),
                FlowStep("manager", "review"),
            ]
        )

    @classmethod
    def iterative(cls, iterations: int = 4) -> "FlowDefinition":
        """Iterative refinement: worker + manager feedback loop."""
        return cls(
            max_iterations=iterations,
            stop_on_approval=True,
            steps=[
                FlowStep("worker", "execute"),
                FlowStep("manager", "feedback"),
            ]
        )

    @classmethod
    def human_in_loop(cls) -> "FlowDefinition":
        """Worker -> Manager -> Human approval gate."""
        return cls(
            max_iterations=6,
            stop_on_approval=True,
            steps=[
                FlowStep("worker", "execute"),
                FlowStep("manager", "review"),
                FlowStep("human", "approve"),
            ]
        )

    @classmethod
    def write_refine(cls, iterations: int = 4) -> "FlowDefinition":
        """Focused on text/doc generation with iteration."""
        return cls(
            max_iterations=iterations,
            stop_on_approval=False,  # Always run all iterations
            steps=[
                FlowStep("worker", "write"),
                FlowStep("manager", "feedback"),
            ]
        )

    @classmethod
    def get_preset(cls, name: str) -> "FlowDefinition":
        """Get a flow preset by name."""
        presets = {
            "default": cls.default,
            "iterative": cls.iterative,
            "human-in-loop": cls.human_in_loop,
            "write-refine": cls.write_refine,
        }
        factory = presets.get(name, cls.default)
        return factory()

    @classmethod
    def list_presets(cls) -> list[dict]:
        """List available presets with descriptions."""
        return [
            {"name": "default", "description": "Worker → Manager review → approve/reject"},
            {"name": "iterative", "description": "N iterations of worker + manager feedback"},
            {"name": "human-in-loop", "description": "Worker → Manager → Human approval gate"},
            {"name": "write-refine", "description": "Text/doc generation with iteration"},
        ]


@dataclass
class Attempt:
    id: str
    started_at: str
    completed_at: Optional[str] = None
    worker_log: str = ""
    diff: str = ""
    review: Optional[dict] = None
    success: bool = False


@dataclass
class Chunk:
    id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    status: ChunkStatus = ChunkStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    current_attempt: Optional[str] = None
    files_affected: list[str] = field(default_factory=list)
    baseline_commit: Optional[str] = None  # Git commit hash before first attempt
    skipped: bool = False  # If True, mark as APPROVED without executing
    current_iteration: int = 0  # Current iteration in flow execution
    current_step: int = 0  # Current step index in flow

    def to_dict(self):
        d = asdict(self)
        d["status"] = self.status.value
        d["attempts"] = [
            {**a, "review": a["review"]} if isinstance(a, dict) else asdict(a)
            for a in self.attempts
        ]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Chunk":
        data = data.copy()
        data["status"] = ChunkStatus(data.get("status", "pending"))
        data["attempts"] = [
            Attempt(**a) if isinstance(a, dict) else a
            for a in data.get("attempts", [])
        ]
        # Backwards compatibility: default skipped to False
        data.setdefault("skipped", False)
        # Backwards compatibility: default flow tracking fields
        data.setdefault("current_iteration", 0)
        data.setdefault("current_step", 0)
        return cls(**data)


@dataclass
class Task:
    id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    plan: Optional[dict] = None
    chunks: list[Chunk] = field(default_factory=list)
    current_chunk: Optional[str] = None
    error: Optional[str] = None
    human_approved: bool = False
    # New fields for resume functionality
    phase: TaskPhase = TaskPhase.CREATED
    checkpoint: Optional[dict] = None  # Recovery info (e.g., last completed step)
    usage: TokenUsage = field(default_factory=TokenUsage)
    cached_responses: dict = field(default_factory=dict)  # Cache for manager API responses
    # Flow definition for customizable execution
    flow: Optional[FlowDefinition] = None

    def to_dict(self):
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "plan": self.plan,
            "chunks": [c.to_dict() for c in self.chunks],
            "current_chunk": self.current_chunk,
            "error": self.error,
            "human_approved": self.human_approved,
            "phase": self.phase.value,
            "checkpoint": self.checkpoint,
            "usage": self.usage.to_dict(),
            "cached_responses": self.cached_responses,
            "flow": self.flow.to_dict() if self.flow else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        data = data.copy()
        data["status"] = TaskStatus(data.get("status", "pending"))
        data["chunks"] = [
            Chunk.from_dict(c) if isinstance(c, dict) else c
            for c in data.get("chunks", [])
        ]
        # Backwards compatibility: default phase based on status
        if "phase" in data:
            data["phase"] = TaskPhase(data["phase"])
        else:
            # Infer phase from status for old tasks
            status = data["status"]
            if status == TaskStatus.PLANNING:
                data["phase"] = TaskPhase.PLANNING
            elif status in (TaskStatus.EXECUTING, TaskStatus.AWAITING_APPROVAL):
                data["phase"] = TaskPhase.EXECUTING
            elif status == TaskStatus.REVIEWING:
                data["phase"] = TaskPhase.REVIEWING
            elif status == TaskStatus.DONE:
                data["phase"] = TaskPhase.EXECUTING  # Completed
            else:
                data["phase"] = TaskPhase.CREATED
        # Backwards compatibility: default usage to empty
        if "usage" in data:
            data["usage"] = TokenUsage.from_dict(data["usage"])
        else:
            data["usage"] = TokenUsage()
        # Backwards compatibility: default cached_responses to empty
        data.setdefault("cached_responses", {})
        data.setdefault("checkpoint", None)
        # Backwards compatibility: default flow to None (will use default at runtime)
        if "flow" in data and data["flow"]:
            data["flow"] = FlowDefinition.from_dict(data["flow"])
        else:
            data["flow"] = None
        return cls(**data)

    @property
    def completed_chunks(self) -> int:
        return sum(1 for c in self.chunks if c.status == ChunkStatus.APPROVED)

    @property
    def total_chunks(self) -> int:
        return len(self.chunks)


@dataclass
class Project:
    """A project represents a codebase that tasks operate on."""
    name: str
    path: Path
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    description: str = ""
    default_flow: Optional[FlowDefinition] = None  # Default flow for new tasks

    def to_dict(self):
        return {
            "name": self.name,
            "path": str(self.path),
            "created_at": self.created_at,
            "description": self.description,
            "default_flow": self.default_flow.to_dict() if self.default_flow else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        data = data.copy()
        data["path"] = Path(data["path"])
        # Backwards compatibility: default_flow may not exist
        if "default_flow" in data and data["default_flow"]:
            data["default_flow"] = FlowDefinition.from_dict(data["default_flow"])
        else:
            data["default_flow"] = None
        return cls(**data)

    @property
    def svengali_dir(self) -> Path:
        return self.path / SVENGALI_DIR

    @property
    def tasks_dir(self) -> Path:
        return self.svengali_dir / "tasks"

    @property
    def guide_path(self) -> Path:
        return self.svengali_dir / "guide.yaml"


class ProjectManager:
    """Manages projects in the projects base directory."""

    def __init__(self, projects_base_dir: Path):
        self.base_dir = Path(projects_base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def create_project(self, name: str, description: str = "") -> Project:
        """Create a new project with initialized workspace."""
        import subprocess

        # Sanitize name for filesystem
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        project_path = self.base_dir / safe_name

        if project_path.exists():
            # Return existing project
            return self.get_project(safe_name)

        # Create directory structure
        project_path.mkdir(parents=True, exist_ok=True)
        svengali_dir = project_path / SVENGALI_DIR
        svengali_dir.mkdir(exist_ok=True)
        (svengali_dir / "tasks").mkdir(exist_ok=True)

        # Create project metadata
        project = Project(
            name=safe_name,
            path=project_path,
            description=description,
        )

        meta_path = svengali_dir / "project.json"
        with open(meta_path, "w") as f:
            json.dump(project.to_dict(), f, indent=2)

        # Initialize git repo
        git_dir = project_path / ".git"
        if not git_dir.exists():
            subprocess.run(["git", "init"], cwd=project_path, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "svengalibot@local"],
                cwd=project_path, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Svengalibot"],
                cwd=project_path, capture_output=True
            )
            # Create initial files
            readme = project_path / "README.md"
            readme.write_text(f"# {name}\n\n{description}\n\nManaged by Svengalibot\n")
            # Add .svengali to gitignore (optional - could also track it)
            gitignore = project_path / ".gitignore"
            gitignore.write_text("# Svengalibot state (uncomment to track)\n# .svengali/\n")
            subprocess.run(["git", "add", "."], cwd=project_path, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=project_path, capture_output=True
            )

        return project

    def get_project(self, name: str) -> Optional[Project]:
        """Get a project by name."""
        project_path = self.base_dir / name
        meta_path = project_path / SVENGALI_DIR / "project.json"

        if not meta_path.exists():
            # Check if directory exists but wasn't initialized by svengali
            if project_path.exists():
                # Initialize svengali in existing directory
                return self._init_existing_directory(project_path, name)
            return None

        try:
            with open(meta_path) as f:
                data = json.load(f)
            return Project.from_dict(data)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to load project {name}: {e}")
            return None

    def _init_existing_directory(self, path: Path, name: str) -> Project:
        """Initialize svengali in an existing directory."""
        svengali_dir = path / SVENGALI_DIR
        svengali_dir.mkdir(exist_ok=True)
        (svengali_dir / "tasks").mkdir(exist_ok=True)

        project = Project(name=name, path=path)
        meta_path = svengali_dir / "project.json"
        with open(meta_path, "w") as f:
            json.dump(project.to_dict(), f, indent=2)

        return project

    def list_projects(self) -> list[Project]:
        """List all projects."""
        projects = []
        for item in self.base_dir.iterdir():
            if item.is_dir() and not item.name.startswith("."):
                project = self.get_project(item.name)
                if project:
                    projects.append(project)
        return sorted(projects, key=lambda p: p.created_at, reverse=True)

    def delete_project(self, name: str, delete_files: bool = False) -> bool:
        """Delete a project. If delete_files=False, only removes .svengali."""
        import shutil
        project_path = self.base_dir / name

        if not project_path.exists():
            return False

        if delete_files:
            shutil.rmtree(project_path)
        else:
            svengali_dir = project_path / SVENGALI_DIR
            if svengali_dir.exists():
                shutil.rmtree(svengali_dir)
        return True

    def update_project(self, name: str, **updates) -> Optional[Project]:
        """Update project settings."""
        project = self.get_project(name)
        if not project:
            return None

        for key, value in updates.items():
            if hasattr(project, key):
                setattr(project, key, value)

        # Save updated project metadata
        meta_path = project.svengali_dir / "project.json"
        with open(meta_path, "w") as f:
            json.dump(project.to_dict(), f, indent=2)

        return project

    def get_state_manager(self, project: Project) -> "StateManager":
        """Get a StateManager scoped to this project."""
        return StateManager(project.tasks_dir)


class StateManager:
    """Manages task state in the filesystem."""

    def __init__(self, tasks_dir: Path):
        self.tasks_dir = Path(tasks_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def _task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id

    def _meta_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "meta.json"

    def _chunks_dir(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "chunks"

    def create_task(self, description: str, flow: Optional[FlowDefinition] = None) -> Task:
        """Create a new task with optional flow definition."""
        task_id = f"task_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        task = Task(id=task_id, description=description, flow=flow)

        # Create directory structure
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        self._chunks_dir(task_id).mkdir(exist_ok=True)

        # Write description
        (task_dir / "description.txt").write_text(description)

        # Write metadata
        self._save_task(task)

        return task

    def _save_task(self, task: Task):
        """Save task to disk atomically."""
        import tempfile
        import os

        task.updated_at = datetime.utcnow().isoformat()
        meta_path = self._meta_path(task.id)

        # Write to temp file first, then rename (atomic on POSIX)
        fd, tmp_path = tempfile.mkstemp(dir=meta_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(task.to_dict(), f, indent=2)
            os.replace(tmp_path, meta_path)  # Atomic rename
        except Exception:
            # Clean up temp file on error
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get_task(self, task_id: str) -> Optional[Task]:
        """Load a task by ID."""
        meta_path = self._meta_path(task_id)
        if not meta_path.exists():
            return None

        try:
            with open(meta_path) as f:
                content = f.read()
                if not content.strip():
                    logger.warning(f"Task file is empty: {meta_path}")
                    return None
                data = json.loads(content)
            return Task.from_dict(data)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse task file {meta_path}: {e}")
            return None

    def update_task(self, task_id: str, **updates) -> Optional[Task]:
        """Update task fields."""
        task = self.get_task(task_id)
        if not task:
            return None

        for key, value in updates.items():
            if hasattr(task, key):
                if key == "status" and isinstance(value, str):
                    value = TaskStatus(value)
                setattr(task, key, value)

        self._save_task(task)
        return task

    def list_tasks(self) -> list[Task]:
        """List all tasks."""
        tasks = []
        for task_dir in self.tasks_dir.iterdir():
            if task_dir.is_dir() and (task_dir / "meta.json").exists():
                task = self.get_task(task_dir.name)
                if task:
                    tasks.append(task)
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def set_plan(self, task_id: str, plan: dict, preserve_state: bool = False) -> Optional[Task]:
        """Set the task plan and create/update chunks.

        Args:
            task_id: The task ID
            plan: The plan dict containing chunks
            preserve_state: If True, preserve existing chunk status/attempts when editing.
                          Useful when user edits the plan mid-execution.
        """
        task = self.get_task(task_id)
        if not task:
            return None

        # Build lookup of existing chunks for preserve_state mode
        existing_chunks = {}
        if preserve_state and task.chunks:
            for chunk in task.chunks:
                existing_chunks[chunk.id] = chunk

        task.plan = plan
        new_chunks = []

        for chunk_data in plan.get("chunks", []):
            chunk_id = f"chunk_{chunk_data['id']:03d}"

            # Check if chunk already exists and we should preserve state
            if preserve_state and chunk_id in existing_chunks:
                existing = existing_chunks[chunk_id]
                # Update content but preserve execution state
                existing.title = chunk_data.get("title", existing.title)
                existing.description = chunk_data.get("description", existing.description)
                existing.acceptance_criteria = chunk_data.get("acceptance_criteria", existing.acceptance_criteria)
                existing.depends_on = chunk_data.get("depends_on", existing.depends_on)
                existing.files_affected = chunk_data.get("files_likely_affected", existing.files_affected)
                # Allow setting skipped flag
                existing.skipped = chunk_data.get("skipped", existing.skipped)
                # If marked as skipped and not yet approved, mark as approved
                if existing.skipped and existing.status == ChunkStatus.PENDING:
                    existing.status = ChunkStatus.APPROVED
                new_chunks.append(existing)
            else:
                # Create new chunk
                skipped = chunk_data.get("skipped", False)
                chunk = Chunk(
                    id=chunk_id,
                    title=chunk_data.get("title", ""),
                    description=chunk_data.get("description", ""),
                    acceptance_criteria=chunk_data.get("acceptance_criteria", []),
                    depends_on=chunk_data.get("depends_on", []),
                    files_affected=chunk_data.get("files_likely_affected", []),
                    skipped=skipped,
                    # If skipped, mark as approved immediately
                    status=ChunkStatus.APPROVED if skipped else ChunkStatus.PENDING,
                )
                new_chunks.append(chunk)

        task.chunks = new_chunks

        # Save plan to file
        plan_path = self._task_dir(task_id) / "plan.json"
        with open(plan_path, "w") as f:
            json.dump(plan, f, indent=2)

        task.status = TaskStatus.EXECUTING
        if task.chunks:
            task.current_chunk = task.chunks[0].id

        self._save_task(task)
        return task

    def get_chunk(self, task_id: str, chunk_id: str) -> Optional[Chunk]:
        """Get a specific chunk."""
        task = self.get_task(task_id)
        if not task:
            return None

        for chunk in task.chunks:
            if chunk.id == chunk_id:
                return chunk
        return None

    def update_chunk(self, task_id: str, chunk_id: str, **updates) -> Optional[Chunk]:
        """Update a chunk."""
        task = self.get_task(task_id)
        if not task:
            return None

        for chunk in task.chunks:
            if chunk.id == chunk_id:
                for key, value in updates.items():
                    if hasattr(chunk, key):
                        if key == "status" and isinstance(value, str):
                            value = ChunkStatus(value)
                        setattr(chunk, key, value)
                self._save_task(task)
                return chunk
        return None

    def create_attempt(self, task_id: str, chunk_id: str) -> Optional[Attempt]:
        """Create a new attempt for a chunk."""
        task = self.get_task(task_id)
        if not task:
            return None

        for chunk in task.chunks:
            if chunk.id == chunk_id:
                attempt_num = len(chunk.attempts) + 1
                attempt = Attempt(
                    id=f"attempt_{attempt_num:03d}",
                    started_at=datetime.utcnow().isoformat(),
                )
                chunk.attempts.append(attempt)
                chunk.current_attempt = attempt.id
                chunk.status = ChunkStatus.IN_PROGRESS

                # Create attempt directory
                attempt_dir = self._chunks_dir(task_id) / chunk_id / attempt.id
                attempt_dir.mkdir(parents=True, exist_ok=True)

                self._save_task(task)
                return attempt
        return None

    def complete_attempt(
        self,
        task_id: str,
        chunk_id: str,
        attempt_id: str,
        worker_log: str,
        diff: str,
        success: bool,
    ) -> Optional[Attempt]:
        """Complete an attempt with results."""
        task = self.get_task(task_id)
        if not task:
            return None

        for chunk in task.chunks:
            if chunk.id == chunk_id:
                for attempt in chunk.attempts:
                    if attempt.id == attempt_id:
                        attempt.completed_at = datetime.utcnow().isoformat()
                        attempt.worker_log = worker_log
                        attempt.diff = diff
                        attempt.success = success

                        # Save logs to files
                        attempt_dir = self._chunks_dir(task_id) / chunk_id / attempt_id
                        attempt_dir.mkdir(parents=True, exist_ok=True)
                        (attempt_dir / "worker_log.txt").write_text(worker_log)
                        (attempt_dir / "diff.patch").write_text(diff)

                        chunk.status = ChunkStatus.REVIEW if success else ChunkStatus.FAILED
                        self._save_task(task)
                        return attempt
        return None

    def set_review(
        self,
        task_id: str,
        chunk_id: str,
        attempt_id: str,
        review: dict,
    ) -> Optional[Chunk]:
        """Set review result for an attempt."""
        task = self.get_task(task_id)
        if not task:
            return None

        for chunk in task.chunks:
            if chunk.id == chunk_id:
                for attempt in chunk.attempts:
                    if attempt.id == attempt_id:
                        attempt.review = review

                        # Save review to file
                        attempt_dir = self._chunks_dir(task_id) / chunk_id / attempt_id
                        attempt_dir.mkdir(parents=True, exist_ok=True)
                        with open(attempt_dir / "review.json", "w") as f:
                            json.dump(review, f, indent=2)

                        if review.get("decision") == "approved":
                            chunk.status = ChunkStatus.APPROVED
                        else:
                            chunk.status = ChunkStatus.REJECTED

                        self._save_task(task)
                        return chunk
        return None

    def get_next_chunk(self, task_id: str) -> Optional[Chunk]:
        """Get the next chunk to process.

        Returns IN_PROGRESS chunks first (for resume), then PENDING chunks.
        """
        task = self.get_task(task_id)
        if not task:
            return None

        approved_ids = {c.id for c in task.chunks if c.status == ChunkStatus.APPROVED}

        # First, check for any IN_PROGRESS chunk that needs to be resumed
        for chunk in task.chunks:
            if chunk.status == ChunkStatus.IN_PROGRESS:
                return chunk

        # Then look for PENDING chunks
        for chunk in task.chunks:
            if chunk.status == ChunkStatus.PENDING:
                # Check if dependencies are met
                deps_met = all(
                    f"chunk_{d:03d}" in approved_ids for d in chunk.depends_on
                )
                if deps_met:
                    return chunk
        return None

    def delete_task(self, task_id: str) -> bool:
        """Delete a task and all its data."""
        import shutil
        task_dir = self._task_dir(task_id)
        if task_dir.exists():
            shutil.rmtree(task_dir)
            return True
        return False

    def cache_manager_response(self, task_id: str, key: str, response: dict) -> bool:
        """Cache a manager API response for recovery after crashes."""
        task = self.get_task(task_id)
        if not task:
            return False
        task.cached_responses[key] = response
        self._save_task(task)
        return True

    def get_cached_response(self, task_id: str, key: str) -> Optional[dict]:
        """Get a cached manager response."""
        task = self.get_task(task_id)
        if not task:
            return None
        return task.cached_responses.get(key)

    def clear_cached_response(self, task_id: str, key: str) -> bool:
        """Clear a cached response after successful processing."""
        task = self.get_task(task_id)
        if not task:
            return False
        if key in task.cached_responses:
            del task.cached_responses[key]
            self._save_task(task)
        return True

    def update_usage(self, task_id: str, prompt_tokens: int, completion_tokens: int, cost: float = 0.0) -> Optional[Task]:
        """Update token usage for a task."""
        task = self.get_task(task_id)
        if not task:
            return None
        task.usage.add(prompt_tokens, completion_tokens, cost)
        self._save_task(task)
        return task

    def set_phase(self, task_id: str, phase: TaskPhase) -> Optional[Task]:
        """Update the task phase."""
        task = self.get_task(task_id)
        if not task:
            return None
        task.phase = phase
        self._save_task(task)
        return task

    def set_checkpoint(self, task_id: str, checkpoint: dict) -> Optional[Task]:
        """Set recovery checkpoint."""
        task = self.get_task(task_id)
        if not task:
            return None
        task.checkpoint = checkpoint
        self._save_task(task)
        return task
