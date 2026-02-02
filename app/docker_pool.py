"""Docker container pool for isolated Claude CLI execution."""

import subprocess
import threading
import logging
import time
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

IMAGE_NAME = "svengalibot-worker"


class ContainerStatus(Enum):
    IDLE = "idle"
    BUSY = "busy"
    BUILDING = "building"
    ERROR = "error"


@dataclass
class DockerConfig:
    """Docker pool configuration."""
    pool_size: int = 3
    memory_limit: str = "4g"
    cpu_limit: float = 2.0
    timeout: int = 600  # 10 minutes


class DockerPool:
    """Manages Docker containers for worker execution."""

    def __init__(self, config: DockerConfig, docker_dir: Path):
        self.config = config
        self.docker_dir = docker_dir
        self._image_ready = False
        self._lock = threading.Lock()

    def initialize(self, background: bool = True):
        """Build the Docker image."""
        if self._image_ready:
            return

        if background:
            thread = threading.Thread(target=self._build_image, daemon=True)
            thread.start()
        else:
            self._build_image()

    def _build_image(self):
        """Build the worker Docker image."""
        logger.info(f"Building Docker image: {IMAGE_NAME}")
        try:
            result = subprocess.run(
                ["docker", "build", "-t", IMAGE_NAME, "."],
                cwd=self.docker_dir,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                self._image_ready = True
                logger.info(f"Docker image {IMAGE_NAME} built successfully")
            else:
                logger.error(f"Docker build failed: {result.stderr}")
        except subprocess.TimeoutExpired:
            logger.error("Docker build timed out")
        except FileNotFoundError:
            logger.error("Docker not found - is it installed?")
        except Exception as e:
            logger.error(f"Docker build error: {e}")

    def is_ready(self) -> bool:
        """Check if Docker image is ready."""
        if self._image_ready:
            return True

        # Check if image exists
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", IMAGE_NAME],
                capture_output=True,
                timeout=10,
            )
            self._image_ready = result.returncode == 0
            return self._image_ready
        except Exception:
            return False

    def execute(
        self,
        workspace_dir: Path,
        prompt: str,
        on_output: Optional[Callable[[str], None]] = None,
        timeout: Optional[int] = None,
    ) -> tuple[bool, str, str]:
        """Execute Claude CLI in a Docker container.

        Args:
            workspace_dir: Project directory to mount
            prompt: Prompt to send to Claude
            on_output: Callback for streaming output
            timeout: Execution timeout in seconds

        Returns:
            Tuple of (success, output, error)
        """
        if not self.is_ready():
            return False, "", "Docker image not ready. Run: docker build -t svengalibot-worker docker/"

        timeout = timeout or self.config.timeout

        # Get Claude auth directory
        claude_config_dir = Path.home() / ".claude"

        # Build docker run command
        cmd = [
            "docker", "run",
            "--rm",  # Remove container after exit
            "-v", f"{workspace_dir.absolute()}:/workspace",
            "-v", f"{claude_config_dir}:/home/worker/.claude",  # Mount Claude auth (read-only)
            "--memory", self.config.memory_limit,
            "--cpus", str(self.config.cpu_limit),
            IMAGE_NAME,
            "claude", "-p", "--dangerously-skip-permissions", prompt,
        ]

        logger.info(f"Running Claude in Docker container for {workspace_dir}")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            output_lines = []

            # Stream output
            for line in iter(process.stdout.readline, ""):
                if line:
                    output_lines.append(line)
                    if on_output:
                        on_output(line)

            process.wait(timeout=timeout)
            output = "".join(output_lines)

            return process.returncode == 0, output, ""

        except subprocess.TimeoutExpired:
            process.kill()
            return False, "".join(output_lines), "Execution timed out"
        except Exception as e:
            logger.error(f"Docker execution failed: {e}")
            return False, "", str(e)

    def execute_streaming(
        self,
        workspace_dir: Path,
        prompt: str,
        timeout: Optional[int] = None,
    ):
        """Execute Claude CLI and yield output lines."""
        if not self.is_ready():
            yield "[ERROR] Docker image not ready"
            return

        timeout = timeout or self.config.timeout

        # Get Claude auth directory
        claude_config_dir = Path.home() / ".claude"

        cmd = [
            "docker", "run",
            "--rm",
            "-v", f"{workspace_dir.absolute()}:/workspace",
            "-v", f"{claude_config_dir}:/home/worker/.claude",  # Mount Claude auth
            "--memory", self.config.memory_limit,
            "--cpus", str(self.config.cpu_limit),
            "--network", "none",
            IMAGE_NAME,
            "claude", "-p", "--dangerously-skip-permissions", prompt,
        ]

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            for line in iter(process.stdout.readline, ""):
                yield line

            process.wait(timeout=timeout)
            yield f"\n[Exit code: {process.returncode}]\n"

        except subprocess.TimeoutExpired:
            process.kill()
            yield "\n[ERROR] Execution timed out\n"
        except Exception as e:
            yield f"\n[ERROR] {e}\n"

    def get_status(self) -> dict:
        """Get Docker pool status."""
        return {
            "image_ready": self.is_ready(),
            "image_name": IMAGE_NAME,
            "config": {
                "memory_limit": self.config.memory_limit,
                "cpu_limit": self.config.cpu_limit,
                "timeout": self.config.timeout,
            }
        }

    def cleanup(self):
        """Clean up any leftover containers."""
        try:
            # Remove any stopped svengalibot containers
            subprocess.run(
                ["docker", "container", "prune", "-f", "--filter", f"ancestor={IMAGE_NAME}"],
                capture_output=True,
                timeout=30,
            )
        except Exception:
            pass


def create_pool_from_config(config_path: Path, docker_dir: Path) -> DockerPool:
    """Create a Docker pool from config file."""
    import yaml

    with open(config_path) as f:
        config = yaml.safe_load(f)

    docker_config = config.get("docker", {})

    pool_config = DockerConfig(
        pool_size=docker_config.get("pool_size", 3),
        memory_limit=docker_config.get("memory", "4g"),
        cpu_limit=docker_config.get("cpus", 2.0),
        timeout=docker_config.get("timeout", 600),
    )

    return DockerPool(pool_config, docker_dir)
