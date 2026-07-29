#!/usr/bin/env python3
"""
corrections: a CLI for a structured corrections registry.

Four commands:
  search  Find entries matching a keyword or phrase.
  add     Create a new entry (auto-write for factual, approval-gated for behavioral).
  audit   Validate the registry's schema and report stale / promotion-candidate entries.
  inject  Emit a session-preamble context block of active entries.

Run `corrections_cli.py <command> --help` for the full option list of any command.

Requires Python 3.9+ and PyYAML. No other dependencies. No network calls.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only when PyYAML is absent
    print(
        "error: PyYAML is required. Install it with: python3 -m pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)

# libyaml's C loader parses 12-31x faster than the pure-Python path, with parse
# output confirmed identical (including which line a syntax error reports). Not
# every PyYAML install has it (an sdist build without the libyaml headers lacks
# the class entirely), so resolve once here and fall back to the pure-Python
# loader when absent -- the CLI behaves exactly as before on those installs.
#
# The dumper is deliberately NOT swapped to CSafeDumper: with allow_unicode=True,
# libyaml's C emitter escapes astral-plane characters (U+10000 and above -- most
# emoji) to \Uxxxxxxxx sequences instead of writing them literally, unlike the
# pure-Python dumper. Both round-trip to the same value, but the bytes on disk
# differ purely based on whether the installing machine happens to have libyaml
# -- an environment-dependent, silently-nondeterministic write for anyone whose
# registry entries contain such a character. save_registry keeps the
# pure-Python safe_dump, which is byte-stable regardless of environment.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

try:
    from corrections_search import search as keyword_search
except ImportError:  # running as `python -m src.corrections_cli` or similar
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from corrections_search import search as keyword_search  # type: ignore[no-redef]


VALID_SEVERITIES = ("factual", "behavioral")
VALID_STATUSES = ("active", "retired", "superseded")
REQUIRED_FIELDS = ("id", "category", "severity", "trigger_context", "correction", "status")


# ─────────────────────────────────────────────────────────────────────────────
# Registry I/O
# ─────────────────────────────────────────────────────────────────────────────


class RegistryError(ValueError):
    """
    A registry file that cannot be trusted to mean what it says.

    Raised when a registry file exists but is not valid UTF-8, fails to parse
    as YAML, or parses to something that is not a registry (not a mapping,
    `entries` is present but is not a list, or an item inside `entries` is not
    a mapping). This registry is hand-edited, and the failure modes of
    hand-written YAML are quiet ones -- catch them here, with a message that
    names the file and the problem, rather than three commands downstream as a
    raw traceback.
    """


def load_registry(path: Path, *, create_if_missing: bool = False) -> dict[str, Any]:
    """
    Load a registry YAML file.

    If the file does not exist: returns an empty-but-valid structure when
    create_if_missing is True (the bootstrap case for `add`, which is meant
    to create a registry's first entry), or raises FileNotFoundError
    otherwise -- `search`/`audit`/`inject` have nothing to search, audit, or
    inject if the path is wrong, and silently treating a typo'd --registry
    as an empty registry hides the typo behind a misleadingly clean "0
    matches" / "0/0 valid" result.

    Raises RegistryError if the path exists but is not a readable file (e.g. a
    directory, or one without read permission), is not valid UTF-8, fails to
    parse as YAML, or parses to something that is not a registry (not a
    mapping, `entries` is present but is not a list, or an item inside
    `entries` is not a mapping).
    """
    if not path.exists():
        if create_if_missing:
            return {"meta": {}, "entries": []}
        raise FileNotFoundError(f"registry not found: {path}")

    try:
        with open(path, encoding="utf-8") as f:
            try:
                data = yaml.load(f, Loader=_YAML_LOADER) or {}
            except yaml.YAMLError as exc:
                mark = getattr(exc, "problem_mark", None)
                location = f" near line {mark.line + 1}" if mark is not None else ""
                raise RegistryError(f"{path}: registry has a YAML syntax error{location}.") from exc
            except UnicodeDecodeError as exc:
                # A registry saved in a non-UTF-8 encoding (Latin-1, UTF-16,
                # cp1252) fails to decode on read. UnicodeDecodeError is a
                # ValueError, not an OSError or a yaml.YAMLError, so without
                # this clause it escapes both handlers as a raw traceback.
                raise RegistryError(
                    f"{path}: registry is not valid UTF-8 -- re-save the file as UTF-8 "
                    f"({exc})."
                ) from exc
    except OSError as exc:
        # Covers --registry pointing at a directory, or a file without read
        # permission -- exists() is True in both cases, so this is the only
        # place left that can catch them before they'd otherwise surface as a
        # raw traceback.
        raise RegistryError(f"{path}: could not read the registry file ({exc.strerror or exc}).") from exc

    if not isinstance(data, dict):
        raise RegistryError(
            f"{path}: the top level of a registry must be a mapping with an `entries:` "
            f"key, got {type(data).__name__}."
        )

    # Distinguish absent/null from present-but-wrong-type. `entries:` with
    # nothing under it (or no `entries:` key at all) is an empty registry --
    # a legitimate starting state. But a present-but-falsy wrong type ({}, "",
    # 0, false) is a malformed registry, and `data.get("entries") or []` would
    # silently coerce every one of those to [], auditing as a clean "0/0
    # valid" and hiding the mistake.
    entries = data.get("entries")
    if entries is None:
        entries = []
    elif not isinstance(entries, list):
        raise RegistryError(f"{path}: `entries:` must be a list, got {type(entries).__name__}.")

    # Every entry has to be a mapping before search/audit/inject can read its
    # fields. A bare string or list item (`- just-a-string`) would otherwise
    # reach an `entry.get(...)` call downstream as an AttributeError traceback.
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RegistryError(
                f"{path}: entry #{i + 1} is a {type(entry).__name__}, not a mapping. "
                f"Every entry needs at least an id, a trigger_context, and a correction."
            )

    data["entries"] = entries
    data.setdefault("meta", {})
    return data


def save_registry(path: Path, data: dict[str, Any]) -> None:
    """
    Write the registry back to disk.

    Writes to a sibling temp file first, then atomically replaces the
    target with os.replace() -- a write interrupted by a crash, Ctrl-C, or a
    full disk leaves the original file untouched instead of a half-written
    registry. The temp file lives next to the target (not in a system temp
    dir) so the final replace is same-filesystem and therefore atomic.

    Note: this rewrites the whole file via yaml.safe_dump (deliberately the
    pure-Python dumper, not the C-accelerated one -- see the module-level comment
    on _YAML_LOADER for why), which does not preserve hand-written comments. If
    you rely on `add` to write entries, keep commentary in entry fields (or a
    separate doc), not inline YAML comments -- they will not survive a write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
        os.replace(tmp_path, path)
    except BaseException:
        # Best-effort cleanup of the temp file on any failure, including
        # Ctrl-C mid-write -- os.replace() itself is atomic, so the only
        # window where a partial file can be left behind is here, in the
        # temp file the atomic step never got to consume.
        tmp_path.unlink(missing_ok=True)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# None-safe formatting. A YAML `null` loads into Python as None; an unguarded
# f-string interpolation of a None field prints the literal word "None" into
# whatever's being rendered. Guard every nullable field through fmt() before
# it reaches a print statement, a written file, or an injected context block.
# ─────────────────────────────────────────────────────────────────────────────


def fmt(value: Any, default: str = "-") -> str:
    """Render a possibly-null field for display without ever printing the string 'None'."""
    if value is None or value == "":
        return default
    return str(value)


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation
# ─────────────────────────────────────────────────────────────────────────────


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        # Checked before `date` because datetime is a subclass of date -- a YAML
        # value like `2026-07-01 10:00:00` (a timestamp, not a bare date) parses to
        # a datetime, and a plain date arithmetic against it raises TypeError
        # downstream (cmd_audit's `as_of - last_fired`). Truncate to the date.
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%d").date()
    raise ValueError(f"not a date: {value!r}")


def validate_entry(entry: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors for one entry. Empty list = valid."""
    errors: list[str] = []

    for field_name in REQUIRED_FIELDS:
        if not entry.get(field_name):
            errors.append(f"missing required field: {field_name}")

    entry_id = entry.get("id", "<no id>")

    severity = entry.get("severity")
    if severity is not None and severity not in VALID_SEVERITIES:
        errors.append(f"{entry_id}: severity must be one of {VALID_SEVERITIES}, got {severity!r}")

    status = entry.get("status")
    if status is not None and status not in VALID_STATUSES:
        errors.append(f"{entry_id}: status must be one of {VALID_STATUSES}, got {status!r}")

    times_fired = entry.get("times_fired", 0)
    if not isinstance(times_fired, int) or isinstance(times_fired, bool) or times_fired < 0:
        errors.append(f"{entry_id}: times_fired must be a non-negative integer, got {times_fired!r}")

    for date_field in ("last_fired", "origin_date"):
        try:
            _parse_date(entry.get(date_field))
        except (ValueError, TypeError):
            errors.append(f"{entry_id}: {date_field} must be null or 'YYYY-MM-DD', got {entry.get(date_field)!r}")

    if status == "superseded" and not entry.get("superseded_by"):
        errors.append(f"{entry_id}: status is 'superseded' but superseded_by is empty")

    if (
        isinstance(times_fired, int)
        and not isinstance(times_fired, bool)
        and times_fired > 0
        and entry.get("last_fired") is None
    ):
        errors.append(
            f"{entry_id}: times_fired is {times_fired} but last_fired is null "
            f"-- a correction that has fired needs a last_fired date, or `corrections audit` "
            f"can't tell whether it's stale"
        )

    return errors


def validate_registry(entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Validate every entry plus registry-level invariants (unique ids). Keyed by entry id."""
    errors_by_id: dict[str, list[str]] = {}
    seen_ids: dict[str, int] = {}

    for i, entry in enumerate(entries):
        entry_id = entry.get("id", f"<entry #{i}>")
        errors = validate_entry(entry)
        seen_ids[entry_id] = seen_ids.get(entry_id, 0) + 1
        if errors:
            errors_by_id[entry_id] = errors

    for entry_id, count in seen_ids.items():
        if count > 1:
            errors_by_id.setdefault(entry_id, []).append(f"duplicate id: appears {count} times in the registry")

    return errors_by_id


# ─────────────────────────────────────────────────────────────────────────────
# search
# ─────────────────────────────────────────────────────────────────────────────


def cmd_search(args: argparse.Namespace) -> int:
    try:
        registry = load_registry(Path(args.registry))
    except (FileNotFoundError, RegistryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    matches = keyword_search(args.query, registry["entries"], top_k=args.top_k, status=args.status)

    if not matches:
        print(f"No matches for {args.query!r}.")
        return 0

    for m in matches:
        print(f"[{m.id}] ({m.category}, score {m.score})")
        print(f"  {m.correction}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# add
# ─────────────────────────────────────────────────────────────────────────────


def cmd_add(args: argparse.Namespace) -> int:
    registry_path = Path(args.registry)
    try:
        registry = load_registry(registry_path, create_if_missing=True)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    existing_ids = {e.get("id") for e in registry["entries"]}

    missing = [
        flag
        for flag, value in (
            ("--id", args.id),
            ("--category", args.category),
            ("--severity", args.severity),
            ("--trigger-context", args.trigger_context),
            ("--correction", args.correction),
        )
        if not value
    ]
    if missing:
        print(f"error: missing required flags: {', '.join(missing)}", file=sys.stderr)
        return 1

    if args.id in existing_ids:
        print(f"error: an entry with id {args.id!r} already exists in {registry_path}", file=sys.stderr)
        return 1

    entry: dict[str, Any] = {
        "id": args.id,
        "category": args.category,
        "severity": args.severity,
        "trigger_context": args.trigger_context,
        "correction": args.correction,
        "status": "active",
        "times_fired": 0,
        "last_fired": None,
        "origin_date": date.today().isoformat(),
    }

    errors = validate_entry(entry)
    if errors:
        print("error: entry failed schema validation:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[dry run] would add to {registry_path}:")
        for k, v in entry.items():
            print(f"  {k}: {fmt(v)}")
        return 0

    if entry["severity"] == "behavioral" and not args.approve:
        reply = None
        if sys.stdin.isatty():
            try:
                reply = input(
                    f"'{args.id}' is a behavioral correction. It changes how the agent should work, "
                    f"so it needs a human sign-off. Add it? [y/N] "
                ).strip().lower()
            except EOFError:
                # isatty() answering True does not guarantee a readable line. A Windows
                # console handle with nothing attached reports as a TTY and then raises
                # here, so the prompt branch has to survive getting no input at all.
                # Without this, the user sees an unhandled traceback where a refusal
                # belongs -- and, worse, the approval gate fails open-looking: the exit
                # code is right by accident, so a caller checking only the status thinks
                # the gate worked as designed.
                reply = None

        if reply is None:
            print(
                "error: behavioral corrections require human approval. Pass --approve to confirm, "
                "or run interactively.",
                file=sys.stderr,
            )
            return 1

        if reply not in ("y", "yes"):
            print("Not added.")
            return 1

    registry["entries"].append(entry)
    save_registry(registry_path, registry)
    print(f"Added '{args.id}' to {registry_path}.")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# audit
# ─────────────────────────────────────────────────────────────────────────────

# An author can write a "Promotion clause: ..." sentence into any free-prose field
# to say, in their own words, what should happen when this entry reaches the
# promotion threshold ("3 fires promotes this to a CI check", "review with the
# team lead before touching CLAUDE.md"). `audit` doesn't require the convention --
# it's optional context, not a schema field -- but when an author did write one,
# quoting it in the promotion-candidates report saves a reviewer the trip to open
# the registry file just to see why a candidate might be worth promoting.
_PROMOTION_CLAUSE_RE = re.compile(r"Promotion clause:\s*(.+)", re.IGNORECASE | re.DOTALL)
_PROMOTION_CLAUSE_MAX_CHARS = 200


def _promotion_clause(entry: dict[str, Any]) -> str:
    """
    Return the entry's own "Promotion clause: ..." sentence, whitespace-collapsed
    and length-capped, or "" if the entry has none.

    Searches every free-prose field a promotion clause could live in --
    correction, trigger_context, pattern -- in that order, and returns the first
    match. The clause is hand-authored and lands wherever the author chose to put
    it; restricting the search to one field would silently miss the next author's
    choice.
    """
    for field_name in ("correction", "trigger_context", "pattern"):
        match = _PROMOTION_CLAUSE_RE.search(str(entry.get(field_name) or ""))
        if not match:
            continue
        clause = " ".join(match.group(1).split())
        if len(clause) > _PROMOTION_CLAUSE_MAX_CHARS:
            clause = clause[:_PROMOTION_CLAUSE_MAX_CHARS].rstrip() + "..."
        return clause
    return ""


def cmd_audit(args: argparse.Namespace) -> int:
    # Validate flags before doing any work or printing anything. A bad --as-of
    # is a usage error (exit 2); catching it here, ahead of the load-and-report
    # below, means `audit --as-of garbage` fails cleanly instead of emitting a
    # full schema report first and then erroring.
    if args.as_of:
        try:
            as_of = _parse_date(args.as_of)
        except ValueError:
            print(
                f"error: --as-of {args.as_of!r} is not a valid date; expected YYYY-MM-DD.",
                file=sys.stderr,
            )
            return 2
    else:
        as_of = date.today()

    registry_path = Path(args.registry)
    try:
        registry = load_registry(registry_path)
    except (FileNotFoundError, RegistryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = registry["entries"]

    errors_by_id = validate_registry(entries)
    valid_count = len(entries) - len(errors_by_id)
    print(f"Schema: {valid_count}/{len(entries)} entries valid.")
    if errors_by_id:
        for entry_id, errors in errors_by_id.items():
            print(f"  [{entry_id}]")
            for e in errors:
                print(f"    - {e}")
        return 1

    promotion_candidates = []
    stale_entries = []
    never_fired = []

    for entry in entries:
        if entry.get("status") != "active":
            continue

        times_fired = entry.get("times_fired", 0)
        last_fired = _parse_date(entry.get("last_fired"))

        if times_fired >= args.promotion_threshold:
            promotion_candidates.append(entry)

        if last_fired is None:
            if times_fired == 0:
                never_fired.append(entry)
        elif (as_of - last_fired).days > args.stale_days:
            stale_entries.append(entry)

    print(f"\nPromotion candidates ({args.promotion_threshold}+ fires): {len(promotion_candidates)}")
    for e in promotion_candidates:
        clause = _promotion_clause(e)
        clause_txt = f'\n    Promotion clause: "{clause}"' if clause else ""
        print(f"  [{e['id']}] fired {e.get('times_fired')} times, last {fmt(e.get('last_fired'))}{clause_txt}")

    print(
        f"\nReview-due entries (no fires in {args.stale_days}+ days as of {as_of.isoformat()}): "
        f"{len(stale_entries)}"
    )
    if stale_entries:
        print(
            "  Review these -- do not reflexively retire. Many corrections are durable "
            "environment facts that are correct precisely because nothing has re-triggered them."
        )
    for e in stale_entries:
        print(f"  [{e['id']}] last fired {fmt(e.get('last_fired'))}")

    print(f"\nNever fired (informational, not a failure): {len(never_fired)}")
    for e in never_fired:
        print(f"  [{e['id']}]")

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# inject
# ─────────────────────────────────────────────────────────────────────────────

_INJECT_HEADERS = {
    "claude": "## Corrections\n\nApply these before doing the described work:\n",
    "openai": "# Corrections\n\nYou must apply the following corrections before doing the described work:\n",
    "generic": "CORRECTIONS (apply before doing the described work):\n",
}

# retired/superseded entries are historical record, not active guidance -- `inject
# --status retired` (or `superseded`) must not tell the reader to apply them the way
# the active-status header does.
_INJECT_HEADERS_INACTIVE = {
    "claude": "## Corrections (retired/superseded -- reference only, do not apply)\n\n",
    "openai": "# Corrections (retired/superseded -- reference only, do not apply)\n\n",
    "generic": "CORRECTIONS (retired/superseded -- reference only, do not apply):\n",
}


def _inject_header(fmt_name: str, status: str) -> str:
    headers = _INJECT_HEADERS if status == "active" else _INJECT_HEADERS_INACTIVE
    return headers[fmt_name]


def _format_entry_block(entry: dict[str, Any], fmt_name: str) -> str:
    correction = fmt(entry.get("correction"))
    trigger = fmt(entry.get("trigger_context"))
    entry_id = fmt(entry.get("id"))
    category = fmt(entry.get("category"))

    if fmt_name == "claude":
        return f"- **{entry_id}** ({category}): {correction}\n  When this applies: {trigger}\n"
    if fmt_name == "openai":
        return f"- {entry_id} [{category}]: {correction}\n  Applies when: {trigger}\n"
    return f"* {entry_id} ({category}): {correction} (applies when: {trigger})\n"


def cmd_inject(args: argparse.Namespace) -> int:
    try:
        registry = load_registry(Path(args.registry))
    except (FileNotFoundError, RegistryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = [e for e in registry["entries"] if e.get("status") == args.status]
    if args.category:
        entries = [e for e in entries if e.get("category") == args.category]

    if not entries:
        print(f"# No {args.status} corrections found.")
        return 0

    out = [_inject_header(args.format, args.status)]
    for entry in entries:
        out.append(_format_entry_block(entry, args.format))
    print("\n".join(out))
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# argument parsing
# ─────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corrections",
        description="A CLI for a structured corrections registry: capture, search, audit, and load "
        "cross-session corrections for agent workflows.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="Find entries matching a keyword or phrase.")
    p_search.add_argument("query", help="Keyword or phrase to search for.")
    p_search.add_argument("--registry", default="corrections-registry.yaml", help="Path to the registry YAML file.")
    p_search.add_argument("--top-k", type=int, default=5, help="Maximum results to return (default: 5).")
    p_search.add_argument("--status", default=None, choices=VALID_STATUSES, help="Only search entries with this status.")
    p_search.set_defaults(func=cmd_search)

    p_add = sub.add_parser("add", help="Create a new entry (auto-write for factual, approval-gated for behavioral).")
    p_add.add_argument("--registry", default="corrections-registry.yaml", help="Path to the registry YAML file.")
    p_add.add_argument("--id", help="Unique kebab-case identifier for the new entry.")
    p_add.add_argument("--category", help="Category label (free text, e.g. voice, architectural, constraint).")
    p_add.add_argument("--severity", choices=VALID_SEVERITIES, help="factual (auto-write) or behavioral (approval-gated).")
    p_add.add_argument("--trigger-context", help="When this correction is likely to matter.")
    p_add.add_argument("--correction", help="The rule to follow, as a positive instruction.")
    p_add.add_argument("--approve", action="store_true", help="Confirm a behavioral entry non-interactively.")
    p_add.add_argument("--dry-run", action="store_true", help="Show what would be added without writing the file.")
    p_add.set_defaults(func=cmd_add)

    p_audit = sub.add_parser("audit", help="Validate the registry's schema and report stale / promotion-candidate entries.")
    p_audit.add_argument("--registry", default="corrections-registry.yaml", help="Path to the registry YAML file.")
    p_audit.add_argument("--as-of", default=None, help="Reference date (YYYY-MM-DD) for staleness. Defaults to today.")
    p_audit.add_argument("--stale-days", type=int, default=30, help="Days without a fire before an entry is 'stale' (default: 30).")
    p_audit.add_argument("--promotion-threshold", type=int, default=5, help="times_fired at or above which an entry is a promotion candidate (default: 5).")
    p_audit.set_defaults(func=cmd_audit)

    p_inject = sub.add_parser("inject", help="Emit a session-preamble context block of active entries.")
    p_inject.add_argument("--registry", default="corrections-registry.yaml", help="Path to the registry YAML file.")
    p_inject.add_argument("--format", choices=list(_INJECT_HEADERS), default="generic", help="Output format (default: generic).")
    p_inject.add_argument("--status", default="active", choices=VALID_STATUSES, help="Only inject entries with this status (default: active).")
    p_inject.add_argument("--category", default=None, help="Only inject entries in this category.")
    p_inject.set_defaults(func=cmd_inject)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Redirected/piped output on a non-UTF-8 codepage (Windows' default) crashes on
    # any non-ASCII content in a registry entry. Reconfigure stdout to UTF-8
    # defensively; guarded because not every stdout stream (one already detached, or
    # replaced by a test harness) supports reconfigure().
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
