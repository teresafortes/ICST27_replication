"""
load_scores.py  —  One long table of scores across every condition on disk.

Used by 7-analyze_conditions.py (and importable by the figure scripts). Reads the
files each pipeline step writes under scores/{condition}/:

  single-shot  diagnosis_scores.csv (2-score_diagnosis.py), repair_scores.csv
               (3-apply_repairs.py), fidelity_scores.csv (4-check_semantic_equivalence.py)
  iterative    attempts.csv + repair_scores.csv (1b-run_iterative.py --score),
               fidelity_scores.csv (4-check_semantic_equivalence.py --condition it_main)
  agentic      agent_runs.csv, repair_scores.csv, fidelity_scores.csv (1c-run_agent.py --score)
  every mode   explanation_codes.csv (5-code_explanations.py), optional: codes of the
               first diagnosis become ce_cites_value (citation level 2) and full_explanation

Rules:
  - one row per condition × model × item × run; items removed from benchmark.json dropped;
  - ground-truth labels come from benchmark.json, not from the score files;
  - unparseable responses count as wrong diagnoses and failed repairs;
  - obligation_correct is NaN for items labeled `unknown` and for ss_nofeedback (which
    doesn't ask for the obligation); otherwise a missing or unparsed answer is wrong;
  - faithful_pass = pass and fidelity in {exact, equivalent}; undetermined (or a passing
    repair with no fidelity row) is not faithful, and fidelity_unresolved flags it for
    the sensitivity analysis;
  - diagnosis columns: spec_correct / obligation_correct = the model's FIRST diagnosis
    (single-shot: its only one; iterative: turn 1; agentic: first diagnosis.json);
    *_last = its final diagnosis (equal to the first in single-shot).

Missing conditions or files are skipped with a printed note.

Usage:
  from load_scores import load_all
  data = load_all()                      # or load_all(scores_dir=Path(...))
  data.long        # main table
  data.attempts    # iterative per-attempt rows (condition column added)
  data.agent_runs  # agentic per-run rows (condition column added)
"""

from __future__ import annotations
import json, os, sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

EVAL_DIR   = Path(__file__).resolve().parent.parent
ROOT       = EVAL_DIR.parent
BENCHMARK  = ROOT / "ii-corpus" / "benchmark.json"
LEAK_CSV   = ROOT / "ii-corpus" / "intent_leak_check.csv"
SCORES_DIR = Path(os.environ.get("SCORES_DIR", EVAL_DIR / "scores"))

sys.path.insert(0, str(EVAL_DIR))
import prompts  # noqa: E402

MODELS   = ["claude-opus-5-5", "deepseek-v4-pro-0813", "qwen3.8-27b"]
FAITHFUL = {"exact", "equivalent"}

# Condition -> mode. it_main_k10 is the plateau pilot (excluded from comparisons).
MODE = {c: v["mode"] for c, v in prompts.CONDITIONS.items()} | {"it_main_k10": "iterative"}
MAIN_OF_MODE = {"single_shot": "ss_main", "iterative": "it_main", "agentic": "ag_main"}
NO_OBLIGATION_QUESTION = {"ss_nofeedback"}

COLUMNS = ["condition", "mode", "model", "item_id", "program", "run", "tier", "task", "operator",
           "gt_obligation", "parsed", "spec_correct", "obligation_correct",
           "spec_correct_last", "obligation_correct_last", "pass", "fidelity",
           "faithful_pass", "weaker", "fidelity_unresolved", "clean_pass"]


@dataclass
class Scores:
    long: pd.DataFrame
    attempts: pd.DataFrame = field(default_factory=pd.DataFrame)
    agent_runs: pd.DataFrame = field(default_factory=pd.DataFrame)
    leak: pd.DataFrame = field(default_factory=pd.DataFrame)
    notes: list = field(default_factory=list)

    def conditions(self) -> list:
        return sorted(self.long["condition"].unique()) if len(self.long) else []


def _bool(s: pd.Series) -> pd.Series:
    """CSV booleans ('True'/'False'/NaN) -> object series of True/False/NaN."""
    m = {True: True, False: False, "True": True, "False": False, "true": True, "false": False,
         1: True, 0: False, 1.0: True, 0.0: False}
    return s.map(lambda v: m.get(v, np.nan) if not (isinstance(v, float) and np.isnan(v)) else np.nan)


def _items() -> pd.DataFrame:
    items = json.loads(BENCHMARK.read_text())["items"]
    return pd.DataFrame([{"item_id": i["item_id"], "program": i["base_program"],
                          "tier": i["tier"], "task": i["task"],
                          "operator": i["mutation_type"],
                          "gt_obligation": i.get("gt_proof_obligation", "unknown")} for i in items])


def _obligation(correct: pd.Series, gt: pd.Series, asked: bool) -> pd.Series:
    """NaN for unknown labels or when the condition doesn't ask; else missing -> wrong."""
    if not asked:
        return pd.Series(np.nan, index=correct.index, dtype=object)
    out = _bool(correct).where(_bool(correct).notna(), False)
    return out.where(gt != "unknown", np.nan)


def _read(path: Path, notes: list):
    if not path.exists():
        notes.append(f"missing {path.relative_to(path.parents[1])}")
        return None
    df = pd.read_csv(path)
    return df if len(df) else None


def _add_fidelity(df: pd.DataFrame, fid, cond: str, notes: list) -> pd.DataFrame:
    if fid is not None:
        fid = fid[["model", "item_id", "run", "fidelity"]].drop_duplicates(["model", "item_id", "run"])
        df = df.merge(fid, on=["model", "item_id", "run"], how="left")
    else:
        df["fidelity"] = np.nan
    passing = df["pass"] == True
    missing = passing & df["fidelity"].isna()
    if missing.any():
        notes.append(f"{cond}: {int(missing.sum())} passing repairs have no fidelity row "
                     f"(counted as unresolved, not faithful)")
    df["fidelity"] = df["fidelity"].where(passing, np.nan)
    df["faithful_pass"] = passing & df["fidelity"].isin(FAITHFUL)
    df["weaker"] = passing & (df["fidelity"] == "weaker")
    df["fidelity_unresolved"] = passing & (df["fidelity"].isna() | (df["fidelity"] == "undetermined"))
    return df


def _single_shot(cond: str, d: Path, notes: list):
    diag, rep = _read(d / "diagnosis_scores.csv", notes), _read(d / "repair_scores.csv", notes)
    if diag is None or rep is None:
        return None
    df = diag[["model", "item_id", "run", "parse_ok", "spec_correct", "obligation_correct"]].merge(
        rep[["model", "item_id", "run", "dafny_pass"]], on=["model", "item_id", "run"], how="outer")
    df["parsed"] = _bool(df["parse_ok"]).fillna(False).astype(bool)
    df["spec_correct"] = _bool(df["spec_correct"]).fillna(False).astype(bool) & df["parsed"]
    df["pass"] = _bool(df["dafny_pass"]).fillna(False).astype(bool)
    df["_obl_raw"] = df["obligation_correct"]
    return df


def _iterative(cond: str, d: Path, notes: list):
    att, fin = _read(d / "attempts.csv", notes), _read(d / "repair_scores.csv", notes)
    if att is None or fin is None:
        return None, None
    att = att.sort_values("turn")
    first = att.groupby(["model", "item_id", "run"]).first().reset_index()
    last = att.groupby(["model", "item_id", "run"]).last().reset_index()
    df = fin[["model", "item_id", "run", "dafny_pass", "turns", "status"]].merge(
        first[["model", "item_id", "run", "parse_ok", "spec_correct", "obligation_correct"]],
        on=["model", "item_id", "run"], how="left").merge(
        last[["model", "item_id", "run", "parse_ok", "spec_correct", "obligation_correct"]].rename(
            columns={"parse_ok": "parse_ok_last", "spec_correct": "spec_last_raw",
                     "obligation_correct": "obl_last_raw"}),
        on=["model", "item_id", "run"], how="left")
    df["parsed"] = _bool(df["parse_ok"]).fillna(False).astype(bool)
    df["spec_correct"] = _bool(df["spec_correct"]).fillna(False).astype(bool) & df["parsed"]
    df["spec_correct_last"] = (_bool(df["spec_last_raw"]).fillna(False).astype(bool)
                               & _bool(df["parse_ok_last"]).fillna(False).astype(bool))
    df["pass"] = _bool(df["dafny_pass"]).fillna(False).astype(bool)
    df["_obl_raw"] = df["obligation_correct"]
    att = att.copy()
    att.insert(0, "condition", cond)
    return df, att


def _agentic(cond: str, d: Path, notes: list):
    runs = _read(d / "agent_runs.csv", notes)
    if runs is None:
        return None, None
    df = runs.copy()
    df["parsed"] = _bool(df["first_diag_ok"]).fillna(False).astype(bool)
    df["spec_correct"] = _bool(df["first_spec_correct"]).fillna(False).astype(bool) & df["parsed"]
    df["spec_correct_last"] = (_bool(df["last_spec_correct"]).fillna(False).astype(bool)
                               & _bool(df["last_diag_ok"]).fillna(False).astype(bool))
    df["pass"] = _bool(df["dafny_pass"]).fillna(False).astype(bool)
    df["_obl_raw"] = df["first_obligation_correct"]
    df["obl_last_raw"] = df["last_obligation_correct"]
    out = runs.copy()
    out.insert(0, "condition", cond)
    return df, out


def load_all(scores_dir: Path | None = None, conditions: list | None = None,
             verbose: bool = True) -> Scores:
    scores_dir = Path(scores_dir) if scores_dir else SCORES_DIR
    items = _items()
    valid = set(items["item_id"])
    notes, frames, attempts, agent_runs = [], [], [], []
    for cond in conditions or list(MODE):
        d = scores_dir / cond
        if not d.is_dir():
            notes.append(f"{cond}: no scores/{cond}/ directory, skipped")
            continue
        mode = MODE[cond]
        extra = None
        if mode == "single_shot":
            df = _single_shot(cond, d, notes)
        elif mode == "iterative":
            df, extra = _iterative(cond, d, notes)
        else:
            df, extra = _agentic(cond, d, notes)
        if df is None:
            notes.append(f"{cond}: score files incomplete, skipped")
            continue
        n0 = len(df)
        df = df[df["item_id"].isin(valid)]
        if n0 - len(df):
            notes.append(f"{cond}: dropped {n0 - len(df)} rows for items removed from benchmark.json")
        df = df.drop(columns=[c for c in ("tier", "task", "mutation_type", "program") if c in df], errors="ignore")
        df = df.merge(items, on="item_id", how="left")
        asked = cond not in NO_OBLIGATION_QUESTION
        df["obligation_correct"] = _obligation(df["_obl_raw"], df["gt_obligation"], asked)
        if "obl_last_raw" in df:
            df["obligation_correct_last"] = _obligation(df["obl_last_raw"], df["gt_obligation"], asked)
        else:
            df["obligation_correct_last"] = df["obligation_correct"]
        if "spec_correct_last" not in df:
            df["spec_correct_last"] = df["spec_correct"]
        df = _add_fidelity(df, _read(d / "fidelity_scores.csv", []), cond, notes)
        if mode == "agentic" and "clean_pass" in df:
            # agents can edit anything: a repair is clean only if made at the mutation site
            # without changing other contracts, signatures or code (1c-run_agent.py), and
            # faithful only if it is also clean (amended 2026-10-26)
            df["clean_pass"] = _bool(df["clean_pass"]).fillna(False).astype(bool)
            df["faithful_pass"] = df["faithful_pass"] & df["clean_pass"]
        else:
            if mode == "agentic":
                notes.append(f"{cond}: agent_runs.csv has no clean_pass (scored before 2026-10-26); clean_pass = pass")
            df["clean_pass"] = df["pass"]      # other modes replace the one clause at the site
        codes = _read(d / "explanation_codes.csv", [])
        if codes is not None:
            codes = codes.assign(ce_cites_value=codes["ce_citation_first"] == 2,
                                 full_explanation=_bool(codes["full_explanation_first"]).fillna(False).astype(bool))
            df = df.merge(codes[["model", "item_id", "run", "ce_cites_value", "full_explanation"]],
                          on=["model", "item_id", "run"], how="left")
        df["condition"], df["mode"] = cond, mode
        df["run"] = df["run"].astype(int)
        keep = COLUMNS + [c for c in ("turns", "status", "verify_calls", "resolve_calls", "edit_scope",
                                      "suspicious_edits", "diagnosis_before_edit", "steps",
                                      "ce_cites_value", "full_explanation")
                          if c in df.columns]
        frames.append(df[keep])
        if extra is not None:
            extra = extra[extra["item_id"].isin(valid)]
            (attempts if mode == "iterative" else agent_runs).append(extra)

    long = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
    leak = pd.DataFrame()
    if LEAK_CSV.exists():
        leak = pd.read_csv(LEAK_CSV)[["item_id", "proxy_leak", "judge_verdict"]]
        leak = leak[leak["item_id"].isin(valid)]
    out = Scores(long=long,
                 attempts=pd.concat(attempts, ignore_index=True) if attempts else pd.DataFrame(),
                 agent_runs=pd.concat(agent_runs, ignore_index=True) if agent_runs else pd.DataFrame(),
                 leak=leak, notes=notes)
    if verbose:
        found = ", ".join(f"{c} ({(long['condition'] == c).sum()} rows)" for c in out.conditions())
        print(f"[load_scores] {scores_dir}\n  loaded: {found or 'nothing'}")
        for n in notes:
            print(f"  note: {n}")
    return out


if __name__ == "__main__":
    s = load_all(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    if len(s.long):
        print(s.long.groupby(["condition", "model"])[["parsed", "spec_correct", "pass", "faithful_pass"]]
              .mean().round(3).to_string())
