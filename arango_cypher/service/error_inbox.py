"""Automatic error reports to a private GitHub issue inbox (PRD §6.3).

When the service hits a bug of its own — AQL it generated that ArangoDB
rejects, or an unexpected exception — it files one issue per *kind* of error
in a private repository, and counts repeats in that issue (how often, how many
users, which versions and endpoints, first and last seen) instead of opening
new ones. Mistakes in a user's own query, missing permissions, and database or
network outages are not reported (:mod:`arango_cypher.service.error_handlers`
decides).

A kind of error is its *fingerprint*: the kind, the error code and the error's
summary with quoted text and numbers masked, so the same bug hit on different
collections or at a different position is one issue. Requests only enqueue;
a background thread files and updates issues (reading the inbox's existing
issues once, so a restart continues the same counts), at most
``max_new_per_hour`` new issues an hour. A closed issue whose error happens
again is reopened with a comment.

Configuration — without the first two the inbox does nothing:

- ``ARANGO_CYPHER_ISSUE_REPO``: ``owner/name`` of the private inbox repository;
- ``ARANGO_CYPHER_ISSUE_TOKEN``: a fine-grained token with Issues read/write on
  that repository only (never logged, never part of a report);
- ``ARANGO_CYPHER_ISSUE_FLUSH_S``: seconds between count updates (default 60);
- ``ARANGO_CYPHER_ISSUE_API``: the GitHub API base URL (tests).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .github_issues import DEFAULT_API, GitHubError, GitHubIssues

_log = logging.getLogger("arango_cypher.service.error_inbox")

REPO_ENV = "ARANGO_CYPHER_ISSUE_REPO"
TOKEN_ENV = "ARANGO_CYPHER_ISSUE_TOKEN"
FLUSH_ENV = "ARANGO_CYPHER_ISSUE_FLUSH_S"
API_ENV = "ARANGO_CYPHER_ISSUE_API"

INBOX_LABEL = "auto-report"
KIND_LABELS = {"translator": "translator", "crash": "crash", "server_error": "crash"}
KIND_TITLES = {
    "translator": "Generated AQL rejected",
    "crash": "Unexpected error",
    "server_error": "Server error",
}

_DEFAULT_FLUSH_S = 60.0
_MAX_NEW_PER_HOUR = 20
_MAX_USERS = 100  # hashed users kept per issue; the count stops growing there
_MAX_LIST = 20  # versions / endpoints kept per issue
_MAX_SECTION = 8000  # characters of Cypher, AQL or traceback in the example
_TITLE_LEN = 120

_MARK = "arango-cypher-inbox"
_FP_RE = re.compile(rf"<!-- {_MARK} fingerprint=([0-9a-f]+) -->")
_STATE_RE = re.compile(rf"<!-- {_MARK} state=(\{{.*?\}}) -->", re.S)
_COUNTS_RE = re.compile(r"<!-- counts:begin -->.*?<!-- counts:end -->", re.S)

# A quoted fragment may span lines: ArangoDB quotes the query text near a
# syntax error, newlines included ("near '-users\nFOR u IN …'").
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
_HEX_RE = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
_NUM_RE = re.compile(r"\d+")


def normalize(summary: str) -> str:
    """*summary* with what varies between occurrences of one bug masked:
    quoted text (names, query fragments), hex digests, numbers."""
    text = _QUOTED_RE.sub("'…'", summary)
    text = _HEX_RE.sub("H", text)
    text = _NUM_RE.sub("N", text)
    return " ".join(text.split())[:300]


def fingerprint(kind: str, code: str, summary: str) -> str:
    raw = f"{kind}|{code}|{normalize(summary)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def hash_user(user: str) -> str:
    return hashlib.sha256(f"{_MARK}:{user}".encode()).hexdigest()[:12]


def _now_iso(clock: float) -> str:
    return datetime.fromtimestamp(clock, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


@dataclass(frozen=True)
class Occurrence:
    """One time the service hit a reportable error.

    *summary* identifies the bug (it is fingerprinted); *message* is what the
    user was shown; *details* is example context for the issue (``cypher``,
    ``aql``, ``database``, ``traceback``, ...). *user* is a stable identity
    (the platform user), stored only hashed.
    """

    kind: str
    code: str
    summary: str
    message: str
    endpoint: str
    version: str
    user: str | None = None
    details: dict[str, str] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.kind, self.code, self.summary)


@dataclass
class Counts:
    seen: int = 0
    users: list[str] = field(default_factory=list)
    first: str | None = None
    last: str | None = None
    versions: list[str] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)

    def add(self, occ: Occurrence, when: str) -> None:
        self.seen += 1
        if occ.user:
            h = hash_user(occ.user)
            if h not in self.users and len(self.users) < _MAX_USERS:
                self.users.append(h)
        self.first = self.first or when
        self.last = when
        for items, value in ((self.versions, occ.version), (self.endpoints, occ.endpoint)):
            if value and value not in items and len(items) < _MAX_LIST:
                items.append(value)

    def merge(self, other: Counts) -> None:
        self.seen += other.seen
        for h in other.users:
            if h not in self.users and len(self.users) < _MAX_USERS:
                self.users.append(h)
        self.first = min(filter(None, (self.first, other.first)), default=None)
        self.last = max(filter(None, (self.last, other.last)), default=None)
        for mine, theirs in ((self.versions, other.versions), (self.endpoints, other.endpoints)):
            for value in theirs:
                if value not in mine and len(mine) < _MAX_LIST:
                    mine.append(value)

    def to_json(self) -> str:
        data = {
            "seen": self.seen,
            "users": self.users,
            "first": self.first,
            "last": self.last,
            "versions": self.versions,
            "endpoints": self.endpoints,
        }
        # ">" only occurs inside JSON strings, where > is the same text;
        # it keeps "-->" from ending the HTML comment early.
        return json.dumps(data, sort_keys=True).replace(">", "\\u003e")

    @classmethod
    def from_json(cls, text: str) -> Counts:
        data = json.loads(text)
        return cls(
            seen=int(data.get("seen", 0)),
            users=[str(u) for u in data.get("users", [])],
            first=data.get("first"),
            last=data.get("last"),
            versions=[str(v) for v in data.get("versions", [])],
            endpoints=[str(e) for e in data.get("endpoints", [])],
        )


def _fenced(language: str, text: str) -> str:
    text = text.strip()
    if len(text) > _MAX_SECTION:
        text = text[:_MAX_SECTION] + "\n… (cut)"
    longest = max([2, *(len(m) for m in re.findall(r"`+", text))])
    fence = "`" * (longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def render_counts(counts: Counts) -> str:
    def joined(items: list[str]) -> str:
        return ", ".join(f"`{i}`" for i in items) or "—"

    users = f"{len(counts.users)}" + ("+" if len(counts.users) >= _MAX_USERS else "")
    return (
        "<!-- counts:begin -->\n"
        "| Seen | Users | First seen | Last seen | Versions | Endpoints |\n"
        "|---:|---:|---|---|---|---|\n"
        f"| {counts.seen} | {users} | {counts.first or '—'} | {counts.last or '—'} "
        f"| {joined(counts.versions)} | {joined(counts.endpoints)} |\n"
        f"<!-- {_MARK} state={counts.to_json()} -->\n"
        "<!-- counts:end -->"
    )


def render_title(occ: Occurrence) -> str:
    title = f"[auto] {KIND_TITLES.get(occ.kind, occ.kind)}: {normalize(occ.summary)}"
    return title if len(title) <= _TITLE_LEN else title[: _TITLE_LEN - 1] + "…"


def render_body(occ: Occurrence, counts: Counts) -> str:
    sections = [
        f"<!-- {_MARK} fingerprint={occ.fingerprint} -->",
        "Filed automatically by a deployed Arango Cypher. Repeats update the counts below; "
        "they do not open new issues. Close this issue once the fix is deployed: if the error "
        "happens again, the issue is reopened with a comment.",
        "## Counts\n\n" + render_counts(counts),
        "## What the user saw\n\n" + _fenced("text", occ.message),
        "## First occurrence\n\n"
        + "\n".join(
            f"- **{label}:** {value}"
            for label, value in (
                ("Endpoint", f"`{occ.endpoint}`"),
                ("Error code", f"`{occ.code}`"),
                ("Version", f"`{occ.version}`"),
                ("Database", f"`{occ.details['database']}`" if occ.details.get("database") else None),
                ("Deployment", f"`{occ.details['deployment']}`" if occ.details.get("deployment") else None),
            )
            if value
        ),
    ]
    for key, heading, language in (
        ("cypher", "Cypher", "cypher"),
        ("aql", "Generated AQL", "aql"),
        ("collections", "Collections bound", "json"),
        ("traceback", "Traceback", "text"),
    ):
        if occ.details.get(key):
            sections.append(f"### {heading}\n\n" + _fenced(language, occ.details[key]))
    return "\n\n".join(sections) + "\n"


def parse_body(body: str | None) -> tuple[str | None, Counts | None]:
    """The fingerprint and counts an inbox issue body records."""
    if not body:
        return None, None
    fp = _FP_RE.search(body)
    state = _STATE_RE.search(body)
    counts: Counts | None = None
    if state:
        try:
            counts = Counts.from_json(state.group(1))
        except (ValueError, TypeError) as exc:
            _log.warning("unreadable counts in an inbox issue (%s); starting them again", exc)
    return (fp.group(1) if fp else None), counts


def replace_counts(body: str, counts: Counts) -> str:
    """*body* with its counts block rewritten; everything else is kept, so a
    person's edits to the issue survive."""
    block = render_counts(counts)
    if _COUNTS_RE.search(body):
        return _COUNTS_RE.sub(lambda _m: block, body, count=1)
    return body.rstrip() + "\n\n## Counts\n\n" + block + "\n"


@dataclass
class _Issue:
    number: int
    state: str
    body: str
    counts: Counts


@dataclass
class _Pending:
    example: Occurrence
    counts: Counts


class ErrorInbox:
    """Files and counts error reports in one GitHub repository."""

    def __init__(
        self,
        client: GitHubIssues,
        *,
        flush_s: float = _DEFAULT_FLUSH_S,
        max_new_per_hour: int = _MAX_NEW_PER_HOUR,
        clock: Any = time.time,
    ) -> None:
        self._client = client
        self._flush_s = flush_s
        self._max_new_per_hour = max_new_per_hour
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._issues: dict[str, _Issue] = {}
        self._loaded = False
        self._created: deque[float] = deque()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    @property
    def repo(self) -> str:
        return self._client.repo

    def report(self, occ: Occurrence) -> dict[str, Any]:
        """Count *occ* and return what the user is told: a reference, how
        often this error has been seen, and the inbox issue when known."""
        fp = occ.fingerprint
        with self._lock:
            pending = self._pending.get(fp)
            if pending is None:
                pending = self._pending[fp] = _Pending(example=occ, counts=Counts())
            pending.counts.add(occ, _now_iso(self._clock()))
            issue = self._issues.get(fp)
            seen = pending.counts.seen + (issue.counts.seen if issue else 0)
            new = issue is None
        if new:
            self._wake.set()  # file a new kind of error promptly
        return {
            "reference": fp[:8],
            "seen": seen,
            "issue": issue.number if issue else None,
            "status": "filed" if issue else "pending",
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "repository": self.repo,
                "issues_known": len(self._issues),
                "pending": len(self._pending),
                "last_error": self.last_error,
            }

    # --- syncing with GitHub ----------------------------------------------------

    def _load(self) -> None:
        issues: dict[str, _Issue] = {}
        for raw in self._client.list_issues(INBOX_LABEL):
            fp, counts = parse_body(raw.get("body"))
            if fp and fp not in issues:
                issues[fp] = _Issue(
                    number=int(raw["number"]),
                    state=raw.get("state", "open"),
                    body=raw.get("body") or "",
                    counts=counts or Counts(),
                )
        with self._lock:
            # Counts reported while loading stay pending and merge on flush.
            self._issues.update(issues)
            self._loaded = True

    def _may_create(self) -> bool:
        now = self._clock()
        while self._created and now - self._created[0] > 3600:
            self._created.popleft()
        return len(self._created) < self._max_new_per_hour

    def flush(self) -> None:
        """File new kinds of error and update the counts of known ones."""
        try:
            if not self._loaded:
                self._load()
        except GitHubError as exc:
            self._fail(exc)
            return
        with self._lock:
            batch, self._pending = self._pending, {}
        for fp, pending in batch.items():
            try:
                self._sync_one(fp, pending)
            except GitHubError as exc:
                self._fail(exc)
                with self._lock:  # keep the counts for the next flush
                    again = self._pending.setdefault(fp, _Pending(example=pending.example, counts=Counts()))
                    again.counts.merge(pending.counts)

    def _sync_one(self, fp: str, pending: _Pending) -> None:
        with self._lock:
            issue = self._issues.get(fp)
        if issue is None:
            if not self._may_create():
                _log.warning("error inbox: new-issue limit reached; holding %s for later", fp[:8])
                with self._lock:
                    again = self._pending.setdefault(fp, _Pending(example=pending.example, counts=Counts()))
                    again.counts.merge(pending.counts)
                return
            occ = pending.example
            labels = [INBOX_LABEL, KIND_LABELS.get(occ.kind, "crash")]
            created = self._client.create(render_title(occ), render_body(occ, pending.counts), labels)
            self._created.append(self._clock())
            with self._lock:
                self._issues[fp] = _Issue(
                    number=int(created["number"]),
                    state="open",
                    body=created.get("body") or "",
                    counts=pending.counts,
                )
            _log.info("error inbox: filed #%s for %s", created["number"], fp[:8])
            return
        # Read the issue as it is now: a person may have edited or closed it
        # since it was loaded, and their edits must survive the update.
        current = self._client.get(issue.number)
        current_body = current.get("body") or issue.body
        _, counts_now = parse_body(current_body)
        counts = counts_now or Counts.from_json(issue.counts.to_json())
        counts.merge(pending.counts)
        body = replace_counts(current_body, counts)
        reopen = current.get("state") == "closed"
        self._client.update(issue.number, body=body, state="open" if reopen else None)
        if reopen:
            self._client.comment(
                issue.number,
                f"Seen again {pending.counts.seen} time(s) after this issue was closed "
                f"(versions: {', '.join(pending.counts.versions) or 'unknown'}). Reopened automatically.",
            )
        with self._lock:
            self._issues[fp] = _Issue(number=issue.number, state="open", body=body, counts=counts)

    def _fail(self, exc: GitHubError) -> None:
        self.last_error = str(exc)
        _log.warning("error inbox: %s", exc)

    # --- background thread ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="error-inbox", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._flush_s)
            self._wake.clear()
            try:
                self.flush()
            except Exception:  # noqa: BLE001 — the inbox must never take the service down
                _log.exception("error inbox: flush failed")


_inbox: ErrorInbox | None = None
_inbox_checked = False
_inbox_lock = threading.Lock()


def get_inbox() -> ErrorInbox | None:
    """The process's inbox, started on first use; ``None`` when it is not
    configured (``ARANGO_CYPHER_ISSUE_REPO`` / ``ARANGO_CYPHER_ISSUE_TOKEN``)."""
    global _inbox, _inbox_checked
    with _inbox_lock:
        if _inbox_checked:
            return _inbox
        _inbox_checked = True
        repo = os.environ.get(REPO_ENV, "").strip()
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not repo or not token:
            return None
        try:
            flush_s = float(os.environ.get(FLUSH_ENV, "") or _DEFAULT_FLUSH_S)
        except ValueError:
            _log.warning("%s is not a number; using %ss", FLUSH_ENV, _DEFAULT_FLUSH_S)
            flush_s = _DEFAULT_FLUSH_S
        try:
            client = GitHubIssues(repo, token, api=os.environ.get(API_ENV, "").strip() or DEFAULT_API)
        except ValueError as exc:
            _log.warning("error inbox disabled: %s", exc)
            return None
        _inbox = ErrorInbox(client, flush_s=flush_s)
        _inbox.start()
        _log.info("error inbox: reporting to %s", repo)
        return _inbox


def reset_inbox() -> None:
    """Stop and forget the process's inbox (tests; a configuration change)."""
    global _inbox, _inbox_checked
    with _inbox_lock:
        if _inbox is not None:
            _inbox.stop()
        _inbox = None
        _inbox_checked = False
