"""Generating LDBC data sets with Datagen, and fetching generated ones (decision 12). No Docker."""

from __future__ import annotations

import gzip
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.ldbc import datagen as dg
from tests.ldbc import datasets as ds


def _write_snapshot(layout: Path) -> Path:
    tag = layout / "initial_snapshot" / "static" / "Tag"
    tag.mkdir(parents=True)
    with gzip.open(tag / "part-00000-a.csv.gz", "wt", encoding="utf-8") as fh:
        fh.write('"1"|"a"|"u"\n')
    return layout / "initial_snapshot"


class FakeDocker:
    """Stands in for :func:`tests.ldbc.datagen._run`: records commands, writes what Datagen would."""

    def __init__(self, *, image: bool = True, writes: bool = True) -> None:
        self.image = image
        self.writes = writes
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, cmd: Sequence[str], timeout: float) -> str:
        cmd = list(cmd)
        self.calls.append((cmd, timeout))
        if cmd[:3] == ["docker", "image", "inspect"] and not self.image:
            raise dg.DatagenError("No such image")
        if cmd[:2] == ["docker", "build"]:
            self.image = True
        if cmd[:2] == ["docker", "run"] and "--entrypoint" not in cmd and self.writes:
            host = Path(cmd[cmd.index("--volume") + 1].rsplit(":", 1)[0])
            _write_snapshot(host / ds.LAYOUT_SUBDIR)
        return "INFO SparkContext: Successfully stopped SparkContext\n"

    def verbs(self) -> list[str]:
        out = []
        for cmd, _ in self.calls:
            if cmd[:2] == ["docker", "run"]:
                out.append("chown" if "--entrypoint" in cmd else "generate")
            else:
                out.append(cmd[1] if cmd[1] != "image" else "inspect")
        return out


class TestCommands:
    def test_generate_uses_the_samples_options_after_the_separator(self, tmp_path):
        cmd = dg.generate_command("0.1", "iso", tmp_path)
        assert cmd[: cmd.index(dg.IMAGE) + 1] == [
            "docker",
            "run",
            "--rm",
            "--volume",
            f"{tmp_path.resolve()}:/out",
            dg.IMAGE,
        ]
        assert cmd[cmd.index(dg.IMAGE) + 1 :] == [
            "--",
            "--mode", "bi",
            "--scale-factor", "0.1",
            "--explode-edges",
            "--format-options", "header=false,quoteAll=true,compression=gzip",
        ]  # fmt: skip

    def test_epoch_millis_only_for_the_epoch_encoding(self, tmp_path):
        assert "--epoch-millis" in dg.generate_command("0.1", "epoch_ms", tmp_path)
        assert "--epoch-millis" not in dg.generate_command("0.1", "iso", tmp_path)

    def test_the_image_is_built_from_the_pinned_commit(self):
        cmd = dg.build_command()
        assert f"{dg.DATAGEN_REPO}#{ds.DATAGEN_COMMIT}" in cmd
        assert "--target=standalone" in cmd and cmd[-1] == dg.IMAGE
        assert dg.IMAGE.endswith(ds.DATAGEN_COMMIT[:8])

    def test_chown_hands_the_output_to_the_host_user(self, tmp_path):
        cmd = dg.chown_command(tmp_path, 501, 20)
        assert cmd[cmd.index("--entrypoint") + 1] == "chown"
        assert cmd[-3:] == ["-R", "501:20", "/out"]


class TestGenerate:
    def test_builds_the_image_only_when_missing(self, tmp_path):
        docker = FakeDocker(image=False)
        layout = dg.generate("0.1", "iso", tmp_path / "out", run=docker)
        assert layout == tmp_path / "out" / ds.LAYOUT_SUBDIR
        assert docker.verbs()[:3] == ["inspect", "build", "generate"]
        assert docker.calls[1][1] == dg.BUILD_TIMEOUT_S and docker.calls[2][1] == dg.GENERATE_TIMEOUT_S

        again = FakeDocker(image=True)
        dg.generate("0.1", "iso", tmp_path / "out2", run=again)
        assert "build" not in again.verbs()

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="no POSIX user ids")
    def test_output_is_chowned_to_the_caller(self, tmp_path):
        docker = FakeDocker()
        dg.generate("0.1", "iso", tmp_path / "out", run=docker)
        assert docker.verbs()[-1] == "chown"
        assert f"{os.getuid()}:{os.getgid()}" in docker.calls[-1][0]

    def test_a_silent_spark_failure_is_caught_by_the_output_check(self, tmp_path):
        """run.py exits 0 when Spark fails: the missing layout, not the exit status, is the signal."""
        with pytest.raises(dg.DatagenError, match=r"wrote no .*initial_snapshot(.|\n)*SparkContext"):
            dg.generate("0.1", "iso", tmp_path / "out", run=FakeDocker(writes=False))

    def test_a_datagen_error_is_a_dataset_error(self):
        assert issubclass(dg.DatagenError, ds.LdbcDatasetError)


class TestRun:
    def test_a_missing_executable_is_named(self):
        with pytest.raises(dg.DatagenError, match="not installed"):
            dg._run(["arango-cypher-no-such-docker-binary"], 5)

    def test_a_failure_carries_the_output_tail(self):
        with pytest.raises(dg.DatagenError, match=r"failed \(3\).*boom"):
            dg._run([sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"], 30)

    def test_returns_stdout_and_stderr(self):
        out = dg._run([sys.executable, "-c", "import sys; print('a'); print('b', file=sys.stderr)"], 30)
        assert "a" in out and "b" in out

    def test_a_timeout_is_named(self):
        with pytest.raises(dg.DatagenError, match="did not finish within"):
            dg._run([sys.executable, "-c", "import time; time.sleep(5)"], 0.2)


@pytest.fixture
def generated_dataset(tmp_path, monkeypatch):
    """A generated data set in the catalog, with a generator that writes a known snapshot."""
    ref = _write_snapshot(tmp_path / "ref" / ds.LAYOUT_SUBDIR)
    entry = ds.LdbcDataset(
        name="fake-gen",
        scale_factor="0.1",
        encoding="iso",
        url=None,
        archive_root="",
        content_digest=ds.content_digest(ref),
        datagen_commit=ds.DATAGEN_COMMIT,
    )
    monkeypatch.setitem(ds.DATASETS, entry.name, entry)
    monkeypatch.setenv("LDBC_CACHE_DIR", str(tmp_path / "cache"))
    calls: list[Path] = []

    def generator(d: ds.LdbcDataset, out: Path) -> None:
        calls.append(out)
        _write_snapshot(out / ds.LAYOUT_SUBDIR)

    return entry, generator, calls


class TestFetchGenerated:
    def test_generates_moves_into_the_cache_and_verifies(self, generated_dataset, tmp_path):
        entry, generator, calls = generated_dataset
        layout = ds.fetch(entry.name, generator=generator)
        assert layout == tmp_path / "cache" / entry.name / ds.LAYOUT_SUBDIR
        assert (layout / "initial_snapshot" / "static" / "Tag").is_dir()
        assert len(calls) == 1 and not calls[0].exists(), "the temporary output is removed"
        assert [p.name for p in (tmp_path / "cache" / entry.name).iterdir()] == ["graphs"]

    def test_generates_once(self, generated_dataset):
        entry, generator, calls = generated_dataset
        ds.fetch(entry.name, generator=generator)
        ds.fetch(entry.name, generator=generator)
        assert len(calls) == 1

    def test_never_downloads(self, generated_dataset):
        entry, generator, _ = generated_dataset

        def downloader(url: str, dest: Path) -> None:
            raise AssertionError("a generated data set has no URL")

        ds.fetch(entry.name, downloader=downloader, generator=generator)

    def test_a_failed_generation_leaves_no_cache(self, generated_dataset, tmp_path):
        entry, _, _ = generated_dataset

        def failing(d: ds.LdbcDataset, out: Path) -> None:
            (out / "graphs").mkdir(parents=True)
            raise dg.DatagenError("docker is not installed or not on PATH")

        with pytest.raises(ds.LdbcDatasetError, match="not installed"):
            ds.fetch(entry.name, generator=failing)
        assert list((tmp_path / "cache" / entry.name).iterdir()) == []

    def test_output_without_the_layout_is_an_error(self, generated_dataset):
        entry, _, _ = generated_dataset
        with pytest.raises(ds.LdbcDatasetError, match="no graphs/ directory"):
            ds.fetch(entry.name, generator=lambda d, out: out.mkdir(parents=True))

    def test_an_incomplete_cache_is_replaced(self, generated_dataset, tmp_path):
        entry, generator, calls = generated_dataset
        stale = tmp_path / "cache" / entry.name / "graphs" / "csv"
        stale.mkdir(parents=True)
        layout = ds.fetch(entry.name, generator=generator)
        assert len(calls) == 1 and (layout / "initial_snapshot").is_dir()

    def test_different_rows_fail_the_digest(self, generated_dataset):
        entry, _, _ = generated_dataset

        def other_rows(d: ds.LdbcDataset, out: Path) -> None:
            snap = _write_snapshot(out / ds.LAYOUT_SUBDIR)
            with gzip.open(snap / "static" / "Tag" / "part-00001-b.csv.gz", "wt", encoding="utf-8") as fh:
                fh.write('"2"|"b"|"u"\n')

        with pytest.raises(ds.LdbcDatasetError, match="content digest mismatch"):
            ds.fetch(entry.name, generator=other_rows)
