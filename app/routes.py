"""Flask routes for the Svengalibot web UI."""

from flask import Blueprint, render_template, request, jsonify, Response, current_app
import json
import queue
import threading
import yaml

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
    """Edit plan mid-execution."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Plan data required"}), 400

    orchestrator = get_orchestrator()
    orchestrator.update_plan(project_name, task_id, data)
    return jsonify({"status": "updated"})


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
