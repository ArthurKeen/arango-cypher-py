"""The static types the semantic analysis pass tracks.

A query shows the type of some values outright — a literal, a node or
relationship a pattern binds, a path, a list literal's elements — and the
semantic pass (``semantic_check``) uses those types the way Neo4j's semantic
analysis does: to reject a variable reused as another kind of value, ``NOT``
on a number, a property read off an integer.

Everything the query does not show (a property value, a parameter, most
function results) is :data:`ANY`, and :data:`ANY` never fails a check. The
model is deliberately one-sided: it may miss an invalid query, it must never
reject a valid one.
"""

from __future__ import annotations

from dataclasses import dataclass

NODE = "node"
REL = "relationship"
PATH = "path"
INT = "integer"
FLOAT = "float"
STRING = "string"
BOOL = "boolean"
NULL = "null"
LIST = "list"
MAP = "map"
ANY = "any"

#: Kinds that are a number.
NUMERIC = frozenset({INT, FLOAT})
#: Graph element kinds.
ENTITIES = frozenset({NODE, REL, PATH})
#: Every kind the query can show for certain; ANY and NULL are not "known".
KNOWN = frozenset({NODE, REL, PATH, INT, FLOAT, STRING, BOOL, LIST, MAP})


@dataclass(frozen=True)
class SType:
    """A static type: a kind, and for a list what the query shows of its elements.

    ``elem`` is the element type when every element has it (``ANY`` when they
    differ or are not shown); ``items`` is the per-position element types of a
    list literal, kept so that ``[n, 1][1]`` is known to be an integer.
    """

    kind: str
    elem: SType | None = None
    items: tuple[SType, ...] | None = None

    @property
    def known(self) -> bool:
        return self.kind in KNOWN

    def is_(self, *kinds: str) -> bool:
        return self.kind in kinds

    def describe(self) -> str:
        """The type as Neo4j's error messages spell it (``List<Integer>``)."""
        if self.kind == LIST:
            inner = self.elem.describe() if self.elem is not None and self.elem.kind != ANY else "Any"
            return f"List<{inner}>"
        return _NAMES.get(self.kind, "Any")


_NAMES = {
    NODE: "Node",
    REL: "Relationship",
    PATH: "Path",
    INT: "Integer",
    FLOAT: "Float",
    STRING: "String",
    BOOL: "Boolean",
    NULL: "Null",
    MAP: "Map",
}

T_ANY = SType(ANY)
T_NULL = SType(NULL)
T_BOOL = SType(BOOL)
T_INT = SType(INT)
T_FLOAT = SType(FLOAT)
T_STRING = SType(STRING)
T_MAP = SType(MAP)
T_NODE = SType(NODE)
T_REL = SType(REL)
T_PATH = SType(PATH)
T_LIST_ANY = SType(LIST, elem=T_ANY)


def list_of(elem: SType) -> SType:
    return SType(LIST, elem=elem)


def list_literal(items: list[SType]) -> SType:
    """The type of a list literal with elements of the given types."""
    return SType(LIST, elem=common_type(items), items=tuple(items))


def common_type(types: list[SType]) -> SType:
    """The one type all of *types* share (ignoring nulls), else :data:`T_ANY`.

    Integers and floats do not unify: a list of both is not shown to be
    either, and nothing here needs "number".
    """
    non_null = [t for t in types if t.kind != NULL]
    if not non_null:
        return T_NULL if types else T_ANY
    first = non_null[0]
    if all(t.kind == first.kind for t in non_null):
        if first.kind != LIST:
            return SType(first.kind)
        elems = [t.elem or T_ANY for t in non_null]
        return list_of(common_type(elems))
    return T_ANY


def element_type(t: SType) -> SType:
    """The type of one element of *t* when *t* is iterated (UNWIND, ``x IN``)."""
    if t.kind == LIST:
        return t.elem or T_ANY
    if t.kind == NULL:
        return T_NULL
    return T_ANY
