"""hpccctl remote execution environment — mTLS relay to container agent.

Uses hpccctl (Go binary) to relay commands to a UDS agent running inside
a persistent Apptainer container on HPC. The TLS agent forwards raw bytes
to the container agent via UDS — no host-side execution.

Container agent shell is zsh. container-zshenv provides PATH, mirrors,
SLURM, proxy env automatically. No wrapper functions, no manual sourcing.

File sync (optional): composes with SSHEnvironment for tar-over-SSH transport
via FileSyncManager.  Disabled when SSH params are not provided.
"""

import logging
import os
import shutil
import subprocess

from tools.environments.base import BaseEnvironment, _popen_bash
from tools.environments.ssh import SSHEnvironment

logger = logging.getLogger(__name__)


def _ensure_hpccctl_available() -> None:
    if not shutil.which("hpccctl"):
        raise RuntimeError(
            "hpccctl is not installed or not in PATH. "
            "Build from ~/academic/hpccctl/ and install to ~/.local/bin/"
        )


class HpccctlEnvironment(BaseEnvironment):
    """Run commands inside HPC container via hpccctl relay mode.

    TLS agent on cal (host) relays raw bytes to UDS agent inside
    the Apptainer container. Commands execute directly in the container
    environment — zsh shell, container-zshenv sourced automatically.
    """

    def __init__(self, addr: str = "127.0.0.1:18923",
                 relay: bool = True,
                 cwd: str = "~", timeout: int = 180,
                 config_path: str = "",
                 ssh_host: str = "", ssh_user: str = "",
                 ssh_port: int = 22, ssh_key_path: str = ""):
        self.addr = addr
        self.relay = relay
        self.config_path = config_path or os.path.expanduser(
            "~/.config/hpccctl/client-config.json"
        )
        self._remote_tmp = "/tmp"

        _ensure_hpccctl_available()
        self._hpccctl_bin = shutil.which("hpccctl")

        super().__init__(cwd=cwd, timeout=timeout)

        self._ping()
        self.init_session()

        # Optional SSH file sync — composes with SSHEnvironment for transport.
        self._ssh_env: SSHEnvironment | None = None
        self._sync_manager = None
        if ssh_host and ssh_user:
            logger.info(
                "hpccctl: enabling SSH file sync to %s@%s:%s",
                ssh_user, ssh_host, ssh_port,
            )
            self._ssh_env = SSHEnvironment(
                host=ssh_host,
                user=ssh_user,
                port=ssh_port,
                key_path=ssh_key_path,
                cwd=cwd,
                timeout=timeout,
            )
            self._sync_manager = self._ssh_env._sync_manager

    def _base_flags(self) -> list:
        """Common hpccctl flags (addr, config)."""
        cmd = ["--addr", self.addr]
        if self.config_path:
            cmd.extend(["--config", self.config_path])
        return cmd

    def _ping(self) -> None:
        """Verify hpccagent is reachable."""
        cmd = [self._hpccctl_bin, "ping"] + self._base_flags()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise RuntimeError(
                    f"hpccctl ping failed (exit {result.returncode}): "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            logger.info("hpccctl connected to %s", self.addr)
        except FileNotFoundError:
            raise RuntimeError(f"hpccctl binary not found: {self._hpccctl_bin}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"hpccctl ping timed out connecting to {self.addr}")

    def get_temp_dir(self) -> str:
        return self._remote_tmp

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """Spawn hpccctl exec process.

        Uses --relay to forward command to container agent via UDS.
        Container agent runs zsh -c inside the Apptainer container.
        container-zshenv sourced automatically — no wrapper needed.
        """
        cmd = [self._hpccctl_bin, "exec",
               "--timeout", str(timeout)]
        cmd.extend(self._base_flags())
        if self.relay:
            cmd.append("--relay")
        cmd.extend(["--cmd", cmd_string])

        return _popen_bash(cmd, stdin_data)

    def _before_execute(self) -> None:
        """Sync files to remote via SSH before each command execution."""
        if self._sync_manager:
            self._sync_manager.sync()

    def cleanup(self):
        """Sync files back, close SSH connection, and remove session directory."""
        if self._sync_manager:
            try:
                logger.info("hpccctl: syncing files back from remote...")
                self._sync_manager.sync_back()
            except Exception:
                pass
        if self._ssh_env:
            try:
                self._ssh_env.cleanup()
            except Exception:
                pass
        try:
            self.execute(f"rm -rf {self._session_dir}", timeout=5)
        except Exception:
            pass
