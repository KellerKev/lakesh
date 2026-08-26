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

_READ_ONLY_LEADING = re.compile(
    r"^\s*(select|show|describe|desc|with|explain|pragma|values)\b",
    re.IGNORECASE,
)

# Statement-level verbs that change something — data, schema, session, or
# what the engine can reach. `attach`/`install`/`load`/`copy` are here
# because a read-only session that can ATTACH a writable database or COPY
# a table to disk is not read-only in any sense the caller would expect.
_WRITE_KEYWORDS = frozenset({
    "insert", "update", "delete", "merge", "upsert", "replace",
    "create", "drop", "alter", "truncate", "rename",
    "grant", "revoke",
    "copy", "export", "import",
    "attach", "detach", "install", "load",
    "set", "reset", "vacuum", "checkpoint", "analyze",
    "begin", "commit", "rollback",
    "call", "execute",
})

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

# Single-quoted strings, double-quoted identifiers, backtick identifiers,
# dollar-quoted bodies, line comments and block comments — in one pass, so
# a comment inside a string and a string inside a comment both behave.
_MASKABLE_RE = re.compile(
    r"""
      '(?:[^']|'')*'          # 'text', with '' escaping
    | "(?:[^"]|"")*"          # "identifier"
    | `[^`]*`                 # `identifier`
    | \$\$.*?\$\$             # $$body$$
    | --[^\n]*                # -- line comment
    | /\*.*?\*/               # /* block comment */
    """,
    re.VERBOSE | re.DOTALL,
)


def strip_literals(sql: str) -> str:
    """`sql` with string literals, quoted identifiers and comments blanked.

    Length-preserving, so any offset computed against the result still
    lines up with the original. The point is that
    `WHERE note = 'please delete'` and `-- drop this later` must not look
    like writes.
    """
    return _MASKABLE_RE.sub(lambda m: " " * len(m.group()), sql)


def is_read_only(sql: str) -> bool:
    """Leading-keyword check — the original gate, behaviour unchanged.

    Covers the obvious top-level cases only. `find_write` is strictly
    stronger; this is kept because `EXPLAIN` / `SHOW` / `PRAGMA` are
    legitimate reads that carry no write keyword for `find_write` to find,
    so the two are used together.
    """
    return bool(_READ_ONLY_LEADING.match(sql.strip().lstrip("(")))


def find_write(sql: str) -> str | None:
    """The first write keyword appearing at statement level, or None.

    Runs over `strip_literals(sql)`, and only counts a keyword that starts
    a clause — the token before it must be a statement boundary
    (start-of-input, `;`, `(`, `)`, or one of the few words that can
    legitimately precede a verb, like `AS` in a CTE). That is what lets
    `SELECT delete_flag FROM t` and `SELECT * FROM updates` through while
    catching `WITH x AS (INSERT …)`.
    """
    cleaned = strip_literals(sql)
    prev: str | None = None
    for match in _WORD_RE.finditer(cleaned):
        word = match.group().lower()
        if word in _WRITE_KEYWORDS and _starts_a_clause(cleaned, match.start(), prev):
            return word.upper()
        prev = word
    return None


# Tokens after which a verb genuinely begins a new clause. `as` covers
# `WITH x AS (INSERT …)`; the bracket forms cover subqueries and stacked
# statements.
_CLAUSE_OPENERS = frozenset({"as", "then", "else", "do", "begin", "union", "all"})


def _starts_a_clause(cleaned: str, start: int, prev: str | None) -> bool:
    before = cleaned[:start].rstrip()
    if not before:
        return True                      # start of the statement
    if before[-1] in ";()":
        return True
    return prev in _CLAUSE_OPENERS


def blocks_write(sql: str) -> str | None:
    """Why `sql` is not a read, or None if it is one.

    `find_write` first because it is strictly stronger; the leading-keyword
    check catches the remaining shapes (a bare `TRUNCATE`-like verb this
    module doesn't list, or anything that simply doesn't begin like a
    read).
    """
    found = find_write(sql)
    if found:
        return found
    if not is_read_only(sql):
        head = _WORD_RE.search(strip_literals(sql))
        return head.group().upper() if head else "statement"
    return None


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
