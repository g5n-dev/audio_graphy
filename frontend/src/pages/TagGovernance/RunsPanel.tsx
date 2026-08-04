import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  getTagOptimizationRun,
  listTagJobs,
  listTagOptimizationRuns,
} from "@/api/services";
import type {
  TagJob,
  TagJobStatus,
  TagJobType,
  TagOptimizationRun,
} from "@/types/api";
import { PanelState } from "@/components/PanelState";
import { formatDate } from "@/components/governance/format";
import { StatusChip } from "@/components/governance/StatusChip";
import { getErrorMessage } from "@/utils/errors";

/**
 * 任务不会再变的状态。列表里只要还有一个非终态任务就继续轮询，全部落到这里就停——
 * 状态机没有从终态出去的边，所以轮询一定会停下来。
 */
const TERMINAL_JOB_STATUSES = new Set<TagJobStatus>([
  "completed",
  "succeeded",
  "failed",
  "cancelled",
]);

/** 优化运行的非终态；Trial 明细只在这两种状态下继续刷新。 */
const ACTIVE_RUN_STATUSES = new Set<TagOptimizationRun["status"]>([
  "queued",
  "running",
]);

const JOB_STATUS_LABELS: Readonly<Record<TagJobStatus, string>> = {
  queued: "排队中",
  running: "运行中",
  retry_wait: "等待重试",
  completed: "已完成",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const JOB_TYPE_LABELS: Readonly<Record<TagJobType, string>> = {
  extract: "标签抽取",
  recompute: "重新计算",
  review_batch: "复核批次",
  evaluate: "金标评估",
  optimize: "有界优化",
  remediate: "修复回填",
  prompt_compile: "提示词编译",
};

const JOB_ORIGIN_LABELS: Readonly<Record<string, string>> = {
  manual: "人工发起",
  serving: "在线服务",
  backfill: "历史回填",
  monitor: "监控触发",
  system: "系统调度",
};

/**
 * 筛选项刻意不含 `succeeded`：服务端的 CHECK 约束只允许 completed，
 * `succeeded` 只是历史 worker 留下的同义别名，两者合并进「已完成」这一个桶。
 */
const STATUS_FILTERS = [
  "queued",
  "running",
  "retry_wait",
  "completed",
  "failed",
  "cancelled",
] as const;

const TYPE_FILTERS = [
  "extract",
  "recompute",
  "review_batch",
  "evaluate",
  "optimize",
  "remediate",
  "prompt_compile",
] as const;

type StatusFilter = (typeof STATUS_FILTERS)[number] | "all";
type TypeFilter = (typeof TYPE_FILTERS)[number] | "all";

function matchesStatus(job: TagJob, filter: StatusFilter): boolean {
  if (filter === "all") return true;
  if (filter === "completed") {
    return job.status === "completed" || job.status === "succeeded";
  }
  return job.status === filter;
}

function progressPercent(job: TagJob): number {
  if (job.total_items <= 0) return 0;
  return Math.min(100, Math.round((job.completed_items / job.total_items) * 100));
}

/**
 * 优化运行的 Trial 明细。
 *
 * 列表接口 `GET /tag-optimization-runs` 只返回运行本身，`trials` 只有详情接口才带；
 * 所以这里必须再取一次详情，而不是从自进化面板已有的列表里读。
 */
function OptimizationRunDetail({ runId }: { runId: number }) {
  const query = useQuery({
    queryKey: ["tag-governance", "optimization-run", runId],
    queryFn: () => getTagOptimizationRun(runId),
    retry: false,
    refetchInterval: (currentQuery) => {
      const status = currentQuery.state.data?.status;
      return status && ACTIVE_RUN_STATUSES.has(status) ? 3_000 : false;
    },
  });

  if (query.isPending) {
    return (
      <p className="ag-compact-state" role="status">
        正在加载优化运行 #{runId} 的 Trial 明细…
      </p>
    );
  }
  if (query.isError) {
    return (
      <p className="ag-compact-state is-error" role="alert">
        Trial 明细加载失败：{getErrorMessage(query.error)}
        <button type="button" onClick={() => void query.refetch()}>
          重新加载
        </button>
      </p>
    );
  }

  const run = query.data;
  const trials = run.trials ?? [];
  return (
    <div className="ag-run-trials">
      <p className="ag-run-trials__summary">
        优化运行 #{run.id} · 阶段 {run.phase} · 基线 #
        {run.baseline_tagger_version_id}
        {run.winner_tagger_version_id
          ? ` · 胜出候选 #${run.winner_tagger_version_id}`
          : ""}
        {run.failure_reason ? ` · ${run.failure_reason}` : ""}
      </p>
      {trials.length === 0 ? (
        <p className="ag-compact-state">该运行尚未产生 Trial。</p>
      ) : (
        <ol className="ag-run-trials__list">
          {trials.map((trial) => (
            <li key={trial.id}>
              <span>Trial {trial.ordinal}</span>
              <strong>{trial.status}</strong>
              <small>
                {trial.mutation_dimension ?? "—"}
                {trial.elimination_reason ? ` · ${trial.elimination_reason}` : ""}
              </small>
            </li>
          ))}
        </ol>
      )}
      <Link to="/tag-governance?tab=evolution">在自进化面板查看候选差异</Link>
    </div>
  );
}

export function RunsPanel() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [expandedJobId, setExpandedJobId] = useState<number | null>(null);

  const jobsQuery = useQuery({
    queryKey: ["tag-governance", "jobs"],
    queryFn: listTagJobs,
    retry: false,
    // 判据看的是服务端返回的全量列表，不是筛选后的视图：把运行中的任务筛掉
    // 不该让它在后台停止刷新，否则切回「全部」时看到的是一份冻结的快照。
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (job) => !TERMINAL_JOB_STATUSES.has(job.status),
      )
        ? 3_000
        : false,
  });

  const items = useMemo(() => jobsQuery.data?.items ?? [], [jobsQuery.data]);
  const hasOptimizeJob = items.some((job) => job.job_type === "optimize");
  const optimizeRunning = items.some(
    (job) =>
      job.job_type === "optimize" && !TERMINAL_JOB_STATUSES.has(job.status),
  );

  // 任务行不带优化运行 ID（`_job_resource` 会把非 extract/recompute 的 scope 抹掉），
  // 所以反向索引只能来自运行列表的 job_id。与自进化面板共用同一个 queryKey：
  // 从那边切过来是零请求。
  const optimizationRunsQuery = useQuery({
    queryKey: ["tag-governance", "evolution", "runs"],
    queryFn: listTagOptimizationRuns,
    enabled: hasOptimizeJob,
    retry: false,
    refetchInterval: optimizeRunning ? 3_000 : false,
  });

  const runIdByJobId = useMemo(() => {
    const index = new Map<number, number>();
    for (const run of optimizationRunsQuery.data?.items ?? []) {
      if (typeof run.job_id === "number") index.set(run.job_id, run.id);
    }
    return index;
  }, [optimizationRunsQuery.data]);

  const filtered = useMemo(
    () =>
      items.filter(
        (job) =>
          matchesStatus(job, statusFilter) &&
          (typeFilter === "all" || job.job_type === typeFilter),
      ),
    [items, statusFilter, typeFilter],
  );

  const filtering = statusFilter !== "all" || typeFilter !== "all";
  const runningCount = items.filter(
    (job) => !TERMINAL_JOB_STATUSES.has(job.status),
  ).length;

  return (
    <>
      <div className="ag-panel-toolbar">
        <div>
          <strong>治理运行记录</strong>
          <span>
            抽取、评估、复核、优化与提示词编译都落在同一条任务流水上，进度、重试次数与失败原因可逐条追溯。
          </span>
        </div>
        <div className="ag-panel-toolbar__actions ag-runs-filters">
          <label>
            <span>状态</span>
            <select
              aria-label="按状态筛选运行"
              value={statusFilter}
              onChange={(event) =>
                setStatusFilter(event.target.value as StatusFilter)
              }
            >
              <option value="all">全部状态</option>
              {STATUS_FILTERS.map((status) => (
                <option key={status} value={status}>
                  {JOB_STATUS_LABELS[status]}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>类型</span>
            <select
              aria-label="按类型筛选运行"
              value={typeFilter}
              onChange={(event) =>
                setTypeFilter(event.target.value as TypeFilter)
              }
            >
              <option value="all">全部类型</option>
              {TYPE_FILTERS.map((jobType) => (
                <option key={jobType} value={jobType}>
                  {JOB_TYPE_LABELS[jobType]}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>
      {runningCount > 0 && (
        <p className="ag-inline-feedback is-success" role="status">
          {runningCount} 个运行尚未结束，列表每 3 秒自动刷新。
        </p>
      )}
      <PanelState
        pending={jobsQuery.isPending}
        error={jobsQuery.error}
        empty={filtered.length === 0}
        emptyTitle={filtering ? "没有符合条件的运行" : "暂无治理运行"}
        emptyDescription={
          filtering
            ? "换一个状态或类型再看，或者选择「全部」查看完整流水。"
            : "发起抽取、评估或优化后，运行会即时出现在这里。"
        }
        onRetry={() => void jobsQuery.refetch()}
        pendingLabel="正在加载治理运行…"
      >
        <div className="ag-version-table-wrap">
          <table className="ag-version-table ag-runs-table">
            <thead>
              <tr>
                <th>运行</th>
                <th>类型</th>
                <th>状态</th>
                <th>进度</th>
                <th>尝试</th>
                <th>创建时间</th>
                <th>结束时间</th>
                <th>操作</th>
              </tr>
            </thead>
            {filtered.map((job) => {
              const optimizationRunId =
                job.job_type === "optimize"
                  ? (runIdByJobId.get(job.id) ?? null)
                  : null;
              const expanded = expandedJobId === job.id;
              return (
                <tbody key={job.id}>
                  <tr>
                    <td>
                      <strong>#{job.id}</strong>
                      <small>
                        {job.origin
                          ? (JOB_ORIGIN_LABELS[job.origin] ?? job.origin)
                          : "—"}
                      </small>
                    </td>
                    <td>{JOB_TYPE_LABELS[job.job_type] ?? job.job_type}</td>
                    <td>
                      <StatusChip
                        status={job.status}
                        labels={JOB_STATUS_LABELS}
                      />
                    </td>
                    <td>
                      <strong>
                        {job.completed_items} / {job.total_items}
                      </strong>
                      <small>
                        {progressPercent(job)}%
                        {job.failed_items > 0
                          ? ` · 失败 ${job.failed_items}`
                          : ""}
                      </small>
                    </td>
                    <td>
                      {job.attempt_count} / {job.max_attempts}
                    </td>
                    <td>{formatDate(job.created_at)}</td>
                    <td>{formatDate(job.finished_at)}</td>
                    <td className="ag-runs-table__actions">
                      <Link to={`/tag-runs/${job.id}`}>查看详情</Link>
                      {optimizationRunId !== null && (
                        <button
                          type="button"
                          className="is-secondary"
                          aria-expanded={expanded}
                          aria-label={`查看运行 ${job.id} 的 Trial 明细`}
                          onClick={() =>
                            setExpandedJobId(expanded ? null : job.id)
                          }
                        >
                          {expanded ? "收起 Trial" : "Trial 明细"}
                        </button>
                      )}
                    </td>
                  </tr>
                  {expanded && optimizationRunId !== null && (
                    <tr className="ag-runs-table__detail">
                      <td colSpan={8}>
                        <OptimizationRunDetail runId={optimizationRunId} />
                      </td>
                    </tr>
                  )}
                </tbody>
              );
            })}
          </table>
        </div>
      </PanelState>
    </>
  );
}
