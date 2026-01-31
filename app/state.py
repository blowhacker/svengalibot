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


class ChunkStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


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
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        data = data.copy()
        data["status"] = TaskStatus(data.get("status", "pending"))
        data["chunks"] = [
            Chunk.from_dict(c) if isinstance(c, dict) else c
            for c in data.get("chunks", [])
        ]
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

    def to_dict(self):
        return {
            "name": self.name,
            "path": str(self.path),
            "created_at": self.created_at,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        data = data.copy()
        data["path"] = Path(data["path"])
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

    def create_task(self, description: str) -> Task:
        """Create a new task."""
        task_id = f"task_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        task = Task(id=task_id, description=description)

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

    def set_plan(self, task_id: str, plan: dict) -> Optional[Task]:
        """Set the task plan and create chunks."""
        task = self.get_task(task_id)
        if not task:
            return None

        task.plan = plan
        task.chunks = []

        for chunk_data in plan.get("chunks", []):
            chunk = Chunk(
                id=f"chunk_{chunk_data['id']:03d}",
                title=chunk_data.get("title", ""),
                description=chunk_data.get("description", ""),
                acceptance_criteria=chunk_data.get("acceptance_criteria", []),
                depends_on=chunk_data.get("depends_on", []),
                files_affected=chunk_data.get("files_likely_affected", []),
            )
            task.chunks.append(chunk)

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
        """Get the next chunk to process."""
        task = self.get_task(task_id)
        if not task:
            return None

        approved_ids = {c.id for c in task.chunks if c.status == ChunkStatus.APPROVED}

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
