"""Pydantic schemas for harness scenarios and campaigns.

Two YAML file types:
  - Scenario file (scenarios/<tool>/<name>.yaml): reusable, target-agnostic recipe.
  - Campaign file (experiments/<NNN>/campaign.yaml): binds scenarios to specific
    targets on specific nodes for a particular experiment run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---- Scenario side ----------------------------------------------------------


class FioGlobal(BaseModel):
    """Global fio settings; defaults match experiment 007's [global] block."""

    model_config = ConfigDict(extra="forbid")

    ioengine: str = "io_uring"
    direct: int = 1
    time_based: int = 1
    runtime: int = 180
    ramp_time: int = 10
    end_fsync: int = 1
    group_reporting: int = 1
    # Testfile size. 2T is load-bearing for 16-thread non-wrap on Gen5 NVMe:
    #   runtime × (drive_bw / numjobs) → 180s × (~700 MB/s × 16) ≈ 2 TB minimum.
    # Below that, threads share byte ranges and serve from drive DRAM cache.
    size: str = "2T"


class FioJob(BaseModel):
    """One fio job within a scenario."""

    model_config = ConfigDict(extra="forbid")

    name: str
    rw: Literal["read", "write", "randread", "randwrite", "readwrite", "rw", "randrw"]
    bs: str
    numjobs: int = 1
    iodepth: int = 1
    offset_increment: str | None = None
    rwmixread: int | None = None
    # Per-job size override (e.g. "128g" to split a 2T testfile into 16×128g
    # regions). When None, the [global] size= applies. Required for multi-thread
    # jobs that use offset_increment, to prevent threads from looping back to
    # the start of the testfile and hitting drive DRAM cache. See experiment 007.
    size: str | None = None

    @model_validator(mode="after")
    def _multi_thread_needs_offset(self) -> FioJob:
        # Encodes the gotcha: numjobs>1 without offset_increment serves from drive DRAM
        # cache. See .agent-notes/gotchas.md and experiment 007.
        if self.numjobs > 1 and self.offset_increment is None:
            raise ValueError(
                f"job {self.name!r}: numjobs={self.numjobs} requires offset_increment "
                "(threads otherwise share byte ranges and serve from drive DRAM cache, "
                "inflating multi-thread throughput; see experiment 007 gotcha)"
            )
        return self


class FioScenarioSpec(BaseModel):
    """A reusable fio scenario recipe. Target-agnostic."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: Literal["fio"] = "fio"
    name: str
    description: str | None = None
    fio_global: FioGlobal = Field(default_factory=FioGlobal, alias="global")
    jobs: list[FioJob]
    drop_caches_between_jobs: bool = True
    timeout_seconds: int | None = None

    @field_validator("jobs")
    @classmethod
    def _jobs_non_empty_and_unique(cls, v: list[FioJob]) -> list[FioJob]:
        if not v:
            raise ValueError("scenario must declare at least one job")
        names = [j.name for j in v]
        dupes = [n for n in set(names) if names.count(n) > 1]
        if dupes:
            raise ValueError(f"duplicate job names: {sorted(dupes)}")
        return v

    def estimated_runtime_seconds(self) -> int:
        """Per-job budget: ramp + runtime + ~5s cache-drop overhead."""
        per_job = self.fio_global.ramp_time + self.fio_global.runtime + 5
        return per_job * len(self.jobs)


# Future v0.1: discriminated union when MlperfScenarioSpec lands.
ScenarioSpec = FioScenarioSpec


# ---- Campaign side ----------------------------------------------------------


class Node(BaseModel):
    """An SSH-reachable cluster node."""

    model_config = ConfigDict(extra="forbid")

    name: str
    ssh: str  # e.g. "sparks@192.168.20.21"


class Cluster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[Node]

    @field_validator("nodes")
    @classmethod
    def _node_names_unique(cls, v: list[Node]) -> list[Node]:
        names = [n.name for n in v]
        dupes = [n for n in set(names) if names.count(n) > 1]
        if dupes:
            raise ValueError(f"duplicate node names: {sorted(dupes)}")
        return v


class StorageTarget(BaseModel):
    """Named storage destination on a specific node."""

    model_config = ConfigDict(extra="forbid")

    name: str
    node: str
    path: Path
    kind: Literal["local-nvme", "lustre", "nfs", "gcs"]
    capacity_gib: int | None = None
    notes: str | None = None


class VendorSpec(BaseModel):
    """Optional reference numbers for '% of spec' columns in the report."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    seqwrite_mbps: float | None = None
    seqread_mbps: float | None = None
    randread_iops: float | None = None
    randwrite_iops: float | None = None


class CampaignRun(BaseModel):
    """One execution: bind a scenario file to a target."""

    model_config = ConfigDict(extra="forbid")

    scenario: str  # path resolved against (campaign_dir / scenarios_dir)
    target: str  # references Campaign.targets[*].name
    repeat: int = 1


class Campaign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    cluster: Cluster
    targets: list[StorageTarget]
    vendor_spec: VendorSpec | None = None
    runs: list[CampaignRun]

    scenarios_dir: Path = Path(".")
    cleanup: Literal["after_campaign", "after_each_run", "never"] = "after_campaign"
    safety_margin_gib: int = 100
    baseline_run_id: str | None = None

    @model_validator(mode="after")
    def _cross_refs(self) -> Campaign:
        node_names = {n.name for n in self.cluster.nodes}
        target_names = {t.name for t in self.targets}
        all_target_names = [t.name for t in self.targets]
        target_dupes = sorted({n for n in all_target_names if all_target_names.count(n) > 1})
        if target_dupes:
            raise ValueError(f"duplicate target names: {target_dupes}")
        for t in self.targets:
            if t.node not in node_names:
                raise ValueError(
                    f"target {t.name!r}: node {t.node!r} not in cluster.nodes ({sorted(node_names)})"
                )
        for i, r in enumerate(self.runs):
            if r.target not in target_names:
                raise ValueError(
                    f"runs[{i}]: target {r.target!r} not in campaign.targets ({sorted(target_names)})"
                )
        return self


# ---- Resolution & loading ---------------------------------------------------


class ResolvedRun(BaseModel):
    """A CampaignRun with its referenced scenario and target loaded."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run: CampaignRun
    scenario: ScenarioSpec
    target: StorageTarget
    scenario_path: Path


def load_yaml(path: Path) -> dict:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data or {}


def load_scenario(path: Path) -> ScenarioSpec:
    return FioScenarioSpec.model_validate(load_yaml(path))


def load_campaign(path: Path) -> tuple[Campaign, list[ResolvedRun]]:
    """Load a campaign and resolve every scenario it references.

    Returns ``(campaign, [ResolvedRun ...])``. Raises ``FileNotFoundError``,
    ``ValidationError``, or ``ValueError`` on any schema or cross-ref problem.

    Capacity sanity (testfile_size <= target.capacity_gib) is checked here.
    Free-space and safety-margin checks happen at run time, via SSH ``df``.
    """
    campaign_path = path.resolve()
    campaign = Campaign.model_validate(load_yaml(campaign_path))

    target_map = {t.name: t for t in campaign.targets}
    base = campaign_path.parent / campaign.scenarios_dir

    resolved: list[ResolvedRun] = []
    for run in campaign.runs:
        scen_path = (base / run.scenario).resolve()
        if not scen_path.exists():
            raise FileNotFoundError(
                f"scenario file not found: {scen_path} "
                f"(resolved from runs[*].scenario={run.scenario!r} "
                f"+ scenarios_dir={campaign.scenarios_dir!s})"
            )
        scenario = load_scenario(scen_path)
        resolved.append(
            ResolvedRun(
                run=run,
                scenario=scenario,
                target=target_map[run.target],
                scenario_path=scen_path,
            )
        )

    # Static capacity sanity. Free-space + safety_margin_gib are checked at run time.
    for rr in resolved:
        if isinstance(rr.scenario, FioScenarioSpec):
            tf_gib = parse_size_gib(rr.scenario.fio_global.size)
            if rr.target.capacity_gib is not None and tf_gib > rr.target.capacity_gib:
                raise ValueError(
                    f"scenario {rr.scenario.name!r}: testfile size "
                    f"{rr.scenario.fio_global.size} ({tf_gib} GiB) exceeds "
                    f"target {rr.target.name!r} capacity_gib={rr.target.capacity_gib}"
                )

    return campaign, resolved


_SIZE_GIB_MULT = {"k": 1 / (1024 * 1024), "m": 1 / 1024, "g": 1.0, "t": 1024.0, "p": 1024.0 * 1024}


def parse_size_gib(s: str) -> int:
    """Convert an fio-style size string ('2T', '4096G', '100M') to GiB (integer)."""
    raw = s.strip().lower()
    if raw.endswith("ib"):
        raw = raw[:-2]
    elif raw.endswith("b"):
        raw = raw[:-1]
    if not raw:
        raise ValueError(f"empty size string: {s!r}")
    unit = raw[-1]
    if unit not in _SIZE_GIB_MULT:
        raise ValueError(f"unknown size suffix in {s!r}; expected K/M/G/T/P")
    return int(float(raw[:-1]) * _SIZE_GIB_MULT[unit])
