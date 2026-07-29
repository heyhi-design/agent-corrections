# Design Rationale

Why this exists, what it's arguing against, and one worked example of the
whole cycle.

## Where the pattern comes from

Practices for recording agent mistakes as structured, reloadable lessons have
been circulating in the agent-engineering community for a while, under
different names. One public version worth crediting directly: Boris Cherny,
Claude Code's creator, has described maintaining a `tasks/lessons.md` file in
his own workflow, updated after every correction from a user with the pattern
and a self-rule to prevent the repeat (see Gergely Orosz's interview, "Building
Claude Code with Boris Cherny," Pragmatic Engineer).

`agent-corrections` adapts that same instinct for a slightly different
problem: a lessons file that grows as an unstructured list works well for one
person's own workflow, but it doesn't hold up once you need to search it,
promote entries out of it, tell a factual typo-fix apart from a judgment call
about how the agent should behave, or load only the relevant subset into a
given session. The schema, the CLI, and the lifecycle in this repository are
the structure a growing lessons file eventually needs.

## Why not just use an observability platform

Trace-and-eval platforms (LangSmith, Arize, Langfuse, and others in that
space) answer "what did the agent do, and was that right." That's necessary
and this project doesn't try to replace it. But it's a different question from
"what has the agent already been told not to do, and is that loaded before it
tries again." A trace tells you a mistake happened. It doesn't, on its own,
prevent the next session from making the same one.

Worth naming honestly: this boundary is moving. LangSmith's datasets and
evaluations layer is already adjacent to correction capture, and other
platforms will likely grow toward it too. The durable differentiator here
isn't "they don't have this yet." It's the tiered write-authority model below:
a correction that changes how the agent behaves gets a human's judgment before
it's trusted, every time, not just at initial setup.

## The tiered write-authority principle

Not every correction is the same kind of claim. A factual correction ("this
API takes milliseconds, not seconds") is cheap to verify and cheap to get
wrong. Getting it right doesn't require a judgment call, so this tool lets it
auto-write. A behavioral correction ("don't skip the confirmation step before
a destructive action") is a decision about how the agent should work. It
compounds. Getting it wrong quietly changes the agent's behavior in a way
nobody signed off on.

That's a version, in tool form, of a broader argument: some decisions
shouldn't be automated even when automating them is technically possible.
Design judgment, hiring decisions, and how a system should behave under
ambiguity all sit on the human side of that line for the same reason a
behavioral correction does here. The tool automates the mechanical parts
(capture, search, loading) and keeps the judgment parts (what counts as a
pattern, what's worth promoting to a permanent rule) with a person. `severity:
behavioral` is where that line is drawn in the schema.

## A worked example

A fictional team, `northwind`, ships a CLI tool. Five different sessions,
across five weeks, have the agent write a changelog entry that forgets the
issue tracker reference. Someone gets tired of catching it in review and
writes it up:

```yaml
- id: changelog-entry-missing-issue-ref
  category: constraint
  severity: factual
  trigger_context: >-
    Writing or amending a CHANGELOG.md entry for a merged PR that closes a
    tracked issue.
  correction: >-
    Every CHANGELOG.md entry that corresponds to a tracked issue includes the
    issue number in parentheses at the end of the line.
  status: active
  times_fired: 1
  last_fired: "2026-02-03"
```

It's added as `factual`: referencing an issue number is a mechanical fact, not
a judgment call, so it auto-writes without a review step. Loaded at the start
of every session via `corrections inject`, it keeps firing (the team is
mid-migration and still forgets by hand sometimes) until `times_fired` passes
the promotion threshold:

```yaml
- id: changelog-entry-missing-issue-ref
  category: constraint
  severity: factual
  trigger_context: >-
    Writing or amending a CHANGELOG.md entry for a merged PR that closes a
    tracked issue.
  correction: >-
    Every CHANGELOG.md entry that corresponds to a tracked issue includes the
    issue number in parentheses at the end of the line.
  status: superseded
  superseded_by: "changelog-lint CI check (require-issue-ref rule, enabled 2026-03-10)"
  times_fired: 5
  last_fired: "2026-03-09"
```

Once it's fired five times in five weeks, it's not really a "remember this"
correction anymore; it's a rule the team decided to enforce mechanically. The
team writes a lint rule, `status` flips to `superseded`, and `superseded_by`
records where the rule actually lives now. `corrections audit` stops flagging
it as a promotion candidate; `corrections inject` stops loading it (superseded
entries aren't active). The registry entry stays in the file as a record of
why the lint rule exists, not as something a session needs reminded of anymore.

See [`LIFECYCLE_SPEC.md`](LIFECYCLE_SPEC.md) for the exact promotion
thresholds and close-out steps this example is following.
