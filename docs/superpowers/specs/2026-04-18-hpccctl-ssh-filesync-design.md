# HPCCCTL SSH File Sync Design

## Context

hpccctl backend runs commands via mTLS relay to an Apptainer container on HPC. It's the only remote backend without FileSyncManager integration — SSH, Modal, and Daytona all have file sync. Currently hpccctl relies on external rclone (sync-watch.sh) for file sync, which is fragile and not integrated.

Goal: add file sync by composing with SSHEnvironment, reusing its tar-over-SSH transport. Zero changes to existing SSH/file_sync code.

## Architecture

```
HpccctlEnvironment
├── Command execution: hpccctl exec --relay (mTLS → UDS → container)
└── File sync (optional): internal SSHEnvironment
    ├── _before_execute() → SSHEnvironment._sync_manager.sync()
    └── cleanup() → SSHEnvironment._sync_manager.sync_back()
```

hpccctl handles command execution via mTLS relay. SSH handles file transport (tar-over-SSH bulk upload/download, SCP single-file upload, SSH batch delete). Same FileSyncManager that SSH uses — mtime+size change detection, deletion tracking, rate limiting, transactional state.

## Changes

### 1. `tools/environments/hpccctl.py`

**Constructor** — add optional SSH params:

```python
def __init__(self, addr="127.0.0.1:18923", relay=True,
             cwd="~", timeout=180, config_path="",
             ssh_host="", ssh_user="", ssh_port=22, ssh_key_path=""):
```

If `ssh_host` and `ssh_user` are provided:
- Create `self._ssh_env = SSHEnvironment(host, user, port, key_path, ...)`
- SSH env handles its own FileSyncManager init, initial sync, and remote dir setup
- Store `self._sync_manager = self._ssh_env._sync_manager`

If SSH params missing:
- `self._ssh_env = None`
- No file sync (backward compatible — current behavior)

**`_before_execute()`** — sync before each command:

```python
def _before_execute(self):
    if self._sync_manager:
        self._sync_manager.sync()
```

**`cleanup()`** — sync back on teardown:

```python
def cleanup(self):
    if self._sync_manager:
        self._sync_manager.sync_back()
    if self._ssh_env:
        self._ssh_env.cleanup()
    # existing session cleanup
```

### 2. `tools/terminal_tool.py`

**Config parsing** (~line 660) — add SSH env vars for hpccctl:

```python
# HPCCCTL-SSH file sync config
"hpccctl_ssh_host": os.getenv("TERMINAL_HPCCCTL_SSH_HOST", ""),
"hpccctl_ssh_user": os.getenv("TERMINAL_HPCCCTL_SSH_USER", ""),
"hpccctl_ssh_port": _parse_env_var("TERMINAL_HPCCCTL_SSH_PORT", "22"),
"hpccctl_ssh_key": os.getenv("TERMINAL_HPCCCTL_SSH_KEY", ""),
```

**Environment creation** (~line 815) — pass SSH params:

```python
return HpccctlEnvironment(
    addr=..., relay=..., cwd=..., timeout=..., config_path=...,
    ssh_host=cc.get("hpccctl_ssh_host", ""),
    ssh_user=cc.get("hpccctl_ssh_user", ""),
    ssh_port=int(cc.get("hpccctl_ssh_port", 22)),
    ssh_key_path=cc.get("hpccctl_ssh_key", ""),
)
```

**Requirements check** (~line 1644) — warn if SSH params missing:

```python
elif env_type == "hpccctl":
    executable = shutil.which("hpccctl")
    if not executable:
        logger.error("hpccctl backend selected but 'hpccctl' not found in PATH")
        return False
    if not os.getenv("TERMINAL_HPCCCTL_SSH_HOST"):
        logger.warning("hpccctl: no SSH params — file sync disabled")
    return True
```

## Not Modified

- `tools/environments/ssh.py` — zero changes
- `tools/environments/file_sync.py` — zero changes
- `tools/environments/base.py` — zero changes

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `TERMINAL_HPCCCTL_SSH_HOST` | `""` | SSH host for file sync |
| `TERMINAL_HPCCCTL_SSH_USER` | `""` | SSH user for file sync |
| `TERMINAL_HPCCCTL_SSH_PORT` | `22` | SSH port |
| `TERMINAL_HPCCCTL_SSH_KEY` | `""` | SSH key path |

All optional. Missing → file sync disabled, hpccctl works as before.

## Verification

1. **With SSH params**: Set all 4 env vars + hpccctl env vars. Run a command. Verify file sync pushes credentials/skills before execution. Verify sync_back on cleanup.
2. **Without SSH params**: Only hpccctl env vars. Run a command. Verify no errors, no file sync, same behavior as before.
3. **Partial SSH params**: Only HOST set, no USER. Verify warning logged, file sync disabled, no crash.
