/**
 * Central state management for graph snapshots.
 *
 * Polls /api/catalog to discover cached snapshots, tracks which
 * snapshot is currently selected for display.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { GraphData, Snapshot } from "../types.generated";

const POLL_INTERVAL_MS = 2000;

export interface GraphStore {
  /** All snapshots grouped by viz-id */
  catalog: Map<string, Snapshot[]>;
  /** Currently displayed graph (if any) */
  activeGraph: GraphData | null;
  /** Currently selected viz-id */
  activeVizId: string | null;
  /** Currently selected snapshot index within the viz-id */
  activeSnapshotIndex: number | null;
  /** Whether we've received data from the server */
  connected: boolean;
  /** Select a snapshot to display */
  selectSnapshot: (vizId: string, index: number) => void;
  /** Remove a snapshot */
  removeSnapshot: (vizId: string, timestamp: string) => void;
}

export function useGraphStore(): GraphStore {
  const [catalog, setCatalog] = useState<Map<string, Snapshot[]>>(new Map());
  const [activeVizId, setActiveVizId] = useState<string | null>(null);
  const [activeSnapshotIndex, setActiveSnapshotIndex] = useState<number | null>(
    null,
  );
  const [connected, setConnected] = useState(false);
  // Fingerprint of the last catalog response — skip setState if unchanged
  const prevFingerprint = useRef("");
  const prevSnapshotCount = useRef(0);

  // Poll /api/catalog
  useEffect(() => {
    let active = true;

    const poll = async () => {
      try {
        const resp = await fetch("/api/catalog");
        if (!resp.ok) return;
        const text = await resp.text();
        if (!active) return;

        // Build a fingerprint from viz-ids + timestamps to avoid
        // re-rendering when the catalog hasn't changed.
        const data: Record<string, Snapshot[]> = JSON.parse(text);
        const fingerprint = Object.entries(data)
          .flatMap(([vid, snaps]) => snaps.map((s) => `${vid}@${s.timestamp}`))
          .sort()
          .join("|");

        // Mark connected as soon as any successful response arrives,
        // regardless of whether the catalog changed.
        setConnected(true);

        if (fingerprint === prevFingerprint.current) return;
        prevFingerprint.current = fingerprint;

        const newCatalog = new Map<string, Snapshot[]>();
        let totalSnapshots = 0;
        for (const [vizId, snapshots] of Object.entries(data)) {
          newCatalog.set(vizId, snapshots);
          totalSnapshots += snapshots.length;
        }

        setCatalog(newCatalog);

        // Auto-select latest snapshot when new data arrives
        if (totalSnapshots > prevSnapshotCount.current) {
          let latestVizId: string | null = null;
          let latestTs = "";
          for (const [vizId, snapshots] of newCatalog) {
            const last = snapshots[snapshots.length - 1];
            if (last && last.timestamp > latestTs) {
              latestTs = last.timestamp;
              latestVizId = vizId;
            }
          }
          if (latestVizId) {
            const snaps = newCatalog.get(latestVizId)!;
            setActiveVizId(latestVizId);
            setActiveSnapshotIndex(snaps.length - 1);
          }
        }
        prevSnapshotCount.current = totalSnapshots;
      } catch {
        // Server not reachable — keep trying
      }
    };

    poll();
    const id = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, []);

  const selectSnapshot = useCallback((vizId: string, index: number) => {
    setActiveVizId(vizId);
    setActiveSnapshotIndex(index);
  }, []);

  const removeSnapshot = useCallback(
    (vizId: string, timestamp: string) => {
      fetch("/api/remove", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ vizId, timestamp }),
      }).catch(() => {});
    },
    [],
  );

  // Derive active graph from selection
  let activeGraph: GraphData | null = null;
  if (activeVizId && activeSnapshotIndex !== null) {
    const snapshots = catalog.get(activeVizId);
    if (snapshots && activeSnapshotIndex < snapshots.length) {
      activeGraph = snapshots[activeSnapshotIndex].graph;
    }
  }

  return {
    catalog,
    activeGraph,
    activeVizId,
    activeSnapshotIndex,
    connected,
    selectSnapshot,
    removeSnapshot,
  };
}
