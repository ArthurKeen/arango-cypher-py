"""Snapshot / restore the ``arango_cypher.service`` module tree around reloads.

Reloading ``arango_cypher.service`` re-executes its ``__init__``, which
*purges and re-imports every* ``arango_cypher.service.*`` submodule (see the
purge loop at the top of ``arango_cypher/service/__init__.py``). A reload
therefore replaces ``routes.cypher`` / ``app`` / ``security`` / … with fresh
module objects and registers routes on a brand-new FastAPI ``app``.

If a test restores only the *top-level* ``arango_cypher.service`` entry on
teardown (the historical pattern), the reloaded submodules leak into
``sys.modules`` while the restored package's ``app`` stays bound to the
*original* submodules. A later test that does ``from
arango_cypher.service.routes import cypher`` then monkeypatches the *stale
reloaded* module — which the running app never references — so the patch
silently misses (the symptom: ``/execute`` returns the real ``MAPPING_NOT_FOUND``
instead of the stubbed behaviour). See
``tests/test_session_tenant_binding.py::TestExecuteTenantViolationStatusCode``.

These helpers snapshot the full ``arango_cypher.service*`` tree before a
reload and restore it exactly afterwards, so no reloaded submodule leaks into
subsequent tests.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_PREFIX = "arango_cypher.service"


def _service_module_names() -> list[str]:
    return [n for n in list(sys.modules) if n == _PREFIX or n.startswith(_PREFIX + ".")]


def snapshot_service_modules() -> dict[str, Any]:
    """Return the current ``arango_cypher.service*`` ``sys.modules`` entries."""
    return {n: sys.modules[n] for n in _service_module_names()}


def restore_service_modules(snapshot: dict[str, Any]) -> None:
    """Restore the tree to *snapshot*, dropping any modules created since.

    Removes every current ``arango_cypher.service*`` entry that was not in
    the snapshot (i.e. reloaded submodules) and re-installs the snapshot,
    so both the top-level package and every submodule point back at the
    original objects the rest of the suite already captured.
    """
    for name in _service_module_names():
        if name not in snapshot:
            del sys.modules[name]
    sys.modules.update(snapshot)


def fresh_service() -> Any:
    """Return the *live* ``arango_cypher.service`` module from
    ``sys.modules``, re-importing if a previous test removed it.

    Other test files (notably ``test_service_hardening.py``) reload
    the service module via ``importlib.import_module`` and replace
    ``sys.modules["arango_cypher.service"]``. A top-level
    ``from arango_cypher import service`` captures the pre-reload
    object; this helper re-resolves on every call so our tests always
    patch the same module instance the routes actually import.
    """
    if "arango_cypher.service" not in sys.modules:
        return importlib.import_module("arango_cypher.service")
    return sys.modules["arango_cypher.service"]


@contextmanager
def patched_arango_client(fake_client_factory: Any) -> Iterator[None]:
    """Patch ``arango_cypher.service.ArangoClient`` on *every* live
    package object that holds a reference to it.

    The test_service_hardening fixture reloads the service module via
    ``importlib.import_module``, after which two distinct objects can
    both claim to be ``arango_cypher.service``:

    * ``sys.modules["arango_cypher.service"]`` — the version restored
      by the fixture's teardown (the *saved* original).
    * ``arango_cypher.service`` (attribute on the parent package) —
      the *reloaded* module, which the autouse fixture monkeypatched
      and whose ``ArangoClient`` may still be the test stub.

    The ``/connect`` endpoint does ``from arango_cypher import service
    as _svc``, which reads the parent-package attribute — i.e. the
    reloaded module. To make the test deterministic regardless of which
    test ran before us, we override ``ArangoClient`` on every live
    candidate; cleanup restores the original references.
    """
    parent = sys.modules.get("arango_cypher")
    candidates: list[Any] = []
    sys_mod = sys.modules.get("arango_cypher.service")
    if sys_mod is not None:
        candidates.append(sys_mod)
    parent_attr = getattr(parent, "service", None) if parent is not None else None
    if parent_attr is not None and not any(parent_attr is c for c in candidates):
        candidates.append(parent_attr)

    if not candidates:
        # Force-resolve when neither view exists yet.
        candidates.append(importlib.import_module("arango_cypher.service"))

    saved: list[tuple[Any, Any]] = []
    for mod in candidates:
        saved.append((mod, getattr(mod, "ArangoClient", None)))
        mod.ArangoClient = fake_client_factory
    try:
        yield
    finally:
        for mod, orig in saved:
            if orig is None:
                if hasattr(mod, "ArangoClient"):
                    delattr(mod, "ArangoClient")
            else:
                mod.ArangoClient = orig
