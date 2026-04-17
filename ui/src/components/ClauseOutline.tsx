import { useEffect, useMemo, useRef, useState } from "react";

interface ClauseEntry {
  type: string;
  variables: string[];
  line: number;
  offset: number;
}

const CLAUSE_RE =
  /\b(MATCH|OPTIONAL\s+MATCH|WHERE|WITH|RETURN|ORDER\s+BY|LIMIT|SKIP|UNWIND|UNION|CREATE|SET|DELETE|DETACH\s+DELETE|MERGE|FOREACH|REMOVE|CALL)\b/gi;

const VAR_RE = /\b([a-zA-Z_]\w*)\s*(?=[:\)\]\,\.\s{])/g;

const RESERVED = new Set([
  "match", "optional", "where", "with", "return", "order", "by", "limit",
  "skip", "unwind", "union", "create", "set", "delete", "detach", "merge",
  "foreach", "remove", "call", "yield", "as", "and", "or", "not", "in",
  "starts", "ends", "contains", "is", "null", "true", "false", "case",
  "when", "then", "else", "end", "asc", "desc", "ascending", "descending",
  "distinct", "all", "any", "none", "single", "exists", "count", "sum",
  "avg", "min", "max", "collect", "on", "shortestpath", "allshortestpaths",
  "node", "relationship",
]);

function extractVariables(segment: string): string[] {
  const vars = new Set<string>();
  const stripped = segment.replace(/'[^']*'|"[^"]*"/g, "");
  let m: RegExpExecArray | null;
  const re = new RegExp(VAR_RE.source, "g");
  while ((m = re.exec(stripped)) !== null) {
    const name = m[1];
    if (!RESERVED.has(name.toLowerCase()) && !/^\d/.test(name)) {
      vars.add(name);
    }
  }
  return Array.from(vars);
}

function parseClauses(cypher: string): ClauseEntry[] {
  const entries: ClauseEntry[] = [];
  const re = new RegExp(CLAUSE_RE.source, "gi");
  let m: RegExpExecArray | null;
  const matchPositions: { type: string; offset: number }[] = [];

  while ((m = re.exec(cypher)) !== null) {
    matchPositions.push({
      type: m[0].replace(/\s+/g, " ").toUpperCase(),
      offset: m.index,
    });
  }

  for (let i = 0; i < matchPositions.length; i++) {
    const { type, offset } = matchPositions[i];
    const nextOffset = i + 1 < matchPositions.length ? matchPositions[i + 1].offset : cypher.length;
    const segment = cypher.slice(offset, nextOffset);
    const line = cypher.slice(0, offset).split("\n").length;
    entries.push({
      type,
      variables: extractVariables(segment),
      line,
      offset,
    });
  }

  return entries;
}

const CLAUSE_COLORS: Record<string, string> = {
  MATCH: "text-blue-400",
  "OPTIONAL MATCH": "text-blue-300",
  WHERE: "text-amber-400",
  WITH: "text-purple-400",
  RETURN: "text-emerald-400",
  "ORDER BY": "text-teal-400",
  LIMIT: "text-gray-400",
  SKIP: "text-gray-400",
  UNWIND: "text-cyan-400",
  UNION: "text-orange-400",
  CREATE: "text-rose-400",
  SET: "text-rose-300",
  DELETE: "text-red-400",
  "DETACH DELETE": "text-red-400",
  MERGE: "text-pink-400",
  FOREACH: "text-violet-400",
  REMOVE: "text-red-300",
  CALL: "text-indigo-300",
};

interface Props {
  cypher: string;
  onJumpToLine?: (line: number) => void;
}

export default function ClauseOutline({ cypher, onJumpToLine }: Props) {
  const [clauses, setClauses] = useState<ClauseEntry[]>([]);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const debouncedCypher = useMemo(() => cypher, [cypher]);

  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      setClauses(parseClauses(debouncedCypher));
    }, 300);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [debouncedCypher]);

  if (clauses.length === 0) {
    return (
      <div className="p-3 text-xs text-gray-600">
        No clauses detected. Start typing a Cypher query.
      </div>
    );
  }

  return (
    <div className="py-1">
      {clauses.map((c, i) => (
        <button
          key={`${c.type}-${c.line}-${i}`}
          onClick={() => onJumpToLine?.(c.line)}
          className="w-full text-left px-3 py-1.5 hover:bg-gray-800/50 transition-colors group flex items-start gap-2"
        >
          <span
            className={`text-xs font-semibold shrink-0 ${CLAUSE_COLORS[c.type] || "text-gray-400"}`}
          >
            {c.type}
          </span>
          {c.variables.length > 0 && (
            <span className="text-[10px] text-gray-500 font-mono truncate">
              {c.variables.join(", ")}
            </span>
          )}
          <span className="ml-auto text-[10px] text-gray-700 shrink-0 group-hover:text-gray-500">
            L{c.line}
          </span>
        </button>
      ))}
    </div>
  );
}
