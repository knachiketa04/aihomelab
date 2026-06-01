#!/usr/bin/env python3
"""
strip_lm_head_lora.py

Strip the lm_head LoRA from a HuggingFace-PEFT LoRA adapter so vLLM can serve it.

vLLM rejects LoRA adapters whose target_modules include lm_head (per-architecture
allowlist; Qwen3 in vLLM 0.15.1 supports only q/k/v/o/gate/up/down_proj). NeMo
AutoModel's `match_all_linear: true` LoRA-tunes every Linear incl. lm_head, leaving:
  - lm_head LoRA tensors in adapter_model.safetensors
  - "lm_head" in adapter_config.json target_modules (and maybe modules_to_save)

This produces a vLLM-servable copy in DST by:
  1. dropping every tensor key containing ".lm_head."
  2. removing lm_head from target_modules and modules_to_save
  3. copying all remaining files unchanged

Conservative (matches ANY key containing ".lm_head."), idempotent (a clean
adapter passes through unchanged), preserves tensor dtype + safetensors header.
Runs inside a container with torch + safetensors (e.g. vllm-loaders:028).

Context: AIHomeLab experiment 028. The 026 Arm-A adapter (Qwen3-8B, r=16,
match_all_linear) targets lm_head + the 7 projections across 36 layers, with no
modules_to_save and no vocab resize — so dropping the lm_head LoRA pair is safe
(body LoRA carries the adaptation; output head reverts to base).
"""

import json
import shutil
from pathlib import Path

from safetensors import safe_open
from safetensors.torch import save_file

# ============================================================================
# Container mountpoints (match the docker -v targets; no edit needed)
# ============================================================================
SRC = "/work/adapter_in"      # dir with adapter_model.safetensors + adapter_config.json
DST = "/work/adapter_out"     # dir to write the lm_head-stripped adapter into
# ============================================================================

# Any tensor key CONTAINING this dot-bounded substring is dropped. Covers every
# PEFT key form for lm_head (lora_A/lora_B, base_layer, lora_magnitude_vector,
# modules_to_save) without ever matching an unrelated "...head" module.
LM_HEAD_MARKER = ".lm_head."

SAFETENSORS_NAME = "adapter_model.safetensors"
CONFIG_NAME = "adapter_config.json"


def _strip_from_listish(value):
    """Remove 'lm_head' from a target_modules / modules_to_save value.
    Handles list, single string, and None. Returns (cleaned_value, removed_bool)."""
    if value is None:
        return None, False
    if isinstance(value, str):
        if value == "lm_head":
            return [], True
        return value, False
    cleaned = [m for m in value if m != "lm_head"]
    removed = len(cleaned) != len(list(value))
    return cleaned, removed


def main():
    src = Path(SRC)
    dst = Path(DST)
    src_st = src / SAFETENSORS_NAME
    src_cfg = src / CONFIG_NAME

    if not src_st.is_file():
        raise SystemExit(f"ERROR: {src_st} not found")
    if not src_cfg.is_file():
        raise SystemExit(f"ERROR: {src_cfg} not found")

    dst.mkdir(parents=True, exist_ok=True)

    # ---- 1. Load tensors + metadata, drop lm_head keys --------------------
    tensors = {}
    dropped_keys = []
    with safe_open(str(src_st), framework="pt") as f:
        metadata = f.metadata()  # original safetensors header metadata (or None)
        all_keys = list(f.keys())
        for key in all_keys:
            if LM_HEAD_MARKER in key:
                dropped_keys.append(key)
                continue
            tensors[key] = f.get_tensor(key)

    before_key_count = len(all_keys)
    after_key_count = len(tensors)

    if not tensors:
        raise SystemExit(
            "ERROR: every tensor was dropped — refusing to write an empty "
            "adapter. Check that SRC really is a LoRA adapter."
        )

    dst_st = dst / SAFETENSORS_NAME
    save_file(tensors, str(dst_st), metadata=metadata)

    # ---- 2. Fix adapter_config.json ---------------------------------------
    with open(src_cfg, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    before_targets = cfg.get("target_modules")
    new_targets, tgt_removed = _strip_from_listish(before_targets)
    if tgt_removed:
        cfg["target_modules"] = new_targets

    before_mts = cfg.get("modules_to_save")
    new_mts, mts_removed = _strip_from_listish(before_mts)
    if mts_removed:
        cfg["modules_to_save"] = new_mts if new_mts else None

    dst_cfg = dst / CONFIG_NAME
    with open(dst_cfg, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    # ---- 3. Copy every other file unchanged -------------------------------
    handled = {SAFETENSORS_NAME, CONFIG_NAME}
    copied = []
    for item in sorted(src.iterdir()):
        if item.is_dir():
            continue
        if item.name in handled:
            continue
        shutil.copy2(item, dst / item.name)
        copied.append(item.name)

    # ---- 4. Verification report -------------------------------------------
    print("=" * 70)
    print("strip_lm_head_lora — verification")
    print("=" * 70)
    print(f"SRC: {src}")
    print(f"DST: {dst}")
    print()
    print("target_modules BEFORE:", before_targets)
    print("target_modules AFTER: ", cfg.get("target_modules"))
    print()
    print("modules_to_save BEFORE:", before_mts)
    print("modules_to_save AFTER: ", cfg.get("modules_to_save"))
    print()
    print(f"tensor keys BEFORE: {before_key_count}")
    print(f"tensor keys AFTER:  {after_key_count}")
    print(f"dropped tensor keys ({len(dropped_keys)}):")
    if dropped_keys:
        for k in dropped_keys:
            print(f"  - {k}")
    else:
        print("  (none — adapter was already lm_head-free; pass-through)")
    print()
    print(f"copied unchanged ({len(copied)}): {copied}")

    # Defensive self-check: no surviving key may contain the marker.
    leaked = [k for k in tensors if LM_HEAD_MARKER in k]
    if leaked:
        raise SystemExit(f"ERROR: lm_head keys leaked into output: {leaked}")

    print()
    print("OK — wrote vLLM-servable adapter to", dst)


if __name__ == "__main__":
    main()
