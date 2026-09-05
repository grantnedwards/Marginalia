#!/usr/bin/env python3
"""Cyclomatic complexity, ABC score and code-LOC for this tree, stdlib `ast` only.

Run:  .venv/bin/python tools/complexity.py [paths...] [--max-cc N --max-abc N --max-loc N]
Exits non-zero when a limit is exceeded, so it can become a repo check next to
tests/test_invariants.py. With no paths it measures `marginalia/`.

WHY THE RULES ARE SPELLED OUT: an unreproducible metric is worthless, and every
tool disagrees. These are the exact rules, so the numbers are arguable.

CYCLOMATIC COMPLEXITY (CC)
  Start at 1 per function. Then +1 for each of:
    ast.If (an `elif` is a nested If, so a 4-way chain scores 3), ast.For,
    ast.AsyncFor, ast.While, ast.IfExp (ternary), ast.ExceptHandler (per
    `except` clause), ast.Assert, ast.match_case (per case, including the
    wildcard), and each `if` clause of a comprehension (`comprehension.ifs`).
  Plus, per ast.BoolOp, +len(values)-1 -- `a and b and c` is +2, because each
  extra operand is another short-circuit exit.
  NOT counted, deliberately: `with`/`async with` (not a branch), `try`/`else`/
  `finally` bodies (only the handlers), `return`/`break`/`continue`,
  comprehension `for` clauses (only their `if`s -- the enumerated rule set says
  `if`, and a map over a list is not a decision), decorators, and boolean
  operators inside a default argument or a decorator expression.
  SCOPING: a nested def or lambda is measured as its OWN entry and does not add
  to its parent. Module-level code (imports, constants, compiled regexes) is one
  pseudo-function `<module>`. A class body is not an entry -- its methods each
  are, and a bare `field: int` list scores nothing anywhere on purpose.

ABC SCORE
  Magnitude = sqrt(A^2 + B^2 + C^2), reported with the raw triple. Same nested
  scoping as CC.
  A -- assignments/stores: ast.Assign (+1 per target, so `a = b = 1` is 2 but
    `a, b = x` is 1 store of one tuple), ast.AugAssign, ast.AnnAssign that has a
    value, ast.NamedExpr (`:=`), each `for` target (statement and comprehension),
    each `with ... as x`, each `except ... as e`. Imports are NOT counted (they
    are module plumbing, and counting them just taxes the top of every file).
  B -- branches, in the ABC sense of "message send": each ast.Call. That is
    function calls, method calls, constructor calls and calls inside
    comprehensions and f-strings. `await f()` is one call, not two. Attribute
    reads (`r["kind"]`, `self.x`) are NOT counted -- Python is not Ruby, so
    counting every attribute access would swamp the B term.
  C -- conditions: each ast.Compare operator (+1 per operator, so `1 <= x < 9`
    is 2), each BoolOp extra operand (+len(values)-1), and +1 for each If,
    IfExp, While, comprehension `if`, ExceptHandler, match_case and Assert test.
    So C double-counts a test that also contains a comparison -- that is the
    point: `if a == b` is both a condition and a comparison.

CODE LOC (per file)
  total physical lines minus (blank lines, comment-only lines, docstring lines).
  Comment-only means the COMMENT token starts the line (tokenize, so a `#`
  inside a string is not a comment and a trailing comment still leaves the line
  counted as code). Docstring lines are lineno..end_lineno of every
  module/class/function docstring Constant -- same classification
  tests/test_invariants.py already uses.
"""

from __future__ import annotations

import ast
import io
import math
import sys
import tokenize
from pathlib import Path

_CC1 = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler,
        ast.Assert, ast.match_case)
_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_COND1 = (ast.If, ast.IfExp, ast.While, ast.ExceptHandler, ast.Assert, ast.match_case)


def _scope_nodes(node):
    """ast.walk, pruned at nested scopes (their bodies belong to their own entry)."""
    out, stack = [], list(_bodies(node))
    while stack:
        cur = stack.pop()
        out.append(cur)
        if isinstance(cur, _SCOPE):
            continue
        stack.extend(ast.iter_child_nodes(cur))
    return out


def _bodies(node):
    if isinstance(node, ast.Lambda):
        return [node.body]
    out = []
    for name in ("body", "orelse", "finalbody", "handlers"):
        out.extend(getattr(node, name, []) or [])
    return out


def _measure(node) -> tuple[int, int, int, int]:
    cc, a, b, c = 1, 0, 0, 0
    for n in _scope_nodes(node):
        if isinstance(n, _CC1):
            cc += 1
        elif isinstance(n, ast.BoolOp):
            cc += len(n.values) - 1
        elif isinstance(n, ast.comprehension):
            cc += len(n.ifs)
        if isinstance(n, _COND1):
            c += 1
        elif isinstance(n, ast.BoolOp):
            c += len(n.values) - 1
        elif isinstance(n, ast.Compare):
            c += len(n.ops)
        elif isinstance(n, ast.comprehension):
            c += len(n.ifs)
            a += 1
        if isinstance(n, ast.Call):
            b += 1
        elif isinstance(n, ast.Assign):
            a += len(n.targets)
        elif isinstance(n, ast.AugAssign | ast.NamedExpr | ast.For | ast.AsyncFor):
            a += 1
        elif isinstance(n, ast.AnnAssign) and n.value is not None:
            a += 1
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            a += 1
        elif isinstance(n, ast.ExceptHandler) and n.name:
            a += 1
    return cc, a, b, c


def _docstring_lines(tree: ast.Module) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                out.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return out


def _code_loc(src: str, tree: ast.Module) -> int:
    lines = src.splitlines()
    skip = _docstring_lines(tree)
    skip.update(i for i, ln in enumerate(lines, 1) if not ln.strip())
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT and not tok.line[:tok.start[1]].strip():
            skip.add(tok.start[0])
    return sum(1 for i in range(1, len(lines) + 1) if i not in skip)


def scan(paths: list[Path]):
    funcs, files, broken = [], [], []
    for path in sorted({p for root in paths for p in ([root] if root.is_file()
                                                      else root.rglob("*.py"))}):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as exc:
            broken.append(f"{path}:{exc.lineno} does not parse: {exc.msg}")
            continue
        for node in [tree, *(n for n in ast.walk(tree) if isinstance(n, _SCOPE))]:
            name = getattr(node, "name", None) or (
                "<module>" if isinstance(node, ast.Module) else "<lambda>")
            if isinstance(node, ast.ClassDef):
                continue  # its methods are measured individually; the class body is plumbing
            cc, a, b, c = _measure(node)
            funcs.append((path, name, getattr(node, "lineno", 1), cc, a, b, c,
                          math.sqrt(a * a + b * b + c * c)))
        files.append((path, _code_loc(src, tree)))
    return funcs, files, broken


_PROBE = '''"""doc"""
# comment
def f(x, y):
    """doc
    two lines"""
    with open(x) as fh:          # `with` is not a branch; `as fh` is an A
        n = 1 if x and y or 0 else 2
    for i in [q for q in y if q > 1 if q < 9]:
        assert i
    try:
        pass
    except (KeyError, ValueError) as e:
        del e
    return n
'''


def _selftest() -> None:
    """CC 1 +If(ternary) +2 BoolOp +For +compr.2ifs +assert +except = 9; C = 9; code LOC = 10."""
    tree = ast.parse(_PROBE)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    cc, a, b, c = _measure(fn)
    assert (cc, a, b, c) == (9, 5, 1, 9), (cc, a, b, c)
    assert _code_loc(_PROBE, tree) == 10, _code_loc(_PROBE, tree)
    print("selftest ok")


def main(argv: list[str]) -> int:
    if argv[:1] == ["--selftest"]:
        return _selftest() or 0
    lim = {"cc": 0, "abc": 0, "loc": 0}
    paths, i = [], 0
    while i < len(argv):
        if argv[i].startswith("--max-"):
            lim[argv[i][6:]] = int(argv[i + 1])
            i += 2
        else:
            paths.append(Path(argv[i]))
            i += 1
    root = Path(__file__).resolve().parent.parent
    funcs, files, broken = scan(paths or [root / "marginalia"])
    rel = lambda p: str(p.relative_to(root)) if p.is_relative_to(root) else str(p)  # noqa: E731

    print(f"{'CC':>4}  {'ABC':>6}  {'A/B/C':>10}  location")
    print("-" * 74)
    for p, n, ln, cc, a, b, c, mag in sorted(funcs, key=lambda r: (-r[3], -r[7])):
        print(f"{cc:>4}  {mag:>6.1f}  {f'{a}/{b}/{c}':>10}  {rel(p)}:{n} (L{ln})")
    print(f"\n{'LOC':>5}  {'maxCC':>5}  {'sumCC':>5}  file")
    print("-" * 74)
    for p, loc in sorted(files, key=lambda r: -r[1]):
        mine = [f for f in funcs if f[0] == p]
        print(f"{loc:>5}  {max(f[3] for f in mine):>5}  {sum(f[3] for f in mine):>5}  {rel(p)}")

    bad = list(broken)  # an unparseable file is a failure, not a silent omission
    bad += [f"CC {cc} > {lim['cc']}: {rel(p)}:{n}"
           for p, n, _l, cc, *_r in funcs if lim["cc"] and cc > lim["cc"]]
    bad += [f"ABC {r[7]:.1f} > {lim['abc']}: {rel(r[0])}:{r[1]}"
            for r in funcs if lim["abc"] and r[7] > lim["abc"]]
    bad += [f"LOC {loc} > {lim['loc']}: {rel(p)}" for p, loc in files
            if lim["loc"] and loc > lim["loc"]]
    if bad:
        print("\nOVER LIMIT:\n" + "\n".join("  " + b for b in bad), file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
