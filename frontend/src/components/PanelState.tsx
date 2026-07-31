import { IconEmpty } from "@arco-design/web-react/icon";
import type { ReactNode } from "react";
import { getErrorMessage } from "@/utils/errors";
import "./PanelState.css";

/**
 * Pending / error / empty guard for a data panel.
 *
 * A failed query must stay distinguishable from "no data": the error branch
 * announces itself through role="alert" and always offers a retry, so a 500 is
 * never rendered as an empty list.
 */
export function PanelState({
  pending,
  error,
  empty,
  emptyTitle,
  emptyDescription,
  onRetry,
  pendingLabel = "正在加载数据…",
  children,
}: {
  pending: boolean;
  error: unknown;
  empty: boolean;
  emptyTitle: string;
  emptyDescription: string;
  onRetry: () => void;
  pendingLabel?: string;
  children: ReactNode;
}) {
  if (pending) {
    return (
      <div className="ag-panel-state" role="status">
        <span className="ag-panel-state-spinner" aria-hidden="true" />
        {pendingLabel}
      </div>
    );
  }
  if (error) {
    return (
      <div className="ag-panel-state is-error" role="alert">
        <strong>数据加载失败</strong>
        <span>{getErrorMessage(error)}</span>
        <button type="button" onClick={onRetry}>
          重新加载
        </button>
      </div>
    );
  }
  if (empty) {
    return (
      <div className="ag-panel-state is-empty" role="status">
        <span className="ag-panel-state-empty-mark" aria-hidden="true">
          <IconEmpty />
        </span>
        <strong>{emptyTitle}</strong>
        <span>{emptyDescription}</span>
      </div>
    );
  }
  return children;
}
