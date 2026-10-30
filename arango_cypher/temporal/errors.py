"""Errors raised by the temporal value library.

Every error carries ``cypher_class``, the Neo4j / openCypher TCK error detail
name a Cypher engine reports for it (``InvalidArgumentValue``,
``InvalidArgumentType``, ``UnsupportedTemporalUnit``, ``NumberOutOfRange``), so
the translator can surface the same error a Neo4j server would.

``NotConstant`` is not an error: it says an expression reads the clock
(``date()``, ``datetime.statement()``, ``time({timezone: 'Europe/Berlin'})`` …)
and so cannot be folded at compile time.
"""

from __future__ import annotations


class TemporalError(Exception):
    """A temporal operation Cypher rejects; ``cypher_class`` names the error."""

    def __init__(self, cypher_class: str, message: str) -> None:
        super().__init__(message)
        self.cypher_class = cypher_class
        self.message = message

    @property
    def tck_detail(self) -> str:
        """The TCK error detail this error is asserted as."""
        return self.cypher_class

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.cypher_class!r}, {self.message!r})"


class InvalidTemporalValue(TemporalError):
    """A well-typed argument with an invalid value: month 13, an unknown time
    zone, an unparsable string, a field combination Neo4j rejects."""

    def __init__(self, message: str) -> None:
        super().__init__("InvalidArgumentValue", message)


class InvalidTemporalType(TemporalError):
    """An argument of the wrong type: ``date(1)``, ``date + date``,
    ``duration * 'x'``, ``{year: 'x'}``."""

    def __init__(self, message: str) -> None:
        super().__init__("InvalidArgumentType", message)


class UnsupportedTemporalUnit(TemporalError):
    """A unit or accessor the value does not have: ``date.truncate('hour', …)``,
    ``date(…).hour``, ``.truncate('fortnight', …)``. The TCK reports it as
    ``InvalidArgumentValue``."""

    def __init__(self, message: str) -> None:
        super().__init__("UnsupportedTemporalUnit", message)

    @property
    def tck_detail(self) -> str:
        return "InvalidArgumentValue"


class TemporalOverflow(TemporalError):
    """A result outside Cypher's range: a year beyond ±999,999,999 or a
    duration component beyond a 64-bit integer."""

    def __init__(self, message: str) -> None:
        super().__init__("NumberOutOfRange", message)


class NotConstant(Exception):
    """The expression reads the clock and cannot be evaluated at compile time."""
