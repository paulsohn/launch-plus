import { useCallback, useEffect, useRef, useState } from "react";
import type cytoscape from "cytoscape";
import { GraphView } from "./components/GraphView";
import { Toolbar } from "./components/Toolbar";
import { DetailPanel } from "./components/DetailPanel";
import { LoadingOverlay } from "./components/LoadingOverlay";
import { useGraphStore } from "./hooks/useGraphStore";

const SIDEBAR_MIN = 220;
const SIDEBAR_MAX = 700;
const SIDEBAR_DEFAULT = 380;

export function App() {
  const store = useGraphStore();
  const cyRef = useRef<cytoscape.Core | null>(null);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [sidebarWidth, setSidebarWidth] = useState(SIDEBAR_DEFAULT);
  const resizing = useRef(false);
  const resizeStartX = useRef(0);
  const resizeStartWidth = useRef(0);

  const handleResizeStart = useCallback((e: React.MouseEvent) => {
    resizing.current = true;
    resizeStartX.current = e.clientX;
    resizeStartWidth.current = sidebarWidth;
    e.preventDefault();
  }, [sidebarWidth]);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!resizing.current) return;
      const delta = resizeStartX.current - e.clientX;
      setSidebarWidth(Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, resizeStartWidth.current + delta)));
    };
    const onUp = () => { resizing.current = false; };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

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
        sidebarWidth={sidebarWidth}
      />
      <DetailPanel
        detail={detail as Parameters<typeof DetailPanel>[0]["detail"]}
        graph={store.activeGraph}
        width={sidebarWidth}
        onResizeStart={handleResizeStart}
        onClose={() => {
          setDetail(null);
          cyRef.current?.elements().removeClass("highlighted faded");
        }}
      />
    </>
  );
}
