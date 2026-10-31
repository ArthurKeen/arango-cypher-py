#!/usr/bin/env bash
# Rebuild arango_cypher/temporal/zoneinfo.zip from the official IANA tz source.
#
# Default build, without backzone: that is the data Java, and so Neo4j, uses.
# Most Linux distributions (and Go's own archive) include backzone, which
# changes pre-1970 offsets such as Europe/Stockholm's in 1818 (+01:12:12
# instead of Neo4j's +00:53:28). The archive is the one arango-gql-gateway
# embeds (tools/tzdata/build.sh there); keep the two on the same version.
#
#   scripts/build_tzdata.sh 2026e
#
# Needs curl, tar, zic (the tz compiler) and zip.
set -euo pipefail
VERSION="${1:?usage: scripts/build_tzdata.sh <tzdata version, e.g. 2026e>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/arango_cypher/temporal"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
curl -fsSL "https://data.iana.org/time-zones/releases/tzdata${VERSION}.tar.gz" -o "$WORK/tzdata.tar.gz"
mkdir "$WORK/src" "$WORK/out"
tar -xzf "$WORK/tzdata.tar.gz" -C "$WORK/src"
(
  cd "$WORK/src"
  zic -b fat -d "$WORK/out" africa antarctica asia australasia europe northamerica southamerica etcetera backward factory
)
cd "$WORK/out"
rm -f "$DEST/zoneinfo.zip"
# Stored, not deflated, with fixed timestamps, so the archive is reproducible.
find . -type f | sed 's|^\./||' | LC_ALL=C sort | TZ=UTC xargs touch -t 198001010000
find . -type f | sed 's|^\./||' | LC_ALL=C sort | zip -q -X -0 "$DEST/zoneinfo.zip" -@
echo "$VERSION" >"$DEST/zoneinfo.version"
echo "built $DEST/zoneinfo.zip from tzdata $VERSION"
