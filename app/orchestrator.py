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

from app.state import ProjectManager, Project, StateManager, Task, TaskStatus, ChunkStatus, TaskPhase, FlowDefinition, FlowStep
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
    USAGE_UPDATE = "usage_update"
    ERROR = "error"
    # Flow-related events
    FLOW_ITERATION = "flow_iteration"
    FLOW_STEP = "flow_step"
    MANAGER_FEEDBACK = "manager_feedback"
    HUMAN_REVIEW_NEEDED = "human_review_needed"


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
        max_attempts: int = 6,
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

    def _emit_usage_update(self, task_id: str, project_name: str, state: StateManager):
        """Emit a usage update event."""
        task = state.get_task(task_id)
        if task:
            self._emit(Event(
                type=EventType.USAGE_UPDATE,
                task_id=task_id,
                data={
                    "project": project_name,
                    "usage": task.usage.to_dict(),
                },
            ))

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
        # Inherit project's default flow if set
        task = state.create_task(description, flow=project.default_flow)

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
        state.set_phase(task_id, TaskPhase.PLANNING)
        self._emit(Event(type=EventType.TASK_PLANNING, task_id=task_id, data={"project": project.name}))

        guide = self._load_guide(project)

        # Check for cached plan first (recovery from crash)
        cached_plan = state.get_cached_response(task_id, "plan")
        if cached_plan:
            logger.info(f"Using cached plan for task {task_id}")
            plan = cached_plan
            state.clear_cached_response(task_id, "plan")
        else:
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
            plan, usage = self.manager.plan_task(task.description, guide, context)

            # Track usage
            state.update_usage(task_id, usage["prompt_tokens"], usage["completion_tokens"], usage["cost"])
            self._emit_usage_update(task_id, project.name, state)

            # Cache the plan immediately in case of crash
            state.cache_manager_response(task_id, "plan", plan)

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
                research_result, usage = self.manager.research(topic, task.description)
                state.update_usage(task_id, usage["prompt_tokens"], usage["completion_tokens"], usage["cost"])
                self._emit_usage_update(task_id, project.name, state)
                self._emit(Event(
                    type=EventType.RESEARCH_COMPLETED,
                    task_id=task_id,
                    data={"project": project.name, "topic": topic, "result": research_result.get("summary", "")},
                ))
                plan.setdefault("research_results", []).append(research_result)

        # Update task with plan
        state.set_plan(task_id, plan)
        state.set_phase(task_id, TaskPhase.PLANNED)
        # Clear the cache now that plan is applied
        state.clear_cached_response(task_id, "plan")

        self._emit(Event(
            type=EventType.TASK_PLANNED,
            task_id=task_id,
            data={"project": project.name, "plan": plan},
        ))

    def _execute_chunks(self, project: Project, task_id: str):
        """Execute all chunks in order."""
        state = self.project_manager.get_state_manager(project)
        task_key = self._task_key(project.name, task_id)

        state.set_phase(task_id, TaskPhase.EXECUTING)

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

            # Check if chunk is marked as skipped
            if chunk.skipped:
                logger.info(f"Skipping chunk {chunk.id} (marked as skipped)")
                state.update_chunk(task_id, chunk.id, status=ChunkStatus.APPROVED)
                self._emit(Event(
                    type=EventType.CHUNK_APPROVED,
                    task_id=task_id,
                    chunk_id=chunk.id,
                    data={"project": project.name, "skipped": True},
                ))
                continue

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
        """Execute a single chunk using flow-driven execution."""
        state = self.project_manager.get_state_manager(project)
        task = state.get_task(task_id)

        if not task:
            return False

        # Get flow definition (use default if not set)
        flow = task.flow or FlowDefinition.default()

        return self._execute_chunk_flow(project, task_id, chunk_id, flow)

    def _execute_chunk_flow(self, project: Project, task_id: str, chunk_id: str, flow: FlowDefinition) -> bool:
        """Execute chunk according to its flow definition."""
        state = self.project_manager.get_state_manager(project)
        task_key = self._task_key(project.name, task_id)

        task = state.get_task(task_id)
        chunk = state.get_chunk(task_id, chunk_id)
        if not task or not chunk:
            return False

        guide = self._load_guide(project)
        previous_feedback = None
        attempt_history = []
        last_worker_result = None

        # Get or capture baseline commit
        if chunk.baseline_commit:
            baseline_commit = chunk.baseline_commit
            logger.info(f"Chunk {chunk_id} using stored baseline: {baseline_commit[:8]}")
        else:
            baseline_commit = self._get_baseline_commit(project.path)
            logger.info(f"Chunk {chunk_id} captured new baseline: {baseline_commit[:8] if baseline_commit else 'none'}")
            if baseline_commit:
                state.update_chunk(task_id, chunk_id, baseline_commit=baseline_commit)

        # Track current chunk for retry/approve lookups
        state.update_task(task_id, current_chunk=chunk_id)

        chunk_spec = {
            "title": chunk.title,
            "description": chunk.description,
            "acceptance_criteria": chunk.acceptance_criteria,
        }

        # Resume from saved iteration/step if chunk was interrupted
        start_iteration = chunk.current_iteration or 0
        start_step = chunk.current_step or 0

        if start_iteration > 0 or start_step > 0:
            logger.info(f"Resuming chunk {chunk_id} from iteration {start_iteration}, step {start_step}")
            self._emit(Event(
                type=EventType.CHUNK_OUTPUT,
                task_id=task_id,
                chunk_id=chunk_id,
                data={
                    "project": project.name,
                    "content": f"\n[Resuming from iteration {start_iteration + 1}, step {start_step + 1}]\n",
                },
            ))

        for iteration in range(start_iteration, flow.max_iterations):
            if self._should_stop(task_key):
                return False

            # Save checkpoint for resume
            state.update_chunk(task_id, chunk_id, current_iteration=iteration, current_step=0)

            self._emit(Event(
                type=EventType.FLOW_ITERATION,
                task_id=task_id,
                chunk_id=chunk_id,
                data={
                    "project": project.name,
                    "iteration": iteration + 1,
                    "max_iterations": flow.max_iterations,
                },
            ))

            # Determine starting step (only for first iteration when resuming)
            iter_start_step = start_step if iteration == start_iteration else 0

            for step_idx, step in enumerate(flow.steps):
                # Skip steps we've already completed when resuming
                if step_idx < iter_start_step:
                    continue

                if self._should_stop(task_key):
                    return False

                # Save step checkpoint
                state.update_chunk(task_id, chunk_id, current_step=step_idx)

                self._emit(Event(
                    type=EventType.FLOW_STEP,
                    task_id=task_id,
                    chunk_id=chunk_id,
                    data={
                        "project": project.name,
                        "step_type": step.type,
                        "step_action": step.action,
                        "step_index": step_idx + 1,
                        "total_steps": len(flow.steps),
                    },
                ))

                result = self._execute_step(
                    project, task_id, chunk_id, step,
                    chunk_spec=chunk_spec,
                    guide=guide,
                    previous_feedback=previous_feedback,
                    baseline_commit=baseline_commit,
                    last_worker_result=last_worker_result,
                    iteration=iteration,
                    attempt_history=attempt_history,
                )

                if result is None:
                    # Step failed
                    if step.optional:
                        continue
                    return False

                if result == "paused":
                    # Human step - pause for input
                    return True  # Will be resumed later

                if step.type == "worker":
                    last_worker_result = result
                    if not result.get("success"):
                        previous_feedback = f"Worker failed: {result.get('error', 'Unknown error')}"
                        break  # Exit step loop, try next iteration

                elif step.type == "manager":
                    if step.action == "review" and flow.stop_on_approval:
                        if result.get("decision") == "approved":
                            # Commit and return success
                            self._commit_chunk(project.path, f"Complete {chunk_id}: {chunk.title}")
                            return True
                        else:
                            # Track for retry
                            previous_feedback = self.manager.summarize_feedback(result)
                            attempt_history.append({
                                "attempt": iteration + 1,
                                "diff": last_worker_result.get("diff", "")[:5000] if last_worker_result else "",
                                "summary": last_worker_result.get("summary", "") if last_worker_result else "",
                                "review": result,
                                "issues": result.get("issues", []),
                            })
                    elif step.action == "feedback":
                        # Feedback mode - just collect suggestions for next iteration
                        previous_feedback = result.get("next_iteration_focus", "")
                        if result.get("suggestions"):
                            suggestions_text = "\n".join(
                                f"- [{s.get('priority', 'medium')}] {s.get('area', '')}: {s.get('suggested', '')}"
                                for s in result.get("suggestions", [])
                            )
                            previous_feedback = f"{previous_feedback}\n\nSuggestions:\n{suggestions_text}"

            # Check if all worker steps succeeded and we're not in approval mode
            if not flow.stop_on_approval and last_worker_result and last_worker_result.get("success"):
                # In iterative mode without approval, continue to next iteration
                pass

        # If we reach here in non-approval mode, commit the final result
        if not flow.stop_on_approval and last_worker_result and last_worker_result.get("success"):
            self._commit_chunk(project.path, f"Complete {chunk_id}: {chunk.title} (after {flow.max_iterations} iterations)")
            state.update_chunk(task_id, chunk_id, status=ChunkStatus.APPROVED)
            self._emit(Event(
                type=EventType.CHUNK_APPROVED,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"project": project.name, "iterations_completed": flow.max_iterations},
            ))
            return True

        # All iterations exhausted without approval
        return False

    def _execute_step(
        self,
        project: Project,
        task_id: str,
        chunk_id: str,
        step: FlowStep,
        chunk_spec: dict,
        guide: dict,
        previous_feedback: str,
        baseline_commit: str,
        last_worker_result: dict,
        iteration: int,
        attempt_history: list,
    ):
        """Execute a single flow step.

        Returns:
            - dict with results for worker/manager steps
            - "paused" string for human steps
            - None if step failed
        """
        state = self.project_manager.get_state_manager(project)

        if step.type == "worker":
            return self._run_worker_step(
                project, task_id, chunk_id, step,
                chunk_spec=chunk_spec,
                guide=guide,
                previous_feedback=previous_feedback,
                baseline_commit=baseline_commit,
                iteration=iteration,
                attempt_history=attempt_history,
            )

        elif step.type == "manager":
            return self._run_manager_step(
                project, task_id, chunk_id, step,
                chunk_spec=chunk_spec,
                guide=guide,
                last_worker_result=last_worker_result,
                iteration=iteration,
            )

        elif step.type == "human":
            return self._await_human_step(project, task_id, chunk_id, step)

        return None

    def _run_worker_step(
        self,
        project: Project,
        task_id: str,
        chunk_id: str,
        step: FlowStep,
        chunk_spec: dict,
        guide: dict,
        previous_feedback: str,
        baseline_commit: str,
        iteration: int,
        attempt_history: list,
    ) -> dict:
        """Run a worker step (execute, write, revise)."""
        state = self.project_manager.get_state_manager(project)
        chunk = state.get_chunk(task_id, chunk_id)

        # Create attempt
        attempt = state.create_attempt(task_id, chunk_id)
        if not attempt:
            return {"success": False, "error": "Failed to create attempt"}

        # After 3 failed iterations, get detailed remediation
        if iteration >= 3 and attempt_history:
            self._emit(Event(
                type=EventType.MANAGER_THINKING,
                task_id=task_id,
                chunk_id=chunk_id,
                data={
                    "project": project.name,
                    "message": f"Iteration {iteration + 1}: Getting detailed remediation plan...",
                },
            ))
            previous_feedback, usage = self.manager.get_detailed_remediation(
                chunk_spec=chunk_spec,
                attempt_history=attempt_history,
                guide=guide,
            )
            state.update_usage(task_id, usage["prompt_tokens"], usage["completion_tokens"], usage["cost"])
            self._emit_usage_update(task_id, project.name, state)

        self._emit(Event(
            type=EventType.CHUNK_STARTED,
            task_id=task_id,
            chunk_id=chunk_id,
            data={"project": project.name, "attempt": iteration + 1},
        ))

        action_desc = {
            "execute": "executing",
            "write": "writing",
            "revise": "revising",
        }.get(step.action, "executing")

        self._emit(Event(
            type=EventType.WORKER_STARTED,
            task_id=task_id,
            chunk_id=chunk_id,
            data={
                "project": project.name,
                "message": f"Claude is {action_desc}: {chunk.title} (iteration {iteration + 1})",
                "workspace": str(project.path),
            },
        ))

        worker = Worker(project.path, mounts=getattr(project, 'mounts', []))
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
            baseline_commit=baseline_commit,
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

        return {
            "success": result.success,
            "diff": result.diff,
            "summary": result.summary,
            "error": result.error,
        }

    def _run_manager_step(
        self,
        project: Project,
        task_id: str,
        chunk_id: str,
        step: FlowStep,
        chunk_spec: dict,
        guide: dict,
        last_worker_result: dict,
        iteration: int,
    ) -> dict:
        """Run a manager step (review, feedback, approve)."""
        state = self.project_manager.get_state_manager(project)
        chunk = state.get_chunk(task_id, chunk_id)

        if not last_worker_result:
            return {"decision": "rejected", "reason": "No worker result to review"}

        diff = last_worker_result.get("diff", "")
        summary = last_worker_result.get("summary", "")

        if step.action == "feedback":
            # Feedback mode - constructive suggestions without approval gate
            self._emit(Event(
                type=EventType.MANAGER_THINKING,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"project": project.name, "message": "Manager providing feedback..."},
            ))

            feedback, usage = self.manager.give_feedback(chunk_spec, diff, summary, guide)
            state.update_usage(task_id, usage["prompt_tokens"], usage["completion_tokens"], usage["cost"])
            self._emit_usage_update(task_id, project.name, state)

            self._emit(Event(
                type=EventType.MANAGER_FEEDBACK,
                task_id=task_id,
                chunk_id=chunk_id,
                data={
                    "project": project.name,
                    "iteration": iteration + 1,
                    "feedback": feedback,
                },
            ))

            return feedback

        elif step.action in ("review", "approve"):
            # Review mode with approval decision
            self._emit(Event(
                type=EventType.CHUNK_REVIEWING,
                task_id=task_id,
                chunk_id=chunk_id,
                data={"project": project.name},
            ))

            review, usage = self.manager.review_chunk(chunk_spec, diff, summary, guide)
            state.update_usage(task_id, usage["prompt_tokens"], usage["completion_tokens"], usage["cost"])
            self._emit_usage_update(task_id, project.name, state)

            # Auto-approve if no critical issues
            issues = review.get("issues", [])
            critical_issues = [i for i in issues if i.get("severity") == "critical"]
            if review.get("decision") != "approved" and not critical_issues:
                logger.info("Auto-approving: manager rejected but no critical issues found")
                review["decision"] = "approved"
                review["auto_approved"] = True
                review["notes"] = "Approved with minor suggestions (no blocking issues)"

            # Get the current attempt to store review
            chunk = state.get_chunk(task_id, chunk_id)
            if chunk and chunk.current_attempt:
                state.set_review(task_id, chunk_id, chunk.current_attempt, review)

            if review.get("decision") == "approved":
                self._emit(Event(
                    type=EventType.CHUNK_APPROVED,
                    task_id=task_id,
                    chunk_id=chunk_id,
                    data={
                        "project": project.name,
                        "auto_approved": review.get("auto_approved", False),
                        "notes": review.get("notes", ""),
                    },
                ))
            else:
                self._emit(Event(
                    type=EventType.CHUNK_REJECTED,
                    task_id=task_id,
                    chunk_id=chunk_id,
                    data={"project": project.name, "review": review, "attempt": iteration + 1},
                ))

            return review

        return {"decision": "rejected", "reason": f"Unknown manager action: {step.action}"}

    def _await_human_step(self, project: Project, task_id: str, chunk_id: str, step: FlowStep):
        """Pause execution for human review/approval."""
        state = self.project_manager.get_state_manager(project)

        self._emit(Event(
            type=EventType.HUMAN_REVIEW_NEEDED,
            task_id=task_id,
            chunk_id=chunk_id,
            data={
                "project": project.name,
                "action": step.action,
                "message": f"Human {step.action} required for chunk {chunk_id}",
            },
        ))

        state.update_task(task_id, status=TaskStatus.AWAITING_APPROVAL)
        self._emit(Event(
            type=EventType.TASK_PAUSED,
            task_id=task_id,
            data={
                "project": project.name,
                "chunk_id": chunk_id,
                "reason": "human_step",
                "message": f"Waiting for human {step.action}",
            },
        ))

        return "paused"

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

    def _get_baseline_commit(self, workspace: Path) -> str:
        """Get the current HEAD commit hash as a baseline for diff comparison."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()
        except Exception as e:
            logger.warning(f"Failed to get baseline commit: {e}")
            return ""

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
        """Resume a paused task intelligently based on phase."""
        task_key = self._task_key(project_name, task_id)
        self._paused_tasks.discard(task_key)

        project = self.project_manager.get_project(project_name)
        if not project:
            return

        state = self.project_manager.get_state_manager(project)
        task = state.get_task(task_id)
        if not task:
            return

        # Only resume if task is actually paused or awaiting approval
        if task.status not in (TaskStatus.PAUSED, TaskStatus.AWAITING_APPROVAL):
            return

        # Smart resume: check if we have a plan already
        if task.plan and task.chunks:
            # Plan exists - continue execution from where we left off
            logger.info(f"Resume: Task {task_id} has plan, continuing execution")
            self._continue_task(project, task_id)
        elif task.phase == TaskPhase.PLANNING:
            # Was in the middle of planning - check for cached plan
            cached_plan = state.get_cached_response(task_id, "plan")
            if cached_plan:
                logger.info(f"Resume: Found cached plan for {task_id}, applying it")
                state.set_plan(task_id, cached_plan)
                state.clear_cached_response(task_id, "plan")
                self._continue_task(project, task_id)
            else:
                # Need to replan
                logger.info(f"Resume: Task {task_id} needs replanning")
                thread = threading.Thread(
                    target=self._process_task,
                    args=(project, task_id),
                    daemon=True,
                )
                self._task_threads[task_key] = thread
                thread.start()
        else:
            # No plan yet - start from scratch
            logger.info(f"Resume: Task {task_id} has no plan, starting fresh")
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

            # Commit any uncommitted changes when manually approving
            self._commit_chunk(project.path, f"Manual approval: {chunk_id} - {chunk.title}")

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
            # Reset chunk to pending and clear iteration/step (keeps attempt history for reference)
            state.update_chunk(task_id, chunk_id,
                               status=ChunkStatus.PENDING,
                               current_iteration=0,
                               current_step=0)
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
