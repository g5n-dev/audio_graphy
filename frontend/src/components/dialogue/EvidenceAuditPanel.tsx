import type {
  DialogueEvidenceRef,
  EntityId,
  ReceptionTagAssignment,
  ReceptionWorkspaceResponse,
} from "@/types/api";
import type { AuditChainTarget } from "./ReceptionAuditChainDrawer";
import { formatClock, formatPercent, formatSeconds } from "./format";

interface EvidenceAuditPanelProps {
  workspace: ReceptionWorkspaceResponse;
  onSeekEvidence: (evidence: DialogueEvidenceRef) => void;
  selectedTagId?: EntityId | null;
  canEdit?: boolean;
  onEditTag?: (tag: ReceptionTagAssignment) => void;
  /** Opens the full provenance chain of one object outside this window. */
  onViewAuditChain?: (target: AuditChainTarget) => void;
}

export function EvidenceAuditPanel({
  workspace,
  onSeekEvidence,
  selectedTagId = null,
  canEdit = false,
  onEditTag,
  onViewAuditChain,
}: EvidenceAuditPanelProps) {
  return (
    <div className="ag-evidence-panel">
      <section aria-labelledby="tag-evidence-heading">
        <div className="ag-panel-heading">
          <h3 id="tag-evidence-heading">标签与证据</h3>
          <span>{workspace.tag_assignments.length}</span>
        </div>
        {workspace.tag_assignments.length === 0 ? (
          <p className="ag-empty-inline">暂无标签证据</p>
        ) : (
          <ul className="ag-evidence-list">
            {workspace.tag_assignments.map((tag) => (
              <li
                key={String(tag.id)}
                className={`ag-evidence-card${selectedTagId !== null && String(selectedTagId) === String(tag.id) ? " is-selected" : ""}`}
              >
                <div className="ag-evidence-card__title">
                  <div>
                    <span>{tag.label_key}</span>
                    <strong>{tag.label_value}</strong>
                  </div>
                  <div className="ag-evidence-card__actions">
                    {/* Every role may read the chain — the provenance route
                        gates on tenant/ownership, not on edit rights — so this
                        stays outside the canEdit branch. */}
                    {onViewAuditChain && (
                      <button
                        type="button"
                        aria-label={`查看标签 ${tag.label_key} 的完整审计链`}
                        onClick={() =>
                          onViewAuditChain({
                            objectType: "dialogue_tag_assignment",
                            objectRef: tag.id,
                            title: `标签 ${tag.label_key}`,
                          })
                        }
                      >
                        审计链
                      </button>
                    )}
                    {canEdit && onEditTag && (
                      <button
                        type="button"
                        aria-label={`从证据卡编辑标签 ${tag.label_key}`}
                        onClick={() => onEditTag(tag)}
                      >
                        编辑
                      </button>
                    )}
                  </div>
                </div>
                <div className="ag-meta-row">
                  <span>
                    {tag.group_key}@{tag.group_version}
                  </span>
                  <span>{formatPercent(tag.confidence)}</span>
                  {tag.is_manual && <em>人工覆盖</em>}
                </div>
                {tag.evidence_refs.length === 0 ? (
                  <p className="ag-evidence-missing">缺少可回放证据</p>
                ) : (
                  <div className="ag-evidence-actions">
                    {tag.evidence_refs.map((evidence) => {
                      const hasTimelinePosition =
                        evidence.timeline_start_ms != null ||
                        evidence.start_ms != null;
                      const startSeconds =
                        (evidence.timeline_start_ms ??
                          evidence.start_ms ??
                          0) / 1000;
                      return (
                        <button
                          type="button"
                          key={evidence.ref_id}
                          disabled={!hasTimelinePosition}
                          onClick={() => onSeekEvidence(evidence)}
                          aria-label={
                            hasTimelinePosition
                              ? `定位证据 ${tag.label_key} ${formatSeconds(startSeconds)} 秒`
                              : `证据 ${tag.label_key} 缺少时间码`
                          }
                          title={
                            hasTimelinePosition
                              ? "在调听时间轴定位"
                              : "该证据未提供时间码"
                          }
                        >
                          <span>
                            {evidence.kind === "audio" ? "原音" : "文本"}
                          </span>
                          <strong>
                            {hasTimelinePosition
                              ? formatClock(startSeconds)
                              : "缺少时间码"}
                          </strong>
                          {evidence.text_excerpt && (
                            <small>{evidence.text_excerpt}</small>
                          )}
                        </button>
                      );
                    })}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="transition-heading">
        <div className="ag-panel-heading">
          <h3 id="transition-heading">状态转移</h3>
          <span>{workspace.state_transitions.length}</span>
        </div>
        {workspace.state_transitions.length === 0 ? (
          <p className="ag-empty-inline">暂无状态转移</p>
        ) : (
          <ol className="ag-transition-list">
            {workspace.state_transitions.map((transition) => (
              <li key={String(transition.id)}>
                <span>{transition.from_state}</span>
                <b aria-hidden="true">→</b>
                <strong>{transition.to_state}</strong>
                <small>
                  {transition.trigger} · {formatPercent(transition.confidence)}
                </small>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section aria-labelledby="audit-heading">
        <div className="ag-panel-heading">
          <h3 id="audit-heading">审计记录</h3>
          <span>{workspace.audit_events.length}</span>
        </div>
        {/* The list above only covers the loaded time window; the reception's
            older events (and their edit reasons) live behind this drawer. */}
        {onViewAuditChain && (
          <button
            type="button"
            className="ag-audit-chain-link"
            onClick={() =>
              onViewAuditChain({
                objectType: "reception",
                objectRef: workspace.reception.id,
                title: "本次接待",
              })
            }
          >
            查看完整审计链
          </button>
        )}
        {workspace.audit_events.length === 0 ? (
          <p className="ag-empty-inline">当前时间窗内暂无人工变更记录</p>
        ) : (
          <ol className="ag-audit-list">
            {workspace.audit_events.map((event) => (
              <li key={String(event.id)}>
                <strong>{event.action}</strong>
                <span>{event.actor ?? "系统"}</span>
                <time dateTime={event.occurred_at}>
                  {new Date(event.occurred_at).toLocaleString("zh-CN")}
                </time>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
