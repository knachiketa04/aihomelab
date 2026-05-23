"""SQLite store (Layer 3) for harness runs, scenarios, metrics, and env snapshots.

Schema (intentionally narrow; widen when a real need lands, not before):

  runs(run_id, campaign_name, campaign_path, started_at, ended_at, status, raw_dir, notes)
  scenarios(run_id, scenario_name, repeat_idx, kind, target_name, target_node,
            target_path, started_at, ended_at, status, error)
  metrics(run_id, scenario_name, repeat_idx, job_name, op, metric, value)
  env_snapshots(run_id, node, key, value)

All timestamps are ISO8601 UTC. The DB is intended for local analysis;
concurrent writers are not supported (one orchestrator per DB at a time).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from harness.parsers.fio import MetricRecord

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    campaign_name TEXT NOT NULL,
    campaign_path TEXT,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    status        TEXT NOT NULL,
    raw_dir       TEXT NOT NULL,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS scenarios (
    run_id        TEXT NOT NULL,
    scenario_name TEXT NOT NULL,
    repeat_idx    INTEGER NOT NULL DEFAULT 0,
    kind          TEXT NOT NULL,
    target_name   TEXT NOT NULL,
    target_node   TEXT NOT NULL,
    target_path   TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    status        TEXT NOT NULL,
    error         TEXT,
    PRIMARY KEY (run_id, scenario_name, repeat_idx),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS metrics (
    run_id        TEXT NOT NULL,
    scenario_name TEXT NOT NULL,
    repeat_idx    INTEGER NOT NULL DEFAULT 0,
    job_name      TEXT NOT NULL,
    op            TEXT NOT NULL,
    metric        TEXT NOT NULL,
    value         REAL NOT NULL,
    PRIMARY KEY (run_id, scenario_name, repeat_idx, job_name, op, metric),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS env_snapshots (
    run_id TEXT NOT NULL,
    node   TEXT NOT NULL,
    key    TEXT NOT NULL,
    value  TEXT,
    PRIMARY KEY (run_id, node, key),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
"""


@dataclass
class RunRow:
    run_id: str
    campaign_name: str
    started_at: str
    ended_at: str | None
    status: str
    raw_dir: str


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def init_db(db_path: Path) -> None:
    """Create tables if they don't exist. Idempotent."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---- Runs --------------------------------------------------------------------


def start_run(
    db_path: Path,
    *,
    run_id: str,
    campaign_name: str,
    campaign_path: Path | None,
    raw_dir: Path,
    notes: str | None = None,
) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO runs(run_id, campaign_name, campaign_path, started_at, status, raw_dir, notes)"
            " VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (run_id, campaign_name, str(campaign_path) if campaign_path else None,
             _now(), str(raw_dir), notes),
        )


def finish_run(db_path: Path, run_id: str, status: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE runs SET ended_at = ?, status = ? WHERE run_id = ?",
            (_now(), status, run_id),
        )


def list_runs(db_path: Path) -> list[RunRow]:
    with _connect(db_path) as conn:
        # rowid DESC as tiebreaker — started_at is second-resolution, so two
        # runs created in the same second would otherwise have undefined order.
        rows = conn.execute(
            "SELECT run_id, campaign_name, started_at, ended_at, status, raw_dir"
            " FROM runs ORDER BY started_at DESC, rowid DESC"
        ).fetchall()
    return [RunRow(*row) for row in rows]


def get_run(db_path: Path, run_id: str) -> RunRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT run_id, campaign_name, started_at, ended_at, status, raw_dir"
            " FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return RunRow(*row) if row else None


# ---- Scenarios ---------------------------------------------------------------


def start_scenario(
    db_path: Path,
    *,
    run_id: str,
    scenario_name: str,
    repeat_idx: int,
    kind: str,
    target_name: str,
    target_node: str,
    target_path: str,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO scenarios(run_id, scenario_name, repeat_idx, kind,"
            " target_name, target_node, target_path, started_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')",
            (run_id, scenario_name, repeat_idx, kind,
             target_name, target_node, target_path, _now()),
        )


def finish_scenario(
    db_path: Path,
    run_id: str,
    scenario_name: str,
    repeat_idx: int,
    status: str,
    error: str | None = None,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE scenarios SET ended_at = ?, status = ?, error = ?"
            " WHERE run_id = ? AND scenario_name = ? AND repeat_idx = ?",
            (_now(), status, error, run_id, scenario_name, repeat_idx),
        )


def scenario_completed(
    db_path: Path, run_id: str, scenario_name: str, repeat_idx: int
) -> bool:
    """True if this (run, scenario, repeat) has ended with status 'ok' — used for resume."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM scenarios"
            " WHERE run_id = ? AND scenario_name = ? AND repeat_idx = ?",
            (run_id, scenario_name, repeat_idx),
        ).fetchone()
    return bool(row) and row[0] == "ok"


# ---- Metrics -----------------------------------------------------------------


def record_metrics(
    db_path: Path,
    *,
    run_id: str,
    scenario_name: str,
    repeat_idx: int,
    records: Iterable[MetricRecord],
) -> int:
    """Insert metric rows. Returns the count written. Re-inserts overwrite."""
    rows = [
        (run_id, scenario_name, repeat_idx, r.job_name, r.op, r.metric, r.value)
        for r in records
    ]
    if not rows:
        return 0
    with _connect(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO metrics"
            "(run_id, scenario_name, repeat_idx, job_name, op, metric, value)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def get_metrics_for_run(db_path: Path, run_id: str) -> list[MetricRecord]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT job_name, op, metric, value FROM metrics WHERE run_id = ?"
            " ORDER BY scenario_name, repeat_idx, job_name, op, metric",
            (run_id,),
        ).fetchall()
    return [MetricRecord(*r) for r in rows]


# ---- Env snapshots -----------------------------------------------------------


def record_env(db_path: Path, run_id: str, node: str, kv: dict[str, str]) -> None:
    with _connect(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO env_snapshots(run_id, node, key, value)"
            " VALUES (?, ?, ?, ?)",
            [(run_id, node, k, v) for k, v in kv.items()],
        )
