#!/usr/bin/env python3
"""
5-code_explanations.py  —  Rule-based coding of the models' final diagnoses.

Codes are applied to the final answer only (the "diagnosis" text of the JSON answer or of
diagnosis.json), never to reasoning traces: Opus 5.5 exposes only a summary of its
thinking, so traces are not comparable across models.

Codes (deterministic; same input, same code)
  ce_citation        0 = the diagnosis does not use the counterexample
                     1 = mentions the counterexample without citing a value
                     2 = cites at least one counterexample value: a variable from the
                         counterexample together with its value (|a| and a.Length are
                         treated as the same name), or a distinctive value (|v| >= 10,
                         not a literal of the program) anywhere in the text
  clause_named       the diagnosis names the faulty clause: it quotes the mutated or the
                     original clause, or one of their conjuncts (whitespace ignored), or
                     gives the clause's line number. For a deleted clause the original
                     (removed) clause is the faulty one.
  point_named        the diagnosis says where the check fails: loop entry, preservation by
                     the loop body, loop exit / return, or a call site (point = which)
  full_explanation   clause_named and point_named

Inputs (written by the scoring steps)
  single-shot  scores/{condition}/diagnosis_scores.csv   pred_diagnosis
  iterative    scores/it_main/attempts.csv               pred_diagnosis (first and last turn)
  agentic      scores/ag_main/agent_runs.csv             first_diagnosis, last_diagnosis
Output
  scores/{condition}/explanation_codes.csv   one row per model × item × run, codes for the
                                             first and the last diagnosis (*_first, *_last)

--review-sample N writes N coded diagnoses for a human spot-check.

Usage
  python3 iii-evaluation/5-code_explanations.py --condition ss_main
  python3 iii-evaluation/5-code_explanations.py --all            # every condition on disk
  python3 iii-evaluation/5-code_explanations.py --condition ss_main --review-sample 40
"""

from __future__ import annotations
import argparse, importlib.util, json, re, sys
from functools import lru_cache
from pathlib import Path

import pandas as pd

EVAL_DIR  = Path(__file__).resolve().parent
ROOT      = EVAL_DIR.parent
CORPUS    = ROOT / "ii-corpus"
SCORES    = EVAL_DIR / "scores"
TABLES    = EVAL_DIR / "analysis" / "tables"

sys.path.insert(0, str(EVAL_DIR))
import prompts  # noqa: E402

_spec = importlib.util.spec_from_file_location("equiv", EVAL_DIR / "4-check_semantic_equivalence.py")
equiv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(equiv)

KEY = ["model", "item_id", "run"]


# ─── counterexample citation ──────────────────────────────────────────────────

NAME  = r"[A-Za-z_|][\w.\[\]|'?]*"
VALUE = r"-?\d+|true|false"
ASSIGN_RE = re.compile(rf"({NAME})\s*(?<![=!<>])=(?!=)\s*({VALUE})\b")
EQ_RE     = re.compile(rf"({NAME})\s*==\s*({VALUE})\b|(?<![\w.])({VALUE})\s*==\s*({NAME})")
CE_WORD   = re.compile(r"counter[- ]?examples?|\bcex\b", re.I)
LINK      = r"\s*(?:==|=|:|\bis\b|\bwas\b|\bequals\b|\bbecomes\b|->|→)\s*"


def ce_pairs(counterexample_nl: str) -> set[tuple[str, str]]:
    """(name, value) pairs stated by the counterexample."""
    pairs = set()
    for m in ASSIGN_RE.finditer(counterexample_nl or ""):
        pairs.add((m.group(1), m.group(2)))
    for m in EQ_RE.finditer(counterexample_nl or ""):
        name, value = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        pairs.add((name, value))
    return {(n.rstrip(".?"), v) for n, v in pairs if not re.fullmatch(VALUE, n)}


def aliases(name: str) -> list[str]:
    """Spellings of one counterexample name: this.x / x, |a| / a.Length."""
    base = name[5:] if name.startswith("this.") else name
    out = {name, base}
    if m := re.fullmatch(r"\|(.+)\|", base):
        out |= {f"{m.group(1)}.Length", f"len({m.group(1)})"}
    if m := re.fullmatch(r"(.+)\.Length", base):
        out.add(f"|{m.group(1)}|")
    return sorted(out, key=len, reverse=True)


def ce_citation(text: str, counterexample_nl: str, source: str = "") -> int:
    text = text or ""
    pairs = ce_pairs(counterexample_nl)
    literals = set(re.findall(r"-?\d+", source or ""))
    for name, value in pairs:
        v = re.escape(value)
        for a in aliases(name):
            if re.search(rf"(?<![\w.]){re.escape(a)}{LINK}{v}(?![\w.])", text) or \
               re.search(rf"(?<![\w.]){v}\s*==\s*{re.escape(a)}(?![\w.])", text):
                return 2
        if value.lstrip("-").isdigit() and abs(int(value)) >= 10 and value not in literals \
                and re.search(rf"(?<![\w.]){v}(?![\w.])", text):
            return 2
    return 1 if CE_WORD.search(text) else 0


# ─── clause and failure point ─────────────────────────────────────────────────

POINTS = {
    "entry": r"\b(on|at|upon) (loop )?entry|before the loop|loop entry|entering the loop|"
             r"\binitially\b|\binitiali[sz]|\bnot (be )?established|\bestablish|start of the loop|"
             r"first iteration|before the first iteration|\binitiation\b",
    "preservation": r"\bmaintain|\bpreserv|after (each|one|an|the|a) iteration|loop body|"
                    r"\binductive|each iteration|next iteration|re-?establish",
    "exit": r"\b(on|at|upon|after) (loop )?exit|after the loop|loop (exits|terminates|ends|finishes)|"
            r"when the loop (exits|terminates|ends|finishes)|end of the (loop|method)|"
            r"\b(on|at|upon) return|when .{0,30}returns|negat\w* (of the )?(loop )?(guard|condition)|"
            r"\bbridge\b",
    "call": r"call(er|ers| site| sites)\b|at the call|before calling|when calling",
}
POINT_RE = {k: re.compile(v, re.I) for k, v in POINTS.items()}


def norm(s: str) -> str:
    return re.sub(r"[\s`;]+", "", s or "")


def clause_body(clause: str) -> str:
    clause = re.sub(r"//.*", "", clause or "")
    return re.sub(r"^\s*(requires|ensures|invariant|decreases)\b", "", clause.strip()).strip().rstrip(";")


def fragments(clause: str) -> list[str]:
    """The clause body and its top-level conjuncts, normalized; short fragments dropped."""
    body = clause_body(clause)
    parts = [body] + [p for p in re.split(r"&&", body) if p.strip()]
    out = [norm(p) for p in parts]
    return [p for p in out if len(p) >= 4 and re.search(r"[A-Za-z_]", p) and re.search(r"[<>=!+\-*/%|]|\w\(", p)]


@lru_cache(maxsize=None)
def faulty_clause(item_id: str) -> tuple[tuple[str, ...], frozenset]:
    """(normalized fragments of the original and mutated clause, their line numbers)."""
    item = ITEMS[item_id]
    kw = equiv.TASK_TO_KEYWORD[item["task"]]
    orig_path = CORPUS / item["tier"] / item["task"] / f"{item['base_program']}.dfy"
    if not orig_path.exists():
        return (), frozenset()
    orig = orig_path.read_text()
    k, mline = equiv.locate_original_clause(orig, item["source"], kw)
    if k is None:
        return (), frozenset()
    lines = orig.splitlines()
    original = equiv.clause_text(lines, k, equiv.clause_extent(lines, k))
    frags = set(fragments(original)) | set(fragments(mline or ""))
    line_nos = {k + 1}
    if mline:
        line_nos |= {i + 1 for i, l in enumerate(item["source"].splitlines()) if l.strip() == mline.strip()}
    return tuple(sorted(frags, key=len, reverse=True)), frozenset(line_nos)


def code_text(text: str, item_id: str) -> dict:
    item = ITEMS[item_id]
    text = text if isinstance(text, str) else ""
    frags, line_nos = faulty_clause(item_id)
    t = norm(text)
    lines_cited = {int(n) for n in re.findall(r"\blines?\s+(\d+)", text, re.I)}
    clause = bool(text) and (any(f in t for f in frags) or bool(lines_cited & line_nos))
    points = [k for k, rx in POINT_RE.items() if rx.search(text)]
    return {"ce_citation": ce_citation(text, item["counterexample_nl"], item["source"]) if text else 0,
            "clause_named": clause, "point_named": bool(points), "point": "|".join(points),
            "full_explanation": clause and bool(points)}


# ─── per condition ────────────────────────────────────────────────────────────

def load_items() -> dict:
    return {i["item_id"]: i for i in json.loads((CORPUS / "benchmark.json").read_text())["items"]}


ITEMS = load_items()


def diagnoses(cond: str) -> pd.DataFrame | None:
    """KEY + first/last diagnosis text for one condition."""
    d, mode = SCORES / cond, prompts.CONDITIONS.get(cond, {}).get("mode", "iterative")
    if mode == "single_shot":
        f = d / "diagnosis_scores.csv"
        if not f.exists():
            return None
        df = pd.read_csv(f)[KEY + ["pred_diagnosis"]]
        return df.assign(first=df["pred_diagnosis"], last=df["pred_diagnosis"])[KEY + ["first", "last"]]
    if mode == "iterative":
        f = d / "attempts.csv"
        if not f.exists():
            return None
        att = pd.read_csv(f).sort_values("turn").groupby(KEY)["pred_diagnosis"]
        return pd.concat([att.first().rename("first"), att.last().rename("last")], axis=1).reset_index()
    f = d / "agent_runs.csv"
    if not f.exists():
        return None
    df = pd.read_csv(f)
    return df.rename(columns={"first_diagnosis": "first", "last_diagnosis": "last"})[KEY + ["first", "last"]]


def code_condition(cond: str) -> pd.DataFrame | None:
    df = diagnoses(cond)
    if df is None:
        print(f"{cond}: no score files, skipped")
        return None
    df = df[df["item_id"].isin(ITEMS)]
    rows = []
    for r in df.itertuples(index=False):
        row = {"model": r.model, "item_id": r.item_id, "run": r.run}
        for tag in ("first", "last"):
            row |= {f"{k}_{tag}": v for k, v in code_text(getattr(r, tag), r.item_id).items()}
        rows.append(row)
    out = pd.DataFrame(rows)
    out.to_csv(SCORES / cond / "explanation_codes.csv", index=False)
    summary = out.groupby("model").agg(
        n=("item_id", "size"),
        ce_cites_value=("ce_citation_first", lambda s: (s == 2).mean()),
        ce_mentions=("ce_citation_first", lambda s: (s >= 1).mean()),
        clause_named=("clause_named_first", "mean"), point_named=("point_named_first", "mean"),
        full_explanation=("full_explanation_first", "mean"))
    print(f"\n{cond} → scores/{cond}/explanation_codes.csv (first diagnosis)\n{summary.round(3).to_string()}")
    return out


# ─── validation ───────────────────────────────────────────────────────────────

def agreement(a: pd.Series, b: pd.Series) -> dict:
    """Raw agreement, Cohen's kappa and Gwet's AC1 for two binary codings."""
    a, b = a.astype(int), b.astype(int)
    n, po = len(a), (a == b).mean()
    pa, pb = a.mean(), b.mean()
    pe_k = pa * pb + (1 - pa) * (1 - pb)
    pi = (pa + pb) / 2
    pe_g = 2 * pi * (1 - pi)
    return {"n": n, "agreement": po, "kappa": (po - pe_k) / (1 - pe_k) if pe_k < 1 else float("nan"),
            "ac1": (po - pe_g) / (1 - pe_g) if pe_g < 1 else float("nan"),
            "human_rate": pb, "rule_rate": pa,
            "rule1_human0": int(((a == 1) & (b == 0)).sum()), "rule0_human1": int(((a == 0) & (b == 1)).sum())}




def review_sample(cond: str, n: int):
    codes, diags = pd.read_csv(SCORES / cond / "explanation_codes.csv"), diagnoses(cond)
    df = codes.merge(diags, on=KEY).sample(n=min(n, len(codes)), random_state=2027)
    df["counterexample_nl"] = df["item_id"].map(lambda i: ITEMS[i]["counterexample_nl"])
    df["mutation_desc"] = df["item_id"].map(lambda i: ITEMS[i]["mutation_desc"])
    cols = KEY + ["mutation_desc", "counterexample_nl", "first", "ce_citation_first",
                  "clause_named_first", "point_first"]
    out = SCORES / cond / "explanation_review_sample.csv"
    df[cols].rename(columns={"first": "diagnosis"}).assign(human_ce="", human_clause="", human_point="").to_csv(out, index=False)
    print(f"wrote {out} ({len(df)} rows; fill the human_* columns)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--condition", help="condition to code (scores/{condition}/)")
    ap.add_argument("--all", action="store_true", help="code every condition with score files")
    ap.add_argument("--review-sample", type=int, metavar="N", help="write N coded diagnoses for spot-checking")
    args = ap.parse_args()
    conds = [c for c in list(prompts.CONDITIONS) if (SCORES / c).is_dir()] if args.all else \
            ([args.condition] if args.condition else [])
    for c in conds:
        code_condition(c)
        if args.review_sample:
            review_sample(c, args.review_sample)
    if not conds:
        ap.print_help()


if __name__ == "__main__":
    main()
