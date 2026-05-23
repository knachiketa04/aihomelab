"""Unit tests for harness/parsers/fio.py."""
from __future__ import annotations

from pathlib import Path

from harness.parsers.fio import MetricRecord, parse_fio_json

FIXTURE = Path(__file__).parent / "fixtures" / "sample-fio-output.json"


def _as_dict(records: list[MetricRecord]) -> dict[tuple[str, str, str], float]:
    return {(r.job_name, r.op, r.metric): r.value for r in records}


def test_parse_sample_extracts_write_metrics():
    records = parse_fio_json(FIXTURE)
    m = _as_dict(records)
    # bw: 13503000000 / 1e6 = 13503.0 MB/s
    assert m[("seqwrite-1t", "write", "bw_mbps")] == 13503.0
    assert m[("seqwrite-1t", "write", "iops")] == 12878.05
    # 1200000 ns / 1000 = 1200 µs
    assert m[("seqwrite-1t", "write", "lat_p50_us")] == 1200.0
    assert m[("seqwrite-1t", "write", "lat_p99_us")] == 4500.0
    assert m[("seqwrite-1t", "write", "lat_p999_us")] == 9100.0


def test_parse_sample_skips_op_with_no_io():
    records = parse_fio_json(FIXTURE)
    # The fixture's `read` block is all zeros — should be elided, not emitted as 0s.
    assert not any(r.op == "read" for r in records)


def test_parse_accepts_dict():
    records = parse_fio_json({"jobs": []})
    assert records == []


def test_parse_handles_missing_percentile_key():
    data = {
        "jobs": [
            {
                "jobname": "x",
                "write": {
                    "bw_bytes": 1000000,
                    "iops": 100.0,
                    "clat_ns": {"percentile": {"99.000000": 5000}},
                },
            }
        ]
    }
    m = _as_dict(parse_fio_json(data))
    assert m[("x", "write", "lat_p99_us")] == 5.0
    assert ("x", "write", "lat_p50_us") not in m  # not in JSON, not synthesized


def test_parse_str_path_falls_back_to_json_literal():
    # A JSON-string source (not a path) should still parse.
    literal = '{"jobs":[{"jobname":"j","write":{"bw_bytes":1000000,"iops":42}}]}'
    m = _as_dict(parse_fio_json(literal))
    assert m[("j", "write", "bw_mbps")] == 1.0
    assert m[("j", "write", "iops")] == 42.0
