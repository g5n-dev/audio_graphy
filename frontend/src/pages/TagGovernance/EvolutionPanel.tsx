import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  IconCheckCircle,
  IconClose,
  IconExclamationCircle,
  IconThunderbolt,
} from "@arco-design/web-react/icon";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import {
  cancelTagOptimizationRun,
  compareTagOptimizationTrials,
  createTagOptimizationRun,
  getTagEvolutionOverview,
  getTagOptimizationRun,
  listTagBadcases,
  listTagOptimizationRuns,
} from "@/api/services";
import type {
  CreateTagOptimizationRunRequest,
  TagBadcase,
  TagEvolutionOverview,
  TagOptimizationCandidateComparison,
  TagOptimizationObjectivePolicy,
  TagOptimizationRun,
  TagOptimizationSourceCohort,
  TagOptimizationTrial,
} from "@/types/api";
import { PanelState } from "@/components/PanelState";
import { getErrorMessage } from "@/utils/errors";

interface EvolutionPanelProps {
  isAdmin: boolean;
  initialDialog?: "optimize" | null;
  initialCohort?: TagOptimizationSourceCohort | null;
}

const ACTIVE_RUN_STATUSES = new Set(["queued", "running"]);
const HARNESS_DIMENSIONS = [
  { id: "context", label: "上下文与示例" },
  { id: "tools", label: "工具与模型" },
  { id: "generation", label: "生成策略" },
  { id: "orchestration", label: "编排 DAG" },
  { id: "memory", label: "经验检索" },
  { id: "output", label: "输出校验与回退" },
] as const;

// build_candidate_comparison 返回的 delta 键就是 Trial metrics / reward_vector
// 里的原始键名，这里只给已知键配中文名，未知键原样透出，避免服务端新增指标时被吞掉。
const DELTA_LABELS: Record<string, string> = {
  macro_f1: "Macro F1",
  critical_recall: "关键召回",
  critical_recall_lcb: "关键召回 LCB",
  evidence_coverage: "证据覆盖",
  evidence_iou: "证据 IoU",
  review_rate: "复核率",
  error_rate: "错误率",
  p95_latency_ms: "P95 时延",
  cost_per_1k: "每千次成本",
  quality_delta: "质量奖励",
  review_rate_delta: "复核率奖励",
  p95_latency_delta: "时延奖励",
  cost_delta: "成本奖励",
};
const RATIO_DELTA_KEYS = new Set([
  "macro_f1",
  "critical_recall",
  "critical_recall_lcb",
  "evidence_coverage",
  "evidence_iou",
  "review_rate",
  "error_rate",
  "quality_delta",
  "review_rate_delta",
]);
// 这些维度越低越好，右侧 Trial 的负 delta 才算改善。
const LOWER_IS_BETTER_DELTA_KEYS = new Set([
  "review_rate",
  "error_rate",
  "p95_latency_ms",
  "cost_per_1k",
  "review_rate_delta",
  "p95_latency_delta",
  "cost_delta",
]);
const RECOMMENDATION_BASIS_LABELS: Record<string, string> = {
  feasibility_then_quality_review_latency_cost:
    "按可行性 → 质量 → 复核率 → 时延 → 成本的词典序排序",
  baseline_retained_by_lexicographic_reward: "词典序奖励下基线仍然最优",
  insufficient_completed_reward: "奖励向量尚不完整",
};

function percent(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 1,
  }).format(value * 100)}%`;
}

function count(value: number | null | undefined): string {
  return new Intl.NumberFormat("zh-CN").format(value ?? 0);
}

function signedPercent(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${percent(value)}`;
}

function deltaLabel(key: string): string {
  return DELTA_LABELS[key] ?? key;
}

function formatDelta(key: string, value: number): string {
  if (RATIO_DELTA_KEYS.has(key)) return signedPercent(value);
  const formatted = new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 3,
  }).format(value);
  return `${value > 0 ? "+" : ""}${formatted}${
    key.endsWith("_ms") ? " ms" : ""
  }`;
}

function deltaTone(key: string, value: number): "better" | "worse" | "flat" {
  if (value === 0) return "flat";
  const improved = LOWER_IS_BETTER_DELTA_KEYS.has(key) ? value < 0 : value > 0;
  return improved ? "better" : "worse";
}

/** 服务端只回传有限数值 delta，非有限值会被过滤，这里同样只渲染数值项。 */
function numericDeltaEntries(
  deltas: Record<string, number | null | undefined> | undefined,
): [string, number][] {
  return Object.entries(deltas ?? {}).filter(
    (entry): entry is [string, number] =>
      typeof entry[1] === "number" && Number.isFinite(entry[1]),
  );
}

function trialLabel(trial: TagOptimizationTrial): string {
  const dimension = trial.mutation?.dimension;
  const dimensionLabel =
    HARNESS_DIMENSIONS.find((item) => item.id === dimension)?.label ??
    (typeof dimension === "string" ? dimension : null);
  const description = trial.mutation?.description;
  return [
    `Trial ${trial.ordinal}`,
    dimensionLabel,
    typeof description === "string" && description ? description : null,
  ]
    .filter((part): part is string => Boolean(part))
    .join(" · ");
}

function displayValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "—";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function runStatus(status: TagOptimizationRun["status"]): string {
  const labels: Record<TagOptimizationRun["status"], string> = {
    queued: "排队中",
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status];
}

function candidateComparison(
  run: TagOptimizationRun,
): TagOptimizationCandidateComparison | null {
  if (run.candidate_comparison) return run.candidate_comparison;
  const value = run.summary.candidate_comparison;
  if (
    typeof value !== "object" ||
    value === null ||
    !Array.isArray((value as Record<string, unknown>).dimensions)
  ) {
    return null;
  }
  return value as TagOptimizationCandidateComparison;
}

function numericSummary(
  summary: Record<string, unknown>,
  key: string,
): number | null {
  const value = summary[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function failureStageLabel(stage: TagBadcase["failure_stage"]): string {
  const labels: Record<TagBadcase["failure_stage"], string> = {
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
  return labels[stage];
}

function cohortLabel(cohort: TagOptimizationSourceCohort): string {
  if (cohort.source === "tag_insights") return "来自标签洞察";
  if (cohort.source === "scheduled") return "周期调度反馈";
  if (cohort.source === "feedback_threshold") return "反馈阈值触发";
  return "全部合格 T2 / T3 反馈";
}

function cohortSummary(cohort: TagOptimizationSourceCohort): string[] {
  const filters = cohort.filters ?? {};
  const items: string[] = [];
  if (filters.store_ids?.length) {
    items.push(`门店 ${filters.store_ids.join("、")}`);
  }
  if (filters.agent_names?.length) {
    items.push(`顾问 ${filters.agent_names.join("、")}`);
  }
  if (filters.reception_ids?.length) {
    items.push(
      filters.reception_ids.length === 1
        ? `接待 ${String(filters.reception_ids[0])}`
        : `${filters.reception_ids.length} 个接待`,
    );
  }
  if (filters.scenarios?.length) {
    items.push(`场景 ${filters.scenarios.join("、")}`);
  }
  if (filters.group_keys?.length) {
    items.push(`标签组 ${filters.group_keys.join("、")}`);
  }
  if (filters.label_keys?.length) {
    items.push(`标签 ${filters.label_keys.join("、")}`);
  }
  if (filters.started_from || filters.started_to) {
    items.push(
      `时间 ${filters.started_from ?? "不限"} → ${filters.started_to ?? "不限"}`,
    );
  }
  if (cohort.group_ids?.length) {
    items.push(`${cohort.group_ids.length} 个精确版本`);
  }
  if (cohort.conflict_only) {
    items.push("仅冲突 / 缺失样本");
  }
  return items.length > 0 ? items : ["服务端选择全部合格反馈"];
}

function OptimizationDialog({
  cohort,
  overview,
  pending,
  mutationError,
  onClose,
  onCreate,
}: {
  cohort: TagOptimizationSourceCohort;
  overview: TagEvolutionOverview | undefined;
  pending: boolean;
  mutationError: unknown;
  onClose: () => void;
  onCreate: (body: CreateTagOptimizationRunRequest) => void;
}) {
  const [targetPolicy, setTargetPolicy] =
    useState<TagOptimizationObjectivePolicy>("balanced");
  const [budget, setBudget] =
    useState<CreateTagOptimizationRunRequest["search_budget"]["max_trials"]>(
      24,
    );
  const [error, setError] = useState<string | null>(null);
  const goldSetVersionId = overview?.recommended_gold_set_version_id ?? null;
  const cohortItems = cohortSummary(cohort);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!goldSetVersionId) {
      setError("当前没有冻结且完整的金标版本，请先完成金标构建。");
      return;
    }
    setError(null);
    onCreate({
      cohort,
      target_policy: { policy: targetPolicy },
      search_budget: {
        max_trials: budget,
        sealed_holdout_queries: 1,
      },
    });
  };

  return (
    <div className="ag-dialog-backdrop">
      <section
        className="ag-governance-dialog ag-evolution-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="evolution-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
      >
        <header>
          <div>
            <span className="ag-card-kicker">BOUNDED HARNESS SEARCH</span>
            <h2 id="evolution-dialog-title">启动自进化优化</h2>
          </div>
          <button
            type="button"
            aria-label="关闭"
            disabled={pending}
            onClick={onClose}
          >
            <IconClose />
          </button>
        </header>
        <form onSubmit={submit}>
          <section className="ag-evolution-cohort is-full">
            <span>反馈 Cohort</span>
            <strong>{cohortLabel(cohort)}</strong>
            <ul aria-label="优化反馈 Cohort 范围">
              {cohortItems.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </section>
          <section className="ag-evolution-cohort is-full">
            <span>生产基线</span>
            <strong>
              {overview?.production_harness?.version ??
                (overview ? "暂无生产 Harness" : "正在由服务端解析…")}
            </strong>
            <small>由服务端锁定当前生产版本，避免客户端提交过期基线。</small>
          </section>
          <section className="ag-evolution-cohort is-full">
            <span>发布金标</span>
            <strong>
              {overview?.recommended_gold_set_label ??
                (goldSetVersionId
                  ? `冻结版本 #${goldSetVersionId}`
                  : overview
                    ? "暂无可用金标"
                    : "正在由服务端解析…")}
            </strong>
            <small>由服务端绑定最新冻结完整金标，前端不能手填或替换。</small>
          </section>
          <label>
            优化目标
            <select
              aria-label="优化目标"
              value={targetPolicy}
              onChange={(event) =>
                setTargetPolicy(
                  event.target.value as TagOptimizationObjectivePolicy,
                )
              }
            >
              <option value="balanced">质量、复核率与成本平衡</option>
              <option value="quality_first">质量优先</option>
              <option value="efficiency_guarded">效率优先，质量硬门禁</option>
            </select>
          </label>
          <label>
            搜索预算
            <select
              aria-label="搜索预算"
              value={budget}
              onChange={(event) =>
                setBudget(
                  Number(
                    event.target.value,
                  ) as CreateTagOptimizationRunRequest["search_budget"]["max_trials"],
                )
              }
            >
              <option value="8">8 个候选 · 快速</option>
              <option value="16">16 个候选</option>
              <option value="24">24 个候选 · 推荐</option>
              <option value="32">32 个候选 · 上限</option>
            </select>
          </label>
          <p className="ag-optimization-scope is-full">
            生产 Harness 由服务端自动绑定；搜索只改变已注册的六类策略维度。
            Sealed Holdout 查询预算固定为 1，不接收错误样本 JSON。
          </p>
          {overview && !goldSetVersionId && (
            <p className="ag-inline-feedback is-error" role="alert">
              没有可发布的完整金标，优化暂不可启动。
            </p>
          )}
          {error && (
            <p className="ag-inline-feedback is-error" role="alert">
              {error}
            </p>
          )}
          {mutationError !== null && mutationError !== undefined ? (
            <p className="ag-inline-feedback is-error" role="alert">
              {getErrorMessage(mutationError, "优化运行创建失败，请稍后重试。")}
              <button type="submit" disabled={pending}>
                重试启动
              </button>
            </p>
          ) : null}
          <footer>
            <button
              type="button"
              className="is-secondary"
              disabled={pending}
              onClick={onClose}
            >
              取消
            </button>
            <button type="submit" disabled={pending || !goldSetVersionId}>
              {pending ? "正在创建…" : "启动优化运行"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function CancelOptimizationDialog({
  runId,
  pending,
  mutationError,
  onClose,
  onConfirm,
}: {
  runId: number;
  pending: boolean;
  mutationError: unknown;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="ag-dialog-backdrop">
      <section
        className="ag-governance-dialog ag-confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cancel-optimization-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
      >
        <header>
          <div>
            <span className="ag-card-kicker">STOP ACTIVE SEARCH</span>
            <h2 id="cancel-optimization-dialog-title">
              确认取消优化运行 #{runId}
            </h2>
          </div>
          <button
            type="button"
            aria-label="关闭取消确认"
            disabled={pending}
            onClick={onClose}
          >
            <IconClose />
          </button>
        </header>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onConfirm();
          }}
        >
          <p className="ag-confirm-dialog__copy">
            取消后，本次搜索及其关联任务会停止；已消耗的 Trial
            预算不会恢复。如仍需优化，需要重新创建运行。
          </p>
          {mutationError !== null && mutationError !== undefined ? (
            <p className="ag-inline-feedback is-error" role="alert">
              {getErrorMessage(mutationError, "取消运行失败，请稍后重试。")}
            </p>
          ) : null}
          <footer>
            <button
              type="button"
              className="is-secondary"
              disabled={pending}
              onClick={onClose}
            >
              继续运行
            </button>
            <button type="submit" className="is-danger" disabled={pending}>
              {pending ? "正在取消…" : "确认取消运行"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function HarnessDiffList({
  comparison,
  ariaLabel,
}: {
  comparison: TagOptimizationCandidateComparison;
  ariaLabel: string;
}) {
  return (
    <ul className="ag-harness-diff-list" aria-label={ariaLabel}>
      {HARNESS_DIMENSIONS.map((dimension) => {
        const item = comparison.dimensions.find(
          (candidate) => candidate.dimension === dimension.id,
        );
        return (
          <li key={dimension.id}>
            <code>{dimension.label}</code>
            {item ? (
              <>
                <span>{displayValue(item.before)}</span>
                <b aria-hidden="true">→</b>
                <strong>{displayValue(item.after)}</strong>
              </>
            ) : (
              <span className="is-unchanged">本次未变更</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function DeltaList({
  ariaLabel,
  emptyCopy,
  deltas,
}: {
  ariaLabel: string;
  emptyCopy: string;
  deltas: Record<string, number | null | undefined> | undefined;
}) {
  const entries = numericDeltaEntries(deltas);
  if (entries.length === 0) {
    return <p className="ag-compact-state">{emptyCopy}</p>;
  }
  return (
    // dl 本身没有隐含 role，显式标成有名字的 group 才能被读屏和测试定位。
    <dl className="ag-trial-delta-list" role="group" aria-label={ariaLabel}>
      {entries.map(([key, value]) => (
        <div key={key} className={`is-${deltaTone(key, value)}`}>
          <dt>{deltaLabel(key)}</dt>
          <dd>{formatDelta(key, value)}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * 对比同一次运行里的任意两个 Trial。
 *
 * Trial 列表只在 GET /tag-optimization-runs/{id} 里返回（列表接口不带 trials），
 * 所以这里必须单独取运行详情；对比结果由服务端 build_candidate_comparison 计算，
 * 前端不自己算 delta，避免和优化器的排序口径分叉。
 */
function TrialComparisonDialog({
  runId,
  onClose,
}: {
  runId: number;
  onClose: () => void;
}) {
  const [leftTrialId, setLeftTrialId] = useState<number | null>(null);
  const [rightTrialId, setRightTrialId] = useState<number | null>(null);
  const detailQuery = useQuery({
    queryKey: ["tag-governance", "evolution", "run", runId],
    queryFn: () => getTagOptimizationRun(runId),
    retry: false,
  });
  const trials = useMemo(
    () => detailQuery.data?.trials ?? [],
    [detailQuery.data],
  );
  const compareMutation = useMutation({
    mutationFn: (pair: { left: number; right: number }) =>
      compareTagOptimizationTrials(runId, pair.left, pair.right),
  });

  useEffect(() => {
    if (trials.length < 2 || leftTrialId !== null) return;
    // 默认摆出「基线 vs 本轮选中的候选」，也就是运行卡片上已经展示的那一对，
    // 用户再改成任意两个 Trial。
    const selected =
      trials.find((trial) => trial.candidate_tagger_version_id != null) ??
      trials[trials.length - 1];
    const baseline =
      trials.find((trial) => trial.id !== selected.id) ?? trials[0];
    setLeftTrialId(baseline.id);
    setRightTrialId(selected.id);
  }, [leftTrialId, trials]);

  const sameTrial =
    leftTrialId !== null &&
    rightTrialId !== null &&
    leftTrialId === rightTrialId;
  const comparison = compareMutation.data ?? null;
  const trialName = (trialId: number | null): string => {
    const trial = trials.find((item) => item.id === trialId);
    return trial ? trialLabel(trial) : `Trial #${trialId ?? "—"}`;
  };

  return (
    <div className="ag-dialog-backdrop">
      <section
        className="ag-governance-dialog ag-trial-compare-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="trial-compare-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
        }}
      >
        <header>
          <div>
            <span className="ag-card-kicker">TRIAL COMPARISON</span>
            <h2 id="trial-compare-dialog-title">对比运行 #{runId} 的 Trial</h2>
          </div>
          <button type="button" aria-label="关闭 Trial 对比" onClick={onClose}>
            <IconClose />
          </button>
        </header>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (leftTrialId === null || rightTrialId === null || sameTrial) {
              return;
            }
            compareMutation.mutate({ left: leftTrialId, right: rightTrialId });
          }}
        >
          <PanelState
            pending={detailQuery.isPending}
            error={detailQuery.error}
            empty={trials.length < 2}
            emptyTitle="可对比的 Trial 不足"
            emptyDescription="本次运行还没有产生两个以上 Trial，等有界搜索推进后再来对比。"
            onRetry={() => void detailQuery.refetch()}
            pendingLabel="正在加载本次运行的 Trial…"
          >
            <label>
              左侧 Trial
              <select
                aria-label="左侧 Trial"
                value={leftTrialId ?? ""}
                onChange={(event) => {
                  compareMutation.reset();
                  setLeftTrialId(Number(event.target.value));
                }}
              >
                {trials.map((trial) => (
                  <option key={trial.id} value={trial.id}>
                    {trialLabel(trial)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              右侧 Trial
              <select
                aria-label="右侧 Trial"
                value={rightTrialId ?? ""}
                onChange={(event) => {
                  compareMutation.reset();
                  setRightTrialId(Number(event.target.value));
                }}
              >
                {trials.map((trial) => (
                  <option key={trial.id} value={trial.id}>
                    {trialLabel(trial)}
                  </option>
                ))}
              </select>
            </label>
            {sameTrial && (
              <p className="ag-inline-feedback is-error" role="alert">
                两侧必须选择不同的 Trial。
              </p>
            )}
            {compareMutation.error !== null &&
            compareMutation.error !== undefined ? (
              <p className="ag-inline-feedback is-error" role="alert">
                {getErrorMessage(
                  compareMutation.error,
                  "Trial 对比失败，请稍后重试。",
                )}
              </p>
            ) : null}
            {comparison && (
              <section
                className="ag-trial-compare-result"
                aria-label={`运行 ${runId} 的 Trial 对比结果`}
              >
                <p
                  className={`ag-inline-feedback ${
                    comparison.recommendation?.trial_id != null
                      ? "is-success"
                      : "is-error"
                  }`}
                  role="status"
                >
                  {comparison.recommendation?.trial_id != null
                    ? `推荐 ${trialName(comparison.recommendation.trial_id)}：${
                        RECOMMENDATION_BASIS_LABELS[
                          comparison.recommendation.basis
                        ] ?? comparison.recommendation.basis
                      }`
                    : "两个 Trial 的奖励向量还不完整，暂时给不出推荐。"}
                </p>
                <h3>指标差异（右侧 − 左侧）</h3>
                <DeltaList
                  ariaLabel="Trial 指标差异"
                  emptyCopy="两个 Trial 没有可比的数值指标。"
                  deltas={comparison.metric_deltas}
                />
                <h3>奖励向量差异</h3>
                <DeltaList
                  ariaLabel="Trial 奖励向量差异"
                  emptyCopy="两个 Trial 没有可比的奖励分量。"
                  deltas={comparison.reward_deltas}
                />
                <div className="ag-candidate-outcomes">
                  <span>改善 {comparison.improved_badcase_count}</span>
                  <span>退化 {comparison.regressed_badcase_count}</span>
                </div>
                <h3>六维 Harness 差异</h3>
                <HarnessDiffList
                  comparison={comparison}
                  ariaLabel="Trial 对比六维差异"
                />
              </section>
            )}
          </PanelState>
          <footer>
            <button type="button" className="is-secondary" onClick={onClose}>
              关闭
            </button>
            <button
              type="submit"
              disabled={
                compareMutation.isPending ||
                sameTrial ||
                leftTrialId === null ||
                rightTrialId === null
              }
            >
              {compareMutation.isPending ? "正在对比…" : "对比 Trial"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function QualityOverview({ overview }: { overview: TagEvolutionOverview }) {
  const quality = overview.quality;
  const feedback = overview.feedback;
  const drift = overview.drift;
  return (
    <>
      <section className="ag-evolution-production">
        <div>
          <span className="ag-card-kicker">PRODUCTION HARNESS</span>
          <h2>{overview.production_harness?.version ?? "尚无生产 Harness"}</h2>
          <p>
            {overview.production_harness
              ? `#${overview.production_harness.id} · ${overview.production_harness.status}`
              : "候选通过离线门禁与灰度后，才可由管理员批准进入生产。"}
          </p>
        </div>
        <span className={`ag-drift-state is-${drift.status}`}>
          {drift.status === "stable"
            ? "分布稳定"
            : drift.status === "watch"
              ? "观察漂移"
              : "晋级暂停"}
        </span>
      </section>
      <div className="ag-evolution-metric-grid">
        <article>
          <span>无偏 Macro F1</span>
          <strong>{percent(quality.unbiased_macro_f1)}</strong>
          <small>Representative Audit · IPW</small>
        </article>
        <article>
          <span>关键召回 LCB</span>
          <strong>{percent(quality.critical_recall_lcb)}</strong>
          <small>Wilson 95% 下界</small>
        </article>
        <article>
          <span>证据 IoU</span>
          <strong>{percent(quality.evidence_iou)}</strong>
          <small>时间区间一致性</small>
        </article>
        <article>
          <span>最差 Slice F1</span>
          <strong>{percent(quality.worst_slice_f1)}</strong>
          <small>防止总体分数掩盖退化</small>
        </article>
      </div>
      <section className="ag-evolution-health-grid">
        <article>
          <header>
            <span>反馈健康度</span>
            {feedback.next_run_eligible ? (
              <IconCheckCircle aria-label="已满足优化样本门槛" />
            ) : (
              <IconExclamationCircle aria-label="尚未满足优化样本门槛" />
            )}
          </header>
          <dl>
            <div>
              <dt>合格反馈</dt>
              <dd>{count(feedback.eligible_count)}</dd>
            </div>
            <div>
              <dt>本轮新增</dt>
              <dd>{count(feedback.new_since_last_run)}</dd>
            </div>
            <div>
              <dt>随机审计</dt>
              <dd>{count(feedback.representative_audit_count)}</dd>
            </div>
            <div>
              <dt>已仲裁</dt>
              <dd>{count(feedback.adjudicated_count)}</dd>
            </div>
          </dl>
          {feedback.blockers.length > 0 && (
            <ul>
              {feedback.blockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          )}
        </article>
        <article>
          <header>
            <span>漂移信号</span>
            <small>
              {drift.affected_slices?.join(" · ") || "无受影响 Slice"}
            </small>
          </header>
          <dl>
            <div>
              <dt>输入 PSI</dt>
              <dd>{drift.input_psi?.toFixed(3) ?? "—"}</dd>
            </div>
            <div>
              <dt>输出 JSD</dt>
              <dd>{drift.output_jsd?.toFixed(3) ?? "—"}</dd>
            </div>
          </dl>
          <p>漂移只暂停晋级并触发随机复核，不单独触发生产回滚。</p>
        </article>
      </section>
    </>
  );
}

function ReleaseSupport({ overview }: { overview: TagEvolutionOverview }) {
  const release = overview.release;
  if (!release) {
    return (
      <section className="ag-evolution-section">
        <header>
          <h2>灰度真值支持</h2>
        </header>
        <p className="ag-compact-state">当前没有进行中的 Harness 灰度。</p>
      </section>
    );
  }
  const counters = [
    ["Served", release.served_count],
    ["Paired", release.paired_count],
    ["Audited", release.audited_count],
    ["Adjudicated", release.adjudicated_count],
  ] as const;
  return (
    <section className="ag-evolution-section">
      <header>
        <div>
          <span className="ag-card-kicker">RELEASE TRUTH SUPPORT</span>
          <h2>灰度计数与等待原因</h2>
        </div>
        <span>{release.stage}</span>
      </header>
      <div className="ag-release-counter-grid">
        {counters.map(([label, value]) => (
          <article key={label}>
            <span>{label}</span>
            <strong>{count(value)}</strong>
          </article>
        ))}
      </div>
      {release.waiting_reasons.length > 0 ? (
        <ul className="ag-release-waiting-list">
          {release.waiting_reasons.map((reason) => (
            <li key={reason}>
              <IconExclamationCircle aria-hidden="true" />
              {reason}
            </li>
          ))}
        </ul>
      ) : (
        <p className="ag-inline-feedback is-success">
          当前阶段样本与时间门禁已满足。
        </p>
      )}
    </section>
  );
}

function BadcaseRegistry({ items }: { items: TagBadcase[] }) {
  return (
    <section className="ag-evolution-section">
      <header>
        <div>
          <span className="ag-card-kicker">BADCASE REGISTRY</span>
          <h2>错误簇与根因</h2>
        </div>
        <span>{items.length} 个当前错误簇</span>
      </header>
      {items.length === 0 ? (
        <p className="ag-compact-state">当前没有开放的结构化 Badcase。</p>
      ) : (
        <div className="ag-badcase-grid">
          {items.map((badcase) => {
            const rootCause = badcase.root_cause ?? {};
            const affectedSlices =
              badcase.affected_slices ??
              (Array.isArray(rootCause.affected_slices)
                ? rootCause.affected_slices.filter(
                    (item): item is string => typeof item === "string",
                  )
                : []);
            const excerpt =
              badcase.representative_excerpt ??
              (typeof rootCause.representative_excerpt === "string"
                ? rootCause.representative_excerpt
                : null);
            const regressionState =
              badcase.last_regression_result ??
              (typeof badcase.regression_result?.status === "string"
                ? badcase.regression_result.status
                : badcase.status);
            return (
              <article key={badcase.id}>
                <header>
                  <span>{failureStageLabel(badcase.failure_stage)}</span>
                  <code>{badcase.tag_key}</code>
                  <small>
                    {count(badcase.support_count ?? badcase.occurrence_count)}{" "}
                    个样本
                  </small>
                </header>
                <h3>{badcase.cluster_label ?? badcase.failure_mode}</h3>
                {excerpt && <blockquote>{excerpt}</blockquote>}
                <footer>
                  <span>{affectedSlices.join(" · ") || "全局"}</span>
                  <b>{regressionState}</b>
                </footer>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function OptimizationRuns({
  items,
  isAdmin,
  cancellingRunId,
  onCancel,
  onCompareTrials,
  onReoptimize,
}: {
  items: TagOptimizationRun[];
  isAdmin: boolean;
  cancellingRunId: number | null;
  onCancel: (runId: number) => void;
  onCompareTrials: (runId: number) => void;
  /** Holdout 未通过时的恢复路径：带原 cohort 重新打开优化对话框。 */
  onReoptimize: (run: TagOptimizationRun) => void;
}) {
  return (
    <section className="ag-evolution-section">
      <header>
        <div>
          <span className="ag-card-kicker">OPTIMIZATION RUNS</span>
          <h2>有界搜索与候选差异</h2>
        </div>
        <span>最多 32 个 Trial / 运行</span>
      </header>
      {items.length === 0 ? (
        <p className="ag-compact-state">尚无优化运行。</p>
      ) : (
        <div className="ag-optimization-run-list">
          {items.map((run) => {
            const completedTrials =
              run.completed_trials ??
              run.trials?.filter((trial) =>
                ["completed", "pruned", "failed", "cancelled"].includes(
                  trial.status,
                ),
              ).length ??
              numericSummary(run.summary, "completed_trials") ??
              0;
            const totalTrials =
              run.total_trials ??
              numericSummary(run.summary, "trial_count") ??
              run.search_budget.max_trials;
            const max = Math.max(totalTrials, 1);
            const comparison = candidateComparison(run);
            const candidateId =
              run.winner_tagger_version_id ??
              run.candidate_tagger_version_id ??
              null;
            const candidateLabel =
              run.candidate_version ??
              (candidateId ? `候选 #${candidateId}` : "候选搜索中");
            // 密封 Holdout 结论由服务端写进 summary（tag_evaluator 完成时落
            // winner / evaluation_run_id / holdout_passed），完成卡必须把
            // 结论和下一步动作渲染出来，而不是只留一个候选编号。
            const holdoutPassed =
              typeof run.summary.holdout_passed === "boolean"
                ? run.summary.holdout_passed
                : null;
            const evaluationRunId = numericSummary(
              run.summary,
              "evaluation_run_id",
            );
            return (
              <article key={run.id}>
                <header>
                  <div>
                    <span className="ag-card-kicker">运行 #{run.id}</span>
                    <h3>
                      {candidateId !== null ? (
                        // 原生 a + hash 链接：本面板可脱离 Router 渲染，见
                        // 上方提示词实验室入口的说明。
                        <a href="#/tag-governance?tab=taggers">
                          {candidateLabel}
                        </a>
                      ) : (
                        candidateLabel
                      )}
                    </h3>
                    <p>
                      基线 #{run.baseline_tagger_version_id}
                      {run.baseline_version
                        ? ` ${run.baseline_version}`
                        : ""} · {cohortLabel(run.cohort)}
                    </p>
                  </div>
                  <div className="ag-optimization-run-actions">
                    <span>{runStatus(run.status)}</span>
                    {/*
                      对比只读 Trial 指标，后端是 require_inspector_or_above，
                      和本面板其它读接口同级，所以不额外按 isAdmin 收窄。
                    */}
                    <button
                      type="button"
                      className="is-compare"
                      aria-label={`对比运行 ${run.id} 的 Trial`}
                      onClick={() => onCompareTrials(run.id)}
                    >
                      对比 Trial
                    </button>
                    {isAdmin && ACTIVE_RUN_STATUSES.has(run.status) && (
                      <button
                        type="button"
                        aria-label={`取消优化运行 ${run.id}`}
                        disabled={cancellingRunId === run.id}
                        onClick={() => onCancel(run.id)}
                      >
                        {cancellingRunId === run.id ? "取消中…" : "取消运行"}
                      </button>
                    )}
                  </div>
                </header>
                <div className="ag-run-progress">
                  <progress
                    aria-label={`优化运行 ${run.id} 进度`}
                    aria-valuemin={0}
                    aria-valuemax={max}
                    aria-valuenow={completedTrials}
                    value={completedTrials}
                    max={max}
                  />
                  <span>
                    {completedTrials} / {totalTrials} Trial
                  </span>
                </div>
                {comparison && (
                  <>
                    <HarnessDiffList
                      comparison={comparison}
                      ariaLabel="候选 Harness 六维差异"
                    />
                    <div className="ag-candidate-outcomes">
                      <span>
                        Macro F1{" "}
                        <b>
                          {signedPercent(
                            comparison.metric_deltas.macro_f1 ??
                              comparison.reward_deltas?.quality_delta,
                          )}
                        </b>
                      </span>
                      <span>
                        复核率{" "}
                        <b>
                          {signedPercent(
                            comparison.metric_deltas.review_rate ??
                              comparison.reward_deltas?.review_rate_delta,
                          )}
                        </b>
                      </span>
                      <span>改善 {comparison.improved_badcase_count}</span>
                      <span>退化 {comparison.regressed_badcase_count}</span>
                    </div>
                  </>
                )}
                {run.status === "completed" && holdoutPassed !== null && (
                  <footer
                    className="ag-optimization-outcome"
                    aria-label={`优化运行 ${run.id} 密封评估结论`}
                  >
                    <span
                      className={`ag-gate-badge ${
                        holdoutPassed ? "is-pass" : "is-fail"
                      }`}
                      role="status"
                    >
                      {holdoutPassed
                        ? "Sealed Holdout 通过"
                        : "Sealed Holdout 未通过"}
                    </span>
                    {holdoutPassed ? (
                      <>
                        {evaluationRunId !== null && (
                          <span>评估 #{evaluationRunId}</span>
                        )}
                        {evaluationRunId !== null && (
                          <a
                            href={`#/tag-governance?tab=deployments&deploy_evaluation_id=${evaluationRunId}`}
                          >
                            创建影子部署
                          </a>
                        )}
                      </>
                    ) : (
                      <>
                        <span>
                          {run.failure_reason ??
                            "候选未通过密封 Holdout 门禁，不能进入发布。"}
                        </span>
                        {isAdmin && (
                          <button
                            type="button"
                            aria-label={`基于运行 ${run.id} 重新优化`}
                            onClick={() => onReoptimize(run)}
                          >
                            重新优化
                          </button>
                        )}
                      </>
                    )}
                  </footer>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

export function EvolutionPanel({
  isAdmin,
  initialDialog = null,
  initialCohort = null,
}: EvolutionPanelProps) {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(
    isAdmin && initialDialog === "optimize",
  );
  const [cancelRunId, setCancelRunId] = useState<number | null>(null);
  const [compareRunId, setCompareRunId] = useState<number | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  // 「重新优化」沿用失败运行的 cohort，让重跑针对同一批反馈样本。
  const [reoptimizeCohort, setReoptimizeCohort] =
    useState<TagOptimizationSourceCohort | null>(null);
  const cohort = useMemo<TagOptimizationSourceCohort>(
    () => initialCohort ?? { source: "eligible_feedback" },
    [initialCohort],
  );
  useEffect(() => {
    if (isAdmin && initialDialog === "optimize") setDialogOpen(true);
  }, [initialDialog, isAdmin]);

  const overviewQuery = useQuery({
    queryKey: ["tag-governance", "evolution", "overview"],
    queryFn: getTagEvolutionOverview,
    retry: false,
    refetchInterval: 15_000,
  });
  const badcasesQuery = useQuery({
    queryKey: ["tag-governance", "evolution", "badcases"],
    queryFn: () => listTagBadcases({ limit: 50 }),
    retry: false,
  });
  const runsQuery = useQuery({
    queryKey: ["tag-governance", "evolution", "runs"],
    queryFn: listTagOptimizationRuns,
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.items.some((run) => ACTIVE_RUN_STATUSES.has(run.status))
        ? 3_000
        : false,
  });
  const createMutation = useMutation({
    mutationFn: (body: CreateTagOptimizationRunRequest) =>
      createTagOptimizationRun(body),
    onSuccess: (run) => {
      setDialogOpen(false);
      setReoptimizeCohort(null);
      setSuccess(
        run.summary.coverage_gate_passed === false
          ? `优化运行 #${run.id} 未启动：可信反馈覆盖不足`
          : run.status === "completed"
          ? `优化运行 #${run.id} 已完成`
          : `优化运行 #${run.id} 已进入队列`,
      );
      queryClient.invalidateQueries({
        queryKey: ["tag-governance", "evolution", "runs"],
      });
    },
  });
  const cancelMutation = useMutation({
    mutationFn: (runId: number) => cancelTagOptimizationRun(runId),
    onMutate: () => setSuccess(null),
    onSuccess: (run) => {
      setCancelRunId(null);
      setSuccess(`优化运行 #${run.id} 已取消`);
      queryClient.invalidateQueries({
        queryKey: ["tag-governance", "evolution", "runs"],
      });
      queryClient.invalidateQueries({
        queryKey: ["tag-governance", "evolution", "overview"],
      });
    },
  });

  const topError =
    overviewQuery.error ?? badcasesQuery.error ?? runsQuery.error;
  const panelError =
    topError ??
    (!dialogOpen ? createMutation.error : null) ??
    (cancelRunId === null ? cancelMutation.error : null) ??
    null;

  return (
    <>
      <div className="ag-panel-toolbar">
        <div>
          <strong>语义标签 Harness 自进化</strong>
          <span>
            用分层真值驱动有界策略搜索，候选自动离线、Shadow
            与灰度，生产由管理员批准。
          </span>
        </div>
        <div className="ag-panel-toolbar__actions">
          {/*
            有界搜索只调超参；提示词本身的编译在实验室里做。
            用原生 a + hash 链接而不是 <Link>：本面板由 props 驱动、可脱离 Router
            单独渲染，它的测试就是这么做的——引入路由依赖会把这个性质弄丢。
          */}
          <a href="#/prompt-lab?tab=compile" className="ag-plab-entry">
            提示词实验室
          </a>
          {isAdmin && (
            <button
              type="button"
              onClick={() => {
                setSuccess(null);
                createMutation.reset();
                setReoptimizeCohort(null);
                setDialogOpen(true);
              }}
            >
              <IconThunderbolt aria-hidden="true" /> 启动优化
            </button>
          )}
        </div>
      </div>
      {success && (
        <p className="ag-inline-feedback is-success" role="status">
          {success}
        </p>
      )}
      {panelError && (
        <p className="ag-inline-feedback is-error" role="alert">
          {getErrorMessage(panelError, "自进化数据暂不可用")}
        </p>
      )}
      <PanelState
        pending={overviewQuery.isPending}
        error={overviewQuery.error}
        empty={!overviewQuery.data}
        emptyTitle="暂无质量基线"
        emptyDescription="完成一次评估后，这里会显示自进化的质量基线与发布支撑材料。"
        onRetry={() => void overviewQuery.refetch()}
        pendingLabel="正在加载自进化质量基线…"
      >
        {overviewQuery.data && (
          <>
            <QualityOverview overview={overviewQuery.data} />
            <ReleaseSupport overview={overviewQuery.data} />
          </>
        )}
      </PanelState>
      <BadcaseRegistry items={badcasesQuery.data?.items ?? []} />
      <OptimizationRuns
        items={runsQuery.data?.items ?? []}
        isAdmin={isAdmin}
        cancellingRunId={
          cancelMutation.isPending ? (cancelMutation.variables ?? null) : null
        }
        onCancel={(runId) => {
          cancelMutation.reset();
          setSuccess(null);
          setCancelRunId(runId);
        }}
        onCompareTrials={(runId) => setCompareRunId(runId)}
        onReoptimize={(run) => {
          setSuccess(null);
          createMutation.reset();
          setReoptimizeCohort(run.cohort);
          setDialogOpen(true);
        }}
      />
      {dialogOpen && (
        <OptimizationDialog
          cohort={reoptimizeCohort ?? cohort}
          overview={overviewQuery.data}
          pending={createMutation.isPending}
          mutationError={createMutation.error}
          onClose={() => {
            createMutation.reset();
            setReoptimizeCohort(null);
            setDialogOpen(false);
          }}
          onCreate={(body) => createMutation.mutate(body)}
        />
      )}
      {compareRunId !== null && (
        <TrialComparisonDialog
          runId={compareRunId}
          onClose={() => setCompareRunId(null)}
        />
      )}
      {cancelRunId !== null && (
        <CancelOptimizationDialog
          runId={cancelRunId}
          pending={cancelMutation.isPending}
          mutationError={cancelMutation.error}
          onClose={() => {
            cancelMutation.reset();
            setCancelRunId(null);
          }}
          onConfirm={() => cancelMutation.mutate(cancelRunId)}
        />
      )}
    </>
  );
}
