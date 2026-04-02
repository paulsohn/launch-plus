/**
 * Convert GraphData into Cytoscape.js element definitions.
 */

import type { ElementDefinition } from "cytoscape";
import type { GraphData } from "./types.generated";

/** Shorten a source path: keep package/launch/file.ext */
function shortenSource(source: string): string {
  const launchIdx = source.lastIndexOf("/launch/");
  if (launchIdx >= 0) {
    const prefix = source.substring(0, launchIdx);
    const pkgSlash = prefix.lastIndexOf("/");
    if (pkgSlash >= 0) {
      return source.substring(pkgSlash + 1);
    }
  }
  return source;
}

export function buildElements(graph: GraphData): ElementDefinition[] {
  const elements: ElementDefinition[] = [];

  // Groups
  for (const g of graph.groups) {
    const label = g.source ? shortenSource(g.source) : "group";
    elements.push({
      group: "nodes",
      data: {
        id: g.id,
        label,
        parent: g.parent ?? undefined,
        type: "group",
        source: g.source ?? "",
      },
    });
  }

  // Nodes
  for (const n of graph.nodes) {
    const label =
      n.type === "composable_node"
        ? (n.plugin || "") + (n.name ? `\n(${n.name})` : "")
        : n.type === "executable"
          ? n.name || n.cmd || "exec"
          : n.type === "load_composable_node"
            ? `load -> ${n.target || "?"}`
            : n.fqn || n.name || "node";

    elements.push({
      group: "nodes",
      data: {
        id: n.id,
        label,
        parent: n.parent ?? undefined,
        type: n.type,
        package: n.package ?? "",
        executable: n.executable ?? "",
        plugin: n.plugin ?? "",
        name: n.name ?? "",
        namespace: n.namespace ?? "",
        fqn: n.fqn ?? "",
        cmd: n.cmd ?? "",
        target: n.target ?? "",
        nodeColor: n.color || "#888",
        params: n.params ?? [],
        remaps: n.remaps ?? [],
      },
    });
  }

  // Topics
  for (const t of graph.topics) {
    elements.push({
      group: "nodes",
      data: {
        id: t.id,
        label: t.name,
        fullName: t.name,
        type: "topic",
      },
    });
  }

  // Edges
  for (const e of graph.edges) {
    elements.push({
      group: "edges",
      data: {
        source: e.source,
        target: e.target,
        type: e.type,
      },
    });
  }

  return elements;
}
