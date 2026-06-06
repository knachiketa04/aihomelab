#!/usr/bin/env python3
"""
028 Phase 2b — algorithmic quality screen (SECONDARY, not the publishable finding).

Screens phase2_outputs.jsonl for dietary-preference violations using an ingredient
blocklist with plant-based-compound exclusions (coconut milk, peanut butter, vegan
cheese, *-free, etc.). This is an APPROXIMATE screen: it flags outputs for human
review rather than asserting a precise pass rate — accurate vegan detection on edge
cases is the LLM-judge's job (deferred). stdlib-only; runs anywhere with python3.

Container mountpoint: /out (holds the generation output jsonl).
"""
import json, re

OUT = "/out/phase2_outputs.jsonl"

# animal flesh — violates BOTH vegan and vegetarian
FLESH = ["beef", "pork", "chicken", "lamb", "mutton", "bacon", "ham", "sausage",
         "turkey", "duck", "veal", "goat", "venison", "fish", "salmon", "tuna",
         "shrimp", "prawn", "crab", "lobster", "anchovy", "anchovies", "oyster",
         "clam", "squid", "cod", "mackerel", "sardine", "scallop", "mussel"]
# animal but not flesh — violates vegan only (OK for vegetarian)
ANIMAL_NONFLESH = ["egg", "eggs", "milk", "butter", "cheese", "cream", "yogurt",
                   "yoghurt", "ghee", "paneer", "curd", "whey", "casein", "honey",
                   "gelatin", "lard", "rennet"]
# qualifiers that, when adjacent, make an otherwise-animal term plant-based
PLANT_QUAL = {"coconut", "almond", "soy", "soya", "oat", "cashew", "rice", "hemp",
              "plant", "plant-based", "plantbased", "nondairy", "non-dairy", "nut",
              "pea", "flax", "vegan", "mock", "faux", "imitation", "sunflower",
              "seed", "cocoa", "apple", "peanut", "macadamia", "walnut"}


def flagged_terms(text, terms):
    t = text.lower()
    hits = []
    for term in terms:
        for m in re.finditer(r"\b" + re.escape(term) + r"\b", t):
            start = m.start()
            prev = t[:start].rstrip().rsplit(" ", 1)[-1].strip(",.;:()-") if start else ""
            following = t[m.end():m.end() + 10]
            if prev in PLANT_QUAL:
                continue
            if following.startswith("-free") or following.startswith(" free") or following.startswith("less"):
                continue
            if term == "cream" and following.startswith(" of tartar"):
                continue
            hits.append(term)
            break  # one confirmed hit is enough to flag this output
    return sorted(set(hits))


def main():
    rows = [json.loads(l) for l in open(OUT) if l.strip()]
    by_pref = {"vegan": [], "vegetarian": []}
    for r in rows:
        pref = (r.get("dietary_preference") or "").lower()
        if pref in by_pref and r.get("generated"):
            by_pref[pref].append(r)

    for pref, items in by_pref.items():
        blocklist = FLESH + (ANIMAL_NONFLESH if pref == "vegan" else [])
        flagged = [(r, h) for r in items if (h := flagged_terms(r["generated"], blocklist))]
        n, f = len(items), len(flagged)
        print("=" * 66)
        if n:
            print(f"{pref.upper()}: {n} requests | {f} flagged for review | "
                  f"{100 * (n - f) / n:.1f}% clean by screen")
        else:
            print(f"{pref.upper()}: 0 requests")
        for r, hits in flagged[:15]:
            snip = " ".join(r["generated"].split())[:130]
            print(f"  [{r.get('request_id')}] {hits} :: {snip}...")
        if f > 15:
            print(f"  ... +{f - 15} more flagged (eyeball these to separate true violations from screen false-positives)")


if __name__ == "__main__":
    main()
