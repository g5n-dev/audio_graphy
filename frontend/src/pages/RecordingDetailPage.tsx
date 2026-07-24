/**
 * Recording detail page — shows segments + tags for a single recording.
 */

import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { Descriptions, Card, Table, Tabs, Tag, Typography, Spin } from "@arco-design/web-react";
import { getRecording, getSegments, getTags } from "@/api/services";

const { Title } = Typography;
const { TabPane } = Tabs;

export default function RecordingDetailPage() {
  const { id } = useParams<{ id: string }>();
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
    <div style={{ padding: 24 }}>
      <Title heading={4} style={{ marginBottom: 16 }}>
        录音详情 #{recording.id}
      </Title>

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
            { label: "录制时间", value: recording.recorded_at ? new Date(recording.recorded_at).toLocaleString("zh-CN") : "-" },
            { label: "索引时间", value: recording.indexed_at ? new Date(recording.indexed_at).toLocaleString("zh-CN") : "-" },
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
                  <Tag color={v === "pass" ? "green" : v === "fail" ? "red" : "blue"}>
                    {v}
                  </Tag>
                ),
              },
              { title: "版本", dataIndex: "version", width: 60 },
              { title: "提示词版本", dataIndex: "prompt_version", width: 120 },
            ]}
          />
        </TabPane>
      </Tabs>
    </div>
  );
}
