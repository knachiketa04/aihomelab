"""Orchestrator (Layer 2 glue).

Walks a Campaign's resolved runs sequentially, dispatching each to the right
Runner. After every scenario finishes, the SQLite store is updated so a
crashed orchestrator can be resumed via ``harness resume`` — scenarios that
already finished with status='ok' are skipped on the next pass.

Dry-run mode propagates to both layers: the orchestrator skips SQLite writes
and the Runner skips SSH. Net effect: print the plan, touch nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from harness.config import Campaign, ResolvedRun, load_campaign
from harness.runners.base import RunContext, Runner
from harness.runners.fio import FioRunner
from harness.store import (
    finish_run,
    finish_scenario,
    init_db,
    list_runs,
    record_metrics,
    scenario_completed,
    start_run,
    start_scenario,
)

ProgressFn = Callable[[str], None]


_RUNNERS: dict[str, type[Runner]] = {
    "fio": FioRunner,
}


def register_runner(kind: str, cls: type[Runner]) -> None:
    """Register a Runner for a scenario kind. Used by tests and v0.1 mlperf stub."""
    _RUNNERS[kind] = cls


def _new_run_id(campaign_name: str) -> str:
    return f"{campaign_name}-{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%S')}"


def find_latest_running_run(
    results_root: Path, campaign_name: str | None = None
) -> str | None:
    """Return the most recent run_id with status='running', optionally filtered by campaign."""
    db_path = results_root / "harness.db"
    if not db_path.exists():
        return None
    for row in list_runs(db_path):
        if row.status != "running":
            continue
        if campaign_name and row.campaign_name != campaign_name:
            continue
        return row.run_id
    return None


def allocate_run(
    campaign_name: str, results_root: Path, *, dry_run: bool = False
) -> tuple[str, Path]:
    """Allocate a fresh ``run_id`` and create its raw_dir. Returns ``(run_id, raw_dir)``.

    Useful for callers (the CLI) that want the raw_dir on disk before invoking
    :func:`run_campaign` — e.g. to open an ``orchestrator.log`` file alongside it.
    """
    run_id = _new_run_id(campaign_name)
    raw_dir = results_root / "raw" / run_id
    if not dry_run:
        raw_dir.mkdir(parents=True, exist_ok=True)
    return run_id, raw_dir


def run_campaign(
    campaign_path: Path,
    *,
    results_root: Path = Path("results"),
    dry_run: bool = False,
    progress: ProgressFn | None = None,
    resume_run_id: str | None = None,
    new_run_id: str | None = None,
) -> tuple[str, str]:
    """Execute (or resume) a campaign.

    Returns ``(run_id, status)`` where status is ``'ok'`` or ``'failed'``.

    Mode selection:
      - ``resume_run_id`` set: continue an existing run, skipping scenarios
        already finished with status='ok'.
      - ``new_run_id`` set: start fresh with the caller-provided run_id (use
        :func:`allocate_run` to obtain one alongside its raw_dir).
      - neither set: allocate a fresh run_id internally.
    """
    if resume_run_id and new_run_id:
        raise ValueError("pass at most one of resume_run_id / new_run_id")

    p = progress or (lambda _: None)
    campaign, resolved = load_campaign(campaign_path)

    db_path = results_root / "harness.db"

    if resume_run_id:
        run_id = resume_run_id
        raw_dir = results_root / "raw" / run_id
        raw_dir.mkdir(parents=True, exist_ok=True)
        p(f"Resuming run {run_id}")
    else:
        run_id = new_run_id or _new_run_id(campaign.name)
        raw_dir = results_root / "raw" / run_id
        raw_dir.mkdir(parents=True, exist_ok=True)
        if not dry_run:
            init_db(db_path)
            start_run(
                db_path,
                run_id=run_id,
                campaign_name=campaign.name,
                campaign_path=campaign_path,
                raw_dir=raw_dir,
            )
        p(f"Starting run {run_id} ({len(resolved)} scenario binding(s))")

    ctx = RunContext(run_id=run_id, raw_dir=raw_dir, dry_run=dry_run)
    overall_status = "ok"

    for idx, rr in enumerate(resolved, start=1):
        any_repeat_failed = _execute_scenario(
            rr=rr,
            idx=idx,
            total=len(resolved),
            campaign=campaign,
            ctx=ctx,
            db_path=db_path,
            dry_run=dry_run,
            progress=p,
        )
        if any_repeat_failed:
            overall_status = "failed"

    if campaign.cleanup == "after_campaign":
        _cleanup_targets(resolved, campaign, ctx, progress=p)

    if not dry_run:
        finish_run(db_path, run_id, overall_status)
    p(f"\nRun {run_id} finished: {overall_status}")
    return run_id, overall_status


def _execute_scenario(
    *,
    rr: ResolvedRun,
    idx: int,
    total: int,
    campaign: Campaign,
    ctx: RunContext,
    db_path: Path,
    dry_run: bool,
    progress: ProgressFn,
) -> bool:
    """Execute one ResolvedRun (scenario+target binding) including all its repeats.

    Returns True if any repeat failed.
    """
    scenario = rr.scenario
    target = rr.target
    runner_cls = _RUNNERS.get(scenario.kind)
    if runner_cls is None:
        progress(f"[FAIL] no runner registered for scenario kind={scenario.kind!r}")
        return True
    runner = runner_cls()
    node = next(n for n in campaign.cluster.nodes if n.name == target.node)

    any_repeat_failed = False
    for repeat in range(rr.run.repeat):
        label = f"{scenario.name}#{repeat}" if rr.run.repeat > 1 else scenario.name

        if not dry_run and scenario_completed(db_path, ctx.run_id, scenario.name, repeat):
            progress(f"[SKIP] {label} (already completed)")
            continue

        progress(f"\n[{idx}/{total}] {label} → {target.name}")
        if not dry_run:
            start_scenario(
                db_path,
                run_id=ctx.run_id,
                scenario_name=scenario.name,
                repeat_idx=repeat,
                kind=scenario.kind,
                target_name=target.name,
                target_node=target.node,
                target_path=str(target.path),
            )

        try:
            progress(f"  prepare ({target.path})")
            runner.prepare(scenario, target, node, ctx)
        except Exception as e:  # noqa: BLE001 — surfacing to the operator
            progress(f"  [FAIL] prepare: {e}")
            if not dry_run:
                finish_scenario(
                    db_path, ctx.run_id, scenario.name, repeat, "failed", error=str(e)
                )
            any_repeat_failed = True
            continue

        scenario_failed = False
        for outcome in runner.run_jobs(scenario, target, node, ctx):
            if outcome.status == "ok":
                n_metrics = 0
                if not dry_run:
                    n_metrics = record_metrics(
                        db_path,
                        run_id=ctx.run_id,
                        scenario_name=scenario.name,
                        repeat_idx=repeat,
                        records=outcome.metrics,
                    )
                progress(
                    f"  [OK] {outcome.job_name}  "
                    f"({outcome.duration_seconds:.0f}s, {n_metrics} metrics)"
                )
            else:
                scenario_failed = True
                progress(
                    f"  [{outcome.status.upper()}] {outcome.job_name}: {outcome.error or '(no error message)'}"
                )

        status = "failed" if scenario_failed else "ok"
        if not dry_run:
            finish_scenario(db_path, ctx.run_id, scenario.name, repeat, status)
        if scenario_failed:
            any_repeat_failed = True

        if campaign.cleanup == "after_each_run":
            progress("  cleanup (after_each_run)")
            runner.cleanup(scenario, target, node, ctx)

    return any_repeat_failed


def _cleanup_targets(
    resolved: list[ResolvedRun],
    campaign: Campaign,
    ctx: RunContext,
    *,
    progress: ProgressFn,
) -> None:
    """Run runner.cleanup once per (target, kind) combination at end of campaign."""
    progress("\nCleanup (after_campaign): removing testfiles")
    seen: set[tuple[str, str]] = set()
    for rr in resolved:
        key = (rr.target.name, rr.scenario.kind)
        if key in seen:
            continue
        seen.add(key)
        runner = _RUNNERS[rr.scenario.kind]()
        node = next(n for n in campaign.cluster.nodes if n.name == rr.target.node)
        runner.cleanup(rr.scenario, rr.target, node, ctx)
        progress(f"  cleaned {rr.target.name}")
