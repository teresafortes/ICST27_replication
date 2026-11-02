#!/usr/bin/env python3
"""
apply_repairs.py  —  Apply LLM-proposed repairs and run Dafny re-verification (Tier 1).

For each response with a parseable repair:
  1. Identify the mutation site by diffing original vs mutated source
  2. Put the LLM's repair there: replace the changed line, or, when the mutation
     deleted a whole line, insert the repair at the deletion point
  3. Run Dafny on the patched file
  4. Record pass/fail

Sanity check: applying the exact original line restores the original program for
every benchmark item.

Reads:
  ii-corpus/benchmark.json                                (mutated source + metadata)
  iii-evaluation/scores/<condition>/diagnosis_scores.csv  (parsed repairs from 2-score_diagnosis.py)

Writes:
  iii-evaluation/scores/<condition>/repair_scores.csv     (one row per response: pass/fail + metadata)

Run:
  python3 3-apply_repairs.py [--workers N] [--timeout T] [--condition ss_main|ss_masked|...]
"""

from __future__ import annotations
import json, re, subprocess, tempfile, argparse, difflib
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import pandas as pd

import prompts

ROOT       = Path(__file__).parent.parent
CORPUS     = ROOT / "ii-corpus"
BENCHMARK  = CORPUS / "benchmark.json"
SCORES_DIR = Path(__file__).parent / "scores"
DAFNY      = Path.home() / ".dotnet" / "tools" / "dafny"

TASK_TO_KEYWORD = {"A": "requires", "B": "invariant", "C": "ensures"}


# ─── repair application ───────────────────────────────────────────────────────

def apply_repair(mutated_source: str, original_source: str, task: str, repair: str) -> str | None:
    """
    Replace the mutated spec line in mutated_source with repair.
    Returns patched source, or None if the mutation site cannot be found.
    """
    keyword     = TASK_TO_KEYWORD[task]
    orig_lines  = original_source.splitlines(keepends=True)
    mut_lines   = mutated_source.splitlines(keepends=True)

    # Use SequenceMatcher to find the changed region involving the keyword
    sm = difflib.SequenceMatcher(None, orig_lines, mut_lines, autojunk=False)
    mut_start = mut_end = None

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        affected = orig_lines[i1:i2] + mut_lines[j1:j2]
        if any(keyword in line for line in affected):
            # For a whole-line deletion (tag "delete", j1 == j2) the repair is
            # inserted at the deletion point; otherwise it replaces the changed lines.
            mut_start, mut_end = j1, j2
            ref_line = orig_lines[i1] if i1 < len(orig_lines) else ""
            break

    if mut_start is None:
        return None

    # Indentation of the original spec line at the mutation site
    indent_str = ref_line[: len(ref_line) - len(ref_line.lstrip())]

    # Normalize repair: ensure it starts with the keyword and preserves indent
    repair = repair.strip()
    # Strip any accidental semicolons (legacy Dafny style)
    repair = repair.rstrip(";").strip()
    if not any(repair.startswith(k) for k in ("requires", "invariant", "ensures", "//")):
        repair = f"{keyword} {repair}"
    repair_line = f"{indent_str}{repair}\n"

    # Replace the mutated lines with the single repair line
    new_lines = mut_lines[:mut_start] + [repair_line] + mut_lines[mut_end:]
    return "".join(new_lines)


# ─── Dafny verification ───────────────────────────────────────────────────────

def dafny_verifies(source: str, timeout: int) -> bool:
    """Write source to a temp file, run Dafny, return True iff exit code == 0."""
    with tempfile.NamedTemporaryFile(suffix=".dfy", mode="w", delete=False) as f:
        f.write(source)
        tmp = f.name
    try:
        result = subprocess.run(
            [str(DAFNY), "verify", "--allow-warnings",
             "--verification-time-limit", str(timeout), tmp],
            capture_output=True, text=True, timeout=timeout + 10,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    finally:
        Path(tmp).unlink(missing_ok=True)


# ─── per-row worker ───────────────────────────────────────────────────────────

def process_row(args):
    row, item_lookup, timeout = args
    item_id  = row["item_id"]
    task     = row["task"]
    repair   = row.get("pred_repair", "")

    result = {
        "model":        row["model"],
        "item_id":      item_id,
        "run":          row["run"],
        "tier":         row["tier"],
        "task":         task,
        "mutation_type":row["mutation_type"],
        "pred_repair":  repair,
        "repair_applied": False,
        "dafny_pass":   False,
        "error":        None,
    }

    if not repair or not isinstance(repair, str) or not row.get("parse_ok", False):
        result["error"] = "no_repair"
        return result

    # Multi-line repairs cannot be applied to a single mutation site.
    # Log and skip rather than silently truncating to the first line.
    if "\n" in str(repair).strip():
        result["error"] = "multiline_repair"
        return result

    item = item_lookup.get(item_id)
    if item is None:
        result["error"] = "item_not_found"
        return result

    # Load original (positive) source
    orig_path = CORPUS / item["tier"] / item["task"] / f"{item['base_program']}.dfy"
    if not orig_path.exists():
        result["error"] = f"original_not_found: {orig_path}"
        return result

    original_source = orig_path.read_text()
    mutated_source  = item["source"]

    patched = apply_repair(mutated_source, original_source, task, repair)
    if patched is None:
        result["error"] = "mutation_site_not_found"
        return result

    result["repair_applied"] = True
    result["dafny_pass"]     = dafny_verifies(patched, timeout)
    return result


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--condition", choices=prompts.SINGLE_SHOT, default="ss_main",
                        help="Which single-shot condition's scores to use (see prompts.py)")
    args = parser.parse_args()

    scores_dir = SCORES_DIR / args.condition
    scores_dir.mkdir(parents=True, exist_ok=True)

    diag_path = scores_dir / "diagnosis_scores.csv"
    if not diag_path.exists():
        raise FileNotFoundError(f"Run score_diagnosis.py --condition {args.condition} first.")

    df = pd.read_csv(diag_path)
    # All runs are applied: run 1 for every item, plus runs 2-3 of the ss_main
    # consistency sample (1-run_evaluation.py --consistency).
    SCORES_DIR_OUT = scores_dir

    benchmark = json.loads(BENCHMARK.read_text())
    item_lookup = {item["item_id"]: item for item in benchmark["items"]}

    jobs = [(row, item_lookup, args.timeout) for row in df.to_dict("records")]
    print(f"[apply_repairs] {len(jobs)} responses to process (workers={args.workers})\n")

    results = []
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_row, j): j for j in jobs}
        for fut in as_completed(futs):
            results.append(fut.result())
            done += 1
            if done % 100 == 0 or done == len(jobs):
                passing = sum(1 for r in results if r["dafny_pass"])
                print(f"  [{done}/{len(jobs)}]  Dafny pass so far: {passing}")

    out = pd.DataFrame(results)
    out.to_csv(SCORES_DIR_OUT / "repair_scores.csv", index=False)

    print(f"\n=== Tier 1 repair success (Dafny pass) by model ===")
    print(out.groupby("model")["dafny_pass"].mean().round(3).to_string())
    print(f"\nOutput → {SCORES_DIR_OUT / 'repair_scores.csv'}")


if __name__ == "__main__":
    main()
