#!/usr/bin/env python3
"""
score_diagnosis.py  —  Parse LLM responses and score diagnosis accuracy.

Reads:
  ii-corpus/benchmark.json               (ground truth: spec element, proof obligation)
  iii-evaluation/responses/{condition}/{model}/*.json (LLM responses)

Writes:
  iii-evaluation/scores/diagnosis_scores.csv   (one row per response)
  iii-evaluation/scores/parse_failures.json    (responses that failed JSON parsing)

Run:
  python3 score_diagnosis.py
"""

from __future__ import annotations
import json, re
from pathlib import Path
import pandas as pd

import prompts

ROOT       = Path(__file__).parent.parent
BENCHMARK  = ROOT / "ii-corpus" / "benchmark.json"
RESP_DIR   = Path(__file__).parent / "responses"          # responses/{condition}/{model}/
SCORES_DIR = Path(__file__).parent / "scores"
MODELS     = ["claude-opus-5-5", "deepseek-v4-pro-0813", "qwen3.8-27b"]

TASK_TO_SPEC = {"A": "requires", "B": "invariant", "C": "ensures"}


# ground truth is stored in benchmark.json as gt_proof_obligation
# (computed during corpus construction by collect_verifier_output.py).
# It can name several obligations ("initiation|preservation") when Dafny
# reports more than one; a prediction is correct if it names any of them.
# "unknown" means none of the three failed, and is excluded from scoring.

def obligation_set(label: str) -> set:
    return set() if label in ("", "unknown") else set(label.split("|"))


# ─── response parsing ─────────────────────────────────────────────────────────

def get_think_and_clean(rec: dict) -> tuple[str, str]:
    """
    Return (think_block, clean_response) for a response record.
    DeepSeek-R1 stores chain-of-thought in reasoning_content (separate API field).
    Other models may embed <think> tags in raw_response (Qwen3 variants).
    Falls back to empty string if neither is present.
    """
    raw = rec.get("raw_response") or ""
    # Prefer explicit reasoning field (DeepSeek-R1: reasoning_content, Qwen3: reasoning_content too)
    reasoning = rec.get("reasoning_content") or ""
    if reasoning:
        return reasoning, raw
    # Fall back to <think> tag parsing (Qwen3 when thinking mode is on)
    m = re.search(r"<think>(.*?)</think>", raw, re.DOTALL)
    think = m.group(1).strip() if m else ""
    clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    return think, clean


def extract_json(text: str) -> dict | None:
    """Extract first valid JSON object from free text, handling markdown fences."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    # Try the full text first — avoids false matches on Dafny set literals like s={}
    # that appear inside string values and would fool the regex patterns below.
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # Fall back to regex extraction for responses with surrounding prose
    for pattern in [r"\{[^{}]*\}", r"\{(?:[^{}]|\{[^{}]*\})*\}"]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


def normalize_spec_element(val: str) -> str:
    v = val.lower().strip()
    if any(k in v for k in ("require", "precondition", "pre-condition")):
        return "requires"
    if any(k in v for k in ("invariant", "loop inv")):
        return "invariant"
    if any(k in v for k in ("ensure", "postcondition", "post-condition")):
        return "ensures"
    return v


def normalize_obligation(val: str) -> str:
    v = val.lower().strip()
    if any(k in v for k in ("initiation", "entry", "establish", "initial")):
        return "initiation"
    if any(k in v for k in ("preservation", "maintained", "maintain", "body")):
        return "preservation"
    # "bridge" is the older name used in the prompt; the study now calls it "termination"
    if any(k in v for k in ("termination", "bridge", "exit", "implies post", "post-condition follows")):
        return "termination"
    return v


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--condition", choices=prompts.SINGLE_SHOT, default="ss_main",
                        help="Which single-shot condition to score (see prompts.py)")
    cli = parser.parse_args()

    resp_dir   = RESP_DIR / cli.condition
    scores_out = SCORES_DIR / cli.condition
    scores_out.mkdir(parents=True, exist_ok=True)

    benchmark = json.loads(BENCHMARK.read_text())
    ground_truth = {
        item["item_id"]: {
            "gt_spec_element":     TASK_TO_SPEC[item["task"]],
            "gt_proof_obligation": item.get("gt_proof_obligation", "unknown"),
            "tier":                item["tier"],
            "task":                item["task"],
            "mutation_type":       item["mutation_type"],
        }
        for item in benchmark["items"]
    }

    rows, failures = [], []

    for model in MODELS:
        model_dir = resp_dir / model
        if not model_dir.exists():
            print(f"  [skip] {model} — no responses yet")
            continue

        files = sorted(model_dir.glob("*.json"))
        print(f"  [{model}] {len(files)} response files")

        for resp_file in files:
            rec     = json.loads(resp_file.read_text())
            item_id = rec["item_id"]
            if item_id not in ground_truth:
                continue  # item was removed from the benchmark
            think, clean = get_think_and_clean(rec)
            parsed  = extract_json(clean)
            gt      = ground_truth.get(item_id, {})

            row = {
                "model":        model,
                "item_id":      item_id,
                "run":          rec["run_index"],
                "tier":         rec["tier"],
                "task":         rec["task"],
                "mutation_type":rec["mutation_type"],
                "parse_ok":     parsed is not None,
                "think_chars":  len(think),
            }

            if parsed is None:
                failures.append({
                    "model": model, "item_id": item_id,
                    "run": rec["run_index"], "raw": clean[:300],
                })
                row.update({
                    "pred_spec_element": None, "pred_proof_obligation": None,
                    "pred_repair": None, "pred_diagnosis": None,
                    "gt_spec_element": gt.get("gt_spec_element"),
                    "gt_proof_obligation": gt.get("gt_proof_obligation"),
                    "spec_correct": False, "obligation_correct": None,
                    "joint_correct": None if gt.get("gt_proof_obligation") == "unknown" else False,
                })
            else:
                pred_spec = normalize_spec_element(parsed.get("spec_element", ""))
                gt_spec   = gt.get("gt_spec_element", "")
                gt_obl    = gt.get("gt_proof_obligation", "unknown")
                spec_ok   = pred_spec == gt_spec
                # proof_obligation absent in baseline condition — score as None
                if "proof_obligation" in parsed:
                    pred_obl = normalize_obligation(parsed.get("proof_obligation", ""))
                    obl_ok   = (pred_obl in obligation_set(gt_obl)) if gt_obl != "unknown" else None
                else:
                    pred_obl = None
                    obl_ok   = None

                row.update({
                    "pred_spec_element":     pred_spec,
                    "pred_proof_obligation": pred_obl,
                    "pred_repair":           parsed.get("repair", "").strip(),
                    "pred_diagnosis":        parsed.get("diagnosis", "").strip(),
                    "gt_spec_element":       gt_spec,
                    "gt_proof_obligation":   gt_obl,
                    "spec_correct":          spec_ok,
                    "obligation_correct":    obl_ok,
                    "joint_correct":         (spec_ok and (obl_ok is True)) if gt_obl != "unknown" else None,
                })
            rows.append(row)

    df = pd.DataFrame(rows)
    df.to_csv(scores_out / "diagnosis_scores.csv", index=False)

    if failures:
        (scores_out / "parse_failures.json").write_text(json.dumps(failures, indent=2))

    print(f"\nScored {len(rows)} responses | parse failures: {len(failures)}\n")
    for col, label in [
        ("spec_correct",       "Spec element accuracy"),
        ("obligation_correct", "Proof obligation accuracy"),
        ("joint_correct",      "Joint accuracy (both correct)"),
    ]:
        sub = df[df[col].notna()].copy()
        sub[col] = sub[col].astype(float)
        print(f"=== {label} by model ===")
        print(sub.groupby("model")[col].mean().round(3).to_string())
        print()

    print(f"Output → {scores_out / 'diagnosis_scores.csv'}")


if __name__ == "__main__":
    main()
