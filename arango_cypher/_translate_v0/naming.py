"""Name normalisation and fresh-name helpers for translator codegen."""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import CoreError


def _pick_fresh_var(name: str, *, forbidden_vars: set[str]) -> str:
    if name not in forbidden_vars:
        forbidden_vars.add(name)
        return name
    i = 1
    while f"{name}_{i}" in forbidden_vars:
        i += 1
    out = f"{name}_{i}"
    forbidden_vars.add(out)
    return out


_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_FOR_DECL = re.compile(rf"\bFOR\s+({_IDENT})(?:\s*,\s*({_IDENT}))?(?:\s*,\s*({_IDENT}))?\s+IN\b")
_LET_DECL = re.compile(rf"\bLET\s+({_IDENT})\s*=(?!=)")
_COLLECT_CLAUSE = re.compile(r"\bCOLLECT\b(.*)")
_ASSIGN = re.compile(rf"(?:^|,)\s*({_IDENT})\s*=(?!=)")
_INTO_DECL = re.compile(rf"\bINTO\s+({_IDENT})")
_AGGREGATE_CLAUSE = re.compile(r"\bAGGREGATE\b(.*)")


def _declared_aql_vars(lines: list[str]) -> set[str]:
    """Every AQL variable the emitted *lines* declare (FOR, LET, COLLECT,
    AGGREGATE, INTO).

    For callers with no scope set of their own that are about to declare a
    variable — reusing a declared name is ERR 1511 ("assigned multiple
    times"). Deliberately over-inclusive: a name declared inside a subquery
    is counted too, which costs at most an unneeded rename, never a collision.
    """
    declared: set[str] = set()
    for line in lines:
        for match in _FOR_DECL.finditer(line):
            declared.update(g for g in match.groups() if g)
        declared.update(_LET_DECL.findall(line))
        declared.update(_INTO_DECL.findall(line))
        for clause_re in (_COLLECT_CLAUSE, _AGGREGATE_CLAUSE):
            clause = clause_re.search(line)
            if clause:
                head = re.split(r"\b(?:INTO|AGGREGATE|WITH COUNT|OPTIONS|KEEP)\b", clause.group(1))[0]
                declared.update(_ASSIGN.findall(head))
    return declared


def _pick_bind_key(base: str, bind_vars: dict[str, Any]) -> str:
    if base not in bind_vars:
        return base
    i = 2
    while f"{base}{i}" in bind_vars:
        i += 1
    return f"{base}{i}"


def _aql_collection_ref(bind_key: str) -> str:
    if not bind_key.startswith("@"):
        raise CoreError("Collection bind key must start with '@'", code="INTERNAL_ERROR")
    return f"@@{bind_key[1:]}"


def _strip_label_backticks(name: str) -> str:
    """Strip a single pair of enclosing backticks from an escaped label."""
    if len(name) >= 2 and name.startswith("`") and name.endswith("`"):
        return name[1:-1]
    return name


def _rewrite_vars(text: str, var_env: dict[str, str]) -> str:
    """Best-effort variable rewrite for post-WITH scopes."""
    if not text or not var_env:
        return text
    out = text
    for k in sorted(var_env.keys(), key=len, reverse=True):
        v = var_env[k]
        if k == v:
            continue
        out = re.sub(rf"\b{re.escape(k)}\b", v, out)
    return out
