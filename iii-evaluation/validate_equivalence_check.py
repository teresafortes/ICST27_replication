#!/usr/bin/env python3
"""
validate_equivalence_check.py  —  Check 4-check_semantic_equivalence.py on known cases.

Every benchmark item gives a clause pair whose relationship we know:
  mutant    R := the item's mutated clause (or `true` for a whole-line deletion).
            Expected: clause deletion -> weaker, clause addition -> stronger,
            bound shift -> weaker in tasks A/B and stronger in task C when the shift
            goes in the stated direction.
  identity  R := (O), the original clause in parentheses. Expected: equivalent.

Prints one table per mode (task × operator × category) and the top reasons for
undetermined results. Results on 2026-10-24 (regenerated benchmark, 1,117 items): mutant
mode 1,117/1,117 in the stated direction (A/B weaker, C stronger; 0 undetermined), as
expected since the generator keeps only direction-confirmed mutants; identity 1,115
equivalent, 0 wrong, 2 undetermined (0.2%). Earlier (2026-10-23, 1,177 items): identity
1,119 equivalent, 0 wrong, 58 undetermined (4.9%).

Usage:
  python3 iii-evaluation/validate_equivalence_check.py [N_PER_CELL] [modes] [out.json]
    N_PER_CELL  sample N items per task×operator (0 = all, default)
    modes       comma-separated: mutant,identity (default both)
"""

import collections, importlib.util, json, random, sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("eq", ROOT / "iii-evaluation" / "4-check_semantic_equivalence.py")
eq = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(ROOT / "iii-evaluation"))
spec.loader.exec_module(eq)


def job(args):
    item, mode = args
    kw = eq.TASK_TO_KEYWORD[item["task"]]
    orig = (eq.CORPUS / item["tier"] / item["task"] / f"{item['base_program']}.dfy").read_text()
    k, mline = eq.locate_original_clause(orig, item["source"], kw)
    if k is None:
        return item["item_id"], mode, "undetermined", None, None, "clause not located"
    lines = orig.splitlines()
    end_k = eq.clause_extent(lines, k)
    cont = "\n".join(lines[k + 1:end_k])
    if mode == "identity":
        R = f"{kw} ({eq.clean_clause(eq.clause_text(lines, k, end_k), kw)})"
    else:
        R = (mline + ("\n" + cont if cont else "")) if mline is not None else f"{kw} true"
    fid, fwd, bwd, reason = eq.classify(orig, k, kw, R, 15)
    return item["item_id"], mode, fid, fwd, bwd, reason


def main():
    items = json.loads((ROOT / "ii-corpus" / "benchmark.json").read_text())["items"]
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if n:
        random.seed(1)
        cells = collections.defaultdict(list)
        for i in items:
            cells[(i["task"], i["mutation_type"])].append(i)
        items = [x for v in cells.values() for x in random.sample(v, min(n, len(v)))]
    modes = sys.argv[2].split(",") if len(sys.argv) > 2 else ["mutant", "identity"]
    with ProcessPoolExecutor(8) as ex:
        res = list(ex.map(job, [(i, m) for i in items for m in modes], chunksize=2))
    meta = {i["item_id"]: (i["task"], i["mutation_type"]) for i in items}
    out = [dict(item_id=r[0], mode=r[1], task=meta[r[0]][0], mut=meta[r[0]][1], fidelity=r[2],
                orig_implies_repair=r[3], repair_implies_orig=r[4], reason=r[5]) for r in res]
    Path(sys.argv[3] if len(sys.argv) > 3 else "validate_equivalence_out.json").write_text(json.dumps(out, indent=1))
    for mode in modes:
        print(f"\n== {mode} ==")
        t = collections.Counter((o["task"], o["mut"], o["fidelity"]) for o in out if o["mode"] == mode)
        print("task mut   " + "  ".join(f"{c[:6]:>6}" for c in eq.CATEGORIES))
        for a, b in sorted({(a, b) for a, b, _ in t}):
            print(f"{a}    {b:4}  " + "  ".join(f"{t[(a, b, c)]:6d}" for c in eq.CATEGORIES))
        reasons = collections.Counter(o["reason"] for o in out if o["mode"] == mode and o["fidelity"] == "undetermined")
        print("undetermined reasons:", reasons.most_common(8))


if __name__ == "__main__":
    main()
