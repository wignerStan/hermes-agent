"""hpccctl remote execution environment — mTLS docker-exec-style command execution.

Uses hpccctl (Go binary) to execute commands on HPC via mTLS.
No SSH, no DPI detection. hpccagent runs on HPC, accepts commands
over TLS with client certificate auth.

hpccagent uses emulator style: ``shell -c "wrapperFunc cmd"``.
No nested ``bash -c`` — the cmd string is the shell script directly.
Wrapper functions (_sif_proxy, etc.) are defined in cal-bashrc.

File sync: rclone for small files (bidirectional, .rcloneignore filtered).
hpccctl push/pull (rsync over tunnel) for large files on demand.

Spawn-per-call: every execute() spawns ``hpccctl exec --wrapper ... CMD``
Session snapshot preserves env vars across calls on remote host.
CWD persists via in-band stdout markers.
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
            "Build from ~/academic/hpccctl/ and install to ~/local/bin/"
        )


class HpccctlEnvironment(BaseEnvironment):
    """Run commands on HPC via hpccctl (mTLS docker-exec-style).

    hpccagent runs on the HPC host (cal), listening on port 18923.
    hpccctl connects via mTLS and executes commands inside the container
    using a shell wrapper function (e.g. _sif_proxy from cal-bashrc).

    This replaces the singularity underlay backend — no fragile bind mount
    management, no proxy injection, no zsh config patches. The wrapper
    function handles all of that natively.
    """

    def __init__(self, addr: str = "localhost:18923",
                 wrapper: str = "_sif_proxy",
                 cwd: str = "~", timeout: int = 180,
                 cert_dir: str = ""):
        super().__init__(cwd=cwd, timeout=timeout)
        self.addr = addr
        self.wrapper = wrapper
        self.cert_dir = cert_dir or os.path.expanduser("~/.hpccctl/certs")

        _ensure_hpccctl_available()
        self._hpccctl_bin = shutil.which("hpccctl")

        # Verify connectivity
        self._ping()

        # Session snapshot on remote host
        self._remote_tmp = "/tmp"
        self.init_session()

    def _base_flags(self) -> list:
        """Common hpccctl flags (addr, certs, wrapper)."""
        cmd = ["--addr", self.addr]
        if self.cert_dir:
            cmd.extend(["--cert-dir", self.cert_dir])
        if self.wrapper:
            cmd.extend(["--wrapper", self.wrapper])
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

        Emulator style: cmd_string is passed directly as the shell script.
        hpccagent builds: ``shell -c "wrapperFunc cmd_string"``.
        No nested ``bash -c`` — avoids quoting/parse issues.
        """
        cmd = [self._hpccctl_bin, "exec",
               "--timeout", str(timeout)]
        cmd.extend(self._base_flags())
        # cmd_string is the raw shell script — hpccagent wraps it:
        # shell -c "_sif_proxy <cmd_string>" (if wrapper set)
        # shell -c "<cmd_string>" (if no wrapper)
        cmd.append(cmd_string)

        return _popen_bash(cmd, stdin_data)

    def _before_execute(self) -> None:
        """Sync small files via rclone before each command execution."""
        # rclone bidirectional sync is handled externally (sync-watch.sh)
        # or can be triggered here for on-demand sync.
        pass

    def cleanup(self):
        """No persistent connection to close."""
        pass
