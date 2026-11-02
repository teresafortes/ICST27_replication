#!/usr/bin/env python3
"""
run_evaluation.py

Sends every item in ii-corpus/benchmark.json to each configured LLM under one
single-shot condition and saves raw responses. Every condition gets one run per item
(run 1). For ss_main, --consistency adds runs 2 and 3 on a fixed stratified sample of
CONSISTENCY_N items (ii-corpus/consistency_sample.json), to measure run-to-run variation.
Prompts come from prompts.py; condition names are listed there (ss_main,
ss_masked, ss_nodafnyref, ss_intent).

Output layout:
  iii-evaluation/
  ├── responses/
  │   └── {condition}/{model_name}/
  │       └── {tier}_{task}_{stem}_run{k}.json   (one file per call)
  ├── logs/
  │   └── {model_name}_run.log                   (progress + errors)
  └── scores/                                     (filled by score_responses.py)

Each response file contains:
  item_id, tier, task, mutation_type, base_program,
  model, run_index, prompt, raw_response, finish_reason,
  usage (tokens), elapsed_s, timestamp

Models:
  claude-opus-5-5       — Claude Opus 5.5           (Anthropic, closed frontier; synchronous, prompt-cached)
  deepseek-v4-pro-0813  — DeepSeek-V4-Pro-0813      (OpenRouter, pinned to DeepSeek's own API)
  qwen3.8-27b           — Qwen3.8-27B               (OpenRouter, pinned to DeepInfra, bf16)
  Every model reasons at effort "high" (the default for Opus and DeepSeek; set explicitly for Qwen) with its recommended sampling
  settings, and every call has the same output cap (MAX_OUTPUT_TOKENS). The serving
  provider, cached and reasoning token counts are saved in each response's usage.

Usage:
  python3 1-run_evaluation.py [--condition ss_main] [--models claude-opus-5-5 deepseek-v4-pro-0813 qwen3.8-27b]
                            [--consistency]   # ss_main runs 2-3 on the 100-item sample
                            [--workers 4]
                            [--benchmark ii-corpus/benchmark.json]
                            [--resume]   # skip items already saved
                            [--tier low] [--task A]   # run a subset
                            [--batch-id msgbatch_xxx]  # resume a Claude batch
"""

import os, json, time, hashlib, argparse, logging
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import openai
import anthropic

import prompts

# ─── paths ────────────────────────────────────────────────────────────────────

ROOT                  = Path(__file__).parent.parent
BENCHMARK             = ROOT / "ii-corpus" / "benchmark.json"
EVAL_DIR              = Path(__file__).parent
RESP_DIR              = EVAL_DIR / "responses"          # responses/{condition}/{model}/
LOG_DIR               = EVAL_DIR / "logs"

# ─── model registry ───────────────────────────────────────────────────────────
# client: "anthropic" | "openai"  (openai client also used for OAI-compatible APIs)

MAX_OUTPUT_TOKENS = 64_000   # same cap for every model (reasoning + answer); hitting it = no answer
CLAUDE            = "claude-opus-5-5"

MODELS = {
    "claude-opus-5-5": {
        "client":      "anthropic",
        "model_id":    "claude-opus-5-5",
        "api_key_env": "API_KEY_ANTHROPIC_ICST",
        # Adaptive thinking at effort "high" (Anthropic's default effort; Opus 5.5 can't turn
        # thinking off). display="summarized" returns a readable thinking summary.
        # Opus rejects non-default temperature/top_p, so no sampling params are sent.
        "extra_kwargs": {"max_tokens":    MAX_OUTPUT_TOKENS,
                         "thinking":      {"type": "adaptive", "display": "summarized"},
                         "output_config": {"effort": "high"}},
        # Synchronous calls, not the Batch API (changed 2026-10-25 after the smoke test):
        # batch requests got no prompt-cache hits (19 of 20 rewrote the reference, even
        # after a warm-up), so a batch call cost ~$0.031 against ~$0.016 for a synchronous
        # call that reads the reference from cache. Same requests either way.
        "batch":       False,
    },
    "deepseek-v4-pro-0813": {
        "client":      "openai",                   # OpenRouter
        "model_id":    "deepseek/deepseek-v4-pro-0813",
        "base_url":    "https://openrouter.ai/api/v1",
        "api_key_env": "API_KEY_OPEN_ROUTER_ICST",
        # Thinking is on by default at effort "high"; sampling parameters are ignored in
        # thinking mode, so none are sent (DeepSeek API docs, "Thinking Mode").
        "extra_kwargs": {"max_tokens": MAX_OUTPUT_TOKENS},
        "extra_body":   {
            "include_reasoning": True,
            "provider": {"order": ["deepseek"], "allow_fallbacks": False},
        },
    },
    "qwen3.8-27b": {
        "client":      "openai",                   # OpenRouter
        "model_id":    "qwen/qwen3.8-27b",
        "base_url":    "https://openrouter.ai/api/v1",
        "api_key_env": "API_KEY_OPEN_ROUTER_ICST",
        # Thinking at effort "high", set explicitly to match the other two models (its own
        # default is "xhigh"; changed 2026-10-25, before any main run). Sampling settings are
        # the model card's recommendation for thinking mode (Qwen/Qwen3.8-27B on Hugging Face).
        "extra_kwargs": {"max_tokens": MAX_OUTPUT_TOKENS, "temperature": 1.0, "top_p": 0.95},
        "extra_body":   {
            "top_k": 20, "min_p": 0.0,
            "reasoning": {"effort": "high"},   # OpenRouter's reasoning object; traces are returned
            "include_reasoning": True,
            "provider": {"order": ["deepinfra/bf16"], "allow_fallbacks": False},
        },
    },
}

# ─── client factory ───────────────────────────────────────────────────────────

def make_client(model_name: str):
    cfg = MODELS[model_name]
    key = os.environ.get(cfg["api_key_env"], "")
    if not key:
        raise EnvironmentError(
            f"API key env var '{cfg['api_key_env']}' not set for model '{model_name}'"
        )
    if cfg["client"] == "anthropic":
        return anthropic.Anthropic(api_key=key), cfg
    else:
        kwargs = {"api_key": key}
        if cfg.get("base_url"):
            kwargs["base_url"] = cfg["base_url"]
        return openai.OpenAI(**kwargs), cfg


# ─── Claude request/response helpers (shared with 1b-run_iterative.py) ────────

def claude_params(cfg: dict, messages: list) -> dict:
    """Messages API parameters for a Claude call; the same dict is used in batch requests.
    A user turn that starts with the Dafny reference gets a cache breakpoint right after
    it, so every request reads the reference from cache (1-hour TTL, because batches can
    take longer than the default 5 minutes). The text sent is unchanged."""
    out = []
    for m in messages:
        parts = prompts.split_reference(m["content"]) if m["role"] == "user" and isinstance(m["content"], str) else None
        if parts:
            m = {"role": "user", "content": [
                {"type": "text", "text": parts[0], "cache_control": {"type": "ephemeral", "ttl": "1h"}},
                {"type": "text", "text": parts[1]}]}
        out.append(m)
    return {"model": cfg["model_id"], "messages": out, **cfg["extra_kwargs"]}


WARMUP_LOG = LOG_DIR / "claude_cache_warmups.jsonl"


def warm_claude_cache(client, cfg: dict, condition: str, logger=None) -> None:
    """Write the Dafny reference to the prompt cache before a batch is submitted.

    Batch requests run in parallel, so without this every request misses the cache and
    writes the reference again (seen in the smoke test: 20 writes, 0 reads). One
    synchronous request with the identical cached prefix (same model, thinking and effort
    settings, same reference block and breakpoint) writes it once; batch requests then
    read it while the 1-hour TTL lasts. The text after the breakpoint is a short
    placeholder and the answer is discarded. Usage goes to WARMUP_LOG, which a cost tracker
    adds to the spend. Hits are best-effort; cache_read_tokens in the responses shows
    whether they happened."""
    params = claude_params(cfg, [{"role": "user", "content": prompts.with_reference("Reply with OK.")}])
    params["max_tokens"] = 1024
    try:
        msg = client.messages.create(**params)
    except Exception as e:                       # a failed warm-up only costs cache hits
        print(f"[claude] cache warm-up failed ({e}); submitting the batch anyway")
        return
    usage = claude_usage(msg)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with WARMUP_LOG.open("a") as f:
        f.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "condition": condition,
                            "model": cfg["model_id"], "usage": usage}) + "\n")
    print(f"[claude] cache warm-up: wrote {usage['cache_write_tokens']}, "
          f"read {usage['cache_read_tokens']} reference tokens")
    if logger:
        logger.info(f"cache warm-up {condition}: {usage}")


def claude_usage(msg) -> dict:
    u = msg.usage
    return {
        "prompt_tokens":     u.input_tokens,
        "completion_tokens": u.output_tokens,
        "total_tokens":      u.input_tokens + u.output_tokens,
        "cache_read_tokens":  getattr(u, "cache_read_input_tokens", None) or 0,
        "cache_write_tokens": getattr(u, "cache_creation_input_tokens", None) or 0,
        "model_served":      msg.model,
    }


def openai_usage(resp) -> dict:
    """Token counts plus what OpenRouter reports about the serving provider."""
    u = resp.usage
    ptd = getattr(u, "prompt_tokens_details", None)
    ctd = getattr(u, "completion_tokens_details", None)
    return {
        "prompt_tokens":     u.prompt_tokens,
        "completion_tokens": u.completion_tokens,
        "total_tokens":      u.total_tokens,
        "cache_read_tokens": (getattr(ptd, "cached_tokens", None) or 0) if ptd else 0,
        "reasoning_tokens":  (getattr(ctd, "reasoning_tokens", None) or 0) if ctd else 0,
        "provider":          (resp.model_extra or {}).get("provider"),
        "model_served":      resp.model,
    }


def claude_content(msg) -> tuple:
    """(answer text, thinking summary) from a Claude message with or without thinking blocks."""
    text      = "".join(b.text for b in msg.content if b.type == "text")
    reasoning = "\n".join(b.thinking for b in msg.content if b.type == "thinking" and b.thinking)
    return text, reasoning


# ─── single API call ──────────────────────────────────────────────────────────

def call_model(client, cfg: dict, prompt, max_retries: int = 4):
    """
    Call the model with exponential back-off on rate-limit / server errors.
    prompt is a string (one user turn) or a list of {"role", "content"} messages
    (multi-turn, used by 1b-run_iterative.py).
    Returns (response_text, reasoning_content, finish_reason, usage_dict, elapsed_s).
    Handles both Anthropic and OpenAI-compatible clients.
    """
    messages = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
    delay = 5
    for attempt in range(max_retries):
        try:
            t0 = time.time()

            if isinstance(client, anthropic.Anthropic):
                # streamed: the SDK refuses non-streaming calls with a large max_tokens
                with client.messages.stream(**claude_params(cfg, messages)) as stream:
                    resp = stream.get_final_message()
                elapsed   = time.time() - t0
                text, reasoning = claude_content(resp)
                finish    = resp.stop_reason
                usage     = claude_usage(resp)

            else:
                call_kwargs = dict(cfg["extra_kwargs"])
                if cfg.get("extra_body"):
                    call_kwargs["extra_body"] = cfg["extra_body"]
                resp = client.chat.completions.create(
                    model=cfg["model_id"],
                    messages=messages,
                    **call_kwargs,
                )
                elapsed = time.time() - t0
                choice  = resp.choices[0]
                text    = choice.message.content or ""
                # DeepSeek-R1: chain-of-thought in reasoning_content
                # Qwen3 via OpenRouter: chain-of-thought in reasoning
                reasoning = (getattr(choice.message, "reasoning_content", None)
                             or getattr(choice.message, "reasoning", None)
                             or "")
                finish  = choice.finish_reason
                usage   = openai_usage(resp)

            return text, reasoning, finish, usage, elapsed

        except (openai.RateLimitError, anthropic.RateLimitError):
            if attempt == max_retries - 1:
                raise
            time.sleep(delay)
            delay *= 2

        except (openai.APIStatusError, anthropic.APIStatusError) as e:
            code = getattr(e, "status_code", 500)
            if code >= 500 and attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


# ─── response file helpers ────────────────────────────────────────────────────

def _base_resp_dir(condition: str) -> Path:
    return RESP_DIR / condition


def batch_state_file(condition: str, tag: str = "") -> Path:
    return LOG_DIR / f"claude_batch_state_{condition}{tag}.json"


def resp_path(model_name: str, item: dict, run_index: int,
              condition: str = "ss_main") -> Path:
    stem = Path(item["file"]).stem
    fname = f"{item['tier']}_{item['task']}_{stem}_run{run_index}.json"
    return _base_resp_dir(condition) / model_name / fname


def already_done(model_name: str, item: dict, run_index: int,
                 condition: str = "ss_main") -> bool:
    return resp_path(model_name, item, run_index, condition).exists()


def save_response(model_name: str, item: dict, run_index: int,
                  raw_response: str, reasoning_content: str, finish_reason: str,
                  usage: dict, elapsed_s: float, prompt: str,
                  condition: str = "ss_main"):
    path = resp_path(model_name, item, run_index, condition)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "item_id":            item["item_id"],
        "tier":               item["tier"],
        "task":               item["task"],
        "mutation_type":      item["mutation_type"],
        "base_program":       item["base_program"],
        "model":              model_name,
        "condition":          condition,
        "run_index":          run_index,
        "prompt":             prompt,
        "raw_response":       raw_response,
        "reasoning_content":  reasoning_content,
        "finish_reason":      finish_reason,
        "usage":              usage,
        "elapsed_s":          round(elapsed_s, 2),
        "timestamp":          datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(record, indent=2))


# ─── Claude Batch API ─────────────────────────────────────────────────────────

def run_claude_batch(jobs: list, resume: bool, logger,
                     existing_batch_id=None, condition: str = "ss_main", tag: str = ""):
    """
    Submit items to the Anthropic Message Batches API (50% cost discount).
    Polls until complete, then writes response files in the same format as
    the synchronous path. Saves state to disk so the run can be resumed with
    --batch-id if interrupted during polling.
    """
    cfg        = MODELS[CLAUDE]
    model_name = CLAUDE
    api_key    = os.environ.get(cfg["api_key_env"], "")
    client     = anthropic.Anthropic(api_key=api_key)
    state_file = batch_state_file(condition, tag)

    if existing_batch_id:
        if not state_file.exists():
            raise FileNotFoundError(
                f"Batch state file not found: {state_file}\n"
                "Cannot resume without it."
            )
        state      = json.loads(state_file.read_text())
        batch_id   = existing_batch_id
        id_to_item = state["id_to_item"]
        print(f"[claude] Resuming batch {batch_id}  ({len(id_to_item)} requests)")

    else:
        pending = [
            (item, run_idx)
            for item, run_idx in jobs
            if not (resume and already_done(model_name, item, run_idx, condition))
        ]

        if not pending:
            print("[claude] All items already done.")
            return 0, 0

        # Use MD5 hash as custom_id (stable, 32 chars, within API's 64-char limit)
        id_to_item = {}
        requests   = []
        for item, run_idx in pending:
            prompt    = prompts.build_single_shot(item, condition)
            custom_id = hashlib.md5(
                f"{condition}:{item['item_id']}:{run_idx}".encode()
            ).hexdigest()
            id_to_item[custom_id] = {
                "item_id":       item["item_id"],
                "tier":          item["tier"],
                "task":          item["task"],
                "mutation_type": item["mutation_type"],
                "base_program":  item["base_program"],
                "file":          item["file"],
                "run_idx":       run_idx,
                "prompt":        prompt,
            }
            requests.append({
                "custom_id": custom_id,
                "params": claude_params(cfg, [{"role": "user", "content": prompt}]),
            })

        warm_claude_cache(client, cfg, condition, logger)
        batch    = client.messages.batches.create(requests=requests)
        batch_id = batch.id

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(
            {"batch_id": batch_id, "id_to_item": id_to_item}, indent=2
        ))
        print(f"[claude] Batch submitted: {batch_id}  ({len(requests)} requests)")
        print(f"[claude] State saved → {state_file}")
        print(f"[claude] To resume if interrupted: --batch-id {batch_id}")

    # Poll until complete
    while True:
        batch  = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(f"[claude] {batch.processing_status}  "
              f"processing={counts.processing}  "
              f"succeeded={counts.succeeded}  "
              f"errored={counts.errored}")
        if batch.processing_status != "in_progress":
            break
        time.sleep(60)

    # Save results
    ok = errors = 0
    for result in client.messages.batches.results(batch_id):
        entry = id_to_item.get(result.custom_id)
        if entry is None:
            continue

        if result.result.type != "succeeded":
            errors += 1
            logger.error(f"err {entry['item_id']} run{entry['run_idx']}  {result.result.type}")
            print(f"  [ERROR] {entry['item_id']} run{entry['run_idx']}: {result.result.type}")
            continue

        msg    = result.result.message
        text, reasoning = claude_content(msg)
        finish = msg.stop_reason
        usage  = claude_usage(msg) | {"batch": True}
        item = {k: entry[k] for k in ("item_id", "tier", "task",
                                       "mutation_type", "base_program", "file")}
        save_response(model_name, item, entry["run_idx"],
                      text, reasoning, finish, usage, 0.0, entry["prompt"], condition)
        ok += 1
        logger.info(f"ok  {entry['item_id']} run{entry['run_idx']}  "
                    f"tokens={usage['total_tokens']}")

    print(f"[claude] DONE — ok={ok} errors={errors}")
    return ok, errors


# ─── per-job worker (non-batch models) ────────────────────────────────────────

def run_job(args):
    model_name, item, run_index, resume, condition = args
    if resume and already_done(model_name, item, run_index, condition):
        return ("skip", model_name, item["item_id"], run_index, None)

    prompt = prompts.build_single_shot(item, condition)

    try:
        client, cfg = make_client(model_name)
        raw, reasoning, finish, usage, elapsed = call_model(client, cfg, prompt)
        save_response(model_name, item, run_index, raw, reasoning, finish,
                      usage, elapsed, prompt, condition)
        return ("ok", model_name, item["item_id"], run_index, usage.get("total_tokens"))

    except Exception as e:
        return ("error", model_name, item["item_id"], run_index, str(e))


# ─── sampling ─────────────────────────────────────────────────────────────────

MODE_SUBSET_FILE = ROOT / "ii-corpus" / "mode_subset.json"
MODE_SUBSET_SIZES = (200, 150, 100)


def mode_subset(items: list, n: int) -> list:
    """The first n items of the fixed order in ii-corpus/mode_subset.json
    (i-corpus-construction/5-order_mode_subset.py). it_main and ag_main run on the same
    subset; n = 200 in the paper."""
    keep = json.loads(MODE_SUBSET_FILE.read_text())["order"][:n]
    by_id = {i["item_id"]: i for i in items}
    return [by_id[i] for i in keep if i in by_id]


def stratified_sample(items: list, limit: int) -> list:
    """
    At most `limit` items, spread evenly across tier×task cells. The seed is fixed,
    so the same sample comes back every time (safe for --resume, and the same items
    across scripts that use it).
    """
    import random, math
    rng = random.Random(42)
    cells: dict[tuple, list] = {}
    for it in items:
        cells.setdefault((it["tier"], it["task"]), []).append(it)
    n_cells  = len(cells)
    per_cell = math.ceil(limit / n_cells)
    sampled  = []
    for cell_items in cells.values():
        sampled.extend(rng.sample(cell_items, min(per_cell, len(cell_items))))
    rng.shuffle(sampled)
    sampled = sampled[:limit]
    print(f"[--limit {limit}] Stratified sample: {len(sampled)} items "
          f"across {n_cells} tier×task cells (≈{per_cell} per cell)")
    return sampled


CONSISTENCY_N      = 100
CONSISTENCY_RUNS   = (2, 3)
CONSISTENCY_FILE   = ROOT / "ii-corpus" / "consistency_sample.json"


def consistency_sample(items: list) -> list:
    """
    The fixed ss_main consistency sample: CONSISTENCY_N items stratified by
    tier × task × operator (seed 2027), saved to CONSISTENCY_FILE the first time and
    reused afterwards so every model gets the same items.
    """
    import random, math
    by_id = {it["item_id"]: it for it in items}
    if CONSISTENCY_FILE.exists():
        saved = json.loads(CONSISTENCY_FILE.read_text())
        missing = [i for i in saved["item_ids"] if i not in by_id]
        if missing:
            raise ValueError(f"{CONSISTENCY_FILE} lists {len(missing)} items not in the benchmark "
                             f"(e.g. {missing[0]}); delete it only if the benchmark was rebuilt "
                             "before any consistency run.")
        return [by_id[i] for i in saved["item_ids"]]
    rng = random.Random(2027)
    cells: dict[tuple, list] = {}
    for it in sorted(items, key=lambda x: x["item_id"]):
        cells.setdefault((it["tier"], it["task"], it["mutation_type"]), []).append(it)
    # Proportional allocation (at least 1 per cell), trimmed to CONSISTENCY_N
    quota = {c: max(1, round(CONSISTENCY_N * len(v) / len(items))) for c, v in cells.items()}
    while sum(quota.values()) > CONSISTENCY_N:
        quota[max(quota, key=quota.get)] -= 1
    while sum(quota.values()) < CONSISTENCY_N:
        quota[max(cells, key=lambda c: len(cells[c]) - quota[c])] += 1
    sample = [it for c in sorted(cells) for it in rng.sample(cells[c], min(quota[c], len(cells[c])))]
    CONSISTENCY_FILE.write_text(json.dumps({
        "description": "ss_main consistency sample: runs 2-3 are collected for these items only",
        "n": len(sample), "seed": 2027, "strata": "tier x task x mutation_type (proportional)",
        "item_ids": [it["item_id"] for it in sample]}, indent=1))
    print(f"[consistency] Wrote {len(sample)}-item sample → {CONSISTENCY_FILE}")
    return sample


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=list(MODELS.keys()),
                        choices=list(MODELS.keys()), metavar="MODEL",
                        help=f"Models to run. Choices: {list(MODELS.keys())} (default: all)")
    parser.add_argument("--consistency", action="store_true",
                        help="ss_main only: collect runs 2 and 3 for the fixed "
                             f"{CONSISTENCY_N}-item consistency sample instead of run 1")
    parser.add_argument("--workers",   type=int, default=4,
                        help="Parallel API threads per model (non-batch models only)")
    parser.add_argument("--benchmark", type=Path, default=BENCHMARK,
                        help="Path to benchmark.json")
    parser.add_argument("--resume",    action="store_true",
                        help="Skip items whose response files already exist")
    parser.add_argument("--tier",      help="Filter to a single tier (low/medium/high)")
    parser.add_argument("--task",      help="Filter to a single task (A/B/C)")
    parser.add_argument("--limit",     type=int, default=None,
                        help="Stratified random sample: take at most N items total, "
                             "distributed evenly across tier×task cells. Use a fixed seed "
                             "so --resume works correctly. Example: --limit 266 ≈ 20%%.")
    parser.add_argument("--batch-id",  dest="batch_id", default=None,
                        help="Resume an existing Claude batch by ID (e.g. msgbatch_xxx)")
    parser.add_argument("--condition", choices=prompts.SINGLE_SHOT, default="ss_main",
                        help="Single-shot condition (see prompts.py). Outputs to "
                             "responses/{condition}/. One run per item (run 1).")
    args = parser.parse_args()

    if args.consistency and args.condition != "ss_main":
        parser.error("--consistency applies to ss_main only")

    # Load benchmark
    if not args.benchmark.exists():
        raise FileNotFoundError(f"Benchmark not found: {args.benchmark}")
    benchmark = json.loads(args.benchmark.read_text())
    items = benchmark["items"]

    if args.tier:
        items = [i for i in items if i["tier"] == args.tier]
    if args.task:
        items = [i for i in items if i["task"] == args.task]

    if args.consistency:
        if args.tier or args.task or args.limit:
            parser.error("--consistency uses the fixed sample; don't combine it with --tier/--task/--limit")
        items = consistency_sample(items)
        run_ids = list(CONSISTENCY_RUNS)
    else:
        if args.limit:
            items = stratified_sample(items, args.limit)
        run_ids = [1]
    jobs_all = [(item, r) for item in items for r in run_ids]
    tag = "_consistency" if args.consistency else ""

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    for model_name in args.models:
        if model_name not in MODELS:
            print(f"Unknown model '{model_name}', skipping.")
            continue

        cfg = MODELS[model_name]

        # Check API key upfront
        api_key = os.environ.get(cfg["api_key_env"], "")
        if not api_key:
            print(f"[{model_name}] WARNING: {cfg['api_key_env']} not set — skipping model.")
            continue

        log_path = LOG_DIR / f"{model_name}_run.log"
        logging.basicConfig(
            filename=str(log_path),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            force=True,
        )
        logger = logging.getLogger(model_name)

        # ── Claude: Anthropic Message Batches API ──────────────────────────
        if cfg.get("batch"):
            print(f"\n[{model_name}] {len(items)} items × runs {run_ids} "
                  f"→ submitting via Anthropic Batch API (50% discount) "
                  f"[condition={args.condition}]")
            run_claude_batch(jobs_all, args.resume, logger,
                             args.batch_id, args.condition, tag)
            print(f"[{model_name}] Log → {log_path}")
            continue

        # ── All other models: synchronous thread pool ──────────────────────
        total_jobs = len(jobs_all)
        jobs = [(model_name, item, run_idx, args.resume, args.condition)
                for item, run_idx in jobs_all]

        print(f"\n[{model_name}] {len(items)} items × runs {run_ids} = {total_jobs} calls "
              f"[condition={args.condition}]")
        if args.resume:
            skip_count = sum(1 for _, item, ri, _, cond in jobs
                             if already_done(model_name, item, ri, cond))
            print(f"[{model_name}] {skip_count} already done, {total_jobs - skip_count} to send")
        print(f"[{model_name}] Sending first call (reasoning models can take 1–3 min each)...\n")

        ok = skip = errors = 0
        total_tokens = 0
        t0 = time.time()

        # Print frequency: every call when workers=1, else every 10
        print_every = 1 if args.workers == 1 else 10

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(run_job, j): j for j in jobs}
            for fut in as_completed(futs):
                status, mname, iid, ri, extra = fut.result()
                if status == "ok":
                    ok += 1
                    total_tokens += extra or 0
                    logger.info(f"ok  {iid} run{ri}  tokens={extra}")
                elif status == "skip":
                    skip += 1
                else:
                    errors += 1
                    logger.error(f"err {iid} run{ri}  {extra}")
                    print(f"  [ERROR] {iid} run{ri}: {extra}")

                done = ok + skip + errors
                if done % print_every == 0 or done == total_jobs:
                    elapsed = (time.time() - t0) / 60
                    rate = done / elapsed if elapsed > 0 else 0
                    remaining = ((total_jobs - done) / rate) if rate > 0 else 0
                    print(f"  [{done}/{total_jobs}] {elapsed:.1f}m elapsed  "
                          f"~{remaining:.0f}m left  "
                          f"ok={ok} skip={skip} err={errors}  "
                          f"tokens={total_tokens:,}")

        print(f"[{model_name}] DONE — ok={ok} skip={skip} errors={errors}  "
              f"total_tokens={total_tokens:,}")
        print(f"[{model_name}] Responses → {RESP_DIR / args.condition / model_name}/")
        print(f"[{model_name}] Log       → {log_path}")


if __name__ == "__main__":
    main()
