/**
 * SpeakerBadge — visual badge for a speaker (M7 T12).
 *
 * Renders role + ambiguity indicator:
 *   - agent → blue dot + "坐席"
 *   - customer → orange dot + "客户"
 *   - unknown → gray dot + "未知"
 *   - AMBIGUOUS tag → yellow ⚠ prefix
 *   - PENDING_REVIEW tag → red ⚠ prefix
 *
 * Compact, reusable in tables, detail panels, and graph node tooltips.
 */

import { Tag, Tooltip } from "@arco-design/web-react";

export type SpeakerRole = "agent" | "customer" | "unknown";
export type AmbiguityTag = "AMBIGUOUS" | "PENDING_REVIEW" | null;

export interface SpeakerBadgeProps {
  role: SpeakerRole;
  ambiguity?: AmbiguityTag;
  /** Optional display name shown before the role label. */
  displayName?: string;
  size?: "small" | "default";
  /**
   * AMBIGUOUS cosine range for the tooltip, from GET /speakers/voiceprint-policy.
   *
   * Absent means "not loaded yet, or the policy request failed" — in which case
   * the tooltip says the merge was low-confidence and stops there. It used to
   * fall back to the wording "0.5–0.7", which is a factual claim about how this
   * deployment scored the merge: correct on default settings and wrong on any
   * deployment that overrode the thresholds, which is precisely the deployment
   * whose operator most needs the real numbers.
   */
  ambiguousRange?: { low: number; high: number };
}

const ROLE_LABEL: Record<SpeakerRole, string> = {
  agent: "坐席",
  customer: "客户",
  unknown: "未知",
};

const ROLE_COLOR: Record<SpeakerRole, string> = {
  agent: "blue",
  customer: "orange",
  unknown: "gray",
};

export function SpeakerBadge({
  role,
  ambiguity,
  displayName,
  size = "default",
  ambiguousRange,
}: SpeakerBadgeProps): JSX.Element {
  const ambiguityPrefix =
    ambiguity === "AMBIGUOUS"
      ? "⚠ "
      : ambiguity === "PENDING_REVIEW"
        ? "⚠ "
        : "";
  const ambiguityColor =
    ambiguity === "AMBIGUOUS"
      ? "rgb(255, 196, 0)"
      : ambiguity === "PENDING_REVIEW"
        ? "red"
        : ROLE_COLOR[role];

  const label = `${ambiguityPrefix}${displayName ? `${displayName} · ` : ""}${ROLE_LABEL[role]}`;
  const tooltip =
    ambiguity === "AMBIGUOUS"
      ? ambiguousRange
        ? `该说话人合并置信度较低（余弦 ${ambiguousRange.low}–${ambiguousRange.high}），检索已降权处理`
        : "该说话人合并置信度较低，检索已降权处理"
      : ambiguity === "PENDING_REVIEW"
        ? "该说话人待人工复核，检索已隐藏"
        : undefined;

  const tag = (
    <Tag
      color={ambiguityColor}
      size={size === "small" ? "small" : "default"}
      style={{ marginLeft: 4 }}
    >
      {label}
    </Tag>
  );

  return tooltip ? <Tooltip content={tooltip}>{tag}</Tooltip> : tag;
}
