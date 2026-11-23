# Integration Guide

How to actually wire a corrections registry into a running agent workflow:
loading it at session start, keeping fire counts honest, running the
promotion review, and setting up the approval gate for behavioral entries.

## 1. Loading corrections at session start

The pattern is provider-agnostic: read the active entries, format them into a
context block, put that block wherever your runtime puts persistent
instructions (a system prompt, a project-level instructions file, whatever
your agent loop reads before the first user turn). Every example below
assumes `corrections-registry.yaml` is your own project's registry: as in
the README, `--registry` resolves against your current working directory,
not against wherever you cloned this repository.

**Provider-agnostic pseudocode:**

```
on session start:
    block = run("corrections inject --format generic --registry <path>")
    prepend block to the context the agent sees before any task-specific instruction
```

**Claude (a regenerated file `CLAUDE.md` imports, or a managed section inside
`CLAUDE.md` itself):**

Claude Code reads a project-root `CLAUDE.md` automatically and supports
`@file` imports inside it, so the loading step should regenerate a file in
place rather than append to one. An append (`>>`) duplicates the block on
every re-run and keeps a superseded entry loading forever, which defeats the
lifecycle this tool is built around (see `LIFECYCLE_SPEC.md`'s close-out step
4). Regenerate instead:

```
python3 src/corrections_cli.py inject --format claude --registry corrections-registry.yaml > corrections.md
```

Add one line to `CLAUDE.md`, written once: `@corrections.md`. `--format
claude` produces markdown with a `## Corrections` heading, which reads
naturally either as its own imported file or pasted into a managed section of
a longer instructions file. Re-run the command above (or automate it as a
pre-session step) whenever the registry changes; the CLI doesn't watch the
file for you.

To see this work end to end: mark an entry `status: retired` or `status:
superseded` in the registry (per `LIFECYCLE_SPEC.md`), re-run the command
above, and confirm that entry's bullet is gone from `corrections.md`. Because
the command overwrites (`>`) instead of appending, the superseded entry
actually stops loading on the next session -- it doesn't just stop being
*added* while an earlier copy of it lingers from a prior run.

**OpenAI (or any Chat Completions / Assistants-style system message):**

```python
import subprocess
from pathlib import Path

# Point this at wherever you cloned agent-corrections -- an anchored path
# means this script works regardless of the caller's working directory.
CLI = Path("/path/to/agent-corrections/src/corrections_cli.py")

block = subprocess.run(
    ["python3", str(CLI), "inject", "--format", "openai",
     "--registry", "corrections-registry.yaml"],
    capture_output=True, text=True, check=True,
).stdout

system_message = block + "\n\n" + your_existing_system_prompt
```

`--format openai` uses a more directive, imperative framing than the `claude`
format, matching how these models tend to respond to explicit "you must"
instructions in a system message.

**Generic fallback (any provider, any framework):**

```
python3 src/corrections_cli.py inject --format generic --registry corrections-registry.yaml
```

Plain text, no markdown assumptions. Safe to paste into any system-prompt-like
field.

**Capping the block.** Instruction-following degrades as a context fills up,
and earlier instructions tend to win. Once a registry grows, pass `--max N` to
any format: it injects at most N entries, behavioral ones first (the
approval-gated tier, costlier to miss), then the most-fired, then the most
recently fired, and ends with a one-line note saying how many were left out.
With `--max`, the block is always in that order, even when nothing is cut.
Without it, every matching entry loads in registry order, as before.

Every format is null-safe: a correction with no `last_fired` yet, or no
`superseded_by`, never renders as the literal string "None" or leaks a raw
YAML `null`. If you're writing your own formatter, guard for this explicitly.
It's an easy field to get wrong: a YAML `null` loads into Python as `None`,
and an unguarded f-string interpolation of a `None` field prints the literal
word "None" straight into a system prompt, which reads as a stray, confusing
token to the model. `fmt()` in `src/corrections_cli.py` is the guard pattern
this project uses.

## 2. Instrumenting fire-count updates

A correction's `times_fired` and `last_fired` fields are only useful if
something actually updates them when the pattern recurs. This tool doesn't
auto-detect "did the agent almost make this mistake." That's a judgment call,
which is the point. The instrumentation step is a habit, not a script:

1. At the end of a session (or as part of whatever review step your team
   already runs), ask: did any loaded correction actually matter this
   session? Not "was it loaded" (all active corrections load every session);
   "did the situation it describes come up."
2. For each one that did, record the fire on that entry:

   ```
   python3 src/corrections_cli.py fire <id> --registry corrections-registry.yaml
   ```

   That bumps `times_fired` by one and stamps `last_fired` with today's date
   (`--date` for an earlier session), editing those two lines and nothing
   else. Deciding *whether* the entry fired stays the human step: nothing here
   "detects" firing from log output, which would be guessing at exactly the
   judgment this step exists to make deliberately. Never record a recurrence
   as a new sibling entry; its count starts over at one and the promotion
   threshold never trips.
3. If a session catches a genuinely new pattern (not a recurrence of an
   existing entry), that's `corrections add`, not an existing entry's fire
   count. See LIFECYCLE_SPEC.md for how to tell the difference.

If your team runs a structured end-of-session review already, this is a
two-minute addition to it, not a new process.

## 3. Running the promotion review

```
python3 src/corrections_cli.py audit --registry corrections-registry.yaml
```

This does two things: validates every entry against the schema (a malformed
entry is a bug in the registry, not a promotion question) and reports two
categories among the active entries:

- **Promotion candidates**: entries at or above the fire threshold (default:
  5 fires; override with `--promotion-threshold`). These have proven
  themselves enough times that "keep re-explaining it to the agent" is no
  longer the cheapest fix.
- **Review-due entries**: active entries that haven't fired in a while
  (default: 30 days; override with `--stale-days`). This is a prompt to
  review, not to reflexively retire: many corrections are durable
  environment facts that are correct precisely because nothing has
  re-triggered them lately. A human glance still helps -- has the situation
  genuinely stopped occurring, or has the team stopped checking for it?

The review itself is a human conversation, not something this tool automates:
for each promotion candidate, decide whether the pattern is stable and
specific enough to become a permanent rule (a linter check, a style guide
entry, a hard-coded validation), and if so, where that rule should live.

**Closing out a promoted correction:**

1. Build the permanent rule (the linter config, the CI check, the style guide
   line) wherever it belongs in your codebase.
2. Close the entry:

   ```
   python3 src/corrections_cli.py retire <id> --registry corrections-registry.yaml \
     --reason "promoted to a lint rule" --superseded-by "<where the rule now lives>"
   ```

   That sets `status: superseded` and `superseded_by`, and stamps the end of
   the entry's validity window (`valid_until`, `retired_reason`). Leave out
   `--superseded-by` to retire an entry that simply stopped applying.
3. Re-run `corrections inject`. Superseded entries stop loading, because the
   agent no longer needs reminding; the rule enforces itself now.
4. If your `meta` block states counts, bring them in line: `corrections audit
   --recount --write` (add `--archive <file>` if you keep an archive).

The entry stays in the registry file. It's the audit trail for why the rule
exists.

**Keeping the loaded file lean.** Once retired and superseded entries pile up,
move them into an archive file verbatim and check the pair:

```
python3 src/corrections_cli.py split --registry corrections-registry.yaml --archive corrections-registry-archive.yaml
python3 src/corrections_cli.py split --registry corrections-registry.yaml --archive corrections-registry-archive.yaml --verify
python3 src/corrections_cli.py audit --registry corrections-registry.yaml --recount --archive corrections-registry-archive.yaml
```

**Adopting this version on an existing registry.** If your entries sit under a
key other than `entries:` (a `corrections:` list, say), every command now fails
loud and names the key instead of reading the file as empty. Rename it in place,
then reconcile the counts:

```
python3 src/corrections_cli.py migrate-key --from corrections --registry corrections-registry.yaml
python3 src/corrections_cli.py audit --registry corrections-registry.yaml --recount --write
```

If you also keep an archive file, migrate its key the same way and pass it to the
recount (`--archive corrections-registry-archive.yaml`), so whole-registry counts
are checked against both files.

`migrate-key` changes that one line and nothing else (`--dry-run` shows it
first).

## 4. Setting up tiered write authority

`corrections add` implements the two-tier model directly:

**Factual corrections auto-write:**

```
python3 src/corrections_cli.py add \
  --registry corrections-registry.yaml \
  --id api-timeout-is-30s-not-10s \
  --category constraint \
  --severity factual \
  --trigger-context "Configuring a timeout for the billing API client." \
  --correction "The billing API's default timeout is 30 seconds, not 10 -- set it explicitly rather than trusting the client library default."
```

No approval step. A fact is either right or wrong, and getting it wrong is
cheap to catch and cheap to fix.

**Behavioral corrections are approval-gated:**

```
python3 src/corrections_cli.py add \
  --registry corrections-registry.yaml \
  --id confirm-before-destructive-migration \
  --category constraint \
  --severity behavioral \
  --trigger-context "Running a database migration that drops or truncates a table." \
  --correction "Never run a destructive migration without an explicit human confirmation step in the same session."
```

Run interactively, this prompts for a yes/no before writing. Run
non-interactively (CI, a script, an agent calling the CLI on its own), it
refuses and asks for an explicit `--approve` flag instead of silently writing
a behavioral rule nobody signed off on. Use `--dry-run` to preview exactly
what would be added without writing anything, whether or not you intend to
approve it yet: that's the safe default for an agent that wants to *propose*
a behavioral correction for a human to review later, rather than write it
directly.

## References

- [`LIFECYCLE_SPEC.md`](LIFECYCLE_SPEC.md): the full lifecycle spec, promotion
  thresholds, and close-out protocol referenced throughout this guide.
- [`DESIGN_RATIONALE.md`](DESIGN_RATIONALE.md): why the tiered model exists
  and a full worked example of one entry's lifecycle.
- [`schema/corrections-schema.yaml`](schema/corrections-schema.yaml): every
  field, documented.
