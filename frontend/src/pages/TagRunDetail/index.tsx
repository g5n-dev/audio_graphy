import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  IconCheck,
  IconClockCircle,
  IconExclamation,
  IconExclamationCircleFill,
  IconLoading,
} from "@arco-design/web-react/icon";
import { Link, useParams } from "react-router-dom";
import { cancelTagJob, getTagJob, retryTagJob } from "@/api/services";
import type { TagJob, TagJobStatus } from "@/types/api";
import "../TagGovernance/tagGovernance.css";

const TERMINAL_STATUSES = new Set<TagJobStatus>([
  "completed",
  "succeeded",
  "failed",
  "cancelled",
]);

function statusLabel(status: TagJobStatus): string {
  return (
    {
      queued: "等待执行",
      running: "运行中",
      retry_wait: "等待重试",
      completed: "运行成功",
      succeeded: "运行成功",
      failed: "运行失败",
      cancelled: "已取消",
    } satisfies Record<TagJobStatus, string>
  )[status];
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp)
    ? new Date(timestamp).toLocaleString("zh-CN", { hour12: false })
    : value;
}

function scopeValues(scope: TagJob["scope"], key: string): string[] {
  const values = scope[key];
  return Array.isArray(values)
    ? values
        .filter(
          (value): value is string | number =>
            typeof value === "string" || typeof value === "number",
        )
        .map(String)
    : [];
}

function RunScope({ job }: { job: TagJob }) {
  const receptions = scopeValues(job.scope, "reception_ids");
  const units = scopeValues(job.scope, "dialogue_unit_ids");
  const stores = scopeValues(job.scope, "store_ids");
  const labels = scopeValues(job.scope, "label_keys");
  const groups = scopeValues(job.scope, "group_ids");
  const hasKnownScope =
    receptions.length ||
    units.length ||
    stores.length ||
    labels.length ||
    groups.length;

  return (
    <section className="ag-run-card" aria-labelledby="run-scope-title">
      <header>
        <div>
          <span className="ag-card-kicker">SCOPE SNAPSHOT</span>
          <h2 id="run-scope-title">运行范围</h2>
        </div>
      </header>
      <dl className="ag-run-scope">
        {receptions.length > 0 && (
          <div>
            <dt>接待</dt>
            <dd>接待 {receptions.join("、")}</dd>
          </div>
        )}
        {units.length > 0 && (
          <div>
            <dt>对话单元</dt>
            <dd>{units.join("、")}</dd>
          </div>
        )}
        {stores.length > 0 && (
          <div>
            <dt>门店</dt>
            <dd>{stores.join("、")}</dd>
          </div>
        )}
        {labels.length > 0 && (
          <div>
            <dt>标签键</dt>
            <dd>{labels.join("、")}</dd>
          </div>
        )}
        {groups.length > 0 && (
          <div>
            <dt>标签组</dt>
            <dd>{groups.join("、")}</dd>
          </div>
        )}
        {!hasKnownScope && (
          <div>
            <dt>原始范围</dt>
            <dd>
              <code>{JSON.stringify(job.scope)}</code>
            </dd>
          </div>
        )}
      </dl>
    </section>
  );
}

type CheckpointState = "pending" | "running" | "completed" | "failed";

function checkpoints(job: TagJob): Array<{
  key: string;
  label: string;
  help: string;
  state: CheckpointState;
}> {
  const success = job.status === "completed" || job.status === "succeeded";
  const started =
    job.status === "running" ||
    job.status === "retry_wait" ||
    success ||
    job.status === "failed";
  return [
    {
      key: "prepare",
      label: "准备输入",
      help: "冻结范围、标签体系和抽取版本",
      state: started ? "completed" : "running",
    },
    {
      key: "extract",
      label: "模型抽取",
      help: `已处理 ${job.completed_items} / ${job.total_items}`,
      state: success
        ? "completed"
        : job.status === "failed"
          ? "failed"
          : job.status === "running"
            ? "running"
            : "pending",
    },
    {
      key: "commit",
      label: "事实入库",
      help: "追加事实并更新 current 投影",
      state: success ? "completed" : "pending",
    },
  ];
}

function Checkpoints({ job }: { job: TagJob }) {
  return (
    <section className="ag-run-card" aria-labelledby="run-checkpoints-title">
      <header>
        <div>
          <span className="ag-card-kicker">CHECKPOINTS</span>
          <h2 id="run-checkpoints-title">执行检查点</h2>
        </div>
        <span>修订 #{job.revision}</span>
      </header>
      <ol className="ag-run-checkpoints">
        {checkpoints(job).map((checkpoint) => (
          <li key={checkpoint.key} className={`is-${checkpoint.state}`}>
            <span className="ag-run-checkpoint-icon" aria-hidden="true">
              {checkpoint.state === "completed"
                ? <IconCheck />
                : checkpoint.state === "failed"
                  ? <IconExclamation />
                  : checkpoint.state === "running"
                    ? <IconLoading />
                    : <IconClockCircle />}
            </span>
            <span>
              <strong>{checkpoint.label}</strong>
              <small>{checkpoint.help}</small>
            </span>
            <b>
              {
                {
                  pending: "待执行",
                  running: "执行中",
                  completed: "已完成",
                  failed: "失败",
                }[checkpoint.state]
              }
            </b>
          </li>
        ))}
      </ol>
    </section>
  );
}

function RunDetails({ job }: { job: TagJob }) {
  return (
    <section className="ag-run-card" aria-labelledby="run-details-title">
      <header>
        <div>
          <span className="ag-card-kicker">TRACEABILITY</span>
          <h2 id="run-details-title">运行溯源</h2>
        </div>
      </header>
      <dl className="ag-run-details">
        <div>
          <dt>任务类型</dt>
          <dd>{job.job_type}</dd>
        </div>
        <div>
          <dt>抽取版本</dt>
          <dd>#{job.tagger_version_id ?? "默认部署"}</dd>
        </div>
        <div>
          <dt>尝试次数</dt>
          <dd>
            {job.attempt_count} / {job.max_attempts}
          </dd>
        </div>
        <div>
          <dt>租约执行器</dt>
          <dd>{job.lease_owner ?? "—"}</dd>
        </div>
        <div>
          <dt>租约到期</dt>
          <dd>{formatDate(job.lease_expires_at)}</dd>
        </div>
        <div>
          <dt>更新时间</dt>
          <dd>{formatDate(job.updated_at)}</dd>
        </div>
      </dl>
    </section>
  );
}

function displayFailureRef(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function FailedSubset({ items }: { items: unknown[] }) {
  const visibleItems = items.slice(0, 50);
  return (
    <section
      className="ag-run-card ag-run-failed-subset"
      aria-labelledby="run-failed-title"
    >
      <header>
        <div>
          <span className="ag-card-kicker">FAILED SUBSET</span>
          <h2 id="run-failed-title">失败子集</h2>
        </div>
        <span>{items.length} 项</span>
      </header>
      <ol>
        {visibleItems.map((item, index) => (
          <li key={`${displayFailureRef(item)}-${index}`}>
            <span>#{index + 1}</span>
            <code>{displayFailureRef(item)}</code>
          </li>
        ))}
      </ol>
      {visibleItems.length < items.length && (
        <p>
          当前仅展示前 {visibleItems.length} 项，重试仍由服务端使用完整失败子集。
        </p>
      )}
    </section>
  );
}

export default function TagRunDetailPage() {
  const params = useParams();
  const queryClient = useQueryClient();
  const jobId = Number(params.id);
  const validId = Number.isSafeInteger(jobId) && jobId > 0;

  const query = useQuery({
    queryKey: ["tag-job", jobId],
    queryFn: () => getTagJob(jobId),
    enabled: validId,
    retry: false,
    refetchInterval: (currentQuery) => {
      const status = currentQuery.state.data?.status;
      return status && !TERMINAL_STATUSES.has(status) ? 3_000 : false;
    },
  });
  const retryMutation = useMutation({
    mutationFn: () => retryTagJob(jobId),
    onSuccess: (job) => {
      queryClient.setQueryData(["tag-job", jobId], job);
      queryClient.invalidateQueries({ queryKey: ["tag-job", jobId] });
    },
  });
  const cancelMutation = useMutation({
    mutationFn: () => cancelTagJob(jobId),
    onSuccess: (job) => {
      queryClient.setQueryData(["tag-job", jobId], job);
    },
  });

  if (!validId) {
    return (
      <main className="ag-tag-run-page">
        <div className="ag-governance-state is-error" role="alert">
          <strong>运行 ID 无效</strong>
          <span>请从标签洞察或治理中心重新进入运行详情。</span>
          <Link to="/tag-governance">返回标签治理</Link>
        </div>
      </main>
    );
  }

  if (query.isPending) {
    return (
      <main className="ag-tag-run-page">
        <div className="ag-governance-state" role="status">
          <span className="ag-governance-spinner" aria-hidden="true" />
          正在加载运行状态…
        </div>
      </main>
    );
  }

  if (query.isError || !query.data) {
    return (
      <main className="ag-tag-run-page">
        <div className="ag-governance-state is-error" role="alert">
          <strong>运行详情加载失败</strong>
          <span>
            {query.error instanceof Error ? query.error.message : "运行不存在"}
          </span>
          <button type="button" onClick={() => void query.refetch()}>
            重新加载
          </button>
        </div>
      </main>
    );
  }

  const job = query.data;
  const progress =
    job.total_items > 0
      ? Math.min(100, (job.completed_items / job.total_items) * 100)
      : 0;
  const failed = job.status === "failed";

  return (
    <main className="ag-tag-run-page">
      <header className="ag-run-hero">
        <div>
          <span className="ag-eyebrow">TAG EXTRACTION RUN</span>
          <h1>抽取运行 #{job.id}</h1>
          <p>
            运行范围、执行进度、失败信息和重试都绑定同一可追溯任务。
          </p>
        </div>
        <div className="ag-run-hero__actions">
          <span className={`ag-run-status is-${job.status}`}>
            {statusLabel(job.status)}
          </span>
          {!TERMINAL_STATUSES.has(job.status) && (
            <button
              type="button"
              className="is-secondary"
              disabled={cancelMutation.isPending}
              onClick={() => cancelMutation.mutate()}
            >
              {cancelMutation.isPending ? "正在取消…" : "取消运行"}
            </button>
          )}
          <Link to="/tag-governance">返回标签治理</Link>
        </div>
      </header>

      <section className="ag-run-overview" aria-label="运行概览">
        <div>
          <span>完成</span>
          <strong>
            {job.completed_items} / {job.total_items}
          </strong>
        </div>
        <div>
          <span>失败项</span>
          <strong>{job.failed_items}</strong>
        </div>
        <div>
          <span>尝试</span>
          <strong>{job.attempt_count}</strong>
        </div>
        <div className="ag-run-overview__progress">
          <span>
            总体进度 <b>{Math.round(progress)}%</b>
          </span>
          <div
            role="progressbar"
            aria-label="运行完成度"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress)}
          >
            <span
              className="ag-run-overview__progress-value"
              style={{ width: `${progress}%` }}
            />
          </div>
        </div>
      </section>

      {query.isFetching && !query.isPending && (
        <p className="ag-run-sync" role="status">
          正在同步最新检查点…
        </p>
      )}
      {retryMutation.isSuccess && (
        <p className="ag-inline-feedback is-success ag-run-feedback" role="status">
          重试任务已进入队列
        </p>
      )}
      {retryMutation.isError && (
        <p className="ag-inline-feedback is-error ag-run-feedback" role="alert">
          {retryMutation.error instanceof Error
            ? retryMutation.error.message
            : "重试失败"}
        </p>
      )}
      {cancelMutation.isSuccess && (
        <p className="ag-inline-feedback is-success ag-run-feedback" role="status">
          运行已取消
        </p>
      )}
      {cancelMutation.isError && (
        <p className="ag-inline-feedback is-error ag-run-feedback" role="alert">
          {cancelMutation.error instanceof Error
            ? cancelMutation.error.message
            : "取消失败"}
        </p>
      )}
      {(failed || job.last_error_message || job.last_error_code) && (
        <section className="ag-run-error" role="alert">
          <div aria-hidden="true">
            <IconExclamationCircleFill />
          </div>
          <span>
            <strong>{job.last_error_message || "运行发生错误"}</strong>
            <small>{job.last_error_code || "UNKNOWN_ERROR"}</small>
          </span>
          {failed && (
            // 终态 failed 恰好在尝试耗尽时产生，而服务端 retry_job 会把
            // attempt_count 归零后重新入队——人工重试正是尝试耗尽后的设计
            // 恢复路径，所以这里不能按 attempt_count 禁用按钮。
            <button
              type="button"
              disabled={retryMutation.isPending}
              onClick={() => retryMutation.mutate()}
            >
              {retryMutation.isPending
                ? "正在重试…"
                : job.attempt_count >= job.max_attempts
                  ? "重置尝试并重试"
                  : "重试运行"}
            </button>
          )}
        </section>
      )}

      {(job.failed_subset?.length ?? 0) > 0 && (
        <FailedSubset items={job.failed_subset ?? []} />
      )}

      <div className="ag-run-grid">
        <Checkpoints job={job} />
        <RunScope job={job} />
        <RunDetails job={job} />
      </div>
    </main>
  );
}
