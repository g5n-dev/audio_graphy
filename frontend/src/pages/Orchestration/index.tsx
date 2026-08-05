/**
 * 任务与编排 —— 流水线拓扑(画布)与治理任务台账(队列)。
 *
 * 两处与设计原型的刻意分歧,都是因为产品不做假数据:
 *
 * 1. 阶段配置只读。参数是 env 驱动的(12-factor),没有运行时改配置的接口
 *    ——所以抽屉展示的是「值 + 控制它的 env 键」,而不是一个提交后什么也
 *    不会发生的表单。
 * 2. 不画吞吐量/成本/P95。系统不采这些指标,画出来就是编的;画布只显示
 *    真实可得的:adapter 模式与队列积压。
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Table, Tag } from "@arco-design/web-react";
import { getOrchestrationTopology, listTagJobs } from "@/api/services";
import type { OrchestrationStage, TagJob } from "@/types/api";
import { PanelState } from "@/components/PanelState";
import { tagJobPollInterval } from "@/utils/tagJobs";
import { OrchestrationCanvas } from "./OrchestrationCanvas";
import "./orchestration.css";

const JOB_STATE_COLOR: Record<string, string> = {
  running: "blue",
  queued: "gray",
  completed: "green",
  succeeded: "green",
  failed: "red",
  cancelled: "gray",
};

function StageDetail({
  stage,
  onClose,
}: {
  stage: OrchestrationStage;
  onClose: () => void;
}) {
  return (
    <aside className="ag-orchestration-detail" aria-label={`${stage.name} 阶段详情`}>
      <header>
        <div>
          <span className="ag-eyebrow">STAGE · 阶段</span>
          <h2>{stage.name}</h2>
          <code>{stage.service}</code>
        </div>
        <button type="button" onClick={onClose} aria-label="关闭阶段详情">
          ✕
        </button>
      </header>
      <p className="ag-orchestration-detail__note">{stage.note}</p>

      {stage.adapter_mode === "mock" && (
        <p className="ag-inline-error" role="status">
          该阶段运行在 mock adapter 上,产出与真实语音内容无关。切换见部署指南。
        </p>
      )}

      <section>
        <h3>运行配置</h3>
        {/* 只读并说明原因:改配置要改 env 再重启,前端给个假表单是欺骗。 */}
        <p className="ag-orchestration-detail__hint">
          配置由环境变量驱动,修改后重启生效——这里展示当前值与对应的键。
        </p>
        <dl>
          {stage.config.map(([label, value, envKey]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>
                <strong>{value}</strong>
                {envKey !== "—" && <code>{envKey}</code>}
              </dd>
            </div>
          ))}
        </dl>
      </section>

      <section>
        <h3>输入</h3>
        <ul className="ag-orchestration-detail__schema">
          {stage.in_schema.map((field) => (
            <li key={field}>{field}</li>
          ))}
        </ul>
        <h3>输出</h3>
        <ul className="ag-orchestration-detail__schema" data-tone="out">
          {stage.out_schema.map((field) => (
            <li key={field}>{field}</li>
          ))}
        </ul>
      </section>
    </aside>
  );
}

export default function OrchestrationPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const topologyQuery = useQuery({
    queryKey: ["orchestration-topology"],
    queryFn: getOrchestrationTopology,
    retry: false,
  });
  const jobsQuery = useQuery({
    queryKey: ["tag-jobs"],
    queryFn: listTagJobs,
    retry: false,
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      const active = items.some(
        (job) => tagJobPollInterval(job.status) !== false,
      );
      return active ? 5_000 : false;
    },
  });

  const stages = useMemo(
    () => topologyQuery.data?.stages ?? [],
    [topologyQuery.data],
  );
  const selected = useMemo(
    () => stages.find((stage) => stage.id === selectedId) ?? null,
    [stages, selectedId],
  );
  const jobs = jobsQuery.data?.items ?? [];

  return (
    <div className="ag-orchestration-page">
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">TASKS &amp; ORCHESTRATION · 任务与编排</span>
          <h1>任务与编排</h1>
          <p>
            处理流水线的真实拓扑、各阶段的运行配置与积压,以及治理任务的执行台账。
          </p>
        </div>
      </header>

      <div className="ag-orchestration-body">
        <section className="ag-orchestration-panel">
          <header>
            <h2>数据流画布</h2>
            <span>左→右为数据流向,点节点看该阶段的配置与输入输出</span>
          </header>
          <PanelState
            pending={topologyQuery.isPending}
            error={topologyQuery.error}
            empty={stages.length === 0}
            emptyTitle="拓扑不可用"
            emptyDescription="后端未返回任何阶段。"
            onRetry={() => void topologyQuery.refetch()}
          >
            <OrchestrationCanvas
              stages={stages}
              links={topologyQuery.data?.links ?? []}
              selectedId={selectedId}
              onSelect={(stage) => setSelectedId(stage.id)}
            />
          </PanelState>
        </section>

        {selected && (
          <StageDetail stage={selected} onClose={() => setSelectedId(null)} />
        )}
      </div>

      <section className="ag-orchestration-panel">
        <header>
          <h2>治理任务台账</h2>
          <span>抽取、重算与评估任务的执行状态;非终态每 5 秒刷新</span>
        </header>
        <PanelState
          pending={jobsQuery.isPending}
          error={jobsQuery.error}
          empty={jobs.length === 0}
          emptyTitle="暂无治理任务"
          emptyDescription="标签重算、评估与优化任务会出现在这里。"
          onRetry={() => void jobsQuery.refetch()}
        >
          <Table
            rowKey="id"
            data={jobs}
            pagination={false}
            columns={[
              {
                title: "任务",
                render: (_v: unknown, job: TagJob) => (
                  <>
                    <strong>#{job.id}</strong>
                    <small style={{ display: "block", color: "#86909c" }}>
                      {job.job_type} · {job.origin}
                    </small>
                  </>
                ),
              },
              {
                title: "进度",
                render: (_v: unknown, job: TagJob) =>
                  job.total_items > 0
                    ? `${job.completed_items} / ${job.total_items}`
                    : "—",
              },
              {
                title: "尝试",
                render: (_v: unknown, job: TagJob) =>
                  `${job.attempt_count} / ${job.max_attempts}`,
              },
              {
                title: "状态",
                render: (_v: unknown, job: TagJob) => (
                  <Tag color={JOB_STATE_COLOR[job.status] ?? "gray"}>
                    {job.status}
                  </Tag>
                ),
              },
              {
                title: "操作",
                align: "right" as const,
                render: (_v: unknown, job: TagJob) => (
                  <Link to={`/tag-runs/${job.id}`}>查看详情</Link>
                ),
              },
            ]}
          />
        </PanelState>
      </section>
    </div>
  );
}
