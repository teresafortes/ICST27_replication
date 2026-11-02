"""Review sheet for agent runs that pass but are not faithful (ag_main, run 1).

For each run: mutation, fidelity class, edit flags, final diagnosis, and two diffs
(original → final program, mutant → final program). Writes nonfaithful_runs.md and
nonfaithful_runs.csv (one row per run, with empty coding columns) next to this script.
"""
import difflib, json, sys
from pathlib import Path
import pandas as pd

HERE = Path(__file__).resolve().parent
EVAL = HERE.parents[1]
ROOT = EVAL.parent
sys.path.insert(0, str(EVAL / "analysis"))
from load_scores import load_all  # noqa: E402

items = {it["item_id"]: it for it in json.loads((ROOT / "ii-corpus/benchmark.json").read_text())["items"]}
runs = pd.read_csv(EVAL / "scores/ag_main/agent_runs.csv")
runs = runs[runs["run"] == 1].set_index(["model", "item_id"])
fid = pd.read_csv(EVAL / "scores/ag_main/fidelity_scores.csv")
fid = fid[fid["run"] == 1].set_index(["model", "item_id"])

long = load_all(None).long
a = long[(long["condition"] == "ag_main") & (long["run"] == 1)]
nf = a[a["pass"].astype(bool) & ~a["faithful_pass"].astype(bool)].sort_values(["model", "item_id"])


def diff(x, y, a_name, b_name):
    return "".join(difflib.unified_diff(x.splitlines(True), y.splitlines(True), a_name, b_name, n=1))


md, rows = ["# Agent runs that pass but are not faithful (ag_main, run 1)\n"], []
for i, r in enumerate(nf.itertuples(), 1):
    it = items[r.item_id]
    orig = (ROOT / "ii-corpus" / r.tier / r.task / f"{it['base_program']}.dfy").read_text()
    resp = json.loads((EVAL / "responses/ag_main" / r.model /
                       f"{r.item_id.replace('/', '_')}_run1.json").read_text())
    final = resp["final_program"]
    ru, fu = runs.loc[(r.model, r.item_id)], fid.loc[(r.model, r.item_id)]
    flags = ru["edit_flags"] if isinstance(ru["edit_flags"], str) else ""
    try:
        dj = json.loads(resp.get("final_diagnosis_json") or "{}")
    except json.JSONDecodeError:
        dj = {}
    rid = f"R{i:02d}"
    rows.append({"id": rid, "model": r.model, "item_id": r.item_id, "task": r.task,
                 "operator": r.operator, "mutation": it["mutation_desc"],
                 "fidelity": r.fidelity, "clean_pass": r.clean_pass, "edit_flags": flags,
                 "repair_clause": fu["pred_repair"], "diag_element": dj.get("spec_element"),
                 "steps": ru["steps"], "verify_calls": ru["verify_calls"],
                 "category": "", "note": ""})
    md += [f"\n## {rid} · {r.model} · `{r.item_id}`\n",
           f"- mutation: {it['mutation_desc']} ({it['mutation_direction']})",
           f"- fidelity: **{r.fidelity}** · clean pass: {r.clean_pass} · flags: {flags or '—'}",
           f"- repair clause scored: `{fu['pred_repair']}`",
           f"- diagnosis ({dj.get('spec_element')}, {dj.get('proof_obligation')}): {dj.get('diagnosis', '')}",
           "\nOriginal → final:\n```diff", diff(orig, final, "original", "final").rstrip(), "```",
           "\nMutant → final:\n```diff", diff(it["source"], final, "mutant", "final").rstrip(), "```"]

(HERE / "nonfaithful_runs.md").write_text("\n".join(md) + "\n")
pd.DataFrame(rows).to_csv(HERE / "nonfaithful_runs.csv", index=False)
print(len(rows), "runs →", HERE / "nonfaithful_runs.md")
