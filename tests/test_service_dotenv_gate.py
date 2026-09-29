"""The service's import-time .env load, and the switch the test suite uses."""

from __future__ import annotations

import os

from arango_cypher.service.app import NO_DOTENV_ENV, _load_dotenv_unless_disabled


class _Loader:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return True


def test_loads_by_default() -> None:
    """Deployments rely on it: a BYOC bundle's baked ROOT_PATH arrives this way."""
    loader = _Loader()

    assert _load_dotenv_unless_disabled({}, loader) is True
    assert loader.calls == 1


def test_the_switch_skips_the_load() -> None:
    loader = _Loader()

    assert _load_dotenv_unless_disabled({NO_DOTENV_ENV: "1"}, loader) is False
    assert loader.calls == 0


def test_only_an_explicit_one_disables_it() -> None:
    loader = _Loader()

    for value in ("", "0", "true"):
        _load_dotenv_unless_disabled({NO_DOTENV_ENV: value}, loader)

    assert loader.calls == 3


def test_no_dotenv_package_is_not_an_error() -> None:
    assert _load_dotenv_unless_disabled({}, None) is False


def test_the_suite_runs_with_the_switch_on() -> None:
    """tests/conftest.py sets it before the service can be imported."""
    assert os.environ.get(NO_DOTENV_ENV) == "1"
