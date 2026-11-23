#!/usr/bin/env python3
"""
corrections: a CLI for a structured corrections registry.

Commands:
  search       Find entries matching a keyword or phrase.
  add          Create a new entry (auto-write for factual, approval-gated for behavioral).
  audit        Validate the schema; report promotion candidates, review-due entries, seeds whose
               assumed rule is absent, and, with --recount, meta counts that drifted.
  inject       Emit a session-preamble context block of active entries (--max caps it).
  fire         Record one recurrence of an existing entry on the entry itself.
  retire       Close an entry's validity window (retired, or superseded by a rule). Never deletes.
  migrate-key  Rename a registry's top-level list key to `entries:`, changing only that line.
  split        Move retired and superseded entries into an archive file verbatim; --verify checks.
  compare      Report likely duplicate lessons across two registries by trigger overlap.

Writes happen only through a named write verb (add, fire, retire, migrate-key, split) or an
explicit --write flag (audit --recount --write). Every other command only reads. fire, retire,
migrate-key, split and audit --recount --write edit the lines they change and leave every other
byte alone; when a file's layout makes that unsafe, they refuse and write nothing rather than
rewrite the whole registry. add splices the new entry in without touching existing text, and
falls back to a full rewrite only for an unusual layout that carries no comments to lose.

Run `corrections_cli.py <command> --help` for the full option list of any command.

Requires Python 3.9+ and PyYAML. No other dependencies. No network calls.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

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

# Optional text fields this CLI type-checks when present (see schema/corrections-schema.yaml).
# `pattern` predates them and stays unchecked, exactly as before.
_OPTIONAL_TEXT_FIELDS = ("mistake", "why", "assumes_rule", "retired_reason")
_DATE_FIELDS = ("last_fired", "origin_date", "valid_until")

# Statuses that belong in an archive file rather than the live registry.
_ARCHIVE_STATUSES = ("retired", "superseded")

# Meta keys `audit --recount` knows how to derive. A total key counts every entry; the
# status keys count entries in that status. Only keys a registry already states are checked.
_TOTAL_COUNT_KEYS = ("total_corrections", "entry_count", "total")

# `inject --max` order: the approval-gated tier first, because a behavioral correction is the
# costlier one to miss. Unknown severities sort last.
_SEVERITY_RANK = {"behavioral": 0, "factual": 1}

_SECTION_SIGN = "§"


# ─────────────────────────────────────────────────────────────────────────────
# Registry I/O
# ─────────────────────────────────────────────────────────────────────────────


class RegistryError(ValueError):
    """
    A registry file that cannot be trusted to mean what it says.

    Raised when a registry file exists but is not valid UTF-8, fails to parse
    as YAML, or parses to something that is not a registry (not a mapping,
    `entries` is present but is not a list, an item inside `entries` is not
    a mapping, or there is no `entries` key while another top-level key holds
    a list). This registry is hand-edited, and the failure modes of
    hand-written YAML are quiet ones -- catch them here, with a message that
    names the file and the problem, rather than three commands downstream as a
    raw traceback.
    """


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    """
    Read and parse a registry file into its top-level mapping, with no checks on
    `entries`. Raises RegistryError when the file cannot be read, is not UTF-8, does
    not parse as YAML, or does not parse to a mapping.
    """
    try:
        with open(path, encoding="utf-8") as f:
            try:
                text = f.read()
                node = yaml.compose(text, Loader=_YAML_LOADER)
                data = yaml.load(text, Loader=_YAML_LOADER) or {}
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
    _refuse_duplicate_top_level_keys(path, node)
    return data


def _refuse_duplicate_top_level_keys(path: Path, node: Any) -> None:
    """
    Raise RegistryError when a top-level key appears more than once. A YAML parser
    keeps only the last occurrence, so a registry with two `entries:` keys reads as
    whatever the second one holds: an empty second list reads the file as "0/0
    valid", and a write through the parsed data would drop the first list's entries.
    """
    if not isinstance(node, yaml.MappingNode):
        return
    seen: dict[str, int] = {}
    for key_node, _ in node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        line = key_node.start_mark.line + 1
        if key_node.value in seen:
            raise RegistryError(
                f"{path}: the top-level key `{key_node.value}:` appears more than once "
                f"(lines {seen[key_node.value]} and {line}). A YAML parser keeps only the "
                f"last one, so everything under the earlier one would be read as missing. "
                f"Merge them into one `{key_node.value}:` key by hand."
            )
        seen[key_node.value] = line


def _entry_like(value: Any) -> bool:
    return isinstance(value, dict) and any(k in value for k in ("id", "trigger_context", "correction"))


def _misplaced_entry_lists(data: dict[str, Any]) -> list[Any]:
    """
    Top-level keys, other than `entries` and `meta`, where entries may be hiding: a
    list holding at least one entry-like mapping, or a non-empty mapping whose every
    value is an entry-like mapping.
    """
    found = []
    for k, v in data.items():
        if k in ("entries", "meta"):
            continue
        # A list counts only when it holds an entry-like mapping: a plain config list
        # (`categories: [design, code]`) beside an empty `entries:` is not hidden entries.
        if isinstance(v, list) and any(_entry_like(x) for x in v):
            found.append(k)
        elif isinstance(v, dict) and v and all(_entry_like(x) for x in v.values()):
            found.append(k)
    return found


def _hidden_entries_message(path: Path, data: dict[str, Any], keys: list[Any]) -> str:
    def describe(k: Any) -> str:
        v = data[k]
        if isinstance(v, list):
            return f"`{k}:` ({len(v)} item(s))"
        return f"`{k}:` (a mapping of {len(v)} entry-like mapping(s))"

    found = ", ".join(describe(k) for k in keys)
    first = keys[0]
    where = "`entries:` is empty" if "entries" in data else "there is no `entries:` key"
    if not isinstance(data[first], list):
        fix = "Entries must be a list under `entries:`; convert that mapping by hand."
    elif "entries" in data:
        fix = (
            f"Delete the empty `entries:` line, then rename the key in place with: "
            f"corrections_cli.py migrate-key --from {first} --registry {path}"
        )
    else:
        fix = (
            f"Rename the key in place (only that line changes) with: "
            f"corrections_cli.py migrate-key --from {first} --registry {path}"
        )
    return (
        f"{path}: {where}, but the top level holds {found}. This CLI reads entries only from "
        f"`entries:`, so it would have read this registry as empty. {fix}"
    )


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
    mapping, `entries` is present but is not a list, an item inside `entries`
    is not a mapping, or `entries` is absent while another top-level key holds
    a list).
    """
    if not path.exists():
        if create_if_missing:
            return {"meta": {}, "entries": []}
        raise FileNotFoundError(f"registry not found: {path}")

    data = _load_yaml_mapping(path)

    # No entries under `entries:` (absent, null, or an empty list), but entries under some
    # other key: they are almost certainly there, under a name this CLI does not read (a
    # `corrections:` list, say). Reading that file as an empty registry would audit it as
    # a clean "0/0 valid" and inject nothing, a false-clean on exactly the file that needs
    # attention. Fail loud and name the key, with the fix.
    entries = data.get("entries")
    if entries is None or (isinstance(entries, list) and not entries):
        hidden = _misplaced_entry_lists(data)
        if hidden:
            raise RegistryError(_hidden_entries_message(path, data, hidden))

    # Distinguish absent/null from present-but-wrong-type. `entries:` with
    # nothing under it (or no `entries:` key and no other list) is an empty
    # registry -- a legitimate starting state. But a present-but-falsy wrong type
    # ({}, "", 0, false) is a malformed registry, and `data.get("entries") or []`
    # would silently coerce every one of those to [], auditing as a clean "0/0
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
    on _YAML_LOADER for why), which does not preserve hand-written comments.
    `add` does not lose them: on an existing file it appends through
    _append_preserving_comments, which splices only the new entry's text, and it
    calls this function only for a brand-new file or for an unusual layout that
    has no comments to lose (with comments, it refuses and writes nothing).
    The other write verbs never call it: they edit lines in place or refuse.
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


# Comment-preserving append. `add` splices the new entry's text into the
# existing file instead of re-dumping the whole registry, so hand-written
# comments (a file header, inline notes, a footer) survive. save_registry above
# still does the full rewrite; `add` only uses it for a brand-new file, or for an
# unusual shape that carries no comments to lose.

_ENTRIES_LINE = re.compile(r"^entries:[ \t]*(?P<flow>\[[ \t]*\])?[ \t]*(?P<comment>#.*)?$")
_ITEM_LINE = re.compile(r"^(?P<indent>[ \t]*)- ")


def _has_comments(text: str) -> bool:
    """True if any line carries a YAML comment. Errs toward True (a '#' inside a
    quoted string counts), which only makes `add` refuse more often, never lose more."""
    return any(re.search(r"(^|\s)#", line) for line in text.splitlines())


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _splice_entry(text: str, entry: dict[str, Any]) -> str | None:
    """
    Return `text` with `entry` appended to its top-level `entries:` list, every
    other byte unchanged, or None when the file's shape is not one this splice
    handles safely (no single top-level `entries:` line, or entries written in
    flow style with items). The caller verifies the result before writing.
    """
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    hits = [i for i, line in enumerate(lines) if _ENTRIES_LINE.match(line.rstrip("\r\n"))]
    if len(hits) != 1:
        return None
    head = hits[0]
    m = _ENTRIES_LINE.match(lines[head].rstrip("\r\n"))
    block = yaml.safe_dump([entry], sort_keys=False, default_flow_style=False, allow_unicode=True)

    if m.group("flow"):  # `entries: []`
        comment = f"  {m.group('comment')}" if m.group("comment") else ""
        lines[head] = f"entries:{comment}\n"
        return "".join(lines[: head + 1]) + block + "".join(lines[head + 1:])

    # Block style: find the list's item indent and its last content line.
    def is_filler(line: str) -> bool:
        s = line.strip()
        return not s or s.startswith("#")

    indent = None
    last = head
    for i in range(head + 1, len(lines)):
        line = lines[i]
        if is_filler(line):
            continue
        item = _ITEM_LINE.match(line)
        if indent is None:
            if not item:
                break  # `entries:` is empty (null) and the next key follows
            indent = item.group("indent")
        if (item and item.group("indent") == indent) or _indent_of(line) > len(indent):
            last = i
            continue
        break
    pad = indent or ""
    block = "".join(pad + b if b.strip() else b for b in block.splitlines(keepends=True))
    return "".join(lines[: last + 1]) + block + "".join(lines[last + 1:])


def _append_preserving_comments(path: Path, entry: dict[str, Any]) -> bool:
    """
    Append `entry` to an existing registry without discarding comments. Returns
    True when written, False when the splice could not be done safely (nothing is
    written then). A splice is written only if re-parsing it shows exactly one
    change: `entry` appended to `entries`, every other key identical.
    """
    text = path.read_text(encoding="utf-8")
    new_text = _splice_entry(text, entry)
    if new_text is None:
        return False
    try:
        before = yaml.load(text, Loader=_YAML_LOADER) or {}
        after = yaml.load(new_text, Loader=_YAML_LOADER) or {}
    except yaml.YAMLError:
        return False
    if not isinstance(after, dict):
        return False
    rest_before = {k: v for k, v in before.items() if k != "entries"}
    rest_after = {k: v for k, v in after.items() if k != "entries"}
    if rest_before != rest_after or after.get("entries") != (before.get("entries") or []) + [entry]:
        return False
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Line-level editing. The write verbs below (fire, retire, migrate-key, split,
# audit --recount --write) change only the lines they must, then re-parse the
# result and write it only if the parse shows exactly the intended change. A file
# whose layout defeats that check is refused, never rewritten wholesale: a
# wholesale rewrite reflows every hand-written scalar and drops every comment,
# which is the registry rot this lifecycle exists to prevent.
# ─────────────────────────────────────────────────────────────────────────────


def _read_text_exact(path: Path) -> str:
    """Read a file with its line endings intact (newline="" keeps CRLF as CRLF)."""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _write_text_atomic(path: Path, text: str) -> None:
    """Write `text` byte-for-byte (no newline translation) via a same-directory temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _parse_text(text: str) -> Any:
    data = yaml.load(text, Loader=_YAML_LOADER)
    return data if data is not None else {}


_BOM = "﻿"


def _split_bom(text: str) -> tuple[str, str]:
    """Separate a leading UTF-8 byte-order mark so line matching sees column 0; callers put it back."""
    return (_BOM, text[1:]) if text.startswith(_BOM) else ("", text)


class _EditRefused(Exception):
    """A line edit that cannot be made without touching a line it was not asked to change."""


def _eol(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _body(line: str) -> str:
    return line.rstrip("\r\n")


def _file_eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _is_filler(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


def _yaml_scalar(value: Any) -> str:
    """Render one scalar as a single line of YAML."""
    out = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=2**31 - 1)
    if out.endswith("\n...\n"):
        out = out[: -len("\n...\n")]
    out = out.rstrip("\n")
    if "\n" in out:
        # A string the dumper folded across lines. JSON's double-quoted form is
        # valid YAML and always one line.
        out = json.dumps(value, ensure_ascii=False)
    return out


def _render_field(key: str, value: Any) -> str:
    return f"{key}: {_yaml_scalar(value)}"


def _trailing_comment(value_text: str, original_value: Any) -> str:
    """
    The inline comment after a field's value, when it can be separated safely: the
    text before some ` #` must re-parse to the field's original value. Returns "" when
    there is none or the split is ambiguous (the comment is then not carried over).
    """
    for m in re.finditer(r"[ \t]+#", value_text):
        val = value_text[: m.start()].strip()
        try:
            parsed = yaml.load(f"v: {val}" if val else "v:", Loader=_YAML_LOADER)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict) and parsed.get("v") == original_value:
            return value_text[m.start():]
    return ""


def _print_diff(path: Path, old_text: str, new_text: str) -> None:
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=str(path),
        tofile=f"{path} (after)",
        n=0,
    )
    for line in diff:
        sys.stdout.write(line if line.endswith("\n") else line + "\n")


@dataclass
class _Item:
    """One entry's line range in a registry file: [start, end), and the column its keys use."""

    start: int
    end: int
    key_col: int


@dataclass
class _EntriesBlock:
    head: int
    flow_empty: bool
    indent: Optional[str]
    items: list = field(default_factory=list)


def _item_key_col(lines: list[str], start: int, end: int, indent: str) -> int:
    first = _body(lines[start])
    after_dash = len(indent) + 1
    rest = first[after_dash:]
    stripped = rest.lstrip(" ")
    if stripped and not stripped.startswith("#"):
        return after_dash + (len(rest) - len(stripped))
    for j in range(start + 1, end):
        if not _is_filler(lines[j]):
            return _indent_of(lines[j])
    return after_dash + 1


def _locate_entries(lines: list[str], data: dict[str, Any]) -> Optional[_EntriesBlock]:
    """
    Find the top-level `entries:` list and each item's line range. Returns None when
    the layout is not one the line-level verbs handle (no single block-style `entries:`
    line, or an item count that disagrees with the parse).
    """
    hits = [i for i, ln in enumerate(lines) if _ENTRIES_LINE.match(_body(ln))]
    if len(hits) != 1:
        return None
    head = hits[0]
    m = _ENTRIES_LINE.match(_body(lines[head]))
    expected = len(data.get("entries") or [])
    if m.group("flow"):
        return _EntriesBlock(head, True, None, []) if expected == 0 else None

    indent: Optional[str] = None
    bounds: list[tuple[int, int]] = []
    start: Optional[int] = None
    last = head
    for i in range(head + 1, len(lines)):
        line = lines[i]
        if _is_filler(line):
            continue
        item = _ITEM_LINE.match(line)
        if indent is None:
            if not item:
                break
            indent = item.group("indent")
        if item and item.group("indent") == indent:
            if start is not None:
                bounds.append((start, last + 1))
            start = i
            last = i
            continue
        if _indent_of(line) > len(indent):
            last = i
            continue
        break
    if start is not None:
        bounds.append((start, last + 1))
    if len(bounds) != expected:
        return None
    items = [_Item(s, e, _item_key_col(lines, s, e, indent or "")) for s, e in bounds]
    return _EntriesBlock(head, False, indent, items)


def _set_item_fields(
    lines: list[str], item: _Item, updates: dict[str, Any], original: dict[str, Any]
) -> list[str]:
    """New lines for one item's range with each field in `updates` set (replaced or appended)."""
    block = list(lines[item.start:item.end])
    default_eol = _eol(block[0]) or "\n"
    col = item.key_col
    for key, value in updates.items():
        rendered = _render_field(key, value)
        key_re = re.compile(re.escape(key) + r":(?:[ \t]|$)")
        idx = None
        for j, ln in enumerate(block):
            text = _body(ln)
            if j > 0 and (_is_filler(ln) or _indent_of(text) != col):
                continue
            if key_re.match(text[col:]):
                idx = j
                break
        if idx is None:
            if not _eol(block[-1]):
                block[-1] += default_eol
            block.append(" " * col + rendered + default_eol)
            continue
        k = idx + 1
        while k < len(block) and (not block[k].strip() or _indent_of(_body(block[k])) > col):
            k += 1
        while k > idx + 1 and not block[k - 1].strip():
            k -= 1
        text = _body(block[idx])
        # A re-parse cannot see comments, so it cannot catch one being deleted. Inside a
        # block scalar (`|` or `>`), a line starting with `#` is content and is replaced
        # with the value; anywhere else it is a comment, and the edit is refused.
        if not text[col + len(key) + 1:].strip().startswith(("|", ">")) and any(
            block[m].strip().startswith("#") for m in range(idx + 1, k)
        ):
            raise _EditRefused(f"a comment line sits inside the lines of `{key}`")
        comment = ""
        if k == idx + 1:
            comment = _trailing_comment(text[col + len(key) + 1:], original.get(key))
        elif re.match(r"\s*(?:[|>][-+0-9]*\s*)?#", text[col + len(key) + 1:]):
            # A multi-line value whose key line carries a comment (`key: > # note`): the
            # rewritten single line would drop it, and a re-parse cannot see that.
            raise _EditRefused(f"a comment sits on the `{key}:` line of a multi-line value")
        block[idx:k] = [text[:col] + rendered + comment + _eol(block[k - 1])]
    return block


def _edit_entry_text(text: str, index: int, updates: dict[str, Any]) -> tuple[Optional[str], str]:
    """Return (new_text, "") with entry `index` updated in place, or (None, why it was refused)."""
    bom, text = _split_bom(text)
    before = _parse_text(text)
    lines = text.splitlines(keepends=True)
    block = _locate_entries(lines, before)
    if block is None or index >= len(block.items):
        return None, "its `entries:` list is in a layout this command cannot edit line by line"
    item = block.items[index]
    try:
        new_block = _set_item_fields(lines, item, updates, before["entries"][index])
    except _EditRefused as exc:
        return None, str(exc)
    new_text = "".join(lines[: item.start] + new_block + lines[item.end:])
    expected = copy.deepcopy(before)
    expected["entries"][index].update(updates)
    try:
        after = _parse_text(new_text)
    except yaml.YAMLError:
        return None, "the edited lines did not re-parse"
    if after != expected:
        return None, "the line edit would have changed more than the named fields"
    return bom + new_text, ""


_META_LINE = re.compile(r"^meta:[ \t]*(?:#.*)?$")


def _edit_meta_text(text: str, updates: dict[str, Any]) -> tuple[Optional[str], str]:
    """Return (new_text, "") with the named top-level `meta:` keys set in place, or (None, why)."""
    bom, text = _split_bom(text)
    before = _parse_text(text)
    lines = text.splitlines(keepends=True)
    hits = [i for i, ln in enumerate(lines) if _META_LINE.match(_body(ln))]
    if len(hits) != 1:
        return None, "there is no single block-style top-level `meta:` line"
    head = hits[0]
    col = None
    end = len(lines)
    for i in range(head + 1, len(lines)):
        if _is_filler(lines[i]):
            continue
        ind = _indent_of(lines[i])
        if ind == 0:
            end = i
            break
        if col is None:
            col = ind
    if col is None:
        return None, "`meta:` has no keys to edit"
    original_meta = before.get("meta") if isinstance(before.get("meta"), dict) else {}
    for key, value in updates.items():
        key_re = re.compile(re.escape(str(key)) + r":(?:[ \t]|$)")
        idx = None
        for i in range(head + 1, end):
            if _is_filler(lines[i]):
                continue
            t = _body(lines[i])
            if _indent_of(t) == col and key_re.match(t[col:]):
                idx = i
                break
        if idx is None:
            return None, f"`meta.{key}` is not on a line of its own"
        t = _body(lines[idx])
        comment = _trailing_comment(t[col + len(str(key)) + 1:], original_meta.get(key))
        lines[idx] = t[:col] + _render_field(str(key), value) + comment + _eol(lines[idx])
    new_text = "".join(lines)
    expected = copy.deepcopy(before)
    expected.setdefault("meta", {})
    expected["meta"].update(updates)
    try:
        after = _parse_text(new_text)
    except yaml.YAMLError:
        return None, "the edited lines did not re-parse"
    if after != expected:
        return None, "the line edit would have changed more than the named counts"
    return bom + new_text, ""


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


def _supersedes_ids(entry: dict[str, Any]) -> list[str]:
    """The ids an entry's `supersedes` names, as a list (empty when absent or malformed)."""
    value = entry.get("supersedes")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


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

    for date_field in _DATE_FIELDS:
        try:
            _parse_date(entry.get(date_field))
        except (ValueError, TypeError):
            errors.append(f"{entry_id}: {date_field} must be null or 'YYYY-MM-DD', got {entry.get(date_field)!r}")

    if status == "superseded" and not entry.get("superseded_by"):
        errors.append(f"{entry_id}: status is 'superseded' but superseded_by is empty")

    if status == "active" and entry.get("valid_until") is not None:
        errors.append(
            f"{entry_id}: status is 'active' but valid_until is set -- an active entry's "
            f"validity window is still open; clear valid_until or retire the entry"
        )

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

    for text_field in _OPTIONAL_TEXT_FIELDS:
        value = entry.get(text_field)
        if value is not None and not isinstance(value, str):
            errors.append(f"{entry_id}: {text_field} must be text, got {type(value).__name__}")

    supersedes = entry.get("supersedes")
    if supersedes is not None:
        refs = [supersedes] if isinstance(supersedes, str) else supersedes
        if not isinstance(refs, list) or not refs or not all(isinstance(r, str) and r.strip() for r in refs):
            errors.append(f"{entry_id}: supersedes must be an entry id or a list of entry ids, got {supersedes!r}")
        elif entry.get("id") in refs:
            errors.append(f"{entry_id}: supersedes names the entry itself")

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
# Shared command helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_or_report(path: Path) -> Optional[dict[str, Any]]:
    """load_registry, printing the error and returning None on failure."""
    try:
        return load_registry(path)
    except (FileNotFoundError, RegistryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _find_entry(entries: list[dict[str, Any]], entry_id: str) -> tuple[Optional[int], str]:
    hits = [i for i, e in enumerate(entries) if e.get("id") == entry_id]
    if not hits:
        return None, f"no entry with id {entry_id!r}"
    if len(hits) > 1:
        return None, f"id {entry_id!r} appears {len(hits)} times; fix the duplicate first"
    return hits[0], ""


def _date_arg(value: Optional[str]) -> tuple[Optional[str], str]:
    """An ISO date from a --date flag (default today), or (None, the error)."""
    if not value:
        return date.today().isoformat(), ""
    try:
        return _parse_date(value).isoformat(), ""
    except ValueError:
        return None, f"--date {value!r} is not a valid date; expected YYYY-MM-DD."


def _approve_behavioral(entry_id: str, action: str) -> Optional[int]:
    """The approval gate for changing a behavioral entry: None to proceed, else an exit code."""
    reply = None
    if sys.stdin.isatty():
        try:
            reply = input(
                f"'{entry_id}' is a behavioral correction. {action} changes what agents are told, "
                f"so it needs a human sign-off. Go ahead? [y/N] "
            ).strip().lower()
        except EOFError:
            reply = None
    if reply is None:
        print(
            "error: behavioral corrections require human approval. Pass --approve to confirm, "
            "or run interactively.",
            file=sys.stderr,
        )
        return 1
    if reply not in ("y", "yes"):
        print("Not changed.")
        return 1
    return None


def _derived_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    statuses = [e.get("status") for e in entries]
    out = {k: len(entries) for k in _TOTAL_COUNT_KEYS}
    for status in VALID_STATUSES:
        out[status] = statuses.count(status)
    return out


def _default_archive(path: Path) -> Path:
    """The archive file `split` uses for a registry by default: `<stem>-archive<suffix>` beside it."""
    return path.with_name(f"{path.stem}-archive{path.suffix}")


def _stated_count_keys(meta: Any) -> list[Any]:
    """The count keys a meta block states, in the meta block's own order."""
    if not isinstance(meta, dict):
        return []
    known = set(_TOTAL_COUNT_KEYS) | set(VALID_STATUSES)
    return [k for k in meta if k in known]


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


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

    # An existing file gets the comment-preserving splice. The full rewrite below
    # stays the path for a new file, or for an unusual layout that carries no
    # comments to lose.
    if registry_path.exists():
        if _append_preserving_comments(registry_path, entry):
            print(f"Added '{args.id}' to {registry_path}.")
            return 0
        if _has_comments(registry_path.read_text(encoding="utf-8")):
            print(
                f"error: could not append to {registry_path} without discarding its comments "
                f"(an unusual layout for `entries:`). Nothing was written; add the entry by hand.",
                file=sys.stderr,
            )
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


def _report_recount(
    path: Path,
    registry: dict[str, Any],
    archive_entries: list[dict[str, Any]],
    archive_path: Optional[Path],
    write: bool,
) -> int:
    """
    Print the stated-versus-derived meta counts. Returns 1 on drift that was not fixed,
    0 otherwise. With `write`, fixes drifted counts in place.
    """
    meta = registry.get("meta")
    derived = _derived_counts(registry["entries"] + archive_entries)
    scope = f"{len(registry['entries'])} entries"
    if archive_path is not None:
        scope += f" + {len(archive_entries)} in {archive_path}"
    print(f"\nMeta counts (stated vs. derived from {scope}):")
    keys = _stated_count_keys(meta)
    if not keys:
        print("  meta states no counts; nothing to reconcile.")
        return 0

    drift: dict[Any, int] = {}
    for key in keys:
        stated = meta[key]
        ok = _is_count(stated) and stated == derived[key]
        print(f"  {str(key):<18} stated {fmt(stated):>5}   derived {derived[key]:>5}   {'ok' if ok else 'DRIFT'}")
        if not ok:
            drift[key] = derived[key]

    if not drift:
        print("  Every stated count matches the entries.")
        return 0
    if not write:
        print(
            f"  {len(drift)} count(s) disagree with the entries. Re-run with --recount --write "
            f"to fix them in place."
        )
        return 1

    text = _read_text_exact(path)
    new_text, why = _edit_meta_text(text, drift)
    if new_text is None:
        by_hand = ", ".join(f"{k}: {v}" for k, v in drift.items())
        print(
            f"error: could not fix the meta counts in place ({why}). Nothing was written; "
            f"set {by_hand} by hand.",
            file=sys.stderr,
        )
        return 1
    _write_text_atomic(path, new_text)
    print(f"  Fixed {len(drift)} count(s) in {path}: " + ", ".join(f"{k} -> {v}" for k, v in drift.items()))
    return 0


def _split_rule_ref(ref: str) -> tuple[str, Optional[str]]:
    if _SECTION_SIGN in ref:
        path_text, section = ref.split(_SECTION_SIGN, 1)
        return path_text.strip(), section.strip() or None
    return ref.strip(), None


def _rule_presence(ref: str, root: Path) -> tuple[Optional[bool], str]:
    """
    Whether an `assumes_rule` reference resolves under `root`: True (present), False
    (absent), or None (not a file reference, so not checkable). Format: a file path,
    optionally followed by a section sign and a heading, e.g. "rules.md § Review".
    """
    path_text, section = _split_rule_ref(ref)
    if not path_text or ("/" not in path_text and "." not in path_text):
        return None, "names no file to check"
    target = root / path_text
    if not target.is_file():
        return False, f"{path_text} is not there"
    if section is None:
        return True, f"{path_text} exists"
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"{path_text} could not be read ({exc})"
    wanted = section.lower()
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#") and wanted in s.lstrip("#").strip().lower():
            return True, f"{path_text} has a heading for {section!r}"
    return False, f"{path_text} has no heading for {section!r}"


def _report_assumed_rules(entries: list[dict[str, Any]], root: Path) -> None:
    assuming = [e for e in entries if e.get("assumes_rule") and e.get("status") in ("active", "superseded")]
    if not assuming:
        return
    absent, present_active, unverifiable = [], [], []
    for e in assuming:
        present, detail = _rule_presence(str(e["assumes_rule"]), root)
        if present is None:
            unverifiable.append((e, detail))
        elif not present:
            absent.append((e, detail))
        elif e.get("status") == "active":
            present_active.append((e, detail))

    print(f"\nSeeds whose assumed rule is absent (rules root: {root}): {len(absent)}")
    for e, detail in absent:
        if e.get("status") == "superseded":
            print(
                f"  [{e['id']}] superseded, but {detail}. It is not loading and nothing enforces "
                f"it. Adopt the rule, or set status: active until you do."
            )
        else:
            print(f"  [{e['id']}] active; {detail}. It stays active until the rule is adopted.")
    if present_active:
        print(f"\nAssumed rule now present, entry still active (review for supersession): {len(present_active)}")
        for e, detail in present_active:
            print(f"  [{e['id']}] {detail}")
    if unverifiable:
        print(f"\nAssumed rule not checkable (no file path): {len(unverifiable)}")
        for e, _detail in unverifiable:
            print(f"  [{e['id']}] assumes {e['assumes_rule']!r}")


def _report_corrected_claims(entries: list[dict[str, Any]]) -> None:
    correcting = [e for e in entries if e.get("supersedes")]
    if not correcting:
        return
    by_id = {e.get("id"): e for e in entries}
    still_active, dangling = [], []
    for newer in correcting:
        for ref in _supersedes_ids(newer):
            older = by_id.get(ref)
            if older is None:
                dangling.append((newer, ref))
            elif older.get("status") == "active" and newer.get("status") == "active":
                still_active.append((newer, older))

    print(f"\nCorrected claims still active: {len(still_active)}")
    for newer, older in still_active:
        print(
            f"  [{older['id']}] is corrected by [{newer['id']}] but still loads. Retire it: "
            f"retire {older['id']} --reason \"corrected by {newer['id']}\""
        )
    if dangling:
        print(f"\nsupersedes names an id not in this registry (it may be archived): {len(dangling)}")
        for newer, ref in dangling:
            print(f"  [{newer['id']}] supersedes {ref!r}")


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
    if (args.write or args.archive or args.live_only) and not args.recount:
        print("error: --write, --archive and --live-only apply only with --recount.", file=sys.stderr)
        return 2
    if args.archive and args.live_only:
        print("error: pass --archive or --live-only, not both.", file=sys.stderr)
        return 2

    registry_path = Path(args.registry)
    if args.archive and Path(args.archive).resolve() == registry_path.resolve():
        print("error: --registry and --archive name the same file.", file=sys.stderr)
        return 2
    try:
        registry = load_registry(registry_path)
    except (FileNotFoundError, RegistryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = registry["entries"]

    archive_entries: list[dict[str, Any]] = []
    archive_path = Path(args.archive) if args.archive else None
    if archive_path is not None:
        archive = _load_or_report(archive_path)
        if archive is None:
            return 1
        archive_entries = archive["entries"]

    errors_by_id = validate_registry(entries)
    valid_count = len(entries) - len(errors_by_id)
    print(f"Schema: {valid_count}/{len(entries)} entries valid.")
    exit_code = 0
    if errors_by_id:
        for entry_id, errors in errors_by_id.items():
            print(f"  [{entry_id}]")
            for e in errors:
                print(f"    - {e}")
        exit_code = 1

    if args.recount:
        # A live file split from an archive keeps whole-registry counts. Recounting it
        # alone would "fix" those counts down to the live file's, so an archive sitting
        # next to it must be named (or the live-only reading chosen) before --write.
        sibling = _default_archive(registry_path)
        unnamed = archive_path is None and not args.live_only and sibling.is_file()
        if unnamed:
            print(
                f"\nNote: an archive sits next to this file ({sibling}); its entries are not counted. "
                f"Pass --archive {sibling} to count both files, or --live-only if the meta counts "
                f"describe this file alone."
            )
        recount_code = _report_recount(
            registry_path, registry, archive_entries, archive_path, args.write and not unnamed
        )
        if args.write and unnamed and recount_code:
            print(
                "error: not writing counts while an unnamed archive sits next to the registry "
                "(see the note above). Nothing was written.",
                file=sys.stderr,
            )
        exit_code = max(exit_code, recount_code)

    if errors_by_id:
        return exit_code

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

    _report_assumed_rules(entries, Path(args.rules_root))
    _report_corrected_claims(entries)

    return exit_code


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


def _inject_rank(indexed: tuple[int, dict[str, Any]]) -> tuple[int, int, int, int]:
    """Sort key for `inject --max`: behavioral first, then most-fired, then most recent."""
    index, entry = indexed
    severity_rank = _SEVERITY_RANK.get(entry.get("severity"), len(_SEVERITY_RANK))
    times_fired = entry.get("times_fired", 0)
    if not _is_count(times_fired):
        times_fired = 0
    try:
        last_fired = _parse_date(entry.get("last_fired"))
    except (ValueError, TypeError):
        last_fired = None
    return (severity_rank, -times_fired, -(last_fired.toordinal() if last_fired else 0), index)


def cmd_inject(args: argparse.Namespace) -> int:
    if args.max is not None and args.max < 1:
        print("error: --max must be 1 or more.", file=sys.stderr)
        return 2
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

    total = len(entries)
    if args.max is not None:
        entries = [e for _, e in sorted(enumerate(entries), key=_inject_rank)[: args.max]]

    out = [_inject_header(args.format, args.status)]
    for entry in entries:
        out.append(_format_entry_block(entry, args.format))
    if len(entries) < total:
        out.append(
            f"({len(entries)} of {total} {args.status} corrections shown; capped by --max, "
            f"behavioral first, then most-fired. Run `corrections search` to find the rest.)\n"
        )
    print("\n".join(out))
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# fire
# ─────────────────────────────────────────────────────────────────────────────


def cmd_fire(args: argparse.Namespace) -> int:
    when, err = _date_arg(args.date)
    if when is None:
        print(f"error: {err}", file=sys.stderr)
        return 2
    path = Path(args.registry)
    registry = _load_or_report(path)
    if registry is None:
        return 1
    index, err = _find_entry(registry["entries"], args.id)
    if index is None:
        print(f"error: {err} in {path}. A new pattern is `add`, not `fire`.", file=sys.stderr)
        return 1
    entry = registry["entries"][index]

    times = entry.get("times_fired", 0)
    if not _is_count(times) or times < 0:
        print(f"error: {args.id}: times_fired is {times!r}, not a non-negative integer; fix it first.", file=sys.stderr)
        return 1
    try:
        prior = _parse_date(entry.get("last_fired"))
    except (ValueError, TypeError):
        print(f"error: {args.id}: last_fired is {entry.get('last_fired')!r}, not a date; fix it first.", file=sys.stderr)
        return 1

    keep_prior = prior is not None and prior > _parse_date(when)
    updates: dict[str, Any] = {"times_fired": times + 1}
    if not keep_prior:
        updates["last_fired"] = when

    text = _read_text_exact(path)
    new_text, why = _edit_entry_text(text, index, updates)
    if new_text is None:
        print(
            f"error: could not record the fire in place ({why}). Nothing was written; set "
            f"times_fired: {times + 1} and last_fired: '{when}' on {args.id} by hand.",
            file=sys.stderr,
        )
        return 1
    if args.dry_run:
        print(f"[dry run] would record a fire of '{args.id}' in {path}:")
        _print_diff(path, text, new_text)
        return 0
    _write_text_atomic(path, new_text)

    shown_last = prior.isoformat() if keep_prior else when
    print(f"Fired '{args.id}': times_fired {times} -> {times + 1}, last_fired {shown_last}.")
    if keep_prior:
        print(f"  (last_fired stays {prior.isoformat()}: it is later than {when}.)")
    status = entry.get("status")
    if status == "superseded":
        print(
            f"Note: '{args.id}' is superseded. A fire after promotion means the rule at "
            f"{fmt(entry.get('superseded_by'))} did not catch it; review that rule."
        )
    elif status == "retired":
        print(f"Note: '{args.id}' is retired, and the pattern came back. Consider a new active entry for it.")
    elif times + 1 >= args.promotion_threshold:
        print(
            f"'{args.id}' is now a promotion candidate ({args.promotion_threshold}+ fires). "
            f"`audit` lists it with any Promotion clause."
        )
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# retire
# ─────────────────────────────────────────────────────────────────────────────


def cmd_retire(args: argparse.Namespace) -> int:
    when, err = _date_arg(args.date)
    if when is None:
        print(f"error: {err}", file=sys.stderr)
        return 2
    reason = " ".join((args.reason or "").split())
    if not reason:
        print("error: --reason must say why the entry stops applying.", file=sys.stderr)
        return 2
    superseded_by = None
    if args.superseded_by is not None:
        superseded_by = " ".join(args.superseded_by.split())
        if not superseded_by:
            print("error: --superseded-by must name where the rule now lives.", file=sys.stderr)
            return 2

    path = Path(args.registry)
    registry = _load_or_report(path)
    if registry is None:
        return 1
    index, err = _find_entry(registry["entries"], args.id)
    if index is None:
        print(f"error: {err} in {path}.", file=sys.stderr)
        return 1
    entry = registry["entries"][index]
    status = entry.get("status")
    if status != "active":
        since = f" (valid until {entry['valid_until']})" if entry.get("valid_until") else ""
        print(f"error: '{args.id}' is already {fmt(status)}{since}. Nothing to retire.", file=sys.stderr)
        return 1

    new_status = "superseded" if superseded_by else "retired"
    updates: dict[str, Any] = {"status": new_status}
    if superseded_by:
        updates["superseded_by"] = superseded_by
    updates["valid_until"] = when
    updates["retired_reason"] = reason

    candidate = dict(entry)
    candidate.update(updates)
    errors = validate_entry(candidate)
    if errors:
        print("error: the retired entry would fail schema validation:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    text = _read_text_exact(path)
    new_text, why = _edit_entry_text(text, index, updates)
    if new_text is None:
        print(f"error: could not retire '{args.id}' in place ({why}). Nothing was written.", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"[dry run] would retire '{args.id}' in {path}:")
        _print_diff(path, text, new_text)
        return 0
    if entry.get("severity") == "behavioral" and not args.approve:
        refused = _approve_behavioral(args.id, "Retiring it")
        if refused is not None:
            return refused
    _write_text_atomic(path, new_text)
    print(
        f"Retired '{args.id}': status {new_status}, valid until {when}. The entry stays in "
        f"{path} as history; `inject` no longer loads it."
    )
    print(
        "Next: `audit --recount --write` brings the meta counts in line (add `--archive <file>` "
        "if you keep an archive), and `split` moves the entry into that archive."
    )
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# migrate-key
# ─────────────────────────────────────────────────────────────────────────────


def cmd_migrate_key(args: argparse.Namespace) -> int:
    from_key = args.from_key
    if from_key == "entries":
        print("error: --from entries names the key this CLI already reads; nothing to migrate.", file=sys.stderr)
        return 2
    path = Path(args.registry)
    if not path.exists():
        print(f"error: registry not found: {path}", file=sys.stderr)
        return 1
    try:
        data = _load_yaml_mapping(path)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if "entries" in data:
        also = f" and a `{from_key}:` key; merge them by hand" if from_key in data else ""
        print(f"error: {path} already has an `entries:` key{also}. Nothing was written.", file=sys.stderr)
        return 1
    if from_key not in data:
        lists = _misplaced_entry_lists(data)
        hint = f" Top-level lists it does have: {', '.join(f'`{k}:`' for k in lists)}." if lists else ""
        print(f"error: {path} has no top-level `{from_key}:` key.{hint}", file=sys.stderr)
        return 1
    value = data[from_key]
    if value is not None and not isinstance(value, list):
        print(f"error: `{from_key}:` holds a {type(value).__name__}, not a list of entries.", file=sys.stderr)
        return 1
    for i, entry in enumerate(value or []):
        if not isinstance(entry, dict):
            print(f"error: `{from_key}:` item #{i + 1} is not a mapping; fix it before migrating.", file=sys.stderr)
            return 1

    text = _read_text_exact(path)
    bom, body_text = _split_bom(text)
    lines = body_text.splitlines(keepends=True)
    key_line = re.compile(r"^" + re.escape(from_key) + r":(?P<rest>(?:[ \t].*)?)$")
    hits = [i for i, ln in enumerate(lines) if key_line.match(_body(ln))]
    if len(hits) != 1:
        print(
            f"error: found {len(hits)} column-0 `{from_key}:` lines, expected exactly 1. "
            f"Rename the key by hand. Nothing was written.",
            file=sys.stderr,
        )
        return 1
    line_no = hits[0]
    rest = key_line.match(_body(lines[line_no])).group("rest")
    lines[line_no] = "entries:" + rest + _eol(lines[line_no])
    new_text = bom + "".join(lines)

    expected = {("entries" if k == from_key else k): v for k, v in data.items()}
    try:
        after = _parse_text(new_text)
    except yaml.YAMLError:
        after = None
    if after != expected:
        print(f"error: renaming line {line_no + 1} would change more than the key. Nothing was written.", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[dry run] would migrate {path}:")
        _print_diff(path, text, new_text)
        return 0
    _write_text_atomic(path, new_text)
    print(
        f"Migrated {path}: line {line_no + 1} `{from_key}:` -> `entries:` "
        f"({len(value or [])} entries). No other line changed."
    )
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# split
# ─────────────────────────────────────────────────────────────────────────────


def _split_invariant(live: dict[str, Any], archive: dict[str, Any]) -> list[str]:
    """
    The standing invariant a live/archive pair keeps: ids disjoint and unique, the live
    file active-only, the archive retired/superseded-only, and every count the live meta
    states equal to the count across both files. Returns the failures.
    """
    failures: list[str] = []
    live_entries, arch_entries = live["entries"], archive["entries"]
    live_ids = [str(e.get("id")) for e in live_entries]
    arch_ids = [str(e.get("id")) for e in arch_entries]

    both = sorted(set(live_ids) & set(arch_ids))
    if both:
        failures.append(f"{len(both)} id(s) in BOTH files: {', '.join(both[:5])}")
    for label, ids in (("live", live_ids), ("archive", arch_ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            failures.append(f"duplicate id(s) within the {label} file: {', '.join(dupes[:5])}")

    misplaced_live = [str(e.get("id")) for e in live_entries if e.get("status") != "active"]
    if misplaced_live:
        failures.append(
            f"{len(misplaced_live)} non-active entry(s) still in the live file (run `split`): "
            f"{', '.join(misplaced_live[:5])}"
        )
    misplaced_arch = [str(e.get("id")) for e in arch_entries if e.get("status") not in _ARCHIVE_STATUSES]
    if misplaced_arch:
        failures.append(
            f"{len(misplaced_arch)} entry(s) in the archive that are neither retired nor superseded: "
            f"{', '.join(misplaced_arch[:5])}"
        )

    meta = live.get("meta")
    derived = _derived_counts(live_entries + arch_entries)
    for key in _stated_count_keys(meta):
        if not (_is_count(meta[key]) and meta[key] == derived[key]):
            failures.append(f"live meta.{key} = {fmt(meta[key])}, but the two files hold {derived[key]}")
    return failures


def _print_invariant(live_path: Path, live: dict[str, Any], archive_path: Path, archive: dict[str, Any]) -> int:
    failures = _split_invariant(live, archive)
    n_live, n_arch = len(live["entries"]), len(archive["entries"])
    if failures:
        print(f"Split invariant FAILED ({live_path} + {archive_path}):")
        for f in failures:
            print(f"  - {f}")
        if all(f.startswith("live meta.") for f in failures):
            print(f"  Only counts drifted: `audit --registry {live_path} --recount --write --archive {archive_path}` fixes them.")
        return 1
    print(
        f"Split invariant OK: live {n_live} + archive {n_arch} = {n_live + n_arch}; ids disjoint, "
        f"statuses placed, stated meta counts agree."
    )
    return 0


def _reindent(span: list[str], from_n: int, to_n: int) -> tuple[Optional[list[str]], str]:
    if from_n == to_n:
        return list(span), ""
    out = []
    for ln in span:
        if not ln.strip():
            out.append(ln)
        elif to_n > from_n:
            out.append(" " * (to_n - from_n) + ln)
        elif ln.startswith(" " * (from_n - to_n)):
            out.append(ln[from_n - to_n:])
        else:
            return None, "an entry line is indented less than its list; re-indent by hand"
    return out, ""


def _append_spans(
    text: str, data: dict[str, Any], spans: list[list[str]], src_indent: str, eol: str, sep: bool
) -> tuple[Optional[str], str]:
    """Append entry spans to an existing archive's `entries:` list, in the archive's line endings."""
    if "\n" in text:
        eol = _file_eol(text)
    spans = [[_body(ln) + eol for ln in span] for span in spans]
    lines = text.splitlines(keepends=True)
    if lines and not _eol(lines[-1]):
        lines[-1] += eol
    block = _locate_entries(lines, data)
    if block is None:
        return None, "the archive's `entries:` list is in a layout this command cannot append to"
    if block.flow_empty:
        m = _ENTRIES_LINE.match(_body(lines[block.head]))
        comment = f"  {m.group('comment')}" if m.group("comment") else ""
        lines[block.head] = "entries:" + comment + (_eol(lines[block.head]) or eol)
        insert_at, target, arch_sep = block.head + 1, src_indent, sep
    elif block.items:
        last = block.items[-1]
        insert_at, target = last.end, block.indent or ""
        arch_sep = last.start > 0 and not lines[last.start - 1].strip()
    else:
        insert_at, target, arch_sep = block.head + 1, src_indent, sep
    added: list[str] = []
    for span in spans:
        shifted, why = _reindent(span, len(src_indent), len(target))
        if shifted is None:
            return None, why
        added.extend(([eol] if arch_sep else []) + shifted)
    lines[insert_at:insert_at] = added
    return "".join(lines), ""


def cmd_split(args: argparse.Namespace) -> int:
    live_path = Path(args.registry)
    archive_path = Path(args.archive) if args.archive else _default_archive(live_path)
    if live_path.resolve() == archive_path.resolve():
        print("error: --registry and --archive name the same file.", file=sys.stderr)
        return 2

    live = _load_or_report(live_path)
    if live is None:
        return 1
    archive = None
    if archive_path.exists():
        archive = _load_or_report(archive_path)
        if archive is None:
            return 1

    if args.verify:
        if archive is None:
            print(f"error: archive not found: {archive_path}", file=sys.stderr)
            return 1
        return _print_invariant(live_path, live, archive_path, archive)

    live_entries = live["entries"]
    move_idx = [i for i, e in enumerate(live_entries) if e.get("status") in _ARCHIVE_STATUSES]
    if not move_idx:
        print(f"Nothing to move: {live_path} holds no retired or superseded entries ({len(live_entries)} entries).")
        return _print_invariant(live_path, live, archive_path, archive) if archive is not None else 0

    for label, reg, reg_path in (("live", live, live_path), ("archive", archive, archive_path)):
        if reg is None:
            continue
        dupes = sorted(str(k) for k, n in Counter(e.get("id") for e in reg["entries"]).items() if n > 1)
        if dupes:
            print(
                f"error: the {label} file {reg_path} repeats id(s) {', '.join(dupes)}; fix the "
                f"duplicate first. Nothing was written.",
                file=sys.stderr,
            )
            return 1

    moved = [live_entries[i] for i in move_idx]
    archive_ids = {e.get("id") for e in archive["entries"]} if archive else set()
    clash = [str(e.get("id")) for e in moved if e.get("id") in archive_ids]
    if clash:
        print(
            f"error: {', '.join(clash)} already in {archive_path}; resolve the duplicate by hand. "
            f"Nothing was written.",
            file=sys.stderr,
        )
        return 1

    live_bom, live_text = _split_bom(_read_text_exact(live_path))
    live_raw = _parse_text(live_text)
    live_lines = live_text.splitlines(keepends=True)
    eol = _file_eol(live_text)
    block = _locate_entries(live_lines, live_raw)
    if block is None or block.flow_empty:
        print(
            f"error: {live_path}'s `entries:` list is in a layout `split` cannot move line by line. "
            f"Nothing was written.",
            file=sys.stderr,
        )
        return 1

    spans: list[list[str]] = []
    removed: set[int] = set()
    sep = False
    for i in move_idx:
        item = block.items[i]
        span = list(live_lines[item.start:item.end])
        if not _eol(span[-1]):
            span[-1] += eol
        spans.append(span)
        preceded_by_blank = item.start > 0 and not live_lines[item.start - 1].strip()
        sep = sep or preceded_by_blank
        rm_end = item.end
        if preceded_by_blank:
            while rm_end < len(live_lines) and not live_lines[rm_end].strip():
                rm_end += 1
        removed.update(range(item.start, rm_end))
    new_live_text = live_bom + "".join(ln for j, ln in enumerate(live_lines) if j not in removed)

    src_indent = block.indent or ""
    reindent_note = ""
    if archive is None:
        head = [ln for ln in live_lines[: block.head] if re.match(r"^classification:", ln)][:1]
        counts = _derived_counts(moved)
        meta_lines = ["meta:", f"  archived_from: {_yaml_scalar(live_path.name)}"]
        meta_lines += [f"  {k}: {counts[k]}" for k in _stated_count_keys(live.get("meta"))]
        header = [
            "# Archive of retired and superseded corrections, moved here verbatim by",
            f"# `corrections_cli.py split` from {live_path.name}, so that file loads active",
            "# entries only. This file's meta counts describe this file. The live file's meta",
            "# keeps the whole-registry counts: check them with `audit --recount --archive`.",
        ]
        arch_text = "".join(head) + eol.join(header + meta_lines + ["entries:"]) + eol
        for span in spans:
            arch_text += (eol if sep else "") + "".join(span)
        archive_before: list[dict[str, Any]] = []
    else:
        arch_bom, old_arch_text = _split_bom(_read_text_exact(archive_path))
        old_arch_raw = _parse_text(old_arch_text)
        arch_text, why = _append_spans(old_arch_text, old_arch_raw, spans, src_indent, eol, sep)
        if arch_text is None:
            print(f"error: could not append to {archive_path} ({why}). Nothing was written.", file=sys.stderr)
            return 1
        arch_text = arch_bom + arch_text
        if "\n" in old_arch_text and _file_eol(old_arch_text) != eol:
            reindent_note += ", line endings matched to the archive"
        archive_before = archive["entries"]
        located = _locate_entries(old_arch_text.splitlines(keepends=True), old_arch_raw)
        target = (located.indent if located and located.items else src_indent) or ""
        if len(target) != len(src_indent):
            reindent_note += f", re-indented from {len(src_indent)} to {len(target)} spaces"
        stated = _stated_count_keys(archive.get("meta"))
        counts = _derived_counts(archive_before + moved)
        updates = {k: counts[k] for k in stated if archive["meta"][k] != counts[k]}
        if updates:
            arch_text, why = _edit_meta_text(arch_text, updates)
            if arch_text is None:
                print(f"error: could not update {archive_path}'s meta counts ({why}). Nothing was written.", file=sys.stderr)
                return 1

    # Verify before writing: the live file lost exactly the moved entries, the archive
    # gained exactly them, in order, and no other key changed.
    kept = [e for i, e in enumerate(live_entries) if i not in set(move_idx)]
    try:
        live_after = _parse_text(new_live_text)
        arch_after = _parse_text(arch_text)
    except yaml.YAMLError:
        live_after = arch_after = None
    ok = (
        isinstance(live_after, dict)
        and isinstance(arch_after, dict)
        and (live_after.get("entries") or []) == kept
        and {k: v for k, v in live_after.items() if k != "entries"}
        == {k: v for k, v in live_raw.items() if k != "entries"}
        and (arch_after.get("entries") or []) == archive_before + moved
    )
    if not ok:
        print("error: the split did not verify (the moved text did not re-parse to the same entries). Nothing was written.", file=sys.stderr)
        return 1

    n_sup = sum(1 for e in moved if e.get("status") == "superseded")
    ids = ", ".join(str(e.get("id")) for e in moved)
    summary = (
        f"{len(moved)} entries ({n_sup} superseded, {len(moved) - n_sup} retired) move from "
        f"{live_path} to {archive_path}; {len(kept)} stay"
    )
    if args.check:
        print(f"[check] Would split: {summary}.")
        print(f"  verbatim{reindent_note}: {ids}")
        print("[check] Nothing written.")
        return 0

    _write_text_atomic(archive_path, arch_text)  # archive first: a crash in between duplicates, never loses
    _write_text_atomic(live_path, new_live_text)
    print(f"Split: {summary}.")
    print(f"  moved verbatim{reindent_note}: {ids}")
    try:
        live_after, archive_after = load_registry(live_path), load_registry(archive_path)
    except RegistryError as exc:
        print(f"error: the split was written, but a file no longer reads as a registry: {exc}",
              file=sys.stderr)
        return 1
    return _print_invariant(live_path, live_after, archive_path, archive_after)


# ─────────────────────────────────────────────────────────────────────────────
# compare
# ─────────────────────────────────────────────────────────────────────────────

# Words too common in agent-correction triggers to signal a shared lesson.
_COMPARE_STOPWORDS = frozenset(
    """
    about after also another before being between both cannot could does doing done each
    either every from have into itself just like made make makes more most much must only
    other over same session sessions should since some such than that their them then there
    these they this those through under until upon very were what when where whether which
    while will with within without would your
    """.split()
)


def _lesson_tokens(text: str) -> set[str]:
    text = (text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"[_\-]", " ", text)
    return {t for t in text.split() if len(t) >= 4 and t not in _COMPARE_STOPWORDS}


def cmd_compare(args: argparse.Namespace) -> int:
    if not 0 < args.min_overlap <= 1:
        print("error: --min-overlap must be above 0 and at most 1.", file=sys.stderr)
        return 2
    if args.top < 1:
        print("error: --top must be 1 or more.", file=sys.stderr)
        return 2
    path_a, path_b = Path(args.a), Path(args.b)
    reg_a = _load_or_report(path_a)
    reg_b = _load_or_report(path_b)
    if reg_a is None or reg_b is None:
        return 1

    def pick(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [e for e in entries if args.status is None or e.get("status") == args.status]

    a_entries, b_entries = pick(reg_a["entries"]), pick(reg_b["entries"])
    same_file = path_a.resolve() == path_b.resolve()
    b_tokens = [_lesson_tokens(str(e.get("trigger_context") or "")) for e in b_entries]

    pairs = []
    for i, x in enumerate(a_entries):
        tx = _lesson_tokens(str(x.get("trigger_context") or ""))
        if not tx:
            continue
        for j, y in enumerate(b_entries):
            if same_file and j <= i:
                continue
            shared = tx & b_tokens[j]
            if not shared:
                continue
            score = len(shared) / len(tx | b_tokens[j])
            if score >= args.min_overlap:
                pairs.append((score, x, y, shared))
    pairs.sort(key=lambda p: -p[0])

    a_ids = {str(e.get("id")) for e in a_entries}
    same_ids = [] if same_file else sorted(a_ids & {str(e.get("id")) for e in b_entries})

    print(f"Comparing {path_a} ({len(a_entries)} entries) with {path_b} ({len(b_entries)} entries) by trigger overlap.")
    print(f"\nLikely duplicate lessons (trigger overlap >= {args.min_overlap:.2f}): {len(pairs)}")
    for score, x, y, shared in pairs[: args.top]:
        print(f"  {score:.2f}  [{x.get('id')}] ({fmt(x.get('status'))})  ~  [{y.get('id')}] ({fmt(y.get('status'))})")
        print(f"        shared: {', '.join(sorted(shared)[:8])}")
    if len(pairs) > args.top:
        print(f"  ...and {len(pairs) - args.top} more (raise --top to see them).")
    print(f"\nSame id in both registries: {len(same_ids)}")
    for entry_id in same_ids:
        print(f"  [{entry_id}]")
    print("\nReport only: which lesson to keep, merge, or retire stays a human call.")
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
    default_registry = "corrections-registry.yaml"

    p_search = sub.add_parser("search", help="Find entries matching a keyword or phrase.")
    p_search.add_argument("query", help="Keyword or phrase to search for.")
    p_search.add_argument("--registry", default=default_registry, help="Path to the registry YAML file.")
    p_search.add_argument("--top-k", type=int, default=5, help="Maximum results to return (default: 5).")
    p_search.add_argument("--status", default=None, choices=VALID_STATUSES, help="Only search entries with this status.")
    p_search.set_defaults(func=cmd_search)

    p_add = sub.add_parser("add", help="Create a new entry (auto-write for factual, approval-gated for behavioral).")
    p_add.add_argument("--registry", default=default_registry, help="Path to the registry YAML file.")
    p_add.add_argument("--id", help="Unique kebab-case identifier for the new entry.")
    p_add.add_argument("--category", help="Category label (free text, e.g. voice, architectural, constraint).")
    p_add.add_argument("--severity", choices=VALID_SEVERITIES, help="factual (auto-write) or behavioral (approval-gated).")
    p_add.add_argument("--trigger-context", help="When this correction is likely to matter.")
    p_add.add_argument("--correction", help="The rule to follow, as a positive instruction.")
    p_add.add_argument("--approve", action="store_true", help="Confirm a behavioral entry non-interactively.")
    p_add.add_argument("--dry-run", action="store_true", help="Show what would be added without writing the file.")
    p_add.set_defaults(func=cmd_add)

    p_audit = sub.add_parser("audit", help="Validate the registry's schema and report stale / promotion-candidate entries.")
    p_audit.add_argument("--registry", default=default_registry, help="Path to the registry YAML file.")
    p_audit.add_argument("--as-of", default=None, help="Reference date (YYYY-MM-DD) for staleness. Defaults to today.")
    p_audit.add_argument("--stale-days", type=int, default=30, help="Days without a fire before an entry is 'stale' (default: 30).")
    p_audit.add_argument("--promotion-threshold", type=int, default=5, help="times_fired at or above which an entry is a promotion candidate (default: 5).")
    p_audit.add_argument("--recount", action="store_true", help="Derive the meta counts from the entries; exit 1 if a stated count drifted.")
    p_audit.add_argument("--write", action="store_true", help="With --recount: fix drifted meta counts in place (only those lines change).")
    p_audit.add_argument("--archive", default=None, help="With --recount: also count this archive file's entries (for whole-registry meta counts).")
    p_audit.add_argument("--live-only", action="store_true", help="With --recount: the meta counts describe this file alone, even though an archive sits next to it.")
    p_audit.add_argument("--rules-root", default=".", help="Directory that `assumes_rule` paths resolve against (default: the current directory).")
    p_audit.set_defaults(func=cmd_audit)

    p_inject = sub.add_parser("inject", help="Emit a session-preamble context block of active entries.")
    p_inject.add_argument("--registry", default=default_registry, help="Path to the registry YAML file.")
    p_inject.add_argument("--format", choices=list(_INJECT_HEADERS), default="generic", help="Output format (default: generic).")
    p_inject.add_argument("--status", default="active", choices=VALID_STATUSES, help="Only inject entries with this status (default: active).")
    p_inject.add_argument("--category", default=None, help="Only inject entries in this category.")
    p_inject.add_argument("--max", type=int, default=None, help="Inject at most N entries: behavioral first, then most-fired, then most recent.")
    p_inject.set_defaults(func=cmd_inject)

    p_fire = sub.add_parser("fire", help="Record one recurrence of an entry: times_fired +1 and last_fired stamped, on the entry itself.")
    p_fire.add_argument("id", help="The id of the entry that fired.")
    p_fire.add_argument("--registry", default=default_registry, help="Path to the registry YAML file.")
    p_fire.add_argument("--date", default=None, help="Date of the fire (YYYY-MM-DD). Defaults to today.")
    p_fire.add_argument("--promotion-threshold", type=int, default=5, help="Report when the entry reaches this many fires (default: 5).")
    p_fire.add_argument("--dry-run", action="store_true", help="Show the line change without writing the file.")
    p_fire.set_defaults(func=cmd_fire)

    p_retire = sub.add_parser("retire", help="Close an entry's validity window: retired, or superseded with --superseded-by. Never deletes.")
    p_retire.add_argument("id", help="The id of the entry to retire.")
    p_retire.add_argument("--reason", required=True, help="Why the entry stops applying (recorded as retired_reason).")
    p_retire.add_argument("--superseded-by", default=None, help="Where the rule that replaces it lives; sets status superseded instead of retired.")
    p_retire.add_argument("--registry", default=default_registry, help="Path to the registry YAML file.")
    p_retire.add_argument("--date", default=None, help="End of the validity window (YYYY-MM-DD). Defaults to today.")
    p_retire.add_argument("--approve", action="store_true", help="Confirm retiring a behavioral entry non-interactively.")
    p_retire.add_argument("--dry-run", action="store_true", help="Show the line changes without writing the file.")
    p_retire.set_defaults(func=cmd_retire)

    p_migrate = sub.add_parser("migrate-key", help="Rename a registry's top-level list key to `entries:`; only that line changes.")
    p_migrate.add_argument("--from", dest="from_key", required=True, help="The top-level key the entries sit under now (e.g. corrections).")
    p_migrate.add_argument("--registry", default=default_registry, help="Path to the registry YAML file.")
    p_migrate.add_argument("--dry-run", action="store_true", help="Show the line change without writing the file.")
    p_migrate.set_defaults(func=cmd_migrate_key)

    p_split = sub.add_parser("split", help="Move retired and superseded entries into an archive file, verbatim.")
    p_split.add_argument("--registry", default=default_registry, help="Path to the live registry YAML file.")
    p_split.add_argument("--archive", default=None, help="Path to the archive file, created if missing (default: <registry stem>-archive<suffix> beside the registry).")
    # Comment lines above or after a moved entry stay in the live file; move them by hand if they belong to it.
    p_split.add_argument("--check", action="store_true", help="Report what would move without writing either file.")
    p_split.add_argument("--verify", action="store_true", help="Only check the live/archive invariant; write nothing.")
    p_split.set_defaults(func=cmd_split)

    p_compare = sub.add_parser("compare", help="Report likely duplicate lessons across two registries by trigger overlap (report only).")
    p_compare.add_argument("a", help="Path to the first registry.")
    p_compare.add_argument("b", help="Path to the second registry.")
    p_compare.add_argument("--min-overlap", type=float, default=0.3, help="Minimum trigger-token overlap, 0 to 1, to report a pair (default: 0.3).")
    p_compare.add_argument("--top", type=int, default=20, help="Show at most this many pairs (default: 20).")
    p_compare.add_argument("--status", default=None, choices=VALID_STATUSES, help="Only compare entries with this status.")
    p_compare.set_defaults(func=cmd_compare)

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
