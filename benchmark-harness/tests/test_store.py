"""Unit tests for harness/store.py SQLite layer."""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.parsers.fio import MetricRecord
from harness.store import (
    finish_run,
    finish_scenario,
    get_metrics_for_run,
    get_run,
    init_db,
    list_runs,
    record_env,
    record_metrics,
    scenario_completed,
    start_run,
    start_scenario,
)


@pytest.fixture
def db(tmp_path) -> Path:
    p = tmp_path / "harness.db"
    init_db(p)
    return p


def test_init_db_is_idempotent(db):
    init_db(db)  # second call should not raise


def _start(db, run_id="r1"):
    start_run(
        db,
        run_id=run_id,
        campaign_name="campaign-x",
        campaign_path=Path("/tmp/c.yaml"),
        raw_dir=Path("/tmp/raw"),
    )


def test_run_lifecycle(db):
    _start(db)
    row = get_run(db, "r1")
    assert row is not None
    assert row.status == "running"
    assert row.ended_at is None

    finish_run(db, "r1", "ok")
    row2 = get_run(db, "r1")
    assert row2.status == "ok"
    assert row2.ended_at is not None


def test_list_runs_orders_by_started_at_desc(db):
    _start(db, "r1")
    _start(db, "r2")
    rows = list_runs(db)
    ids = [r.run_id for r in rows]
    # r2 was started later → first in list (insertion order is monotone here).
    assert ids[0] == "r2"


def test_scenario_lifecycle_and_completed_flag(db):
    _start(db)
    start_scenario(
        db,
        run_id="r1",
        scenario_name="tiny",
        repeat_idx=0,
        kind="fio",
        target_name="t",
        target_node="n",
        target_path="/p",
    )
    assert not scenario_completed(db, "r1", "tiny", 0)
    finish_scenario(db, "r1", "tiny", 0, status="ok")
    assert scenario_completed(db, "r1", "tiny", 0)


def test_record_metrics_and_query(db):
    _start(db)
    records = [
        MetricRecord("seqwrite-1t", "write", "bw_mbps", 13503.0),
        MetricRecord("seqwrite-1t", "write", "iops", 12878.05),
        MetricRecord("seqwrite-1t", "write", "lat_p99_us", 4500.0),
    ]
    n = record_metrics(db, run_id="r1", scenario_name="tiny", repeat_idx=0, records=records)
    assert n == 3
    fetched = get_metrics_for_run(db, "r1")
    assert len(fetched) == 3
    by_metric = {m.metric: m.value for m in fetched}
    assert by_metric["bw_mbps"] == 13503.0
    assert by_metric["iops"] == 12878.05


def test_record_metrics_replaces_on_repeat(db):
    _start(db)
    record_metrics(
        db, run_id="r1", scenario_name="tiny", repeat_idx=0,
        records=[MetricRecord("j", "write", "iops", 100.0)],
    )
    record_metrics(
        db, run_id="r1", scenario_name="tiny", repeat_idx=0,
        records=[MetricRecord("j", "write", "iops", 200.0)],
    )
    fetched = get_metrics_for_run(db, "r1")
    assert len(fetched) == 1
    assert fetched[0].value == 200.0


def test_env_snapshot_upsert(db):
    _start(db)
    record_env(db, "r1", "spark01", {"fio": "fio-3.36", "kernel": "6.5.0"})
    record_env(db, "r1", "spark01", {"fio": "fio-3.37"})  # overwrite
    # No public getter for env yet; just verify no error and rough shape via raw query.
    import sqlite3
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT key, value FROM env_snapshots WHERE run_id = ? AND node = ? ORDER BY key",
        ("r1", "spark01"),
    ).fetchall()
    conn.close()
    assert dict(rows) == {"fio": "fio-3.37", "kernel": "6.5.0"}
