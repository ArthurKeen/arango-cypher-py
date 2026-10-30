"""Unit tests for the LDBC schema, data set fetcher and loader (WP-31). No database."""

from __future__ import annotations

import gzip
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from tests.helpers.fake_arango import FakeDb
from tests.ldbc import datasets as ds
from tests.ldbc.loader import (
    _MISSING,
    LdbcLoadError,
    _BulkWriter,
    _LabelIndex,
    _parse,
    convert,
    count_differences,
    edge_doc,
    lpg_indexes,
    node_doc,
    pg_indexes,
    read_rows,
)
from tests.ldbc.schema import (
    CONCRETE_LABELS,
    EDGE_FILES,
    NODE_FILES,
    REL_TYPES,
    SUPERTYPES,
    Column,
    labels_of,
    snake,
)


def _write_part(entity_dir: Path, lines: list[str], name: str = "part-00000-abc.csv.gz") -> Path:
    entity_dir.mkdir(parents=True, exist_ok=True)
    p = entity_dir / name
    with gzip.open(p, "wt", encoding="utf-8", newline="") as fh:
        fh.write("".join(f"{line}\n" for line in lines))
    return p


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_relationship_types_are_the_fifteen_ldbc_types(self):
        assert set(REL_TYPES) == {
            "KNOWS",
            "HAS_CREATOR",
            "REPLY_OF",
            "CONTAINER_OF",
            "HAS_MEMBER",
            "HAS_MODERATOR",
            "HAS_TAG",
            "HAS_INTEREST",
            "HAS_TYPE",
            "IS_SUBCLASS_OF",
            "IS_LOCATED_IN",
            "IS_PART_OF",
            "STUDY_AT",
            "WORK_AT",
            "LIKES",
        }

    def test_file_inventory_matches_the_reference_headers(self):
        assert len(NODE_FILES) == 8
        assert len(EDGE_FILES) == 23

    def test_city_and_company_endpoints_resolve_through_their_id_space(self):
        by_name = {e.name: e for e in EDGE_FILES}
        assert by_name["Person_isLocatedIn_City"].dst_space == "Place"
        assert by_name["Person_workAt_Company"].dst_space == "Organisation"
        assert by_name["Comment_replyOf_Post"].dst_space == "Post"

    def test_edge_properties_follow_the_reference(self):
        by_name = {e.name: e for e in EDGE_FILES}
        assert [c.name for c in by_name["Person_studyAt_University"].columns] == [
            "creationDate",
            "src",
            "dst",
            "classYear",
        ]
        assert [c.name for c in by_name["Place_isPartOf_Place"].columns] == ["src", "dst"]

    @pytest.mark.parametrize(
        "label,expected",
        [("Comment", ["Message", "Comment"]), ("City", ["Place", "City"]), ("Person", ["Person"])],
    )
    def test_labels_of_puts_the_supertype_first(self, label, expected):
        assert labels_of(label) == expected

    def test_labels_of_rejects_a_supertype(self):
        with pytest.raises(ValueError, match="not a concrete"):
            labels_of("Message")

    def test_every_supertype_member_is_concrete(self):
        for members in SUPERTYPES.values():
            assert set(members) <= set(CONCRETE_LABELS)

    @pytest.mark.parametrize(
        "name,expected",
        [("TagClass", "tag_class"), ("Person", "person"), ("IS_SUBCLASS_OF", "is_subclass_of")],
    )
    def test_snake(self, name, expected):
        assert snake(name) == expected


# ---------------------------------------------------------------------------
# CSV → values
# ---------------------------------------------------------------------------


class TestConvert:
    def test_empty_field_is_missing_like_neo4j_ignore_empty_strings(self):
        assert convert("", Column("imageFile", "str"), "iso") is _MISSING

    def test_ints(self):
        assert convert("962072674305", Column("id", "id"), "iso") == 962072674305
        assert convert("140", Column("length", "int"), "iso") == 140

    def test_epoch_ms_timestamps_become_ints(self):
        assert convert("1262531431499", Column("creationDate", "datetime"), "epoch_ms") == 1262531431499
        assert convert("447811200000", Column("birthday", "date"), "epoch_ms") == 447811200000

    def test_iso_timestamps_stay_text(self):
        v = "2010-01-03T15:10:31.499+00:00"
        assert convert(v, Column("creationDate", "datetime"), "iso") == v
        assert convert("1984-03-11", Column("birthday", "date"), "iso") == "1984-03-11"

    def test_iso_encoding_rejects_an_epoch_value(self):
        with pytest.raises(ValueError, match="not ISO-8601"):
            convert("1262531431499", Column("creationDate", "datetime"), "iso")

    def test_string_lists_split_on_semicolon(self):
        assert convert("fa;ku;en", Column("speaks", "str_list"), "iso") == ["fa", "ku", "en"]

    def test_unicode_survives(self):
        assert convert("Hugo Cháve", Column("content", "str"), "iso") == "Hugo Cháve"


class TestParse:
    def test_wrong_field_count_names_the_location(self):
        cols = (Column("id", "id"), Column("name", "str"))
        with pytest.raises(LdbcLoadError, match=r"Tag/part-1:7: expected 2 fields"):
            _parse(["1"], cols, "iso", "Tag/part-1:7")

    def test_bad_value_names_the_column(self):
        cols = (Column("id", "id"), Column("name", "str"))
        with pytest.raises(LdbcLoadError, match=r"column 'id' \(id\)"):
            _parse(["x", "n"], cols, "iso", "Tag/part-1:1")

    def test_empty_fields_are_omitted(self):
        cols = (Column("id", "id"), Column("imageFile", "str"), Column("content", "str"))
        assert _parse(["1", "", "hi"], cols, "iso", "w") == {"id": 1, "content": "hi"}


class TestReadRows:
    def test_quoted_pipes_and_empty_fields(self, tmp_path):
        _write_part(tmp_path / "Post", ['"1"|"a|b"|""', '"2"|"plain"|"x"'])
        rows = list(read_rows(tmp_path / "Post"))
        assert rows == [
            ("part-00000-abc.csv.gz", 1, ["1", "a|b", ""]),
            ("part-00000-abc.csv.gz", 2, ["2", "plain", "x"]),
        ]

    def test_reads_every_part_file_and_skips_markers(self, tmp_path):
        _write_part(tmp_path / "Tag", ['"1"|"a"|"u"'], "part-00000-a.csv.gz")
        _write_part(tmp_path / "Tag", ['"2"|"b"|"u"'], "part-00001-b.csv.gz")
        (tmp_path / "Tag" / "_SUCCESS").write_text("")
        assert [r[2][0] for r in read_rows(tmp_path / "Tag")] == ["1", "2"]

    def test_an_entity_without_part_files_is_an_error(self, tmp_path):
        (tmp_path / "Tag").mkdir()
        with pytest.raises(LdbcLoadError, match="no part-"):
            list(read_rows(tmp_path / "Tag"))


# ---------------------------------------------------------------------------
# Rows → documents
# ---------------------------------------------------------------------------

_COMMENT = next(nf for nf in NODE_FILES if nf.name == "Comment")
_REPLY_OF_POST = next(ef for ef in EDGE_FILES if ef.name == "Comment_replyOf_Post")


class TestDocuments:
    def test_pg_node_is_keyed_by_id_in_its_label_collection(self):
        coll, doc = node_doc(_COMMENT, {"id": 7, "content": "yes"}, "Comment", "pg")
        assert coll == "comment"
        assert doc == {"_key": "7", "id": 7, "content": "yes"}

    def test_lpg_node_carries_type_and_all_labels(self):
        coll, doc = node_doc(_COMMENT, {"id": 7}, "Comment", "lpg")
        assert coll == "nodes"
        assert doc == {"_key": "Comment_7", "type": "Comment", "labels": ["Message", "Comment"], "id": 7}

    def test_pg_edge_points_into_member_collections(self):
        coll, doc = edge_doc(_REPLY_OF_POST, {"src": 7, "dst": 3, "creationDate": 5}, "Comment", "Post", "pg")
        assert coll == "reply_of"
        assert doc == {"_from": "comment/7", "_to": "post/3", "creationDate": 5}

    def test_lpg_edge_carries_its_type(self):
        coll, doc = edge_doc(_REPLY_OF_POST, {"src": 7, "dst": 3}, "Comment", "Post", "lpg")
        assert coll == "edges"
        assert doc == {"_from": "nodes/Comment_7", "_to": "nodes/Post_3", "type": "REPLY_OF"}

    def test_dangling_endpoint_is_an_error(self):
        idx = _LabelIndex()
        idx.add("Place", 0, "Country")
        assert idx.resolve("Place", 0, "w") == "Country"
        with pytest.raises(LdbcLoadError, match="endpoint Place 99 does not exist"):
            idx.resolve("Place", 99, "w")


class TestIndexes:
    def test_pg_has_a_unique_id_index_per_concrete_label(self):
        unique = {s.collection for s in pg_indexes() if s.unique and s.fields == ("id",)}
        assert unique == {snake(lbl) for lbl in CONCRETE_LABELS}

    def test_lpg_edge_vcis_lead_with_the_traversal_anchor(self):
        edge_fields = {s.fields for s in lpg_indexes() if s.collection == "edges"}
        assert edge_fields == {("_from", "type"), ("_to", "type")}

    def test_index_names_are_unique(self):
        for specs in (pg_indexes(), lpg_indexes()):
            names = [s.name for s in specs]
            assert len(names) == len(set(names))


class TestBulkWriter:
    def test_flushes_at_batch_size_and_on_demand(self):
        db = FakeDb({"tag": []})
        w = _BulkWriter(db, batch_size=2)
        for i in range(3):
            w.add("tag", {"_key": str(i)})
        assert len(db._collections["tag"]) == 2
        w.flush_all()
        assert len(db._collections["tag"]) == 3

    def test_a_rejected_import_names_the_collection(self):
        db = FakeDb({"tag": [{"_key": "1"}]})
        w = _BulkWriter(db, batch_size=10)
        w.add("tag", {"_key": "1"})
        with pytest.raises(LdbcLoadError, match=r"into 'tag' failed"):
            w.flush_all()


# ---------------------------------------------------------------------------
# Data set fetch + verification
# ---------------------------------------------------------------------------


def _snapshot(root: Path, *, split: bool = False, extra_row: bool = False) -> Path:
    snap = root / "initial_snapshot"
    rows = ['"1"|"a"|"u"', '"2"|"b"|"u"'] + (['"3"|"c"|"u"'] if extra_row else [])
    if split:
        _write_part(snap / "static" / "Tag", rows[1:], "part-00000-x.csv.gz")
        _write_part(snap / "static" / "Tag", rows[:1], "part-00001-y.csv.gz")
    else:
        _write_part(snap / "static" / "Tag", rows, "part-00000-z.csv.gz")
    return snap


class TestContentDigest:
    def test_ignores_partitioning_and_part_file_names(self, tmp_path):
        a = ds.content_digest(_snapshot(tmp_path / "a"))
        b = ds.content_digest(_snapshot(tmp_path / "b", split=True))
        assert a == b

    def test_changes_when_a_row_changes(self, tmp_path):
        a = ds.content_digest(_snapshot(tmp_path / "a"))
        b = ds.content_digest(_snapshot(tmp_path / "b", extra_row=True))
        assert a != b

    def test_missing_directory_is_an_error(self, tmp_path):
        with pytest.raises(ds.LdbcDatasetError, match="not a directory"):
            ds.content_digest(tmp_path / "nope")


@pytest.fixture
def fake_dataset(tmp_path, monkeypatch):
    """A one-entity data set registered in the catalog, served by a fake downloader."""
    src = tmp_path / "src"
    root_name = "fake-sf"
    layout = src / root_name / "graphs" / "csv" / "bi" / "composite-projected-fk"
    snap = _snapshot(layout)
    archive = tmp_path / "fake.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for f in sorted(src.rglob("*")):
            zf.write(f, f.relative_to(src).as_posix())
    entry = ds.LdbcDataset(
        name="fake",
        scale_factor="0",
        encoding="iso",
        url="https://example.invalid/fake.zip",
        archive_root=root_name,
        content_digest=ds.content_digest(snap),
        datagen_commit="0" * 40,
    )
    monkeypatch.setitem(ds.DATASETS, "fake", entry)
    monkeypatch.setenv("LDBC_CACHE_DIR", str(tmp_path / "cache"))
    calls: list[str] = []

    def downloader(url: str, dest: Path) -> None:
        calls.append(url)
        dest.write_bytes(archive.read_bytes())

    return entry, downloader, calls


class TestFetch:
    def test_downloads_extracts_and_verifies(self, fake_dataset, tmp_path):
        entry, downloader, calls = fake_dataset
        layout = ds.fetch("fake", downloader=downloader)
        assert (layout / "initial_snapshot" / "static" / "Tag").is_dir()
        assert calls == [entry.url]

    def test_reuses_the_cache_without_downloading(self, fake_dataset):
        _, downloader, calls = fake_dataset
        ds.fetch("fake", downloader=downloader)
        ds.fetch("fake", downloader=downloader)
        assert len(calls) == 1

    def test_a_changed_cache_fails_the_digest(self, fake_dataset):
        _, downloader, _ = fake_dataset
        layout = ds.fetch("fake", downloader=downloader)
        _write_part(layout / "initial_snapshot" / "static" / "Tag", ['"9"|"z"|"u"'], "part-00009-q.csv.gz")
        with pytest.raises(ds.LdbcDatasetError, match="content digest mismatch"):
            ds.fetch("fake", downloader=downloader)

    def test_unknown_data_set(self):
        with pytest.raises(ds.LdbcDatasetError, match="unknown LDBC data set"):
            ds.fetch("sf9000")

    def test_download_failure_names_the_url(self, fake_dataset):
        def broken(url: str, dest: Path) -> None:
            raise OSError("connection refused")

        with pytest.raises(ds.LdbcDatasetError, match=r"example\.invalid.*connection refused"):
            ds.fetch("fake", downloader=broken)

    def test_not_a_zip(self, fake_dataset):
        with pytest.raises(ds.LdbcDatasetError, match="not a zip"):
            ds.fetch("fake", downloader=lambda url, dest: dest.write_bytes(b"<html>"))

    def test_refuses_members_that_escape_the_cache(self, fake_dataset, tmp_path):
        evil = tmp_path / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../../escaped.txt", "x")
        with pytest.raises(ds.LdbcDatasetError, match="outside"):
            ds.fetch("fake", downloader=lambda url, dest: dest.write_bytes(evil.read_bytes()))
        assert not (tmp_path / "escaped.txt").exists()

    def test_catalog_pins_both_encodings_at_every_scale(self):
        by_scale: dict[str, set[str]] = {}
        for d in ds.DATASETS.values():
            by_scale.setdefault(d.scale_factor, set()).add(d.encoding)
            assert len(d.content_digest) == 64
            assert d.datagen_commit == ds.DATAGEN_COMMIT
            assert d.generated or (d.url or "").startswith("https://")
        assert by_scale == {"0.003": {"epoch_ms", "iso"}, "0.1": {"epoch_ms", "iso"}}


class TestCountDifferences:
    def test_names_only_what_differs(self):
        csv = Counter({"Comment": 471, "Post": 3189})
        db = Counter({"Comment": 470, "Post": 3189, "Stray": 1})
        assert count_differences(csv, db) == ["Comment 471 → 470", "Stray 0 → 1"]

    def test_equal_counts_have_no_differences(self):
        assert count_differences(Counter({"A": 1}), Counter({"A": 1})) == []
