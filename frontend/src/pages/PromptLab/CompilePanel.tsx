import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { PanelState } from "@/components/PanelState";
import { GovernanceDialog } from "@/components/governance/GovernanceDialog";
import { StatusChip } from "@/components/governance/StatusChip";
import { compactCount, formatDate } from "@/components/governance/format";
import {
  createPromptCompilation,
  getTagJob,
  listPromptArtifacts,
  listTaggerVersions,
} from "@/api/services";
import type {
  CreatePromptCompilationRequest,
  PromptArtifactStatus,
  PromptArtifactSummary,
  PromptCompilerName,
  PromptLabReadiness,
  TagJobStatus,
} from "@/types/api";
import { getErrorMessage } from "@/utils/errors";

import { PROMPT_LAB_STATUS_LABELS } from "./labels";

/** 任务不会再变的状态。轮询到这里就停，横幅从此说的是结论而不是「进行中」。 */
const TERMINAL_JOB_STATUSES: TagJobStatus[] = [
  "succeeded",
  "completed",
  "failed",
  "cancelled",
];

const COMPILERS: { id: PromptCompilerName; label: string; note: string }[] = [
  { id: "builtin", label: "内置（无模型）", note: "按错误簇套用固定句式，不消耗 Provider 预算" },
  {
    id: "builtin_grounded",
    label: "内置（模型改写）",
    note: "由强模型依据错误簇写规则，校验不过则回落模板",
  },
  { id: "dspy_mipro", label: "DSPy MIPRO", note: "指令重写，需要 optimizer extras" },
  { id: "dspy_bootstrap", label: "DSPy Bootstrap", note: "示例自举，需要 optimizer extras" },
  { id: "dspy_gepa", label: "DSPy GEPA", note: "反思式演化，需要 optimizer extras" },
  {
    id: "textgrad_tgd",
    label: "TextGrad 梯度",
    note: "逐簇诊断后定向改写规则，需要 optimizer extras",
  },
];

const STATUS_FILTERS: { id: PromptArtifactStatus | "all"; label: string }[] = [
  { id: "all", label: "全部状态" },
  { id: "draft", label: "草稿" },
  { id: "review", label: "待复核" },
  { id: "accepted", label: "已采纳" },
  { id: "rejected", label: "已拒绝" },
  { id: "superseded", label: "已被取代" },
];

interface CompilerForm {
  baselineId: string;
  goldSetVersionId: string;
  compiler: PromptCompilerName;
  maxPatches: string;
  minClusterSupport: string;
  demoCount: "0" | "2" | "4";
  redactionMode: "synthetic" | "masked";
  maxPromptTokens: string;
  efficiencyPolicy: "quality_uplift_v1" | "token_reduction_v1";
  maxProviderCalls: string;
  maxProviderTokens: string;
  maxCostMicrounits: string;
  maxWallSeconds: string;
}

const INITIAL_FORM: CompilerForm = {
  baselineId: "",
  goldSetVersionId: "",
  compiler: "builtin",
  maxPatches: "8",
  minClusterSupport: "3",
  demoCount: "0",
  redactionMode: "synthetic",
  maxPromptTokens: "3072",
  efficiencyPolicy: "quality_uplift_v1",
  maxProviderCalls: "120",
  maxProviderTokens: "1500000",
  maxCostMicrounits: "2000000",
  maxWallSeconds: "1800",
};

/**
 * `max` 省略表示后端没有业务上界（如 max_cost_microunits 只有 ge=1）。
 *
 * 但无论有没有上界，都用 isSafeInteger 而不是 isInteger：超出安全范围的输入会被
 * Number() 静默取整到最近的可表示值（"9007199254740993" → …992），列是 BIGINT 收得下，
 * 于是后端记账用的是一个用户从没填过的数字。
 */
function bounded(
  raw: string,
  { min, max, label }: { min: number; max?: number; label: string },
): { value: number } | { error: string } {
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < min || (max !== undefined && value > max)) {
    return {
      error:
        max === undefined
          ? `${label}必须是不小于 ${min} 的整数。`
          : `${label}必须是 ${min} 到 ${max} 之间的整数。`,
    };
  }
  return { value };
}

/**
 * 客户端完整复刻后端 schemas/prompt_lab.py 的上下界。
 *
 * 后端用 StrictModel，越界会直接 422——让用户提交之后才知道哪个字段填错了是很差
 * 的体验，而这些边界是稳定契约，值得在两边各写一份。
 */
function buildRequest(
  form: CompilerForm,
): { body: CreatePromptCompilationRequest } | { error: string } {
  const baselineId = Number(form.baselineId);
  if (!Number.isInteger(baselineId) || baselineId <= 0) {
    return { error: "请选择一个基线抽取版本。" };
  }
  const checks = [
    bounded(form.maxPatches, { min: 1, max: 32, label: "补丁上限" }),
    bounded(form.minClusterSupport, { min: 1, max: 100, label: "最小簇支撑" }),
    bounded(form.maxPromptTokens, { min: 512, max: 8192, label: "Prompt token 上限" }),
    bounded(form.maxProviderCalls, { min: 1, max: 1000, label: "调用次数上限" }),
    bounded(form.maxProviderTokens, {
      min: 1000,
      max: 50_000_000,
      label: "Token 上限",
    }),
    bounded(form.maxCostMicrounits, { min: 1, label: "成本上限" }),
    bounded(form.maxWallSeconds, { min: 60, max: 7200, label: "耗时上限" }),
  ];
  const failed = checks.find((check) => "error" in check);
  if (failed && "error" in failed) return { error: failed.error };
  const [
    maxPatches,
    minClusterSupport,
    maxPromptTokens,
    maxProviderCalls,
    maxProviderTokens,
    maxCostMicrounits,
    maxWallSeconds,
  ] = checks.map((check) => ("value" in check ? check.value : 0));

  let goldSetVersionId: number | undefined;
  if (form.goldSetVersionId.trim()) {
    const parsed = Number(form.goldSetVersionId);
    if (!Number.isInteger(parsed) || parsed <= 0) {
      return { error: "金标集版本 ID 必须是正整数，或留空。" };
    }
    goldSetVersionId = parsed;
  }

  return {
    body: {
      baseline_tagger_version_id: baselineId,
      ...(goldSetVersionId === undefined ? {} : { gold_set_version_id: goldSetVersionId }),
      compiler: {
        compiler: form.compiler,
        max_patches: maxPatches,
        min_cluster_support: minClusterSupport,
        instruction_candidates: 4,
        textgrad_iterations: 2,
        demo_count: Number(form.demoCount) as 0 | 2 | 4,
        redaction_mode: form.redactionMode,
        max_prompt_tokens: maxPromptTokens,
        efficiency_policy: form.efficiencyPolicy,
        seed: 0,
      },
      budget: {
        max_provider_calls: maxProviderCalls,
        max_provider_tokens: maxProviderTokens,
        max_cost_microunits: maxCostMicrounits,
        max_wall_seconds: maxWallSeconds,
      },
    },
  };
}

function BudgetCeiling({ form }: { form: CompilerForm }) {
  const summary = useMemo(() => {
    const calls = Number(form.maxProviderCalls);
    const tokens = Number(form.maxProviderTokens);
    const cost = Number(form.maxCostMicrounits);
    const wall = Number(form.maxWallSeconds);
    return {
      cost: Number.isFinite(cost) ? (cost / 1_000_000).toFixed(2) : "—",
      tokens: Number.isFinite(tokens) ? compactCount(tokens) : "—",
      perCall:
        Number.isFinite(tokens) && Number.isFinite(calls) && calls > 0
          ? compactCount(Math.floor(tokens / calls))
          : "—",
      minutes: Number.isFinite(wall) ? Math.round(wall / 60) : "—",
    };
  }, [form.maxCostMicrounits, form.maxProviderCalls, form.maxProviderTokens, form.maxWallSeconds]);

  return (
    <div className="ag-plab-cost-estimate">
      {/* 这四个字段都是 cap，不是预估账单——标题必须说清楚，否则会被当成报价。 */}
      <span className="ag-card-kicker">预算上限（非预估账单）</span>
      <dl>
        <div>
          <dt>最坏成本</dt>
          <dd>≤ ¥{summary.cost}</dd>
        </div>
        <div>
          <dt>最坏 Token</dt>
          <dd>≤ {summary.tokens}</dd>
        </div>
        <div>
          <dt>平均每次调用</dt>
          <dd>≤ {summary.perCall} token</dd>
        </div>
        <div>
          <dt>最长耗时</dt>
          <dd>≤ {summary.minutes} 分钟</dd>
        </div>
      </dl>
    </div>
  );
}

export function CompilePanel({
  isAdmin,
  readiness,
  selectedArtifactId,
  onSelectArtifact,
}: {
  isAdmin: boolean;
  readiness: PromptLabReadiness | undefined;
  selectedArtifactId: number | null;
  onSelectArtifact: (id: number) => void;
}) {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<PromptArtifactStatus | "all">("all");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [form, setForm] = useState<CompilerForm>(INITIAL_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  // 跟任务而不是跟产物：编译失败时永远不会有产物落库，只盯产物的话横幅会一直转，
  // 而失败原因只存在于任务行里——UI 没有任何别的途径能知道它失败了。
  const [pending, setPending] = useState<{ compilationId: number; jobId: number } | null>(
    null,
  );

  const artifacts = useQuery({
    queryKey: ["prompt-lab", "artifacts", statusFilter],
    queryFn: () =>
      listPromptArtifacts(
        statusFilter === "all" ? { limit: 50 } : { status: statusFilter, limit: 50 },
      ),
    retry: false,
  });

  // 编译是异步的：POST 只返回 compilation_id，产物由 worker 落库。轮询任务行，
  // 它一定会走到终态（succeeded / failed / cancelled），所以轮询一定会停。
  const job = useQuery({
    queryKey: ["tag-jobs", pending?.jobId],
    queryFn: () => getTagJob(pending!.jobId),
    enabled: pending !== null,
    retry: false,
    refetchInterval: (query) =>
      query.state.data && TERMINAL_JOB_STATUSES.includes(query.state.data.status)
        ? false
        : 3_000,
  });

  const taggers = useQuery({
    // 与治理页共用同一个 key：从那边跳过来是零请求。
    queryKey: ["tag-governance", "taggers"],
    queryFn: listTaggerVersions,
    enabled: dialogOpen,
    retry: false,
  });

  const compile = useMutation({
    mutationFn: createPromptCompilation,
    onSuccess: (result) => {
      setPending({ compilationId: result.compilation_id, jobId: result.job_id });
      setDialogOpen(false);
      setForm(INITIAL_FORM);
      void queryClient.invalidateQueries({ queryKey: ["prompt-lab", "artifacts"] });
    },
  });

  const items = artifacts.data?.items ?? [];
  const jobStatus = job.data?.status;
  const artifactLanded =
    pending !== null && items.some((item) => item.compilation_id === pending.compilationId);

  // 产物一落库就拉一次列表，否则要等下一次手动刷新才看得到。
  useEffect(() => {
    if (jobStatus === "succeeded" || jobStatus === "completed") {
      void queryClient.invalidateQueries({ queryKey: ["prompt-lab", "artifacts"] });
    }
  }, [jobStatus, queryClient]);
  const blockedReason = readiness && !readiness.ready
    ? "编译前置条件尚未满足，请先在「数据就绪」中查看还差什么。"
    : null;

  const submit = () => {
    const built = buildRequest(form);
    if ("error" in built) {
      setFormError(built.error);
      return;
    }
    setFormError(null);
    compile.mutate(built.body);
  };

  return (
    <div className="ag-plab-compile">
      <div className="ag-panel-toolbar">
        <div>
          <h2>编译运行</h2>
          <p>每次编译产出一个待复核的候选 Prompt，不会直接影响生产。</p>
        </div>
        <div className="ag-panel-toolbar__actions">
          <label className="ag-plab-inline-field">
            <span>状态</span>
            <select
              aria-label="按状态筛选产物"
              value={statusFilter}
              onChange={(event) =>
                setStatusFilter(event.target.value as PromptArtifactStatus | "all")
              }
            >
              {STATUS_FILTERS.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          {isAdmin && (
            <button
              type="button"
              disabled={Boolean(blockedReason)}
              onClick={() => {
                setFormError(null);
                setDialogOpen(true);
              }}
            >
              发起编译
            </button>
          )}
        </div>
      </div>

      {blockedReason && <p className="ag-compact-state">{blockedReason}</p>}
      {pending !== null && !artifactLanded && (
        <p
          className={
            jobStatus === "failed" || jobStatus === "cancelled"
              ? "ag-inline-feedback is-error"
              : "ag-inline-feedback is-success"
          }
          role={jobStatus === "failed" || jobStatus === "cancelled" ? "alert" : "status"}
        >
          {jobStatus === "failed"
            ? `编译失败：${job.data?.last_error_message ?? "未记录原因"}`
            : jobStatus === "cancelled"
              ? "编译任务已被取消。"
              : job.error
                ? `编译任务已入队（#${pending.jobId}），但状态查询失败：${getErrorMessage(
                    job.error,
                    "接口暂不可用",
                  )}`
                : "编译任务已入队，正在等待产物落库…"}
        </p>
      )}
      {compile.isError && !dialogOpen && (
        <p className="ag-inline-feedback is-error" role="alert">
          {getErrorMessage(compile.error, "编译任务创建失败，请稍后重试。")}
        </p>
      )}

      <PanelState
        pending={artifacts.isPending}
        error={artifacts.error}
        empty={items.length === 0}
        emptyTitle="尚无编译产物"
        emptyDescription="发起一次编译后，候选 Prompt 会出现在这里等待复核。"
        onRetry={() => void artifacts.refetch()}
      >
        <ul className="ag-plab-artifact-list">
          {items.map((item) => (
            <ArtifactCard
              key={item.id}
              item={item}
              selected={item.id === selectedArtifactId}
              onSelect={() => onSelectArtifact(item.id)}
            />
          ))}
        </ul>
      </PanelState>

      {dialogOpen && (
        <GovernanceDialog
          id="prompt-compile-dialog-title"
          kicker="OFFLINE PROMPT COMPILATION"
          title="发起 Prompt 编译"
          pending={compile.isPending}
          onClose={() => setDialogOpen(false)}
          onSubmit={submit}
          submitLabel="发起编译"
          pendingLabel="正在入队…"
          error={
            formError ??
            (compile.isError
              ? getErrorMessage(compile.error, "编译任务创建失败。")
              : null)
          }
        >
          <label>
            基线抽取版本
            <select
              autoFocus
              aria-label="基线抽取版本"
              value={form.baselineId}
              onChange={(event) =>
                setForm({ ...form, baselineId: event.target.value })
              }
            >
              <option value="">请选择…</option>
              {(taggers.data?.items ?? []).map((tagger) => (
                <option key={tagger.id} value={String(tagger.id)}>
                  #{tagger.id} {tagger.version}
                </option>
              ))}
            </select>
          </label>
          <label>
            金标集版本 ID（可选）
            <input
              aria-label="金标集版本 ID"
              inputMode="numeric"
              value={form.goldSetVersionId}
              onChange={(event) =>
                setForm({ ...form, goldSetVersionId: event.target.value })
              }
            />
          </label>
          <label className="is-full">
            编译器
            <select
              aria-label="编译器"
              value={form.compiler}
              onChange={(event) =>
                setForm({ ...form, compiler: event.target.value as PromptCompilerName })
              }
            >
              {COMPILERS.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label} — {option.note}
                </option>
              ))}
            </select>
          </label>
          <label>
            补丁上限
            <input
              aria-label="补丁上限"
              inputMode="numeric"
              value={form.maxPatches}
              onChange={(event) => setForm({ ...form, maxPatches: event.target.value })}
            />
          </label>
          <label>
            最小簇支撑
            <input
              aria-label="最小簇支撑"
              inputMode="numeric"
              value={form.minClusterSupport}
              onChange={(event) =>
                setForm({ ...form, minClusterSupport: event.target.value })
              }
            />
          </label>
          {/*
            两个控件都禁用：本版本没有任何编译器会产出 demo（两个 proposer 都写死
            demos=()），后端现在也会直接拒绝 demo_count>0。留着可选就是让用户
            提交一个必然被拒的请求，或者——在后端加守卫之前——拿到一个静默忽略了
            他选择的产物。等编译器真的能产出示例时，去掉 disabled 即可。
          */}
          <label>
            内联示例条数
            <select
              aria-label="内联示例条数"
              value={form.demoCount}
              disabled
              onChange={(event) =>
                setForm({ ...form, demoCount: event.target.value as "0" | "2" | "4" })
              }
            >
              <option value="0">0 条（本版本编译器不产出示例）</option>
            </select>
          </label>
          <label>
            示例脱敏方式
            {/* verbatim 是后端模型层的调试值，API 不接受，这里也不提供。 */}
            <select
              aria-label="示例脱敏方式"
              value={form.redactionMode}
              disabled
              onChange={(event) =>
                setForm({
                  ...form,
                  redactionMode: event.target.value as "synthetic" | "masked",
                })
              }
            >
              <option value="synthetic">合成改写（推荐）</option>
              <option value="masked">实体掩码</option>
            </select>
          </label>
          <label>
            Prompt token 上限
            <input
              aria-label="Prompt token 上限"
              inputMode="numeric"
              value={form.maxPromptTokens}
              onChange={(event) =>
                setForm({ ...form, maxPromptTokens: event.target.value })
              }
            />
          </label>
          <label>
            效率封套
            <select
              aria-label="效率封套"
              value={form.efficiencyPolicy}
              onChange={(event) =>
                setForm({
                  ...form,
                  efficiencyPolicy: event.target.value as
                    | "quality_uplift_v1"
                    | "token_reduction_v1",
                })
              }
            >
              <option value="quality_uplift_v1">提质优先（允许 Prompt 变长）</option>
              <option value="token_reduction_v1">降本优先（要求 Token 下降）</option>
            </select>
          </label>
          <label>
            调用次数上限
            <input
              aria-label="调用次数上限"
              inputMode="numeric"
              value={form.maxProviderCalls}
              onChange={(event) =>
                setForm({ ...form, maxProviderCalls: event.target.value })
              }
            />
          </label>
          <label>
            Token 上限
            <input
              aria-label="Token 上限"
              inputMode="numeric"
              value={form.maxProviderTokens}
              onChange={(event) =>
                setForm({ ...form, maxProviderTokens: event.target.value })
              }
            />
          </label>
          <label>
            成本上限（微单位）
            <input
              aria-label="成本上限"
              inputMode="numeric"
              value={form.maxCostMicrounits}
              onChange={(event) =>
                setForm({ ...form, maxCostMicrounits: event.target.value })
              }
            />
          </label>
          <label>
            耗时上限（秒）
            <input
              aria-label="耗时上限"
              inputMode="numeric"
              value={form.maxWallSeconds}
              onChange={(event) =>
                setForm({ ...form, maxWallSeconds: event.target.value })
              }
            />
          </label>
          <div className="is-full">
            <BudgetCeiling form={form} />
          </div>
        </GovernanceDialog>
      )}
    </div>
  );
}

function ArtifactCard({
  item,
  selected,
  onSelect,
}: {
  item: PromptArtifactSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        className={`ag-plab-artifact-card${selected ? " is-selected" : ""}`}
        aria-pressed={selected}
        aria-label={`查看产物 ${item.id} 的差异`}
        onClick={onSelect}
      >
        <span className="ag-plab-artifact-card__head">
          <span className="ag-card-kicker">产物 #{item.id}</span>
          <StatusChip status={item.status} labels={PROMPT_LAB_STATUS_LABELS} />
        </span>
        <span className="ag-plab-artifact-card__meta">
          编译 #{item.compilation_id} · {item.compiler} {item.compiler_version} ·
          基线 #{item.baseline_tagger_version_id}
        </span>
        <span className="ag-plab-artifact-card__meta">
          {compactCount(item.prompt_token_estimate)} token ·{" "}
          {item.accepted_patch_ids.length} 条已采纳 ·{" "}
          {item.redaction_report.demo_count} 个示例
        </span>
        {item.parent_artifact_id !== null && (
          <span className="ag-plab-artifact-card__meta">
            由 #{item.parent_artifact_id} 的复核决策派生
          </span>
        )}
        {!item.input_budget_report.fits && (
          <span className="ag-inline-feedback is-error">超出单次输入预算</span>
        )}
        <span className="ag-plab-artifact-card__meta">
          {formatDate(item.created_at)}
        </span>
      </button>
    </li>
  );
}
