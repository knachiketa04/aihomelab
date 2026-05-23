"""Tests for the markdown report renderer."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from harness.cli import main as cli_main
from harness.orchestrator import run_campaign
from harness.parsers.fio import MetricRecord
from harness.report import render_report
from harness.store import (
    init_db,
    record_metrics,
    start_run,
    start_scenario,
    finish_scenario,
    finish_run,
)

FIXTURES = Path(__file__).parent / "fixtures"
CAMPAIGN = FIXTURES / "campaign.yaml"
SAMPLE_JSON = (FIXTURES / "sample-fio-output.json").read_text()


def _seed_db_minimal(db: Path) -> str:
    """Stamp a fake run with no associated campaign file on disk."""
    init_db(db)
    run_id = "fake-run-001"
    start_run(
        db,
        run_id=run_id,
        campaign_name="fake-campaign",
        campaign_path=Path("/nonexistent/campaign.yaml"),
        raw_dir=Path("/tmp/nope"),
    )
    start_scenario(
        db,
        run_id=run_id,
        scenario_name="seqwrite-test",
        repeat_idx=0,
        kind="fio",
        target_name="t",
        target_node="n",
        target_path="/p",
    )
    record_metrics(
        db, run_id=run_id, scenario_name="seqwrite-test", repeat_idx=0,
        records=[
            MetricRecord("seqwrite-1t", "write", "bw_mbps", 13503.0),
            MetricRecord("seqwrite-1t", "write", "iops", 12878.05),
            MetricRecord("seqwrite-1t", "write", "lat_p99_us", 4500.0),
        ],
    )
    finish_scenario(db, run_id, "seqwrite-test", 0, "ok")
    finish_run(db, run_id, "ok")
    return run_id


def test_render_report_basic_table(tmp_path):
    db = tmp_path / "harness.db"
    run_id = _seed_db_minimal(db)
    md = render_report(db, run_id)
    assert f"# Run {run_id}" in md
    assert "**Campaign:** fake-campaign" in md
    assert "seqwrite-test" in md
    assert "seqwrite-1t" in md
    assert "13503 MB/s" in md
    assert "4500 µs" in md
    # No campaign on disk → no "% of spec".
    assert "% of spec" in md  # column header still present
    assert "—" in md  # value displayed as em-dash


def test_render_report_unknown_run_raises(tmp_path):
    db = tmp_path / "harness.db"
    init_db(db)
    with pytest.raises(ValueError, match="no run"):
        render_report(db, "ghost")


def test_render_report_uses_vendor_spec_when_campaign_available(tmp_path, monkeypatch):
    # Run a real campaign end-to-end so the report can re-load it.
    def fake_run(argv, capture_output, text, timeout=None, input=None):
        last = argv[-1]
        if "stat -c %s" in last:
            return MagicMock(returncode=0, stdout="NONE\n", stderr="")
        if "fio --output-format=json+" in last:
            return MagicMock(returncode=0, stdout=SAMPLE_JSON, stderr="")
        return MagicMock(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Build a campaign-with-vendor-spec under tmp_path so campaign_path is stable.
    scen_dir = tmp_path / "scenarios" / "fio"
    scen_dir.mkdir(parents=True)
    (scen_dir / "tiny.yaml").write_text(
        "kind: fio\nname: seqwrite-only\n"
        "global: { runtime: 5, ramp_time: 1, size: 100M }\n"
        "drop_caches_between_jobs: false\n"
        "jobs:\n"
        "  - { name: seqwrite-1t, rw: write, bs: 1M, numjobs: 1, iodepth: 32 }\n"
    )
    camp = tmp_path / "campaign.yaml"
    camp.write_text(
        "name: vendor-spec-test\n"
        "cluster: { nodes: [{ name: spark01, ssh: sparks@x }] }\n"
        "targets:\n  - { name: t, node: spark01, path: /tmp/x, kind: local-nvme, capacity_gib: 100 }\n"
        "vendor_spec:\n"
        "  label: Sample Drive\n"
        "  seqwrite_mbps: 14000\n"
        "scenarios_dir: scenarios\n"
        "runs:\n  - { scenario: fio/tiny.yaml, target: t }\n"
    )
    run_id, _ = run_campaign(camp, results_root=tmp_path)
    md = render_report(tmp_path / "harness.db", run_id)
    # 13503 / 14000 ≈ 96%.
    assert "96%" in md
    assert "Sample Drive" in md


def test_render_report_with_baseline_diff(tmp_path):
    db = tmp_path / "harness.db"
    # Run 1
    init_db(db)
    start_run(db, run_id="r1", campaign_name="c", campaign_path=None, raw_dir=Path("/x"))
    start_scenario(db, run_id="r1", scenario_name="s", repeat_idx=0, kind="fio",
                   target_name="t", target_node="n", target_path="/p")
    record_metrics(db, run_id="r1", scenario_name="s", repeat_idx=0,
                   records=[MetricRecord("j", "write", "bw_mbps", 10000.0)])
    finish_scenario(db, "r1", "s", 0, "ok")
    finish_run(db, "r1", "ok")
    # Run 2 (10% higher)
    start_run(db, run_id="r2", campaign_name="c", campaign_path=None, raw_dir=Path("/x"))
    start_scenario(db, run_id="r2", scenario_name="s", repeat_idx=0, kind="fio",
                   target_name="t", target_node="n", target_path="/p")
    record_metrics(db, run_id="r2", scenario_name="s", repeat_idx=0,
                   records=[MetricRecord("j", "write", "bw_mbps", 11000.0)])
    finish_scenario(db, "r2", "s", 0, "ok")
    finish_run(db, "r2", "ok")

    md = render_report(db, "r2", baseline_run_id="r1")
    assert "**Baseline:** r1" in md
    assert "vs baseline" in md
    assert "+10.0%" in md


def test_cli_report_writes_file(tmp_path):
    db = tmp_path / "harness.db"
    run_id = _seed_db_minimal(db)
    out = tmp_path / "out.md"
    result = CliRunner().invoke(
        cli_main, ["report", "--results-root", str(tmp_path), "-o", str(out), run_id]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    text = out.read_text()
    assert f"# Run {run_id}" in text


def test_cli_report_unknown_run_errors(tmp_path):
    db = tmp_path / "harness.db"
    init_db(db)
    result = CliRunner().invoke(
        cli_main, ["report", "--results-root", str(tmp_path), "ghost"]
    )
    assert result.exit_code != 0
    assert "no run" in result.output


def test_cli_report_default_output_path(tmp_path):
    db = tmp_path / "harness.db"
    run_id = _seed_db_minimal(db)
    result = CliRunner().invoke(
        cli_main, ["report", "--results-root", str(tmp_path), run_id]
    )
    assert result.exit_code == 0, result.output
    default_path = tmp_path / "reports" / f"{run_id}.md"
    assert default_path.exists()
