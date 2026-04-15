"""Singularity/Apptainer persistent container environment.

Security-hardened with --containall, --no-home, capability dropping.
Supports configurable resource limits and optional filesystem persistence
via writable overlay directories that survive across sessions.
"""

import logging
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home
from tools.environments.base import (
    BaseEnvironment,
    _load_json_store,
    _popen_bash,
    _save_json_store,
)

logger = logging.getLogger(__name__)

_SNAPSHOT_STORE = get_hermes_home() / "singularity_snapshots.json"


def _find_singularity_executable() -> str:
    """Locate the apptainer or singularity CLI binary."""
    if shutil.which("apptainer"):
        return "apptainer"
    if shutil.which("singularity"):
        return "singularity"
    raise RuntimeError(
        "Neither 'apptainer' nor 'singularity' was found in PATH. "
        "Install Apptainer (https://apptainer.org/docs/admin/main/installation.html) "
        "or Singularity and ensure the CLI is available."
    )


def _ensure_singularity_available() -> str:
    """Preflight check: resolve the executable and verify it responds."""
    exe = _find_singularity_executable()
    try:
        result = subprocess.run(
            [exe, "version"], capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"Singularity backend selected but '{exe}' could not be executed."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"'{exe} version' timed out.")

    if result.returncode != 0:
        stderr = result.stderr.strip()[:200]
        raise RuntimeError(f"'{exe} version' failed (exit code {result.returncode}): {stderr}")
    return exe


def _load_snapshots() -> dict:
    return _load_json_store(_SNAPSHOT_STORE)


def _save_snapshots(data: dict) -> None:
    _save_json_store(_SNAPSHOT_STORE, data)


def _get_scratch_dir() -> Path:
    custom_scratch = os.getenv("TERMINAL_SCRATCH_DIR")
    if custom_scratch:
        scratch_path = Path(custom_scratch)
        scratch_path.mkdir(parents=True, exist_ok=True)
        return scratch_path

    from tools.environments.base import get_sandbox_dir
    sandbox = get_sandbox_dir() / "singularity"

    scratch = Path("/scratch")
    if scratch.exists() and os.access(scratch, os.W_OK):
        user_scratch = scratch / os.getenv("USER", "hermes") / "hermes-agent"
        user_scratch.mkdir(parents=True, exist_ok=True)
        logger.info("Using /scratch for sandboxes: %s", user_scratch)
        return user_scratch

    sandbox.mkdir(parents=True, exist_ok=True)
    return sandbox


def _get_apptainer_cache_dir() -> Path:
    cache_dir = os.getenv("APPTAINER_CACHEDIR")
    if cache_dir:
        cache_path = Path(cache_dir)
        cache_path.mkdir(parents=True, exist_ok=True)
        return cache_path
    scratch = _get_scratch_dir()
    cache_path = scratch / ".apptainer"
    cache_path.mkdir(parents=True, exist_ok=True)
    return cache_path


_sif_build_lock = threading.Lock()


def _get_or_build_sif(image: str, executable: str = "apptainer") -> str:
    if image.endswith('.sif') and Path(image).exists():
        return image
    if not image.startswith('docker://'):
        return image

    image_name = image.replace('docker://', '').replace('/', '-').replace(':', '-')
    cache_dir = _get_apptainer_cache_dir()
    sif_path = cache_dir / f"{image_name}.sif"

    if sif_path.exists():
        return str(sif_path)

    with _sif_build_lock:
        if sif_path.exists():
            return str(sif_path)

        logger.info("Building SIF image (one-time setup)...")
        logger.info("  Source: %s", image)
        logger.info("  Target: %s", sif_path)

        tmp_dir = cache_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["APPTAINER_TMPDIR"] = str(tmp_dir)
        env["APPTAINER_CACHEDIR"] = str(cache_dir)

        try:
            result = subprocess.run(
                [executable, "build", str(sif_path), image],
                capture_output=True, text=True, timeout=600, env=env,
            )
            if result.returncode != 0:
                logger.warning("SIF build failed, falling back to docker:// URL")
                logger.warning("  Error: %s", result.stderr[:500])
                return image
            logger.info("SIF image built successfully")
            return str(sif_path)
        except subprocess.TimeoutExpired:
            logger.warning("SIF build timed out, falling back to docker:// URL")
            if sif_path.exists():
                sif_path.unlink()
            return image
        except Exception as e:
            logger.warning("SIF build error: %s, falling back to docker:// URL", e)
            return image


def _build_underlay_flags() -> list[str]:
    """Build apptainer exec flags for HPC underlay mode (kernel 3.10).

    Replicates the bind-mount pattern from cal-bashrc:
    HOME, container-data, container-bin, /tmp, proxy stack, HPC dirs,
    SLURM libs, Homebrew, passwd, ZDOTDIR.
    Activated by HERMES_SINGULARITY_HPC_MODE=underlay env var.
    """
    flags = ["--underlay"]
    home = os.getenv("HOME", "")
    if home:
        flags.extend(["--bind", home, "--home", home])
    sdata = os.path.join(home, "container-data")
    if os.path.isdir(sdata):
        flags.extend(["--bind", f"{sdata}:/opt/container-data"])
    sbin = os.path.join(home, "container-bin")
    if os.path.isdir(sbin):
        flags.extend(["--bind", f"{sbin}:/opt/container-bin"])
    flags.extend(["--bind", "/tmp:/tmp"])

    # Proxy stack: inject LD_PRELOAD into container for transparent SOCKS5
    proxy_dir = os.path.join(home, ".proxy-stack")
    pc_so = os.path.join(proxy_dir, "libproxychains4-rust.so")
    pr_so = os.path.join(proxy_dir, "libproxy-resolve.so")
    # Config lookup: check proxy-stack dir first (vscode-tunnel pattern),
    # then legacy home dir path. Skip injection entirely if no config found.
    conf = ""
    for _c in (
        os.path.join(proxy_dir, "proxychains4.conf"),
        os.path.join(home, ".proxychains4-rust.conf"),
    ):
        if os.path.isfile(_c):
            conf = _c
            break
    if os.path.isfile(pc_so) and conf:
        preload_parts = ["/opt/pr/libproxychains4-rust.so"]
        if os.path.isfile(pr_so):
            preload_parts.append("/opt/pr/libproxy-resolve.so")
        flags.extend(["--bind", f"{proxy_dir}:/opt/pr"])
        flags.extend(["--env", f"LD_PRELOAD={':'.join(preload_parts)}"])
        flags.extend(["--env", "LD_LIBRARY_PATH=/opt/pr"])
        flags.extend(["--bind", f"{conf}:/opt/pr/proxychains4.conf"])
        flags.extend(["--env", "PROXYCHAINS_CONF_FILE=/opt/pr/proxychains4.conf"])

    # HPC directories (SLURM, gridview) — auto-detect
    for d in ("/opt/gridview", "/opt/hpc/software",
              "/public/slurm_share", "/public/software"):
        if os.path.isdir(d):
            flags.extend(["--bind", f"{d}:{d}"])

    # SLURM .so + munge socket
    slurm_libs = os.path.join(sdata, "slurm-libs")
    if os.path.isdir(slurm_libs):
        for f in ("liblua-5.1.so", "libjson-c.so.2"):
            p = os.path.join(slurm_libs, f)
            if os.path.isfile(p):
                flags.extend(["--bind", f"{p}:/lib64/{f}"])
    munge_sock = "/opt/gridview/munge/run/munge/munge.socket.2"
    if os.path.exists(munge_sock):
        flags.extend(["--bind", f"{munge_sock}:/run/munge/munge.socket.2"])

    # Homebrew bind mounts (underlay: rootfs read-only)
    hb = os.path.join(sdata, "homebrew")
    for sub in ("bin", "lib", "etc"):
        sub_path = os.path.join(hb, sub)
        if os.path.isdir(sub_path):
            flags.extend(["--bind", f"{sub_path}:/home/linuxbrew/.linuxbrew/{sub}"])

    # Fixed passwd
    passwd = os.path.join(proxy_dir, "etc-passwd")
    if os.path.isfile(passwd):
        flags.extend(["--bind", f"{passwd}:/etc/passwd"])

    # ZDOTDIR for container zsh config isolation
    flags.extend(["--env", "ZDOTDIR=/opt/container-data/zsh"])

    return flags


class SingularityEnvironment(BaseEnvironment):
    """Hardened Singularity/Apptainer container with resource limits and persistence.

    Spawn-per-call: every execute() spawns a fresh ``apptainer exec ... bash -c`` process.
    Session snapshot preserves env vars across calls.
    CWD persists via in-band stdout markers.
    """

    def __init__(
        self,
        image: str,
        cwd: str = "~",
        timeout: int = 60,
        cpu: float = 0,
        memory: int = 0,
        disk: int = 0,
        persistent_filesystem: bool = False,
        task_id: str = "default",
    ):
        super().__init__(cwd=cwd, timeout=timeout)
        self.executable = _ensure_singularity_available()
        self.image = _get_or_build_sif(image, self.executable)
        self.instance_id = f"hermes_{uuid.uuid4().hex[:12]}"
        self._instance_started = False
        self._persistent = persistent_filesystem
        self._task_id = task_id
        self._overlay_dir: Optional[Path] = None
        self._cpu = cpu
        self._memory = memory

        if self._persistent:
            overlay_base = _get_scratch_dir() / "hermes-overlays"
            overlay_base.mkdir(parents=True, exist_ok=True)
            self._overlay_dir = overlay_base / f"overlay-{task_id}"
            self._overlay_dir.mkdir(parents=True, exist_ok=True)

        self._start_instance()
        self.init_session()

    def _start_instance(self):
        # HPC underlay mode: per-command exec, no persistent instance
        if os.getenv("HERMES_SINGULARITY_HPC_MODE", "") == "underlay":
            self._underlay_flags = _build_underlay_flags()
            self._instance_started = True
            logger.info("Singularity underlay mode (per-command exec)")
            return

        cmd = [self.executable, "instance", "start"]
        cmd.extend(["--containall", "--no-home"])

        if self._persistent and self._overlay_dir:
            cmd.extend(["--overlay", str(self._overlay_dir)])
        else:
            cmd.append("--writable-tmpfs")

        try:
            from tools.credential_files import get_credential_file_mounts, get_skills_directory_mount
            for mount_entry in get_credential_file_mounts():
                cmd.extend(["--bind", f"{mount_entry['host_path']}:{mount_entry['container_path']}:ro"])
            for skills_mount in get_skills_directory_mount():
                cmd.extend(["--bind", f"{skills_mount['host_path']}:{skills_mount['container_path']}:ro"])
        except Exception as e:
            logger.debug("Singularity: could not load credential/skills mounts: %s", e)

        if self._memory > 0:
            cmd.extend(["--memory", f"{self._memory}M"])
        if self._cpu > 0:
            cmd.extend(["--cpus", str(self._cpu)])

        cmd.extend([str(self.image), self.instance_id])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                raise RuntimeError(f"Failed to start instance: {result.stderr}")
            self._instance_started = True
            logger.info("Singularity instance %s started (persistent=%s)",
                        self.instance_id, self._persistent)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Instance start timed out")

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """Spawn a bash process inside the Singularity instance."""
        # HPC underlay mode: per-command apptainer exec --underlay
        if getattr(self, '_underlay_flags', None):
            cmd = [self.executable, "exec"] + self._underlay_flags + [str(self.image)]
            if login:
                cmd.extend(["bash", "-l", "-c", cmd_string])
            else:
                cmd.extend(["bash", "-c", cmd_string])
            return _popen_bash(cmd, stdin_data)

        if not self._instance_started:
            raise RuntimeError("Singularity instance not started")

        cmd = [self.executable, "exec",
               f"instance://{self.instance_id}"]
        if login:
            cmd.extend(["bash", "-l", "-c", cmd_string])
        else:
            cmd.extend(["bash", "-c", cmd_string])

        return _popen_bash(cmd, stdin_data)

    def cleanup(self):
        """Stop the instance. If persistent, the overlay dir survives."""
        if getattr(self, '_underlay_flags', None):
            return  # No instance to stop in underlay mode

        if self._instance_started:
            try:
                subprocess.run(
                    [self.executable, "instance", "stop", self.instance_id],
                    capture_output=True, text=True, timeout=30,
                )
                logger.info("Singularity instance %s stopped", self.instance_id)
            except Exception as e:
                logger.warning("Failed to stop Singularity instance %s: %s", self.instance_id, e)
            self._instance_started = False

        if self._persistent and self._overlay_dir:
            snapshots = _load_snapshots()
            snapshots[self._task_id] = str(self._overlay_dir)
            _save_snapshots(snapshots)
