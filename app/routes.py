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


def get_state_manager():
    """Get state manager instance."""
    from app.state import StateManager
    return StateManager(current_app.config["TASKS_DIR"])


def get_orchestrator():
    """Get or create global orchestrator singleton."""
    global _orchestrator
    with _orchestrator_lock:
        if _orchestrator is None:
            from app.orchestrator import create_orchestrator_from_config
            # Ensure workspace exists
            workspace = current_app.config["WORKSPACE_DIR"]
            workspace.mkdir(parents=True, exist_ok=True)

            _orchestrator = create_orchestrator_from_config(
                config_path=current_app.config["CONFIG_PATH"],
                tasks_dir=current_app.config["TASKS_DIR"],
                repos_dir=current_app.config["REPOS_DIR"],
                guide_path=current_app.config["GUIDE_PATH"],
                workspace_path=workspace,
                prompts_dir=current_app.config["DATA_DIR"].parent / "prompts" / "manager",
                vagrant_dir=current_app.config["DATA_DIR"].parent / "vagrant",
            )
            # Subscribe to events for SSE
            _orchestrator.subscribe(_broadcast_event)
        return _orchestrator


def _broadcast_event(event):
    """Broadcast an event to all SSE subscribers for a task."""
    task_id = event.task_id
    event_data = {
        "type": event.type.value,
        "task_id": task_id,
        "chunk_id": event.chunk_id,
        "data": event.data,
    }

    with _queues_lock:
        # Store in buffer for late-joining clients
        if task_id not in _event_buffer:
            _event_buffer[task_id] = []
        _event_buffer[task_id].append(event_data)
        # Trim buffer if too large
        if len(_event_buffer[task_id]) > MAX_BUFFERED_EVENTS:
            _event_buffer[task_id] = _event_buffer[task_id][-MAX_BUFFERED_EVENTS:]
        if task_id in _event_queues:
            for q in _event_queues[task_id]:
                try:
                    q.put_nowait(event_data)
                except queue.Full:
                    pass


# Dashboard
@main_bp.route("/")
def index():
    """Dashboard with task list and status."""
    return render_template("index.html")


# Task CRUD
@main_bp.route("/task", methods=["POST"])
def create_task():
    """Create a new task."""
    data = request.get_json()
    description = data.get("description", "")

    if not description:
        return jsonify({"error": "Description required"}), 400

    orchestrator = get_orchestrator()
    task = orchestrator.start_task(description)

    return jsonify({
        "id": task.id,
        "status": task.status.value,
    })


@main_bp.route("/task/<task_id>")
def get_task(task_id):
    """Task detail view."""
    state = get_state_manager()
    task = state.get_task(task_id)
    if not task:
        return "Task not found", 404
    return render_template("task.html", task_id=task_id)


@main_bp.route("/task/<task_id>/json")
def get_task_json(task_id):
    """Get task data as JSON."""
    state = get_state_manager()
    task = state.get_task(task_id)

    if not task:
        return jsonify({"error": "Task not found"}), 404

    return jsonify(task.to_dict())


@main_bp.route("/tasks")
def list_tasks():
    """List all tasks as JSON."""
    state = get_state_manager()
    tasks = state.list_tasks()

    return jsonify({
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
@main_bp.route("/task/<task_id>/pause", methods=["POST"])
def pause_task(task_id):
    """Pause a running task."""
    orchestrator = get_orchestrator()
    orchestrator.pause_task(task_id)
    return jsonify({"status": "paused"})


@main_bp.route("/task/<task_id>/resume", methods=["POST"])
def resume_task(task_id):
    """Resume a paused task."""
    orchestrator = get_orchestrator()
    orchestrator.resume_task(task_id)
    return jsonify({"status": "resumed"})


@main_bp.route("/task/<task_id>/cancel", methods=["POST"])
def cancel_task(task_id):
    """Cancel a task."""
    orchestrator = get_orchestrator()
    orchestrator.cancel_task(task_id)
    return jsonify({"status": "cancelled"})


@main_bp.route("/task/<task_id>/approve", methods=["POST"])
def approve_chunk(task_id):
    """Manually approve current chunk."""
    data = request.get_json() or {}
    chunk_id = data.get("chunk_id")

    if not chunk_id:
        # Get current chunk
        state = get_state_manager()
        task = state.get_task(task_id)
        if task and task.current_chunk:
            chunk_id = task.current_chunk

    if chunk_id:
        orchestrator = get_orchestrator()
        orchestrator.approve_chunk(task_id, chunk_id)
        return jsonify({"status": "approved", "chunk_id": chunk_id})

    return jsonify({"error": "No chunk to approve"}), 400


@main_bp.route("/task/<task_id>/plan", methods=["PUT"])
def update_plan(task_id):
    """Edit plan mid-execution."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Plan data required"}), 400

    orchestrator = get_orchestrator()
    orchestrator.update_plan(task_id, data)
    return jsonify({"status": "updated"})


@main_bp.route("/task/<task_id>", methods=["DELETE"])
def delete_task(task_id):
    """Delete a task."""
    state = get_state_manager()
    if state.delete_task(task_id):
        return jsonify({"status": "deleted"})
    return jsonify({"error": "Task not found"}), 404


# SSE streaming
@main_bp.route("/task/<task_id>/stream")
def stream_task(task_id):
    """SSE endpoint for live task output."""
    # Get initial state BEFORE entering generator (while still in request context)
    from app.state import StateManager
    state = StateManager(current_app.config["TASKS_DIR"])
    task = state.get_task(task_id)
    initial_state = task.to_dict() if task else None

    # Get buffered events before entering generator
    with _queues_lock:
        buffered = list(_event_buffer.get(task_id, []))

    def generate():
        # Create a queue for this subscriber
        q = queue.Queue(maxsize=100)

        with _queues_lock:
            if task_id not in _event_queues:
                _event_queues[task_id] = []
            _event_queues[task_id].append(q)

        try:
            # Send initial connection event
            yield f"data: {json.dumps({'type': 'connected', 'task_id': task_id})}\n\n"

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
                if task_id in _event_queues:
                    try:
                        _event_queues[task_id].remove(q)
                    except ValueError:
                        pass
                    if not _event_queues[task_id]:
                        del _event_queues[task_id]

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
