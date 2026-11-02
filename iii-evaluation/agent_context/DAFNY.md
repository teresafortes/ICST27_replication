---
name: dafny
description: Reference for reading, verifying and debugging Dafny programs and their specifications (requires, ensures, loop invariants, decreases, frames). Use when working with .dfy files or Dafny verifier output.
---

# Dafny reference for verification work

Based on the Dafny Reference Manual (https://dafny.org/latest/DafnyRef/DafnyRef).
Section numbers in brackets point to the manual.

## 1. How Dafny verifies

- Dafny is **modular**: each method and function is verified on its own, using only the
  *specifications* (not the bodies) of the methods it calls. [13.6.1.3]
- A program is verified when every method and function verifies and there are no
  unproven assumptions (`assume` statements). [13.6.1.3]
- Specifications and ghost code are used only during verification; the compiler drops them. [1]
- Dafny first checks that each specification is **well-formed** on its own (no out-of-bounds
  index, null dereference or division by zero inside `requires`/`ensures`), then checks the body
  against the specification. These are reported as separate assertion batches. [7.8]

## 2. Contracts

| Clause | Meaning | Default when absent |
|---|---|---|
| `requires P` | Precondition. Checked at every call site; the body may assume it on entry. | `requires true` |
| `ensures Q` | Postcondition. Must hold on every return path. | `ensures true` |
| `modifies S` | Heap locations a method (or loop) may write. | Nothing (empty set) for methods |
| `reads S` | Heap locations a function may read. | Functions: nothing. Methods: anything (`*`) |
| `decreases E` | Termination measure for recursion or loops. | Dafny guesses (see §4) |

[7.1.1–7.1.6]

- Multiple `requires` (or `ensures`) clauses are **conjoined in order**. Order matters: an earlier
  conjunct can make a later one well-defined, e.g. `requires 0 < a.Length` before
  `requires a[0] == 0`. [7.1.1, 7.1.2]
- Clauses can be labeled: `requires L: 0 < i < 10`. [7.1.1]
- Framing applies only to the heap (objects and arrays). Local variables, `seq`, `set`,
  `multiset` and `map` are values and never appear in frames. [7.1.4]
- `modifies` means "may write". Writing a location not in the frame is an error even if
  the old value is restored later. Newly allocated objects may always be modified. [7.1.6]

```dafny
class Thermostat {
  var target: int
  var history: seq<int>

  method SetTarget(t: int)
    modifies this                                       // writes fields of this object
    ensures target == t
    ensures history == old(history) + [old(target)]    // old(...) = value on entry
  {
    history := history + [target];
    target := t;
  }
}
```

## 3. Loops

Multiple `invariant` clauses are conjoined in source order. [7.1.7]

For a loop `while G invariant I { Body }`, Dafny proves:

1. **I holds on entry**: it must be true just before the loop is first reached. [7.1.7, 8.15.1]
2. **I is maintained**: assuming `I && G` at the start of an iteration, `I` holds again after
   `Body`. [8.15.1]
3. **After the loop, Dafny knows only `I && !G`** (plus facts about variables the loop does
   not assign). Anything needed later, such as the method's postcondition, must follow from
   that. Dafny does not discover loop properties by itself. [8.15.1]

Other rules:

- Invariants are **not checked at `break`**. [8.15.4]
- **Loop targets**: local variables that the loop body never assigns are known to be unchanged.
  If the body may write the heap, the loop's effective `modifies` clause is the enclosing
  method's unless the loop declares its own. Add an invariant (or a narrower loop `modifies`)
  to keep facts about heap locations the loop does not touch. [8.15.3]

```dafny
method Clamp(a: array<int>, lo: int, hi: int)
  requires lo <= hi
  modifies a
  ensures forall k :: 0 <= k < a.Length ==> lo <= a[k] <= hi
  ensures forall k :: 0 <= k < a.Length && lo <= old(a[k]) <= hi ==> a[k] == old(a[k])
{
  var i := 0;
  while i < a.Length
    invariant 0 <= i <= a.Length                                    // bounds
    invariant forall k :: 0 <= k < i ==> lo <= a[k] <= hi           // done part: in range
    invariant forall k :: 0 <= k < i && lo <= old(a[k]) <= hi ==> a[k] == old(a[k])
    invariant forall k :: i <= k < a.Length ==> a[k] == old(a[k])   // rest: untouched
  {
    if a[i] < lo {
      a[i] := lo;
    } else if a[i] > hi {
      a[i] := hi;
    }
    i := i + 1;
  }
  // Here Dafny knows: invariants && !(i < a.Length), so i == a.Length.
}
```

Checks on this example:
- *On entry*: with `i == 0`, the bounds hold and the `0 <= k < 0` invariants are vacuously
  true; the "untouched" invariant holds because nothing has been written yet.
- *Maintained*: the body changes only `a[i]`, so the facts about `k < i` still hold, the new
  element `a[i]` is now in range (and unchanged if it already was), and the "untouched"
  invariant still covers `k >= i + 1`.
- *After the loop*: `i <= a.Length && !(i < a.Length)` gives `i == a.Length`, so the
  "done part" invariants become the postconditions. Without the upper bound `i <= a.Length`,
  this last step fails. Without the "untouched" invariant, the second postcondition fails,
  because Dafny would not know what `a[i]` held before the iteration that changed it.

## 4. Termination

- Every loop and recursive call needs a measure that **strictly decreases** and is **bounded
  below** (for integers, the bound is 0). [8.15.2]
- For a loop guard `i < n`, Dafny guesses `decreases n - i`. For recursion, it guesses the
  parameter list in order. [8.15.2, 7.1.3]
- Measures are lexicographic tuples: `decreases x, y`. [7.1.3]
- `decreases *` skips the termination proof (allowed for loops and compiled methods only,
  not for functions or lemmas); a method calling such code must be `decreases *` too. [7.1.3]
- Termination is its own check. It is separate from loop invariants and postconditions.

## 5. Types and collections

| Type | Key operations |
|---|---|
| `int`, `nat` | `nat` is `int` restricted to `0 <= n`. Division and `%` are Euclidean: `a % b` is never negative. No implicit `int`/`real` conversion; use `as int` / `as real`. |
| `seq<T>` | `|s|`, `s[i]`, `s[i := v]`, `s + t`, `x in s`, slices `s[lo..hi]`, `s[lo..]`, `s[..hi]`; `s[0..|s|] == s`; `<` / `<=` mean (proper) prefix. |
| `set<T>` | `x in s`, `x !in s`, `|s|`, `+` union, `-` difference, `*` intersection, `!!` disjoint. |
| `multiset<T>` | `m[x]` multiplicity, `+`, `-`, `*`, `|m|`; `multiset(s)` converts a sequence. |
| `map<K,V>` | `m[k]`, `m[k := v]`, `k in m`, `m.Keys`, `m.Values`, `m.Items`; `+` merges (right side wins). |
| `array<T>` | `a.Length`, `a[i]`, `new int[n]`. Arrays live on the heap and need `modifies`/`reads`. `a[..]`, `a[lo..hi]`, `a[lo..]`, `a[..hi]` give a `seq<T>` (requires `0 <= lo <= hi <= a.Length`). |

[5.5, 5.6.3.1, 5.10.2]

Common idioms:
- Permutation of an array: `multiset(a[..]) == multiset(old(a[..]))`.
- Sortedness: `forall i, j :: 0 <= i < j < a.Length ==> a[i] <= a[j]`.
- Quantifier bounds also act as well-formedness guards: `forall i :: 0 <= i < |s| ==> s[i] > 0`.

## 6. Two-state expressions

- `old(e)`: value of heap reads in `e` on method entry. It has no effect on locals or
  parameters. `old@L(e)` uses the state at label `L`. [9.22]
- `fresh(e)`: objects in `e` were allocated after method entry. [9.23]
- `unchanged(o)`: every field of `o` is equal to its value on entry. [9.25]

## 7. Functions, predicates, lemmas and proof statements

- **Functions** are pure; the body defines them, so a postcondition is usually not needed.
  **Predicates** are boolean functions. A `ghost` function or predicate exists only for
  verification. [6.3, 7.3]
- **Lemmas** are ghost methods used as proofs: `lemma L(x: T) requires P(x) ensures Q(x) { ... }`.
  Calling `L(x)` makes `Q(x)` available. Prefer `requires P ensures Q` over `ensures P ==> Q`. [6.3.3, 13.7.1.3]
- `assert P;` makes Dafny prove `P` at that point and then use it. Good for locating where a
  proof breaks. [8.17]
- `assert P by { ... }` proves `P` using the block's steps, then forgets them. [8.17]
- `assume P;` is accepted without proof and makes the program unsound; Dafny warns about it. [8.18]
- `forall x | P(x) ensures Q(x) { L(x); }` proves `forall x :: P(x) ==> Q(x)` from a lemma. [6.3]
- `calc == { A; B; { L(x); } C; }` writes a chain of equalities (or `<`, `<=`, `==>`, ...) with
  justifications. [8.23, 13.7.1.3]

### Worked proofs

**Induction with a lemma.** A recursive lemma call is the induction hypothesis.
Dafny checks that the lemma's `decreases` measure (here `n`) goes down.

```dafny
function Rep(x: int, n: nat): seq<int>
{
  if n == 0 then [] else [x] + Rep(x, n - 1)
}

lemma RepLength(x: int, n: nat)
  ensures |Rep(x, n)| == n
{
  if n > 0 {
    RepLength(x, n - 1);   // induction hypothesis for n - 1
  }
}
```

**A `calc` proof.** Each step is checked separately; the hint in braces is used for the step
it sits on.

```dafny
lemma RepAppend(x: int, n: nat, m: nat)
  ensures Rep(x, n + m) == Rep(x, n) + Rep(x, m)
{
  if n > 0 {
    calc == {
      Rep(x, n + m);
      [x] + Rep(x, (n - 1) + m);            // unfold the definition
      { RepAppend(x, n - 1, m); }           // induction hypothesis
      [x] + (Rep(x, n - 1) + Rep(x, m));
      ([x] + Rep(x, n - 1)) + Rep(x, m);    // concatenation is associative
      Rep(x, n) + Rep(x, m);                // fold the definition
    }
  }
}
```

**Using a lemma inside a loop with `assert ... by`.** The lemma's fact is needed only to
re-establish the invariant, so it is kept inside the `by` block.

```dafny
method Fill(x: int, n: nat) returns (s: seq<int>)
  ensures s == Rep(x, n)
{
  s := [];
  var i := 0;
  while i < n
    invariant 0 <= i <= n
    invariant s == Rep(x, i)
  {
    s := s + [x];
    assert s == Rep(x, i + 1) by {
      RepAppend(x, i, 1);   // Rep(x, i + 1) == Rep(x, i) + Rep(x, 1), and Rep(x, 1) == [x]
    }
    i := i + 1;
  }
}
```

**Permutation with `multiset`.** Swapping two elements keeps the multiset of the array, so
the permutation invariant is maintained automatically. The invariant `i % 2 == 0` is what
lets Dafny conclude, after the loop, that every pair was handled.

```dafny
method SwapPairs(a: array<int>)
  modifies a
  ensures multiset(a[..]) == multiset(old(a[..]))
  ensures forall k :: 0 <= k && k + 1 < a.Length && k % 2 == 0 ==> a[k] == old(a[k + 1]) && a[k + 1] == old(a[k])
{
  var i := 0;
  while i + 1 < a.Length
    invariant 0 <= i <= a.Length
    invariant i % 2 == 0
    invariant multiset(a[..]) == multiset(old(a[..]))
    invariant forall k :: 0 <= k < i && k % 2 == 0 ==> a[k] == old(a[k + 1]) && a[k + 1] == old(a[k])
    invariant forall k :: i <= k < a.Length ==> a[k] == old(a[k])
  {
    a[i], a[i + 1] := a[i + 1], a[i];
    i := i + 2;
  }
}
```

## 8. Reading verifier output

Each error line has the form `file.dfy(line,col): Error: message`. When a specification
clause is involved, a `Related location` line points at the clause that could not be proved.
Exact wording varies across Dafny versions ("might not hold" in older ones, "could not be
proved" in newer ones).

| Message | What failed |
|---|---|
| `a postcondition could not be proved on this return path` + `this is the postcondition that could not be proved` | An `ensures` clause does not follow from the state at that return. |
| `this loop invariant could not be proved on entry` | The invariant is false just before the loop. |
| `this invariant could not be proved to be maintained by the loop` | One iteration of the body does not re-establish the invariant. |
| `a precondition for this call could not be proved` + `this is the precondition that could not be proved` | A callee's `requires` clause is not established at the call. |
| `function precondition could not be proved` | A function is applied outside its `requires`. |
| `assertion might not hold` / `this proposition could not be proved` | An `assert` (or an internal assertion) cannot be proved. |
| `index out of range`, `lower bound out of range`, `upper bound below lower bound or above length of ...` | An index or slice may be out of bounds, possibly inside a specification. |
| `decreases expression might not decrease`, `decreases expression must be bounded below by 0`, `this decreases clause was not satisfied` | Termination. |
| `value does not satisfy the subset constraints of 'nat'` | A value may be negative where a `nat` is required. |
| `possible division by zero` | A divisor may be 0. |

Messages such as `arguments to > must be of a numeric type ...` or
`first argument to && must be of type bool` are **type errors**, reported before verification.
The program must type-check before any of the checks above run.

## 9. Debugging a failing proof

The manual's method is to turn the failure into an `assert` and move it backwards (computing the
weakest precondition) until the problem is obvious. [13.7.1]

1. **Failing postcondition**: replace `return e;` with `r := e; return;`, then add
   `assert <postcondition>;` just before the return. The error moves to the `assert`.
2. **Move the assert up** through statements:

| Code after the failing point | Assert before it |
|---|---|
| `x := Y; assert P;` | `assert P[x := Y];` |
| `if B { assert P; ... }` | `assert B ==> P;` |
| `if B { ... } else { assert P; ... }` | `assert !B ==> P;` |
| `var x := m(k); assert P(x);` (with `m` requires `A`, ensures `B`) | `assert A(k); assert forall x :: B(k, x) ==> P(x);` |

3. **Split** compound goals: `assert A && B;` into `assert A; assert B;`, and
   `assert A ==> B;` into `if A { assert B; }`.
4. **Quantifiers**: prove `forall x :: P(x) ==> Q(x)` with
   `forall x | P(x) ensures Q(x) { assert Q(x); }`. For `exists`, assert the property for a
   concrete witness first: `assert P(x0);`.
5. **For loops**, check the three obligations in §3 separately: assert the invariant just before
   the loop, assert it at the end of the body, and assert the postcondition right after the loop.

[13.7.1.1–13.7.1.3]

**Counterexamples**: `--extract-counterexample` reports a possible counterexample as a set of
assumptions about variable values. Dafny cannot guarantee that it actually violates the assertion,
so treat it as a hint. [13.7.1.4]

## 10. Command line

```
dafny verify program.dfy
dafny verify --extract-counterexample program.dfy     # report a potential counterexample
dafny verify --verification-time-limit 30 program.dfy # seconds per verification attempt
dafny verify --isolate-assertions program.dfy         # verify each assertion in its own batch
```

[13.6.1.3, 13.9.7]

For slow or timing-out proofs: `assume false;` at a point stops verification there, which helps
find the expensive part (remove it afterwards). `assert P by { ... }` keeps helper facts from
slowing down later proofs. [13.7.2]
