"""Platform sessions — connect as the user the ArangoDB platform already
signed in, instead of asking for a URL, username and password.

Deployed on the Arango Platform (Container Manager / BYOC), every request to
the service passes through the platform gateway, which authenticates the
browser's platform login and forwards the caller's platform JWT as
``Authorization: Bearer <jwt>``. The operator also injects the coordinator's
in-cluster address as ``ARANGO_DEPLOYMENT_ENDPOINT``. Together they let the
Workbench open a session as the logged-in user with no stored credential and
no login form — the user only picks a database and a graph.

This module never decides who the caller is: the coordinator validates the
JWT on the session's first call, so a forged or expired token is refused by
ArangoDB itself, and the session can see exactly what the user's platform
permissions allow.

``ARANGO_CYPHER_PLATFORM_AUTH=off`` disables the path entirely (the manual
connect dialog still works).
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING
from urllib.parse import unquote

from fastapi import Request

if TYPE_CHECKING:
    from arango import ArangoClient
    from arango.database import StandardDatabase

#: ``off`` / ``0`` / ``false`` / ``no`` disable platform sessions.
PLATFORM_AUTH_ENV = "ARANGO_CYPHER_PLATFORM_AUTH"
#: The coordinator address the platform operator injects into the container.
DEPLOYMENT_ENDPOINT_ENV = "ARANGO_DEPLOYMENT_ENDPOINT"

_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})

#: ``/_service/uds/_db/<db>/<instance>`` — the database a BYOC instance is
#: scoped to. ``/_service/uds/_global/<instance>`` has none.
_MOUNT_DB_RE = re.compile(r"^/_service/uds/_db/([^/]+)(?:/|$)")


def platform_auth_enabled() -> bool:
    return os.getenv(PLATFORM_AUTH_ENV, "auto").strip().lower() not in _DISABLED_VALUES


def platform_endpoint() -> str | None:
    """The coordinator URL a platform session connects to, or ``None``.

    ``ARANGO_DEPLOYMENT_ENDPOINT`` (operator-injected, in-cluster) wins;
    ``ARANGO_URL`` is the fallback for a deployment that configures the
    coordinator explicitly. Always server configuration — never taken from
    the request, so a caller cannot point the forwarded JWT at another host.
    """
    if not platform_auth_enabled():
        return None
    for name in (DEPLOYMENT_ENDPOINT_ENV, "ARANGO_URL"):
        value = os.getenv(name, "").strip()
        if value:
            return value.rstrip("/")
    return None


def forwarded_token(request: Request) -> str | None:
    """The platform JWT the gateway forwarded, or ``None``."""
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() != "bearer ":
        return None
    token = auth[7:].strip()
    return token or None


def mount_database(root_path: str) -> str | None:
    """The database a ``/_service/uds/_db/<db>/…`` mount is scoped to."""
    match = _MOUNT_DB_RE.match(root_path or "")
    return unquote(match.group(1)) if match else None


def default_database() -> str:
    """The database a platform session opens when the caller names none.

    The instance's own mount database first — the app was deployed *into*
    it — then ``ARANGO_DB``, then ``_system``.
    """
    return mount_database(os.getenv("ROOT_PATH", "")) or os.getenv("ARANGO_DB", "").strip() or "_system"


def open_platform_database(client: ArangoClient, name: str, token: str) -> StandardDatabase:
    """A database handle that authenticates every call with the user's JWT."""
    return client.db(name, auth_method="jwt", user_token=token)
