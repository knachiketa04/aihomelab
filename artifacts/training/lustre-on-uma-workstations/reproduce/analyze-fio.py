#!/usr/bin/env python3
"""analyze-fio.py — parse pillar-*.log files and produce comparison tables.

Expects logs from all three pillars in one directory. For Pillar 3, both
host1 and host2 logs must be present in the same directory to compute the
aggregate (sum of per-host throughput). Gather them with scp first:

    scp <host1>:${EXP_ROOT}/logs/pillar*.log ./logs/
    scp <host2>:${EXP_ROOT}/logs/pillar*.log ./logs/
    ./analyze-fio.py --log-dir ./logs/
"""

import argparse
import re
from pathlib import Path

# fio summary line: "  READ: bw=2264MiB/s (2374MB/s), ..."  or  "  WRITE: ..."
# Note: fio uses lowercase 'kB' for small numbers (e.g., "1477kB/s") — regex
# is case-insensitive on the unit suffix to handle both kB and KB.
RE_BW = re.compile(r"^\s*(READ|WRITE):\s*bw=([\d.]+)([KkMG]i?B)/s\s*\(([\d.]+)([KkMG]B)/s\)")

UNIT_MB = {
    "B":   1e-6,
    "KB":  1e-3, "kB": 1e-3,
    "MB":  1.0,
    "GB":  1e3,
    "KiB": 1.024e-3,
    "MiB": 1.04858,
    "GiB": 1073.74,
}

TESTS = [
    ("seq-write-1m", "WRITE", "1 MiB seq write"),
    ("seq-read-1m",  "READ",  "1 MiB seq read"),
    ("seq-write-64k","WRITE", "64 KiB seq write"),
    ("rand-rw-4k",   "READ",  "4 KiB random read"),
    ("rand-rw-4k",   "WRITE", "4 KiB random write"),
]


def parse_log(path: Path) -> dict:
    """Return dict keyed by READ|WRITE → MB/s float, or {} if no match."""
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(errors="ignore").splitlines():
        m = RE_BW.match(line)
        if not m:
            continue
        op = m.group(1)
        # Use the decimal-MB/s value (group 4–5) for consistency
        val, unit = float(m.group(4)), m.group(5)
        out[op] = val * UNIT_MB.get(unit, 1.0)
    return out


def fmt(mb_per_s: float | None) -> str:
    if mb_per_s is None:
        return "-"
    if mb_per_s >= 1000:
        return f"{mb_per_s/1000:.2f} GB/s"
    return f"{mb_per_s:.0f} MB/s"


def pillar_table(label: str, log_dir: Path, pillar: str, client: str | None = None):
    print(f"\n=== {label} ===")
    print(f"{'Test':<22} {'Result':>14}")
    print("-" * 38)
    for name, op, desc in TESTS:
        prefix = f"pillar{pillar}-"
        if client:
            prefix += f"{client}-"
        log = log_dir / f"{prefix}{name}.log"
        bw = parse_log(log).get(op)
        print(f"{desc:<22} {fmt(bw):>14}")


def pillar3_aggregate(log_dir: Path):
    print(f"\n=== Pillar 3 (concurrent, 2-way striped) ===")
    print(f"{'Test':<22} {'host1':>12} {'host2':>12} {'aggregate':>14}")
    print("-" * 64)
    for name, op, desc in TESTS:
        h1 = parse_log(log_dir / f"pillar3-host1-{name}.log").get(op)
        h2 = parse_log(log_dir / f"pillar3-host2-{name}.log").get(op)
        agg = (h1 or 0) + (h2 or 0) if (h1 is not None and h2 is not None) else None
        print(f"{desc:<22} {fmt(h1):>12} {fmt(h2):>12} {fmt(agg):>14}")


def cross_compare(log_dir: Path):
    print(f"\n=== Cross-pillar (1 MiB seq write / read) ===")
    p1w = parse_log(log_dir / "pillar1-seq-write-1m.log").get("WRITE")
    p1r = parse_log(log_dir / "pillar1-seq-read-1m.log").get("READ")
    p2w = parse_log(log_dir / "pillar2-seq-write-1m.log").get("WRITE")
    p2r = parse_log(log_dir / "pillar2-seq-read-1m.log").get("READ")
    h1w = parse_log(log_dir / "pillar3-host1-seq-write-1m.log").get("WRITE")
    h2w = parse_log(log_dir / "pillar3-host2-seq-write-1m.log").get("WRITE")
    h1r = parse_log(log_dir / "pillar3-host1-seq-read-1m.log").get("READ")
    h2r = parse_log(log_dir / "pillar3-host2-seq-read-1m.log").get("READ")
    p3w = (h1w or 0) + (h2w or 0) if (h1w and h2w) else None
    p3r = (h1r or 0) + (h2r or 0) if (h1r and h2r) else None
    print(f"{'Pillar':<32} {'write':>14} {'read':>14}")
    print("-" * 62)
    print(f"{'1. single-node loopback':<32} {fmt(p1w):>14} {fmt(p1r):>14}")
    print(f"{'2. cross-node single-OST':<32} {fmt(p2w):>14} {fmt(p2r):>14}")
    print(f"{'3. distributed concurrent (sum)':<32} {fmt(p3w):>14} {fmt(p3r):>14}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-dir", type=Path, default=Path("./logs"),
                    help="Directory containing pillar*-*.log files (default: ./logs)")
    args = ap.parse_args()

    if not args.log_dir.is_dir():
        ap.error(f"log dir not found: {args.log_dir}")

    pillar_table("Pillar 1 (single-node loopback)", args.log_dir, "1")
    pillar_table("Pillar 2 (cross-node single-OST)", args.log_dir, "2")
    pillar3_aggregate(args.log_dir)
    cross_compare(args.log_dir)


if __name__ == "__main__":
    main()
