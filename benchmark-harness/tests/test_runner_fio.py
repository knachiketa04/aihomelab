"""Unit tests for the fio runner.

We mock subprocess.run at the boundary (harness.ssh module) rather than the
Runner internals, so tests cover the ssh command shape, the rendered fio
config, and the JSON capture path end-to-end.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.config import FioGlobal, FioJob, FioScenarioSpec, Node, StorageTarget
from harness.runners.base import RunContext
from harness.runners.fio import FioRunner, render_fio_config

NODE = Node(name="spark01", ssh="sparks@192.168.20.21")
TARGET = StorageTarget(
    name="spark01-nvme",
    node="spark01",
    path=Path("/home/sparks/bench/"),
    kind="local-nvme",
    capacity_gib=3700,
)

SAMPLE_JSON = (Path(__file__).parent / "fixtures" / "sample-fio-output.json").read_text()


def _scenario(jobs):
    return FioScenarioSpec(
        kind="fio",
        name="test-scenario",
        fio_global=FioGlobal(runtime=5, ramp_time=1, size="100G"),
        jobs=jobs,
        drop_caches_between_jobs=False,
    )


def test_render_single_thread_job():
    job = FioJob(name="seqwrite-1t", rw="write", bs="1M", numjobs=1, iodepth=32)
    cfg = render_fio_config(_scenario([job]), job, "/home/sparks/bench")
    assert "[global]" in cfg
    assert "directory=/home/sparks/bench" in cfg
    assert "filename=testfile" in cfg
    assert "size=100G" in cfg
    assert "[seqwrite-1t]" in cfg
    assert "rw=write" in cfg
    assert "numjobs=1" in cfg
    assert "offset_increment=" not in cfg  # not declared, must not appear


def test_render_multi_thread_includes_offset_increment():
    job = FioJob(
        name="seqread-16t", rw="read", bs="1M",
        numjobs=16, iodepth=32, offset_increment="64g",
    )
    cfg = render_fio_config(_scenario([job]), job, "/home/sparks/bench")
    assert "offset_increment=64g" in cfg


def test_render_mixed_includes_rwmixread():
    job = FioJob(name="mix", rw="randrw", bs="4k", numjobs=1, iodepth=64, rwmixread=70)
    cfg = render_fio_config(_scenario([job]), job, "/home/sparks/bench")
    assert "rwmixread=70" in cfg


def test_render_per_job_size_override():
    # 007 pattern: 16-thread seq jobs need per-job size= to bound each thread's region.
    job = FioJob(
        name="seqread-16t", rw="read", bs="1M",
        numjobs=16, iodepth=1, offset_increment="128g", size="128g",
    )
    cfg = render_fio_config(_scenario([job]), job, "/home/sparks/bench")
    # Both global and per-job size appear; the per-job one wins in fio precedence.
    assert "size=100G" in cfg  # global default from _scenario
    assert "size=128g" in cfg  # per-job override


def test_render_short_rw_alias_accepted():
    # 007's mixed 70/30 job uses `rw=rw` (the fio short alias for `readwrite`).
    job = FioJob(name="mixed", rw="rw", bs="1M", numjobs=1, iodepth=1, rwmixread=70)
    cfg = render_fio_config(_scenario([job]), job, "/home/sparks/bench")
    assert "rw=rw" in cfg
    assert "rwmixread=70" in cfg


def test_dry_run_writes_placeholder_and_does_not_ssh(tmp_path, monkeypatch):
    def boom(*a, **kw):
        pytest.fail("subprocess.run should not be called in dry-run mode")

    monkeypatch.setattr(subprocess, "run", boom)
    ctx = RunContext(run_id="r1", raw_dir=tmp_path, dry_run=True)
    scen = _scenario([FioJob(name="j1", rw="write", bs="1M")])

    runner = FioRunner()
    runner.prepare(scen, TARGET, NODE, ctx)
    outcomes = list(runner.run_jobs(scen, TARGET, NODE, ctx))
    assert len(outcomes) == 1
    assert outcomes[0].status == "ok"
    assert outcomes[0].raw_json_path.exists()
    # In dry-run, no metrics are produced (placeholder JSON is empty).
    assert outcomes[0].metrics == []


def test_run_jobs_captures_json_and_parses_metrics(tmp_path, monkeypatch):
    """Wire up a fake subprocess that returns the sample fio JSON; verify parse."""
    # subprocess.run is called for each ssh invocation; for run_jobs there's
    # exactly one ssh call per job (no cache-drop since we disabled it).
    calls = {"n": 0}

    def fake_run(argv, capture_output, text, timeout=None, input=None):
        calls["n"] += 1
        return MagicMock(returncode=0, stdout=SAMPLE_JSON, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ctx = RunContext(run_id="r1", raw_dir=tmp_path, dry_run=False)
    scen = _scenario([FioJob(name="seqwrite-1t", rw="write", bs="1M")])
    outcomes = list(FioRunner().run_jobs(scen, TARGET, NODE, ctx))

    assert calls["n"] == 1  # one ssh call per job, no cache-drop
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.status == "ok"
    # JSON was captured to local raw dir
    assert o.raw_json_path is not None and o.raw_json_path.exists()
    saved = json.loads(o.raw_json_path.read_text())
    assert saved["fio version"] == "fio-3.36"
    # Metrics were parsed from the captured JSON
    bw = [m for m in o.metrics if m.metric == "bw_mbps"][0]
    assert bw.value == 13503.0


def test_run_jobs_drops_caches_when_enabled(tmp_path, monkeypatch):
    """drop_caches_between_jobs=True adds two ssh calls per job: sudo -n sync, sudo -n tee."""
    captured_argvs: list[list[str]] = []

    def fake_run(argv, capture_output, text, timeout=None, input=None):
        captured_argvs.append(argv)
        return MagicMock(returncode=0, stdout=SAMPLE_JSON, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ctx = RunContext(run_id="r1", raw_dir=tmp_path, dry_run=False)
    scen = FioScenarioSpec(
        kind="fio",
        name="cache-drop-test",
        fio_global=FioGlobal(runtime=5, ramp_time=1, size="100G"),
        jobs=[FioJob(name="j1", rw="write", bs="1M")],
        drop_caches_between_jobs=True,
    )
    list(FioRunner().run_jobs(scen, TARGET, NODE, ctx))

    # 3 ssh invocations per job: sudo -n sync, sudo -n tee, fio.
    assert len(captured_argvs) == 3
    sync_call, tee_call, fio_call = captured_argvs
    # Neither sync nor tee should request a tty: NOPASSWD means no prompt to answer.
    assert "-t" not in sync_call
    assert "-t" not in tee_call
    assert "sudo -n sync" in sync_call[-1]
    assert "sudo -n tee /proc/sys/vm/drop_caches" in tee_call[-1]
    # fio invocation is last.
    assert any("fio --output-format=json+" in arg for arg in fio_call)


def test_run_jobs_yields_failed_outcome_when_cache_drop_fails(tmp_path, monkeypatch):
    """A cache-drop failure (e.g. NOPASSWD missing) yields a failed JobOutcome,
    not an uncaught exception that would kill the whole campaign."""
    def fake_run(argv, capture_output, text, timeout=None, input=None):
        # Mimic `sudo -n sync` failing with the canonical NOPASSWD-missing message.
        if "sudo -n" in argv[-1]:
            return MagicMock(
                returncode=1, stdout="",
                stderr="sudo: a password is required\n",
            )
        return MagicMock(returncode=0, stdout=SAMPLE_JSON, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = RunContext(run_id="r1", raw_dir=tmp_path, dry_run=False)
    scen = FioScenarioSpec(
        kind="fio",
        name="cache-drop-fail-test",
        fio_global=FioGlobal(runtime=5, ramp_time=1, size="100G"),
        jobs=[FioJob(name="j1", rw="write", bs="1M")],
        drop_caches_between_jobs=True,
    )
    outcomes = list(FioRunner().run_jobs(scen, TARGET, NODE, ctx))
    assert len(outcomes) == 1
    assert outcomes[0].status == "failed"
    assert "cache drop failed" in (outcomes[0].error or "")
    assert "NOPASSWD" in (outcomes[0].error or "")


def test_run_jobs_handles_remote_failure(tmp_path, monkeypatch):
    def fake_run(argv, capture_output, text, timeout=None, input=None):
        return MagicMock(returncode=1, stdout="", stderr="fio: cannot write\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ctx = RunContext(run_id="r1", raw_dir=tmp_path, dry_run=False)
    scen = _scenario([FioJob(name="j1", rw="write", bs="1M")])
    outcomes = list(FioRunner().run_jobs(scen, TARGET, NODE, ctx))
    assert outcomes[0].status == "failed"
    assert "fio: cannot write" in (outcomes[0].error or "")


def test_prepare_refuses_to_shrink_existing_testfile(tmp_path, monkeypatch):
    """If a smaller testfile exists, refuse rather than silently extend (007 gotcha)."""
    def fake_run(argv, capture_output, text, timeout=None, input=None):
        if "stat -c %s" in argv[-1]:
            # 1 GiB existing file; scenario asks for 100 GiB.
            return MagicMock(returncode=0, stdout=str(1024**3) + "\n", stderr="")
        return MagicMock(returncode=0, stdout="probe-ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = RunContext(run_id="r1", raw_dir=tmp_path, dry_run=False)
    scen = _scenario([FioJob(name="j1", rw="write", bs="1M")])
    with pytest.raises(RuntimeError, match="Aborting rather than silently extending"):
        FioRunner().prepare(scen, TARGET, NODE, ctx)


def test_prepare_prefills_when_no_testfile_exists(tmp_path, monkeypatch):
    """Fresh target → prepare writes the full testfile (no fallocate shortcut)."""
    captured: list[list[str]] = []

    def fake_run(argv, capture_output, text, timeout=None, input=None):
        captured.append(argv)
        last = argv[-1]
        if "stat -c %s" in last:
            return MagicMock(returncode=0, stdout="NONE\n", stderr="")
        return MagicMock(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = RunContext(run_id="r1", raw_dir=tmp_path, dry_run=False)
    scen = _scenario([FioJob(name="j1", rw="write", bs="1M")])
    FioRunner().prepare(scen, TARGET, NODE, ctx)

    cmds = [a[-1] for a in captured]
    prefill = [c for c in cmds if "fio --name=prefill" in c]
    assert len(prefill) == 1
    assert "--rw=write" in prefill[0]
    assert "--direct=1" in prefill[0]
    assert "--ioengine=libaio" in prefill[0]
    assert "touch" in prefill[0]
    assert ".prefilled" in prefill[0]


def test_prepare_reprefills_when_marker_absent(tmp_path, monkeypatch):
    """A big-enough testfile WITHOUT the .prefilled marker → re-prefill in place.

    Catches the 'leftover from fallocate-only path' case where the file's
    ext4 extents may still be unwritten (zero-fast-path inflates reads).
    """
    captured: list[list[str]] = []

    def fake_run(argv, capture_output, text, timeout=None, input=None):
        captured.append(argv)
        last = argv[-1]
        if "stat -c %s" in last:
            # File exists at full required size.
            return MagicMock(returncode=0, stdout=str(200 * 1024**3) + "\n", stderr="")
        if "test -f" in last and ".prefilled" in last:
            return MagicMock(returncode=0, stdout="NO\n", stderr="")
        return MagicMock(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = RunContext(run_id="r1", raw_dir=tmp_path, dry_run=False)
    scen = _scenario([FioJob(name="j1", rw="write", bs="1M")])
    FioRunner().prepare(scen, TARGET, NODE, ctx)

    prefill = [a[-1] for a in captured if "fio --name=prefill" in a[-1]]
    assert len(prefill) == 1, "should re-prefill in place when marker is absent"


def test_prepare_skips_when_marker_present_and_file_big_enough(tmp_path, monkeypatch):
    """Healthy state: testfile exists AND .prefilled marker → skip prefill."""
    captured: list[list[str]] = []

    def fake_run(argv, capture_output, text, timeout=None, input=None):
        captured.append(argv)
        last = argv[-1]
        if "stat -c %s" in last:
            return MagicMock(returncode=0, stdout=str(200 * 1024**3) + "\n", stderr="")
        if "test -f" in last and ".prefilled" in last:
            return MagicMock(returncode=0, stdout="YES\n", stderr="")
        return MagicMock(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = RunContext(run_id="r1", raw_dir=tmp_path, dry_run=False)
    scen = _scenario([FioJob(name="j1", rw="write", bs="1M")])
    FioRunner().prepare(scen, TARGET, NODE, ctx)

    prefill = [a[-1] for a in captured if "fio --name=prefill" in a[-1]]
    assert prefill == [], "should not prefill when marker is present"


def test_cleanup_removes_testfile_and_marker(tmp_path, monkeypatch):
    captured: list[list[str]] = []

    def fake_run(argv, capture_output, text, timeout=None, input=None):
        captured.append(argv)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = RunContext(run_id="r1", raw_dir=tmp_path, dry_run=False)
    scen = _scenario([FioJob(name="j1", rw="write", bs="1M")])
    FioRunner().cleanup(scen, TARGET, NODE, ctx)
    assert len(captured) == 1
    cmd = captured[0][-1]
    assert "rm -f" in cmd
    assert "testfile" in cmd
    assert "testfile.prefilled" in cmd
