#!/usr/bin/env python3
"""
1b-run_iterative.py  —  The iterative condition (it_main): up to k attempts per item,
with Dafny feedback between attempts.

Turn 1 is the ss_main prompt. After each attempt:
  1. The response is parsed like a single-shot response (2-score_diagnosis.py).
  2. If it can't be applied (no JSON, no repair, multi-line repair), the next turn is
     ITERATIVE_UNAPPLIED with the reason.
  3. Otherwise the repair is applied to the ORIGINAL mutant (3-apply_repairs.py's
     apply_repair; attempts don't accumulate) and Dafny is run on the result.
  4. If Dafny passes (exit code 0), the conversation stops. If not, the next turn is
     ITERATIVE_FEEDBACK with the patched program, its error block and counterexample,
     built exactly as for the benchmark prompts (build_prompt_fields, with
     the temp path replaced by program.dfy).
A conversation ends when a repair verifies or after k attempts (k counts every model
turn, including unapplied ones).

Every model, Claude included, runs each conversation start to finish in a thread pool.
(Claude can instead go through the Batch API, one batch per round, if MODELS sets
"batch": True; dropped 2026-10-25 because batch requests missed the prompt cache.)

Output (one file per conversation, rewritten after every turn):
  responses/{out}/{model}/{tier}_{task}_{stem}_run{r}.json
    item_id, tier, task, mutation_type, base_program, model, condition, run_index,
    k, status (active | verified | exhausted | error), messages (the full
    conversation, assistant turns without reasoning), attempts: [
      {turn, raw_response, reasoning_content, finish_reason, usage, elapsed_s,
       timestamp, parse_ok, spec_element, proof_obligation, diagnosis, repair,
       unapplied_reason, repair_applied, patched_source, dafny_pass,
       dafny_exit_code, dafny_timeout, error_message, counterexample_nl}]
{out} is it_main for k=5 and it_main_k{k} otherwise (the k=10 plateau pilot).

--score (no API calls) writes scores/{out}/:
  attempts.csv       one row per attempt, with diagnosis correctness
  repair_scores.csv  the last attempt of each conversation, in 3-apply_repairs.py's
                     format plus `turns`, so 4-check_semantic_equivalence.py
                     --condition {out} classifies the final repairs.

Usage:
  python3 1b-run_iterative.py [--models ...] [--k 5] [--runs 1] [--limit N]
                              [--tier low] [--task A] [--workers 4] [--timeout 20]
  python3 1b-run_iterative.py --k 10 --limit 50          # plateau pilot
  python3 1b-run_iterative.py --score [--k 5]
Re-running the same command resumes: finished conversations are skipped, live ones
continue from their last turn, and an interrupted Claude batch is picked up again.
"""

from __future__ import annotations
import argparse, hashlib, importlib.util, json, logging, os, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
ROOT     = EVAL_DIR.parent
sys.path.insert(0, str(EVAL_DIR))
import prompts


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

runner    = _load("run_evaluation", EVAL_DIR / "1-run_evaluation.py")
scoring   = _load("score_diagnosis", EVAL_DIR / "2-score_diagnosis.py")
repairing = _load("apply_repairs", EVAL_DIR / "3-apply_repairs.py")
collector = _load("collect_verifier_output", ROOT / "i-corpus-construction" / "2-collect_verifier_output.py")

BENCHMARK = ROOT / "ii-corpus" / "benchmark.json"
CORPUS    = ROOT / "ii-corpus"
RESP_DIR  = EVAL_DIR / "responses"
SCORES    = EVAL_DIR / "scores"
LOG_DIR   = EVAL_DIR / "logs"
DAFNY     = Path.home() / ".dotnet" / "tools" / "dafny"
CLAUDE    = runner.CLAUDE
MAX_API_ERRORS = 3   # consecutive failed calls before a conversation is marked "error"


def out_name(k: int) -> str:
    return "it_main" if k == 5 else f"it_main_k{k}"


# ─── conversation state ───────────────────────────────────────────────────────

def conv_path(out: str, model: str, item: dict, run: int) -> Path:
    stem = Path(item["file"]).stem
    return RESP_DIR / out / model / f"{item['tier']}_{item['task']}_{stem}_run{run}.json"


def load_or_init(out: str, model: str, item: dict, run: int, k: int) -> dict:
    path = conv_path(out, model, item, run)
    if path.exists():
        state = json.loads(path.read_text())
        if state["k"] != k:
            raise ValueError(f"{path} was run with k={state['k']}, not {k}")
        return state
    return {
        "item_id": item["item_id"], "tier": item["tier"], "task": item["task"],
        "mutation_type": item["mutation_type"], "base_program": item["base_program"],
        "file": item["file"], "model": model, "condition": "it_main", "run_index": run,
        "k": k, "status": "active", "api_errors": 0,
        "messages": [{"role": "user", "content": prompts.build_single_shot(item, "ss_main")}],
        "attempts": [],
    }


def save(out: str, state: dict):
    path = conv_path(out, state["model"], state, state["run_index"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


# ─── one attempt ──────────────────────────────────────────────────────────────

def run_dafny(source: str, timeout: int) -> tuple[str, int | None]:
    """Verify source as program.dfy with counterexamples; returns (output, exit code or None on timeout)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "program.dfy"
        path.write_text(source)
        try:
            # Run from the temp dir so Dafny reports locations as program.dfy(L,C)
            r = subprocess.run(
                [str(DAFNY), "verify", "--allow-warnings", "--extract-counterexample",
                 "--verification-time-limit", str(timeout), "program.dfy"],
                capture_output=True, text=True, timeout=timeout + 10, cwd=d)
        except subprocess.TimeoutExpired:
            return "", None
        return collector.sanitize_paths(r.stdout + r.stderr, str(path)), r.returncode


def evaluate(state: dict, item: dict, original: str, response: tuple, timeout: int):
    """Record one model turn, verify its repair, and queue the next user turn if needed."""
    text, reasoning, finish, usage, elapsed = response
    _, clean = scoring.get_think_and_clean({"raw_response": text, "reasoning_content": reasoning})
    parsed = scoring.extract_json(clean)
    repair = parsed.get("repair") if isinstance(parsed, dict) else None

    att = {
        "turn": len(state["attempts"]) + 1, "raw_response": text,
        "reasoning_content": reasoning, "finish_reason": finish, "usage": usage,
        "elapsed_s": round(elapsed, 2), "timestamp": datetime.now(timezone.utc).isoformat(),
        "parse_ok": isinstance(parsed, dict),
        "spec_element": str(parsed.get("spec_element", "")) if isinstance(parsed, dict) else "",
        "proof_obligation": str(parsed.get("proof_obligation", "")) if isinstance(parsed, dict) else "",
        "diagnosis": str(parsed.get("diagnosis", "")) if isinstance(parsed, dict) else "",
        "repair": repair.strip() if isinstance(repair, str) else "",
        "unapplied_reason": None, "repair_applied": False, "patched_source": None,
        "dafny_pass": False, "dafny_exit_code": None, "dafny_timeout": False,
        "error_message": None, "counterexample_nl": None,
    }
    state["messages"].append({"role": "assistant", "content": clean})
    state["api_errors"] = 0

    if not att["parse_ok"]:
        att["unapplied_reason"] = "no_json"
    elif not att["repair"]:
        att["unapplied_reason"] = "no_repair"
    elif "\n" in att["repair"]:
        att["unapplied_reason"] = "multiline_repair"

    if att["unapplied_reason"] is None:
        patched = repairing.apply_repair(item["source"], original, item["task"], att["repair"])
        if patched is None:   # can't happen for current items (all gold repairs apply)
            state["attempts"].append(att)
            state["status"] = "error"
            state["error"] = "mutation_site_not_found"
            return
        att["repair_applied"], att["patched_source"] = True, patched
        output, code = run_dafny(patched, timeout)
        att["dafny_exit_code"] = code
        att["dafny_timeout"] = code is None
        att["dafny_pass"] = code == 0

    state["attempts"].append(att)
    if att["dafny_pass"]:
        state["status"] = "verified"
        return
    if len(state["attempts"]) >= state["k"]:
        state["status"] = "exhausted"
        return

    if att["unapplied_reason"]:
        followup = prompts.build_iterative_followup(unapplied_reason=att["unapplied_reason"])
    else:
        if att["dafny_timeout"]:
            err, cex = f"Dafny timed out after {timeout} seconds.", "No counterexample was produced."
        else:
            fields = collector.build_prompt_fields(att["patched_source"], output, "program.dfy")
            err, cex = fields["error_message"], fields["counterexample_nl"]
        att["error_message"], att["counterexample_nl"] = err, cex
        followup = prompts.build_iterative_followup(err, cex, source=att["patched_source"])
    state["messages"].append({"role": "user", "content": followup})


def api_failure(state: dict, err: str, logger):
    state["api_errors"] = state.get("api_errors", 0) + 1
    logger.error(f"err {state['item_id']} run{state['run_index']} turn{len(state['attempts']) + 1}  {err}")
    if state["api_errors"] >= MAX_API_ERRORS:
        state["status"] = "error"
        state["error"] = err


# ─── synchronous models ───────────────────────────────────────────────────────

def run_conversation(out, model, state, item, original, timeout, logger):
    client, cfg = runner.make_client(model)
    while state["status"] == "active":
        try:
            response = runner.call_model(client, cfg, state["messages"])
        except Exception as e:
            api_failure(state, str(e), logger)
            save(out, state)
            continue
        evaluate(state, item, original, response, timeout)
        save(out, state)
        logger.info(f"turn {state['item_id']} run{state['run_index']} "
                    f"#{len(state['attempts'])} pass={state['attempts'][-1]['dafny_pass']}")
    return state


def run_sync(out, model, states, items, originals, workers, timeout, logger):
    live = [s for s in states if s["status"] == "active"]
    print(f"[{model}] {len(live)} live conversations ({len(states) - len(live)} finished)")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run_conversation, out, model, s, items[s["item_id"]],
                          originals[s["item_id"]], timeout, logger) for s in live]
        for fut in as_completed(futs):
            s = fut.result()
            done += 1
            print(f"  [{done}/{len(live)}] {s['item_id']} run{s['run_index']}: "
                  f"{s['status']} after {len(s['attempts'])} attempt(s)")


# ─── Claude: one batch per round ──────────────────────────────────────────────

def custom_id(out: str, state: dict) -> str:
    turn = len(state["attempts"]) + 1
    return hashlib.md5(f"{out}:{state['item_id']}:{state['run_index']}:{turn}".encode()).hexdigest()


def run_claude_rounds(out, states, items, originals, workers, timeout, logger):
    cfg = runner.MODELS[CLAUDE]
    client = runner.anthropic.Anthropic(api_key=os.environ[cfg["api_key_env"]])
    state_file = LOG_DIR / f"claude_batch_state_{out}.json"
    by_id = {}

    while True:
        live = [s for s in states if s["status"] == "active"]
        by_id = {custom_id(out, s): s for s in live}
        if state_file.exists():
            batch_id = json.loads(state_file.read_text())["batch_id"]
            print(f"[claude] Resuming batch {batch_id}")
        elif live:
            requests = [{"custom_id": cid, "params": runner.claude_params(cfg, s["messages"])}
                        for cid, s in by_id.items()]
            runner.warm_claude_cache(client, cfg, out, logger)
            batch_id = client.messages.batches.create(requests=requests).id
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({"batch_id": batch_id}))
            turns = sorted({len(s["attempts"]) + 1 for s in live})
            print(f"[claude] Batch {batch_id}: {len(requests)} requests (turn {turns})")
        else:
            break

        while True:
            batch = client.messages.batches.retrieve(batch_id)
            c = batch.request_counts
            print(f"[claude] {batch.processing_status}  processing={c.processing}  "
                  f"succeeded={c.succeeded}  errored={c.errored}")
            if batch.processing_status != "in_progress":
                break
            time.sleep(60)

        jobs = []
        for result in client.messages.batches.results(batch_id):
            s = by_id.get(result.custom_id)
            if s is None:          # already recorded before an interruption
                continue
            if result.result.type != "succeeded":
                api_failure(s, result.result.type, logger)
                save(out, s)
                continue
            msg = result.result.message
            text, reasoning = runner.claude_content(msg)
            usage = runner.claude_usage(msg) | {"batch": True}
            jobs.append((s, (text, reasoning, msg.stop_reason, usage, 0.0)))

        def work(job):
            s, response = job
            evaluate(s, items[s["item_id"]], originals[s["item_id"]], response, timeout)
            save(out, s)
            return s
        with ThreadPoolExecutor(max_workers=workers) as ex:
            finished = list(ex.map(work, jobs))
        state_file.unlink()
        n_pass = sum(s["status"] == "verified" for s in finished)
        print(f"[claude] Round done: {len(finished)} attempts recorded, {n_pass} verified")


# ─── scoring ──────────────────────────────────────────────────────────────────

def score(out: str):
    import pandas as pd
    items = {i["item_id"]: i for i in json.loads(BENCHMARK.read_text())["items"]}
    rows, finals = [], []
    for path in sorted((RESP_DIR / out).glob("*/*.json")):
        s = json.loads(path.read_text())
        item = items.get(s["item_id"])
        if item is None:
            continue
        gt_spec = scoring.TASK_TO_SPEC[s["task"]]
        gt_obl = scoring.obligation_set(item.get("gt_proof_obligation", "unknown"))
        base = {"model": s["model"], "item_id": s["item_id"], "run": s["run_index"],
                "tier": s["tier"], "task": s["task"], "mutation_type": s["mutation_type"],
                "status": s["status"], "k": s["k"]}
        for a in s["attempts"]:
            pred_spec = scoring.normalize_spec_element(a["spec_element"]) if a["parse_ok"] else ""
            pred_obl = scoring.normalize_obligation(a["proof_obligation"]) if a["parse_ok"] else ""
            rows.append({**base, "turn": a["turn"], "parse_ok": a["parse_ok"],
                         "pred_spec_element": pred_spec, "spec_correct": pred_spec == gt_spec,
                         "gt_proof_obligation": item.get("gt_proof_obligation", "unknown"),
                         "pred_proof_obligation": pred_obl,
                         "obligation_correct": (pred_obl in gt_obl) if gt_obl else None,
                         "pred_diagnosis": a.get("diagnosis", ""),
                         "pred_repair": a["repair"], "unapplied_reason": a["unapplied_reason"],
                         "repair_applied": a["repair_applied"], "dafny_pass": a["dafny_pass"],
                         "dafny_exit_code": a["dafny_exit_code"], "dafny_timeout": a["dafny_timeout"]})
        if s["attempts"] and s["status"] != "active":
            last = s["attempts"][-1]
            finals.append({**{c: base[c] for c in ("model", "item_id", "run", "tier", "task", "mutation_type")},
                           "pred_repair": last["repair"], "repair_applied": last["repair_applied"],
                           "dafny_pass": last["dafny_pass"],
                           "error": last["unapplied_reason"] or s.get("error"),
                           "turns": len(s["attempts"]), "status": s["status"]})
    (SCORES / out).mkdir(parents=True, exist_ok=True)
    att, fin = pd.DataFrame(rows), pd.DataFrame(finals)
    att.to_csv(SCORES / out / "attempts.csv", index=False)
    fin.to_csv(SCORES / out / "repair_scores.csv", index=False)
    print(f"{len(att)} attempts, {len(fin)} finished conversations → {SCORES / out}/")
    if len(fin):
        print("\nVerified within k, by model:")
        print(fin.groupby("model")["dafny_pass"].mean().round(3).to_string())
        print("\nCumulative pass rate by attempt:")
        n = fin.groupby("model").size()
        for m in n.index:
            turns = fin[(fin["model"] == m) & fin["dafny_pass"]]["turns"]
            k = int(fin["turns"].max())
            print(f"  {m:18s} " + "  ".join(f"{(turns <= t).sum() / n[m]:.3f}" for t in range(1, k + 1)))


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=[CLAUDE, "deepseek-v4-pro-0813", "qwen3.8-27b"],
                    choices=list(runner.MODELS))
    ap.add_argument("--k", type=int, default=5, help="Maximum attempts per conversation")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel conversations (other models) / Dafny checks (Claude)")
    ap.add_argument("--timeout", type=int, default=20,
                    help="Dafny verification time limit, as in 3-apply_repairs.py")
    ap.add_argument("--subset", type=int, choices=runner.MODE_SUBSET_SIZES,
                    help="Run on the first N items of ii-corpus/mode_subset.json (N = 200 in the paper; required for main runs)")
    ap.add_argument("--limit", type=int, help="Stratified sample of N items (same seed as 1-run_evaluation.py); pilots")
    ap.add_argument("--tier")
    ap.add_argument("--task")
    ap.add_argument("--score", action="store_true", help="Write scores/{out}/ from saved conversations; no API calls")
    args = ap.parse_args()
    out = out_name(args.k)

    if args.score:
        score(out)
        return

    if not (args.subset or args.limit):
        sys.exit("Pass --subset N (main run; N = 200 in the paper) or --limit N (pilot).")
    items = json.loads(BENCHMARK.read_text())["items"]
    if args.subset:
        items = runner.mode_subset(items, args.subset)
    if args.tier:
        items = [i for i in items if i["tier"] == args.tier]
    if args.task:
        items = [i for i in items if i["task"] == args.task]
    if args.limit:
        items = runner.stratified_sample(items, args.limit)
    by_id = {i["item_id"]: i for i in items}
    originals = {i["item_id"]: (CORPUS / i["tier"] / i["task"] / f"{i['base_program']}.dfy").read_text()
                 for i in items}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for model in args.models:
        cfg = runner.MODELS[model]
        if not os.environ.get(cfg["api_key_env"]):
            print(f"[{model}] WARNING: {cfg['api_key_env']} not set — skipping model.")
            continue
        logging.basicConfig(filename=str(LOG_DIR / f"{model}_{out}.log"), level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s", force=True)
        logger = logging.getLogger(model)
        states = [load_or_init(out, model, i, r, args.k) for i in items for r in range(1, args.runs + 1)]
        print(f"\n[{model}] {len(items)} items × {args.runs} run(s), k={args.k} → responses/{out}/{model}/")
        if cfg.get("batch"):
            run_claude_rounds(out, states, by_id, originals, args.workers, args.timeout, logger)
        else:
            run_sync(out, model, states, by_id, originals, args.workers, args.timeout, logger)
        statuses = {st: sum(s["status"] == st for s in states) for st in ("verified", "exhausted", "error", "active")}
        print(f"[{model}] {statuses}")


if __name__ == "__main__":
    main()
