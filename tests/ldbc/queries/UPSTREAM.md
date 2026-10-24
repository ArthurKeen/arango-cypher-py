# Vendored LDBC SNB reference queries

| Directory | Upstream | Path | Commit |
| --- | --- | --- | --- |
| `interactive_v2/` | https://github.com/ldbc/ldbc_snb_interactive_v2_impls | `cypher/queries/*.cypher` (all 37) | `d05cc7620c295362ef7b0cfd32d5ac1dfde5feb3` |
| `bi/` | https://github.com/ldbc/ldbc_snb_bi | `neo4j/queries/bi-{1..20}.cypher` | `47dd38b40844ecdb0e42e5a610c369535304786d` |

The BI GDS helper scripts (`bi-19-create-graph.cypher` and similar) are not
vendored; BI15, BI19 and BI20 are out of subset (`../manifest.yml`).

## Rules

- Files in `interactive_v2/` and `bi/` are **never edited**. The manifest pins
  each one's SHA-256, and `tests/ldbc/test_ldbc_ratchet.py` fails on any
  change.
- An adaptation is a new file in `variants/` named
  `<upstream file stem>.<reason>.cypher`. Its header comment states what
  changed and why, and it gets a manifest entry under `variants`.

## Refreshing to a newer upstream commit

1. Clone both repositories at the new commit.
2. Copy the files over the ones here.
3. Update the commits in this file, in `NOTICE`, and in `../manifest.yml`.
4. Re-pin the hashes with `.venv/bin/python -m tests.ldbc.corpus --rehash`.
5. Re-record the ledger with `.venv/bin/python -m tests.ldbc.ledger --write`.
6. Review both diffs in the same PR.
