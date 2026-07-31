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
  createTagOptimizationRun,
  getTagEvolutionOverview,
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
}: {
  items: TagOptimizationRun[];
  isAdmin: boolean;
  cancellingRunId: number | null;
  onCancel: (runId: number) => void;
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
            return (
              <article key={run.id}>
                <header>
                  <div>
                    <span className="ag-card-kicker">运行 #{run.id}</span>
                    <h3>
                      {run.candidate_version ??
                        (candidateId ? `候选 #${candidateId}` : "候选搜索中")}
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
                    <ul
                      className="ag-harness-diff-list"
                      aria-label="候选 Harness 六维差异"
                    >
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
  const [success, setSuccess] = useState<string | null>(null);
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
        {isAdmin && (
          <button
            type="button"
            onClick={() => {
              setSuccess(null);
              createMutation.reset();
              setDialogOpen(true);
            }}
          >
            <IconThunderbolt aria-hidden="true" /> 启动优化
          </button>
        )}
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
      />
      {dialogOpen && (
        <OptimizationDialog
          cohort={cohort}
          overview={overviewQuery.data}
          pending={createMutation.isPending}
          mutationError={createMutation.error}
          onClose={() => {
            createMutation.reset();
            setDialogOpen(false);
          }}
          onCreate={(body) => createMutation.mutate(body)}
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
