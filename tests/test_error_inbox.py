"""The error inbox (``arango_cypher/service/error_inbox.py``) against a fake
GitHub issues API over real HTTP (``tests/helpers/fake_github.py``).

One issue per kind of error; repeats update its counts; a restart continues
the same issue; a closed issue is reopened when its error comes back; a
person's edits to an issue survive; GitHub failures lose no counts; new issues
are rate-limited; the token never appears anywhere it writes.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from arango_cypher.service import error_inbox as ei
from arango_cypher.service.github_issues import GitHubError, GitHubIssues
from tests.helpers.fake_github import REPO, TOKEN, FakeGitHub

HYPHEN_A = "ArangoDB error 1501: syntax error, unexpected - operator near '-AWS-DOCS-DEMO_Chunks, IAM-AWS-D...' at position 1:477"
HYPHEN_B = "ArangoDB error 1501: syntax error, unexpected - operator near '-users, roles' at position 1:12"


def occurrence(
    summary: str = HYPHEN_A, *, user: str | None = "alice", version: str = "0.2.0", kind: str = "translator"
) -> ei.Occurrence:
    return ei.Occurrence(
        kind=kind,
        code="GENERATED_AQL_REJECTED",
        summary=summary,
        message=f"Arango Cypher translated this query into AQL that ArangoDB rejected: {summary}",
        endpoint="/execute",
        version=version,
        user=user,
        details={
            "cypher": "MATCH (a:User)-[:CAN_ASSUME]->(b:Role) RETURN a, b",
            "aql": "WITH IAM-x\nFOR a IN @@u",
            "database": "IAM",
        },
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1_760_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def github() -> Iterator[FakeGitHub]:
    fake = FakeGitHub()
    yield fake
    fake.close()


def make_inbox(github: FakeGitHub, **kwargs) -> ei.ErrorInbox:
    return ei.ErrorInbox(GitHubIssues(REPO, TOKEN, api=github.api), **kwargs)


class TestFingerprint:
    def test_one_bug_on_different_collections_is_one_fingerprint(self) -> None:
        assert ei.fingerprint("translator", "C", HYPHEN_A) == ei.fingerprint("translator", "C", HYPHEN_B)

    def test_different_errors_differ(self) -> None:
        other = "ArangoDB error 1579: access after data-modification by traversal"
        assert ei.fingerprint("translator", "C", HYPHEN_A) != ei.fingerprint("translator", "C", other)
        assert ei.fingerprint("translator", "C", HYPHEN_A) != ei.fingerprint("crash", "C", HYPHEN_A)

    def test_a_quoted_fragment_spanning_lines_is_masked_whole(self) -> None:
        # What ArangoDB 3.11 actually says when the WITH line is short.
        a = "ArangoDB error 1501: syntax error, unexpected - operator near '-users\nFOR u IN users RETURN u' at position 1:9"
        b = "ArangoDB error 1501: syntax error, unexpected - operator near '-AWS-DOCS-DEMO_Chunks\nFOR u IN u...' at position 1:9"
        assert ei.fingerprint("translator", "C", a) == ei.fingerprint("translator", "C", b)

    def test_normalize_masks_what_varies(self) -> None:
        assert (
            ei.normalize(HYPHEN_A)
            == "ArangoDB error N: syntax error, unexpected - operator near '…' at position N:N"
        )
        assert ei.normalize('digest 9f86d081884c7d65 and "quoted"') == "digest H and '…'"


class TestBody:
    def test_counts_survive_a_round_trip_and_a_comment_breaker(self) -> None:
        counts = ei.Counts()
        occ = occurrence()
        counts.add(occ, "2026-10-14 01:00 UTC")
        counts.endpoints.append("/evil-->")
        body = ei.render_body(occ, counts)
        fp, parsed = ei.parse_body(body)
        assert fp == occ.fingerprint and parsed == counts

    def test_replacing_counts_keeps_a_persons_edits(self) -> None:
        counts = ei.Counts()
        counts.add(occurrence(), "t1")
        body = ei.render_body(occurrence(), counts) + "\nTriage: caused by the WITH prelude.\n"
        counts.add(occurrence(user="bob"), "t2")
        updated = ei.replace_counts(body, counts)
        assert "Triage: caused by the WITH prelude." in updated
        assert ei.parse_body(updated)[1].seen == 2


class TestInbox:
    def test_a_new_error_is_filed_once_with_its_example(self, github: FakeGitHub) -> None:
        inbox = make_inbox(github)
        first = inbox.report(occurrence())
        assert first == {
            "reference": occurrence().fingerprint[:8],
            "seen": 1,
            "issue": None,
            "status": "pending",
        }
        inbox.flush()
        assert list(github.issues) == [1]
        issue = github.issues[1]
        assert {lb["name"] for lb in issue["labels"]} == {"auto-report", "translator"}
        assert issue["title"].startswith("[auto] Generated AQL rejected: ArangoDB error N: syntax error")
        assert "MATCH (a:User)-[:CAN_ASSUME]->(b:Role)" in issue["body"] and "`IAM`" in issue["body"]
        later = inbox.report(occurrence(HYPHEN_B))
        assert later["issue"] == 1 and later["status"] == "filed" and later["seen"] == 2

    def test_repeats_count_in_the_same_issue(self, github: FakeGitHub) -> None:
        inbox = make_inbox(github)
        inbox.report(occurrence())
        inbox.flush()
        for user in ("alice", "bob", "bob", None):
            inbox.report(occurrence(HYPHEN_B, user=user, version="0.2.1"))
        inbox.flush()
        assert list(github.issues) == [1]
        _, counts = ei.parse_body(github.issues[1]["body"])
        assert counts.seen == 5 and len(counts.users) == 2
        assert counts.versions == ["0.2.0", "0.2.1"]
        assert "| 5 | 2 |" in github.issues[1]["body"]

    def test_a_restart_continues_the_same_issue(self, github: FakeGitHub) -> None:
        first = make_inbox(github)
        first.report(occurrence())
        first.flush()
        restarted = make_inbox(github)
        restarted.report(occurrence(HYPHEN_B))
        restarted.flush()
        assert list(github.issues) == [1]
        assert ei.parse_body(github.issues[1]["body"])[1].seen == 2
        assert restarted.report(occurrence())["seen"] == 3

    def test_a_closed_issue_is_reopened_when_the_error_returns(self, github: FakeGitHub) -> None:
        inbox = make_inbox(github)
        inbox.report(occurrence())
        inbox.flush()
        github.issues[1]["state"] = "closed"  # someone closed it after a fix
        inbox.report(occurrence(version="0.2.1"))
        inbox.flush()
        assert github.issues[1]["state"] == "open"
        assert github.comments == [
            (1, "Seen again 1 time(s) after this issue was closed (versions: 0.2.1). Reopened automatically.")
        ]

    def test_a_persons_edit_to_the_issue_survives_an_update(self, github: FakeGitHub) -> None:
        inbox = make_inbox(github)
        inbox.report(occurrence())
        inbox.flush()
        github.issues[1]["body"] += "\nTriage: fixed by #17.\n"
        inbox.report(occurrence())
        inbox.flush()
        assert "Triage: fixed by #17." in github.issues[1]["body"]
        assert ei.parse_body(github.issues[1]["body"])[1].seen == 2

    def test_a_github_failure_loses_no_counts(self, github: FakeGitHub) -> None:
        inbox = make_inbox(github)
        inbox.report(occurrence())
        github.fail_next = 502
        inbox.flush()  # loading the inbox fails
        assert github.issues == {} and "HTTP 502" in (inbox.last_error or "")
        inbox.report(occurrence())
        inbox.flush()
        assert ei.parse_body(github.issues[1]["body"])[1].seen == 2

    def test_new_issues_are_rate_limited(self, github: FakeGitHub) -> None:
        clock = Clock()
        inbox = make_inbox(github, max_new_per_hour=1, clock=clock)
        inbox.report(occurrence())
        inbox.report(occurrence("TypeError in service.routes.cypher.execute_endpoint", kind="crash"))
        inbox.flush()
        assert len(github.issues) == 1
        clock.now += 3601
        inbox.flush()
        assert len(github.issues) == 2

    def test_the_token_is_never_written_or_raised(self, github: FakeGitHub) -> None:
        inbox = make_inbox(github)
        inbox.report(occurrence())
        inbox.flush()
        github.fail_next = 500
        with pytest.raises(GitHubError) as err:
            GitHubIssues(REPO, TOKEN, api=github.api).list_issues("auto-report")
        assert TOKEN not in str(err.value)
        assert TOKEN not in github.everything_written()

    def test_a_wrong_token_is_reported_not_raised(self, github: FakeGitHub) -> None:
        inbox = ei.ErrorInbox(GitHubIssues(REPO, "wrong", api=github.api))
        inbox.report(occurrence())
        inbox.flush()
        assert github.issues == {} and "HTTP 401 Bad credentials" in (inbox.last_error or "")
        assert inbox.status()["pending"] == 1


class TestConfiguration:
    def test_without_a_repository_and_token_there_is_no_inbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ei.REPO_ENV, raising=False)
        monkeypatch.delenv(ei.TOKEN_ENV, raising=False)
        ei.reset_inbox()
        try:
            assert ei.get_inbox() is None
        finally:
            ei.reset_inbox()

    def test_with_both_the_inbox_starts(self, monkeypatch: pytest.MonkeyPatch, github: FakeGitHub) -> None:
        monkeypatch.setenv(ei.REPO_ENV, REPO)
        monkeypatch.setenv(ei.TOKEN_ENV, TOKEN)
        monkeypatch.setenv(ei.API_ENV, github.api)
        monkeypatch.setenv(ei.FLUSH_ENV, "3600")
        ei.reset_inbox()
        try:
            inbox = ei.get_inbox()
            assert inbox is not None and inbox.repo == REPO and ei.get_inbox() is inbox
        finally:
            ei.reset_inbox()
