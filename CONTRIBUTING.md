# Contributing to agent-corrections

Thanks for considering a contribution. This project accepts issues and pull
requests. Before you start, a quick read of `DESIGN_RATIONALE.md` will save you
time. It explains what this tool is trying to be, which makes it easier to
tell whether an idea fits.

## Development setup

```
git clone https://github.com/heyhi-design/agent-corrections.git
cd agent-corrections
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests/ -v
```

No other dependencies. If `python3 -m pip install` refuses with an "externally
managed environment" error, create a virtual environment first (see the
Quickstart in `README.md` for the one-liner, including the Windows variant). If
`pytest` isn't green on a clean clone, that's a bug in this repo. Please open an
issue rather than working around it.

## Ways to contribute

**Report a bug.** Use the Bug Report issue template. Include what you ran, what
you expected, and what happened instead.

**Propose a new example correction.** Use the "Correction pattern proposal"
issue template, or open a PR directly against `examples/example-registry.yaml`.
Either way, an entry needs:

- A `trigger_context` specific enough that someone could recognize the pattern
  from it, not just the literal first incident that prompted it
- A `correction` written as a positive instruction ("do X"), not a description
  of the bug ("X was broken")
- At least one concrete, realistic scenario in mind, even if it's not written
  into the entry itself, so you're ready to describe it in the PR

Entries that name a specific product, company, or person by name will be asked
to generalize before merge. The example registry demonstrates the pattern; it
isn't meant to accumulate one contributor's actual internal registry.

**Propose a CLI change.** Open an issue first if the change affects a command's
flags or output format, since those are what someone's script or CI job might
already depend on. Bug fixes that don't change intended behavior don't need a
pre-discussion issue.

**Propose a schema change.** `schema/corrections-schema.yaml` is deliberately
small and stable: it's the contract that any tooling built on top of a
registry depends on. Schema changes are conservative and discussion-first.
Open an issue describing the field, why the existing fields don't cover it,
and what breaks (or doesn't) for existing registries if it's added. Additive,
optional fields are much easier to accept than anything that changes the
meaning of an existing field.

## Pull request expectations

- `python3 -m pytest tests/ -v` passes, including any new tests your change needs
- New CLI behavior gets a test in `tests/test_cli.py`; new search behavior gets
  one in `tests/test_search.py`; schema changes get one in
  `tests/test_schema_validation.py`
- New or changed example entries validate: `python3 src/corrections_cli.py audit --registry examples/example-registry.yaml`
- Commits are signed off (see DCO, below)
- The PR description says what changed and why, not just what

CI runs the test suite and schema validation on every PR. A red check means the
PR isn't ready yet, not that it's rejected. Fix and push again.

## Developer Certificate of Origin (DCO)

This project uses the DCO instead of a CLA. Every commit must include a
`Signed-off-by` trailer certifying you wrote it (or otherwise have the right to
submit it under this project's license). Add it automatically:

```
git commit -s -m "your commit message"
```

This appends a line like:

```
Signed-off-by: Your Name <your.email@example.com>
```

If you forgot to sign off a commit, `git commit --amend -s` fixes the most
recent one. For a longer history, `git rebase --signoff <base-branch>` signs
off every commit in the branch.

## Code style

Match the existing code: `from __future__ import annotations` at the top of
new modules, type hints on function signatures, no dependencies beyond the
standard library and PyYAML. Run `python3 -m pytest tests/ -v` before opening
the PR. There's no separate linter gate yet.

## Response time

This is a small, actively maintained project. Expect a first response on
issues and PRs within 5 business days.
