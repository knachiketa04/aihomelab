"""Markdown report rendering (Layer 4).

Builds a per-run report from the SQLite store:

  - One section per scenario, showing primary metric + p99 latency + % of
    vendor spec. Seq jobs report bw_mbps, rand jobs report iops (mirrors
    007's analyze.sh Measured table).
  - Optional baseline run for cross-run diffs (% change vs baseline).
  - Falls back gracefully if the campaign YAML moved/deleted: tables still
    render, but seq-vs-rand classification and "% of spec" are skipped.

The renderer queries the store; the CLI ``harness report <run_id>`` is the
operator-facing entry point.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import jinja2
from pydantic import ValidationError

from harness.config import FioJob, FioScenarioSpec, VendorSpec, load_campaign
from harness.store import (
    ScenarioRow,
    StoredMetric,
    get_all_metrics,
    get_run,
    get_run_campaign_path,
    get_scenarios_for_run,
)


@dataclass
class ReportRow:
    """One row in the per-scenario Measured table."""

    job_name: str
    op: str
    primary_label: str  # "bw_mbps" | "iops"
    primary_value: float | None
    p99_us: float | None
    pct_of_spec: float | None  # 0..100+, or None when unknown
    delta_vs_baseline_pct: float | None  # signed % change vs baseline


@dataclass
class ScenarioSection:
    scenario_name: str
    repeat_idx: int
    target_name: str
    status: str
    rows: list[ReportRow]


def render_report(
    db_path: Path,
    run_id: str,
    *,
    baseline_run_id: str | None = None,
) -> str:
    """Render the run as markdown. Raises ValueError if the run is unknown."""
    run = get_run(db_path, run_id)
    if run is None:
        raise ValueError(f"no run {run_id!r} in {db_path}")

    scenarios = get_scenarios_for_run(db_path, run_id)
    metrics = get_all_metrics(db_path, run_id)
    metrics_by_key = _group_metrics(metrics)

    base_metrics_by_key: dict | None = None
    base_run = None
    if baseline_run_id:
        base_run = get_run(db_path, baseline_run_id)
        if base_run is None:
            raise ValueError(f"no baseline run {baseline_run_id!r} in {db_path}")
        base_metrics_by_key = _group_metrics(get_all_metrics(db_path, baseline_run_id))

    job_lookup, vendor_spec = _load_job_metadata(db_path, run_id)

    sections = [
        _build_section(s, metrics_by_key, base_metrics_by_key, job_lookup, vendor_spec)
        for s in scenarios
    ]

    return _TEMPLATE.render(
        run=run,
        baseline=base_run,
        sections=sections,
        vendor_spec=vendor_spec,
    )


# ---- internals --------------------------------------------------------------


_MetricKey = tuple[str, int, str, str, str]  # scenario, repeat, job, op, metric


def _group_metrics(metrics: Iterable[StoredMetric]) -> dict[_MetricKey, float]:
    return {
        (m.scenario_name, m.repeat_idx, m.job_name, m.op, m.metric): m.value
        for m in metrics
    }


def _load_job_metadata(
    db_path: Path, run_id: str
) -> tuple[dict[tuple[str, str], FioJob], VendorSpec | None]:
    """Try to load the campaign YAML referenced by this run; return:
    - {(scenario_name, job_name): FioJob}
    - VendorSpec (or None)

    Returns ({}, None) silently if the campaign file is missing or unreadable —
    the report falls back to raw metrics without "% of spec".
    """
    cp = get_run_campaign_path(db_path, run_id)
    if not cp:
        return {}, None
    cp_path = Path(cp)
    if not cp_path.is_file():
        return {}, None
    try:
        campaign, resolved = load_campaign(cp_path)
    except (FileNotFoundError, ValidationError, ValueError):
        return {}, None

    lookup: dict[tuple[str, str], FioJob] = {}
    for rr in resolved:
        if isinstance(rr.scenario, FioScenarioSpec):
            for job in rr.scenario.jobs:
                lookup[(rr.scenario.name, job.name)] = job
    return lookup, campaign.vendor_spec


def _build_section(
    s: ScenarioRow,
    metrics_by_key: dict[_MetricKey, float],
    base_by_key: dict[_MetricKey, float] | None,
    job_lookup: dict[tuple[str, str], FioJob],
    vendor_spec: VendorSpec | None,
) -> ScenarioSection:
    # Collect (job_name, op) pairs present in this scenario.
    present: set[tuple[str, str]] = set()
    for (scen, rep, job, op, _metric), _v in metrics_by_key.items():
        if scen == s.scenario_name and rep == s.repeat_idx:
            present.add((job, op))

    rows: list[ReportRow] = []
    for job_name, op in sorted(present):
        job_meta = job_lookup.get((s.scenario_name, job_name))
        primary_label = _primary_metric(job_meta, op)
        primary_value = metrics_by_key.get(
            (s.scenario_name, s.repeat_idx, job_name, op, primary_label)
        )
        p99 = metrics_by_key.get(
            (s.scenario_name, s.repeat_idx, job_name, op, "lat_p99_us")
        )
        pct_spec = _pct_of_spec(job_meta, op, primary_label, primary_value, vendor_spec)
        delta_baseline = None
        if base_by_key is not None and primary_value is not None:
            base_val = base_by_key.get(
                (s.scenario_name, s.repeat_idx, job_name, op, primary_label)
            )
            if base_val and base_val != 0:
                delta_baseline = (primary_value - base_val) / base_val * 100.0
        rows.append(
            ReportRow(
                job_name=job_name,
                op=op,
                primary_label=primary_label,
                primary_value=primary_value,
                p99_us=p99,
                pct_of_spec=pct_spec,
                delta_vs_baseline_pct=delta_baseline,
            )
        )

    return ScenarioSection(
        scenario_name=s.scenario_name,
        repeat_idx=s.repeat_idx,
        target_name=s.target_name,
        status=s.status,
        rows=rows,
    )


def _primary_metric(job: FioJob | None, op: str) -> str:
    """Decide which metric to feature for a given (job, op).

    Seq jobs (rw in {read,write,readwrite}) → bw_mbps.
    Rand jobs (rw in {randread,randwrite,randrw}) → iops.
    If job metadata is missing, fall back to bw_mbps (safe; works for most ops).
    """
    if job is None:
        return "bw_mbps"
    if job.rw in ("randread", "randwrite", "randrw"):
        return "iops"
    return "bw_mbps"


def _pct_of_spec(
    job: FioJob | None,
    op: str,
    metric: str,
    value: float | None,
    spec: VendorSpec | None,
) -> float | None:
    if job is None or spec is None or value is None:
        return None
    target = _spec_target(job, op, metric, spec)
    if not target:
        return None
    return value / target * 100.0


def _spec_target(job: FioJob, op: str, metric: str, spec: VendorSpec) -> float | None:
    """Map (job.rw, op, metric) to the right VendorSpec field."""
    if metric == "bw_mbps":
        if op == "write":
            return spec.seqwrite_mbps
        if op == "read":
            return spec.seqread_mbps
        return None
    if metric == "iops":
        if job.rw == "randread" or (job.rw == "randrw" and op == "read"):
            return spec.randread_iops
        if job.rw == "randwrite" or (job.rw == "randrw" and op == "write"):
            return spec.randwrite_iops
        return None
    return None


# ---- template ---------------------------------------------------------------


_TEMPLATE = jinja2.Template(
    """# Run {{ run.run_id }}

- **Campaign:** {{ run.campaign_name }}
- **Started:** {{ run.started_at }}
- **Ended:** {{ run.ended_at or "(in progress)" }}
- **Status:** {{ run.status }}
{% if baseline -%}
- **Baseline:** {{ baseline.run_id }} ({{ baseline.started_at }})
{% endif -%}
{% if vendor_spec and vendor_spec.label %}
- **Spec reference:** {{ vendor_spec.label }}
{% endif %}

## Per-scenario results
{% for s in sections %}
### {{ s.scenario_name }}{% if s.repeat_idx %} (repeat {{ s.repeat_idx }}){% endif %} → {{ s.target_name }}

{% if not s.rows -%}
*(no metrics recorded; scenario status: {{ s.status }})*
{%- else -%}
| Job | Op | Throughput / IOPS | p99 latency | % of spec{% if baseline %} | vs baseline{% endif %} |
| --- | --- | ---: | ---: | ---:{% if baseline %} | ---:{% endif %} |
{% for r in s.rows -%}
| {{ r.job_name }} | {{ r.op }} | {{ fmt_primary(r) }} | {{ fmt_p99(r) }} | {{ fmt_pct(r.pct_of_spec) }}{% if baseline %} | {{ fmt_delta(r.delta_vs_baseline_pct) }}{% endif %} |
{% endfor %}
{%- endif %}
{% endfor %}
""",
    trim_blocks=True,
    lstrip_blocks=True,
)


def _fmt_primary(r: ReportRow) -> str:
    if r.primary_value is None:
        return "—"
    if r.primary_label == "bw_mbps":
        return f"{r.primary_value:.0f} MB/s"
    return f"{r.primary_value:.0f} IOPS"


def _fmt_p99(r: ReportRow) -> str:
    if r.p99_us is None:
        return "—"
    return f"{r.p99_us:.0f} µs"


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f}%"


def _fmt_delta(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


_TEMPLATE.globals.update(
    fmt_primary=_fmt_primary,
    fmt_p99=_fmt_p99,
    fmt_pct=_fmt_pct,
    fmt_delta=_fmt_delta,
)
