"""Claude CLI worker for executing coding tasks."""

import base64
import json
import logging
import subprocess
import threading
import queue
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Iterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Docker image name
DOCKER_IMAGE = "svengalibot-worker"


@dataclass
class WorkerResult:
    success: bool
    output: str
    diff: str
    summary: str
    error: Optional[str] = None


class Worker:
    """Worker that invokes Claude CLI to execute coding tasks."""

    def __init__(
        self,
        workspace_dir: Path,
        use_docker: bool = False,
        docker_memory: str = "4g",
        docker_cpus: float = 2.0,
    ):
        self.workspace_dir = Path(workspace_dir)
        self.use_docker = use_docker
        self.docker_memory = docker_memory
        self.docker_cpus = docker_cpus

    def _build_prompt(
        self,
        chunk_spec: dict,
        context: str,
        guide: dict,
        previous_feedback: Optional[str] = None,
    ) -> str:
        """Build the prompt for Claude CLI."""
        parts = []

        parts.append("## Task")
        parts.append(chunk_spec.get("description", ""))

        parts.append("\n## Acceptance Criteria")
        for criterion in chunk_spec.get("acceptance_criteria", []):
            parts.append(f"- {criterion}")

        if guide:
            parts.append("\n## Code Guide (must follow)")
            for section, rules in guide.items():
                if isinstance(rules, list):
                    for rule in rules:
                        parts.append(f"- {rule}")

        if context:
            parts.append(f"\n## Context\n{context}")

        if previous_feedback:
            parts.append(f"\n## Previous Attempt Feedback\n{previous_feedback}")
            parts.append("\nPlease address the issues from the previous attempt.")

        parts.append("\n## Instructions")
        parts.append("1. Implement the task according to the specification")
        parts.append("2. Follow all code guide rules")
        parts.append("3. Commit your changes with a clear message")
        parts.append("4. Provide a brief summary of what you implemented")

        return "\n".join(parts)

    def execute_local(
        self,
        chunk_spec: dict,
        context: str,
        guide: dict,
        previous_feedback: Optional[str] = None,
        on_output: Optional[Callable[[str], None]] = None,
        baseline_commit: Optional[str] = None,
    ) -> WorkerResult:
        """Execute a chunk locally using Claude CLI.

        Args:
            baseline_commit: If provided, diff against this commit instead of current HEAD.
                           This is important for retries where we want to see ALL changes
                           across multiple attempts, not just changes in the latest attempt.
        """
        prompt = self._build_prompt(chunk_spec, context, guide, previous_feedback)

        # Build command - use -p for print mode with prompt via stdin
        # --dangerously-skip-permissions allows file writes without prompting
        # WARNING: This should only run in isolated VM environments for safety
        cmd = ["claude", "-p", "--dangerously-skip-permissions"]

        logger.info(f"Executing Claude CLI in {self.workspace_dir}")
        logger.debug(f"Prompt (first 200 chars): {prompt[:200]}...")

        try:
            # Use provided baseline, or capture current HEAD if not provided
            # For retries, caller should pass the original baseline from before first attempt
            if not baseline_commit:
                baseline_commit = self._get_current_commit()
            logger.info(f"Using baseline commit for diff: {baseline_commit[:8] if baseline_commit else 'none'}")

            # Pass prompt directly as argument (not via stdin)
            # Using --print flag for non-interactive mode
            cmd = ["claude", "--dangerously-skip-permissions", "-p", prompt]
            logger.info(f"Starting Claude CLI in {self.workspace_dir}")
            logger.info(f"Prompt length: {len(prompt)} chars")

            process = subprocess.Popen(
                cmd,
                cwd=self.workspace_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            logger.info("Waiting for Claude to complete (max 10 minutes)...")

            # Use communicate() instead of readline - Claude doesn't stream stdout
            try:
                output, _ = process.communicate(timeout=600)  # 10 minute timeout
                logger.info(f"Claude finished with exit code {process.returncode}")
                logger.info(f"Output length: {len(output)} chars")

                # Send output to callback in chunks for UI update
                if on_output and output:
                    # Send in reasonable chunks
                    for i in range(0, len(output), 500):
                        chunk = output[i:i+500]
                        on_output(chunk)

            except subprocess.TimeoutExpired:
                logger.error("Claude timed out after 10 minutes")
                process.kill()
                output, _ = process.communicate()
                output = output or ""
            logger.info(f"Claude CLI exited with code {process.returncode}")

            # Get diff of all changes since baseline (includes committed changes)
            diff = self._get_diff_since_commit(baseline_commit)

            # Extract summary (last paragraph or explicit summary)
            summary = self._extract_summary(output)

            return WorkerResult(
                success=process.returncode == 0,
                output=output,
                diff=diff,
                summary=summary,
                error=None if process.returncode == 0 else f"Exit code: {process.returncode}",
            )

        except Exception as e:
            import traceback
            logger.error(f"Worker execute_local failed: {e}\n{traceback.format_exc()}")
            return WorkerResult(
                success=False,
                output="",
                diff="",
                summary="",
                error=str(e),
            )

    def execute_docker(
        self,
        chunk_spec: dict,
        context: str,
        guide: dict,
        previous_feedback: Optional[str] = None,
        on_output: Optional[Callable[[str], None]] = None,
        baseline_commit: Optional[str] = None,
    ) -> WorkerResult:
        """Execute a chunk in a Docker container for isolation.

        Args:
            baseline_commit: If provided, diff against this commit.
        """
        prompt = self._build_prompt(chunk_spec, context, guide, previous_feedback)

        # Get Claude auth directory
        claude_config_dir = Path.home() / ".claude"

        # Refresh OAuth token on host before Docker run
        # Must make actual API call to trigger token refresh
        try:
            logger.info("Refreshing OAuth token on host...")
            subprocess.run(
                ["claude", "-p", "--dangerously-skip-permissions", "say ok"],
                capture_output=True,
                timeout=60,
            )
            # Ensure credentials file is flushed to disk
            subprocess.run(["sync"], capture_output=True)
            logger.info("Token refreshed successfully")

            # Debug: show host file details
            cred_file = claude_config_dir / ".credentials.json"
            if cred_file.exists():
                stat = cred_file.stat()
                logger.info(f"Host credentials file: {cred_file}")
                logger.info(f"Host credentials mtime: {stat.st_mtime} ({datetime.fromtimestamp(stat.st_mtime)})")
                # Read and log expiresAt
                try:
                    with open(cred_file) as f:
                        creds = json.load(f)
                    expires = creds.get("claudeAiOauth", {}).get("expiresAt")
                    logger.info(f"Host credentials expiresAt: {expires}")
                except Exception as e:
                    logger.warning(f"Could not read host credentials: {e}")
            else:
                logger.error(f"Host credentials file NOT FOUND: {cred_file}")
        except Exception as e:
            logger.warning(f"Token refresh failed (continuing anyway): {e}")

        # Read credentials directly in Python to avoid Docker mount caching issues
        cred_file = claude_config_dir / ".credentials.json"
        settings_file = claude_config_dir / "settings.json"

        creds_b64 = ""
        settings_b64 = ""
        try:
            if cred_file.exists():
                creds_content = cred_file.read_text()
                creds_b64 = base64.b64encode(creds_content.encode()).decode()
                logger.info(f"Read credentials ({len(creds_content)} bytes)")
                # Log expiresAt for debugging
                try:
                    creds_data = json.loads(creds_content)
                    expires = creds_data.get("claudeAiOauth", {}).get("expiresAt")
                    logger.info(f"Credentials expiresAt: {expires}")
                except:
                    pass
            if settings_file.exists():
                settings_b64 = base64.b64encode(settings_file.read_text().encode()).decode()
        except Exception as e:
            logger.error(f"Failed to read credentials: {e}")

        # Build docker run command
        # Pass credentials as base64 env vars to avoid mount caching issues
        setup_script = (
            "echo $CREDS_B64 | base64 -d > /home/worker/.claude/.credentials.json; "
            "echo $SETTINGS_B64 | base64 -d > /home/worker/.claude/settings.json 2>/dev/null || true; "
            "echo '=== Credentials expiresAt ==='; "
            "grep -o 'expiresAt\":[0-9]*' /home/worker/.claude/.credentials.json | head -1; "
            "echo '=== Running Claude ==='; "
            "claude -p --dangerously-skip-permissions"
        )

        cmd = [
            "docker", "run",
            "--rm",  # Remove container after exit
            "-i",  # Keep stdin open for prompt
            "-v", f"{self.workspace_dir.absolute()}:/workspace",
            "-e", "HOME=/home/worker",
            "-e", f"CREDS_B64={creds_b64}",
            "-e", f"SETTINGS_B64={settings_b64}",
            "--memory", self.docker_memory,
            "--cpus", str(self.docker_cpus),
            DOCKER_IMAGE,
            "bash", "-c", setup_script,
        ]

        logger.info(f"Executing Claude CLI in Docker container")
        logger.info(f"Host .claude dir: {claude_config_dir}")
        logger.info(f"Docker script: {setup_script[:200]}...")
        logger.debug(f"Prompt (first 200 chars): {prompt[:200]}...")

        try:
            if not baseline_commit:
                baseline_commit = self._get_current_commit()
            logger.info(f"Using baseline commit for diff: {baseline_commit[:8] if baseline_commit else 'none'}")

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            logger.info("Waiting for Docker Claude to complete (max 10 minutes)...")

            try:
                output, _ = process.communicate(input=prompt, timeout=600)
                logger.info(f"Docker Claude finished with exit code {process.returncode}")

                if on_output and output:
                    for i in range(0, len(output), 500):
                        chunk = output[i:i+500]
                        on_output(chunk)

            except subprocess.TimeoutExpired:
                logger.error("Docker Claude timed out after 10 minutes")
                process.kill()
                output, _ = process.communicate()
                output = output or ""

            # Get diff
            diff = self._get_diff_since_commit(baseline_commit)
            summary = self._extract_summary(output)

            return WorkerResult(
                success=process.returncode == 0,
                output=output,
                diff=diff,
                summary=summary,
                error=None if process.returncode == 0 else f"Exit code: {process.returncode}",
            )

        except Exception as e:
            import traceback
            logger.error(f"Worker execute_docker failed: {e}\n{traceback.format_exc()}")
            return WorkerResult(
                success=False,
                output="",
                diff="",
                summary="",
                error=str(e),
            )

    def execute(
        self,
        chunk_spec: dict,
        context: str,
        guide: dict,
        previous_feedback: Optional[str] = None,
        on_output: Optional[Callable[[str], None]] = None,
        baseline_commit: Optional[str] = None,
    ) -> WorkerResult:
        """Execute a chunk using the configured method (local or Docker)."""
        if self.use_docker:
            return self.execute_docker(
                chunk_spec, context, guide, previous_feedback, on_output, baseline_commit
            )
        else:
            return self.execute_local(
                chunk_spec, context, guide, previous_feedback, on_output, baseline_commit
            )


    def _get_current_commit(self) -> str:
        """Get the current HEAD commit hash."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _get_diff_since_commit(self, commit_hash: str) -> str:
        """Get diff of all changes since a specific commit, including new files."""
        if not commit_hash:
            return ""
        try:
            diff_parts = []

            # Get diff between baseline commit and current HEAD (committed changes)
            result = subprocess.run(
                ["git", "diff", commit_hash, "HEAD"],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                diff_parts.append(result.stdout)

            # Get uncommitted changes to tracked files
            result = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                diff_parts.append(result.stdout)

            # Get list of new untracked files (not in git yet)
            result = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
            )
            untracked_files = result.stdout.strip().split("\n") if result.stdout.strip() else []

            # Include content of new files in diff format
            for filepath in untracked_files:
                if not filepath:
                    continue
                full_path = self.workspace_dir / filepath
                if full_path.is_file() and full_path.stat().st_size < 50000:  # Skip large files
                    try:
                        content = full_path.read_text(encoding="utf-8", errors="replace")
                        # Format as diff for new file
                        diff_parts.append(f"diff --git a/{filepath} b/{filepath}")
                        diff_parts.append(f"new file mode 100644")
                        diff_parts.append(f"--- /dev/null")
                        diff_parts.append(f"+++ b/{filepath}")
                        lines = content.split("\n")
                        diff_parts.append(f"@@ -0,0 +1,{len(lines)} @@")
                        for line in lines:
                            diff_parts.append(f"+{line}")
                    except Exception as e:
                        logger.warning(f"Could not read untracked file {filepath}: {e}")

            return "\n".join(diff_parts)
        except Exception as e:
            logger.error(f"Failed to get diff: {e}")
            return ""

    def _extract_summary(self, output: str) -> str:
        """Extract a summary from worker output."""
        lines = output.strip().split("\n")

        # Look for explicit summary markers
        summary_markers = ["summary:", "## summary", "what i did:", "changes made:"]
        for i, line in enumerate(lines):
            if any(marker in line.lower() for marker in summary_markers):
                return "\n".join(lines[i:])

        # Return last paragraph as summary
        paragraphs = output.strip().split("\n\n")
        if paragraphs:
            return paragraphs[-1]

        return output[:500] if output else "No summary available"

    def get_clarification(
        self,
        chunk_spec: dict,
        review_feedback: dict,
    ) -> str:
        """Ask Claude to clarify its implementation decisions before retry.

        This is a soft ask - giving Claude a chance to explain context
        the reviewer might have missed.
        """
        issues_text = "\n".join(
            f"- [{i.get('severity', 'issue').upper()}] {i.get('description', '')}"
            for i in review_feedback.get("issues", [])
        )

        prompt = f"""A code reviewer has flagged some concerns with your implementation.

## What you implemented
{chunk_spec.get('title', 'Task')}: {chunk_spec.get('description', '')}

## Reviewer's concerns
{issues_text}

## Reviewer's summary
{review_feedback.get('summary', '')}

## Your response

Before we proceed with changes, is there any context the reviewer might be missing?
For example:
- Constraints or requirements that informed your approach
- Trade-offs you intentionally made
- Reasons why the flagged approach might be preferable

Keep it brief (2-3 sentences per point). Only mention points where you think there's genuine misunderstanding.
If the reviewer's concerns are all valid, just say "The feedback is fair, I'll address these issues."

Respond conversationally, not as code."""

        cmd = ["claude", "-p", "--dangerously-skip-permissions"]

        try:
            result = subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=60,
            )
            response = result.stdout.strip()
            logger.info(f"Worker clarification: {response[:200]}...")
            return response
        except Exception as e:
            logger.error(f"Failed to get clarification: {e}")
            return "The feedback is fair, I'll address these issues."

    def push_changes(self, message: str = "Worker changes") -> bool:
        """Commit and push changes to remote."""
        try:
            # Stage all changes
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.workspace_dir,
                check=True,
            )

            # Commit
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self.workspace_dir,
                check=True,
            )

            # Push
            subprocess.run(
                ["git", "push", "origin", "HEAD"],
                cwd=self.workspace_dir,
                check=True,
            )

            return True
        except subprocess.CalledProcessError:
            return False


class AsyncWorker:
    """Async wrapper for worker that supports streaming output."""

    def __init__(self, worker: Worker):
        self.worker = worker
        self.output_queue: queue.Queue = queue.Queue()
        self.result: Optional[WorkerResult] = None
        self.running = False

    def start(
        self,
        chunk_spec: dict,
        context: str,
        guide: dict,
        previous_feedback: Optional[str] = None,
    ):
        """Start execution in background thread."""
        self.running = True
        self.result = None

        def run():
            def on_output(line: str):
                self.output_queue.put(line)

            self.result = self.worker.execute_local(
                chunk_spec, context, guide, previous_feedback, on_output
            )
            self.running = False
            self.output_queue.put(None)  # Signal completion

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

    def get_output(self, timeout: float = 0.1) -> Optional[str]:
        """Get next output line, or None if done."""
        try:
            return self.output_queue.get(timeout=timeout)
        except queue.Empty:
            return "" if self.running else None

    def is_running(self) -> bool:
        return self.running

    def get_result(self) -> Optional[WorkerResult]:
        return self.result
