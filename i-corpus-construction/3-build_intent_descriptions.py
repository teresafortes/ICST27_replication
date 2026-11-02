#!/usr/bin/env python3
"""
3-build_intent_descriptions.py  —  Build and leak-check the intended-behavior descriptions.

One description per ORIGINAL (pre-mutation) program, used by the ss_intent condition
(iii-evaluation/prompts.py). Descriptions are docstring-level: what each method
does and its key behavior, in plain English, two to four sentences, with no Dafny syntax,
formulas or exact bounds. Plain-language properties ("the array must be sorted") are fine,
because real docstrings say that; bounds, off-by-one details and formulas are not.

Sources
  mbpp     MBPP problem statement for the task id (56 programs, dafny-synthesis_task_id_N).
           https://raw.githubusercontent.com/google-research/google-research/master/mbpp/mbpp.jsonl
  clover   CloverBench docstring, dataset/CloverBench/textbook_algo/<name>/<name>_spec.txt
           (18 programs, Clover_<name>). https://github.com/ChuyueSun/Clover
  written  Hand-written from the original program (237 programs), stored in
           i-corpus-construction/intent_written.json ("written").
A real (mbpp/clover) text that fails the text check is kept verbatim in original_text and
flagged; its description is the docstring-level rewrite in intent_written.json
("edited_real") when there is one, so nothing is edited silently.

Checks
  1. Text check (automatic): reject descriptions with comparison or arithmetic operators,
     Dafny syntax or keywords, or a numeric literal that appears in the program's spec
     clauses. Number words for those literals are reported as warnings.
  2. Clause check, per benchmark item (we know which clause was mutated):
       proxy (free, always run): flag the item when the description contains a distinctive
         identifier of the original clause with a quantity word near it, or one of the
         clause's numeric literals (digits or words).
       judge (--judge-prompts writes the prompts; --judge runs them): a judge model is asked
         whether the description states the original clause precisely enough to write it
         back. LEAK = yes. This is the leak rate reported in the paper.

Outputs
  ii-corpus/intent_descriptions.json  descriptions + check results (reviewed stays false)
  ii-corpus/intent_review_sheet.csv   one row per program, for the human reviewer
  ii-corpus/intent_leak_check.csv     one row per benchmark item (proxy, and judge if run)
  iii-evaluation/logs/intent_judge_prompts.jsonl  (--judge-prompts / --judge)

Usage
  python3 i-corpus-construction/3-build_intent_descriptions.py [--sources DIR]
      DIR holds mbpp.jsonl and the unzipped Clover repository (Clover-main/). Without
      --sources, the original texts already stored in intent_descriptions.json are reused.
  ... --judge-prompts            also write the judge prompts (no API calls)
  ... --judge --yes-spend        run the judge (paid API calls; GPT-5.4 by default)
"""

from __future__ import annotations
import argparse, csv, importlib.util, json, re, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT        = Path(__file__).parent.parent
CORPUS      = ROOT / "ii-corpus"
BENCHMARK   = CORPUS / "benchmark.json"
OUT_JSON    = CORPUS / "intent_descriptions.json"
REVIEW_CSV  = CORPUS / "intent_review_sheet.csv"
LEAK_CSV    = CORPUS / "intent_leak_check.csv"
WRITTEN     = Path(__file__).parent / "intent_written.json"
JUDGE_JSONL = ROOT / "iii-evaluation" / "logs" / "intent_judge_prompts.jsonl"
JUDGE_OUT   = ROOT / "iii-evaluation" / "logs" / "intent_judge_results.jsonl"

_spec = importlib.util.spec_from_file_location(
    "eq", ROOT / "iii-evaluation" / "4-check_semantic_equivalence.py")
eq = importlib.util.module_from_spec(_spec)
sys.path.insert(0, str(ROOT / "iii-evaluation"))
_spec.loader.exec_module(eq)

ABOUT = (
    "Intended-behavior descriptions for the ss_intent condition, one per "
    "ORIGINAL (pre-mutation) program, built by i-corpus-construction/3-build_intent_descriptions.py. "
    "source: 'mbpp' = MBPP problem statement for the task id; 'clover' = CloverBench docstring; "
    "'written' = hand-written from the original program (intent_written.json). Docstring level: "
    "purpose and key behavior in plain English, no Dafny syntax, formulas or exact bounds. "
    "original_text keeps a real description verbatim; edited=true means description is a "
    "docstring-level rewrite of it because the original failed the text check. "
    "Set reviewed to true only after a co-author checks the text; prompts refuse to build "
    "for programs without a reviewed description.")

# ─── check 1: text check ──────────────────────────────────────────────────────

OPERATOR_RE = re.compile(
    r"<=|>=|==|!=|=>|<|>|≤|≥|≠|="                 # comparison / implication
    r"|\w\s*[*/%^]\s*\w"                          # arithmetic between operands
    r"|\w\s*\+\s*\w|\w\s+-\s+\w")                 # a + b, a - b (not hyphenated words)
# Dafny tokens that are not ordinary English. Words that are also English ("requires",
# "exists", "array", "true") are allowed; their Dafny use is caught by the syntax patterns.
SYNTAX_RE = re.compile(
    r"::|:=|\.\.|\|\w[^|]*\||\w\[[^\]]*\]|\.Length\b|\bold\s*\(|\bmultiset\s*[({]"
    r"|\b(?:ensures|invariant|modifies|forall|ghost|nat|int|bool|seq|array2)\b")
NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "twenty": 20,
    "hundred": 100, "thousand": 1000, "half": 2, "twice": 2, "double": 2, "triple": 3,
    "dozen": 12}
SPEC_LINE_RE = re.compile(r"^\s*(requires|ensures|invariant|decreases)\b")


def original_path(item: dict) -> Path:
    return CORPUS / item["tier"] / item["task"] / f"{item['base_program']}.dfy"


def spec_literals(source: str) -> set[str]:
    """Numeric literals in the program's spec clauses (requires/ensures/invariant/decreases)."""
    lines = source.splitlines()
    lits = set()
    for k, line in enumerate(lines):
        if SPEC_LINE_RE.match(line):
            text = eq.strip_line_comment(eq.clause_text(lines, k, eq.clause_extent(lines, k)))
            lits |= {n.lstrip("-") for n in NUMBER_RE.findall(text)}
    return lits


def text_check(desc: str, literals: set[str]) -> tuple[list[str], list[str]]:
    """(violations, warnings) for one description."""
    violations, warnings = [], []
    if m := OPERATOR_RE.search(desc):
        violations.append(f"operator {m.group(0)!r}")
    if m := SYNTAX_RE.search(desc):
        violations.append(f"Dafny syntax {m.group(0)!r}")
    for n in NUMBER_RE.findall(desc):
        if n.lstrip("-") in literals:
            violations.append(f"spec literal {n}")
    words = re.findall(r"[a-z]+", desc.lower())
    for w in words:
        if w in NUMBER_WORDS and str(NUMBER_WORDS[w]) in literals and NUMBER_WORDS[w] >= 2:
            warnings.append(f"number word {w!r} ({NUMBER_WORDS[w]} is a spec literal)")
    n_sent = len(re.findall(r"[.!?](?:\s|$)", desc.strip()))
    if not 1 <= n_sent <= 4:
        warnings.append(f"{n_sent} sentences")
    return violations, sorted(set(warnings))


# ─── check 2: clause check ────────────────────────────────────────────────────

DAFNY_WORDS = {
    "forall", "exists", "old", "Length", "Length0", "Length1", "multiset", "seq", "set", "map",
    "int", "nat", "bool", "real", "char", "string", "true", "false", "null", "in", "if", "then",
    "else", "var", "fresh", "reads", "modifies", "requires", "ensures", "invariant",
    "decreases", "this", "Keys", "Values", "Items", "as", "match", "case", "unchanged",
    "allocated", "new", "returns", "result", "res", "ret", "len", "arr", "idx", "tmp"}
QUANTITY_WORDS = re.compile(
    r"^(?:least|most|more|less|fewer|greater|smaller|larger|bigger|exceed\w*|bound\w*|between"
    r"|positive|negative|nonnegative|non-negative|nonzero|non-zero|zero|one|strict\w*|below"
    r"|above|under|over|within|plus|minus|times|twice|double\w*|half|square\w*|sum|equal\w*"
    r"|same|minimum|maximum|limit\w*|first|last|before|after|up|exactly|only|every|all|each"
    r"|no|none|never|always|inclusive|exclusive|including|excluding|beyond|range|length\w*"
    r"|size)$")
WINDOW = 6


def split_identifier(name: str) -> list[str]:
    """isSortedRange / is_sorted_range -> ['is', 'sorted', 'range']."""
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).replace("_", " ").split()
    return [p.lower() for p in parts]


def clause_keywords(clause: str) -> set[str]:
    """Distinctive words of a clause: identifier parts of four or more letters."""
    words = set()
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_']*", clause):
        if ident in DAFNY_WORDS or len(ident) < 3:
            continue
        words |= {p for p in split_identifier(ident) if len(p) >= 4 and p not in DAFNY_WORDS}
    return words


def proxy_leak(desc: str, clause: str) -> str | None:
    """Reason string when the description likely restates the clause, else None."""
    words = re.findall(r"[a-z][a-z-]*", desc.lower())
    for n in NUMBER_RE.findall(clause):
        n = n.lstrip("-")
        if n in ("0", "1"):
            continue
        if re.search(rf"(?<![\w.]){re.escape(n)}(?![\w.])", desc):
            return f"clause literal {n}"
        for w, v in NUMBER_WORDS.items():
            if str(v) == n and w in words and w not in ("half", "double"):
                return f"clause literal {n} as {w!r}"
    keys = clause_keywords(clause)
    for i, w in enumerate(words):
        hit = next((k for k in sorted(keys) if w.startswith(k[:6]) or k.startswith(w) and len(w) >= 4), None)
        if hit:
            near = words[max(0, i - WINDOW): i + WINDOW + 1]
            q = next((x for x in near if QUANTITY_WORDS.match(x)), None)
            if q:
                return f"identifier '{hit}' near quantity word '{q}'"
    return None


def item_clauses(item: dict) -> tuple[str | None, str | None, str]:
    """(original clause, mutated clause, masked original program) for one item."""
    kw = eq.TASK_TO_KEYWORD[item["task"]]
    orig = original_path(item).read_text()
    k, mline = eq.locate_original_clause(orig, item["source"], kw)
    if k is None:
        return None, None, orig
    lines = orig.splitlines()
    end = eq.clause_extent(lines, k)
    original = eq.clause_text(lines, k, end).strip()
    indent = re.match(r"\s*", lines[k]).group(0)
    masked = "\n".join(lines[:k] + [f"{indent}{kw} /* ??? the clause in question ??? */"] + lines[end:])
    return original, (mline.strip() if mline else None), masked


JUDGE_PROMPT = """\
You are auditing a benchmark for information leakage. The benchmark gives a model a \
Dafny program in which one specification clause was changed, and asks it to restore the \
clause. Some conditions also give the model a short natural-language description of the \
program's intended behavior. We must check that the description does not give the answer \
away.

=== DESCRIPTION SHOWN TO THE MODEL ===
{description}

=== PROGRAM (the clause in question is masked) ===
{masked}

=== THE ORIGINAL CLAUSE AT THE MASKED LOCATION ===
{original}

Question: using ONLY the description (not the code around the masked clause), could a \
reader write back this exact clause, including its bounds, comparison operators \
(strict or not), constants and arithmetic? Saying a property in general terms (for \
example "the array is sorted" or "the index is within the array") does not count as \
stating it precisely; stating the exact bound, off-by-one detail, constant or formula does.

Respond with ONLY a JSON object:
{{"verdict": "<leak | partial | no_leak>", "evidence": "<the words in the description that \
give the clause away, or empty>"}}
  leak     the description states the clause precisely enough to write it back exactly
  partial  the description names the property but not its exact bounds/operators/constants
  no_leak  the description says nothing specific about this clause\
"""


def build_judge_prompt(desc: str, masked: str, original: str) -> str:
    return JUDGE_PROMPT.format(description=desc, masked=masked, original=original)


def run_judge(prompts: list[dict], model_key: str, workers: int = 8) -> dict[str, dict]:
    """Paid API calls (leak_judge.py). Resumable: items already in JUDGE_OUT are skipped."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    spec = importlib.util.spec_from_file_location("leak_judge", Path(__file__).parent / "leak_judge.py")
    judge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(judge)
    client, cfg = judge.make_client(model_key)
    done = {}
    if JUDGE_OUT.exists():
        for line in JUDGE_OUT.read_text().splitlines():
            r = json.loads(line)
            if r.get("judge") == model_key and r.get("verdict"):
                done[r["item_id"]] = r
    todo = [p for p in prompts if p["item_id"] not in done]
    print(f"Leak judge {model_key}: {len(todo)} items to judge ({len(done)} done)")
    lock = threading.Lock()

    def one(p):
        raw = judge.call(client, cfg, p["prompt"])
        parsed = judge.extract_json(raw) or {}
        r = {"item_id": p["item_id"], "judge": model_key, "verdict": parsed.get("verdict"),
             "evidence": parsed.get("evidence"), "raw": raw}
        with lock:
            with JUDGE_OUT.open("a") as f:
                f.write(json.dumps(r) + "\n")
            done[p["item_id"]] = r
            if len(done) % 50 == 0:
                print(f"  {len(done)}/{len(prompts)}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, todo))
    return done


# ─── sources ──────────────────────────────────────────────────────────────────

def load_real_texts(sources: Path | None, programs: dict, existing: dict) -> dict[str, str]:
    """Original text for every mbpp/clover program."""
    texts = {}
    mbpp = {}
    if sources:
        for line in (sources / "mbpp.jsonl").read_text().splitlines():
            r = json.loads(line)
            mbpp[r["task_id"]] = r["text"].strip()
        clover = sources / "Clover-main" / "dataset" / "CloverBench" / "textbook_algo"
    for prog, src in programs.items():
        if src not in ("mbpp", "clover"):
            continue
        if sources and src == "mbpp":
            texts[prog] = mbpp[int(prog.rsplit("_", 1)[1])]
        elif sources and src == "clover":
            name = prog[len("Clover_"):]
            texts[prog] = (clover / name / f"{name}_spec.txt").read_text().strip()
        else:
            texts[prog] = existing.get(prog, {}).get("original_text")
            if not texts[prog]:
                raise SystemExit(f"No original text for {prog}; run with --sources DIR.")
    return texts


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", type=Path, help="directory with mbpp.jsonl and Clover-main/")
    ap.add_argument("--judge-prompts", action="store_true", help="write the judge prompts (no API calls)")
    ap.add_argument("--judge", action="store_true", help="run the judge model (paid API calls)")
    ap.add_argument("--judge-model", default="gpt-5.4", help="key in leak_judge.py JUDGE_MODELS")
    ap.add_argument("--yes-spend", action="store_true", help="confirm paid API calls for --judge")
    args = ap.parse_args()
    if args.judge and not args.yes_spend:
        raise SystemExit("--judge makes paid API calls; add --yes-spend to confirm.")

    bench = json.loads(BENCHMARK.read_text())
    items = bench["items"]
    existing = json.loads(OUT_JSON.read_text())["programs"] if OUT_JSON.exists() else {}
    written = json.loads(WRITTEN.read_text())
    one_item = {}
    for it in items:
        one_item.setdefault(it["base_program"], it)

    programs = {}
    for prog in sorted(one_item):
        if prog.startswith("Clover_"):
            programs[prog] = "clover"
        elif re.fullmatch(r"dafny-synthesis_task_id_\d+", prog):
            programs[prog] = "mbpp"
        else:
            programs[prog] = "written"
    real = load_real_texts(args.sources, programs, existing)

    # ── descriptions + check 1
    out, literals = {}, {}
    for prog, src in programs.items():
        literals[prog] = spec_literals(original_path(one_item[prog]).read_text())
        entry = {"source": src}
        if src == "written":
            desc = written["written"].get(prog)
            if not desc:
                raise SystemExit(f"No written description for {prog} in {WRITTEN.name}")
        else:
            entry["original_text"] = real[prog]
            desc = real[prog]
        v, w = text_check(desc, literals[prog])
        entry["original_text_check"] = {"pass": not v, "violations": v} if src != "written" else None
        # A real text is replaced only by an explicit rewrite in intent_written.json, made
        # because it failed the text check or (edit_reason) states a formula in words.
        if src != "written" and prog in written["edited_real"]:
            desc = written["edited_real"][prog]["description"]
            entry["edited"] = True
            entry["edit_reason"] = written["edited_real"][prog].get("reason")
            v, w = text_check(desc, literals[prog])
        else:
            entry["edited"] = False
        entry["description"] = desc
        entry["text_check"] = {"pass": not v, "violations": v, "warnings": w}
        entry["reviewed"] = existing.get(prog, {}).get("reviewed", False) and \
            existing.get(prog, {}).get("description") == desc
        if entry["original_text_check"] is None:
            del entry["original_text_check"]
        out[prog] = entry

    # ── check 2: clause check
    rows, prompts = [], []
    judge_results = {}
    for it in items:
        prog = it["base_program"]
        original, mutated, masked = item_clauses(it)
        desc = out[prog]["description"]
        row = {"item_id": it["item_id"], "program": prog, "source": programs[prog],
               "task": it["task"], "mutation_type": it["mutation_type"],
               "original_clause": original, "mutated_clause": mutated,
               "proxy_leak": None, "proxy_reason": None, "judge_verdict": None, "judge_evidence": None}
        if original is None:
            row["proxy_reason"] = "clause not located"
        else:
            reason = proxy_leak(desc, original)
            row["proxy_leak"], row["proxy_reason"] = bool(reason), reason
            prompts.append({"item_id": it["item_id"], "prompt": build_judge_prompt(desc, masked, original)})
        rows.append(row)

    if args.judge_prompts or args.judge:
        JUDGE_JSONL.parent.mkdir(parents=True, exist_ok=True)
        JUDGE_JSONL.write_text("".join(json.dumps(p) + "\n" for p in prompts))
        n_chars = sum(len(p["prompt"]) for p in prompts)
        print(f"Wrote {len(prompts)} judge prompts to {JUDGE_JSONL.relative_to(ROOT)} "
              f"(~{n_chars // 4:,} input tokens at 4 chars/token)")
    if args.judge:
        judge_results = run_judge(prompts, args.judge_model)
        for row in rows:
            r = judge_results.get(row["item_id"])
            if r:
                row["judge_verdict"], row["judge_evidence"] = r["verdict"], r["evidence"]

    # per-program proxy summary
    flagged = defaultdict(list)
    for row in rows:
        if row["proxy_leak"]:
            flagged[row["program"]].append(f"{row['item_id']}: {row['proxy_reason']}")
    for prog, entry in out.items():
        entry["proxy_leak_items"] = len(flagged[prog])

    OUT_JSON.write_text(json.dumps({"_about": ABOUT, "programs": out}, indent=1, ensure_ascii=False) + "\n")
    with LEAK_CSV.open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    n_items = Counter(r["program"] for r in rows)
    old_notes = ({r["program"]: r.get("reviewer_notes", "") for r in csv.DictReader(REVIEW_CSV.open(newline=""))}
                 if REVIEW_CSV.exists() else {})
    with REVIEW_CSV.open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["program", "source", "edited", "description", "original_text",
                     "text_check_violations", "text_check_warnings", "n_items",
                     "proxy_leak_items", "proxy_reasons", "reviewed", "reviewer_notes"])
        for prog, e in out.items():
            # keep the review: reviewed comes from the JSON (reset when a description changes),
            # reviewer notes from the previous sheet
            wr.writerow([prog, e["source"], e["edited"], e["description"], e.get("original_text", ""),
                         "; ".join(e["text_check"]["violations"]), "; ".join(e["text_check"]["warnings"]),
                         n_items[prog], e["proxy_leak_items"], " | ".join(flagged[prog]),
                         "TRUE" if e["reviewed"] else "", old_notes.get(prog, "")])

    # ── report
    print(f"Programs: {len(out)}  " + "  ".join(f"{s}={c}" for s, c in Counter(programs.values()).items()))
    orig_fail = [p for p, e in out.items() if e.get("original_text_check") and not e["original_text_check"]["pass"]]
    print(f"Real texts failing the text check: {len(orig_fail)}" +
          "".join(f"\n  {p}: {out[p]['original_text_check']['violations']}"
                  f"{'  -> edited' if out[p]['edited'] else '  -> FLAGGED, no rewrite'}" for p in orig_fail))
    fails = [p for p, e in out.items() if not e["text_check"]["pass"]]
    print(f"Descriptions failing the text check: {len(fails)}" +
          "".join(f"\n  {p}: {out[p]['text_check']['violations']}" for p in fails))
    warns = [p for p, e in out.items() if e["text_check"]["warnings"]]
    print(f"Descriptions with warnings: {len(warns)}" +
          "".join(f"\n  {p}: {out[p]['text_check']['warnings']}" for p in warns))
    located = [r for r in rows if r["proxy_leak"] is not None]
    print(f"\nClause check (proxy): {sum(r['proxy_leak'] for r in located)}/{len(located)} items flagged "
          f"({100 * sum(r['proxy_leak'] for r in located) / len(located):.1f}%); "
          f"{len(rows) - len(located)} clauses not located")
    cells = defaultdict(lambda: [0, 0])
    for r in located:
        for key in (("task", r["task"]), ("op", r["mutation_type"]), ("source", r["source"])):
            cells[key][0] += r["proxy_leak"]
            cells[key][1] += 1
    for key in sorted(cells):
        k, n = cells[key]
        print(f"  {key[0]:6} {key[1]:8} {k:4d}/{n:<4d} {100 * k / n:5.1f}%")
    if judge_results:
        v = Counter(r["judge_verdict"] for r in rows if r["judge_verdict"])
        print(f"\nClause check (judge {args.judge_model}): {dict(v)}; "
              f"leak rate {v['leak'] / max(1, sum(v.values())):.1%}")


if __name__ == "__main__":
    main()
