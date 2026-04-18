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

/** Node types that are compound (can contain children). */
const COMPOUND_TYPES = new Set(["group", "container", "lcn_wrapper"]);

/**
 * Compute the lowest common ancestor of a set of node IDs.
 * Only returns a compound node (group or container) — never a leaf.
 * Returns undefined if no compound ancestor is shared.
 */
function computeLCA(
  nodeIds: string[],
  parentMap: Map<string, string | undefined>,
  typeMap: Map<string, string>,
): string | undefined {
  if (nodeIds.length === 0) return undefined;

  function ancestors(id: string): string[] {
    const chain: string[] = [];
    let current: string | undefined = id;
    while (current) {
      chain.push(current);
      current = parentMap.get(current);
    }
    return chain;
  }

  // Start with ancestors of first node
  let common = new Set(ancestors(nodeIds[0]));
  for (let i = 1; i < nodeIds.length; i++) {
    const anc = new Set(ancestors(nodeIds[i]));
    common = new Set([...common].filter((a) => anc.has(a)));
  }
  if (common.size === 0) return undefined;

  // Find deepest common ancestor that is a compound type
  const chain = ancestors(nodeIds[0]);
  for (const a of chain) {
    if (common.has(a) && COMPOUND_TYPES.has(typeMap.get(a) ?? "")) return a;
  }
  return undefined;
}

/** Count how many compound ancestors a node has (its nesting depth). */
function compoundDepth(
  id: string,
  parentMap: Map<string, string | undefined>,
  typeMap: Map<string, string>,
): number {
  let depth = 0;
  let cur = parentMap.get(id);
  while (cur) {
    if (COMPOUND_TYPES.has(typeMap.get(cur) ?? "")) depth++;
    cur = parentMap.get(cur);
  }
  return depth;
}

export function buildElements(graph: GraphData): ElementDefinition[] {
  const elements: ElementDefinition[] = [];

  // Build parent map and type map for LCA computation
  const parentMap = new Map<string, string | undefined>();
  const typeMap = new Map<string, string>();

  // Groups — first pass: populate parentMap/typeMap so depth is computable
  for (const g of graph.groups) {
    const type = g.groupType === "lcn_wrapper" ? "lcn_wrapper" : "group";
    parentMap.set(g.id, g.parent ?? undefined);
    typeMap.set(g.id, type);
  }

  // Nodes — populate parentMap/typeMap for compound node types (containers)
  // before computing group depths, so nesting inside a container is counted.
  for (const n of graph.nodes) {
    parentMap.set(n.id, n.parent ?? undefined);
    typeMap.set(n.id, n.type);
  }

  // Groups — second pass: build elements with depth
  for (const g of graph.groups) {
    const isLcnWrapper = g.groupType === "lcn_wrapper";
    const label = isLcnWrapper
      ? "LoadComposableNodes"
      : g.source
        ? shortenSource(g.source)
        : "group";
    const type = isLcnWrapper ? "lcn_wrapper" : "group";
    const depth = compoundDepth(g.id, parentMap, typeMap);
    elements.push({
      group: "nodes",
      data: {
        id: g.id,
        label,
        parent: g.parent ?? undefined,
        type,
        depth,
        source: g.source ?? "",
        args: g.args ?? [],
        includeArgs: g.includeArgs ?? null,
      },
    });
  }

  // Nodes
  for (const n of graph.nodes) {
    const label =
      n.type === "composable_node"
        ? n.fqn || n.name || "composable"
        : n.type === "executable"
          ? n.name || n.cmd || "exec"
          : n.type === "load_composable_node"
            ? "load"
            : n.fqn || n.name || "vertex";

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
        extraArgs: n.extraArgs ?? [],
      },
    });
  }

  // Build topic -> connected node IDs map from edges
  const topicConnections = new Map<string, string[]>();
  const topicIds = new Set(graph.topics.map((t) => t.id));
  for (const e of graph.edges) {
    if (topicIds.has(e.target)) {
      const list = topicConnections.get(e.target) ?? [];
      list.push(e.source);
      topicConnections.set(e.target, list);
    }
    if (topicIds.has(e.source)) {
      const list = topicConnections.get(e.source) ?? [];
      list.push(e.target);
      topicConnections.set(e.source, list);
    }
  }

  // Topics — place each in the LCA of its connected nodes
  for (const t of graph.topics) {
    const connectedNodes = topicConnections.get(t.id) ?? [];
    const lcaParent = computeLCA(connectedNodes, parentMap, typeMap);
    if (lcaParent) {
      parentMap.set(t.id, lcaParent);
    }
    elements.push({
      group: "nodes",
      data: {
        id: t.id,
        label: t.name,
        fullName: t.name,
        type: "topic",
        parent: lcaParent,
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
