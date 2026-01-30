"""File-based state management for tasks and chunks."""

import json
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict


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
        """Save task to disk."""
        task.updated_at = datetime.utcnow().isoformat()
        with open(self._meta_path(task.id), "w") as f:
            json.dump(task.to_dict(), f, indent=2)

    def get_task(self, task_id: str) -> Optional[Task]:
        """Load a task by ID."""
        meta_path = self._meta_path(task_id)
        if not meta_path.exists():
            return None

        with open(meta_path) as f:
            data = json.load(f)

        return Task.from_dict(data)

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
