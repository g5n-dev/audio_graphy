/**
 * SpeakerMergeReviewModal — confirm/reject one SpeakerMergePending row.
 *
 * Upgrades the old inline Popconfirm: shows the candidate's fuzzy /
 * voiceprint scores and lets the reviewer attach ``notes`` and an optional
 * ``voiceprint_score`` (both already supported by the backend request
 * schema — the old UI always sent an empty body).
 *
 * Shared by SpeakerProfile/Detail.tsx and VoiceprintQualityDrawer.
 */

import { useEffect, useState } from "react";
import {
  Descriptions,
  Input,
  InputNumber,
  Modal,
  Typography,
} from "@arco-design/web-react";
import type { SpeakerMergePendingListItem } from "@/api/advancedGraph";
import {
  useSpeakerMergeReview,
  type MergeReviewInput,
} from "@/hooks/useSpeakerMergeReview";

const { Text } = Typography;

export type MergeReviewMode = "confirm" | "reject";

export interface SpeakerMergeReviewModalProps {
  visible: boolean;
  mode: MergeReviewMode;
  row: SpeakerMergePendingListItem | null;
  /** Merge target (canonical SpeakerNode). Defaults to row.matched_speaker_node_id. */
  targetSpeakerId?: number;
  onClose: () => void;
  /** Called after a successful confirm/reject. */
  onResolved?: () => void;
}

export function SpeakerMergeReviewModal({
  visible,
  mode,
  row,
  targetSpeakerId,
  onClose,
  onResolved,
}: SpeakerMergeReviewModalProps): JSX.Element {
  const { confirm, reject, submitting } = useSpeakerMergeReview();
  const [notes, setNotes] = useState("");
  const [score, setScore] = useState<number | undefined>(undefined);
  /**
   * Arco keeps the modal mounted through its ~400ms exit animation, so
   * rendering the live props would blank the body and flip the title back
   * to "确认合并" mid-fade. Hold the last open state for the animation.
   */
  const [shown, setShown] = useState<{
    mode: MergeReviewMode;
    row: SpeakerMergePendingListItem;
    targetSpeakerId?: number;
  } | null>(null);

  useEffect(() => {
    if (visible && row) {
      setShown({ mode, row, targetSpeakerId });
      setNotes("");
      setScore(undefined);
    }
  }, [visible, row, mode, targetSpeakerId]);

  const activeMode = shown?.mode ?? mode;
  const activeRow = shown?.row ?? row;
  const activeTarget = shown?.targetSpeakerId ?? targetSpeakerId;

  async function onOk(): Promise<void> {
    if (!activeRow) {
      return;
    }
    const input: MergeReviewInput = {};
    if (notes.trim()) {
      input.notes = notes.trim();
    }
    let ok: boolean;
    if (activeMode === "confirm") {
      if (score !== undefined) {
        input.voiceprint_score = score;
      }
      ok = await confirm(
        activeRow.id,
        activeTarget ?? activeRow.matched_speaker_node_id,
        input,
      );
    } else {
      ok = await reject(activeRow.id, input);
    }
    if (ok) {
      onResolved?.();
      onClose();
    }
  }

  return (
    <Modal
      visible={visible}
      title={activeMode === "confirm" ? "确认合并" : "驳回合并"}
      okText={activeMode === "confirm" ? "确认合并" : "驳回"}
      cancelText="取消"
      okButtonProps={{
        status: activeMode === "reject" ? "danger" : undefined,
        loading: submitting,
      }}
      onOk={() => void onOk()}
      onCancel={onClose}
      autoFocus={false}
      focusLock
    >
      {activeRow ? (
        <>
          <Descriptions
            column={2}
            size="small"
            style={{ marginBottom: 16 }}
            data={[
              { label: "Pending ID", value: `#${activeRow.id}` },
              { label: "候选名称", value: activeRow.candidate_name },
              { label: "Fuzzy 分数", value: activeRow.fuzzy_score.toFixed(3) },
              {
                label: "声纹余弦",
                value:
                  activeRow.voiceprint_score !== null
                    ? activeRow.voiceprint_score.toFixed(3)
                    : "—",
              },
              { label: "来源录音", value: `#${activeRow.recording_id}` },
              {
                label: "合并目标",
                value: `Speaker #${activeTarget ?? activeRow.matched_speaker_node_id}`,
              },
            ]}
          />
          {activeMode === "confirm" ? (
            <div style={{ marginBottom: 12 }}>
              <Text type="secondary" style={{ display: "block", marginBottom: 4 }}>
                声纹余弦（可选，-1 ~ 1，将写入审计与置信度）
              </Text>
              <InputNumber
                value={score}
                onChange={(v) => setScore(typeof v === "number" ? v : undefined)}
                min={-1}
                max={1}
                step={0.05}
                placeholder="留空则沿用已有分数"
                style={{ width: "100%" }}
              />
            </div>
          ) : null}
          <div>
            <Text type="secondary" style={{ display: "block", marginBottom: 4 }}>
              复核备注（可选）
            </Text>
            <Input.TextArea
              value={notes}
              onChange={setNotes}
              placeholder={
                activeMode === "confirm"
                  ? "例如：已人工听音核对，确认为同一客户"
                  : "例如：声音明显不同，驳回"
              }
              rows={3}
              maxLength={500}
              showWordLimit
            />
          </div>
        </>
      ) : null}
    </Modal>
  );
}
