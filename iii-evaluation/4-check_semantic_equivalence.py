#!/usr/bin/env python3
"""
4-check_semantic_equivalence.py  —  Tier 2: how does a passing repair relate to the
original specification clause?

For each repair that passed Dafny re-verification (Tier 1), compare the repaired clause R
with the original clause O that the mutation changed, and classify it as:

  exact         R is textually identical to O (after normalizing whitespace, `;`, comments)
  equivalent    O <==> R is proven
  weaker        O ==> R is proven, R ==> O is not   (the repair accepts more states)
  stronger      R ==> O is proven, O ==> R is not   (the repair accepts fewer states)
  neither       neither direction is proven          (incomparable, or too hard to prove)
  undetermined  the check could not be built or run  (see `undetermined_reason`)

"Proven" means Dafny verified the implication; "not proven" means verification failed,
which usually means the implication is false but can also be solver incompleteness or a
timeout (timeouts are recorded in `undetermined_reason` even when classified).

How the implications are checked
--------------------------------
The check runs inside the ORIGINAL program, so Dafny sees the real declarations, types,
functions and predicates (the earlier version declared every identifier as `int`, which
failed to type-check for most clauses). Both directions are checked in one Dafny run and
told apart by the line of the failing assertion.

  requires (task A)  Two lemmas are inserted just before the enclosing declaration, with
                     the same type parameters and parameters. Each assumes the
                     declaration's requires clauses in order, with X in O's position, and
                     ensures Y.  O ==> R uses X = O, Y = R; R ==> O uses X = R, Y = O.
  ensures (task C)   Same, with the output parameters (or a function's named result) as
                     extra parameters. Context: the other ensures clauses, plus the
                     requires clauses when the declaration has no `modifies` clause (with a
                     modifies clause the pre-state facts don't describe the post-state).
                     Clauses using old/fresh/unchanged are two-state and left undetermined.
  invariant (task B) Two `if * { ...; assume false; }` blocks are inserted as the first
                     statements of the loop body. Each havocs the loop's frame (`modify ...;`)
                     when the loop can write the heap, havocs the loop's assignment targets
                     (`x := *;`), assumes the loop's invariants in order with X in O's
                     position, and asserts Y. Inside the body every havocked name is already
                     a loop target, so the check doesn't change what any loop modifies. For
                     `for` loops the loop variable can't be havocked, so the check also
                     assumes its in-loop range (including the guard).

Reads:
  ii-corpus/benchmark.json                          (mutated source + metadata)
  ii-corpus/{tier}/{task}/{base_program}.dfy        (original programs)
  iii-evaluation/scores/{condition}/repair_scores.csv   (Tier 1 results)

Writes:
  iii-evaluation/scores/{condition}/fidelity_scores.csv

Run:
  python3 iii-evaluation/4-check_semantic_equivalence.py [--condition ss_main] [--workers N] [--timeout T]
  (--condition takes any single-shot condition name from prompts.SINGLE_SHOT)
"""

from __future__ import annotations
import json, re, subprocess, tempfile, difflib, argparse
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

ROOT       = Path(__file__).parent.parent
CORPUS     = ROOT / "ii-corpus"
BENCHMARK  = CORPUS / "benchmark.json"
SCORES_DIR = Path(__file__).parent / "scores"
DAFNY      = Path.home() / ".dotnet" / "tools" / "dafny"

TASK_TO_KEYWORD = {"A": "requires", "B": "invariant", "C": "ensures"}
CATEGORIES      = ["exact", "equivalent", "weaker", "stronger", "neither", "undetermined"]

CLAUSE_KEYWORDS = ("requires", "ensures", "invariant", "decreases", "modifies", "reads")
CLAUSE_RE = re.compile(r"^\s*(requires|ensures|invariant|decreases|modifies|reads)\b")
DECL_RE   = re.compile(
    r"^\s*(?:(?:ghost|static|abstract|twostate|least|greatest|opaque|\{:[^}]*\})\s+)*"
    r"(method|lemma|function|predicate|constructor|iterator)\b")
TWO_STATE_RE = re.compile(r"\b(old|fresh|unchanged)\s*(@\w+)?\s*\(")
CHECK_NAME   = "EqCheckZZ"


# ─── text helpers ─────────────────────────────────────────────────────────────

def strip_line_comment(line: str) -> str:
    """Drop a trailing `//` comment."""
    i = line.find("//")
    return line if i < 0 else line[:i]


def normalize_spec_line(line: str) -> str:
    """Normalize a spec line for exact comparison: strip comments, semicolons, whitespace."""
    line = strip_line_comment(line.strip()).strip()
    line = line.rstrip(";").strip()
    return re.sub(r"\s+", "", line)


def clean_clause(text: str, keyword: str | None = None) -> str:
    """Return the bare expression of a clause: no keyword, label, attributes, `;` or comments."""
    text = " ".join(strip_line_comment(l).strip() for l in text.splitlines()).strip()
    if keyword is None:
        m = CLAUSE_RE.match(text)
        if m:
            text = text[m.end():]
    elif text.startswith(keyword):
        text = text[len(keyword):]
    text = text.strip()
    while True:  # attributes such as {:induction false} and labels such as L:
        m = re.match(r"^\{:[^}]*\}\s*", text) or re.match(r"^[A-Za-z_]\w*\s*:(?![:=|])\s*", text)
        if not m:
            break
        text = text[m.end():]
    return text.rstrip().rstrip(";").strip()


def equality_sides(expr: str):
    """The two sides of a top-level `A == B` (whitespace and outer parentheses removed), or
    None. `==>`, `<==>`, `!=`, `<=` and `>=` are not equalities."""
    e = re.sub(r"\s+", "", expr)
    while e.startswith("(") and matching_paren(e, 0) == len(e) - 1:
        e = e[1:-1]
    depth, hits = 0, []
    for i, ch in enumerate(e):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif (depth == 0 and e.startswith("==", i) and not e.startswith("==>", i)
              and (i == 0 or e[i - 1] not in "<=!>") ):
            hits.append(i)
    if len(hits) != 1:
        return None
    i = hits[0]
    return e[:i], e[i + 2:]


def swapped_equality(a: str, b: str) -> bool:
    """`A == B` vs. `B == A` (amended 2026-10-27: Dafny could not decide this for multiset
    equalities, e.g. `m == multiset(r)+rest` vs. `multiset(r)+rest == m`)."""
    x, y = equality_sides(a), equality_sides(b)
    return bool(x and y and x == y[::-1])


def split_repair_clauses(text: str):
    """Split a repair that holds several clauses (`invariant A; decreases B;` or one clause
    per line) into [(keyword, clause text)]. Text before the first keyword gets keyword None."""
    flat = " ".join(strip_line_comment(l) for l in str(text).splitlines())
    parts = re.split(r"(?<![\w.])(?=(?:requires|ensures|invariant|decreases|modifies|reads)\b)", flat)
    out = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = CLAUSE_RE.match(part)
        out.append((m.group(1) if m else None, part.rstrip(";").strip()))
    return out


def repair_candidates(text: str, keyword: str):
    """The clause(s) a repair proposes for the mutated position, with `decreases` clauses
    dropped (termination measures are proof annotations; amended 2026-10-27).
    Returns (clauses of `keyword`, reason) where reason is set when another kind of clause
    remains."""
    parts = [(k, c) for k, c in split_repair_clauses(text) if k != "decreases"]
    if any(k is not None and k != keyword for k, _ in parts):
        return None, "repair is a different kind of clause"
    return [c if k else f"{keyword} {c}" for k, c in parts], None


def brace_balance(text: str) -> int:
    return text.count("{") - text.count("}")


def clause_extent(lines: list[str], k: int) -> int:
    """
    Index one past the last line of the clause starting at line k. A clause continues
    onto following lines until the next clause keyword, a line starting with `{`, or a
    line where its own trailing `{` opens a body.
    """
    if strip_line_comment(lines[k]).rstrip().endswith("{") and brace_balance(strip_line_comment(lines[k])) > 0:
        return k + 1
    j = k + 1
    while j < len(lines):
        s = strip_line_comment(lines[j]).strip()
        if not s:
            j += 1
            continue
        if CLAUSE_RE.match(lines[j]) or s.startswith("{") or DECL_RE.match(lines[j]):
            break
        if s.endswith("{") and brace_balance(s) > 0:
            break
        j += 1
    # don't swallow trailing blank lines
    while j > k + 1 and not strip_line_comment(lines[j - 1]).strip():
        j -= 1
    return j


def clause_text(lines: list[str], start: int, end: int) -> str:
    text = "\n".join(lines[start:end])
    s = strip_line_comment(text).rstrip()
    if s.endswith("{") and brace_balance(s) > 0:  # body brace on the clause line
        text = s[:-1]
    return text


def matching_paren(text: str, i: int, open_c: str = "(", close_c: str = ")") -> int:
    """Index of the bracket closing the one at text[i], or -1."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == open_c:
            depth += 1
        elif text[j] == close_c:
            depth -= 1
            if depth == 0:
                return j
    return -1


def split_top_level(text: str, sep: str = ",") -> list[str]:
    parts, depth, cur = [], 0, ""
    for c in text:
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        if c == sep and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def clean_params(text: str) -> list[str]:
    """`ghost x: int := 0, nameonly y: T` → ['x: int', 'y: T']."""
    out = []
    for p in split_top_level(text):
        p = re.sub(r"^(?:(?:ghost|nameonly|older|new)\s+)+", "", p)
        p = re.split(r":=", p)[0].strip()
        if ":" in p:
            out.append(p)
    return out


# ─── locating the mutated clause and its context ──────────────────────────────

def locate_original_clause(original_source: str, mutated_source: str, keyword: str):
    """
    Return (k, mutated_line) where k is the index of the mutated clause's first line in
    the original program, and mutated_line is the line that replaced it (None when the
    mutation deleted the whole line). Returns (None, None) if no changed keyword line.
    """
    orig = original_source.splitlines()
    mut = mutated_source.splitlines()
    sm = difflib.SequenceMatcher(None, orig, mut, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for off, line in enumerate(orig[i1:i2]):
            if re.match(r"^\s*" + keyword + r"\b", line):
                k = i1 + off
                mline = mut[j1 + off] if tag == "replace" and j1 + off < j2 else None
                return k, mline
    return None, None


def find_decl(lines: list[str], k: int):
    """Index of the declaration line enclosing line k (nearest preceding declaration)."""
    for i in range(k, -1, -1):
        if DECL_RE.match(lines[i]):
            return i
    return None


def decl_spec(lines: list[str], d: int):
    """
    Parse the declaration starting at line d. Returns (header_text, clauses) where clauses
    is a list of (keyword, start, end) for the spec clauses before the body.
    """
    i = d + 1
    while i < len(lines) and not CLAUSE_RE.match(lines[i]):
        s = strip_line_comment(lines[i]).strip()
        if s.startswith("{") or (s.endswith("{") and brace_balance(s) > 0):
            break
        if DECL_RE.match(lines[i]):
            break
        i += 1
    header = "\n".join(strip_line_comment(l) for l in lines[d:i])
    clauses = []
    while i < len(lines) and CLAUSE_RE.match(lines[i]):
        kw = CLAUSE_RE.match(lines[i]).group(1)
        end = clause_extent(lines, i)
        clauses.append((kw, i, end))
        s = strip_line_comment(lines[end - 1]).rstrip()
        if s.endswith("{") and brace_balance(s) > 0:
            break
        i = end
        while i < len(lines) and not strip_line_comment(lines[i]).strip():
            i += 1
    return header, clauses


def parse_header(header: str):
    """
    Parse a declaration header. Returns a dict with kind, static, tparams (text or ''),
    params (list of 'x: T'), outs (list of 'r: T') — or None when it can't be parsed.
    """
    text = " ".join(header.split())
    m = DECL_RE.match(text)
    if not m:
        return None
    kind = m.group(1)
    static = bool(re.search(r"\bstatic\b", text[:m.end()]))
    twostate = bool(re.search(r"\btwostate\b", text[:m.end()]))
    rest = text[m.end():].lstrip()
    if kind in ("function", "predicate"):
        rest = re.sub(r"^method\b\s*", "", rest)
    while rest.startswith("{:"):
        rest = rest[rest.index("}") + 1:].lstrip()
    nm = re.match(r"^[A-Za-z_][\w'?]*", rest)
    name = nm.group(0) if nm else ""
    rest = rest[len(name):].lstrip()
    tparams = ""
    if rest.startswith("<"):
        j = matching_paren(rest, 0, "<", ">")
        if j < 0:
            return None
        tparams, rest = rest[: j + 1], rest[j + 1:].lstrip()
    if not rest.startswith("("):
        return None
    j = matching_paren(rest, 0)
    if j < 0:
        return None
    params, rest = clean_params(rest[1:j]), rest[j + 1:].strip()
    outs = []
    rm = re.match(r"^returns\s*\(", rest)
    if rm:
        jj = matching_paren(rest, rm.end() - 1)
        outs = clean_params(rest[rm.end(): jj]) if jj > 0 else []
    elif rest.startswith(":"):
        r2 = rest[1:].lstrip()
        if r2.startswith("("):  # named function result
            jj = matching_paren(r2, 0)
            outs = clean_params(r2[1:jj]) if jj > 0 else []
    return {"kind": kind, "name": name, "static": static, "twostate": twostate,
            "tparams": tparams, "params": params, "outs": outs}


# ─── building the check programs ──────────────────────────────────────────────
#
# Each builder returns (program_text, filter_symbol, roles, reason). `roles` maps a
# 1-based line number of the inserted code to (direction, role):
#   role "x"        the line assuming X (O for fwd, R for bwd)
#   role "y"        the line(s) proving Y
#   role "after"    clauses assumed after X (their well-formedness may depend on X)
#   role "context"  any other inserted line (earlier clauses, havocs, frame)
# so a Dafny error can be attributed (see run_check).

def build_lemma_check(lines, k, end_k, keyword, repair_expr):
    """requires/ensures: insert two lemmas just before the enclosing declaration."""
    d = find_decl(lines, k)
    if d is None:
        return None, None, None, "no enclosing declaration"
    header, clauses = decl_spec(lines, d)
    info = parse_header(header)
    if info is None:
        return None, None, None, "could not parse declaration header"
    if info["kind"] == "iterator":
        return None, None, None, "iterator"
    if info["twostate"]:
        return None, None, None, "two-state declaration"
    if not any(s == k for _, s, _ in clauses):
        return None, None, None, "clause not found in declaration spec"

    O = clean_clause(clause_text(lines, k, end_k), keyword)
    R = repair_expr
    if TWO_STATE_RE.search(O) or TWO_STATE_RE.search(R):
        return None, None, None, "two-state clause (old/fresh/unchanged)"
    has_modifies = any(kw == "modifies" for kw, _, _ in clauses)

    def context(x_expr):
        """Assumed clauses in declaration order, with X in O's position."""
        reqs, role = [], "context"
        for kw, s, e in clauses:
            expr = clean_clause(clause_text(lines, s, e), kw)
            if s == k:
                reqs.append(("x", x_expr))
                role = "after"
            elif kw == "requires" and (keyword == "requires" or not has_modifies):
                reqs.append((role, expr))
            elif kw == "ensures" and keyword == "ensures" and not TWO_STATE_RE.search(expr):
                reqs.append((role, expr))
        return reqs

    params = info["params"] + (info["outs"] if keyword == "ensures" else [])
    indent = re.match(r"^\s*", lines[d]).group(0)
    static = "static " if info["static"] else ""
    out, roles = [], {}
    for direction, x, y in (("fwd", O, R), ("bwd", R, O)):
        def emit(text, role):
            out.append(text)
            roles[d + len(out)] = (direction, role)
        emit(f"{indent}{static}lemma {CHECK_NAME}{direction}{info['tparams']}({', '.join(params)})", "context")
        for role, c in context(x):
            emit(f"{indent}  requires ({c})", role)
        emit(f"{indent}  ensures ({y})", "y")
        emit(f"{indent}{{}}", "y")  # postcondition failures are reported on the closing brace
    program = lines[:d] + out + lines[d:]
    return "\n".join(program) + "\n", CHECK_NAME, roles, None


def loop_targets(body: str):
    """
    Names assigned in a loop body (excluding names declared in it), the objects whose
    elements or fields it writes, whether it allocates, and whether it calls methods.
    """
    targets, declared, heap_bases = set(), set(), set()
    for m in re.finditer(r":=|:\|", body):
        # Dafny doesn't treat an assignment as a loop target when its path leaves the loop
        # (`x := v; break;`): skip assignments whose enclosing block goes on to break/return.
        depth, j = 0, m.end()
        while j < len(body):
            if body[j] == "{":
                depth += 1
            elif body[j] == "}":
                if depth == 0:
                    break
                depth -= 1
            j += 1
        if re.search(r"\b(break|return)\b", body[m.end(): j]):
            continue
        start = max(body.rfind(";", 0, m.start()), body.rfind("{", 0, m.start()),
                    body.rfind("}", 0, m.start()), body.rfind("\n", 0, m.start()))
        lhs = body[start + 1: m.start()].strip()
        lhs = re.split(r"=>|\bthen\b|\belse\b", lhs)[-1].strip()  # `case A => x := ...`
        lhs = re.sub(r"^ghost\s+", "", lhs)
        is_decl = lhs.startswith("var ")
        lhs = re.sub(r"^var\s+", "", lhs)
        for part in split_top_level(lhs):
            part = re.split(r":", part)[0].strip()  # var x: T := ...
            if re.fullmatch(r"[A-Za-z_]\w*", part):
                (declared if is_decl else targets).add(part)
            else:
                base = re.match(r"[A-Za-z_]\w*", part)
                if base:
                    heap_bases.add(base.group(0))
    allocates = bool(re.search(r"\bnew\b", body))
    calls = bool(re.search(r"(^|[;{}\s])[A-Za-z_][\w.]*\s*\([^;]*\)\s*;", body))
    return targets - declared, heap_bases, allocates, calls


def method_locals(text: str) -> dict[str, str | None]:
    """Locals declared with `var` in the given method text → declared or inferred type (or None)."""
    names = {}
    for m in re.finditer(r"\bvar\s+([^;]*?)\s*(:=|:\||;)(\s*[^;]*)", text):
        decl, init = m.group(1), (m.group(3) or "").strip()
        parts = split_top_level(decl)
        for part in parts:
            name, _, typ = part.partition(":")
            name, typ = name.strip(), typ.strip() or None
            if not re.fullmatch(r"[A-Za-z_]\w*", name):
                continue
            if typ is None and len(parts) == 1 and m.group(2) == ":=":
                if re.fullmatch(r"-?\d+", init):
                    typ = "int"
                elif init in ("true", "false"):
                    typ = "bool"
            names[name] = typ
    return names


FOR_RE = re.compile(r"\bfor\s+([A-Za-z_]\w*)\s*(?::\s*([^:=]+?))?\s*:=\s*(.+?)\s+(to|downto)\s+(.+?)\s*\{?\s*$")


def build_loop_check(lines, k, end_k, repair_expr):
    """invariant: insert two `if *` check blocks just before the enclosing loop."""
    # Walk up over the loop's other clauses and the (possibly multi-line) loop condition.
    w, for_m = None, None
    for i in range(k - 1, -1, -1):
        s = strip_line_comment(lines[i])
        if re.search(r"\bwhile\b", s):
            w = i
            break
        if re.search(r"\bfor\b", s) and ":=" in s:
            for_m = FOR_RE.search(s)
            if not for_m:
                return None, None, None, "could not parse for-loop header"
            w = i
            break
        if CLAUSE_RE.match(lines[i]) or not s.strip():
            continue
        if re.search(r"[;{}]\s*$", s):  # a previous statement or block ended: no loop header
            break
    if w is None:
        return None, None, None, "enclosing loop not found"
    d = find_decl(lines, w)
    if d is None:
        return None, None, None, "no enclosing declaration"
    header, method_clauses = decl_spec(lines, d)
    info = parse_header(header)
    if info is None or not info["name"]:
        return None, None, None, "could not parse enclosing declaration"
    if info["kind"] == "iterator":
        return None, None, None, "iterator"

    # the loop's clauses run from the line after the loop header to the body brace
    i = w + 1
    while i < len(lines) and not CLAUSE_RE.match(lines[i]) and not strip_line_comment(lines[i]).strip().startswith("{"):
        i += 1
    loop_clauses = []
    while i < len(lines) and CLAUSE_RE.match(lines[i]):
        end = clause_extent(lines, i)
        loop_clauses.append((CLAUSE_RE.match(lines[i]).group(1), i, end))
        i = end
        while i < len(lines) and not strip_line_comment(lines[i]).strip():
            i += 1
    if not any(s == k for _, s, _ in loop_clauses):
        return None, None, None, "clause not found among loop invariants"

    # loop body: from the first `{` after the clauses to its matching `}`
    rest = "\n".join(strip_line_comment(l) for l in lines[loop_clauses[-1][2] - 1:])
    b0 = rest.find("{")
    b1 = matching_paren(rest, b0, "{", "}") if b0 >= 0 else -1
    if b1 < 0:
        return None, None, None, "loop body not found"
    targets, heap_bases, allocates, calls = loop_targets(rest[b0 + 1: b1])

    O = clean_clause(clause_text(lines, k, end_k), "invariant")
    R = repair_expr
    invs = [(s, clean_clause(clause_text(lines, s, e), kw)) for kw, s, e in loop_clauses if kw == "invariant"]
    idents = set(re.findall(r"\b[A-Za-z_]\w*\b", " ".join([O, R] + [x for _, x in invs])))

    # Only locals and output parameters can be havocked; any other assigned name is a
    # field of `this`, i.e. a heap write.
    locals_ = method_locals("\n".join(lines[d:w]))
    outs = {p.split(":")[0].strip(): p.split(":", 1)[1].strip() for p in info["outs"]}
    in_scope = {**locals_, **outs}
    if targets - set(in_scope) - ({for_m.group(1)} if for_m else set()):
        heap_bases = heap_bases | {"this"}  # assignments to fields of this
    havoc = sorted((targets & set(in_scope) & idents))

    # Heap: havoc the objects the loop writes; with method calls, also the plain names in
    # the loop's (or method's) modifies clause.
    frame = [clean_clause(clause_text(lines, s, e), kw) for kw, s, e in loop_clauses if kw == "modifies"] \
        or [clean_clause(clause_text(lines, s, e), kw) for kw, s, e in method_clauses if kw == "modifies"]
    frame = [f for f in frame if f.strip() not in ("", "{}")]
    allocated = set(re.findall(r"\b([A-Za-z_]\w*)\s*:=\s*new\b", "\n".join(lines[d:w])))
    modify = []
    if heap_bases or allocates or calls:
        modify = frame + sorted((heap_bases & allocated) - set(frame))

    # The check goes first inside the loop body: every name it havocs is already a
    # target of this loop (and of any enclosing loop), so no loop's targets change.
    # A `for` loop's variable can't be assigned; it keeps its in-loop range (incl. guard).
    decls = []
    if modify:  # evaluate the frame before locals are havocked
        decls.append(f"modify {', '.join(modify)};")
    decls += [f"{v} := *;" for v in havoc if not (for_m and v == for_m.group(1))]
    rename = {}

    # line holding the body's `{`, and the insertion point after it
    first = loop_clauses[-1][2] - 1
    body_line = first + rest[:b0].count("\n")
    if rest[:b1].count("\n") == rest[:b0].count("\n"):
        return None, None, None, "one-line loop body"
    after = strip_line_comment(lines[body_line])[strip_line_comment(lines[body_line]).index("{") + 1:].strip() \
        if "{" in strip_line_comment(lines[body_line]) else ""
    if after:
        return None, None, None, "code after the loop body brace on the same line"

    def ren(expr):
        for a, b in rename.items():
            expr = re.sub(rf"\b{a}\b", b, expr)
        return expr

    indent = re.match(r"^\s*", lines[w]).group(0) + "  "
    out, roles = [], {}
    base = body_line + 1  # 0-based index of the first inserted line
    for direction, x, y in (("fwd", O, R), ("bwd", R, O)):
        def emit(text, role):
            out.append(text)
            roles[base + len(out)] = (direction, role)
        emit(f"{indent}if * {{", "context")
        for dl in decls:
            emit(f"{indent}  {dl}", "context")
        role = "context"
        for s, expr in invs:
            if s == k:
                emit(f"{indent}  assume ({ren(x)});", "x")
                role = "after"
            else:
                emit(f"{indent}  assume ({ren(expr)});", role)
        emit(f"{indent}  assert ({ren(y)});", "y")
        emit(f"{indent}  assume false;", "context")
        emit(f"{indent}}}", "context")
    program = lines[:base] + out + lines[base:]
    return "\n".join(program) + "\n", info["name"] + ".", roles, None


# ─── running Dafny ────────────────────────────────────────────────────────────

def run_check(program: str, filter_symbol: str, roles: dict, timeout: int):
    """
    Verify the check program. Returns ({'fwd': ..., 'bwd': ...}, reason) where each value
    is True (proven), False (not proven) or None (undetermined). Error attribution:
      error on a "y" line                      → that direction is not proven
      error on the bwd "x" line (R assumed),   → R doesn't give what O gave (if R ==> O held,
        or a bwd clause after it                 everything well-formed after O would be): not proven
      error on the fwd "x" line, a context     → the check's context is insufficient
        line before X, a fwd clause after X,     (the original verifies): undetermined
        or outside the inserted code
      timeout                                  → undetermined
    """
    with tempfile.NamedTemporaryFile(suffix=".dfy", mode="w", delete=False) as f:
        f.write(program)
        tmp = f.name
    try:
        r = subprocess.run(
            [str(DAFNY), "verify", "--allow-warnings", "--verification-time-limit", str(timeout),
             "--filter-symbol", filter_symbol, tmp],
            capture_output=True, text=True, timeout=timeout * 4 + 60)
        out = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return {"fwd": None, "bwd": None}, "dafny process timed out"
    finally:
        Path(tmp).unlink(missing_ok=True)

    fin = re.search(r"finished with (\d+) verified, (\d+) errors?", out)
    if r.returncode not in (0, 4) or not fin:
        err = next((l.split("Error:", 1)[1].strip() for l in out.splitlines() if "Error:" in l), "no summary")
        return {"fwd": None, "bwd": None}, f"dafny exit {r.returncode}: {err[:100]}"
    if int(fin.group(1)) + int(fin.group(2)) == 0:
        return {"fwd": None, "bwd": None}, "nothing was verified (filter matched no symbol)"

    result, reason = {"fwd": True, "bwd": True}, None
    for l in out.splitlines():
        m = re.search(r"\.dfy\((\d+),\d+\): Error: (.*)", l)
        if not m:
            continue
        line_no, msg = int(m.group(1)), m.group(2)
        if "timed out" in msg:
            dirs = {roles[line_no][0]} if line_no in roles else {"fwd", "bwd"}
            for dd in dirs:
                result[dd] = None
            reason = "verification timed out"
            continue
        if line_no not in roles:
            return {"fwd": None, "bwd": None}, f"error outside the check: {msg[:90]}"
        direction, role = roles[line_no]
        if role == "y" or (direction == "bwd" and role in ("x", "after")):
            # R doesn't give what O gave (incl. well-formedness of the clauses after it)
            if result[direction] is not None:
                result[direction] = False
        else:
            result[direction] = None
            reason = f"check context not well-formed ({role}): {msg[:70]}"
    return result, reason


def classify(original_source: str, k: int, keyword: str, repair_clause: str, timeout: int = 15):
    """
    Compare the clause at line k of the original program with repair_clause (the full
    clause text replacing it). Returns (fidelity, orig_implies_repair, repair_implies_orig, reason).
    """
    lines = original_source.splitlines()
    end_k = clause_extent(lines, k)
    O = clean_clause(clause_text(lines, k, end_k), keyword)
    R = clean_clause(repair_clause, keyword)
    if re.sub(r"\s+", "", O) == re.sub(r"\s+", "", R):
        return "exact", True, True, None
    if swapped_equality(O, R):
        return "equivalent", True, True, None
    if not R:
        return "undetermined", None, None, "empty repair expression"
    if keyword == "invariant":
        program, symbol, roles, reason = build_loop_check(lines, k, end_k, R)
    else:
        program, symbol, roles, reason = build_lemma_check(lines, k, end_k, keyword, R)
    if program is None:
        return "undetermined", None, None, reason
    res, reason = run_check(program, symbol, roles, timeout)
    fwd, bwd = res["fwd"], res["bwd"]
    if fwd is None or bwd is None:
        return "undetermined", fwd, bwd, reason
    category = {(True, True): "equivalent", (True, False): "weaker",
                (False, True): "stronger", (False, False): "neither"}[(fwd, bwd)]
    return category, fwd, bwd, reason



def classify_repair(original_source: str, k: int, keyword: str, clauses: list, timeout: int = 15,
                    continuation: str = ""):
    """classify for a repair made of one or more clauses of `keyword`. Several clauses are
    compared as their conjunction, except loop invariants: invariants added next to the
    restored one are proof annotations, so if any single invariant is exact or equivalent to
    the original, that is the result (amended 2026-10-27)."""
    if len(clauses) > 1 and keyword == "invariant":
        for c in clauses:
            res = classify(original_source, k, keyword, c + continuation, timeout)
            if res[0] in ("exact", "equivalent"):
                return res
    if len(clauses) == 1:
        clause = clauses[0]
    else:
        clause = f"{keyword} " + " && ".join(f"({clean_clause(c, keyword)})" for c in clauses)
    return classify(original_source, k, keyword, clause + continuation, timeout)


# ─── per-row worker ───────────────────────────────────────────────────────────

def process_row(args):
    row, item_lookup, timeout = args
    result = {
        "model":         row["model"],
        "item_id":       row["item_id"],
        "run":           row["run"],
        "tier":          row["tier"],
        "task":          row["task"],
        "mutation_type": row["mutation_type"],
        "pred_repair":   row["pred_repair"],
        "fidelity":      None,   # one of CATEGORIES
        "original_line": None,
        "orig_implies_repair": None,
        "repair_implies_orig": None,
        "undetermined_reason": None,
        "error":         None,
    }
    keyword = TASK_TO_KEYWORD[row["task"]]
    item = item_lookup.get(row["item_id"])
    if item is None:
        result["error"] = "item_not_found"
        return result
    orig_path = CORPUS / item["tier"] / item["task"] / f"{item['base_program']}.dfy"
    if not orig_path.exists():
        result["error"] = "original_not_found"
        return result
    original_source = orig_path.read_text()
    k, _ = locate_original_clause(original_source, item["source"], keyword)
    if k is None:
        result["error"] = "original_line_not_found"
        return result
    lines = original_source.splitlines()
    end_k = clause_extent(lines, k)
    result["original_line"] = lines[k].rstrip()

    repair = str(row["pred_repair"]).strip() if row["pred_repair"] else ""
    if normalize_spec_line(repair) == normalize_spec_line(lines[k]) and end_k == k + 1:
        result.update(fidelity="exact", orig_implies_repair=True, repair_implies_orig=True)
        return result
    clauses, why = repair_candidates(repair, keyword)
    if clauses is None:
        result.update(fidelity="undetermined", undetermined_reason=why)
        return result
    if not clauses:
        result.update(fidelity="undetermined", undetermined_reason="empty repair expression")
        return result
    # the repair replaces the clause's first line; any continuation lines stay in place
    cont = "\n" + "\n".join(lines[k + 1: end_k]) if end_k > k + 1 else ""
    fid, fwd, bwd, reason = classify_repair(original_source, k, keyword, clauses, timeout, cont)
    result.update(fidelity=fid, orig_implies_repair=fwd, repair_implies_orig=bwd, undetermined_reason=reason)
    return result


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    import sys
    import pandas as pd
    sys.path.insert(0, str(Path(__file__).parent))
    import prompts  # condition names (iii-evaluation/prompts.py)

    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--condition", default="ss_main",
                        help="Condition whose repair scores to classify: a single-shot condition "
                             "(see prompts.py) or an iterative output (it_main, it_main_k10) "
                             "written by 1b-run_iterative.py --score (default: ss_main)")
    args = parser.parse_args()

    scores_dir = SCORES_DIR / args.condition
    scores_dir.mkdir(parents=True, exist_ok=True)

    repair_path = scores_dir / "repair_scores.csv"
    if not repair_path.exists():
        raise FileNotFoundError(f"Run 3-apply_repairs.py --condition {args.condition} first.")

    df_repairs = pd.read_csv(repair_path)
    # All runs are classified: run 1 for every item, plus runs 2-3 of the ss_main
    # consistency sample (1-run_evaluation.py --consistency).
    passing = df_repairs[df_repairs["dafny_pass"] == True].copy()
    print(f"[check_equivalence] {len(passing)} Dafny-passing repairs to classify\n")

    benchmark   = json.loads(BENCHMARK.read_text())
    item_lookup = {item["item_id"]: item for item in benchmark["items"]}
    passing = passing[passing["item_id"].isin(item_lookup)]

    jobs = [(row, item_lookup, args.timeout) for row in passing.to_dict("records")]
    results, done = [], 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_row, j): j for j in jobs}
        for fut in as_completed(futs):
            results.append(fut.result())
            done += 1
            if done % 100 == 0 or done == len(jobs):
                print(f"  [{done}/{len(jobs)}]")

    out = pd.DataFrame(results)
    out.to_csv(scores_dir / "fidelity_scores.csv", index=False)

    total = max(len(out), 1)
    print("\n=== Relation of passing repairs to the original clause ===")
    counts = out["fidelity"].value_counts()
    for label in CATEGORIES:
        n = counts.get(label, 0)
        print(f"  {label:12s}: {n:4d}  ({n / total * 100:.1f}%)")
    print("\n=== By model ===")
    print(out.groupby(["model", "fidelity"]).size().unstack(fill_value=0).to_string())
    und = out[out["fidelity"] == "undetermined"]["undetermined_reason"].value_counts().head(8)
    if len(und):
        print("\n=== Top reasons for undetermined ===")
        print(und.to_string())
    print(f"\nOutput → {scores_dir / 'fidelity_scores.csv'}")


if __name__ == "__main__":
    main()
