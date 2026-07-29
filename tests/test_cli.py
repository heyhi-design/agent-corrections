"""
CLI tests: subprocess the four commands (search, add, audit, inject) exactly as a
user on the command line would invoke them, against the bundled example registry.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import corrections_cli

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "src" / "corrections_cli.py"
EXAMPLE_REGISTRY = REPO / "examples" / "example-registry.yaml"


def _example_entry_count() -> int:
    # Computed, not hardcoded: a contributor adding an entry per CONTRIBUTING.md
    # shouldn't have to also go find and update an unrelated magic number here.
    with open(EXAMPLE_REGISTRY, encoding="utf-8") as f:
        return len(yaml.safe_load(f)["entries"])


def run_cli(*args, stdin_devnull=True):
    cmd = [sys.executable, str(CLI), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL if stdin_devnull else None,
        timeout=30,
    )


# ── search ───────────────────────────────────────────────────────────────────


def test_search_command_finds_a_matching_entry():
    result = run_cli("search", "design token", "--registry", str(EXAMPLE_REGISTRY))
    assert result.returncode == 0, result.stderr
    assert "hardcoded-hex-bypasses-token" in result.stdout


def test_search_command_reports_no_matches_cleanly():
    result = run_cli("search", "zzzznonexistentqueryzzzz", "--registry", str(EXAMPLE_REGISTRY))
    assert result.returncode == 0, result.stderr
    assert "No matches" in result.stdout


def test_search_command_errors_on_missing_registry_file():
    result = run_cli("search", "token", "--registry", "/nonexistent-path/does-not-exist.yaml")
    assert result.returncode == 1
    # Assert on the filename only. The CLI renders the path through Path(), which
    # prints POSIX-style input with backslash separators on Windows -- the full
    # "/nonexistent-path/..." literal is not a substring there, but the basename is.
    assert "does-not-exist.yaml" in result.stderr


# ── audit ────────────────────────────────────────────────────────────────────


def test_audit_command_is_schema_valid_and_exits_zero():
    result = run_cli("audit", "--registry", str(EXAMPLE_REGISTRY))
    assert result.returncode == 0, result.stderr
    n = _example_entry_count()
    assert f"{n}/{n} entries valid" in result.stdout


def test_audit_command_surfaces_stale_and_promotion_candidates_with_fixed_as_of():
    result = run_cli("audit", "--registry", str(EXAMPLE_REGISTRY), "--as-of", "2026-07-10")
    assert result.returncode == 0, result.stderr
    assert "readme-badge-links-to-wrong-branch" in result.stdout  # stale: last fired 2026-01-15
    assert "test-fixture-uses-production-api-key-env-var" in result.stdout  # 5+ fires, promotion candidate


def test_audit_command_frames_staleness_as_review_due_not_retire():
    # The heading and its explanatory line read as "review this," never as "delete
    # this" -- a corrections registry accumulates durable environment facts that
    # are correct precisely because nothing has re-triggered them lately, and a
    # "stale" label reads as a removal prompt to a reader skimming the report.
    result = run_cli("audit", "--registry", str(EXAMPLE_REGISTRY), "--as-of", "2026-07-10")
    assert result.returncode == 0, result.stderr
    assert "Review-due entries" in result.stdout
    assert "do not reflexively retire" in result.stdout
    assert "Stale entries" not in result.stdout


def test_audit_command_quotes_a_promotion_clause_when_the_entry_has_one(tmp_path):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "entries:\n"
        "  - id: with-clause\n"
        "    category: architectural\n"
        "    severity: factual\n"
        "    trigger_context: when testing\n"
        "    correction: >-\n"
        "      Do the right thing. Promotion clause: 3 fires promotes this to a\n"
        "      CI lint rule.\n"
        "    status: active\n"
        "    times_fired: 5\n"
        "    last_fired: '2026-07-01'\n",
        encoding="utf-8",
    )
    result = run_cli("audit", "--registry", str(registry), "--as-of", "2026-07-10")
    assert result.returncode == 0, result.stderr
    assert "Promotion clause:" in result.stdout
    assert "3 fires promotes this to a CI lint rule." in result.stdout


def test_audit_command_promotion_candidate_without_a_clause_degrades_gracefully(tmp_path):
    # No entry field carries a "Promotion clause:" sentence -- the candidate still
    # reports cleanly, just without the extra line. No crash, no "None" leaking in,
    # no "Promotion clause:" heading with nothing after it.
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "entries:\n"
        "  - id: no-clause\n"
        "    category: architectural\n"
        "    severity: factual\n"
        "    trigger_context: when testing\n"
        "    correction: do the right thing\n"
        "    status: active\n"
        "    times_fired: 5\n"
        "    last_fired: '2026-07-01'\n",
        encoding="utf-8",
    )
    result = run_cli("audit", "--registry", str(registry), "--as-of", "2026-07-10")
    assert result.returncode == 0, result.stderr
    assert "[no-clause] fired 5 times, last 2026-07-01" in result.stdout
    assert "Promotion clause:" not in result.stdout


def test_audit_command_errors_on_missing_registry_file():
    # A missing --registry path is a typo to surface, not an empty registry to pass
    # silently -- audit must fail loudly and name the path, not report a misleadingly
    # clean "0/0 entries valid".
    result = run_cli("audit", "--registry", "/nonexistent-path/does-not-exist.yaml")
    assert result.returncode == 1
    # Assert on the filename only. The CLI renders the path through Path(), which
    # prints POSIX-style input with backslash separators on Windows -- the full
    # "/nonexistent-path/..." literal is not a substring there, but the basename is.
    assert "does-not-exist.yaml" in result.stderr


def test_audit_command_reports_yaml_syntax_error_cleanly(tmp_path):
    bad = tmp_path / "broken.yaml"
    bad.write_text("entries: [\n  unclosed\n", encoding="utf-8")
    result = run_cli("audit", "--registry", str(bad))
    assert result.returncode == 1
    assert "YAML syntax error" in result.stderr
    assert str(bad) in result.stderr
    assert "Traceback" not in result.stderr


def test_audit_command_reports_non_mapping_top_level(tmp_path):
    bad = tmp_path / "not-a-mapping.yaml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    result = run_cli("audit", "--registry", str(bad))
    assert result.returncode == 1
    assert "mapping" in result.stderr
    assert "Traceback" not in result.stderr


def test_audit_command_reports_entries_field_not_a_list(tmp_path):
    bad = tmp_path / "entries-not-a-list.yaml"
    bad.write_text("entries: not-a-list\n", encoding="utf-8")
    result = run_cli("audit", "--registry", str(bad))
    assert result.returncode == 1
    assert "must be a list" in result.stderr
    assert "Traceback" not in result.stderr


def test_audit_command_reports_entries_field_that_is_a_falsy_non_list(tmp_path):
    # `entries: {}` (also `""`, `0`, `false`) is a malformed registry, not an empty
    # one. Pre-fix, `data.get("entries") or []` coerced every falsy non-list to [],
    # so a broken file audited as a clean "0/0 entries valid" and the mistake stayed
    # invisible. Absent / null / empty-list remain valid; present-but-wrong-type is
    # now refused by name.
    bad = tmp_path / "entries-empty-mapping.yaml"
    bad.write_text("entries: {}\n", encoding="utf-8")
    result = run_cli("audit", "--registry", str(bad))
    assert result.returncode == 1
    assert "must be a list" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_non_mapping_entry_is_reported_cleanly_by_every_command(tmp_path):
    # A bare string (or list) item inside `entries` -- `- just-a-string` -- would reach
    # an `entry.get(...)` call as an AttributeError traceback in audit, inject, and
    # search alike. load_registry rejects it up front, so all three fail cleanly.
    bad = tmp_path / "non-mapping-entry.yaml"
    bad.write_text("entries:\n  - just-a-string\n", encoding="utf-8")
    for invocation in (
        ("audit", "--registry", str(bad)),
        ("inject", "--registry", str(bad)),
        ("search", "token", "--registry", str(bad)),
    ):
        result = run_cli(*invocation)
        assert result.returncode == 1, f"{invocation[0]}: {result.stderr}"
        assert "not a mapping" in result.stderr, f"{invocation[0]}: {result.stderr!r}"
        assert "Traceback" not in result.stderr, f"{invocation[0]}: {result.stderr!r}"


def test_audit_command_reports_a_non_utf8_registry_cleanly(tmp_path):
    # A registry saved in a non-UTF-8 encoding raises UnicodeDecodeError on read. It's
    # a ValueError, not an OSError or a yaml.YAMLError, so pre-fix it escaped both of
    # load_registry's handlers as a raw traceback. write_bytes plants genuinely invalid
    # UTF-8 (0xff is never a valid UTF-8 byte) -- write_text could not produce this.
    bad = tmp_path / "non-utf8.yaml"
    bad.write_bytes(b"entries:\n  - id: x\n    correction: \xff\xfe not utf-8\n")
    result = run_cli("audit", "--registry", str(bad))
    assert result.returncode == 1
    assert "UTF-8" in result.stderr
    assert str(bad) in result.stderr
    assert "Traceback" not in result.stderr


def test_audit_command_reports_registry_path_that_is_a_directory_cleanly(tmp_path):
    # --registry pointing at a directory (a plausible typo -- passing the project
    # root instead of the yaml file inside it) must fail with a clean message, not
    # an IsADirectoryError traceback.
    a_directory = tmp_path / "not-a-file"
    a_directory.mkdir()
    result = run_cli("audit", "--registry", str(a_directory))
    assert result.returncode == 1
    assert str(a_directory) in result.stderr
    assert "Traceback" not in result.stderr


def test_audit_command_handles_a_full_datetime_in_last_fired(tmp_path):
    # A bare `last_fired: 2026-07-01 10:00:00` (a timestamp, not a date-only value)
    # parses via YAML as a datetime, not a date -- audit must coerce it rather than
    # crash on date-vs-datetime subtraction.
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "entries:\n"
        "  - id: datetime-last-fired\n"
        "    category: factual\n"
        "    severity: factual\n"
        "    trigger_context: when testing\n"
        "    correction: do the right thing\n"
        "    status: active\n"
        "    times_fired: 1\n"
        "    last_fired: 2026-07-01 10:00:00\n",
        encoding="utf-8",
    )
    result = run_cli("audit", "--registry", str(registry), "--as-of", "2026-07-10")
    assert result.returncode == 0, result.stderr
    assert "1/1 entries valid" in result.stdout
    assert "Traceback" not in result.stderr


def test_audit_as_of_with_invalid_date_is_a_usage_error():
    result = run_cli("audit", "--registry", str(EXAMPLE_REGISTRY), "--as-of", "07/10/2026")
    assert result.returncode == 2
    assert "07/10/2026" in result.stderr
    assert "Traceback" not in result.stderr
    # The flag is validated before any work, so a bad --as-of must not emit the
    # schema report first. Empty stdout is what distinguishes this from the
    # pre-fix ordering, where "Schema: N/N entries valid." printed before the error.
    assert result.stdout == ""


# ── inject ───────────────────────────────────────────────────────────────────


def test_inject_claude_format_has_no_yaml_artifacts_or_none_strings():
    result = run_cli("inject", "--format", "claude", "--registry", str(EXAMPLE_REGISTRY))
    assert result.returncode == 0, result.stderr
    assert "\nNone\n" not in result.stdout
    assert " None " not in result.stdout
    assert "- id:" not in result.stdout  # no raw YAML syntax leaking through
    assert "plan-prior-art-blindness" in result.stdout


def test_inject_openai_format_produces_output():
    result = run_cli("inject", "--format", "openai", "--registry", str(EXAMPLE_REGISTRY))
    assert result.returncode == 0, result.stderr
    assert "Corrections" in result.stdout
    assert "plan-prior-art-blindness" in result.stdout


def test_inject_generic_format_produces_output():
    result = run_cli("inject", "--format", "generic", "--registry", str(EXAMPLE_REGISTRY))
    assert result.returncode == 0, result.stderr
    assert "CORRECTIONS" in result.stdout


def test_inject_only_includes_active_entries_by_default():
    result = run_cli("inject", "--format", "generic", "--registry", str(EXAMPLE_REGISTRY))
    assert result.returncode == 0, result.stderr
    # 'commit-message-missing-ticket-ref' is status: superseded and
    # 'python2-print-statement-syntax' is status: retired — neither should be injected
    assert "commit-message-missing-ticket-ref" not in result.stdout
    assert "python2-print-statement-syntax" not in result.stdout


def test_inject_command_errors_on_missing_registry_file():
    result = run_cli("inject", "--registry", "/nonexistent-path/does-not-exist.yaml")
    assert result.returncode == 1
    # Assert on the filename only. The CLI renders the path through Path(), which
    # prints POSIX-style input with backslash separators on Windows -- the full
    # "/nonexistent-path/..." literal is not a substring there, but the basename is.
    assert "does-not-exist.yaml" in result.stderr


def test_inject_inactive_status_header_does_not_instruct_applying():
    # 'python2-print-statement-syntax' is retired; 'commit-message-missing-ticket-ref'
    # is superseded. Neither is live guidance, so the header must be the inactive
    # variant, not the default (active) "apply these" header. Checking the exact
    # header text -- not a bare "apply" substring search -- because the inactive
    # header's own wording ("do not apply") and an entry body's own field content
    # ("When this applies:") both legitimately contain that substring.
    for status in ("retired", "superseded"):
        result = run_cli("inject", "--format", "claude", "--registry", str(EXAMPLE_REGISTRY), "--status", status)
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith(corrections_cli._INJECT_HEADERS_INACTIVE["claude"]), (
            f"--status {status} did not emit the inactive header: {result.stdout!r}"
        )
        assert not result.stdout.startswith(corrections_cli._INJECT_HEADERS["claude"]), (
            f"--status {status} emitted the active 'apply these' header: {result.stdout!r}"
        )


# ── add ──────────────────────────────────────────────────────────────────────


def _tmp_registry(tmp_path: Path) -> Path:
    dest = tmp_path / "registry.yaml"
    shutil.copy(EXAMPLE_REGISTRY, dest)
    return dest


def test_add_dry_run_does_not_modify_the_file(tmp_path):
    registry = _tmp_registry(tmp_path)
    before = registry.read_text(encoding="utf-8")

    result = run_cli(
        "add",
        "--registry", str(registry),
        "--id", "dry-run-only",
        "--category", "factual",
        "--severity", "factual",
        "--trigger-context", "when testing dry run",
        "--correction", "do not actually write anything",
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    assert "dry run" in result.stdout.lower()
    assert registry.read_text(encoding="utf-8") == before


def test_add_factual_entry_is_written_and_searchable(tmp_path):
    registry = _tmp_registry(tmp_path)

    result = run_cli(
        "add",
        "--registry", str(registry),
        "--id", "new-factual-entry",
        "--category", "factual",
        "--severity", "factual",
        "--trigger-context", "when testing add",
        "--correction", "this entry was added by a test",
    )
    assert result.returncode == 0, result.stderr

    search_result = run_cli("search", "added by a test", "--registry", str(registry))
    assert "new-factual-entry" in search_result.stdout


def test_add_creates_registry_file_when_missing_and_writes_the_entry(tmp_path):
    # `add` is the one command that must NOT error on a missing --registry path -- it's
    # the bootstrap flow for a brand-new registry (a fresh clone has no
    # corrections-registry.yaml yet). This is the one call site load_registry's
    # create_if_missing=True must keep working.
    registry = tmp_path / "corrections-registry.yaml"
    assert not registry.exists()

    result = run_cli(
        "add",
        "--registry", str(registry),
        "--id", "bootstrap-entry",
        "--category", "factual",
        "--severity", "factual",
        "--trigger-context", "when testing bootstrap creation",
        "--correction", "this entry created the registry file",
    )
    assert result.returncode == 0, result.stderr
    assert registry.exists()

    search_result = run_cli("search", "created the registry file", "--registry", str(registry))
    assert "bootstrap-entry" in search_result.stdout


def test_add_behavioral_without_approval_is_refused_noninteractively(tmp_path):
    registry = _tmp_registry(tmp_path)
    before = registry.read_text(encoding="utf-8")

    result = run_cli(
        "add",
        "--registry", str(registry),
        "--id", "should-not-be-added",
        "--category", "constraint",
        "--severity", "behavioral",
        "--trigger-context", "when testing the approval gate",
        "--correction", "this should require approval",
    )
    assert result.returncode == 1
    assert "approval" in result.stderr.lower() or "approve" in result.stderr.lower()
    assert registry.read_text(encoding="utf-8") == before


def test_add_behavioral_refuses_when_the_prompt_gets_no_input(tmp_path, monkeypatch, capsys):
    """
    The approval gate must refuse cleanly when stdin claims to be interactive but yields
    nothing -- not crash with an unhandled EOFError.

    This is not hypothetical and it is not only about odd terminals. On Windows, the null
    device is a CHARACTER device, so `isatty()` answers True for stdin redirected to NUL.
    The test above therefore takes the non-interactive branch on Linux and the *prompt*
    branch on Windows, where `input()` immediately hits EOF. That divergence went unseen
    until the suite was first run on a Windows runner: the traceback exits 1, so the
    assertion on the return code passed and only the stderr assertion caught it.

    Driving it through monkeypatch rather than a subprocess makes the guard platform
    independent -- it now fails on Linux too if the handler is removed, instead of waiting
    for someone to run the Windows leg.
    """
    registry = _tmp_registry(tmp_path)
    before = registry.read_text(encoding="utf-8")

    monkeypatch.setattr(corrections_cli.sys.stdin, "isatty", lambda: True, raising=False)

    def _eof(*_args, **_kwargs):
        raise EOFError("EOF when reading a line")

    monkeypatch.setattr("builtins.input", _eof)

    code = corrections_cli.main([
        "add",
        "--registry", str(registry),
        "--id", "should-not-be-added",
        "--category", "constraint",
        "--severity", "behavioral",
        "--trigger-context", "when the prompt gets no input",
        "--correction", "this should require approval",
    ])

    captured = capsys.readouterr()
    assert code == 1
    assert "approval" in captured.err.lower() or "approve" in captured.err.lower()
    assert registry.read_text(encoding="utf-8") == before


def test_add_behavioral_with_approve_flag_succeeds(tmp_path):
    registry = _tmp_registry(tmp_path)

    result = run_cli(
        "add",
        "--registry", str(registry),
        "--id", "approved-behavioral-entry",
        "--category", "constraint",
        "--severity", "behavioral",
        "--trigger-context", "when testing the approval gate with --approve",
        "--correction", "this was explicitly approved",
        "--approve",
    )
    assert result.returncode == 0, result.stderr

    search_result = run_cli("search", "explicitly approved", "--registry", str(registry))
    assert "approved-behavioral-entry" in search_result.stdout


def test_add_missing_required_flag_fails_clearly(tmp_path):
    registry = _tmp_registry(tmp_path)
    result = run_cli(
        "add",
        "--registry", str(registry),
        "--id", "incomplete-entry",
        "--category", "factual",
        "--severity", "factual",
        # --trigger-context and --correction omitted on purpose
    )
    assert result.returncode == 1
    assert "--trigger-context" in result.stderr
    assert "--correction" in result.stderr


def test_add_duplicate_id_is_rejected(tmp_path):
    registry = _tmp_registry(tmp_path)
    result = run_cli(
        "add",
        "--registry", str(registry),
        "--id", "plan-prior-art-blindness",  # already present in the example registry
        "--category", "factual",
        "--severity", "factual",
        "--trigger-context", "duplicate attempt",
        "--correction", "should be rejected",
    )
    assert result.returncode == 1
    assert "already exists" in result.stderr


# ── save_registry atomicity ─────────────────────────────────────────────────


def test_save_registry_does_not_corrupt_the_original_file_on_a_failed_write(tmp_path, monkeypatch):
    registry = _tmp_registry(tmp_path)
    before = registry.read_text(encoding="utf-8")
    data = corrections_cli.load_registry(registry)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(corrections_cli.yaml, "safe_dump", _boom)

    with pytest.raises(RuntimeError):
        corrections_cli.save_registry(registry, data)

    # The original file must survive a write that dies partway through, and the
    # temp file used for the atomic swap must not be left behind.
    assert registry.read_text(encoding="utf-8") == before
    leftovers = list(tmp_path.glob(f".{registry.name}.tmp-*"))
    assert leftovers == [], f"temp file(s) left behind after a failed write: {leftovers}"


def test_save_registry_replace_is_the_last_step(tmp_path):
    # A direct, non-mocked exercise of the atomic path: after a real save, no
    # temp file remains and the target holds the new content.
    registry = _tmp_registry(tmp_path)
    data = corrections_cli.load_registry(registry)
    data["entries"].append(
        {
            "id": "atomic-write-check",
            "category": "factual",
            "severity": "factual",
            "trigger_context": "when testing",
            "correction": "the write completed",
            "status": "active",
            "times_fired": 0,
            "last_fired": None,
        }
    )

    corrections_cli.save_registry(registry, data)

    leftovers = list(tmp_path.glob(f".{registry.name}.tmp-*"))
    assert leftovers == []
    reloaded = corrections_cli.load_registry(registry)
    assert any(e["id"] == "atomic-write-check" for e in reloaded["entries"])


# ── encoding robustness ─────────────────────────────────────────────────────


def test_cli_survives_a_non_utf8_output_encoding(tmp_path):
    # Simulates the Windows-default-codepage crash (F-AC-08) on any platform: force
    # stdout to start life as cp1252 via PYTHONIOENCODING, and confirm main()'s
    # guarded reconfigure to UTF-8 lets genuinely non-ASCII field content print
    # instead of raising UnicodeEncodeError. The registry fixture below carries a
    # real non-ASCII character for exactly this reason -- the bundled example
    # registry is plain ASCII and would pass this test even without the fix.
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "entries:\n"
        "  - id: non-ascii-entry\n"
        "    category: factual\n"
        "    severity: factual\n"
        "    trigger_context: when testing unicode output\n"
        "    correction: \"use an em dash \\u2014 and a smart quote \\u2018like this\\u2019\"\n"
        "    status: active\n"
        "    times_fired: 0\n"
        "    last_fired: null\n",
        encoding="utf-8",
    )

    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    result = subprocess.run(
        [sys.executable, str(CLI), "search", "unicode", "--registry", str(registry)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "non-ascii-entry" in result.stdout


# ── --help cleanliness ───────────────────────────────────────────────────────


def test_top_level_help_is_clean():
    result = run_cli("--help")
    assert result.returncode == 0
    assert "search" in result.stdout and "add" in result.stdout and "audit" in result.stdout and "inject" in result.stdout


def test_each_subcommand_help_exits_zero():
    for command in ("search", "add", "audit", "inject"):
        result = run_cli(command, "--help")
        assert result.returncode == 0, f"{command} --help failed: {result.stderr}"
        assert result.stdout.strip(), f"{command} --help produced no output"
