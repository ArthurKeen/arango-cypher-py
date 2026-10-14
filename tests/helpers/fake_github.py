"""A fake GitHub issues API on localhost, for the error inbox tests.

Implements what ``arango_cypher/service/github_issues.py`` calls, with
GitHub's shapes: list issues by label (``state=all``, ``per_page``/``page``),
read, create and update one, and comment. Requests without the expected
``Authorization: Bearer <token>`` get GitHub's 401. ``fail_next`` makes the
next request answer with that status.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

TOKEN = "ghp_fake_inbox_token_0123456789"
REPO = "acme/inbox"


class FakeGitHub:
    def __init__(self, repo: str = REPO, token: str = TOKEN) -> None:
        self.repo = repo
        self.token = token
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: list[tuple[int, str]] = []
        self.requests: list[tuple[str, str]] = []
        self.fail_next: int | None = None
        self._lock = threading.Lock()
        fake = self
        base = f"/repos/{repo}"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def _guard(self) -> bool:
                fake.requests.append((self.command, self.path))
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._send(401, {"message": "Bad credentials"})
                    return False
                if fake.fail_next is not None:
                    status, fake.fail_next = fake.fail_next, None
                    self._send(status, {"message": "Server Error"})
                    return False
                return True

            def do_GET(self) -> None:
                if not self._guard():
                    return
                url = urlparse(self.path)
                if url.path == f"{base}/issues":
                    q = parse_qs(url.query)
                    label = q.get("labels", [""])[0]
                    page, size = int(q.get("page", ["1"])[0]), int(q.get("per_page", ["30"])[0])
                    with fake._lock:
                        found = [
                            i
                            for _, i in sorted(fake.issues.items())
                            if label in {lb["name"] for lb in i["labels"]}
                        ]
                    self._send(200, found[(page - 1) * size : page * size])
                    return
                m = re.fullmatch(rf"{re.escape(base)}/issues/(\d+)", url.path)
                if m and int(m.group(1)) in fake.issues:
                    self._send(200, fake.issues[int(m.group(1))])
                    return
                self._send(404, {"message": "Not Found"})

            def do_POST(self) -> None:
                if not self._guard():
                    return
                body = self._json()
                if self.path == f"{base}/issues":
                    with fake._lock:
                        number = len(fake.issues) + 1
                        issue = {
                            "number": number,
                            "title": body["title"],
                            "body": body.get("body", ""),
                            "labels": [{"name": n} for n in body.get("labels", [])],
                            "state": "open",
                        }
                        fake.issues[number] = issue
                    self._send(201, issue)
                    return
                m = re.fullmatch(rf"{re.escape(base)}/issues/(\d+)/comments", self.path)
                if m and int(m.group(1)) in fake.issues:
                    fake.comments.append((int(m.group(1)), body["body"]))
                    self._send(201, {"id": len(fake.comments), "body": body["body"]})
                    return
                self._send(404, {"message": "Not Found"})

            def do_PATCH(self) -> None:
                if not self._guard():
                    return
                m = re.fullmatch(rf"{re.escape(base)}/issues/(\d+)", self.path)
                if not m or int(m.group(1)) not in fake.issues:
                    self._send(404, {"message": "Not Found"})
                    return
                with fake._lock:
                    issue = fake.issues[int(m.group(1))]
                    issue.update({k: v for k, v in self._json().items() if k in ("body", "state", "title")})
                self._send(200, issue)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.api = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def everything_written(self) -> str:
        """All text the inbox put on GitHub (titles, bodies, comments)."""
        parts = [i["title"] + "\n" + i["body"] for i in self.issues.values()]
        parts += [c for _, c in self.comments]
        return "\n".join(parts)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
