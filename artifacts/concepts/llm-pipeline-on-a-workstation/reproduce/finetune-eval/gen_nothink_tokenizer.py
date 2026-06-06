# Generate a thinking-stripped Qwen3 tokenizer for NeMo Automodel 0.3.0 ChatDataset.
#
# Why: Qwen3's bundled chat template emits an empty `<think>\n</think>` on the FINAL assistant
# turn via the branch `{%- if loop.last or (not loop.last and reasoning_content) %}`, which never
# consults `enable_thinking`. SFT renders full conversations with add_generation_prompt=False, so
# enable_thinking=False does NOT remove it (that flag only affects the generation-prompt branch).
# NeMo Automodel 0.3.0 ChatDataset exposes no enable_thinking / chat_template_kwargs knob — only a
# `chat_template` param, applied raw (`tokenizer.chat_template = chat_template`, no file-path
# resolution until 0.4.0). So the robust 0.3.0 fix is to bake a fixed template into a local tokenizer
# copy and point the recipe's dataset tokenizer at it.
#
# The edit: change the assistant-branch condition so a <think> block is emitted ONLY when real
# reasoning_content exists; reasoning-less assistant turns render clean as `<|im_start|>assistant\n{content}`.
#
# Run inside the container (has transformers); writes to Lustre via the -v $EXP:/data mount:
#   docker run --rm -i -v $EXP:/data -v $HFCACHE:/root/.cache/huggingface \
#     -e HF_TOKEN="${HF_TOKEN:-}" -e HF_HUB_OFFLINE=1 \
#     --entrypoint python $IMG - < gen_nothink_tokenizer.py

from transformers import AutoTokenizer

tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-8B")
ct = tok.get_chat_template()
marker = "loop.last or (not loop.last and reasoning_content)"
assert ct.count(marker) == 1, f"expected exactly 1 occurrence of the marker, found {ct.count(marker)}"
tok.chat_template = ct.replace(marker, "reasoning_content")
tok.save_pretrained("/data/qwen3-tok-nothink")
print("OK: saved /data/qwen3-tok-nothink ; marker removed:", marker not in tok.chat_template)
