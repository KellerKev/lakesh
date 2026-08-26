"""Tests for PII masking.

The most valuable test in this file is
`test_default_rules_leave_a_plain_analytics_page_alone`. A masking feature
that fires on ordinary data gets switched off, and then it protects
nothing — so every default rule has to earn its place against a corpus of
benign lookalikes, not just against examples of the thing it detects.
"""
from __future__ import annotations

import datetime as dt
import decimal
import re
import time
import uuid

import pytest

from lakesh import mask
from lakesh.mask import (
    DEFAULT_RULES,
    LABEL_RE,
    MASKED,
    Policy,
    iban_mod97,
    luhn,
    mask_rows,
    mask_text,
    masked_like,
)


def _policy(mode="mask", labels=None):
    index = {r.label: r for r in DEFAULT_RULES}
    rules = (
        tuple(index[l] for l in labels) if labels
        else tuple(r for r in DEFAULT_RULES if r.default_on)
    )
    return Policy(mode, rules, "test")


def _mask_one(value, policy=None, column="c"):
    rows, report = mask_rows(policy or _policy(), [column], [(value,)])
    return rows[0][0], report


# --------------------------------------------------------------------------
# false positives — the discipline


BENIGN = [
    "ORD-4532019876543210",      # order number
    "1234567890123456",          # 16 digits, fails Luhn
    "2026-08-26",                # ISO date
    "build 10.0.19045.3803",     # version string
    "192.168.0.1",               # IP — pii.ip is off by default
    "8.5.0.1",                   # simultaneously an IP and a version
    "v1.2.3",
    "SKU12345678901234",
    "USD 1,234.56",
    "550e8400-e29b-41d4-a716-446655440000",
    "987-65-4320",               # SSN-shaped but a reserved 9xx prefix
    "INV 4532 0198 7654 3210",
]


@pytest.mark.parametrize("value", BENIGN)
def test_default_rules_leave_a_plain_analytics_page_alone(value):
    """The load-bearing test. Any new default rule must pass it."""
    masked, report = _mask_one(value)
    assert masked == value
    assert report.findings == {}


def test_credit_card_requires_luhn():
    """Without the checksum, a 16-digit order number masks."""
    assert _mask_one("1234567890123456")[0] == "1234567890123456"
    assert _mask_one("4111111111111111")[0] == MASKED
    assert luhn("378282246310005") and not luhn("1234567890123456")


def test_iban_requires_mod97():
    assert iban_mod97("GB82WEST12345698765432")
    assert not iban_mod97("GB82WEST12345698765433")
    assert _mask_one("GB82WEST12345698765433")[0] == "GB82WEST12345698765433"
    assert _mask_one("GB82WEST12345698765432")[0] == MASKED


def test_phone_requires_separators():
    """Requiring separators is what rejects a date, a build number and an
    order number — a digit-count rule masks all three."""
    for benign in ("2026-08-26", "build 10.0.19045.3803", "SKU12345678901234"):
        assert _mask_one(benign)[0] == benign
    for real in ("+1 415 555 0132", "415-555-0132", "(020) 7946 0958"):
        assert _mask_one(real)[0] == MASKED


def test_ssn_excludes_reserved_ranges():
    for invalid in ("987-65-4320", "000-12-3456", "666-12-3456",
                    "123-00-6789", "123-45-0000"):
        assert _mask_one(invalid)[0] == invalid
    assert _mask_one("123-45-6789")[0] == MASKED


def test_ip_is_off_by_default_because_it_collides_with_versions():
    """`8.5.0.1` is a valid IPv4 address and an ordinary version string.
    No regex separates them, so only the operator can decide."""
    assert _mask_one("8.5.0.1")[0] == "8.5.0.1"
    assert _mask_one("192.168.1.1", _policy(labels=["pii.ip"]))[0] == MASKED


def test_real_pii_is_masked():
    for value in ("john.doe@example.com", "+1 415 555 0132", "123-45-6789",
                  "4111111111111111", "GB82WEST12345698765432"):
        assert _mask_one(value)[0] == MASKED


# --------------------------------------------------------------------------
# detection modes


def test_value_rule_survives_aliasing():
    """`SELECT email AS x` renames the output column, so the column rule
    never fires. The value rule still does — which is why value detection
    is primary."""
    masked, report = _mask_one("a@b.com", column="x")
    assert masked == MASKED
    assert "pii.email" in report.findings


def test_column_rule_catches_what_no_value_pattern_can():
    """There is no honest value regex for a human name."""
    masked, report = _mask_one("Alice Smith", column="first_name")
    assert masked == MASKED
    assert "pii.name" in report.findings


def test_column_rule_is_defeated_by_aliasing():
    """Documented rather than pretended away."""
    masked, _ = _mask_one("Alice Smith", column="x")
    assert masked == "Alice Smith"


def test_free_text_keeps_the_non_sensitive_part():
    masked, _ = _mask_one("contact bob@corp.com about invoice 12")
    assert "bob@corp.com" not in masked
    assert "invoice 12" in masked


# --------------------------------------------------------------------------
# masked shapes


def test_masked_shapes_are_keyed_off_python_type():
    assert masked_like("x") == MASKED
    assert masked_like(42) == 0
    assert masked_like(1.5) == 0.0
    assert masked_like(decimal.Decimal("1234.56")) == decimal.Decimal("0.00")
    assert masked_like(dt.date(2026, 1, 2)) == dt.date(9999, 12, 31)
    assert masked_like(b"\xde\xad") == b""
    assert masked_like(uuid.uuid4()) == uuid.UUID(int=0)


def test_bool_masks_to_false_not_zero():
    """`bool` is a subclass of `int`; checking int first masks True as 0."""
    assert masked_like(True) is False
    assert masked_like(False) is False


def test_datetime_does_not_fall_through_to_date():
    """`datetime` is a subclass of `date`; the wide branch first would
    turn a timestamp into a bare date."""
    out = masked_like(dt.datetime(2026, 1, 2, 3, 4))
    assert isinstance(out, dt.datetime)


def test_tzaware_datetime_keeps_its_tzinfo():
    out = masked_like(dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc))
    assert out.tzinfo is dt.timezone.utc


def test_null_stays_null():
    """NULL means "no data"; masked means "data withheld". Conflating them
    makes an agent's reasoning about null rates quietly wrong."""
    assert masked_like(None) is None
    masked, _ = _mask_one(None, column="email")
    assert masked is None


def test_masked_decimal_preserves_scale():
    assert str(masked_like(decimal.Decimal("1234.56"))) == "0.00"


def test_masked_decimal_survives_jsonable():
    """`_jsonable` turns an integral Decimal into an int deliberately; a
    masked one must not become a float."""
    from lakesh.mcp import _jsonable
    assert _jsonable(masked_like(decimal.Decimal("1234.56"))) == 0.0
    assert _jsonable(masked_like(decimal.Decimal("42"))) == 0


def test_sentinel_date_is_not_a_plausible_real_value():
    """The conventional epoch sentinel occurs in real data as a default,
    so it cannot be told apart from a genuine value."""
    assert masked_like(dt.date(2026, 1, 2)).year == 9999


# --------------------------------------------------------------------------
# performance guards


def test_long_cell_does_not_backtrack():
    """The obvious email pattern is quadratic on a long string with no
    `@`: 25ms for one 3.2KB cell, minutes for a page. The `requires`
    literal pre-filter is what makes it free."""
    big = "x" * 4000
    start = time.perf_counter()
    mask_rows(_policy(), ["note"], [(big,)] * 50)
    assert (time.perf_counter() - start) < 1.0


def test_every_value_pattern_is_bounded():
    """Unbounded `+`/`*` is what makes the obvious email pattern quadratic
    on a long non-matching string. Every shipped pattern uses counted
    quantifiers instead, so none of them can backtrack catastrophically
    even when the `requires` pre-filter does not apply."""
    unbounded = re.compile(r"(?<!\\)[+*](?![\d,}])")
    for rule in DEFAULT_RULES:
        if rule.value is not None:
            body = re.sub(r"\\[+*]", "", rule.value.pattern)
            body = re.sub(r"\[[^\]]*\]", "C", body)      # ignore char classes
            assert not unbounded.search(body), f"{rule.label}: {rule.value.pattern}"


def test_oversized_cells_are_skipped_and_counted():
    huge = "a@b.com " + "x" * (mask.MAX_SCAN_CHARS + 10)
    masked, report = _mask_one(huge)
    assert masked == huge                       # not scanned
    assert report.skipped_long_values == 1      # and said so


def test_non_string_cells_never_reach_a_regex():
    """Analytic pages are mostly numeric, so skipping non-strings is the
    largest real-world saving and it is free."""
    calls = []

    class _CountingPattern:
        pattern = "stub"

        def search(self, text):
            calls.append(text)
            return None

        def sub(self, repl, text):
            return text

    from lakesh.mask import Rule
    policy = Policy("mask", (Rule(label="pii.stub", value=_CountingPattern()),), "t")
    mask_rows(policy, ["n"], [(i,) for i in range(100)])
    assert calls == []

    mask_rows(policy, ["n"], [("a string",)])
    assert calls == ["a string"]


# --------------------------------------------------------------------------
# modes and labels


def test_audit_reports_without_altering_the_data():
    rows, report = mask_rows(_policy("audit"), ["c"], [("a@b.com",)])
    assert rows[0][0] == "a@b.com"
    assert "pii.email" in report.findings


def test_off_does_nothing():
    rows, report = mask_rows(Policy("off"), ["c"], [("a@b.com",)])
    assert rows[0][0] == "a@b.com"
    assert report.findings == {}


def test_labels_match_the_duckicelake_tag_shape():
    """`{namespace}.{name}` so a finding can be POSTed to its
    /object-tags endpoint unchanged."""
    for rule in DEFAULT_RULES:
        assert LABEL_RE.match(rule.label), rule.label
    assert not LABEL_RE.match("PII.Email")
    assert not LABEL_RE.match("pii")
    assert not LABEL_RE.match("pii.a.b")
    assert LABEL_RE.match("phi.mrn")


def test_report_carries_the_honest_note():
    _rows, report = mask_rows(_policy(), ["c"], [("a@b.com",)])
    payload = report.as_dict(_policy())
    assert "not access control" in payload["note"]
    assert payload["findings"]["pii.email"]["columns"] == ["c"]


def test_mask_text_covers_plan_and_error_strings():
    """EXPLAIN plans embed filter literals verbatim."""
    plan = "SEQ_SCAN users Filters: (email = 'alice@corp.com')"
    out = mask_text(_policy(), plan)
    assert "alice@corp.com" not in out
    assert "SEQ_SCAN users" in out
    # audit mode reports but does not alter
    assert mask_text(_policy("audit"), plan) == plan
