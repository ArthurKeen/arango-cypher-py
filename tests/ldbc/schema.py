"""The LDBC SNB schema as its data generator lays it out on disk.

One source of truth for the loader (WP-31), the mapping fixtures (WP-32) and
the Neo4j oracle loader (WP-33). It describes the ``composite-projected-fk``
CSV layout that the Neo4j reference implementations consume: pipe-delimited,
quoted, gzip-compressed, **no header row**, one directory per node or edge
file. The column orders below are the header files of
``ldbc/ldbc_snb_bi`` ``neo4j/headers/`` and ``ldbc/ldbc_snb_interactive_v2_impls``
``cypher/headers/`` (identical apart from the timestamp types; see
``docs/ldbc_benchmark_plan.md`` §2.3).

Labels follow the Neo4j reference load: a comment carries ``Message`` and
``Comment``, a city ``Place`` and ``City``, a company ``Organisation`` and
``Company``. Concrete labels are the leaves; supertypes are the three parents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

#: How a column's raw CSV text becomes a document value.
#: ``datetime``/``date`` depend on the dataset's encoding (epoch-ms or ISO).
ColumnKind = Literal["id", "str", "int", "datetime", "date", "str_list", "label", "start", "end"]

SUPERTYPES: dict[str, tuple[str, ...]] = {
    "Message": ("Post", "Comment"),
    "Place": ("City", "Country", "Continent"),
    "Organisation": ("University", "Company"),
}

CONCRETE_LABELS: tuple[str, ...] = (
    "Person",
    "Forum",
    "Post",
    "Comment",
    "Tag",
    "TagClass",
    "City",
    "Country",
    "Continent",
    "University",
    "Company",
)

_SUPERTYPE_OF: dict[str, str] = {member: sup for sup, members in SUPERTYPES.items() for member in members}


@dataclass(frozen=True)
class Column:
    name: str
    kind: ColumnKind


@dataclass(frozen=True)
class NodeFile:
    """A node directory. ``id_space`` is the ``ID(<space>)`` group of the header.

    ``label`` is the concrete label of every row, or ``None`` when the rows
    carry it in a ``:LABEL`` column (Place, Organisation).
    """

    name: str
    part: Literal["static", "dynamic"]
    columns: tuple[Column, ...]
    label: str | None

    @property
    def id_space(self) -> str:
        return self.name


@dataclass(frozen=True)
class EdgeFile:
    """An edge directory. Endpoint id spaces come from ``START_ID``/``END_ID``.

    ``src_labels``/``dst_labels`` are the concrete labels an endpoint can have
    (the LDBC specification's per-file endpoint types).
    """

    name: str
    part: Literal["static", "dynamic"]
    rel_type: str
    src_space: str
    dst_space: str
    columns: tuple[Column, ...]
    src_labels: tuple[str, ...]
    dst_labels: tuple[str, ...]


def _cols(spec: str) -> tuple[Column, ...]:
    out = []
    for item in spec.split():
        name, kind = item.split(":")
        out.append(Column(name, kind))  # type: ignore[arg-type]
    return tuple(out)


_DYN_EDGE = "creationDate:datetime src:start dst:end"
_STATIC_EDGE = "src:start dst:end"

NODE_FILES: tuple[NodeFile, ...] = (
    NodeFile("Place", "static", _cols("id:id name:str url:str type:label"), None),
    NodeFile("Organisation", "static", _cols("id:id type:label name:str url:str"), None),
    NodeFile("Tag", "static", _cols("id:id name:str url:str"), "Tag"),
    NodeFile("TagClass", "static", _cols("id:id name:str url:str"), "TagClass"),
    NodeFile("Forum", "dynamic", _cols("creationDate:datetime id:id title:str"), "Forum"),
    NodeFile(
        "Person",
        "dynamic",
        _cols(
            "creationDate:datetime id:id firstName:str lastName:str gender:str birthday:date "
            "locationIP:str browserUsed:str speaks:str_list email:str_list"
        ),
        "Person",
    ),
    NodeFile(
        "Post",
        "dynamic",
        _cols(
            "creationDate:datetime id:id imageFile:str locationIP:str browserUsed:str "
            "language:str content:str length:int"
        ),
        "Post",
    ),
    NodeFile(
        "Comment",
        "dynamic",
        _cols("creationDate:datetime id:id locationIP:str browserUsed:str content:str length:int"),
        "Comment",
    ),
)


def _edge(name: str, part: Literal["static", "dynamic"], extra: str = "") -> EdgeFile:
    """``Comment_hasCreator_Person`` → HAS_CREATOR from Comment to Person.

    Place and Organisation endpoints are id spaces, not labels: the concrete
    label of such an endpoint is looked up per row (a ``Person_isLocatedIn_City``
    row names a Place id).
    """
    src, verb, dst = name.split("_")
    rel_type = re.sub(r"(?<!^)(?=[A-Z])", "_", verb).upper()
    spec = (_DYN_EDGE if part == "dynamic" else _STATIC_EDGE) + (f" {extra}" if extra else "")
    src_labels, dst_labels = _ENDPOINT_LABELS.get(name, ((src,), (dst,)))
    return EdgeFile(
        name,
        part,
        rel_type,
        _ID_SPACE.get(src, src),
        _ID_SPACE.get(dst, dst),
        _cols(spec),
        src_labels,
        dst_labels,
    )


#: File-name endpoint → the ``ID(<space>)`` it references in the headers.
_ID_SPACE = {"City": "Place", "Country": "Place", "University": "Organisation", "Company": "Organisation"}

#: The two files whose names use a supertype: their concrete endpoints per the
#: LDBC SNB specification (a university is in a city, a company in a country;
#: a city is part of a country, a country part of a continent).
_ENDPOINT_LABELS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Organisation_isLocatedIn_Place": (("University", "Company"), ("City", "Country")),
    "Place_isPartOf_Place": (("City", "Country"), ("Country", "Continent")),
}

EDGE_FILES: tuple[EdgeFile, ...] = (
    _edge("Organisation_isLocatedIn_Place", "static"),
    _edge("Place_isPartOf_Place", "static"),
    _edge("TagClass_isSubclassOf_TagClass", "static"),
    _edge("Tag_hasType_TagClass", "static"),
    _edge("Comment_hasCreator_Person", "dynamic"),
    _edge("Comment_hasTag_Tag", "dynamic"),
    _edge("Comment_isLocatedIn_Country", "dynamic"),
    _edge("Comment_replyOf_Comment", "dynamic"),
    _edge("Comment_replyOf_Post", "dynamic"),
    _edge("Forum_containerOf_Post", "dynamic"),
    _edge("Forum_hasMember_Person", "dynamic"),
    _edge("Forum_hasModerator_Person", "dynamic"),
    _edge("Forum_hasTag_Tag", "dynamic"),
    _edge("Person_hasInterest_Tag", "dynamic"),
    _edge("Person_isLocatedIn_City", "dynamic"),
    _edge("Person_knows_Person", "dynamic"),
    _edge("Person_likes_Comment", "dynamic"),
    _edge("Person_likes_Post", "dynamic"),
    _edge("Person_studyAt_University", "dynamic", "classYear:int"),
    _edge("Person_workAt_Company", "dynamic", "workFrom:int"),
    _edge("Post_hasCreator_Person", "dynamic"),
    _edge("Post_hasTag_Tag", "dynamic"),
    _edge("Post_isLocatedIn_Country", "dynamic"),
)

REL_TYPES: tuple[str, ...] = tuple(sorted({e.rel_type for e in EDGE_FILES}))


def _ordered(labels: set[str]) -> list[str]:
    return [lbl for lbl in CONCRETE_LABELS if lbl in labels]


def rel_domain(rel_type: str) -> list[str]:
    """Concrete labels a *rel_type* edge can start at, across all its files."""
    return _ordered({lbl for e in EDGE_FILES if e.rel_type == rel_type for lbl in e.src_labels})


def rel_range(rel_type: str) -> list[str]:
    """Concrete labels a *rel_type* edge can end at, across all its files."""
    return _ordered({lbl for e in EDGE_FILES if e.rel_type == rel_type for lbl in e.dst_labels})


def labels_of(concrete: str) -> list[str]:
    """All labels a node of *concrete* label carries, supertype first."""
    if concrete not in CONCRETE_LABELS:
        raise ValueError(f"not a concrete LDBC label: {concrete!r}")
    sup = _SUPERTYPE_OF.get(concrete)
    return [sup, concrete] if sup else [concrete]


def snake(name: str) -> str:
    """``TagClass`` → ``tag_class``; ``HAS_CREATOR`` → ``has_creator``."""
    if name.isupper() or "_" in name:
        return name.lower()
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
