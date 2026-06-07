---
license: apache-2.0
base_model: Qwen/Qwen3-8B
library_name: peft
pipeline_tag: text-generation
tags:
  - lora
  - peft
  - qwen3
  - recipes
  - vegan
  - vegetarian
  - instruction-tuning
  - distillation
datasets:
  - knachiketa004/vegan-vegetarian-recipes-qa
language:
  - en
---

# Vegetarian and Vegan Recipe Q&A — Qwen3-8B LoRA adapter

A small LoRA adapter that fine-tunes `Qwen/Qwen3-8B` for vegan and vegetarian recipe Q&A. It was built as the fine-tune stage of an end-to-end LLM pipeline run on workstation hardware, where the real subject was the storage behavior at each stage, not the recipes.

> **Status:** Draft. Companion to the [Vegetarian & Vegan Recipe Q&A dataset](https://huggingface.co/datasets/knachiketa004/vegan-vegetarian-recipes-qa) and the methodology article *[What I learned building an LLM pipeline on a workstation](https://github.com/knachiketa04/aihomelab/tree/main/artifacts/concepts/llm-pipeline-on-a-workstation)*.

## The honest summary first

This is a demo artifact from a storage-infrastructure experiment, not a production recipe tool. A frontier model on a phone writes a better curry than this adapter does, and that was never the point.

What it actually learned: the training set's house style, plus a partial improvement on dietary-constraint adherence. It did not learn to out-cook anything.

So pick by need. If you want a good vegan recipe, use a bigger model. If you want a small, openly-licensed adapter to study or to build on, this is that.

## What this is

- A **LoRA adapter** (~87 MB) on top of `Qwen/Qwen3-8B`. You serve it as base-plus-adapter; it is not a standalone model.
- Trained by **teacher-student distillation**: a 32B teacher (`Qwen/Qwen3-32B`, run locally) wrote the instruction set, and this 8B student learned from it.
- Domain: recipe Q&A only. Tuned for recipe-shaped requests, mediocre at anything else.

## Intended use and out of scope

**Intended:** recipe-style instruction following (give me a recipe, suggest a substitution, adapt a dish to a dietary constraint), and as a small, reproducible adapter for people studying distillation or LoRA serving.

**Out of scope:** any use where a dietary mistake matters (allergy management, medical, commercial menus) without a second-pass safety filter. See Limitations. Not a general-purpose assistant.

## How to use it

The adapter is served on top of the base model with vLLM's LoRA support. The published adapter has had its `lm_head` LoRA pair stripped so vLLM accepts it (see Notes), so it loads as-is:

```bash
vllm serve Qwen/Qwen3-8B \
  --enable-lora \
  --lora-modules vegetarian=knachiketa004/vegetarian-recipe-qwen3-8b-lora \
  --max-lora-rank 16
```

Then send chat requests with `"model": "vegetarian"`. Two practical notes:

- **Disable thinking for clean recipe output.** Qwen3 defaults thinking on. Pass `chat_template_kwargs: {"enable_thinking": false}` in the request (or append `/no_think` to the user message). The adapter was trained with a custom thinking-stripped Qwen3 template, so disabling thinking at serve time matches training behavior.
- **Do not merge the adapter.** On the NeMo Automodel build used here, `merge_and_unload()` produced an untrained-scoring model on Qwen3 (NeMo Automodel issue #1226, since fixed by PR #1395). Serve adapter-on-base instead, which is the configuration above. For a merged single-model deploy, use a build that includes the #1395 fix and validate the merged outputs against adapter-on-base first.

## Training

| Setting | Value |
| --- | --- |
| Base model | `Qwen/Qwen3-8B` (bf16) |
| Method | LoRA, all linear layers (q/k/v/o/gate/up/down projections) |
| LoRA rank / alpha | 16 / 32 |
| Global batch size | 32 |
| Epochs | 2 (~650 steps) |
| Optimizer / LR | Adam, 1e-4 cosine to 1e-5 min |
| Sequence length | 2048, answer-only loss masking |
| Tokenizer | Qwen3 chat template, thinking stripped (custom template) |
| Framework | NeMo Automodel 26.02, FSDP2 |
| Hardware | 2x NVIDIA DGX Spark (Grace-class, UMA), single and two-node arms |
| Training data | train split (10,424 pairs) of the [companion dataset](https://huggingface.co/datasets/knachiketa004/vegan-vegetarian-recipes-qa) (11,582 pairs total) |

The fine-tune was one arm of a checkpoint-storage A/B; the storage findings live in the methodology article and its reproduce kit.

## Evaluation and limitations

This adapter has real, measured limits. Read them before using it.

- **Dietary correctness is improved but not solved.** On a deliberately hard prompt ("a vegan Punjabi curry with chickpeas and spinach, no coconut") across five sampled seeds at temperature 0.7, the adapter offered coconut oil despite "no coconut" in **2 of 5** samples, versus **4 of 5** for the un-fine-tuned base. Better, not fixed. Hand-verified on a small sample; illustrative, not a benchmark.
- **It invents inaccurate dish names.** On the same prompt it stamped a "Gobi" (cauliflower) label on a chickpea dish in **4 of 5** samples; the base never did. It picked up the training set's habit of naming each dish without the accuracy to make the name correct.
- **Single-domain and single-language.** Recipe Q&A in English only. A recipe-tuned model and a weak general one.
- **Residual dietary-vocabulary errors.** Like the dataset it learned from, it can still slip an animal product into a "vegan" answer by name. For any use where correctness matters, run a second-pass dietary filter (an LLM judge, not a keyword screen, since a keyword filter flags "replace the fish with tofu" as a violation).
- **Cuisine attribution is imprecise.** Regional labels are sometimes wrong even when the dish itself is described correctly.

These bounds inherit from the [dataset's documented limitations](https://huggingface.co/datasets/knachiketa004/vegan-vegetarian-recipes-qa); the dataset card is the fuller account.

## Notes

- **The `lm_head` strip.** The training recipe applied LoRA to every linear layer, which on Qwen3 includes `lm_head`. vLLM rejects an adapter whose `target_modules` include `lm_head`. The published adapter has that one LoRA pair removed from `adapter_model.safetensors` and `adapter_config.json`, which restores clean serving with no meaningful quality loss: the body LoRA across the q/k/v/o/gate/up/down projections carries the adaptation, the output head simply reverts to base, and there was no vocabulary resize.

## License and attribution

The adapter weights are released under **Apache-2.0**, as a derivative of the Apache-2.0 base model (`Qwen/Qwen3-8B`).

The adapter was **trained on the [Vegetarian & Vegan Recipe Q&A dataset](https://huggingface.co/datasets/knachiketa004/vegan-vegetarian-recipes-qa), which is licensed CC BY-SA 4.0** (it inherits ShareAlike from Wikibooks Cookbook and Wikipedia sources used as grounding and few-shot context). The Apache-2.0 license on these weights does not relicense that dataset: if you redistribute the dataset, its CC BY-SA 4.0 terms apply to it, and attribution to its sources is required. The data sources, via the dataset:

- Wikibooks Cookbook (CC BY-SA 3.0), Project Gutenberg (public domain), Wikipedia food articles (CC BY-SA 4.0). The Wikibooks Cookbook material is CC BY-SA 3.0; it is included in the CC BY-SA 4.0 dataset under the one-way compatibility that CC BY-SA 4.0 permits for 3.0 content, so the dataset's 4.0 label and the 3.0 source label are both correct.
- Teacher model `Qwen/Qwen3-32B` (Apache-2.0)

## Disclaimer

This is a personal open-source project, provided **as-is under the Apache-2.0 license, with no warranty of any kind, express or implied, and no guarantee of fitness for any purpose**. You use it at your own risk. As the Limitations section documents, this adapter makes dietary mistakes; do not rely on it for allergy, medical, nutritional, or any other safety-critical decision without independent verification. The author accepts no liability for any use of this model or its outputs. The warranty disclaimer and limitation of liability in the Apache-2.0 license govern; this paragraph restates them in plain language and does not change them.

## Citation

```bibtex
@misc{nachiketa2026vegetarianrecipeqwen3lora,
  author = {Nachiketa, Kumar},
  title  = {Vegetarian and Vegan Recipe Q&A: a Qwen3-8B LoRA adapter},
  year   = {2026},
  publisher = {Hugging Face},
  howpublished = {\url{https://huggingface.co/knachiketa004/vegetarian-recipe-qwen3-8b-lora}}
}
```

## Reproducibility and companion materials

- **Methodology article:** *What I learned building an LLM pipeline on a workstation*. Covers the full pipeline (data prep, distillation, fine-tune, eval, serve) and the storage findings at each stage.
- **Dataset:** [knachiketa004/vegan-vegetarian-recipes-qa](https://huggingface.co/datasets/knachiketa004/vegan-vegetarian-recipes-qa).
- **Reproduce kit:** the as-run training and serving scripts (LoRA recipe, serve command, the `lm_head` strip) in the [AIHomeLab repository](https://github.com/knachiketa04/aihomelab/tree/main/artifacts/concepts/llm-pipeline-on-a-workstation/reproduce).

## Acknowledgments

Built on NVIDIA DGX Spark hardware in personal capacity, not affiliated with any employer. On top of vLLM (Apache-2.0), Hugging Face Transformers and PEFT (Apache-2.0), NeMo Automodel, and the Qwen3 model family from Alibaba (Apache-2.0).

---

*This model card is a living document. Issues and suggestions welcome via the AIHomeLab repository.*
