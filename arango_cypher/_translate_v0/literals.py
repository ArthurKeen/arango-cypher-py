"""Literal and type-expression helpers for the v0 translator."""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import CoreError


def _aql_string_literal(value: str) -> str:
    """Return a minimally escaped AQL double-quoted string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# One escape sequence of an openCypher string literal (the grammar admits only
# these after a backslash).
_CYPHER_ESCAPE = re.compile(r"\\(?:U([0-9A-Fa-f]{8})|([BFNRT])|(.))", re.DOTALL)


def _aql_string_from_cypher(literal: str) -> str:
    """An openCypher string literal, quotes included, as an AQL string literal.

    The two languages agree on where a string ends (both read a backslash and
    the character after it as one escape), so the text is kept and only the
    escapes AQL reads differently are rewritten: AQL takes ``\\N``, ``\\T``,
    ``\\B``, ``\\F`` and ``\\R`` as plain letters where openCypher means the
    lowercase escapes, and has no eight-digit ``\\U``, so that becomes a
    ``\\u`` surrogate pair, which AQL decodes after reading the string.
    """

    def rewrite(match: re.Match[str]) -> str:
        wide, upper, other = match.groups()
        if upper:
            return "\\" + upper.lower()
        if wide:
            code = int(wide, 16)
            if code > 0x10FFFF:
                raise CoreError(f"String escape \\U{wide} is not a Unicode code point", code="UNSUPPORTED")
            if code < 0x10000:
                return f"\\u{code:04x}"
            code -= 0x10000
            return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"
        return match.group(0)

    return _CYPHER_ESCAPE.sub(rewrite, literal)


def _compile_type_of_relationship(
    rel_type: str, rel_var: str, rel_style: str | None, bind_vars: dict[str, Any]
) -> str:
    if rel_style == "GENERIC_WITH_TYPE":
        if "relTypeField" not in bind_vars:
            raise CoreError("relTypeField missing for GENERIC_WITH_TYPE", code="INVALID_MAPPING")
        return f"{rel_var}[@relTypeField]"
    return _aql_string_literal(rel_type)
