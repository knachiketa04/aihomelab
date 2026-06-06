#!/usr/bin/env python3
"""
build_prompts.py — assemble the per-request prompt list for the synthetic-generation run.

Reads templates.json + 023's grounding JSONL files, emits a JSONL file with one
row per intended vLLM request. Deterministic given --seed. The variable user
turn is materialized here; generate.py later wraps it with system + few-shot
from templates.json at request time (so the prefix stays byte-identical for
prefix caching).

Output row schema:
  {
    "request_id":           "025-00001",
    "template":             "substitution",
    "dietary_preference":   "vegan" | "vegetarian",
    "user_turn":            "Generate one Q&A pair. ...",
    "placeholders":         {"ingredient": "...", ...},   # for debug / spot-check
    "grounding_row_id":     int | null                    # for traceability
  }
"""

import argparse
import json
import random
import sys
from pathlib import Path

# -----------------------------------------------------------------------------
# Topic pools (locked 2026-05-27)
# -----------------------------------------------------------------------------

INGREDIENTS = [
    "butter", "eggs", "milk", "heavy cream", "cheese", "parmesan", "paneer", "ghee",
    "yogurt", "sour cream", "honey", "gelatin", "fish sauce", "oyster sauce",
    "anchovies", "lard", "chicken broth", "beef broth", "bacon", "chicken",
    "ground beef", "mayonnaise", "buttermilk", "condensed milk", "ricotta",
    "mozzarella", "feta", "cream cheese", "mascarpone", "all-purpose flour",
    "white sugar", "brown sugar", "breadcrumbs", "soy sauce", "miso",
    "worcestershire sauce", "sesame oil", "peanut butter", "white wine", "red wine",
    "coconut milk", "almond flour", "cornstarch", "baking powder", "vanilla",
    "chocolate", "walnuts", "cashews", "pine nuts", "sunflower seeds",
]

RECIPE_CONTEXTS = [
    "baking chocolate chip cookies", "making a creamy pasta sauce",
    "preparing a curry base", "making a vinaigrette", "baking a cake",
    "making pancakes", "making a stir-fry sauce", "preparing risotto",
    "making pizza dough", "making bread", "making a quiche", "making lasagna",
    "making ice cream", "making a smoothie", "making a soup base", "making a roux",
    "making a salad dressing", "marinating tofu", "preparing a stew",
    "making a frosting", "making granola", "making a pie crust",
    "preparing biryani", "making a chutney", "making a dal tadka",
    "making a sambar", "making a Thai curry paste", "making sushi rice",
    "making a custard", "making mac and cheese",
]

TECHNIQUES = [
    "tempering whole spices (tadka)", "blooming spices in oil",
    "dry-toasting spices", "sweating onions", "caramelizing onions", "deglazing",
    "fond-building", "mounting a sauce with fat", "emulsifying a vinaigrette",
    "making a roux", "blooming gelatin alternatives (agar agar)", "laminating dough",
    "kneading versus stretch-and-fold", "autolyse in bread-making", "proofing dough",
    "scoring bread", "tempering chocolate", "blanching vegetables",
    "shocking vegetables", "sautéing versus stir-frying",
    "dry-rubbing versus marinating", "brining tofu", "pressing tofu",
    "charring vegetables on open flame", "steaming versus boiling",
    "pressure-cooking lentils", "pre-soaking lentils", "sprouting legumes",
    "fermenting batter for dosa/idli", "making cashew cream", "making a flax egg",
    "making aquafaba meringue", "reducing a sauce",
    "clarifying butter (or making vegan brown butter)",
    "folding versus stirring batter", "resting bread dough", "resting cooked rice",
    "finishing with acid", "finishing with fresh herbs",
    "balancing salt-sweet-acid-fat-heat",
]

NUTRITION_TARGETS = [
    "high-protein", "low-carb", "iron-rich", "calcium-rich", "high-fiber",
    "low-sodium", "anti-inflammatory", "energy-dense", "light/low-calorie",
    "gut-healthy",
]

CUISINES = [
    "Indian", "South Indian", "North Indian", "Bengali", "Gujarati",
    "Hyderabadi", "Maharashtrian", "Kashmiri", "Bihari", "North East Indian",
    "Mediterranean", "Italian", "Thai", "Japanese", "Mexican",
    "Middle Eastern", "Lebanese", "Chinese", "Korean", "American",
]

MEAL_TYPES = [
    "breakfast", "lunch", "dinner", "snack", "brunch",
    "dessert", "post-workout meal", "lunchbox meal",
]

INDIAN_SOUTH_BREAKFAST_DISHES = [
    "masala dosa", "plain dosa", "rava dosa", "neer dosa",
    "idli", "rava idli", "mini idli with sambar",
    "upma", "semiya upma", "ven pongal", "sweet pongal (sakkarai pongal)",
    "Pesarattu (Andhra moong dosa)",
]

INDIAN_TARGET_INGREDIENTS = ["paneer", "ghee", "malai (clotted cream)", "khoya (reduced milk solids)"]

INDIAN_CURRY_NAMES = [
    "palak paneer", "paneer butter masala", "kadai paneer", "paneer tikka masala",
    "shahi paneer", "matar paneer", "methi malai paneer", "dum aloo",
    "navratan korma", "malai kofta", "paneer pasanda", "paneer bhurji",
    "paneer makhani", "paneer do pyaza", "restaurant-style dal makhani",
]

# state → list of dal names belonging to that regional tradition
INDIAN_STATE_TO_DALS = {
    "Tamil Nadu":      ["sambar", "rasam", "Tamil Nadu poricha kuzhambu"],
    "Andhra Pradesh":  ["pappu (Andhra-style)"],
    "Hyderabad":       ["Hyderabadi khatti dal"],
    "Bengal":          ["Bengali cholar dal"],
    "Gujarat":         ["Gujarati dal (sweet-sour)"],
    "Punjab":          ["Punjabi dal makhani"],
    "Maharashtra":     ["Maharashtrian varan"],
    "Rajasthan":       ["Rajasthani panchmel dal"],
    "Kerala":          ["Kerala parippu curry"],
    "Kashmir":         ["Kashmiri yakhni-style dal"],
}

INDIAN_COMPARISON_DALS = [
    "North Indian dal tadka", "dal fry", "plain yellow moong dal",
    "plain toor dal", "urad dal",
]

# Per-target dietary directive — injected into adaptation-style user turns so the
# rule sits next to the task, not buried in the system prompt. The system-prompt
# rule alone proved too weak to override the model's "adapt = make plant-based"
# prior on vegetarian-target requests (smoke-100 v2).
DIETARY_DIRECTIVE = {
    "vegan":      "(REMOVE all animal products — dairy, eggs, honey, gelatin.)",
    "vegetarian": "(REMOVE only meat, poultry, fish, and gelatin. KEEP dairy and eggs unchanged — do NOT substitute them.)",
}

INDIAN_SWEETS = [
    # Pan-Indian base
    "besan laddoo", "motichoor laddoo", "coconut laddoo",
    "gajar ka halwa (carrot halwa)", "sooji halwa", "moong dal halwa",
    "badam halwa", "kheer (rice pudding)", "seviyan (vermicelli pudding)",
    "phirni", "sandesh", "rasmalai", "jalebi", "gulab jamun", "peda",
    # Bihar / Gaya additions
    "thekua (Bihar/Gaya, Chhath festival)", "tilkut (Gaya sesame-jaggery winter sweet)",
    "khaja (Silao/Bihar layered sweet)", "anarsa (Bihar)",
    "laung-latika (Bihar/UP)",
    # Varanasi / Banaras additions
    "malaiyo (Banarasi winter milk-foam dessert)", "Banarasi launglata",
    "Banarasi peda", "makhan-malai (Varanasi)",
]

# -----------------------------------------------------------------------------
# Generation logic
# -----------------------------------------------------------------------------

# Template share within the non-Indian half (must sum to 1.0).
BASE_TEMPLATE_WEIGHTS = {
    "substitution":        1.0,
    "adaptation":          1.0,
    "constraint_cooking":  1.0,
    "nutrition_target":    1.0,
    "technique_explainer": 1.0,
    "cross_cuisine":       1.0,
}

# Template share within the Indian half (must sum to 1.0).
INDIAN_TEMPLATE_WEIGHTS = {
    "indian_south_breakfast":           1.0,
    "indian_paneer_ghee_substitution":  1.0,
    "indian_regional_dal":              1.0,
    "indian_sweet_adaptation":          1.0,
}


def normalize(weights: dict) -> dict:
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()}


def weighted_pick(rng: random.Random, weights: dict) -> str:
    items = list(weights.items())
    r = rng.random()
    acc = 0.0
    for k, w in items:
        acc += w
        if r < acc:
            return k
    return items[-1][0]


def pick_dietary(rng: random.Random, split: dict) -> str:
    return weighted_pick(rng, split)


def short_grounding_excerpt(row: dict, max_chars: int = 350) -> str:
    """Build a compact prose excerpt from a 023 grounding row for use in a prompt.
    023 schema uses ingredients_raw + steps_raw (fallback to older field names
    kept for robustness against schema drift)."""
    parts = []
    title = row.get("title") or row.get("name") or row.get("recipe_name") or ""
    if title:
        parts.append(title.strip())
    ingredients = (row.get("ingredients_raw")
                   or row.get("ingredients")
                   or row.get("ingredient_list")
                   or [])
    if isinstance(ingredients, list) and ingredients:
        head = ", ".join(str(i).strip() for i in ingredients[:8])
        parts.append("Ingredients: " + head)
    steps = (row.get("steps_raw")
             or row.get("method")
             or row.get("instructions")
             or row.get("steps")
             or "")
    if isinstance(steps, list) and steps:
        steps = steps[0]
    if isinstance(steps, str) and steps:
        parts.append("Method: " + steps.strip())
    excerpt = " | ".join(parts)
    if len(excerpt) > max_chars:
        excerpt = excerpt[: max_chars - 1] + "…"
    excerpt = excerpt.replace('"', "'")
    return excerpt or "(no description available)"


def has_content(row: dict) -> bool:
    """True if the row has either ingredients or method steps populated."""
    ing = row.get("ingredients_raw") or row.get("ingredients") or row.get("ingredient_list") or []
    steps = row.get("steps_raw") or row.get("method") or row.get("instructions") or row.get("steps") or ""
    return bool((isinstance(ing, list) and ing) or (isinstance(steps, list) and steps) or (isinstance(steps, str) and steps))


def filter_for_adaptation(rows: list, target_diet: str) -> list:
    """Pick grounding rows whose diet_tag means adaptation is non-trivial.
    Target vegan → adapt from {vegetarian, non_vegetarian} (remove dairy/eggs/meat).
    Target vegetarian → adapt from non_vegetarian only (vegetarian→vegetarian is a no-op)."""
    if target_diet == "vegan":
        return [r for r in rows if r.get("diet_tag") in {"vegetarian", "non_vegetarian"}]
    if target_diet == "vegetarian":
        return [r for r in rows if r.get("diet_tag") == "non_vegetarian"]
    return rows


def filter_for_constraint_cooking(rows: list, target_diet: str) -> list:
    """Pick rows whose ingredient pool is already compatible with target diet."""
    if target_diet == "vegan":
        return [r for r in rows if r.get("diet_tag") == "vegan"]
    if target_diet == "vegetarian":
        return [r for r in rows if r.get("diet_tag") in {"vegan", "vegetarian"}]
    return rows


def load_jsonl(path: Path) -> list:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def fill_template(
    template_name: str,
    template_def: dict,
    dietary: str,
    rng: random.Random,
    grounding_pools: dict,
) -> tuple[str, dict, int | None]:
    """Return (user_turn_text, placeholders_dict, grounding_row_id_or_None)."""
    placeholders: dict = {"dietary_preference": dietary}
    grounding_row_id: int | None = None

    if template_name == "substitution":
        placeholders["ingredient"] = rng.choice(INGREDIENTS)
        placeholders["recipe_context"] = rng.choice(RECIPE_CONTEXTS)

    elif template_name == "adaptation":
        pool = grounding_pools[f"adaptation_{dietary}"]
        idx = rng.randrange(len(pool))
        row = pool[idx]
        grounding_row_id = row.get("_orig_idx")
        placeholders["grounding_excerpt"] = short_grounding_excerpt(row)
        placeholders["dietary_directive"] = DIETARY_DIRECTIVE[dietary]

    elif template_name == "constraint_cooking":
        pool = grounding_pools[f"constraint_{dietary}"]
        idx = rng.randrange(len(pool))
        row = pool[idx]
        grounding_row_id = row.get("_orig_idx")
        ingredients = row.get("ingredients_raw") or row.get("ingredients") or row.get("ingredient_list") or []
        if isinstance(ingredients, list) and ingredients:
            k = min(len(ingredients), rng.randint(3, 5))
            sampled = rng.sample(ingredients, k)
            placeholders["ingredient_list"] = ", ".join(str(i).strip() for i in sampled)
        else:
            placeholders["ingredient_list"] = "tomatoes, onions, garlic, ginger"

    elif template_name == "nutrition_target":
        placeholders["nutrition_target"] = rng.choice(NUTRITION_TARGETS)
        placeholders["cuisine"] = rng.choice(CUISINES)
        placeholders["meal_type"] = rng.choice(MEAL_TYPES)

    elif template_name == "technique_explainer":
        placeholders["technique"] = rng.choice(TECHNIQUES)

    elif template_name == "cross_cuisine":
        placeholders["cuisine_a"] = rng.choice(CUISINES)
        pool = grounding_pools["cross_cuisine"]
        idx = rng.randrange(len(pool))
        row = pool[idx]
        grounding_row_id = row.get("_orig_idx")
        placeholders["grounding_excerpt"] = short_grounding_excerpt(row)

    elif template_name == "indian_south_breakfast":
        placeholders["dish"] = rng.choice(INDIAN_SOUTH_BREAKFAST_DISHES)

    elif template_name == "indian_paneer_ghee_substitution":
        placeholders["target_ingredient"] = rng.choice(INDIAN_TARGET_INGREDIENTS)
        placeholders["curry_name"] = rng.choice(INDIAN_CURRY_NAMES)

    elif template_name == "indian_regional_dal":
        state = rng.choice(list(INDIAN_STATE_TO_DALS.keys()))
        placeholders["state"] = state
        placeholders["dal_name"] = rng.choice(INDIAN_STATE_TO_DALS[state])
        placeholders["comparison_dal"] = rng.choice(INDIAN_COMPARISON_DALS)

    elif template_name == "indian_sweet_adaptation":
        placeholders["sweet_name"] = rng.choice(INDIAN_SWEETS)
        placeholders["dietary_directive"] = DIETARY_DIRECTIVE[dietary]

    else:
        raise ValueError(f"Unknown template: {template_name}")

    user_turn = template_def["user_turn_template"].format(**placeholders)
    return user_turn, placeholders, grounding_row_id


def parse_split(spec: str) -> dict:
    """Parse 'vegan=0.4,vegetarian=0.6' into a normalized dict."""
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    out = {}
    for p in parts:
        k, v = p.split("=")
        out[k.strip()] = float(v)
    return normalize(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--templates", required=True, type=Path,
                    help="Path to templates.json")
    ap.add_argument("--n", required=True, type=int,
                    help="Number of prompts to generate")
    ap.add_argument("--grounding", required=True, type=Path,
                    help="Directory containing 023's deduped JSONL files")
    ap.add_argument("--indian-cuisine-fraction", type=float, default=0.35,
                    help="Fraction of total prompts that go to Indian-subset templates")
    ap.add_argument("--dietary-split", type=str, default="vegan=0.4,vegetarian=0.6",
                    help="Per-dietary-tag split, e.g. 'vegan=0.4,vegetarian=0.6'")
    ap.add_argument("--out", required=True, type=Path,
                    help="Output JSONL path")
    ap.add_argument("--seed", type=int, default=42,
                    help="RNG seed for deterministic prompt generation")
    args = ap.parse_args()

    rng = random.Random(args.seed)

    templates_doc = json.loads(args.templates.read_text(encoding="utf-8"))
    template_defs = templates_doc["templates"]

    # Load + tag with original row index for traceability
    gc_rows = load_jsonl(args.grounding / "grounding_context.jsonl")
    sr_rows = load_jsonl(args.grounding / "structured_recipes_diet_friendly.jsonl")
    for i, r in enumerate(gc_rows):
        r["_orig_idx"] = i
    for i, r in enumerate(sr_rows):
        r["_orig_idx"] = i

    # Drop title-only rows (insufficient context for adaptation/cross_cuisine)
    gc_rich = [r for r in gc_rows if has_content(r)]
    sr_rich = [r for r in sr_rows if has_content(r)]
    print(f"[build_prompts] grounding_context: {len(gc_rows)} loaded → {len(gc_rich)} with content", file=sys.stderr)
    print(f"[build_prompts] structured_recipes: {len(sr_rows)} loaded → {len(sr_rich)} with content", file=sys.stderr)

    # Pre-compute per-target-diet sub-pools so sampling is O(1) per request.
    grounding_pools = {
        "adaptation_vegan":       filter_for_adaptation(gc_rich, "vegan"),
        "adaptation_vegetarian":  filter_for_adaptation(gc_rich, "vegetarian"),
        "constraint_vegan":       filter_for_constraint_cooking(sr_rich, "vegan"),
        "constraint_vegetarian":  filter_for_constraint_cooking(sr_rich, "vegetarian"),
        "cross_cuisine":          gc_rich,  # no diet filter — cuisine adaptation only
    }
    for key, pool in grounding_pools.items():
        print(f"[build_prompts]   pool '{key}': {len(pool)} rows", file=sys.stderr)
        if not pool:
            sys.exit(f"[build_prompts] ERROR: pool '{key}' is empty after filtering — cannot continue")

    dietary_split = parse_split(args.dietary_split)
    base_weights = normalize(BASE_TEMPLATE_WEIGHTS)
    indian_weights = normalize(INDIAN_TEMPLATE_WEIGHTS)

    n_indian = int(round(args.n * args.indian_cuisine_fraction))
    n_base = args.n - n_indian
    print(f"[build_prompts] total={args.n}  base={n_base}  indian={n_indian}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with args.out.open("w", encoding="utf-8") as out_f:
        # Generate base-template requests
        for _ in range(n_base):
            tmpl = weighted_pick(rng, base_weights)
            dietary = pick_dietary(rng, dietary_split)
            user_turn, placeholders, grounding_id = fill_template(
                tmpl, template_defs[tmpl], dietary, rng, grounding_pools)
            written += 1
            out_f.write(json.dumps({
                "request_id":         f"025-{written:06d}",
                "template":           tmpl,
                "dietary_preference": dietary,
                "user_turn":          user_turn,
                "placeholders":       placeholders,
                "grounding_row_id":   grounding_id,
            }) + "\n")

        # Generate Indian-subset requests
        for _ in range(n_indian):
            tmpl = weighted_pick(rng, indian_weights)
            dietary = pick_dietary(rng, dietary_split)
            user_turn, placeholders, grounding_id = fill_template(
                tmpl, template_defs[tmpl], dietary, rng, grounding_pools)
            written += 1
            out_f.write(json.dumps({
                "request_id":         f"025-{written:06d}",
                "template":           tmpl,
                "dietary_preference": dietary,
                "user_turn":          user_turn,
                "placeholders":       placeholders,
                "grounding_row_id":   grounding_id,
            }) + "\n")

    print(f"[build_prompts] wrote {written} prompts to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
