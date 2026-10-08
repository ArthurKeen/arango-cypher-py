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
with a bare "Can't connect to host(s)" (seen on prod.demo). The platform
injects that CA as ``ARANGO_DEPLOYMENT_CA``; see :func:`platform_tls_verify`
for the policy and its overrides.

Work that outlives a request (the background schema warm) cannot rely on the
forwarded JWT, which expires. The platform also injects an integration
sidecar (``INTEGRATION_HTTP_ADDRESS[_FULL]``) that names the user a token
belongs to (``/_integration/authn/v1/identity``) and mints tokens for that
user (``/_integration/authn/v1/createToken``); see :func:`background_token`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
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

#: The CA that signs the injected endpoint's certificate (operator-injected):
#: a file path, or the PEM text itself.
DEPLOYMENT_CA_ENV = "ARANGO_DEPLOYMENT_CA"
#: A CA bundle (PEM path) that signs the platform endpoint's certificate.
PLATFORM_CA_BUNDLE_ENV = "ARANGO_CYPHER_PLATFORM_CA_BUNDLE"
#: The integration sidecar's address, injected into BYOC pods (the first set wins).
SIDECAR_ADDRESS_ENVS = ("INTEGRATION_HTTP_ADDRESS_FULL", "INTEGRATION_HTTP_ADDRESS")
#: Lifetime, in seconds, of a token minted for background work (default 3600).
SIDECAR_TOKEN_LIFETIME_ENV = "ARANGO_CYPHER_SIDECAR_TOKEN_LIFETIME_S"
#: ``on`` / ``off`` override the ``auto`` TLS policy.
PLATFORM_VERIFY_TLS_ENV = "ARANGO_CYPHER_PLATFORM_VERIFY_TLS"

_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})
_ENABLED_VALUES = frozenset({"on", "1", "true", "yes"})

#: Seconds for the diagnostic probe that runs only after a connect failed.
_PROBE_TIMEOUT_S = 5.0
#: Seconds for a call to the integration sidecar.
_SIDECAR_TIMEOUT_S = 5.0
_DEFAULT_SIDECAR_TOKEN_LIFETIME_S = 3600

_logger = logging.getLogger(__name__)

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


_ca_lock = threading.Lock()
_ca_files: dict[str, str] = {}


def deployment_ca() -> str | None:
    """A file holding the CA the platform injected for its endpoint, or ``None``.

    ``ARANGO_DEPLOYMENT_CA`` names a file; if it holds the PEM text instead,
    the text is written once to a private temporary file, since TLS libraries
    take a path. ``None`` when it is unset, or names no file.
    """
    value = os.getenv(DEPLOYMENT_CA_ENV, "").strip()
    if not value:
        return None
    if "-----BEGIN" not in value:
        if os.path.isfile(value):
            return value
        _logger.warning(
            "%s names no file (%s); the platform endpoint is not verified", DEPLOYMENT_CA_ENV, value
        )
        return None
    digest = hashlib.sha256(value.encode()).hexdigest()
    with _ca_lock:
        path = _ca_files.get(digest)
        if path is None or not os.path.isfile(path):
            fd, path = tempfile.mkstemp(prefix="arango-deployment-ca-", suffix=".pem")
            with os.fdopen(fd, "w") as f:
                f.write(value if value.endswith("\n") else value + "\n")
            _ca_files[digest] = path
        return path


def platform_tls_verify() -> bool | str:
    """TLS verification for the platform endpoint, as python-arango's
    ``verify_override`` takes it: a CA bundle path, ``True`` or ``False``.

    In order: a configured ``ARANGO_CYPHER_PLATFORM_CA_BUNDLE``; an explicit
    ``ARANGO_CYPHER_PLATFORM_VERIFY_TLS=on|off``; for the operator-injected
    endpoint, the CA the platform injected with it (``ARANGO_DEPLOYMENT_CA``),
    or no verification when there is none, since its certificate comes from the
    cluster's own CA; for an explicitly configured ``ARANGO_URL``, the system
    trust store.
    """
    bundle = os.getenv(PLATFORM_CA_BUNDLE_ENV, "").strip()
    if bundle:
        return bundle
    mode = os.getenv(PLATFORM_VERIFY_TLS_ENV, "auto").strip().lower()
    if mode in _ENABLED_VALUES:
        return True
    if mode in _DISABLED_VALUES:
        return False
    if not os.getenv(DEPLOYMENT_ENDPOINT_ENV, "").strip():
        return True
    return deployment_ca() or False


def describe_tls_verify(verify: bool | str) -> str:
    """How the platform endpoint is verified, for logs and diagnostics."""
    if isinstance(verify, str):
        if verify == deployment_ca():
            return f"verified against the injected CA ({DEPLOYMENT_CA_ENV})"
        return f"verified against {PLATFORM_CA_BUNDLE_ENV}"
    return "verified against the system trust store" if verify else "not verified"


def describe_endpoint(endpoint: str) -> str:
    """``scheme://host:port`` of *endpoint*, for error messages."""
    parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def endpoint_answer(endpoint: str, token: str, verify: bool | str) -> str:
    """What one direct ``GET /_api/version`` with *token* gets: ``HTTP <code>``,
    or why the request failed. Never includes the token."""
    try:
        resp = requests.get(
            f"{endpoint.rstrip('/')}/_api/version",
            headers={"Authorization": f"bearer {token}"},
            timeout=_PROBE_TIMEOUT_S,
            verify=verify,
        )
    except requests.exceptions.SSLError as exc:
        return (
            f"TLS verification failed ({exc.__class__.__name__}); set {PLATFORM_CA_BUNDLE_ENV} to the cluster CA, "
            f"or {PLATFORM_VERIFY_TLS_ENV}=off"
        )
    except requests.exceptions.Timeout:
        return f"no answer within {_PROBE_TIMEOUT_S:g}s"
    except requests.exceptions.ConnectionError as exc:
        return f"connection failed ({str(exc).replace(token, '<token>')[:200]})"
    except requests.exceptions.RequestException as exc:
        return f"request failed ({exc.__class__.__name__})"
    return f"HTTP {resp.status_code}"


def probe_endpoint(endpoint: str, token: str, verify: bool | str) -> str:
    """Why the endpoint cannot be reached, after python-arango failed to.

    python-arango reports every transport failure as "Can't connect to
    host(s) within limit (N)" with the cause discarded, which does not say
    whether DNS, the port, TLS or a timeout is at fault. One direct request
    names it. Never includes the token.
    """
    answer = endpoint_answer(endpoint, token, verify)
    if answer.startswith("HTTP "):
        return f"a direct request answers {answer}, so the failure is inside the driver"
    return answer


def token_facts(token: str) -> dict[str, object]:
    """What a JWT says about itself, without its signature or claim values:
    algorithm, issuer, claim names, lifetime, and whether python-arango's own
    pre-check accepts it. Unverified, so for diagnostics only."""
    import time

    try:
        header = jwt.get_unverified_header(token)
        payload = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        return {"parsable": False, "error": exc.__class__.__name__}
    exp, iat = payload.get("exp"), payload.get("iat")
    try:
        # python-arango's own pre-check (expiry, and an ``exp`` claim), run by
        # the driver itself; building the handle makes no request.
        from arango import ArangoClient

        ArangoClient(hosts="http://127.0.0.1:9").db("_system", auth_method="jwt", user_token=token)
        driver_accepts = True
    except (jwt.PyJWTError, JWTExpiredError, KeyError):
        driver_accepts = False
    return {
        "parsable": True,
        "alg": header.get("alg"),
        "iss": payload.get("iss"),
        "claims": sorted(payload),
        "lifetime_s": exp - iat if isinstance(exp, int | float) and isinstance(iat, int | float) else None,
        "expires_in_s": round(exp - time.time()) if isinstance(exp, int | float) else None,
        "python_arango_accepts": driver_accepts,
    }


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

    python-arango decodes the token locally before any request, without
    checking the signature: it refuses an expired token, and a token without
    an ``exp`` claim with a bare ``KeyError``. Those, and PyJWT's errors for a
    malformed token, are narrowed to :class:`PlatformTokenError` here. The
    signature is still the coordinator's to check, on the first call.
    """
    try:
        return client.db(name, auth_method="jwt", user_token=token)
    except JWTExpiredError as exc:
        raise PlatformTokenError("the platform login has expired") from exc
    except KeyError as exc:
        raise PlatformTokenError(f"the platform login has no {exc} claim") from exc
    except jwt.PyJWTError as exc:
        raise PlatformTokenError(f"the platform login is not a usable ArangoDB token ({exc})") from exc


# --- Integration sidecar ----------------------------------------------------


class SidecarError(Exception):
    """The integration sidecar could not answer. The message never contains a token."""


def sidecar_address() -> str | None:
    """The integration sidecar's base URL, or ``None`` off the platform."""
    for name in SIDECAR_ADDRESS_ENVS:
        value = os.getenv(name, "").strip().rstrip("/")
        if value:
            return value if "://" in value else f"http://{value}"
    return None


_identity_lock = threading.Lock()
_identities: dict[str, str] = {}
_MAX_IDENTITIES = 1000


def sidecar_identity(token: str) -> str | None:
    """The user *token* belongs to, as the sidecar (which validates it) says.

    ``None`` off the platform, or when the sidecar does not know the token.
    Answers are cached by the token's hash: gateway tokens rotate, so the cache
    is cleared rather than allowed to grow without bound.
    """
    address = sidecar_address()
    if address is None:
        return None
    key = hashlib.sha256(token.encode()).hexdigest()
    with _identity_lock:
        if key in _identities:
            return _identities[key]
    try:
        resp = requests.get(
            f"{address}/_integration/authn/v1/identity",
            headers={"Authorization": f"bearer {token}"},
            timeout=_SIDECAR_TIMEOUT_S,
        )
    except requests.exceptions.RequestException as exc:
        _logger.warning("integration sidecar identity lookup failed: %s", exc.__class__.__name__)
        return None
    if resp.status_code != 200:
        _logger.warning("integration sidecar identity lookup answered HTTP %s", resp.status_code)
        return None
    try:
        user = resp.json().get("user")
    except ValueError:
        return None
    if not isinstance(user, str) or not user:
        return None
    with _identity_lock:
        if len(_identities) >= _MAX_IDENTITIES:
            _identities.clear()
        _identities[key] = user
    return user


def sidecar_token_lifetime_s() -> int:
    raw = os.getenv(SIDECAR_TOKEN_LIFETIME_ENV, "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_SIDECAR_TOKEN_LIFETIME_S
    except ValueError:
        _logger.warning(
            "%s=%r is not a number of seconds; using %s",
            SIDECAR_TOKEN_LIFETIME_ENV,
            raw,
            _DEFAULT_SIDECAR_TOKEN_LIFETIME_S,
        )
        return _DEFAULT_SIDECAR_TOKEN_LIFETIME_S
    return max(60, value)


def sidecar_token(user: str, lifetime_s: int) -> str:
    """A new token for *user*, minted by the integration sidecar.

    Refuses an empty user: the sidecar would mint a token for its default
    (root) account, which would let background work exceed the caller's
    permissions.
    """
    if not user:
        raise SidecarError("refusing to mint a token without a user")
    address = sidecar_address()
    if address is None:
        raise SidecarError("no integration sidecar is configured")
    try:
        resp = requests.post(
            f"{address}/_integration/authn/v1/createToken",
            json={"lifetime": f"{lifetime_s}s", "user": user},
            timeout=_SIDECAR_TIMEOUT_S,
        )
    except requests.exceptions.RequestException as exc:
        raise SidecarError(f"the integration sidecar did not answer ({exc.__class__.__name__})") from exc
    if resp.status_code != 200:
        raise SidecarError(f"the integration sidecar answered HTTP {resp.status_code}")
    try:
        token = resp.json().get("token")
    except ValueError as exc:
        raise SidecarError("the integration sidecar answered with something other than JSON") from exc
    if not isinstance(token, str) or not token:
        raise SidecarError("the integration sidecar answered without a token")
    return token


def background_token(user: str | None) -> str | None:
    """A token for *user* that outlives the request, or ``None``.

    ``None`` when the user is unknown (as identified by the sidecar) or the
    sidecar cannot mint one; the caller then keeps the request's own token.
    Never mints for an unknown user.
    """
    if not user or sidecar_address() is None:
        return None
    try:
        return sidecar_token(user, sidecar_token_lifetime_s())
    except SidecarError as exc:
        _logger.warning("background token for %r unavailable: %s", user, exc)
        return None


def open_minted_database(client: ArangoClient, name: str, token: str) -> StandardDatabase:
    """A database handle that sends a sidecar-minted *token* as-is.

    python-arango checks a token locally before using it (it must carry an
    ``exp`` claim and not have expired), which this service need not insist
    on for a minted token: the server checks the signature and expiry, and
    applies that user's permissions, either way. Its
    ``superuser_token`` path is the one that sends a token unchecked; it
    grants nothing beyond what the token itself carries.
    """
    return client.db(name, superuser_token=token)
