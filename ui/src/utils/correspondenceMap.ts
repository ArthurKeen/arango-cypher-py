const CYPHER_CLAUSE_RE =
  /^\s*(MATCH|OPTIONAL\s+MATCH|WHERE|WITH|RETURN|ORDER\s+BY|LIMIT|SKIP|UNWIND|UNION|CREATE|SET|DELETE|DETACH\s+DELETE|MERGE|FOREACH|REMOVE|CALL)\b/i;

const AQL_KEYWORD_MAP: Record<string, string[]> = {
  MATCH: ["FOR", "LET"],
  "OPTIONAL MATCH": ["FOR", "LET"],
  WHERE: ["FILTER"],
  WITH: ["LET", "COLLECT"],
  RETURN: ["RETURN"],
  "ORDER BY": ["SORT"],
  LIMIT: ["LIMIT"],
  SKIP: ["LIMIT"],
  UNWIND: ["FOR"],
  CREATE: ["INSERT"],
  SET: ["UPDATE", "REPLACE"],
  DELETE: ["REMOVE"],
  "DETACH DELETE": ["REMOVE"],
  MERGE: ["UPSERT"],
  FOREACH: ["FOR"],
};

interface ClauseRange {
  clauseType: string;
  startLine: number;
  endLine: number;
}

function identifyCypherClauses(cypher: string): ClauseRange[] {
  const lines = cypher.split("\n");
  const ranges: ClauseRange[] = [];

  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(CYPHER_CLAUSE_RE);
    if (m) {
      const clauseType = m[1].replace(/\s+/g, " ").toUpperCase();
      if (ranges.length > 0) {
        ranges[ranges.length - 1].endLine = i - 1;
      }
      ranges.push({ clauseType, startLine: i, endLine: i });
    }
  }
  if (ranges.length > 0) {
    ranges[ranges.length - 1].endLine = cypher.split("\n").length - 1;
  }

  return ranges;
}

export function buildCorrespondenceMap(
  cypher: string,
  aql: string,
): Map<number, number[]> {
  const result = new Map<number, number[]>();
  if (!cypher.trim() || !aql.trim()) return result;

  const clauses = identifyCypherClauses(cypher);
  const aqlLines = aql.split("\n");

  const aqlLineKeywords: string[] = aqlLines.map((line) => {
    const trimmed = line.trim().toUpperCase();
    const kw = trimmed.split(/[\s(]/)[0];
    return kw || "";
  });

  for (const clause of clauses) {
    const mappedAqlKeywords = AQL_KEYWORD_MAP[clause.clauseType] || [];
    const matchedAqlLines: number[] = [];

    for (let a = 0; a < aqlLineKeywords.length; a++) {
      if (mappedAqlKeywords.includes(aqlLineKeywords[a])) {
        matchedAqlLines.push(a);
      }
    }

    for (let cl = clause.startLine; cl <= clause.endLine; cl++) {
      const existing = result.get(cl) || [];
      result.set(cl, [...existing, ...matchedAqlLines]);
    }
  }

  return result;
}

export function buildReverseMap(
  forward: Map<number, number[]>,
): Map<number, number[]> {
  const reverse = new Map<number, number[]>();
  for (const [cypherLine, aqlLines] of forward) {
    for (const aqlLine of aqlLines) {
      const existing = reverse.get(aqlLine) || [];
      if (!existing.includes(cypherLine)) {
        existing.push(cypherLine);
      }
      reverse.set(aqlLine, existing);
    }
  }
  return reverse;
}
