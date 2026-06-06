#!/usr/bin/env python3
"""
Pass 2 — Vegan classification.

Reads from /data/unified/unified.jsonl
Writes to /data/tagged/tagged.jsonl

Adds a `diet_tag` field per row:

  vegan           — no flesh, seafood, dairy, eggs, animal byproducts, or hidden animal-derived
  vegetarian      — no flesh or seafood, but contains dairy/eggs/byproducts/hidden
  non_vegetarian  — contains flesh or seafood
  unknown         — structured_recipe with empty ingredients_raw (can't classify)
  context         — narrative_recipe / recipe_context rows (whole-book or whole-article text;
                    can't be meaningfully diet-classified at this granularity, used as
                    grounding material for Phase 3 prompts regardless of diet content)

Also adds a `diet_evidence` field for structured_recipe rows: list of
{"category": "flesh|seafood|dairy|eggs|byproducts|hidden", "term": "milk",
 "ingredient": "1 cup whole milk"} dicts — debugging aid for false-positive review.

Plant-based modifier override: if an ingredient line contains a known vegan modifier
("almond", "soy", "oat", "coconut", "cashew", "vegan", etc.), blocklist matches on
that line are ignored. Catches "almond milk", "vegan butter", "soy cheese", etc.
False-negative risk acknowledged (e.g., "soy chicken nuggets" → unflagged), accepted
for the pilot.

Pure stdlib. Single-pass; ~4K rows.
"""

import json
import re
import sys
from pathlib import Path

IN_PATH = Path("/data/unified/unified.jsonl")
OUT_PATH = Path("/data/tagged/tagged.jsonl")

# Ingredient blocklist, categorized. Substrings; matched case-insensitive on
# word boundaries. Order within category irrelevant.
BLOCKLIST = {
    "flesh": [
        "chicken", "beef", "pork", "lamb", "mutton", "veal", "venison",
        "rabbit", "ham", "bacon", "prosciutto", "salami", "sausage",
        "pepperoni", "chorizo", "duck", "goose", "turkey", "quail",
    ],
    "seafood": [
        # Note: "fish" is matched as a standalone word AND as a suffix on compound
        # names (catfish, swordfish, monkfish, etc.) via the separate FISH_SUFFIX_RE.
        "fish", "salmon", "tuna", "cod", "shrimp", "prawn", "crab", "lobster",
        "oyster", "clam", "mussel", "scallop", "anchovy", "sardine", "octopus",
        "squid", "mackerel", "trout", "halibut", "tilapia", "caviar", "roe",
        # Fish names that do NOT contain "fish" substring (would slip past suffix matcher)
        "bass", "perch", "snapper", "grouper", "mahi", "mahi-mahi", "pike",
        "pollock", "sole", "flounder", "herring", "sea bream", "sea bass",
        "barramundi", "tilefish", "rockfish",
        # Crustaceans/molluscs beyond the common short list
        "crayfish", "crawfish", "krill", "abalone", "snail", "escargot",
    ],
    "dairy": [
        "milk", "butter", "cream", "cheese", "yogurt", "yoghurt", "ghee",
        "whey", "casein", "lactose", "paneer", "buttermilk", "kefir",
        "mozzarella", "cheddar", "parmesan", "feta", "ricotta", "brie",
    ],
    "eggs": [
        "egg", "eggs", "yolk", "yolks", "mayonnaise",
    ],
    "byproducts": [
        "gelatin", "rennet", "lard", "tallow", "suet", "bone broth",
        "fish sauce", "oyster sauce", "dashi", "worcestershire", "honey",
    ],
    "hidden": [
        "isinglass", "carmine", "cochineal", "l-cysteine",
    ],
}

# If any of these appear in the same ingredient line as a blocklist match,
# the blocklist match is treated as a plant-based version and ignored.
VEGAN_MODIFIERS = {
    "vegan", "plant-based", "plant based", "non-dairy", "nondairy",
    "almond", "soy", "soya", "oat", "coconut", "cashew", "rice", "hemp",
    "tofu", "tempeh", "seitan", "jackfruit", "nutritional yeast",
    "cashews", "almonds", "macadamia", "hazelnut",
}

# Compile blocklist into word-boundary regexes per term
COMPILED_BLOCKLIST = {
    category: [(term, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
               for term in terms]
    for category, terms in BLOCKLIST.items()
}

# Catch compound fish names (catfish, swordfish, monkfish, kingfish, sailfish,
# blackfish, cuttlefish, sunfish, etc.) that wouldn't trigger the standalone
# "fish" word-boundary match. Pattern: any word ending in "fish" of length >=4
# (excludes the bare word "fish" which is already covered by the blocklist).
FISH_SUFFIX_RE = re.compile(r"\b\w+fish\b", re.IGNORECASE)


def has_vegan_modifier(text_lower):
    return any(mod in text_lower for mod in VEGAN_MODIFIERS)


def classify_structured(ingredients_raw):
    """
    Returns (diet_tag, evidence_list).

    evidence_list: [{"category", "term", "ingredient"}] for each blocklist match
    that survived the vegan-modifier override.
    """
    if not ingredients_raw:
        return "unknown", []

    evidence = []
    for ing in ingredients_raw:
        ing_lower = ing.lower()
        if has_vegan_modifier(ing_lower):
            continue
        for category, term_patterns in COMPILED_BLOCKLIST.items():
            for term, pattern in term_patterns:
                if pattern.search(ing_lower):
                    evidence.append({
                        "category": category,
                        "term": term,
                        "ingredient": ing,
                    })
                    break  # at most one hit per category per ingredient
        # Compound fish-suffix matcher (catches catfish/swordfish/etc.) —
        # tagged as seafood. Only fires if seafood didn't already match.
        if not any(e["category"] == "seafood" and e["ingredient"] == ing for e in evidence):
            m = FISH_SUFFIX_RE.search(ing_lower)
            if m:
                evidence.append({
                    "category": "seafood",
                    "term": m.group(0),
                    "ingredient": ing,
                })

    categories_hit = {e["category"] for e in evidence}
    if categories_hit & {"flesh", "seafood"}:
        diet_tag = "non_vegetarian"
    elif categories_hit & {"dairy", "eggs", "byproducts", "hidden"}:
        diet_tag = "vegetarian"
    else:
        diet_tag = "vegan"
    return diet_tag, evidence


def main():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    tag_counts = {"vegan": 0, "vegetarian": 0, "non_vegetarian": 0,
                  "unknown": 0, "context": 0}
    category_hit_counts = {cat: 0 for cat in BLOCKLIST}
    total_in = 0

    with open(IN_PATH) as fin, open(OUT_PATH, "w") as fout:
        for line in fin:
            total_in += 1
            row = json.loads(line)
            if row.get("content_type") == "structured_recipe":
                diet_tag, evidence = classify_structured(row.get("ingredients_raw") or [])
                row["diet_tag"] = diet_tag
                row["diet_evidence"] = evidence
                for cat in {e["category"] for e in evidence}:
                    category_hit_counts[cat] += 1
            else:
                row["diet_tag"] = "context"
                row["diet_evidence"] = []
            tag_counts[row["diet_tag"]] += 1
            fout.write(json.dumps(row, ensure_ascii=False) + "\n")

    print("=== Pass 2 vegan-classify complete ===")
    print(f"Output: {OUT_PATH}  ({OUT_PATH.stat().st_size:,} bytes)")
    print(f"Total rows: {total_in}")
    print("=== Diet tag distribution ===")
    for tag in ["vegan", "vegetarian", "non_vegetarian", "unknown", "context"]:
        n = tag_counts[tag]
        pct = 100.0 * n / total_in if total_in else 0.0
        print(f"  {tag:16s}: {n:5d} ({pct:.1f}%)")
    structured_total = sum(tag_counts[t] for t in ["vegan", "vegetarian", "non_vegetarian", "unknown"])
    print(f"=== Category hits (per structured_recipe row, among {structured_total}) ===")
    print("(a row may hit multiple categories; counts are # of rows with >=1 hit in that category)")
    for cat in ["flesh", "seafood", "dairy", "eggs", "byproducts", "hidden"]:
        n = category_hit_counts[cat]
        pct = 100.0 * n / structured_total if structured_total else 0.0
        print(f"  {cat:12s}: {n:5d} ({pct:.1f}%)")


if __name__ == "__main__":
    main()
