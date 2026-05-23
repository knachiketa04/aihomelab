"""Unit tests for the SSH automation layer (mocked subprocess)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.config import Node, StorageTarget
from harness.ssh import (
    RemoteError,
    df_free_bytes,
    preflight_target,
    run_remote,
    run_remote_sudo,
)

NODE = Node(name="spark01", ssh="sparks@192.168.20.21")
TARGET = StorageTarget(
    name="spark01-nvme",
    node="spark01",
    path=Path("/home/sparks/bench/"),
    kind="local-nvme",
    capacity_gib=3700,
)


def _fake_subprocess(monkeypatch, returncode=0, stdout="", stderr=""):
    """Patch subprocess.run to return a deterministic CompletedProcess and capture argv."""
    calls: list[list[str]] = []

    def fake_run(argv, capture_output, text, timeout=None):
        calls.append(argv)
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = stderr
        return proc

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_run_remote_builds_correct_argv(monkeypatch):
    calls = _fake_subprocess(monkeypatch, stdout="hello\n")
    result = run_remote(NODE, "echo hello")
    assert result.ok
    assert result.stdout == "hello\n"
    argv = calls[0]
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "-t" not in argv  # non-sudo: no tty
    assert NODE.ssh in argv
    assert argv[-1] == "echo hello"


def test_run_remote_sudo_adds_tty_and_prefix(monkeypatch):
    calls = _fake_subprocess(monkeypatch)
    run_remote_sudo(NODE, "sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'")
    argv = calls[0]
    assert "-t" in argv  # sudo always allocates tty
    assert argv[-1].startswith("sudo -- ")


def test_run_remote_sudo_does_not_double_prefix(monkeypatch):
    calls = _fake_subprocess(monkeypatch)
    run_remote_sudo(NODE, "sudo -n whoami")
    argv = calls[0]
    # Already starts with 'sudo ', should not be re-wrapped.
    assert argv[-1] == "sudo -n whoami"


def test_run_remote_raises_on_nonzero_when_check_true(monkeypatch):
    _fake_subprocess(monkeypatch, returncode=2, stderr="boom\n")
    with pytest.raises(RemoteError) as exc:
        run_remote(NODE, "false")
    assert "exit 2" in str(exc.value)
    assert exc.value.result.exit_code == 2


def test_run_remote_returns_result_when_check_false(monkeypatch):
    _fake_subprocess(monkeypatch, returncode=2, stderr="boom\n")
    result = run_remote(NODE, "false", check=False)
    assert not result.ok
    assert result.exit_code == 2


def test_run_remote_timeout_returns_124_when_check_false(monkeypatch):
    def fake_run(argv, capture_output, text, timeout=None):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout, output=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_remote(NODE, "sleep 99", timeout=1, check=False)
    assert result.exit_code == 124
    assert "timed out" in result.stderr


def test_dry_run_skips_subprocess(monkeypatch):
    def boom(*a, **kw):
        pytest.fail("subprocess.run should not be called in dry-run mode")

    monkeypatch.setattr(subprocess, "run", boom)
    result = run_remote(NODE, "echo nope", dry_run=True)
    assert result.ok and result.dry_run


def test_df_free_bytes_parses_output(monkeypatch):
    _fake_subprocess(monkeypatch, stdout="2199023255552\n")  # 2 TiB
    assert df_free_bytes(NODE, "/home/sparks/bench/") == 2199023255552


def test_preflight_passes_when_free_exceeds_required(monkeypatch):
    # mkdir succeeds, df returns 3 TiB free.
    seq = iter([("", 0), ("3298534883328\n", 0)])  # 3 TiB

    def fake_run(argv, capture_output, text, timeout=None):
        stdout, code = next(seq)
        return MagicMock(returncode=code, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    finding = preflight_target(NODE, TARGET, required_gib=2048, safety_margin_gib=100)
    assert finding.ok
    assert finding.free_gib == 3072
    assert "OK" in finding.message


def test_preflight_fails_when_free_below_required_plus_margin(monkeypatch):
    seq = iter([("", 0), ("2199023255552\n", 0)])  # 2 TiB

    def fake_run(argv, capture_output, text, timeout=None):
        stdout, code = next(seq)
        return MagicMock(returncode=code, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    finding = preflight_target(NODE, TARGET, required_gib=2048, safety_margin_gib=100)
    assert not finding.ok
    assert "free space" in finding.message
    assert finding.free_gib == 2048


def test_preflight_fails_when_mkdir_fails(monkeypatch):
    def fake_run(argv, capture_output, text, timeout=None):
        return MagicMock(returncode=1, stdout="", stderr="permission denied\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    finding = preflight_target(NODE, TARGET, required_gib=2048, safety_margin_gib=100)
    assert not finding.ok
    assert "cannot create path" in finding.message


def test_preflight_dry_run_does_not_ssh(monkeypatch):
    def boom(*a, **kw):
        pytest.fail("subprocess.run should not be called in dry-run mode")

    monkeypatch.setattr(subprocess, "run", boom)
    finding = preflight_target(NODE, TARGET, 2048, 100, dry_run=True)
    assert finding.ok and "dry-run" in finding.message
