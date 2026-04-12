interface Props {
  visible: boolean;
  message?: string;
}

export function LoadingOverlay({ visible, message }: Props) {
  if (!visible) return null;

  return (
    <div className="loading-overlay">
      <div className="spinner" />
      <p>{message || "Computing layout..."}</p>
    </div>
  );
}
