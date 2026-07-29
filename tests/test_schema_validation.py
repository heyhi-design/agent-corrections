"""Schema validation tests: validate_entry() and validate_registry()."""

from datetime import date, datetime
from pathlib import Path

import yaml

from corrections_cli import _parse_date, validate_entry, validate_registry

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def make_entry(**overrides):
    entry = {
        "id": "test-entry",
        "category": "constraint",
        "severity": "factual",
        "trigger_context": "when testing",
        "correction": "do the right thing",
        "status": "active",
        "times_fired": 0,
        "last_fired": None,
    }
    entry.update(overrides)
    return entry


def test_valid_entry_has_no_errors():
    assert validate_entry(make_entry()) == []


def test_missing_required_field_is_reported():
    entry = make_entry()
    del entry["correction"]
    errors = validate_entry(entry)
    assert any("correction" in e for e in errors)


def test_invalid_severity_is_reported():
    errors = validate_entry(make_entry(severity="urgent"))
    assert any("severity" in e for e in errors)


def test_invalid_status_is_reported():
    errors = validate_entry(make_entry(status="done"))
    assert any("status" in e for e in errors)


def test_fired_entry_without_last_fired_date_is_reported():
    # times_fired > 0 with last_fired: null is a genuine internal contradiction -- it
    # claims the correction fired but can't say when, which makes it invisible to
    # `corrections audit`'s staleness check (neither "stale" nor "never fired" catches it).
    errors = validate_entry(make_entry(times_fired=3, last_fired=None))
    assert any("last_fired" in e for e in errors)


def test_zero_times_fired_with_null_last_fired_is_valid():
    # The normal case for a freshly-written entry that hasn't fired yet.
    assert validate_entry(make_entry(times_fired=0, last_fired=None)) == []


def test_negative_times_fired_is_reported():
    errors = validate_entry(make_entry(times_fired=-1))
    assert any("times_fired" in e for e in errors)


def test_non_integer_times_fired_is_reported():
    errors = validate_entry(make_entry(times_fired="a lot"))
    assert any("times_fired" in e for e in errors)


def test_bool_is_not_accepted_as_times_fired():
    # bool is a subclass of int in Python; explicitly reject it so `times_fired: true`
    # doesn't silently pass as 1.
    errors = validate_entry(make_entry(times_fired=True))
    assert any("times_fired" in e for e in errors)


def test_malformed_last_fired_is_reported():
    errors = validate_entry(make_entry(last_fired="not-a-date"))
    assert any("last_fired" in e for e in errors)


def test_null_last_fired_is_valid():
    assert validate_entry(make_entry(last_fired=None)) == []


def test_superseded_without_superseded_by_is_reported():
    errors = validate_entry(make_entry(status="superseded", times_fired=5, last_fired="2026-01-01"))
    assert any("superseded_by" in e for e in errors)


def test_superseded_with_superseded_by_is_valid():
    errors = validate_entry(
        make_entry(
            status="superseded",
            times_fired=5,
            last_fired="2026-01-01",
            superseded_by="a linter rule",
        )
    )
    assert errors == []


def test_optional_pattern_field_does_not_break_validation():
    errors = validate_entry(make_entry(pattern="the failure mode explained"))
    assert errors == []


def test_parse_date_returns_none_for_none():
    assert _parse_date(None) is None


def test_parse_date_passes_a_plain_date_through():
    assert _parse_date(date(2026, 7, 1)) == date(2026, 7, 1)


def test_parse_date_parses_an_iso_string():
    assert _parse_date("2026-07-01") == date(2026, 7, 1)


def test_parse_date_coerces_a_full_datetime_to_a_date():
    # A YAML value like `2026-07-01 10:00:00` (a timestamp, not a date-only value)
    # parses to a datetime.datetime -- which IS a datetime.date subclass, so it must
    # be truncated to .date() rather than passed through, or later arithmetic against
    # a plain date raises.
    result = _parse_date(datetime(2026, 7, 1, 10, 0, 0))
    assert result == date(2026, 7, 1)
    assert type(result) is date


def test_entry_with_a_full_datetime_last_fired_is_schema_valid():
    errors = validate_entry(make_entry(times_fired=1, last_fired=datetime(2026, 7, 1, 10, 0, 0)))
    assert errors == []


def test_duplicate_ids_are_reported_at_registry_level():
    entries = [make_entry(id="dup"), make_entry(id="dup")]
    errors_by_id = validate_registry(entries)
    assert "dup" in errors_by_id
    assert any("duplicate" in e for e in errors_by_id["dup"])


def test_unique_ids_produce_no_registry_errors():
    entries = [make_entry(id="one"), make_entry(id="two")]
    assert validate_registry(entries) == {}


def test_example_registry_is_fully_schema_valid():
    """The bundled example registry must itself be valid — this is the registry every
    other smoke test and quickstart step runs against."""
    with open(EXAMPLES / "example-registry.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    errors_by_id = validate_registry(data["entries"])
    assert errors_by_id == {}, errors_by_id


def test_example_registry_has_at_least_eight_entries():
    # A floor, not a ceiling. CONTRIBUTING.md invites contributors to add an entry --
    # a hardcoded upper bound here turns every accepted contribution into an
    # unrelated, confusing CI failure the moment the count crosses it (it did, at
    # exactly 12, before this test was rewritten).
    with open(EXAMPLES / "example-registry.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert len(data["entries"]) >= 8


def test_example_registry_covers_all_three_statuses():
    with open(EXAMPLES / "example-registry.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    statuses = {e["status"] for e in data["entries"]}
    assert statuses == {"active", "retired", "superseded"}
