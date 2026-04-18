"""hpccctl remote execution environment — mTLS relay to container agent.

Uses hpccctl (Go binary) to relay commands to a UDS agent running inside
a persistent Apptainer container on HPC. The TLS agent forwards raw bytes
to the container agent via UDS — no host-side execution.

Container agent shell is zsh. container-zshenv provides PATH, mirrors,
SLURM, proxy env automatically. No wrapper functions, no manual sourcing.

File sync: rclone for small files (bidirectional, .rcloneignore filtered).
hpccctl push/pull (rsync over tunnel) for large files on demand.
"""

import logging
import os
import shutil
import subprocess

from tools.environments.base import BaseEnvironment, _popen_bash

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
                 config_path: str = ""):
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
        """Sync small files via rclone before each command execution."""
        # rclone bidirectional sync is handled externally (sync-watch.sh)
        pass

    def cleanup(self):
        """No persistent connection to close."""
        pass
