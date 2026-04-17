import { useEffect, useRef, useState, useCallback } from "react";
import { EditorState, StateEffect, StateField, type Extension } from "@codemirror/state";
import {
  EditorView,
  Decoration,
  lineNumbers,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  drawSelection,
  dropCursor,
  highlightSpecialChars,
} from "@codemirror/view";
import {
  bracketMatching,
  foldGutter,
  foldKeymap,
  indentOnInput,
} from "@codemirror/language";
import { closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { highlightSelectionMatches, searchKeymap } from "@codemirror/search";
import { oneDark } from "./theme";
import { aql, setAqlSchemaContext, type AqlSchemaContext } from "../lang/aql";

const setAqlHighlightEffect = StateEffect.define<number[]>();

const aqlHighlightMark = Decoration.line({
  class: "cm-correspondence-highlight",
});

const aqlHighlightField: Extension = StateField.define({
  create() {
    return Decoration.none;
  },
  update(decos, tr) {
    for (const e of tr.effects) {
      if (e.is(setAqlHighlightEffect)) {
        const lines = e.value;
        return Decoration.set(
          lines
            .filter((l) => l >= 1 && l <= tr.state.doc.lines)
            .sort((a, b) => a - b)
            .map((l) => aqlHighlightMark.range(tr.state.doc.line(l).from)),
        );
      }
    }
    return decos;
  },
  provide: (f) => EditorView.decorations.from(f),
});

interface Props {
  value: string;
  bindVars: Record<string, unknown>;
  error: string | null;
  onModified?: (modified: boolean, editedAql: string) => void;
  mapping?: Record<string, unknown>;
  onFormat?: () => void;
  highlightLines?: number[];
  onHoverLine?: (line: number | null) => void;
}

const AQL_CLAUSE_RE = /^(FOR|LET|FILTER|RETURN|SORT|LIMIT|COLLECT|INSERT|UPDATE|REPLACE|REMOVE|UPSERT|WINDOW|SEARCH|PRUNE)\b/i;
const INDENT_OPEN_RE = /^(FOR|COLLECT)\b/i;

function formatAql(src: string): string {
  const raw = src.replace(/\r\n?/g, "\n").trim();
  if (!raw) return raw;

  const tokens: string[] = [];
  let buf = "";
  let inStr: string | null = null;
  let inBlock = false;
  let parenDepth = 0;

  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i];

    if (inStr) {
      buf += ch;
      if (ch === "\\" && i + 1 < raw.length) { buf += raw[++i]; continue; }
      if (ch === inStr) inStr = null;
      continue;
    }
    if (inBlock) {
      buf += ch;
      if (ch === "*" && raw[i + 1] === "/") { buf += raw[++i]; inBlock = false; }
      continue;
    }
    if (ch === '"' || ch === "'") { inStr = ch; buf += ch; continue; }
    if (ch === "/" && raw[i + 1] === "*") { buf += ch + raw[++i]; inBlock = true; continue; }
    if (ch === "/" && raw[i + 1] === "/") {
      while (i < raw.length && raw[i] !== "\n") buf += raw[i++];
      continue;
    }
    if (ch === "(") parenDepth++;
    if (ch === ")") parenDepth--;
    if (/\s/.test(ch) && parenDepth === 0) {
      if (buf) { tokens.push(buf); buf = ""; }
      continue;
    }
    buf += ch;
  }
  if (buf) tokens.push(buf);

  const lines: string[] = [];
  let indent = 0;
  let lineBuf: string[] = [];

  const flush = () => {
    if (lineBuf.length === 0) return;
    lines.push("  ".repeat(indent) + lineBuf.join(" "));
    lineBuf = [];
  };

  for (const tok of tokens) {
    if (AQL_CLAUSE_RE.test(tok) && lineBuf.length > 0) {
      flush();
    }
    if (/^RETURN\b/i.test(tok) && indent > 0) {
      indent--;
    }
    lineBuf.push(tok);

    if (INDENT_OPEN_RE.test(tok) && lineBuf.length === 1) {
      flush();
      indent++;
    }
  }
  flush();

  return lines.join("\n");
}

export default function AqlEditor({ value, bindVars, error, onModified, mapping, onFormat: _onFormat, highlightLines, onHoverLine }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const [showBindVars, setShowBindVars] = useState(false);
  const transpilerValueRef = useRef(value);
  const onModifiedRef = useRef(onModified);
  onModifiedRef.current = onModified;
  const onHoverLineRef = useRef(onHoverLine);
  onHoverLineRef.current = onHoverLine;

  useEffect(() => {
    transpilerValueRef.current = value;
  }, [value]);

  useEffect(() => {
    if (!mapping) {
      setAqlSchemaContext(null);
      return;
    }
    const pm = (mapping as Record<string, unknown>).physical_mapping as Record<string, unknown> | undefined;
    if (!pm) {
      setAqlSchemaContext(null);
      return;
    }
    const ctx: AqlSchemaContext = {
      entities: (pm.entities || {}) as AqlSchemaContext["entities"],
      relationships: (pm.relationships || {}) as AqlSchemaContext["relationships"],
      bindVars,
    };
    setAqlSchemaContext(ctx);
  }, [mapping, bindVars]);

  const handleDocChange = useCallback((update: { state: { doc: { toString: () => string } } }) => {
    const edited = update.state.doc.toString();
    const original = transpilerValueRef.current;
    onModifiedRef.current?.(edited !== original, edited);
  }, []);

  useEffect(() => {
    if (!containerRef.current) return;

    const updateListener = EditorView.updateListener.of((update) => {
      if (update.docChanged) handleDocChange(update);
    });

    const state = EditorState.create({
      doc: value,
      extensions: [
        highlightSpecialChars(),
        history(),
        drawSelection(),
        dropCursor(),
        indentOnInput(),
        bracketMatching(),
        closeBrackets(),
        foldGutter(),
        lineNumbers(),
        highlightActiveLineGutter(),
        highlightActiveLine(),
        highlightSelectionMatches({ highlightWordAroundCursor: true, minSelectionLength: 1 }),
        aql(),
        keymap.of([
          ...closeBracketsKeymap,
          ...foldKeymap,
          ...searchKeymap,
          ...historyKeymap,
          indentWithTab,
          ...defaultKeymap,
        ]),
        oneDark,
        updateListener,
        aqlHighlightField,
        EditorView.theme({
          "&": { height: "100%" },
          ".cm-scroller": { overflow: "auto" },
          ".cm-tooltip.cm-tooltip-autocomplete": {
            fontFamily: "monospace",
            fontSize: "12px",
          },
          ".cm-tooltip-autocomplete ul li": {
            padding: "2px 8px",
          },
          ".cm-completionLabel": {
            fontFamily: "monospace",
          },
          ".cm-completionDetail": {
            fontStyle: "italic",
            opacity: "0.7",
            marginLeft: "8px",
          },
          ".cm-correspondence-highlight": {
            backgroundColor: "rgba(59, 130, 246, 0.1)",
          },
        }),
      ],
    });

    const view = new EditorView({ state, parent: containerRef.current });
    viewRef.current = view;

    const onMouseMove = (e: MouseEvent) => {
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
      if (pos != null) {
        const line = view.state.doc.lineAt(pos).number;
        onHoverLineRef.current?.(line);
      }
    };
    const onMouseLeave = () => onHoverLineRef.current?.(null);

    view.dom.addEventListener("mousemove", onMouseMove);
    view.dom.addEventListener("mouseleave", onMouseLeave);

    return () => {
      view.dom.removeEventListener("mousemove", onMouseMove);
      view.dom.removeEventListener("mouseleave", onMouseLeave);
      view.destroy();
      viewRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (current !== value) {
      view.dispatch({
        changes: { from: 0, to: current.length, insert: value },
      });
    }
  }, [value]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({ effects: setAqlHighlightEffect.of(highlightLines ?? []) });
  }, [highlightLines]);

  const handleFormat = useCallback(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    const formatted = formatAql(current);
    if (formatted !== current) {
      view.dispatch({
        changes: { from: 0, to: current.length, insert: formatted },
      });
    }
  }, []);

  const hasBindVars = Object.keys(bindVars).length > 0;

  return (
    <div className="h-full flex flex-col">
      {!error && value && (
        <div className="flex items-center gap-1.5 px-2 py-1 border-b border-gray-800 bg-gray-900/30">
          <button
            onClick={handleFormat}
            className="px-2 py-0.5 text-[10px] font-medium rounded bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors flex items-center gap-1"
            title="Format AQL (reindent)"
          >
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M2 3h12M4 6h8M2 9h12M4 12h8" />
            </svg>
            Format
          </button>
        </div>
      )}
      {error ? (
        <div className="flex-1 flex items-center justify-center p-4">
          <div className="p-4 rounded bg-red-900/30 border border-red-800 text-red-300 text-sm max-w-full overflow-auto">
            {error}
          </div>
        </div>
      ) : (
        <div className="flex-1 min-h-0" ref={containerRef} />
      )}

      {hasBindVars && (
        <div className="border-t border-gray-700">
          <button
            onClick={() => setShowBindVars(!showBindVars)}
            className="w-full px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 flex items-center gap-1.5 transition-colors"
          >
            <span className={`transition-transform ${showBindVars ? "rotate-90" : ""}`}>
              &#9654;
            </span>
            Bind Variables ({Object.keys(bindVars).length})
          </button>
          {showBindVars && (
            <pre className="px-3 pb-2 text-xs text-gray-300 overflow-auto max-h-32 font-mono">
              {JSON.stringify(bindVars, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
