"""Runner abstract base class (Layer 2).

Each Runner implements ``prepare → run_jobs → cleanup`` for one benchmarking
tool. The orchestrator (step 5) walks a Campaign, instantiates the right Runner
per scenario, and invokes these hooks in sequence. The fio Runner ships in v0;
MLPerf Storage stubs the interface for v0.1.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Iterator, Literal

from harness.config import Node, ScenarioSpec, StorageTarget
from harness.parsers.fio import MetricRecord


@dataclass
class RunContext:
    """Per-run context handed to each Runner method."""

    run_id: str
    raw_dir: Path  # local: results/raw/<run_id>/
    dry_run: bool = False


@dataclass
class JobOutcome:
    """Result of running one job within a scenario."""

    job_name: str
    status: Literal["ok", "failed", "timed_out"]
    raw_json_path: Path | None = None  # local path
    duration_seconds: float = 0.0
    error: str | None = None
    metrics: list[MetricRecord] = field(default_factory=list)


class Runner(ABC):
    """Tool-specific runner. Subclasses set ``kind`` to match the scenario kind."""

    kind: ClassVar[str]

    @abstractmethod
    def prepare(
        self,
        scenario: ScenarioSpec,
        target: StorageTarget,
        node: Node,
        ctx: RunContext,
    ) -> None:
        """Idempotent setup: ensure testfile exists, capture env, etc.

        Must NOT raise on conditions the orchestrator can recover from
        (e.g. transient ssh failures should be reported via exceptions the
        orchestrator catches; refuse-to-run guards like insufficient disk
        should raise ``RuntimeError`` with a clear message).
        """

    @abstractmethod
    def run_jobs(
        self,
        scenario: ScenarioSpec,
        target: StorageTarget,
        node: Node,
        ctx: RunContext,
    ) -> Iterator[JobOutcome]:
        """Yield one JobOutcome per job in the scenario, in declaration order."""

    @abstractmethod
    def cleanup(
        self,
        scenario: ScenarioSpec,
        target: StorageTarget,
        node: Node,
        ctx: RunContext,
    ) -> None:
        """Remove testfile(s) and any per-scenario temp state.

        The orchestrator decides *when* to call this (per the Campaign's
        cleanup mode: after_each_run | after_campaign | never).
        """
