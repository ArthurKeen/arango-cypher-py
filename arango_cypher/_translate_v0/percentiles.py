"""``percentileDisc()`` / ``percentileCont()``: aggregates AQL has no COLLECT form of.

AQL's ``COLLECT … AGGREGATE`` has no percentile, and its ``PERCENTILE()``
function rejects 0 and ranks differently from Neo4j. So a percentile is a
``collect()`` of its argument (``_parse_collect`` in ``core``) finished after
grouping by :func:`percentile_value`, which computes Neo4j's definition over
the sorted non-null values:

- ``percentileDisc(x, p)``: the value at index ``floor(p·n)``, one less when
  ``p·n`` is a whole number other than 0; the last value for ``p = 1``.
- ``percentileCont(x, p)``: the linear interpolation at ``p·(n−1)``.

``p`` outside ``[0, 1]`` is Neo4j's ``NumberOutOfRange``, raised for each group
when it is computed (``FAIL``), as Neo4j raises it at run time; no values, or
a null ``p``, gives null.
"""

from __future__ import annotations

import re
from typing import Any

_PERCENTILE_CALL = re.compile(r"^(percentileDisc|percentileCont)\s*\(", re.IGNORECASE)

_OUT_OF_RANGE = '"NumberOutOfRange: a percentile must be a number between 0.0 and 1.0"'


def parse_percentile(text: str) -> dict[str, Any] | None:
    """``{"inner", "distinct", "slice", "percentile": (kind, p_text)}`` when *text*
    is exactly one percentile call, else None (the shape of ``_parse_collect``)."""
    t = text.strip()
    m = _PERCENTILE_CALL.match(t)
    if m is None or not t.endswith(")"):
        return None
    args = _split_arguments(t[m.end() : -1])
    if args is None or len(args) != 2:
        return None
    kind = "disc" if m.group(1).lower() == "percentiledisc" else "cont"
    return {"inner": args[0], "distinct": False, "slice": None, "percentile": (kind, args[1])}


def _split_arguments(text: str) -> list[str] | None:
    """Split a call's argument text at top-level commas; None if unbalanced.

    Brackets and quoted strings are skipped, so ``f(a, [1, 2])`` is two
    arguments and ``(a) + (b)`` closing early is caught.
    """
    args: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    i = 0
    while i < len(text):
        c = text[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                return None
        elif c == "," and depth == 0:
            args.append(text[start:i].strip())
            start = i + 1
        i += 1
    if depth != 0 or quote is not None:
        return None
    args.append(text[start:].strip())
    return args


def percentile_value(values: str, kind: str, p: str) -> str:
    """AQL for the percentile *kind* (``disc``/``cont``) at *p* of the list *values*."""
    if kind == "disc":
        at = (
            "(_pc == 1 OR _pp == 1) ? _ps[_pc - 1]"
            " : _ps[(_pf != FLOOR(_pf) OR _pf == 0) ? FLOOR(_pf) : FLOOR(_pf) - 1]"
        )
        index = "LET _pf = _pp * _pc"
    else:
        at = (
            "(_pc == 1 OR _pp == 1 OR FLOOR(_pf) == CEIL(_pf)) ? _ps[FLOOR(_pf)]"
            " : _ps[FLOOR(_pf)] * (CEIL(_pf) - _pf) + _ps[CEIL(_pf)] * (_pf - FLOOR(_pf))"
        )
        index = "LET _pf = _pp * (_pc - 1)"
    return (
        f"FIRST(LET _pp = {p} LET _ps = SORTED(REMOVE_VALUE({values}, null)) LET _pc = LENGTH(_ps) "
        f"{index} RETURN _pp == null ? null : (NOT IS_NUMBER(_pp) OR _pp < 0 OR _pp > 1) ? FAIL({_OUT_OF_RANGE})"
        f" : _pc == 0 ? null : {at})"
    )
