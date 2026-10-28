#!/usr/bin/env bash
# Regenerate the Cypher parser from grammar/Cypher.g4 into arango_cypher/_antlr/.
#
# Reproduces the committed files byte for byte: ANTLR 4.13.2, Python3 target,
# listener and visitor methods on the contexts; the separate CypherListener.py /
# CypherVisitor.py files are not kept. Needs a Java runtime (for example
# `brew install openjdk`, then JAVA_HOME or PATH pointing at it) and the
# antlr4-tools wrapper from the project's virtualenv.
set -euo pipefail
cd "$(dirname "$0")/.."
ANTLR="${ANTLR:-.venv/bin/antlr4}"
if [[ -d /opt/homebrew/opt/openjdk/bin ]]; then
  export PATH="/opt/homebrew/opt/openjdk/bin:$PATH"
fi
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
"$ANTLR" -v 4.13.2 -Dlanguage=Python3 -visitor -o "$out" grammar/Cypher.g4
for f in CypherParser.py CypherLexer.py Cypher.interp Cypher.tokens CypherLexer.interp CypherLexer.tokens; do
  cp "$out/grammar/$f" "arango_cypher/_antlr/$f"
done
echo "regenerated arango_cypher/_antlr from grammar/Cypher.g4"
