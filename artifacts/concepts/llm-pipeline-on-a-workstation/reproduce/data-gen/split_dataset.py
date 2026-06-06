#!/usr/bin/env python3
"""
split_dataset.py — Phase 4 prep, pass 3: chat.jsonl → train/val/test 90/5/5.

GROUP-STRATIFIED by grounding_row_id to prevent grounding-excerpt leakage across
splits: every chat row that was generated from the same grounding recipe excerpt
lands in the same split. Rows with grounding_row_id == null (substitution,
nutrition_target, technique_explainer, all indian_* templates) carry no shared
excerpt, so each becomes its own singleton group and distributes freely.

Assignment: deterministic (fixed seed). Groups are shuffled then placed
largest-first into whichever split is furthest below its target fraction — this
keeps the 90/5/5 ratio tight even though group sizes vary.

Pure stdlib.
"""

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path


def load_jsonl(path: Path) -> list:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def group_key(row: dict, idx: int):
    """Real grounding_row_id groups rows together; null → unique singleton."""
    gid = row.get("grounding_row_id")
    if gid is None:
        return f"__singleton_{idx}"
    return f"g{gid}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, type=Path, help="chat.jsonl")
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--train", type=float, default=0.90)
    ap.add_argument("--val", type=float, default=0.05)
    ap.add_argument("--test", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rows = load_jsonl(args.inp)
    n = len(rows)
    if n == 0:
        sys.exit("[split_dataset] ERROR: no rows in input")

    targets = {"train": args.train, "val": args.val, "test": args.test}
    s = sum(targets.values())
    targets = {k: v / s for k, v in targets.items()}  # normalize

    # Build groups
    groups = defaultdict(list)
    for i, r in enumerate(rows):
        groups[group_key(r, i)].append(r)
    group_items = list(groups.items())

    # Deterministic shuffle, then largest-group-first
    rng = random.Random(args.seed)
    rng.shuffle(group_items)
    group_items.sort(key=lambda kv: len(kv[1]), reverse=True)

    split_rows = {"train": [], "val": [], "test": []}
    for _, members in group_items:
        # place into the split that is currently furthest below its target count
        best = None
        best_deficit = None
        for name in ("train", "val", "test"):
            target_count = targets[name] * n
            deficit = target_count - len(split_rows[name])
            if best_deficit is None or deficit > best_deficit:
                best_deficit = deficit
                best = name
        split_rows[best].extend(members)

    # --- Leakage assertion FIRST (before any write): no real grounding_row_id
    #     spans >1 split. Asserting before writing means a detected leak never
    #     leaves bad split files on disk. ---
    gid_to_splits = defaultdict(set)
    for name in ("train", "val", "test"):
        for r in split_rows[name]:
            gid = r.get("grounding_row_id")
            if gid is not None:
                gid_to_splits[gid].add(name)
    leaked = {g: sp for g, sp in gid_to_splits.items() if len(sp) > 1}
    if leaked:
        sys.exit(f"[split_dataset] FATAL leakage: {len(leaked)} grounding_row_ids span "
                 f"multiple splits, e.g. {dict(list(leaked.items())[:3])}")

    # --- Write atomically (tmp + replace per file) ---
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("train", "val", "test"):
        out = args.out_dir / f"{name}.jsonl"
        tmp = out.with_suffix(out.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in split_rows[name]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(out)

    # --- Report ---
    def stats(name):
        rs = split_rows[name]
        diet = defaultdict(int)
        suspect = 0
        for r in rs:
            diet[r.get("dietary_preference", "?")] += 1
            if r.get("leakage_suspect"):
                suspect += 1
        diet_str = ", ".join(f"{k}={v}" for k, v in sorted(diet.items()))
        sp = f"{100.0*suspect/len(rs):.1f}%" if rs else "n/a"
        frac = f"{100.0*len(rs)/n:.1f}%"
        return f"  {name:5s}: {len(rs):6d} ({frac})  [{diet_str}]  suspect={sp}"

    print("=== split_dataset (group-stratified by grounding_row_id) ===")
    print(f"  input rows : {n}")
    print(f"  groups     : {len(group_items)}  "
          f"(real-grounding: {sum(1 for k,_ in group_items if not k.startswith('__singleton'))}, "
          f"singleton: {sum(1 for k,_ in group_items if k.startswith('__singleton'))})")
    for name in ("train", "val", "test"):
        print(stats(name))
    print(f"  leakage check: PASS (0 grounding_row_ids span multiple splits)")
    print(f"  wrote train/val/test.jsonl → {args.out_dir}")


if __name__ == "__main__":
    main()
