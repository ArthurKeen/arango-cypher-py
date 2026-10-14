"""``scripts/package-byoc.sh`` bakes the error inbox's two settings, and only them.

The platform forwards no app env, so the inbox repository and token travel in
the bundle's ``.env``. Nothing else from the repo ``.env`` (passwords, LLM
keys) may ride along, and a bundle holding the token is written mode 600.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "package-byoc.sh"


def package(tmp_path: Path, env_file: str, **env: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    (tmp_path / "env").write_text(env_file)
    out = tmp_path / "bundle.tar.gz"
    base = {k: v for k, v in os.environ.items() if not k.startswith("ARANGO_CYPHER_ISSUE_")}
    result = subprocess.run(
        ["bash", str(SCRIPT), str(out)],
        env={**base, "ENV_FILE": str(tmp_path / "env"), "PACKAGE_INCLUDE_UI": "0", **env},
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result, out


def baked_env(out: Path) -> dict[str, str]:
    with tarfile.open(out) as tar:
        member = next(m for m in tar.getmembers() if m.name.removeprefix("./") == ".env")
        text = tar.extractfile(member).read().decode()  # type: ignore[union-attr]
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def test_both_settings_are_baked_alone_and_the_bundle_is_private(tmp_path: Path) -> None:
    result, out = package(
        tmp_path,
        'ARANGO_PASSWORD=never-ship\nOPENAI_API_KEY=sk-never\nARANGO_CYPHER_ISSUE_REPO=acme/inbox\nARANGO_CYPHER_ISSUE_TOKEN="ghp_x"\n',
    )
    assert result.returncode == 0, result.stderr
    assert baked_env(out) == {"ARANGO_CYPHER_ISSUE_REPO": "acme/inbox", "ARANGO_CYPHER_ISSUE_TOKEN": "ghp_x"}
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert "ghp_x" not in result.stdout + result.stderr


def test_without_an_inbox_nothing_is_baked(tmp_path: Path) -> None:
    result, out = package(tmp_path, "ARANGO_PASSWORD=never-ship\n")
    assert result.returncode == 0, result.stderr
    assert "will not report its errors" in result.stderr
    with tarfile.open(out) as tar:
        assert not any(m.name.removeprefix("./") == ".env" for m in tar.getmembers())


@pytest.mark.parametrize(
    "only", ["ARANGO_CYPHER_ISSUE_REPO=acme/inbox\n", "ARANGO_CYPHER_ISSUE_TOKEN=ghp_x\n"]
)
def test_half_a_configuration_is_refused(tmp_path: Path, only: str) -> None:
    result, _ = package(tmp_path, only)
    assert result.returncode != 0 and "set both" in result.stderr


def test_the_environment_wins_over_the_env_file(tmp_path: Path) -> None:
    result, out = package(
        tmp_path,
        "ARANGO_CYPHER_ISSUE_REPO=acme/old\nARANGO_CYPHER_ISSUE_TOKEN=ghp_old\n",
        ARANGO_CYPHER_ISSUE_REPO="acme/new",
        ARANGO_CYPHER_ISSUE_TOKEN="ghp_new",
    )
    assert result.returncode == 0, result.stderr
    assert baked_env(out)["ARANGO_CYPHER_ISSUE_REPO"] == "acme/new"
