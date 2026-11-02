#!/usr/bin/env python3
"""
collect_verifier_output.py

For every .dfy file in ii-corpus/{tier}/{task}/negatives/:
  1. Run `dafny verify --extract-counterexample` and capture output
  2. Parse the error message and counterexample assume-lines
  3. Format counterexample as natural language
  4. Build a structured prompt for the LLM evaluation
  5. Save everything to ii-corpus/benchmark_full.json (the full pool; the 600-item
     evaluation set, benchmark.json, is drawn from it by 4-sample_evaluation_set.py)

An item is kept only if Dafny exits with a verification error (code 4) that is not
only a timeout. File paths are replaced with "program.dfy" in everything the model
sees, and each counterexample is labeled with the error it belongs to.

Usage:
  python3 collect_verifier_output.py [--workers N] [--timeout T]
  python3 collect_verifier_output.py --rebuild-prompts   # re-derive prompts from stored output
"""

import os, re, json, subprocess, argparse, time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

DAFNY  = os.path.expanduser("~/.dotnet/tools/dafny")
DAFNY_EXIT_VERIFICATION_ERROR = 4  # 2 = parse/resolution/type error; other codes = crash
ROOT   = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "ii-corpus"
OUT    = CORPUS / "benchmark_full.json"

TIERS = ["low", "medium", "high"]
TASKS = ["A", "B", "C"]

# The prompt keeps the older term "bridge" for the third obligation; the study
# (labels, scoring, paper) calls it "termination". The opening sentence is neutral: it
# says only that the program fails verification, not that it contains a mutation.
PROMPT_TEMPLATE = """\
You are a formal verification assistant. The following Dafny program fails Dafny verification.

=== PROGRAM ===
{source}

=== DAFNY ERROR ===
{error_message}

=== COUNTEREXAMPLE ===
{counterexample_nl}

Analyze the failure and respond with ONLY a valid JSON object — no explanation, no markdown, no code fences. Use exactly this structure:

{{
  "spec_element": "<one of: requires | invariant | ensures>",
  "proof_obligation": "<one of: initiation | preservation | bridge>",
  "diagnosis": "<explanation of what broke and why>",
  "repair": "<the corrected Dafny specification line that restores the intended behavior>"
}}

Definitions:
- requires: the precondition that must hold before the method executes
- invariant: the loop invariant that must hold throughout the loop
- ensures: the postcondition that the method must guarantee on return
- initiation: the precondition does not establish the invariant at loop entry
- preservation: the loop body does not maintain the invariant
- bridge: the invariant and loop exit condition do not imply the postcondition\
"""


# ─── output parsing ───────────────────────────────────────────────────────────

def run_dafny(path: Path, timeout: int):
    """Run dafny with --extract-counterexample; return (stdout+stderr, returncode)."""
    result = subprocess.run(
        [DAFNY, "verify", "--allow-warnings", "--extract-counterexample",
         "--verification-time-limit", str(timeout), str(path)],
        capture_output=True, text=True, timeout=10 * timeout + 60  # the limit is per assertion batch
    )
    return (result.stdout + result.stderr), result.returncode


def parse_error_message(output: str) -> str:
    """
    Extract only the semantically meaningful error lines from Dafny output:
      - Lines containing ': Error:' or ': Related location:' or 'Related message:'
      - Their immediately following code-snippet lines (| ...)
      - The final 'Dafny program verifier finished...' summary line
    Drops: counterexample assume-block, deprecated-style warnings, blank lines.
    """
    lines = output.splitlines()
    result = []
    in_cex_block = False
    include_next_snippet = False  # carry flag: include | lines after a kept annotation

    for line in lines:
        stripped = line.strip()

        # Enter/exit counterexample assume block
        if "Related counterexample:" in line:
            in_cex_block = True
            include_next_snippet = False
            continue
        if in_cex_block:
            if not stripped:          # blank line = end of assume block
                in_cex_block = False
            continue

        # Summary line — always keep
        if stripped.startswith("Dafny program verifier finished"):
            result.append(line.rstrip())
            include_next_snippet = False
            continue

        # Code-snippet lines: "  |", "3 |   code", "  |   ^^^" — keep if following a kept annotation
        if re.match(r"^\s*(\d+\s*)?\|", line):
            if include_next_snippet:
                result.append(line.rstrip())
            continue

        # Blank line — keep only if we have content and it separates sections
        if not stripped:
            if result and result[-1].strip():
                result.append("")
            include_next_snippet = False
            continue

        # Keep Error lines, Related location/message lines
        is_error    = ": Error:"            in line
        is_related  = ": Related location:" in line or "Related message:"   in line
        if is_error or is_related:
            result.append(line.rstrip())
            include_next_snippet = True
            continue

        # Drop everything else (deprecated warnings, WARNING: counterexample, etc.)
        include_next_snippet = False

    # Remove leading/trailing blanks
    while result and not result[0].strip():
        result.pop(0)
    while result and not result[-1].strip():
        result.pop()
    return "\n".join(result)


def parse_counterexample_blocks(output: str) -> list[dict]:
    """
    Split Dafny's output into one counterexample block per verification error.

    Dafny prints each error line followed by "Related counterexample:" and a list
    of program states, each a location header ("file.dfy(L,C): <label>:") and
    "assume ...;" lines. Returns a list of
      {"error_line": L, "error": <message>, "states": [{"label", "raw_assumes"}]}
    in output order, so each counterexample stays tied to the error it explains.
    """
    error_re  = re.compile(r"\.dfy\((\d+),\d+\):\s*Error:\s*(.*)")
    loc_re    = re.compile(r"\.dfy\(\d+,\d+\):\s*(.*?):?\s*$")
    assume_re = re.compile(r"assume\s+(.+);")

    blocks, last_error, current_block, current_state = [], None, None, None
    for line in output.splitlines():
        stripped = line.strip()
        m = error_re.search(line)
        if m:
            last_error = {"error_line": int(m.group(1)), "error": m.group(2).strip()}
            current_block = current_state = None
            continue
        if "Related counterexample:" in line:
            current_block = dict(last_error or {"error_line": None, "error": ""}, states=[])
            blocks.append(current_block)
            current_state = None
            continue
        if current_block is None:
            continue
        if stripped.startswith("WARNING:") or stripped.startswith("ghost var") \
                or "counterexampleLoopGuard" in stripped or stripped.startswith("Temporary variables"):
            continue
        if ".dfy(" in stripped and not stripped.startswith("assume"):
            loc = loc_re.search(stripped)
            current_state = {"label": loc.group(1).strip() if loc else "", "raw_assumes": []}
            current_block["states"].append(current_state)
            continue
        if current_state is not None and "assume " in stripped:
            am = assume_re.search(stripped)
            if am:
                current_state["raw_assumes"].append(am.group(1))
    return blocks


_VALUE = r"(-?\d+|true|false|null)"
_NAME  = r"(\|\w[\w.]*\||\w[\w.]*(?:\[\w+\])?)"   # x, a.Length, a[i], |s|

def extract_bindings(assume_expr: str) -> dict[str, str]:
    """
    Extract name = value facts from an assume expression, e.g.
    "3675 == |str1| && 3674 == i" -> {"|str1|": "3675", "i": "3674"}.
    Sequence lengths (|s|) are kept; they often explain bound failures.
    """
    bindings = {}
    for m in re.finditer(_VALUE + r"\s*==\s*" + _NAME, assume_expr):
        bindings[m.group(2)] = m.group(1)
    for m in re.finditer(_NAME + r"\s*==\s*" + _VALUE, assume_expr):
        bindings[m.group(1)] = m.group(2)
    return bindings


def states_to_nl(states: list[dict]) -> str:
    """Describe one counterexample (a list of states) in natural language."""
    parts, seen = [], {}
    for state in states:
        if not state["raw_assumes"]:
            continue
        new = {}
        for expr in state["raw_assumes"]:
            new.update(extract_bindings(expr))
        if not new:
            continue
        changed = {k: v for k, v in new.items() if seen.get(k) != v} or new
        seen.update(new)
        values = ", ".join(f"{k} = {v}" for k, v in sorted(changed.items()))
        parts.append(f"At {state['label'] or 'a program state'}: {values}.")
    if parts:
        return " ".join(parts)
    raw = next((s["raw_assumes"] for s in states if s["raw_assumes"]), [])
    return f"State: {'; '.join(raw[:3])}." if raw else ""


def counterexamples_to_nl(blocks: list[dict]) -> str:
    """
    Describe all counterexamples, one per error, each labeled with the line of the
    error it belongs to. A single counterexample is labeled too when Dafny reported
    several errors, so it is clear which error it explains. Returns a fixed message
    when Dafny produced none.
    """
    described = [(b, states_to_nl(b["states"])) for b in blocks]
    described = [(b, d) for b, d in described if d]
    if not described:
        return "No counterexample was produced."
    if len(described) == 1 and len(blocks) == 1:
        return described[0][1]
    return "\n".join(f"For the error at line {b['error_line']}: {d}" for b, d in described)


def sanitize_paths(text: str, file_path: str) -> str:
    """
    Replace the mutant's file path with a neutral name. The path names the task
    directory (A/B/C), the mutation operator (_cd/_bs/_ca), the negatives/ folder
    and the original DafnyBench file, all of which would leak answers.
    """
    for p in sorted({file_path, file_path.replace("ii-corpus/", "corpus/", 1),
                     os.path.basename(file_path)}, key=len, reverse=True):
        text = text.replace(p, "program.dfy")
    # Dafny prints the path as given or relative to the working directory, so also
    # replace any remaining path that ends in the file name.
    return re.sub(r"[^\s\"'()]*/program\.dfy", "program.dfy", text)


def build_prompt_fields(source: str, output: str, file_path: str) -> dict:
    """Derive everything the model sees from the mutant source and Dafny's output."""
    error_msg = sanitize_paths(parse_error_message(output), file_path)
    blocks    = parse_counterexample_blocks(output)
    cex_nl    = counterexamples_to_nl(blocks)
    return {
        "error_message":         error_msg,
        "counterexample_nl":     cex_nl,
        "counterexample_states": blocks,
        "has_counterexample":    cex_nl != "No counterexample was produced.",
        "prompt": PROMPT_TEMPLATE.format(source=source.strip(),
                                         error_message=error_msg,
                                         counterexample_nl=cex_nl),
    }


# ─── ground truth proof obligation ───────────────────────────────────────────

# Error-line wording for each obligation; older Dafny says "might not hold",
# Dafny 4.x says "could not be proved".
OBLIGATION_PATTERNS = {
    "initiation":   r"loop invariant (?:could not be proved|might not hold) on entry",
    "preservation": r"invariant (?:could not be proved to be maintained|might not be maintained) by the loop",
    "termination":  r"postcondition (?:could not be proved|might not hold) on this return path",
}

def infer_proof_obligations(raw_output: str) -> list:
    """
    Derive the ground-truth proof obligations from Dafny's error lines.

    Returns every obligation Dafny reports (sorted), so an item can have more
    than one (e.g. an invariant that fails both on entry and when maintained).
    Returns [] when the failure is none of the three (index out of range, call
    or function precondition, termination, ...); such items are excluded from
    obligation scoring.
    """
    found = set()
    for line in raw_output.splitlines():
        if ": Error:" not in line:
            continue
        for obligation, pattern in OBLIGATION_PATTERNS.items():
            if re.search(pattern, line, re.IGNORECASE):
                found.add(obligation)
    return sorted(found)

def obligation_label(obligations: list) -> str:
    """String form of the label: 'termination', 'initiation|preservation', or 'unknown'."""
    return "|".join(obligations) if obligations else "unknown"


# ─── per-file worker ──────────────────────────────────────────────────────────

def process_file(args):
    path_str, tier, task, timeout = args
    path = Path(path_str)
    stem = path.stem  # e.g. Foo_cd  or  Bar_bs

    # Infer mutation type from suffix
    if stem.endswith("_cd"):
        mut_type = "cd"
        base_name = stem[:-3]
    elif stem.endswith("_bs"):
        mut_type = "bs"
        base_name = stem[:-3]
    elif stem.endswith("_ca"):
        mut_type = "ca"
        base_name = stem[:-3]
    else:
        mut_type = "unknown"
        base_name = stem

    item_id = f"{tier}/{task}/{stem}"

    try:
        source = path.read_text()
        output, returncode = run_dafny(path, timeout)
    except subprocess.TimeoutExpired:
        return {"item_id": item_id, "error": "timeout"}
    except Exception as e:
        return {"item_id": item_id, "error": str(e)}

    # Only genuine verification failures become benchmark items.
    if returncode != DAFNY_EXIT_VERIFICATION_ERROR:
        return {"item_id": item_id,
                "error": f"not a verification failure (dafny exit code {returncode})"}
    error_lines = [l for l in output.splitlines() if ": Error:" in l]
    if error_lines and all("timed out" in l for l in error_lines):
        return {"item_id": item_id,
                "error": "verification only timed out (no confirmed failure)"}

    gt_obligations = infer_proof_obligations(output)
    fields = build_prompt_fields(source, output, str(path))

    return {
        "item_id":              item_id,
        "tier":                 tier,
        "task":                 task,
        "mutation_type":        mut_type,
        "base_program":         base_name,
        "file":                 os.path.relpath(path, ROOT),   # e.g. ii-corpus/low/A/negatives/X_cd.dfy
        "source":               source,
        "dafny_output":         output,
        **{k: fields[k] for k in ("error_message", "counterexample_nl",
                                  "counterexample_states", "has_counterexample")},
        "gt_proof_obligation":  obligation_label(gt_obligations),
        "gt_proof_obligations": gt_obligations,
        "prompt":               fields["prompt"],
        "dafny_exit_code":      returncode,
    }


# ─── generator log ────────────────────────────────────────────────────────────

GENERATOR_LOG = CORPUS / "mutation_log.json"

def attach_generator_log(results: list) -> list:
    """
    Copy each mutant's description and confirmed direction from the generator's log
    onto its item, and return "failed" entries for program × task × operator cells
    that produced no mutant (excluded program, original doesn't verify, or no
    candidate passed the generator's checks).
    """
    if not GENERATOR_LOG.exists():
        return []
    by_id, failed = {r["item_id"]: r for r in results}, []
    for entry in json.loads(GENERATOR_LOG.read_text()):
        if "tier" not in entry:
            failed.append({"item_id": entry.get("file", "?"), "error": f"generator error: {entry.get('error')}"})
            continue
        program = entry.get("program") or Path(entry["file"]).stem
        ops = ["cd", "bs"] if entry["task"] in ("A", "B") else ["ca", "bs"]
        for op in ops:
            item_id = f"{entry['tier']}/{entry['task']}/{program}_{op}"
            if item_id in by_id:
                by_id[item_id]["mutation_desc"]      = entry.get(f"{op}_desc")
                by_id[item_id]["mutation_direction"] = entry.get(f"{op}_direction")
            elif entry.get("skipped"):
                failed.append({"item_id": item_id, "error": entry["skipped"]})
            elif op in entry.get("no_mutant", {}):
                failed.append({"item_id": item_id, "error": entry["no_mutant"][op]})
    return failed


# ─── main ─────────────────────────────────────────────────────────────────────

def dafny_version() -> str:
    try:
        return subprocess.run([DAFNY, "--version"], capture_output=True, text=True).stdout.strip()
    except Exception:
        return "unknown"


def z3_version() -> str:
    try:
        out = subprocess.run(["z3", "--version"], capture_output=True, text=True).stdout.strip()
        return out.replace("Z3 version ", "") + " (from PATH)"
    except Exception:
        return "unknown"


def rebuild_prompts():
    """
    Regenerate the model-facing fields (error message, counterexample text, prompt)
    and the obligation labels for every item in benchmark_full.json from the stored Dafny
    output, without re-running Dafny. Use after changing how prompts are built, then run
    4-sample_evaluation_set.py --refresh to copy the rebuilt items into benchmark.json.
    """
    benchmark = json.loads(OUT.read_text())
    for item in benchmark["items"]:
        item.update(build_prompt_fields(item["source"], item["dafny_output"],
                                        item["file"].replace("corpus/", "ii-corpus/", 1)
                                        if item["file"].startswith("corpus/") else item["file"]))
        obligations = infer_proof_obligations(item["dafny_output"])
        item["gt_proof_obligation"]  = obligation_label(obligations)
        item["gt_proof_obligations"] = obligations
    benchmark["meta"]["prompts_rebuilt_with"] = dafny_version()
    OUT.write_text(json.dumps(benchmark, indent=2))
    print(f"Rebuilt prompts for {len(benchmark['items'])} items -> {OUT}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--rebuild-prompts", action="store_true",
                        help="Regenerate prompts and labels from the stored Dafny output only")
    args = parser.parse_args()

    if args.rebuild_prompts:
        rebuild_prompts()
        return

    jobs = []
    for tier in TIERS:
        for task in TASKS:
            neg_dir = CORPUS / tier / task / "negatives"
            if not neg_dir.exists():
                continue
            for dfy in sorted(neg_dir.glob("*.dfy")):
                jobs.append((str(dfy), tier, task, args.timeout))

    total = len(jobs)
    print(f"[collect_verifier_output] {total} files to process")
    print(f"[collect_verifier_output] workers={args.workers}, timeout={args.timeout}s\n")

    results = []
    errors  = []
    done    = 0
    t0      = time.time()

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_file, j): j for j in jobs}
        for fut in as_completed(futures):
            done += 1
            rec = fut.result()
            if "error" in rec and len(rec) == 2:
                errors.append(rec)
            else:
                results.append(rec)
            if done % 100 == 0 or done == total:
                elapsed = (time.time() - t0) / 60
                print(f"  [{done}/{total}] elapsed {elapsed:.1f}m  ok={len(results)}  err={len(errors)}")

    # Sort for determinism
    results.sort(key=lambda r: r["item_id"])
    errors.extend(attach_generator_log(results))
    errors.sort(key=lambda r: r["item_id"])

    benchmark = {
        "meta": {
            "total":   total,
            "ok":      len(results),
            "errors":  len(errors),
            "generated": time.strftime("%Y-%m-%d"),
            "dafny":   dafny_version(),
            "z3":      z3_version(),
            "notes":   "Generated by 1-generate_mutations.py (seeded candidate order, polarity-aware "
                       "bound shifts, direction confirmed by the in-context equivalence check). "
                       "Cells without an item are listed in \"failed\" with the reason.",
        },
        "items":  results,
        "failed": errors,
    }

    OUT.write_text(json.dumps(benchmark, indent=2))
    print(f"\n=== DONE ===")
    print(f"  Processed : {len(results)}/{total}")
    print(f"  Errors    : {len(errors)}")
    print(f"  Output    : {OUT}")

    # Per-tier/task breakdown
    from collections import Counter
    counts = Counter((r["tier"], r["task"]) for r in results)
    print("\nBreakdown:")
    for tier in TIERS:
        for task in TASKS:
            k = (tier, task)
            print(f"  {tier}/{task}: {counts.get(k, 0)}")


if __name__ == "__main__":
    main()
