"""The openCypher TCK's temporal scenarios as an oracle for ``arango_cypher.temporal``.

Every scenario of ``tests/tck/features/expressions/temporal/Temporal1-10``
is run through a tiny, test-only evaluator for the query shapes those
scenarios use — ``CREATE`` of one node per label with literal properties,
``MATCH`` of those nodes by label, ``WITH`` / ``RETURN`` projections, and
expressions made of literals, maps, lists, temporal function calls, property
access, arithmetic and comparison — and the result row is checked against the
scenario's expected table. The evaluator calls only the library's public API,
so a passing row means the library computes exactly the value Neo4j returns.

This is not the TCK harness for the translator (``tests/tck/``); it needs no
database and runs in the fast suite.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from arango_cypher import temporal as T
from tests.tck.gherkin import Scenario, parse_feature

FEATURES = Path(__file__).parent / "tck" / "features" / "expressions" / "temporal"

# --- the expression evaluator ----------------------------------------------------

_TOKEN = re.compile(
    r"\s*(?:(?P<num>\d+\.\d+|\d+)|(?P<str>'(?:[^'\\]|\\.)*')|(?P<name>[A-Za-z_][A-Za-z_0-9]*)"
    r"|(?P<op><=|>=|<>|[-+*/=<>(){}\[\],:.]))"
)


@dataclass(frozen=True)
class Tok:
    kind: str  # num / str / name / op / end
    text: str
    start: int
    end: int


def tokenize(text: str) -> list[Tok]:
    tokens: list[Tok] = []
    pos = 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        m = _TOKEN.match(text, pos)
        if m is None or m.end() == pos:
            raise ValueError(f"cannot tokenize {text[pos:]!r}")
        kind = m.lastgroup or "op"
        tokens.append(Tok(kind, m.group(kind), m.start(kind), m.end(kind)))
        pos = m.end()
    tokens.append(Tok("end", "", len(text), len(text)))
    return tokens


@dataclass
class Node:
    labels: tuple[str, ...]
    props: dict[str, Any]


_CONSTRUCTORS = ("date", "localtime", "time", "localdatetime", "datetime", "duration")
_BETWEEN = {"between": None, "inmonths": "months", "indays": "days", "inseconds": "seconds"}


def call(name: str, args: list[Any]) -> Any:
    lower = name.lower()
    if lower in _CONSTRUCTORS:
        return T.construct(lower, *args)
    head, _, tail = lower.partition(".")
    if tail == "truncate" and head in _CONSTRUCTORS:
        return T.truncate(head, args[0], args[1], args[2] if len(args) > 2 else None)
    if lower == "datetime.fromepoch":
        return T.from_epoch(args[0], args[1])
    if lower == "datetime.fromepochmillis":
        return T.from_epoch_millis(args[0])
    if head == "duration" and tail in _BETWEEN:
        return T.duration_between(args[0], args[1], _BETWEEN[tail])
    if tail in T.CLOCK_SOURCES and head in _CONSTRUCTORS:
        return T.clock(head, tail, *args)
    if lower == "tostring":
        return T.to_string(args[0])
    raise NotImplementedError(f"function {name}")


def _compare_op(op: str, a: Any, b: Any) -> bool | None:
    if op == "=":
        return T.equals(a, b)
    if op == "<>":
        eq = T.equals(a, b)
        return None if eq is None else not eq
    c = T.compare(a, b)
    if c is None:
        return None
    return {"<": c < 0, ">": c > 0, "<=": c <= 0, ">=": c >= 0}[op]


_ARITH: dict[str, Callable[[Any, Any], Any]] = {
    "+": T.add,
    "-": T.subtract,
    "*": T.multiply,
    "/": T.divide,
}


@dataclass
class Evaluator:
    """Parses and evaluates one query (or one expected-value literal) at once:
    every scenario produces a single row, so there is nothing to plan."""

    text: str
    env: dict[str, Any] = field(default_factory=dict)
    nodes: list[Node] = field(default_factory=list)
    pos: int = 0

    def __post_init__(self) -> None:
        self.toks = tokenize(self.text)

    # token helpers
    @property
    def tok(self) -> Tok:
        return self.toks[self.pos]

    def at(self, text: str) -> bool:
        return self.tok.kind in ("op", "name") and self.tok.text.upper() == text.upper()

    def take(self, text: str | None = None) -> Tok:
        tok = self.tok
        if text is not None and not self.at(text):
            raise ValueError(f"expected {text!r} at {tok.text!r} in {self.text!r}")
        self.pos += 1
        return tok

    # queries
    def run(self) -> dict[str, Any] | None:
        """The RETURN row, or None for an update-only query."""
        while self.tok.kind != "end":
            clause = self.take().text.upper()
            if clause == "CREATE":
                self._create()
            elif clause == "MATCH":
                self._match()
            elif clause == "WITH":
                self.env = self._items()
            elif clause == "RETURN":
                columns = self._items()
                if self.tok.kind != "end":
                    raise ValueError(f"trailing input after RETURN in {self.text!r}")
                return columns
            else:
                raise NotImplementedError(f"clause {clause}")
        return None

    def _pattern(self) -> tuple[str | None, tuple[str, ...], dict[str, Any]]:
        self.take("(")
        var = self.take().text if self.tok.kind == "name" else None
        labels: tuple[str, ...] = ()
        while self.at(":"):
            self.take(":")
            labels += (self.take().text,)
        props = self._map() if self.at("{") else {}
        self.take(")")
        return var, labels, props

    def _create(self) -> None:
        while True:
            var, labels, props = self._pattern()
            node = Node(labels, props)
            self.nodes.append(node)
            if var:
                self.env[var] = node
            if not self.at(","):
                return
            self.take(",")

    def _match(self) -> None:
        while True:
            var, labels, _ = self._pattern()
            found = [n for n in self.nodes if set(labels) <= set(n.labels)]
            if len(found) != 1:
                raise ValueError(f"MATCH {labels} found {len(found)} nodes")
            if var:
                self.env[var] = found[0]
            if not self.at(","):
                return
            self.take(",")

    def _items(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while True:
            start = self.tok.start
            value = self.expr()
            end = self.toks[self.pos - 1].end
            name = self.text[start:end].strip()
            if self.at("AS"):
                self.take("AS")
                name = self.take().text
            out[name] = value
            if not self.at(","):
                return out
            self.take(",")

    # expressions
    def expr(self) -> Any:
        left = self._additive()
        for op in ("<=", ">=", "<>", "=", "<", ">"):
            if self.tok.kind == "op" and self.tok.text == op:
                self.take()
                right = self._additive()
                return _compare_op(op, left, right)
        return left

    def _additive(self) -> Any:
        value = self._multiplicative()
        while self.tok.kind == "op" and self.tok.text in ("+", "-"):
            op = self.take().text
            value = self._apply(op, value, self._multiplicative())
        return value

    def _multiplicative(self) -> Any:
        value = self._unary()
        while self.tok.kind == "op" and self.tok.text in ("*", "/"):
            op = self.take().text
            value = self._apply(op, value, self._unary())
        return value

    @staticmethod
    def _apply(op: str, a: Any, b: Any) -> Any:
        if T.is_temporal(a) or T.is_temporal(b):
            return _ARITH[op](a, b)
        if a is None or b is None:
            return None
        return {"+": a + b, "-": a - b, "*": a * b, "/": a / b}[op]

    def _unary(self) -> Any:
        if self.at("-"):
            self.take()
            value = self._unary()
            return None if value is None else -value
        return self._postfix()

    def _postfix(self) -> Any:
        value = self._primary()
        while self.at("."):
            self.take(".")
            value = self._property(value, self.take().text)
        return value

    @staticmethod
    def _property(value: Any, name: str) -> Any:
        if value is None:
            return None
        if isinstance(value, Node):
            return value.props.get(name)
        return T.get_property(value, name)

    def _primary(self) -> Any:
        tok = self.tok
        if tok.kind == "num":
            self.take()
            return float(tok.text) if "." in tok.text else int(tok.text)
        if tok.kind == "str":
            self.take()
            return re.sub(r"\\(.)", r"\1", tok.text[1:-1])
        if self.at("("):
            self.take("(")
            value = self.expr()
            self.take(")")
            return value
        if self.at("{"):
            return self._map()
        if self.at("["):
            self.take("[")
            items: list[Any] = []
            while not self.at("]"):
                items.append(self.expr())
                if self.at(","):
                    self.take(",")
            self.take("]")
            return items
        if tok.kind == "name":
            lowered = tok.text.lower()
            if lowered in ("null", "true", "false"):
                self.take()
                return {"null": None, "true": True, "false": False}[lowered]
            # a dotted function name, or a variable followed by property access
            parts = [self.take().text]
            mark = self.pos
            while self.at(".") and self.toks[self.pos + 1].kind == "name":
                self.take(".")
                parts.append(self.take().text)
            if self.at("("):
                self.take("(")
                args: list[Any] = []
                while not self.at(")"):
                    args.append(self.expr())
                    if self.at(","):
                        self.take(",")
                self.take(")")
                return call(".".join(parts), args)
            self.pos = mark
            if parts[0] not in self.env:
                raise ValueError(f"unknown variable {parts[0]!r} in {self.text!r}")
            return self.env[parts[0]]
        raise ValueError(f"unexpected {tok.text!r} in {self.text!r}")

    def _map(self) -> dict[str, Any]:
        self.take("{")
        out: dict[str, Any] = {}
        while not self.at("}"):
            key = self.take().text
            self.take(":")
            out[key] = self.expr()
            if self.at(","):
                self.take(",")
        self.take("}")
        return out


def literal(text: str) -> Any:
    ev = Evaluator(text)
    value = ev.expr()
    if ev.tok.kind != "end":
        raise ValueError(f"trailing input in literal {text!r}")
    return value


def matches(actual: Any, expected: Any) -> bool:
    """A result value against the TCK's expected literal: temporal values are
    written as their string form."""
    if T.is_temporal(actual):
        return isinstance(expected, str) and str(actual) == expected
    if isinstance(actual, list):
        return (
            isinstance(expected, list)
            and len(actual) == len(expected)
            and all(matches(a, e) for a, e in zip(actual, expected, strict=True))
        )
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    return bool(actual == expected)


# --- the scenarios -------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    feature: str
    scenario: Scenario

    @property
    def queries(self) -> list[str]:
        """Every query of the scenario in order (setup, the query, a control
        query); the last one's result is checked."""
        return [step.doc_string for step in self.scenario.steps if step.doc_string is not None]

    @property
    def expected(self) -> list[list[str]]:
        for step in self.scenario.steps:
            if step.text.startswith("the result should be") and step.data_table is not None:
                return step.data_table
        raise ValueError(f"no expected result in {self.scenario.name}")


def _cases() -> list[Case]:
    cases: list[Case] = []
    for path in sorted(FEATURES.glob("Temporal*.feature"), key=lambda p: int(p.stem[len("Temporal") :])):
        for scenario in parse_feature(path).scenarios:
            cases.append(Case(path.stem, scenario))
    return cases


CASES = _cases()


NOT_CONSTANT = "not constant"


def run_case(case: Case) -> tuple[bool, str]:
    """(passed, explanation) for one scenario; a scenario reading the clock
    gives (False, NOT_CONSTANT)."""
    nodes: list[Node] = []
    columns: dict[str, Any] | None = None
    try:
        for query in case.queries:
            columns = Evaluator(query, nodes=nodes).run()
    except T.NotConstant:
        return False, NOT_CONSTANT
    except T.TemporalError as exc:
        return False, f"raised {exc!r}"
    if columns is None:
        return False, "the query has no RETURN"
    table = case.expected
    header, rows = table[0], table[1:]
    if len(rows) != 1:
        return False, f"expected {len(rows)} rows, the evaluator produces one"
    for column, cell in zip(header, rows[0], strict=True):
        if column not in columns:
            return False, f"no column {column!r} in {sorted(columns)}"
        expected = literal(cell)
        if not matches(columns[column], expected):
            return False, f"{column}: got {columns[column]!s} ({columns[column]!r}), expected {cell}"
    return True, ""


@pytest.mark.parametrize("case", CASES, ids=[f"{c.feature}:{c.scenario.name}" for c in CASES])
def test_tck_temporal_row(case: Case) -> None:
    passed, why = run_case(case)
    if why == NOT_CONSTANT:
        pytest.skip("reads the clock: not a constant expression")
    queries = "\n".join(case.queries)
    assert passed, f"{case.feature} {case.scenario.name}\n{queries}\n{why}"


def test_every_temporal_feature_is_covered() -> None:
    """All ten feature files are read and none is empty."""
    by_feature: dict[str, int] = {}
    for case in CASES:
        by_feature[case.feature] = by_feature.get(case.feature, 0) + 1
    assert sorted(by_feature) == sorted(f"Temporal{i}" for i in range(1, 11))
    assert all(n > 0 for n in by_feature.values())


if __name__ == "__main__":  # a per-feature pass count: python -m tests.test_temporal_tck_tables
    totals: dict[str, list[int]] = {}
    for case in CASES:
        passed, why = run_case(case)
        counts = totals.setdefault(case.feature, [0, 0, 0])
        counts[1] += 1
        counts[0] += passed
        counts[2] += why == NOT_CONSTANT
        if not passed:
            print(f"{'CLOCK' if why == NOT_CONSTANT else 'FAIL'} {case.feature} {case.scenario.name}: {why}")
    for feature, (ok, total, clock) in totals.items():
        print(f"{feature}: {ok}/{total} ({clock} read the clock)")
    print(f"total: {sum(c[0] for c in totals.values())}/{sum(c[1] for c in totals.values())}")
