"""Claude CLI worker for executing coding tasks."""

import logging
import subprocess
import threading
import queue
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
        ssh_host: Optional[str] = None,
        ssh_user: str = "vagrant",
        use_docker: bool = False,
        docker_memory: str = "4g",
        docker_cpus: float = 2.0,
    ):
        self.workspace_dir = Path(workspace_dir)
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
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
        import os
        claude_config_dir = Path.home() / ".claude"

        # Build docker run command
        cmd = [
            "docker", "run",
            "--rm",  # Remove container after exit
            "-v", f"{self.workspace_dir.absolute()}:/workspace",
            "-v", f"{claude_config_dir}:/home/worker/.claude",  # Mount Claude auth (read-only)
            "--memory", self.docker_memory,
            "--cpus", str(self.docker_cpus),
            DOCKER_IMAGE,
            "claude", "-p", "--dangerously-skip-permissions", prompt,
        ]

        logger.info(f"Executing Claude CLI in Docker container")
        logger.debug(f"Prompt (first 200 chars): {prompt[:200]}...")

        try:
            if not baseline_commit:
                baseline_commit = self._get_current_commit()
            logger.info(f"Using baseline commit for diff: {baseline_commit[:8] if baseline_commit else 'none'}")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            logger.info("Waiting for Docker Claude to complete (max 10 minutes)...")

            try:
                output, _ = process.communicate(timeout=600)
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

    def execute_streaming(
        self,
        chunk_spec: dict,
        context: str,
        guide: dict,
        previous_feedback: Optional[str] = None,
    ) -> Iterator[str]:
        """Execute a chunk and yield output line by line."""
        prompt = self._build_prompt(chunk_spec, context, guide, previous_feedback)

        if self.ssh_host:
            cmd = [
                "ssh",
                f"{self.ssh_user}@{self.ssh_host}",
                f"cd /workspace && claude '{prompt}'",
            ]
        else:
            cmd = ["claude", prompt]

        process = subprocess.Popen(
            cmd,
            cwd=self.workspace_dir if not self.ssh_host else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        for line in iter(process.stdout.readline, ""):
            yield line

        process.wait()
        yield f"\n[Process exited with code {process.returncode}]\n"

    def execute_ssh(
        self,
        chunk_spec: dict,
        context: str,
        guide: dict,
        previous_feedback: Optional[str] = None,
        on_output: Optional[Callable[[str], None]] = None,
    ) -> WorkerResult:
        """Execute a chunk via SSH on a remote VM."""
        if not self.ssh_host:
            raise ValueError("SSH host not configured")

        prompt = self._build_prompt(chunk_spec, context, guide, previous_feedback)

        # Escape prompt for shell
        escaped_prompt = prompt.replace("'", "'\"'\"'")

        cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            f"{self.ssh_user}@{self.ssh_host}",
            f"cd /workspace && claude --print '{escaped_prompt}'",
        ]

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            output_lines = []
            for line in iter(process.stdout.readline, ""):
                output_lines.append(line)
                if on_output:
                    on_output(line)

            process.wait()
            output = "".join(output_lines)

            # Get diff from remote
            diff = self._get_remote_diff()

            summary = self._extract_summary(output)

            return WorkerResult(
                success=process.returncode == 0,
                output=output,
                diff=diff,
                summary=summary,
                error=None if process.returncode == 0 else f"Exit code: {process.returncode}",
            )

        except Exception as e:
            return WorkerResult(
                success=False,
                output="",
                diff="",
                summary="",
                error=str(e),
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

    def _get_remote_diff(self) -> str:
        """Get git diff from remote VM."""
        if not self.ssh_host:
            return ""

        try:
            result = subprocess.run(
                [
                    "ssh",
                    f"{self.ssh_user}@{self.ssh_host}",
                    "cd /workspace && git diff HEAD",
                ],
                capture_output=True,
                text=True,
            )
            return result.stdout
        except Exception:
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
