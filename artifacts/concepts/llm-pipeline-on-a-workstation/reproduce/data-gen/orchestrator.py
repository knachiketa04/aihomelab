#!/usr/bin/env python3
"""
orchestrator.py — the overnight ~20K-pair teacher run.

Drives data-parallel Qwen3-32B generation across node0 + node1 in chunks,
cold-restarting vLLM between chunks to bound memory accumulation (validated
in Phase 4: node0 NVRM OOM after ~500 requests on default config). Locks
the stability config: --enforce-eager + --max-num-seqs 32 + --gpu-mem-util 0.80.

RUN LOCATION: node0, inside a tmux session, so it survives SSH disconnect.
node0 is reached via local subprocess; node1 via ssh.

Pre-reqs:
  - vllm-node0 + vllm-node1 tmux sessions are NOT running when launched
  - a NOPASSWD sudoers entry for sync + drop_caches is installed on both nodes
  - passwordless ssh from node0 to node1 (key auth)
  - generate.py supports --skip-completed + --max-requests (verify before launch)
  - templates.json + prompts-node0.jsonl + prompts-node1.jsonl deployed

Outputs:
  - progress.txt: human-readable timestamped log of every state transition
  - all-node0.jsonl + all-node1.jsonl: grow incrementally, append-only
  - vllm-<node>-chunkNN.log, gen-<node>-chunkNN.log: per-chunk logs

Bail-out:
  - 2 consecutive chunks where either node added < (chunk_size / 2) new rows
  - shared-filesystem free < 10% before any chunk
"""

import argparse
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ----- Locked configuration -----
EXP_ROOT = Path("/data")
SCRIPTS = EXP_ROOT / "scripts"
MODEL_DIR = EXP_ROOT / "hf-cache/Qwen3-32B"
RESULTS = EXP_ROOT / "results/phase5-full-generation"

NODES = {
    "node1": {
        "ssh": "USER@node1",  # set USER to your ssh login on node 1
        "python": str(EXP_ROOT / "venv-node1/bin/python"),
        "vllm": str(EXP_ROOT / "venv-node1/bin/vllm"),
        # Single-node mode: node1 chews through the FULL 20K prompt list.
        # node0 is left running but idle (it hosts the shared filesystem metadata + first object-store target).
        "prompts": str(EXP_ROOT / "prompts/all-prompts.jsonl"),
        "output": str(EXP_ROOT / "outputs/all-node1.jsonl"),
    },
}

VLLM_FLAGS = " ".join([
    "--tensor-parallel-size 1",
    "--port 8000",
    "--gpu-memory-utilization 0.80",
    "--max-num-seqs 32",
    "--max-num-batched-tokens 8192",
    "--enable-prefix-caching",
    "--enforce-eager",
])

TMUX_SESSION = "vllm-chunk"
STARTUP_TIMEOUT_SEC = 600
LUSTRE_MIN_FREE_PCT = 10
MAX_CONSECUTIVE_FAILS = 2


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def progress_write(progress_path: Path, msg: str) -> None:
    line = f"{now_iso()}  {msg}"
    print(line, flush=True)
    with progress_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_local(cmd: str, timeout=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def on_node(node_key: str, command: str, timeout=None) -> subprocess.CompletedProcess:
    """Run command locally for node0 (ssh is None) or via ssh for node1."""
    node = NODES[node_key]
    if node["ssh"] is None:
        return run_local(command, timeout=timeout)
    quoted = shlex.quote(command)
    return run_local(
        f"ssh -o BatchMode=yes -o ConnectTimeout=10 {node['ssh']} {quoted}",
        timeout=timeout,
    )


def popen_on_node(node_key: str, command: str) -> subprocess.Popen:
    """Long-running version (for generate.py); returns Popen for parallel waits."""
    node = NODES[node_key]
    if node["ssh"] is None:
        return subprocess.Popen(command, shell=True)
    quoted = shlex.quote(command)
    return subprocess.Popen(
        f"ssh -o BatchMode=yes {node['ssh']} {quoted}",
        shell=True,
    )


def start_vllm(node_key: str, chunk_id: int) -> str:
    """Cold-start vLLM in a tmux session on the given node. Returns log path."""
    node = NODES[node_key]
    log_path = str(RESULTS / f"vllm-{node_key}-chunk{chunk_id:02d}.log")
    # Defensive: kill ANY pre-existing vllm process / tmux session that could hold port 8000
    on_node(node_key, f"tmux kill-session -t {TMUX_SESSION} 2>/dev/null || true")
    on_node(node_key, "tmux kill-session -t vllm-node0 2>/dev/null || true")
    on_node(node_key, "tmux kill-session -t vllm-node1 2>/dev/null || true")
    on_node(node_key, "pkill -9 -f 'vllm serve' 2>/dev/null || true")
    time.sleep(2)
    on_node(node_key, "sudo sync && sudo tee /proc/sys/vm/drop_caches <<< 3 >/dev/null")
    # PATH must include venv/bin so flashinfer's JIT can find `ninja` for first-run kernel compile
    venv_bin = node['vllm'].rsplit('/', 1)[0]
    serve_cmd = (
        f"PATH={venv_bin}:/usr/local/bin:/usr/bin:/bin "
        f"{node['vllm']} serve {MODEL_DIR} {VLLM_FLAGS} > {log_path} 2>&1"
    )
    on_node(node_key, f"tmux new -d -s {TMUX_SESSION} {shlex.quote(serve_cmd)}")
    return log_path


def wait_for_vllm(node_key: str, log_path: str, timeout: int = STARTUP_TIMEOUT_SEC) -> bool:
    deadline = time.time() + timeout
    poll = f"grep -q 'Application startup complete' {log_path} 2>/dev/null"
    while time.time() < deadline:
        if on_node(node_key, poll).returncode == 0:
            return True
        time.sleep(5)
    return False


def stop_vllm(node_key: str) -> None:
    on_node(node_key, f"tmux kill-session -t {TMUX_SESSION} 2>/dev/null || true")


def health_check(node_key: str) -> bool:
    cmd = "curl -s -o /dev/null -w '%{http_code}' --max-time 30 http://localhost:8000/v1/models"
    res = on_node(node_key, cmd, timeout=40)
    return res.returncode == 0 and res.stdout.strip() == "200"


def lustre_free_pct() -> int:
    res = run_local("df --output=pcent /mnt/shared | tail -1 | tr -dc '0-9'")
    if res.returncode != 0 or not res.stdout.strip():
        return -1
    return 100 - int(res.stdout.strip())


def count_lines(path: str) -> int:
    res = run_local(f"[ -f {path} ] && wc -l < {path} || echo 0")
    try:
        return int(res.stdout.strip() or "0")
    except ValueError:
        return 0


def run_generate(node_key: str, chunk_id: int, max_requests: int) -> subprocess.Popen:
    node = NODES[node_key]
    log_path = str(RESULTS / f"gen-{node_key}-chunk{chunk_id:02d}.log")
    cmd = (
        f"{node['python']} {SCRIPTS}/generate.py "
        f"--prompts {node['prompts']} "
        f"--templates {SCRIPTS}/templates.json "
        f"--vllm-url http://localhost:8000/v1/chat/completions "
        f"--model {MODEL_DIR} "
        f"--concurrency 32 "
        f"--max-requests {max_requests} "
        f"--skip-completed "
        f"--out {node['output']} "
        f"2> {log_path}"
    )
    return popen_on_node(node_key, cmd)


def run_chunk(chunk_id: int, max_requests: int, progress: Path) -> bool:
    progress_write(progress, f"CHUNK {chunk_id:02d} START max_per_node={max_requests}")

    log_paths = {n: start_vllm(n, chunk_id) for n in NODES}
    progress_write(progress, f"CHUNK {chunk_id:02d} vllm-launching, waiting startup")

    # Parallel wait so a failure on one node doesn't delay diagnosis by the full timeout
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(NODES)) as ex:
        futures = {n: ex.submit(wait_for_vllm, n, log_paths[n]) for n in NODES}
        ready = {n: f.result() for n, f in futures.items()}
    if not all(ready.values()):
        progress_write(progress, f"CHUNK {chunk_id:02d} FAIL vllm startup ready={ready}")
        for n in NODES:
            stop_vllm(n)
        return False

    healthy = {n: health_check(n) for n in NODES}
    if not all(healthy.values()):
        progress_write(progress, f"CHUNK {chunk_id:02d} FAIL health-check healthy={healthy}")
        for n in NODES:
            stop_vllm(n)
        return False

    progress_write(progress, f"CHUNK {chunk_id:02d} vllm-ready, launching generate")

    procs = {n: run_generate(n, chunk_id, max_requests) for n in NODES}
    for n, p in procs.items():
        p.wait()

    for n in NODES:
        stop_vllm(n)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk-size", type=int, default=1000)
    ap.add_argument("--total-per-node", type=int, default=10000)
    args = ap.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    progress = RESULTS / "progress.txt"

    progress_write(progress, f"=== START total_per_node={args.total_per_node} chunk_size={args.chunk_size} ===")

    free = lustre_free_pct()
    progress_write(progress, f"Lustre free: {free}%")
    if 0 <= free < LUSTRE_MIN_FREE_PCT:
        progress_write(progress, f"ABORT: Lustre below {LUSTRE_MIN_FREE_PCT}% free")
        sys.exit(1)

    n_chunks = (args.total_per_node + args.chunk_size - 1) // args.chunk_size
    consecutive_fails = 0

    for chunk_id in range(n_chunks):
        free = lustre_free_pct()
        if 0 <= free < LUSTRE_MIN_FREE_PCT:
            progress_write(progress, f"ABORT before chunk {chunk_id}: Lustre {free}% free")
            sys.exit(1)

        before = {n: count_lines(NODES[n]["output"]) for n in NODES}
        ok = run_chunk(chunk_id, args.chunk_size, progress)
        after = {n: count_lines(NODES[n]["output"]) for n in NODES}
        delta = {n: after[n] - before[n] for n in NODES}
        progress_write(progress, f"CHUNK {chunk_id:02d} DONE counts={after} delta={delta}")

        min_expected = args.chunk_size // 2
        chunk_failed = (not ok) or any(d < min_expected for d in delta.values())
        if chunk_failed:
            consecutive_fails += 1
            progress_write(progress, f"CHUNK {chunk_id:02d} FLAGGED-FAIL consecutive={consecutive_fails}")
            if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                progress_write(progress, f"BAIL OUT after {consecutive_fails} consecutive failures")
                sys.exit(2)
        else:
            consecutive_fails = 0

    final = {n: count_lines(NODES[n]["output"]) for n in NODES}
    progress_write(progress, f"=== COMPLETE final={final} ===")


if __name__ == "__main__":
    main()
