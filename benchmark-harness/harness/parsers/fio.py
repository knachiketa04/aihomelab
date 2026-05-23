"""Parse fio ``--output-format=json+`` output into normalized metric records.

Extraction logic mirrors the jq helpers in the existing 007 reproduce kit
(artifacts/data-prep/spark-nvme-fio-baseline/reproduce/analyze.sh):

  bw_mbps     = .jobs[0].<op>.bw_bytes / 1e6        (decimal MB/s)
  iops        = .jobs[0].<op>.iops
  lat_pNN_us  = .jobs[0].<op>.clat_ns.percentile."NN.000000" / 1000
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Fio json+ ops that may carry measurements. 'trim' included for completeness
# even though no current scenario uses it.
_OPS = ("read", "write", "trim")

# Percentiles we surface as named metrics. Keys are the JSON percentile keys.
_PERCENTILES = {
    "50.000000": "lat_p50_us",
    "99.000000": "lat_p99_us",
    "99.900000": "lat_p999_us",
}


@dataclass(frozen=True)
class MetricRecord:
    """One normalized measurement extracted from a fio JSON output."""

    job_name: str
    op: str  # 'read' | 'write' | 'trim'
    metric: str  # 'bw_mbps' | 'iops' | 'lat_pNN_us'
    value: float


def parse_fio_json(source: Path | str | dict[str, Any]) -> list[MetricRecord]:
    """Extract MetricRecords from a fio JSON output.

    ``source`` may be a path to a JSON file, a JSON-encoded string, or an
    already-decoded dict. Returns records only for ops that actually carry
    nonzero IO (skips ops fio reports as 0/0/0 because the workload didn't
    exercise them, e.g. ``write`` in a read-only job).
    """
    data = _load(source)
    jobs = data.get("jobs") or []
    if not jobs:
        return []
    # group_reporting=1 (our default) collapses numjobs into one summary.
    job = jobs[0]
    job_name = job.get("jobname") or "<unnamed>"

    records: list[MetricRecord] = []
    for op in _OPS:
        op_block = job.get(op) or {}
        bw_bytes = op_block.get("bw_bytes") or 0
        iops = op_block.get("iops") or 0
        if bw_bytes == 0 and iops == 0:
            continue  # op not exercised in this job
        records.append(MetricRecord(job_name, op, "bw_mbps", bw_bytes / 1e6))
        records.append(MetricRecord(job_name, op, "iops", float(iops)))

        clat = (op_block.get("clat_ns") or {}).get("percentile") or {}
        for key, metric_name in _PERCENTILES.items():
            ns = clat.get(key)
            if ns is None:
                continue
            records.append(MetricRecord(job_name, op, metric_name, ns / 1000.0))

    return records


def _load(source: Path | str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, dict):
        return source
    if isinstance(source, Path):
        return json.loads(source.read_text())
    # str: try as a path first if it looks like one and exists, else parse as JSON.
    p = Path(source)
    if p.exists() and p.is_file():
        return json.loads(p.read_text())
    return json.loads(source)
