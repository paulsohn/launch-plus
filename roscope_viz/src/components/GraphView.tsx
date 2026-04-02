/**
 * Cytoscape.js graph visualization component.
 *
 * Converts GraphData into Cytoscape elements, runs cose layout,
 * and handles interaction (click for detail, double-click to collapse groups).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import cytoscape from "cytoscape";
import type { GraphData } from "../types.generated";
import { buildElements } from "../graph-elements";

/** Cytoscape style definitions */
const cyStyles: cytoscape.StylesheetStyle[] = [
  {
    selector: 'node[type="group"]',
    style: {
      shape: "round-rectangle",
      "background-color": "rgba(40, 40, 80, 0.4)",
      "border-color": "#334",
      "border-width": 1,
      label: "data(label)",
      "text-valign": "top",
      "text-halign": "center",
      "font-size": "10px",
      color: "#8888aa",
      "text-margin-y": 8,
      padding: "16px",
      "text-wrap": "wrap",
    },
  },
  {
    selector: 'node[type="node"], node[type="lifecycle_node"]',
    style: {
      shape: "round-rectangle",
      "background-color": "data(nodeColor)",
      "border-color": "#555",
      "border-width": 1,
      "min-width": "40px",
      "min-height": "20px",
      padding: "8px",
      label: "data(label)",
      "text-valign": "center",
      "text-halign": "center",
      "font-size": "11px",
      color: "#1a1a2e",
      "font-weight": "bold",
      "text-wrap": "wrap",
    },
  },
  {
    selector: 'node[type="container"]',
    style: {
      shape: "round-rectangle",
      "background-color": "rgba(15, 52, 96, 0.5)",
      "border-color": "#2980b9",
      "border-width": 2,
      label: "data(label)",
      "text-valign": "top",
      "text-halign": "center",
      "font-size": "11px",
      color: "#5dade2",
      "text-margin-y": 8,
      padding: "12px",
      "text-wrap": "wrap",
    },
  },
  {
    selector: 'node[type="composable_node"]',
    style: {
      shape: "round-rectangle",
      "background-color": "data(nodeColor)",
      "background-opacity": 0.8,
      "border-color": "#5dade2",
      "border-width": 1,
      "min-width": "40px",
      "min-height": "20px",
      padding: "6px",
      label: "data(label)",
      "text-valign": "center",
      "text-halign": "center",
      "font-size": "10px",
      color: "#1a1a2e",
      "text-wrap": "wrap",
    },
  },
  {
    selector: 'node[type="load_composable_node"]',
    style: {
      shape: "round-rectangle",
      "background-color": "rgba(93, 173, 226, 0.15)",
      "border-color": "#5dade2",
      "border-width": 2,
      "border-style": "dashed",
      label: "data(label)",
      "text-valign": "top",
      "text-halign": "center",
      "font-size": "10px",
      color: "#5dade2",
      "text-margin-y": 8,
      padding: "10px",
      "text-wrap": "wrap",
    },
  },
  {
    selector: 'node[type="executable"]',
    style: {
      shape: "round-rectangle",
      "background-color": "data(nodeColor)",
      "border-color": "#555",
      "border-width": 1,
      "min-width": "40px",
      "min-height": "20px",
      padding: "8px",
      label: "data(label)",
      "text-valign": "center",
      "text-halign": "center",
      "font-size": "10px",
      color: "#1a1a2e",
      "text-wrap": "wrap",
    },
  },
  {
    selector: 'node[type="topic"]',
    style: {
      shape: "diamond",
      "background-color": "#e67e22",
      "border-color": "#d35400",
      "border-width": 1,
      "min-width": "40px",
      "min-height": "20px",
      padding: "6px",
      label: "data(label)",
      "text-valign": "center",
      "text-halign": "center",
      "font-size": "9px",
      color: "#fff",
      "text-wrap": "wrap",
    },
  },
  {
    selector: 'edge[type="remap"]',
    style: {
      width: 1,
      "line-color": "#555",
      "curve-style": "bezier",
      "target-arrow-shape": "none",
      opacity: 0.5,
    },
  },
  {
    selector: 'edge[type="load_target"]',
    style: {
      width: 2,
      "line-color": "#5dade2",
      "line-style": "dashed",
      "curve-style": "bezier",
      "target-arrow-shape": "triangle",
      "target-arrow-color": "#5dade2",
      "arrow-scale": 1.2,
      opacity: 0.7,
    },
  },
  {
    selector: "node.highlighted",
    style: { "border-color": "#e94560", "border-width": 3 },
  },
  {
    selector: "node.faded",
    style: { opacity: 0.2 },
  },
  {
    selector: "edge.faded",
    style: { opacity: 0.1 },
  },
];

interface Props {
  graph: GraphData;
  cyRef: React.RefObject<cytoscape.Core | null>;
  onNodeTap: (data: Record<string, unknown>) => void;
  onBackgroundTap: () => void;
}

export function GraphView({ graph, cyRef, onNodeTap, onBackgroundTap }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [loading, setLoading] = useState(true);

  const initCy = useCallback(() => {
    if (!containerRef.current) return;

    // Destroy previous instance
    cyRef.current?.destroy();

    const elements = buildElements(graph);

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      style: cyStyles,
      layout: { name: "preset" },
      // Use default wheelSensitivity (1.0)
      minZoom: 0.05,
      maxZoom: 3,
    });

    cyRef.current = cy;

    // Use grid layout as a baseline — places every non-compound
    // node in a grid so nothing overlaps.
    setLoading(true);
    cy.layout({
      name: "grid",
      fit: true,
      padding: 40,
      avoidOverlap: true,
      condense: true,
      nodeDimensionsIncludeLabels: true,
    } as cytoscape.LayoutOptions).run();
    setLoading(false);

    // Event handlers
    cy.on("tap", "node", (evt) => {
      const d = evt.target.data();
      if (d.type === "group") return;
      onNodeTap(d);
    });

    cy.on("tap", (evt) => {
      if (evt.target === cy) onBackgroundTap();
    });

    // Double-click group to toggle collapse
    cy.on("dblclick", 'node[type="group"]', (evt) => {
      const node = evt.target;
      const expanded = !node.data("_expanded");
      node.data("_expanded", expanded);
      if (expanded) {
        node.children().hide();
      } else {
        node.children().show();
      }
    });

    // Shift+drag resize
    let resizing: {
      node: cytoscape.NodeSingular;
      startW: number;
      startH: number;
      startX: number;
      startY: number;
    } | null = null;

    cy.on("mousedown", "node", (evt) => {
      if (!evt.originalEvent.shiftKey) return;
      const node = evt.target;
      const pos = evt.position;
      const bb = node.boundingBox();
      const threshold = 12;
      if (
        Math.abs(pos.x - bb.x2) < threshold ||
        Math.abs(pos.y - bb.y2) < threshold
      ) {
        resizing = {
          node,
          startW: node.width(),
          startH: node.height(),
          startX: pos.x,
          startY: pos.y,
        };
        node.ungrabify();
        evt.originalEvent.preventDefault();
      }
    });

    cy.on("mousemove", (evt) => {
      if (!resizing) return;
      const pos = evt.position;
      resizing.node.style({
        width: Math.max(30, resizing.startW + pos.x - resizing.startX),
        height: Math.max(20, resizing.startH + pos.y - resizing.startY),
      });
    });

    const endResize = () => {
      if (resizing) {
        resizing.node.grabify();
        resizing = null;
      }
    };
    cy.on("mouseup", endResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph, onNodeTap, onBackgroundTap]);

  useEffect(() => {
    initCy();
    return () => {
      cyRef.current?.destroy();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initCy]);

  return (
    <>
      <div id="cy" ref={containerRef} />
      {loading && (
        <div className="loading-overlay">
          <div className="spinner" />
          <p>Computing layout...</p>
        </div>
      )}
    </>
  );
}
