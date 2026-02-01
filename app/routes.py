"""Flask routes for the Svengalibot web UI."""

from flask import Blueprint, render_template, request, jsonify, Response, current_app
import json
import queue
import threading
import yaml

from app.conversation import CollaborationTask, TaskStatus as ConvTaskStatus, Message, get_example_prompts

main_bp = Blueprint("main", __name__)

# Global event queues for SSE (task_id -> list of queues)
_event_queues: dict[str, list[queue.Queue]] = {}
_event_buffer: dict[str, list[dict]] = {}  # Store recent events per task
_queues_lock = threading.Lock()
MAX_BUFFERED_EVENTS = 100

# Global singleton for orchestrator (must persist across requests)
_orchestrator = None
_orchestrator_lock = threading.Lock()


def get_project_manager():
    """Get project manager instance."""
    from app.state import ProjectManager
    return ProjectManager(current_app.config["PROJECTS_DIR"])


def get_state_manager_for_project(project_name: str):
    """Get state manager scoped to a project."""
    from app.state import ProjectManager
    pm = ProjectManager(current_app.config["PROJECTS_DIR"])
    project = pm.get_project(project_name)
    if not project:
        return None
    return pm.get_state_manager(project)


def get_state_manager():
    """Get state manager instance (legacy - uses default tasks dir)."""
    from app.state import StateManager
    return StateManager(current_app.config["TASKS_DIR"])


def get_orchestrator(project_name: str = None):
    """Get or create orchestrator for a project."""
    global _orchestrator
    with _orchestrator_lock:
        if _orchestrator is None:
            from app.orchestrator import Orchestrator
            from app.state import ProjectManager
            from app.manager import Manager
            from app.git_coordinator import GitCoordinator
            import os

            # Load config
            config_path = current_app.config["CONFIG_PATH"]
            with open(config_path) as f:
                config = yaml.safe_load(f)

            # Create manager
            api_key = os.environ.get("OPENAI_API_KEY", config.get("manager", {}).get("api_key", ""))
            model = config.get("manager", {}).get("model", "gpt-5.2")
            prompts_dir = current_app.config["DATA_DIR"].parent / "prompts" / "manager"
            manager = Manager(api_key=api_key, model=model, prompts_dir=prompts_dir)

            # Create project manager
            project_manager = ProjectManager(current_app.config["PROJECTS_DIR"])

            # Create orchestrator with project support
            # Default to 6 attempts: 3 with simple feedback, then 3 with detailed remediation
            _orchestrator = Orchestrator(
                project_manager=project_manager,
                manager=manager,
                guide_path=current_app.config["GUIDE_PATH"],
                prompts_dir=prompts_dir,
                max_attempts=config.get("worker", {}).get("max_attempts_per_chunk", 6),
            )
            # Subscribe to events for SSE
            _orchestrator.subscribe(_broadcast_event)
        return _orchestrator


def _broadcast_event(event):
    """Broadcast an event to all SSE subscribers for a task."""
    task_id = event.task_id
    project_name = event.data.get("project") if event.data else None
    stream_key = f"{project_name}:{task_id}" if project_name else task_id

    event_data = {
        "type": event.type.value,
        "task_id": task_id,
        "project": project_name,
        "chunk_id": event.chunk_id,
        "data": event.data,
    }

    with _queues_lock:
        # Store in buffer for late-joining clients
        if stream_key not in _event_buffer:
            _event_buffer[stream_key] = []
        _event_buffer[stream_key].append(event_data)
        # Trim buffer if too large
        if len(_event_buffer[stream_key]) > MAX_BUFFERED_EVENTS:
            _event_buffer[stream_key] = _event_buffer[stream_key][-MAX_BUFFERED_EVENTS:]
        if stream_key in _event_queues:
            for q in _event_queues[stream_key]:
                try:
                    q.put_nowait(event_data)
                except queue.Full:
                    pass


# Dashboard
@main_bp.route("/")
def index():
    """Dashboard with project and task list."""
    return render_template("index.html")


# Project CRUD
@main_bp.route("/projects")
def list_projects():
    """List all projects."""
    pm = get_project_manager()
    projects = pm.list_projects()
    return jsonify({
        "projects": [
            {
                "name": p.name,
                "path": str(p.path),
                "created_at": p.created_at,
                "description": p.description,
            }
            for p in projects
        ]
    })


@main_bp.route("/project", methods=["POST"])
def create_project():
    """Create a new project."""
    data = request.get_json()
    name = data.get("name", "").strip()
    description = data.get("description", "")

    if not name:
        return jsonify({"error": "Project name required"}), 400

    pm = get_project_manager()
    project = pm.create_project(name, description)

    return jsonify({
        "name": project.name,
        "path": str(project.path),
        "created_at": project.created_at,
    })


@main_bp.route("/project/<project_name>")
def get_project(project_name):
    """Get project details page."""
    pm = get_project_manager()
    project = pm.get_project(project_name)
    if not project:
        return "Project not found", 404
    return render_template("project.html", project_name=project_name)


@main_bp.route("/project/<project_name>/json")
def get_project_json(project_name):
    """Get project data as JSON."""
    pm = get_project_manager()
    project = pm.get_project(project_name)
    if not project:
        return jsonify({"error": "Project not found"}), 404

    # Get tasks for this project
    state = pm.get_state_manager(project)
    tasks = state.list_tasks()

    return jsonify({
        **project.to_dict(),
        "tasks": [
            {
                "id": t.id,
                "status": t.status.value,
                "description": t.description[:200],
                "completed_chunks": t.completed_chunks,
                "total_chunks": t.total_chunks,
                "created_at": t.created_at,
            }
            for t in tasks
        ]
    })


@main_bp.route("/project/<project_name>", methods=["DELETE"])
def delete_project(project_name):
    """Delete a project (keeps files, removes .svengali)."""
    data = request.get_json() or {}
    delete_files = data.get("delete_files", False)

    pm = get_project_manager()
    if pm.delete_project(project_name, delete_files=delete_files):
        return jsonify({"status": "deleted"})
    return jsonify({"error": "Project not found"}), 404


@main_bp.route("/project/<project_name>/flow", methods=["PUT"])
def update_project_flow(project_name):
    """Update default flow for a project."""
    from app.state import FlowDefinition, FlowStep

    data = request.get_json()
    if not data:
        return jsonify({"error": "Flow data required"}), 400

    pm = get_project_manager()
    project = pm.get_project(project_name)
    if not project:
        return jsonify({"error": "Project not found"}), 404

    # Allow clearing the flow by passing null/None
    if data.get("clear"):
        pm.update_project(project_name, default_flow=None)
        return jsonify({"status": "cleared"})

    # Validate flow structure
    errors = []

    max_iterations = data.get("max_iterations", 4)
    if not isinstance(max_iterations, int) or max_iterations < 1 or max_iterations > 20:
        errors.append("max_iterations must be an integer between 1 and 20")

    steps = data.get("steps", [])
    if not steps:
        errors.append("At least one step is required")

    valid_types = {"worker", "manager", "human"}
    valid_actions = {
        "worker": {"execute", "write", "revise"},
        "manager": {"review", "feedback", "approve"},
        "human": {"review", "approve"},
    }

    for i, step in enumerate(steps):
        step_type = step.get("type")
        step_action = step.get("action")

        if step_type not in valid_types:
            errors.append(f"Step {i+1}: Invalid type '{step_type}'")
        elif step_action not in valid_actions.get(step_type, set()):
            errors.append(f"Step {i+1}: Invalid action '{step_action}' for type '{step_type}'")

    if errors:
        return jsonify({"error": "Validation failed", "details": errors}), 400

    # Build FlowDefinition
    flow = FlowDefinition(
        max_iterations=max_iterations,
        stop_on_approval=data.get("stop_on_approval", True),
        steps=[
            FlowStep(
                type=s.get("type"),
                action=s.get("action"),
                optional=s.get("optional", False),
                config=s.get("config", {}),
            )
            for s in steps
        ],
    )

    pm.update_project(project_name, default_flow=flow)

    return jsonify({"status": "updated", "flow": flow.to_dict()})


# Task CRUD
@main_bp.route("/project/<project_name>/task", methods=["POST"])
def create_task(project_name):
    """Create a new task within a project."""
    data = request.get_json()
    description = data.get("description", "")

    if not description:
        return jsonify({"error": "Description required"}), 400

    pm = get_project_manager()
    project = pm.get_project(project_name)
    if not project:
        return jsonify({"error": "Project not found"}), 404

    orchestrator = get_orchestrator()
    task = orchestrator.start_task(project, description)

    return jsonify({
        "id": task.id,
        "project": project_name,
        "status": task.status.value,
    })


@main_bp.route("/project/<project_name>/task/<task_id>")
def get_task(project_name, task_id):
    """Task detail view."""
    state = get_state_manager_for_project(project_name)
    if not state:
        return "Project not found", 404
    task = state.get_task(task_id)
    if not task:
        return "Task not found", 404
    return render_template("task.html", project_name=project_name, task_id=task_id)


@main_bp.route("/project/<project_name>/task/<task_id>/json")
def get_task_json(project_name, task_id):
    """Get task data as JSON."""
    state = get_state_manager_for_project(project_name)
    if not state:
        return jsonify({"error": "Project not found"}), 404
    task = state.get_task(task_id)

    if not task:
        return jsonify({"error": "Task not found"}), 404

    result = task.to_dict()
    result["project"] = project_name
    return jsonify(result)


@main_bp.route("/project/<project_name>/tasks")
def list_tasks(project_name):
    """List all tasks for a project as JSON."""
    state = get_state_manager_for_project(project_name)
    if not state:
        return jsonify({"error": "Project not found"}), 404
    tasks = state.list_tasks()

    return jsonify({
        "project": project_name,
        "tasks": [
            {
                "id": t.id,
                "status": t.status.value,
                "description": t.description[:200],
                "completed_chunks": t.completed_chunks,
                "total_chunks": t.total_chunks,
                "created_at": t.created_at,
            }
            for t in tasks
        ]
    })


# Task control
@main_bp.route("/project/<project_name>/task/<task_id>/pause", methods=["POST"])
def pause_task(project_name, task_id):
    """Pause a running task."""
    orchestrator = get_orchestrator()
    orchestrator.pause_task(project_name, task_id)
    return jsonify({"status": "paused"})


@main_bp.route("/project/<project_name>/task/<task_id>/resume", methods=["POST"])
def resume_task(project_name, task_id):
    """Resume a paused task."""
    orchestrator = get_orchestrator()
    orchestrator.resume_task(project_name, task_id)
    return jsonify({"status": "resumed"})


@main_bp.route("/project/<project_name>/task/<task_id>/cancel", methods=["POST"])
def cancel_task(project_name, task_id):
    """Cancel a task."""
    orchestrator = get_orchestrator()
    orchestrator.cancel_task(project_name, task_id)
    return jsonify({"status": "cancelled"})


@main_bp.route("/project/<project_name>/task/<task_id>/approve", methods=["POST"])
def approve_chunk(project_name, task_id):
    """Manually approve current chunk."""
    from app.state import ChunkStatus

    data = request.get_json() or {}
    chunk_id = data.get("chunk_id")

    if not chunk_id:
        # Find the chunk that needs approval (rejected, review, or in_progress)
        state = get_state_manager_for_project(project_name)
        if state:
            task = state.get_task(task_id)
            if task:
                for chunk in task.chunks:
                    if chunk.status in (ChunkStatus.REJECTED, ChunkStatus.REVIEW, ChunkStatus.IN_PROGRESS):
                        chunk_id = chunk.id
                        break

    if chunk_id:
        orchestrator = get_orchestrator()
        orchestrator.approve_chunk(project_name, task_id, chunk_id)
        return jsonify({"status": "approved", "chunk_id": chunk_id})

    return jsonify({"error": "No chunk to approve"}), 400


@main_bp.route("/project/<project_name>/task/<task_id>/retry", methods=["POST"])
def retry_chunk(project_name, task_id):
    """Retry a failed/rejected chunk."""
    from app.state import ChunkStatus

    data = request.get_json() or {}
    chunk_id = data.get("chunk_id")

    if not chunk_id:
        # Find the chunk that needs retry (rejected, failed, or in_progress)
        state = get_state_manager_for_project(project_name)
        if state:
            task = state.get_task(task_id)
            if task:
                for chunk in task.chunks:
                    if chunk.status in (ChunkStatus.REJECTED, ChunkStatus.FAILED, ChunkStatus.IN_PROGRESS):
                        chunk_id = chunk.id
                        break

    if chunk_id:
        orchestrator = get_orchestrator()
        orchestrator.retry_chunk(project_name, task_id, chunk_id)
        return jsonify({"status": "retrying", "chunk_id": chunk_id})

    return jsonify({"error": "No chunk to retry"}), 400


@main_bp.route("/project/<project_name>/task/<task_id>/plan", methods=["PUT"])
def update_plan(project_name, task_id):
    """Edit plan mid-execution with validation."""
    from app.state import ChunkStatus

    data = request.get_json()
    if not data:
        return jsonify({"error": "Plan data required"}), 400

    # Get current task state for validation
    state = get_state_manager_for_project(project_name)
    if not state:
        return jsonify({"error": "Project not found"}), 404

    task = state.get_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    # Validate: can't un-approve or reset completed chunks
    existing_approved = {c.id for c in task.chunks if c.status == ChunkStatus.APPROVED}
    new_chunks = data.get("chunks", [])

    errors = []
    for chunk_data in new_chunks:
        chunk_id = f"chunk_{chunk_data['id']:03d}"
        if chunk_id in existing_approved:
            # Check if trying to reset status of approved chunk
            if chunk_data.get("status") and chunk_data.get("status") != "approved":
                errors.append(f"Cannot reset status of completed chunk {chunk_id}")

    if errors:
        return jsonify({"error": "Validation failed", "details": errors}), 400

    # Update plan with preserve_state=True to keep existing chunk progress
    state.set_plan(task_id, data, preserve_state=True)

    return jsonify({"status": "updated"})


@main_bp.route("/project/<project_name>/task/<task_id>/flow", methods=["PUT"])
def update_flow(project_name, task_id):
    """Update flow definition for a task."""
    from app.state import FlowDefinition, FlowStep

    data = request.get_json()
    if not data:
        return jsonify({"error": "Flow data required"}), 400

    state = get_state_manager_for_project(project_name)
    if not state:
        return jsonify({"error": "Project not found"}), 404

    task = state.get_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    # Validate flow structure
    errors = []

    # Validate max_iterations
    max_iterations = data.get("max_iterations", 4)
    if not isinstance(max_iterations, int) or max_iterations < 1 or max_iterations > 20:
        errors.append("max_iterations must be an integer between 1 and 20")

    # Validate steps
    steps = data.get("steps", [])
    if not steps:
        errors.append("At least one step is required")

    valid_types = {"worker", "manager", "human"}
    valid_actions = {
        "worker": {"execute", "write", "revise"},
        "manager": {"review", "feedback", "approve"},
        "human": {"review", "approve"},
    }

    for i, step in enumerate(steps):
        step_type = step.get("type")
        step_action = step.get("action")

        if step_type not in valid_types:
            errors.append(f"Step {i+1}: Invalid type '{step_type}'. Must be one of: {valid_types}")
        elif step_action not in valid_actions.get(step_type, set()):
            errors.append(f"Step {i+1}: Invalid action '{step_action}' for type '{step_type}'. Must be one of: {valid_actions[step_type]}")

    if errors:
        return jsonify({"error": "Validation failed", "details": errors}), 400

    # Build FlowDefinition
    flow = FlowDefinition(
        max_iterations=max_iterations,
        stop_on_approval=data.get("stop_on_approval", True),
        steps=[
            FlowStep(
                type=s.get("type"),
                action=s.get("action"),
                optional=s.get("optional", False),
                config=s.get("config", {}),
            )
            for s in steps
        ],
    )

    # Update task with new flow
    task.flow = flow
    state.update_task(task_id, flow=flow)

    return jsonify({"status": "updated", "flow": flow.to_dict()})


@main_bp.route("/flow/presets")
def get_flow_presets():
    """Get available flow presets."""
    from app.state import FlowDefinition

    presets = FlowDefinition.list_presets()

    # Add full flow definition for each preset
    result = []
    for preset in presets:
        flow = FlowDefinition.get_preset(preset["name"])
        result.append({
            **preset,
            "flow": flow.to_dict(),
        })

    return jsonify({"presets": result})


@main_bp.route("/project/<project_name>/files")
def list_project_files(project_name):
    """List files in a project directory as a tree structure."""
    import os

    pm = get_project_manager()
    project = pm.get_project(project_name)
    if not project:
        return jsonify({"error": "Project not found"}), 404

    # Get optional path parameter for subdirectory
    subpath = request.args.get("path", "")

    # Build full path and validate it's within project
    if subpath:
        full_path = project.path / subpath
        # Security: ensure path is within project directory
        try:
            full_path.resolve().relative_to(project.path.resolve())
        except ValueError:
            return jsonify({"error": "Invalid path"}), 400
    else:
        full_path = project.path

    if not full_path.exists():
        return jsonify({"error": "Path not found"}), 404

    # Directories/files to ignore
    ignore_patterns = {
        ".git", ".svengali", "__pycache__", "node_modules",
        ".venv", "venv", ".env", ".idea", ".vscode",
        "*.pyc", "*.pyo", ".DS_Store", "*.egg-info",
    }

    def should_ignore(name):
        if name in ignore_patterns:
            return True
        for pattern in ignore_patterns:
            if pattern.startswith("*") and name.endswith(pattern[1:]):
                return True
        return False

    def get_file_info(path, base_path):
        """Get file/directory info."""
        rel_path = path.relative_to(base_path)
        stat = path.stat()
        return {
            "name": path.name,
            "path": str(rel_path),
            "type": "directory" if path.is_dir() else "file",
            "size": stat.st_size if path.is_file() else None,
            "extension": path.suffix[1:] if path.suffix else None,
        }

    def scan_directory(dir_path, base_path, depth=0, max_depth=10):
        """Recursively scan directory."""
        if depth > max_depth:
            return []

        items = []
        try:
            entries = sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            for entry in entries:
                if should_ignore(entry.name):
                    continue

                info = get_file_info(entry, base_path)

                if entry.is_dir():
                    info["children"] = scan_directory(entry, base_path, depth + 1, max_depth)
                    info["expanded"] = depth < 1  # Auto-expand first level

                items.append(info)
        except PermissionError:
            pass

        return items

    tree = scan_directory(full_path, project.path)

    return jsonify({
        "project": project_name,
        "root": str(project.path),
        "tree": tree,
    })


@main_bp.route("/project/<project_name>/file")
def get_project_file(project_name):
    """Get contents of a file in a project."""
    pm = get_project_manager()
    project = pm.get_project(project_name)
    if not project:
        return jsonify({"error": "Project not found"}), 404

    file_path = request.args.get("path", "")
    if not file_path:
        return jsonify({"error": "Path required"}), 400

    full_path = project.path / file_path

    # Security: ensure path doesn't escape project directory via ..
    if ".." in file_path.split("/"):
        return jsonify({"error": "Invalid path"}), 400

    if not full_path.exists():
        return jsonify({"error": "File not found"}), 404

    if not full_path.is_file():
        return jsonify({"error": "Not a file"}), 400

    # Check file size (limit to 1MB)
    if full_path.stat().st_size > 1024 * 1024:
        return jsonify({"error": "File too large", "size": full_path.stat().st_size}), 400

    # Try to read as text
    try:
        content = full_path.read_text(encoding="utf-8")
        return jsonify({
            "path": file_path,
            "content": content,
            "size": len(content),
            "extension": full_path.suffix[1:] if full_path.suffix else None,
        })
    except UnicodeDecodeError:
        return jsonify({"error": "Binary file", "path": file_path}), 400


@main_bp.route("/project/<project_name>/task/<task_id>", methods=["DELETE"])
def delete_task(project_name, task_id):
    """Delete a task."""
    state = get_state_manager_for_project(project_name)
    if not state:
        return jsonify({"error": "Project not found"}), 404
    if state.delete_task(task_id):
        return jsonify({"status": "deleted"})
    return jsonify({"error": "Task not found"}), 404


# SSE streaming
@main_bp.route("/project/<project_name>/task/<task_id>/stream")
def stream_task(project_name, task_id):
    """SSE endpoint for live task output."""
    # Get initial state BEFORE entering generator (while still in request context)
    from app.state import ProjectManager
    pm = ProjectManager(current_app.config["PROJECTS_DIR"])
    project = pm.get_project(project_name)
    initial_state = None
    if project:
        state = pm.get_state_manager(project)
        task = state.get_task(task_id)
        initial_state = task.to_dict() if task else None
        if initial_state:
            initial_state["project"] = project_name

    # Get buffered events before entering generator (keyed by project:task)
    stream_key = f"{project_name}:{task_id}"
    with _queues_lock:
        buffered = list(_event_buffer.get(stream_key, []))

    def generate():
        # Create a queue for this subscriber
        q = queue.Queue(maxsize=100)

        with _queues_lock:
            if stream_key not in _event_queues:
                _event_queues[stream_key] = []
            _event_queues[stream_key].append(q)

        try:
            # Send initial connection event
            yield f"data: {json.dumps({'type': 'connected', 'project': project_name, 'task_id': task_id})}\n\n"

            # Send current task state (captured before generator started)
            if initial_state:
                yield f"data: {json.dumps({'type': 'state', 'task': initial_state})}\n\n"

            # Send buffered events that were emitted before this client connected
            for event in buffered:
                yield f"data: {json.dumps(event)}\n\n"

            # Stream events
            while True:
                try:
                    event = q.get(timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    # Send heartbeat
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

        finally:
            # Clean up queue
            with _queues_lock:
                if stream_key in _event_queues:
                    try:
                        _event_queues[stream_key].remove(q)
                    except ValueError:
                        pass
                    if not _event_queues[stream_key]:
                        del _event_queues[stream_key]

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


# Guide management
@main_bp.route("/guide")
def view_guide():
    """View/edit guide page."""
    return render_template("guide.html")


@main_bp.route("/guide/json")
def get_guide_json():
    """Get guide as JSON."""
    guide_path = current_app.config["GUIDE_PATH"]
    if guide_path.exists():
        with open(guide_path) as f:
            guide = yaml.safe_load(f)
        return jsonify(guide or {})
    return jsonify({})


@main_bp.route("/guide", methods=["PUT"])
def update_guide():
    """Update guide."""
    data = request.get_json()
    guide_path = current_app.config["GUIDE_PATH"]

    with open(guide_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)

    return jsonify({"status": "updated"})


# Config
@main_bp.route("/config")
def get_config():
    """Get app configuration."""
    config_path = current_app.config["CONFIG_PATH"]
    if config_path.exists():
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        # Don't expose sensitive data
        if "manager" in cfg and "api_key" in cfg.get("manager", {}):
            del cfg["manager"]["api_key"]
        return jsonify(cfg or {})
    return jsonify({})


# VM status (optional)
@main_bp.route("/vms")
def get_vm_status():
    """Get VM pool status."""
    try:
        orchestrator = get_orchestrator()
        if orchestrator.vm_pool:
            return jsonify(orchestrator.vm_pool.get_status())
    except Exception:
        pass
    return jsonify({"error": "VM pool not configured"})


# ============================================================
# Chat-based Collaboration Task Routes
# ============================================================

# Store for collaboration tasks (in-memory for now, persisted to project dir)
_collab_tasks: dict[str, dict[str, CollaborationTask]] = {}  # project_name -> {task_id -> task}
_collab_tasks_lock = threading.Lock()


def _get_collab_task(project_name: str, task_id: str) -> CollaborationTask:
    """Get a collaboration task from memory or disk."""
    import logging
    logger = logging.getLogger(__name__)

    with _collab_tasks_lock:
        if project_name in _collab_tasks and task_id in _collab_tasks[project_name]:
            return _collab_tasks[project_name][task_id]

    # Try to load from disk
    pm = get_project_manager()
    project = pm.get_project(project_name)
    if not project:
        logger.warning(f"Project not found: {project_name}")
        return None

    task_file = project.path / ".svengali" / "chats" / f"{task_id}.json"
    logger.info(f"Looking for task file: {task_file}")

    if task_file.exists():
        try:
            data = json.loads(task_file.read_text())
            task = CollaborationTask.from_dict(data)
            with _collab_tasks_lock:
                _collab_tasks.setdefault(project_name, {})[task_id] = task
            return task
        except Exception as e:
            logger.error(f"Failed to load task {task_id}: {e}")
            import traceback
            traceback.print_exc()
    else:
        logger.warning(f"Task file does not exist: {task_file}")

    return None


def _save_collab_task(project_name: str, task: CollaborationTask):
    """Save a collaboration task to memory and disk (requires Flask context)."""
    with _collab_tasks_lock:
        _collab_tasks.setdefault(project_name, {})[task.id] = task

    # Persist to disk
    pm = get_project_manager()
    project = pm.get_project(project_name)
    if project:
        _save_collab_task_direct(project, task)


def _save_collab_task_direct(project, task: CollaborationTask):
    """Save a collaboration task to disk (no Flask context needed)."""
    with _collab_tasks_lock:
        _collab_tasks.setdefault(project.name, {})[task.id] = task

    chats_dir = project.path / ".svengali" / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)
    task_file = chats_dir / f"{task.id}.json"
    task_file.write_text(json.dumps(task.to_dict(), indent=2))


def _list_collab_tasks(project_name: str) -> list[CollaborationTask]:
    """List all collaboration tasks for a project."""
    pm = get_project_manager()
    project = pm.get_project(project_name)
    if not project:
        return []

    chats_dir = project.path / ".svengali" / "chats"
    if not chats_dir.exists():
        return []

    tasks = []
    for task_file in chats_dir.glob("*.json"):
        try:
            data = json.loads(task_file.read_text())
            task = CollaborationTask.from_dict(data)
            tasks.append(task)
        except Exception:
            continue

    return sorted(tasks, key=lambda t: t.created_at, reverse=True)


@main_bp.route("/prompts/examples")
def get_example_prompts_route():
    """Get example collaboration prompts."""
    return jsonify({"prompts": get_example_prompts()})


@main_bp.route("/project/<project_name>/chat", methods=["POST"])
def create_collab_task(project_name):
    """Create a new chat/collaboration task."""
    import uuid
    from datetime import datetime

    data = request.get_json()
    prompt = data.get("prompt", "").strip()

    if not prompt:
        return jsonify({"error": "Collaboration prompt is required"}), 400

    pm = get_project_manager()
    project = pm.get_project(project_name)
    if not project:
        return jsonify({"error": "Project not found"}), 404

    # Create task
    task_id = f"chat_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    task = CollaborationTask(
        id=task_id,
        prompt=prompt,
        max_iterations=data.get("max_iterations", 10),
        worker_provider=data.get("worker_provider", "claude"),
        reviewer_provider=data.get("reviewer_provider", "openai"),
    )

    # Add initial system message
    task.add_message(
        role="system",
        content=f"Collaboration started: {prompt}",
        provider="system",
    )

    _save_collab_task(project_name, task)

    # Verify task is retrievable before returning
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Created task {task_id} for project {project_name}")

    # Double-check task is in memory cache
    with _collab_tasks_lock:
        if project_name not in _collab_tasks or task_id not in _collab_tasks[project_name]:
            logger.error(f"Task {task_id} not in memory cache after save!")
            _collab_tasks.setdefault(project_name, {})[task_id] = task

    # Get necessary objects before starting thread (within app context)
    app = current_app._get_current_object()
    orchestrator = get_orchestrator()
    projects_dir = current_app.config["PROJECTS_DIR"]
    guide_path = current_app.config["GUIDE_PATH"]

    # Start the collaboration in background
    _start_collaboration(project_name, task_id, app, orchestrator, projects_dir, guide_path)

    return jsonify({
        "id": task.id,
        "project": project_name,
        "status": task.status.value,
    })


def _start_collaboration(project_name: str, task_id: str, app, orchestrator, projects_dir, guide_path):
    """Start collaboration execution in background thread."""
    thread = threading.Thread(
        target=_run_collaboration,
        args=(project_name, task_id, app, orchestrator, projects_dir, guide_path),
        daemon=True,
    )
    thread.start()


def _run_collaboration(project_name: str, task_id: str, app, orchestrator, projects_dir, guide_path):
    """Execute the collaboration loop between worker and reviewer."""
    import time
    from app.state import ProjectManager

    # Get task from memory/disk
    with _collab_tasks_lock:
        if project_name in _collab_tasks and task_id in _collab_tasks[project_name]:
            task = _collab_tasks[project_name][task_id]
        else:
            return

    # Get project using projects_dir directly (no Flask context needed)
    pm = ProjectManager(projects_dir)
    project = pm.get_project(project_name)
    if not project:
        return

    # Update status
    task.status = ConvTaskStatus.RUNNING
    _save_collab_task_direct(project, task)

    # Broadcast status update
    _broadcast_collab_event(project_name, task_id, {
        "type": "state",
        "task": task.to_dict(),
    })

    try:
        # Load guide (no Flask context needed)
        guide = _load_guide_direct(project, guide_path)

        for iteration in range(task.max_iterations):
            task.iteration = iteration + 1
            _save_collab_task_direct(project, task)

            # Broadcast iteration start
            _broadcast_collab_event(project_name, task_id, {
                "type": "flow_iteration",
                "data": {"iteration": iteration + 1, "max_iterations": task.max_iterations},
            })

            # Worker turn (Claude)
            _broadcast_collab_event(project_name, task_id, {
                "type": "worker_started",
                "data": {"message": "Claude is working..."},
            })

            worker_output = _run_worker_turn(project, task, guide, orchestrator)

            if worker_output.get("paused"):
                return  # Task was paused

            # Add worker message
            worker_msg = task.add_message(
                role="worker",
                content=worker_output.get("summary", "") or worker_output.get("output", ""),
                provider="claude",
                metadata={"diff": worker_output.get("diff", "")},
            )
            _save_collab_task_direct(project, task)

            _broadcast_collab_event(project_name, task_id, {
                "type": "message",
                "data": worker_msg.to_dict(),
            })

            # Reviewer turn (OpenAI)
            _broadcast_collab_event(project_name, task_id, {
                "type": "chunk_reviewing",
                "data": {"message": "OpenAI is reviewing..."},
            })

            review = _run_reviewer_turn(project, task, worker_output, guide, orchestrator)

            # Check for approval
            is_approved = review.get("decision") == "approved" or review.get("approved", False)

            if is_approved:
                reviewer_msg = task.add_message(
                    role="reviewer",
                    content=review.get("notes", review.get("summary", "Work approved!")),
                    provider="openai",
                    metadata={"decision": "approved"},
                )
                _save_collab_task_direct(project, task)

                _broadcast_collab_event(project_name, task_id, {
                    "type": "chunk_approved",
                    "data": {"notes": review.get("notes", "Approved!")},
                })

                _broadcast_collab_event(project_name, task_id, {
                    "type": "message",
                    "data": reviewer_msg.to_dict(),
                })

                # Task complete
                task.status = ConvTaskStatus.COMPLETED
                task.approved = True
                _save_collab_task_direct(project, task)

                _broadcast_collab_event(project_name, task_id, {
                    "type": "task_completed",
                    "data": {"message": "Collaboration completed successfully!"},
                })
                return

            else:
                # Add feedback message
                feedback_text = review.get("feedback_for_retry", review.get("summary", "Please revise."))
                reviewer_msg = task.add_message(
                    role="reviewer",
                    content=feedback_text,
                    provider="openai",
                    metadata={"decision": "rejected", "review": review},
                )
                _save_collab_task_direct(project, task)

                _broadcast_collab_event(project_name, task_id, {
                    "type": "chunk_rejected",
                    "data": {"review": review},
                })

                _broadcast_collab_event(project_name, task_id, {
                    "type": "message",
                    "data": reviewer_msg.to_dict(),
                })

            # Brief pause between iterations
            time.sleep(0.5)

        # Max iterations reached
        task.status = ConvTaskStatus.COMPLETED
        task.add_message(
            role="system",
            content=f"Max iterations ({task.max_iterations}) reached.",
            provider="system",
        )
        _save_collab_task_direct(project, task)

        _broadcast_collab_event(project_name, task_id, {
            "type": "task_completed",
            "data": {"message": f"Max iterations ({task.max_iterations}) reached."},
        })

    except Exception as e:
        import traceback
        error_msg = str(e)
        task.status = ConvTaskStatus.FAILED
        task.error = error_msg
        task.add_message(
            role="system",
            content=f"Error: {error_msg}",
            provider="system",
        )
        _save_collab_task_direct(project, task)

        _broadcast_collab_event(project_name, task_id, {
            "type": "error",
            "data": {"error": error_msg},
        })


def _load_guide_for_project(project) -> dict:
    """Load guide for a project (requires Flask context)."""
    guide_path = current_app.config["GUIDE_PATH"]
    return _load_guide_direct(project, guide_path)


def _load_guide_direct(project, guide_path) -> dict:
    """Load guide for a project (no Flask context needed)."""
    if project.guide_path.exists():
        with open(project.guide_path) as f:
            return yaml.safe_load(f) or {}
    if guide_path and guide_path.exists():
        with open(guide_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _run_worker_turn(project, task: CollaborationTask, guide: dict, orchestrator) -> dict:
    """Run a single worker turn using Claude."""
    from app.worker import Worker

    # Get recent context from conversation
    recent_messages = task.messages[-5:] if task.messages else []
    context = "\n".join([
        f"[{m.role}]: {m.content[:500]}" for m in recent_messages
    ])

    # Build task spec from prompt
    chunk_spec = {
        "title": f"Iteration {task.iteration}",
        "description": task.prompt,
        "acceptance_criteria": ["Complete the requested work", "Address any previous feedback"],
    }

    # Get previous feedback if any
    previous_feedback = ""
    for msg in reversed(task.messages):
        if msg.role == "reviewer":
            previous_feedback = msg.content
            break

    worker = Worker(project.path)
    output_buffer = []

    def on_output(line: str):
        output_buffer.append(line)
        _broadcast_collab_event(project.name, task.id, {
            "type": "chunk_output",
            "data": {"content": line},
        })

    # Check if paused
    task_key = f"{project.name}:{task.id}"
    if task_key in _paused_collab_tasks:
        return {"paused": True}

    result = worker.execute_local(
        chunk_spec,
        context=context,
        guide=guide,
        previous_feedback=previous_feedback,
        on_output=on_output,
        baseline_commit="",
    )

    return {
        "success": result.success,
        "output": "".join(output_buffer),
        "diff": result.diff,
        "summary": result.summary,
        "error": result.error,
    }


def _run_reviewer_turn(project, task: CollaborationTask, worker_output: dict, guide: dict, orchestrator) -> dict:
    """Run a single reviewer turn using OpenAI."""
    chunk_spec = {
        "title": f"Iteration {task.iteration}",
        "description": task.prompt,
        "acceptance_criteria": ["Complete the requested work", "Address any previous feedback"],
    }

    review, usage = orchestrator.manager.review_chunk(
        chunk_spec,
        worker_output.get("diff", ""),
        worker_output.get("summary", ""),
        guide,
    )

    return review


# Paused collaboration tasks
_paused_collab_tasks: set[str] = set()


def _broadcast_collab_event(project_name: str, task_id: str, event: dict):
    """Broadcast event to SSE subscribers for a collaboration task."""
    stream_key = f"{project_name}:{task_id}"
    event["task_id"] = task_id
    event["project"] = project_name

    with _queues_lock:
        if stream_key not in _event_buffer:
            _event_buffer[stream_key] = []
        _event_buffer[stream_key].append(event)
        if len(_event_buffer[stream_key]) > MAX_BUFFERED_EVENTS:
            _event_buffer[stream_key] = _event_buffer[stream_key][-MAX_BUFFERED_EVENTS:]

        if stream_key in _event_queues:
            for q in _event_queues[stream_key]:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass


@main_bp.route("/project/<project_name>/chat/<task_id>")
def get_collab_task_page(project_name, task_id):
    """Chat task view page."""
    pm = get_project_manager()
    project = pm.get_project(project_name)
    if not project:
        return "Project not found", 404

    task = _get_collab_task(project_name, task_id)
    if not task:
        return "Task not found", 404

    return render_template("chat_task.html", project_name=project_name, task_id=task_id)


@main_bp.route("/project/<project_name>/chat/<task_id>/json")
def get_collab_task_json(project_name, task_id):
    """Get collaboration task as JSON."""
    task = _get_collab_task(project_name, task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    return jsonify(task.to_dict())


@main_bp.route("/project/<project_name>/chat/<task_id>/stream")
def stream_collab_task(project_name, task_id):
    """SSE endpoint for live collaboration output."""
    task = _get_collab_task(project_name, task_id)
    initial_state = task.to_dict() if task else None
    if initial_state:
        initial_state["project"] = project_name

    stream_key = f"{project_name}:{task_id}"
    with _queues_lock:
        buffered = list(_event_buffer.get(stream_key, []))

    def generate():
        q = queue.Queue(maxsize=100)

        with _queues_lock:
            if stream_key not in _event_queues:
                _event_queues[stream_key] = []
            _event_queues[stream_key].append(q)

        try:
            yield f"data: {json.dumps({'type': 'connected', 'project': project_name, 'task_id': task_id})}\n\n"

            if initial_state:
                yield f"data: {json.dumps({'type': 'state', 'task': initial_state})}\n\n"

            for event in buffered:
                yield f"data: {json.dumps(event)}\n\n"

            while True:
                try:
                    event = q.get(timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

        finally:
            with _queues_lock:
                if stream_key in _event_queues:
                    try:
                        _event_queues[stream_key].remove(q)
                    except ValueError:
                        pass
                    if not _event_queues[stream_key]:
                        del _event_queues[stream_key]

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


@main_bp.route("/project/<project_name>/chat/<task_id>/pause", methods=["POST"])
def pause_collab_task(project_name, task_id):
    """Pause a running collaboration task."""
    task = _get_collab_task(project_name, task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    task_key = f"{project_name}:{task_id}"
    _paused_collab_tasks.add(task_key)
    task.status = ConvTaskStatus.PAUSED
    _save_collab_task(project_name, task)

    _broadcast_collab_event(project_name, task_id, {
        "type": "task_paused",
        "data": {"message": "Task paused by user"},
    })

    return jsonify({"status": "paused"})


@main_bp.route("/project/<project_name>/chat/<task_id>/resume", methods=["POST"])
def resume_collab_task(project_name, task_id):
    """Resume a paused collaboration task."""
    task = _get_collab_task(project_name, task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    task_key = f"{project_name}:{task_id}"
    _paused_collab_tasks.discard(task_key)

    # Get necessary objects before starting thread (within app context)
    app = current_app._get_current_object()
    orchestrator = get_orchestrator()
    projects_dir = current_app.config["PROJECTS_DIR"]
    guide_path = current_app.config["GUIDE_PATH"]

    # Restart the collaboration from current iteration
    _start_collaboration(project_name, task_id, app, orchestrator, projects_dir, guide_path)

    return jsonify({"status": "resumed"})


@main_bp.route("/project/<project_name>/chat/<task_id>/cancel", methods=["POST"])
def cancel_collab_task(project_name, task_id):
    """Cancel a collaboration task."""
    task = _get_collab_task(project_name, task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    task.status = ConvTaskStatus.FAILED
    task.error = "Cancelled by user"
    _save_collab_task(project_name, task)

    _broadcast_collab_event(project_name, task_id, {
        "type": "task_failed",
        "data": {"error": "Cancelled by user"},
    })

    return jsonify({"status": "cancelled"})


@main_bp.route("/project/<project_name>/chats")
def list_collab_tasks(project_name):
    """List all collaboration tasks for a project."""
    tasks = _list_collab_tasks(project_name)
    return jsonify({
        "project": project_name,
        "tasks": [
            {
                "id": t.id,
                "prompt": t.prompt[:100] + ("..." if len(t.prompt) > 100 else ""),
                "status": t.status.value,
                "iteration": t.iteration,
                "max_iterations": t.max_iterations,
                "created_at": t.created_at,
                "approved": t.approved,
            }
            for t in tasks
        ]
    })


# ============================================================
# Backward-compatible routes for old URL format
# These help users who have old browser tabs open
# ============================================================

@main_bp.route("/task/<task_id>")
@main_bp.route("/task/<task_id>/json")
@main_bp.route("/task/<task_id>/stream")
def old_task_routes(task_id):
    """Redirect old task URLs to project-based structure."""
    return render_template("redirect.html",
        message="This URL format is outdated. Tasks now live under projects.",
        redirect_url="/"), 301


@main_bp.route("/tasks")
def old_tasks_list():
    """Redirect old tasks list to projects."""
    return render_template("redirect.html",
        message="Tasks are now organized by project.",
        redirect_url="/"), 301
