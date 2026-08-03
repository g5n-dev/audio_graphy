import {
  IconBranch,
  IconClose,
  IconSound,
} from "@arco-design/web-react/icon";
import { type FormEvent, useMemo, useState } from "react";
import type {
  DialogueEvidenceRef,
  ReceptionTagAssignment,
  TagDefinition,
} from "@/types/api";
import { formatClock } from "./format";

interface TagAssignmentEditorProps {
  tag: ReceptionTagAssignment;
  definition?: TagDefinition;
  isSaving: boolean;
  error?: string | null;
  onCancel: () => void;
  onSeekEvidence: (evidence: DialogueEvidenceRef) => void;
  onViewLineage?: (factId: number) => void;
  onSubmit: (input: TagCorrectionDraft) => void;
}

export interface TagCorrectionDraft {
  labelValue: string;
  reason: string;
  evidenceRefIds: string[];
}

function evidenceLabel(evidence: DialogueEvidenceRef): string {
  const startMs = evidence.timeline_start_ms ?? evidence.start_ms ?? 0;
  const endMs = evidence.timeline_end_ms ?? evidence.end_ms ?? startMs;
  return `${evidence.ref_id} · ${formatClock(startMs / 1_000)}–${formatClock(
    endMs / 1_000,
  )}`;
}

function factIdFromModelRun(modelRunId: string | null | undefined): number | null {
  const match = modelRunId?.match(/^fact:(\d+)$/);
  const value = match ? Number(match[1]) : Number.NaN;
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

/**
 * Why the save button is disabled, in form order.
 *
 * A disabled button that never says what is missing leaves the reviewer
 * guessing between four independent conditions, so every unmet one is named.
 */
function submitBlockers({
  labelValue,
  definition,
  evidenceCount,
  retainedEvidenceCount,
  reason,
}: {
  labelValue: string;
  definition?: TagDefinition;
  evidenceCount: number;
  retainedEvidenceCount: number;
  reason: string;
}): string[] {
  const allowedValues = definition?.allowed_values ?? [];
  const blockers: string[] = [];
  if (labelValue.trim().length === 0) {
    blockers.push("标签值尚未填写");
  } else if (
    definition?.value_type === "number" &&
    !Number.isFinite(Number(labelValue))
  ) {
    blockers.push("标签值需要填写数字");
  } else if (
    definition?.value_type === "boolean" &&
    labelValue !== "true" &&
    labelValue !== "false"
  ) {
    blockers.push("标签值需要选择 true 或 false");
  } else if (
    allowedValues.length > 0 &&
    !allowedValues.some((value) => String(value) === labelValue)
  ) {
    blockers.push("标签值不在当前标签体系的允许值内");
  }
  if (evidenceCount === 0) {
    blockers.push("该标签没有可验证证据，不能直接覆盖");
  } else if (retainedEvidenceCount === 0) {
    blockers.push("至少需要保留一条证据");
  }
  if (reason.trim().length === 0) {
    blockers.push("标签编辑原因尚未填写");
  }
  return blockers;
}

export function TagAssignmentEditor({
  tag,
  definition,
  isSaving,
  error,
  onCancel,
  onSeekEvidence,
  onViewLineage,
  onSubmit,
}: TagAssignmentEditorProps) {
  const evidence = useMemo(
    () => tag.evidence_refs.filter((item) => Boolean(item.ref_id)),
    [tag.evidence_refs],
  );
  const [labelValue, setLabelValue] = useState(tag.label_value);
  const [reason, setReason] = useState("");
  const [selectedEvidence, setSelectedEvidence] = useState<Set<string>>(
    () => new Set(evidence.map((item) => item.ref_id)),
  );
  const [editedAssignmentId, setEditedAssignmentId] = useState(tag.id);

  // The draft belongs to one assignment, so it is discarded only when the
  // editor is pointed at a different assignment.  Resetting on anything derived
  // from the tag payload (the evidence array, the label value) threw the
  // reviewer's typing away on every background refetch of the workspace query:
  // a refetch rebuilds `tag.evidence_refs` with a fresh identity even when the
  // assignment itself is unchanged.
  if (editedAssignmentId !== tag.id) {
    setEditedAssignmentId(tag.id);
    setLabelValue(tag.label_value);
    setReason("");
    setSelectedEvidence(new Set(evidence.map((item) => item.ref_id)));
  }

  const allowedValues = definition?.allowed_values ?? [];
  const blockers = submitBlockers({
    labelValue,
    definition,
    evidenceCount: evidence.length,
    retainedEvidenceCount: selectedEvidence.size,
    reason,
  });
  const canSubmit = !isSaving && blockers.length === 0;
  const lineageFactId = factIdFromModelRun(tag.model_run_id);
  const isDirty =
    labelValue !== tag.label_value ||
    reason.trim().length > 0 ||
    selectedEvidence.size !== evidence.length ||
    evidence.some((item) => !selectedEvidence.has(item.ref_id));

  const requestCancel = () => {
    if (isSaving) return;
    if (
      isDirty &&
      !window.confirm("当前标签草稿尚未保存，确认放弃本次编辑吗？")
    ) {
      return;
    }
    onCancel();
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    onSubmit({
      labelValue: labelValue.trim(),
      reason: reason.trim(),
      evidenceRefIds: evidence
        .map((item) => item.ref_id)
        .filter((refId) => selectedEvidence.has(refId)),
    });
  };

  return (
    <section
      className="ag-tag-assignment-editor"
      aria-labelledby="tag-assignment-editor-title"
    >
      <header>
        <div>
          <span className="ag-eyebrow">MANUAL FACT</span>
          <h2 id="tag-assignment-editor-title">
            编辑标签 · {definition?.name ?? tag.label_key}
          </h2>
          <p>
            <span>{`${tag.group_key}@${tag.group_version}`}</span> · 原结果{" "}
            {tag.label_value}
          </p>
        </div>
        <button
          type="button"
          aria-label="关闭标签编辑"
          onClick={requestCancel}
        >
          <IconClose />
        </button>
      </header>

      <form onSubmit={submit}>
        {lineageFactId !== null && onViewLineage && (
          <button
            type="button"
            className="ag-tag-assignment-editor__lineage"
            onClick={() => onViewLineage(lineageFactId)}
          >
            <IconBranch aria-hidden="true" />
            查看事实 #{lineageFactId} 溯源
          </button>
        )}
        <label>
          标签值
          {allowedValues.length > 0 || definition?.value_type === "boolean" ? (
            <select
              aria-label="标签值"
              value={labelValue}
              onChange={(event) => setLabelValue(event.target.value)}
            >
              {(allowedValues.length > 0
                ? allowedValues
                : [true, false]
              ).map((value) => (
                <option key={String(value)} value={String(value)}>
                  {String(value)}
                </option>
              ))}
            </select>
          ) : (
            <input
              aria-label="标签值"
              type={definition?.value_type === "number" ? "number" : "text"}
              step={definition?.value_type === "number" ? "any" : undefined}
              value={labelValue}
              onChange={(event) => setLabelValue(event.target.value)}
            />
          )}
        </label>

        <fieldset>
          <legend>保留证据</legend>
          {evidence.length === 0 ? (
            <p className="ag-inline-feedback is-error" role="alert">
              该标签没有可验证证据，不能直接覆盖；请先进入人工复核补齐证据。
            </p>
          ) : (
            <ul>
              {evidence.map((item) => (
                <li key={item.ref_id}>
                  <label>
                    <input
                      type="checkbox"
                      aria-label={`证据 ${item.ref_id}`}
                      checked={selectedEvidence.has(item.ref_id)}
                      onChange={(event) => {
                        setSelectedEvidence((current) => {
                          const next = new Set(current);
                          if (event.target.checked) next.add(item.ref_id);
                          else next.delete(item.ref_id);
                          return next;
                        });
                      }}
                    />
                    <span>{evidenceLabel(item)}</span>
                  </label>
                  <button
                    type="button"
                    aria-label={`回听证据 ${item.ref_id}`}
                    onClick={() => onSeekEvidence(item)}
                  >
                    <IconSound aria-hidden="true" />
                    回听
                  </button>
                </li>
              ))}
            </ul>
          )}
        </fieldset>

        <label>
          标签编辑原因
          <textarea
            rows={3}
            aria-label="标签编辑原因"
            value={reason}
            maxLength={500}
            onChange={(event) => setReason(event.target.value)}
            placeholder="必填，将与人工事实一起写入审计"
          />
        </label>

        {error && (
          <p className="ag-inline-feedback is-error" role="alert">
            {error}
          </p>
        )}

        {!isSaving && blockers.length > 0 && (
          <p
            className="ag-inline-feedback"
            id="tag-assignment-editor-blockers"
            role="status"
          >
            暂不能保存：{blockers.join("；")}。
          </p>
        )}

        <footer>
          <button
            type="button"
            className="is-secondary"
            onClick={requestCancel}
          >
            取消
          </button>
          <button
            type="submit"
            disabled={!canSubmit}
            aria-describedby={
              !isSaving && blockers.length > 0
                ? "tag-assignment-editor-blockers"
                : undefined
            }
          >
            {isSaving ? "正在保存…" : "保存人工更正"}
          </button>
        </footer>
      </form>
    </section>
  );
}
