"""Exception handlers that report the service's own bugs to the error inbox.

What is reported (PRD §6.3), each as an :class:`~.error_inbox.Occurrence`:

- **translator** — generated AQL that ArangoDB rejected (the route marks the
  ``HTTPException`` with ``inbox_report``: the query, the AQL, the collections);
- **server_error** — any other 500 whose cause is not the database, the
  network or the user (an exception wrapped by ``_translate_errors``);
- **crash** — an exception nothing handled.

Not reported: 4xx (the user's query, permissions, tenant refusals), and 5xx
caused by ArangoDB or the network — those are outages or data, not bugs here.
A reported error's JSON carries ``report`` (reference, how often seen, issue)
next to ``detail``, which the Workbench turns into "reported automatically".
Without an inbox configured these handlers change nothing but the shape of an
unhandled 500 (JSON with a message instead of plain "Internal Server Error").
"""

from __future__ import annotations

import os
import traceback
from typing import Any

import requests
from arango.exceptions import ArangoError
from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .app import _svc_logger, app
from .error_inbox import Occurrence, get_inbox
from .security import _sanitize_error, _sessions

#: Causes of a 500 that are not bugs in this service.
_NOT_OUR_BUG = (ArangoError, requests.RequestException, ConnectionError, TimeoutError)
_MAX_TRACEBACK = 8000


def _session_of(request: Request) -> Any | None:
    token = request.headers.get("X-Arango-Session", "")
    if not token:
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
    return _sessions.get(token) if token else None


def _endpoint(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def _message(detail: Any) -> str:
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("error") or detail)
    return str(detail)


def _crash_summary(exc: BaseException) -> tuple[str, str]:
    """``"<Type> in <module.function>"`` for the innermost frame in this
    package (line numbers left out: they move between versions), and the
    traceback."""
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if "/arango_cypher/" in f.filename.replace("\\", "/")]
    frame = (ours or frames)[-1] if frames else None
    if frame is None:
        where = "unknown"
    else:
        path = frame.filename.replace("\\", "/")
        module = path.rsplit("/arango_cypher/", 1)[-1].removesuffix(".py").replace("/", ".")
        where = f"{module}.{frame.name}"
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return f"{type(exc).__name__} in {where}", text[-_MAX_TRACEBACK:]


def _report(
    request: Request, *, kind: str, code: str, summary: str, message: str, details: dict[str, str]
) -> Any:
    inbox = get_inbox()
    if inbox is None:
        return None
    session = _session_of(request)
    info = {k: _sanitize_error(v) for k, v in details.items() if v}
    if session is not None:
        try:
            info.setdefault("database", str(session.db.name))
        except Exception as exc:  # noqa: BLE001 — a report must never fail the response
            _svc_logger.debug("error inbox: no database name for the session (%s)", exc)
    if os.environ.get("ROOT_PATH"):
        info.setdefault("deployment", os.environ["ROOT_PATH"])
    occ = Occurrence(
        kind=kind,
        code=code,
        summary=_sanitize_error(summary),
        message=_sanitize_error(message),
        endpoint=_endpoint(request),
        version=str(app.version),
        user=getattr(session, "platform_user", None),
        details=info,
    )
    try:
        return inbox.report(occ)
    except Exception:  # noqa: BLE001 — reporting is best-effort; the user still gets the error
        _svc_logger.exception("error inbox: could not record a report")
        return None


def _report_http(request: Request, exc: StarletteHTTPException) -> Any:
    if exc.status_code != 500:
        return None
    hint = getattr(exc, "inbox_report", None)
    if isinstance(hint, dict):
        return _report(
            request,
            kind=str(hint.get("kind", "server_error")),
            code=str(hint.get("code", "SERVER_ERROR")),
            summary=str(hint.get("summary", "")),
            message=_message(exc.detail),
            details=dict(hint.get("details") or {}),
        )
    cause = exc.__cause__
    if isinstance(cause, _NOT_OUR_BUG):
        return None
    code = str(exc.detail.get("code", "SERVER_ERROR")) if isinstance(exc.detail, dict) else "SERVER_ERROR"
    if cause is not None:
        summary, trace = _crash_summary(cause)
        return _report(
            request,
            kind="server_error",
            code=code,
            summary=summary,
            message=_message(exc.detail),
            details={"traceback": trace},
        )
    return _report(
        request,
        kind="server_error",
        code=code,
        summary=_message(exc.detail),
        message=_message(exc.detail),
        details={},
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_with_report(request: Request, exc: StarletteHTTPException):
    report = _report_http(request, exc)
    if report is None:
        return await http_exception_handler(request, exc)
    return JSONResponse(
        {"detail": exc.detail, "report": report},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception):
    summary, trace = _crash_summary(exc)
    message = (
        f"Arango Cypher hit an unexpected error ({type(exc).__name__}). "
        "This is a bug in Arango Cypher, not in your query."
    )
    report = _report(
        request,
        kind="crash",
        code="INTERNAL_ERROR",
        summary=summary,
        message=message,
        details={"traceback": trace},
    )
    body: dict[str, Any] = {
        "detail": {"error": "internal_error", "code": "INTERNAL_ERROR", "message": message}
    }
    if report is not None:
        body["report"] = report
    return JSONResponse(body, status_code=500)
