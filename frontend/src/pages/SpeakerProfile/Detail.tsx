/**
 * SpeakerProfile detail page — M7 WS-3 T12.
 *
 * Shows a single speaker:
 *   - Header: display name + role badge + ambiguity indicator
 *   - Voiceprint hash (truncated; PIPL: NO raw vector shown)
 *   - Aggregate stats: recordings_count / total_speech_sec / merge_confidence
 *   - Related recordings table (recording_id, strategy, ambiguity)
 *   - "跨录音关系" mini-view — a tiny G6 subgraph showing this speaker
 *     and its recordings (visual only, no interactivity in M7 skeleton).
 */

import { useQuery } from "@tanstack/react-query";
import { useParams, useNavigate } from "react-router-dom";
import {
  Button,
  Card,
  Descriptions,
  Spin,
  Table,
  Typography,
} from "@arco-design/web-react";
import dayjs from "dayjs";
import { IconArrowLeft } from "@arco-design/web-react/icon";
import { getSpeaker } from "@/api/speakers";
import { SpeakerBadge } from "@/components/SpeakerBadge";

const { Title, Text } = Typography;

export default function SpeakerProfileDetailPage(): JSX.Element {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const speakerId = Number(id);

  const { data, isLoading, error } = useQuery({
    queryKey: ["speaker", speakerId],
    queryFn: () => getSpeaker(speakerId),
    enabled: !!speakerId && !Number.isNaN(speakerId),
  });

  if (isLoading) {
    return (
      <div style={{ padding: 24, textAlign: "center" }}>
        <Spin size={32} />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div style={{ padding: 24 }}>
        <Title heading={5}>说话人不存在或加载失败</Title>
        <Button onClick={() => navigate("/speakers")}>返回列表</Button>
      </div>
    );
  }

  return (
    <div style={{ padding: 24 }}>
      <div style={{ marginBottom: 16 }}>
        <Button
          type="text"
          icon={<IconArrowLeft />}
          onClick={() => navigate("/speakers")}
          style={{ marginRight: 8 }}
        >
          返回列表
        </Button>
      </div>

      <Card style={{ marginBottom: 16 }}>
        <Title heading={4} style={{ marginBottom: 4 }}>
          {data.display_name}
          <SpeakerBadge
            role={data.speaker_role}
            ambiguity={data.ambiguity_tag}
          />
        </Title>
        <Text type="secondary" style={{ fontSize: 13 }}>
          SpeakerNode #{data.id} · tenant={data.tenant_id}
        </Text>

        <Descriptions
          column={2}
          style={{ marginTop: 16 }}
          data={[
            {
              label: "声纹Hash",
              value: (
                <code style={{ fontSize: 13 }}>{data.voiceprint_hash}</code>
              ),
            },
            {
              label: "录音数",
              value: data.recordings_count,
            },
            {
              label: "首次出现",
              value: data.first_seen
                ? dayjs(data.first_seen).format("YYYY-MM-DD HH:mm")
                : "-",
            },
            {
              label: "累计发言时长",
              value: `${data.total_speech_sec.toFixed(1)}s`,
            },
            {
              label: "合并策略",
              value: data.merge_strategy,
            },
            {
              label: "合并置信度",
              value: data.merge_confidence.toFixed(3),
            },
          ]}
        />
      </Card>

      <Card title="关联录音" style={{ marginBottom: 16 }}>
        <Table
          data={data.related_recordings}
          rowKey="recording_id"
          size="small"
          pagination={{ pageSize: 10 }}
          onRow={(record) => ({
            onClick: () => navigate(`/recordings/${record.recording_id}`),
            style: { cursor: "pointer" },
          })}
          columns={[
            {
              title: "录音ID",
              dataIndex: "recording_id",
              width: 100,
            },
            {
              title: "Voiceprint",
              dataIndex: "voiceprint_id",
              width: 140,
              render: (val: string) => (
                <code style={{ fontSize: 12, color: "#86909c" }}>{val}</code>
              ),
            },
            {
              title: "链接策略",
              dataIndex: "strategy",
              width: 140,
            },
            {
              title: "Ambiguity",
              dataIndex: "ambiguity_tag",
              render: (val: string | null) => val ?? "—",
            },
          ]}
        />
      </Card>

      <Card title="跨录音关系（mini-view）">
        <CrossRecordingMiniView
          speakerId={data.id}
          recordings={data.recordings_list}
        />
      </Card>
    </div>
  );
}

/**
 * CrossRecordingMiniView — visual placeholder showing the speaker's
 * relationship to its recordings as a simple textual graph.
 *
 * M7 ships this as a non-interactive skeleton; M8 will swap in a full
 * G6 visualization with circle nodes (speaker=blue/orange/gray by role,
 * AMBIGUOUS=dashed border) connected to per-recording nodes.
 */
function CrossRecordingMiniView({
  speakerId,
  recordings,
}: {
  speakerId: number;
  recordings: number[];
}): JSX.Element {
  return (
    <div
      style={{
        padding: 16,
        background: "#f7f8fa",
        borderRadius: 4,
        fontFamily: "monospace",
        fontSize: 13,
      }}
    >
      <div>
        <span
          style={{
            display: "inline-block",
            width: 12,
            height: 12,
            background: "#165dff",
            borderRadius: "50%",
            marginRight: 6,
          }}
        />
        Speaker #{speakerId}
      </div>
      <div style={{ marginTop: 8, paddingLeft: 18 }}>
        {recordings.length === 0 ? (
          <Text type="secondary">无关联录音</Text>
        ) : (
          recordings.map((recId) => (
            <div key={recId}>
              <span style={{ color: "#86909c" }}>└─</span>{" "}
              <a
                href={`#/recordings/${recId}`}
                style={{ color: "#165dff" }}
              >
                Recording #{recId}
              </a>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
