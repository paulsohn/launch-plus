import type { MouseEvent as ReactMouseEvent } from "react";
import type { ArgEntry, GraphData, ParamEntry, RemapEntry } from "../types.generated";

interface NodeDetail {
  type?: string;
  package?: string;
  executable?: string;
  plugin?: string;
  name?: string;
  namespace?: string;
  fqn?: string;
  cmd?: string;
  target?: string;
  fullName?: string;
  source?: string;
  params?: ParamEntry[];
  remaps?: RemapEntry[];
  args?: ArgEntry[];
  includeArgs?: Record<string, string> | null;
}

interface Props {
  detail: NodeDetail | null;
  graph: GraphData;
  width: number;
  onResizeStart: (e: ReactMouseEvent) => void;
  onClose: () => void;
}

export function DetailPanel({ detail, graph, width, onResizeStart, onClose }: Props) {
  const displayType: Record<string, string> = {
    group: "Include boundary",
    lcn_wrapper: "LoadComposableNodes call",
    container: "Node container",
    node: "Node",
    lifecycle_node: "Lifecycle node",
    composable_node: "Composable node",
    load_composable_node: "Load composable node",
    executable: "Executable",
    topic: "Topic",
  };

  const title = !detail
    ? ""
    : detail.type === "group" || detail.type === "lcn_wrapper"
      ? detail.source || displayType[detail.type as string] || detail.type
      : detail.fqn || detail.fullName || detail.name || "Vertex";

  return (
    <div id="detail-panel" style={{ width }}>
      <div id="detail-resize-handle" onMouseDown={onResizeStart} />
      <div id="detail-header">
        <span id="detail-title">{title || "Nothing selected"}</span>
        {detail && (
          <button id="detail-close" onClick={onClose} aria-label="Close details">
            &times;
          </button>
        )}
      </div>
      <div id="detail-body">
        {!detail && <GraphStats graph={graph} />}
        {detail && <h3>Info</h3>}
        {detail?.type && (
          <Field label="Type" value={displayType[detail.type] ?? detail.type} />
        )}
        {detail?.source && <Field label="Source" value={detail.source} />}
        {detail?.package && <Field label="Package" value={detail.package} />}
        {detail?.executable && (
          <Field label="Executable" value={detail.executable} />
        )}
        {detail?.plugin && <Field label="Plugin" value={detail.plugin} />}
        {detail?.name && <Field label="Name" value={detail.name} />}
        {detail?.namespace && (
          <Field label="Namespace" value={detail.namespace} />
        )}
        {detail?.fqn && <Field label="FQN" value={detail.fqn} />}
        {detail?.cmd && <Field label="Command" value={detail.cmd} />}
        {detail?.target && <Field label="Target" value={detail.target} />}
        {detail?.fullName && <Field label="Topic" value={detail.fullName} />}

        {detail?.args && detail.args.length > 0 && (
          <>
            <h3>Declared Args ({detail.args.length})</h3>
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Value</th>
                  <th>Set</th>
                </tr>
              </thead>
              <tbody>
                {detail.args.map((a, i) => (
                  <tr key={i}>
                    <td>{a.name}</td>
                    <td>{a.value}</td>
                    <td className="arg-set-cell">
                      <input
                        type="checkbox"
                        readOnly
                        checked={!a.isDefault}
                        title={a.isDefault ? "Default value" : "Explicitly set"}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}

        {detail?.params && detail.params.length > 0 && (
          <ParamsTable params={detail.params} />
        )}

        {detail?.remaps && detail.remaps.length > 0 && (
          <>
            <h3>Remaps ({detail.remaps.length})</h3>
            <table>
              <thead>
                <tr>
                  <th>From</th>
                  <th>To</th>
                </tr>
              </thead>
              <tbody>
                {detail.remaps.map((r, i) => (
                  <tr key={i}>
                    <td>{r.from}</td>
                    <td>{r.to}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="field">
      <span className="field-label">{label}:</span> {value}
    </div>
  );
}

function ParamsTable({ params }: { params: ParamEntry[] }) {
  // Determine which entries win (last occurrence of each name wins).
  const winnerIndices = new Set<number>();
  const seen = new Set<string>();
  for (let i = params.length - 1; i >= 0; i--) {
    if (!seen.has(params[i].name)) {
      seen.add(params[i].name);
      winnerIndices.add(i);
    }
  }
  const activeCount = winnerIndices.size;

  return (
    <>
      <h3>Parameters ({activeCount})</h3>
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Value</th>
          </tr>
        </thead>
        <tbody>
          {params.map((p, i) => {
            const overridden = !winnerIndices.has(i);
            return (
              <tr key={i} style={overridden ? { opacity: 0.35 } : undefined}>
                <td style={overridden ? { textDecoration: "line-through" } : undefined}>{p.name}</td>
                <td style={overridden ? { textDecoration: "line-through" } : undefined}>{p.value}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

function StatRow({ label, value }: { label: string; value: number }) {
  return (
    <tr>
      <td style={{ color: "#8888aa", paddingRight: "12px" }}>{label}</td>
      <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{value}</td>
    </tr>
  );
}

function GraphStats({ graph }: { graph: GraphData }) {
  const nodes = graph.nodes;
  const groups = graph.groups;

  const standaloneCount = nodes.filter((n) => n.type === "node" || n.type === "lifecycle_node").length;
  const lifecycleCount = nodes.filter((n) => n.type === "lifecycle_node").length;
  const composableCount = nodes.filter((n) => n.type === "composable_node").length;
  const totalNodeCount = standaloneCount + composableCount;
  const containerCount = nodes.filter((n) => n.type === "container").length;
  const executableCount = nodes.filter((n) => n.type === "executable").length;
  const includeCount = groups.filter((g) => g.groupType === "include").length;
  const lcnCallCount = groups.filter((g) => g.groupType === "lcn_wrapper").length;
  const topicCount = graph.topics.length;
  const remapCount = graph.edges.filter((e) => e.type === "remap").length;
  const paramCount = nodes.reduce(
    (s, n) => s + new Set(n.params?.map((p) => p.name) ?? []).size,
    0,
  );
  const packageCount = new Set(nodes.map((n) => n.package).filter(Boolean)).size;

  const { package: pkg, launcher, timestamp } = graph.metadata;
  const ts = new Date(timestamp).toLocaleString();

  return (
    <>
      <div style={{ color: "#8888aa", fontSize: "11px", marginBottom: "12px" }}>
        <div style={{ marginBottom: "2px" }}>{pkg} / {launcher}</div>
        <div>{ts}</div>
      </div>

      <h3>Vertices</h3>
      <table style={{ width: "100%" }}>
        <tbody>
          <StatRow label="Nodes (total)" value={totalNodeCount} />
          <StatRow label="↳ Standalone" value={standaloneCount} />
          {lifecycleCount > 0 && <StatRow label="  ↳ Lifecycle" value={lifecycleCount} />}
          {composableCount > 0 && <StatRow label="↳ Composable" value={composableCount} />}
          {containerCount > 0 && <StatRow label="Node containers" value={containerCount} />}
          {lcnCallCount > 0 && <StatRow label="LoadComposableNodes calls" value={lcnCallCount} />}
          {executableCount > 0 && <StatRow label="Executables" value={executableCount} />}
          <StatRow label="Tracked topics" value={topicCount} />
        </tbody>
      </table>

      <h3>Structure</h3>
      <table style={{ width: "100%" }}>
        <tbody>
          <StatRow label="Include files" value={includeCount} />
          <StatRow label="Packages" value={packageCount} />
        </tbody>
      </table>

      <h3>Connections</h3>
      <table style={{ width: "100%" }}>
        <tbody>
          <StatRow label="Remaps" value={remapCount} />
          <StatRow label="Parameters" value={paramCount} />
        </tbody>
      </table>

      <p style={{ color: "#555577", fontSize: "11px", marginTop: "16px" }}>
        Click or drag a vertex to inspect it.
      </p>
    </>
  );
}
