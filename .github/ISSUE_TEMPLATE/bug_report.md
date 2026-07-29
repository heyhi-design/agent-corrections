---
name: Bug report
about: Something in the schema, CLI, or docs isn't working as documented
title: "[bug] "
labels: bug
---

**What did you run?**

The exact command, including flags. If it's a script calling the CLI, the
relevant snippet.

```
python3 src/corrections_cli.py ...
```

**What did you expect to happen?**

**What happened instead?**

Paste the full output, including any traceback.

**Environment**

- Python version: `python3 --version`
- PyYAML version: `python3 -c "import yaml; print(yaml.__version__)"`
- OS:

**Registry file (if relevant)**

If the bug involves a specific registry file, a minimal reproduction (a
trimmed registry with just the entry that triggers it) is the fastest path to
a fix. Please don't paste a full production registry if it contains anything
you'd rather not share; a two-entry reproduction is usually enough.
