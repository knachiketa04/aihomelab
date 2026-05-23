"""Command-line interface for the benchmark harness.

v0 commands:
  harness validate <campaign.yaml>      schema-check a campaign + its scenarios
  harness preflight <campaign.yaml>     ssh to each node, verify capacity + path
  harness run <campaign.yaml>           preflight, then execute the campaign
  harness resume <campaign.yaml>        resume the latest in-progress run
  harness list-runs                     show stored runs (newest first)
  harness report <run-id>               render a markdown report for a run
"""
from __future__ import annotations

import sys
from pathlib import Path

import click
from pydantic import ValidationError

from harness import __version__
from harness.config import FioScenarioSpec, load_campaign, parse_size_gib
from harness.orchestrator import allocate_run, find_latest_running_run, run_campaign
from harness.report import render_report
from harness.ssh import preflight_target
from harness.store import list_runs as store_list_runs


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="harness")
def main() -> None:
    """benchmark-harness: declarative, unattended storage benchmarking."""


@main.command()
@click.argument("campaign_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def validate(campaign_path: Path) -> None:
    """Schema-check a campaign and the scenarios it references.

    Loads the campaign YAML, parses it against the pydantic schema, then
    resolves and loads every scenario referenced by runs[*]. Prints a summary
    and exits 0 if all checks pass.
    """
    try:
        campaign, resolved = load_campaign(campaign_path)
    except FileNotFoundError as e:
        click.echo(f"[FAIL] {e}", err=True)
        sys.exit(1)
    except ValidationError as e:
        click.echo(f"[FAIL] schema error in {campaign_path}:", err=True)
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "<root>"
            click.echo(f"  {loc}: {err['msg']}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"[FAIL] {e}", err=True)
        sys.exit(1)

    click.echo(f"[OK] campaign {campaign.name!r}")
    if campaign.description:
        click.echo(f"     {campaign.description}")

    click.echo(
        f"     {len(campaign.cluster.nodes)} node(s):  "
        + ", ".join(f"{n.name} ({n.ssh})" for n in campaign.cluster.nodes)
    )

    click.echo(f"     {len(campaign.targets)} target(s):")
    for t in campaign.targets:
        cap = f"{t.capacity_gib} GiB" if t.capacity_gib is not None else "?"
        click.echo(f"       - {t.name}  ({t.kind} @ {t.node}:{t.path}, cap={cap})")

    click.echo(f"     {len(resolved)} run(s):")
    total_seconds = 0
    for i, rr in enumerate(resolved, start=1):
        budget = rr.scenario.estimated_runtime_seconds() * rr.run.repeat
        total_seconds += budget
        njobs = len(rr.scenario.jobs)
        size = rr.scenario.fio_global.size if isinstance(rr.scenario, FioScenarioSpec) else "?"
        click.echo(
            f"       {i}. {rr.scenario.name}  →  {rr.target.name}  "
            f"({njobs} job(s), repeat={rr.run.repeat}, testfile={size}, "
            f"~{budget // 60} min budget)"
        )

    hrs, mins = divmod(total_seconds // 60, 60)
    click.echo(f"     cleanup mode:   {campaign.cleanup}")
    click.echo(f"     safety margin:  {campaign.safety_margin_gib} GiB")
    click.echo(f"     total budget:   ~{total_seconds // 60} min ({hrs}h {mins}m)")

    # Note: free-space pre-flight runs at execution time (needs SSH).
    _print_capacity_notice(resolved, campaign.safety_margin_gib)


def _print_capacity_notice(resolved: list, safety_margin_gib: int) -> None:
    """Surface per-target testfile footprint so the operator sees disk demand upfront."""
    by_target: dict[str, int] = {}
    for rr in resolved:
        if isinstance(rr.scenario, FioScenarioSpec):
            tf = parse_size_gib(rr.scenario.fio_global.size)
            by_target[rr.target.name] = max(by_target.get(rr.target.name, 0), tf)
    if by_target:
        click.echo("     disk demand (max testfile per target, free-space check at run time):")
        for name, gib in by_target.items():
            click.echo(f"       - {name}: {gib} GiB testfile + {safety_margin_gib} GiB margin")


@main.command()
@click.argument("campaign_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--dry-run",
    is_flag=True,
    help="Don't actually SSH; just print what would be checked.",
)
def preflight(campaign_path: Path, dry_run: bool) -> None:
    """SSH to each node and verify each target is reachable + has enough free space.

    Aggregates the largest testfile required per target across all runs, then
    checks ``free_space >= required + safety_margin_gib``. Reports every
    finding, then exits non-zero if any target failed.
    """
    try:
        campaign, resolved = load_campaign(campaign_path)
    except (FileNotFoundError, ValidationError, ValueError) as e:
        click.echo(f"[FAIL] could not load campaign: {e}", err=True)
        sys.exit(1)

    node_map = {n.name: n for n in campaign.cluster.nodes}

    requirements: dict[str, int] = {}
    for rr in resolved:
        if isinstance(rr.scenario, FioScenarioSpec):
            tf_gib = parse_size_gib(rr.scenario.fio_global.size)
            requirements[rr.target.name] = max(requirements.get(rr.target.name, 0), tf_gib)

    if not requirements:
        click.echo("[OK] no targets exercised by this campaign — nothing to preflight.")
        return

    click.echo(f"Preflight for campaign {campaign.name!r}{' (dry-run)' if dry_run else ''}:")
    findings = []
    for target in campaign.targets:
        if target.name not in requirements:
            continue
        finding = preflight_target(
            node_map[target.node],
            target,
            required_gib=requirements[target.name],
            safety_margin_gib=campaign.safety_margin_gib,
            dry_run=dry_run,
        )
        findings.append(finding)
        status = "[OK]  " if finding.ok else "[FAIL]"
        click.echo(f"  {status} {finding.node_name}:{finding.target_name}  ({finding.path})")
        click.echo(f"         {finding.message}")

    fails = [f for f in findings if not f.ok]
    if fails:
        click.echo(f"\n{len(fails)} of {len(findings)} preflight check(s) failed.", err=True)
        sys.exit(1)
    click.echo(f"\nAll {len(findings)} preflight check(s) passed.")


def _make_progress(log_file: Path | None):
    """Build a progress callback that emits to click.echo and (optionally) a log file."""
    fh = open(log_file, "a", buffering=1) if log_file else None  # line-buffered

    def progress(msg: str) -> None:
        click.echo(msg)
        if fh is not None:
            fh.write(msg + "\n")

    return progress, fh


@main.command()
@click.argument("campaign_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Print the plan; don't ssh or write to the DB.")
@click.option(
    "--no-preflight",
    is_flag=True,
    help="Skip the capacity preflight (NOT recommended for unattended runs).",
)
@click.option(
    "--results-root",
    type=click.Path(path_type=Path),
    default=Path("results"),
    show_default=True,
    help="Directory for the SQLite store and per-run raw output.",
)
def run(campaign_path: Path, dry_run: bool, no_preflight: bool, results_root: Path) -> None:
    """Execute a campaign end-to-end (unattended). Preflights by default."""
    try:
        campaign, resolved = load_campaign(campaign_path)
    except (FileNotFoundError, ValidationError, ValueError) as e:
        click.echo(f"[FAIL] could not load campaign: {e}", err=True)
        sys.exit(1)

    if not no_preflight and not dry_run:
        click.echo("Preflight:")
        node_map = {n.name: n for n in campaign.cluster.nodes}
        requirements: dict[str, int] = {}
        for rr in resolved:
            if isinstance(rr.scenario, FioScenarioSpec):
                tf = parse_size_gib(rr.scenario.fio_global.size)
                requirements[rr.target.name] = max(requirements.get(rr.target.name, 0), tf)
        for target in campaign.targets:
            if target.name not in requirements:
                continue
            finding = preflight_target(
                node_map[target.node],
                target,
                required_gib=requirements[target.name],
                safety_margin_gib=campaign.safety_margin_gib,
            )
            status = "[OK]  " if finding.ok else "[FAIL]"
            click.echo(f"  {status} {finding.node_name}:{finding.target_name}  {finding.message}")
            if not finding.ok:
                click.echo("\nPreflight failed. Aborting (use --no-preflight to override).", err=True)
                sys.exit(1)

    # Pre-allocate raw_dir so we can open the orchestrator.log file alongside it.
    run_id, raw_dir = allocate_run(campaign.name, results_root, dry_run=dry_run)
    log_file = (raw_dir / "orchestrator.log") if not dry_run else None
    progress, fh = _make_progress(log_file)
    try:
        _, status = run_campaign(
            campaign_path,
            results_root=results_root,
            dry_run=dry_run,
            progress=progress,
            new_run_id=run_id,
        )
    finally:
        if fh is not None:
            fh.close()

    sys.exit(0 if status == "ok" else 2)


@main.command()
@click.argument(
    "campaign_path",
    required=False,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--run-id", help="Explicit run to resume; otherwise picks the latest 'running'.")
@click.option(
    "--results-root",
    type=click.Path(path_type=Path),
    default=Path("results"),
    show_default=True,
)
def resume(campaign_path: Path | None, run_id: str | None, results_root: Path) -> None:
    """Resume an interrupted run. Skips scenarios that already finished OK."""
    if not run_id:
        if campaign_path is None:
            click.echo("provide either a campaign path or --run-id", err=True)
            sys.exit(1)
        # Need campaign_name to filter; load just enough to read it.
        try:
            campaign, _ = load_campaign(campaign_path)
        except (FileNotFoundError, ValidationError, ValueError) as e:
            click.echo(f"[FAIL] could not load campaign: {e}", err=True)
            sys.exit(1)
        run_id = find_latest_running_run(results_root, campaign.name)
        if run_id is None:
            click.echo(f"no in-progress run found for campaign {campaign.name!r}", err=True)
            sys.exit(1)

    if campaign_path is None:
        click.echo("--run-id requires a campaign path so scenarios can be re-resolved", err=True)
        sys.exit(1)

    raw_dir = results_root / "raw" / run_id
    log_file = raw_dir / "orchestrator.log"
    progress, fh = _make_progress(log_file)
    try:
        _, status = run_campaign(
            campaign_path,
            results_root=results_root,
            progress=progress,
            resume_run_id=run_id,
        )
    finally:
        if fh is not None:
            fh.close()
    sys.exit(0 if status == "ok" else 2)


@main.command(name="list-runs")
@click.option(
    "--results-root",
    type=click.Path(path_type=Path),
    default=Path("results"),
    show_default=True,
)
def list_runs_cmd(results_root: Path) -> None:
    """Show runs stored in this results directory (newest first)."""
    db = results_root / "harness.db"
    if not db.exists():
        click.echo(f"no harness.db at {db}", err=True)
        sys.exit(1)
    rows = store_list_runs(db)
    if not rows:
        click.echo("(no runs recorded)")
        return
    click.echo(f"{'run_id':<48} {'campaign':<24} {'status':<10} started_at")
    for r in rows:
        click.echo(f"{r.run_id:<48} {r.campaign_name:<24} {r.status:<10} {r.started_at}")


@main.command()
@click.argument("run_id")
@click.option("--baseline", "baseline_run_id", help="Run ID to diff against.")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output path. Default: <results-root>/reports/<run_id>.md",
)
@click.option(
    "--results-root",
    type=click.Path(path_type=Path),
    default=Path("results"),
    show_default=True,
)
def report(
    run_id: str,
    baseline_run_id: str | None,
    output: Path | None,
    results_root: Path,
) -> None:
    """Render a markdown report for RUN_ID.

    Reads the SQLite store at <results-root>/harness.db. If the campaign
    YAML referenced by the run is still on disk, jobs are classified
    (seq vs rand) and "% of spec" is computed from vendor_spec.
    """
    db = results_root / "harness.db"
    if not db.exists():
        click.echo(f"no harness.db at {db}", err=True)
        sys.exit(1)
    try:
        text = render_report(db, run_id, baseline_run_id=baseline_run_id)
    except ValueError as e:
        click.echo(f"[FAIL] {e}", err=True)
        sys.exit(1)

    out_path = output or (results_root / "reports" / f"{run_id}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    click.echo(f"wrote {out_path}")


if __name__ == "__main__":
    main()
