"""The error the semantic analysis pass raises for a query that is not valid Cypher.

Every other translator refusal is about *this translator*: a feature outside
its subset (``UNSUPPORTED``), a gap still to close (``NOT_IMPLEMENTED``). A
:class:`CypherSemanticError` is about *the query*: Neo4j rejects it too, so no
other engine — and no LLM fallback — would run it. Callers tell the two apart
by ``code``:

- ``CYPHER_SEMANTIC_ERROR`` — what Neo4j reports as a ``SyntaxError`` from its
  compile-time semantic analysis (a variable bound twice, a variable used as two
  kinds of value, a literal of the wrong type, an integer literal out of range).
- ``CYPHER_TYPE_ERROR`` — what Neo4j reports as a ``TypeError`` or
  ``ArgumentError`` while it runs the query, raised here because the query
  shows the offending value before it runs (a literal, or a list literal's
  element).

``neo4j_error`` carries the openCypher error detail (``VariableAlreadyBound``,
``InvalidArgumentType``, ...) and the message starts with it, so a caller that
shows only the message still shows which rule the query broke.
"""

from __future__ import annotations

from arango_query_core import CoreError

SEMANTIC_ERROR_CODE = "CYPHER_SEMANTIC_ERROR"
TYPE_ERROR_CODE = "CYPHER_TYPE_ERROR"


class CypherSemanticError(CoreError):
    """A query Neo4j rejects as invalid, found by static analysis of the query."""

    def __init__(self, neo4j_error: str, message: str, *, code: str = SEMANTIC_ERROR_CODE):
        if code not in (SEMANTIC_ERROR_CODE, TYPE_ERROR_CODE):
            raise ValueError(f"CypherSemanticError code must be a semantic or type error code, got {code!r}")
        super().__init__(f"{neo4j_error}: {message}", code=code)
        self.neo4j_error = neo4j_error


def semantic_error(neo4j_error: str, message: str) -> CypherSemanticError:
    """A compile-time ``SyntaxError`` (Neo4j's semantic analysis)."""
    return CypherSemanticError(neo4j_error, message, code=SEMANTIC_ERROR_CODE)


def type_error(neo4j_error: str, message: str) -> CypherSemanticError:
    """A ``TypeError``/``ArgumentError`` the query shows before it runs."""
    return CypherSemanticError(neo4j_error, message, code=TYPE_ERROR_CODE)
