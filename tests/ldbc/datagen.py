"""Generate LDBC SNB data sets with LDBC's data generator (docs/ldbc_benchmark_plan.md decision 12).

LDBC publishes the BI ``composite-projected-fk`` layout this harness loads only
as the SF0.003 samples and from SF1 up, so SF0.1 is generated here with
`ldbc_snb_datagen_spark <https://github.com/ldbc/ldbc_snb_datagen_spark>`_,
pinned to the commit the samples name (:data:`tests.ldbc.datasets.DATAGEN_COMMIT`).
Datagen is deterministic: given the samples' options it reproduces their rows
exactly (SF0.003 ISO regenerated to the pinned content digest on 2026-10-30),
so a generated data set is pinned by content digest like a downloaded one.

Datagen runs in Docker. Its image is built on first use from the pinned
commit's Dockerfile (``standalone`` target) and tagged :data:`IMAGE`; nothing is
pulled from a registry. Two quirks shape the code:

* ``run.py`` exits 0 when Spark fails, so success is judged by the output
  directory, never by the exit status.
* The container runs as root and cannot run as the host user (Spark needs a
  passwd entry to resolve ``user.home``), so the output is handed back to the
  host user with a ``chown`` from the same image. On Docker Desktop that is a
  no-op; on Linux it keeps the cache deletable.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from tests.ldbc.datasets import DATAGEN_COMMIT, LAYOUT_SUBDIR, Encoding, LdbcDatasetError

logger = logging.getLogger(__name__)

DATAGEN_REPO = "https://github.com/ldbc/ldbc_snb_datagen_spark.git"
IMAGE = f"arango-cypher-ldbc-datagen:{DATAGEN_COMMIT[:8]}"

#: Seconds allowed to build the image (Maven and Spark downloads included).
BUILD_TIMEOUT_S = 3600
#: Seconds allowed for one generation; SF0.1 takes about three minutes.
GENERATE_TIMEOUT_S = 3600
#: Seconds allowed for a ``docker image inspect`` or the ``chown``.
SHORT_TIMEOUT_S = 300

#: ``--format-options`` of the published samples (their README).
_FORMAT_OPTIONS = "header=false,quoteAll=true,compression=gzip"

#: Characters of Datagen's output kept in an error message.
_LOG_TAIL = 1500

#: ``(command, timeout_s) -> combined stdout and stderr``; raises :class:`DatagenError`.
Runner = Callable[[Sequence[str], float], str]


class DatagenError(LdbcDatasetError):
    """Datagen could not be built or run, or wrote no data set."""


def _run(cmd: Sequence[str], timeout: float) -> str:
    try:
        done = subprocess.run(list(cmd), check=True, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise DatagenError("docker is not installed or not on PATH") from e
    except subprocess.CalledProcessError as e:
        raise DatagenError(
            f"{' '.join(cmd)} failed ({e.returncode}): {((e.stdout or '') + (e.stderr or '')).strip()[-_LOG_TAIL:]}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise DatagenError(f"{' '.join(cmd)} did not finish within {e.timeout}s") from e
    return (done.stdout or "") + (done.stderr or "")


def build_command() -> list[str]:
    return ["docker", "build", f"{DATAGEN_REPO}#{DATAGEN_COMMIT}", "--target=standalone", "-t", IMAGE]


def generate_command(scale_factor: str, encoding: Encoding, out: Path) -> list[str]:
    """The ``docker run`` that writes SF *scale_factor* in *encoding* under *out*.

    ``run.py`` passes what follows ``--`` to Datagen; without it the options are
    taken as its own and rejected.
    """
    extra = ["--epoch-millis"] if encoding == "epoch_ms" else []
    return [
        "docker", "run", "--rm", "--volume", f"{out.resolve()}:/out", IMAGE,
        "--", "--mode", "bi", "--scale-factor", scale_factor, "--explode-edges", *extra,
        "--format-options", _FORMAT_OPTIONS,
    ]  # fmt: skip


def chown_command(out: Path, uid: int, gid: int) -> list[str]:
    return [
        "docker", "run", "--rm", "--volume", f"{out.resolve()}:/out", "--entrypoint", "chown", IMAGE,
        "-R", f"{uid}:{gid}", "/out",
    ]  # fmt: skip


def image_present(run: Runner = _run) -> bool:
    try:
        run(["docker", "image", "inspect", IMAGE], SHORT_TIMEOUT_S)
    except DatagenError:
        return False
    return True


def generate(scale_factor: str, encoding: Encoding, out: Path, *, run: Runner = _run) -> Path:
    """Generate SF *scale_factor* into the empty directory *out*; return its ``composite-projected-fk`` dir.

    Builds :data:`IMAGE` first if it is missing. The caller verifies the content
    digest; this only checks that Datagen wrote the layout at all.
    """
    if not image_present(run):
        logger.info(
            "building %s from %s at %s (one-off, several minutes)", IMAGE, DATAGEN_REPO, DATAGEN_COMMIT
        )
        run(build_command(), BUILD_TIMEOUT_S)
    out.mkdir(parents=True, exist_ok=True)
    logger.info("generating LDBC SF%s (%s) into %s", scale_factor, encoding, out)
    log = run(generate_command(scale_factor, encoding, out), GENERATE_TIMEOUT_S)
    if hasattr(os, "getuid"):
        run(chown_command(out, os.getuid(), os.getgid()), SHORT_TIMEOUT_S)
    layout = out / LAYOUT_SUBDIR
    if not (layout / "initial_snapshot").is_dir():
        raise DatagenError(
            f"Datagen SF{scale_factor} ({encoding}) wrote no {LAYOUT_SUBDIR / 'initial_snapshot'} under {out}. "
            f"Its output ends:\n{log.strip()[-_LOG_TAIL:]}"
        )
    return layout
