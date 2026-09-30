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

TLS to the operator endpoint: it serves a certificate from the cluster's own
CA, which the container does not trust by default, so a verifying client fails
with a bare "Can't connect to host(s)" (seen on prod.demo). See
:func:`platform_tls_verify` for the policy and its overrides.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

import jwt
import requests
from arango.exceptions import JWTExpiredError
from fastapi import Request

if TYPE_CHECKING:
    from arango import ArangoClient
    from arango.database import StandardDatabase

#: ``off`` / ``0`` / ``false`` / ``no`` disable platform sessions.
PLATFORM_AUTH_ENV = "ARANGO_CYPHER_PLATFORM_AUTH"
#: The coordinator address the platform operator injects into the container.
DEPLOYMENT_ENDPOINT_ENV = "ARANGO_DEPLOYMENT_ENDPOINT"

#: A CA bundle (PEM path) that signs the platform endpoint's certificate.
PLATFORM_CA_BUNDLE_ENV = "ARANGO_CYPHER_PLATFORM_CA_BUNDLE"
#: ``on`` / ``off`` override the ``auto`` TLS policy.
PLATFORM_VERIFY_TLS_ENV = "ARANGO_CYPHER_PLATFORM_VERIFY_TLS"

_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})
_ENABLED_VALUES = frozenset({"on", "1", "true", "yes"})

#: Seconds for the diagnostic probe that runs only after a connect failed.
_PROBE_TIMEOUT_S = 5.0

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


def platform_tls_verify() -> bool | str:
    """TLS verification for the platform endpoint, as python-arango's
    ``verify_override`` takes it: a CA bundle path, ``True`` or ``False``.

    A configured CA bundle always wins. Otherwise ``auto`` verifies an
    explicitly configured ``ARANGO_URL`` but not the operator-injected
    in-cluster endpoint, whose certificate comes from the cluster's own CA —
    the same choice the platform's first-party services make (autograph
    connects to ``ARANGO_DEPLOYMENT_ENDPOINT`` with ``verify_override=False``).
    ``ARANGO_CYPHER_PLATFORM_VERIFY_TLS=on`` insists on verification.
    """
    bundle = os.getenv(PLATFORM_CA_BUNDLE_ENV, "").strip()
    if bundle:
        return bundle
    mode = os.getenv(PLATFORM_VERIFY_TLS_ENV, "auto").strip().lower()
    if mode in _ENABLED_VALUES:
        return True
    if mode in _DISABLED_VALUES:
        return False
    return not os.getenv(DEPLOYMENT_ENDPOINT_ENV, "").strip()


def describe_endpoint(endpoint: str) -> str:
    """``scheme://host:port`` of *endpoint*, for error messages."""
    parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def probe_endpoint(endpoint: str, token: str, verify: bool | str) -> str:
    """Why the endpoint cannot be reached, after python-arango failed to.

    python-arango reports every transport failure as "Can't connect to
    host(s) within limit (N)" with the cause discarded, which does not say
    whether DNS, the port, TLS or a timeout is at fault. One direct request
    names it. Never includes the token.
    """
    try:
        resp = requests.get(
            f"{endpoint.rstrip('/')}/_api/version",
            headers={"Authorization": f"bearer {token}"},
            timeout=_PROBE_TIMEOUT_S,
            verify=verify,
        )
    except requests.exceptions.SSLError as exc:
        return f"TLS verification failed ({exc.__class__.__name__}); set {PLATFORM_CA_BUNDLE_ENV} to the cluster CA"
    except requests.exceptions.Timeout:
        return f"no answer within {_PROBE_TIMEOUT_S:g}s"
    except requests.exceptions.ConnectionError as exc:
        return f"connection failed ({str(exc).replace(token, '<token>')[:200]})"
    except requests.exceptions.RequestException as exc:
        return f"request failed ({exc.__class__.__name__})"
    return f"a direct request answers HTTP {resp.status_code}, so the failure is inside the driver"


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


def choose_database(accessible: list[str] | None) -> str:
    """The database to open when the caller named none.

    :func:`default_database` when the user can open it — or when their list
    is unknown. Otherwise ``_system``, then their first database: a mount
    database need not exist (an instance can be scoped to a database that
    was since dropped, or that this user cannot see), and opening it would
    fail the session before the user ever gets to the database picker.
    """
    preferred = default_database()
    if accessible is None or preferred in accessible:
        return preferred
    if "_system" in accessible:
        return "_system"
    return accessible[0] if accessible else preferred


class PlatformTokenError(Exception):
    """The forwarded token is not a usable ArangoDB JWT (malformed, expired,
    or not issued by ArangoDB). The message never contains the token."""


def open_platform_database(client: ArangoClient, name: str, token: str) -> StandardDatabase:
    """A database handle that authenticates every call with the user's JWT.

    python-arango decodes the token locally before any request — it needs
    ``iss=arangodb`` and ``exp``/``iat`` claims — and raises PyJWT's errors
    raw, so they are narrowed to :class:`PlatformTokenError` here. The
    signature is still the coordinator's to check, on the first call.
    """
    try:
        return client.db(name, auth_method="jwt", user_token=token)
    except JWTExpiredError as exc:
        raise PlatformTokenError("the platform login has expired") from exc
    except jwt.PyJWTError as exc:
        raise PlatformTokenError(f"the platform login is not a usable ArangoDB token ({exc})") from exc
