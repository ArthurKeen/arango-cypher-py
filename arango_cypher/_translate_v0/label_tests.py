"""Label tests for nodes of a type-discriminated (``LABEL``) collection.

A ``LABEL`` entity mapping stores a node's label in ``typeField``. A node can
have several labels; a mapping that can store them names a ``labelsField``
as well, an array listing every label of a node that has more than one::

    {"style": "LABEL", "collectionName": "vertices",
     "typeField": "type", "typeValue": "A", "labelsField": "labels"}

``CREATE (:A:B)`` then stores ``{type: "A", labels: ["A", "B"]}`` and the
node has label ``B`` although its type field says ``A``, so a label test
reads both: ``(n[@typeField] == @typeValue OR @typeValue IN n[@labelsField])``.
Without ``labelsField`` the test is the type field alone, as before.
"""

from __future__ import annotations

from typing import Any

from .literals import _aql_string_literal
from .naming import _pick_bind_key


def labels_field(emap: dict[str, Any] | None) -> str | None:
    """The labels array field of a ``LABEL`` entity mapping, if it has one."""
    if not emap or emap.get("style") != "LABEL":
        return None
    lf = emap.get("labelsField")
    return lf if isinstance(lf, str) and lf else None


def bind_value(base: str, value: Any, bind_vars: dict[str, Any]) -> str:
    """A bind key holding *value*: an existing ``base…`` key with that value, or a new one."""
    for key, existing in bind_vars.items():
        if existing == value and (key == base or (key.startswith(base) and key[len(base) :].isdigit())):
            return key
    key = _pick_bind_key(base, bind_vars)
    bind_vars[key] = value
    return key


def shared_label_fields(physical_mapping: dict[str, Any] | None) -> tuple[str, str] | None:
    """``(typeField, labelsField)`` when every ``LABEL`` entity that names a
    labels field shares both fields, else ``None``."""
    specs = [
        e
        for e in ((physical_mapping or {}).get("entities") or {}).values()
        if isinstance(e, dict) and labels_field(e) is not None
    ]
    pairs = {(e.get("typeField") or "type", e["labelsField"]) for e in specs}
    return next(iter(pairs)) if len(pairs) == 1 else None


def label_write(var: str, labels: list[str], *, add: bool, fields: tuple[str, str]) -> str:
    """An UPDATE patch for ``SET var:L…`` (*add*) or ``REMOVE var:L…``.

    The labels array becomes the node's labels plus (or minus) *labels*, in
    order; the type field keeps its label unless that is removed, when it
    takes the first label left, or null — which the UPDATE drops
    (``keepNull: false``) — when none is.
    """
    tf, lf = (f"{var}[{_lit(f)}]" for f in fields)
    current = f"(IS_ARRAY({lf}) ? {lf} : {tf} != null ? [{tf}] : [])"
    names = "[" + ", ".join(_lit(lab) for lab in labels) + "]"
    if add:
        new_labels = f"UNION_DISTINCT({current}, {names})"
        new_type = f"({tf} != null ? {tf} : {_lit(labels[0])})"
    else:
        new_labels = f"{current}[* FILTER CURRENT NOT IN {names}]"
        new_type = f"({tf} IN {names} ? FIRST({new_labels}) : {tf})"
    return f"{{{_lit(fields[1])}: {new_labels}, {_lit(fields[0])}: {new_type}}}"


def _lit(name: str) -> str:
    return _aql_string_literal(name)


def label_test(ref: str, tf_key: str, tv_key: str, emap: dict[str, Any], bind_vars: dict[str, Any]) -> str:
    """``ref[@tf_key] == @tv_key``, also accepting the label in the labels array (module docstring)."""
    base = f"{ref}[@{tf_key}] == @{tv_key}"
    lf = labels_field(emap)
    if lf is None:
        return base
    lf_key = bind_value("labelsField", lf, bind_vars)
    return f"({base} OR @{tv_key} IN {ref}[@{lf_key}])"
