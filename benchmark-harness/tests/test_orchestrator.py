"""Tests for the orchestrator and the run / resume / list-runs CLI commands.

The fio runner is used in-process; subprocess.run (the ssh boundary) is
patched to return the captured sample-fio-output.json. The orchestrator
sees a happy path unless the test deliberately injects a failure.
"""
from __future__ import annotations

import subprocess
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from harness.cli import main as cli_main
from harness.orchestrator import find_latest_running_run, run_campaign
from harness.store import get_metrics_for_run, get_run, list_runs

FIXTURES = Path(__file__).parent / "fixtures"
CAMPAIGN = FIXTURES / "campaign.yaml"
SAMPLE_JSON = (FIXTURES / "sample-fio-output.json").read_text()


def _ok_subprocess(monkeypatch):
    """Patch subprocess.run so every ssh call succeeds with SAMPLE_JSON on stdout."""
    calls: list[list[str]] = []

    def fake_run(argv, capture_output, text, timeout=None, input=None):
        calls.append(argv)
        # stat probe (testfile presence check) → return NONE so fallocate runs
        last = argv[-1]
        if "stat -c %s" in last:
            return MagicMock(returncode=0, stdout="NONE\n", stderr="")
        # df probe → return plenty of free space
        if "df -B1" in last:
            return MagicMock(returncode=0, stdout="999999999999999\n", stderr="")
        # fio invocation → return the sample JSON
        if "fio --output-format=json+" in last:
            return MagicMock(returncode=0, stdout=SAMPLE_JSON, stderr="")
        return MagicMock(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_run_campaign_end_to_end(tmp_path, monkeypatch):
    _ok_subprocess(monkeypatch)
    run_id, status = run_campaign(CAMPAIGN, results_root=tmp_path)
    assert status == "ok"
    db = tmp_path / "harness.db"
    row = get_run(db, run_id)
    assert row is not None and row.status == "ok"
    metrics = get_metrics_for_run(db, run_id)
    assert any(m.metric == "bw_mbps" and m.value == 13503.0 for m in metrics)


def test_run_campaign_creates_raw_dir_and_json_files(tmp_path, monkeypatch):
    _ok_subprocess(monkeypatch)
    run_id, _ = run_campaign(CAMPAIGN, results_root=tmp_path)
    raw_dir = tmp_path / "raw" / run_id
    assert raw_dir.is_dir()
    # One scenario, one job → one captured JSON.
    json_files = list(raw_dir.rglob("*.json"))
    assert len(json_files) == 1
    # And an env capture file per node touched.
    env_files = list(raw_dir.glob("env-*.txt"))
    assert env_files


def test_resume_skips_already_completed_scenarios(tmp_path, monkeypatch):
    _ok_subprocess(monkeypatch)
    run_id, _ = run_campaign(CAMPAIGN, results_root=tmp_path)
    # Resume the same run_id: scenarios already 'ok' should be skipped, so
    # the orchestrator should perform NO ssh calls for jobs this time.
    captured: list[list[str]] = []

    def watcher(argv, capture_output, text, timeout=None, input=None):
        captured.append(argv)
        last = argv[-1]
        if "stat -c %s" in last:
            return MagicMock(returncode=0, stdout="999999999999\n", stderr="")
        if "rm -f" in last:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", watcher)
    _, status = run_campaign(CAMPAIGN, results_root=tmp_path, resume_run_id=run_id)
    assert status == "ok"
    # No fio invocations happened on resume — the cache-drop / fio calls would
    # appear here if scenarios were re-executed.
    assert not any("fio --output-format=json+" in c[-1] for c in captured)


def test_cleanup_after_campaign_calls_cleanup_once_per_target(tmp_path, monkeypatch):
    calls = _ok_subprocess(monkeypatch)
    run_campaign(CAMPAIGN, results_root=tmp_path)
    # Exactly one `rm -f .../testfile` should appear (single target in fixture).
    rm_calls = [c for c in calls if "rm -f" in c[-1] and "testfile" in c[-1]]
    assert len(rm_calls) == 1


def test_dry_run_does_not_write_db(tmp_path, monkeypatch):
    def boom(*a, **kw):
        pytest.fail("subprocess.run should not be called in dry-run mode")

    monkeypatch.setattr(subprocess, "run", boom)
    run_id, status = run_campaign(CAMPAIGN, results_root=tmp_path, dry_run=True)
    assert status == "ok"
    # No DB file should exist (we never called init_db).
    assert not (tmp_path / "harness.db").exists()


def test_prepare_failure_marks_scenario_failed_and_continues(tmp_path, monkeypatch):
    # Make every ssh call FAIL. prepare's mkdir is the first ssh call;
    # it will error and the orchestrator should record 'failed'.
    def always_fail(argv, capture_output, text, timeout=None, input=None):
        return MagicMock(returncode=1, stdout="", stderr="ssh: refused\n")

    monkeypatch.setattr(subprocess, "run", always_fail)
    run_id, status = run_campaign(CAMPAIGN, results_root=tmp_path)
    assert status == "failed"
    row = get_run(tmp_path / "harness.db", run_id)
    assert row.status == "failed"


def test_find_latest_running_run(tmp_path, monkeypatch):
    # Run one campaign; it should finish 'ok'. find_latest_running_run should return None
    # for that campaign because nothing is left 'running'.
    _ok_subprocess(monkeypatch)
    _, _ = run_campaign(CAMPAIGN, results_root=tmp_path)
    assert find_latest_running_run(tmp_path, "smoke-test") is None

    # Now mark one row as still 'running' and verify it's found.
    db = tmp_path / "harness.db"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE runs SET status='running' WHERE 1=1")
    conn.commit()
    conn.close()
    rid = find_latest_running_run(tmp_path, "smoke-test")
    assert rid is not None
    # Wrong campaign name → no match.
    assert find_latest_running_run(tmp_path, "nonexistent") is None


def test_cli_run_dry_run(tmp_path):
    # dry-run path: no subprocess, no DB writes, exit 0.
    result = CliRunner().invoke(
        cli_main,
        ["run", "--dry-run", "--results-root", str(tmp_path), str(CAMPAIGN)],
    )
    assert result.exit_code == 0, result.output
    assert "Starting run smoke-test-" in result.output


def test_cli_list_runs_empty(tmp_path):
    result = CliRunner().invoke(
        cli_main, ["list-runs", "--results-root", str(tmp_path)]
    )
    assert result.exit_code != 0  # no DB
    assert "no harness.db" in result.output


def test_cli_run_then_list_runs(tmp_path, monkeypatch):
    _ok_subprocess(monkeypatch)
    r1 = CliRunner().invoke(
        cli_main,
        ["run", "--no-preflight", "--results-root", str(tmp_path), str(CAMPAIGN)],
    )
    assert r1.exit_code == 0, r1.output
    r2 = CliRunner().invoke(
        cli_main, ["list-runs", "--results-root", str(tmp_path)]
    )
    assert r2.exit_code == 0
    assert "smoke-test-" in r2.output
    assert "ok" in r2.output


def test_cli_resume_without_campaign_or_runid_errors(tmp_path):
    result = CliRunner().invoke(
        cli_main, ["resume", "--results-root", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "campaign path or --run-id" in result.output


def test_cli_run_preflight_recognizes_existing_workspace(tmp_path, monkeypatch):
    """Regression: `harness run` preflight must use existing_file_path so a
    crashed-run-leftover testfile doesn't keep blocking future runs."""
    # df reports less than a 2 TiB testfile would need; stat reports a full-size
    # existing workspace file. The harness should preflight OK and proceed.
    # smoke campaign uses tiny.yaml (size=100M → required_gib=0), but we want to
    # simulate the real regression: large existing testfile, limited free space.
    # The "reusing" message appears whenever the existing-file path was passed
    # to preflight_target and a usable file was found.
    def fake_run(argv, capture_output, text, timeout=None, input=None):
        last = argv[-1]
        if "stat -c %s" in last and "testfile" in last:
            return MagicMock(returncode=0, stdout=str(2 * 1024**4) + "\n", stderr="")
        if "df -B1" in last:
            # 150 GiB free: enough for safety margin (100), but not for a 2 TiB
            # testfile if existing-file logic were absent.
            return MagicMock(returncode=0, stdout=str(150 * 1024**3) + "\n", stderr="")
        if "fio --output-format=json+" in last:
            return MagicMock(returncode=0, stdout=SAMPLE_JSON, stderr="")
        return MagicMock(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = CliRunner().invoke(
        cli_main,
        ["run", "--results-root", str(tmp_path), str(CAMPAIGN)],
    )
    # If the regression returns, the run would exit non-zero with "Preflight failed".
    assert result.exit_code == 0, result.output
    assert "reusing existing" in result.output
