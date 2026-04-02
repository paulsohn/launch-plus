import type { ParamEntry, RemapEntry } from "../types.generated";

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
  params?: ParamEntry[];
  remaps?: RemapEntry[];
}

interface Props {
  detail: NodeDetail | null;
  onClose: () => void;
}

export function DetailPanel({ detail, onClose }: Props) {
  if (!detail) return null;

  return (
    <div id="detail-panel">
      <div id="detail-header">
        <span id="detail-title">
          {detail.fqn || detail.fullName || detail.name || "Node"}
        </span>
        <button id="detail-close" onClick={onClose}>
          &times;
        </button>
      </div>
      <div id="detail-body">
        <h3>Info</h3>
        {detail.type && <Field label="Type" value={detail.type} />}
        {detail.package && <Field label="Package" value={detail.package} />}
        {detail.executable && (
          <Field label="Executable" value={detail.executable} />
        )}
        {detail.plugin && <Field label="Plugin" value={detail.plugin} />}
        {detail.name && <Field label="Name" value={detail.name} />}
        {detail.namespace && (
          <Field label="Namespace" value={detail.namespace} />
        )}
        {detail.fqn && <Field label="FQN" value={detail.fqn} />}
        {detail.cmd && <Field label="Command" value={detail.cmd} />}
        {detail.target && <Field label="Target" value={detail.target} />}
        {detail.fullName && <Field label="Topic" value={detail.fullName} />}

        {detail.params && detail.params.length > 0 && (
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

        {detail.remaps && detail.remaps.length > 0 && (
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
