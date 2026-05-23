"""fio Runner (Layer 2).

Workflow per scenario:

  prepare:
    - mkdir -p target.path
    - capture env (fio --version, uname, df) into ctx.raw_dir/env-<node>.txt
    - if testfile missing or smaller than scenario size: fallocate
    - if testfile present and >= scenario size: skip (saves ~3 min on Gen5)

  run_jobs (per FioJob, in declaration order):
    - if scenario.drop_caches_between_jobs: sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'
    - render the [global] + [job] fio config from scenario + job
    - pipe rendered config via ssh stdin to: `cat > /tmp/harness-<job>.fio && fio --output-format=json+ <path>`
    - capture stdout JSON to ctx.raw_dir/<scenario>/<job>.json
    - parse via parsers/fio.py, attach metrics to JobOutcome

  cleanup:
    - rm target.path/testfile (the orchestrator decides whether to call this)
"""
from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import ClassVar, Iterator

import jinja2

from harness.config import FioJob, FioScenarioSpec, Node, ScenarioSpec, StorageTarget, parse_size_gib
from harness.parsers.fio import parse_fio_json
from harness.runners.base import JobOutcome, Runner, RunContext
from harness.ssh import RemoteError, df_free_bytes, run_remote, run_remote_sudo

TESTFILE_NAME = "testfile"  # one file per target; reused across jobs


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
        existing_gib = _existing_testfile_gib(node, testfile_path)
        if existing_gib is None:
            self._allocate_testfile(node, testfile_path, scenario.fio_global.size)
        elif existing_gib < required_gib:
            raise RuntimeError(
                f"existing testfile at {node.name}:{testfile_path} is {existing_gib} GiB, "
                f"scenario {scenario.name!r} requires {required_gib} GiB. "
                f"Aborting rather than silently extending (see experiment 007 gotcha)."
            )
        # else: testfile big enough, reuse.

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
        # -f tolerates already-absent file (idempotent).
        run_remote(
            node,
            f"rm -f {shlex.quote(testfile_path)}",
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

    def _allocate_testfile(self, node: Node, path: str, size: str) -> None:
        # fallocate is fast on ext4/xfs (~3 min for 2 TiB on Gen5). Falls back to
        # truncate-then-write if fallocate isn't supported by the filesystem.
        cmd = (
            f"fallocate -l {shlex.quote(size)} {shlex.quote(path)} "
            f"|| truncate -s {shlex.quote(size)} {shlex.quote(path)}"
        )
        run_remote(node, cmd, timeout=1200)

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
