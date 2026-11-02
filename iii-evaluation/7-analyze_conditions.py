#!/usr/bin/env python3
"""
7-analyze_conditions.py  —  The statistical analysis of the paper
across every condition and mode.

Missing conditions are skipped.

Data: analysis/load_scores.py (one row per condition × model × item × run).
Comparisons use run 1 of every condition. it_main_k10 (plateau pilot) is only used for
the cumulative pass curve.

Outputs (in --out-dir, default iii-evaluation/analysis/):
  tables/conditions/metrics.{csv,tex}          primary metrics per condition × model
  tables/conditions/sensitivity.csv            faithful pass excluding unresolved fidelity
  tables/conditions/consistency.csv            ss_main agreement across runs (if >1 run)
  tables/conditions/mcnemar.{csv,tex}          paired tests vs. the reference condition,
                                               Holm-adjusted within RQ families
  tables/conditions/model_pairs.csv            exploratory model-vs-model paired tests (post hoc,
                                               amended 2026-10-27), Holm within MODEL_FAMILIES
  tables/conditions/masked_rule.csv            masked-error obligation drop vs. ±5 pp
  tables/conditions/obligation_recall.csv      obligation accuracy vs. the majority-class
                                               baseline, recall per label, macro recall
  tables/conditions/mixed_effects.csv          glmer (lme4) odds ratios, model (b) for faithful pass
  tables/conditions/diag_repair_crosstab.csv   pass rate after a correct vs. wrong diagnosis (replaces model (a))
  tables/conditions/iterative_curve.csv        cumulative pass rate by attempt
  tables/conditions/intent_by_leak.csv         ss_intent vs. ss_main split by leak verdict
  tables/conditions/agent_edit_scope.csv, agent_status.csv, agent_behaviour.csv
  tables/conditions/agent_edits.csv            what agents changed besides the mutated clause
                                               (proof, contract, signature, code, escape), share
                                               of passing and of all runs
  tables/conditions/agent_clean_pass.csv       pass rate vs. clean pass rate per agent condition
  tables/conditions/by_task_operator.csv       exploratory breakdown
  summary_conditions.json                      everything above + headline rules

Statistics:
  - McNemar: exact (binomial) test on discordant pairs; paired difference with an
    Agresti–Min (2005) adjusted-Wald 95% CI.
  - Holm–Bonferroni at α = .05 within each family in FAMILIES.
  - Program-cluster bootstrap (robustness check for the paired tests): items share base
    programs, so whole programs are resampled with replacement (BOOT_B resamples, fixed
    seed) and the paired difference recomputed; percentile 95% CI and a two-sided
    bootstrap p, Holm-adjusted in the same families. `cluster_agrees` says whether the
    bootstrap reaches the same significance verdict as McNemar.
  - Mixed effects: logistic regression with random intercepts per program and per item
    (items nested in programs), fitted with R lme4::glmer (Laplace, bobyqa); Wald 95% CIs
    on odds ratios. If R/lme4 is missing, statsmodels BinomialBayesMixedGLM (variational
    Bayes) is used and flagged.

Usage:
  python3 iii-evaluation/7-analyze_conditions.py [--scores-dir DIR] [--out-dir DIR]
"""

from __future__ import annotations
import argparse, json, re, shutil, subprocess, sys, tempfile, warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import binom

sys.path.insert(0, str(Path(__file__).parent / "analysis"))
from load_scores import load_all, MODELS, MAIN_OF_MODE  # noqa: E402

warnings.filterwarnings("ignore")

ALPHA = 0.05
SESOI = 0.05          # smallest effect of interest: 5 percentage points
BOOT_B = 4000         # program-cluster bootstrap resamples per paired test
BOOT_SEED = 2027
RE = "(1 | program) + (1 | item_id)"   # random intercepts: items nested in base programs
DIAG = ["spec_correct", "obligation_correct"]
DIAG_LAST = ["spec_correct_last", "obligation_correct_last"]
REPAIR = ["pass", "faithful_pass"]

# Each comparison: (family, variation, reference, metrics). Variations change one thing
# relative to their mode's main condition; ag_main is compared with ss_main on the agent
# item subset (the matched pairs). RQ3 uses each mode's final diagnosis (*_last) and
# adds `weaker` (a verifying repair that weakens the spec) for the autonomy rule.
FAMILIES = [
    ("RQ1 diagnosis", "ss_masked",     "ss_main", DIAG),
    ("RQ2 repair",    "ss_masked",     "ss_main", REPAIR),
    ("RQ3 modes",     "it_main",       "ss_main", DIAG_LAST + REPAIR + ["weaker"]),
    ("RQ3 modes",     "ag_main",       "ss_main", DIAG_LAST + REPAIR + ["clean_pass", "weaker"]),
    ("RQ4 context",   "ss_nodafnyref", "ss_main", DIAG + REPAIR),
    ("RQ4 context",   "ss_intent",     "ss_main", DIAG + REPAIR),
]

# Model-vs-model comparisons (exploratory, added 2026-10-27 after the results): each
# family is (family, condition, metrics), tested for every pair of models on the items
# both answered. ag_main pass is 100% for every model, so only faithful pass is tested
# in the mode families.
MODEL_FAMILIES = [
    ("Models single-shot", "ss_main", DIAG + REPAIR),
    ("Models modes",       "it_main", ["faithful_pass"]),
    ("Models modes",       "ag_main", ["faithful_pass"]),
]


# ─── statistics ───────────────────────────────────────────────────────────────

def mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    return 1.0 if n == 0 else float(min(1.0, 2 * binom.cdf(min(b, c), n, 0.5)))


def paired_diff_ci(a: int, b: int, c: int, d: int, z: float = 1.959964):
    """Variation minus reference with an Agresti–Min adjusted-Wald CI.
    a = both correct, b = reference only, c = variation only, d = neither."""
    n = a + b + c + d
    diff = (c - b) / n
    a_, b_, c_, d_ = a + .5, b + .5, c + .5, d + .5
    n_ = n + 2
    p12, p21 = b_ / n_, c_ / n_
    est = p21 - p12
    se = np.sqrt(((p12 + p21) - (p21 - p12) ** 2) / n_)
    return diff, max(-1, est - z * se), min(1, est + z * se)


def holm(pvals: list) -> list:
    """Holm-adjusted p-values (same order as input)."""
    m = len(pvals)
    order = np.argsort(pvals)
    adj, running = [0.0] * m, 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvals[i]))
        adj[i] = running
    return adj


def cluster_bootstrap(delta: np.ndarray, cluster: np.ndarray, b: int = BOOT_B,
                      seed: int = BOOT_SEED) -> tuple:
    """Paired difference (mean of delta = variation − reference per item) with programs
    resampled with replacement: (percentile CI low, high, two-sided p, n programs)."""
    codes, cl = np.unique(cluster, return_inverse=True)
    sums = np.bincount(cl, weights=delta)
    sizes = np.bincount(cl).astype(float)
    k = len(codes)
    if k < 2:
        return np.nan, np.nan, np.nan, k
    idx = np.random.default_rng(seed).integers(0, k, size=(b, k))
    boot = sums[idx].sum(axis=1) / sizes[idx].sum(axis=1)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    p = min(1.0, 2 * min(((boot <= 0).sum() + 1) / (b + 1), ((boot >= 0).sum() + 1) / (b + 1)))
    return float(lo), float(hi), float(p), k


def paired_test(ref: pd.DataFrame, var: pd.DataFrame, metric: str) -> dict | None:
    m = ref[["item_id", "program", metric]].merge(var[["item_id", metric]], on="item_id",
                                                  suffixes=("_ref", "_var")).dropna()
    if not len(m):
        return None
    r, v = m[f"{metric}_ref"].astype(bool), m[f"{metric}_var"].astype(bool)
    a, b, c, d = int((r & v).sum()), int((r & ~v).sum()), int((~r & v).sum()), int((~r & ~v).sum())
    diff, lo, hi = paired_diff_ci(a, b, c, d)
    blo, bhi, bp, k = cluster_bootstrap((v.astype(float) - r.astype(float)).to_numpy(),
                                        m["program"].to_numpy())
    return {"n": len(m), "n_programs": k, "ref_rate": r.mean(), "var_rate": v.mean(), "diff": diff,
            "ci_low": lo, "ci_high": hi, "b_ref_only": b, "c_var_only": c,
            "p": mcnemar_exact(b, c), "boot_ci_low": blo, "boot_ci_high": bhi, "boot_p": bp}


# ─── tables ───────────────────────────────────────────────────────────────────

def metrics_table(r1: pd.DataFrame) -> pd.DataFrame:
    def agg(g):
        npass = g["pass"].sum()
        return pd.Series({
            "n": len(g), "parse_rate": g["parsed"].mean(),
            "spec_acc": g["spec_correct"].mean(),
            "obligation_acc": pd.to_numeric(g["obligation_correct"], errors="coerce").mean(),
            "n_obligation": int(g["obligation_correct"].notna().sum()),
            "spec_acc_last": g["spec_correct_last"].mean(),
            "obligation_acc_last": pd.to_numeric(g["obligation_correct_last"], errors="coerce").mean(),
            "pass_rate": g["pass"].mean(), "faithful_pass_rate": g["faithful_pass"].mean(),
            "weakening_share": g["weaker"].sum() / npass if npass else np.nan,
            "unresolved_share": g["fidelity_unresolved"].sum() / npass if npass else np.nan,
        })
    out = r1.groupby(["condition", "mode", "model"]).apply(agg).reset_index()
    return out.astype({"n": int, "n_obligation": int})


def sensitivity_table(r1: pd.DataFrame) -> pd.DataFrame:
    """Faithful pass rate with items whose passing repair has unresolved fidelity removed."""
    def agg(g):
        k = g[~g["fidelity_unresolved"]]
        return pd.Series({"n_kept": len(k), "n_excluded": len(g) - len(k),
                          "faithful_pass_rate": g["faithful_pass"].mean(),
                          "faithful_pass_rate_excl_unresolved": k["faithful_pass"].mean() if len(k) else np.nan})
    return r1.groupby(["condition", "model"]).apply(agg).reset_index().astype({"n_kept": int, "n_excluded": int})


def obligation_table(r1: pd.DataFrame) -> pd.DataFrame:
    """Obligation accuracy next to the majority-class baseline (always "termination", correct
    whenever termination is among the item's obligations), recall per label (single labels;
    combinations pooled as "multiple") and macro-averaged recall over those groups."""
    d = r1[r1["obligation_correct"].notna()].copy()
    if not len(d):
        return pd.DataFrame()
    d["correct"] = d["obligation_correct"].astype(float)
    d["group"] = d["gt_obligation"].where(~d["gt_obligation"].str.contains(r"\|"), "multiple")
    rows = []
    for (cond, model), g in d.groupby(["condition", "model"]):
        rec = g.groupby("group")["correct"].mean()
        rows.append({"condition": cond, "model": model, "n": len(g),
                     "obligation_acc": g["correct"].mean(),
                     "majority_baseline": g["gt_obligation"].str.contains("termination").mean(),
                     "macro_recall": rec.mean(),
                     **{f"recall_{k}": v for k, v in rec.items()},
                     **{f"n_{k}": int(v) for k, v in g["group"].value_counts().items()}})
    return pd.DataFrame(rows)


def consistency_table(long: pd.DataFrame) -> pd.DataFrame:
    ss = long[long["condition"] == "ss_main"]
    rows = []
    for model, g in ss.groupby("model"):
        runs = g["run"].nunique()
        if runs < 2:
            continue
        for metric in ["spec_correct", "pass"]:
            w = g.pivot_table(index="item_id", columns="run", values=metric, aggfunc="first").dropna()
            w = w.astype(float)
            # Robustness: does run 1 give the same rate as a majority vote over the runs?
            rows.append({"model": model, "metric": metric, "runs": runs, "n_items": len(w),
                         "all_runs_agree": (w.nunique(axis=1) == 1).mean(),
                         "rate_run1": w[1].mean() if 1 in w else float("nan"),
                         "rate_majority": (w.mean(axis=1) > 0.5).mean()})
    return pd.DataFrame(rows)


def mcnemar_tests(r1: pd.DataFrame) -> pd.DataFrame:
    rows, have = [], set(r1["condition"])
    for fam, var, ref, metrics in FAMILIES:
        if var not in have or ref not in have:
            continue
        for model in MODELS:
            R = r1[(r1["condition"] == ref) & (r1["model"] == model)].copy()
            V = r1[(r1["condition"] == var) & (r1["model"] == model)].copy()
            if not len(R) or not len(V):
                continue
            for metric in metrics:
                # diagnosis *_last in the variation is compared with the reference's
                # (only) diagnosis when the reference is single-shot
                ref_metric = metric.replace("_last", "") if ref == "ss_main" else metric
                Rm = pd.DataFrame({"item_id": R["item_id"], "program": R["program"],
                                   metric: R[ref_metric]})
                res = paired_test(Rm, V, metric)
                if res:
                    rows.append({"family": fam, "variation": var, "reference": ref,
                                 "model": model, "metric": metric, **res})
    df = pd.DataFrame(rows)
    if len(df):
        df["p_holm"] = df["boot_p_holm"] = np.nan
        for fam, g in df.groupby("family"):
            df.loc[g.index, "p_holm"] = holm(g["p"].tolist())
            df.loc[g.index, "boot_p_holm"] = holm(g["boot_p"].fillna(1.0).tolist())
        df["significant"] = df["p_holm"] < ALPHA
        df["boot_significant"] = df["boot_p_holm"] < ALPHA
        df["cluster_agrees"] = df["significant"] == df["boot_significant"]
    return df


def model_pairs(r1: pd.DataFrame) -> pd.DataFrame:
    """Paired tests between models within one condition (exploratory). diff = model_a −
    model_b, with model_a the earlier model in MODELS."""
    rows, have = [], set(r1["condition"])
    for fam, cond, metrics in MODEL_FAMILIES:
        if cond not in have:
            continue
        C = r1[r1["condition"] == cond]
        for i, ma in enumerate(MODELS):
            for mb in MODELS[i + 1:]:
                A, B = C[C["model"] == ma], C[C["model"] == mb]
                if not len(A) or not len(B):
                    continue
                for metric in metrics:
                    # paired_test reports var − ref, so model_a is the variation
                    res = paired_test(B, A, metric)
                    if res:
                        res = {("rate_b" if k == "ref_rate" else "rate_a" if k == "var_rate"
                                else "b_only" if k == "b_ref_only" else "a_only" if k == "c_var_only"
                                else k): v for k, v in res.items()}
                        rows.append({"family": fam, "condition": cond, "model_a": ma,
                                     "model_b": mb, "metric": metric, **res})
    df = pd.DataFrame(rows)
    if len(df):
        df["p_holm"] = df["boot_p_holm"] = np.nan
        for fam, g in df.groupby("family"):
            df.loc[g.index, "p_holm"] = holm(g["p"].tolist())
            df.loc[g.index, "boot_p_holm"] = holm(g["boot_p"].fillna(1.0).tolist())
        df["significant"] = df["p_holm"] < ALPHA
        df["boot_significant"] = df["boot_p_holm"] < ALPHA
        df["cluster_agrees"] = df["significant"] == df["boot_significant"]
    return df


def masked_rule(tests: pd.DataFrame) -> pd.DataFrame:
    t = tests[(tests.get("variation") == "ss_masked") & (tests.get("metric") == "obligation_correct")] \
        if len(tests) else pd.DataFrame()
    def verdict(diff, lo, hi):
        if diff <= -SESOI:
            return "drop >= 5 pp: obligation accuracy largely reads the verifier's message"
        if lo >= -SESOI and hi <= SESOI:
            return "CI within ±5 pp: obligation inferred without the wording"
        return "inconclusive"
    rows = []
    for _, r in t.iterrows():
        rows.append({"model": r["model"], "diff": r["diff"], "ci_low": r["ci_low"],
                     "ci_high": r["ci_high"], "p_holm": r["p_holm"],
                     "verdict": verdict(r["diff"], r["ci_low"], r["ci_high"]),
                     "boot_ci_low": r["boot_ci_low"], "boot_ci_high": r["boot_ci_high"],
                     "verdict_cluster": verdict(r["diff"], r["boot_ci_low"], r["boot_ci_high"])})
    return pd.DataFrame(rows)


def iterative_curve(attempts: pd.DataFrame) -> pd.DataFrame:
    if not len(attempts):
        return pd.DataFrame()
    a = attempts[attempts["run"] == 1].copy()
    a["dafny_pass"] = a["dafny_pass"].astype(str) == "True"
    first_pass = a[a["dafny_pass"]].groupby(["condition", "model", "item_id"])["turn"].min()
    convs = a.groupby(["condition", "model"])["item_id"].nunique()
    rows = []
    for (cond, model), n in convs.items():
        k = int(a[(a["condition"] == cond)]["k"].max()) if "k" in a else int(a["turn"].max())
        fp = first_pass.loc[(cond, model)] if (cond, model) in first_pass.index.droplevel(2) else pd.Series(dtype=float)
        for t in range(1, k + 1):
            rows.append({"condition": cond, "model": model, "attempt": t, "n": n,
                         "cumulative_pass": float((fp <= t).sum()) / n})
    return pd.DataFrame(rows)


def intent_by_leak(r1: pd.DataFrame, leak: pd.DataFrame) -> pd.DataFrame:
    if not {"ss_intent", "ss_main"} <= set(r1["condition"]) or not len(leak):
        return pd.DataFrame()
    if leak["judge_verdict"].notna().any():
        groups, source = leak.set_index("item_id")["judge_verdict"], "judge"
    else:
        groups = leak.set_index("item_id")["proxy_leak"].map(
            {True: "proxy_leak", False: "proxy_no_leak", "True": "proxy_leak", "False": "proxy_no_leak"})
        source = "proxy (judge not run yet)"
    rows = []
    for verdict in sorted(groups.dropna().unique()):
        ids = set(groups[groups == verdict].index)
        for model in MODELS:
            R = r1[(r1["condition"] == "ss_main") & (r1["model"] == model) & r1["item_id"].isin(ids)]
            V = r1[(r1["condition"] == "ss_intent") & (r1["model"] == model) & r1["item_id"].isin(ids)]
            for metric in DIAG + REPAIR:
                res = paired_test(R, V, metric) if len(R) and len(V) else None
                if res:
                    rows.append({"verdict_source": source, "verdict": verdict, "model": model,
                                 "metric": metric, **res})
    return pd.DataFrame(rows)


def agent_tables(runs: pd.DataFrame) -> dict:
    if not len(runs):
        return {}
    r = runs[runs["run"] == 1].copy()
    scope = (r.groupby(["condition", "model"])["edit_scope"].value_counts(normalize=True)
             .rename("share").reset_index())
    status = (r.groupby(["condition", "model"])["status"].value_counts(normalize=True)
              .rename("share").reset_index())
    if "resolve_calls" not in r:              # conversations from before `resolve` existed
        r["resolve_calls"] = np.nan
    r["escape_hatch"] = r["suspicious_edits"].fillna("").astype(str).str.len() > 0
    for flag, pat in [("uses_assume", "assume"), ("uses_verify_false", "verify"),
                      ("uses_axiom", "axiom"), ("uses_expect", "expect")]:
        r[flag] = r["suspicious_edits"].fillna("").astype(str).str.contains(pat)
    beh = r.groupby(["condition", "model"]).agg(
        n=("item_id", "size"), verify_calls_mean=("verify_calls", "mean"),
        resolve_calls_mean=("resolve_calls", "mean"),
        steps_mean=("steps", "mean"),
        diagnosis_before_edit=("diagnosis_before_edit", lambda s: (s.astype(str) == "True").mean()),
        escape_hatch=("escape_hatch", "mean"), uses_assume=("uses_assume", "mean"),
        uses_verify_false=("uses_verify_false", "mean"), uses_axiom=("uses_axiom", "mean"),
        uses_expect=("uses_expect", "mean")).reset_index()
    # what agents changed (several flags per run), among passing runs and among all runs
    flag_cols = [c for c in r.columns if c.startswith("edit_") and c not in ("edit_scope", "edit_flags")]
    edits = []
    if flag_cols:
        for subset, rows in (("passing", r[_truthy(r["dafny_pass"])]), ("all", r)):
            g = rows.assign(**{c: _truthy(rows[c]) for c in flag_cols}).groupby(["condition", "model"])
            edits.append(g[flag_cols].mean().assign(n=g.size(), runs=subset).reset_index())
    rates = r.assign(pass_=_truthy(r["dafny_pass"]),
                     clean=_truthy(r["clean_pass"]) if "clean_pass" in r else np.nan).groupby(
        ["condition", "model"]).agg(n=("item_id", "size"), pass_rate=("pass_", "mean"),
                                    clean_pass_rate=("clean", "mean")).reset_index()
    return {"agent_edit_scope": scope, "agent_status": status, "agent_behaviour": beh,
            "agent_edits": pd.concat(edits, ignore_index=True) if edits else pd.DataFrame(),
            "agent_clean_pass": rates}


def _truthy(s: pd.Series) -> pd.Series:
    return s.astype(str).isin(["True", "true", "1", "1.0"])


def by_task_operator(r1: pd.DataFrame) -> pd.DataFrame:
    return (r1.groupby(["condition", "model", "task", "operator"])
            [["spec_correct", "pass", "faithful_pass", "weaker"]].mean().reset_index())


# ─── mixed-effects models ─────────────────────────────────────────────────────

R_SCRIPT = r"""
suppressMessages(library(lme4))
args <- commandArgs(trailingOnly = TRUE)
d <- read.csv(args[1], stringsAsFactors = TRUE)
f <- as.formula(args[2])
for (v in c("model", "task", "operator", "tier", "mode")) if (v %in% names(d)) d[[v]] <- factor(d[[v]])
if ("model" %in% names(d)) d$model <- relevel(d$model, ref = "claude-opus-5-5")
if ("mode" %in% names(d)) d$mode <- relevel(d$mode, ref = "single_shot")
m <- glmer(f, data = d, family = binomial,
           control = glmerControl(optimizer = "bobyqa", optCtrl = list(maxfun = 2e5)))
s <- summary(m)$coefficients
conv <- if (length(m@optinfo$conv$lme4$messages)) paste(m@optinfo$conv$lme4$messages, collapse = "; ") else ""
out <- data.frame(term = rownames(s), estimate = s[, 1], se = s[, 2], p = s[, 4],
                  item_sd = sqrt(as.numeric(VarCorr(m)$item_id)),
                  program_sd = if ("program" %in% names(VarCorr(m))) sqrt(as.numeric(VarCorr(m)$program)) else NA,
                  n = nrow(d), n_items = length(unique(d$item_id)),
                  n_programs = if ("program" %in% names(d)) length(unique(d$program)) else NA,
                  warning = conv)
write.csv(out, args[3], row.names = FALSE)
"""


def lme4_available() -> bool:
    if not shutil.which("Rscript"):
        return False
    r = subprocess.run(["Rscript", "-e", "suppressMessages(library(lme4))"], capture_output=True)
    return r.returncode == 0


def fit_glmer(df: pd.DataFrame, formula: str, engine: str) -> pd.DataFrame:
    with tempfile.TemporaryDirectory() as tmp:
        data, script, out = Path(tmp, "d.csv"), Path(tmp, "m.R"), Path(tmp, "o.csv")
        d = df.copy()
        for c in d.columns:
            if d[c].dtype == bool or d[c].dtype == object and set(d[c].dropna().unique()) <= {True, False}:
                d[c] = d[c].astype(float)
        d.to_csv(data, index=False)
        if engine == "lme4":
            script.write_text(R_SCRIPT)
            r = subprocess.run(["Rscript", str(script), str(data), formula, str(out)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                return pd.DataFrame([{"term": "FIT FAILED", "warning": r.stderr.strip()[-300:]}])
            res = pd.read_csv(out)
        else:
            from statsmodels.genmod.bayes_mixed_glm import BinomialBayesMixedGLM
            # `pass` is a Python keyword, which patsy can't evaluate
            fixed = formula.split("+ (1 |")[0].strip()
            fixed = re.sub(r"\bpass\b", "pass_", fixed)
            d = d.rename(columns={"pass": "pass_"})
            vc = {"item": "0 + C(item_id)"}
            if "(1 | program)" in formula:
                vc = {"program": "0 + C(program)", **vc}
            m = BinomialBayesMixedGLM.from_formula(fixed, vc, d).fit_vb()
            sd = dict(zip(m.model.vcp_names, np.exp(m.vcp_mean)))
            res = pd.DataFrame({"term": m.model.exog_names, "estimate": m.fe_mean, "se": m.fe_sd,
                                "p": np.nan, "item_sd": sd.get("item", np.nan),
                                "program_sd": sd.get("program", np.nan),
                                "n": len(d), "n_items": d["item_id"].nunique(),
                                "n_programs": d["program"].nunique() if "program" in d else np.nan,
                                "warning": "variational Bayes (lme4 unavailable)"})
    res["odds_ratio"] = np.exp(res["estimate"])
    res["or_ci_low"] = np.exp(res["estimate"] - 1.959964 * res["se"])
    res["or_ci_high"] = np.exp(res["estimate"] + 1.959964 * res["se"])
    return res


def mixed_models(r1: pd.DataFrame, engine: str) -> pd.DataFrame:
    """(b) faithful_pass ~ mode × model + task + operator + (1 | program) + (1 | item).

    Amended 2026-10-26: models (a), (c) and
    (b) for pass are not estimable on these data (every agent run passes, iterative passes
    96-100%, and in single-shot a wrong element almost always fails), so (a) is replaced by
    diag_repair_crosstab() and (c) and (b)-pass are dropped."""
    out = []
    mains = r1[r1["condition"].isin(MAIN_OF_MODE.values())]
    if mains["mode"].nunique() >= 2:
        f = f"faithful_pass ~ mode * model + task + operator + {RE}"
        res = fit_glmer(mains[["faithful_pass", "mode", "model", "task", "operator", "program", "item_id"]],
                        f, engine)
        res.insert(0, "model_spec", f"(b) {f}")
        out.append(res)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def wilson(k: int, n: int, z: float = 1.959964):
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (mid - half, mid + half)


def diag_repair_crosstab(r1: pd.DataFrame) -> pd.DataFrame:
    """Replaces model (a): pass rate when the element is diagnosed correctly vs. wrongly, per
    mode's main condition and model (final diagnosis in iterative and agentic mode), with
    Wilson CIs and a two-sided Fisher exact test. Descriptive: items share programs."""
    from scipy.stats import fisher_exact
    rows = []
    for mode, cond in MAIN_OF_MODE.items():
        d = r1[r1["condition"] == cond]
        col = "spec_correct" if mode == "single_shot" else "spec_correct_last"
        if not len(d) or col not in d:
            continue
        for model in MODELS:
            g = d[d["model"] == model]
            if not len(g):
                continue
            ok, ps = _truthy(g[col]), _truthy(g["pass"])
            n_ok, n_bad = int(ok.sum()), int((~ok).sum())
            k_ok, k_bad = int(ps[ok].sum()), int(ps[~ok].sum())
            p = fisher_exact([[k_ok, n_ok - k_ok], [k_bad, n_bad - k_bad]])[1] if n_ok and n_bad else np.nan
            lo_ok, hi_ok = wilson(k_ok, n_ok)
            lo_bad, hi_bad = wilson(k_bad, n_bad)
            rows.append({"mode": mode, "condition": cond, "model": model,
                         "n_correct": n_ok, "pass_if_correct": k_ok / n_ok if n_ok else np.nan,
                         "ci_low_correct": lo_ok, "ci_high_correct": hi_ok,
                         "n_wrong": n_bad, "pass_if_wrong": k_bad / n_bad if n_bad else np.nan,
                         "ci_low_wrong": lo_bad, "ci_high_wrong": hi_bad, "fisher_p": p})
    return pd.DataFrame(rows)


# ─── headline rules (§7 "What would change the headline") ─────────────────────

def headline_rules(metrics: pd.DataFrame, tests: pd.DataFrame, masked: pd.DataFrame,
                   crosstab: pd.DataFrame) -> list:
    rules = []
    mains = metrics[metrics["condition"].isin(MAIN_OF_MODE.values())]
    if len(mains):
        low = (mains["weakening_share"].fillna(0) < SESOI).all()
        rules.append({"rule": "weakening share < 5% for every model in every mode",
                      "evaluated_on": sorted(mains["condition"].unique()), "triggered": bool(low),
                      "consequence": "lead with diagnosis (RQ1); report fidelity as 'verifying repairs "
                                     "mostly restore the original'" if low else "weakening can stay the headline"})
    if len(tests):
        w = tests[(tests["family"] == "RQ3 modes") & (tests["metric"] == "weaker")]
        if len(w):
            up = w[(w["significant"]) & (w["diff"] > 0)]
            rules.append({"rule": "weakening significantly higher in iterative/agentic than single-shot",
                          "evaluated_on": sorted(w["variation"].unique()), "triggered": bool(len(up)),
                          "where": up[["variation", "model"]].to_dict("records"),
                          "consequence": "headline becomes 'autonomy raises pass rates at the cost of "
                                         "fidelity'" if len(up) else "no autonomy-fidelity headline"})
    if len(masked):
        rules.append({"rule": "masked-error obligation drop vs. ±5 pp",
                      "evaluated_on": ["ss_masked"],
                      "triggered": bool(masked["verdict"].str.startswith("drop").any()),
                      "per_model": masked[["model", "verdict"]].to_dict("records")})
    if len(crosstab):
        s = crosstab[crosstab["condition"] == "ss_main"]
        holds = s[(s["pass_if_correct"] > s["pass_if_wrong"]) & (s["fisher_p"] < 0.05)]
        none = len(holds) == 0
        rules.append({"rule": "single-shot pass rate not higher after a correct diagnosis (Fisher p < .05 "
                              "for no model; replaces the model (a) odds-ratio rule, amended 2026-10-26)",
                      "evaluated_on": ["ss_main"], "triggered": none,
                      "holds_for": holds["model"].tolist(),
                      "consequence": "drop the claim that correct diagnosis goes with successful repair"
                                     if none else "keep the diagnosis–repair association"})
    return rules


# ─── main ─────────────────────────────────────────────────────────────────────

def write(df: pd.DataFrame, path: Path, tex: bool = False):
    if df is None or not len(df):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path.with_suffix(".csv"), index=False)
    if tex:
        path.with_suffix(".tex").write_text(df.to_latex(index=False, float_format="%.3f", escape=True))
    print(f"  wrote {path.with_suffix('.csv').name}" + (" (+ .tex)" if tex else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scores-dir", type=Path, default=None, help="default: iii-evaluation/scores")
    ap.add_argument("--out-dir", type=Path, default=Path(__file__).parent / "analysis")
    ap.add_argument("--engine", choices=["auto", "lme4", "statsmodels"], default="auto")
    args = ap.parse_args()

    data = load_all(args.scores_dir)
    long = data.long
    if not len(long):
        print("No scores found; nothing to analyze.")
        return
    r1 = long[long["run"] == 1]
    r1_cmp = r1[r1["condition"] != "it_main_k10"]
    tdir = args.out_dir / "tables" / "conditions"

    metrics = metrics_table(r1)
    tests = mcnemar_tests(r1_cmp)
    masked = masked_rule(tests)
    pairs = model_pairs(r1_cmp)
    engine = args.engine if args.engine != "auto" else ("lme4" if lme4_available() else "statsmodels")
    print(f"[mixed effects] engine: {engine}")
    mixed = mixed_models(r1_cmp, engine)
    crosstab = diag_repair_crosstab(r1)
    curve = iterative_curve(data.attempts)
    intent = intent_by_leak(r1_cmp, data.leak)
    agent = agent_tables(data.agent_runs)

    print("\nOutputs:")
    write(metrics, tdir / "metrics", tex=True)
    write(sensitivity_table(r1), tdir / "sensitivity")
    write(consistency_table(long), tdir / "consistency")
    write(tests, tdir / "mcnemar", tex=True)
    write(pairs, tdir / "model_pairs")
    write(masked, tdir / "masked_rule")
    write(obligation_table(r1), tdir / "obligation_recall")
    write(mixed, tdir / "mixed_effects")
    write(crosstab, tdir / "diag_repair_crosstab")
    write(curve, tdir / "iterative_curve")
    write(intent, tdir / "intent_by_leak")
    for name, df in agent.items():
        write(df, tdir / name)
    write(by_task_operator(r1_cmp), tdir / "by_task_operator")

    rules = headline_rules(metrics, tests, masked, crosstab)
    summary = {"conditions": data.conditions(), "notes": data.notes, "mixed_effects_engine": engine,
               "families": [{"family": f, "variation": v, "reference": r, "metrics": m}
                            for f, v, r, m in FAMILIES],
               "metrics": metrics.to_dict("records"),
               "tests": tests.to_dict("records") if len(tests) else [],
               "model_pairs": pairs.to_dict("records") if len(pairs) else [],
               "headline_rules": rules}
    (args.out_dir / "summary_conditions.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"  wrote {args.out_dir / 'summary_conditions.json'}")

    pd.set_option("display.width", 200)
    print("\nPrimary metrics (run 1):")
    print(metrics[["condition", "model", "n", "spec_acc", "obligation_acc", "pass_rate",
                   "faithful_pass_rate", "weakening_share"]].round(3).to_string(index=False))
    if len(tests):
        print("\nPaired tests (Holm within family):")
        print(tests[["family", "variation", "model", "metric", "n", "diff", "ci_low", "ci_high",
                     "p_holm", "significant", "boot_ci_low", "boot_ci_high", "boot_p_holm",
                     "cluster_agrees"]].round(3).to_string(index=False))
        dis = tests[~tests["cluster_agrees"]]
        if len(dis):
            print(f"\nProgram-cluster bootstrap disagrees with McNemar on {len(dis)} test(s); "
                  "report those as not robust to program clustering.")
    if len(pairs):
        print("\nModel-vs-model paired tests (exploratory, Holm within family):")
        print(pairs[["family", "condition", "model_a", "model_b", "metric", "n", "rate_a", "rate_b",
                     "diff", "ci_low", "ci_high", "p_holm", "significant", "boot_p_holm",
                     "cluster_agrees"]].round(3).to_string(index=False))
    print("\nHeadline rules:")
    for r in rules:
        print(f"  - {r['rule']}: {'TRIGGERED' if r['triggered'] else 'not triggered'}"
              + (f" → {r['consequence']}" if r.get("consequence") else "")
              + (f" {r['per_model']}" if r.get("per_model") else ""))
    if not rules:
        print("  (none evaluable: needs ss_main plus the relevant conditions)")


if __name__ == "__main__":
    main()
