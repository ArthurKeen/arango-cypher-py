"""Read a pattern from its bound end (``MATCH (m:Message)-[:HAS_TAG]->(tag)``
after ``tag`` is bound).

A MATCH after WITH, and an OPTIONAL MATCH, compile a pattern from its first
node. When that node is new and the last one is already bound, the pattern
scanned every ``Message`` for each ``tag`` and kept those whose traversal
reached it: right rows, but the work of a cross product (LDBC BI2 ran past the
30 s limit at the smallest scale). Read backwards, with each relationship's
direction flipped, the same pattern is a traversal from the bound ``tag``.

The reversed pattern is a view over the parse tree with the accessors the
bound-pattern compiler reads; nothing is copied or re-parsed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .._antlr.CypherParser import CypherParser


class _ReversedRelationship:
    """A relationship pattern with its arrowheads swapped."""

    def __init__(self, ctx: CypherParser.OC_RelationshipPatternContext) -> None:
        self._ctx = ctx

    def oC_LeftArrowHead(self) -> Any:  # noqa: N802 — mirrors the ANTLR accessor
        return self._ctx.oC_RightArrowHead()

    def oC_RightArrowHead(self) -> Any:  # noqa: N802
        return self._ctx.oC_LeftArrowHead()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)


class _Chain:
    def __init__(self, relationship: Any, node: CypherParser.OC_NodePatternContext) -> None:
        self._relationship = relationship
        self._node = node

    def oC_RelationshipPattern(self) -> Any:  # noqa: N802
        return self._relationship

    def oC_NodePattern(self) -> CypherParser.OC_NodePatternContext:  # noqa: N802
        return self._node


class _ReversedElement:
    def __init__(self, elem: CypherParser.OC_PatternElementContext) -> None:
        chains = elem.oC_PatternElementChain() or []
        nodes = [elem.oC_NodePattern(), *(c.oC_NodePattern() for c in chains)]
        relationships = [c.oC_RelationshipPattern() for c in chains]
        self._start = nodes[-1]
        self._chains = [
            _Chain(_ReversedRelationship(relationships[i]), nodes[i])
            for i in reversed(range(len(relationships)))
        ]

    def oC_NodePattern(self) -> CypherParser.OC_NodePatternContext:  # noqa: N802
        return self._start

    def oC_PatternElementChain(self) -> list[_Chain]:  # noqa: N802
        return self._chains


def _variable(node: CypherParser.OC_NodePatternContext | None) -> str | None:
    if node is None or node.oC_Variable() is None:
        return None
    return node.oC_Variable().getText().strip()


def from_bound_end(elem: CypherParser.OC_PatternElementContext, bound: Mapping[str, str]) -> Any:
    """*elem*, or its reversal when its first node is new and its last one bound."""
    chains = elem.oC_PatternElementChain() or []
    if elem.oC_NodePattern() is None or not chains:
        return elem
    if _variable(elem.oC_NodePattern()) in bound:
        return elem
    if _variable(chains[-1].oC_NodePattern()) not in bound:
        return elem
    return _ReversedElement(elem)
