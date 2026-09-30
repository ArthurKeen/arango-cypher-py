import { EditorView } from "@codemirror/view";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";

// Every colour is a CSS variable defined per theme in src/index.css, so one
// extension serves day and night and editors follow a toggle live.
const colors = {
  bg: "var(--cm-bg)",
  fg: "var(--cm-fg)",
  keyword: "var(--cm-keyword)",
  string: "var(--cm-string)",
  number: "var(--cm-number)",
  function: "var(--cm-function)",
  variable: "var(--cm-variable)",
  variableSpecial: "var(--cm-variable-special)",
  type: "var(--cm-type)",
  comment: "var(--cm-comment)",
  operator: "var(--cm-operator)",
  bracket: "var(--cm-bracket)",
  punctuation: "var(--cm-punctuation)",
};

const accent = (alpha: number) => `rgba(var(--cm-accent-rgb), ${alpha})`;

const highlightStyle = HighlightStyle.define([
  { tag: t.keyword, color: colors.keyword, fontWeight: "bold" },
  { tag: t.string, color: colors.string },
  { tag: t.number, color: colors.number },
  { tag: [t.function(t.variableName), t.function(t.name)], color: colors.function },
  { tag: t.variableName, color: colors.variable },
  { tag: t.special(t.variableName), color: colors.variableSpecial, fontWeight: "bold" },
  { tag: t.typeName, color: colors.type, fontWeight: "bold" },
  { tag: [t.lineComment, t.blockComment], color: colors.comment, fontStyle: "italic" },
  { tag: t.operator, color: colors.operator },
  { tag: t.bracket, color: colors.bracket },
  { tag: t.punctuation, color: colors.punctuation },
]);

const baseTheme = EditorView.theme(
  {
    "&": {
      color: colors.fg,
      backgroundColor: colors.bg,
    },
    ".cm-content": {
      caretColor: "var(--cm-caret)",
    },
    ".cm-cursor, .cm-dropCursor": {
      borderLeftColor: "var(--cm-caret)",
    },
    "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection":
      {
        backgroundColor: accent(0.22),
      },
    ".cm-panels": {
      backgroundColor: "var(--cm-panel-bg)",
      color: colors.fg,
    },
    ".cm-panels.cm-panels-top": {
      borderBottom: "1px solid var(--cm-border)",
    },
    ".cm-panels.cm-panels-bottom": {
      borderTop: "1px solid var(--cm-border)",
    },
    ".cm-searchMatch": {
      backgroundColor: "rgba(250, 204, 21, 0.2)",
      outline: "1px solid rgba(250, 204, 21, 0.4)",
    },
    ".cm-searchMatch.cm-searchMatch-selected": {
      backgroundColor: "rgba(250, 204, 21, 0.4)",
    },
    ".cm-activeLine": {
      backgroundColor: accent(0.06),
    },
    ".cm-selectionMatch": {
      backgroundColor: accent(0.15),
    },
    ".cm-matchingBracket, .cm-nonmatchingBracket": {
      backgroundColor: accent(0.25),
      outline: `1px solid ${accent(0.5)}`,
    },
    ".cm-gutters": {
      backgroundColor: colors.bg,
      color: "var(--cm-gutter-fg)",
      border: "none",
      borderRight: "1px solid var(--cm-border)",
    },
    ".cm-activeLineGutter": {
      backgroundColor: accent(0.1),
    },
    ".cm-foldPlaceholder": {
      backgroundColor: "transparent",
      border: "none",
      color: colors.comment,
    },
    ".cm-tooltip": {
      border: "1px solid var(--cm-border)",
      backgroundColor: "var(--cm-panel-bg)",
      color: colors.fg,
    },
    ".cm-tooltip .cm-tooltip-arrow:before": {
      borderTopColor: "transparent",
      borderBottomColor: "transparent",
    },
    ".cm-tooltip .cm-tooltip-arrow:after": {
      borderTopColor: "var(--cm-panel-bg)",
      borderBottomColor: "var(--cm-panel-bg)",
    },
    ".cm-tooltip-autocomplete": {
      "& > ul > li[aria-selected]": {
        backgroundColor: accent(0.2),
        color: colors.fg,
      },
    },
  },
);

export const editorTheme = [baseTheme, syntaxHighlighting(highlightStyle)];
