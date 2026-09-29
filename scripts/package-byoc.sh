#!/usr/bin/env bash
# Build the BYOC tarball for the Arango Container Manager (manual packaging).
#
# Layout is FLAT — `entrypoint` and `pyproject.toml` sit at the archive root.
# The platform extracts into the service workdir and looks for ./entrypoint
# there; a nested layout fails with "No entrypoint found". Set
# PACKAGE_USE_TOPDIR=1 for the nested form if a cluster needs it.
#
# The UI is bundled from ui/dist and must sit beside arango_cypher/, because
# arango_cypher/service/ui.py resolves it as <root>/ui/dist. Pass
# PACKAGE_INCLUDE_UI=0 to ship a headless API with no Workbench.
#
# macOS puts Apple metadata (com.apple.provenance, com.apple.quarantine) into
# PAX headers, which makes some Linux extractors fail with "stream closed: EOF".
# COPYFILE_DISABLE and `xattr -cr` below prevent that; both are no-ops on Linux.
#
# See docs/byoc-deployment.md. Ported from arango-ontoextract's
# scripts/package-arango-manual.sh.
set -euo pipefail

export COPYFILE_DISABLE=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-${REPO_ROOT}/arango-cypher-byoc.tar.gz}"
NAME="${PACKAGE_DIR_NAME:-myservice}"
STAGE="$(mktemp -d)"
cleanup() { rm -rf "${STAGE}"; }
trap cleanup EXIT

BUNDLE="${STAGE}/${NAME}"
mkdir -p "${BUNDLE}"

# --- Python package + build metadata -------------------------------------
cp -R "${REPO_ROOT}/arango_cypher" "${BUNDLE}/"
cp "${REPO_ROOT}/pyproject.toml" "${BUNDLE}/"
[[ -f "${REPO_ROOT}/uv.lock" ]] && cp "${REPO_ROOT}/uv.lock" "${BUNDLE}/"
[[ -f "${REPO_ROOT}/README.md" ]] && cp "${REPO_ROOT}/README.md" "${BUNDLE}/"
[[ -f "${REPO_ROOT}/LICENSE" ]] && cp "${REPO_ROOT}/LICENSE" "${BUNDLE}/"

cp "${REPO_ROOT}/entrypoint" "${BUNDLE}/entrypoint"
chmod +x "${BUNDLE}/entrypoint"

# The platform reads the first whitespace-separated token of `entrypoint` and
# runs `python /project/<token>`. Verify rather than trust: a stray edit here
# costs a full upload/deploy cycle to discover.
first_token="$(awk 'NR==1{print $1; exit}' "${BUNDLE}/entrypoint")"
if [[ "${first_token}" != "entrypoint" ]]; then
	echo "error: entrypoint line 1 must begin with the token 'entrypoint' (found '${first_token}')." >&2
	echo "       The platform runs: python /project/${first_token}" >&2
	exit 1
fi

# Strip caches that bloat the archive and can shadow installed code.
find "${BUNDLE}" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "${BUNDLE}" -type f -name '*.pyc' -delete 2>/dev/null || true

# --- Sample-query corpora -------------------------------------------------
# /sample-queries reads <root>/tests/fixtures/datasets/*/query-corpus.yml
# (arango_cypher/service/routes/schema.py:588). They live under tests/ but are
# demo content, not test scaffolding: without them the Workbench's sample-query
# picker is empty, which is how the first bring-up surfaced this. ~12K of YAML.
if [[ "${PACKAGE_INCLUDE_SAMPLES:-1}" == "1" ]]; then
	sample_count=0
	while IFS= read -r corpus; do
		rel="${corpus#${REPO_ROOT}/}"
		mkdir -p "${BUNDLE}/$(dirname "${rel}")"
		cp "${corpus}" "${BUNDLE}/${rel}"
		sample_count=$((sample_count + 1))
	done < <(find "${REPO_ROOT}/tests/fixtures/datasets" -name 'query-corpus.yml' 2>/dev/null)
	echo "==> Included ${sample_count} sample-query corpora (/sample-queries)"
fi

# --- Workbench UI ---------------------------------------------------------
if [[ "${PACKAGE_INCLUDE_UI:-1}" == "1" ]]; then
	if [[ "${PACKAGE_BUILD_UI:-1}" == "1" ]]; then
		if ! command -v npm >/dev/null 2>&1; then
			echo "error: npm not on PATH. Install Node, or set PACKAGE_BUILD_UI=0 to bundle" >&2
			echo "       the existing ui/dist, or PACKAGE_INCLUDE_UI=0 for a headless API." >&2
			exit 1
		fi
		echo "==> Building the Workbench UI (ui/dist)..."
		(
			cd "${REPO_ROOT}/ui"
			if [[ -f package-lock.json ]]; then npm ci; else npm install; fi
			npm run build
		)
	fi
	if [[ ! -f "${REPO_ROOT}/ui/dist/index.html" ]]; then
		echo "error: ui/dist/index.html missing. Run 'cd ui && npm run build', or set" >&2
		echo "       PACKAGE_INCLUDE_UI=0 to ship without the Workbench." >&2
		exit 1
	fi
	mkdir -p "${BUNDLE}/ui"
	cp -R "${REPO_ROOT}/ui/dist" "${BUNDLE}/ui/dist"
	echo "==> Included ui/dist (served at /frontend and /ui)"
else
	echo "==> Skipping the UI (PACKAGE_INCLUDE_UI=0): /frontend and /ui will 404." >&2
fi

# --- Mount prefix ---------------------------------------------------------
# FastAPI needs its mount prefix to emit correct absolute URLs, and the
# platform's deploy `env` map carries platform metadata only — it does not
# forward arbitrary app env to the container (verified: ROOT_PATH passed there
# never arrived). So it is baked at package time, the same way
# arango-ontoextract bakes SERVICE_URL_PATH_PREFIX.
#
# Without it /docs still renders, but Swagger requests /openapi.json at the
# CLUSTER ROOT, which serves ArangoDB's own Core API spec — a page that looks
# healthy and documents the wrong API. The Workbench itself is unaffected
# (Vite `base: "./"` makes its assets prefix-relative), which is exactly why
# this is easy to miss.
#
#   SERVICE_ROOT_PATH=/_service/uds/_db/<db>/<instance> bash scripts/package-byoc.sh
if [[ -n "${SERVICE_ROOT_PATH:-}" ]]; then
	printf 'ROOT_PATH=%s\n' "${SERVICE_ROOT_PATH%/}" >> "${BUNDLE}/.env"
	echo "==> Baked ROOT_PATH=${SERVICE_ROOT_PATH%/} into the bundle"
else
	echo "==> No SERVICE_ROOT_PATH set: /docs will point Swagger at the cluster-root spec." >&2
	echo "    Pass SERVICE_ROOT_PATH=/_service/uds/_db/<db>/<instance> to fix it." >&2
fi

# --- Optional .env --------------------------------------------------------
# Default OFF. The repo .env carries ARANGO_PASSWORD and LLM API keys, which
# must not ride along in a tarball that gets uploaded, archived or shared.
# Prefer setting env in the Container Manager UI.
if [[ "${PACKAGE_INCLUDE_ENV:-0}" == "1" ]]; then
	if [[ -f "${REPO_ROOT}/.env" ]]; then
		cp "${REPO_ROOT}/.env" "${BUNDLE}/.env"
		echo "==> Bundled .env (PACKAGE_INCLUDE_ENV=1) — confirm it holds no secret you" >&2
		echo "    would not put in the archive." >&2
	fi
elif [[ -f "${REPO_ROOT}/.env" ]]; then
	echo "==> Skipping .env (set PACKAGE_INCLUDE_ENV=1 to bundle it; the UI is safer for secrets)." >&2
fi

# --- Archive --------------------------------------------------------------
if [[ "$(uname -s)" == "Darwin" ]] && command -v xattr >/dev/null 2>&1; then
	xattr -cr "${BUNDLE}" 2>/dev/null || true
fi

if [[ "${PACKAGE_USE_TOPDIR:-0}" == "1" ]]; then
	tar -czf "${OUT}" -C "${STAGE}" "${NAME}"
	echo "Wrote ${OUT} (nested: ${NAME}/…)"
else
	tar -czf "${OUT}" -C "${BUNDLE}" .
	echo "Wrote ${OUT} (flat: entrypoint at archive root)"
fi

echo "==> $(du -h "${OUT}" | cut -f1) — verify with: tar -tzf ${OUT} | head"
