"""VM pool manager for Vagrant-based worker VMs."""

import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum


class VMStatus(Enum):
    IDLE = "idle"
    BUSY = "busy"
    PROVISIONING = "provisioning"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class VM:
    name: str
    status: VMStatus = VMStatus.STOPPED
    ssh_host: Optional[str] = None
    ssh_port: int = 22
    current_task: Optional[str] = None
    last_used: Optional[float] = None


@dataclass
class VMPoolConfig:
    vagrant_dir: Path
    pool_size: int = 3
    base_box: str = "ubuntu/jammy64"
    memory: int = 4096
    cpus: int = 2
    ssh_user: str = "vagrant"


class VMPool:
    """Manages a pool of Vagrant VMs for worker execution."""

    def __init__(self, config: VMPoolConfig):
        self.config = config
        self.vms: dict[str, VM] = {}
        self._lock = threading.Lock()
        self._initialized = False

    def initialize(self, background: bool = True):
        """Initialize the VM pool."""
        if self._initialized:
            return

        # Create VM entries
        for i in range(1, self.config.pool_size + 1):
            vm_name = f"worker-{i}"
            self.vms[vm_name] = VM(name=vm_name)

        if background:
            thread = threading.Thread(target=self._warm_pool, daemon=True)
            thread.start()
        else:
            self._warm_pool()

        self._initialized = True

    def _warm_pool(self):
        """Start all VMs in the pool."""
        for vm_name in self.vms:
            self._start_vm(vm_name)

    def _start_vm(self, vm_name: str):
        """Start a specific VM."""
        with self._lock:
            if vm_name in self.vms:
                self.vms[vm_name].status = VMStatus.PROVISIONING

        try:
            # Run vagrant up for this VM
            result = subprocess.run(
                ["vagrant", "up", vm_name],
                cwd=self.config.vagrant_dir,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minute timeout
            )

            if result.returncode == 0:
                # Get SSH config
                ssh_config = self._get_ssh_config(vm_name)
                with self._lock:
                    if vm_name in self.vms:
                        self.vms[vm_name].status = VMStatus.IDLE
                        self.vms[vm_name].ssh_host = ssh_config.get("host", "127.0.0.1")
                        self.vms[vm_name].ssh_port = ssh_config.get("port", 22)
            else:
                with self._lock:
                    if vm_name in self.vms:
                        self.vms[vm_name].status = VMStatus.ERROR

        except subprocess.TimeoutExpired:
            with self._lock:
                if vm_name in self.vms:
                    self.vms[vm_name].status = VMStatus.ERROR
        except Exception:
            with self._lock:
                if vm_name in self.vms:
                    self.vms[vm_name].status = VMStatus.ERROR

    def _get_ssh_config(self, vm_name: str) -> dict:
        """Get SSH configuration for a VM."""
        try:
            result = subprocess.run(
                ["vagrant", "ssh-config", vm_name],
                cwd=self.config.vagrant_dir,
                capture_output=True,
                text=True,
            )

            config = {}
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("HostName"):
                    config["host"] = line.split()[1]
                elif line.startswith("Port"):
                    config["port"] = int(line.split()[1])
                elif line.startswith("User"):
                    config["user"] = line.split()[1]
                elif line.startswith("IdentityFile"):
                    config["key"] = line.split()[1]

            return config

        except Exception:
            return {}

    def acquire(self, task_id: str) -> Optional[VM]:
        """Acquire an idle VM for a task."""
        with self._lock:
            for vm in self.vms.values():
                if vm.status == VMStatus.IDLE:
                    vm.status = VMStatus.BUSY
                    vm.current_task = task_id
                    vm.last_used = time.time()
                    return vm
        return None

    def release(self, vm_name: str, reset: bool = True):
        """Release a VM back to the pool."""
        with self._lock:
            if vm_name in self.vms:
                vm = self.vms[vm_name]
                vm.current_task = None
                vm.status = VMStatus.PROVISIONING if reset else VMStatus.IDLE

        if reset:
            # Reset VM in background
            thread = threading.Thread(
                target=self._reset_vm,
                args=(vm_name,),
                daemon=True,
            )
            thread.start()

    def _reset_vm(self, vm_name: str):
        """Reset a VM to clean state."""
        try:
            # Restore to snapshot
            subprocess.run(
                ["vagrant", "snapshot", "restore", vm_name, "clean", "--no-provision"],
                cwd=self.config.vagrant_dir,
                capture_output=True,
                timeout=300,
            )

            with self._lock:
                if vm_name in self.vms:
                    self.vms[vm_name].status = VMStatus.IDLE

        except Exception:
            # If snapshot restore fails, try full reprovision
            self._start_vm(vm_name)

    def create_snapshot(self, vm_name: str, snapshot_name: str = "clean"):
        """Create a snapshot of a VM."""
        try:
            subprocess.run(
                ["vagrant", "snapshot", "save", vm_name, snapshot_name],
                cwd=self.config.vagrant_dir,
                capture_output=True,
                timeout=300,
            )
            return True
        except Exception:
            return False

    def get_status(self) -> dict:
        """Get status of all VMs."""
        with self._lock:
            return {
                name: {
                    "status": vm.status.value,
                    "current_task": vm.current_task,
                    "ssh_host": vm.ssh_host,
                    "ssh_port": vm.ssh_port,
                }
                for name, vm in self.vms.items()
            }

    def get_idle_count(self) -> int:
        """Get count of idle VMs."""
        with self._lock:
            return sum(1 for vm in self.vms.values() if vm.status == VMStatus.IDLE)

    def execute_on_vm(
        self,
        vm: VM,
        command: str,
        timeout: int = 300,
    ) -> tuple[bool, str]:
        """Execute a command on a VM via SSH."""
        if not vm.ssh_host:
            return False, "VM SSH not configured"

        try:
            ssh_config = self._get_ssh_config(vm.name)
            key_file = ssh_config.get("key", "")

            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-p", str(vm.ssh_port),
            ]

            if key_file:
                cmd.extend(["-i", key_file])

            cmd.extend([
                f"{self.config.ssh_user}@{vm.ssh_host}",
                command,
            ])

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            return result.returncode == 0, result.stdout + result.stderr

        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)

    def shutdown(self):
        """Shutdown all VMs."""
        try:
            subprocess.run(
                ["vagrant", "halt"],
                cwd=self.config.vagrant_dir,
                capture_output=True,
                timeout=300,
            )
        except Exception:
            pass

        with self._lock:
            for vm in self.vms.values():
                vm.status = VMStatus.STOPPED

    def destroy(self, vm_name: Optional[str] = None):
        """Destroy VM(s)."""
        try:
            cmd = ["vagrant", "destroy", "-f"]
            if vm_name:
                cmd.append(vm_name)

            subprocess.run(
                cmd,
                cwd=self.config.vagrant_dir,
                capture_output=True,
                timeout=300,
            )
        except Exception:
            pass


# Convenience function for creating a pool from config file
def create_pool_from_config(config_path: Path, vagrant_dir: Path) -> VMPool:
    """Create a VM pool from a YAML config file."""
    import yaml

    with open(config_path) as f:
        config = yaml.safe_load(f)

    vm_config = config.get("vm", {})

    pool_config = VMPoolConfig(
        vagrant_dir=vagrant_dir,
        pool_size=config.get("worker", {}).get("vm_pool_size", 3),
        base_box=vm_config.get("base_box", "ubuntu/jammy64"),
        memory=vm_config.get("memory", 4096),
        cpus=vm_config.get("cpus", 2),
    )

    return VMPool(pool_config)
