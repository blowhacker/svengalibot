"""Claude CLI worker for executing coding tasks."""

import base64
import json
import logging
import os
import platform
import re
import subprocess
import threading
import queue
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Iterator, Union
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
        mounts: list[dict] = None,
    ):
        self.workspace_dir = Path(workspace_dir)
        self.use_docker = use_docker
        self.docker_memory = docker_memory
        self.docker_cpus = docker_cpus
        self.mounts = mounts or []

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

        if self.mounts:
            parts.append("\n## Available Data Mounts")
            for m in self.mounts:
                mode = "read-only" if m.get("readonly", True) else "read-write"
                parts.append(f"- `{m['container']}` ({mode})")

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

        try:
            cmd_parts, setup_script, auth_mode = self._setup_docker_auth()
        except RuntimeError as e:
            return WorkerResult(
                success=False, output="", diff="", summary="",
                error=str(e),
            )

        # Append the Claude run command to setup script
        full_setup = (
            setup_script +
            " echo '=== Running Claude ==='; "
            "claude -p --dangerously-skip-permissions"
        )

        cmd = [
            "docker", "run",
            "--rm",
            "-i",
        ] + cmd_parts + [
            DOCKER_IMAGE,
            "bash", "-c", full_setup,
        ]

        logger.info("Executing Claude CLI in Docker container")
        logger.info(f"Docker script: {full_setup[:200]}...")
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
        finally:
            self._cleanup_docker_auth()

    def _extract_keychain_credentials(self) -> Optional[dict]:
        """Extract Claude OAuth credentials from macOS Keychain.

        Claude Code on macOS stores OAuth tokens in the system Keychain
        under service name "Claude Code-credentials" instead of on disk.
        Returns the parsed credentials dict, or None if not found.
        """
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(f"Keychain lookup failed: {result.stderr.strip()}")
                return None

            creds_json = result.stdout.strip()
            if not creds_json:
                return None

            creds = json.loads(creds_json)
            logger.info(f"Keychain credentials extracted (expiresAt: {creds.get('claudeAiOauth', {}).get('expiresAt')})")
            return creds
        except json.JSONDecodeError:
            logger.warning("Keychain credentials are not valid JSON")
            return None
        except FileNotFoundError:
            # 'security' command not found (not macOS)
            return None
        except Exception as e:
            logger.warning(f"Keychain extraction failed: {e}")
            return None

    @staticmethod
    def _strip_ansi(text: str) -> str:
        """Strip ANSI escape sequences from text for clean summary extraction."""
        return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b\[[\?]?[0-9;]*[hlm]', '', text)

    def _setup_docker_auth(self):
        """Set up Docker authentication and return (auth_env_args, setup_script, cleanup_fn).

        Shared between execute_docker and execute_docker_pty to avoid duplication.
        Returns:
            tuple: (cmd_parts, setup_script, auth_mode) where cmd_parts are docker run args
                   before the image name, setup_script is the bash script prefix,
                   and auth_mode is a string describing the auth method used.
        Raises:
            RuntimeError: If no authentication method is available.
        """
        claude_config_dir = Path.home() / ".claude"

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        cred_file = claude_config_dir / ".credentials.json"
        has_cred_file = cred_file.exists()
        keychain_creds = None

        # On macOS, try extracting OAuth creds from Keychain if no file on disk
        if not api_key and not has_cred_file and platform.system() == "Darwin":
            keychain_creds = self._extract_keychain_credentials()
            if keychain_creds:
                logger.info("Extracted OAuth credentials from macOS Keychain")

        if api_key:
            auth_mode = "api_key"
            logger.info("Using ANTHROPIC_API_KEY for Docker auth")
        elif has_cred_file:
            auth_mode = "mount" if platform.system() == "Linux" else "base64"
            try:
                with open(cred_file) as f:
                    creds_data = json.load(f)
                expires = creds_data.get("claudeAiOauth", {}).get("expiresAt")
                logger.info(f"Using credentials file (expiresAt: {expires})")
            except Exception as e:
                logger.warning(f"Could not read host credentials: {e}")
        elif keychain_creds:
            auth_mode = "keychain"
            logger.info("Using macOS Keychain OAuth credentials for Docker auth")
        else:
            logger.error(f"No auth available: ANTHROPIC_API_KEY not set and {cred_file} not found")
            hint = (
                "Run 'claude' to authenticate first. On macOS, also ensure Keychain access is allowed."
                if platform.system() == "Darwin"
                else "Run 'claude' to authenticate, or set ANTHROPIC_API_KEY."
            )
            raise RuntimeError(f"No Docker auth available. {hint}")

        creds_b64 = ""
        settings_b64 = ""
        if auth_mode == "base64":
            try:
                logger.info("Refreshing OAuth token on host...")
                subprocess.run(
                    ["claude", "-p", "--dangerously-skip-permissions", "say ok"],
                    capture_output=True,
                    timeout=60,
                )
                subprocess.run(["sync"], capture_output=True)
                logger.info("Token refreshed successfully")
            except Exception as e:
                logger.warning(f"Token refresh failed (continuing anyway): {e}")

            settings_file = claude_config_dir / "settings.json"
            try:
                creds_content = cred_file.read_text()
                creds_b64 = base64.b64encode(creds_content.encode()).decode()
                logger.info(f"Read credentials ({len(creds_content)} bytes)")
                if settings_file.exists():
                    settings_b64 = base64.b64encode(settings_file.read_text().encode()).decode()
            except Exception as e:
                logger.error(f"Failed to read credentials: {e}")

        # Build cmd parts and setup_script based on auth_mode
        cmd_parts = [
            "-v", f"{self.workspace_dir.absolute()}:/workspace",
            "-e", "HOME=/home/worker",
            "--memory", self.docker_memory,
            "--cpus", str(self.docker_cpus),
        ]

        if auth_mode == "api_key":
            cmd_parts.extend(["-e", f"ANTHROPIC_API_KEY={api_key}"])
            setup_script = "echo '=== Claude auth: API key ===';"

        elif auth_mode in ("mount", "keychain"):
            import tempfile, shutil
            self._auth_tmpdir = tempfile.mkdtemp(prefix="svengali-auth-")

            if auth_mode == "keychain":
                creds_path = Path(self._auth_tmpdir) / ".credentials.json"
                creds_path.write_text(json.dumps(keychain_creds))
                logger.info("Wrote Keychain credentials to temp dir")
            else:
                shutil.copy2(cred_file, Path(self._auth_tmpdir) / ".credentials.json")

            settings_file = claude_config_dir / "settings.json"
            if settings_file.exists():
                shutil.copy2(settings_file, Path(self._auth_tmpdir) / "settings.json")
            logger.info(f"Auth temp dir: {self._auth_tmpdir}")

            cmd_parts.extend(["-v", f"{self._auth_tmpdir}:/home/worker/.claude"])
            setup_script = (
                "echo '=== Claude auth: volume mount ==='; "
                "if [ ! -s /home/worker/.claude/.credentials.json ]; then "
                "  echo 'ERROR: credentials file missing or empty' >&2; exit 1; "
                "fi; "
                "grep -o 'expiresAt\":[0-9]*' /home/worker/.claude/.credentials.json | head -1;"
            )

        else:
            # base64 mode
            cmd_parts.extend([
                "-e", f"CREDS_B64={creds_b64}",
                "-e", f"SETTINGS_B64={settings_b64}",
            ])
            setup_script = (
                "echo '=== Claude auth: base64 inject ==='; "
                "printf '%s' \"$CREDS_B64\" | base64 -d > /home/worker/.claude/.credentials.json; "
                "printf '%s' \"$SETTINGS_B64\" | base64 -d > /home/worker/.claude/settings.json 2>/dev/null || true; "
                "if [ ! -s /home/worker/.claude/.credentials.json ]; then "
                "  echo 'ERROR: credentials decode failed - file is empty' >&2; exit 1; "
                "fi; "
                "python3 -c \"import json; json.load(open('/home/worker/.claude/.credentials.json'))\" 2>/dev/null || "
                "  { echo 'ERROR: credentials file is not valid JSON' >&2; exit 1; }; "
                "grep -o 'expiresAt\":[0-9]*' /home/worker/.claude/.credentials.json | head -1;"
            )

        # Add user-configured mounts
        for mount in self.mounts:
            mode = "ro" if mount.get("readonly", True) else "rw"
            cmd_parts.extend(["-v", f"{mount['host']}:{mount['container']}:{mode}"])

        return cmd_parts, setup_script, auth_mode

    def _cleanup_docker_auth(self):
        """Clean up temp auth dir if created by _setup_docker_auth."""
        if hasattr(self, '_auth_tmpdir') and self._auth_tmpdir:
            import shutil
            try:
                shutil.rmtree(self._auth_tmpdir)
            except Exception:
                pass
            self._auth_tmpdir = None

    def execute_local_pty(
        self,
        chunk_spec: dict,
        context: str,
        guide: dict,
        previous_feedback: Optional[str] = None,
        on_output: Optional[Callable[[bytes], None]] = None,
        baseline_commit: Optional[str] = None,
    ) -> WorkerResult:
        """Execute a chunk locally using Claude CLI with a PTY for full terminal rendering.

        The PTY allows Claude's React Ink UI (colors, spinners, progress bars) to render
        properly, and the raw ANSI output is streamed via on_output as bytes.
        """
        import pty
        import select
        import fcntl
        import struct
        import termios

        prompt = self._build_prompt(chunk_spec, context, guide, previous_feedback)

        logger.info(f"Executing Claude CLI (PTY mode) in {self.workspace_dir}")
        logger.debug(f"Prompt (first 200 chars): {prompt[:200]}...")

        try:
            if not baseline_commit:
                baseline_commit = self._get_current_commit()
            logger.info(f"Using baseline commit for diff: {baseline_commit[:8] if baseline_commit else 'none'}")

            # Create PTY pair
            master_fd, slave_fd = pty.openpty()

            # Set terminal size to 120x40
            winsize = struct.pack('HHHH', 40, 120, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

            # Pass prompt as positional argument for one-shot mode.
            # This way Claude processes the prompt and exits without needing
            # stdin to be a TTY. stdout/stderr on PTY slave ensures isatty(1)==true
            # so React Ink renders its full UI.
            cmd = ["claude", "--dangerously-skip-permissions", prompt]
            logger.info(f"Starting Claude CLI (PTY) in {self.workspace_dir}")
            logger.info(f"Prompt length: {len(prompt)} chars")

            process = subprocess.Popen(
                cmd,
                cwd=self.workspace_dir,
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
            )

            # Close slave_fd in parent - child has it
            os.close(slave_fd)

            logger.info("Reading PTY output (max 10 minutes)...")

            # Collect all raw output for summary extraction
            raw_output_parts = []
            import time
            start_time = time.time()
            timeout = 600  # 10 minutes

            while True:
                # Check timeout
                if time.time() - start_time > timeout:
                    logger.error("Claude PTY timed out after 10 minutes")
                    process.kill()
                    break

                # Check if process has exited and no more data
                ready, _, _ = select.select([master_fd], [], [], 0.1)

                if ready:
                    try:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        raw_output_parts.append(data)
                        if on_output:
                            on_output(data)
                    except OSError:
                        # PTY closed
                        break
                elif process.poll() is not None:
                    # Process exited - drain remaining output
                    while True:
                        ready, _, _ = select.select([master_fd], [], [], 0.05)
                        if not ready:
                            break
                        try:
                            data = os.read(master_fd, 4096)
                            if not data:
                                break
                            raw_output_parts.append(data)
                            if on_output:
                                on_output(data)
                        except OSError:
                            break
                    break

            os.close(master_fd)
            process.wait()

            logger.info(f"Claude CLI (PTY) exited with code {process.returncode}")

            # Combine raw output and strip ANSI for summary
            raw_bytes = b''.join(raw_output_parts)
            output_text = raw_bytes.decode('utf-8', errors='replace')
            clean_output = self._strip_ansi(output_text)

            # Get diff
            diff = self._get_diff_since_commit(baseline_commit)
            summary = self._extract_summary(clean_output)

            return WorkerResult(
                success=process.returncode == 0,
                output=clean_output,
                diff=diff,
                summary=summary,
                error=None if process.returncode == 0 else f"Exit code: {process.returncode}",
            )

        except Exception as e:
            import traceback
            logger.error(f"Worker execute_local_pty failed: {e}\n{traceback.format_exc()}")
            return WorkerResult(
                success=False,
                output="",
                diff="",
                summary="",
                error=str(e),
            )

    def execute_docker_pty(
        self,
        chunk_spec: dict,
        context: str,
        guide: dict,
        previous_feedback: Optional[str] = None,
        on_output: Optional[Callable[[bytes], None]] = None,
        baseline_commit: Optional[str] = None,
    ) -> WorkerResult:
        """Execute a chunk in Docker with PTY allocation for terminal rendering.

        Uses `script` inside the container to allocate a PTY (since docker -t
        requires the host stdin to be a TTY, which it isn't from subprocess).
        The prompt is written to a temp file and mounted into the container
        to avoid shell escaping issues.
        """
        import tempfile

        prompt = self._build_prompt(chunk_spec, context, guide, previous_feedback)

        logger.info("Executing Claude CLI in Docker container (PTY mode)")
        logger.debug(f"Prompt (first 200 chars): {prompt[:200]}...")

        try:
            cmd_parts, setup_script, auth_mode = self._setup_docker_auth()
        except RuntimeError as e:
            return WorkerResult(
                success=False, output="", diff="", summary="",
                error=str(e),
            )

        # Write prompt to temp file and mount it into container
        prompt_tmpfile = tempfile.NamedTemporaryFile(
            mode='w', suffix='.txt', delete=False, prefix='svengali-prompt-')
        prompt_tmpfile.write(prompt)
        prompt_tmpfile.close()
        self._prompt_tmpfile = prompt_tmpfile.name

        try:
            if not baseline_commit:
                baseline_commit = self._get_current_commit()
            logger.info(f"Using baseline commit for diff: {baseline_commit[:8] if baseline_commit else 'none'}")

            # Use `script` to allocate a PTY inside the container.
            # Single quotes around the -c argument prevent the outer bash from
            # expanding $(cat ...). The inner shell (spawned by script) reads the
            # prompt file directly — no intermediate variable, no escaping issues.
            full_setup = (
                setup_script +
                " echo '=== Running Claude (PTY) ==='; "
                "exec script -qfc "
                "'exec claude --dangerously-skip-permissions "
                "\"$(cat /tmp/svengali_prompt.txt)\"' /dev/null"
            )

            cmd = [
                "docker", "run",
                "--rm",
                "-e", "COLUMNS=120",
                "-e", "LINES=40",
                "-v", f"{prompt_tmpfile.name}:/tmp/svengali_prompt.txt:ro",
            ] + cmd_parts + [
                DOCKER_IMAGE,
                "bash", "-c", full_setup,
            ]

            logger.info(f"Docker script: {full_setup[:200]}...")

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            logger.info("Reading Docker PTY output...")

            # Read loop - script gives us PTY output on stdout as bytes
            raw_output_parts = []
            import time
            start_time = time.time()
            timeout = 600

            while True:
                if time.time() - start_time > timeout:
                    logger.error("Docker Claude PTY timed out after 10 minutes")
                    process.kill()
                    break

                data = process.stdout.read(4096)
                if not data:
                    break

                raw_output_parts.append(data)
                if on_output:
                    on_output(data)

            process.wait()
            logger.info(f"Docker Claude (PTY) finished with exit code {process.returncode}")

            # Combine and clean output
            raw_bytes = b''.join(raw_output_parts)
            output_text = raw_bytes.decode('utf-8', errors='replace')
            clean_output = self._strip_ansi(output_text)

            diff = self._get_diff_since_commit(baseline_commit)
            summary = self._extract_summary(clean_output)

            return WorkerResult(
                success=process.returncode == 0,
                output=clean_output,
                diff=diff,
                summary=summary,
                error=None if process.returncode == 0 else f"Exit code: {process.returncode}",
            )

        except Exception as e:
            import traceback
            logger.error(f"Worker execute_docker_pty failed: {e}\n{traceback.format_exc()}")
            return WorkerResult(
                success=False,
                output="",
                diff="",
                summary="",
                error=str(e),
            )
        finally:
            self._cleanup_docker_auth()
            # Clean up prompt temp file
            if hasattr(self, '_prompt_tmpfile') and self._prompt_tmpfile:
                try:
                    os.unlink(self._prompt_tmpfile)
                except Exception:
                    pass
                self._prompt_tmpfile = None

    def execute(
        self,
        chunk_spec: dict,
        context: str,
        guide: dict,
        previous_feedback: Optional[str] = None,
        on_output: Optional[Callable] = None,
        baseline_commit: Optional[str] = None,
        use_pty: bool = False,
    ) -> WorkerResult:
        """Execute a chunk using the configured method (local or Docker).

        Args:
            use_pty: If True, use PTY execution for full terminal rendering.
                     The on_output callback will receive bytes instead of str.
        """
        # PTY mode only for local execution — gives us xterm.js rendering.
        # Docker stays on the proven execute_docker path (uses -p flag).
        if use_pty and not self.use_docker:
            return self.execute_local_pty(
                chunk_spec, context, guide, previous_feedback, on_output, baseline_commit
            )
        elif self.use_docker:
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
