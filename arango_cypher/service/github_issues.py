"""The slice of the GitHub REST API the error inbox uses (issues on one repo).

The token travels only in the ``Authorization`` header; it is never logged and
never part of an exception message. Every failure is a :class:`GitHubError`
naming the operation and the HTTP status.
"""

from __future__ import annotations

from typing import Any

import requests

DEFAULT_API = "https://api.github.com"
_TIMEOUT_S = 10.0
_PAGE_SIZE = 100
_MAX_PAGES = 50  # 5,000 issues: far past any inbox this service should have


class GitHubError(Exception):
    """A GitHub API call failed. Never contains the token."""


class GitHubIssues:
    """Issues of one repository (``owner/name``)."""

    def __init__(
        self, repo: str, token: str, *, api: str = DEFAULT_API, timeout_s: float = _TIMEOUT_S
    ) -> None:
        if not repo or "/" not in repo:
            raise ValueError(f"repository must be 'owner/name', not {repo!r}")
        if not token:
            raise ValueError("a GitHub token is required")
        self.repo = repo
        self._api = api.rstrip("/")
        self._timeout_s = timeout_s
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "arango-cypher-error-inbox",
            }
        )

    def _call(self, what: str, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._api}/repos/{self.repo}{path}"
        try:
            resp = self._session.request(method, url, timeout=self._timeout_s, **kwargs)
        except requests.RequestException as exc:
            raise GitHubError(f"{what}: no answer from GitHub ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            try:
                reason = resp.json().get("message", "")
            except ValueError:
                reason = ""
            raise GitHubError(f"{what}: GitHub answered HTTP {resp.status_code} {reason}".rstrip())
        return resp.json() if resp.content else None

    def list_issues(self, label: str) -> list[dict[str, Any]]:
        """Every issue (open and closed, not pull requests) carrying *label*."""
        issues: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self._call(
                "list issues",
                "GET",
                "/issues",
                params={"labels": label, "state": "all", "per_page": _PAGE_SIZE, "page": page},
            )
            issues.extend(i for i in batch if "pull_request" not in i)
            if len(batch) < _PAGE_SIZE:
                break
        return issues

    def get(self, number: int) -> dict[str, Any]:
        return self._call(f"read issue #{number}", "GET", f"/issues/{number}")

    def create(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return self._call(
            "create issue", "POST", "/issues", json={"title": title, "body": body, "labels": labels}
        )

    def update(self, number: int, *, body: str | None = None, state: str | None = None) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if body is not None:
            fields["body"] = body
        if state is not None:
            fields["state"] = state
        return self._call(f"update issue #{number}", "PATCH", f"/issues/{number}", json=fields)

    def comment(self, number: int, body: str) -> None:
        self._call(f"comment on issue #{number}", "POST", f"/issues/{number}/comments", json={"body": body})
