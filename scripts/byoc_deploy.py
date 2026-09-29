#!/usr/bin/env python3
"""Upload and deploy the BYOC bundle to an Arango Container Manager cluster.

Wraps the two platform endpoints the Container Manager UI calls — the file
manager for the upload, then ACP for the deployment — and polls until the
service reports ``DEPLOYED``. That makes a release repeatable from a terminal
or CI instead of a sequence of UI clicks.

Configuration comes from the repo-root ``.env``:

===================  =========================================================
``ARANGO_URL``       coordinator URL (``ARANGO_ENDPOINT`` also accepted)
``ARANGO_USER``     platform user (``ARANGO_USERNAME`` also accepted)
``ARANGO_PASSWORD``  platform password
``ARANGO_DB``        database to mount under; omit for a ``_global`` mount
===================  =========================================================

No token is written to disk, and no credential is ever printed.

Typical use::

    python3 scripts/byoc_deploy.py list                  # what exists already
    bash scripts/package-byoc.sh                         # build the tarball
    python3 scripts/byoc_deploy.py release --version 0.2.0-1
    python3 scripts/byoc_deploy.py verify                # poll the public URL

Ported from arango-ontoextract's ``scripts/byoc_deploy.py``; the platform
quirks it encodes are documented in ``docs/byoc-deployment.md``.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARBALL = REPO_ROOT / "arango-cypher-byoc.tar.gz"

DEFAULT_APP_NAME = "arango-cypher-py"
DEFAULT_INSTANCE = "arango-cypher-py"
#: py13base is the house standard but does not exist on every cluster —
#: prod.demo.pilot.arango.ai offers node22base, py12base, py12cugraph,
#: py12torch, test. This package supports 3.11/3.12, so py12base is correct.
DEFAULT_BASE_IMAGE = "py12base"
DEFAULT_DISPLAY_NAME = "Arango Cypher Workbench"
DEFAULT_DESCRIPTION = "openCypher → AQL translation, NL → Cypher, and a query Workbench."

ACP = "/_platform/acp/v1"
FILEMANAGER = "/_platform/filemanager/global/byoc/"

READY = {"DEPLOYED"}
FAILED = {"FAILED", "ERROR", "TERMINATED"}


class DeployError(RuntimeError):
    """A platform call failed, or the cluster refused the request."""


def load_env(path: Path) -> dict[str, str]:
    """Parse a dotenv file into a plain dict.

    Surrounding quotes are stripped: the platform and ``docker --env-file``
    preserve them where python-dotenv does not, and a quoted password surfaces
    as a 401 that points nowhere near its cause.
    """
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


class Platform:
    """Thin client over the Container Manager endpoints a BYOC release needs."""

    def __init__(self, base: str, user: str, password: str, *, timeout: float = 60.0) -> None:
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self._jwt: str | None = None

    def authenticate(self) -> None:
        response = self.session.post(
            f"{self.base}/_open/auth",
            json={"username": self.user, "password": self.password},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise DeployError(f"auth failed: HTTP {response.status_code}")
        token = response.json().get("jwt")
        if not token:
            raise DeployError("auth response carried no 'jwt' field")
        self._jwt = token

    def _headers(self) -> dict[str, str]:
        if self._jwt is None:
            self.authenticate()
        return {"Authorization": f"Bearer {self._jwt}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.timeout)
        url = f"{self.base}{path}"
        response = self.session.request(method, url, headers=self._headers(), **kwargs)
        # One transparent re-auth: the JWT outlives most calls but not a slow upload.
        if response.status_code == 401:
            self.authenticate()
            response = self.session.request(method, url, headers=self._headers(), **kwargs)
        if response.status_code >= 400:
            raise DeployError(f"{method} {path} -> HTTP {response.status_code}: {response.text[:400]}")
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text[:400]}

    def list_packages(self) -> list[dict]:
        return self._request("GET", FILEMANAGER).get("services", [])

    def list_services(self) -> list[dict]:
        return self._request("POST", f"{ACP}/list_services", json={}).get("services", [])

    def upload(self, tarball: Path, name: str, version: str) -> dict:
        """Upload the package. The platform keys on (name, version) and rejects a reuse."""
        with tarball.open("rb") as handle:
            return self._request(
                "POST",
                FILEMANAGER,
                data={"name": name, "version": version, "language": "python", "type": "Service"},
                files={"file": (tarball.name, handle, "application/gzip")},
                timeout=600,
            )

    def deploy(
        self,
        name: str,
        version: str,
        instance: str,
        db_name: str | None,
        base_image: str,
        *,
        has_ui: bool = True,
        display_name: str | None = None,
        description: str | None = None,
        root_path: str | None = None,
    ) -> dict:
        # Every value must be a string: the platform decodes `env` as a protobuf
        # string->string map and rejects a JSON boolean with
        # "invalid value for string field value: true".
        env: dict[str, str] = {
            "service_type": "base_type",
            "base_image": base_image,
            "app_instance_name": instance,
        }
        # Omitting db_name mounts the service under _global instead of _db/<db>.
        if db_name:
            env["db_name"] = db_name
        # Advertising a UI makes the platform present this as an app rather than
        # a bare endpoint. We bundle the Workbench, so this is normally true.
        if has_ui:
            env["has_ui"] = "true"
        if display_name:
            env["display_name"] = display_name
        if description:
            env["description"] = description
        # FastAPI needs the mount prefix to emit correct absolute URLs. Without
        # it /docs renders but points Swagger at the cluster-root
        # /openapi.json, which serves ArangoDB's own Core API spec rather than
        # this service's — a page that looks fine and documents the wrong API.
        if root_path:
            env["ROOT_PATH"] = root_path
        return self._request(
            "POST",
            f"{ACP}/uds",
            json={"app_name": name, "app_version": version, "env": env},
            timeout=180,
        )

    def service_status(self, service_id: str) -> dict:
        return self._request("GET", f"{ACP}/service/{service_id}")

    def delete_service(self, service_id: str) -> None:
        """Remove a deployed service. There is no update endpoint — delete then
        deploy is what an update is."""
        self._request("DELETE", f"{ACP}/service/{service_id}", timeout=120)

    def find_instances(self, instance: str) -> list[dict]:
        """Every deployed service running under ``app_instance_name``.

        Destructive commands take an instance name rather than a generated id
        like ``arango-user-defined-08fxo``, which nobody can type from memory
        and which changes on every redeploy.
        """
        found = []
        for service in self.list_services():
            uds = ((service.get("serviceMeta") or {}).get("udsMeta")) or {}
            if uds.get("appInstanceName") == instance:
                found.append(
                    {
                        "serviceId": service.get("serviceId"),
                        "version": uds.get("version"),
                        "status": service.get("status"),
                        "dbName": service.get("dbName"),
                    }
                )
        return found

    def resolve_instance(self, instance: str) -> dict | None:
        """Exactly one service for this instance name, or None. Refuses ambiguity."""
        matches = self.find_instances(instance)
        if len(matches) > 1:
            raise DeployError(
                f"{len(matches)} services are running as instance {instance!r}: "
                f"{[m['serviceId'] for m in matches]}. Delete the extras by id first."
            )
        return matches[0] if matches else None

    def wait_until_ready(
        self, service_id: str, *, timeout_s: float = 600.0, interval_s: float = 10.0
    ) -> dict:
        deadline = time.monotonic() + timeout_s
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.service_status(service_id)
            info = last.get("serviceInfo") if isinstance(last, dict) else {}
            if not isinstance(info, dict):
                info = {}
            state = str(info.get("status") or last.get("status") or "").upper()
            if state in READY:
                return last
            if state in FAILED:
                raise DeployError(f"service {service_id} reached {state}: {last}")
            print(f"    status={state or '(unknown)'} — waiting {interval_s:.0f}s", flush=True)
            time.sleep(interval_s)
        raise DeployError(f"timed out after {timeout_s:.0f}s; last status: {last}")


def mount_path(instance: str, db_name: str | None) -> str:
    """The public prefix the platform serves this instance under.

    This must equal the ``ROOT_PATH`` the service is configured with, or every
    URL the app generates will be wrong.
    """
    scope = f"_db/{db_name}" if db_name else "_global"
    return f"/_service/uds/{scope}/{instance}"


def _service_id_of(result: dict) -> tuple[str | None, str | None]:
    """Pull (serviceId, status) out of a deploy/status response.

    The create response nests everything under ``serviceInfo``; reading the top
    level alone silently yields None for both.
    """
    info = result.get("serviceInfo") if isinstance(result, dict) else None
    if not isinstance(info, dict):
        info = result if isinstance(result, dict) else {}
    return info.get("serviceId") or info.get("service_id"), info.get("status")


def read_app_version() -> str:
    """The release version from ``pyproject.toml`` — this package's single source."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not match:
        raise DeployError("could not read version from pyproject.toml")
    return match.group(1)


def next_build_version(platform: Platform, name: str, release: str) -> str:
    """``<release>-<n>``, where n is one past the highest already uploaded.

    The platform rejects re-uploading an existing (name, version), so a rebuild
    of the same release needs a fresh build number rather than a version bump.
    """
    highest = 0
    for package in platform.list_packages():
        if package.get("name") != name:
            continue
        version = str(package.get("version") or "")
        match = re.fullmatch(rf"{re.escape(release)}-(\d+)", version)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{release}-{highest + 1}"


def preflight(tarball: Path) -> None:
    """Refuse to upload a bundle the platform will reject or that cannot boot."""
    if not tarball.is_file():
        raise DeployError(f"{tarball} not found — run: bash scripts/package-byoc.sh")

    import tarfile

    with tarfile.open(tarball, "r:gz") as archive:
        names = archive.getnames()
        entry = next((n for n in names if n in ("./entrypoint", "entrypoint")), None)
        if entry is None:
            raise DeployError("no ./entrypoint at the archive root — the platform needs a flat layout")
        member = archive.extractfile(entry)
        first = member.readline().decode("utf-8", "replace").strip() if member else ""
        token = first.split()[0] if first.split() else ""
        if token != "entrypoint":
            raise DeployError(
                f"entrypoint line 1 must begin with the token 'entrypoint' (found {token!r}); "
                f"the platform would run: python /project/{token}"
            )
        if not any(n.endswith("ui/dist/index.html") for n in names):
            print("==> warning: no ui/dist in the bundle; /frontend will 404", file=sys.stderr)


def resolve_config(args: argparse.Namespace) -> tuple[Platform, str, str | None]:
    env = load_env(REPO_ROOT / ".env")
    endpoint = args.endpoint or env.get("ARANGO_URL") or env.get("ARANGO_ENDPOINT")
    user = env.get("ARANGO_USER") or env.get("ARANGO_USERNAME")
    password = env.get("ARANGO_PASSWORD")
    if not (endpoint and user and password):
        raise DeployError(
            "need ARANGO_URL (or ARANGO_ENDPOINT), ARANGO_USER (or ARANGO_USERNAME) "
            "and ARANGO_PASSWORD in .env"
        )
    db_name = env.get("ARANGO_DB") if args.db is None else (args.db or None)
    return Platform(endpoint, user, password), endpoint, db_name


def cmd_list(args: argparse.Namespace) -> int:
    platform, endpoint, _ = resolve_config(args)
    print(f"cluster: {endpoint}\n")
    packages = [p for p in platform.list_packages() if p.get("name") == args.name]
    print(f"uploaded packages for {args.name!r}: {len(packages)}")
    for package in sorted(packages, key=lambda p: str(p.get("version"))):
        print(f"  {package.get('version')}")
    services = platform.find_instances(args.instance)
    print(f"\nrunning services as {args.instance!r}: {len(services)}")
    for service in services:
        print(
            f"  {service['serviceId']}  version={service['version']}  "
            f"status={service['status']}  db={service['dbName']}"
        )
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    tarball = Path(args.tarball)
    preflight(tarball)
    platform, endpoint, db_name = resolve_config(args)

    release = args.version or read_app_version()
    version = release if args.exact else next_build_version(platform, args.name, release)

    print(f"cluster : {endpoint}")
    print(f"package : {args.name} {version}")
    print(f"instance: {args.instance}  db={db_name or '(global)'}")
    print(f"image   : {args.base_image}")
    print(f"mount   : {mount_path(args.instance, db_name)}\n")

    existing = platform.resolve_instance(args.instance)
    if existing and not args.replace:
        raise DeployError(
            f"instance {args.instance!r} is already running as {existing['serviceId']} "
            f"(version {existing['version']}). Pass --replace to delete and redeploy — "
            "there is no in-place update endpoint."
        )

    print("==> uploading...", flush=True)
    platform.upload(tarball, args.name, version)

    if existing:
        print(f"==> deleting {existing['serviceId']} (--replace)...", flush=True)
        platform.delete_service(existing["serviceId"])

    print("==> deploying...", flush=True)
    result = platform.deploy(
        args.name,
        version,
        args.instance,
        db_name,
        args.base_image,
        has_ui=not args.no_ui,
        display_name=args.display_name,
        description=args.description,
        root_path=mount_path(args.instance, db_name),
    )
    service_id, status = _service_id_of(result)
    if not service_id:
        raise DeployError(f"deploy returned no serviceId: {result}")
    print(f"==> serviceId={service_id} status={status}", flush=True)

    platform.wait_until_ready(service_id, timeout_s=args.timeout)
    url = f"{endpoint.rstrip('/')}{mount_path(args.instance, db_name)}/"
    print(f"\nDEPLOYED  {url}")
    print(f"verify with: python3 {Path(__file__).name} verify")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    platform, endpoint, db_name = resolve_config(args)
    base = f"{endpoint.rstrip('/')}{mount_path(args.instance, db_name)}"
    checks = [("/openapi.json", "API"), ("/connections", "service"), ("/frontend", "Workbench")]
    ok = True
    for path, label in checks:
        url = f"{base}{path}"
        try:
            response = platform.session.get(url, headers=platform._headers(), timeout=30)
            state = "OK " if response.status_code < 400 else "FAIL"
            if response.status_code >= 400:
                ok = False
            print(f"  [{state}] {label:9s} {response.status_code}  {path}")
        except Exception as exc:  # noqa: BLE001 - report, do not abort the sweep
            ok = False
            print(f"  [FAIL] {label:9s} {type(exc).__name__}  {path}")
    print(f"\n{base}/")
    return 0 if ok else 1


def cmd_delete(args: argparse.Namespace) -> int:
    platform, _, _ = resolve_config(args)
    existing = platform.resolve_instance(args.instance)
    if not existing:
        print(f"no service running as {args.instance!r}")
        return 0
    print(f"deleting {existing['serviceId']} (version {existing['version']})...")
    platform.delete_service(existing["serviceId"])
    print("deleted")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--endpoint", help="override the cluster URL from .env")
    parser.add_argument("--db", help="database to mount under; '' forces a _global mount")
    parser.add_argument("--name", default=DEFAULT_APP_NAME, help="package name on the platform")
    parser.add_argument("--instance", default=DEFAULT_INSTANCE, help="app_instance_name")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="show uploaded packages and running services")
    p_list.set_defaults(func=cmd_list)

    p_rel = sub.add_parser("release", help="upload, deploy, and wait for DEPLOYED")
    p_rel.add_argument("--tarball", default=str(DEFAULT_TARBALL))
    p_rel.add_argument("--version", help="release version (default: pyproject version)")
    p_rel.add_argument("--exact", action="store_true", help="use --version verbatim, no -N build suffix")
    p_rel.add_argument("--base-image", default=DEFAULT_BASE_IMAGE)
    p_rel.add_argument("--display-name", default=DEFAULT_DISPLAY_NAME)
    p_rel.add_argument("--description", default=DEFAULT_DESCRIPTION)
    p_rel.add_argument("--no-ui", action="store_true", help="do not advertise a frontend")
    p_rel.add_argument("--replace", action="store_true", help="delete the running instance first")
    p_rel.add_argument("--timeout", type=float, default=600.0)
    p_rel.set_defaults(func=cmd_release)

    p_ver = sub.add_parser("verify", help="probe the deployed service's public URL")
    p_ver.set_defaults(func=cmd_verify)

    p_del = sub.add_parser("delete", help="delete the running service for this instance")
    p_del.set_defaults(func=cmd_delete)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
