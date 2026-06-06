#!/usr/bin/env python3
"""
generate.py — drive vLLM Qwen3-32B to materialize Q&A pairs from the teacher.

Reads a prompts JSONL (from build_prompts.py) and templates.json, fires
concurrent /v1/chat/completions requests against a single vLLM endpoint,
streams responses to an output JSONL one line at a time (flushed every
--flush-every responses).

Design notes:
- One fixed prefix (system + 3 few-shot turns) is shared across all requests
  so vLLM's prefix-cache reuses KV after the first prefill.
- Sampling params + max_tokens are pulled per-template from templates.json.
- chat_template_kwargs.enable_thinking=False kills Qwen3 thinking-mode bleed.
- response_format={"type": "json_object"} forces strict JSON via vLLM guided
  decoding. Parsing happens client-side; failures are logged but don't abort.
"""

import argparse
import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def load_jsonl(path: Path) -> list:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_messages(system_prompt: str, few_shot: list, user_turn: str) -> list:
    msgs = [{"role": "system", "content": system_prompt}]
    msgs.extend(few_shot)
    msgs.append({"role": "user", "content": user_turn})
    return msgs


def build_payload(
    model: str,
    messages: list,
    sampling: dict,
    response_format: dict,
) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": sampling["temperature"],
        "top_p": sampling["top_p"],
        "max_tokens": sampling["max_tokens"],
        "response_format": response_format,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    # vLLM accepts top_k at top-level; OpenAI client uses extra_body. Top-level works for raw httpx.
    if "top_k" in sampling:
        payload["top_k"] = sampling["top_k"]
    return payload


async def generate_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url: str,
    model: str,
    system_prompt: str,
    few_shot: list,
    response_format: dict,
    templates: dict,
    row: dict,
    request_timeout: float,
) -> dict:
    """Issue one /v1/chat/completions request and return a normalized result row."""
    tmpl_name = row["template"]
    tmpl = templates[tmpl_name]
    payload = build_payload(
        model=model,
        messages=build_messages(system_prompt, few_shot, row["user_turn"]),
        sampling=tmpl["sampling"],
        response_format=response_format,
    )
    result = {
        "request_id":         row["request_id"],
        "template":           tmpl_name,
        "dietary_preference": row["dietary_preference"],
        "placeholders":       row.get("placeholders", {}),
        "grounding_row_id":   row.get("grounding_row_id"),
        "request_sent_at":    iso_now(),
        "response_received_at": None,
        "latency_ms":         None,
        "tokens_prompt":      None,
        "tokens_completion":  None,
        "finish_reason":      None,
        "raw_response":       None,
        "parsed_qa":          None,
        "parse_error":        None,
        "http_error":         None,
    }
    t0 = time.monotonic()
    async with sem:
        try:
            resp = await client.post(url, json=payload, timeout=request_timeout)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            result["http_error"] = f"{type(e).__name__}: {e}"
            result["response_received_at"] = iso_now()
            result["latency_ms"] = int((time.monotonic() - t0) * 1000)
            return result
        except Exception as e:
            result["http_error"] = f"{type(e).__name__}: {e}"
            result["response_received_at"] = iso_now()
            result["latency_ms"] = int((time.monotonic() - t0) * 1000)
            return result

    result["response_received_at"] = iso_now()
    result["latency_ms"] = int((time.monotonic() - t0) * 1000)

    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
        result["raw_response"]      = content
        result["finish_reason"]     = choice.get("finish_reason")
        usage = data.get("usage", {}) or {}
        result["tokens_prompt"]     = usage.get("prompt_tokens")
        result["tokens_completion"] = usage.get("completion_tokens")
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "q" in parsed and "a" in parsed:
                result["parsed_qa"] = {"q": parsed["q"], "a": parsed["a"]}
            else:
                result["parse_error"] = "JSON parsed but missing 'q' and/or 'a' keys"
        except json.JSONDecodeError as e:
            result["parse_error"] = f"JSONDecodeError: {e}"
    except (KeyError, IndexError, TypeError) as e:
        result["http_error"] = f"Malformed response: {type(e).__name__}: {e}"

    return result


async def run(args: argparse.Namespace):
    templates_doc = json.loads(args.templates.read_text(encoding="utf-8"))
    system_prompt = templates_doc["system_prompt"]
    few_shot = templates_doc["few_shot"]
    response_format = templates_doc.get("response_format", {"type": "json_object"})
    templates = templates_doc["templates"]

    prompts = load_jsonl(args.prompts)
    print(f"[generate] loaded {len(prompts)} prompts from {args.prompts}", file=sys.stderr)
    print(f"[generate] target endpoint: {args.vllm_url}", file=sys.stderr)
    print(f"[generate] model: {args.model}", file=sys.stderr)
    print(f"[generate] concurrency: {args.concurrency}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)

    # Resume / skip-completed support
    completed_ids = set()
    if args.skip_completed and args.out.exists():
        with args.out.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rid = json.loads(line).get("request_id")
                    if rid:
                        completed_ids.add(rid)
                except json.JSONDecodeError:
                    pass
        print(f"[generate] --skip-completed: {len(completed_ids)} existing request_ids in {args.out}", file=sys.stderr)
    prompts = [p for p in prompts if p["request_id"] not in completed_ids]
    print(f"[generate] {len(prompts)} prompts remaining after skip-completed filter", file=sys.stderr)
    if args.max_requests is not None and len(prompts) > args.max_requests:
        prompts = prompts[: args.max_requests]
        print(f"[generate] capped to {args.max_requests} new requests this invocation", file=sys.stderr)

    sem = asyncio.Semaphore(args.concurrency)

    completed = 0
    parsed_ok = 0
    parse_failed = 0
    http_failed = 0
    started_at = time.monotonic()

    out_f = args.out.open("a" if args.skip_completed else "w", encoding="utf-8")

    # Allow Ctrl-C to flush and close cleanly
    stop_event = asyncio.Event()

    def _sigint_handler():
        if not stop_event.is_set():
            print("\n[generate] SIGINT received — finishing in-flight requests, no new launches", file=sys.stderr)
            stop_event.set()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _sigint_handler)
    except NotImplementedError:
        pass  # Windows; we don't run there

    limits = httpx.Limits(max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = []
        for row in prompts:
            if stop_event.is_set():
                break
            task = asyncio.create_task(generate_one(
                client=client,
                sem=sem,
                url=args.vllm_url,
                model=args.model,
                system_prompt=system_prompt,
                few_shot=few_shot,
                response_format=response_format,
                templates=templates,
                row=row,
                request_timeout=args.request_timeout,
            ))
            tasks.append(task)

        for fut in asyncio.as_completed(tasks):
            res = await fut
            out_f.write(json.dumps(res, ensure_ascii=False) + "\n")
            completed += 1
            if res.get("http_error"):
                http_failed += 1
            elif res.get("parse_error"):
                parse_failed += 1
            else:
                parsed_ok += 1
            if completed % args.flush_every == 0:
                out_f.flush()
            if completed % args.progress_every == 0 or completed == len(tasks):
                elapsed = time.monotonic() - started_at
                rate = completed / elapsed if elapsed > 0 else 0.0
                print(
                    f"[generate] {completed}/{len(tasks)} done  "
                    f"({rate:.2f}/s)  parsed_ok={parsed_ok}  parse_failed={parse_failed}  http_failed={http_failed}",
                    file=sys.stderr,
                )

    out_f.flush()
    out_f.close()

    elapsed = time.monotonic() - started_at
    print(
        f"[generate] DONE  total={completed}  parsed_ok={parsed_ok}  "
        f"parse_failed={parse_failed}  http_failed={http_failed}  elapsed={elapsed:.1f}s  "
        f"avg_rate={completed/elapsed:.2f}/s",
        file=sys.stderr,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts", required=True, type=Path,
                    help="Input JSONL of prompts produced by build_prompts.py")
    ap.add_argument("--templates", required=True, type=Path,
                    help="templates.json (system_prompt + few_shot + per-template sampling)")
    ap.add_argument("--vllm-url", required=True, type=str,
                    help="vLLM chat completions endpoint, e.g. http://localhost:8000/v1/chat/completions")
    ap.add_argument("--model", required=True, type=str,
                    help="Model name as registered with vLLM serve (typically the --model arg to vllm serve)")
    ap.add_argument("--concurrency", type=int, default=32,
                    help="Max in-flight requests")
    ap.add_argument("--out", required=True, type=Path,
                    help="Output JSONL path")
    ap.add_argument("--flush-every", type=int, default=50,
                    help="Flush output file every N completions")
    ap.add_argument("--progress-every", type=int, default=10,
                    help="Print progress every N completions")
    ap.add_argument("--request-timeout", type=float, default=600.0,
                    help="Per-request timeout in seconds")
    ap.add_argument("--skip-completed", action="store_true",
                    help="Read --out as existing JSONL; skip any request_id already present (resumable + appends)")
    ap.add_argument("--max-requests", type=int, default=None,
                    help="Stop after N NEW completions in this invocation (does not count skipped). For chunked runs.")
    args = ap.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
