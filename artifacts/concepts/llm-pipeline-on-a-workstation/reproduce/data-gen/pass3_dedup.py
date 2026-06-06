#!/usr/bin/env python3
"""
Pass 3 — Near-duplicate dedup + final output split.

Reads from /data/tagged/tagged.jsonl
Writes two files (the Phase 3 consumption targets):

  /data/deduped/structured_recipes_diet_friendly.jsonl
      content_type=structured_recipe AND diet_tag IN (vegan, vegetarian),
      MinHash-deduped at Jaccard >= 0.85.
      Used by Phase 3 as format-shape examples for the teacher.

  /data/deduped/grounding_context.jsonl
      ALL tagged.jsonl rows passed through unchanged (no filter, no dedup).
      Used by Phase 3 as prompt-grounding material — including non_vegetarian
      recipes (for "adapt this to vegan/vegetarian" prompts) and context rows
      (Gutenberg books, Wikipedia articles).

Dedup algorithm:
  - MinHash signature on lowercased word-set of (title + " " + ingredients_raw)
  - num_perm=128 (datasketch default)
  - LSH index with threshold=0.85
  - First-seen-wins: insert into LSH only after checking; if any near-duplicate
    already in the index, drop the current row

Requires `datasketch` in the node0 venv (one-time install).
"""

import json
import re
from pathlib import Path

from datasketch import MinHash, MinHashLSH

IN_PATH = Path("/data/tagged/tagged.jsonl")
DIET_OUT = Path("/data/deduped/structured_recipes_diet_friendly.jsonl")
CONTEXT_OUT = Path("/data/deduped/grounding_context.jsonl")

NUM_PERM = 128
JACCARD_THRESHOLD = 0.85
TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text):
    """Lowercase, alphanumeric word tokens."""
    return set(TOKEN_RE.findall(text.lower()))


def make_minhash(tokens):
    m = MinHash(num_perm=NUM_PERM)
    for t in tokens:
        m.update(t.encode("utf-8"))
    return m


def signature_text(row):
    """Concatenate title + ingredients for MinHash input."""
    title = row.get("title") or ""
    ingredients = row.get("ingredients_raw") or []
    return title + " " + " ".join(ingredients)


def main():
    DIET_OUT.parent.mkdir(parents=True, exist_ok=True)

    lsh = MinHashLSH(threshold=JACCARD_THRESHOLD, num_perm=NUM_PERM)
    kept_keys = set()

    stats = {
        "total_rows": 0,
        "diet_friendly_candidates": 0,
        "diet_friendly_kept": 0,
        "diet_friendly_dropped_dup": 0,
        "diet_friendly_dropped_empty": 0,
        "context_rows_written": 0,
        "vegan_kept": 0,
        "vegetarian_kept": 0,
    }

    # Single pass: write context (always) + collect dedup candidates for diet output.
    diet_candidates = []  # list of (row, key, minhash)

    with open(IN_PATH) as fin, open(CONTEXT_OUT, "w") as fctx:
        for line in fin:
            stats["total_rows"] += 1
            row = json.loads(line)

            # Every row goes to grounding_context unchanged
            fctx.write(json.dumps(row, ensure_ascii=False) + "\n")
            stats["context_rows_written"] += 1

            # Filter for diet-friendly dedup candidate
            if row.get("content_type") != "structured_recipe":
                continue
            if row.get("diet_tag") not in ("vegan", "vegetarian"):
                continue
            stats["diet_friendly_candidates"] += 1

            tokens = tokenize(signature_text(row))
            if not tokens:
                stats["diet_friendly_dropped_empty"] += 1
                continue

            m = make_minhash(tokens)
            key = row["id"]
            diet_candidates.append((row, key, m))

    # LSH dedup pass over candidates
    with open(DIET_OUT, "w") as fdiet:
        for row, key, m in diet_candidates:
            near = lsh.query(m)
            if near:
                stats["diet_friendly_dropped_dup"] += 1
                continue
            lsh.insert(key, m)
            kept_keys.add(key)
            fdiet.write(json.dumps(row, ensure_ascii=False) + "\n")
            stats["diet_friendly_kept"] += 1
            if row["diet_tag"] == "vegan":
                stats["vegan_kept"] += 1
            elif row["diet_tag"] == "vegetarian":
                stats["vegetarian_kept"] += 1

    print("=== Pass 3 dedup + final split complete ===")
    print(f"Input: {IN_PATH}  ({stats['total_rows']} rows)")
    print()
    print(f"Grounding context output: {CONTEXT_OUT}  ({CONTEXT_OUT.stat().st_size:,} bytes)")
    print(f"  rows written: {stats['context_rows_written']} (all source rows, no filter, no dedup)")
    print()
    print(f"Diet-friendly output: {DIET_OUT}  ({DIET_OUT.stat().st_size:,} bytes)")
    print(f"  candidates (structured + vegan/vegetarian): {stats['diet_friendly_candidates']}")
    print(f"  dropped — empty signature:                  {stats['diet_friendly_dropped_empty']}")
    print(f"  dropped — near-duplicate (Jaccard >= {JACCARD_THRESHOLD}): {stats['diet_friendly_dropped_dup']}")
    print(f"  kept:                                       {stats['diet_friendly_kept']}")
    print(f"    of which vegan:      {stats['vegan_kept']}")
    print(f"    of which vegetarian: {stats['vegetarian_kept']}")
    if stats["diet_friendly_candidates"]:
        dup_pct = 100.0 * stats["diet_friendly_dropped_dup"] / stats["diet_friendly_candidates"]
        print(f"  duplicate rate: {dup_pct:.1f}%")


if __name__ == "__main__":
    main()
