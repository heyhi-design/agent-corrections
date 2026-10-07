"""
`add` must not discard a registry's hand-written comments.

`add` used to re-dump the whole registry through yaml.safe_dump, which deleted
every YAML comment in the file: a header explaining what the registry is for,
inline notes, a footer. It now splices only the new entry's text into the file
and checks the result (re-parsed, exactly one change: the appended entry) before
writing it. A brand-new file still goes through the full write; an unusual
`entries:` layout falls back to the full write only when the file has no comments
to lose, and otherwise refuses with nothing written.
"""

import shutil
from pathlib import Path

import yaml

import corrections_cli

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_REGISTRY = REPO / "examples" / "example-registry.yaml"

BEHAVIORAL_ARGS = [
    "--severity", "behavioral", "--approve",
    "--category", "taxonomy-consistency",
    "--trigger-context", "Auditing labels against a team glossary.",
    "--correction", "Load the glossary before calling a label undecided.",
]


def _add(path: Path, entry_id: str) -> int:
    return corrections_cli.main(["add", "--registry", str(path), "--id", entry_id, *BEHAVIORAL_ARGS])


def _ids(path: Path) -> list:
    return [e["id"] for e in yaml.safe_load(path.read_text(encoding="utf-8"))["entries"]]


def test_header_and_inline_comments_survive_empty_flow_list(tmp_path):
    p = tmp_path / "reg.yaml"
    original = (
        "# Header line one: what this registry is.\n"
        "# Header line two: who curates it.\n"
        "meta:\n"
        '  version: "0.1.0"  # an inline note\n'
        "entries: []\n"
        "# footer\n"
    )
    p.write_text(original, encoding="utf-8")
    assert _add(p, "first-entry") == 0
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# Header line one: what this registry is.\n# Header line two: who curates it.\n")
    assert '  version: "0.1.0"  # an inline note\n' in text
    assert text.rstrip().endswith("# footer")
    assert _ids(p) == ["first-entry"]


def test_append_to_block_list_keeps_every_prior_byte(tmp_path):
    p = tmp_path / "reg.yaml"
    _add(p, "one")  # creates the file (no comments yet)
    body = p.read_text(encoding="utf-8")
    p.write_text("# header\n" + body + "\n# trailing footer\n", encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    assert _add(p, "two") == 0
    after = p.read_text(encoding="utf-8")
    assert _ids(p) == ["one", "two"]
    # Everything before the new entry is untouched, and the footer still closes the file.
    prefix = before.split("\n# trailing footer")[0]
    assert after.startswith(prefix)
    assert after.rstrip().endswith("# trailing footer")


def test_indented_list_style_is_matched(tmp_path):
    p = tmp_path / "reg.yaml"
    p.write_text(
        "# header\n"
        "meta: {}\n"
        "entries:\n"
        "  - id: existing\n"
        "    category: taxonomy-consistency\n"
        "    severity: factual\n"
        "    trigger_context: x\n"
        "    correction: y\n"
        "    status: active\n"
        "other_key: kept\n",
        encoding="utf-8",
    )
    assert _add(p, "appended") == 0
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert [e["id"] for e in data["entries"]] == ["existing", "appended"]
    assert data["other_key"] == "kept"
    assert "  - id: appended\n" in p.read_text(encoding="utf-8")


def test_new_file_is_still_created(tmp_path):
    p = tmp_path / "sub" / "new.yaml"
    assert _add(p, "brand-new") == 0
    assert _ids(p) == ["brand-new"]


def test_unspliceable_layout_with_comments_refuses_and_writes_nothing(tmp_path, capsys):
    p = tmp_path / "reg.yaml"
    original = (
        "# a comment that a full rewrite would destroy\n"
        "entries: [{id: a, category: c, severity: factual, trigger_context: t, correction: x, status: active}]\n"
    )
    p.write_text(original, encoding="utf-8")
    assert _add(p, "b") == 1
    assert p.read_text(encoding="utf-8") == original
    assert "without discarding its comments" in capsys.readouterr().err


def test_unspliceable_layout_without_comments_falls_back_to_rewrite(tmp_path):
    p = tmp_path / "reg.yaml"
    p.write_text(
        "entries: [{id: a, category: c, severity: factual, trigger_context: t, correction: x, status: active}]\n",
        encoding="utf-8",
    )
    assert _add(p, "b") == 0
    assert _ids(p) == ["a", "b"]


def test_example_registry_comments_survive_an_add(tmp_path):
    p = tmp_path / "example-registry.yaml"
    shutil.copy(EXAMPLE_REGISTRY, p)
    before = p.read_text(encoding="utf-8")
    header = before.split("\nmeta:")[0]
    assert header.count("\n") > 10  # the documented header is present to begin with
    n = len(_ids(p))
    assert _add(p, "regression-probe-entry") == 0
    after = p.read_text(encoding="utf-8")
    # The new entry is appended at the end; everything before it is byte-identical,
    # including the header and the section comments inside `entries:`.
    assert after.startswith(before.rstrip("\n"))
    assert after.count("#") == before.count("#")
    assert _ids(p)[: n] == [e for e in _ids(EXAMPLE_REGISTRY)]
    assert _ids(p)[-1] == "regression-probe-entry"
    assert len(_ids(p)) == n + 1
