"""Smoke tests for `harness validate` and the underlying config loader."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from harness.cli import main as cli_main
from harness.config import FioJob, load_campaign, parse_size_gib

FIXTURES = Path(__file__).parent / "fixtures"
CAMPAIGN_OK = FIXTURES / "campaign.yaml"


def test_load_campaign_ok():
    campaign, resolved = load_campaign(CAMPAIGN_OK)
    assert campaign.name == "smoke-test"
    assert len(resolved) == 1
    assert resolved[0].scenario.name == "tiny-smoke"
    assert resolved[0].target.name == "spark01-tmp"


def test_cli_validate_ok():
    result = CliRunner().invoke(cli_main, ["validate", str(CAMPAIGN_OK)])
    assert result.exit_code == 0, result.output
    assert "[OK]" in result.output
    assert "smoke-test" in result.output
    assert "1 run(s)" in result.output


def test_cli_validate_missing_file():
    result = CliRunner().invoke(cli_main, ["validate", "/nonexistent.yaml"])
    assert result.exit_code != 0


def test_multi_thread_without_offset_rejected():
    # The 007 gotcha: numjobs>1 without offset_increment serves from drive DRAM cache.
    with pytest.raises(ValidationError):
        FioJob(name="bad", rw="read", bs="1M", numjobs=4, iodepth=32)


def test_multi_thread_with_offset_accepted():
    FioJob(name="ok", rw="read", bs="1M", numjobs=4, iodepth=32, offset_increment="64g")


@pytest.mark.parametrize(
    "raw,expected_gib",
    [("2T", 2048), ("100G", 100), ("4096G", 4096), ("100M", 0), ("1P", 1024 * 1024)],
)
def test_parse_size_gib(raw, expected_gib):
    assert parse_size_gib(raw) == expected_gib


def test_parse_size_gib_rejects_unknown_suffix():
    with pytest.raises(ValueError):
        parse_size_gib("100X")


def test_capacity_sanity_blocks_oversize_testfile(tmp_path):
    # Build a campaign whose scenario asks for 2T against a 100 GiB target.
    scen = tmp_path / "scenarios" / "fio" / "huge.yaml"
    scen.parent.mkdir(parents=True)
    scen.write_text(
        "kind: fio\nname: huge\nglobal: { size: 2T, runtime: 5, ramp_time: 1 }\n"
        "jobs:\n  - { name: w, rw: write, bs: 1M }\n"
    )
    camp = tmp_path / "campaign.yaml"
    camp.write_text(
        "name: oversize-test\n"
        "cluster: { nodes: [{ name: n, ssh: u@h }] }\n"
        "targets:\n  - { name: t, node: n, path: /tmp/x, kind: local-nvme, capacity_gib: 100 }\n"
        "scenarios_dir: scenarios\n"
        "runs:\n  - { scenario: fio/huge.yaml, target: t }\n"
    )
    with pytest.raises(ValueError, match="exceeds.*capacity_gib"):
        load_campaign(camp)


def test_cross_ref_unknown_target_rejected(tmp_path):
    camp = tmp_path / "campaign.yaml"
    camp.write_text(
        "name: bad-ref\n"
        "cluster: { nodes: [{ name: n, ssh: u@h }] }\n"
        "targets:\n  - { name: t, node: n, path: /tmp/x, kind: local-nvme }\n"
        "runs:\n  - { scenario: x.yaml, target: nonexistent }\n"
    )
    with pytest.raises(ValidationError, match="nonexistent"):
        load_campaign(camp)
