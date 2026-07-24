import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { formatClock, formatPercent } from "@/components/dialogue/format";
import type {
  TagInsightEvidenceRef,
  TagInsightGroup,
  TagInsightMatrixRow,
} from "@/types/api";

interface TagMatrixProps {
  groups: TagInsightGroup[];
  rows: TagInsightMatrixRow[];
}

const ROW_HEIGHT = 72;
const VIEWPORT_HEIGHT = 504;
const OVERSCAN = 4;

function groupIdentity(group: TagInsightGroup): string {
  return group.group_id ?? group.group_key;
}

function collectEvidence(row: TagInsightMatrixRow): TagInsightEvidenceRef[] {
  const refs = [
    ...row.cells.flatMap((cell) =>
      cell.assignments.flatMap((assignment) => assignment.evidence_refs),
    ),
    ...row.merged.evidence_refs,
  ];
  return [...new Map(refs.map((ref) => [ref.ref_id, ref])).values()];
}

function receptionIdFromTarget(targetId: string): string | null {
  const trimmed = targetId.trim();
  const persistedTarget = /^reception:(\d+)\/unit:\d+$/.exec(trimmed);
  if (persistedTarget?.[1]) return persistedTarget[1];
  return /^\d+$/.test(trimmed) ? trimmed : null;
}

export function TagMatrix({ groups, rows }: TagMatrixProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [activeRowIndex, setActiveRowIndex] = useState<number | null>(null);
  const visibleCount = Math.ceil(VIEWPORT_HEIGHT / ROW_HEIGHT) + OVERSCAN * 2;
  const viewportHeight = Math.min(
    VIEWPORT_HEIGHT,
    Math.max(ROW_HEIGHT, rows.length * ROW_HEIGHT),
  );
  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const end = Math.min(rows.length, start + visibleCount);
  const activeRow =
    activeRowIndex === null ? null : (rows[activeRowIndex] ?? null);
  const activeEvidence = useMemo(
    () => (activeRow ? collectEvidence(activeRow) : []),
    [activeRow],
  );

  useEffect(() => {
    setScrollTop(0);
    setActiveRowIndex(null);
    if (viewportRef.current) viewportRef.current.scrollTop = 0;
  }, [rows]);

  return (
    <section
      className="ag-insight-section ag-tag-matrix"
      aria-labelledby="tag-matrix-title"
    >
      <div className="ag-insight-section__heading">
        <div>
          <h2 id="tag-matrix-title">标签矩阵</h2>
          <p>接待/时间窗 × 标签 × 标签组；仅渲染可视区以控制大矩阵开销。</p>
        </div>
        <span>{rows.length} 行</span>
      </div>

      {rows.length === 0 ? (
        <p className="ag-chart-empty">分析结果没有可对齐的标签单元格。</p>
      ) : (
        <div className="ag-matrix-layout">
          <div
            className="ag-matrix"
            role="table"
            aria-label="多标签组对比矩阵"
            style={
              {
                "--ag-matrix-groups": String(groups.length),
              } as React.CSSProperties
            }
          >
            <div className="ag-matrix__header" role="row">
              <span role="columnheader">目标 / 标签</span>
              {groups.map((group) => (
                <span role="columnheader" key={groupIdentity(group)}>
                  {`${group.group_key}@${group.version}`}
                  <small>
                    {group.source} · P{group.priority}
                  </small>
                </span>
              ))}
              <span role="columnheader">合并结果</span>
              <span role="columnheader">质量</span>
            </div>
            <div
              ref={viewportRef}
              className="ag-matrix__viewport"
              style={{ height: viewportHeight }}
              onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
            >
              <div
                className="ag-matrix__spacer"
                style={{ height: rows.length * ROW_HEIGHT }}
              >
                {rows.slice(start, end).map((row, offset) => {
                  const rowIndex = start + offset;
                  return (
                    <div
                      className={`ag-matrix__row${row.conflict ? " is-conflict" : ""}`}
                      role="row"
                      key={`${row.target_id}-${row.window.start_ms}-${row.label_key}`}
                      style={{
                        height: ROW_HEIGHT,
                        transform: `translateY(${rowIndex * ROW_HEIGHT}px)`,
                      }}
                    >
                      <button
                        type="button"
                        role="cell"
                        className="ag-matrix__target"
                        aria-label={`查看 ${row.target_id} ${row.label_key} 的证据`}
                        aria-pressed={activeRowIndex === rowIndex}
                        onClick={() => setActiveRowIndex(rowIndex)}
                      >
                        <strong>{row.label_key}</strong>
                        <span>
                          {row.target_id} ·{" "}
                          {formatClock(row.window.start_ms / 1000)}–
                          {formatClock(row.window.end_ms / 1000)}
                        </span>
                      </button>
                      {groups.map((group) => {
                        const cell = row.cells.find(
                          (candidate) =>
                            groupIdentity(candidate.group) ===
                            groupIdentity(group),
                        );
                        return (
                          <div
                            role="cell"
                            key={groupIdentity(group)}
                            className={cell?.missing ? "is-missing" : undefined}
                          >
                            {cell?.missing || !cell ? (
                              <span className="ag-cell-missing">缺失</span>
                            ) : (
                              cell.assignments.map((assignment) => (
                                <span
                                  className="ag-cell-value"
                                  key={`${assignment.label_key}-${assignment.value}`}
                                >
                                  <strong>{assignment.value}</strong>
                                  <small>
                                    {formatPercent(assignment.confidence)}
                                    {assignment.is_manual ? " · 人工" : ""}
                                  </small>
                                </span>
                              ))
                            )}
                          </div>
                        );
                      })}
                      <div role="cell" className="ag-matrix__merged">
                        {row.merged.values.length > 0 ? (
                          <>
                            <strong>{row.merged.values.join(" / ")}</strong>
                            <small>
                              {row.merged.selected_group_keys.join(", ") || "—"}
                            </small>
                          </>
                        ) : (
                          <span>无共同值</span>
                        )}
                      </div>
                      <div role="cell" className="ag-matrix__quality">
                        {row.conflict && <strong>冲突</strong>}
                        {row.missing_group_keys.length > 0 && (
                          <span>缺 {row.missing_group_keys.length} 组</span>
                        )}
                        {!row.conflict &&
                          row.missing_group_keys.length === 0 && (
                            <span className="is-ok">一致</span>
                          )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          <aside className="ag-matrix-evidence" aria-label="标签证据下钻">
            <h3>证据下钻</h3>
            {!activeRow ? (
              <p>选择矩阵左侧的目标/标签单元查看原音与文本证据。</p>
            ) : activeEvidence.length === 0 ? (
              <p>该单元没有可回放证据，建议进入人工复核。</p>
            ) : (
              <>
                <strong>{activeRow.label_key}</strong>
                <span>{activeRow.target_id}</span>
                <ul>
                  {activeEvidence.map((evidence) => {
                    const receptionId = receptionIdFromTarget(
                      activeRow.target_id,
                    );
                    return (
                      <li key={evidence.ref_id}>
                        <span>
                          {evidence.kind === "audio" ? "原音" : "文本"} ·{" "}
                          {evidence.recording_id}
                        </span>
                        {evidence.text_excerpt && (
                          <blockquote>{evidence.text_excerpt}</blockquote>
                        )}
                        {receptionId ? (
                          <Link
                            to={`/receptions/${encodeURIComponent(receptionId)}/workspace?recording=${encodeURIComponent(evidence.recording_id)}&at=${evidence.start_ms ?? 0}`}
                          >
                            到调听工作台定位{" "}
                            {formatClock((evidence.start_ms ?? 0) / 1000)}
                          </Link>
                        ) : (
                          <small role="note">快照未提供接待映射</small>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
          </aside>
        </div>
      )}
    </section>
  );
}
