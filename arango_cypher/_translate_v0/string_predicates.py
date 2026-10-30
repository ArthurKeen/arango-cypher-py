"""``STARTS WITH`` / ``ENDS WITH`` / ``CONTAINS`` / ``=~`` with Cypher's semantics.

Cypher's string predicates are null unless both operands are strings
(``1 CONTAINS 1`` is null); AQL's string functions convert their operands
(``CONTAINS(1, 1)`` is true). Every substring contains the empty string
(``'abc' CONTAINS ''`` is true); AQL's ``CONTAINS(x, '')`` is false. And the
``ENDS WITH`` test is an equality, which ``NOT`` must negate as a whole
(AQL's ``NOT RIGHT(x, 3) == 'def'`` negates the ``RIGHT``).

:func:`compile_string_predicate` builds the test with an ``IS_STRING`` guard
on each operand the query does not show to be a string — the ``!= null``
guard it replaces, so a filter on an indexed attribute costs the same.
"""

from __future__ import annotations

import re

#: An AQL string literal (the translator copies Cypher's quotes).
_STRING_LITERAL = re.compile(r"""^(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")$""", re.DOTALL)


def is_string_literal(aql: str) -> bool:
    return bool(_STRING_LITERAL.match(aql.strip()))


def _is_empty_literal(aql: str) -> bool:
    return aql.strip() in ("''", '""')


def string_test(kind: str, base: str, rhs: str) -> str:
    """The bare AQL test for *kind* (``starts``/``ends``/``contains``/``regex``),
    valid when both operands are strings."""
    if kind == "starts":
        return f"STARTS_WITH({base}, {rhs})"
    if kind == "ends":
        return f"(RIGHT({base}, LENGTH({rhs})) == {rhs})"
    if kind == "contains":
        if _is_empty_literal(rhs):
            return "true"
        if is_string_literal(rhs):
            return f"CONTAINS({base}, {rhs})"
        return f"(LENGTH({rhs}) == 0 OR CONTAINS({base}, {rhs}))"
    if kind == "regex":
        return f"REGEX_TEST({base}, {rhs})"
    raise ValueError(f"unknown string predicate {kind!r}")


def compile_string_predicate(
    kind: str, base: str, rhs: str, *, base_is_string: bool, rhs_is_string: bool, mode: str | None
) -> str:
    """The predicate in *mode*: ``None`` (a value: true/false/null), ``"true"``
    (holds) or ``"false"`` (fails, under NOT). *base_is_string* /
    *rhs_is_string* say the query shows that operand to be a string."""
    test = string_test(kind, base, rhs)
    guards = [f"IS_STRING({op})" for op, known in ((base, base_is_string), (rhs, rhs_is_string)) if not known]
    if mode is None:
        return f"(({' AND '.join(guards)}) ? {test} : null)" if guards else f"({test})"
    body = test if mode == "true" else f"NOT {test}" if test.startswith("(") else f"NOT ({test})"
    return "(" + " AND ".join([*guards, body]) + ")"
