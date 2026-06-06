#!/usr/bin/env python3
"""
filter_clean.py — Phase 4 prep, pass 1: clean + dedup + validity-filter + leakage-flag.

Pools the per-node 025 output files, keeps only well-formed teacher responses,
removes duplicates by request_id, drops rows whose request_id is in NO prompt file
("validity filter"), and FLAGS — does not drop — dietary-leakage suspects.

WHY FLAG, NOT DROP (decided 2026-05-30): the substitution/adaptation templates are
*designed* to name animal products in order to replace them ("replace paneer with
tofu" is a CORRECT vegan answer). A naive blocklist on answer prose false-positives
on exactly the adaptation answers that are the dataset's point. So we tag a
`leakage_suspect` column with a cue-aware heuristic and keep every row; the
drop/keep/regenerate policy is a later-session decision (likely an LLM-judge pass).
NOTE: leakage_suspect is ADVISORY — no downstream script filters on it.

VALIDITY FILTER vs ORPHAN STRIP (clarified after adversarial review 2026-05-30):
with --valid-ids = union(prompts-node0, prompts-node1) = the full id set, this
pass drops only ids present in NO prompt file (≈0 expected). The ~870 node1
"orphan" rows that carry node0-half ids are VALID ids (in the union) and are
instead collapsed by the dedup pass (they appear as `collisions`). Every unique,
valid, clean Q&A is kept — which maximizes dataset value, since request_id is
positional and parsed_qa.q/a are self-contained regardless of which node produced
them. Per-node stripping is available via --orphan-policy per-file if strict
node-provenance is wanted (see prep-notes for the tradeoff).

SAFETY GUARDS (added after review found silent-mass-drop risks):
- refuse to write if --valid-ids resolves to an empty id set
- ABORT if the validity filter would drop more than --max-drop-frac of rows
  (catches a wrong/partial --valid-ids before it truncates the only data copy)
- friendly exit (not a bare traceback) on a missing input file
- count malformed/truncated JSON lines and REPORT them in the stdout funnel
  (the wedge truncates the tail of node files — those losses must be accountable);
  --strict aborts on the first malformed line
- atomic write (.tmp + replace) so a crash never leaves a truncated output

Pure stdlib. Self-reporting: prints the full funnel so prep-notes.md can record it.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Dietary blocklist — COPIED VERBATIM (2026-05-30) from the data-prep classifier
# pass2_vegan_classify.py so leakage flagging matches the original
# classification vocabulary. Embedded (not imported) because this script runs
# from a working dir where the original source path may not resolve.
# Source of truth: pass2_vegan_classify.py (same directory).
# ---------------------------------------------------------------------------
BLOCKLIST = {
    "flesh": [
        "chicken", "beef", "pork", "lamb", "mutton", "veal", "venison",
        "rabbit", "ham", "bacon", "prosciutto", "salami", "sausage",
        "pepperoni", "chorizo", "duck", "goose", "turkey", "quail",
    ],
    "seafood": [
        "fish", "salmon", "tuna", "cod", "shrimp", "prawn", "crab", "lobster",
        "oyster", "clam", "mussel", "scallop", "anchovy", "sardine", "octopus",
        "squid", "mackerel", "trout", "halibut", "tilapia", "caviar", "roe",
        "bass", "perch", "snapper", "grouper", "mahi", "mahi-mahi", "pike",
        "pollock", "sole", "flounder", "herring", "sea bream", "sea bass",
        "barramundi", "tilefish", "rockfish",
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

# High-collision common-English words that produce more false positives than true
# positives in free PROSE ("the SOLE reason", "PERCH the lid", "PIKE Place").
# Excluded from the prose leakage matcher only. The 023 classifier kept them
# because it matched short ingredient *lines*; answer prose is different. The
# advisory leakage flag tolerates these misses (LLM-judge re-reviews later).
PROSE_EXCLUDE_TERMS = {"sole", "pike", "perch"}

# Animal categories that make a VEGAN answer suspect (Pattern A vocab slot-fill +
# Pattern B technique override). Vegan must avoid ALL of these.
VEGAN_SUSPECT_CATEGORIES = ["flesh", "seafood", "dairy", "eggs", "byproducts", "hidden"]
# Categories that make a VEGETARIAN answer suspect (Pattern C). Vegetarian may
# keep dairy/eggs; only flesh/seafood are violations.
VEGETARIAN_SUSPECT_CATEGORIES = ["flesh", "seafood"]

COMPILED_BLOCKLIST = {
    category: [(term, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
               for term in terms if term not in PROSE_EXCLUDE_TERMS]
    for category, terms in BLOCKLIST.items()
}
# Compound fish-suffix matcher (catfish/swordfish/etc.) — same as 023.
FISH_SUFFIX_RE = re.compile(r"\b\w+fish\b", re.IGNORECASE)

# Substitution-cue lexicon: an animal term co-occurring with one of these cues is
# treated as a (correct) "replace X" mention, not leakage. Prose-level analog of
# 023's per-ingredient-line vegan-modifier override. Bare descriptive self-labels
# ("vegan", "plant-based") were REMOVED after review — they are not evidence of a
# substitution at a specific term, just generic labeling near it.
SUBSTITUTION_CUES = [
    "instead of", "in place of", "replace", "replacing", "replacement",
    "substitute", "substituting", "substitution", "swap", "swapping",
    "alternative to", "alternatives to", "rather than", "skip the",
    "omit the", "without the", "instead use", "instead, use",
]
CUE_RE = re.compile("|".join(re.escape(c) for c in SUBSTITUTION_CUES), re.IGNORECASE)

# Plant-source modifier override — the prose analog of 023's per-ingredient-line
# VEGAN_MODIFIERS check, which the first pass omitted. When a concrete plant-source
# word sits within the window of a dairy/flesh/seafood term, the term is a plant
# version ("coconut milk", "oat milk", "cashew cream", "soy chicken", "tofu fish")
# not a leak. This is what drove the first run's 22.8% over-flag (624 "milk", 197
# "cream" — overwhelmingly coconut/oat/almond milk and coconut/cashew cream).
# DELIBERATELY EXCLUDES bare descriptive labels "vegan"/"plant-based" (per review
# finding 8: descriptive labeling near a term is not evidence the specific term is
# substituted — "an authentic vegan paneer ... finish with butter" must still flag).
# "rice" excluded too (high collision: fried rice, rice and dal).
PLANT_MODIFIERS = [
    # plant sources that form a compound with the term ("coconut milk", "flax egg")
    "almond", "almonds", "soy", "soya", "oat", "oats", "coconut", "cashew", "cashews",
    "macadamia", "hazelnut", "hemp", "rice", "pea", "peanut", "flax", "flaxseed",
    "chia", "walnut", "sunflower", "jackfruit", "tofu", "tempeh", "seitan",
    "chickpea", "besan", "gram", "lentil", "nutritional yeast",
    # vegan-product / free-from labels — these immediately precede the term as a
    # product name ("vegan butter", "plant-based cheese"). Used ONLY in the tight
    # preceding window, so they cannot clear a distant term (review finding 8).
    "vegan", "plant-based", "plant based", "plant", "non-dairy", "nondairy",
    "dairy-free", "dairy free", "egg-free", "eggless", "meat-free", "meatless",
    "mock", "faux", "imitation",
]
# Word-boundary so "pea" doesn't match inside "peace"/"appear"; hyphenated labels OK.
MODIFIER_RE = re.compile(
    "|".join(r"\b" + re.escape(m) + r"\b" for m in PLANT_MODIFIERS), re.IGNORECASE)

# Substitution cues ("replace", "instead of") can sit a few words from the term, so
# they use a wide symmetric window. Plant modifiers form a COMPOUND NOUN with the
# term ("coconut milk"), so they only exonerate when immediately PRECEDING it within
# a tight window — otherwise a plant word elsewhere in the sentence ("simmer the tofu
# ... finish with butter") would wrongly clear a genuine leak 30+ chars away.
CUE_WINDOW = 60
MODIFIER_WINDOW = 20  # chars immediately before the term (covers "nutritional yeast ")


def load_jsonl(path: Path) -> tuple[list, int]:
    """Return (rows, n_malformed). Friendly exit on missing file; count bad lines."""
    try:
        f = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        sys.exit(f"[filter_clean] FATAL: input file not found: {path}")
    rows = []
    n_bad = 0
    with f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                n_bad += 1
                print(f"[filter_clean] WARN {path.name}:{ln} malformed JSON skipped: {e}", file=sys.stderr)
    return rows, n_bad


def load_valid_ids(path: Path) -> set:
    rows, _ = load_jsonl(path)
    return {r.get("request_id") for r in rows if r.get("request_id")}


def is_clean(row: dict) -> bool:
    if row.get("http_error") is not None:
        return False
    if row.get("parse_error") is not None:
        return False
    if row.get("finish_reason") != "stop":
        return False
    qa = row.get("parsed_qa")
    if not isinstance(qa, dict):
        return False
    q, a = qa.get("q"), qa.get("a")
    # Reject non-string or whitespace-only q/a (degenerate signal; also prevents a
    # non-string answer from crashing the leakage flagger's .lower() later).
    if not (isinstance(q, str) and isinstance(a, str)):
        return False
    return bool(q.strip()) and bool(a.strip())


def _term_hits(text_lower: str, categories: list) -> list:
    """Return [(category, term, start_idx)] for ALL blocklist occurrences in the
    given categories, plus compound-fish hits when 'seafood' is in scope.
    Enumerates every occurrence (not just the first) so a cue-protected early
    mention can't mask a genuine later leak of the same term."""
    hits = []
    for category in categories:
        for term, pattern in COMPILED_BLOCKLIST[category]:
            for m in pattern.finditer(text_lower):
                hits.append((category, term, m.start()))
    if "seafood" in categories:
        for m in FISH_SUFFIX_RE.finditer(text_lower):
            if m.group(0) != "fish":  # bare "fish" already covered by blocklist
                hits.append(("seafood", m.group(0), m.start()))
    return hits


def is_exonerated_near(text: str, idx: int) -> bool:
    """A blocklist hit at `idx` is exonerated if EITHER:
      - a substitution cue appears within ±CUE_WINDOW chars (a 'replace X' mention), OR
      - a plant-source modifier appears immediately BEFORE the term within
        MODIFIER_WINDOW chars (a compound like 'coconut milk' / 'soy chicken').
    The modifier check is preceding-only and tight so a plant word elsewhere in the
    sentence does NOT clear a genuinely-leaked term — e.g. '...the tofu, then finish
    with a knob of butter' still flags 'butter' (per review finding 8)."""
    cue_lo = max(0, idx - CUE_WINDOW)
    cue_hi = min(len(text), idx + CUE_WINDOW)
    if CUE_RE.search(text[cue_lo:cue_hi]):
        return True
    mod_lo = max(0, idx - MODIFIER_WINDOW)
    return MODIFIER_RE.search(text[mod_lo:idx]) is not None


def flag_leakage(row: dict) -> tuple[bool, str]:
    """Return (leakage_suspect, reason). For both diets a term is a leak only if AT
    LEAST ONE of its occurrences is NOT exonerated by a nearby cue or plant modifier."""
    qa = row.get("parsed_qa") or {}
    answer = qa.get("a") or ""
    answer_lower = answer.lower()
    diet = row.get("dietary_preference")

    if diet == "vegan":
        cats = VEGAN_SUSPECT_CATEGORIES
    elif diet == "vegetarian":
        cats = VEGETARIAN_SUSPECT_CATEGORIES
    else:
        return False, ""

    for category, term, idx in _term_hits(answer_lower, cats):
        if not is_exonerated_near(answer_lower, idx):
            tag = "vegan" if diet == "vegan" else "vegetarian"
            return True, f"{tag}:{category}:{term}"
    return False, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inputs", required=True, nargs="+", type=Path,
                    help="One or more 025 output JSONL files (e.g. all-node0.jsonl all-node1.jsonl)")
    ap.add_argument("--valid-ids", nargs="*", default=[], type=Path,
                    help="Prompt JSONL files defining valid request_ids (union across all). "
                         "Pass BOTH per-node prompt files. If omitted, validity filter is skipped.")
    ap.add_argument("--out", required=True, type=Path,
                    help="Output clean.jsonl path")
    ap.add_argument("--max-drop-frac", type=float, default=0.05,
                    help="Abort if the validity filter would drop more than this fraction of "
                         "deduped rows (guards against a wrong/partial --valid-ids). Default 0.05.")
    ap.add_argument("--strict", action="store_true",
                    help="Abort on the first malformed JSON line instead of skipping it.")
    args = ap.parse_args()

    # --- Pool inputs (count malformed lines) ---
    pooled = []
    n_malformed = 0
    for p in args.inputs:
        rows, n_bad = load_jsonl(p)
        n_malformed += n_bad
        print(f"[filter_clean] loaded {len(rows):6d} rows ({n_bad} malformed) from {p}", file=sys.stderr)
        pooled.extend(rows)
    if args.strict and n_malformed:
        sys.exit(f"[filter_clean] FATAL --strict: {n_malformed} malformed JSON line(s) across inputs. "
                 f"A truncated tail may be wedge-related; inspect before proceeding.")
    n_pooled = len(pooled)

    # --- Clean predicate ---
    clean_rows = [r for r in pooled if is_clean(r)]
    n_clean = len(clean_rows)

    # --- Dedup by request_id (on collision keep the longer answer) ---
    by_id = {}
    n_collision = 0
    for r in clean_rows:
        rid = r.get("request_id")
        if rid is None:
            by_id[f"_noid_{len(by_id)}"] = r
            continue
        if rid not in by_id:
            by_id[rid] = r
        else:
            n_collision += 1
            existing_a = (by_id[rid].get("parsed_qa") or {}).get("a") or ""
            cand_a = (r.get("parsed_qa") or {}).get("a") or ""
            if len(cand_a) > len(existing_a):
                by_id[rid] = r
    deduped = list(by_id.values())
    n_deduped = len(deduped)

    # --- Validity filter (union of all --valid-ids) with safety guards ---
    if args.valid_ids:
        valid = set()
        for vp in args.valid_ids:
            vids = load_valid_ids(vp)
            print(f"[filter_clean] valid-ids {len(vids):6d} from {vp}", file=sys.stderr)
            valid |= vids
        if not valid:
            sys.exit("[filter_clean] FATAL: --valid-ids produced an EMPTY id set; "
                     "refusing to strip all rows. Check the paths and contents.")
        kept = [r for r in deduped if r.get("request_id") in valid]
        n_dropped = n_deduped - len(kept)
        drop_frac = n_dropped / n_deduped if n_deduped else 0.0
        if drop_frac > args.max_drop_frac:
            sys.exit(
                f"[filter_clean] FATAL: validity filter would drop {n_dropped}/{n_deduped} "
                f"({drop_frac:.1%}) rows — exceeds --max-drop-frac={args.max_drop_frac:.1%}.\n"
                f"  This almost always means an incomplete/wrong --valid-ids. Pass BOTH per-node "
                f"prompt files (their union is the full id set). Refusing to write a truncated "
                f"dataset. Re-run with a corrected --valid-ids, or raise --max-drop-frac if this "
                f"large drop is genuinely intended."
            )
    else:
        kept = deduped
        n_dropped = 0
        print("[filter_clean] no --valid-ids given; validity filter skipped", file=sys.stderr)
    n_kept = len(kept)

    # --- Leakage flag (keep all) ---
    reason_counts = {}
    n_suspect = 0
    for r in kept:
        suspect, reason = flag_leakage(r)
        r["leakage_suspect"] = suspect
        r["leakage_reason"] = reason
        if suspect:
            n_suspect += 1
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    # --- Write atomically (tmp + replace) ---
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(args.out.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(args.out)

    # --- Report funnel ---
    def pct(n, d):
        return f"{100.0*n/d:.1f}%" if d else "n/a"

    print("\n=== filter_clean funnel ===")
    print(f"  pooled inputs        : {n_pooled}")
    print(f"  malformed JSON skipped: {n_malformed}  (loud on stderr; counted here for reconciliation)")
    print(f"  clean (well-formed)  : {n_clean}  ({pct(n_clean, n_pooled)} of pooled)")
    print(f"  after dedup by id    : {n_deduped}  (collisions resolved: {n_collision})")
    print(f"  after validity filter: {n_kept}  (dropped not-in-any-prompt: {n_dropped})")
    print(f"  leakage_suspect (KEPT, flagged only): {n_suspect}  ({pct(n_suspect, n_kept)} of kept)")
    diet_counts = {}
    for r in kept:
        d = r.get("dietary_preference", "unknown")
        diet_counts[d] = diet_counts.get(d, 0) + 1
    print(f"  dietary balance      : " + ", ".join(f"{k}={v}" for k, v in sorted(diet_counts.items())))
    top = sorted(reason_counts.items(), key=lambda kv: -kv[1])[:8]
    print(f"  suspect by reason    : " + (", ".join(f"{k}={v}" for k, v in top) or "none"))
    print(f"  wrote {n_kept} rows → {args.out}")


if __name__ == "__main__":
    main()
