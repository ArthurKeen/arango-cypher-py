import {
  type CompletionContext,
  type CompletionResult,
  type Completion,
} from "@codemirror/autocomplete";

export interface MappingSchema {
  entityLabels: string[];
  relationshipTypes: string[];
  entityProperties: Record<string, string[]>;
  relationshipProperties: Record<string, string[]>;
}

/**
 * Extract entity labels, relationship types, and property names from the
 * mapping JSON that the workbench stores in state.
 */
export function extractSchema(
  mapping: Record<string, unknown> | null | undefined,
): MappingSchema {
  if (!mapping || typeof mapping !== "object") {
    return { entityLabels: [], relationshipTypes: [], entityProperties: {}, relationshipProperties: {} };
  }
  const pm =
    (mapping.physical_mapping as Record<string, unknown>) ??
    (mapping.physicalMapping as Record<string, unknown>) ??
    {};

  const entities = (pm.entities ?? {}) as Record<
    string,
    Record<string, unknown>
  >;
  const relationships = (pm.relationships ?? {}) as Record<
    string,
    Record<string, unknown>
  >;

  const entityLabels = Object.keys(entities);
  const relationshipTypes = Object.keys(relationships);

  const entityProperties: Record<string, string[]> = {};
  for (const [label, meta] of Object.entries(entities)) {
    const props = meta.properties;
    if (props && typeof props === "object") {
      entityProperties[label] = Object.keys(props as Record<string, unknown>);
    }
  }

  const relationshipProperties: Record<string, string[]> = {};
  for (const [rtype, meta] of Object.entries(relationships)) {
    const props = meta.properties;
    if (props && typeof props === "object") {
      relationshipProperties[rtype] = Object.keys(
        props as Record<string, unknown>,
      );
    }
  }

  return {
    entityLabels,
    relationshipTypes,
    entityProperties,
    relationshipProperties,
  };
}

const Ctx = { Node: 0, Relationship: 1, Unknown: 2 } as const;
type Ctx = (typeof Ctx)[keyof typeof Ctx];

/**
 * Walk backwards from `pos` to determine whether the cursor sits inside a
 * node pattern `( … )` or a relationship pattern `[ … ]`.
 */
function detectContext(doc: string, pos: number): Ctx {
  let depth = 0;
  for (let i = pos - 1; i >= 0; i--) {
    const ch = doc[i];
    if (ch === ")" || ch === "]") {
      depth++;
    } else if (ch === "(" && depth > 0) {
      depth--;
    } else if (ch === "[" && depth > 0) {
      depth--;
    } else if (ch === "(" && depth === 0) {
      return Ctx.Node;
    } else if (ch === "[" && depth === 0) {
      return Ctx.Relationship;
    }
  }
  return Ctx.Unknown;
}

function makeCompletions(
  labels: string[],
  kind: "type" | "class",
  boost: number,
): Completion[] {
  return labels.map((l) => ({
    label: l,
    type: kind,
    boost,
  }));
}

/**
 * Build a CodeMirror `CompletionSource` that offers mapping-aware suggestions:
 *
 * - After `:` inside `(…)` → entity labels
 * - After `:` inside `[…]` → relationship types
 * - After `.` on a variable bound to a known label → property names
 */
export function cypherCompletion(
  schemaRef: { current: MappingSchema },
) {
  return function completionSource(
    ctx: CompletionContext,
  ): CompletionResult | null {
    const { state, pos } = ctx;
    const doc = state.doc.toString();
    const schema = schemaRef.current;

    // --- Trigger 1: after `:` (label / rel-type position) ---
    // Match a colon optionally followed by partial identifier chars
    const beforeColon = doc.slice(Math.max(0, pos - 60), pos);
    const colonMatch = beforeColon.match(/:([A-Za-z_]\w*)?$/);
    if (colonMatch) {
      const partial = colonMatch[1] ?? "";
      const from = pos - partial.length;
      const context = detectContext(doc, pos);

      if (context === Ctx.Node) {
        return {
          from,
          options: makeCompletions(schema.entityLabels, "type", 2),
          filter: true,
        };
      }

      if (context === Ctx.Relationship) {
        return {
          from,
          options: makeCompletions(schema.relationshipTypes, "type", 2),
          filter: true,
        };
      }
    }

    // --- Trigger 2: after `variable.` → property names ---
    const beforeDot = doc.slice(Math.max(0, pos - 80), pos);
    const dotMatch = beforeDot.match(/\b([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)?$/);
    if (dotMatch) {
      const varName = dotMatch[1];
      const partial = dotMatch[2] ?? "";
      const from = pos - partial.length;

      // Resolve variable to label by scanning for `(varName:Label)` pattern
      const labelPattern = new RegExp(
        `\\(\\s*${varName}\\s*:\\s*([A-Z_]\\w*)`,
        "i",
      );
      const labelMatch = doc.match(labelPattern);
      if (labelMatch) {
        const label = labelMatch[1];
        const props = schema.entityProperties[label];
        if (props && props.length > 0) {
          return {
            from,
            options: props.map((p) => ({
              label: p,
              type: "property",
              boost: 1,
            })),
            filter: true,
          };
        }
      }
    }

    return null;
  };
}
