"""Claude CLI worker for executing coding tasks."""

import logging
import subprocess
import threading
import queue
from pathlib import Path
from typing import Optional, Callable, Iterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)


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
    ):
        self.workspace_dir = Path(workspace_dir)
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user

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
    ) -> WorkerResult:
        """Execute a chunk locally using Claude CLI."""
        prompt = self._build_prompt(chunk_spec, context, guide, previous_feedback)

        # Build command - use -p for print mode with prompt via stdin
        cmd = ["claude", "-p"]

        logger.info(f"Executing Claude CLI in {self.workspace_dir}")
        logger.debug(f"Prompt (first 200 chars): {prompt[:200]}...")

        try:
            # Get diff before
            diff_before = self._get_git_diff()

            # Run Claude CLI with prompt via stdin
            process = subprocess.Popen(
                cmd,
                cwd=self.workspace_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            # Send prompt to stdin
            process.stdin.write(prompt)
            process.stdin.close()

            output_lines = []
            for line in iter(process.stdout.readline, ""):
                output_lines.append(line)
                logger.debug(f"Claude output: {line.rstrip()}")
                if on_output:
                    on_output(line)

            process.wait()
            output = "".join(output_lines)
            logger.info(f"Claude CLI exited with code {process.returncode}")

            # Get diff after
            diff_after = self._get_git_diff()
            diff = diff_after if diff_after != diff_before else ""

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

    def _get_git_diff(self) -> str:
        """Get current git diff in workspace."""
        try:
            result = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
            )
            return result.stdout
        except Exception:
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
