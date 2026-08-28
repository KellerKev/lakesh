"""One place that knows what a write looks like, and what this session may do.

Same philosophy as `redact.py`: a single module owns the judgement, so
there is exactly one thing to audit and one thing to fix.

### The gate

Two checks, of increasing strength:

* `is_read_only` — the original leading-keyword test. Cheap, and it is
  what the MCP server has always used.
* `find_write` — scans for a write keyword *anywhere* at statement level.
  This closes two holes `is_read_only` waves through: the
  `WITH x AS (INSERT …) SELECT * FROM x` smuggling that its own docstring
  conceded, and stacked statements like `SELECT 1; DROP TABLE t`.

Neither is a parser, and saying so matters. `find_write` runs over SQL
whose string literals, quoted identifiers and comments have been blanked
by `strip_literals`, so `WHERE note = 'please delete'` and a column named
`delete_flag` don't trip it — but a determined caller with an unusual
dialect can still surprise it.

DuckDB's `json_serialize_sql()` was the obvious stronger alternative and
was rejected deliberately: it refuses anything that is not a pure SELECT,
which correctly catches the CTE smuggling, but it also cannot parse
legitimate Snowflake reads (time travel, `RESULT_SCAN`). In a read-only
session that means refusing valid *queries* — a dialect-agnostic keyword
scan is the better primary.

### The ratchet

Modelled on Snowflake's Restricted Session Scope: two layers (operator
policy, then caller narrowing), narrowing only, and **restrictions cannot
be relaxed for the life of the session**.

That last property is enforced by the *absence of an API to relax it* —
`Session.narrow()` takes no boolean, there is no `widen()`, and the MCP
tool that calls it takes no argument. A check that could be bypassed
would be a weaker guarantee than a method that does not exist.

Process lifetime is the session: an MCP client spawns `lakesh mcp` per
session, `lakesh exec` runs one statement, and the REPL is one process per
sitting. Module-level state is therefore exactly session-scoped —
`mcp._KNOWN_SECRETS` is the existing precedent for that pattern.

**The honest limit:** a caller able to spawn a *second* `lakesh mcp` gets
a fresh session. That is inherent to process-per-session and is the same
escape Snowflake documents ("start a new chat"). Caller narrowing is
therefore a guardrail; only the policy layer — a config key, or an env var
in the client's spawn block — is a control that travels to every spawn.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .config import Profile

# --------------------------------------------------------------------------
# what counts as a read

# Statements that begin a read. Deliberately broader than ANSI, because
# lakesh is a universal tool and each engine has its own spelling:
#   from    — DuckDB's FROM-first syntax
#   table   — Postgres / DuckDB `TABLE t`
#   list    — Snowflake stage listing
#   call    — resolved per-procedure, see `_call_is_read`
_READ_ONLY_LEADING = re.compile(
    r"^\s*(select|show|describe|desc|with|explain|pragma|values|from|table|"
    r"list|call|use|analyze|begin|start|set)\b",
    re.IGNORECASE,
)

# Statement-level verbs that change something — data, schema, session, or
# what the engine can reach. `attach`/`install`/`load`/`copy` are here
# because a read-only session that can ATTACH a writable database or COPY
# a table to disk is not read-only in any sense the caller would expect.
#
# `do` is here because a Postgres DO block executes an arbitrary body,
# and `execute` covers Snowflake's EXECUTE IMMEDIATE and prepared
# statements. Neither can be inspected from the call site.
_WRITE_KEYWORDS = frozenset({
    "insert", "update", "delete", "merge", "upsert", "replace",
    "create", "drop", "alter", "truncate", "rename",
    "grant", "revoke",
    "copy", "export", "import",
    "attach", "detach", "install", "load",
    "reset", "vacuum", "checkpoint",
    "commit", "rollback",
    "do", "execute", "exec",
    # File transfer. `put` and `remove` change what is in a stage, so
    # they are writes in the ordinary sense. `get` is the asymmetric one:
    # it *reads* remotely but *writes* to local disk, and the filesystem
    # sandbox does not cover driver-side file access — measured. So it is
    # a write here even though it looks like the mirror of a read.
    #
    # All three were already refused, but only because they do not begin
    # like a read. Naming them makes the refusal a decision and the
    # message name the right verb.
    "put", "get", "remove",
})

# Verbs whose read/write nature depends on what follows them, resolved by
# `_qualified_write` rather than by membership alone.
_CONDITIONAL_KEYWORDS = frozenset({"set", "begin", "start", "call", "analyze"})

# `BEGIN READ ONLY` / `START TRANSACTION READ ONLY` is the one transaction
# form a read-only session should positively want, and `SET` is a
# prerequisite for many legitimate read workloads (`SET SESSION …`,
# `SET TIME ZONE`). Treat them as reads only when the read-only intent is
# explicit or the setting is session-scoped.
_READ_ONLY_QUALIFIER = re.compile(r"\bread\s+only\b", re.IGNORECASE)
_SESSION_SET = re.compile(
    r"^\s*set\s+(session|local|time\s+zone|timezone|search_path)\b", re.IGNORECASE)

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9$]*")

# Single-quoted strings, double-quoted identifiers, backtick identifiers,
# line comments and block comments. `#` is MySQL's and BigQuery's second
# line-comment syntax.
_QUOTED_RE = r"""
      '(?:[^']|'')*'          # 'text', with '' escaping
    | "(?:[^"]|"")*"          # "identifier"
    | `[^`]*`                 # `identifier`
    | --[^\n]*                # -- line comment
    | \#[^\n]*                # # line comment (MySQL, BigQuery)
    | /\*.*?\*/               # /* block comment */
"""

# Dollar-quoted bodies, including the TAGGED form `$BODY$ … $BODY$` that
# every `CREATE FUNCTION … LANGUAGE plpgsql` in the wild uses. The
# backreference makes the closing tag match the opening one.
_DOLLAR_RE = r"| \$(\w*)\$.*?\$\1\$"

_MASKABLE_RE = re.compile(_QUOTED_RE + _DOLLAR_RE, re.VERBOSE | re.DOTALL)
_EXECUTABLE_RE = re.compile(_QUOTED_RE, re.VERBOSE | re.DOTALL)


def strip_literals(sql: str, *, keep_bodies: bool = False) -> str:
    """`sql` with string literals, quoted identifiers and comments blanked.

    Length-preserving, so any offset computed against the result still
    lines up with the original. The point is that
    `WHERE note = 'please delete'` and `-- drop this later` must not look
    like writes.

    `keep_bodies=True` leaves dollar-quoted bodies intact. That
    distinction matters and is easy to get backwards: a `$$ … $$` body is
    a *literal* as far as masking is concerned, but it is *executable
    code* as far as the write gate is concerned. Blanking it hides the
    `DELETE` inside `DO $$ BEGIN DELETE FROM t; END $$`, which is exactly
    how that statement used to pass the gate.
    """
    pattern = _EXECUTABLE_RE if keep_bodies else _MASKABLE_RE
    return pattern.sub(lambda m: " " * len(m.group()), sql)


def _leads_like_read(sql: str) -> bool:
    """Whether the statement *begins* like a read.

    Runs against stripped SQL so a leading comment does not defeat it —
    `/* note */ SELECT 1` is a read, and used to be refused because this
    test saw the raw text while the error message took its head word from
    the stripped text.

    Not sufficient on its own: `CALL` and `BEGIN` lead like reads and may
    not be, which is why `is_read_only` also consults `find_write`.
    """
    cleaned = strip_literals(sql, keep_bodies=True).strip().lstrip("(")
    return bool(_READ_ONLY_LEADING.match(cleaned))


def is_read_only(sql: str) -> bool:
    """Whether `sql` is a read.

    Both halves of the judgement, not just the leading keyword: a
    statement that starts like a read can still smuggle a write (`WITH x
    AS (INSERT …)`, `SELECT 1; DROP TABLE t`, an unvouched `CALL`). This
    used to be the leading-keyword test alone, which meant the
    unrestricted MCP path applied a weaker gate than the read-only one.
    """
    return blocks_write(sql) is None


def _is_function_call(cleaned: str, match: re.Match) -> bool:
    """True when the word is a function call rather than a statement.

    `replace(`, `truncate(`, `insert(` and `analyze(` are scalar
    functions on one engine or another, and every one of them used to be
    refused because the character before the word was `(` — which the old
    rule read as "start of a subquery". A verb is a call when it is
    followed by `(` and preceded by something that can precede an
    expression.
    """
    after = cleaned[match.end():].lstrip()
    if not after.startswith("("):
        return False
    before = cleaned[:match.start()].rstrip()
    if not before:
        return False                       # `INSERT (...)` at the head is DML
    # A statement boundary before it means it really is a statement.
    return before[-1] not in ";"


def _qualified_write(sql: str, word: str, cleaned: str, start: int) -> bool:
    """Whether a conditional verb is a write in this statement."""
    if word in ("begin", "start"):
        # `BEGIN READ ONLY` is the transaction form a read-only session
        # most wants; a bare BEGIN opens a writable one.
        return not _READ_ONLY_QUALIFIER.search(cleaned[start:start + 60])
    if word == "set":
        return not _SESSION_SET.match(cleaned[start:])
    if word == "analyze":
        # `EXPLAIN (ANALYZE …)` and `EXPLAIN ANALYZE …` are the same
        # statement; the parenthesised spelling used to be refused and
        # the bare one allowed.
        return "explain" not in cleaned[:start].lower()
    if word == "call":
        return not _call_is_read(cleaned[start:])
    return True


# Procedures known to be reads. Populated by the dialect registry; this
# is the fallback for a source with no registered dialect.
_READ_PROCEDURES: set[str] = set()

_CALL_TARGET_RE = re.compile(r"call\s+([\w.$]+)", re.IGNORECASE)


def _call_is_read(fragment: str) -> bool:
    """Whether `CALL x(...)` names a procedure vouched for as a read.

    There is no way to determine this from the statement: Snowflake
    deprecated the volatility keywords for procedures, procedure bodies
    can build SQL at runtime, and procedures are not atomic — one that
    fails midway can still have written. So lakesh never guesses. Either
    the procedure is a built-in whose behaviour is known (DuckLake's read
    procedures are a closed set) or a human listed it in config.
    """
    found = _CALL_TARGET_RE.match(fragment.lstrip())
    if not found:
        return False
    return found.group(1).rsplit(".", 1)[-1].lower() in _READ_PROCEDURES


def set_read_procedures(names) -> None:
    """Install the allow-list for this session."""
    _READ_PROCEDURES.clear()
    _READ_PROCEDURES.update(n.lower() for n in names)


def find_write(sql: str) -> str | None:
    """The first write keyword appearing at statement level, or None.

    Runs over SQL whose literals and comments are blanked but whose
    dollar-quoted bodies are **kept**, so a write inside a procedure body
    is still visible.
    """
    cleaned = strip_literals(sql, keep_bodies=True)
    prev: str | None = None
    for match in _WORD_RE.finditer(cleaned):
        word = match.group().lower()
        if word not in _WRITE_KEYWORDS and word not in _CONDITIONAL_KEYWORDS:
            prev = word
            continue
        if _is_function_call(cleaned, match):
            prev = word
            continue
        if not _starts_a_clause(cleaned, match.start(), prev):
            prev = word
            continue
        if word in _CONDITIONAL_KEYWORDS and not _qualified_write(
                sql, word, cleaned, match.start()):
            prev = word
            continue
        return word.upper()
    return None


# Tokens after which a verb genuinely begins a new clause.
#
# `as` is deliberately NOT here: `WITH x AS (INSERT …)` is already caught
# by the `(` rule, while treating `as` as an opener refuses
# `SELECT * FROM tbl AS load` — any alias that collides with a verb.
# Same reasoning retires `then`/`else`, which only ever fired on CASE
# expressions containing a function like `replace()`.
_CLAUSE_OPENERS = frozenset({"do", "begin", "union"})


def _starts_a_clause(cleaned: str, start: int, prev: str | None) -> bool:
    before = cleaned[:start].rstrip()
    if not before:
        return True                      # start of the statement
    if before[-1] in ";()":
        return True
    return prev in _CLAUSE_OPENERS


# sqlglot, when installed, is consulted as a SECOND opinion that can only
# ever *add* a refusal. The keyword scan stays authoritative for allowing,
# so the safety floor is identical whether or not the package is present —
# which is the property a safety control needs. sqlglot degrades
# unparseable statements to `exp.Command`, whose only useful field is the
# leading keyword, i.e. the same signal the keyword scan already uses; the
# gain is on the statements it *does* parse.
_SQLGLOT_WRITES = (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
    "TruncateTable", "Copy", "Grant", "Revoke",
)


def _sqlglot_write(sql: str, dialect_name: str = "") -> str | None:
    """A write sqlglot sees that the keyword scan missed, or None.

    Never used to permit anything: a parse failure, a missing package or
    an unrecognised node all return None and leave the keyword scan's
    verdict standing.
    """
    try:
        import sqlglot
    except ImportError:
        return None
    try:
        parsed = sqlglot.parse(sql, dialect=dialect_name or None)
    except Exception:
        return None                      # unparseable: keyword scan stands
    for statement in parsed:
        if statement is None:
            continue
        kind = type(statement).__name__
        if kind in _SQLGLOT_WRITES:
            return kind.replace("Table", "").upper()
    return None


def blocks_write(sql: str, dialect_name: str = "") -> str | None:
    """Why `sql` is not a read, or None if it is one.

    `find_write` first because it is strictly stronger; the leading-keyword
    check catches the remaining shapes (a bare `TRUNCATE`-like verb this
    module doesn't list, or anything that simply doesn't begin like a
    read).
    """
    found = find_write(sql)
    if found:
        return found
    if not _leads_like_read(sql):
        head = _WORD_RE.search(strip_literals(sql, keep_bodies=True))
        return head.group().upper() if head else "statement"
    return _sqlglot_write(sql, dialect_name)


# --------------------------------------------------------------------------
# the ratchet

POLICY = "policy"   # the operator: profile key, env var, server flag
USER = "user"       # the caller: CLI flag, REPL meta-command, MCP tool


@dataclass(frozen=True)
class Restriction:
    """An effective restriction and where it came from.

    `set_by` matters as much as `read_only`: a caller told only "blocked"
    will retry, while a caller told "blocked by profile.read_only, and it
    cannot be relaxed from here" can stop and say so.
    """

    read_only: bool = False
    source: str | None = None
    set_by: str | None = None

    def describe(self) -> str:
        if not self.read_only:
            return "unrestricted"
        origin = "the operator's config" if self.source == POLICY else "this session"
        return f"read-only (set by {origin}: {self.set_by})"

    def as_dict(self) -> dict:
        return {
            "read_only": self.read_only,
            "source": self.source,
            "set_by": self.set_by,
            "relaxable": False,
        }


def _env_read_only() -> bool:
    return os.environ.get("LAKESH_READ_ONLY", "0").lower() in ("1", "true", "yes")


class Session:
    """Process-lifetime restriction state.

    There is deliberately no way to widen. `narrow()` is the only mutator
    and it takes no boolean.
    """

    def __init__(self) -> None:
        self._user_set_by: str | None = None
        self._flag_set_by: str | None = None   # policy, set at launch

    # -- mutators ---------------------------------------------------------

    def narrow(self, set_by: str) -> Restriction:
        """Latch caller-applied read-only on. Idempotent; never reversible."""
        if self._user_set_by is None:
            self._user_set_by = set_by
        return Restriction(True, USER, self._user_set_by)

    def set_policy_flag(self, set_by: str) -> None:
        """Record a launch-time operator restriction (`--read-only`)."""
        if self._flag_set_by is None:
            self._flag_set_by = set_by

    def reset_for_tests(self) -> None:
        self._user_set_by = None
        self._flag_set_by = None

    # -- queries ----------------------------------------------------------

    def policy(self, prof: Profile | None = None) -> Restriction:
        """The operator layer, recomputed per call from profile + env +
        launch flag. Recomputing is safe: an operator tightening config
        mid-session takes effect, and one loosening it cannot undo a
        caller's latch, which is checked separately in `effective`."""
        if prof is not None and prof.read_only:
            return Restriction(True, POLICY, f"profile {prof.name!r} read_only")
        if _env_read_only():
            return Restriction(True, POLICY, "LAKESH_READ_ONLY")
        if self._flag_set_by:
            return Restriction(True, POLICY, self._flag_set_by)
        return Restriction()

    def effective(self, prof: Profile | None = None) -> Restriction:
        """policy OR user. Boolean OR is the whole precedence rule: a layer
        may add a restriction, never subtract one.

        Policy wins the label when both apply — the caller needs to know it
        would still be restricted in a fresh session.
        """
        policy = self.policy(prof)
        if policy.read_only:
            return policy
        if self._user_set_by:
            return Restriction(True, USER, self._user_set_by)
        return Restriction()


SESSION = Session()


def refusal(restriction: Restriction, blocked: str) -> dict:
    """The payload for a rejected write. Names the offending verb and where
    the restriction came from, because "blocked" alone tells a caller
    nothing it can act on."""
    if restriction.source == POLICY:
        origin = (
            f"The restriction comes from the operator's config "
            f"({restriction.set_by}) and cannot be relaxed from here — "
            f"LAKESH_MCP_WRITE does not override it."
        )
    else:
        origin = (
            f"The restriction was set by this session ({restriction.set_by}) "
            f"and cannot be lifted; start a new session if it was set in error."
        )
    return {
        "error": (
            f"write rejected: this statement contains `{blocked}` and the "
            f"session is read-only. {origin}"
        ),
        "error_type": "read_only_blocked",
        "blocked": blocked,
        "restriction": restriction.as_dict(),
    }
