import { useCallback, useEffect, useRef, useState } from "react";
import { EditorState } from "@codemirror/state";
import { EditorView, lineNumbers, keymap } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { json } from "@codemirror/lang-json";
import { bracketMatching } from "@codemirror/language";
import { closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { oneDark } from "./theme";
import SchemaGraph from "./SchemaGraph";

interface Props {
  mapping: Record<string, unknown>;
  onChange: (mapping: Record<string, unknown>) => void;
}

const SAMPLE_MAPPING = {
  conceptual_schema: {
    entities: [
      { name: "Person", labels: ["Person"], properties: [] },
    ],
    relationships: [
      {
        type: "KNOWS",
        fromEntity: "Person",
        toEntity: "Person",
        properties: [],
      },
    ],
  },
  physical_mapping: {
    entities: {
      Person: {
        style: "COLLECTION",
        collectionName: "persons",
        properties: {
          name: { field: "name", type: "string" },
          age: { field: "age", type: "number" },
          email: { field: "email", type: "string", indexed: true },
        },
      },
    },
    relationships: {
      KNOWS: {
        style: "DEDICATED_COLLECTION",
        edgeCollectionName: "knows",
        domain: "Person",
        range: "Person",
        properties: {
          since: { field: "since", type: "number" },
        },
      },
    },
  },
};

export default function MappingPanel({ mapping, onChange }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [owlBusy, setOwlBusy] = useState(false);
  const [viewMode, setViewMode] = useState<"json" | "graph">("json");
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const mappingRef = useRef(mapping);
  mappingRef.current = mapping;

  const handleExportOwl = useCallback(async () => {
    setOwlBusy(true);
    try {
      const res = await fetch("/mapping/export-owl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mapping: mappingRef.current }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const blob = new Blob([data.turtle], { type: "text/turtle" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "mapping.owl.ttl";
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setParseError(e instanceof Error ? e.message : "Export failed");
    } finally {
      setOwlBusy(false);
    }
  }, []);

  const handleImportOwl = useCallback(() => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".ttl,.owl,.turtle";
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      setOwlBusy(true);
      try {
        const turtle = await file.text();
        const res = await fetch("/mapping/import-owl", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ turtle }),
        });
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        const merged = {
          conceptualSchema: data.conceptualSchema,
          physicalMapping: data.physicalMapping,
          metadata: data.metadata,
        };
        onChangeRef.current(merged);
        if (viewRef.current) {
          viewRef.current.dispatch({
            changes: {
              from: 0,
              to: viewRef.current.state.doc.length,
              insert: JSON.stringify(merged, null, 2),
            },
          });
        }
        setParseError(null);
      } catch (e) {
        setParseError(e instanceof Error ? e.message : "Import failed");
      } finally {
        setOwlBusy(false);
      }
    };
    input.click();
  }, []);

  const initial =
    Object.keys(mapping).length > 0
      ? JSON.stringify(mapping, null, 2)
      : JSON.stringify(SAMPLE_MAPPING, null, 2);

  // Counter-based guard: incremented before programmatic edits, decremented
  // by the update listener. This avoids the race where a boolean flag is
  // reset synchronously before the async listener fires.
  const externalUpdateCount = useRef(0);

  useEffect(() => {
    if (!containerRef.current) return;

    const state = EditorState.create({
      doc: initial,
      extensions: [
        lineNumbers(),
        history(),
        bracketMatching(),
        closeBrackets(),
        json(),
        oneDark,
        keymap.of([
          ...closeBracketsKeymap,
          ...defaultKeymap,
          ...historyKeymap,
        ]),
        EditorView.updateListener.of((update) => {
          if (!update.docChanged) return;
          if (externalUpdateCount.current > 0) {
            externalUpdateCount.current -= 1;
            return;
          }
          const text = update.state.doc.toString();
          try {
            const parsed = JSON.parse(text);
            setParseError(null);
            onChangeRef.current(parsed);
          } catch (e) {
            setParseError(
              e instanceof Error ? e.message : "Invalid JSON",
            );
          }
        }),
        EditorView.theme({
          "&": { height: "100%" },
          ".cm-scroller": { overflow: "auto" },
        }),
      ],
    });

    const view = new EditorView({ state, parent: containerRef.current });
    viewRef.current = view;

    // Parse initial value
    try {
      const parsed = JSON.parse(initial);
      onChangeRef.current(parsed);
    } catch {
      // keep current
    }

    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync CodeMirror when the mapping prop changes externally (e.g. from schema introspect).
  // We use a counter instead of a boolean flag because CodeMirror's update listener
  // fires asynchronously after dispatch, so a boolean would already be reset.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const newText = JSON.stringify(mapping, null, 2);
    const currentText = view.state.doc.toString();
    if (newText !== currentText) {
      externalUpdateCount.current += 1;
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: newText },
      });
      setParseError(null);
    }
  }, [mapping]);

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-700 bg-gray-900/50">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setViewMode("json")}
            className={`text-xs font-medium uppercase tracking-wide transition-colors ${viewMode === "json" ? "text-indigo-400" : "text-gray-500 hover:text-gray-300"}`}
          >
            JSON
          </button>
          <span className="text-gray-700">|</span>
          <button
            onClick={() => setViewMode("graph")}
            className={`text-xs font-medium uppercase tracking-wide transition-colors ${viewMode === "graph" ? "text-indigo-400" : "text-gray-500 hover:text-gray-300"}`}
          >
            Graph
          </button>
        </div>
        <div className="flex items-center gap-1.5">
          {parseError && (
            <span className="text-xs text-red-400 truncate max-w-[120px]" title={parseError}>
              {parseError}
            </span>
          )}
          <button
            onClick={handleImportOwl}
            disabled={owlBusy}
            className="px-2 py-0.5 text-[10px] rounded bg-gray-700 text-gray-400 hover:text-gray-200 transition-colors disabled:opacity-40"
            title="Import OWL/Turtle"
          >
            OWL
          </button>
          <button
            onClick={handleExportOwl}
            disabled={owlBusy}
            className="px-2 py-0.5 text-[10px] rounded bg-gray-700 text-gray-400 hover:text-gray-200 transition-colors disabled:opacity-40"
            title="Export as OWL/Turtle"
          >
            TTL
          </button>
        </div>
      </div>
      <div
        className="flex-1 min-h-0"
        style={{ display: viewMode === "graph" ? "block" : "none" }}
      >
        <SchemaGraph mapping={mapping} />
      </div>
      <div
        className="flex-1 min-h-0"
        ref={containerRef}
        style={{ display: viewMode === "json" ? "block" : "none" }}
      />
    </div>
  );
}
