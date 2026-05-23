"""SSH automation layer (Layer 1).

Wraps ``ssh -o BatchMode=yes`` for remote command execution. Sudo invocations
auto-add ``-t`` because the lab's spark01/spark02 sudoers config refuses
non-tty elevation (see .agent-notes/feedback_ssh-t-for-sudo.md).

Public surface:
  - run_remote(node, cmd)           : non-sudo command, returns RemoteResult
  - run_remote_sudo(node, cmd)      : sudo command, auto-tty
  - df_free_bytes(node, path)       : parse ``df -B1`` free bytes
  - preflight_target(...)           : per-target capacity preflight, returns a finding
"""
from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from harness.config import Node, StorageTarget

_DEFAULT_CONNECT_TIMEOUT = 10  # seconds; applied to every ssh call


class RemoteError(RuntimeError):
    """Non-zero exit (or timeout) from a remote command."""

    def __init__(self, result: RemoteResult):
        self.result = result
        super().__init__(
            f"remote command failed on {result.node} (exit {result.exit_code}): {result.command}\n"
            f"stderr: {result.stderr.strip()[:1000]}"
        )


@dataclass
class RemoteResult:
    node: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class PreflightFinding:
    target_name: str
    node_name: str
    path: str
    ok: bool
    message: str
    free_gib: int | None = None
    required_gib: int | None = None


def _ssh_argv(node: Node, *, tty: bool) -> list[str]:
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={_DEFAULT_CONNECT_TIMEOUT}",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if tty:
        argv.append("-t")
    argv.append(node.ssh)
    return argv


def _run(
    node_name: str,
    argv: list[str],
    command: str,
    *,
    timeout: int | None,
    check: bool,
    dry_run: bool,
    stdin: str | None = None,
) -> RemoteResult:
    if dry_run:
        return RemoteResult(
            node=node_name,
            command=command,
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            dry_run=True,
        )
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, input=stdin
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - t0
        if isinstance(exc.stdout, str):
            stdout = exc.stdout
        else:
            stdout = (exc.stdout or b"").decode(errors="replace")
        result = RemoteResult(
            node=node_name,
            command=command,
            exit_code=124,
            stdout=stdout,
            stderr=f"timed out after {timeout}s",
            duration_seconds=elapsed,
        )
        if check:
            raise RemoteError(result) from None
        return result
    elapsed = time.monotonic() - t0
    result = RemoteResult(
        node=node_name,
        command=command,
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_seconds=elapsed,
    )
    if check and not result.ok:
        raise RemoteError(result)
    return result


def run_remote(
    node: Node,
    command: str,
    *,
    timeout: int | None = None,
    check: bool = True,
    dry_run: bool = False,
    stdin: str | None = None,
) -> RemoteResult:
    """Run a non-sudo command on ``node``.

    Returns a RemoteResult. Raises RemoteError on non-zero exit if ``check=True``
    (default). Use ``check=False`` when the caller wants to inspect the result.

    ``stdin`` is piped to the remote command's stdin (useful for pushing
    rendered fio configs without scp).
    """
    argv = _ssh_argv(node, tty=False) + [command]
    return _run(
        node.name, argv, command,
        timeout=timeout, check=check, dry_run=dry_run, stdin=stdin,
    )


def run_remote_sudo(
    node: Node,
    command: str,
    *,
    timeout: int | None = None,
    check: bool = True,
    dry_run: bool = False,
) -> RemoteResult:
    """Run a sudo command on ``node``. Always allocates a TTY (``-t``).

    The lab's sudoers refuses non-tty elevation (bitten ~3× in one session);
    callers should always use this helper rather than building their own
    ``sudo`` invocation through :func:`run_remote`.
    """
    sudo_cmd = command if command.startswith("sudo ") else f"sudo -- {command}"
    argv = _ssh_argv(node, tty=True) + [sudo_cmd]
    return _run(node.name, argv, sudo_cmd, timeout=timeout, check=check, dry_run=dry_run)


def df_free_bytes(node: Node, path: str | Path, *, dry_run: bool = False) -> int:
    """Return free bytes on the filesystem containing ``path`` on ``node``.

    Uses ``df -B1 --output=avail``. Returns 0 in dry_run mode.
    """
    if dry_run:
        return 0
    result = run_remote(
        node,
        f"df -B1 --output=avail {shlex.quote(str(path))} | tail -n 1",
        timeout=30,
    )
    return int(result.stdout.strip())


def preflight_target(
    node: Node,
    target: StorageTarget,
    required_gib: int,
    safety_margin_gib: int,
    *,
    dry_run: bool = False,
    existing_file_path: str | None = None,
) -> PreflightFinding:
    """Verify the target path is reachable and has enough free space.

    Does NOT raise on failure: returns a finding so the caller can aggregate
    across all targets before deciding whether to proceed. Idempotently
    creates the target directory if it doesn't exist.

    If ``existing_file_path`` is provided and points to a file already at
    ``required_gib`` or larger, the free-space check uses 0 incremental
    bytes (the run will reuse the existing file). If the file exists but
    is smaller than ``required_gib``, preflight fails fast — the runner
    would refuse to extend it anyway (encodes the 007 gotcha).
    """
    path = str(target.path)
    if dry_run:
        return PreflightFinding(
            target_name=target.name,
            node_name=node.name,
            path=path,
            ok=True,
            message=f"dry-run (would check ≥{required_gib + safety_margin_gib} GiB free)",
            required_gib=required_gib,
        )

    mkdir_res = run_remote(node, f"mkdir -p {shlex.quote(path)}", check=False, timeout=30)
    if not mkdir_res.ok:
        return PreflightFinding(
            target_name=target.name,
            node_name=node.name,
            path=path,
            ok=False,
            message=f"cannot create path: {mkdir_res.stderr.strip()}",
            required_gib=required_gib,
        )

    try:
        free_bytes = df_free_bytes(node, path)
    except (RemoteError, ValueError) as exc:
        return PreflightFinding(
            target_name=target.name,
            node_name=node.name,
            path=path,
            ok=False,
            message=f"df failed: {exc}",
            required_gib=required_gib,
        )
    free_gib = free_bytes // (1024**3)

    # Account for an existing usable file the runner would reuse rather than
    # re-allocate. Three cases:
    #   - file absent  : need full required_gib NEW.
    #   - file >= req  : need 0 NEW (the file will be reused).
    #   - file < req   : runner will refuse; fail preflight now with a clear msg.
    existing_gib = _stat_file_size_gib(node, existing_file_path) if existing_file_path else None
    if existing_gib is None or existing_file_path is None:
        effective_required = required_gib
        reuse_note = ""
    elif existing_gib >= required_gib:
        effective_required = 0
        reuse_note = f", reusing existing {existing_gib} GiB workspace file"
    else:
        return PreflightFinding(
            target_name=target.name,
            node_name=node.name,
            path=path,
            ok=False,
            free_gib=free_gib,
            required_gib=required_gib,
            message=(
                f"existing file at {existing_file_path} is {existing_gib} GiB, "
                f"smaller than required {required_gib} GiB. The runner refuses "
                f"to extend it (007 silent-extend bug). Remove the file first."
            ),
        )

    needed = effective_required + safety_margin_gib
    if free_gib < needed:
        return PreflightFinding(
            target_name=target.name,
            node_name=node.name,
            path=path,
            ok=False,
            free_gib=free_gib,
            required_gib=required_gib,
            message=(
                f"free space {free_gib} GiB < required {effective_required} GiB + "
                f"safety_margin {safety_margin_gib} GiB = {needed} GiB"
            ),
        )
    return PreflightFinding(
        target_name=target.name,
        node_name=node.name,
        path=path,
        ok=True,
        free_gib=free_gib,
        required_gib=required_gib,
        message=f"OK ({free_gib} GiB free; needs {needed} GiB{reuse_note})",
    )


def _stat_file_size_gib(node: Node, path: str) -> int | None:
    """Return file size in GiB if it exists on ``node``, else None."""
    r = run_remote(
        node,
        f"stat -c %s {shlex.quote(path)} 2>/dev/null || echo NONE",
        check=False,
        timeout=15,
    )
    out = r.stdout.strip()
    if not out or out == "NONE":
        return None
    try:
        return int(out) // (1024**3)
    except ValueError:
        return None
