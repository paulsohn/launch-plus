import type { MouseEvent as ReactMouseEvent } from "react";
import type { ArgEntry, ExtraArgEntry, GraphData, ParamEntry, RemapEntry } from "../types.generated";

interface NodeDetail {
  id?: string;
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
  connType?: string;
  source?: string;
  params?: ParamEntry[];
  remaps?: RemapEntry[];
  extraArgs?: ExtraArgEntry[];
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
  };

  const connFamilyLabel: Record<string, string> = {
    topic: "Topic",
    service: "Service",
    action: "Action",
    unknown: "Unknown",
  };

  function typeLabel(type: string, connType?: string): string {
    if (type === "connection") {
      const family = connFamilyLabel[connType ?? ""] ?? connType ?? "Unknown";
      return `Connection (${family})`;
    }
    return displayType[type] ?? type;
  }

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
          <Field label="Type" value={typeLabel(detail.type, detail.connType as string | undefined)} />
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
        {detail?.fullName && <Field label="Name" value={detail.fullName} />}
        {detail?.type === "connection" && (
          <ConnectionNodeGroups detail={detail} graph={graph} />
        )}

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

        {detail?.extraArgs && detail.extraArgs.length > 0 && (
          <>
            <h3>Extra Args ({detail.extraArgs.length})</h3>
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                {detail.extraArgs.map((ea, i) => (
                  <tr key={i}>
                    <td>{ea.name}</td>
                    <td>{ea.value}</td>
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

// Map protocol family to role labels (out = initiator, in = receiver)
const ROLE_LABELS: Record<string, { out: string; in: string }> = {
  topic: { out: "Publishers", in: "Subscriptions" },
  service: { out: "Clients", in: "Servers" },
  action: { out: "Clients", in: "Servers" },
};

function ConnectionNodeGroups({
  detail,
  graph,
}: {
  detail: NodeDetail;
  graph: GraphData;
}) {
  const connId = detail.id;
  if (!connId) return null;

  const nodeMap = new Map(graph.nodes.map((n) => [n.id, n]));

  const outNodes: { fqn: string; connType: string }[] = [];
  const inNodes: { fqn: string; connType: string }[] = [];
  const unknownNodes: { fqn: string }[] = [];

  for (const e of graph.edges) {
    if (e.type !== "remap") continue;
    let nodeId: string | undefined;
    let role: "out" | "in" | "unknown";

    if (e.target === connId) {
      nodeId = e.source;
      role = e.directed ? "out" : "unknown";
    } else if (e.source === connId) {
      nodeId = e.target;
      role = e.directed ? "in" : "unknown";
    } else {
      continue;
    }

    const n = nodeMap.get(nodeId);
    const fqn = n?.fqn ?? n?.name ?? nodeId;
    if (role === "out") outNodes.push({ fqn, connType: e.connType ?? "" });
    else if (role === "in") inNodes.push({ fqn, connType: e.connType ?? "" });
    else unknownNodes.push({ fqn });
  }

  const labels = ROLE_LABELS[detail.connType ?? ""] ?? null;
  const hasAny = outNodes.length + inNodes.length + unknownNodes.length > 0;
  if (!hasAny) return null;

  return (
    <>
      <h3>Connected nodes</h3>
      {outNodes.length > 0 && (
        <>
          <h4 style={{ margin: "4px 0 2px", color: "#8888aa", fontWeight: "normal" }}>
            {labels?.out ?? "Out"}
          </h4>
          <table style={{ width: "100%", wordBreak: "break-all" }}>
            <tbody>
              {outNodes.map((n, i) => <tr key={i}><td>{n.fqn}</td></tr>)}
            </tbody>
          </table>
        </>
      )}
      {inNodes.length > 0 && (
        <>
          <h4 style={{ margin: "4px 0 2px", color: "#8888aa", fontWeight: "normal" }}>
            {labels?.in ?? "In"}
          </h4>
          <table style={{ width: "100%", wordBreak: "break-all" }}>
            <tbody>
              {inNodes.map((n, i) => <tr key={i}><td>{n.fqn}</td></tr>)}
            </tbody>
          </table>
        </>
      )}
      {unknownNodes.length > 0 && (
        <>
          <h4 style={{ margin: "4px 0 2px", color: "#8888aa", fontWeight: "normal" }}>
            Unknown / Mismatched
          </h4>
          <table style={{ width: "100%", wordBreak: "break-all" }}>
            <tbody>
              {unknownNodes.map((n, i) => <tr key={i}><td>{n.fqn}</td></tr>)}
            </tbody>
          </table>
        </>
      )}
    </>
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
  const connectionCount = graph.connections.length;
  const topicConnCount = graph.connections.filter((c) => c.connType === "topic").length;
  const serviceConnCount = graph.connections.filter((c) => c.connType === "service").length;
  const actionConnCount = graph.connections.filter((c) => c.connType === "action").length;
  const unknownConnCount = graph.connections.filter((c) => c.connType === "unknown").length;
  const remapCount = graph.edges.filter((e) => e.type === "remap").length;
  const paramCount = nodes.reduce(
    (s, n) => s + new Set(n.params?.map((p) => p.name) ?? []).size,
    0,
  );
  const packageCount = new Set(nodes.map((n) => n.package).filter(Boolean)).size;

  const { package: pkg, launcher, timestamp, version } = graph.metadata;
  const ts = new Date(timestamp).toLocaleString();

  return (
    <>
      <div style={{ color: "#8888aa", fontSize: "11px", marginBottom: "12px" }}>
        <div style={{ marginBottom: "2px" }}>{pkg} / {launcher}</div>
        <div style={{ marginBottom: "2px" }}>{ts}</div>
        <div>roscope v{version}</div>
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
          <StatRow label="Connections (total)" value={connectionCount} />
          {topicConnCount > 0 && <StatRow label="↳ Topics" value={topicConnCount} />}
          {serviceConnCount > 0 && <StatRow label="↳ Services" value={serviceConnCount} />}
          {actionConnCount > 0 && <StatRow label="↳ Actions" value={actionConnCount} />}
          {unknownConnCount > 0 && <StatRow label="↳ Unknown" value={unknownConnCount} />}
        </tbody>
      </table>

      <h3>Structure</h3>
      <table style={{ width: "100%" }}>
        <tbody>
          <StatRow label="Include files" value={includeCount} />
          <StatRow label="Packages" value={packageCount} />
        </tbody>
      </table>

      <h3>Attributes</h3>
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
