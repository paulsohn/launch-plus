import type { ArgEntry, ParamEntry, RemapEntry } from "../types.generated";

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
  onClose: () => void;
}

export function DetailPanel({ detail, onClose }: Props) {
  const title = !detail
    ? ""
    : detail.type === "group"
      ? detail.source || "Group"
      : detail.fqn || detail.fullName || detail.name || "Vertex";

  return (
    <div id="detail-panel">
      <div id="detail-header">
        <span id="detail-title">{title || "Nothing selected"}</span>
        {detail && (
          <button id="detail-close" onClick={onClose}>
            &times;
          </button>
        )}
      </div>
      <div id="detail-body">
        {!detail && <p style={{ color: "#8888aa", fontSize: "12px" }}>Click or drag a vertex to inspect it.</p>}
        {detail && <h3>Info</h3>}
        {detail?.type && <Field label="Type" value={detail.type} />}
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

        {detail?.includeArgs && Object.keys(detail.includeArgs).length > 0 && (
          <>
            <h3>Include Args ({Object.keys(detail.includeArgs).length})</h3>
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(detail.includeArgs).map(([k, v]) => (
                  <tr key={k}>
                    <td>{k}</td>
                    <td>{v}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}

        {detail?.args && detail.args.length > 0 && (
          <>
            <h3>Declared Args ({detail.args.length})</h3>
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Value</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {detail.args.map((a, i) => (
                  <tr key={i}>
                    <td>{a.name}</td>
                    <td>{a.value}</td>
                    <td className="arg-badge">
                      {a.isDefault ? "default" : "set"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}

        {detail?.params && detail.params.length > 0 && (
          <>
            <h3>Parameters ({detail.params.length})</h3>
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                {detail.params.map((p, i) => (
                  <tr key={i}>
                    <td>{p.name}</td>
                    <td>{p.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
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
