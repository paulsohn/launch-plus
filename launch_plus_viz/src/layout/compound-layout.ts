/**
 * Custom recursive compound layout for Cytoscape.js.
 *
 * Algorithm:
 * 1. Build a tree of parent-child relationships from Cytoscape nodes.
 * 2. Bottom-up: compute sizes for each node. Leaf nodes use their
 *    rendered bounding box. Compound nodes lay out children in a
 *    flow-grid (left-to-right, wrapping) and derive their size.
 * 3. Top-down: position nodes using the computed offsets.
 */

import type cytoscape from "cytoscape";

const PADDING_X = 16;
const PADDING_TOP = 32; // extra top for compound label
const PADDING_BOTTOM = 16;
const GAP_X = 20;
const GAP_Y = 20;
const DEFAULT_LEAF_W = 120;
const DEFAULT_LEAF_H = 40;

interface LayoutRect {
  w: number;
  h: number;
}

interface ChildPlacement {
  id: string;
  x: number; // offset relative to parent's content area origin
  y: number;
  w: number;
  h: number;
}

/**
 * Run the compound layout. Positions all nodes in `cy` using a
 * bottom-up size computation followed by top-down positioning.
 */
export function compoundLayout(cy: cytoscape.Core): void {
  const nodes = cy.nodes();
  if (nodes.length === 0) return;

  // Build parent -> children map
  const childrenMap = new Map<string | null, string[]>();
  const nodeMap = new Map<string, cytoscape.NodeSingular>();

  nodes.forEach((n) => {
    const id = n.id();
    nodeMap.set(id, n);
    const parentId: string | null = n.data("parent") ?? null;
    const list = childrenMap.get(parentId) ?? [];
    list.push(id);
    childrenMap.set(parentId, list);
  });

  // Memoized sizes
  const sizeCache = new Map<string, LayoutRect>();
  // Memoized child placements for compound nodes
  const placementCache = new Map<string, ChildPlacement[]>();

  /**
   * Estimate leaf node size from its label.
   */
  function leafSize(node: cytoscape.NodeSingular): LayoutRect {
    const label: string = node.data("label") ?? "";
    const lines = label.split("\n");
    const maxLineLen = Math.max(...lines.map((l) => l.length), 1);
    // Rough: ~7px per char, ~18px per line
    const w = Math.max(DEFAULT_LEAF_W, maxLineLen * 7 + 24);
    const h = Math.max(DEFAULT_LEAF_H, lines.length * 18 + 16);
    return { w, h };
  }

  /**
   * Compute size of a node (recursively for compounds).
   */
  function computeSize(id: string): LayoutRect {
    const cached = sizeCache.get(id);
    if (cached) return cached;

    const children = childrenMap.get(id);
    if (!children || children.length === 0) {
      const node = nodeMap.get(id)!;
      const size = leafSize(node);
      sizeCache.set(id, size);
      return size;
    }

    // Compute children sizes first
    const childSizes: { id: string; w: number; h: number }[] = [];
    for (const cid of children) {
      const cs = computeSize(cid);
      childSizes.push({ id: cid, w: cs.w, h: cs.h });
    }

    // Flow-grid layout: left-to-right, wrap at threshold
    const totalArea = childSizes.reduce((s, c) => s + c.w * c.h, 0);
    const maxRowW = Math.max(
      300,
      Math.sqrt(totalArea) * 1.8,
      // At least wide enough for the widest child
      ...childSizes.map((c) => c.w),
    );

    const placements: ChildPlacement[] = [];
    let curX = 0;
    let curY = 0;
    let rowH = 0;
    let maxX = 0;

    for (const child of childSizes) {
      if (curX > 0 && curX + child.w > maxRowW) {
        // Wrap to next row
        curY += rowH + GAP_Y;
        curX = 0;
        rowH = 0;
      }
      placements.push({ id: child.id, x: curX, y: curY, w: child.w, h: child.h });
      curX += child.w + GAP_X;
      rowH = Math.max(rowH, child.h);
      maxX = Math.max(maxX, curX - GAP_X);
    }

    const contentW = maxX;
    const contentH = curY + rowH;
    const compoundW = contentW + 2 * PADDING_X;
    const compoundH = contentH + PADDING_TOP + PADDING_BOTTOM;

    placementCache.set(id, placements);
    const size = { w: compoundW, h: compoundH };
    sizeCache.set(id, size);
    return size;
  }

  // Compute sizes for all root nodes
  const roots = childrenMap.get(null) ?? [];
  for (const rid of roots) {
    computeSize(rid);
  }

  // Flow-grid layout for root level
  const rootSizes = roots.map((id) => ({ id, ...sizeCache.get(id)! }));
  const totalRootArea = rootSizes.reduce((s, r) => s + r.w * r.h, 0);
  const rootMaxW = Math.max(800, Math.sqrt(totalRootArea) * 1.5);

  const rootPlacements: ChildPlacement[] = [];
  let rx = 0;
  let ry = 0;
  let rRowH = 0;

  for (const root of rootSizes) {
    if (rx > 0 && rx + root.w > rootMaxW) {
      ry += rRowH + GAP_Y * 2;
      rx = 0;
      rRowH = 0;
    }
    rootPlacements.push({ id: root.id, x: rx, y: ry, w: root.w, h: root.h });
    rx += root.w + GAP_X * 2;
    rRowH = Math.max(rRowH, root.h);
  }

  // Position nodes top-down
  function positionNode(id: string, originX: number, originY: number): void {
    const node = nodeMap.get(id);
    if (!node) return;

    const size = sizeCache.get(id)!;
    // Position at center of allocated area
    node.position({ x: originX + size.w / 2, y: originY + size.h / 2 });

    // Position children if compound
    const placements = placementCache.get(id);
    if (placements) {
      const contentOriginX = originX + PADDING_X;
      const contentOriginY = originY + PADDING_TOP;
      for (const p of placements) {
        positionNode(p.id, contentOriginX + p.x, contentOriginY + p.y);
      }
    }
  }

  for (const rp of rootPlacements) {
    positionNode(rp.id, rp.x, rp.y);
  }
}
