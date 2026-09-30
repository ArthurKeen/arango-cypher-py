import { useEffect, useRef, useCallback, useState } from "react";
import cytoscape from "cytoscape";
import type { Core, EventObject, NodeSingular } from "cytoscape";
import { categoricalColor, graphPalette, type GraphPalette } from "../theme/graphPalette";
import { useTheme } from "../theme/theme";

export interface CyNode {
  id: string;
  label: string;
  color?: string;
  data: Record<string, unknown>;
}

export interface CyEdge {
  source: string;
  target: string;
  label?: string;
  data?: Record<string, unknown>;
}

interface Props {
  nodes: CyNode[];
  edges: CyEdge[];
  onNodeClick?: (node: CyNode) => void;
  onEdgeClick?: (edge: CyEdge) => void;
  onBackgroundClick?: () => void;
}


// Selection is Arango Green; labels and edges come from the active theme so
// they stay legible on the canvas (bg-gray-900: #f8f8f8 day, #1a1a1a night).
function graphStyle(p: GraphPalette): cytoscape.StylesheetJson {
  return [
    {
      selector: "node",
      style: {
        label: "data(label)",
        "background-color": "data(color)",
        "text-valign": "bottom",
        "text-halign": "center",
        "font-size": "10px",
        color: p.text,
        "text-margin-y": 6,
        width: 36,
        height: 36,
        "border-width": 2,
        "border-color": "data(color)",
        "border-opacity": 0.4,
        "text-max-width": "80px",
        "text-wrap": "ellipsis",
      } as cytoscape.Css.Node,
    },
    {
      selector: "node:active",
      style: {
        "overlay-opacity": 0.15,
        "overlay-color": p.accent,
      } as cytoscape.Css.Node,
    },
    {
      selector: "node.selected",
      style: {
        "border-width": 3,
        "border-color": p.accent,
        "border-opacity": 1,
      } as cytoscape.Css.Node,
    },
    {
      selector: "edge",
      style: {
        width: 1.5,
        "line-color": p.edge,
        "target-arrow-color": p.edge,
        "target-arrow-shape": "triangle",
        "curve-style": "bezier",
        label: "data(label)",
        "font-size": "9px",
        color: p.textSecondary,
        "text-background-color": p.canvas,
        "text-background-opacity": 0.85,
        "text-background-padding": "3px",
        "text-rotation": "autorotate",
      } as cytoscape.Css.Edge,
    },
    {
      selector: "edge:active",
      style: {
        "overlay-opacity": 0.1,
        "overlay-color": p.accent,
      } as cytoscape.Css.Edge,
    },
  ];
}

function pickLabel(data: Record<string, unknown>): string {
  for (const key of ["name", "title", "NAME", "label", "_key"]) {
    const v = data[key];
    if (typeof v === "string" && v) return v;
  }
  const id = data._id;
  if (typeof id === "string") {
    const parts = id.split("/");
    return parts[1] ?? id;
  }
  return String(data._key ?? "");
}

export default function CytoscapeGraph({
  nodes,
  edges,
  onNodeClick,
  onEdgeClick,
  onBackgroundClick,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const [theme] = useTheme();
  const palette = graphPalette(theme);
  const paletteRef = useRef(palette);
  paletteRef.current = palette;
  const [hoverInfo, setHoverInfo] = useState<{ x: number; y: number; label: string } | null>(null);

  const onNodeClickRef = useRef(onNodeClick);
  onNodeClickRef.current = onNodeClick;
  const onEdgeClickRef = useRef(onEdgeClick);
  onEdgeClickRef.current = onEdgeClick;
  const onBackgroundClickRef = useRef(onBackgroundClick);
  onBackgroundClickRef.current = onBackgroundClick;

  const buildElements = useCallback(() => {
    const collColors = new Map<string, string>();
    let colorIdx = 0;

    const cyNodes = nodes.map((n) => {
      const coll = n.id.split("/")[0] || "default";
      if (!collColors.has(coll)) {
        collColors.set(coll, categoricalColor(paletteRef.current, colorIdx++));
      }
      return {
        data: {
          id: n.id,
          label: n.label || pickLabel(n.data),
          color: n.color || collColors.get(coll)!,
          ...n.data,
        },
      };
    });

    const cyEdges = edges.map((e, i) => ({
      data: {
        id: `edge-${i}`,
        source: e.source,
        target: e.target,
        label: e.label || "",
        ...e.data,
      },
    }));

    return [...cyNodes, ...cyEdges];
  }, [nodes, edges]);

  useEffect(() => {
    if (!containerRef.current) return;

    const cy = cytoscape({
      container: containerRef.current,
      elements: buildElements(),
      layout: {
        name: "cose",
        animate: false,
        nodeRepulsion: () => 8000,
        idealEdgeLength: () => 120,
        gravity: 0.3,
        padding: 40,
      } as cytoscape.LayoutOptions,
      style: graphStyle(paletteRef.current),
      minZoom: 0.15,
      maxZoom: 5,
      wheelSensitivity: 0.3,
    });

    cy.on("tap", "node", (evt: EventObject) => {
      const node = evt.target as NodeSingular;
      cy.nodes().removeClass("selected");
      node.addClass("selected");
      const d = node.data();
      const cyNode: CyNode = { id: d.id, label: d.label, color: d.color, data: d };
      onNodeClickRef.current?.(cyNode);
    });

    cy.on("tap", "edge", (evt: EventObject) => {
      const edge = evt.target;
      const d = edge.data();
      const cyEdge: CyEdge = { source: d.source, target: d.target, label: d.label, data: d };
      onEdgeClickRef.current?.(cyEdge);
    });

    cy.on("tap", (evt: EventObject) => {
      if (evt.target === cy) {
        cy.nodes().removeClass("selected");
        onBackgroundClickRef.current?.();
      }
    });

    cy.on("mouseover", "node", (evt: EventObject) => {
      const node = evt.target as NodeSingular;
      const pos = node.renderedPosition();
      setHoverInfo({ x: pos.x, y: pos.y, label: node.data("label") });
      containerRef.current!.style.cursor = "pointer";
    });

    cy.on("mouseout", "node", () => {
      setHoverInfo(null);
      containerRef.current!.style.cursor = "default";
    });

    cyRef.current = cy;

    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A theme toggle restyles in place: positions, zoom and selection survive.
  useEffect(() => {
    cyRef.current?.style(graphStyle(palette));
  }, [palette]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    cy.elements().remove();
    cy.add(buildElements());
    cy.layout({
      name: "cose",
      animate: false,
      nodeRepulsion: () => 8000,
      idealEdgeLength: () => 120,
      gravity: 0.3,
      padding: 40,
    } as cytoscape.LayoutOptions).run();
  }, [buildElements]);

  return (
    <div className="relative w-full h-full">
      <div ref={containerRef} className="w-full h-full bg-gray-900 rounded border border-gray-800" />
      {hoverInfo && (
        <div
          className="absolute pointer-events-none px-2 py-1 rounded bg-gray-800 text-xs text-gray-200 border border-gray-700 shadow-lg z-10"
          style={{ left: hoverInfo.x + 20, top: hoverInfo.y - 10 }}
        >
          {hoverInfo.label}
        </div>
      )}
    </div>
  );
}
