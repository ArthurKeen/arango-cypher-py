"""``sync``: narrowing python-arango results to the synchronous case, loudly."""

from __future__ import annotations

import pytest
from arango.job import AsyncJob, BatchJob

from arango_cypher._arango_sync import bind, sync


def test_a_synchronous_result_passes_through_unchanged() -> None:
    rows = [{"a": 1}]

    assert sync(rows) is rows
    assert sync(0) == 0  # a zero count is a result, not a missing one
    assert sync([]) == []


@pytest.mark.parametrize("job_cls", [AsyncJob, BatchJob])
def test_an_async_or_batch_job_is_rejected(job_cls: type) -> None:
    job = job_cls.__new__(job_cls)  # no connection needed to test the type check

    with pytest.raises(TypeError, match="synchronous"):
        sync(job)


def test_none_is_rejected() -> None:
    with pytest.raises(TypeError, match="got None"):
        sync(None)


def test_bind_copies_without_changing_values() -> None:
    values = {"@col": "persons", "n": 20}

    out = bind(values)

    assert out == values
    assert out is not values
