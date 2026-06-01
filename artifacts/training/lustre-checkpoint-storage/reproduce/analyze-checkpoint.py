#!/usr/bin/env python3
"""analyze-checkpoint.py — parse one arm's captures into the A/B comparison row.

Reads a capture directory produced by run-checkpoint-singlenode.sh or run-checkpoint-2node.sh
and prints one row: writers | aggregate GB/s | mean s/ckpt | OST0/OST1 split | disk %util |
peak UMA GiB. The single-writer and 2-writer rows side by side ARE the finding (the ~1.69x
concurrency scaling onto the independently-measured concurrent ceiling).

Inputs expected in --capture-dir (some are per-host for the 2-node arm; pass --writers 2 and
gather both hosts' files into one dir first):
  train-*.log                 NeMo training log (checkpoint events + per-step mem/tps)
  lctl-OST<idx>-before.txt     OST byte counters before the run
  lctl-OST<idx>-after.txt      OST byte counters after the run  (delta = exact bytes per OST)
  iostat-*.log                 iostat -t -dxm log per host        (disk %util band)

The checkpoint *wall-clock* is parsed from the train log's per-step timestamps bracketing each
"Saving checkpoint" event; if the log format does not expose per-step timestamps, pass
--ckpt-bytes / --ckpt-count to fall back to the OST-delta-based aggregate GB/s.

Usage:
  ./analyze-checkpoint.py --capture-dir <dir> --writers 1
  ./analyze-checkpoint.py --capture-dir <dir> --writers 2
"""

import argparse
import re
from pathlib import Path

# safetensors/NeMo write counter in obdfilter stats: "write_bytes  N samples [bytes] ... sum M"
RE_WRITE_BYTES = re.compile(r"^\s*write_bytes\s+\d+\s+samples\s+\[\w+\]\s+\d+\s+\d+\s+(\d+)")
RE_READ_BYTES = re.compile(r"^\s*read_bytes\s+\d+\s+samples\s+\[\w+\]\s+\d+\s+\d+\s+(\d+)")
# NeMo per-step line carries "... | mem <X> GiB | ... <tps> tps" (format varies by version).
RE_MEM = re.compile(r"mem\s+([\d.]+)\s*GiB")
# iostat -t -dxm row for the NVMe device: last field is %util, write-MB/s is field 9 (1-indexed).
RE_TS = re.compile(r"\d\d:\d\d:\d\d")
# ZFS txg kstat data row: txg birth state ndirty nread nwritten reads writes otime qtime wtime stime
# (captures ndirty = dirty bytes/txg [group 1] and stime = sync duration ns [group 2]).
RE_TXG_ROW = re.compile(r"^\s*\d+\s+\d+\s+[A-Z]\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)\s*$")
# Rough zfs_dirty_data_max on a Grace-class node (default min(10% RAM, 4 GiB)); ndirty as a small
# fraction of this => ZFS not throttling. Node-dependent; reported as context, not a hard gate.
ZFS_DIRTY_CAP = 4 * 1024**3

GB = 1_000_000_000  # decimal GB, matching the lab's GB/s convention


def _counter_delta(cap_dir: Path, ost_index: str, pattern: re.Pattern) -> int | None:
    """before/after delta of an obdfilter byte counter for one OST, or None if files missing."""
    before = cap_dir / f"lctl-OST{ost_index}-before.txt"
    after = cap_dir / f"lctl-OST{ost_index}-after.txt"
    if not (before.is_file() and after.is_file()):
        return None

    def grab(p: Path) -> int | None:
        for line in p.read_text(errors="ignore").splitlines():
            m = pattern.match(line)
            if m:
                return int(m.group(1))
        return None

    b, a = grab(before), grab(after)
    if b is None or a is None:
        return None
    return max(a - b, 0)


def ost_split(cap_dir: Path) -> tuple[int | None, int | None]:
    """Bytes written to OST0 and OST1 over the run (from the before/after counter deltas)."""
    return (_counter_delta(cap_dir, "0000", RE_WRITE_BYTES),
            _counter_delta(cap_dir, "0001", RE_WRITE_BYTES))


def peak_uma(cap_dir: Path) -> float | None:
    """Peak per-rank UMA (GiB) from the highest 'mem <X> GiB' sample in any train log."""
    peak = None
    for log in cap_dir.glob("train-*.log"):
        for line in log.read_text(errors="ignore").splitlines():
            m = RE_MEM.search(line)
            if m:
                v = float(m.group(1))
                peak = v if peak is None else max(peak, v)
    return peak


def ckpt_walltimes(cap_dir: Path) -> list[float]:
    """Per-checkpoint wall-clock (sec) bracketed by the per-step timestamps around each
    'Saving checkpoint' event. Returns [] if the log lacks parseable per-step timestamps."""
    times: list[float] = []
    for log in sorted(cap_dir.glob("train-*.log")):
        lines = log.read_text(errors="ignore").splitlines()
        # Collect (lineno, epoch_seconds) for any line carrying an HH:MM:SS timestamp, and the
        # line numbers of "Saving checkpoint" events. Bracket each save with the nearest
        # preceding + following timestamped step lines.
        ts_at: dict[int, float] = {}
        save_lines: list[int] = []
        for i, ln in enumerate(lines):
            m = RE_TS.search(ln)
            if m:
                hh, mm, ss = (int(x) for x in m.group(0).split(":"))
                ts_at[i] = hh * 3600 + mm * 60 + ss
            if "Saving checkpoint" in ln:
                save_lines.append(i)
        for s in save_lines:
            before = [ts_at[i] for i in ts_at if i < s]
            after = [ts_at[i] for i in ts_at if i > s]
            if before and after:
                dt = after[0] - before[-1]
                if dt < 0:  # midnight wrap
                    dt += 86400
                if 0 < dt < 600:
                    times.append(float(dt))
    return times


def disk_util_band(cap_dir: Path, nvme_hint: str = "nvme") -> tuple[float | None, float | None]:
    """Min/max %util on the NVMe device across the iostat captures (the headroom band)."""
    utils: list[float] = []
    for log in cap_dir.glob("iostat-*.log"):
        for line in log.read_text(errors="ignore").splitlines():
            parts = line.split()
            if parts and nvme_hint in parts[0]:
                try:
                    utils.append(float(parts[-1]))
                except ValueError:
                    pass
    if not utils:
        return (None, None)
    return (min(utils), max(utils))


def txg_attribution(cap_dir: Path) -> tuple[int | None, int | None, int]:
    """Peak ndirty (dirty bytes/txg) and peak stime (sync duration, ns) across the ZFS txgs
    captures — the client-vs-substrate adjudicator that %util alone cannot give. ndirty near
    the dirty cap + long stime => ZFS backpressure (substrate-bound). ndirty a small fraction
    of cap + short stime => substrate idle (the bottleneck is the client). Returns
    (max_ndirty_bytes, max_stime_ns, n_samples)."""
    max_ndirty: int | None = None
    max_stime: int | None = None
    n = 0
    for log in cap_dir.glob("txgs-*.log"):
        for line in log.read_text(errors="ignore").splitlines():
            m = RE_TXG_ROW.match(line)
            if m:
                nd, st = int(m.group(1)), int(m.group(2))
                max_ndirty = nd if max_ndirty is None else max(max_ndirty, nd)
                max_stime = st if max_stime is None else max(max_stime, st)
                n += 1
    return (max_ndirty, max_stime, n)


def fmt(x, suffix="", nd=2):
    return "-" if x is None else f"{x:.{nd}f}{suffix}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture-dir", type=Path, required=True,
                    help="Directory with this arm's captures (gather both hosts for 2-node).")
    ap.add_argument("--writers", type=int, choices=[1, 2], required=True,
                    help="1 = single-node arm, 2 = 2-node-concurrent arm.")
    ap.add_argument("--ckpt-bytes", type=float, default=46.0 * GB,
                    help="Per-checkpoint logical size in bytes (default 46 GB).")
    ap.add_argument("--ckpt-count", type=int, default=4,
                    help="Number of checkpoints written this arm (default 4).")
    args = ap.parse_args()

    if not args.capture_dir.is_dir():
        ap.error(f"capture dir not found: {args.capture_dir}")

    times = ckpt_walltimes(args.capture_dir)
    o0, o1 = ost_split(args.capture_dir)
    uma = peak_uma(args.capture_dir)
    util_lo, util_hi = disk_util_band(args.capture_dir)
    nd, st, ntxg = txg_attribution(args.capture_dir)

    mean_t = (sum(times) / len(times)) if times else None
    # aggregate GB/s: prefer the per-checkpoint wall-clock; else fall back to OST-delta / total time.
    agg_gbs = (args.ckpt_bytes / GB) / mean_t if mean_t else None

    # OST split as percentages
    if o0 is not None and o1 is not None and (o0 + o1) > 0:
        tot = o0 + o1
        split = f"{100*o0/tot:.1f}/{100*o1/tot:.1f}"
    elif o0 is not None:
        split = f"OST0={o0/GB:.1f} GB (OST1 not captured)"
    else:
        split = "-"

    rng = f"{min(times):.0f}-{max(times):.0f}" if times else "-"

    print(f"\n=== arm: {args.writers}-writer ({'single-node' if args.writers == 1 else '2-node-concurrent'}) ===")
    print(f"{'writers':<22}{args.writers}")
    print(f"{'aggregate GB/s':<22}{fmt(agg_gbs, ' GB/s')}")
    print(f"{'mean s/ckpt':<22}{fmt(mean_t, ' s', nd=0)}  (range {rng} s, n={len(times)})")
    print(f"{'OST0/OST1 split':<22}{split}")
    print(f"{'disk %util band':<22}{fmt(util_lo, '%', nd=0)} .. {fmt(util_hi, '%', nd=0)}")
    print(f"{'peak UMA GiB':<22}{fmt(uma, ' GiB', nd=2)}")

    # --- Attribution: the load-bearing client-vs-substrate adjudicator (ZFS txgs). %util alone is
    #     degenerate here because ~0.78 GB/s sits inside this stack's own file-backed-zpool band. ---
    nd_mb = nd / 1e6 if nd is not None else None
    st_ms = st / 1e6 if st is not None else None
    nd_pct = 100 * nd / ZFS_DIRTY_CAP if nd is not None else None
    print(f"{'ZFS peak ndirty':<22}{fmt(nd_mb, ' MB/txg', nd=1)}"
          f"{'' if nd_pct is None else f'  (~{nd_pct:.1f}% of ~4 GiB dirty cap)'}")
    print(f"{'ZFS peak txg stime':<22}{fmt(st_ms, ' ms', nd=1)}  (sync duration; short => no backpressure)")
    if nd is not None and st is not None:
        # Idle if ndirty stays well below the throttle onset (~60% of the dirty cap) and sync stays
        # far under the 5 s txg timeout. Validation (2026-06-01) saw 102-194 MB ndirty / 83-98 ms
        # stime across both arms — idle with margin; a substrate-bound stack would pin ndirty near
        # cap with stime approaching seconds. Thresholds set loose so normal variation doesn't flip.
        idle = nd < 0.25 * ZFS_DIRTY_CAP and st < 500_000_000   # < 25% of dirty cap and < 500 ms sync
        verdict = ("ZFS substrate IDLE -> client-bound (substrate-ceiling rejected)" if idle
                   else "ZFS HOT -> re-check: ndirty/stime high, may be substrate-bound on this stack")
        print(f"{'attribution':<22}{verdict}  (n={ntxg} txg samples)")
    elif ntxg == 0:
        print(f"{'attribution':<22}- (no txgs-*.log captured; wrong OST*_POOL? attribution UNVERIFIED)")
    print(f"\nNote: the ZFS verdict adjudicates substrate-idle vs throttling; for the writer-off-CPU"
          f" (latency-bound, not CPU-serialized) half, eyeball the per-thread pidstat-cpu-*.log.")
    if not times:
        print("\nNOTE: no per-checkpoint wall-clock parsed from the train log (timestamp format"
              " may differ). aggregate GB/s above is None; read s/ckpt from the log manually,"
              " or cross-check with the OST byte delta.")
    print("\nThe finding: run both arms, place the 1-writer and 2-writer rows side by side."
          " 2-writer aggregate should be ~1.69x the 1-writer aggregate (0.78 -> 1.32 GB/s),"
          " landing on the independently-fio-measured ~1.35 GB/s concurrent ceiling, with the"
          " disk %util band staying well under saturation in BOTH arms (client-bound).")


if __name__ == "__main__":
    main()
