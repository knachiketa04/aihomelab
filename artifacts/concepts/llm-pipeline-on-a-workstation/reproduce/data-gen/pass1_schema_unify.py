#!/usr/bin/env python3
"""
Pass 1 — Schema unify.

Reads from /data/raw/{wikibooks,gutenberg,wikipedia}/
Writes to /data/unified/unified.jsonl

Lands all three sources in one unified schema with a `content_type` discriminator:

  {
    "id": "<source>::<row_id>",
    "source": "wikibooks|gutenberg|wikipedia",
    "content_type": "structured_recipe|narrative_recipe|recipe_context",
    "title": "...",
    "ingredients_raw": [...] | null,   # populated only for structured_recipe
    "steps_raw": [...] | null,         # populated only for structured_recipe
    "text": "..." | null,              # populated for narrative_recipe / recipe_context
    "url": "...",
    "license": "CC-BY-SA-4.0 | PD-US",
    "source_metadata": {...}
  }

Pure stdlib. Single-pass per source. Designed for ~4K total rows; not parallelized.
"""

import json
import re
import sys
from pathlib import Path

RAW_ROOT = Path("/data/raw")
OUT_PATH = Path("/data/unified/unified.jsonl")

INGREDIENT_RE = re.compile(r"^\s*ingredient[s]?[:\s]*$", re.IGNORECASE)
PROCEDURE_RE = re.compile(
    r"^\s*(procedure[s]?(\[\d+\])?|preparation|instruction[s]?|method|direction[s]?|step[s]?)[:\s]*$",
    re.IGNORECASE,
)
WIKIPEDIA_LIST_RE = re.compile(r"^list of\b", re.IGNORECASE)


def extract_wikibooks(jsonl_path):
    with open(jsonl_path) as f:
        for line in f:
            row = json.loads(line)
            rd = row.get("recipe_data") or {}
            infobox = rd.get("infobox") or {}
            text_lines = rd.get("text_lines") or []
            ingredients_raw = []
            steps_raw = []
            for tl in text_lines:
                if not isinstance(tl, dict):
                    continue
                section = (tl.get("section") or "").strip()
                line_type = tl.get("line_type")
                text = (tl.get("text") or "").strip()
                if not text:
                    continue
                if line_type == "ul" and INGREDIENT_RE.match(section):
                    ingredients_raw.append(text)
                elif line_type == "ol" and PROCEDURE_RE.match(section):
                    steps_raw.append(text)
            yield {
                "id": f"wikibooks::{row.get('filename', 'unknown')}",
                "source": "wikibooks",
                "content_type": "structured_recipe",
                "title": (rd.get("title") or "").strip(),
                "ingredients_raw": ingredients_raw,
                "steps_raw": steps_raw,
                "text": None,
                "url": rd.get("url"),
                "license": "CC-BY-SA-4.0",
                "source_metadata": {
                    "infobox_category": infobox.get("category"),
                    "servings": infobox.get("servings"),
                    "time": infobox.get("time"),
                    "difficulty": infobox.get("difficulty"),
                    "filename": row.get("filename"),
                },
            }


def extract_gutenberg(dir_path):
    for txt_path in sorted(dir_path.glob("*.txt")):
        try:
            text = txt_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  WARN: failed to read {txt_path}: {e}", file=sys.stderr)
            continue
        # Parse Gutenberg header for the canonical title; fall back to filename stem.
        title = txt_path.stem
        for line in text.splitlines()[:50]:
            stripped = line.strip()
            if stripped.lower().startswith("title:"):
                title = stripped[6:].strip()
                break
        book_id_match = re.match(r"book-(\d+)", txt_path.stem)
        book_id = book_id_match.group(1) if book_id_match else None
        yield {
            "id": f"gutenberg::{txt_path.stem}",
            "source": "gutenberg",
            "content_type": "narrative_recipe",
            "title": title,
            "ingredients_raw": None,
            "steps_raw": None,
            "text": text,
            "url": f"https://www.gutenberg.org/ebooks/{book_id}" if book_id else None,
            "license": "PD-US",
            "source_metadata": {
                "filename": txt_path.name,
                "book_id": book_id,
                "byte_size": txt_path.stat().st_size,
            },
        }


def extract_wikipedia(jsonl_path):
    """Yields kept rows; tracks dropped (list-article) count via closure on counter dict."""
    with open(jsonl_path) as f:
        for line in f:
            row = json.loads(line)
            title = (row.get("title") or "").strip()
            if WIKIPEDIA_LIST_RE.match(title):
                continue
            yield {
                "id": f"wikipedia::{row.get('url', title)}",
                "source": "wikipedia",
                "content_type": "recipe_context",
                "title": title,
                "ingredients_raw": None,
                "steps_raw": None,
                "text": row.get("text"),
                "url": row.get("url"),
                "license": "CC-BY-SA-4.0",
                "source_metadata": {
                    "category": row.get("category"),
                },
            }


def main():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    counts = {"wikibooks": 0, "gutenberg": 0, "wikipedia_kept": 0, "wikipedia_dropped": 0}
    wb_yield = {"with_ingredients": 0, "with_steps": 0, "both_empty": 0}

    with open(OUT_PATH, "w") as out:
        for row in extract_wikibooks(RAW_ROOT / "wikibooks" / "wikibooks.jsonl"):
            counts["wikibooks"] += 1
            if row["ingredients_raw"]:
                wb_yield["with_ingredients"] += 1
            if row["steps_raw"]:
                wb_yield["with_steps"] += 1
            if not row["ingredients_raw"] and not row["steps_raw"]:
                wb_yield["both_empty"] += 1
            out.write(json.dumps(row, ensure_ascii=False) + "\n")

        for row in extract_gutenberg(RAW_ROOT / "gutenberg"):
            counts["gutenberg"] += 1
            out.write(json.dumps(row, ensure_ascii=False) + "\n")

        wp_path = RAW_ROOT / "wikipedia" / "recipes.jsonl"
        for row in extract_wikipedia(wp_path):
            counts["wikipedia_kept"] += 1
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
        with open(wp_path) as f:
            wp_total = sum(1 for _ in f)
        counts["wikipedia_dropped"] = wp_total - counts["wikipedia_kept"]

    total = counts["wikibooks"] + counts["gutenberg"] + counts["wikipedia_kept"]
    print("=== Pass 1 schema-unify complete ===")
    print(f"Output: {OUT_PATH}  ({OUT_PATH.stat().st_size:,} bytes)")
    print(f"Total rows written: {total}")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print(f"=== Wikibooks extractor yield (of {counts['wikibooks']} rows) ===")
    for k, v in wb_yield.items():
        pct = 100.0 * v / counts["wikibooks"] if counts["wikibooks"] else 0.0
        print(f"  {k}: {v} ({pct:.1f}%)")


if __name__ == "__main__":
    main()
