# Lifecycle Specification

The full state machine for a correction entry: what each status means, when a
correction is ready to promote, and how to close out a promotion cleanly.

## The three statuses

A correction entry's `status` field holds exactly one of three values:

- **`active`**: Currently loaded and enforced. `corrections inject` includes
  it. This is the default status for a new entry.
- **`retired`**: No longer relevant. The tool, pattern, or codebase state it
  referred to is gone (a dependency was removed, a migration finished, an API
  version was dropped). Not loaded by `inject`. Kept in the registry as
  history, not as noise.
- **`superseded`**: The pattern proved itself and was promoted into a
  permanent, mechanically-enforced rule somewhere else. Not loaded by
  `inject`, because the agent no longer needs reminding; the permanent rule
  does the enforcing now. `superseded_by` records where that rule lives.

**`promoted` is not a status.** It's the action of moving an entry from
`active` to `superseded`. There's no fourth value in the schema for it; if you
find yourself wanting to write `status: promoted`, what you mean is
`status: superseded` plus a `superseded_by` reference.

## When an entry is ready to promote

Two independent thresholds, either one sufficient:

- **5 or more total fires.** `corrections audit` reports this automatically
  (`--promotion-threshold` to change the number). This is a lifetime count,
  not a rolling window: the schema stores a running `times_fired` total and
  a single `last_fired` date, not a list of individual fire events, so the
  tool has no way to ask "how many of those happened in the last 30 days."
  If a correction fired 5 times over the last year rather than the last
  month, `audit` still surfaces it as a candidate; whether that pace is
  actually "proven" or just "old" is exactly the kind of call the human
  review step below exists to make.
- **3 or more distinct sessions**, even if the raw fire count is lower. This
  matters because `times_fired` is meant as a floor count of *sessions*
  where the correction applied, not a count of every literal occurrence
  within one session (see `schema/corrections-schema.yaml`). If you're
  incrementing it per session honestly, the two thresholds mostly agree; the
  session-count framing exists for a correction that's clearly real but
  fires rarely enough that 5 total fires takes a long time to accumulate.
  `corrections audit` does not surface this lower bar on its own: its default
  flags at 5 (the first threshold above). Apply the 3-session bar as a
  judgment call during human review, or run `corrections audit
  --promotion-threshold 3` to have it list 3-fire candidates.

Staleness is a separate, genuinely date-based check: `--stale-days` (default
30) compares `last_fired` against the audit's reference date. Promotion
eligibility and staleness ask different questions, and an entry can be both
at once (fired enough times to be a candidate, but quiet lately).

Either threshold is a signal to review, not an automatic promotion. A
correction that's fired 6 times because it's genuinely unstable (the
underlying tool keeps changing, the "right" answer keeps shifting) is not
ready to become a permanent rule; it's ready for someone to ask why it keeps
recurring.

## The close-out protocol

1. **Build the permanent rule.** A linter config, a CI check, a style guide
   entry, a hard-coded validation, whatever form of enforcement fits. This
   step happens outside the registry entirely.
2. **Verify the rule actually covers the trigger.** Before flipping status,
   confirm the new rule would have caught the same situation the correction's
   `trigger_context` describes. A promoted rule that's narrower than the
   correction it replaces silently loses coverage.
3. **Flip the entry.** `status: superseded`, `superseded_by` set to a
   specific, findable pointer (a file path, a CI job name, a rule id).
   "Handled elsewhere" is not a valid `superseded_by` value; a future reader
   needs to be able to go look at the thing.
4. **Re-run `corrections inject`.** Confirm the superseded entry no longer
   appears in the loaded context. If it still does, the status flip didn't
   take, or something is filtering on the wrong field.

The entry itself is never deleted. A superseded entry is the audit trail for
why the permanent rule exists, useful the next time someone asks "why do we
lint for this."

`corrections retire <id> --reason "..." --superseded-by "<pointer>"` does step
3 as one edit: it sets `status: superseded` and `superseded_by`, and closes the
entry's validity window with `valid_until` and `retired_reason`. Without
`--superseded-by` it sets `status: retired` instead. If your `meta` block
states counts, run `corrections audit --recount --write` afterwards (with
`--archive <file>` if you keep an archive) so they follow.

## Change one entry at a time

Add, merge, or retire one entry at a time; never rewrite the registry whole.
A wholesale rewrite, by a person or an agent, shortens entries, drops the
reasons, reflows hand-written text, and loses comments, and a reviewer cannot
tell which lesson changed. `fire`, `retire`, `migrate-key`, `split` and
`audit --recount --write` follow the rule: each edits only the lines it must,
re-reads the file to confirm nothing else moved, and refuses, writing nothing,
when the layout makes that unsafe. `add` appends the one new entry; only on an
unusual layout with no comments to lose does it fall back to a full rewrite.

## Recording a fire

When a known pattern recurs, `corrections fire <id>` increments that entry's
`times_fired` and stamps `last_fired` (today, or `--date`). It never moves
`last_fired` backwards. Do not add a second entry for a recurrence: a sibling
entry starts its own count at one, so neither copy ever reaches the promotion
threshold. A fire on a `superseded` entry is worth reading: the permanent rule
did not catch the situation it was promoted to cover.

## Retiring: a validity window, not a deletion

An entry's validity window runs from `origin_date` to `valid_until`. Retiring
closes the window and records why (`retired_reason`); the entry stays in the
file. An `active` entry with `valid_until` set fails validation, because an
active entry's window is by definition still open. Retiring a behavioral entry
asks for the same human sign-off as adding one (`--approve`, or the prompt).

## Counts are derived, not trusted

Hand-maintained `meta` counts drift. `corrections audit --recount` derives every
count a meta block states from the entries, prints stated against derived, and
exits 1 on drift; `--write` fixes the drifted lines in place. When an archive
file sits beside the registry under the default name, recount asks you to name
it (`--archive <file>`) or to confirm the counts describe the live file alone
(`--live-only`) before it writes, so whole-registry counts are never overwritten
with live-only ones.

## Active and archive

A registry that loads every session should hold what is in force. When retired
and superseded entries pile up, `corrections split --archive <file>` moves them
into an archive file verbatim (creating it on first use, appending after that;
the default is `<registry name>-archive.<ext>` beside the registry). Comment
lines above or after a moved entry stay in the live file; move them by hand if
they belong to it.
The live file's `meta` keeps the counts for both files; check them with
`corrections audit --recount --archive <file>`. `corrections split --verify`
checks the pair: no id in both files, no repeated id within one, only active
entries in the live file, only retired and superseded ones in the archive, and
every stated count equal to the count across both.

## Seeds and the rules they assume

A framework seed is often written on the assumption that some rule already
exists in the instance it lands in. If the rule was never adopted, the seed
either stays active with nobody asking why, or, worse, ships `superseded` by a
rule the instance does not have and silently stops loading. `assumes_rule`
makes the coupling checkable. `corrections audit` flags a seed whose assumed
file or section is absent, and separately lists active entries whose assumed
rule has since arrived (candidates for supersession).

## Corrected claims

When a later entry finds that an earlier entry's claim was wrong, the later one
carries `supersedes: <earlier id>`. It points back, unlike `superseded_by`,
which points forward at a permanent rule. `corrections audit` lists every
corrected entry that is still active, so the wrong claim stops loading once
someone retires it.

## The same lesson, learned twice

Two registries (two instances, or a template and an instance) tend to learn
some lessons twice. `corrections compare <a> <b>` reports likely duplicates by
trigger overlap, plus entries that share an id. It is a report: which lesson to
keep, merge, or retire stays a human call.

## The human review standard

Promotion is a judgment call, not a threshold crossing. `corrections audit`
tells you an entry is *eligible*; a human still decides whether it's *ready*.
Questions worth asking before promoting:

- Is the pattern stable? (Has the correction text itself changed recently, or
  settled?)
- Is it specific enough to encode mechanically? Some behavioral corrections
  are judgment calls that resist a linter rule (design taste, when to push
  back on a request); those may be better served staying `active` and loaded
  every session indefinitely, or migrating into a team's style guide as prose
  rather than an enforced check.
- Does promoting it lose the *reason*, not just the rule? A good
  `superseded_by` reference points somewhere that still explains the "why,"
  not just the "what."

Who does the review is a team decision this tool doesn't make for you.
`corrections add`'s approval gate on behavioral entries is the same principle
applied one step earlier: a human confirms a new behavioral correction before
it's trusted; a human likewise confirms when one graduates to permanent.
Factual corrections (typos, API details, formatting rules) don't need this
step at either point. They're not judgment calls.
