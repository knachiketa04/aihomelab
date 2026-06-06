#!/usr/bin/env python3
"""
to_chat_format.py — Phase 4 prep, pass 2: clean.jsonl → chat-format chat.jsonl.

Converts each cleaned teacher row into a student-training chat record. The
training signal is the GENERATED Q&A pair (parsed_qa.q / parsed_qa.a), NOT the
teacher's user_turn (which was the generation instruction "Generate one Q&A
pair…" — scaffolding, not what the student should learn).

Output row schema (chat.jsonl):
  {
    "messages": [
      {"role": "user",      "content": <parsed_qa.q>},
      {"role": "assistant", "content": <parsed_qa.a>}
    ],
    "dietary_preference": "vegan" | "vegetarian",
    "template":           <template name>,
    "cuisine":            <best-effort from placeholders, or null>,
    "grounding_row_id":   int | null,
    "leakage_suspect":    bool,
    "request_id":         str
  }

We carry leakage_suspect through so a later session can filter on it without
re-deriving. Pure stdlib.
"""

import argparse
import json
import sys
from pathlib import Path


def derive_cuisine(row: dict) -> str | None:
    """Best-effort cuisine label. Not a top-level 025 field — lives in placeholders
    for some templates, implicit for the indian_* family."""
    tmpl = row.get("template", "")
    ph = row.get("placeholders") or {}
    if tmpl.startswith("indian_"):
        # state field (regional dal) is most specific; else generic Indian
        return ph.get("state") or "Indian"
    if tmpl == "nutrition_target":
        return ph.get("cuisine")
    if tmpl == "cross_cuisine":
        return ph.get("cuisine_a")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, type=Path, help="clean.jsonl")
    ap.add_argument("--out", required=True, type=Path, help="chat.jsonl")
    args = ap.parse_args()

    n_in = n_out = n_skipped = 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(args.out.suffix + ".tmp")  # atomic write
    with args.inp.open("r", encoding="utf-8") as fin, tmp.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            n_in += 1
            row = json.loads(line)
            qa = row.get("parsed_qa") or {}
            q, a = qa.get("q"), qa.get("a")
            # Defensive: clean.jsonl should already guarantee string + non-blank,
            # but re-check here so a bad row can never become a degenerate
            # user->blank-assistant training example.
            if not (isinstance(q, str) and q.strip() and isinstance(a, str) and a.strip()):
                n_skipped += 1
                continue
            out = {
                "messages": [
                    {"role": "user", "content": q},
                    {"role": "assistant", "content": a},
                ],
                "dietary_preference": row.get("dietary_preference"),
                "template":           row.get("template"),
                "cuisine":            derive_cuisine(row),
                "grounding_row_id":   row.get("grounding_row_id"),
                "leakage_suspect":    row.get("leakage_suspect", False),
                "request_id":         row.get("request_id"),
            }
            fout.write(json.dumps(out, ensure_ascii=False) + "\n")
            n_out += 1
    tmp.replace(args.out)

    print("=== to_chat_format ===")
    print(f"  in  : {n_in}")
    print(f"  out : {n_out}")
    print(f"  skipped (missing q/a): {n_skipped}")
    print(f"  wrote {n_out} rows → {args.out}")


if __name__ == "__main__":
    main()
