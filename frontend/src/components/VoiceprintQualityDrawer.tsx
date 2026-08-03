/**
 * VoiceprintQualityDrawer — 声纹质量中心.
 *
 * A right-side drawer opened from the /speakers page header. Four sections:
 *   1. Pipeline status — enable_voiceprint / adapter mode (honest signal:
 *      the voiceprint pipeline may be design-spec only in this deployment).
 *   2. Sampling strategy — how candidate voiceprints are collected.
 *   3. Merge decision rules — Layer1 cosine / Layer2 fuzzy / Layer3 manual
 *      thresholds, fetched live from GET /speakers/voiceprint-policy so the
 *      UI never lies after an ops threshold change.
 *   4. Global reconfirm queue — all pending SpeakerMergePending rows for the
 *      tenant (the per-speaker card on the detail page only shows its own),
 *      plus a resolved-history tab. Inspector/admin can confirm/reject via
 *      SpeakerMergeReviewModal; viewer is read-only.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
} from "@arco-design/web-react";
import { Link } from "react-router-dom";
import dayjs from "dayjs";
import {
  listSpeakerMergePending,
  type SpeakerMergePendingListItem,
} from "@/api/advancedGraph";
import { useVoiceprintPolicy } from "@/hooks/useVoiceprintPolicy";
import { useAuthStore } from "@/stores/auth";
import {
  SpeakerMergeReviewModal,
  type MergeReviewMode,
} from "@/components/SpeakerMergeReviewModal";

const { Text } = Typography;

const STRATEGY_LABEL: Record<string, string> = {
  weighted_mean:
    "逐段提取后按时长加权平均（错分片段只占一份权重，并可被离群剔除）",
  longest_segment:
    "最长片段单次提取（按说话人取最长片段裁剪后提取一次 embedding）",
};

const PAGE_SIZE = 20;
const RESOLVED_STATUSES = ["resolved_inferred", "resolved_rejected"];

export interface VoiceprintQualityDrawerProps {
  visible: boolean;
  onClose: () => void;
}

export function VoiceprintQualityDrawer({
  visible,
  onClose,
}: VoiceprintQualityDrawerProps): JSX.Element {
  const user = useAuthStore((s) => s.user);
  const canReview = user?.role === "admin" || user?.role === "inspector";

  const policyQuery = useVoiceprintPolicy({ enabled: visible });
  const policy = policyQuery.data;

  /**
   * Pending and resolved are fetched separately with a server-side status
   * filter. One mixed page would let resolved rows crowd out older pending
   * ones — the queue would silently disagree with the header badge, which
   * counts server-side.
   */
  const [pendingPage, setPendingPage] = useState(1);
  const [resolvedPage, setResolvedPage] = useState(1);

  const pendingQuery = useQuery({
    queryKey: ["speaker-merge-pending", "global-pending", pendingPage],
    queryFn: () =>
      listSpeakerMergePending({
        status: "pending",
        limit: PAGE_SIZE,
        offset: (pendingPage - 1) * PAGE_SIZE,
      }),
    enabled: visible,
    refetchInterval: visible ? 30_000 : false,
  });
  const resolvedQuery = useQuery({
    queryKey: ["speaker-merge-pending", "global-resolved", resolvedPage],
    queryFn: () =>
      listSpeakerMergePending({
        status: RESOLVED_STATUSES,
        limit: PAGE_SIZE,
        offset: (resolvedPage - 1) * PAGE_SIZE,
      }),
    enabled: visible,
  });
  const pendingRows = pendingQuery.data?.items ?? [];
  const pendingTotal = pendingQuery.data?.total ?? 0;
  const resolvedRows = resolvedQuery.data?.items ?? [];
  const resolvedTotal = resolvedQuery.data?.total ?? 0;

  const [review, setReview] = useState<{
    mode: MergeReviewMode;
    row: SpeakerMergePendingListItem;
  } | null>(null);

  const layer1 = policy?.layer1;
  const layer2 = policy?.layer2;

  return (
    <Drawer
      visible={visible}
      onCancel={onClose}
      footer={null}
      width={560}
      title="声纹质量中心"
    >
      <Spin loading={policyQuery.isLoading} style={{ display: "block" }}>
        {policy ? (
          <Alert
            type={policy.enable_voiceprint ? "success" : "warning"}
            style={{ marginBottom: 16 }}
            title={
              policy.enable_voiceprint
                ? "声纹链路已启用"
                : "声纹链路当前未启用"
            }
            content={
              <span>
                adapter 模式：
                <Tag size="small" style={{ margin: "0 4px" }}>
                  {policy.adapter_voiceprint_mode}
                </Tag>
                {policy.enable_voiceprint
                  ? "diarization 与声纹提取按下方策略运行。"
                  : "以下采样与合并规则为设计规格，采集管线接线待落地；已有数据（如人工复核结果）仍按规则展示。"}
                {policy.retention_cascade
                  ? " DSAR/保留期擦除会级联删除声纹数据。"
                  : " 注意：声纹擦除级联当前被关闭（司法保全模式）。"}
              </span>
            }
          />
        ) : null}

        <Descriptions
          column={1}
          size="small"
          title="采样策略"
          style={{ marginBottom: 16 }}
          data={
            policy
              ? [
                  {
                    label: "候选声纹",
                    value:
                      STRATEGY_LABEL[policy.sampling.strategy] ??
                      policy.sampling.strategy,
                  },
                  {
                    label: "最短片段",
                    value: `${policy.sampling.min_segment_sec}s（更短的片段不参与声纹提取；diarization 自身丢弃 < ${policy.sampling.diarization_min_segment_sec}s）`,
                  },
                  {
                    label: "最少有效语音",
                    value: `${policy.sampling.min_total_sec}s（不足则该说话人不建立跨录音声纹）`,
                  },
                  {
                    label: "每人采样上限",
                    value: `${policy.sampling.max_segments_per_speaker} 段 / 录音，说话人上限 ${policy.sampling.max_speakers} 人 / 文件`,
                  },
                  {
                    label: "向量维度",
                    value: `${policy.sampling.embedding_dim} 维，L2 归一化（CAM++ zh-cn 16k）`,
                  },
                ]
              : []
          }
        />

        <Descriptions
          column={1}
          size="small"
          title="合并判定规则"
          style={{ marginBottom: 16 }}
          data={
            layer1 && layer2
              ? [
                  {
                    label: "Layer 1 · 声纹余弦",
                    value: (
                      <span>
                        cos ≥ {layer1.ambiguous_threshold} 直接合并；
                        {layer1.cosine_threshold} – {layer1.ambiguous_threshold}{" "}
                        合并并标记 <Tag size="small" color="gold">AMBIGUOUS</Tag>
                        （检索降权）；低于 {layer1.cosine_threshold} 新建说话人
                      </span>
                    ),
                  },
                  {
                    label: "Layer 2 · 名称模糊",
                    value: layer2.enabled ? (
                      <span>
                        fuzzy ≥ {layer2.fuzzy_inferred_threshold} 产生合并提议，进入{" "}
                        <Tag size="small" color="red">PENDING_REVIEW</Tag>
                        （检索隐藏）；声纹余弦 ≥ {layer2.voiceprint_reconfirm_cosine}{" "}
                        可自动核定，否则等待人工复核
                      </span>
                    ) : (
                      <Text type="secondary">已停用</Text>
                    ),
                  },
                  {
                    label: "Layer 3 · 人工",
                    value: "复核队列中确认 / 驳回，是唯一可改写既有说话人档案的路径",
                  },
                ]
              : []
          }
        />
      </Spin>

      <Tabs defaultActiveTab="pending">
        <Tabs.TabPane
          key="pending"
          title={`待复核（${pendingTotal}）`}
        >
          <Spin loading={pendingQuery.isLoading} style={{ display: "block" }}>
            {pendingTotal === 0 ? (
              <Text type="secondary">无待处理项</Text>
            ) : (
              <Table
                data={pendingRows}
                rowKey="id"
                size="small"
                pagination={{
                  total: pendingTotal,
                  pageSize: PAGE_SIZE,
                  current: pendingPage,
                  onChange: setPendingPage,
                  showTotal: true,
                }}
                columns={[
                  { title: "ID", dataIndex: "id", width: 70 },
                  { title: "候选名称", dataIndex: "candidate_name" },
                  {
                    title: "Fuzzy",
                    dataIndex: "fuzzy_score",
                    width: 80,
                    render: (v: number) => v.toFixed(3),
                  },
                  {
                    title: "声纹",
                    dataIndex: "voiceprint_score",
                    width: 80,
                    render: (v: number | null) =>
                      v !== null ? v.toFixed(3) : "—",
                  },
                  {
                    title: "目标",
                    dataIndex: "matched_speaker_node_id",
                    width: 90,
                    render: (v: number) => (
                      <Link to={`/speakers/${v}`}>#{v}</Link>
                    ),
                  },
                  ...(canReview
                    ? [
                        {
                          title: "操作",
                          key: "action",
                          width: 140,
                          render: (
                            _: unknown,
                            row: SpeakerMergePendingListItem,
                          ) => (
                            <>
                              <Button
                                size="mini"
                                type="primary"
                                style={{ marginRight: 8 }}
                                onClick={() =>
                                  setReview({ mode: "confirm", row })
                                }
                              >
                                确认
                              </Button>
                              <Button
                                size="mini"
                                status="danger"
                                onClick={() =>
                                  setReview({ mode: "reject", row })
                                }
                              >
                                驳回
                              </Button>
                            </>
                          ),
                        },
                      ]
                    : []),
                ]}
              />
            )}
          </Spin>
        </Tabs.TabPane>
        <Tabs.TabPane key="resolved" title={`已处理（${resolvedTotal}）`}>
          {resolvedTotal === 0 ? (
            <Text type="secondary">暂无历史记录</Text>
          ) : (
            <Table
              data={resolvedRows}
              rowKey="id"
              size="small"
              loading={resolvedQuery.isLoading}
              pagination={{
                total: resolvedTotal,
                pageSize: PAGE_SIZE,
                current: resolvedPage,
                onChange: setResolvedPage,
                showTotal: true,
              }}
              columns={[
                { title: "ID", dataIndex: "id", width: 70 },
                { title: "候选名称", dataIndex: "candidate_name" },
                {
                  title: "结果",
                  dataIndex: "status",
                  width: 110,
                  render: (v: string) =>
                    v === "resolved_inferred" ? (
                      <Tag size="small" color="green">已合并</Tag>
                    ) : (
                      <Tag size="small" color="red">已驳回</Tag>
                    ),
                },
                {
                  title: "处理时间",
                  dataIndex: "resolved_at",
                  width: 150,
                  render: (v: string | null) =>
                    v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "—",
                },
                {
                  title: "备注",
                  dataIndex: "notes",
                  render: (v: string | null) => v ?? "—",
                },
              ]}
            />
          )}
        </Tabs.TabPane>
      </Tabs>

      <SpeakerMergeReviewModal
        visible={review !== null}
        mode={review?.mode ?? "confirm"}
        row={review?.row ?? null}
        onClose={() => setReview(null)}
      />
    </Drawer>
  );
}
