/**
 * SpeakerProfile detail page — M7 WS-3 T12 + M9 R2 T15.
 *
 * Shows a single speaker:
 *   - Header: display name + role badge + ambiguity indicator
 *   - Voiceprint hash (truncated; PIPL: NO raw vector shown)
 *   - Aggregate stats: recordings_count / total_speech_sec / merge_confidence
 *   - Related recordings table (recording_id, strategy, ambiguity)
 *   - "跨录音关系" mini-view — a tiny G6 subgraph showing this speaker
 *     and its recordings (visual only, no interactivity in M7 skeleton).
 *
 * M9 R2 T15 additions:
 *   - "Pending fuzzy merges" card showing SpeakerMergePending rows
 *     targeting this speaker (L8 reconfirm work-queue). Inspector/admin
 *     can confirm or reject inline.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams, useNavigate, Link } from "react-router-dom";
import {
  Button,
  Card,
  Descriptions,
  Spin,
  Table,
  Tag,
  Typography,
} from "@arco-design/web-react";
import dayjs from "dayjs";
import { IconArrowLeft } from "@arco-design/web-react/icon";
import { getSpeaker } from "@/api/speakers";
import {
  listSpeakerMergePending,
  type SpeakerMergePendingListItem,
} from "@/api/advancedGraph";
import { PanelState } from "@/components/PanelState";
import { SpeakerBadge } from "@/components/SpeakerBadge";
import {
  SpeakerMergeReviewModal,
  type MergeReviewMode,
} from "@/components/SpeakerMergeReviewModal";
import { useVoiceprintPolicy } from "@/hooks/useVoiceprintPolicy";
import { useAuthStore } from "@/stores/auth";
import { getErrorMessage } from "@/utils/errors";
import { buildFocusParam } from "@/utils/urlParams";

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

  const policyQuery = useVoiceprintPolicy();
  const ambiguousRange = policyQuery.data
    ? {
        low: policyQuery.data.layer1.cosine_threshold,
        high: policyQuery.data.layer1.ambiguous_threshold,
      }
    : undefined;

  if (isLoading) {
    return (
      <div style={{ padding: 24, textAlign: "center" }}>
        <Spin size={32} />
      </div>
    );
  }

  if (error || !data) {
    // Show what the backend said rather than always claiming the speaker is
    // missing: a permission or transport failure reads very differently from
    // a deleted record, and the operator can only tell if we say which.
    return (
      <div style={{ padding: 24 }}>
        <Title heading={5}>说话人不存在或加载失败</Title>
        <Text type="secondary" style={{ display: "block", margin: "8px 0 16px" }}>
          {error ? getErrorMessage(error) : "未返回该说话人的数据。"}
        </Text>
        <Button onClick={() => navigate("/speakers")}>返回列表</Button>
      </div>
    );
  }

  return (
    <div>
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">SPEAKER PROFILE · 说话人详情</span>
          <h1>
            {data.display_name}
            <SpeakerBadge
              role={data.speaker_role}
              ambiguity={data.ambiguity_tag}
              ambiguousRange={ambiguousRange}
            />
          </h1>
          <p>声纹、角色、合并策略与关联录音一览。SpeakerNode #{data.id} · tenant={data.tenant_id}</p>
        </div>
        <div className="ag-feature-header__actions">
          <Button
            type="text"
            icon={<IconArrowLeft />}
            onClick={() => navigate("/speakers")}
            style={{ marginRight: 8, color: "var(--ag-text-secondary)" }}
          >
            返回列表
          </Button>
          <Link to={`/graph?focus=${buildFocusParam("speaker", speakerId)}`}>
            在图谱中查看
          </Link>
        </div>
      </header>

      <div style={{ padding: 24 }}>
      <Card style={{ marginBottom: 16 }}>
        <Descriptions
          column={2}
          style={{ marginTop: 16 }}
          data={[
            {
              label: "声纹Hash",
              value: (
                <code style={{ fontSize: 14 }}>{data.voiceprint_hash}</code>
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
                <code style={{ fontSize: 14, color: "#86909c" }}>{val}</code>
              ),
            },
            {
              title: "链接策略",
              dataIndex: "strategy",
              width: 140,
            },
            {
              title: "声纹余弦",
              dataIndex: "cosine_similarity",
              width: 110,
              render: (val: number | null) =>
                val !== null && val !== undefined ? val.toFixed(3) : "—",
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

      <PendingMergesCard speakerId={data.id} />
      </div>
    </div>
  );
}

/**
 * PendingMergesCard — M9 R2 T15.
 *
 * Lists SpeakerMergePending rows whose ``matched_speaker_node_id``
 * equals this speaker. Inspector/admin confirm or reject through
 * SpeakerMergeReviewModal (notes + optional voiceprint_score);
 * viewer sees the rows read-only.
 */
function PendingMergesCard({ speakerId }: { speakerId: number }): JSX.Element {
  const user = useAuthStore((s) => s.user);
  const canReview = user?.role === "admin" || user?.role === "inspector";
  const pendingQuery = useQuery({
    queryKey: ["speaker-merge-pending", "by-speaker", speakerId],
    queryFn: () =>
      // Filtered server-side: a client-side filter over one capped page
      // hides this speaker's older rows once the tenant queue grows.
      listSpeakerMergePending({
        status: "pending",
        matched_speaker_node_id: speakerId,
        limit: 50,
      }),
    refetchInterval: 30_000,
  });

  const rows = pendingQuery.data?.items ?? [];

  const [review, setReview] = useState<{
    mode: MergeReviewMode;
    row: SpeakerMergePendingListItem;
  } | null>(null);

  return (
    <Card title="待确认的模糊合并 (L8 reconfirm queue)" style={{ marginTop: 16 }}>
      {/* A rejected request must not read as 无待处理项: this queue is how a
          wrong automatic merge gets caught, so the failure has to be visible
          and retryable instead of silently empty. */}
      <PanelState
        pending={pendingQuery.isLoading}
        error={pendingQuery.error}
        empty={rows.length === 0}
        emptyTitle="无待处理项"
        emptyDescription="该说话人当前没有等待复核的模糊合并。"
        onRetry={() => void pendingQuery.refetch()}
        pendingLabel="正在加载待复核队列…"
      >
        <Table
          data={rows}
          rowKey="id"
          size="small"
          pagination={false}
          columns={[
            { title: "Pending ID", dataIndex: "id", width: 100 },
            {
              title: "Candidate name",
              dataIndex: "candidate_name",
            },
            {
              title: "Fuzzy score",
              dataIndex: "fuzzy_score",
              render: (v: number) => (
                <Tag color="arc-orange">{v.toFixed(3)}</Tag>
              ),
            },
            {
              title: "Voiceprint",
              dataIndex: "voiceprint_score",
              width: 100,
              render: (v: number | null) => (v !== null ? v.toFixed(3) : "—"),
            },
            {
              title: "Recording",
              dataIndex: "recording_id",
              width: 110,
            },
            ...(canReview
              ? [
                  {
                    title: "Action",
                    key: "action",
                    width: 180,
                    render: (
                      _: unknown,
                      row: SpeakerMergePendingListItem,
                    ) => (
                      <>
                        <Button
                          size="mini"
                          type="primary"
                          style={{ marginRight: 8 }}
                          onClick={() => setReview({ mode: "confirm", row })}
                        >
                          Confirm
                        </Button>
                        <Button
                          size="mini"
                          status="danger"
                          onClick={() => setReview({ mode: "reject", row })}
                        >
                          Reject
                        </Button>
                      </>
                    ),
                  },
                ]
              : []),
          ]}
        />
      </PanelState>
      <SpeakerMergeReviewModal
        visible={review !== null}
        mode={review?.mode ?? "confirm"}
        row={review?.row ?? null}
        targetSpeakerId={speakerId}
        onClose={() => setReview(null)}
      />
    </Card>
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
        fontSize: 14,
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
