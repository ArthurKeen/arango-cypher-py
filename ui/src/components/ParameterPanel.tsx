import { useEffect, useMemo, useRef, useState } from "react";

interface Props {
  cypher: string;
  params: Record<string, unknown>;
  onChange: (params: Record<string, unknown>) => void;
}

function extractParams(cypher: string): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const m of cypher.matchAll(/\$([a-zA-Z_]\w*)/g)) {
    if (!seen.has(m[1])) {
      seen.add(m[1]);
      result.push(m[1]);
    }
  }
  return result;
}

function parseJsonValue(raw: string): unknown {
  const trimmed = raw.trim();
  if (!trimmed) return undefined;
  try {
    return JSON.parse(trimmed);
  } catch {
    return trimmed;
  }
}

function serializeValue(val: unknown): string {
  if (val === undefined || val === null) return "";
  if (typeof val === "string") {
    try {
      JSON.parse(val);
      return val;
    } catch {
      return val;
    }
  }
  return JSON.stringify(val);
}

export default function ParameterPanel({ cypher, params, onChange }: Props) {
  const detectedNames = useMemo(() => extractParams(cypher), [cypher]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    setDrafts((prev) => {
      const next: Record<string, string> = {};
      for (const name of detectedNames) {
        next[name] = prev[name] ?? serializeValue(params[name]);
      }
      return next;
    });
  }, [detectedNames, params]);

  useEffect(() => {
    const next: Record<string, unknown> = {};
    let hasValues = false;
    for (const name of detectedNames) {
      const raw = drafts[name];
      if (raw !== undefined && raw.trim() !== "") {
        next[name] = parseJsonValue(raw);
        hasValues = true;
      }
    }
    if (hasValues || Object.keys(params).length > 0) {
      const serialized = JSON.stringify(next);
      const currentSerialized = JSON.stringify(
        Object.fromEntries(
          Object.entries(params).filter(([k]) => detectedNames.includes(k)),
        ),
      );
      if (serialized !== currentSerialized) {
        onChangeRef.current(next);
      }
    }
  }, [drafts, detectedNames, params]);

  if (detectedNames.length === 0) return null;

  function handleChange(name: string, value: string) {
    setDrafts((prev) => ({ ...prev, [name]: value }));
  }

  return (
    <div className="border-t border-gray-800">
      <div className="px-3 py-1.5 bg-gray-900/30 border-b border-gray-800 flex items-center gap-2">
        <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">
          Parameters
        </span>
        <span className="text-xs text-gray-600">
          {detectedNames.length}
        </span>
      </div>
      <div className="px-3 py-2 space-y-2 max-h-40 overflow-y-auto">
        {detectedNames.map((name) => {
          const raw = drafts[name] ?? "";
          let isValidJson = false;
          if (raw.trim()) {
            try {
              JSON.parse(raw);
              isValidJson = true;
            } catch {
              /* treat as string */
            }
          }
          return (
            <div key={name} className="flex items-center gap-2">
              <label className="text-xs text-indigo-400 font-mono w-28 flex-shrink-0 truncate">
                ${name}
              </label>
              <input
                type="text"
                value={raw}
                onChange={(e) => handleChange(name, e.target.value)}
                placeholder="JSON value or string"
                className="flex-1 px-2 py-1 bg-gray-800 border border-gray-700 rounded text-sm text-gray-200 font-mono focus:border-indigo-500 focus:outline-none"
              />
              {raw.trim() && (
                <span
                  className={`text-xs flex-shrink-0 ${isValidJson ? "text-emerald-500" : "text-gray-500"}`}
                >
                  {isValidJson ? "json" : "str"}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
