/**
 * 治理与实验室页面共用的展示格式化。
 *
 * 这些函数原本在 TagGovernance/index.tsx 与 EvolutionPanel.tsx 里各存一份，
 * 实现相同但签名略有出入。集中在这里是为了让「—」代表缺失、正负号代表方向
 * 这类约定在几个页面之间保持一致——读者在不同页面看到同一个符号时，它应该
 * 表示同一件事。
 */

const PERCENT_FORMATTER = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 1,
});
const COUNT_FORMATTER = new Intl.NumberFormat("zh-CN");

/** 缺失值统一显示为破折号，而不是 0% —— 两者含义完全不同。 */
export function compactPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return `${PERCENT_FORMATTER.format(value * 100)}%`;
}

export function compactCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return COUNT_FORMATTER.format(value);
}

/**
 * 带符号百分比，用于指标变化。
 *
 * 零显示为 "+0.0%" 而非 "0.0%"：沿用 EvolutionPanel 的既有约定，且「显式测量到没有
 * 变化」与「没有测量」需要看起来不同——后者是破折号。
 */
export function signedPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return `${value >= 0 ? "+" : ""}${compactPercent(value)}`;
}

/** 带符号整数，用于 fixed_token_delta / headroom_delta。符号规则同 signedPercent。 */
export function signedCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return `${value >= 0 ? "+" : ""}${COUNT_FORMATTER.format(value)}`;
}

export function numericMetric(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** 无法解析的时间原样返回，避免向用户展示 "Invalid Date"。 */
export function formatDate(value?: string | null): string {
  if (!value) return "—";
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp)
    ? new Date(timestamp).toLocaleString("zh-CN", { hour12: false })
    : value;
}

/** 未知形状的值的兜底展示，用于后端仍在演进的 JSON 字段。 */
export function displayValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "—";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

const FAILURE_STAGE_LABELS: Readonly<Record<string, string>> = {
  vad: "VAD",
  asr: "ASR",
  speaker: "说话人",
  boundary: "边界切分",
  schema: "标签体系",
  tag_reasoning: "标签推理",
  evidence: "证据定位",
  fusion: "融合策略",
  insufficient_audio: "音质不足",
};

/**
 * 失败阶段码转中文。
 *
 * 入参放宽到 `string | null`：治理页拿到的是 TagBadcase 的联合类型，而梯度记录里
 * 该字段可空且由后端自由填写，收窄会让后者无法复用。未知码原样返回而不是显示
 * 占位符——运维需要看见后端新增了什么阶段。
 */
export function failureStageLabel(stage: string | null | undefined): string {
  if (!stage) return "—";
  return FAILURE_STAGE_LABELS[stage] ?? stage;
}

export type GovernanceStatusTone = "success" | "warning" | "danger" | "info";

const DEFAULT_LABELS: Readonly<Record<string, string>> = {
  draft: "草稿",
  validating: "校验中",
  evaluating: "评估中",
  qualified: "已达标",
  rejected: "未达标",
  validated: "已校验",
  published: "已发布",
  deprecated: "已停用",
  queued: "排队中",
  running: "运行中",
  retry_wait: "等待重试",
  completed: "已完成",
  failed: "失败",
  shadow: "影子验证",
  canary_5: "5% 灰度",
  canary_25: "25% 灰度",
  awaiting_admin: "等待管理员审批",
  production: "生产",
  rolled_back: "已回滚",
  retired: "已退役",
};

/**
 * 状态码转中文。
 *
 * `labels` 用于同一状态码在不同域含义不同的场景——`rejected` 在标签治理里是
 * 「未达标」（评估没过门禁），在 Prompt 实验室里是「已拒绝」（人工否决了补丁）。
 * 覆盖而不是改默认表，是因为默认表被治理页的既有断言依赖着。
 */
export function statusLabel(
  status: string,
  labels?: Readonly<Record<string, string>>,
): string {
  return labels?.[status] ?? DEFAULT_LABELS[status] ?? status;
}

export function statusTone(status: string): GovernanceStatusTone {
  if (status === "published" || status === "production" || status === "completed") {
    return "success";
  }
  if (status === "failed" || status === "rolled_back") {
    return "danger";
  }
  if (status === "awaiting_admin" || status.includes("canary")) {
    return "warning";
  }
  return "info";
}
