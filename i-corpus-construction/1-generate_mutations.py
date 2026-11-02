#!/usr/bin/env python3
"""
generate_mutations.py

For each .dfy in ii-corpus/{tier}/{task}/:
  Task A (requires,  CD weakens; BS shifts a bound by ±1):  _cd.dfy and _bs.dfy in negatives/
  Task B (invariant, CD weakens; BS shifts a bound by ±1):  _cd.dfy and _bs.dfy in negatives/
  Task C (ensures,   CA strengthens; BS shifts a bound by ±1): _ca.dfy and _bs.dfy in negatives/

For each program × task × operator, the candidate mutations are shuffled with a seed
derived from (program, task, operator), and the first candidate that passes every check
is kept:
  - the original program verifies (120 s limit);
  - Dafny rejects the mutant with a verification error (exit code 4, as the collector
    runs it, with counterexample extraction) that is not only a timeout;
  - the mutant isn't degenerate (an added conjunct must mention a variable);
  - the in-context equivalence check (iii-evaluation/4-check_semantic_equivalence.py)
    confirms the mutant clause's direction against the original clause: weaker for
    tasks A/B (CD and BS), stronger for task C (CA and BS).
Bound shifts are polarity-aware: a bound inside a quantifier range or an implication's
antecedent is shifted the other way, so the whole clause moves in the stated direction.
Duplicate programs listed in EXCLUDED_PROGRAMS are skipped.
Results (including why a cell produced no mutant) are logged to ii-corpus/mutation_log.json.

Usage:
  python3 generate_mutations.py [--workers N] [--timeout T] [--tier low|medium|high] [--task A|B|C]
"""

import os, re, sys, json, random, hashlib, importlib.util, subprocess, tempfile, argparse, time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spec_polarity import polarity  # noqa: E402

DAFNY   = os.path.expanduser("~/.dotnet/tools/dafny")
DAFNY_EXIT_VERIFICATION_ERROR = 4
CORPUS  = str(Path(__file__).resolve().parent.parent / "ii-corpus")
LOG     = os.path.join(CORPUS, "mutation_log.json")
EQ_PATH = Path(__file__).resolve().parent.parent / "iii-evaluation" / "4-check_semantic_equivalence.py"

# Programs that duplicate another program in the corpus.
EXCLUDED_PROGRAMS = {
    "Dafny_Verify_tmp_tmphq7j0row_Fine_Tune_Examples_error_data_completion_07":
        "duplicate of Dafny_Verify_tmp_tmphq7j0row_dataset_C_convert_examples_07 (identical program)",
    "Dafny_Verify_tmp_tmphq7j0row_Fine_Tune_Examples_error_data_completion_11":
        "duplicate of Dafny_Verify_tmp_tmphq7j0row_dataset_C_convert_examples_11 (identical program)",
    "dafny-synthesis_task_id_618":
        "near-duplicate of dafny-synthesis_task_id_261 (same specification; for loop instead of while)",
}

# Direction each operator must have, by task (mutant clause vs. original clause).
EXPECTED_DIRECTION = {("A", "cd"): "weaker", ("A", "bs"): "weaker",
                      ("B", "cd"): "weaker", ("B", "bs"): "weaker",
                      ("C", "ca"): "stronger", ("C", "bs"): "stronger"}

# ─── helpers ──────────────────────────────────────────────────────────────────

def strip_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'//[^\n]*', '', text)
    return text

def split_conjuncts(expr):
    """
    Split expr on top-level && — not inside parens/brackets AND not inside
    a quantifier body (forall/exists ... :: <body>).
    Strategy: find the position of the first '::' at depth 0; any && after
    that is inside a quantifier body and must not be split.
    """
    # Pass 1: find the position of the first :: at depth 0
    depth = 0
    first_coloncolon = len(expr)
    for i in range(len(expr) - 1):
        if expr[i] in '([{':
            depth += 1
        elif expr[i] in ')]}':
            depth -= 1
        elif expr[i:i+2] == '::' and depth == 0:
            first_coloncolon = i
            break

    # Pass 2: split on && at depth 0, only before first_coloncolon
    parts, depth, start, i = [], 0, 0, 0
    while i < len(expr):
        if expr[i] in '([{':
            depth += 1
        elif expr[i] in ')]}':
            depth -= 1
        elif expr[i:i+2] == '&&' and depth == 0 and i < first_coloncolon:
            parts.append(expr[start:i].strip())
            start = i + 2
            i += 2
            continue
        i += 1
    parts.append(expr[start:].strip())
    return [p for p in parts if p]

def only_timeouts(output):
    """True if every Dafny error line reports a verification timeout."""
    errors = [l for l in output.splitlines() if ": Error:" in l]
    return bool(errors) and all("timed out" in l for l in errors)

def dafny_rejects(source_text, timeout):
    """
    Return True only if Dafny fails with a VERIFICATION error (not a
    parse/type/resolution error).  We want mutations that compile but
    fail the verifier — those produce counterexamples an LLM must reason about.
    Runs Dafny the way the collector does (with counterexample extraction), so a
    kept mutant is never dropped later because extraction crashes.
    """
    with tempfile.NamedTemporaryFile(suffix='.dfy', mode='w', delete=False) as f:
        f.write(source_text)
        tmp = f.name
    try:
        r = subprocess.run(
            [DAFNY, "verify", "--allow-warnings", "--extract-counterexample",
             "--verification-time-limit", "20", tmp],
            capture_output=True, text=True, timeout=timeout
        )
        # Dafny exit codes: 0 = verified, 2 = parse/resolution/type error,
        # 4 = verification error; anything else is a crash or internal error.
        # Matching error text missed type errors (e.g. "arguments to > must be
        # of a numeric type"), so rely on the exit code. A run whose only errors
        # are timeouts is not a confirmed failure, so reject it too.
        if r.returncode != DAFNY_EXIT_VERIFICATION_ERROR:
            return False
        return not only_timeouts(r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return False
    finally:
        os.unlink(tmp)

def dafny_verifies(path, timeout):
    """
    Return True if the original program verifies. A mutant's injected fault is
    only the sole fault when the original verifies, so programs that fail (or
    time out) under the current Dafny version are skipped. Uses a longer limit
    than the mutant check so slow-but-valid proofs are kept.
    """
    try:
        r = subprocess.run(
            [DAFNY, "verify", "--allow-warnings", "--verification-time-limit", "120", path],
            capture_output=True, text=True, timeout=max(timeout, 150)
        )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False

# ─── spec-line extraction ─────────────────────────────────────────────────────

def find_spec_lines(source, keyword):
    """
    Return list of (line_index, indent, content) for lines matching keyword,
    searching in the stripped source but returning indices into original lines.
    """
    results = []
    for i, line in enumerate(source.splitlines()):
        m = re.match(r'^(\s*)' + keyword + r'\s+(.*)', line)
        if m:
            results.append((i, m.group(1), m.group(2).rstrip()))
    return results

def replace_line(source, line_idx, new_line):
    lines = source.splitlines(keepends=True)
    lines[line_idx] = new_line + ('\n' if not new_line.endswith('\n') else '')
    return ''.join(lines)

def remove_line(source, line_idx):
    lines = source.splitlines(keepends=True)
    del lines[line_idx]
    return ''.join(lines)

# ─── CD: clause deletion ──────────────────────────────────────────────────────

def cd_candidates(source, keyword):
    """
    Yield (mutated_source, description) by removing one conjunct from keyword lines.
    Strategy 1: split on && and remove each conjunct.
    Strategy 2: if only one requires/invariant line in a group, try removing the line.
    """
    spec = find_spec_lines(source, keyword)
    if not spec:
        return

    # Strategy 1: remove a conjunct from a line that has &&
    for idx, indent, content in spec:
        parts = split_conjuncts(content)
        if len(parts) > 1:
            for j in range(len(parts)):
                remaining = [p for k, p in enumerate(parts) if k != j]
                new_content = ' && '.join(remaining)
                new_line = f"{indent}{keyword} {new_content}"
                yield replace_line(source, idx, new_line), f"CD: removed conjunct [{j}] from {keyword} line {idx}"
        # Strategy 2: remove whole line (only if multiple spec lines exist)
        elif len(spec) > 1:
            yield remove_line(source, idx), f"CD: removed entire {keyword} line {idx}"

# ─── BS: bound shift ─────────────────────────────────────────────────────────

def bs_shifts(content, weaken):
    """
    Yield (shifted_content, description) by shifting numeric/variable bounds.
    weaken=True  → loosen the shifted bound (used for requires/invariant)
    weaken=False → tighten the shifted bound (used for ensures)

    The direction refers to the whole clause. Each comparison's position has a
    polarity (spec_polarity.polarity): inside a quantifier range or an implication's
    antecedent it is negative, so the comparison is tightened to weaken the clause
    (and loosened to strengthen it). Positions of unclear polarity (call arguments,
    indices, if-conditions, <==>) are skipped. The direction is confirmed afterwards
    by the equivalence check.
    """
    def loosen(pos):
        """True to loosen the comparison at pos, False to tighten it, None if unclear."""
        p = polarity(content, pos)
        if p == 0:
            return None
        return weaken == (p == 1)

    # ── integer literals in comparisons ──────────────────────────────────────
    # For weakening:  >= n → >= n-1 ;  <= n → <= n+1 ;  > n → > n-1 ;  < n → < n+1
    # For tightening: >= n → >= n+1 ;  <= n → <= n-1 ;  > n → > n+1 ;  < n → < n-1
    int_patterns = [
        # (regex, group_with_number, delta_if_weaken)
        (r'(>=\s*)(-?\d+)(?=[^.\w]|$)',    2, -1),
        (r'(>\s*)(-?\d+)(?=[^.\w]|$)',     2, -1),
        (r'(<=\s*)(-?\d+)(?=[^.\w]|$)',    2, +1),
        (r'(<\s*)(-?\d+)(?=[^.\w]|$)',     2, +1),
    ]
    for pat, grp, weak_delta in int_patterns:
        for m in re.finditer(pat, content):
            lo = loosen(m.start(grp))
            if lo is None:
                continue
            delta = weak_delta if lo else -weak_delta
            n = int(m.group(grp))
            new_n = n + delta
            new_content = content[:m.start(grp)] + str(new_n) + content[m.end(grp):]
            direction = ("loosen bound" if lo else "tighten bound") + (", weakens clause" if weaken else ", strengthens clause")
            yield new_content, f"BS({direction}): {m.group(0).strip()} → {m.group(grp-1).strip()}{new_n}"

    # ── variable / expression bounds (add ±1) ─────────────────────────────────
    # Match: <= <expr>  or  < <expr>  or  >= <expr>  or  > <expr>
    # where <expr> does NOT start with a digit (already handled above)
    # and is a plausible Dafny expression (identifiers, dots, |...|, parens)
    var_bound_pat = r'(<=|<|>=|>)\s*([a-zA-Z_|\(][a-zA-Z0-9_.|()\[\]+\-*/\s]*?)(\s*(?:&&|\|\||==>|<==|$|\)))'
    for m in re.finditer(var_bound_pat, content):
        op   = m.group(1)
        expr = m.group(2).strip()
        tail = m.group(3)
        if not expr:
            continue
        lo = loosen(m.start(2))
        if lo is None:
            continue
        # Determine shift direction based on operator and the comparison's direction
        if op in ('<=', '<'):
            delta_str = ' + 1' if lo else ' - 1'
        else:  # >= >
            delta_str = ' - 1' if lo else ' + 1'
        new_expr = f"({expr}){delta_str}"
        new_content = content[:m.start()] + op + ' ' + new_expr + tail + content[m.end():]
        direction = ("loosen bound" if lo else "tighten bound") + (", weakens clause" if weaken else ", strengthens clause")
        yield new_content, f"BS({direction}): {op} {expr} → {op} {new_expr}"

def bs_candidates(source, keyword, weaken):
    """Yield (mutated_source, description) for all BS shifts on keyword lines."""
    seen = set()
    for idx, indent, content in find_spec_lines(source, keyword):
        for new_content, desc in bs_shifts(content, weaken):
            if new_content == content:
                continue
            key = (idx, new_content)
            if key in seen:
                continue
            seen.add(key)
            new_line = f"{indent}{keyword} {new_content}"
            yield replace_line(source, idx, new_line), desc

# ─── CA: clause addition ──────────────────────────────────────────────────────

def ca_candidates(source):
    """
    Yield (mutated_source, description) by adding a strengthening conjunct
    to ensures lines.  Tries several heuristics in order of quality.
    """
    spec = find_spec_lines(source, 'ensures')
    if not spec:
        return

    for idx, indent, content in spec:
        # Heuristic 1: find X.Length == Y or |X| == Y → add && Y > 0
        for m in re.finditer(r'(\w+\.Length|[|]\w+[|])\s*==\s*(\w+)', content):
            rhs = m.group(2)
            addition = f"{rhs} > 0"
            yield (replace_line(source, idx, f"{indent}ensures {content} && {addition}"),
                   f"CA: added '{addition}' to ensures line {idx}")

        # Heuristic 2: find >= 0 or > 0 → add != 0 (or > 0)
        for m in re.finditer(r'(\w[\w.]*)\s*(?:>=|>)\s*0(?=[^.\d]|$)', content):
            var = m.group(1)
            addition = f"{var} != 0"
            yield (replace_line(source, idx, f"{indent}ensures {content} && {addition}"),
                   f"CA: added '{addition}' to ensures line {idx}")

        # Heuristic 3: find forall with >= 0 lower bound → tighten to > 0
        for m in re.finditer(r'(0\s*<=\s*(\w+)\s*<)', content):
            var = m.group(2)
            addition = f"{var} != 0"
            yield (replace_line(source, idx, f"{indent}ensures {content} && {addition}"),
                   f"CA: added '{addition}' (non-zero index) to ensures line {idx}")

        # Heuristic 4: find equality ensures X == expr → add X >= 0 (might not hold if expr < 0)
        for m in re.finditer(r'\b(\w+)\s*==\s*[^=]', content):
            var = m.group(1)
            if var in ('true', 'false', 'null'):
                continue
            addition = f"{var} >= 0"
            yield (replace_line(source, idx, f"{indent}ensures {content} && {addition}"),
                   f"CA: added '{addition}' to ensures line {idx}")

        # Heuristic 5: add a cross-ensures conjunct from another ensures line
        for other_idx, other_indent, other_content in spec:
            if other_idx == idx:
                continue
            # Take a sub-expression from other ensures and add a stricter version
            for m in re.finditer(r'(\w+\.Length|[|]\w+[|])\s*>\s*0', other_content):
                addition = m.group(0).replace('> 0', '> 1')
                yield (replace_line(source, idx, f"{indent}ensures {content} && {addition}"),
                       f"CA: cross-ensures tightening '{addition}' at line {idx}")
                break  # one per other_line is enough

# ─── per-file worker ──────────────────────────────────────────────────────────

TASK_CFG = {
    'A': {'keyword': 'requires', 'cd_fn': cd_candidates, 'bs_weaken': True,  'ca': False},
    'B': {'keyword': 'invariant','cd_fn': cd_candidates, 'bs_weaken': True,  'ca': False},
    'C': {'keyword': 'ensures',  'cd_fn': None,          'bs_weaken': False, 'ca': True},
}

_EQ = None

def equivalence_module():
    """iii-evaluation/4-check_semantic_equivalence.py, loaded once per worker."""
    global _EQ
    if _EQ is None:
        sys.path.insert(0, str(EQ_PATH.parent))
        spec = importlib.util.spec_from_file_location("check_semantic_equivalence", EQ_PATH)
        _EQ = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_EQ)
    return _EQ

def mutant_direction(original, mutated, keyword, timeout=15):
    """
    Classify the mutant clause against the original clause in the original program's
    context, as validate_equivalence_check.py's "mutant" mode does. Returns
    (category, reason); a deleted line is compared as `keyword true`.
    """
    eq = equivalence_module()
    k, mline = eq.locate_original_clause(original, mutated, keyword)
    if k is None:
        return "undetermined", "mutated clause not located"
    lines = original.splitlines()
    end_k = eq.clause_extent(lines, k)
    cont = "\n".join(lines[k + 1:end_k])
    R = (mline + ("\n" + cont if cont else "")) if mline is not None else f"{keyword} true"
    fid, _, _, reason = eq.classify(original, k, keyword, R, timeout)
    return fid, reason

def is_degenerate(desc):
    """An added conjunct that mentions no variable (e.g. `0 > 0`) is degenerate."""
    m = re.search(r"added '([^']*)'|tightening '([^']*)'", desc or "")
    added = m and (m.group(1) or m.group(2))
    return bool(added) and not re.search(r"[A-Za-z_]", added)

def candidate_seed(base, task, op):
    """Stable seed from (program, task, operator); Python's hash() is salted per process."""
    return int(hashlib.sha256(f"{base}|{task}|{op}".encode()).hexdigest()[:16], 16)

def select_mutant(candidates, original, keyword, expected, seed, timeout):
    """
    Shuffle the candidates with the seed and return the first that passes every check,
    as (mutated_source, description, direction, n_candidates, rejections). Rejections
    count why earlier candidates were skipped.
    """
    cands = list(candidates)
    random.Random(seed).shuffle(cands)
    rejections = {}
    for src, desc in cands:
        if is_degenerate(desc):
            rejections["degenerate"] = rejections.get("degenerate", 0) + 1
            continue
        if not dafny_rejects(src, timeout):
            rejections["not rejected by Dafny"] = rejections.get("not rejected by Dafny", 0) + 1
            continue
        direction, _ = mutant_direction(original, src, keyword)
        if direction != expected:
            key = f"direction {direction}, expected {expected}"
            rejections[key] = rejections.get(key, 0) + 1
            continue
        return src, desc, direction, len(cands), rejections
    return None, None, None, len(cands), rejections

def no_mutant_reason(n_candidates, rejections):
    if n_candidates == 0:
        return "no candidate mutation sites"
    return "no candidate passed the checks (" + ", ".join(f"{v} {k}" for k, v in sorted(rejections.items())) + ")"

def process_file(args):
    src_path, tier, task, timeout = args
    cfg     = TASK_CFG[task]
    keyword = cfg['keyword']
    stem    = Path(src_path).stem
    primary = 'cd' if task in ('A', 'B') else 'ca'
    result  = {'file': src_path, 'tier': tier, 'task': task, 'program': stem,
               'cd': None, 'ca': None, 'bs': None,
               'cd_desc': None, 'ca_desc': None, 'bs_desc': None, 'no_mutant': {}}

    neg_dir = os.path.join(os.path.dirname(src_path), 'negatives')
    os.makedirs(neg_dir, exist_ok=True)
    for op in (primary, 'bs'):          # never leave a stale mutant from an earlier run
        stale = os.path.join(neg_dir, f"{stem}_{op}.dfy")
        if os.path.exists(stale):
            os.remove(stale)

    if stem in EXCLUDED_PROGRAMS:
        result['skipped'] = EXCLUDED_PROGRAMS[stem]
        return result
    if not dafny_verifies(src_path, timeout):
        result['skipped'] = 'original program does not verify under Dafny 4.11 (proof error or timeout at 120s)'
        return result

    source = open(src_path).read()
    candidates = {'cd': lambda: cd_candidates(source, keyword),
                  'ca': lambda: ca_candidates(source),
                  'bs': lambda: bs_candidates(source, keyword, cfg['bs_weaken'])}

    for op in (primary, 'bs'):
        mutated, desc, direction, n, rejections = select_mutant(
            candidates[op](), source, keyword, EXPECTED_DIRECTION[(task, op)],
            candidate_seed(stem, task, op), timeout)
        result[f'{op}_candidates'] = n
        result[f'{op}_rejections'] = rejections
        if mutated:
            out = os.path.join(neg_dir, f"{stem}_{op}.dfy")
            open(out, 'w').write(mutated)
            result[op] = out
            result[f'{op}_desc'] = desc
            result[f'{op}_direction'] = direction
        else:
            result['no_mutant'][op] = no_mutant_reason(n, rejections)

    return result

# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--timeout', type=int, default=30)
    parser.add_argument('--tier',   choices=['low','medium','high'], default=None)
    parser.add_argument('--task',   choices=['A','B','C'],           default=None)
    args = parser.parse_args()

    tiers = [args.tier] if args.tier else ['low', 'medium', 'high']
    tasks = [args.task] if args.task else ['A', 'B', 'C']

    # Build work list
    jobs = []
    for tier in tiers:
        for task in tasks:
            task_dir = os.path.join(CORPUS, tier, task)
            for fname in sorted(f for f in os.listdir(task_dir) if f.endswith('.dfy')):
                jobs.append((os.path.join(task_dir, fname), tier, task, args.timeout))

    print(f"[generate_mutations] {len(jobs)} files × up to 2 mutations each")
    print(f"[generate_mutations] workers={args.workers}, timeout={args.timeout}s\n")

    results = []
    done = 0
    t0 = time.time()

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_file, j): j for j in jobs}
        for fut in as_completed(futures):
            try:
                r = fut.result()
            except Exception as e:
                r = {'file': futures[fut][0], 'error': str(e)}
            results.append(r)
            done += 1
            if done % 50 == 0 or done == len(jobs):
                elapsed = time.time() - t0
                cd_ok  = sum(1 for x in results if x.get('cd') or x.get('ca'))
                bs_ok  = sum(1 for x in results if x.get('bs'))
                print(f"  [{done}/{len(jobs)}] elapsed {elapsed/60:.1f}m  "
                      f"CD/CA: {cd_ok}  BS: {bs_ok}")

    # ── summary ───────────────────────────────────────────────────────────────
    print("\n=== SUMMARY ===")
    for tier in tiers:
        for task in tasks:
            subset = [r for r in results if r.get('tier') == tier and r.get('task') == task]
            total  = len(subset)
            primary_key = 'cd' if task in ('A','B') else 'ca'
            primary_ok  = sum(1 for r in subset if r.get(primary_key))
            bs_ok       = sum(1 for r in subset if r.get('bs'))
            print(f"  {tier}/{task}: {total} files  |  "
                  f"{'CD' if task != 'C' else 'CA'}={primary_ok}/{total}  "
                  f"BS={bs_ok}/{total}")

    # paths relative to the repo root, e.g. ii-corpus/low/A/X.dfy
    root = os.path.dirname(CORPUS)
    for r in results:
        for key in ('file', 'cd', 'ca', 'bs'):
            if r.get(key):
                r[key] = os.path.relpath(r[key], root)
    results.sort(key=lambda r: r.get('file', ''))
    with open(LOG, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\nLog saved: {LOG}")


if __name__ == '__main__':
    main()
