import { useEffect, useMemo, useState } from "react";
import { getHealth, type HealthInfo } from "../api/client";
import { buildReport, issueUrl } from "../utils/bugReport";

interface Props {
  error: string | null;
  cypher: string;
  aql: string;
  onClose: () => void;
}

// "Report a problem": shows exactly what will be filed, then opens a
// pre-filled GitHub issue in the user's own browser (they submit it there).
// The repository is public, so the query is left out unless the user asks.
export default function ReportProblem({ error, cypher, aql, onClose }: Props) {
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [includeQuery, setIncludeQuery] = useState(false);
  const [edited, setEdited] = useState<{ title: string; body: string } | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getHealth()
      .then((h) => {
        if (!cancelled) setHealth(h);
      })
      .catch((err) => {
        if (!cancelled) setHealthError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const generated = useMemo(
    () =>
      buildReport({
        appVersion: health?.version ?? null,
        analyzerVersion: health?.analyzer_version ?? null,
        error,
        userAgent: navigator.userAgent,
        includeQuery,
        cypher,
        aql,
      }),
    [health, error, includeQuery, cypher, aql],
  );
  // Regenerating (versions arrive, the checkbox changes) replaces edits only
  // until the user types; after that their text wins.
  const report = edited ?? generated;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(`# ${report.title}\n\n${report.body}`);
      setCopied(true);
    } catch (err) {
      console.warn("Copying the report failed:", err);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" role="dialog" aria-modal="true" aria-labelledby="report-title">
      <div className="bg-gray-900 border border-gray-700 rounded-lg shadow-2xl w-[680px] max-h-[85vh] flex flex-col">
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
          <h2 id="report-title" className="text-sm font-semibold text-gray-50">Report a problem</h2>
          <button onClick={onClose} aria-label="Close" className="text-gray-400 hover:text-gray-200 text-lg leading-none">
            &times;
          </button>
        </div>
        <div className="px-4 py-3 space-y-3 overflow-auto">
          <p className="text-xs text-gray-400">
            This opens a <strong className="text-gray-200">public</strong> GitHub issue, pre-filled with the text below.
            Review it first — nothing is sent until you press Submit on GitHub. You need a GitHub account; without one,
            use Copy and send it to your Arango contact.
          </p>
          {healthError && (
            <p role="alert" className="text-xs text-amber-400">
              Could not read the app's versions ({healthError}); they are marked unknown.
            </p>
          )}
          <label className="flex items-center gap-2 text-xs text-gray-300">
            <input
              type="checkbox"
              checked={includeQuery}
              onChange={(e) => {
                setIncludeQuery(e.target.checked);
                setEdited(null);
              }}
              className="accent-indigo-600"
            />
            Include my Cypher and AQL (may contain names or values from your data)
          </label>
          <label className="block">
            <span className="text-xs text-gray-400 block mb-1">Title</span>
            <input
              value={report.title}
              onChange={(e) => setEdited({ ...report, title: e.target.value })}
              className="w-full px-3 py-2 rounded bg-gray-950 border border-gray-700 text-sm text-gray-50 focus:border-indigo-500 focus:outline-none"
            />
          </label>
          <label className="block">
            <span className="text-xs text-gray-400 block mb-1">Report</span>
            <textarea
              value={report.body}
              onChange={(e) => setEdited({ ...report, body: e.target.value })}
              rows={14}
              className="w-full px-3 py-2 rounded bg-gray-950 border border-gray-700 text-xs font-mono text-gray-200 focus:border-indigo-500 focus:outline-none"
            />
          </label>
        </div>
        <div className="flex justify-end gap-2 px-4 py-3 border-t border-gray-800">
          <button onClick={copy} className="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 text-gray-300 border border-gray-700">
            {copied ? "Copied" : "Copy"}
          </button>
          <button onClick={onClose} className="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 text-gray-300 border border-gray-700">
            Cancel
          </button>
          <a
            href={issueUrl(report)}
            target="_blank"
            rel="noopener noreferrer"
            className="px-3 py-1.5 text-sm rounded bg-indigo-600 hover:bg-indigo-500 text-white"
          >
            Open GitHub issue
          </a>
        </div>
      </div>
    </div>
  );
}
