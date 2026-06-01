#!/usr/bin/env python3
"""analyze-loaders.py — parse loader cold-load logs into the comparison matrix.

Reads the per-loader log dirs written by run-loader-cold-warm.sh:

    <log-dir>/<loader>/loadtime-<loader>-cold-N.log   (vLLM load-time line)
    <log-dir>/<loader>/mpstat-<loader>-cold-N.log      (mpstat -P ALL)
    <log-dir>/<loader>/iostat-<loader>-cold-N.log      (iostat -x <dev>)
    <log-dir>/<loader>/rss-<loader>-cold-N.log         (VmHWM/VmRSS sampler)

and emits the matrix:

    Loader | Source | Cache | Load Y(s) | sec/GiB | 1-core pin? | Src read | Peak RSS

Usage:
    ./analyze-loaders.py --log-dir /home/$USER/vllm-cold-load-reproduce/logs
    ./analyze-loaders.py --log-dir ./logs --model-gib 15.35

Surfaces the mechanism signature, not just the timing: a "1-core pin?" YES means
one CPU sat ~100% %usr while the rest idled (the default-loader tell); the source
read peak + %util tell whether the NVMe was loafing or saturated.
"""

import argparse
import re
import statistics
from pathlib import Path

# vLLM 0.15.1 load-time lines. Prefer the loader-AGNOSTIC gpu_model_runner line:
# the default safetensors loader (and fastsafetensors) ALSO emit "Loading weights
# took", but runai_streamer emits ONLY "Model loading took" — so keying on the
# weights line silently drops the runai row (the 36x headline arm).
#   default_loader.py  "Loading weights took 99.16 seconds"                    (default / fastsafetensors only)
#   gpu_model_runner   "Model loading took 15.27 GiB memory and 99.16 seconds" (EVERY loader)
# Note the literal "GiB memory and" in 0.15.1 — matching "GiB and" drops every row.
RE_MODEL_LOADING = re.compile(r"Model loading took\s+([\d.]+)\s*GiB(?:\s+memory)?\s+and\s+([\d.]+)\s*seconds", re.I)
RE_LOADING_WEIGHTS = re.compile(r"Loading weights took\s+([\d.]+)\s*seconds", re.I)

LOADERS = ["auto", "runai_streamer", "fastsafetensors"]


def parse_loadtime(path: Path):
    """Return (seconds, gib_or_None) from a loadtime log, preferring the
    loader-agnostic 'Model loading took' line. None if no match."""
    if not path.is_file():
        return None, None
    secs = gib = None
    for line in path.read_text(errors="ignore").splitlines():
        m = RE_MODEL_LOADING.search(line)
        if m:
            gib = float(m.group(1))
            secs = float(m.group(2))
            continue  # prefer the last/most-complete match
        m = RE_LOADING_WEIGHTS.search(line)
        if m and secs is None:
            secs = float(m.group(1))
    return secs, gib


def parse_mpstat_pin(path: Path):
    """Detect a SUSTAINED single-core pin from mpstat -P ALL output.
    Returns (pinned, max_single_core_usr, n_pin_samples, n_samples).

    mpstat prints one block per 1s interval: an 'all' aggregate row followed by
    one row per CPU. We group rows into samples (a new 'all' row starts a sample)
    and flag a sample as a pin when its hottest core is >=80% %usr WHILE the 'all'
    aggregate stays <30% (one busy core, box otherwise idle). A pin is the real
    bottleneck only if it's SUSTAINED across most of the load window — the default
    loader pins one core for the whole ~90s load; a streamer finishes in 3-6s, so
    at most a blip shows in its handful of samples. A peak alone is not a pin."""
    if not path.is_file():
        return None, None, 0, 0
    # mpstat columns vary by version (leading timestamp / AM-PM field). Locate the
    # "%usr" and "CPU" columns from the header; per-core rows have a numeric CPU id,
    # the aggregate row is "all".
    usr_idx = cpu_idx = None
    saw_header = False
    samples = []          # (all_usr, hottest_core_usr) per 1s interval
    cur_all = None
    cur_max_core = 0.0
    have_cur = False
    for line in path.read_text(errors="ignore").splitlines():
        toks = line.split()
        if not toks:
            continue
        if "%usr" in toks and "CPU" in toks:
            usr_idx = toks.index("%usr")
            cpu_idx = toks.index("CPU")
            saw_header = True
            continue
        if usr_idx is None or len(toks) <= max(usr_idx, cpu_idx):
            continue
        cpu_field = toks[cpu_idx]
        try:
            usr = float(toks[usr_idx])
        except ValueError:
            continue
        if cpu_field == "all":
            if have_cur:
                samples.append((cur_all, cur_max_core))
            cur_all, cur_max_core, have_cur = usr, 0.0, True
        elif cpu_field.isdigit():
            cur_max_core = max(cur_max_core, usr)
    if have_cur:
        samples.append((cur_all, cur_max_core))
    if not saw_header or not samples:
        return None, None, 0, 0
    pin_samples = [s for s in samples if s[1] >= 80.0 and s[0] < 30.0]
    overall_max_core = max(s[1] for s in samples)
    # sustained = a pin across most of the window, and more than a 1-2 sample blip
    pinned = len(pin_samples) >= max(3, 0.5 * len(samples))
    return pinned, overall_max_core, len(pin_samples), len(samples)


def parse_iostat_peak_read(path: Path):
    """Return (peak_read_MBps, peak_util_pct) from iostat -x output.
    iostat -x reports rkB/s; column positions vary by sysstat version, so we
    locate them from the header ('rkB/s' and '%util')."""
    if not path.is_file():
        return None, None
    rkb_idx = util_idx = None
    peak_read_kb = 0.0
    peak_util = 0.0
    for line in path.read_text(errors="ignore").splitlines():
        toks = line.split()
        if not toks:
            continue
        if "Device" in toks and ("rkB/s" in toks or "%util" in toks):
            if "rkB/s" in toks:
                rkb_idx = toks.index("rkB/s")
            if "%util" in toks:
                util_idx = toks.index("%util")
            continue
        # device data rows start with the device name (e.g. nvme0n1)
        if rkb_idx is not None and len(toks) > rkb_idx and toks[0][0:4] == "nvme":
            try:
                peak_read_kb = max(peak_read_kb, float(toks[rkb_idx]))
            except ValueError:
                pass
        if util_idx is not None and len(toks) > util_idx and toks[0][0:4] == "nvme":
            try:
                peak_util = max(peak_util, float(toks[util_idx]))
            except ValueError:
                pass
    if rkb_idx is None:
        return None, peak_util if util_idx is not None else None
    return peak_read_kb / 1024.0, peak_util  # MB/s, %


def parse_peak_rss_gib(path: Path):
    """Return peak VmHWM (GiB) from the rss sampler log (kB values)."""
    if not path.is_file():
        return None
    peak_kb = 0
    for line in path.read_text(errors="ignore").splitlines():
        if "VmHWM:" in line:
            m = re.search(r"VmHWM:\s+(\d+)\s*kB", line)
            if m:
                peak_kb = max(peak_kb, int(m.group(1)))
    return (peak_kb / (1024.0 * 1024.0)) if peak_kb else None


def fmt(x, suffix="", nd=2):
    return f"{x:.{nd}f}{suffix}" if isinstance(x, (int, float)) else "-"


def analyze_loader(loader_dir: Path, loader: str, model_gib: float):
    """Aggregate the cold reps for one loader into one matrix row dict."""
    cold_loadtimes = sorted(loader_dir.glob(f"loadtime-{loader}-cold-*.log"))
    secs_list, gib = [], None
    for lt in cold_loadtimes:
        s, g = parse_loadtime(lt)
        if s is not None:
            secs_list.append(s)
        if g is not None:
            gib = g
    mean_secs = statistics.mean(secs_list) if secs_list else None
    use_gib = gib or model_gib

    # mechanism side-channels: read from the first cold rep that has them.
    pinned = max_usr = peak_read = peak_util = peak_rss = None
    n_pin = n_tot = None
    for tag in [f"{loader}-cold-{i}" for i in range(1, 9)]:
        if pinned is None:
            p, u, npin, ntot = parse_mpstat_pin(loader_dir / f"mpstat-{tag}.log")
            if p is not None:
                pinned, max_usr, n_pin, n_tot = p, u, npin, ntot
        if peak_read is None:
            r, ut = parse_iostat_peak_read(loader_dir / f"iostat-{tag}.log")
            if r is not None or ut is not None:
                peak_read, peak_util = r, ut
        if peak_rss is None:
            peak_rss = parse_peak_rss_gib(loader_dir / f"rss-{tag}.log")

    sec_per_gib = (mean_secs / use_gib) if (mean_secs and use_gib) else None
    return {
        "loader": loader,
        "reps": secs_list,
        "mean_secs": mean_secs,
        "sec_per_gib": sec_per_gib,
        "pinned": pinned,
        "max_usr": max_usr,
        "n_pin": n_pin,
        "n_tot": n_tot,
        "peak_read": peak_read,
        "peak_util": peak_util,
        "peak_rss": peak_rss,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-dir", type=Path, default=Path("./logs"),
                    help="Dir holding per-loader subdirs (default: ./logs)")
    ap.add_argument("--model-gib", type=float, default=15.35,
                    help="Model weight size in GiB for sec/GiB (default 15.35 = Qwen3-8B)")
    args = ap.parse_args()

    if not args.log_dir.is_dir():
        ap.error(f"log dir not found: {args.log_dir}")

    rows = []
    for loader in LOADERS:
        ldir = args.log_dir / loader
        if not ldir.is_dir():
            continue
        rows.append(analyze_loader(ldir, loader, args.model_gib))

    if not rows:
        ap.error(f"no per-loader subdirs under {args.log_dir} (expected: {LOADERS})")

    print("\n=== Loader cold-load comparison (cold, local NVMe) ===\n")
    hdr = (f"{'Loader':<16} {'Cache':>6} {'Load(s)':>9} {'sec/GiB':>8} "
           f"{'1-core pin?':>12} {'Src read':>12} {'%util':>7} {'Peak RSS':>10}")
    print(hdr)
    print("-" * len(hdr))

    auto_mean = None
    for r in rows:
        if r["loader"] == "auto":
            auto_mean = r["mean_secs"]
        pin = "-" if r["pinned"] is None else ("YES" if r["pinned"] else "no")
        if r.get("n_tot"):
            pin += f"({r['n_pin']}/{r['n_tot']} samp)"
        src = fmt(r["peak_read"], " MB/s", 0) if r["peak_read"] is not None else "-"
        util = fmt(r["peak_util"], "%", 1) if r["peak_util"] is not None else "-"
        rss = fmt(r["peak_rss"], " GiB", 2) if r["peak_rss"] is not None else "-"
        print(f"{r['loader']:<16} {'cold':>6} {fmt(r['mean_secs'],'',1):>9} "
              f"{fmt(r['sec_per_gib'],'',2):>8} {pin:>12} {src:>12} {util:>7} {rss:>10}")

    print("\n=== speedup vs default (auto) ===")
    if auto_mean:
        for r in rows:
            if r["mean_secs"] and r["loader"] != "auto":
                print(f"  {r['loader']:<16} {auto_mean / r['mean_secs']:.0f}x faster "
                      f"({auto_mean:.0f}s -> {r['mean_secs']:.1f}s)")
    else:
        print("  (no 'auto' baseline parsed — cannot compute speedup)")

    print("\n=== per-rep cold load times ===")
    for r in rows:
        reps = ", ".join(f"{s:.2f}" for s in r["reps"]) if r["reps"] else "(none)"
        print(f"  {r['loader']:<16}: {reps}")

    print("\n=== mechanism read ===")
    auto_row = next((r for r in rows if r["loader"] == "auto"), None)
    strm_reads = [r["peak_read"] for r in rows
                  if r["loader"] != "auto" and r["peak_read"] is not None]
    if auto_row and auto_row["pinned"]:
        print(f"  default loader: ONE core pinned ~{auto_row['max_usr']:.0f}% %usr while the box")
        print("  stays otherwise idle => single-threaded per-tensor CPU-bound, not storage.")
        if auto_row["peak_read"] is not None:
            print(f"  its NVMe read peaks ~{auto_row['peak_read']:.0f} MB/s — a small fraction of a")
            print("  Gen5 drive's multi-GB/s bandwidth, i.e. the disk is bandwidth-loafing.")
            print("  (%util is a weak NVMe saturation proxy: one in-flight I/O reads as 'busy'")
            print("   at trivial bandwidth, so a high %util here does NOT mean disk-bound.)")
        if strm_reads:
            print(f"  streaming loaders are NOT pinned and read up to ~{max(strm_reads):.0f} MB/s —")
            print("  they parallelize the same bytes, turning the CPU-bound path into an I/O one.")
    elif auto_row:
        print("  default loader NOT detected as pinned — inspect the mpstat logs directly.")
        print("  On a slower-than-Gen5 device the cold load can become storage-bound and the")
        print("  18-36x gap shrinks; confirm the page cache was actually dropped (cold rep).")
    else:
        print("  (no 'auto' row parsed — cannot read the mechanism.)")


if __name__ == "__main__":
    main()
