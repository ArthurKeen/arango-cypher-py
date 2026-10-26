"""The LDBC database lifecycle: start what the run owns, remove it afterwards, never touch others'. No Docker."""

from __future__ import annotations

import pytest

from tests.ldbc.stack import STACK_ARANGO_URL, STACK_NEO4J_URI, StackError, ldbc_stack


class FakeDocker:
    """Records the compose commands ldbc_stack would run; can fail the ``up``."""

    def __init__(self, *, fail_up: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail_up = fail_up

    def __call__(self, cmd) -> None:
        self.calls.append(list(cmd))
        if self.fail_up and "up" in cmd:
            raise StackError("health check timed out")

    def verbs(self) -> list[tuple[str, tuple[str, ...]]]:
        out = []
        for c in self.calls:
            verb = "up" if "up" in c else "rm" if "rm" in c else "?"
            out.append((verb, tuple(s for s in c if s in ("arangodb", "neo4j"))))
        return out


def _reachable(url: str) -> bool:
    return True


def test_starts_and_removes_what_the_run_owns():
    docker = FakeDocker()
    with ldbc_stack(arango=True, env={}, run=docker, reachable=_reachable) as ep:
        assert ep.arango_url == STACK_ARANGO_URL
        assert ep.started == ("arangodb",)
        assert docker.verbs() == [("up", ("arangodb",))]
    assert docker.verbs() == [("up", ("arangodb",)), ("rm", ("arangodb",))]
    assert "--volumes" in docker.calls[-1] and "--stop" in docker.calls[-1]


def test_a_given_server_is_used_and_never_touched():
    docker = FakeDocker()
    with ldbc_stack(
        arango=True, env={"ARANGO_URL": "http://localhost:28529"}, run=docker, reachable=_reachable
    ) as ep:
        assert ep.arango_url == "http://localhost:28529"
        assert ep.started == ()
    assert docker.calls == []


def test_an_unreachable_given_server_is_an_error_not_a_silent_start():
    docker = FakeDocker()
    with pytest.raises(StackError, match="not reachable"):
        with ldbc_stack(
            arango=True, env={"ARANGO_URL": "http://localhost:1"}, run=docker, reachable=lambda u: False
        ):
            pass
    assert docker.calls == []


def test_teardown_runs_when_the_body_fails():
    docker = FakeDocker()
    with pytest.raises(RuntimeError, match="boom"):
        with ldbc_stack(arango=True, env={}, run=docker, reachable=_reachable):
            raise RuntimeError("boom")
    assert docker.verbs()[-1] == ("rm", ("arangodb",))


def test_teardown_runs_when_the_start_fails():
    docker = FakeDocker(fail_up=True)
    with pytest.raises(StackError, match="timed out"):
        with ldbc_stack(arango=True, env={}, run=docker, reachable=_reachable):
            pytest.fail("the body must not run when the start failed")
    assert docker.verbs() == [("up", ("arangodb",)), ("rm", ("arangodb",))]


def test_keep_stack_skips_teardown():
    docker = FakeDocker()
    with ldbc_stack(arango=True, env={"LDBC_KEEP_STACK": "1"}, run=docker, reachable=_reachable):
        pass
    assert docker.verbs() == [("up", ("arangodb",))]


def test_only_the_services_this_run_started_are_removed():
    docker = FakeDocker()
    env = {"ARANGO_URL": "http://localhost:28529"}
    with ldbc_stack(arango=True, neo4j=True, env=env, run=docker, reachable=_reachable) as ep:
        assert ep.neo4j_uri == STACK_NEO4J_URI and ep.started == ("neo4j",)
    assert docker.verbs() == [("up", ("neo4j",)), ("rm", ("neo4j",))]


def test_nothing_requested_runs_nothing():
    docker = FakeDocker()
    with ldbc_stack(arango=False, neo4j=False, env={}, run=docker, reachable=_reachable) as ep:
        assert ep.started == ()
    assert docker.calls == []
