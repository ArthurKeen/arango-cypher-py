"""``_read_rows``: the cap on rows ``/execute`` and ``/execute-aql`` return.

The live counterpart is ``tests/integration/test_execute_row_cap_live.py``;
these cover what a real server will not readily produce (a failing close).
The fake mirrors python-arango's ``Cursor``: iterable, and
``close(ignore_missing: bool = False) -> bool | None``.
"""

from __future__ import annotations

import logging

import pytest

from arango_cypher.service.routes.cypher import _read_rows


class FakeCursor:
    def __init__(self, n: int, *, close_error: Exception | None = None) -> None:
        self._rows = iter(range(n))
        self.read = 0
        self.closed_with: list[bool] = []
        self._close_error = close_error

    def __iter__(self) -> FakeCursor:
        return self

    def __next__(self) -> int:
        value = next(self._rows)
        self.read += 1
        return value

    def close(self, ignore_missing: bool = False) -> bool | None:
        self.closed_with.append(ignore_missing)
        if self._close_error is not None:
            raise self._close_error
        return True


def test_without_a_cap_every_row_is_read() -> None:
    cursor = FakeCursor(5)
    assert _read_rows(cursor, None) == ([0, 1, 2, 3, 4], False)
    assert cursor.closed_with == []


def test_exactly_the_cap_is_not_truncated() -> None:
    cursor = FakeCursor(3)
    assert _read_rows(cursor, 3) == ([0, 1, 2], False)
    assert cursor.closed_with == []


def test_more_than_the_cap_reads_one_extra_row_and_closes_the_cursor() -> None:
    cursor = FakeCursor(1_000_000)
    rows, truncated = _read_rows(cursor, 3)
    assert (rows, truncated) == ([0, 1, 2], True)
    assert cursor.read == 4  # not the million
    assert cursor.closed_with == [True]


def test_a_failing_close_keeps_the_rows_and_logs_why(caplog: pytest.LogCaptureFixture) -> None:
    cursor = FakeCursor(10, close_error=RuntimeError("coordinator went away"))
    with caplog.at_level(logging.WARNING, logger="arango_cypher.service"):
        assert _read_rows(cursor, 2) == ([0, 1], True)
    assert "closing a truncated cursor failed: RuntimeError: coordinator went away" in caplog.text


def test_a_plain_iterable_needs_no_close() -> None:
    assert _read_rows(iter(range(4)), 2) == ([0, 1], True)
