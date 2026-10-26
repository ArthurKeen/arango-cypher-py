"""Bring the LDBC databases up for a run and take them down afterwards.

The LDBC suites and tools (params, oracle, execution, bench) need ArangoDB and,
for the oracle, Neo4j. :func:`ldbc_stack` provides them:

* **Someone else's server is used, never touched.** If ``ARANGO_URL`` /
  ``NEO4J_URI`` is set, that server is used as-is — CI's service containers, or
  a long-lived dev instance — and nothing is started or stopped.
* **Otherwise the run owns its databases.** The service is started from
  ``docker-compose.ldbc.yml`` (own ports, capped memory), the run waits for its
  health check, and on exit — success or failure — the services it started
  are stopped and removed with their volumes (``docker compose rm --stop
  --volumes``): no container or data outlives the run, and a service the run
  did not start is never stopped.
* ``LDBC_KEEP_STACK=1`` skips the teardown, to inspect the databases after a run.

Bringing a service up is idempotent: if this stack's container is already
running (a kept stack, or a concurrent run), ``up`` reuses it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.ldbc.yml"
PROJECT = "arango_cypher_ldbc"

#: Endpoints of the services in docker-compose.ldbc.yml.
STACK_ARANGO_URL = "http://127.0.0.1:28531"
STACK_NEO4J_URI = "bolt://127.0.0.1:27688"

#: Seconds to wait for health checks.
UP_TIMEOUT_S = 240

Runner = Callable[[Sequence[str]], None]


class StackError(RuntimeError):
    """The databases could not be provided."""


@dataclass(frozen=True)
class Endpoints:
    arango_url: str | None
    neo4j_uri: str | None
    #: Services this run started (and will remove), e.g. ``("arangodb",)``.
    started: tuple[str, ...] = ()


def _run(cmd: Sequence[str]) -> None:
    try:
        subprocess.run(
            list(cmd), cwd=ROOT, check=True, capture_output=True, text=True, timeout=UP_TIMEOUT_S + 60
        )
    except FileNotFoundError as e:
        raise StackError("docker is not installed or not on PATH") from e
    except subprocess.CalledProcessError as e:
        raise StackError(
            f"{' '.join(cmd)} failed ({e.returncode}): {(e.stderr or e.stdout or '').strip()[-800:]}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise StackError(f"{' '.join(cmd)} did not finish within {e.timeout}s") from e


def _compose(*args: str) -> list[str]:
    return ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", PROJECT, *args]


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=20)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return True


def arango_reachable(url: str) -> bool:
    user = os.environ.get("ARANGO_USER") or "root"
    pw = os.environ.get("ARANGO_PASS") or "openSesame"
    import base64

    req = urllib.request.Request(
        f"{url.rstrip('/')}/_api/version",
        headers={"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 — local test database
            return resp.status == 200
    except OSError:
        return False


@contextmanager
def ldbc_stack(
    *,
    arango: bool = True,
    neo4j: bool = False,
    env: dict[str, str] | None = None,
    run: Runner = _run,
    reachable: Callable[[str], bool] = arango_reachable,
) -> Iterator[Endpoints]:
    """Provide the requested databases for the duration of the ``with`` block."""
    env = os.environ if env is None else env
    wanted: list[str] = []
    arango_url: str | None = None
    neo4j_uri: str | None = None

    if arango:
        given = env.get("ARANGO_URL")
        if given:
            if not reachable(given):
                raise StackError(
                    f"ARANGO_URL={given} is set but not reachable; unset it to let the run start its own"
                )
            arango_url = given
        else:
            wanted.append("arangodb")
            arango_url = STACK_ARANGO_URL
    if neo4j:
        given = env.get("NEO4J_URI")
        if given:
            neo4j_uri = given  # the driver reports an unreachable server with a clear error
        else:
            wanted.append("neo4j")
            neo4j_uri = STACK_NEO4J_URI

    def teardown() -> None:
        if not wanted:
            return
        if env.get("LDBC_KEEP_STACK") == "1":
            logger.warning("LDBC_KEEP_STACK=1: leaving %s running (%s)", ", ".join(wanted), PROJECT)
            return
        logger.info("removing LDBC databases: %s", ", ".join(wanted))
        run(_compose("rm", "--stop", "--force", "--volumes", *wanted))

    if wanted:
        logger.info("starting LDBC databases: %s", ", ".join(wanted))
        try:
            run(_compose("up", "-d", "--wait", "--wait-timeout", str(UP_TIMEOUT_S), *wanted))
        except BaseException:
            # A failed or timed-out start can leave containers running.
            teardown()
            raise
    try:
        yield Endpoints(arango_url=arango_url, neo4j_uri=neo4j_uri, started=tuple(wanted))
    finally:
        teardown()
