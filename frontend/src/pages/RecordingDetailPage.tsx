/**
 * Recording detail page — shows segments + tags for a single recording.
 *
 * GD-004 (graph drilldown closed loop): 4 export tabs added after
 * the existing segments/tags tabs — 图谱关系 / 说话人 / 接待 / 时间演化.
 * Each renders an OutLinkCard entry point rather than embedding the
 * full page.
 *
 * GD-010: consumes `?at=<ms>` URL param — highlights the matching
 * segment row and scrolls it into view.
 */

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useSearchParams } from "react-router-dom";
import {
  Button,
  Descriptions,
  Card,
  Message,
  Table,
  Tabs,
  Tag,
  Typography,
  Spin,
} from "@arco-design/web-react";
import {
  IconBranch,
  IconCheckCircleFill,
  IconClockCircle,
  IconMessage,
  IconUser,
} from "@arco-design/web-react/icon";
import {
  getRecording,
  getRecordingProcessingRun,
  getSegments,
  getTags,
  reindexRecording,
} from "@/api/services";
import type { PipelineRunResponse, SegmentResponse } from "@/types/api";
import { OutLinkCard } from "@/components/OutLinkCard";
import { useAuthStore } from "@/stores/auth";
import { getErrorMessage } from "@/utils/errors";
import { parseAtParam, buildFocusParam } from "@/utils/urlParams";

const { Title } = Typography;
const { TabPane } = Tabs;

// Human-readable names for pipeline projections (required vs completed).
const projectionLabelMap: Record<string, string> = {
  file_index: "文件索引",
  vector: "向量索引",
  graph: "图谱投影",
};

// Pipeline run states that mean the run is still moving forward.
const RUN_ACTIVE_STATES = new Set([
  "queued",
  "claimed",
  "vad",
  "asr",
  "segments",
  "chunks",
  "projections",
  "verifying",
]);

// Pipeline run states that mean the run stopped without producing an index.
const RUN_FAILED_STATES = new Set([
  "partial",
  "failed_retryable",
  "failed_terminal",
]);

const runStateLabelMap: Record<string, string> = {
  queued: "排队中",
  claimed: "已认领",
  vad: "语音检测",
  asr: "语音识别",
  segments: "分段",
  chunks: "切块",
  projections: "投影写入",
  verifying: "校验中",
  ready: "完成",
  ready_no_speech: "完成（无语音）",
  partial: "部分完成",
  failed_retryable: "失败（可重试）",
  failed_terminal: "失败（终止）",
  superseded: "已被取代",
};

/**
 * Pipeline progress panel — visible whenever the recording has an active
 * or failed run, so the operator can see which stage the ingest is at and
 * why it failed instead of staring at a status word.
 */
function PipelinePanel({
  run,
  recordingStatus,
  isAdmin,
  onRetry,
  retryPending,
}: {
  run: PipelineRunResponse;
  recordingStatus: string;
  isAdmin: boolean;
  onRetry: (force: boolean) => void;
  retryPending: boolean;
}) {
  const failed = RUN_FAILED_STATES.has(run.state) || recordingStatus === "failed";
  const active = !failed && RUN_ACTIVE_STATES.has(run.state);
  const required = run.required_projections;
  const completed = new Set(run.completed_projections);

  return (
    <Card
      style={{ marginBottom: 16 }}
      title={failed ? "处理失败" : "处理进行中"}
      extra={
        isAdmin ? (
          failed ? (
            <Button
              size="small"
              type="primary"
              status="danger"
              loading={retryPending}
              onClick={() => onRetry(false)}
            >
              重试处理
            </Button>
          ) : active ? (
            <Button
              size="small"
              type="outline"
              loading={retryPending}
              // force=true breaks a stale lease so a stuck run can requeue.
              onClick={() => onRetry(true)}
            >
              强制重跑
            </Button>
          ) : null
        ) : null
      }
    >
      <div role={failed ? "alert" : "status"}>
        <div style={{ marginBottom: 12 }}>
          <Tag color={failed ? "red" : "blue"}>
            {runStateLabelMap[run.state] ?? run.state}
          </Tag>
          <span style={{ marginLeft: 8, color: "#86909c", fontSize: 13 }}>
            第 {run.generation} 代 · 第 {run.attempt_count} 次尝试
          </span>
        </div>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
          {required.map((projection) => {
            const done = completed.has(projection);
            return (
              <span
                key={projection}
                style={{ color: done ? "#00b42a" : "#86909c", fontSize: 13 }}
              >
                {done ? (
                  <IconCheckCircleFill style={{ marginRight: 4 }} />
                ) : (
                  <IconClockCircle style={{ marginRight: 4 }} />
                )}
                {projectionLabelMap[projection] ?? projection}
              </span>
            );
          })}
        </div>
        {failed && (
          <div style={{ marginTop: 12 }}>
            <Typography.Text type="error">
              错误代码：{run.error_code ?? "未知"}
            </Typography.Text>
            <Typography.Paragraph
              type="error"
              style={{ marginTop: 4, marginBottom: 0 }}
            >
              {run.error_message ?? "后端未返回错误详情，请查看服务端日志。"}
            </Typography.Paragraph>
          </div>
        )}
      </div>
    </Card>
  );
}

export default function RecordingDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const recordingId = Number(id);
  const queryClient = useQueryClient();
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === "admin";

  const { data: recording, isLoading: loadingRec } = useQuery({
    queryKey: ["recording", recordingId],
    queryFn: () => getRecording(recordingId),
    enabled: !!recordingId,
    // While the pipeline is still working, refresh so the page flips to
    // indexed (and the panel disappears) without a manual reload.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "processing" ? 5000 : false;
    },
  });

  const activeRunId = recording?.active_pipeline_run_id ?? null;
  const pipelineRelevant =
    recording?.status === "queued" ||
    recording?.status === "processing" ||
    recording?.status === "failed";

  const { data: pipelineRun } = useQuery({
    queryKey: ["recording", recordingId, "processing-run", activeRunId],
    queryFn: () => getRecordingProcessingRun(recordingId, activeRunId as number),
    enabled: !!recordingId && activeRunId !== null && pipelineRelevant,
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      return state === undefined || RUN_ACTIVE_STATES.has(state) ? 4000 : false;
    },
  });

  const reindexMutation = useMutation({
    mutationFn: (force: boolean) => reindexRecording(recordingId, { force }),
    onSuccess: () => {
      Message.success("已重新排队处理");
      // Refresh both the list and this detail so polling restarts on the
      // new run immediately.
      void queryClient.invalidateQueries({ queryKey: ["recordings"] });
      void queryClient.invalidateQueries({ queryKey: ["recording", recordingId] });
    },
    onError: (error) => {
      Message.error(getErrorMessage(error, "重新处理失败"));
    },
  });

  const { data: segments, isLoading: loadingSeg } = useQuery({
    queryKey: ["segments", recordingId],
    queryFn: () => getSegments(recordingId, { page: 1, page_size: 100 }),
    enabled: !!recordingId,
  });

  const { data: tags } = useQuery({
    queryKey: ["tags", recordingId],
    queryFn: () => getTags(recordingId, "current"),
    enabled: !!recordingId,
  });

  // GD-010: consume `?at=<ms>` → highlight matching segment row
  const atMs = parseAtParam(searchParams.get("at"));
  const atSec = atMs !== null ? atMs / 1000 : null;
  const [highlightSegId, setHighlightSegId] = useState<number | null>(null);
  const scrollDoneRef = useRef(false);

  useEffect(() => {
    if (atSec === null || !segments?.items) return;
    const match = segments.items.find(
      (seg) => seg.start_sec <= atSec && atSec <= seg.end_sec,
    );
    if (match) {
      setHighlightSegId(match.id);
    }
  }, [atSec, segments]);

  useEffect(() => {
    if (highlightSegId === null || scrollDoneRef.current) return;
    const timer = window.setTimeout(() => {
      const row = document.querySelector(
        `tr[data-row-key="${highlightSegId}"]`,
      ) as HTMLElement | null;
      if (row) {
        row.scrollIntoView({ behavior: "smooth", block: "center" });
        scrollDoneRef.current = true;
      }
    }, 200);
    return () => window.clearTimeout(timer);
  }, [highlightSegId]);

  if (loadingRec) {
    return (
      <div style={{ padding: 48, textAlign: "center" }}>
        <Spin size={40} />
      </div>
    );
  }

  if (!recording) {
    return (
      <div style={{ padding: 48, textAlign: "center" }}>
        <Title heading={5}>录音未找到</Title>
      </div>
    );
  }

  return (
    <div>
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">RECORDING DETAIL · 录音详情</span>
          <h1>录音 #{recording.id}</h1>
          <p>查看分段转写、标签、图谱关系与说话人信息。</p>
        </div>
      </header>

      <div style={{ padding: 24 }}>
      {pipelineRun && pipelineRelevant && (
        <PipelinePanel
          run={pipelineRun}
          recordingStatus={recording.status}
          isAdmin={isAdmin}
          onRetry={(force) => reindexMutation.mutate(force)}
          retryPending={reindexMutation.isPending}
        />
      )}
      <Card style={{ marginBottom: 16 }}>
        <Descriptions
          column={3}
          data={[
            { label: "门店ID", value: recording.store_id },
            { label: "坐席", value: recording.agent_name },
            { label: "状态", value: <Tag>{recording.status}</Tag> },
            { label: "管线状态", value: recording.pipeline_state },
            { label: "录音标识", value: `#${recording.id}` },
            { label: "提示词版本", value: recording.prompt_version ?? "-" },
            {
              label: "录制时间",
              value: recording.recorded_at
                ? new Date(recording.recorded_at).toLocaleString("zh-CN")
                : "-",
            },
            {
              label: "索引时间",
              value: recording.indexed_at
                ? new Date(recording.indexed_at).toLocaleString("zh-CN")
                : "-",
            },
            { label: "分段数", value: recording.segments_count },
          ]}
        />
      </Card>

      <Tabs>
        <TabPane key="segments" title={`分段 (${segments?.total ?? 0})`}>
          <Table
            data={segments?.items ?? []}
            rowKey="id"
            loading={loadingSeg}
            size="small"
            pagination={{ pageSize: 20, showTotal: true }}
            onRow={(record: SegmentResponse) => {
              if (highlightSegId === record.id) {
                return { style: { background: "#e8f3ff" } };
              }
              return {};
            }}
            columns={[
              { title: "#", dataIndex: "idx", width: 50 },
              {
                title: "开始",
                dataIndex: "start_sec",
                width: 80,
                render: (v: number) => `${v.toFixed(2)}s`,
              },
              {
                title: "结束",
                dataIndex: "end_sec",
                width: 80,
                render: (v: number) => `${v.toFixed(2)}s`,
              },
              { title: "说话人", dataIndex: "speaker", width: 80 },
              { title: "转写", dataIndex: "transcript" },
            ]}
          />
        </TabPane>
        <TabPane key="tags" title={`标签 (${tags?.tags.length ?? 0})`}>
          <Table
            data={tags?.tags ?? []}
            rowKey={(row) => `${row.tag_path}-${row.version}`}
            size="small"
            pagination={false}
            columns={[
              { title: "标签路径", dataIndex: "tag_path" },
              {
                title: "标签值",
                dataIndex: "tag_value",
                width: 120,
                render: (v: string) => (
                  <Tag
                    color={
                      v === "pass" ? "green" : v === "fail" ? "red" : "blue"
                    }
                  >
                    {v}
                  </Tag>
                ),
              },
              { title: "版本", dataIndex: "version", width: 60 },
              { title: "提示词版本", dataIndex: "prompt_version", width: 120 },
            ]}
          />
        </TabPane>

        {/* ── GD-004: 4 export tabs (entry cards, not embedded pages) ── */}

        <TabPane
          key="graph-relation"
          title={
            <span>
              <IconBranch style={{ marginRight: 4 }} />
              图谱关系
            </span>
          }
        >
          <OutLinkCard
            icon={<IconBranch />}
            title="图谱关系"
            description="查看此录音的实体关系图谱，了解涉及的客户、产品、竞品等实体及其关联。"
            to={`/graph?focus=${buildFocusParam("录音", recording.id)}`}
          />
        </TabPane>

        <TabPane
          key="speakers"
          title={
            <span>
              <IconUser style={{ marginRight: 4 }} />
              说话人
            </span>
          }
        >
          <OutLinkCard
            icon={<IconUser />}
            title="说话人"
            description="查看参与此录音的说话人画像，包括声纹信息、发言统计与跨录音关系。"
            to={`/speakers?focus=${buildFocusParam("录音", recording.id)}`}
          />
        </TabPane>

        <TabPane
          key="reception"
          title={
            <span>
              <IconMessage style={{ marginRight: 4 }} />
              接待
            </span>
          }
        >
          {/* 接待列表暂无按录音筛选的服务端能力，直接带录音焦点跳转会落在
              一个无法生效的筛选上；改为带门店焦点进入队列，由录制时间定位。 */}
          <OutLinkCard
            icon={<IconMessage />}
            title="接待"
            description={`接待队列暂不支持按录音直接筛选。点击后将进入门店 ${recording.store_id} 的接待队列，请结合录制时间定位此录音所属的接待。`}
            to={`/receptions?focus=${buildFocusParam("门店", recording.store_id)}`}
          />
        </TabPane>

        <TabPane
          key="time-evolution"
          title={
            <span>
              <IconClockCircle style={{ marginRight: 4 }} />
              时间演化
            </span>
          }
        >
          <OutLinkCard
            icon={<IconClockCircle />}
            title="时间演化"
            description="查看此录音的标签时间演化历史，追踪知识图谱中边的新增、过期与取代。"
            to={`/time-travel?recording=${recording.id}`}
          />
        </TabPane>
      </Tabs>
      </div>
    </div>
  );
}
