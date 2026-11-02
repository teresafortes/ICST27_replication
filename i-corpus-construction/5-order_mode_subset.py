#!/usr/bin/env python3
"""
5-order_mode_subset.py  —  Order the evaluation set for the iterative and agentic runs.

it_main and ag_main run on the same subset of the 600-item evaluation set, of size
N = 200, 150 or 100, (200 in the paper). The subset is the first N items of one fixed order, so the
three possible subsets are nested and the order can be fixed before N is known.

Order: stratified by tier × task × operator. Within each cell the items are shuffled
and the j-th of n gets the key (j + u) / n with u uniform in [0, 1); items are sorted by
key, so any prefix holds close to each cell's share of the 600. Of 2,000 such orders
(seeds 2027..4026) the one kept has the best balance of the 600-set sampler's secondary
attributes (obligation label, clause kind, errors, counterexample, corpus, program size)
summed over the three prefixes N = 200, 150, 100. Uses only benchmark metadata, never
model output.

Output: ii-corpus/mode_subset.json  {"meta": ..., "order": [item_id, ...]}  (all 600)

Usage
  python3 i-corpus-construction/5-order_mode_subset.py            # write
  python3 i-corpus-construction/5-order_mode_subset.py --report   # balance only
Do not re-run after iterative or agentic runs have started.
"""

import argparse, collections, importlib.util, json, random
from pathlib import Path

ROOT   = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "ii-corpus"
OUT    = CORPUS / "mode_subset.json"
SEED   = 2027
N_DRAWS = 2000
SIZES  = (200, 150, 100)

_spec = importlib.util.spec_from_file_location("sampler", Path(__file__).parent / "4-sample_evaluation_set.py")
sampler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sampler)


def order(items, seed):
    rng = random.Random(seed)
    cells = collections.defaultdict(list)
    for i in sorted(items, key=lambda i: i["item_id"]):
        cells[sampler.cell(i)].append(i)
    keyed = []
    for c in sorted(cells):
        group = cells[c]
        rng.shuffle(group)
        keyed += [((j + rng.random()) / len(group), i["item_id"]) for j, i in enumerate(group)]
    return [iid for _, iid in sorted(keyed)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    items = json.loads((CORPUS / "benchmark.json").read_text())["items"]
    by_id = {i["item_id"]: i for i in items}
    size = collections.Counter(i["base_program"] for i in items)
    size_of = {p: min(k, 5) for p, k in size.items()}
    target = {k: sampler.dist(v) for k, v in sampler.attributes(items, size_of).items()}
    cell_target = sampler.dist([sampler.cell(i) for i in items])

    def imbalance(ids):
        return sum(sampler.tv(sampler.dist(v), target[k])
                   for n in SIZES
                   for k, v in sampler.attributes([by_id[i] for i in ids[:n]], size_of).items())

    best = min(range(SEED, SEED + N_DRAWS), key=lambda s: imbalance(order(items, s)))
    ids = order(items, best)
    print(f"kept seed {best} of {N_DRAWS} (imbalance {imbalance(ids):.3f})")
    for n in SIZES:
        sub = [by_id[i] for i in ids[:n]]
        tvs = {k: round(sampler.tv(sampler.dist(v), target[k]), 3)
               for k, v in sampler.attributes(sub, size_of).items()}
        tvs["cell"] = round(sampler.tv(sampler.dist([sampler.cell(i) for i in sub]), cell_target), 3)
        tasks = collections.Counter(i["task"] for i in sub)
        print(f"N={n}: tasks {dict(sorted(tasks.items()))}, programs {len({i['base_program'] for i in sub})}, TV {tvs}")
    if args.report:
        return
    OUT.write_text(json.dumps({"meta": {
        "purpose": "it_main and ag_main run on the first N items (N = 200)",
        "method": "stratified order by tier x task x operator (within-cell shuffle, jittered rank keys); best of 2,000 orders on secondary-attribute balance over N = 200, 150, 100",
        "seed": best, "draws": N_DRAWS, "items": len(ids), "script": "i-corpus-construction/5-order_mode_subset.py"},
        "order": ids}, indent=2))
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
