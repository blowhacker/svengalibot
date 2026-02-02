"""Conversation-based task model for AI collaboration."""

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from enum import Enum

logger = logging.getLogger(__name__)


class MessageRole(Enum):
    SYSTEM = "system"
    USER = "user"
    WORKER = "worker"      # Claude (or other worker AI)
    REVIEWER = "reviewer"  # OpenAI (or other reviewer AI)


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Message:
    """A single message in the conversation."""
    role: str           # system, user, worker, reviewer
    content: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    provider: str = ""  # claude, openai, user, system
    metadata: dict = field(default_factory=dict)  # tokens, files changed, etc.

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        return cls(**data)


@dataclass
class CollaborationTask:
    """A task representing AI collaboration."""
    id: str
    prompt: str                    # The collaboration prompt from user
    status: TaskStatus = TaskStatus.PENDING
    messages: list[Message] = field(default_factory=list)
    iteration: int = 0
    max_iterations: int = 10
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Provider settings (can be overridden by prompt)
    worker_provider: str = "claude"
    reviewer_provider: str = "openai"
    skip_reviewer: bool = False  # Claude-only mode

    # Outcome
    approved: bool = False
    error: Optional[str] = None

    def to_dict(self):
        return {
            "id": self.id,
            "prompt": self.prompt,
            "status": self.status.value,
            "messages": [m.to_dict() for m in self.messages],
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "worker_provider": self.worker_provider,
            "reviewer_provider": self.reviewer_provider,
            "skip_reviewer": self.skip_reviewer,
            "approved": self.approved,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CollaborationTask":
        data = data.copy()
        data["status"] = TaskStatus(data.get("status", "pending"))
        data["messages"] = [Message.from_dict(m) for m in data.get("messages", [])]
        return cls(**data)

    def add_message(self, role: str, content: str, provider: str = "", metadata: dict = None):
        """Add a message to the conversation."""
        msg = Message(
            role=role,
            content=content,
            provider=provider,
            metadata=metadata or {},
        )
        self.messages.append(msg)
        self.updated_at = datetime.utcnow().isoformat()
        return msg


# Example collaboration prompts
EXAMPLE_PROMPTS = {
    "code_review": {
        "name": "Code Review",
        "description": "Standard coding with review",
        "prompt": """Claude implements the requested changes to the codebase.
OpenAI reviews the code for bugs, security issues, and code quality.
Claude addresses the feedback or explains why changes aren't needed.
Continue until OpenAI approves the changes.""",
    },
    "brainstorm": {
        "name": "Brainstorming",
        "description": "Generate and refine ideas",
        "prompt": """Claude generates creative ideas based on the request.
OpenAI critically evaluates each idea for feasibility and impact.
Claude refines promising ideas or defends them with evidence.
Continue for {iterations} rounds or until consensus.""",
    },
    "writing": {
        "name": "Writing & Editing",
        "description": "Write and refine content",
        "prompt": """Claude writes the requested content as markdown files.
OpenAI reviews for clarity, accuracy, and engagement.
Claude incorporates feedback or explains editorial choices.
Continue until OpenAI is satisfied with the quality.""",
    },
    "research": {
        "name": "Research & Analysis",
        "description": "Research with verification",
        "prompt": """Claude researches the topic and presents findings.
OpenAI fact-checks claims and identifies gaps.
Claude provides sources or corrects information.
Continue until OpenAI confirms the research is thorough.""",
    },
    "debate": {
        "name": "Debate",
        "description": "Explore ideas through debate",
        "prompt": """Claude takes a position on the topic.
OpenAI plays devil's advocate and challenges assumptions.
Claude defends or refines the position.
Continue for {iterations} rounds to fully explore the topic.""",
    },
}


def get_example_prompts() -> list[dict]:
    """Return list of example prompts."""
    return [
        {"id": k, **v}
        for k, v in EXAMPLE_PROMPTS.items()
    ]
