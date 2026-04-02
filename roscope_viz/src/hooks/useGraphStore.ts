/**
 * Central state management for graph snapshots.
 *
 * Manages the catalog of viz-ids and their snapshots,
 * tracks which snapshot is currently selected for display,
 * and handles WebSocket messages.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { GraphData, ServerMessage, Snapshot } from "../types.generated";
import { VizWebSocket, getWsUrl } from "../ws";

export interface GraphStore {
  /** All snapshots grouped by viz-id */
  catalog: Map<string, Snapshot[]>;
  /** Currently displayed graph (if any) */
  activeGraph: GraphData | null;
  /** Currently selected viz-id */
  activeVizId: string | null;
  /** Currently selected snapshot index within the viz-id */
  activeSnapshotIndex: number | null;
  /** Whether we're connected to the server */
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
  const wsRef = useRef<VizWebSocket | null>(null);

  const handleMessage = useCallback((msg: ServerMessage) => {
    switch (msg.type) {
      case "catalog": {
        const newCatalog = new Map<string, Snapshot[]>();
        for (const [vizId, snapshots] of Object.entries(msg.snapshots)) {
          newCatalog.set(vizId, snapshots);
        }
        setCatalog(newCatalog);
        setConnected(true);

        // Auto-select the latest snapshot of the first viz-id
        const firstVizId = newCatalog.keys().next().value;
        if (firstVizId) {
          const snapshots = newCatalog.get(firstVizId)!;
          setActiveVizId(firstVizId);
          setActiveSnapshotIndex(snapshots.length - 1);
        }
        break;
      }
      case "snapshot": {
        setCatalog((prev) => {
          const next = new Map(prev);
          const existing = next.get(msg.vizId) || [];
          next.set(msg.vizId, [...existing, msg.snapshot]);
          return next;
        });
        // Auto-select newly pushed snapshot
        setActiveVizId(msg.vizId);
        setCatalog((prev) => {
          const snapshots = prev.get(msg.vizId);
          if (snapshots) {
            setActiveSnapshotIndex(snapshots.length - 1);
          }
          return prev;
        });
        break;
      }
      case "removed": {
        setCatalog((prev) => {
          const next = new Map(prev);
          const existing = next.get(msg.vizId);
          if (existing) {
            const filtered = existing.filter(
              (s) => s.timestamp !== msg.timestamp,
            );
            if (filtered.length === 0) {
              next.delete(msg.vizId);
            } else {
              next.set(msg.vizId, filtered);
            }
          }
          return next;
        });
        break;
      }
    }
  }, []);

  useEffect(() => {
    const ws = new VizWebSocket(getWsUrl(), handleMessage);
    wsRef.current = ws;
    return () => ws.close();
  }, [handleMessage]);

  const selectSnapshot = useCallback((vizId: string, index: number) => {
    setActiveVizId(vizId);
    setActiveSnapshotIndex(index);
  }, []);

  const removeSnapshot = useCallback(
    (vizId: string, timestamp: string) => {
      wsRef.current?.send({ type: "remove", vizId, timestamp });
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
