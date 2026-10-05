"""``GET /examples`` — verified examples mined from the connected database.

Written offline by ``arango-cypher-py mine-examples`` into the database's
``arango_cypher_examples`` collection; served here without an LLM, so a
deployment with no API key still shows them. Scoped to the session's database
and, when one is bound, its named graph.
"""

from __future__ import annotations

import time

from arango.exceptions import ArangoError
from fastapi import Depends, HTTPException, Query

from ...query_mining.store import MAX_LIST, collection_name, list_examples
from ..app import _svc_logger, app
from ..observability import log_endpoint_timing
from ..security import _get_session, _Session


@app.get("/examples")
def mined_examples(
    limit: int = Query(50, ge=1, le=MAX_LIST),
    session: _Session = Depends(_get_session),
):
    """The session database's verified examples: question, Cypher, params,
    the AQL that was verified, and where the example came from."""
    t0 = time.perf_counter()
    try:
        examples = list_examples(session.db, graph=session.graph_name, limit=limit)
    except ArangoError as exc:
        _svc_logger.warning("listing mined examples from %s failed: %s", collection_name(), exc)
        log_endpoint_timing("/examples", round((time.perf_counter() - t0) * 1000, 1), status="error")
        raise HTTPException(
            status_code=502,
            detail={"error": "examples_unavailable", "message": f"Could not read {collection_name()}: {exc}"},
        ) from exc
    log_endpoint_timing(
        "/examples",
        round((time.perf_counter() - t0) * 1000, 1),
        examples=len(examples),
        graph=session.graph_name or "",
    )
    return {"examples": examples}
