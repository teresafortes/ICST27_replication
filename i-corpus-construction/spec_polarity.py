"""
spec_polarity.py  —  Polarity of a position inside a Dafny boolean expression.

Shared by 1-generate_mutations.py (to shift each bound in the direction that weakens
or strengthens its whole clause) and audit_benchmark.py (to check it).
"""

import re


def polarity(expr: str, pos: int) -> int:
    """
    Polarity of character position pos inside a boolean expression:
    +1 if making the sub-formula there weaker makes the whole expression weaker,
    -1 if it reverses (implication antecedent, `forall x | range`, negation),
     0 if unclear (inside an index, call argument, if-condition or <==>).
    A syntactic approximation.
    """
    frames = [{"sign": 1, "range": False, "unclear": False, "lhs_rev": False}]
    k = 0
    while k < pos:
        f = frames[-1]
        if expr.startswith("<==>", k):
            f["unclear"] = True; k += 4; continue
        if expr.startswith("==>", k):
            k += 3; continue
        if expr.startswith("<==", k):
            f["lhs_rev"] = True; k += 3; continue
        if expr.startswith("::", k):
            f["range"] = False; k += 2; continue
        c = expr[k]
        if c in "([":
            call_or_index = c == "[" or (k > 0 and re.match(r"[\w)]", expr[k - 1]) is not None)
            negated = expr[:k].rstrip().endswith("!")
            sign = segment_sign(expr, k, f) * (-1 if negated else 1)
            frames.append({"sign": sign, "range": False, "unclear": call_or_index, "lhs_rev": False})
        elif c in ")]" and len(frames) > 1:
            frames.pop()
        elif re.match(r"if\b", expr[k:k + 3]) and (k == 0 or not expr[k - 1].isalnum()):
            f["unclear"] = True
        elif c == "|" and re.search(r"\bforall\b[^:]*$", expr[:k]) and expr[k:k + 2] != "||" and expr[k - 1] != "|":
            f["range"] = True
        k += 1
    if any(f["unclear"] for f in frames):
        return 0
    sign = segment_sign(expr, pos, frames[-1])
    return -sign if frames[-1]["range"] else sign


def segment_sign(expr: str, pos: int, frame: dict) -> int:
    """-1 if another ==> follows pos at the same depth (pos is in an antecedent)."""
    depth, follows = 0, False
    for m in re.finditer(r"\(|\)|\[|\]|<==>|==>|::", expr[pos:]):
        t = m.group(0)
        if t in "([":
            depth += 1
        elif t in ")]":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and t == "==>":
            follows = True
            break
        elif depth == 0:
            break
    sign = frame["sign"] * (-1 if follows else 1)
    return -sign if frame["lhs_rev"] else sign
