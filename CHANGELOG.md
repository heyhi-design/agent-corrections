# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once a first version is
tagged.

## [0.1.0] - 2026-07-29

The first public release.

### Added

- **The schema (`schema/`)** : the shape of a correction. What triggered it, what to do instead, how
  often it has fired, and where it is in its lifecycle.
- **The CLI (`src/`)** : `search`, `add`, `audit`, and `inject`. Standard library plus a YAML parser,
  no network calls.
- **The two-tier write gate** : factual corrections are written straight to the registry; behavioral
  ones need a human to sign off, because a correction that changes how an agent works is a decision,
  not a data entry.
- **Provider-agnostic injection** : `inject --format claude | openai | generic` emits the same active
  corrections in whatever shape your runtime reads as instructions. Every format is null-safe, so a
  correction with an empty field never renders the literal word "None" into a system prompt.
- **The lifecycle (`LIFECYCLE_SPEC.md`)** : how a correction gets promoted, superseded, and retired.
  A registry nobody prunes stops being read.
- **`audit`** : surfaces review-due entries and promotion candidates, so the review actually happens.
  A promotion candidate that carries its own hand-written "Promotion clause: ..." sentence (in
  `correction`, `trigger_context`, or `pattern`) has it quoted in the report, so a reviewer doesn't
  have to open the registry file just to see why the author thought it was worth promoting.
- **An example registry (`examples/`)** : twelve entries demonstrating the schema, both review tiers,
  and both lifecycle exits. A working demo, not a template to copy.
- **The guides** : `INTEGRATION_GUIDE.md` for wiring this into a running agent loop,
  `DESIGN_RATIONALE.md` for why it is shaped this way.
- **Two diagrams (`docs/`)** : `stories-vs-entries.svg` and `lifecycle.svg`, embedded in the README.
  One frames the problem (a correction as a story that leaves versus a structured entry that stays);
  one shows the lifecycle (a correction promoted into a permanent rule, the original entry kept as
  superseded history). Self-contained SVGs, no external dependencies, and they render in light and dark.
- **Test suite (`tests/`)** : the CLI, schema validation, search, provider parity across the three
  inject formats, and the loader's behavior on malformed or non-UTF-8 registries. The suite runs on
  Linux across the supported Python versions, with a Windows leg.

### Performance

- Registry loading now uses the C-accelerated YAML loader when PyYAML's libyaml
  bindings are available (falls back to the pure-Python loader otherwise, with
  identical parsing either way, including error line numbers), and `search` compiles
  its query's word-boundary pattern once per call instead of once per field per entry.
  Registry *writes* deliberately keep the pure-Python dumper -- the C dumper escapes
  astral-plane Unicode (most emoji) differently, which would make on-disk output
  depend on the installing machine's environment. No change to search results,
  ranking, or file output at any registry size.

### Known limitations

- Fire counts do not update themselves. Something in your loop has to notice a correction fired and
  say so; `INTEGRATION_GUIDE.md` section 2 covers the options, none of which are magic.
- `add` rewrites the registry through a YAML serializer, which does not preserve hand-written inline
  comments. Keep commentary in entry fields rather than in YAML comments.
- Search is keyword matching, not semantic. It is honest about that, and it is enough at the size a
  registry stays useful at.
