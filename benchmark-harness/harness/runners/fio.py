"""fio Runner (Layer 2).

Workflow per scenario:

  prepare:
    - mkdir -p target.path
    - capture env (fio --version, uname, df) into ctx.raw_dir/env-<node>.txt
    - if testfile missing or smaller than scenario size: prefill (write full
      file with zeros via fio, then touch a .prefilled marker)
    - if testfile present and >= scenario size BUT marker absent: prefill
      again (the file's ext4 extents may still be in the "unwritten" state,
      which makes O_DIRECT reads return zeros from a kernel zero-page without
      ever issuing I/O to the drive — see methodology note below)
    - if testfile present, >= scenario size, AND marker present: skip
      (saves ~3-5 min on Gen5)

  run_jobs (per FioJob, in declaration order):
    - if scenario.drop_caches_between_jobs: sudo -n sync + sudo -n tee /proc/sys/vm/drop_caches
    - render the [global] + [job] fio config from scenario + job
    - pipe rendered config via ssh stdin to:
        `cat > /tmp/harness-<job>.fio && fio --output-format=json+ <path>`
    - capture stdout JSON to ctx.raw_dir/<scenario>/<job>.json
    - parse via parsers/fio.py, attach metrics to JobOutcome

  cleanup:
    - rm target.path/testfile and target.path/testfile.prefilled (the
      orchestrator decides whether to call this)

Methodology note — why we prefill instead of just `fallocate`:

  `fallocate -l SIZE FILE` reserves disk blocks but does NOT write data to
  them. ext4 marks these extents "unwritten". When a process reads from an
  unwritten extent — even with O_DIRECT — ext4 returns zeros from a kernel
  zero-page without issuing I/O to the drive. This optimization is correct
  but breaks read benchmarks: the harness would report read bandwidths far
  above the drive's PCIe link ceiling because most "reads" never touched
  the drive. Prefilling the file with one full-size sequential write forces
  all extents into the "written" state. After that, reads exercise the
  drive end-to-end. Cost: ~3-5 min one-time per testfile (comparable to
  fallocate); amortized to zero across repeated runs that reuse the file.
"""
from __future__ import annotations

import shlex
import time
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import jinja2

from harness.config import (
    FioJob,
    FioScenarioSpec,
    Node,
    ScenarioSpec,
    StorageTarget,
    parse_size_gib,
)
from harness.parsers.fio import parse_fio_json
from harness.runners.base import JobOutcome, RunContext, Runner
from harness.ssh import RemoteError, run_remote

TESTFILE_NAME = "testfile"  # one file per target; reused across jobs
PREFILLED_MARKER_SUFFIX = ".prefilled"  # sibling marker indicating full write completed


_FIO_TEMPLATE = jinja2.Template(
    """[global]
ioengine={{ g.ioengine }}
direct={{ g.direct }}
time_based={{ g.time_based }}
runtime={{ g.runtime }}
ramp_time={{ g.ramp_time }}
end_fsync={{ g.end_fsync }}
group_reporting={{ g.group_reporting }}
directory={{ target_dir }}
filename={{ testfile_name }}
size={{ g.size }}

[{{ job.name }}]
rw={{ job.rw }}
bs={{ job.bs }}
numjobs={{ job.numjobs }}
iodepth={{ job.iodepth }}
{% if job.offset_increment -%}
offset_increment={{ job.offset_increment }}
{% endif -%}
{% if job.size -%}
size={{ job.size }}
{% endif -%}
{% if job.rwmixread is not none -%}
rwmixread={{ job.rwmixread }}
{% endif -%}
""",
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def render_fio_config(
    scenario: FioScenarioSpec, job: FioJob, target_dir: str, testfile_name: str = TESTFILE_NAME
) -> str:
    """Render a one-job .fio config (the [global] block + a single [job])."""
    return _FIO_TEMPLATE.render(
        g=scenario.fio_global,
        job=job,
        target_dir=target_dir,
        testfile_name=testfile_name,
    )


class FioRunner(Runner):
    kind: ClassVar[str] = "fio"

    def prepare(
        self,
        scenario: ScenarioSpec,
        target: StorageTarget,
        node: Node,
        ctx: RunContext,
    ) -> None:
        if not isinstance(scenario, FioScenarioSpec):
            raise TypeError(f"FioRunner cannot prepare scenario kind={type(scenario).__name__}")

        target_dir = str(target.path)
        testfile_path = _testfile_path(target_dir)
        required_gib = parse_size_gib(scenario.fio_global.size)

        # 1. ensure target dir exists.
        run_remote(node, f"mkdir -p {shlex.quote(target_dir)}", dry_run=ctx.dry_run, timeout=30)

        # 2. capture env snapshot.
        env_text = self._capture_env(node, target_dir, ctx)
        env_log = ctx.raw_dir / f"env-{node.name}.txt"
        env_log.parent.mkdir(parents=True, exist_ok=True)
        env_log.write_text(env_text)

        # 3. handle testfile.
        if ctx.dry_run:
            return
        marker_path = _marker_path(testfile_path)
        existing_gib = _existing_testfile_gib(node, testfile_path)
        prefilled = existing_gib is not None and _check_prefilled(node, marker_path)
        if existing_gib is None:
            self._allocate_testfile(
                node, testfile_path, marker_path, scenario.fio_global.size
            )
        elif existing_gib < required_gib:
            raise RuntimeError(
                f"existing testfile at {node.name}:{testfile_path} is {existing_gib} GiB, "
                f"scenario {scenario.name!r} requires {required_gib} GiB. "
                f"Aborting rather than silently extending (see experiment 007 gotcha)."
            )
        elif not prefilled:
            # File exists at the right size but the .prefilled marker is missing.
            # The file's extents may still be unwritten (e.g. from a previous
            # fallocate-only allocation, or a crashed prior run). Re-prefill in
            # place to force all extents into the "written" state. This rewrites
            # the same on-disk space — no new allocation, no df-space change.
            self._allocate_testfile(
                node, testfile_path, marker_path, scenario.fio_global.size
            )
        # else: testfile big enough AND prefilled, reuse.

    def run_jobs(
        self,
        scenario: ScenarioSpec,
        target: StorageTarget,
        node: Node,
        ctx: RunContext,
    ) -> Iterator[JobOutcome]:
        if not isinstance(scenario, FioScenarioSpec):
            raise TypeError(f"FioRunner cannot run scenario kind={type(scenario).__name__}")

        scenario_raw_dir = ctx.raw_dir / scenario.name
        scenario_raw_dir.mkdir(parents=True, exist_ok=True)
        target_dir = str(target.path)

        for job in scenario.jobs:
            if scenario.drop_caches_between_jobs:
                try:
                    self._drop_caches(node, ctx)
                except RemoteError as e:
                    # Don't kill the whole campaign on a cache-drop failure;
                    # yield a failed outcome so the orchestrator records it
                    # and moves on. Common cause: NOPASSWD sudoers not set.
                    yield JobOutcome(
                        job_name=job.name,
                        status="failed",
                        error=(
                            f"cache drop failed (sudo NOPASSWD likely missing for "
                            f"sync/tee /proc/sys/vm/drop_caches): "
                            f"{e.result.stderr.strip()[:300]}"
                        ),
                    )
                    continue

            fio_config = render_fio_config(scenario, job, target_dir)
            remote_fio = f"/tmp/harness-{scenario.name}-{job.name}.fio"
            yield self._run_one_job(
                node=node,
                job_name=job.name,
                fio_config=fio_config,
                remote_fio_path=remote_fio,
                local_json_path=scenario_raw_dir / f"{job.name}.json",
                timeout=scenario.timeout_seconds,
                ctx=ctx,
            )

    def cleanup(
        self,
        scenario: ScenarioSpec,
        target: StorageTarget,
        node: Node,
        ctx: RunContext,
    ) -> None:
        testfile_path = _testfile_path(str(target.path))
        marker_path = _marker_path(testfile_path)
        # -f tolerates already-absent file (idempotent).
        run_remote(
            node,
            f"rm -f {shlex.quote(testfile_path)} {shlex.quote(marker_path)}",
            dry_run=ctx.dry_run,
            timeout=30,
            check=False,
        )

    # ---- helpers ------------------------------------------------------------

    def _capture_env(self, node: Node, target_dir: str, ctx: RunContext) -> str:
        if ctx.dry_run:
            return f"# dry-run env capture for {node.name}\n"
        # Best-effort: any individual probe failing should not abort prepare.
        parts = []
        for label, cmd in [
            ("fio_version", "fio --version 2>&1 | head -n 1"),
            ("uname", "uname -a"),
            ("df", f"df -h {shlex.quote(target_dir)} 2>&1"),
            ("date_utc", "date -u +%Y-%m-%dT%H:%M:%SZ"),
        ]:
            try:
                r = run_remote(node, cmd, check=False, timeout=15)
                parts.append(f"## {label}\n{r.stdout.strip()}\n")
            except RemoteError as e:
                parts.append(f"## {label} (FAILED)\n{e}\n")
        return "\n".join(parts)

    def _allocate_testfile(
        self, node: Node, path: str, marker_path: str, size: str
    ) -> None:
        # Pre-fill the testfile with one full-size sequential write via fio. This
        # forces every ext4 extent into the "written" state so subsequent O_DIRECT
        # reads exercise the drive (and not the kernel's zero-page fast path for
        # unwritten extents). See the module docstring "Methodology note" for the
        # full reasoning. Cost: ~3-5 min for 2 TiB on Gen5 NVMe.
        #
        # On success, a sibling .prefilled marker is created so the next run can
        # skip re-prefill. If fio fails, the marker is NOT created — the next run
        # will retry the prefill.
        cmd = (
            f"fio --name=prefill --filename={shlex.quote(path)} "
            f"--rw=write --bs=1M --size={shlex.quote(size)} "
            f"--ioengine=libaio --direct=1 --iodepth=16 "
            f"--end_fsync=1 --output-format=minimal > /dev/null "
            f"&& touch {shlex.quote(marker_path)}"
        )
        run_remote(node, cmd, timeout=1800)

    def _drop_caches(self, node: Node, ctx: RunContext) -> None:
        # Two-step form so each sudo invocation can be whitelisted in
        # /etc/sudoers.d/ with NOPASSWD on a specific command path. Avoids
        # fragile sh -c quoting in sudoers rules.
        #
        # Required sudoers (one-time setup per node):
        #   sparks ALL=(root) NOPASSWD: /usr/bin/sync, /usr/bin/tee /proc/sys/vm/drop_caches
        #
        # `sudo -n` is non-interactive: fails immediately if NOPASSWD isn't
        # configured, instead of timing out at 60s waiting for a password
        # prompt that has nowhere to be typed.
        run_remote(node, "sudo -n sync", dry_run=ctx.dry_run, timeout=30)
        run_remote(
            node,
            "echo 3 | sudo -n tee /proc/sys/vm/drop_caches > /dev/null",
            dry_run=ctx.dry_run,
            timeout=30,
        )

    def _run_one_job(
        self,
        *,
        node: Node,
        job_name: str,
        fio_config: str,
        remote_fio_path: str,
        local_json_path: Path,
        timeout: int | None,
        ctx: RunContext,
    ) -> JobOutcome:
        # Pipe config via stdin, run fio, capture JSON via stdout.
        cmd = (
            f"cat > {shlex.quote(remote_fio_path)} && "
            f"fio --output-format=json+ {shlex.quote(remote_fio_path)} && "
            f"rm -f {shlex.quote(remote_fio_path)}"
        )
        t0 = time.monotonic()
        if ctx.dry_run:
            local_json_path.parent.mkdir(parents=True, exist_ok=True)
            local_json_path.write_text("{}\n")  # placeholder, no metrics
            return JobOutcome(
                job_name=job_name,
                status="ok",
                raw_json_path=local_json_path,
                duration_seconds=0.0,
            )
        try:
            result = run_remote(node, cmd, stdin=fio_config, timeout=timeout, check=True)
        except RemoteError as e:
            return JobOutcome(
                job_name=job_name,
                status="timed_out" if e.result.exit_code == 124 else "failed",
                duration_seconds=time.monotonic() - t0,
                error=e.result.stderr.strip()[:1000] or str(e),
            )

        local_json_path.parent.mkdir(parents=True, exist_ok=True)
        local_json_path.write_text(result.stdout)
        try:
            metrics = parse_fio_json(local_json_path)
        except (ValueError, KeyError) as e:
            return JobOutcome(
                job_name=job_name,
                status="failed",
                raw_json_path=local_json_path,
                duration_seconds=time.monotonic() - t0,
                error=f"parse error: {e}",
            )
        return JobOutcome(
            job_name=job_name,
            status="ok",
            raw_json_path=local_json_path,
            duration_seconds=time.monotonic() - t0,
            metrics=metrics,
        )


def _testfile_path(target_dir: str) -> str:
    base = target_dir.rstrip("/")
    return f"{base}/{TESTFILE_NAME}"


def _marker_path(testfile_path: str) -> str:
    return f"{testfile_path}{PREFILLED_MARKER_SUFFIX}"


def _existing_testfile_gib(node: Node, path: str) -> int | None:
    """Return testfile size in GiB if it exists, else None."""
    r = run_remote(
        node,
        f"stat -c %s {shlex.quote(path)} 2>/dev/null || echo NONE",
        check=False,
        timeout=15,
    )
    out = r.stdout.strip()
    if not out or out == "NONE":
        return None
    try:
        size_bytes = int(out)
    except ValueError:
        return None
    return size_bytes // (1024**3)


def _check_prefilled(node: Node, marker_path: str) -> bool:
    """True if the .prefilled marker exists on ``node``."""
    r = run_remote(
        node,
        f"test -f {shlex.quote(marker_path)} && echo YES || echo NO",
        check=False,
        timeout=15,
    )
    return r.stdout.strip() == "YES"
