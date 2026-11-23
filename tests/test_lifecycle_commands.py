"""
Lifecycle commands: the fail-loud reader and migrate-key, fire, retire, audit --recount,
split (with its verify step), inject --max, the optional fields, and compare.

Every write verb here must change only the lines it names. The tests check that by diffing
the file before and after, not only by re-parsing it: a re-parse cannot see a dropped comment
or a reflowed scalar.
"""

import subprocess
import sys
from pathlib import Path

import yaml

import corrections_cli

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "src" / "corrections_cli.py"


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )


def changed_lines(before: str, after: str):
    """(removed, added) lines between two texts, ignoring unchanged lines."""
    import difflib

    diff = list(difflib.ndiff(before.splitlines(), after.splitlines()))
    return [d[2:] for d in diff if d.startswith("- ")], [d[2:] for d in diff if d.startswith("+ ")]


ENTRY = """\
  - id: {id}
    category: process
    severity: {severity}
    trigger_context: When {id} is about to happen.
    correction: Do the {id} thing instead.
    status: {status}
    times_fired: {fired}
    last_fired: {last}
"""


def registry_text(*entries, meta=None, key="entries"):
    head = "# a hand-written header comment\n"
    if meta:
        head += "meta:\n" + "".join(f"  {k}: {v}\n" for k, v in meta.items())
    body = "".join(
        ENTRY.format(
            id=e.get("id", "x"),
            severity=e.get("severity", "factual"),
            status=e.get("status", "active"),
            fired=e.get("fired", 0),
            last=e.get("last", "null"),
        )
        + "".join(f"    {k}: {v}\n" for k, v in e.get("extra", {}).items())
        for e in entries
    )
    return head + f"{key}:\n" + body


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_bytes(text.encode("utf-8"))
    return p


# ── LB1.1: fail loud on a mis-keyed registry; migrate-key ──────────────────────


def test_a_registry_keyed_on_another_list_fails_loud_and_names_the_key(tmp_path):
    p = write(tmp_path, "r.yaml", "corrections:\n  - id: x\n")
    for command in (("audit",), ("inject",), ("search", "x")):
        result = run_cli(*command, "--registry", str(p))
        assert result.returncode != 0, command
        assert "`corrections:`" in result.stderr
        assert "migrate-key --from corrections" in result.stderr
        assert "0/0" not in result.stdout


def test_an_empty_entries_key_stays_valid(tmp_path):
    for text in ("entries:\n", "entries: []\n", "meta:\n  version: 1\nentries:\n"):
        result = run_cli("audit", "--registry", str(write(tmp_path, "e.yaml", text)))
        assert result.returncode == 0, (text, result.stderr)
        assert "Schema: 0/0 entries valid." in result.stdout


def test_a_meta_only_file_with_no_list_anywhere_is_still_an_empty_registry(tmp_path):
    result = run_cli("audit", "--registry", str(write(tmp_path, "m.yaml", "meta:\n  version: 1\n")))
    assert result.returncode == 0, result.stderr


def test_a_plain_config_list_beside_an_empty_entries_is_not_hidden_entries(tmp_path):
    # A list of strings is not a registry hiding under another key: the file stays an
    # empty registry, and `add` can still create its first entry.
    p = write(tmp_path, "c.yaml", "meta:\n  version: 1\ncategories: [design, code]\nentries: []\n")
    assert run_cli("audit", "--registry", str(p)).returncode == 0
    result = run_cli(
        "add", "--registry", str(p), "--id", "n1", "--category", "process", "--severity", "factual",
        "--trigger", "When n1 happens.", "--correction", "Do n1 instead.",
    )
    assert result.returncode == 0, result.stderr
    assert [e["id"] for e in yaml.safe_load(p.read_text(encoding="utf-8"))["entries"]] == ["n1"]


def test_a_duplicated_top_level_entries_key_fails_loud_and_writes_nothing(tmp_path):
    # A YAML parser keeps the last of two `entries:` keys; reading on would audit the file
    # as 0/0 valid, and a recount write or an `add` would drop the first list's entries.
    text = registry_text({"id": "a"}, meta={"total": 1}) + "entries: []\n"
    p = write(tmp_path, "d.yaml", text)
    for command in (("audit",), ("inject",), ("audit", "--recount", "--write")):
        result = run_cli(*command, "--registry", str(p))
        assert result.returncode != 0, command
        assert "appears more than once" in result.stderr
        assert "0/0" not in result.stdout
    assert p.read_text(encoding="utf-8") == text


def test_a_populated_entries_registry_audits_n_of_n(tmp_path):
    p = write(tmp_path, "p.yaml", registry_text({"id": "a"}, {"id": "b"}, {"id": "c"}))
    result = run_cli("audit", "--registry", str(p))
    assert result.returncode == 0, result.stderr
    assert "Schema: 3/3 entries valid." in result.stdout


def test_migrate_key_changes_exactly_one_line(tmp_path):
    original = registry_text({"id": "a"}, {"id": "b"}, meta={"active": 2}, key="corrections")
    p = write(tmp_path, "r.yaml", original)
    result = run_cli("migrate-key", "--from", "corrections", "--registry", str(p))
    assert result.returncode == 0, result.stderr
    removed, added = changed_lines(original, p.read_text(encoding="utf-8"))
    assert removed == ["corrections:"] and added == ["entries:"]
    assert run_cli("audit", "--registry", str(p)).returncode == 0


def test_migrate_key_keeps_crlf_line_endings_and_an_inline_comment(tmp_path):
    original = "meta:\r\n  active: 1\r\ncorrections:  # the old key\r\n  - id: x\r\n"
    p = write(tmp_path, "r.yaml", original)
    assert run_cli("migrate-key", "--from", "corrections", "--registry", str(p)).returncode == 0
    assert p.read_bytes() == original.replace("corrections:", "entries:").encode("utf-8")


def test_migrate_key_dry_run_writes_nothing(tmp_path):
    original = "corrections:\n  - id: x\n"
    p = write(tmp_path, "r.yaml", original)
    result = run_cli("migrate-key", "--from", "corrections", "--registry", str(p), "--dry-run")
    assert result.returncode == 0
    assert "+entries:" in result.stdout
    assert p.read_text(encoding="utf-8") == original


def test_migrate_key_refuses_when_entries_already_exists_or_the_key_is_missing(tmp_path):
    both = write(tmp_path, "both.yaml", "entries: []\ncorrections:\n  - id: x\n")
    assert run_cli("migrate-key", "--from", "corrections", "--registry", str(both)).returncode == 1
    neither = write(tmp_path, "neither.yaml", "items:\n  - id: x\n")
    result = run_cli("migrate-key", "--from", "corrections", "--registry", str(neither))
    assert result.returncode == 1
    assert "`items:`" in result.stderr
    assert run_cli("migrate-key", "--from", "entries", "--registry", str(neither)).returncode == 2


# ── LB1.2: fire ─────────────────────────────────────────────────────────────────


def test_fire_increments_the_counter_and_stamps_last_fired_on_the_same_entry(tmp_path):
    original = registry_text({"id": "a", "fired": 2, "last": "'2026-01-01'"}, {"id": "b"})
    p = write(tmp_path, "r.yaml", original)
    result = run_cli("fire", "a", "--registry", str(p), "--date", "2026-03-04")
    assert result.returncode == 0, result.stderr
    removed, added = changed_lines(original, p.read_text(encoding="utf-8"))
    assert removed == ["    times_fired: 2", "    last_fired: '2026-01-01'"]
    assert added == ["    times_fired: 3", "    last_fired: '2026-03-04'"]
    entries = yaml.safe_load(p.read_text(encoding="utf-8"))["entries"]
    assert len(entries) == 2, "fire must not append a sibling entry"


def test_fire_adds_missing_counter_fields_and_keeps_an_inline_comment(tmp_path):
    original = (
        "entries:\n"
        "- id: a\n  category: c\n  severity: factual\n  trigger_context: t\n"
        "  correction: c\n  status: active  # keep me\n"
    )
    p = write(tmp_path, "r.yaml", original)
    assert run_cli("fire", "a", "--registry", str(p), "--date", "2026-03-04").returncode == 0
    text = p.read_text(encoding="utf-8")
    assert "  status: active  # keep me\n" in text
    entry = yaml.safe_load(text)["entries"][0]
    assert entry["times_fired"] == 1 and entry["last_fired"] == "2026-03-04"


def test_fire_never_moves_last_fired_backwards(tmp_path):
    p = write(tmp_path, "r.yaml", registry_text({"id": "a", "fired": 1, "last": "'2026-05-01'"}))
    result = run_cli("fire", "a", "--registry", str(p), "--date", "2026-01-01")
    assert result.returncode == 0
    entry = yaml.safe_load(p.read_text(encoding="utf-8"))["entries"][0]
    assert entry["times_fired"] == 2 and entry["last_fired"] == "2026-05-01"


def test_fire_reports_a_promotion_candidate_and_audit_lists_it(tmp_path):
    p = write(tmp_path, "r.yaml", registry_text({"id": "a", "fired": 4, "last": "'2026-01-01'"}))
    result = run_cli("fire", "a", "--registry", str(p))
    assert "promotion candidate" in result.stdout
    audit = run_cli("audit", "--registry", str(p))
    assert "Promotion candidates (5+ fires): 1" in audit.stdout


def test_fire_on_an_unknown_id_or_bad_date_writes_nothing(tmp_path):
    original = registry_text({"id": "a"})
    p = write(tmp_path, "r.yaml", original)
    assert run_cli("fire", "nope", "--registry", str(p)).returncode == 1
    assert run_cli("fire", "a", "--registry", str(p), "--date", "soon").returncode == 2
    assert p.read_text(encoding="utf-8") == original


def test_fire_on_a_superseded_entry_says_the_rule_did_not_catch_it(tmp_path):
    p = write(
        tmp_path,
        "r.yaml",
        registry_text({"id": "a", "status": "superseded", "extra": {"superseded_by": "lint rule 7"}}),
    )
    result = run_cli("fire", "a", "--registry", str(p))
    assert result.returncode == 0
    assert "lint rule 7 did not catch it" in result.stdout


# ── LB1.3: audit --recount ─────────────────────────────────────────────────────


def test_recount_exits_nonzero_on_drift_and_write_fixes_only_the_count_lines(tmp_path):
    original = registry_text(
        {"id": "a"}, {"id": "b", "status": "retired"}, meta={"total_corrections": 5, "active": 2, "retired": 1}
    )
    p = write(tmp_path, "r.yaml", original)
    result = run_cli("audit", "--registry", str(p), "--recount")
    assert result.returncode == 1
    assert "DRIFT" in result.stdout and p.read_text(encoding="utf-8") == original

    result = run_cli("audit", "--registry", str(p), "--recount", "--write")
    assert result.returncode == 0, result.stdout + result.stderr
    removed, added = changed_lines(original, p.read_text(encoding="utf-8"))
    assert removed == ["  total_corrections: 5", "  active: 2"]
    assert added == ["  total_corrections: 2", "  active: 1"]
    assert run_cli("audit", "--registry", str(p), "--recount").returncode == 0


def test_recount_counts_an_archive_for_whole_registry_meta(tmp_path):
    live = write(tmp_path, "live.yaml", registry_text({"id": "a"}, meta={"total_corrections": 2, "active": 1, "retired": 1}))
    archive = write(tmp_path, "arch.yaml", registry_text({"id": "b", "status": "retired"}))
    assert run_cli("audit", "--registry", str(live), "--recount").returncode == 1
    assert run_cli("audit", "--registry", str(live), "--recount", "--archive", str(archive)).returncode == 0


def test_write_and_archive_need_recount(tmp_path):
    p = write(tmp_path, "r.yaml", registry_text({"id": "a"}))
    assert run_cli("audit", "--registry", str(p), "--write").returncode == 2
    assert run_cli("audit", "--registry", str(p), "--archive", str(p)).returncode == 2


def test_a_registry_that_states_no_counts_has_nothing_to_reconcile(tmp_path):
    result = run_cli("audit", "--registry", str(write(tmp_path, "r.yaml", registry_text({"id": "a"}))), "--recount")
    assert result.returncode == 0
    assert "states no counts" in result.stdout


# ── LB1.4: retire ───────────────────────────────────────────────────────────────


def test_retire_refuses_rather_than_drop_a_comment_on_a_multi_line_key_line(tmp_path):
    original = registry_text({"id": "b", "extra": {"superseded_by": "> # old note\n      something"}})
    p = write(tmp_path, "r.yaml", original)
    result = run_cli("retire", "b", "--registry", str(p), "--reason", "replaced",
                     "--superseded-by", "rules.md § Review", "--approve")
    assert result.returncode != 0
    assert "comment" in result.stderr
    assert p.read_text(encoding="utf-8") == original


def test_retire_closes_the_validity_window_and_keeps_the_entry(tmp_path):
    original = registry_text({"id": "a"}, {"id": "b"})
    p = write(tmp_path, "r.yaml", original)
    result = run_cli("retire", "a", "--registry", str(p), "--reason", "tool removed", "--date", "2026-02-02")
    assert result.returncode == 0, result.stderr
    removed, added = changed_lines(original, p.read_text(encoding="utf-8"))
    assert removed == ["    status: active"]
    assert added == ["    status: retired", "    valid_until: '2026-02-02'", "    retired_reason: tool removed"]
    entries = yaml.safe_load(p.read_text(encoding="utf-8"))["entries"]
    assert [e["id"] for e in entries] == ["a", "b"]
    assert "No active" not in run_cli("inject", "--registry", str(p)).stdout
    assert "* a " not in run_cli("inject", "--registry", str(p)).stdout


def test_retire_with_superseded_by_is_the_promotion_close_out(tmp_path):
    p = write(tmp_path, "r.yaml", registry_text({"id": "a"}))
    result = run_cli("retire", "a", "--registry", str(p), "--reason", "promoted", "--superseded-by", "rules.md § Review")
    assert result.returncode == 0, result.stderr
    entry = yaml.safe_load(p.read_text(encoding="utf-8"))["entries"][0]
    assert entry["status"] == "superseded" and entry["superseded_by"] == "rules.md § Review"
    assert run_cli("audit", "--registry", str(p)).returncode == 0


def test_retire_a_behavioral_entry_needs_approval(tmp_path):
    original = registry_text({"id": "a", "severity": "behavioral"})
    p = write(tmp_path, "r.yaml", original)
    result = run_cli("retire", "a", "--registry", str(p), "--reason", "r")
    assert result.returncode == 1 and "approval" in result.stderr
    assert p.read_text(encoding="utf-8") == original
    assert run_cli("retire", "a", "--registry", str(p), "--reason", "r", "--approve").returncode == 0


def test_retire_refuses_an_entry_that_is_already_closed(tmp_path):
    p = write(tmp_path, "r.yaml", registry_text({"id": "a", "status": "retired"}))
    assert run_cli("retire", "a", "--registry", str(p), "--reason", "again").returncode == 1


def test_an_active_entry_with_a_closed_window_is_a_schema_error():
    entry = {
        "id": "a", "category": "c", "severity": "factual", "trigger_context": "t",
        "correction": "c", "status": "active", "valid_until": "2026-01-01",
    }
    assert any("valid_until" in e for e in corrections_cli.validate_entry(entry))


# ── LB1.5: split + verify; inject --max ─────────────────────────────────────────


def test_split_moves_closed_entries_verbatim_into_a_new_archive(tmp_path):
    live_text = registry_text(
        {"id": "a"},
        {"id": "b", "status": "retired"},
        {"id": "c", "status": "superseded", "extra": {"superseded_by": "x.md"}},
        meta={"total_corrections": 3, "active": 1, "superseded": 1, "retired": 1},
    )
    live = write(tmp_path, "live.yaml", live_text)
    archive = tmp_path / "arch.yaml"
    result = run_cli("split", "--registry", str(live), "--archive", str(archive))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Split invariant OK" in result.stdout

    new_live = live.read_text(encoding="utf-8")
    arch = archive.read_text(encoding="utf-8")
    assert [e["id"] for e in yaml.safe_load(new_live)["entries"]] == ["a"]
    assert [e["id"] for e in yaml.safe_load(arch)["entries"]] == ["b", "c"]
    for moved in ("b", "c"):
        start = live_text.index(f"  - id: {moved}\n")
        end = live_text.find("  - id:", start + 1)
        assert live_text[start: end if end != -1 else None] in arch, f"{moved} did not move verbatim"
    assert new_live.startswith("# a hand-written header comment\n")
    assert yaml.safe_load(arch)["meta"]["total_corrections"] == 2
    assert run_cli("split", "--registry", str(live), "--archive", str(archive), "--verify").returncode == 0


def test_split_appends_to_an_existing_archive_and_updates_its_counts(tmp_path):
    live = write(tmp_path, "live.yaml", registry_text({"id": "a"}, {"id": "b", "status": "retired"}))
    archive = write(
        tmp_path, "arch.yaml", registry_text({"id": "old", "status": "retired"}, meta={"total_corrections": 1, "retired": 1})
    )
    assert run_cli("split", "--registry", str(live), "--archive", str(archive)).returncode == 0
    data = yaml.safe_load(archive.read_text(encoding="utf-8"))
    assert [e["id"] for e in data["entries"]] == ["old", "b"]
    assert data["meta"] == {"total_corrections": 2, "retired": 2}


def test_split_check_writes_nothing_and_a_clash_refuses(tmp_path):
    live_text = registry_text({"id": "a"}, {"id": "b", "status": "retired"})
    live = write(tmp_path, "live.yaml", live_text)
    archive = tmp_path / "arch.yaml"
    result = run_cli("split", "--registry", str(live), "--archive", str(archive), "--check")
    assert result.returncode == 0 and "Nothing written" in result.stdout
    assert live.read_text(encoding="utf-8") == live_text and not archive.exists()

    write(tmp_path, "arch.yaml", registry_text({"id": "b", "status": "retired"}))
    assert run_cli("split", "--registry", str(live), "--archive", str(archive)).returncode == 1
    assert live.read_text(encoding="utf-8") == live_text


def test_split_verify_flags_misplaced_entries_and_count_drift(tmp_path):
    live = write(tmp_path, "live.yaml", registry_text({"id": "a"}, {"id": "b", "status": "retired"}, meta={"active": 3}))
    archive = write(tmp_path, "arch.yaml", registry_text({"id": "a", "status": "retired"}))
    result = run_cli("split", "--registry", str(live), "--archive", str(archive), "--verify")
    assert result.returncode == 1
    assert "BOTH files" in result.stdout and "non-active" in result.stdout
    assert "live meta.active" in result.stdout


def test_inject_max_caps_the_block_behavioral_first(tmp_path):
    p = write(
        tmp_path,
        "r.yaml",
        registry_text(
            {"id": "fact-often", "fired": 9, "last": "'2026-01-01'"},
            {"id": "behav-once", "severity": "behavioral", "fired": 1, "last": "'2026-01-01'"},
            {"id": "fact-never"},
        ),
    )
    result = run_cli("inject", "--registry", str(p), "--max", "2")
    assert result.returncode == 0
    out = result.stdout
    assert out.index("behav-once") < out.index("fact-often")
    assert "fact-never" not in out
    assert "2 of 3 active corrections shown" in out
    assert "None" not in out
    assert run_cli("inject", "--registry", str(p), "--max", "0").returncode == 2


def test_inject_without_max_keeps_registry_order_and_adds_no_note(tmp_path):
    p = write(tmp_path, "r.yaml", registry_text({"id": "z-first"}, {"id": "a-second", "severity": "behavioral"}))
    out = run_cli("inject", "--registry", str(p)).stdout
    assert out.index("z-first") < out.index("a-second")
    assert "shown;" not in out


# ── LB1.6: optional fields, assumes_rule, supersedes ────────────────────────────


def _valid(**extra):
    entry = {
        "id": "a", "category": "c", "severity": "factual", "trigger_context": "t",
        "correction": "c", "status": "active",
    }
    entry.update(extra)
    return entry


def test_optional_fields_validate_by_type():
    assert corrections_cli.validate_entry(_valid(mistake="m", why="w", supersedes="b", assumes_rule="r.md")) == []
    assert corrections_cli.validate_entry(_valid(supersedes=["b", "c"])) == []
    assert any("mistake" in e for e in corrections_cli.validate_entry(_valid(mistake=3)))
    assert any("supersedes" in e for e in corrections_cli.validate_entry(_valid(supersedes=[1])))
    assert any("itself" in e for e in corrections_cli.validate_entry(_valid(supersedes="a")))


def test_audit_flags_a_superseded_seed_whose_assumed_rule_is_absent(tmp_path):
    p = write(
        tmp_path,
        "r.yaml",
        registry_text(
            {"id": "s", "status": "superseded", "extra": {"superseded_by": "rules.md", "assumes_rule": '"os/rules.md § Verify"'}},
        ),
    )
    result = run_cli("audit", "--registry", str(p), "--rules-root", str(tmp_path))
    assert result.returncode == 0
    assert "assumed rule is absent" in result.stdout and "[s] superseded" in result.stdout

    (tmp_path / "os").mkdir()
    (tmp_path / "os" / "rules.md").write_text("# Rules\n\n## Verify\n", encoding="utf-8")
    result = run_cli("audit", "--registry", str(p), "--rules-root", str(tmp_path))
    assert "assumed rule is absent (rules root: " in result.stdout
    assert "[s] superseded" not in result.stdout


def test_audit_says_when_an_assumed_rule_arrived_for_an_active_seed(tmp_path):
    (tmp_path / "rules.md").write_text("## Verify\n", encoding="utf-8")
    p = write(tmp_path, "r.yaml", registry_text({"id": "s", "extra": {"assumes_rule": '"rules.md § Verify"'}}))
    result = run_cli("audit", "--registry", str(p), "--rules-root", str(tmp_path))
    assert "review for supersession): 1" in result.stdout


def test_audit_lists_a_corrected_claim_that_is_still_active(tmp_path):
    p = write(tmp_path, "r.yaml", registry_text({"id": "old"}, {"id": "new", "extra": {"supersedes": "old"}}))
    result = run_cli("audit", "--registry", str(p))
    assert result.returncode == 0
    assert "Corrected claims still active: 1" in result.stdout
    assert "retire old" in result.stdout


def test_a_registry_without_the_new_fields_prints_none_of_the_new_sections(tmp_path):
    p = write(tmp_path, "r.yaml", registry_text({"id": "a"}))
    out = run_cli("audit", "--registry", str(p)).stdout
    assert "assumed rule" not in out and "Corrected claims" not in out


# ── LB1.7: compare ──────────────────────────────────────────────────────────────


def test_compare_reports_overlapping_triggers_and_shared_ids_and_exits_zero(tmp_path):
    a = write(
        tmp_path,
        "a.yaml",
        "entries:\n"
        "  - id: one\n    trigger_context: Writing database migration scripts without rollback steps\n"
        "  - id: same\n    trigger_context: Unrelated formatting question about headings\n",
    )
    b = write(
        tmp_path,
        "b.yaml",
        "entries:\n"
        "  - id: uno\n    trigger_context: A database migration script missing its rollback steps\n"
        "  - id: same\n    trigger_context: Something else entirely different here\n",
    )
    result = run_cli("compare", str(a), str(b))
    assert result.returncode == 0, result.stderr
    assert "[one]" in result.stdout and "[uno]" in result.stdout
    assert "Same id in both registries: 1" in result.stdout


def test_compare_a_registry_with_itself_reports_no_self_pairs(tmp_path):
    a = write(tmp_path, "a.yaml", "entries:\n  - id: one\n    trigger_context: database migration rollback steps\n")
    result = run_cli("compare", str(a), str(a))
    assert result.returncode == 0
    assert "Likely duplicate lessons (trigger overlap >= 0.30): 0" in result.stdout


def test_compare_fails_loud_on_a_mis_keyed_registry(tmp_path):
    a = write(tmp_path, "a.yaml", "entries: []\n")
    b = write(tmp_path, "b.yaml", "corrections:\n  - id: x\n")
    assert run_cli("compare", str(a), str(b)).returncode == 1


def test_every_new_subcommand_has_help():
    for command in ("fire", "retire", "migrate-key", "split", "compare"):
        result = run_cli(command, "--help")
        assert result.returncode == 0, command
        assert result.stdout.strip()


# ── regressions from the adversarial pass ─────────────────────────────────────


def test_an_empty_entries_key_beside_a_populated_list_fails_loud(tmp_path):
    p = write(tmp_path, "half.yaml", "entries:\ncorrections:\n  - id: x\n    category: c\n")
    result = run_cli("audit", "--registry", str(p))
    assert result.returncode == 1
    assert "`entries:` is empty" in result.stderr and "`corrections:`" in result.stderr
    assert "No active" not in run_cli("inject", "--registry", str(p)).stdout


def test_entries_hidden_in_a_mapping_fail_loud(tmp_path):
    p = write(tmp_path, "map.yaml", "corrections:\n  a:\n    id: a\n    correction: c\n")
    result = run_cli("audit", "--registry", str(p))
    assert result.returncode == 1
    assert "mapping" in result.stderr and "convert" in result.stderr


def test_an_empty_unrelated_list_is_not_mistaken_for_hidden_entries(tmp_path):
    p = write(tmp_path, "e.yaml", "meta:\n  v: 1\nexamples: []\n")
    result = run_cli("audit", "--registry", str(p))
    assert result.returncode == 0, result.stderr


def test_recount_write_will_not_overwrite_whole_registry_counts_with_an_unnamed_archive(tmp_path):
    live_text = registry_text({"id": "a"}, meta={"total_corrections": 2, "active": 1, "retired": 1})
    live = write(tmp_path, "reg.yaml", live_text)
    write(tmp_path, "reg-archive.yaml", registry_text({"id": "b", "status": "retired"}))
    result = run_cli("audit", "--registry", str(live), "--recount", "--write")
    assert result.returncode == 1
    assert "--archive" in result.stdout and "Nothing was written" in result.stderr
    assert live.read_text(encoding="utf-8") == live_text
    named = run_cli("audit", "--registry", str(live), "--recount", "--archive", str(tmp_path / "reg-archive.yaml"))
    assert named.returncode == 0, named.stdout
    live_only = run_cli("audit", "--registry", str(live), "--recount", "--write", "--live-only")
    assert live_only.returncode == 0
    assert yaml.safe_load(live.read_text(encoding="utf-8"))["meta"]["total_corrections"] == 1


def test_recount_rejects_the_registry_as_its_own_archive(tmp_path):
    p = write(tmp_path, "r.yaml", registry_text({"id": "a"}, meta={"active": 1}))
    assert run_cli("audit", "--registry", str(p), "--recount", "--archive", str(p)).returncode == 2
    assert run_cli("audit", "--registry", str(p), "--recount", "--archive", str(p), "--live-only").returncode == 2


def test_split_refuses_a_live_file_that_repeats_an_id(tmp_path):
    live_text = registry_text({"id": "a"}, {"id": "a", "status": "retired"})
    live = write(tmp_path, "live.yaml", live_text)
    archive = tmp_path / "arch.yaml"
    result = run_cli("split", "--registry", str(live), "--archive", str(archive))
    assert result.returncode == 1 and "repeats id" in result.stderr
    assert live.read_text(encoding="utf-8") == live_text and not archive.exists()


def test_split_verify_does_not_accept_non_integer_counts(tmp_path):
    live = write(tmp_path, "live.yaml", registry_text({"id": "a"}, meta={"total": "true", "active": 1.0}))
    archive = write(tmp_path, "arch.yaml", "entries: []\n")
    result = run_cli("split", "--registry", str(live), "--archive", str(archive), "--verify")
    assert result.returncode == 1
    assert "meta.total" in result.stdout and "meta.active" in result.stdout


def test_split_writes_moved_entries_in_the_archive_line_endings(tmp_path):
    live = write(tmp_path, "live.yaml", registry_text({"id": "a"}, {"id": "b", "status": "retired"}).replace("\n", "\r\n"))
    archive = write(tmp_path, "arch.yaml", registry_text({"id": "old", "status": "retired"}))
    result = run_cli("split", "--registry", str(live), "--archive", str(archive))
    assert result.returncode == 0, result.stderr
    assert b"\r\n" not in archive.read_bytes()
    assert "line endings matched" in result.stdout
    assert b"\r\n" in live.read_bytes()


def test_split_defaults_the_archive_beside_the_registry(tmp_path, monkeypatch):
    live = write(tmp_path, "reg.yaml", registry_text({"id": "a"}, {"id": "b", "status": "retired"}))
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    result = subprocess.run(
        [sys.executable, str(CLI), "split", "--registry", str(live)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30, cwd=str(elsewhere),
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "reg-archive.yaml").is_file()
    assert not list(elsewhere.iterdir())


def test_fire_refuses_rather_than_delete_a_comment_inside_a_field(tmp_path):
    original = (
        "entries:\n"
        "  - id: a\n    category: c\n    severity: factual\n    trigger_context: t\n"
        "    correction: c\n    status: active\n    times_fired: 3\n      # why it is three\n"
        "    last_fired: '2026-01-01'\n"
    )
    p = write(tmp_path, "r.yaml", original)
    result = run_cli("fire", "a", "--registry", str(p))
    assert result.returncode == 1 and "comment" in result.stderr
    assert p.read_text(encoding="utf-8") == original


def test_a_byte_order_mark_is_matched_past_and_kept(tmp_path):
    original = "\ufeffcorrections:\n  - id: x\n    category: c\n    severity: factual\n    trigger_context: t\n    correction: c\n    status: active\n"
    p = write(tmp_path, "bom.yaml", original)
    assert run_cli("migrate-key", "--from", "corrections", "--registry", str(p)).returncode == 0
    assert p.read_bytes() == original.replace("corrections:", "entries:").encode("utf-8")
    result = run_cli("fire", "x", "--registry", str(p), "--date", "2026-02-02")
    assert result.returncode == 0, result.stderr
    assert p.read_bytes().startswith("\ufeffentries:".encode("utf-8"))
    assert yaml.safe_load(p.read_text(encoding="utf-8-sig"))["entries"][0]["times_fired"] == 1
