"""Masking recognisable PII out of result values as they are rendered.

### What this is, and what it is not

lakesh masking defends against **incidental exposure** — an agent that
`SELECT *`s a table and pulls a column it never needed into a model's
context. It is **not access control**, and it does not stop a caller who
is trying to get the data out. Anything the SQL does to a value before
lakesh sees it defeats masking:

* `SELECT substr(email, 1, 5) FROM users` — the fragment no longer looks
  like an email address. Partial PII, unmasked.
* `SELECT count(*) FROM users WHERE email LIKE 'a%'` — the value never
  appears in the result; the count is the leak.
* `SELECT md5(ssn), count(*) FROM t GROUP BY 1` — a stable
  re-identification key, unmasked.
* `SELECT id FROM users ORDER BY email` — the ordering leaks the values.
* `SELECT email AS x FROM t` — defeats the column-name rules, because the
  output column is called `x`. Value rules still fire, which is exactly
  why value detection is primary here.

If a caller **must not be able to read** a column, enforce it where the
data lives: a Snowflake or duckicelake masking policy, or a view that
never selects it. This is a layer on top of that, not a substitute.

Naming follows from that: this is `masking`, never `secure`, `protected`
or `compliant`. It is also deliberately not called `redact` — that word
already means credentials in this codebase (see `redact.py`) and carries
stakes masking does not meet. The two markers differ for the same reason,
so a reader can tell a withheld credential from a withheld value.

### Why the rules look paranoid

Because a naive pattern set is unusable, and a masking feature that eats
legitimate results gets switched off — after which it protects nothing.
Measured against real data and benign lookalikes, a naive set masked an
ISO date, a build number, an IP address and an order number as "phone",
and any 13-19 digit run as a credit card. Every default rule below
therefore carries either a checksum (`luhn`, `iban_mod97`), a structural
requirement (separators in the phone pattern), or an exclusion of known-
invalid ranges (the SSN prefixes). Rules that cannot be made precise —
an IP address is indistinguishable from a version string — ship **off**.

### Why every rule needs a `requires` literal

The obvious email pattern backtracks quadratically on a long string with
no `@` in it: measured at 25ms for a single 3.2KB cell, which is minutes
for a page with a few text columns. Skipping the regex when the cell does
not contain a literal the pattern cannot match without turns 4957ms into
0.0ms over the same data, with identical hits. It is mandatory, including
for custom rules.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

# Distinct from `redact.MASK` ("***") on purpose: a reader must be able to
# tell a withheld credential from a withheld data value.
MASKED = "***masked***"

MASK_MODES = ("off", "mask", "audit")

# The ratchet order, which is deliberately not the declaration order
# above. `audit` returns unmasked rows with a findings report, so it is
# *weaker* than `mask` and a caller must never be able to move from
# `mask` down to it.
_STRICTNESS = {"off": 0, "audit": 1, "mask": 2}

# duckicelake's tag shape: {namespace}.{name}, two parts, lowercase. Match
# it so a finding can be POSTed to its /object-tags endpoint unchanged.
LABEL_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

# A single cell longer than this is not scanned. Regex cost is at best
# linear in length, and one multi-megabyte JSON blob per row would
# dominate everything else.
MAX_SCAN_CHARS = 4096


# --------------------------------------------------------------------------
# verifiers — the difference between a usable rule and a noisy one

def luhn(text: str) -> bool:
    """Luhn check over the digits of `text`.

    Without it, `1234567890123456` — an ordinary 16-digit order number —
    masks as a credit card. With it, it does not, while `4111111111111111`
    still does.
    """
    digits = [int(c) for c in re.sub(r"\D", "", text)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_mod97(text: str) -> bool:
    """ISO 13616 check digits."""
    compact = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if not 15 <= len(compact) <= 34:
        return False
    rotated = compact[4:] + compact[:4]
    try:
        numeric = "".join(
            str(int(c, 36)) if c.isalpha() else c for c in rotated
        )
    except ValueError:
        return False
    return int(numeric) % 97 == 1


# --------------------------------------------------------------------------
# rules

@dataclass(frozen=True)
class Rule:
    """One kind of sensitive value.

    `value` is the primary detector — it survives `SELECT email AS x`,
    which the column rule does not. `column` is a cheap secondary for
    columns whose contents no regex can recognise.
    """

    label: str
    requires: str = ""
    value: re.Pattern | None = None
    column: re.Pattern | None = None
    verify: Callable[[str], bool] | None = None
    whole_cell: bool = False
    default_on: bool = True
    why: str = ""

    def matches_value(self, text: str) -> bool:
        if self.value is None:
            return False
        if self.requires and self.requires not in text:
            return False          # the speed guard; see the module docstring
        found = self.value.search(text)
        if not found:
            return False
        return self.verify(found.group()) if self.verify else True

    def scrub(self, text: str) -> str:
        """Replace the matching spans, or the whole cell for a rule whose
        match implies the entire value is sensitive."""
        if self.value is None or self.whole_cell:
            return MASKED
        if self.requires and self.requires not in text:
            return text
        if self.verify is None:
            return self.value.sub(MASKED, text)
        return self.value.sub(
            lambda m: MASKED if self.verify(m.group()) else m.group(), text
        )


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        label="pii.email",
        requires="@",
        value=re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,63}\.[A-Za-z]{2,24}"),
        column=re.compile(r"(?:^|[_\-])e[_\-]?mail(?:[_\-]|$)|^email$", re.I),
        why="high-precision value pattern",
    ),
    Rule(
        label="pii.ssn",
        requires="-",
        # Reserved ranges excluded, which is what stops `987-65-4320`
        # (a common synthetic id) from matching.
        value=re.compile(r"(?<!\d)(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?!\d)"),
        column=re.compile(r"(?:^|_)(ssn|social_security(_number)?)(?:_|$)", re.I),
        why="reserved ranges excluded",
    ),
    Rule(
        label="pii.credit_card",
        requires="",          # digits only; the Luhn check is the filter
        value=re.compile(r"(?<![\d\-])(?:\d[ \-]?){12,18}\d(?![\d\-])"),
        column=re.compile(r"(?:^|_)(card|pan|cc)(_number|_num|no)?(?:_|$)", re.I),
        verify=luhn,
        why="Luhn required — without it, order numbers mask",
    ),
    Rule(
        label="pii.iban",
        requires="",
        value=re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}[A-Z0-9]{11,30}(?![A-Z0-9])"),
        column=re.compile(r"(?:^|_)iban(?:_|$)", re.I),
        verify=iban_mod97,
        why="mod-97 required",
    ),
    Rule(
        label="pii.phone",
        requires="",
        # Separators are REQUIRED. That is what rejects an ISO date, a
        # build number, an IP address and a 16-digit order number, all of
        # which a digit-count rule masks.
        # The trailing/leading digit lookarounds matter: without them a
        # grouped 16-digit run like "INV 4532 0198 7654 3210" gets sliced
        # into a phone-shaped substring. A match adjacent to more digits
        # on either side is part of something longer, not a phone number.
        value=re.compile(
            r"(?<![\w\-.])(?<!\d[ .\-])(?:\+\d{1,3}[ .\-]?)?"
            r"(?:\(\d{2,4}\)|\d{2,4})[ .\-]\d{3,4}[ .\-]\d{3,4}"
            r"(?![\w\-])(?![ .\-]\d)"
        ),
        column=re.compile(r"(?:^|_)(phone|mobile|telephone|msisdn)(?:_|$)", re.I),
        why="separators required",
    ),
    # Column-name only: no honest value pattern exists for these. A regex
    # for human names would match a large share of every text column.
    Rule(
        label="pii.name",
        column=re.compile(
            r"(?:^|_)(first|last|given|family|sur|full|middle|maiden|contact)_name$"
            r"|^(name|fullname|customer_name|employee_name|patient_name)$", re.I),
        whole_cell=True,
        why="column name only — no value pattern is honest for names",
    ),
    Rule(
        label="pii.address",
        column=re.compile(
            r"(?:^|_)(street|address(_line\d?)?|addr\d?|postal_?code|post_?code|"
            r"zip(_code)?)(?:_|$)", re.I),
        whole_cell=True,
        why="column name only",
    ),
    Rule(
        label="pii.date_of_birth",
        column=re.compile(r"(?:^|_)(dob|date_of_birth|birth_?date|birthday)(?:_|$)", re.I),
        whole_cell=True,
        why="column name only",
    ),
    # Off by default: undecidable at value level. `8.5.0.1` is a valid
    # IPv4 address and an ordinary version string, and no regex separates
    # them — only the operator knows which their data holds.
    Rule(
        label="pii.ip",
        requires=".",
        value=re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"),
        column=re.compile(r"(?:^|_)(ip|ip_address|client_ip|remote_addr)(?:_|$)", re.I),
        default_on=False,
        why="collides with version strings; opt in per dataset",
    ),
)


def rule_index(rules: tuple[Rule, ...] = DEFAULT_RULES) -> dict[str, Rule]:
    return {r.label: r for r in rules}


def default_labels() -> tuple[str, ...]:
    return tuple(r.label for r in DEFAULT_RULES if r.default_on)


# --------------------------------------------------------------------------
# masked shapes

_EPOCH_SENTINEL_DATE = _dt.date(9999, 12, 31)
_EPOCH_SENTINEL_DT = _dt.datetime(9999, 12, 31)


def masked_like(value: Any) -> Any:
    """The masked stand-in for `value`, keyed off its **Python type**.

    Not the declared SQL type, deliberately: Postgres `numeric(10,2)`
    arrives as a DuckDB `DECIMAL` through the attached catalog and as a
    Python `str` through `adbc_scan`, so a declared-type rule would
    silently not fire on the native path.

    Ordering is narrow-to-wide because `bool` is a subclass of `int` and
    `datetime` is a subclass of `date` — checking the wide one first masks
    `True` as `0` and a timestamp as a bare date.

    `None` stays `None`. NULL means "no data" and masked means "data
    withheld"; conflating them would make an agent's reasoning about null
    rates quietly wrong.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0
    if isinstance(value, decimal.Decimal):
        # Preserve scale so a masked NUMBER(10,2) still looks like one.
        return decimal.Decimal(0).quantize(value) if value.as_tuple().exponent <= 0 \
            else decimal.Decimal(0)
    if isinstance(value, float):
        return 0.0
    if isinstance(value, str):
        return MASKED
    if isinstance(value, _dt.datetime):
        return _EPOCH_SENTINEL_DT.replace(tzinfo=value.tzinfo)
    if isinstance(value, _dt.date):
        return _EPOCH_SENTINEL_DATE
    if isinstance(value, _dt.time):
        return _dt.time(0, 0)
    if isinstance(value, _dt.timedelta):
        return _dt.timedelta(0)
    if isinstance(value, (bytes, bytearray)):
        return b""
    if isinstance(value, uuid.UUID):
        return uuid.UUID(int=0)
    return MASKED


# --------------------------------------------------------------------------
# policy + report

@dataclass(frozen=True)
class Policy:
    mode: str = "off"
    rules: tuple[Rule, ...] = ()
    source: str = ""

    @property
    def active(self) -> bool:
        return self.mode in ("mask", "audit")

    @property
    def masks(self) -> bool:
        """`audit` detects and reports but does not alter the data."""
        return self.mode == "mask"


@dataclass
class Report:
    findings: dict[str, dict] = field(default_factory=dict)
    masked_columns: list[str] = field(default_factory=list)
    skipped_long_values: int = 0

    def note(self, label: str, column: str) -> None:
        entry = self.findings.setdefault(label, {"columns": [], "cells": 0})
        entry["cells"] += 1
        if column and column not in entry["columns"]:
            entry["columns"].append(column)

    def as_dict(self, policy: Policy) -> dict | None:
        if not policy.active:
            return None
        out: dict[str, Any] = {
            "mode": policy.mode,
            "source": policy.source,
            "findings": self.findings,
            "note": (
                "Values were masked as they were rendered. This is not access "
                "control: any SQL transformation applied before rendering "
                "(substr, hashing, LIKE filters, aggregates, ORDER BY) defeats it."
            ),
        }
        if self.masked_columns:
            out["masked_columns"] = self.masked_columns
        if self.skipped_long_values:
            out["skipped_long_values"] = self.skipped_long_values
        return out


def column_rules(policy: Policy, columns: list[str]) -> dict[int, Rule]:
    """Column index → rule, from the column-name pass.

    Runs once per result and short-circuits whole columns, so it is both
    the cheapest check and the one that saves the most work.
    """
    hits: dict[int, Rule] = {}
    for i, name in enumerate(columns or []):
        for rule in policy.rules:
            if rule.column is not None and rule.column.search(str(name)):
                hits[i] = rule
                break
    return hits


def mask_rows(
    policy: Policy, columns: list[str], rows: list[tuple]
) -> tuple[list[tuple], Report]:
    """Apply `policy` to `rows`. Returns the rows (unchanged in `audit`
    mode) and what was found."""
    report = Report()
    if not policy.active or not rows:
        return rows, report

    by_column = column_rules(policy, columns)
    for i, rule in by_column.items():
        name = columns[i] if i < len(columns) else str(i)
        report.masked_columns.append(name)

    value_rules = [r for r in policy.rules if r.value is not None]
    out: list[tuple] = []
    for row in rows:
        cells = list(row)
        for i, value in enumerate(cells):
            name = columns[i] if i < len(columns) else ""
            rule = by_column.get(i)
            if rule is not None:
                if value is not None:
                    report.note(rule.label, name)
                    if policy.masks:
                        cells[i] = masked_like(value)
                continue
            # Only strings ever reach a regex. Analytic pages are mostly
            # numeric, so this is the largest real-world saving and free.
            if not isinstance(value, str):
                continue
            if len(value) > MAX_SCAN_CHARS:
                report.skipped_long_values += 1
                continue
            for vr in value_rules:
                if vr.matches_value(value):
                    report.note(vr.label, name)
                    if policy.masks:
                        cells[i] = vr.scrub(cells[i])
        out.append(tuple(cells))
    return out, report


def mask_text(policy: Policy, text: str) -> str:
    """Mask free text — an EXPLAIN plan or an error payload.

    Plans embed filter literals, so `EXPLAIN … WHERE email='a@corp.com'`
    ships the value verbatim without this.
    """
    if not policy.masks or not text or len(text) > MAX_SCAN_CHARS * 16:
        return text
    for rule in policy.rules:
        if rule.value is not None:
            text = rule.scrub(text)
    return text


def resolve(cfg, prof=None, requested: str | None = None,
            session_mode: str | None = None) -> Policy:
    """Effective masking policy: config, then session latch, then the
    per-call request — and the strictest of them wins.

    `off < audit < mask`. A caller may only move *up* that order, because
    `audit` returns unmasked data and so would otherwise be an unmask
    switch: an operator's `mode = "mask"` must not be turnable into
    `audit` by whoever is asking.
    """
    import os

    # NOT the order of MASK_MODES — that is declaration order, and using
    # it would rank `audit` above `mask`, letting a caller turn an
    # operator's masking into an unmask switch.
    rank = _STRICTNESS
    candidates: list[tuple[str, str]] = []

    mode = getattr(cfg, "masking_mode", "off") or "off"
    labels = getattr(cfg, "masking_rules", None)
    if mode != "off":
        candidates.append((mode, "config [masking]"))

    if prof is not None and getattr(prof, "masking_mode", None):
        if prof.masking_mode != "off":
            candidates.append((prof.masking_mode, f"profile {prof.name!r} masking"))
        if prof.masking_rules is not None:
            labels = prof.masking_rules

    env = os.environ.get("LAKESH_MASK")
    if env and env in MASK_MODES and env != "off":
        candidates.append((env, "LAKESH_MASK"))
    if session_mode and session_mode != "off":
        candidates.append((session_mode, "set_masking tool"))
    if requested and requested != "off":
        candidates.append((requested, f"mask={requested!r}"))

    if not candidates:
        return Policy("off", (), "")
    best_mode, source = max(candidates, key=lambda c: rank.get(c[0], 0))

    index = rule_index()
    chosen = (
        tuple(index[l] for l in labels if l in index) if labels is not None
        else tuple(r for r in DEFAULT_RULES if r.default_on)
    )
    return Policy(best_mode, chosen, source)
