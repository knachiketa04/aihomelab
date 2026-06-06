#!/usr/bin/env python3
"""
Spot-check Phase 2 output for human-eye sanity. Read-only.

Shows:
- File-level summary (rows, size) for both deduped outputs
- Sample titles from structured_recipes_diet_friendly (mixed vegan + vegetarian)
- Source × diet_tag distribution in grounding_context
- One full vegetarian row (with diet_evidence) for evidence-trail sanity
- One full vegan row for vegan-corpus sanity
"""

import json
import random
from collections import Counter
from pathlib import Path

DEDUPED = Path("/data/deduped")
DIET = DEDUPED / "structured_recipes_diet_friendly.jsonl"
CONTEXT = DEDUPED / "grounding_context.jsonl"

random.seed(42)


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f]


def main():
    diet_rows = load_jsonl(DIET)
    context_rows = load_jsonl(CONTEXT)

    print(f"=== {DIET.name} ===")
    print(f"  rows: {len(diet_rows)}")
    print(f"  bytes: {DIET.stat().st_size:,}")
    diet_tag_counts = Counter(r["diet_tag"] for r in diet_rows)
    for tag in ["vegan", "vegetarian"]:
        print(f"    {tag}: {diet_tag_counts[tag]}")
    print()

    print(f"=== {CONTEXT.name} — source × diet_tag distribution ===")
    print(f"  rows: {len(context_rows)}")
    print(f"  bytes: {CONTEXT.stat().st_size:,}")
    src_diet = Counter((r["source"], r["diet_tag"]) for r in context_rows)
    for (src, tag), n in sorted(src_diet.items()):
        print(f"    {src:10s}  {tag:16s}  {n:5d}")
    print()

    print("=== diet-friendly: 10 random titles (mixed vegan + vegetarian) ===")
    sample = random.sample(diet_rows, min(10, len(diet_rows)))
    for r in sample:
        tag = r["diet_tag"]
        print(f"  [{tag:11s}] {r['title']}")
    print()

    print("=== diet-friendly: 5 first titles ===")
    for r in diet_rows[:5]:
        print(f"  [{r['diet_tag']:11s}] {r['title']}")
    print()

    print("=== diet-friendly: 5 last titles ===")
    for r in diet_rows[-5:]:
        print(f"  [{r['diet_tag']:11s}] {r['title']}")
    print()

    print("=== one full vegetarian row (with diet_evidence) ===")
    veg = next((r for r in diet_rows if r["diet_tag"] == "vegetarian"), None)
    if veg:
        print(f"  title: {veg['title']}")
        print(f"  ingredients_raw ({len(veg['ingredients_raw'])}):")
        for ing in veg["ingredients_raw"]:
            print(f"    - {ing}")
        print(f"  diet_evidence ({len(veg['diet_evidence'])}):")
        for ev in veg["diet_evidence"]:
            print(f"    - [{ev['category']}] matched \"{ev['term']}\" in: {ev['ingredient']}")
    print()

    print("=== one full vegan row (sanity: should have no diet_evidence) ===")
    vgn = next((r for r in diet_rows if r["diet_tag"] == "vegan"), None)
    if vgn:
        print(f"  title: {vgn['title']}")
        print(f"  ingredients_raw ({len(vgn['ingredients_raw'])}):")
        for ing in vgn["ingredients_raw"]:
            print(f"    - {ing}")
        print(f"  diet_evidence: {vgn['diet_evidence']}")  # should be []


if __name__ == "__main__":
    main()
