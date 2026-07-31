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
import { useQuery } from "@tanstack/react-query";
import { useParams, useSearchParams } from "react-router-dom";
import {
  Descriptions,
  Card,
  Table,
  Tabs,
  Tag,
  Typography,
  Spin,
} from "@arco-design/web-react";
import {
  IconBranch,
  IconClockCircle,
  IconMessage,
  IconUser,
} from "@arco-design/web-react/icon";
import { getRecording, getSegments, getTags } from "@/api/services";
import type { SegmentResponse } from "@/types/api";
import { OutLinkCard } from "@/components/OutLinkCard";
import { parseAtParam, buildFocusParam } from "@/utils/urlParams";

const { Title } = Typography;
const { TabPane } = Tabs;

export default function RecordingDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const recordingId = Number(id);

  const { data: recording, isLoading: loadingRec } = useQuery({
    queryKey: ["recording", recordingId],
    queryFn: () => getRecording(recordingId),
    enabled: !!recordingId,
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
          <OutLinkCard
            icon={<IconMessage />}
            title="接待"
            description="跳转到此录音所属的接待工作台，查看完整接待流程与状态路径分析。"
            to={`/receptions?focus=${buildFocusParam("录音", recording.id)}`}
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
