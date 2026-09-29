"""Narrow python-arango's API results to the synchronous case this package uses.

python-arango types every call as ``T | AsyncJob[T] | BatchJob[T] | None``,
because one method serves standard, async and batch database handles alike.
This package only ever holds a standard (synchronous) handle, where the
result is always ``T``. :func:`sync` states that once, and fails loudly if the
assumption is ever broken, rather than a ``cast`` or ``# type: ignore`` at
every call site silently trusting it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, overload

from arango.cursor import Cursor
from arango.job import AsyncJob, BatchJob

T = TypeVar("T")

__all__ = ["bind", "sync"]


@overload
def sync(result: Cursor | AsyncJob[Cursor] | BatchJob[Cursor] | None) -> Cursor: ...


@overload
def sync(result: T | AsyncJob[T] | BatchJob[T] | None) -> T: ...


# The Cursor overload exists because mypy solves T from the *outer* call:
# ``list(sync(cursor))`` would infer T = Iterable[Any], and the invariant job
# types then fail to match ``AsyncJob[Cursor]``.
def sync(result: Any) -> Any:
    """Return *result* from a synchronous python-arango call.

    Raises ``TypeError`` for an async/batch job — this package never opens
    such handles, so receiving one means a caller passed the wrong database —
    and for ``None``, which a standard handle never returns.
    """
    if isinstance(result, (AsyncJob, BatchJob)):
        raise TypeError(
            f"expected a synchronous python-arango result, got {type(result).__name__}; "
            "pass a standard database handle, not an async or batch one"
        )
    if result is None:
        raise TypeError("expected a synchronous python-arango result, got None")
    return result


def bind(values: Mapping[str, Any]) -> dict[str, Any]:
    """AQL bind variables, typed as python-arango accepts them at runtime.

    Its stubs type values as ``numbers.Number``, which mypy does not treat
    ``int`` as satisfying, so every literal ``{"n": 20}`` is flagged. Values
    are passed through unchanged.
    """
    return dict(values)
