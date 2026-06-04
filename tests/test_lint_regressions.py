"""Static regression checks for the specific bug classes that hit
production this week. Each test corresponds to a real incident.

If you find yourself adding a new ``# noqa`` to skip one of these, stop
and read the test docstring — there was a reason it was added.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


def _docstring_line_set(src: str) -> set[int]:
    """1-indexed line numbers occupied by docstrings.

    Module/class/function docstrings often mention forbidden keywords
    deliberately (e.g. "replaces the SQL-Server-only OUTPUT INSERTED
    pattern"). Without this exclusion, the lint flags its own
    explanation as a violation.
    """
    out: set[int] = set()
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return out
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not (isinstance(body, list) and body):
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            start = first.value.lineno
            end = getattr(first.value, "end_lineno", None) or start
            for ln in range(start, end + 1):
                out.add(ln)
    return out


REPO_ROOT = Path(__file__).resolve().parents[1]
# Files we lint. Excludes throwaway tooling and the SQL-Server-only
# legacy schema (which deliberately still has those keywords).
EXCLUDE_DIRS = {".venv", ".git", ".claude", ".github", "tests", "node_modules"}
EXCLUDE_FILES = {"schema.sql"}


def _live_files(suffixes: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for p in REPO_ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.name in EXCLUDE_FILES:
            continue
        if p.suffix not in suffixes:
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# 1. Forbidden SQL syntax — the codebase is MySQL-backed. SQL-Server-only
#    keywords must not appear in live code, because pymysql will either
#    500 or silently no-op when it hits them.
# ---------------------------------------------------------------------------
class TestNoForbiddenSqlSyntaxInLiveCode:
    """The application talks to MySQL via pymysql. The project was
    originally written for SQL Server and we migrated. Several
    SQL-Server-specific operators kept showing up afterwards in code
    that's now executed by MySQL. Each one breaks differently — some
    500 the page, some fail silently inside a try/except — so we
    lint them out explicitly rather than discovering them under load.

    Incidents covered:
      * TOP (n) ........... orders page, recon page, backups page (500)
      * OUTPUT INSERTED ... reconciler, order poller, dividends, ai_recs
                            (silent writes that never persist)
      * SYSUTCDATETIME() . settings store (UPDATE ran but timestamp wrong)
      * ISNULL(x, y) ..... llm_ops/tracker, analytics aggregations (500)
      * TRY_CONVERT ...... /dashboard, /api/projects (500 on auth lookup)
      * dbo.<table> ...... settings store (Unknown database 'dbo')
    """

    BANNED = [
        # (regex, kind, hint)
        (r"\bSELECT\s+TOP\s+", "TOP", "Use ORDER BY ... LIMIT :n at end"),
        (r"\bOUTPUT\s+INSERTED\b", "OUTPUT INSERTED",
         "Use db.connection.insert_returning_id helper"),
        (r"\bSYSUTCDATETIME\s*\(\s*\)", "SYSUTCDATETIME",
         "Use UTC_TIMESTAMP(6)"),
        (r"\bISNULL\s*\(", "ISNULL", "Use COALESCE(x, y)"),
        (r"\bTRY_CONVERT\s*\(", "TRY_CONVERT",
         "Use direct equality — CHAR(36) collation is case-insensitive"),
        (r"\bGETUTCDATE\s*\(\s*\)", "GETUTCDATE", "Use UTC_TIMESTAMP()"),
        (r"\bNEWID\s*\(\s*\)", "NEWID", "Use UUID()"),
        (r"\bdbo\.\w+", "dbo. prefix",
         "Strip — MySQL doesn't use schema prefixes that way"),
    ]

    @pytest.mark.parametrize("pattern,kind,hint", BANNED)
    def test_keyword_not_present(self, pattern, kind, hint):
        compiled = re.compile(pattern, re.IGNORECASE)
        offenders: list[str] = []
        for f in _live_files((".py",)):
            try:
                src = f.read_text(encoding="utf-8")
            except Exception:
                continue
            doc_lines = _docstring_line_set(src)
            for ln_i, ln in enumerate(src.splitlines(), 1):
                # Skip docstrings (their content frequently *describes*
                # the very keywords we forbid) and comment-only lines.
                if ln_i in doc_lines:
                    continue
                if ln.lstrip().startswith("#"):
                    continue
                if compiled.search(ln):
                    rel = f.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{ln_i}: {ln.strip()[:120]}")
        assert not offenders, (
            f"\n\n{kind} found in live code (use: {hint}):\n  "
            + "\n  ".join(offenders[:10])
            + ("\n  ..." if len(offenders) > 10 else "")
        )


# ---------------------------------------------------------------------------
# 2. insert_returning_id / return-name mismatches
# ---------------------------------------------------------------------------
def test_no_insert_returning_id_return_name_mismatch():
    """My OUTPUT-INSERTED transformer occasionally left the WRONG variable
    name on the return statement (e.g. inserted into ``snapshot_id`` but
    returned ``closure_id`` which was undefined). Found three of these
    in production today after they had been silently crashing the
    portfolio snapshotter for hours. Lint them out."""
    problems: list[str] = []
    for f in _live_files((".py",)):
        try:
            src = f.read_text(encoding="utf-8")
        except Exception:
            continue
        lines = src.splitlines()
        for i, line in enumerate(lines):
            m = re.search(r"(\w+)\s*=\s*insert_returning_id\(", line)
            if not m:
                continue
            assigned = m.group(1)
            # Scan the next 25 lines for a return statement
            for j in range(i + 1, min(i + 25, len(lines))):
                rm = re.match(r"\s*return\s+(\w+)", lines[j])
                if not rm:
                    continue
                returned = rm.group(1)
                if returned == assigned:
                    break
                if returned in ("None", "True", "False"):
                    break
                rel = f.relative_to(REPO_ROOT)
                problems.append(
                    f"{rel}:{i+1}: assigned {assigned!r} but returns "
                    f"{returned!r} at line {j+1}"
                )
                break
    assert not problems, (
        "\n\ninsert_returning_id assign/return mismatches "
        "(would NameError at runtime):\n  " + "\n  ".join(problems)
    )


# ---------------------------------------------------------------------------
# 3. Concentration math must use stable reference, not options_bp.
# ---------------------------------------------------------------------------
def test_concentration_module_uses_stable_reference():
    """concentration.py must NOT bake in ``buying_power`` as its
    multiplier. The fix earlier today routes through ``_resolve_reference``
    which prefers ``project.max_equity_allocation``. Regressing this
    silently re-introduces the "trading stops after a few hours" bug."""
    src = (REPO_ROOT / "risk" / "concentration.py").read_text(encoding="utf-8")
    # The cap calculation line must reference 'reference', not raw bp.
    has_resolve = "_resolve_reference" in src
    assert has_resolve, (
        "risk/concentration.py no longer calls _resolve_reference — "
        "the cap will use fluctuating options_buying_power again and "
        "silently block trades once the project has a few positions open."
    )


# ---------------------------------------------------------------------------
# 4. _split_mysql_statements must skip semicolons inside SQL comments.
# ---------------------------------------------------------------------------
def test_split_mysql_statements_skips_comment_semicolons():
    """Today's incident: a `-- ...` comment in schema_mysql.sql contained
    a semicolon, the splitter treated it as a statement boundary, and
    the leftover comment text was executed as SQL on every app start
    (1064 parse error → trader.service failed to start, prod went
    down). The splitter now recognises `--` line comments and
    `/* ... */` block comments. Lock that in."""
    from db.connection import _split_mysql_statements

    # Line comment with embedded semicolon must NOT split.
    sql = (
        "-- This comment has a ; semicolon in it\n"
        "CREATE TABLE foo (id INT);\n"
        "CREATE TABLE bar (id INT);\n"
    )
    out = [s for s in _split_mysql_statements(sql) if s.strip()]
    assert len(out) == 2, (
        f"line-comment semicolon split the file into {len(out)} chunks; "
        f"the comment ate part of the next statement: {out}"
    )

    # Block comment with embedded semicolon must NOT split.
    sql = "/* a; b */ CREATE TABLE q (id INT); SELECT 1;"
    out = [s for s in _split_mysql_statements(sql) if s.strip()]
    assert len(out) == 2

    # String literals with semicolons still work.
    sql = "INSERT INTO foo VALUES ('a;b'); SELECT 1;"
    out = [s for s in _split_mysql_statements(sql) if s.strip()]
    assert len(out) == 2


# ---------------------------------------------------------------------------
# 5. async route handlers must not call AlpacaClient methods directly
#    — they MUST go through asyncio.to_thread(...), or the synchronous
#    Alpaca SDK blocks the event loop and freezes the entire app.
#    Production hung at 15:04 today because of this exact bug class.
# ---------------------------------------------------------------------------
def test_async_routes_do_not_block_event_loop_with_alpaca_calls():
    """In api/main.py: any `async def api_…` body that contains a
    direct `client.<method>(…)` invocation (no surrounding
    `asyncio.to_thread`) is a latent hang — Alpaca's HTTP SDK is
    synchronous, so an unwrapped call inside an async handler blocks
    the whole event loop for as long as Alpaca takes to respond.

    Whitelist: calls to local helpers / sync DB repos are fine — only
    `AlpacaClient` instance methods are dangerous. Detection heuristic:
    a function that creates `AlpacaClient(...)` and then uses the
    resulting variable on a method call line that does NOT also
    contain `to_thread`.
    """
    import ast
    src_path = REPO_ROOT / "api" / "main.py"
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        # We only care about FastAPI route handlers — name them api_*
        # or the page-rendering ones (helps narrow false positives).
        if not (node.name.startswith("api_") or node.name.endswith("_page")):
            continue
        # Walk the function body for `client = AlpacaClient(...)` plus
        # any synchronous `client.<m>(...)` call.
        bound_names: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call):
                fn = sub.value.func
                if (isinstance(fn, ast.Name) and fn.id == "AlpacaClient"
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)):
                    bound_names.add(sub.targets[0].id)
        if not bound_names:
            continue
        # Now scan the source lines of this function for
        # `<bound>.<method>(...)` calls that are NOT inside an
        # `asyncio.to_thread(...)` wrapper.
        start = node.lineno
        end = node.end_lineno or start
        for ln in range(start, end + 1):
            line = src.splitlines()[ln - 1]
            stripped = line.strip()
            if "to_thread" in stripped:
                continue
            for name in bound_names:
                # Patterns like `client.foo(` etc.
                pat = (rf"\b{re.escape(name)}\."
                       r"[A-Za-z_][A-Za-z0-9_]*\s*\(")
                if re.search(pat, stripped):
                    offenders.append(
                        f"{node.name} (line {ln}): {stripped[:100]}"
                    )
                    break
    assert not offenders, (
        "\nAsync FastAPI route handlers must wrap synchronous Alpaca "
        "calls in `await asyncio.to_thread(...)`. Direct calls block "
        "the event loop and freeze the entire app under slow Alpaca "
        "responses (today's prod hang). Offenders:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 6. systemd unit (in deploy scripts) must use main.py all, not api.
# ---------------------------------------------------------------------------
def test_deploy_scripts_use_main_py_all():
    """The systemd ExecStart was wrong for half a day — set to
    ``main.py api`` (autorun=False), so the FastAPI app came up but the
    MultiTenantRunner (Scanner/Strategist/Guardrail/Executor) never
    spawned. Service looked healthy, agents were dead. Lint that out."""
    offenders = []
    for f in _live_files((".py", ".sh", ".bat", ".ps1", ".service")):
        try:
            src = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # Skip the explicit dual-mode helpers (we DO want to mention
        # "main.py api" in docstrings or migration notes).
        for ln_i, ln in enumerate(src.splitlines(), 1):
            if "ExecStart" not in ln:
                continue
            if "main.py api" in ln:
                offenders.append(
                    f"{f.relative_to(REPO_ROOT)}:{ln_i}: {ln.strip()}"
                )
    assert not offenders, (
        "\n\nDeploy artifacts have ExecStart=python main.py api — that "
        "starts the API without the runner, no agents will fire:\n  "
        + "\n  ".join(offenders)
    )
