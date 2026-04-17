import { useMemo, useState } from "react";
import type { HistoryEntry } from "../api/store";

interface Props {
  history: HistoryEntry[];
  onSelect: (cypher: string) => void;
  onClear: () => void;
  onClose: () => void;
}

function formatTime(ts: number): string {
  const d = new Date(ts);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();

  if (sameDay) {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function QueryHistory({ history, onSelect, onClear, onClose }: Props) {
  const [search, setSearch] = useState("");

  const filtered = useMemo(() => {
    if (!search.trim()) return history;
    const lower = search.toLowerCase();
    return history.filter(
      (h) =>
        h.cypher.toLowerCase().includes(lower) ||
        h.aqlPreview.toLowerCase().includes(lower),
    );
  }, [history, search]);

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative w-full max-w-md bg-gray-900 border-l border-gray-800 flex flex-col shadow-2xl">
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
          <h2 className="text-sm font-semibold text-white">Query History</h2>
          <div className="flex items-center gap-2">
            {history.length > 0 && (
              <button
                onClick={onClear}
                className="px-2 py-1 text-xs rounded bg-gray-800 hover:bg-red-900/50 text-gray-400 hover:text-red-300 transition-colors"
              >
                Clear All
              </button>
            )}
            <button
              onClick={onClose}
              className="px-2 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 text-gray-400 hover:text-gray-200 transition-colors"
            >
              Close
            </button>
          </div>
        </div>

        <div className="px-4 py-2 border-b border-gray-800">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search queries..."
            className="w-full px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-sm text-gray-200 placeholder-gray-500 focus:border-indigo-500 focus:outline-none"
          />
        </div>

        <div className="flex-1 overflow-y-auto">
          {filtered.length === 0 ? (
            <div className="flex items-center justify-center h-32">
              <p className="text-gray-600 text-sm">
                {history.length === 0 ? "No queries yet." : "No matches found."}
              </p>
            </div>
          ) : (
            <div className="divide-y divide-gray-800/50">
              {filtered.map((entry, i) => (
                <button
                  key={`${entry.timestamp}-${i}`}
                  onClick={() => {
                    onSelect(entry.cypher);
                    onClose();
                  }}
                  className="w-full text-left px-4 py-3 hover:bg-gray-800/60 transition-colors group"
                >
                  <div className="flex items-start justify-between gap-3">
                    <pre className="text-xs text-gray-200 font-mono whitespace-pre-wrap break-all line-clamp-3 flex-1">
                      {entry.cypher}
                    </pre>
                    <span className="text-xs text-gray-600 flex-shrink-0 mt-0.5">
                      {formatTime(entry.timestamp)}
                    </span>
                  </div>
                  {entry.aqlPreview && (
                    <p className="text-xs text-gray-500 mt-1 truncate font-mono">
                      → {entry.aqlPreview}
                    </p>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="px-4 py-2 border-t border-gray-800 text-xs text-gray-600">
          {history.length} {history.length === 1 ? "entry" : "entries"}
        </div>
      </div>
    </div>
  );
}
