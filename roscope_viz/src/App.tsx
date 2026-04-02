import { useCallback, useRef, useState } from "react";
import type cytoscape from "cytoscape";
import { GraphView } from "./components/GraphView";
import { Toolbar } from "./components/Toolbar";
import { DetailPanel } from "./components/DetailPanel";
import { LoadingOverlay } from "./components/LoadingOverlay";
import { useGraphStore } from "./hooks/useGraphStore";

export function App() {
  const store = useGraphStore();
  const cyRef = useRef<cytoscape.Core | null>(null);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);

  const handleNodeTap = useCallback((data: Record<string, unknown>) => {
    setDetail(data);
  }, []);

  const handleBackgroundTap = useCallback(() => {
    setDetail(null);
  }, []);

  if (!store.connected) {
    return (
      <LoadingOverlay visible={true} message="Connecting to server..." />
    );
  }

  if (!store.activeGraph) {
    return (
      <div className="loading-overlay">
        <p>No graph data available. Run roscope resolve --visualize.</p>
      </div>
    );
  }

  return (
    <>
      <Toolbar cyRef={cyRef} />
      <GraphView
        graph={store.activeGraph}
        cyRef={cyRef}
        onNodeTap={handleNodeTap}
        onBackgroundTap={handleBackgroundTap}
      />
      <DetailPanel
        detail={detail as Parameters<typeof DetailPanel>[0]["detail"]}
        onClose={() => {
          setDetail(null);
          cyRef.current?.elements().removeClass("highlighted faded");
        }}
      />
    </>
  );
}
