#!/usr/bin/env bash
#
# Start the FastAPI service with HTTP-proxy env vars stripped.
#
# Why this exists
# ---------------
# When this repo is opened in Cursor (or any IDE that runs a sandboxed
# shell), the dev terminal inherits an HTTP/HTTPS proxy pointing at a
# loopback port — e.g. `HTTPS_PROXY=http://127.0.0.1:59564`. The proxy
# enforces an allowlist; arbitrary ArangoDB hosts (anything outside
# `localhost`, GitHub, npm, PyPI, etc.) are rejected at the HTTPS
# `CONNECT` step with `403 Forbidden`. python-arango uses `requests`,
# which honours `HTTPS_PROXY`, so `POST /connect` fails with
# `Tunnel connection failed: 403 Forbidden` for every remote DB even
# though the URL and credentials are correct.
#
# This launcher unsets every proxy env var the `requests`/`urllib3`
# stack respects, then execs uvicorn. The service then connects to
# ArangoDB directly (which is what we want — the sandbox proxy adds
# no value for outbound DB traffic).
#
# Outside a sandboxed dev environment this script is a no-op; production
# launchers should set proxy env vars only when the deployment actually
# needs them, in which case use that launcher instead.
#
# Usage
# -----
#   ./scripts/run_service.sh                       # default: 127.0.0.1:8001
#   ./scripts/run_service.sh --host 0.0.0.0        # bind all interfaces
#   ./scripts/run_service.sh --port 8000 --reload  # any uvicorn flag works

set -euo pipefail

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY \
      http_proxy https_proxy all_proxy \
      SOCKS_PROXY SOCKS5_PROXY socks_proxy socks5_proxy \
      GIT_HTTP_PROXY GIT_HTTPS_PROXY

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UVICORN="${REPO_ROOT}/.venv/bin/uvicorn"

if [[ ! -x "${UVICORN}" ]]; then
  echo "error: ${UVICORN} not found; run 'python -m venv .venv && pip install -e .[dev,service]' first" >&2
  exit 1
fi

DEFAULT_ARGS=(arango_cypher.service:app --host 127.0.0.1 --port 8001)

if [[ $# -gt 0 ]]; then
  exec "${UVICORN}" arango_cypher.service:app "$@"
else
  exec "${UVICORN}" "${DEFAULT_ARGS[@]}"
fi
