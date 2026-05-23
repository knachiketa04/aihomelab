"""Command-line interface for the benchmark harness.

v0 commands:
  harness validate <campaign.yaml>      schema-check a campaign + its scenarios
  harness preflight <campaign.yaml>     ssh to each node, verify capacity + path
"""
from __future__ import annotations

import sys
from pathlib import Path

import click
from pydantic import ValidationError

from harness import __version__
from harness.config import FioScenarioSpec, load_campaign, parse_size_gib
from harness.ssh import preflight_target


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


if __name__ == "__main__":
    main()
