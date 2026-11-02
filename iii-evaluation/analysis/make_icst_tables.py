#!/usr/bin/env python3
"""
make_icst_tables.py  —  LaTeX tables for the ICST'27 paper, in the style of the ICLR'27
tables: booktabs, grouped column headers, a logo before each model name, cell shading
normalized within each column, and the best value of each column in bold.

Reads the scores (analysis/load_scores.py) and the outputs of 7-analyze_conditions.py.
Writes iii-evaluation/analysis/paper_assets/tables/*.tex, each a complete table environment to \\input.
The paper needs \\usepackage[table]{xcolor} and \\input{icst_macros}.

  tab_benchmark.tex    benchmark composition (pool and evaluation set); no runs needed
  tab_modes.tex        diagnosis and repair by mode on the shared N-item subset (RQ1-RQ3)
  tab_singleshot.tex   single-shot main condition (600 items) and the paired change for
                       each variation (RQ1, RQ4)
  tab_agents.tex       agent behaviour: turns, verify calls, clean pass, edits

Usage:
  python3 iii-evaluation/analysis/make_icst_tables.py
  python3 iii-evaluation/analysis/make_icst_tables.py --scores-dir DIR --tables-dir DIR --demo
      (--demo marks every table as synthetic: for testing the layout on fake scores)
"""
from __future__ import annotations
import argparse, importlib.util, json, sys, warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import icst_style as st                      # noqa: E402
from load_scores import load_all              # noqa: E402

warnings.filterwarnings("ignore")
EVAL = Path(__file__).resolve().parents[1]
CORPUS = st.ROOT / "ii-corpus"


def _analysis_module():
    spec = importlib.util.spec_from_file_location("analyze_conditions", EVAL / "7-analyze_conditions.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["analyze_conditions"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── cell formatting ──────────────────────────────────────────────────────────

def heat(values: pd.Series, higher_is_better: bool = True, color: str | None = None) -> list[str]:
    """Cell strings: percentage, shaded by position within the column, best in bold."""
    v = pd.to_numeric(values, errors="coerce")
    color = color or (st.HEAT_GOOD if higher_is_better else st.HEAT_BAD)
    lo, hi = v.min(), v.max()
    best = v.max() if higher_is_better else v.min()
    out = []
    for x in v:
        if pd.isna(x):
            out.append("--")
            continue
        s = f"{100 * x:.1f}"
        if hi > lo:
            t = (x - lo) / (hi - lo)
            if not higher_is_better and color == st.HEAT_GOOD:
                t = 1 - t
            k = round(st.HEAT_MAX * t)
            if k >= 3:
                s = f"\\cellcolor{{{color}!{k}}}" + s
        if np.isclose(x, best) and hi > lo:
            s = s.replace(f"{100 * x:.1f}", f"\\textbf{{{100 * x:.1f}}}")
        out.append(s)
    return out


def diff_cell(row: pd.Series | None, scale: float = 0.25) -> str:
    """Paired difference in pp with its 95% CI, diverging shading, markers."""
    if row is None or pd.isna(row.get("diff")):
        return "--"
    d, lo, hi = 100 * row["diff"], 100 * row["ci_low"], 100 * row["ci_high"]
    sig = bool(row.get("significant"))
    agrees = bool(row.get("cluster_agrees", True))
    txt = f"{d:+.1f}"
    if sig:
        txt = f"\\textbf{{{txt}}}\\sig" + ("" if agrees else "\\clusterdis")
    else:
        txt = f"\\textcolor{{black!55}}{{{txt}}}"
    ci = f"{{\\scriptsize\\textcolor{{black!55}}{{[{lo:+.0f}, {hi:+.0f}]}}}}"
    k = min(st.HEAT_MAX, round(abs(d) / scale)) if sig else 0
    shade = f"\\cellcolor{{{'diffup' if d > 0 else 'diffdown'}!{k}}}" if k >= 3 else ""
    return f"{shade}{txt}\\,{ci}"


def notes(text: str) -> str:
    """Explanatory note set under the tabular (IEEE captions are small caps, so the
    caption keeps only the finding and the details go here, as in the ICLR tables)."""
    return ("\\par\\vspace{3pt}{\\footnotesize\\raggedright " + text + "\\par}")


def demo_note(demo: bool) -> str:
    return "\\textcolor{red}{\\textbf{[SYNTHETIC DATA]}} " if demo else ""


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"  wrote {path}")


# ─── tables ───────────────────────────────────────────────────────────────────

def tab_benchmark(out: Path):
    pool = json.loads((CORPUS / "benchmark_full.json").read_text())["items"]
    ev = json.loads((CORPUS / "benchmark.json").read_text())["items"]
    P, E = pd.DataFrame(pool), pd.DataFrame(ev)
    for df in (P, E):
        df["labeled"] = df["gt_proof_obligation"] != "unknown"
    task_info = {"A": ("\\texttt{requires}", "weakened"), "B": ("\\texttt{invariant}", "weakened"),
                 "C": ("\\texttt{ensures}", "strengthened")}
    op_name = {"cd": "CD", "bs": "BS", "ca": "CA"}
    rows = []
    for task in "ABC":
        ops = sorted(E.loc[E.task == task, "mutation_type"].unique(), key=lambda o: "cdbsca".index(o))
        for i, op in enumerate(ops):
            p = P[(P.task == task) & (P.mutation_type == op)]
            e = E[(E.task == task) & (E.mutation_type == op)]
            el, dirn = task_info[task]
            lead = f"{task} & {el} {dirn}" if i == 0 else " & "
            tiers = " & ".join(str((e.tier == t).sum()) for t in ["low", "medium", "high"])
            rows.append(f"{lead} & {op_name[op]} & {len(p):,} & {len(e)} & {tiers} & "
                        f"{100 * e.labeled.mean():.0f} \\\\")
        if task != "C":
            rows.append("\\addlinespace[2pt]")
    tiers_all = " & ".join(str((E.tier == t).sum()) for t in ["low", "medium", "high"])
    total = (f"\\rowcolor{{totalrow}}\\multicolumn{{3}}{{l}}{{\\textbf{{Total}} "
             f"({P.base_program.nunique()} programs)}} & \\textbf{{{len(P):,}}} & \\textbf{{{len(E)}}} & "
             f"{tiers_all} & {100 * E.labeled.mean():.0f} \\\\")
    nt0 = notes("Each item injects one fault of confirmed direction into one clause. The evaluation set is a stratified sample of the pool (quotas per tier $\\times$ task $\\times$ operator; every program at least once). Obl.: items whose failure has an obligation label; the others are scored on element and repair only. CD: clause deletion; BS: bound shift; CA: clause addition.")
    tex = rf"""\begin{{table}}[t]
\centering
\caption{{Benchmark composition}}
\label{{tab:benchmark}}
\setlength{{\tabcolsep}}{{3pt}}
\footnotesize
\begin{{tabular}}{{llcrrrrrr}}
\toprule
 & & & \multicolumn{{2}}{{c}}{{\textbf{{Items}}}} & \multicolumn{{3}}{{c}}{{\textbf{{Eval.\ by tier}}}} & \\
\cmidrule(lr){{4-5}} \cmidrule(lr){{6-8}}
Task & Faulty clause & Op. & Pool & Eval. & Low & Med. & High & Obl.\,\% \\
\midrule
{chr(10).join(rows)}
\midrule
{total}
\bottomrule
\end{{tabular}}
{nt0}
\end{{table}}
"""
    write(out / "tab_benchmark.tex", tex)


def _subset_items(n: int) -> set:
    order = json.loads((CORPUS / "mode_subset.json").read_text())["order"]
    return set(order[:n])


def tab_modes(r1: pd.DataFrame, A, out: Path, subset: int, diag: str, demo: bool, tdir: Path = None):
    items = _subset_items(subset)
    conds = ["ss_main", "it_main", "ag_main", "ag_main_cc"]
    sub = r1[r1.condition.isin(conds) & r1.item_id.isin(items)]
    if sub.empty:
        print("  tab_modes: no data, skipped")
        return
    m = A.metrics_table(sub)
    if diag == "last":
        it = m["mode"] != "single_shot"
        m.loc[it, "spec_acc"] = m.loc[it, "spec_acc_last"]
        m.loc[it, "obligation_acc"] = m.loc[it, "obligation_acc_last"]
    cols = [("spec_acc", True), ("obligation_acc", True), ("pass_rate", True),
            ("faithful_pass_rate", True), ("weakening_share", False)]
    keys = st.MODELS + ([st.CC_KEY] if (m.condition == "ag_main_cc").any() else [])

    def get(key, cond):
        model = "claude-opus-5-5" if key == st.CC_KEY else key
        c = "ag_main_cc" if key == st.CC_KEY else cond
        r = m[(m.condition == c) & (m.model == model)]
        return r.iloc[0] if len(r) else None

    blocks = {}
    for cond in ["ss_main", "it_main", "ag_main"]:
        for col, good in cols:
            vals = []
            for key in keys:
                if key == st.CC_KEY and cond != "ag_main":
                    vals.append(np.nan)
                    continue
                r = get(key, cond)
                vals.append(r[col] if r is not None else np.nan)
            blocks[(cond, col)] = heat(pd.Series(vals), good)
    lines = []
    for i, key in enumerate(keys):
        cells = [blocks[(c, col)][i] for c in ["ss_main", "it_main", "ag_main"] for col, _ in cols]
        name = st.TEX[key]
        if key == st.CC_KEY:
            lines.append("\\midrule")
        lines.append(f"{name} & " + " & ".join(cells) + " \\\\")
    sub_hdr = " & ".join(["Elem.", "Obl.", "Pass", "Faith.", "Weak."] * 3)
    which = "final" if diag == "last" else "first"
    # majority-class baseline for obligations on this subset (7-analyze_conditions.py)
    base = "\\tbd{78}"
    rec = tdir / "obligation_recall.csv" if tdir else None
    if rec and rec.exists():
        b = pd.read_csv(rec).query("condition == 'it_main'")["majority_baseline"]
        if len(b):
            base = f"{100 * b.iloc[0]:.1f}"
    nt1 = notes(f"Diagnosis and repair (\\%) on the same {subset} items in each mode. "
                f"Elem./Obl.: element and obligation accuracy ({which} diagnosis in iterative and agentic mode); "
                f"obligation accuracy is on labeled items, where always answering ``termination'' scores {base}\\%. "
                "Pass: the repaired program verifies. Faith.: verifies and restores the original clause or an "
                "equivalent one (agents: also a clean pass). Weak.: share of passing repairs that weaken the "
                "specification. Shading is normalized within each column: \\colorbox{heatgood!35}{higher is better}, "
                "\\colorbox{heatbad!35}{higher is worse}; bold marks the column's best.")
    tex = rf"""\begin{{table*}}[t]
\centering
\caption{{{demo_note(demo)}Verifier access raises pass rates; only iteration also raises faithful repairs.}}
\label{{tab:modes}}
\setlength{{\tabcolsep}}{{3.4pt}}
\small
\begin{{tabular}}{{l*{{15}}{{r}}}}
\toprule
 & \multicolumn{{5}}{{c}}{{\textbf{{Single-shot}}}} & \multicolumn{{5}}{{c}}{{\textbf{{Iterative}} ($k \leq 5$)}} & \multicolumn{{5}}{{c}}{{\textbf{{Agentic}}}} \\
\cmidrule(lr){{2-6}} \cmidrule(lr){{7-11}} \cmidrule(lr){{12-16}}
Model & {sub_hdr} \\
\midrule
{chr(10).join(lines)}
\bottomrule
\end{{tabular}}
{nt1}
\end{{table*}}
"""
    write(out / "tab_modes.tex", tex)


def tab_singleshot(r1: pd.DataFrame, A, tests: pd.DataFrame, out: Path, demo: bool):
    ss = r1[r1.condition == "ss_main"]
    if ss.empty:
        print("  tab_singleshot: no data, skipped")
        return
    m = A.metrics_table(ss).set_index("model")
    cols = [("spec_acc", True), ("obligation_acc", True), ("pass_rate", True),
            ("faithful_pass_rate", True), ("weakening_share", False)]
    main_cells = {col: heat(pd.Series([m.loc[k, col] if k in m.index else np.nan for k in st.MODELS]), good)
                  for col, good in cols}
    diffs = [("ss_masked", "obligation_correct"), ("ss_nodafnyref", "spec_correct"),
             ("ss_nodafnyref", "pass"), ("ss_intent", "pass"), ("ss_intent", "faithful_pass")]

    def test_row(model, var, metric):
        t = tests[(tests.model == model) & (tests.variation == var) & (tests.metric == metric)
                  & (tests.reference == "ss_main")]
        return t.iloc[0] if len(t) else None

    lines = []
    for i, key in enumerate(st.MODELS):
        cells = [main_cells[col][i] for col, _ in cols] + [diff_cell(test_row(key, v, mt)) for v, mt in diffs]
        lines.append(f"{st.TEX[key]} & " + " & ".join(cells) + " \\\\")
    nt2 = notes("Left: the single-shot main condition on all 600 items (\\%; shading and bold as in Table~\\ref{tab:modes}). Right: paired change from the main condition in percentage points, with 95\\% CI, on the same items. \\textbf{Bold}\\sig: significant after Holm correction within the research question; \\clusterdis: the program-cluster bootstrap disagrees. Shading (significant changes only): \\colorbox{diffup!35}{increase}, \\colorbox{diffdown!35}{decrease}.")
    tex = rf"""\begin{{table*}}[t]
\centering
\caption{{{demo_note(demo)}No context variation changes an outcome by more than 2 points.}}
\label{{tab:singleshot}}
\setlength{{\tabcolsep}}{{3pt}}
\small
\begin{{tabular}}{{l*{{5}}{{r}}*{{5}}{{c}}}}
\toprule
 & \multicolumn{{5}}{{c}}{{\textbf{{Main condition}} (\%)}} & \multicolumn{{1}}{{c}}{{\textbf{{Masked msg.}}}} & \multicolumn{{2}}{{c}}{{\textbf{{No Dafny reference}}}} & \multicolumn{{2}}{{c}}{{\textbf{{Intent description}}}} \\
\cmidrule(lr){{2-6}} \cmidrule(lr){{7-7}} \cmidrule(lr){{8-9}} \cmidrule(lr){{10-11}}
Model & Elem. & Obl. & Pass & Faith. & Weak. & $\Delta$Obl. & $\Delta$Elem. & $\Delta$Pass & $\Delta$Pass & $\Delta$Faith. \\
\midrule
{chr(10).join(lines)}
\bottomrule
\end{{tabular}}
{nt2}
\end{{table*}}
"""
    write(out / "tab_singleshot.tex", tex)


def tab_agents(tdir: Path, out: Path, demo: bool):
    try:
        beh = pd.read_csv(tdir / "agent_behaviour.csv")
        clean = pd.read_csv(tdir / "agent_clean_pass.csv")
        edits = pd.read_csv(tdir / "agent_edits.csv")
    except FileNotFoundError:
        print("  tab_agents: agent tables missing, skipped")
        return
    edits = edits[edits.runs == "passing"]
    keys = [(k, "ag_main") for k in st.MODELS]
    if (beh.condition == "ag_main_cc").any():
        keys.append((st.CC_KEY, "ag_main_cc"))

    def pick(df, key, cond, col):
        model = "claude-opus-5-5" if key == st.CC_KEY else key
        r = df[(df.condition == cond) & (df.model == model)]
        return r.iloc[0][col] if len(r) and col in r else np.nan

    def num(vals, fmt="{:.1f}"):
        return [fmt.format(v) if pd.notna(v) else "--" for v in vals]

    steps = num([pick(beh, k, c, "steps_mean") for k, c in keys])
    verify = num([pick(beh, k, c, "verify_calls_mean") for k, c in keys])
    before = heat(pd.Series([pick(beh, k, c, "diagnosis_before_edit") for k, c in keys]))
    passr = heat(pd.Series([pick(clean, k, c, "pass_rate") for k, c in keys]))
    cleanr = heat(pd.Series([pick(clean, k, c, "clean_pass_rate") for k, c in keys]))
    outside = pd.Series([1 - pick(edits, k, c, "edit_site") if pd.notna(pick(edits, k, c, "edit_site")) else np.nan
                         for k, c in keys])
    proof = heat(pd.Series([pick(edits, k, c, "edit_proof_added") for k, c in keys]), False)
    contract = heat(pd.Series([pick(edits, k, c, "edit_contract") for k, c in keys]), False)
    code = heat(pd.Series([pick(edits, k, c, "edit_code") for k, c in keys]), False)
    escape = heat(pd.Series([pick(edits, k, c, "edit_escape") for k, c in keys]), False)
    lines = []
    for i, (key, cond) in enumerate(keys):
        name = st.TEX[key]
        if key == st.CC_KEY:
            lines.append("\\midrule")
        lines.append(f"{name} & {steps[i]} & {verify[i]} & {before[i]} & {passr[i]} & {cleanr[i]} & "
                     f"{proof[i]} & {contract[i]} & {code[i]} & {escape[i]} \\\\")
    nt3 = notes("Means per conversation for turns and \\texttt{verify} calls (Claude Code: Dafny runs). Diag.\\ first: \\texttt{diagnosis.json} written before the first edit. Clean: verifies, edits the mutated clause, changes no other contract, signature or code, and uses no escape hatch. Edits outside the clause are shares of passing runs (several can apply); red shading, higher is worse.")
    tex = rf"""\begin{{table*}}[t]
\centering
\caption{{{demo_note(demo)}Agents nearly always repair the faulty clause and rarely change anything else.}}
\label{{tab:agents}}
\setlength{{\tabcolsep}}{{3.6pt}}
\small
\begin{{tabular}}{{lrrrrrrrrr}}
\toprule
 & \multicolumn{{3}}{{c}}{{\textbf{{Process}}}} & \multicolumn{{2}}{{c}}{{\textbf{{Outcome}} (\%)}} & \multicolumn{{4}}{{c}}{{\textbf{{Outside the clause}} (\% passes)}} \\
\cmidrule(lr){{2-4}} \cmidrule(lr){{5-6}} \cmidrule(lr){{7-10}}
Model & Turns & Verify & Diag.\ first & Pass & Clean & Proof & Contract & Code & Escape \\
\midrule
{chr(10).join(lines)}
\bottomrule
\end{{tabular}}
{nt3}
\end{{table*}}
"""
    write(out / "tab_agents.tex", tex)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scores-dir", default=None, help="Default: iii-evaluation/scores")
    ap.add_argument("--tables-dir", default=str(EVAL / "analysis" / "tables" / "conditions"),
                    help="Outputs of 7-analyze_conditions.py")
    ap.add_argument("--out-dir", default=str(st.PAPER / "tables"))
    ap.add_argument("--subset", type=int, default=200, help="Mode-comparison subset size (N)")
    ap.add_argument("--diagnosis", choices=["first", "last"], default="last",
                    help="Which diagnosis to score in iterative/agentic mode")
    ap.add_argument("--demo", action="store_true", help="Mark tables as synthetic")
    args = ap.parse_args()
    out, tdir = Path(args.out_dir), Path(args.tables_dir)

    tab_benchmark(out)
    A = _analysis_module()
    data = load_all(Path(args.scores_dir) if args.scores_dir else None, verbose=False)
    r1 = data.long[data.long["run"].astype(int) == 1]
    tests = pd.read_csv(tdir / "mcnemar.csv") if (tdir / "mcnemar.csv").exists() else pd.DataFrame(
        columns=["model", "variation", "metric", "reference"])
    tab_modes(r1, A, out, args.subset, args.diagnosis, args.demo, tdir)
    tab_singleshot(r1, A, tests, out, args.demo)
    tab_agents(tdir, out, args.demo)


if __name__ == "__main__":
    main()
