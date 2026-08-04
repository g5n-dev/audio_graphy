import { IconClose, IconHistory } from "@arco-design/web-react/icon";
import { PanelState } from "@/components/PanelState";
import type {
  EntityId,
  ProvenanceObjectType,
  ReceptionProvenanceChain,
} from "@/types/api";
import { getErrorStatus } from "@/utils/errors";

export interface AuditChainTarget {
  objectType: ProvenanceObjectType;
  objectRef: EntityId;
  /** Human-readable name of the audited object, shown in the drawer title. */
  title: string;
}

interface ReceptionAuditChainDrawerProps {
  target: AuditChainTarget;
  data?: ReceptionProvenanceChain;
  pending: boolean;
  error: unknown;
  onRetry: () => void;
  onClose: () => void;
}

const EVENT_TYPE_COPY: Record<string, string> = {
  created: "创建",
  derived: "自动推导",
  merged: "合并",
  split: "拆分",
  edited: "人工编辑",
  superseded: "被覆盖",
  deleted: "删除",
  restored: "恢复",
};

/** The reason an operator typed, as the backend persisted it in the payload. */
function editReason(detail: Record<string, unknown>): string | null {
  const reason = detail.reason;
  return typeof reason === "string" && reason.trim() ? reason : null;
}

/**
 * Complete provenance chain for one audited object.
 *
 * The workspace's audit list only holds the events of the loaded 600-second
 * window, so a manual tag correction — and the reason its author was forced to
 * type — stops being visible as soon as the window moves. This drawer asks the
 * provenance endpoint for the object's whole chain instead of the window.
 */
export function ReceptionAuditChainDrawer({
  target,
  data,
  pending,
  error,
  onRetry,
  onClose,
}: ReceptionAuditChainDrawerProps) {
  // The endpoint answers 404 for an object that has never produced an event,
  // and its message is an untranslated server string. That is an empty chain,
  // not a failure the operator can retry their way out of.
  const chainIsEmpty = getErrorStatus(error) === 404;
  const events = data?.items ?? [];

  return (
    <aside
      className="ag-tag-lineage-drawer"
      role="dialog"
      aria-modal="false"
      aria-labelledby="audit-chain-title"
    >
      <header>
        <div>
          <span className="ag-eyebrow">AUDIT CHAIN</span>
          <h2 id="audit-chain-title">{target.title} · 完整审计链</h2>
        </div>
        <button type="button" aria-label="关闭完整审计链" onClick={onClose}>
          <IconClose />
        </button>
      </header>

      <PanelState
        pending={pending}
        error={chainIsEmpty ? null : error}
        empty={chainIsEmpty || events.length === 0}
        emptyTitle="暂无审计记录"
        emptyDescription="该对象尚未产生溯源事件；自动推导与人工编辑都会在这里留痕。"
        onRetry={onRetry}
        pendingLabel="正在加载完整审计链…"
      >
        <div className="ag-tag-lineage-content">
          <div className="ag-tag-lineage-summary">
            <IconHistory aria-hidden="true" />
            <span>
              <strong>共 {data?.total ?? events.length} 条溯源事件</strong>
              <small>
                {data?.truncated
                  ? `按时间正序显示前 ${events.length} 条`
                  : "按时间正序显示全部记录"}
              </small>
            </span>
          </div>
          <ol className="ag-audit-chain-list">
            {events.map((event) => {
              const reason = editReason(event.detail);
              return (
                <li key={String(event.id)}>
                  <div className="ag-audit-chain-list__head">
                    <strong>
                      {EVENT_TYPE_COPY[event.action] ?? event.action}
                    </strong>
                    <time dateTime={event.occurred_at}>
                      {new Date(event.occurred_at).toLocaleString("zh-CN")}
                    </time>
                  </div>
                  <div className="ag-meta-row">
                    <span>{event.actor ?? "系统"}</span>
                    {event.algorithm_version && (
                      <span>{event.algorithm_version}</span>
                    )}
                  </div>
                  {reason ? (
                    <p className="ag-audit-chain-list__reason">
                      <span>编辑原因</span>
                      {reason}
                    </p>
                  ) : (
                    <p className="ag-audit-chain-list__reason is-empty">
                      系统事件，无需填写编辑原因
                    </p>
                  )}
                </li>
              );
            })}
          </ol>
        </div>
      </PanelState>
    </aside>
  );
}
