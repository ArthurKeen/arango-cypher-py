"""SET and REMOVE as one write per variable.

AQL refuses to touch a collection again after modifying it in the same query
(ERR 1579), so a statement's SET and REMOVE items are gathered per variable, in
the order the query gives them, and each variable gets a single write:

- property changes, ``+=`` maps and removals become one ``UPDATE``, with
  ``OPTIONS {keepNull: false}`` when something is removed (``UPDATE`` merges, and
  a ``null`` under that option is what deletes an attribute);
- ``SET n = {…}`` becomes one ``REPLACE`` of the document the later items
  produce, keeping the fields the mapping relies on (``_from``/``_to`` and any
  type field), which a plain replacement would drop;
- after ``CREATE`` the changes are folded into the inserted document instead.

Property keys arrive as AQL text: a SET key as the Cypher token (``x``, or a
backtick-quoted name, safe after the name guard), a REMOVE key as an AQL string
(``"x"``). ``name`` is the decoded property name, so a later item on the same
property replaces an earlier one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .literals import _aql_string_literal

OpKind = Literal["set", "merge", "replace", "remove"]

DROP_NULLS = "OPTIONS {keepNull: false}"


@dataclass(frozen=True)
class PropertyOp:
    kind: OpKind
    name: str = ""  # decoded property name (set / remove)
    key: str = ""  # AQL object key (set) or AQL string (remove)
    value: str = ""  # compiled AQL expression (set / merge / replace)


@dataclass(frozen=True)
class StoredWrite:
    """One write of a stored document: ``<operation> <var> WITH <document> IN …``."""

    operation: Literal["UPDATE", "REPLACE"]
    document: str
    options: str = ""


@dataclass
class PropertyWrites:
    """The SET and REMOVE items of one statement, per variable, in query order."""

    ops: dict[str, list[PropertyOp]] = field(default_factory=dict)

    def add(self, var: str, op: PropertyOp) -> None:
        self.ops.setdefault(var, []).append(op)

    def __bool__(self) -> bool:
        return bool(self.ops)

    def stored_write(self, var: str, keep_fields: list[str]) -> StoredWrite:
        """The single write that applies *var*'s items to its stored document.

        *keep_fields* are AQL strings naming attributes a replacement must keep.
        """
        ops = self.ops[var]
        last_replace = max((i for i, op in enumerate(ops) if op.kind == "replace"), default=None)
        if last_replace is not None:
            doc = _apply(ops[last_replace].value, ops[last_replace + 1 :])
            if keep_fields:
                doc = f"MERGE({doc}, KEEP({var}, {', '.join(keep_fields)}))"
            return StoredWrite("REPLACE", doc)

        removes = any(op.kind == "remove" for op in ops)
        segments = _segments(ops)
        if any(op.kind == "merge" for op in ops):
            document = f"MERGE({var}, {', '.join(segments)})"
        elif len(segments) == 1:
            document = segments[0]
        else:
            document = f"MERGE({', '.join(segments)})"
        return StoredWrite("UPDATE", document, DROP_NULLS if removes else "")

    def folded_into(self, var: str, document: str, server_fields: list[str]) -> str:
        """*document* (an INSERT body) with *var*'s items applied before insertion.

        *server_fields* are ``key: value`` entries the insert sets for the
        mapping (type field, ``_from``/``_to``); a replacement keeps them.
        """
        ops = self.ops.get(var)
        if not ops:
            return document
        last_replace = max((i for i, op in enumerate(ops) if op.kind == "replace"), default=None)
        if last_replace is None:
            return _apply(document, ops, reads=var)
        base = _apply(document, ops[: last_replace + 1], reads=var)
        if server_fields:
            base = f"MERGE({base}, {{{', '.join(server_fields)}}})"
        return _apply(base, ops[last_replace + 1 :], reads=var)


def _set_literal(ops: list[PropertyOp], *, nulls_for_removes: bool) -> str:
    """One object literal for a run of set (and remove) items; later wins."""
    entries: dict[str, str] = {}
    for op in ops:
        if op.kind == "set":
            entries.pop(op.name, None)
            entries[op.name] = f"{op.key}: {op.value}"
        elif op.kind == "remove" and nulls_for_removes:
            entries.pop(op.name, None)
            entries[op.name] = f"{op.key}: null"
    return "{" + ", ".join(entries.values()) + "}"


def _segments(ops: list[PropertyOp]) -> list[str]:
    """UPDATE patches: runs of set/remove items as literals, ``+=`` maps as given."""
    out: list[str] = []
    run: list[PropertyOp] = []
    for op in ops:
        if op.kind == "merge":
            if run:
                out.append(_set_literal(run, nulls_for_removes=True))
                run = []
            out.append(op.value)
        else:
            run.append(op)
    if run:
        out.append(_set_literal(run, nulls_for_removes=True))
    return out


def _apply(document: str, ops: list[PropertyOp], *, reads: str | None = None) -> str:
    """*document* with *ops* applied as an expression: MERGE for set and ``+=``,
    UNSET for remove, the value itself for a replacement. Used where nothing
    merges for us (REPLACE, INSERT).

    With *reads*, an item whose value reads that variable sees the document as
    the items before it left it: the variable is not bound yet when a CREATE is
    folded, so its document so far is passed in as ``_d<n>``.
    """
    out = document
    run: list[PropertyOp] = []
    depth = 0

    def flush() -> None:
        nonlocal out, run
        if run:
            out = f"MERGE({out}, {_set_literal(run, nulls_for_removes=False)})"
            run = []

    for op in ops:
        if reads and op.value and _reads_variable(op.value, reads):
            flush()
            alias = f"_d{depth}"
            depth += 1
            value = _rename_variable(op.value, reads, alias)
            if op.kind == "set":
                result = f"MERGE({alias}, {{{op.key}: {value}}})"
            elif op.kind == "merge":
                result = f"MERGE({alias}, {value})"
            else:
                result = value
            out = f"FIRST(FOR {alias} IN [{out}] RETURN {result})"
        elif op.kind == "set":
            run.append(op)
        elif op.kind == "merge":
            flush()
            out = f"MERGE({out}, {op.value})"
        elif op.kind == "replace":
            run = []
            out = op.value
        elif op.kind == "remove":
            flush()
            out = f"UNSET({out}, {op.key})"
    flush()
    return out


_QUOTED = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`")


def _variable_pattern(var: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w.@]){re.escape(var)}(?!\w)")


def _reads_variable(expression: str, var: str) -> bool:
    """Whether *expression* refers to the AQL variable *var* (outside quotes)."""
    return _variable_pattern(var).search(_QUOTED.sub("''", expression)) is not None


def _rename_variable(expression: str, var: str, to: str) -> str:
    """*expression* with references to *var* (outside quotes) renamed to *to*."""
    pattern = _variable_pattern(var)
    pieces: list[str] = []
    last = 0
    for quoted in _QUOTED.finditer(expression):
        pieces.append(pattern.sub(to, expression[last : quoted.start()]))
        pieces.append(quoted.group(0))
        last = quoted.end()
    pieces.append(pattern.sub(to, expression[last:]))
    return "".join(pieces)


def keep_fields_for(collection: str, physical_mapping: dict[str, Any]) -> list[str]:
    """Attributes a replacement in *collection* must keep, as AQL strings.

    ``_from``/``_to`` when the mapping stores relationships there (an edge
    without them is rejected, ERR 1233), and the type field of every
    label-style entity or typed relationship stored there: dropping it would
    silently remove the document's label.
    """
    fields: list[str] = []
    for spec in (physical_mapping.get("relationships") or {}).values():
        edge = spec.get("edgeCollectionName") or spec.get("collectionName")
        if edge == collection:
            fields += ["_from", "_to"]
            if spec.get("style") == "GENERIC_WITH_TYPE":
                fields.append(spec.get("typeField") or "type")
    for spec in (physical_mapping.get("entities") or {}).values():
        if spec.get("collectionName") == collection and spec.get("style") == "LABEL":
            fields.append(spec.get("typeField") or "type")
    return [_aql_string_literal(f) for f in dict.fromkeys(fields)]
