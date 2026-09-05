"""Enforces docs/SPEC.md "Invariants (a test greps each)" -- structurally, not by word grep.

A word grep gives FALSE POSITIVES here, and this build already hit two of them:
`timefmt.py`'s docstring says "does NOT import discord", and `epub.py`'s says
"ordered paragraphs". A test that fails on a comment gets deleted, not fixed. So
every check below reads the AST (or, for .sql, comment-stripped text) and never
counts a hit inside a docstring or a comment.

Files are DISCOVERED by walking `marginalia/`, so a module added later is covered
with no edit here.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "marginalia"


# --------------------------------------------------------------------- walking

def _py_files() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def _sql_files() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.sql") if "__pycache__" not in p.parts)


def _loc(path: Path, line: int) -> str:
    return f"{path.relative_to(ROOT)}:{line}"


@cache
def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@cache
def _docstring_ids(path: Path) -> frozenset[int]:
    """id() of every Constant node that is a module/class/function docstring."""
    out = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                out.add(id(first.value))
    return frozenset(out)


def _code_strings(path: Path):
    """(lineno, text) for every string literal that is NOT a docstring.

    Comments never reach the AST at all, so they are excluded for free. f-string
    pieces arrive as Constants inside JoinedStr, so `f"<t:{x}:R>"` is covered.
    """
    skip = _docstring_ids(path)
    for node in ast.walk(_tree(path)):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in skip):
            yield node.lineno, node.value


def _sql_code_lines(path: Path):
    """(lineno, text) for each .sql line with `--` comments stripped."""
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        yield i, line.split("--", 1)[0]


def _imported_roots(path: Path):
    """(lineno, top-level module name) for every import statement. Never string-matched."""
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import: `from . import x` has no top-level name.
            if node.level == 0 and node.module:
                yield node.lineno, node.module.split(".")[0]


# ------------------------------------------------------------------ invariant 1

def test_invariant_1_timestamp_markup_only_in_timefmt():
    bad = [f"{_loc(p, ln)} contains '<t:' in a string literal"
           for p in _py_files() if p.name != "timefmt.py"
           for ln, s in _code_strings(p) if "<t:" in s]
    assert not bad, (
        "INVARIANT 1 BROKEN: '<t:' timestamp markup must appear only in timefmt.py.\n"
        "Call timefmt.stamp()/ts() instead of hand-rolling the markup.\n" + "\n".join(bad))


# ------------------------------------------------------------------ invariant 2

# A table name only counts in a real SQL position. This is what stops
# epub.py's "ordered paragraphs" prose from reading as a violation.
_TEXT_TABLE = re.compile(
    r"""(?:\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:IF\s+NOT\s+EXISTS\s+)?["'`\[]?(paragraphs|para_fts)\b"""
    r"""|\b(paragraphs|para_fts)\s+MATCH\b"""
    r"""|\b(para_fts)\s*\()""",
    re.IGNORECASE,
)
_TEXT_TABLE_OK = {"library.py", "schema.sql"}


def _text_table_hit(s: str) -> str | None:
    m = _TEXT_TABLE.search(s)
    return next(g for g in m.groups() if g) if m else None


def test_invariant_2_text_tables_only_in_library_and_schema():
    """`paragraphs` / `para_fts` reachable only from library.py and schema.sql.

    Scope per SPEC: this guards ungated access to book TEXT. cogs/quote.py reading
    the `books` METADATA row (autocomplete, locator byline, /purge_book) is
    in-bounds and is deliberately NOT flagged -- `books` is not matched here.
    """
    bad = []
    for p in _py_files():
        if p.name in _TEXT_TABLE_OK:
            continue
        for ln, s in _code_strings(p):
            hit = _text_table_hit(s)
            if hit:
                # offset the match inside a multi-line SQL literal back to a real line
                off = s[:_TEXT_TABLE.search(s).start()].count("\n")
                bad.append(f"{_loc(p, ln + off)} names TEXT table '{hit}' in a SQL position")
    for p in _sql_files():
        if p.name in _TEXT_TABLE_OK:
            continue
        for ln, s in _sql_code_lines(p):
            hit = _text_table_hit(s)
            if hit:
                bad.append(f"{_loc(p, ln)} names TEXT table '{hit}' in a SQL position")
    assert not bad, (
        "INVARIANT 2 BROKEN: the TEXT tables paragraphs/para_fts may be named only in\n"
        "library.py and schema.sql -- everything else goes through the library API so the\n"
        "spoiler gate cannot be bypassed.\n" + "\n".join(bad))


# ------------------------------------------------------------------ invariant 3

_ENV_ATTRS = ("environ", "environb", "getenv")


def test_invariant_3_os_environ_only_in_config():
    bad = []
    for p in _py_files():
        if p.name == "config.py":
            continue
        for node in ast.walk(_tree(p)):
            if (isinstance(node, ast.Attribute) and node.attr in _ENV_ATTRS
                    and isinstance(node.value, ast.Name) and node.value.id == "os"):
                bad.append(f"{_loc(p, node.lineno)} reads os.{node.attr}")
            elif isinstance(node, ast.ImportFrom) and node.module == "os" and node.level == 0:
                bad += [f"{_loc(p, node.lineno)} imports os.{a.name}"
                        for a in node.names if a.name in _ENV_ATTRS]
    assert not bad, (
        "INVARIANT 3 BROKEN: only config.py may read the environment. Everything else takes\n"
        "a Config, which is what keeps the rest of the tree testable and token-free.\n"
        + "\n".join(bad))


# ------------------------------------------------------------------ invariant 4

_TERMINAL_EXCS = {"Forbidden", "NotFound"}


def _except_names(handler: ast.ExceptHandler) -> set[str]:
    t = handler.type
    parts = t.elts if isinstance(t, ast.Tuple) else ([t] if t is not None else [])
    return {p.attr if isinstance(p, ast.Attribute) else p.id
            for p in parts if isinstance(p, ast.Attribute | ast.Name)}


def _call_names(nodes) -> set[str]:
    out = set()
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Call):
                f = sub.func
                out.add(f.attr if isinstance(f, ast.Attribute) else
                        f.id if isinstance(f, ast.Name) else "")
    return out - {""}


def _awaited_call_names(nodes) -> set[str]:
    out = set()
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Await) and isinstance(sub.value, ast.Call):
                f = sub.value.func
                out.add(f.attr if isinstance(f, ast.Attribute) else
                        f.id if isinstance(f, ast.Name) else "")
    return out - {""}


def _shallow_stmts(body):
    """Statements in this handler's OWN control flow.

    Does not descend into a loop or a function defined inside the handler: a
    `continue` belonging to a fresh loop written in the handler is not a retry of
    the operation that failed.
    """
    for stmt in body:
        yield stmt
        if isinstance(stmt, ast.For | ast.AsyncFor | ast.While | ast.FunctionDef
                      | ast.AsyncFunctionDef | ast.Lambda):
            continue
        for name in ("body", "orelse", "finalbody"):
            yield from _shallow_stmts(getattr(stmt, name, []) or [])
        for h in getattr(stmt, "handlers", []) or []:
            yield from _shallow_stmts(h.body)


def test_invariant_4_forbidden_notfound_handlers_are_terminal():
    """Every `except Forbidden/NotFound` handler must be terminal -- never a retry.

    Repeating an invalid request burns the 10,000-per-10-minutes invalid-request
    ceiling and ends in a Cloudflare IP ban on the host.

    WHAT THIS CATCHES, exactly:
      * `continue` in the handler's own control flow (re-enters the enclosing loop)
      * a `while` loop opened inside the handler (a retry loop)
      * any call to `sleep` / `asyncio.sleep` anywhere in the handler (backoff)
      * a re-call, by NAME, of an operation the `try` body awaited -- e.g. `try:
        await ch.send(x)` / `except Forbidden: await ch.send(x)`

    WHAT IT DOES NOT CATCH (name-based AST matching has limits, stated so nobody
    over-trusts this test):
      * a retry hidden behind a differently-named helper, or reached by falling
        through to a caller that loops
      * an alias: the try awaits `op()` where `op = ch.send`, the handler awaits
        `ch.send()` directly -- different names, not flagged
      * a retry driven by mutating state the caller loops on (`self._pending`)
      * `break`, which is treated as terminal (it EXITS a loop) -- correct today
        for cogs/reading.py's event-creation loop
    """
    bad = []
    for p in _py_files():
        for node in ast.walk(_tree(p)):
            if not isinstance(node, ast.Try | ast.TryStar):
                continue
            awaited = _awaited_call_names(node.body)
            for h in node.handlers:
                if not (_except_names(h) & _TERMINAL_EXCS):
                    continue
                where = _loc(p, h.lineno)
                stmts = list(_shallow_stmts(h.body))
                if any(isinstance(s, ast.Continue) for s in stmts):
                    bad.append(f"{where} `except {'/'.join(sorted(_except_names(h)))}` "
                               "uses `continue`: that retries the failed request")
                if any(isinstance(s, ast.While) for s in stmts):
                    bad.append(f"{where} opens a `while` retry loop in the handler")
                if "sleep" in _call_names(h.body):
                    bad.append(f"{where} sleeps in the handler: backoff implies a retry")
                again = sorted(awaited & _call_names(h.body))
                if again:
                    bad.append(f"{where} re-calls the failed operation(s) "
                               f"{again} that the `try` body awaited")
    assert not bad, (
        "INVARIANT 4 BROKEN: a Forbidden/NotFound handler must be terminal. Retrying an\n"
        "invalid request burns the invalid-request ceiling and gets the host IP-banned.\n"
        + "\n".join(bad))


# ------------------------------------------------------------------ invariant 5

def test_invariant_5_mention_everyone_appears_nowhere():
    """`mention_everyone` appears nowhere in the shipped package -- PLAIN TEXT, unlike the
    invariants above. The word is in no prose here either (no docstring, no comment), so the
    false positives that force AST matching on 1/2/3/6 do not exist for it, and a hit in a
    comment is a hit worth reading anyway. docs/ENV.md discusses the permission bit to say
    why it is excluded from the invite mask; that is out of scope on purpose.
    """
    bad = [f"{_loc(p, ln)} names mention_everyone"
           for p in _py_files() + _sql_files()
           for ln, s in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1)
           if "mention_everyone" in s]
    assert not bad, (
        "INVARIANT 5 BROKEN: mention_everyone must appear nowhere in marginalia/. The bot\n"
        "never asks for or grants @everyone; sends pass explicit AllowedMentions.\n"
        + "\n".join(bad))


# ------------------------------------------------------------------ invariant 6

_NO_DISCORD = {"timefmt.py", "schedule.py"}


def test_invariant_6_timefmt_and_schedule_do_not_import_discord():
    """Parsed from ast.Import/ast.ImportFrom -- timefmt.py's docstring SAYS
    "Deliberately does NOT import discord", which is exactly the sentence a word
    grep would report as a violation."""
    found = {p.name for p in _py_files() if p.name in _NO_DISCORD}
    assert found == _NO_DISCORD, (
        f"INVARIANT 6 CANNOT BE CHECKED: expected {sorted(_NO_DISCORD)} under marginalia/, "
        f"found {sorted(found)}. Renamed or deleted? Update _NO_DISCORD.")
    bad = [f"{_loc(p, ln)} imports `discord`"
           for p in _py_files() if p.name in _NO_DISCORD
           for ln, root in _imported_roots(p) if root == "discord"]
    assert not bad, (
        "INVARIANT 6 BROKEN: timefmt.py and schedule.py are pure logic and must stay\n"
        "importable (and unit-testable) with no discord.py present.\n" + "\n".join(bad))


# ------------------------------------------------------------------ invariant 7

def test_invariant_7_every_module_imports_with_empty_environment():
    """Proven, not asserted: import every module in a subprocess with `env -i`.

    No token, no DISCORD_*, no HOME, no PATH, no gateway. This is the invariant
    that keeps the tree testable on a machine that will never hold the token --
    any module doing config or network work at import time fails here.
    """
    mods = sorted(
        ".".join(p.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")
        for p in _py_files()
    )
    assert "marginalia" in mods and len(mods) > 5, f"module discovery looks wrong: {mods}"
    probe = (
        "import importlib, json, sys, traceback\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"bad = []\n"
        f"for m in {mods!r}:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except BaseException:\n"
        "        frames = traceback.extract_tb(sys.exc_info()[2])\n"
        # blame the last frame inside OUR tree, not <frozen os> or site-packages
        f"        ours = [f for f in frames if f.filename.startswith({str(ROOT)!r})]\n"
        "        tb = (ours or frames)[-1]\n"
        "        bad.append('%s -> %s:%s %s' % (m, tb.filename, tb.lineno,\n"
        "                                       traceback.format_exception_only(\n"
        "                                           *sys.exc_info()[:2])[-1].strip()))\n"
        "print(json.dumps(bad))\n"
    )
    # env={} is the programmatic `env -i`: the child inherits nothing.
    proc = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env={},
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, (
        "INVARIANT 7 BROKEN: the import probe itself died under an empty environment.\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr[-2000:]}")
    bad = json.loads(proc.stdout.splitlines()[-1])
    assert not bad, (
        "INVARIANT 7 BROKEN: these modules do not import with an EMPTY environment (no\n"
        "token, no Discord connection). Move the work into a function called at runtime.\n"
        + "\n".join(bad))


# ------------------------------------------------------- the complexity ratchet, wired in

def test_the_complexity_gate_holds_over_the_package():
    """In-process; invariant 7 shells out because it needs an EMPTY environment, this does not.
    SCOPED TO `marginalia/`, so the tool's own CC-18 `_measure` and CC-17 `main` are out of
    scope by construction -- it is a ratchet on the shipped package, not on itself."""
    from tools.complexity import main
    assert main([str(PKG), "--max-cc", "15", "--max-abc", "30", "--max-loc", "350"]) == 0
