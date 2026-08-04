import { statusLabel, statusTone } from "./format";
import "./statusChip.css";

export function StatusChip({
  status,
  labels,
}: {
  status: string;
  labels?: Readonly<Record<string, string>>;
}) {
  return (
    <span className={`ag-governance-status is-${statusTone(status)}`}>
      {statusLabel(status, labels)}
    </span>
  );
}
