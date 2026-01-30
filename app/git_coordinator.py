"""Git coordination for managing code between host and VMs."""

import subprocess
from pathlib import Path
from typing import Optional
from dataclasses import dataclass


@dataclass
class BranchInfo:
    name: str
    commit: str
    behind: int = 0
    ahead: int = 0


class GitCoordinator:
    """Manages git operations for the supervisor-worker workflow."""

    def __init__(self, bare_repo_path: Path, workspace_path: Optional[Path] = None):
        self.bare_repo_path = Path(bare_repo_path)
        self.workspace_path = Path(workspace_path) if workspace_path else None

    def init_bare_repo(self) -> bool:
        """Initialize the bare repository if it doesn't exist."""
        if self.bare_repo_path.exists():
            return True

        try:
            self.bare_repo_path.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "--bare"],
                cwd=self.bare_repo_path,
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def get_clone_url(self) -> str:
        """Get the URL for cloning the bare repo."""
        return str(self.bare_repo_path.absolute())

    def create_task_branch(self, task_id: str) -> str:
        """Create a branch for a task."""
        branch_name = f"task/{task_id}"

        if self.workspace_path:
            try:
                # Create branch from current HEAD
                subprocess.run(
                    ["git", "checkout", "-b", branch_name],
                    cwd=self.workspace_path,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                # Branch might already exist
                subprocess.run(
                    ["git", "checkout", branch_name],
                    cwd=self.workspace_path,
                    check=True,
                    capture_output=True,
                )

        return branch_name

    def create_chunk_branch(self, task_id: str, chunk_id: str) -> str:
        """Create a branch for a specific chunk."""
        branch_name = f"task/{task_id}/{chunk_id}"

        if self.workspace_path:
            # First ensure we're on the task branch
            task_branch = f"task/{task_id}"
            try:
                subprocess.run(
                    ["git", "checkout", task_branch],
                    cwd=self.workspace_path,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                pass

            try:
                subprocess.run(
                    ["git", "checkout", "-b", branch_name],
                    cwd=self.workspace_path,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                subprocess.run(
                    ["git", "checkout", branch_name],
                    cwd=self.workspace_path,
                    check=True,
                    capture_output=True,
                )

        return branch_name

    def get_diff(self, base_ref: str = "HEAD~1", target_ref: str = "HEAD") -> str:
        """Get diff between two refs."""
        if not self.workspace_path:
            return ""

        try:
            result = subprocess.run(
                ["git", "diff", base_ref, target_ref],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
            )
            return result.stdout
        except subprocess.CalledProcessError:
            return ""

    def get_staged_diff(self) -> str:
        """Get diff of staged changes."""
        if not self.workspace_path:
            return ""

        try:
            result = subprocess.run(
                ["git", "diff", "--cached"],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
            )
            return result.stdout
        except subprocess.CalledProcessError:
            return ""

    def get_unstaged_diff(self) -> str:
        """Get diff of unstaged changes."""
        if not self.workspace_path:
            return ""

        try:
            result = subprocess.run(
                ["git", "diff"],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
            )
            return result.stdout
        except subprocess.CalledProcessError:
            return ""

    def get_all_changes_diff(self) -> str:
        """Get diff of all changes (staged and unstaged)."""
        if not self.workspace_path:
            return ""

        try:
            result = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
            )
            return result.stdout
        except subprocess.CalledProcessError:
            return ""

    def commit(self, message: str, add_all: bool = True) -> Optional[str]:
        """Create a commit and return the commit hash."""
        if not self.workspace_path:
            return None

        try:
            if add_all:
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=self.workspace_path,
                    check=True,
                    capture_output=True,
                )

            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self.workspace_path,
                check=True,
                capture_output=True,
            )

            # Get commit hash
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()

        except subprocess.CalledProcessError:
            return None

    def push(self, branch: Optional[str] = None, force: bool = False) -> bool:
        """Push to remote."""
        if not self.workspace_path:
            return False

        try:
            cmd = ["git", "push", "-u", "origin"]
            if branch:
                cmd.append(branch)
            else:
                cmd.append("HEAD")
            if force:
                cmd.append("--force")

            subprocess.run(
                cmd,
                cwd=self.workspace_path,
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def pull(self, branch: Optional[str] = None) -> bool:
        """Pull from remote."""
        if not self.workspace_path:
            return False

        try:
            cmd = ["git", "pull", "origin"]
            if branch:
                cmd.append(branch)

            subprocess.run(
                cmd,
                cwd=self.workspace_path,
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def merge_chunk(self, task_id: str, chunk_id: str) -> bool:
        """Merge a chunk branch into the task branch."""
        if not self.workspace_path:
            return False

        chunk_branch = f"task/{task_id}/{chunk_id}"
        task_branch = f"task/{task_id}"

        try:
            # Checkout task branch
            subprocess.run(
                ["git", "checkout", task_branch],
                cwd=self.workspace_path,
                check=True,
                capture_output=True,
            )

            # Merge chunk branch
            subprocess.run(
                ["git", "merge", chunk_branch, "-m", f"Merge {chunk_id}"],
                cwd=self.workspace_path,
                check=True,
                capture_output=True,
            )

            return True
        except subprocess.CalledProcessError:
            return False

    def reset_to_commit(self, commit: str, hard: bool = False) -> bool:
        """Reset to a specific commit."""
        if not self.workspace_path:
            return False

        try:
            cmd = ["git", "reset"]
            if hard:
                cmd.append("--hard")
            cmd.append(commit)

            subprocess.run(
                cmd,
                cwd=self.workspace_path,
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def get_current_branch(self) -> Optional[str]:
        """Get the current branch name."""
        if not self.workspace_path:
            return None

        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None

    def get_commit_log(self, count: int = 10) -> list[dict]:
        """Get recent commit log."""
        if not self.workspace_path:
            return []

        try:
            result = subprocess.run(
                [
                    "git", "log",
                    f"-{count}",
                    "--pretty=format:%H|%an|%ae|%s|%ai",
                ],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
            )

            commits = []
            for line in result.stdout.strip().split("\n"):
                if line:
                    parts = line.split("|")
                    if len(parts) >= 5:
                        commits.append({
                            "hash": parts[0],
                            "author": parts[1],
                            "email": parts[2],
                            "message": parts[3],
                            "date": parts[4],
                        })
            return commits

        except subprocess.CalledProcessError:
            return []

    def clone_for_vm(self, vm_workspace: Path, branch: Optional[str] = None) -> bool:
        """Clone the bare repo into a VM workspace."""
        try:
            cmd = ["git", "clone", str(self.bare_repo_path), str(vm_workspace)]
            if branch:
                cmd.extend(["-b", branch])

            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def get_file_at_commit(self, file_path: str, commit: str = "HEAD") -> Optional[str]:
        """Get file contents at a specific commit."""
        if not self.workspace_path:
            return None

        try:
            result = subprocess.run(
                ["git", "show", f"{commit}:{file_path}"],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
            )
            return result.stdout
        except subprocess.CalledProcessError:
            return None

    def has_uncommitted_changes(self) -> bool:
        """Check if there are uncommitted changes."""
        if not self.workspace_path:
            return False

        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
            )
            return bool(result.stdout.strip())
        except subprocess.CalledProcessError:
            return False
