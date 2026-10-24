"""The vendored LDBC SNB operations (``tests/ldbc/manifest.yml``).

Every operation is a verbatim upstream file pinned by SHA-256; variants are
labelled adaptations of one operation. ``out_of_subset`` marks operations whose
reference implementation needs Neo4j GDS or APOC procedures — they stay in the
corpus, recorded as such, and are excluded from pass-rate denominators.

``python -m tests.ldbc.corpus --rehash`` re-pins the hashes after a deliberate
refresh (see ``queries/UPSTREAM.md``).
"""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

LDBC_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = LDBC_DIR / "manifest.yml"
QUERIES_DIR = LDBC_DIR / "queries"

Kind = Literal["read", "insert", "delete"]


class CorpusError(ValueError):
    """The manifest and the vendored files disagree."""


@dataclass(frozen=True)
class Operation:
    id: str
    file: str
    sha256: str
    workload: str
    kind: Kind
    out_of_subset: str | None = None
    #: Output columns built by ``collect()``, whose element order Cypher leaves
    #: undefined; compared as multisets.
    unordered_columns: tuple[str, ...] = ()
    #: For a variant, the operation it adapts and why.
    variant_of: str | None = None
    reason: str | None = None

    @property
    def path(self) -> Path:
        return QUERIES_DIR / self.file

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(manifest: Path = MANIFEST_PATH) -> list[Operation]:
    """Every operation, then every variant, in manifest order."""
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise CorpusError(f"{manifest}: expected a version-1 manifest")
    ops: list[Operation] = []
    by_id: dict[str, Operation] = {}
    for entry in raw.get("operations") or []:
        op = Operation(
            id=entry["id"],
            file=entry["file"],
            sha256=entry["sha256"],
            workload=entry["workload"],
            kind=entry["kind"],
            out_of_subset=entry.get("out_of_subset"),
            unordered_columns=tuple(entry.get("unordered_columns") or ()),
        )
        ops.append(op)
        by_id[op.id] = op
    for entry in raw.get("variants") or []:
        base = by_id.get(entry["of"])
        if base is None:
            raise CorpusError(f"variant {entry['id']} adapts unknown operation {entry['of']!r}")
        ops.append(
            Operation(
                id=entry["id"],
                file=entry["file"],
                sha256=entry["sha256"],
                workload=base.workload,
                kind=base.kind,
                out_of_subset=base.out_of_subset,
                unordered_columns=base.unordered_columns,
                variant_of=base.id,
                reason=entry["reason"],
            )
        )
    ids = [op.id for op in ops]
    if len(ids) != len(set(ids)):
        raise CorpusError(f"duplicate operation ids in {manifest}")
    return ops


def hash_mismatches(ops: list[Operation]) -> list[str]:
    """``"IC1: interactive_v2/… (missing|changed)"`` for every file that differs from its pin."""
    out = []
    for op in ops:
        if not op.path.is_file():
            out.append(f"{op.id}: {op.file} is missing")
        elif _sha256(op.path) != op.sha256:
            out.append(f"{op.id}: {op.file} changed (pinned {op.sha256[:12]}, now {_sha256(op.path)[:12]})")
    return out


def unlisted_files(ops: list[Operation]) -> list[str]:
    """Vendored ``.cypher`` files the manifest does not mention."""
    listed = {op.file for op in ops}
    return sorted(
        p.relative_to(QUERIES_DIR).as_posix()
        for p in QUERIES_DIR.rglob("*.cypher")
        if p.relative_to(QUERIES_DIR).as_posix() not in listed
    )


def rehash(manifest: Path = MANIFEST_PATH) -> int:
    """Rewrite each entry's ``sha256`` in place; returns how many changed."""
    text = manifest.read_text(encoding="utf-8")
    changed = 0
    for op in load(manifest):
        pattern = re.compile(rf"(file: {re.escape(op.file)}\n\s+sha256: )([0-9a-f]{{64}})")
        actual = _sha256(op.path)

        def _sub(m: re.Match[str], actual: str = actual) -> str:
            return m.group(1) + actual

        text, n = pattern.subn(_sub, text)
        if n != 1:
            raise CorpusError(f"{manifest}: could not locate the sha256 line for {op.file}")
        changed += op.sha256 != actual
    manifest.write_text(text, encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check or re-pin the vendored LDBC query hashes.")
    ap.add_argument("--rehash", action="store_true", help="re-pin every sha256 in the manifest")
    args = ap.parse_args(argv)
    if args.rehash:
        print(f"re-pinned {rehash()} file hash(es)")
        return 0
    ops = load()
    problems = hash_mismatches(ops) + [f"unlisted: {f}" for f in unlisted_files(ops)]
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
