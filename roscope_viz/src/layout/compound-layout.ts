/**
 * Compound layout for Cytoscape.js.
 *
 * Pipeline:
 *   1. Bottom-up size computation — leaf sizes from label dimensions,
 *      compound sizes from aspect-ratio-optimal strip packing of children.
 *   2. Top-down positioning — place each node using computed offsets.
 *   3. Post-processing — topics to centroid of connected nodes (+ repulsion),
 *      LCN marker nodes snapped to the edge of their parent facing the target.
 *
 * Child ordering is factored into a swappable ChildOrderStrategy:
 *   - packOnlyStrategy (default): single layer, strip packing chooses rows.
 *   - A future Sugiyama strategy can return pre-assigned layers (one array
 *     per topological layer) derived from directed topic edges, causing each
 *     layer to be rendered as a forced single row and stacked top-to-bottom.
 *     Add it here when remap direction info becomes available.
 *
 * To add Sugiyama later:
 *   1. Annotate remaps with direction: `<remap from="..." to="..." type="sub"/>`.
 *   2. Propagate direction through the graph IR (schema.py → graph.py → types).
 *   3. Implement `sugiyamaStrategy: ChildOrderStrategy` in a new file
 *      `layout/sugiyama-strategy.ts`:
 *        - Build a DAG from `siblingEdges` (directed topic edges between siblings).
 *        - Assign topological layers via Kahn's algorithm.
 *        - Minimise crossings with the barycenter heuristic.
 *        - Return `[[layer0_ids], [layer1_ids], ...]`.
 *   4. Pass it to `compoundLayout(cy, sugiyamaStrategy)` in GraphView.tsx.
 *   No other changes are needed — multi-layer rendering is already handled.
 */

import type cytoscape from "cytoscape";

// ── Constants ─────────────────────────────────────────────────────────

const PADDING_X = 16;
const PADDING_TOP = 32; // extra top clearance for compound label
const PADDING_BOTTOM = 16;
const GAP_X = 20;
const GAP_Y = 20;
const DEFAULT_LEAF_W = 120;
const DEFAULT_LEAF_H = 40;
const TOPIC_NODE_MIN_DIST = 60;  // min distance between a topic and any non-topic node
const TOPIC_TOPIC_MIN_DIST = 50; // min distance between two topics

// ── Strategy interface ────────────────────────────────────────────────

/**
 * Determines the ordering of children within a compound node.
 *
 * Returns children as layers (arrays of ID arrays):
 *   - Single layer  → strip packing chooses rows automatically.
 *   - Multiple layers → each layer is rendered as one forced row (Sugiyama).
 *
 * @param childIds      IDs of direct children of the compound.
 * @param siblingEdges  Directed edges between those children.
 *                      Currently unused; exposed for a future Sugiyama
 *                      implementation that derives topological layers from
 *                      directed topic edges.
 */
export type ChildOrderStrategy = (
  childIds: string[],
  siblingEdges: Array<{ source: string; target: string }>,
) => string[][];

/** Default: single layer, strip packing finds the optimal row arrangement. */
export const packOnlyStrategy: ChildOrderStrategy = (ids) => [ids];

// ── Internal types ────────────────────────────────────────────────────

interface LayoutRect {
  w: number;
  h: number;
}

interface ChildPlacement {
  id: string;
  x: number; // offset from parent content-area origin
  y: number;
  w: number;
  h: number;
}

// ── Entry point ───────────────────────────────────────────────────────

export function compoundLayout(
  cy: cytoscape.Core,
  strategy: ChildOrderStrategy = packOnlyStrategy,
): void {
  const nodes = cy.nodes();
  if (nodes.length === 0) return;

  // ── Build maps ──────────────────────────────────────────────────────

  const childrenMap = new Map<string | null, string[]>();
  const nodeMap = new Map<string, cytoscape.NodeSingular>();

  nodes.forEach((n: cytoscape.NodeSingular) => {
    const id = n.id();
    nodeMap.set(id, n);
    const parentId: string | null = n.data("parent") ?? null;
    const list = childrenMap.get(parentId) ?? [];
    list.push(id);
    childrenMap.set(parentId, list);
  });

  // Directed sibling edges per parent, passed to strategy for Sugiyama use
  const siblingEdgesMap = new Map<
    string | null,
    Array<{ source: string; target: string }>
  >();
  cy.edges().forEach((e: cytoscape.EdgeSingular) => {
    const sp: string | null = e.source().data("parent") ?? null;
    const tp: string | null = e.target().data("parent") ?? null;
    if (sp === tp) {
      const list = siblingEdgesMap.get(sp) ?? [];
      list.push({ source: e.source().id(), target: e.target().id() });
      siblingEdgesMap.set(sp, list);
    }
  });

  // ── Bottom-up size computation ──────────────────────────────────────

  const sizeCache = new Map<string, LayoutRect>();
  // Per compound: one entry per layer — { yOffset, placements }
  const compoundLayersCache = new Map<
    string,
    Array<{ yOffset: number; placements: ChildPlacement[] }>
  >();

  function leafSize(node: cytoscape.NodeSingular): LayoutRect {
    const label: string = node.data("label") ?? "";
    const lines = label.split("\n");
    const maxLen = Math.max(...lines.map((l) => l.length), 1);
    return {
      w: Math.max(DEFAULT_LEAF_W, maxLen * 7 + 24),
      h: Math.max(DEFAULT_LEAF_H, lines.length * 18 + 16),
    };
  }

  function computeSize(id: string): LayoutRect {
    const cached = sizeCache.get(id);
    if (cached) return cached;

    const children = childrenMap.get(id);
    if (!children || children.length === 0) {
      const size = leafSize(nodeMap.get(id)!);
      sizeCache.set(id, size);
      return size;
    }

    // Recurse before using child sizes
    children.forEach(computeSize);

    const sibEdges = siblingEdgesMap.get(id) ?? [];
    const layers = strategy(children, sibEdges);
    const multiLayer = layers.length > 1;

    const layerEntries: Array<{ yOffset: number; placements: ChildPlacement[] }> = [];
    let curY = 0;
    let maxContentW = 0;

    for (const layer of layers) {
      const items = layer.map((cid) => ({ id: cid, ...sizeCache.get(cid)! }));
      // Multi-layer (Sugiyama): forced single row per layer.
      // Single layer (pack-only): strip packing chooses rows.
      const placements = multiLayer ? packRow(items) : stripPack(items);
      const { w, h } = packBounds(placements);
      layerEntries.push({ yOffset: curY, placements });
      curY += h + GAP_Y;
      maxContentW = Math.max(maxContentW, w);
    }

    const size: LayoutRect = {
      w: maxContentW + 2 * PADDING_X,
      h: curY - GAP_Y + PADDING_TOP + PADDING_BOTTOM,
    };

    compoundLayersCache.set(id, layerEntries);
    sizeCache.set(id, size);
    return size;
  }

  const roots = childrenMap.get(null) ?? [];
  roots.forEach(computeSize);

  // ── Top-down positioning ────────────────────────────────────────────

  const rootItems = roots.map((id) => ({ id, ...sizeCache.get(id)! }));
  const rootPlacements = stripPack(rootItems);

  function positionNode(id: string, originX: number, originY: number): void {
    const node = nodeMap.get(id);
    if (!node) return;
    const { w, h } = sizeCache.get(id)!;
    node.position({ x: originX + w / 2, y: originY + h / 2 });

    const layers = compoundLayersCache.get(id);
    if (layers) {
      for (const { yOffset, placements } of layers) {
        for (const p of placements) {
          positionNode(
            p.id,
            originX + PADDING_X + p.x,
            originY + PADDING_TOP + yOffset + p.y,
          );
        }
      }
    }
  }

  for (const rp of rootPlacements) {
    positionNode(rp.id, rp.x, rp.y);
  }

  // ── Post-processing ─────────────────────────────────────────────────

  positionTopics(cy);
  snapLcnMarkers(cy);
}

// ── Packing helpers ───────────────────────────────────────────────────

/** Place all items left-to-right in a single row (no wrapping). */
function packRow(
  items: Array<{ id: string; w: number; h: number }>,
): ChildPlacement[] {
  return greedyPack(items, Infinity);
}

/**
 * Strip packing: try several candidate widths, pick the one whose
 * output bounding box aspect ratio is closest to TARGET_ASPECT.
 */
function stripPack(
  items: Array<{ id: string; w: number; h: number }>,
): ChildPlacement[] {
  if (items.length === 0) return [];
  if (items.length === 1) return [{ ...items[0], x: 0, y: 0 }];

  const totalArea = items.reduce((s, c) => s + c.w * c.h, 0);
  const maxItemW = Math.max(...items.map((c) => c.w));
  const sqrtArea = Math.sqrt(totalArea);
  const TARGET_ASPECT = 1.4; // slightly wider than tall

  // Candidate widths: from single-column to 2.6× square
  const candidates = [0.7, 0.9, 1.1, 1.4, 1.7, 2.1, 2.6].map((f) =>
    Math.max(maxItemW, sqrtArea * f),
  );

  let best: ChildPlacement[] = greedyPack(items, candidates[0]);
  let bestScore = Infinity;

  for (const w of candidates) {
    const placements = greedyPack(items, w);
    const bounds = packBounds(placements);
    const aspect = bounds.h > 0 ? bounds.w / bounds.h : TARGET_ASPECT;
    const score = Math.abs(Math.log(aspect / TARGET_ASPECT));
    if (score < bestScore) {
      bestScore = score;
      best = placements;
    }
  }

  return best;
}

function greedyPack(
  items: Array<{ id: string; w: number; h: number }>,
  maxW: number,
): ChildPlacement[] {
  const placements: ChildPlacement[] = [];
  let curX = 0,
    curY = 0,
    rowH = 0;
  for (const item of items) {
    if (curX > 0 && curX + item.w > maxW) {
      curY += rowH + GAP_Y;
      curX = 0;
      rowH = 0;
    }
    placements.push({ id: item.id, x: curX, y: curY, w: item.w, h: item.h });
    curX += item.w + GAP_X;
    rowH = Math.max(rowH, item.h);
  }
  return placements;
}

function packBounds(placements: ChildPlacement[]): LayoutRect {
  if (placements.length === 0) return { w: 0, h: 0 };
  return {
    w: Math.max(...placements.map((p) => p.x + p.w)),
    h: Math.max(...placements.map((p) => p.y + p.h)),
  };
}

// ── Topic post-processing ─────────────────────────────────────────────

function positionTopics(cy: cytoscape.Core): void {
  const topics = cy.nodes('[type="topic"]');
  if (topics.length === 0) return;

  // Initial placement: centroid of connected non-topic nodes
  topics.forEach((t: cytoscape.NodeSingular) => {
    const neighbors = t.neighborhood().nodes().not('[type="topic"]');
    if (neighbors.length === 0) return;
    let cx = 0, cy2 = 0;
    neighbors.forEach((n: cytoscape.NodeSingular) => {
      const p = n.position();
      cx += p.x;
      cy2 += p.y;
    });
    t.position({ x: cx / neighbors.length, y: cy2 / neighbors.length });
  });

  // Iterative repulsion: push topics away from nodes and other topics.
  // Topics are mobile; non-topic nodes are fixed anchors.
  const nonTopics = cy.nodes().not('[type="topic"]');
  const topicArr: cytoscape.NodeSingular[] = [];
  topics.forEach((t: cytoscape.NodeSingular) => { topicArr.push(t); });

  for (let iter = 0; iter < 60; iter++) {
    let moved = false;

    for (const t of topicArr) {
      let fx = 0, fy = 0;

      // Repulsion from non-topic nodes (fixed)
      nonTopics.forEach((n: cytoscape.NodeSingular) => {
        const tp = t.position(), np = n.position();
        const dx = tp.x - np.x, dy = tp.y - np.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < TOPIC_NODE_MIN_DIST && dist > 0.01) {
          const mag = (TOPIC_NODE_MIN_DIST - dist) / dist;
          fx += dx * mag;
          fy += dy * mag;
        }
      });

      // Repulsion from other topics (mobile — use current positions)
      for (const other of topicArr) {
        if (other === t) continue;
        const tp = t.position(), op = other.position();
        const dx = tp.x - op.x, dy = tp.y - op.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < TOPIC_TOPIC_MIN_DIST && dist > 0.01) {
          const mag = (TOPIC_TOPIC_MIN_DIST - dist) / (2 * dist);
          fx += dx * mag;
          fy += dy * mag;
        }
      }

      if (Math.abs(fx) > 0.1 || Math.abs(fy) > 0.1) {
        const tp = t.position();
        t.position({ x: tp.x + fx, y: tp.y + fy });
        moved = true;
      }
    }

    if (!moved) break;
  }
}

// ── LCN marker snap ───────────────────────────────────────────────────

/**
 * Snap each load_composable_node marker to the edge of its parent compound
 * facing the target lcn_wrapper, minimising the visible edge length.
 */
function snapLcnMarkers(cy: cytoscape.Core): void {
  cy.nodes('[type="load_composable_node"]').forEach(
    (marker: cytoscape.NodeSingular) => {
      const loadEdge = marker.connectedEdges('[type="load_target"]');
      if (loadEdge.length === 0) return;

      const wrapper = loadEdge.connectedNodes('[type="lcn_wrapper"]').first();
      if (wrapper.length === 0) return;

      const parent = marker.parent();
      if (parent.length === 0) return;

      const wPos = wrapper.position();
      const bb = parent.boundingBox();
      const pCx = (bb.x1 + bb.x2) / 2;
      const pCy = (bb.y1 + bb.y2) / 2;

      // Direction from parent centre toward the wrapper
      const dx = wPos.x - pCx;
      const dy = wPos.y - pCy;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < 1) return;

      // Intersect the ray with the inset content boundary of the parent
      const inset = 24;
      const halfW = Math.max(0, (bb.x2 - bb.x1) / 2 - inset);
      const halfH = Math.max(0, (bb.y2 - bb.y1) / 2 - inset);
      const ndx = dx / dist,
        ndy = dy / dist;

      let t = Infinity;
      if (Math.abs(ndx) > 0.001) t = Math.min(t, halfW / Math.abs(ndx));
      if (Math.abs(ndy) > 0.001) t = Math.min(t, halfH / Math.abs(ndy));
      if (!isFinite(t)) return;

      marker.position({ x: pCx + ndx * t, y: pCy + ndy * t });
    },
  );
}
