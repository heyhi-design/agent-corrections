"""
Provider parity for `inject`.

The repository claims the corrections registry is provider-agnostic. `inject` is where
that claim is actually cashed: three formats (`claude`, `openai`, `generic`), one registry,
and whichever one you ask for is the one you should get.

The existing inject tests check that each format produces *some* output. That is weaker
than it looks. `test_inject_openai_format_produces_output` asserts the word "Corrections"
appears -- but the claude header ("## Corrections") contains it too, so that test would go
right on passing if `--format openai` silently emitted claude output. These tests assert
the formats are *distinguishable*, which is the property the claim actually rests on.
"""

import itertools
import subprocess
import sys
from pathlib import Path

import yaml

import corrections_cli

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "src" / "corrections_cli.py"
EXAMPLE_REGISTRY = REPO / "examples" / "example-registry.yaml"

FORMATS = tuple(corrections_cli._INJECT_HEADERS)


def _active_ids():
    with open(EXAMPLE_REGISTRY, encoding="utf-8") as f:
        entries = yaml.safe_load(f)["entries"]
    return [e["id"] for e in entries if e.get("status") == "active"]


def _inject(fmt):
    result = subprocess.run(
        [sys.executable, str(CLI), "inject", "--format", fmt, "--registry", str(EXAMPLE_REGISTRY)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"--format {fmt} exited {result.returncode}: {result.stderr}"
    return result.stdout


# ── every format is complete ─────────────────────────────────────────────────


def test_every_format_renders_every_active_correction():
    """A format that quietly drops entries is worse than one that fails loudly."""
    active = _active_ids()
    assert active, "the example registry has no active entries, so this test proves nothing"

    for fmt in FORMATS:
        output = _inject(fmt)
        missing = [entry_id for entry_id in active if entry_id not in output]
        assert not missing, f"--format {fmt} dropped active corrections: {missing}"


def test_no_format_emits_none_or_raw_yaml():
    """
    Every field of a correction is nullable. An unguarded null renders as the literal word
    "None" straight into a system prompt, where it reads to the model as a stray token.
    """
    for fmt in FORMATS:
        output = _inject(fmt)
        assert "None" not in output, f"--format {fmt} leaked a null as the string 'None'"
        assert "- id:" not in output, f"--format {fmt} leaked raw YAML"
        assert "trigger_context:" not in output, f"--format {fmt} leaked a raw field name"


def test_no_format_emits_a_retired_or_superseded_correction():
    for fmt in FORMATS:
        output = _inject(fmt)
        assert "commit-message-missing-ticket-ref" not in output  # superseded
        assert "python2-print-statement-syntax" not in output  # retired


# ── the formats are actually distinguishable ─────────────────────────────────


def test_asking_for_a_format_gets_you_that_format_and_not_another():
    """
    Round-trip: the format selected on the command line is the format that comes back.

    Compared body-first, with the header stripped. Comparing whole outputs would be a
    weaker check than it looks: the headers alone differ between formats, so two formats
    rendering byte-identical *bodies* would still produce distinguishable output and slip
    past a naive uniqueness assertion.
    """
    outputs = {fmt: _inject(fmt) for fmt in FORMATS}

    bodies = {}
    for fmt, output in outputs.items():
        header = corrections_cli._INJECT_HEADERS[fmt]
        assert output.startswith(header), f"--format {fmt} did not emit its own header"
        bodies[fmt] = output[len(header):]

    assert len(set(bodies.values())) == len(FORMATS), (
        "two formats rendered byte-identical bodies: "
        f"{ {fmt: body[:60] for fmt, body in bodies.items()} }"
    )


def test_every_declared_format_renders_entries_differently_from_every_other():
    """
    The guard, and it has to be pairwise.

    `_format_entry_block` dispatches on format name and falls through to the generic body
    for anything it does not recognize, while argparse derives its `--format` choices
    straight from `_INJECT_HEADERS`. Adding a header is therefore enough to expose a new
    format on the command line but not enough to make it render as anything: the new
    format ships a distinct header wrapped around generic bodies, and every other test in
    this repository stays green.

    Checking each format against `generic` alone would catch that one case and miss its
    sibling, where two *named* formats collapse into each other (an edit to the `openai`
    branch that leaves it identical to `claude`). Both are the same defect wearing
    different clothes: a format that is declared but not actually implemented. Pairwise
    comparison catches both.
    """
    entry = {
        "id": "sample-correction",
        "category": "testing",
        "correction": "Do the thing the correct way.",
        "trigger_context": "When the thing is about to be done the other way.",
    }

    bodies = {fmt: corrections_cli._format_entry_block(entry, fmt) for fmt in FORMATS}

    for first, second in itertools.combinations(FORMATS, 2):
        assert bodies[first] != bodies[second], (
            f"--format {first} and --format {second} render entries identically. One of "
            f"them is declared in _INJECT_HEADERS without a matching branch in "
            f"_format_entry_block, so it is not really implemented: it is another format "
            f"wearing a different header."
        )


def test_a_null_field_renders_as_a_placeholder_in_every_format():
    """The fmt() guard, exercised through each format's own body renderer."""
    entry = {"id": "sample-correction", "category": None, "correction": None, "trigger_context": None}

    for fmt in FORMATS:
        body = corrections_cli._format_entry_block(entry, fmt)
        assert "None" not in body, f"--format {fmt} rendered a null field as the string 'None'"
        assert "sample-correction" in body
