"""Search tests: corrections_search.search()."""

from pathlib import Path

import yaml

from corrections_search import normalize, search

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

FIXTURE = [
    {
        "id": "sql-migration-missing-rollback-script",
        "category": "formatting",
        "severity": "behavioral",
        "trigger_context": "Reviewing a new SQL migration file before merge.",
        "correction": "Every migration needs a matching rollback script alongside the forward migration, not just the forward migration alone.",
        "status": "active",
        "times_fired": 1,
        "last_fired": "2026-04-18",
    },
    {
        "id": "hardcoded-hex-bypasses-token",
        "category": "architectural",
        "severity": "behavioral",
        "trigger_context": "Reviewing generated component code in a project with a design token system.",
        "correction": "Any hardcoded color that matches an existing token's value must reference the token instead.",
        "status": "active",
        "times_fired": 4,
        "last_fired": "2026-06-30",
    },
    {
        "id": "retired-example",
        "category": "factual",
        "severity": "factual",
        "trigger_context": "An old pattern nobody hits anymore.",
        "correction": "This no longer applies.",
        "status": "retired",
        "times_fired": 2,
        "last_fired": "2025-01-01",
    },
]


def test_normalize_strips_punctuation_and_hyphens():
    assert normalize("TanStack-Table's Sort!") == "tanstack table s sort"


def test_empty_query_returns_no_results():
    assert search("", FIXTURE) == []
    assert search("   ", FIXTURE) == []


def test_verbatim_match_is_word_boundary_anchored_not_raw_substring():
    # "table" must not match "unauditable" -- a short query shouldn't score via the
    # verbatim-phrase bonus just because it's embedded inside an unrelated longer word.
    fixture_with_trap = FIXTURE + [
        {
            "id": "unrelated-entry",
            "category": "constraint",
            "severity": "factual",
            "trigger_context": "Merging a PR that leaves the change history unauditable.",
            "correction": "Keep every merge traceable to its ticket.",
            "status": "active",
            "times_fired": 1,
            "last_fired": "2026-01-01",
        }
    ]
    results = search("table", fixture_with_trap)
    assert "unrelated-entry" not in [m.id for m in results]


def test_search_returns_at_least_one_matching_entry_with_expected_fields():
    results = search("formatting", FIXTURE)
    assert len(results) >= 1
    top = results[0]
    assert top.id
    assert top.category
    assert top.correction


def test_search_ranks_verbatim_correction_match_above_unrelated_entries():
    results = search("matching rollback script", FIXTURE)
    assert results, "expected at least one match"
    assert results[0].id == "sql-migration-missing-rollback-script"


def test_search_matches_on_id_terms():
    results = search("token", FIXTURE)
    ids = [m.id for m in results]
    assert "hardcoded-hex-bypasses-token" in ids


def test_search_is_case_insensitive():
    lower = [m.id for m in search("hardcoded color", FIXTURE)]
    upper = [m.id for m in search("HARDCODED COLOR", FIXTURE)]
    assert lower == upper
    assert "hardcoded-hex-bypasses-token" in lower


def test_search_respects_status_filter():
    results = search("no longer applies", FIXTURE, status="active")
    assert all(m.entry["status"] == "active" for m in results)
    assert "retired-example" not in [m.id for m in results]


def test_search_respects_top_k():
    # "reviewing" appears in both the migration and hardcoded-hex entries' trigger_context,
    # so an uncapped search would return 2+ matches -- top_k=1 must truncate to exactly 1.
    uncapped = search("reviewing", FIXTURE, top_k=10)
    assert len(uncapped) >= 2, "fixture setup assumption broken: expected 2+ matches for 'reviewing'"

    capped = search("reviewing", FIXTURE, top_k=1)
    assert len(capped) == 1


def test_search_scores_are_sorted_descending():
    results = search("token color hardcoded", FIXTURE)
    scores = [m.score for m in results]
    assert scores == sorted(scores, reverse=True)


def test_search_over_the_real_example_registry_finds_relevant_entries():
    with open(EXAMPLES / "example-registry.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    results = search("design token", data["entries"])
    assert results
    assert results[0].id == "hardcoded-hex-bypasses-token"
