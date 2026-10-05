import { useEffect, useMemo, useRef, useState } from "react";
import { getHealth, type HealthInfo } from "../api/client";
import { DEFAULT_TITLE, buildDetails, composeBody, issueUrl } from "../utils/bugReport";

interface Props {
  error: string | null;
  cypher: string;
  aql: string;
  onClose: () => void;
}

// "Report a problem": shows exactly what will be filed, then opens a
// pre-filled GitHub issue in the user's own browser (they submit it there).
// The repository is public, so the query is left out — and quoted values in
// the error masked — unless the user asks for them.
export default function ReportProblem({ error, cypher, aql, onClose }: Props) {
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [includeQuery, setIncludeQuery] = useState(false);
  const [title, setTitle] = useState(DEFAULT_TITLE);
  // The user's own words live apart from the generated details, so ticking
  // "include my query" (or the versions arriving) never discards them.
  const [whatHappened, setWhatHappened] = useState("");
  const [editedDetails, setEditedDetails] = useState<string | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const whatRef = useRef<HTMLTextAreaElement>(null);
  const detailsRef = useRef<HTMLTextAreaElement>(null);
  // Callers pass a fresh onClose each render; the ref keeps the focus/Escape
  // effect below from re-running (and stealing focus) on every parent render.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

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

  // Escape closes; focus starts in "What happened" and returns to whatever
  // opened the dialog.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    whatRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      opener?.focus?.();
    };
  }, []);

  const generatedDetails = useMemo(
    () =>
      buildDetails({
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
  const details = editedDetails ?? generatedDetails;
  const body = composeBody(whatHappened, details);

  const link = useMemo(() => {
    try {
      return { href: issueUrl(title, body), problem: null as string | null };
    } catch (err) {
      return { href: null, problem: err instanceof Error ? err.message : String(err) };
    }
  }, [title, body]);

  const toggleQuery = (next: boolean) => {
    if (
      editedDetails !== null &&
      !window.confirm("Changing this replaces your edits to the report details. Continue?")
    ) {
      return;
    }
    setEditedDetails(null);
    setIncludeQuery(next);
  };

  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(`# ${title}\n\n${body}`);
      setCopyState("copied");
    } catch (err) {
      // No clipboard on plain-http deployments; let the user copy by hand.
      console.warn("Copying the report failed:", err);
      setCopyState("failed");
      detailsRef.current?.focus();
      detailsRef.current?.select();
    }
  };

  const fieldClass =
    "w-full px-3 py-2 rounded bg-gray-950 border border-gray-700 text-gray-50 focus:border-indigo-500 focus:outline-none";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="report-title"
    >
      <div className="bg-gray-900 border border-gray-700 rounded-lg shadow-2xl w-full max-w-[680px] max-h-[90vh] flex flex-col">
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
          <h2 id="report-title" className="text-sm font-semibold text-gray-50">Report a problem</h2>
          <button onClick={onClose} aria-label="Close" className="text-gray-400 hover:text-gray-200 text-lg leading-none">
            &times;
          </button>
        </div>
        <div className="px-4 py-3 space-y-3 overflow-auto">
          <p className="text-xs text-gray-400">
            This opens a <strong className="text-gray-200">public</strong> GitHub issue, pre-filled with the text below.
            Nothing is sent until you press Submit on GitHub. You need a GitHub account; without one, use Copy and send
            it to your Arango contact.
          </p>
          <p className="text-xs text-amber-400">
            Error messages can quote your query. Quoted values are hidden unless you include your query — check the
            details before submitting.
          </p>
          {healthError && (
            <p role="alert" className="text-xs text-amber-400">
              Could not read the app's versions ({healthError}); they are marked unknown.
            </p>
          )}
          <label className="block">
            <span className="text-xs text-gray-400 block mb-1">Title</span>
            <input value={title} onChange={(e) => setTitle(e.target.value)} className={`${fieldClass} text-sm`} />
          </label>
          <label className="block">
            <span className="text-xs text-gray-400 block mb-1">What happened? What did you expect?</span>
            <textarea
              ref={whatRef}
              value={whatHappened}
              onChange={(e) => setWhatHappened(e.target.value)}
              rows={3}
              className={`${fieldClass} text-sm`}
            />
          </label>
          <label className="flex items-center gap-2 text-xs text-gray-300">
            <input
              type="checkbox"
              checked={includeQuery}
              onChange={(e) => toggleQuery(e.target.checked)}
              className="accent-indigo-600"
            />
            Include my Cypher and AQL, and show quoted values (may contain names or values from your data)
          </label>
          <label className="block">
            <span className="text-xs text-gray-400 block mb-1">Report details</span>
            <textarea
              ref={detailsRef}
              value={details}
              onChange={(e) => {
                setEditedDetails(e.target.value);
                setCopyState("idle");
              }}
              rows={12}
              className={`${fieldClass} text-xs font-mono`}
            />
          </label>
          {copyState === "failed" && (
            <p role="alert" className="text-xs text-red-400">
              Couldn't copy automatically. The details are selected — copy them with your keyboard.
            </p>
          )}
          {link.problem && (
            <p role="alert" className="text-xs text-red-400">
              Couldn't build the GitHub link ({link.problem}). Use Copy instead.
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2 px-4 py-3 border-t border-gray-800">
          <button
            onClick={copy}
            className="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 text-gray-300 border border-gray-700"
          >
            {copyState === "copied" ? "Copied" : "Copy"}
          </button>
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 text-gray-300 border border-gray-700"
          >
            Cancel
          </button>
          {link.href ? (
            <a
              href={link.href}
              target="_blank"
              rel="noopener noreferrer"
              className="px-3 py-1.5 text-sm rounded bg-indigo-600 hover:bg-indigo-500 text-white"
            >
              Open GitHub issue
            </a>
          ) : (
            <span className="px-3 py-1.5 text-sm rounded bg-gray-700 text-gray-400 cursor-not-allowed">
              Open GitHub issue
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
