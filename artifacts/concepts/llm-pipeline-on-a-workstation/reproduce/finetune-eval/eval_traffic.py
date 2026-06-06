#!/usr/bin/env python3
"""
028 Phase 2 — eval-traffic driver.

Fires the 579-example test split at the vLLM server behind a CONSTANT system
prompt, exercising + measuring the Stage-3 storage touch points in one pass:
  - TP3 prefix cache : constant system prompt -> hit rate = Δhits / Δqueries
  - TP2 KV cache     : peak kv_cache_usage_perc sampled during traffic
  - TP6 audit log    : full-fidelity (prompt+response) output JSONL bytes / wall-clock
and banks every generated output for the quality evals (Phase 2b).

stdlib-only (urllib) so it runs in vllm-loaders on node1 hitting localhost.
Container mountpoints: /data (dataset, ro), /out (run output, rw), /work (this script).
"""
import json, os, time, threading, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

SERVER      = "http://localhost:8000"
MODEL       = "vegan"            # the --lora-modules adapter name
TEST        = "/data/test.jsonl"
OUT         = "/out/phase2_outputs.jsonl"
CONCURRENCY = 64                 # matches --max-num-seqs (fills the batch -> meaningful KV peak)
MAX_TOKENS  = 512
TEMP        = 0.7
KV_CAPACITY_TOKENS = 19989 * 16  # num_gpu_blocks * block_size from cache_config_info

SYSTEM_PROMPT = (
    "You are a helpful vegan and vegetarian recipe assistant. Always honor the "
    "user's stated dietary preference: a 'vegan' request must contain no animal "
    "products whatsoever (no meat, fish, eggs, dairy, honey, gelatin), and a "
    "'vegetarian' request must contain no meat or fish. Give a clear, complete, "
    "step-by-step recipe using commonly available ingredients, with quantities "
    "and cooking techniques. Keep the response focused and practical."
)


def http(method, path, payload=None, timeout=600):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(SERVER + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def scrape():
    out = {"pq": 0.0, "ph": 0.0, "kv": 0.0, "run": 0.0, "wait": 0.0}
    try:
        text = http("GET", "/metrics")
    except Exception:
        return out
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        try:
            val = float(line.rsplit(" ", 1)[1])
        except (ValueError, IndexError):
            continue
        if "prefix_cache_queries_total" in line and "external" not in line:
            out["pq"] = val
        elif "prefix_cache_hits_total" in line and "external" not in line:
            out["ph"] = val
        elif "kv_cache_usage_perc" in line:
            out["kv"] = val
        elif "num_requests_running" in line:
            out["run"] = val
        elif "num_requests_waiting" in line:
            out["wait"] = val
    return out


def load_rows():
    rows = []
    with open(TEST) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            user = ref = None
            for m in obj.get("messages", []):
                if m.get("role") == "user" and user is None:
                    user = m.get("content")
                elif m.get("role") == "assistant" and ref is None:
                    ref = m.get("content")
            if user is None:
                continue
            rows.append({"request_id": obj.get("request_id"),
                         "dietary_preference": obj.get("dietary_preference"),
                         "cuisine": obj.get("cuisine"),
                         "user": user, "reference": ref})
    return rows


def one_request(row):
    payload = {"model": MODEL,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": row["user"]}],
               "max_tokens": MAX_TOKENS, "temperature": TEMP}
    t0 = time.time()
    try:
        resp = json.loads(http("POST", "/v1/chat/completions", payload))
        ch = resp["choices"][0]
        row["generated"] = ch["message"]["content"]
        row["finish_reason"] = ch.get("finish_reason")
        row["usage"] = resp.get("usage")
        row["error"] = None
    except Exception as e:
        row["generated"] = None
        row["error"] = str(e)
    row["latency_s"] = round(time.time() - t0, 2)
    return row


_peak = {"kv": 0.0, "run": 0.0, "wait": 0.0}
_stop = threading.Event()


def monitor():
    while not _stop.is_set():
        m = scrape()
        _peak["kv"] = max(_peak["kv"], m["kv"])
        _peak["run"] = max(_peak["run"], m["run"])
        _peak["wait"] = max(_peak["wait"], m["wait"])
        time.sleep(0.5)


def main():
    rows = load_rows()
    print(f"loaded {len(rows)} test prompts; concurrency={CONCURRENCY}, max_tokens={MAX_TOKENS}")
    before = scrape()
    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = [ex.submit(one_request, r) for r in rows]
        for i, fu in enumerate(as_completed(futs), 1):
            results.append(fu.result())
            if i % 50 == 0:
                print(f"  {i}/{len(rows)} done ({time.time()-t0:.0f}s)")
    dur = time.time() - t0
    _stop.set()
    mon.join(timeout=2)
    after = scrape()

    with open(OUT, "w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    out_bytes = os.path.getsize(OUT)

    ok = [r for r in results if r.get("error") is None]
    out_toks = sum((r.get("usage") or {}).get("completion_tokens", 0) for r in ok)
    prm_toks = sum((r.get("usage") or {}).get("prompt_tokens", 0) for r in ok)
    dq = after["pq"] - before["pq"]
    dh = after["ph"] - before["ph"]
    hit = (dh / dq * 100) if dq > 0 else 0.0

    print("=" * 64)
    print("028 Phase 2 — eval-traffic report")
    print("=" * 64)
    print(f"requests        : {len(results)} ({len(ok)} ok, {len(results)-len(ok)} err)")
    print(f"wall-clock      : {dur:.1f} s   throughput: {len(ok)/dur:.2f} req/s, {out_toks/dur:.0f} out-tok/s")
    print(f"tokens          : prompt={prm_toks}  output={out_toks}")
    print(f"TP3 prefix cache: Δqueries={dq:.0f}  Δhits={dh:.0f}  hit-rate={hit:.1f}%")
    print(f"TP2 KV cache    : peak usage={_peak['kv']*100:.1f}%  (cap ~{KV_CAPACITY_TOKENS:,} tok; "
          f"peak ~{_peak['kv']*KV_CAPACITY_TOKENS:,.0f} tok)")
    print(f"   batch        : peak running={_peak['run']:.0f}  peak waiting={_peak['wait']:.0f}")
    print(f"TP6 audit log   : full-fidelity (prompt+response) = {out_bytes/1e6:.2f} MB total, "
          f"{out_bytes/dur/1e3:.1f} KB/s sustained")
    print(f"outputs -> {OUT}")


if __name__ == "__main__":
    main()
