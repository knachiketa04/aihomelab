#!/usr/bin/env python3
"""
split_prompts.py — slice the 20K prompt list into two balanced halves for the
data-parallel two-node teacher run (node0 + node1).

Round-robin assignment preserves template + dietary mix across both shards
(naive first-half/second-half would put all base prompts on one node and all
Indian prompts on the other, since build_prompts.py emits base first then
Indian).
"""

import argparse
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, type=Path)
    ap.add_argument("--out-node0", required=True, type=Path)
    ap.add_argument("--out-node1", required=True, type=Path)
    args = ap.parse_args()

    args.out_node0.parent.mkdir(parents=True, exist_ok=True)
    args.out_node1.parent.mkdir(parents=True, exist_ok=True)

    n01 = n02 = 0
    with args.inp.open("r", encoding="utf-8") as f, \
         args.out_node0.open("w", encoding="utf-8") as f01, \
         args.out_node1.open("w", encoding="utf-8") as f02:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            if i % 2 == 0:
                f01.write(line + "\n")
                n01 += 1
            else:
                f02.write(line + "\n")
                n02 += 1

    print(f"[split_prompts] wrote {n01} prompts → {args.out_node0}")
    print(f"[split_prompts] wrote {n02} prompts → {args.out_node1}")


if __name__ == "__main__":
    main()
