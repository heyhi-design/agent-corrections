## What changed

## Why

## Checklist

- [ ] `python3 -m pytest tests/ -v` passes locally
- [ ] New behavior has a test (`tests/test_cli.py`, `tests/test_search.py`, or `tests/test_schema_validation.py`)
- [ ] If this adds or changes an example entry: `python3 src/corrections_cli.py audit --registry examples/example-registry.yaml` reports it schema-valid
- [ ] If this changes `schema/corrections-schema.yaml`: there's a linked issue discussing the field first (see `CONTRIBUTING.md`)
- [ ] Commits are signed off (`git commit -s`) per the DCO in `CONTRIBUTING.md`
- [ ] No product, company, or person names beyond what's already in the example registry

## Anything reviewers should look at closely?
