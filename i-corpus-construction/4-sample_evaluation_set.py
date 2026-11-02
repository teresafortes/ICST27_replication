#!/usr/bin/env python3
"""
4-sample_evaluation_set.py  —  Draw the fixed 600-item evaluation set from the full pool.

The full pool (1,117 items, 308 programs) is ii-corpus/benchmark_full.json, written by
2-collect_verifier_output.py. Every condition is run on the evaluation set written to
ii-corpus/benchmark.json, which all evaluation and analysis scripts read. The pool is
released too.

Design (fixed before any model output on the regenerated benchmark existed):
  1. Strata: tier × task × operator (18 cells). Each cell gets a proportional quota of
     the 600 (largest-remainder rounding), so the sample has exactly the pool's mix.
  2. Coverage: every one of the 308 programs contributes at least one item. Programs
     are visited in random order; each takes one item from the cell furthest below its
     quota. The rest of every quota is drawn uniformly at random within the cell.
  3. Balance (rerandomization): 2,000 draws with seeds 2027..4026; the draw kept is
     the one whose distributions of the secondary attributes below are closest to the
     pool's (sum of total-variation distances). Uses only benchmark metadata, never
     model output.
       obligation label (gt_proof_obligation, including "unknown"), clause kind
       (requires / ensures / invariant / other), number of errors (1, 2, 3+),
       has_counterexample, source corpus (MBPP-DFY, Clover, other), and the program's
       share of items (so large programs are not over-represented)

Usage
  python3 i-corpus-construction/4-sample_evaluation_set.py            # writes benchmark.json
  python3 i-corpus-construction/4-sample_evaluation_set.py --report   # balance table only
  python3 i-corpus-construction/4-sample_evaluation_set.py --refresh  # keep the same item ids,
      copy their current fields from the pool (after 2-collect_verifier_output.py --rebuild-prompts)

Re-running gives the same sample. Do not re-run after evaluation runs have started.
"""

import argparse, collections, json, random, re
from pathlib import Path

ROOT   = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "ii-corpus"
POOL   = CORPUS / "benchmark_full.json"
OUT    = CORPUS / "benchmark.json"

N_SAMPLE    = 600
MASTER_SEED = 2027
N_DRAWS     = 2000


def cell(it):
    return (it["tier"], it["task"], it["mutation_type"])


def clause_kind(it):
    m = re.search(r"\b(requires|ensures|invariant|decreases)\b", it["mutation_desc"])
    if m:
        return m.group(1)
    # bound shifts don't name the clause; read it from the mutated line
    line = re.search(r"line (\d+)", it["mutation_desc"])
    if line:
        src = it["source"].splitlines()
        n = int(line.group(1)) - 1
        if 0 <= n < len(src):
            m = re.search(r"\b(requires|ensures|invariant|decreases)\b", src[n])
            if m:
                return m.group(1)
    return "other"


def n_errors(it):
    return min(len(re.findall(r"Error:", it["error_message"])), 3)


def source_corpus(it):
    p = it["base_program"]
    if p.startswith("dafny-synthesis"):
        return "mbpp"
    if p.startswith("Clover"):
        return "clover"
    return "other"


def attributes(items, size):
    return {
        "obligation": [i["gt_proof_obligation"] or "unknown" for i in items],
        "clause":     [clause_kind(i) for i in items],
        "errors":     [n_errors(i) for i in items],
        "cex":        [bool(i["has_counterexample"]) for i in items],
        "corpus":     [source_corpus(i) for i in items],
        "prog_size":  [size[i["base_program"]] for i in items],
    }


def dist(values):
    c = collections.Counter(values)
    n = sum(c.values())
    return {k: v / n for k, v in c.items()}


def tv(p, q):
    return 0.5 * sum(abs(p.get(k, 0) - q.get(k, 0)) for k in set(p) | set(q))


def quotas(items, n):
    counts = collections.Counter(cell(i) for i in items)
    total = sum(counts.values())
    exact = {c: n * k / total for c, k in counts.items()}
    q = {c: int(v) for c, v in exact.items()}
    for c in sorted(exact, key=lambda c: (-(exact[c] - q[c]), c))[: n - sum(q.values())]:
        q[c] += 1
    return q


def draw(items, quota, seed):
    rng = random.Random(seed)
    by_prog = collections.defaultdict(list)
    for i in items:
        by_prog[i["base_program"]].append(i)
    count = collections.Counter()
    chosen = set()
    progs = sorted(by_prog)
    rng.shuffle(progs)
    for p in progs:
        open_ = [i for i in by_prog[p] if count[cell(i)] < quota[cell(i)]]
        if not open_:
            return None                      # a program whose cells are all full: reject
        best = max((quota[cell(i)] - count[cell(i)]) / quota[cell(i)] for i in open_)
        pick = rng.choice([i for i in open_ if (quota[cell(i)] - count[cell(i)]) / quota[cell(i)] == best])
        chosen.add(pick["item_id"])
        count[cell(pick)] += 1
    by_cell = collections.defaultdict(list)
    for i in items:
        if i["item_id"] not in chosen:
            by_cell[cell(i)].append(i)
    for c in sorted(quota):
        rest = sorted(by_cell[c], key=lambda i: i["item_id"])
        chosen.update(i["item_id"] for i in rng.sample(rest, quota[c] - count[c]))
    return chosen


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true", help="print the balance table, write nothing")
    ap.add_argument("--refresh", action="store_true", help="same item ids, fields re-copied from the pool")
    args = ap.parse_args()

    pool = json.loads(POOL.read_text())
    if args.refresh:
        current = json.loads(OUT.read_text())
        by_id = {i["item_id"]: i for i in pool["items"]}
        current["items"] = [by_id[i["item_id"]] for i in current["items"]]
        current["meta"].update({k: v for k, v in pool["meta"].items() if k not in ("total", "ok")})
        OUT.write_text(json.dumps(current, indent=2))
        print(f"refreshed {len(current['items'])} items in {OUT.relative_to(ROOT)}")
        return
    items = pool["items"]
    size = collections.Counter(i["base_program"] for i in items)
    size_of = {p: min(k, 5) for p, k in size.items()}        # 5 = five or six items
    quota = quotas(items, N_SAMPLE)
    target = {k: dist(v) for k, v in attributes(items, size_of).items()}

    best, best_score, rejected = None, None, 0
    for s in range(MASTER_SEED, MASTER_SEED + N_DRAWS):
        chosen = draw(items, quota, s)
        if chosen is None:
            rejected += 1
            continue
        sample = [i for i in items if i["item_id"] in chosen]
        score = sum(tv(dist(v), target[k]) for k, v in attributes(sample, size_of).items())
        if best_score is None or score < best_score:
            best, best_score, best_seed = chosen, score, s
    sample = [i for i in items if i["item_id"] in best]

    print(f"pool {len(items)} items / {len(size)} programs -> sample {len(sample)} items / "
          f"{len({i['base_program'] for i in sample})} programs")
    print(f"draws: {N_DRAWS} ({rejected} rejected), kept seed {best_seed}, imbalance {best_score:.4f}")
    print("\ncell (tier, task, op)        pool  sample  quota")
    pc, sc = collections.Counter(map(cell, items)), collections.Counter(map(cell, sample))
    for c in sorted(pc):
        print(f"  {str(c):26s} {pc[c]:5d} {sc[c]:7d} {quota[c]:6d}")
    sa = attributes(sample, size_of)
    for k, t in target.items():
        d = dist(sa[k])
        print(f"\n{k} (TV {tv(d, t):.3f})")
        for v in sorted(t, key=str):
            print(f"  {str(v):34s} pool {100*t[v]:5.1f}%  sample {100*d.get(v, 0):5.1f}%")

    if args.report:
        return
    meta = dict(pool["meta"])
    meta.update({
        "total": len(sample), "ok": len(sample),
        "evaluation_sample": {
            "pool": "ii-corpus/benchmark_full.json", "pool_items": len(items),
            "pool_programs": len(size), "items": len(sample),
            "programs": len({i["base_program"] for i in sample}),
            "method": "proportional quotas per tier x task x operator; every program at least once; "
                      f"best of {N_DRAWS} draws on secondary-attribute balance",
            "seed": best_seed, "imbalance": round(best_score, 4),
            "script": "i-corpus-construction/4-sample_evaluation_set.py",
        },
    })
    OUT.write_text(json.dumps({"meta": meta, "items": sample, "failed": pool["failed"]}, indent=2))
    print(f"\nwrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
